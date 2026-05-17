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
    "ignoreerrors": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "cookies": "cookies.txt",
    "geo_bypass": True,
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    },
    "extractor_args": {
        "youtube": {
            "skip": ["dash", "hls"],
            "player_client": ["web", "android", "ios"],
        }
    },
    "retries": 5,
    "fragment_retries": 5,
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
        data = await loop.run_in_executor(None, lambda: cls.ytdl.extract_info(url, download=False))
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
        return cls(discord.FFmpegPCMAudio(data["url"], **FFMPEG_OPTIONS), data=data)

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
            print(f"[ERROR] {error}")
        player = self.get_player(guild.id)
        source = player.next()
        if source and guild.voice_client and guild.voice_client.is_connected():
            guild.voice_client.play(source, after=lambda e: self._after(guild, e))
            if guild.voice_client.source:
                guild.voice_client.source.volume = player.volume

    async def spotify_to_youtube(self, url: str):
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            return url, None
        try:
            sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET))
            if "track" in url:
                track = sp.track(url)
                return f"ytsearch:{track['name']} {track['artists'][0]['name']}", track
            return url, None
        except:
            return url, None

    @app_commands.command(name="play", description="Phát nhạc từ YouTube hoặc Spotify")
    @app_commands.describe(query="Link hoặc tên bài hát")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send(
                embed=discord.Embed(description="❌ Bạn cần vào kênh thoại trước!", color=0xFF4444)
            )

        guild = interaction.guild
        channel = interaction.user.voice.channel
        vc = guild.voice_client

        try:
            if vc is None:
                vc = await channel.connect(timeout=15, reconnect=True)
            elif vc.channel != channel:
                await vc.move_to(channel)
        except Exception as e:
            return await interaction.followup.send(embed=discord.Embed(description=f"Lỗi kết nối: {e}", color=0xFF4444))

        player = self.get_player(guild.id)

        if "spotify.com" in query.lower():
            query, _ = await self.spotify_to_youtube(query)

        if not re.match(r"https?://", query):
            query = f"ytsearch:{query}"

        try:
            sources = await YTDLSource.from_url(query, loop=self.bot.loop)
        except Exception as e:
            err = str(e).lower()
            if "drm" in err:
                tip = "❌ Bài hát này bị DRM Protection, không phát được."
            elif "cookies" in err or "sign in" in err:
                tip = "❌ Lỗi cookies. Kiểm tra file cookies.txt"
            else:
                tip = f"❌ Lỗi: {str(e)[:150]}"
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
                embed.add_field(name="Uploader", value=source.uploader)
                if source.thumbnail:
                    embed.set_thumbnail(url=source.thumbnail)
                await interaction.followup.send(embed=embed)
                return

        # Added to queue
        added = sources[0]
        embed = discord.Embed(title="✅ Đã thêm vào Queue", description=f"[{added.title}]({added.url})", color=EMBED_COLOR)
        embed.add_field(name="Duration", value=YTDLSource.fmt_dur(added.duration))
        await interaction.followup.send(embed=embed)

    # ================== Các lệnh khác ==================
    @app_commands.command(name="join", description="Bot vào kênh thoại")
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message(embed=discord.Embed(description="❌ Vào kênh thoại trước!", color=0xFF4444), ephemeral=True)
        
        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        try:
            if vc is None:
                await channel.connect(timeout=15, reconnect=True)
                await interaction.response.send_message(embed=discord.Embed(description="✅ Bot đã vào kênh!", color=EMBED_COLOR))
            elif vc.channel != channel:
                await vc.move_to(channel)
                await interaction.response.send_message(embed=discord.Embed(description="✅ Đã chuyển kênh!", color=EMBED_COLOR))
            else:
                await interaction.response.send_message(embed=discord.Embed(description="Bot đã ở trong kênh.", color=EMBED_COLOR), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(embed=discord.Embed(description=f"Lỗi: {e}", color=0xFF4444))

    @app_commands.command(name="skip", description="Bỏ qua bài hát")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message(embed=discord.Embed(description="⏭️ Skipped!", color=EMBED_COLOR))
        else:
            await interaction.response.send_message(embed=discord.Embed(description="Không có bài đang phát.", color=0xFF4444), ephemeral=True)

    @app_commands.command(name="stop", description="Dừng nhạc và rời kênh")
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

    @app_commands.command(name="resume", description="Tiếp tục")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message(embed=discord.Embed(description="▶️ Resumed!", color=EMBED_COLOR))

    @app_commands.command(name="loop", description="Treo bài hiện tại")
    async def loop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        player.loop = not player.loop
        if player.loop: player.loop_queue = False
        status = "✅ Bật Treo bài" if player.loop else "❌ Tắt Treo"
        await interaction.response.send_message(embed=discord.Embed(description=status, color=EMBED_COLOR))

# ====================== RUN ======================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

@bot.event
async def on_ready():
    print(f"[✅] Bot online: {bot.user}")
    try:
        await bot.add_cog(MusicCog(bot))
        await bot.tree.sync()
        print("[✅] Commands synced.")
    except Exception as e:
        print(f"[❌] Sync error: {e}")

    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=BOT_STATUS))

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ Chưa set DISCORD_TOKEN!")
        exit(1)
    bot.run(BOT_TOKEN)