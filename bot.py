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
EMBED_COLOR = 0x1DB954

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "cookiefile": "cookies.txt",
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    },
    "extractor_args": {
        "youtube": {
            "player_client": ["web", "android"],
            "skip": ["dash", "hls"],
        }
    },
    "retries": 10,
    "geo_bypass": True,
    "geo_bypass_country": "VN",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class YTDLSource(discord.PCMVolumeTransformer):
    ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

    @classmethod
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: cls.ytdl.extract_info(url, download=False))
        
        if "entries" in data:
            entries = [e for e in data["entries"] if e]
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
        if not sec: return "Live"
        m, s = divmod(int(sec), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    def get_player(self, guild_id):
        if guild_id not in self.players:
            self.players[guild_id] = {"queue": deque(), "current": None, "loop": False}
        return self.players[guild_id]

    @app_commands.command(name="play", description="Phát nhạc")
    @app_commands.describe(query="Link hoặc tên bài hát")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)   # Giữ defer

        if not interaction.user.voice:
            return await interaction.followup.send("❌ Bạn phải vào voice channel trước!", ephemeral=True)

        # Kết nối voice
        vc = interaction.guild.voice_client
        if not vc:
            vc = await interaction.user.voice.channel.connect()

        player = self.get_player(interaction.guild.id)

        try:
            # Tối ưu query
            if not re.match(r"https?://", query):
                query = f"ytsearch:{query}"

            sources = await YTDLSource.from_url(query)
            
            if not sources:
                return await interaction.followup.send("❌ Không tìm thấy bài hát.")

            # Thêm vào queue
            for source in sources:
                player["queue"].append(source)

            # Phát ngay nếu chưa có nhạc
            if not vc.is_playing() and not vc.is_paused():
                source = player["queue"].popleft()
                player["current"] = source
                vc.play(source, after=lambda e: self.after_playing(interaction.guild))

                embed = discord.Embed(title="🎵 Đang phát", description=f"**{source.title}**", color=EMBED_COLOR)
                embed.add_field(name="Thời lượng", value=YTDLSource.fmt_dur(source.duration))
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(f"✅ Đã thêm vào queue: **{sources[0].title}**")

        except Exception as e:
            print(f"Error: {e}")
            await interaction.followup.send(f"❌ Lỗi: {str(e)[:200]}", ephemeral=True)

    def after_playing(self, guild):
        player = self.get_player(guild.id)
        if player["queue"]:
            source = player["queue"].popleft()
            player["current"] = source
            guild.voice_client.play(source, after=lambda e: self.after_playing(guild))

    # Các lệnh khác (skip, stop...)
    @app_commands.command(name="skip", description="Skip bài hiện tại")
    async def skip(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏭️ Skipped!", ephemeral=True)
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.stop()


intents = discord.Intents.default()
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Bot online: {bot.user}")
    if os.path.exists("cookies.txt"):
        print("✅ Cookies.txt đã load")
    await bot.add_cog(MusicCog(bot))
    await bot.tree.sync()

bot.run(BOT_TOKEN)