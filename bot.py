import asyncio
import os
import re
import traceback
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# ====================== CONFIG ======================
BOT_TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

COMMAND_PREFIX = "!"
EMBED_COLOR = 0x1DB954
BOT_STATUS = "/bé bo"

# ===================================================

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "cookies": "cookies.txt",                    # ← Quan trọng để fix lỗi Sign in
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    },
    "extractor_args": {
        "youtube": {
            "skip": ["dash", "hls"],
            "player_client": ["android", "web", "ios"],
        }
    },
    "retries": 5,
    "fragment_retries": 5,
    "skip_unavailable_fragments": True,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -hide_banner -loglevel error",
    "options": "-vn -bufsize 512k",
}


class YTDLSource(discord.PCMVolumeTransformer):
    ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title", "Unknown")
        self.url = data.get("webpage_url", data.get("url", ""))
        self.duration = data.get("duration", 0)
        self.thumbnail = data.get("thumbnail", "")
        self.uploader = data.get("uploader", "Unknown")

    @classmethod
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: cls.ytdl.extract_info(url, download=False)
        )
        if data is None:
            raise ValueError("Không thể lấy thông tin bài hát.")

        if "entries" in data:
            entries = [e for e in data["entries"] if e]
            if not entries:
                raise ValueError("Playlist trống.")
            return [cls._make(e) for e in entries]
        return [cls._make(data)]

    @classmethod
    def _make(cls, data):
        return cls(
            discord.FFmpegPCMAudio(data["url"], **FFMPEG_OPTIONS),
            data=data,
        )

    @staticmethod
    def fmt_dur(sec):
        if not sec:
            return "Live"
        m, s = divmod(int(sec), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class MusicPlayer:
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.queue = deque()
        self.current = None
        self.loop = False
        self.loop_queue = False
        self.volume = 0.5

    def add(self, sources):
        self.queue.extend(sources)

    def next(self):
        if self.loop and self.current:
            return self.current
        if self.loop_queue and self.current:
            self.queue.append(self.current)
        if self.queue:
            self.current = self.queue.popleft()
            return self.current
        self.current = None
        return None

    def clear(self):
        self.queue.clear()
        self.current = None


class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    def get_player(self, guild_id):
        if guild_id not in self.players:
            self.players[guild_id] = MusicPlayer(guild_id)
        return self.players[guild_id]

    def _after(self, guild, error=None):
        if error:
            print(f"[ERROR] Playback error: {error}")
        player = self.get_player(guild.id)
        source = player.next()
        if source and guild.voice_client and guild.voice_client.is_connected():
            guild.voice_client.play(source, after=lambda e: self._after(guild, e))
            if guild.voice_client.source:
                guild.voice_client.source.volume = player.volume

    # ===================== SPOTIFY =====================
    async def spotify_to_youtube(self, url: str):
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            return url, None
        try:
            sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET
            ))
            
            if "track" in url:
                track = sp.track(url)
                query = f"{track['name']} {track['artists'][0]['name']}"
                return f"ytsearch:{query}", track
            elif "playlist" in url or "album" in url:
                return url, None  # yt-dlp sẽ xử lý trực tiếp
        except Exception as e:
            print(f"Spotify Error: {e}")
        return url, None

    # ===================== PLAY =====================
    @app_commands.command(name="play", description="Phát nhạc từ YouTube hoặc Spotify")
    @app_commands.describe(query="Link YouTube/Spotify hoặc tên bài hát")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send(
                embed=discord.Embed(description="❌ Bạn cần vào kênh thoại trước!", color=0xFF4444)
            )

        guild = interaction.guild
        vc = guild.voice_client
        vc_channel = interaction.user.voice.channel

        try:
            if vc is None:
                vc = await vc_channel.connect(timeout=15, reconnect=True)
            elif vc.channel != vc_channel:
                await vc.move_to(vc_channel)
        except Exception as e:
            return await interaction.followup.send(
                embed=discord.Embed(description=f"Lỗi kết nối: {e}", color=0xFF4444)
            )

        player = self.get_player(guild.id)
        original_query = query

        # Xử lý Spotify
        if "spotify.com" in query.lower():
            query, _ = await self.spotify_to_youtube(query)

        if not re.match(r"https?://", query):
            query = f"ytsearch:{query}"

        try:
            sources = await YTDLSource.from_url(query, loop=self.bot.loop)
        except Exception as e:
            error_msg = str(e)
            if "Sign in to confirm" in error_msg or "cookies" in error_msg:
                tip = "❌ Lỗi cookies YouTube. Hãy upload file `cookies.txt` lên Railway."
            elif "403" in error_msg:
                tip = "❌ YouTube chặn tạm thời. Thử lại sau."
            else:
                tip = f"❌ Không thể phát: {error_msg[:200]}"
            return await interaction.followup.send(embed=discord.Embed(description=tip, color=0xFF4444))

        player.add(sources)

        if not vc.is_playing() and not vc.is_paused():
            source = player.next()
            if source:
                vc.play(source, after=lambda e: self._after(guild, e))
                if vc.source:
                    vc.source.volume = player.volume

                embed = discord.Embed(title="🎵 Now Playing", description=f"[{source.title}]({source.url})", color=EMBED_COLOR)
                embed.add_field(name="Duration", value=YTDLSource.fmt_dur(source.duration))
                embed.add_field(name="Channel", value=source.uploader)
                if source.thumbnail:
                    embed.set_thumbnail(url=source.thumbnail)
                embed.set_footer(text=f"Requested by {interaction.user.display_name}")
                return await interaction.followup.send(embed=embed)

        # Added to queue
        added = sources[0]
        embed = discord.Embed(title="✅ Added to Queue", description=f"[{added.title}]({added.url})", color=EMBED_COLOR)
        embed.add_field(name="Duration", value=YTDLSource.fmt_dur(added.duration))
        embed.add_field(name="Position", value=f"#{len(player.queue)}")
        if len(sources) > 1:
            embed.add_field(name="Playlist", value=f"Đã thêm {len(sources)} bài", inline=False)
        if added.thumbnail:
            embed.set_thumbnail(url=added.thumbnail)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="join", description="Bot vào kênh thoại")
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message(embed=discord.Embed(description="❌ Bạn cần vào kênh thoại trước!", color=0xFF4444), ephemeral=True)

        vc_channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        try:
            if vc is None:
                await vc_channel.connect(timeout=15, reconnect=True)
                await interaction.response.send_message(embed=discord.Embed(description="✅ Bot đã vào kênh thoại!", color=EMBED_COLOR))
            elif vc.channel != vc_channel:
                await vc.move_to(vc_channel)
                await interaction.response.send_message(embed=discord.Embed(description="✅ Bot đã chuyển kênh!", color=EMBED_COLOR))
            else:
                await interaction.response.send_message(embed=discord.Embed(description="Bot đã ở trong kênh rồi.", color=EMBED_COLOR), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(embed=discord.Embed(description=f"Lỗi: {e}", color=0xFF4444), ephemeral=True)

    @app_commands.command(name="loop", description="Bật/Tắt treo bài hiện tại")
    async def loop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        player.loop = not player.loop
        if player.loop:
            player.loop_queue = False
        status = "✅ **TREO** bài hiện tại" if player.loop else "❌ Đã tắt treo"
        await interaction.response.send_message(embed=discord.Embed(description=status, color=EMBED_COLOR))

    @app_commands.command(name="loopqueue", description="Bật/Tắt treo toàn bộ hàng chờ")
    async def loopqueue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        player.loop_queue = not player.loop_queue
        if player.loop_queue:
            player.loop = False
        status = "✅ **TREO HÀNG CHỜ**" if player.loop_queue else "❌ Đã tắt treo hàng chờ"
        await interaction.response.send_message(embed=discord.Embed(description=status, color=EMBED_COLOR))

    @app_commands.command(name="skip", description="Bỏ qua bài hiện tại")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message(embed=discord.Embed(description="⏭️ Skipped!", color=EMBED_COLOR))
        else:
            await interaction.response.send_message(embed=discord.Embed(description="Không có bài nào đang phát.", color=0xFF4444), ephemeral=True)

    @app_commands.command(name="stop", description="Dừng và rời kênh")
    async def stop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        player.clear()
        vc = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
        await interaction.response.send_message(embed=discord.Embed(description="⏹️ Đã dừng và rời kênh.", color=EMBED_COLOR))

    @app_commands.command(name="pause", description="Tạm dừng")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message(embed=discord.Embed(description="⏸️ Paused.", color=EMBED_COLOR))
        else:
            await interaction.response.send_message(embed=discord.Embed(description="Không có gì đang phát.", color=0xFF4444), ephemeral=True)

    @app_commands.command(name="resume", description="Tiếp tục phát")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message(embed=discord.Embed(description="▶️ Resumed!", color=EMBED_COLOR))
        else:
            await interaction.response.send_message(embed=discord.Embed(description="Bot không đang tạm dừng.", color=0xFF4444), ephemeral=True)

    @app_commands.command(name="queue", description="Xem danh sách chờ")
    async def queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        embed = discord.Embed(title="📜 Queue", color=EMBED_COLOR)
        
        if player.current:
            loop_status = " | 🔁 Treo bài" if player.loop else " | 🔁 Treo queue" if player.loop_queue else ""
            embed.add_field(
                name="Now Playing",
                value=f"[{player.current.title}]({player.current.url}) `{YTDLSource.fmt_dur(player.current.duration)}`{loop_status}",
                inline=False
            )
        
        if player.queue:
            lines = [f"`{i}.` [{s.title}]({s.url}) `{YTDLSource.fmt_dur(s.duration)}`" 
                    for i, s in enumerate(list(player.queue)[:15], 1)]
            if len(player.queue) > 15:
                lines.append(f"...và {len(player.queue)-15} bài nữa")
            embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
        
        if not player.current and not player.queue:
            embed.description = "Queue trống."
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="volume", description="Điều chỉnh âm lượng (0-200)")
    @app_commands.describe(level="Mức âm lượng")
    async def volume(self, interaction: discord.Interaction, level: int):
        if not 0 <= level <= 200:
            return await interaction.response.send_message(embed=discord.Embed(description="Âm lượng phải từ 0 đến 200!", color=0xFF4444), ephemeral=True)
        
        player = self.get_player(interaction.guild.id)
        player.volume = level / 100.0
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = player.volume
        await interaction.response.send_message(embed=discord.Embed(description=f"🔊 Volume: **{level}%**", color=EMBED_COLOR))

    @app_commands.command(name="nowplaying", description="Thông tin bài đang phát")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        if not player.current:
            return await interaction.response.send_message(embed=discord.Embed(description="Không có bài nào đang phát.", color=0xFF4444), ephemeral=True)

        s = player.current
        embed = discord.Embed(title="🎵 Now Playing", description=f"[{s.title}]({s.url})", color=EMBED_COLOR)
        embed.add_field(name="Duration", value=YTDLSource.fmt_dur(s.duration))
        embed.add_field(name="Channel", value=s.uploader)
        if s.thumbnail:
            embed.set_thumbnail(url=s.thumbnail)
        await interaction.response.send_message(embed=embed)


# ====================== BOT SETUP ======================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

@bot.event
async def on_ready():
    print(f"[✅] Bot đã online: {bot.user}")
    try:
        await bot.add_cog(MusicCog(bot))
        synced = await bot.tree.sync()
        print(f"[✅] Đã sync {len(synced)} slash commands.")
    except Exception as e:
        print(f"[❌] Sync lỗi: {e}")
        traceback.print_exc()

    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name=BOT_STATUS)
    )

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("[❌] Chưa thiết lập DISCORD_TOKEN!")
        exit(1)
    bot.run(BOT_TOKEN)