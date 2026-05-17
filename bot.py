import asyncio
import os
import re
import traceback
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

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
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    },
    "extractor_args": {
        "youtube": {
            "skip": ["dash", "hls"],
            "player_client": ["android", "web"],
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
        self.url = data.get("webpage_url", "")
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
            print(f"[ERROR] {error}")
        player = self.get_player(guild.id)
        source = player.next()
        if source and guild.voice_client and guild.voice_client.is_connected():
            guild.voice_client.play(source, after=lambda e: self._after(guild, e))
            guild.voice_client.source.volume = player.volume

    # ===================== SPOTIFY HANDLER =====================
    async def spotify_to_youtube(self, spotify_url: str):
        """Chuyển link Spotify thành query YouTube"""
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyClientCredentials
            
            sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET
            ))
            
            if "track" in spotify_url:
                track = sp.track(spotify_url)
                query = f"{track['name']} {track['artists'][0]['name']}"
                return f"ytsearch:{query}", track
                
            elif "playlist" in spotify_url:
                playlist = sp.playlist(spotify_url)
                return playlist['external_urls']['spotify'], playlist  # trả về link gốc để yt_dlp xử lý
                
            elif "album" in spotify_url:
                album = sp.album(spotify_url)
                return album['external_urls']['spotify'], album
                
        except Exception as e:
            print(f"Spotify Error: {e}")
        return spotify_url, None

    # ===================== PLAY COMMAND =====================
    @app_commands.command(name="play", description="Phát nhạc từ YouTube hoặc Spotify")
    @app_commands.describe(query="Link YouTube/Spotify hoặc tên bài hát")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send(
                embed=discord.Embed(description="Bạn cần vào kênh thoại trước!", color=0xFF4444)
            )

        vc_channel = interaction.user.voice.channel
        guild = interaction.guild
        vc = guild.voice_client

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
        spotify_data = None

        # Xử lý Spotify
        if "spotify.com" in query:
            query, spotify_data = await self.spotify_to_youtube(query)

        if not re.match(r"https?://", query):
            query = f"ytsearch:{query}"

        try:
            sources = await YTDLSource.from_url(query, loop=self.bot.loop)
        except Exception as e:
            msg = str(e)
            if "403" in msg or "forbidden" in msg.lower():
                tip = "Lỗi 403: YouTube chặn. Thử lại sau hoặc dùng link khác."
            else:
                tip = f"Không thể phát: {msg[:150]}"
            return await interaction.followup.send(
                embed=discord.Embed(description=tip, color=0xFF4444)
            )

        player.add(sources)

        if not vc.is_playing() and not vc.is_paused():
            source = player.next()
            if source:
                vc.play(source, after=lambda e: self._after(guild, e))
                vc.source.volume = player.volume

                embed = discord.Embed(
                    title="Now Playing",
                    description=f"[{source.title}]({source.url})",
                    color=EMBED_COLOR,
                )
                embed.add_field(name="Duration", value=YTDLSource.fmt_dur(source.duration))
                embed.add_field(name="Channel", value=source.uploader)
                if source.thumbnail:
                    embed.set_thumbnail(url=source.thumbnail)
                embed.set_footer(text=f"Requested by {interaction.user.display_name}")
                return await interaction.followup.send(embed=embed)

        # Added to queue
        added = sources[0]
        embed = discord.Embed(
            title="Added to Queue",
            description=f"[{added.title}]({added.url})",
            color=EMBED_COLOR,
        )
        embed.add_field(name="Duration", value=YTDLSource.fmt_dur(added.duration))
        embed.add_field(name="Position", value=f"#{len(player.queue)}")
        if len(sources) > 1:
            embed.add_field(name="Playlist", value=f"Đã thêm {len(sources)} bài", inline=False)
        if added.thumbnail:
            embed.set_thumbnail(url=added.thumbnail)
        await interaction.followup.send(embed=embed)


# ===================== CÁC COMMAND KHÁC GIỮ NGUYÊN =====================
    # (Các command join, loop, skip, stop, pause, resume, queue, volume, nowplaying, leave giữ nguyên như cũ)

    @app_commands.command(name="join", description="Bot tham gia kênh thoại")
    async def join(self, interaction: discord.Interaction):
        # ... (giữ nguyên code cũ của bạn)
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message(
                embed=discord.Embed(description="Bạn cần vào kênh thoại trước!", color=0xFF4444), ephemeral=True
            )

        vc_channel = interaction.user.voice.channel
        guild = interaction.guild
        vc = guild.voice_client

        try:
            if vc is None:
                await vc_channel.connect(timeout=15, reconnect=True)
                await interaction.response.send_message(embed=discord.Embed(description="✅ Bot đã vào kênh!", color=EMBED_COLOR))
            elif vc.channel != vc_channel:
                await vc.move_to(vc_channel)
                await interaction.response.send_message(embed=discord.Embed(description="✅ Bot đã chuyển kênh!", color=EMBED_COLOR))
            else:
                await interaction.response.send_message(embed=discord.Embed(description="Bot đã ở trong kênh rồi.", color=EMBED_COLOR), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(embed=discord.Embed(description=f"Lỗi: {e}", color=0xFF4444), ephemeral=True)

    # ... (Bạn có thể copy lại phần còn lại của class MusicCog từ file cũ)

# ====================== SETUP ======================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

@bot.event
async def on_ready():
    print(f"[OK] Bot online: {bot.user}")
    try:
        await bot.add_cog(MusicCog(bot))
        synced = await bot.tree.sync()
        print(f"[OK] Synced {len(synced)} commands.")
    except Exception as e:
        print(f"[ERROR] Sync failed: {e}")

    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name=BOT_STATUS)
    )

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("[ERROR] Chưa set DISCORD_TOKEN!")
        exit(1)
    bot.run(BOT_TOKEN)