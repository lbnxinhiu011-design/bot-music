import asyncio
import os
import re
import traceback
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_TOKEN")
COMMAND_PREFIX = "!"
EMBED_COLOR = 0x1DB954
BOT_STATUS = "/bé bo"

# ================== YTDL OPTIONS MỚI - TỐI ƯU COOKIES ==================
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "cookiefile": "cookies.txt",                    # Bắt buộc phải có file này
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8",
        "Referer": "https://www.youtube.com/",
    },
    "extractor_args": {
        "youtube": {
            "player_client": ["web", "android", "ios", "web_embedded", "android_embedded"],
            "skip": ["dash", "hls"],
        }
    },
    "retries": 20,
    "fragment_retries": 20,
    "skip_unavailable_fragments": True,
    "geo_bypass": True,
    "geo_bypass_country": "VN",
    "age_limit": 0,
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
        try:
            data = await loop.run_in_executor(
                None, lambda: cls.ytdl.extract_info(url, download=False)
            )
        except Exception as e:
            print(f"[YTDL Error] {e}")
            raise

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

    @app_commands.command(name="play", description="Phát nhạc từ link hoặc tên bài")
    @app_commands.describe(query="Link YouTube hoặc tên bài hát")
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
            return await interaction.followup.send(embed=discord.Embed(description=f"Lỗi kết nối: {e}", color=0xFF4444))

        player = self.get_player(guild.id)

        if not re.match(r"https?://", query):
            query = f"ytsearch:{query}"

        try:
            sources = await YTDLSource.from_url(query, loop=self.bot.loop)
        except Exception as e:
            msg = str(e)
            if "Sign in to confirm" in msg or "403" in msg:
                tip = "❌ YouTube chặn cookies.\nHãy export cookies.txt **mới nhất** và deploy lại."
            else:
                tip = f"❌ Lỗi: {msg[:250]}"
            return await interaction.followup.send(embed=discord.Embed(description=tip, color=0xFF4444))

        player.add(sources)

        if not vc.is_playing() and not vc.is_paused():
            source = player.next()
            if source:
                vc.play(source, after=lambda e: self._after(guild, e))
                vc.source.volume = player.volume
                
                embed = discord.Embed(title="Now Playing", description=f"[{source.title}]({source.url})", color=EMBED_COLOR)
                embed.add_field(name="Thời lượng", value=YTDLSource.fmt_dur(source.duration))
                embed.add_field(name="Kênh", value=source.uploader)
                if source.thumbnail:
                    embed.set_thumbnail(url=source.thumbnail)
                embed.set_footer(text=f"Requested by {interaction.user.display_name}")
                return await interaction.followup.send(embed=embed)

        # Thêm vào queue
        added = sources[0]
        embed = discord.Embed(title="Đã thêm vào hàng chờ", description=f"[{added.title}]({added.url})", color=EMBED_COLOR)
        embed.add_field(name="Thời lượng", value=YTDLSource.fmt_dur(added.duration))
        await interaction.followup.send(embed=embed)


# ====================== CÁC COMMAND KHÁC (GỬI NGẮN GỌN) ======================
    # (Bạn có thể copy phần command cũ join, skip, stop, loop... từ file cũ vào đây)

    @app_commands.command(name="skip", description="Bỏ qua bài hiện tại")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("⏭️ Đã skip!", ephemeral=True)

    @app_commands.command(name="stop", description="Dừng nhạc và rời kênh")
    async def stop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        player.clear()
        vc = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
        await interaction.response.send_message("🛑 Đã dừng và rời kênh.", ephemeral=True)


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


@bot.event
async def on_ready():
    print(f"[OK] Bot online: {bot.user}")
    
    if os.path.exists("cookies.txt"):
        size = os.path.getsize("cookies.txt") / 1024
        print(f"✅ Đã load cookies.txt ({size:.1f} KB)")
    else:
        print("❌ Không tìm thấy cookies.txt!")

    try:
        await bot.add_cog(MusicCog(bot))
        await bot.tree.sync()
        print("[OK] Slash commands synced.")
    except Exception as e:
        print(f"[ERROR] {e}")

    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=BOT_STATUS))


if __name__ == "__main__":
    bot.run(BOT_TOKEN)