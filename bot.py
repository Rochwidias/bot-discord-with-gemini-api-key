import discord
from discord.ext import commands
from discord import app_commands
from google import genai
from datetime import datetime
import yt_dlp
import asyncio
import time
import sys
import os
import platform 
import random
from dotenv import load_dotenv
from db import get_supabase

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# --- ATURAN MAIN (HARAP GANTI KREDENSIAL INI) ---
TOKEN = os.getenv('TOKEN_DISCORD_BOT')
GEMINI_API_KEY = os.getenv('TOKEN_GEMINI_API')
GEMINI_MODEL_NAME = 'gemini-3.1-flash-lite' 

SYSTEM_PROMPT = (
    "Asisten yang dikembangkan oleh @rennsh. "
    "Wajib menggunakan emoji"
    "menggunakan bahasa sopan"
    "respon dengan rapih terstruktur tetapi jelas"
    "respon dengan efisien"
    "maksimal ketikan 1800"
)

play_attempts = {}
idle_timers = {}      # {guild_id: timestamp} — kapan mulai idle
volumes = {}          # {guild_id: float 0.0-2.0}

# Inisialisasi API Google Gemini
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- INSTANSIASI DATA GLOBAL ---
queues = {}             # Antrian lagu: {guild_id: [song_dict, ...]}
history_queues = {}     # Riwayat lagu untuk Undo: {guild_id: [song_dict], ...}
current_song = {}       # Lagu yang sedang diputar: {guild_id: song_dict}
active_channels = {}    # Channel teks dan voice aktif: {guild_id: {'text': id, 'voice': id}}
player_messages = {}    # ID pesan panel utama musik: {guild_id: message_id}
repeat_status = {}      # Status repeat per server: {guild_id: boolean}

# --- FUNGSI UTILITAS UTAMA ---
def simpan_log(username, pesan, balasan, engine="AI"):
    sb = get_supabase()
    if not sb:
        return
    try:
        sb.table("chat_logs").insert({
            "username": username,
            "pesan": pesan,
            "balasan": balasan,
            "engine": engine
        }).execute()
    except Exception as e:
        print(f"Gagal menyimpan log ke Supabase: {e}")

# --- SPOTIFY (via yt-dlp, tanpa API key) ---
import re as _re

async def cari_spotify(query):
    m = _re.match(r"(?:https?://)?(?:open\.)?spotify\.com/(track|playlist|album)/([a-zA-Z0-9]+)", query.strip())
    if not m:
        return None, "Link Spotify tidak valid"
    tipe, sid = m.group(1), m.group(2)
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
        if not data:
            return None, "Gagal ambil data dari Spotify"
        items = []
        if 'entries' in data:
            for e in data['entries']:
                if e:
                    items.append({"title": e.get("title", ""), "artist": e.get("artist", "") or e.get("uploader", "")})
        else:
            items.append({"title": data.get("title", ""), "artist": data.get("artist", "") or data.get("uploader", "")})
        if not items or not items[0]["title"]:
            return None, "Gak dapet info lagu dari link itu"
        return items, None
    except Exception as e:
        return None, str(e)

def bersihkan_ffmpeg():
    """Menghentikan proses ffmpeg yang berjalan di background."""
    try:
        if platform.system() == "Windows": 
            os.system("taskkill /F /IM ffmpeg.exe >nul 2>&1")
        else: 
            os.system("pkill -9 ffmpeg >/dev/null 2>&1")
    except Exception as e:
        print(f"Error cleaning FFmpeg process: {e}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if not interaction.response.is_done(): 
        await interaction.response.send_message("Waduh, lagi ada gangguan internal nih.", ephemeral=True)

def get_queue(guild_id):
    if guild_id not in queues: queues[guild_id] = []
    return queues[guild_id]

def format_duration(seconds):
    if not seconds: return "00:00"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def get_elapsed_time(guild_id):
    curr = current_song.get(guild_id)
    if not curr: return 0
    offset = curr.get('seek_offset', 0)
    start = curr.get('start_time')
    if start and isinstance(start, (int, float)): 
        return offset + (time.time() - start)
    return offset 

# --- STATUS BOT ---
async def update_bot_presence():
    curr = next(iter(current_song.values()), None)
    if curr:
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=curr['title']
        ))
    else:
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.playing, 
            name="Realms of Wumpus"
        ))

# --- PANEL EMED & PEMBERSIHAN DOOBEL MESSAGE ---
def build_player_embed(guild_id):
    curr = current_song.get(guild_id)
    q = get_queue(guild_id)
    rep = "Aktif 🔁" if repeat_status.get(guild_id, False) else "Nonaktif"
    
    if not curr:
        return discord.Embed(title="🛑 Musik Berhenti", description="Antrian kosong. Tambahkan lagu dengan `/play`", color=discord.Color.red())
        
    elapsed = get_elapsed_time(guild_id)
    dur = curr.get('duration', 0)
    
    embed = discord.Embed(title="🎶 Sedang Memutar", description=f"**{curr['title']}**", color=discord.Color.blue())
    progress_text = f"`[{format_duration(elapsed)} / {format_duration(dur)}]`"
    embed.add_field(name="Durasi", value=progress_text, inline=True)
    embed.add_field(name="Repeat", value=f"`{rep}`", inline=True)
    
    if q:
        up_next = "\n".join([f"`{i+1}.` {s['title']}" for i, s in enumerate(q[:3])])
        if len(q) > 3: up_next += f"\n*...dan {len(q)-3} lainnya*"
        embed.add_field(name="📜 Selanjutnya", value=up_next, inline=False)
        
    return embed

async def update_player_message(guild_id, text_channel, resend=False):
    embed = build_player_embed(guild_id)
    view = MusicControlView()
    
    async for message in text_channel.history(limit=10):
        if message.author == bot.user and message.id != player_messages.get(guild_id):
            try: await message.delete()
            except discord.Forbidden: pass 

    old_msg_id = player_messages.get(guild_id)
    
    if resend or not old_msg_id:
        if old_msg_id:
            try:
                old_msg = await text_channel.fetch_message(old_msg_id)
                await old_msg.delete()
            except Exception as e: 
                print(f"Failed to delete old message: {e}")
        new_msg = await text_channel.send(embed=embed, view=view)
        player_messages[guild_id] = new_msg.id
    else:
        try:
            old_msg = await text_channel.fetch_message(old_msg_id)
            await old_msg.edit(embed=embed, view=view)
        except Exception as e:
            print(f"Failed to edit message {old_msg_id}: {e}. Re-sending.")
            new_msg = await text_channel.send(embed=embed, view=view)
            player_messages[guild_id] = new_msg.id

# --- TOMBOL INTERAKTIF UI (discord.ui.View) ---
class MusicControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="btn_playpause")
    async def btn_playpause(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        g_id = interaction.guild.id
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.followup.send("⚠️ Mohon berada di Voice Channel.", ephemeral=True)
            return

        curr = current_song.get(g_id)
        if not curr:
            await interaction.followup.send("❌ Tidak ada lagu yang diputar.", ephemeral=True)
            return

        if vc.is_paused():
            vc.resume()
            if curr: curr['start_time'] = time.time()
            await interaction.followup.send("⏯️ Melanjutkan pemutaran.")
        elif vc.is_playing():
            vc.pause()
            current_elapsed = get_elapsed_time(g_id)
            if curr: 
                curr['seek_offset'] = current_elapsed
                curr['start_time'] = None 
            await interaction.followup.send("⏸️ Dijeda.")
        save_state_all()
        await update_player_message(g_id, interaction.channel)

    @discord.ui.button(label="Undo", emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="btn_undo")
    async def btn_undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        g_id = interaction.guild.id
        hq = history_queues.get(g_id, [])
        if not hq:
            await interaction.followup.send("❌ Gak ada lagu sebelumnya!", ephemeral=True)
            return
        
        prev = hq.pop() 
        curr = current_song.get(g_id)

        if curr: 
            curr['seek'] = 0 
            queues[g_id].insert(0, curr)
        prev['seek'] = 0 
        queues[g_id].insert(0, prev)

        vc = interaction.guild.voice_client
        if vc and vc.is_playing(): vc.stop() 
        else: await play_next(g_id, interaction.channel)

        await interaction.followup.send("⏪ Undo berhasil! Memutar ulang lagu sebelumnya.")

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="btn_skip")
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        g_id = interaction.guild.id
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing(): 
            await interaction.followup.send("⚠️ Tidak ada musik yang diputar.", ephemeral=True)
            return

        vc.stop()
        await asyncio.sleep(1) 
        await play_next(g_id, interaction.channel)
        await interaction.followup.send("⏭️ Melewati lagu.")

    @discord.ui.button(label="Random", emoji="🔀", style=discord.ButtonStyle.success, custom_id="btn_random")
    async def btn_random(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        g_id = interaction.guild.id
        q = get_queue(g_id)
        if len(q) > 1:
            random.shuffle(q)
            save_state_all()
            await update_player_message(g_id, interaction.channel)
            await interaction.followup.send("🔀 Antrian lagu berhasil diacak!")
        else:
            await interaction.followup.send("❌ Antrian kurang dari 2 lagu untuk diacak.", ephemeral=True)

    @discord.ui.button(label="Repeat", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="btn_repeat")
    async def btn_repeat(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        g_id = interaction.guild.id
        repeat_status[g_id] = not repeat_status.get(g_id, False)
        save_state_all()
        await update_player_message(g_id, interaction.channel)
        status = "Aktif" if repeat_status[g_id] else "Nonaktif"
        await interaction.followup.send(f"🔁 Repeat mode sekarang: **{status}**.")

    @discord.ui.button(label="List", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="btn_list")
    async def btn_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        g_id = interaction.guild.id
        q = get_queue(g_id)
        if not q:
            await interaction.response.send_message("Antrian kosong.", ephemeral=True)
            return
        teks = "**📜 Daftar Antrian Lengkap:**\n"
        for i, s in enumerate(q, 1): teks += f"`{i}.` {s['title']} `[{format_duration(s['duration'])}]`\n"
        await interaction.response.send_message(teks[:2000], ephemeral=True)

# --- SAVE & RESTORE STATE VIA SUPABASE ---
def save_state_all():
    sb = get_supabase()
    all_guilds = set(queues.keys()).union(set(current_song.keys()))
    for guild_id in all_guilds:
        q = get_queue(guild_id)
        curr = current_song.get(guild_id)
        hq = history_queues.get(guild_id, [])
        if not curr and not q and guild_id not in active_channels: continue

        row = {
            "guild_id": guild_id,
            "queue": q,
            "history": hq,
            "repeat": repeat_status.get(guild_id, False),
            "volume": volumes.get(guild_id, 0.5),
            "text_channel_id": active_channels.get(guild_id, {}).get('text'),
            "voice_channel_id": active_channels.get(guild_id, {}).get('voice'),
            "player_message_id": player_messages.get(guild_id)
        }
        if curr:
            curr_copy = curr.copy()
            current_elapsed = get_elapsed_time(guild_id)
            curr_copy['seek'] = current_elapsed
            row["current_song"] = curr_copy

        if sb:
            try:
                sb.table("guild_states").upsert(row, on_conflict="guild_id").execute()
            except Exception as e:
                print(f"Gagal menyimpan state guild {guild_id} ke Supabase: {e}")

async def state_saver_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = time.time()

        # 1. Save state
        if current_song or queues:
            save_state_all()
            for g_id, curr in list(current_song.items()):
                if g_id in active_channels and curr and curr.get('start_time') is not None:
                    tc = bot.get_channel(active_channels[g_id].get('text'))
                    if tc: 
                        await update_player_message(g_id, tc, resend=False)

        # 2. Auto-leave & RAM cleanup
        cleanup_guilds = set()
        for g_id in list(active_channels.keys()):
            guild = bot.get_guild(g_id)
            if not guild:
                cleanup_guilds.add(g_id)
                continue
            vc = guild.voice_client
            if not vc:
                cleanup_guilds.add(g_id)
                continue
            if vc.is_playing() or vc.is_paused():
                idle_timers.pop(g_id, None)
            else:
                q = get_queue(g_id)
                curr = current_song.get(g_id)
                if not q and not curr:
                    if g_id not in idle_timers:
                        idle_timers[g_id] = now
                    elif now - idle_timers[g_id] >= 120:
                        print(f"[{g_id}] Idle >2 menit, leave otomatis.")
                        vc.stop()
                        await vc.disconnect()
                        cleanup_guilds.add(g_id)

        for g_id in cleanup_guilds:
            queues.pop(g_id, None)
            history_queues.pop(g_id, None)
            current_song.pop(g_id, None)
            active_channels.pop(g_id, None)
            player_messages.pop(g_id, None)
            repeat_status.pop(g_id, None)
            play_attempts.pop(g_id, None)
            idle_timers.pop(g_id, None)
            volumes.pop(g_id, None)
        if cleanup_guilds:
            save_state_all()
            print(f"🧹 Cleanup {len(cleanup_guilds)} guild idle")

        await update_bot_presence()
        await asyncio.sleep(10)

# --- ENGINE PEMUTAR LAGU & AUTOPLAY ---
if platform.system() == "Windows":
    _ffmpeg_test = os.path.join(BASE_DIR, "bin", "ffmpeg", "ffmpeg.exe")
else:
    _ffmpeg_test = "ffmpeg"
try:
    import subprocess
    subprocess.run([_ffmpeg_test, "-version"], capture_output=True, check=True)
    print(f"✅ FFmpeg siap: {_ffmpeg_test}")
except Exception:
    print(f"⚠️ FFmpeg tidak ditemukan di: {_ffmpeg_test}")

ytdl_format_options = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'nocheckcertificate': True,
    'quiet': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'geo_bypass': True,
    'extractor_args': {'youtube': {'player_client': ['android']}},
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}
COOKIES_FILE = os.path.join(BASE_DIR, "youtube_cookies.txt")
_cookies_env = os.getenv("YOUTUBE_COOKIES")
if _cookies_env:
    with open(COOKIES_FILE, "w") as f:
        f.write(_cookies_env)
    ytdl_format_options['cookiefile'] = COOKIES_FILE
    print("✅ Cookies YouTube dimuat dari YOUTUBE_COOKIES env")
elif os.path.exists(COOKIES_FILE):
    ytdl_format_options['cookiefile'] = COOKIES_FILE
    print("✅ Cookies YouTube dimuat dari file lokal")

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

async def play_next(guild_id: int, text_channel=None):
    queue = get_queue(guild_id)
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client: 
        print(f"[{guild_id}] Gagal play_next karena tidak ada Voice Client.")
        return
    vc = guild.voice_client
    
    curr = current_song.get(guild_id)
    
    if curr:
        if repeat_status.get(guild_id, False):
            curr['seek'] = 0 
            get_queue(guild_id).insert(0, curr)
        else:
            if guild_id not in history_queues: history_queues[guild_id] = []
            history_queues[guild_id].append(curr)
            if len(history_queues[guild_id]) > 10: history_queues[guild_id].pop(0)

    if not queue and curr and not repeat_status.get(guild_id, False):
        try:
            data = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(f"ytsearch:{curr['title']}", download=False))
            if 'entries' in data and data['entries']:
                entry = data['entries'][0]
                queue.append({'webpage_url': entry['webpage_url'], 'title': entry['title'], 'duration': entry['duration'], 'seek': 0})
        except Exception as e:
            print(f"Autoplay YouTube failed: {e}")

    if not queue:
        current_song.pop(guild_id, None)
        play_attempts.pop(guild_id, None)
        save_state_all()
        await update_bot_presence()
        if text_channel and guild_id in player_messages:
            try:
                msg = await text_channel.fetch_message(player_messages[guild_id])
                await msg.delete()
            except Exception as e: pass
            player_messages.pop(guild_id, None) 
        return

    attempt = play_attempts.get(guild_id, 0)
    if attempt >= 3:
        print(f"[{guild_id}] Gagal 3x berturut-turut. Hentikan pemutaran.")
        queue.clear()
        play_attempts.pop(guild_id, None)
        current_song.pop(guild_id, None)
        save_state_all()
        await update_bot_presence()
        if text_channel and guild_id in player_messages:
            try:
                msg = await text_channel.fetch_message(player_messages[guild_id])
                await msg.delete()
            except Exception: pass
            player_messages.pop(guild_id, None)
        return

    next_info = queue.pop(0)
    seek_time = next_info.get('seek', 0)
    
    next_info['seek_offset'] = seek_time 
    next_info['start_time'] = time.time() 
    current_song[guild_id] = next_info
    
    await update_bot_presence()

    try:
        data = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(next_info['webpage_url'], download=False))
        
        before_opts = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
        if seek_time > 0: before_opts = f'-ss {seek_time} {before_opts}'
            
        opts = {'before_options': before_opts, 'options': '-vn'}
        
        if platform.system() == "Windows":
            ffmpeg_path = os.path.join(BASE_DIR, "bin", "ffmpeg", "ffmpeg.exe")
        else:
            ffmpeg_path = "ffmpeg" 

        audio_source = discord.FFmpegPCMAudio(data['url'], executable=ffmpeg_path, **opts)
        
        vc.play(discord.PCMVolumeTransformer(audio_source, volume=volumes.get(guild_id, 0.5)), 
                after=lambda e: asyncio.run_coroutine_threadsafe(play_next(guild_id, text_channel), bot.loop))
        
        play_attempts[guild_id] = 0
        if text_channel: await update_player_message(guild_id, text_channel, resend=True)
    except Exception as e:
        print(f"Error saat memutar lagu {next_info['title']}: {e}. Mencoba skip.")
        play_attempts[guild_id] = attempt + 1
        await asyncio.sleep(2)
        await play_next(guild_id, text_channel)

# --- STARTUP BOT & RESTORE STATE ---
@bot.event
async def on_ready():
    bot.add_view(MusicControlView()) 
    await bot.tree.sync() 
    print('======================================')
    print(f'Bot siap! | User: {bot.user}')
    print('======================================')

    bot.loop.create_task(state_saver_task()) 

    try:
        sb = get_supabase()
        saved_states = []
        if sb:
            response = sb.table("guild_states").select("*").execute()
            saved_states = response.data
        if saved_states:
            print(f"✅ Restoring state dari Supabase ({len(saved_states)} guild)...")
            for row in saved_states:
                guild_id = row["guild_id"]
                guild = bot.get_guild(guild_id)
                if not guild: continue

                vc_id = row.get("voice_channel_id")
                tc_id = row.get("text_channel_id")
                if not vc_id and not tc_id: continue

                vc = guild.get_channel(vc_id)
                tc = guild.get_channel(tc_id) if tc_id else None

                row_queue = row.get("queue", [])
                row_history = row.get("history", [])
                saved_curr = row.get("current_song")

                if sb:
                    try:
                        sb.table("guild_states").upsert({
                            "guild_id": guild_id,
                            "queue": row_queue,
                            "history": row_history,
                            "current_song": saved_curr,
                        }, on_conflict="guild_id").execute()
                    except Exception as e:
                        print(f"Gagal update state guild {guild_id}: {e}")

                if not row_queue and not saved_curr:
                    print(f"ℹ️ Guild {guild_id}: Tidak ada lagu valid untuk di-restore.")
                    continue

                if vc:
                    try:
                        await vc.connect()
                        queues[guild_id] = row.get("queue", [])
                        history_queues[guild_id] = row.get("history", [])
                        repeat_status[guild_id] = row.get("repeat", False)
                        volumes[guild_id] = row.get("volume", 0.5)
                        if row.get("player_message_id"):
                            player_messages[guild_id] = row["player_message_id"]

                        saved_curr = row.get("current_song")
                        if saved_curr:
                            queues[guild_id].insert(0, saved_curr)
                        active_channels[guild_id] = {
                            'text': tc.id if tc else None,
                            'voice': vc.id
                        }

                        print(f"[*] Menghubungkan ke Guild ID {guild_id}. Memutar ulang...")
                        await play_next(guild_id, tc)
                    except Exception as e:
                        print(f"🚨 Gagal me-restore state di guild {guild_id}: {e}")
        else:
            print("ℹ️ Tidak ada state tersimpan di Supabase. Mulai dari awal.")

    except Exception as e:
        print(f"🛑 ERROR fatal saat startup/restore: {e}")
    finally:
        await update_bot_presence()

# --- COMMANDS (SLASH COMMANDS) ---

@bot.tree.command(name="chat", description="Ngobrol secara private dengan AI 🤖")
async def chat(interaction: discord.Interaction, pesan: str):
    await interaction.response.defer(ephemeral=True)

    try:
        response = await gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL_NAME,
            contents=pesan,
            config=genai.types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT
            )
        )
        jawaban = response.text
        engine_used = "GEMINI"

    except Exception as e:
        print(f"[Error] Gemini error: {e}")
        await interaction.followup.send(
            "Waduh, AI Gemini sedang bermasalah. Coba lagi nanti.", 
            ephemeral=True
        )
        return

    final_reply = f"✨ Bot (dibuat oleh @Rochwidias via Gemini):\n\n{jawaban}"

    if len(final_reply) > 2000:
        final_reply = final_reply[:1997] + "..."

    await interaction.followup.send(final_reply, ephemeral=True)
    simpan_log(interaction.user.name, pesan, jawaban, engine=engine_used)

@bot.tree.command(name="play", description="Putar lagu dari YouTube / Spotify 🎵")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ Masuk voice channel dulu! 🎤", ephemeral=True)

    channel = interaction.user.voice.channel
    guild_id = interaction.guild.id
    voice_client = interaction.guild.voice_client

    try:
        if not voice_client:
            voice_client = await channel.connect()
        elif voice_client.channel != channel:
            await voice_client.move_to(channel)
        active_channels[guild_id] = {'text': interaction.channel.id, 'voice': channel.id}
    except Exception as e:
        print(f"Error koneksi voice: {e}")
        return await interaction.followup.send("❌ Gagal mengelola Voice Client.", ephemeral=True)

    search_query = query if query.startswith("http") else f"ytsearch:{query}"

    spotify_items = None
    if "spotify.com" in query:
        spotify_items, err = await cari_spotify(query)
        if err:
            return await interaction.followup.send(f"❌ Spotify error: {err}", ephemeral=True)
        if spotify_items:
            item = spotify_items[0]
            search_query = f"ytsearch:{item['artist']} - {item['title']}"
            if len(spotify_items) > 1:
                await interaction.followup.send(f"📀 Lagi muterin **{len(spotify_items)}** track dari playlist...", ephemeral=True)
            else:
                await interaction.followup.send(f"🔍 Nyari **{item['title']}** - {item['artist']} di YouTube...", ephemeral=True)

    try:
        data = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
        if not data:
            return await interaction.followup.send(f"❌ Gagal ambil data.", ephemeral=True)

        is_search = search_query.startswith("ytsearch:")
        is_playlist = 'entries' in data and data.get('extractor') and 'playlist' in data.get('extractor', '').lower()

        if is_playlist:
            entries = [e for e in data['entries'] if e]
            if not entries:
                return await interaction.followup.send(f"❌ Playlist kosong.", ephemeral=True)
            for entry in entries:
                get_queue(guild_id).append({
                    'webpage_url': entry.get('webpage_url'),
                    'title': entry.get('title', 'Unknown'),
                    'duration': entry.get('duration', 0),
                    'seek': 0
                })
            await interaction.followup.send(f"📑 **{len(entries)}** lagu dari playlist masuk antrian.", ephemeral=True)
            save_state_all()
            if not voice_client.is_playing() and not voice_client.is_paused():
                await play_next(guild_id, interaction.channel)
            else:
                await update_player_message(guild_id, interaction.channel, resend=False)
            return
        elif is_search or 'entries' in data:
            data = data['entries'][0] if data['entries'] else None
            if not data:
                return await interaction.followup.send(f"❌ Lagu **'{query}'** tidak ditemukan.", ephemeral=True)

        song_info = {
            'webpage_url': data.get('webpage_url'),
            'title': data.get('title', 'Unknown Title'),
            'duration': data.get('duration', 0),
            'seek': 0
        }
        get_queue(guild_id).append(song_info)

        if "spotify.com" in query and spotify_items and len(spotify_items) > 1:
            for item in spotify_items[1:]:
                try:
                    d = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(f"ytsearch:{item['artist']} - {item['title']}", download=False))
                    if d and 'entries' in d and d['entries']:
                        e = d['entries'][0]
                        get_queue(guild_id).append({'webpage_url': e['webpage_url'], 'title': e['title'], 'duration': e['duration'], 'seek': 0})
                except:
                    continue

        save_state_all()

        if not voice_client.is_playing() and not voice_client.is_paused():
            await play_next(guild_id, interaction.channel)
            await interaction.followup.send(f"🎵 Memulai: **{song_info['title']}**", ephemeral=True)
        else:
            await update_player_message(guild_id, interaction.channel, resend=False)
            await interaction.followup.send(f"✅ Masuk antrian: **{song_info['title']}**", ephemeral=True)

    except Exception as e:
        print(f"Error di /play: {e}")
        await interaction.followup.send(f"❌ Gagal mengambil lagu dari YouTube: `{e}`", ephemeral=True)

@bot.tree.command(name="stop", description="Hentikan musik, bersihkan antrian, dan keluar dari VC.")
async def stop_music(interaction: discord.Interaction):
    g_id = interaction.guild.id
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("Gak lagi di Voice Channel.", ephemeral=True)

    get_queue(g_id).clear()
    history_queues.pop(g_id, None)
    current_song.pop(g_id, None)
    
    if g_id in player_messages:
        try:
            msg = await interaction.channel.fetch_message(player_messages[g_id])
            await msg.delete()
        except Exception as e: pass
        player_messages.pop(g_id, None)
        
    save_state_all() 

    vc.stop() 
    await vc.disconnect()
    await update_bot_presence()
    
    return await interaction.response.send_message("🛑 Musik dihentikan total, antrian dihapus.", ephemeral=True)

@bot.tree.command(name="leave", description="Keluar dari VC tanpa hapus antrian.")
async def leave_vc(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("Gak lagi di Voice Channel.", ephemeral=True)
    g_id = interaction.guild.id
    vc.stop()
    await vc.disconnect()
    idle_timers.pop(g_id, None)
    await update_bot_presence()
    await interaction.response.send_message("👋 Keluar dari Voice Channel. Antrian disimpan.", ephemeral=True)

@bot.tree.command(name="clear", description="Hapus semua antrian tanpa stop.")
async def clear_queue(interaction: discord.Interaction):
    g_id = interaction.guild.id
    q = get_queue(g_id)
    if not q:
        return await interaction.response.send_message("Antrian udah kosong.", ephemeral=True)
    q.clear()
    history_queues.pop(g_id, None)
    save_state_all()
    await update_player_message(g_id, interaction.channel)
    await interaction.response.send_message("🗑️ Antrian dibersihkan.", ephemeral=True)

@bot.tree.command(name="volume", description="Atur volume 0-200%")
async def set_volume(interaction: discord.Interaction, persen: int):
    if persen < 0 or persen > 200:
        return await interaction.response.send_message("❌ Volume 0-200 aja.", ephemeral=True)
    g_id = interaction.guild.id
    vol = persen / 100
    volumes[g_id] = vol
    vc = interaction.guild.voice_client
    if vc and vc.source and hasattr(vc.source, 'volume'):
        vc.source.volume = vol
    save_state_all()
    await interaction.response.send_message(f"🔊 Volume → **{persen}%**", ephemeral=True)

@bot.tree.command(name="queue", description="Tampilkan semua antrian.")
async def show_queue(interaction: discord.Interaction):
    g_id = interaction.guild.id
    q = get_queue(g_id)
    if not q:
        return await interaction.response.send_message("Antrian kosong.", ephemeral=True)
    curr = current_song.get(g_id)
    teks = ""
    if curr:
        teks += f"▶️ **Sekarang:** {curr['title']} `[{format_duration(curr.get('duration', 0))}]`\n\n"
    teks += "**📜 Antrian:**\n"
    for i, s in enumerate(q, 1):
        teks += f"`{i}.` {s['title']} `[{format_duration(s['duration'])}]`\n"
    await interaction.response.send_message(teks[:2000], ephemeral=True)

@bot.tree.command(name="remove", description="Hapus lagu dari antrian (nomor).")
async def remove_queue(interaction: discord.Interaction, nomor: int):
    g_id = interaction.guild.id
    q = get_queue(g_id)
    if nomor < 1 or nomor > len(q):
        return await interaction.response.send_message(f"❌ Nomor 1-{len(q)} aja.", ephemeral=True)
    removed = q.pop(nomor - 1)
    save_state_all()
    await update_player_message(g_id, interaction.channel)
    await interaction.response.send_message(f"🗑️ **{removed['title']}** dihapus dari antrian.", ephemeral=True)

@bot.tree.command(name="now", description="Lagu yang sedang diputar.")
async def now_playing(interaction: discord.Interaction):
    g_id = interaction.guild.id
    curr = current_song.get(g_id)
    if not curr:
        return await interaction.response.send_message("❌ Gak ada lagu yang diputar.", ephemeral=True)

    elapsed = get_elapsed_time(g_id)
    dur = curr.get('duration', 0) or 1
    bar_len = 20
    pos = min(int(elapsed / dur * bar_len), bar_len)
    bar = "🟢" + "▬" * pos + "🔘" + "▬" * (bar_len - pos - 1) if pos < bar_len else "🟢" + "▬" * bar_len + "🔘"
    embed = discord.Embed(title="🎶 Now Playing", description=f"**{curr['title']}**", color=discord.Color.green())
    embed.add_field(name="Progress", value=f"{bar}\n`[{format_duration(elapsed)} / {format_duration(dur)}]`", inline=False)
    vol = volumes.get(g_id, 0.5)
    embed.add_field(name="Volume", value=f"🔊 {int(vol * 100)}%", inline=True)
    rep = "Aktif 🔁" if repeat_status.get(g_id, False) else "Nonaktif"
    embed.add_field(name="Repeat", value=f"`{rep}`", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Cek delay bot.")
async def ping(interaction: discord.Interaction):
    ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! `{ms}ms`", ephemeral=True)

def main():
    while True:
        try:
            bersihkan_ffmpeg() 
            bot.run(TOKEN)
            break 
        except Exception as e:
            print(f"=====================")
            print(f"Bot mengalami crash atau error fatal: {e}")
            print("Mencoba restart bot dalam 5 detik...")
            bersihkan_ffmpeg()
            time.sleep(5)
            os.execv(sys.executable, ['python'] + sys.argv)

if __name__ == '__main__':
    main()