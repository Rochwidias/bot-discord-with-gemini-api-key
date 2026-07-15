import discord
from discord.ext import commands
from discord import app_commands
from ollama import AsyncClient
from datetime import datetime
import yt_dlp
import asyncio
import time
import sys
import os
import platform 
import json 
import random 

# --- ATURAN MAIN (HARAP GANTI TOKEN INI) ---
TOKEN = 'TOKEN DISCORD' # <<< GANTI DENGAN TOKEN ANDA
MODEL_NAME = 'llama3.2:3b'
STATE_FILE = "queue_state.json" 
SYSTEM_PROMPT = (
    "Asisten yang dikembangkan oleh @rennsh. "
    "Wajib menggunakan emoji"
    "menggunakan bahasa sopan"
    "respon dengan rapih terstruktur tetapi jelas"
    "respon dengan efisien"
    "maksimal ketikan 1800"
)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)
ollama_client = AsyncClient()

# --- INSTANSIASI DATA GLOBAL ---
queues = {}             # Antrian lagu: {guild_id: [song_dict, ...]}
history_queues = {}     # Riwayat lagu untuk Undo: {guild_id: [song_dict], ...}
current_song = {}       # Lagu yang sedang diputar: {guild_id: song_dict}
active_channels = {}    # Channel teks dan voice aktif: {guild_id: {'text': id, 'voice': id}}
player_messages = {}    # ID pesan panel utama musik: {guild_id: message_id}
repeat_status = {}      # Status repeat per server: {guild_id: boolean}

# --- FUNGSI UTILITAS UTAMA ---
def simpan_log(username, pesan, balasan):
    """Menyimpan log percakapan ke file chat_log.txt."""
    waktu = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_text = f"[{waktu}] User: {username} | Pesan: {pesan} | Bot: {balasan}\n"
    with open("chat_log.txt", "a", encoding="utf-8") as file:
        file.write(log_text)

def bersihkan_ffmpeg():
    """Menghentikan proses ffmpeg yang berjalan di background."""
    try:
        # Perintah untuk Windows
        if platform.system() == "Windows": 
            os.system("taskkill /F /IM ffmpeg.exe >nul 2>&1")
        # Perintah untuk Linux/Mac (gunakan pkill -9)
        else: 
            os.system("pkill -9 ffmpeg >/dev/null 2>&1")
    except Exception as e:
        print(f"Error cleaning FFmpeg process: {e}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Penanganan error untuk Slash Command."""
    if not interaction.response.is_done(): 
        await interaction.response.send_message("Waduh, lagi ada gangguan internal nih.", ephemeral=True)

def get_queue(guild_id):
    """Mendapatkan antrian lagu atau membuat yang baru."""
    if guild_id not in queues: queues[guild_id] = []
    return queues[guild_id]

def format_duration(seconds):
    """Mengubah detik menjadi format MM:SS atau HH:MM:SS."""
    if not seconds: return "00:00"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def get_elapsed_time(guild_id):
    """Menghitung waktu yang telah berlalu sejak lagu dimulai."""
    curr = current_song.get(guild_id)
    if not curr: return 0
    offset = curr.get('seek_offset', 0)
    start = curr.get('start_time')
    if start and isinstance(start, (int, float)): 
        return offset + (time.time() - start)
    # Jika start_time tidak valid, kembalikan offset saja
    return offset 

# --- STATUS BOT (RICH PRESENCE DENGAN TIMER) ---
async def update_bot_presence():
    """Memperbarui status bot di Discord dengan informasi lagu."""
    curr = next(iter(current_song.values()), None)
    if curr:
        start_ts = curr.get('start_time', time.time())
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=curr['title'],
            details=f"Memutar: {curr['title']}",
            state=f"Durasi: {format_duration(curr.get('duration', 0))}",
            assets={
                "large_image": "https://cdn-icons-png.flaticon.com/512/3114/3114846.png",
                "large_text": "Music Player",
                "small_image": "https://cdn-icons-png.flaticon.com/512/3114/3114846.png",
                "small_text": "Playing"
            },
            start=start_ts
        ))
    else:
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.playing, 
            name="Realms of Wumpus"
        ))

# --- PANEL EMED & PEMBERSIHAN DOOBEL MESSAGE ---
def build_player_embed(guild_id):
    """Membuat embed untuk panel musik."""
    curr = current_song.get(guild_id)
    q = get_queue(guild_id)
    rep = "Aktif 🔁" if repeat_status.get(guild_id, False) else "Nonaktif"
    
    if not curr:
        return discord.Embed(title="🛑 Musik Berhenti", description="Antrian kosong. Tambahkan lagu dengan `/play`", color=discord.Color.red())
        
    elapsed = get_elapsed_time(guild_id)
    dur = curr.get('duration', 0)
    
    embed = discord.Embed(title="🎶 Sedang Memutar", description=f"**{curr['title']}**", color=discord.Color.blue())
    # Menampilkan progress saat ini
    progress_text = f"`[{format_duration(elapsed)} / {format_duration(dur)}]`"
    embed.add_field(name="Durasi", value=progress_text, inline=True)
    embed.add_field(name="Repeat", value=f"`{rep}`", inline=True)
    
    if q:
        # Tampilkan 3 lagu pertama di antrian
        up_next = "\n".join([f"`{i+1}.` {s['title']}" for i, s in enumerate(q[:3])])
        if len(q) > 3: up_next += f"\n*...dan {len(q)-3} lainnya*"
        embed.add_field(name="📜 Selanjutnya", value=up_next, inline=False)
        
    return embed

async def update_player_message(guild_id, text_channel, resend=False):
    """Memperbarui pesan panel musik secara berkala."""
    embed = build_player_embed(guild_id)
    view = MusicControlView()
    
    # Logika Anti-Dobel: Hapus pesan bot yang lama secara paksa
    async for message in text_channel.history(limit=10):
        if message.author == bot.user and message.id != player_messages.get(guild_id):
            try: await message.delete()
            except discord.Forbidden: pass # Bot tidak punya izin hapus

    old_msg_id = player_messages.get(guild_id)
    
    if resend or not old_msg_id:
        # Jika harus re-send (misalnya setelah restart/connect baru), hapus yang lama dan kirim baru
        if old_msg_id:
            try:
                old_msg = await text_channel.fetch_message(old_msg_id)
                await old_msg.delete()
            except Exception as e: 
                print(f"Failed to delete old message: {e}")
        new_msg = await text_channel.send(embed=embed, view=view)
        player_messages[guild_id] = new_msg.id
    else:
        # Jika hanya update/edit
        try:
            old_msg = await text_channel.fetch_message(old_msg_id)
            await old_msg.edit(embed=embed, view=view)
        except Exception as e:
            # Gagal edit (mungkin pesan sudah dihapus), maka harus re-send
            print(f"Failed to edit message {old_msg_id}: {e}. Re-sending.")
            new_msg = await text_channel.send(embed=embed, view=view)
            player_messages[guild_id] = new_msg.id

# --- TOMBOL INTERAKTIF UI (discord.ui.View) ---
class MusicControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="btn_playpause")
    async def btn_playpause(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Play/Pause fungsi."""
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

        # Toggle Play/Pause state
        if vc.is_paused():
            vc.resume()
            if curr: curr['start_time'] = time.time() # Reset timer saat resume
            await interaction.followup.send("⏯️ Melanjutkan pemutaran.")
        elif vc.is_playing():
            vc.pause()
            # Hitung seek offset sebelum pause
            current_elapsed = get_elapsed_time(g_id)
            if curr: 
                curr['seek_offset'] = current_elapsed
                curr['start_time'] = None # Reset start time karena dipause
            await interaction.followup.send("⏸️ Dijeda.")
        save_state_all()
        await update_player_message(g_id, interaction.channel)

    @discord.ui.button(label="Undo", emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="btn_undo")
    async def btn_undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Mengulang lagu sebelumnya."""
        await interaction.response.defer()
        g_id = interaction.guild.id
        hq = history_queues.get(g_id, [])
        if not hq:
            await interaction.followup.send("❌ Gak ada lagu sebelumnya!", ephemeral=True)
            return
        
        # Pop riwayat dan masukkan ke antrian
        prev = hq.pop() 
        curr = current_song.get(g_id)

        if curr: 
            # Masukkan lagu yang sedang diputar kembali ke antrian (setelah undo)
            curr['seek'] = 0 
            queues[g_id].insert(0, curr)
        prev['seek'] = 0 # Reset seek untuk lagu sebelumnya
        queues[g_id].insert(0, prev)

        vc = interaction.guild.voice_client
        if vc and vc.is_playing(): vc.stop() # Stop stream saat akan undo
        else: await play_next(g_id, interaction.channel) # Langsung mainkan lagu yang di-undo

        await interaction.followup.send("⏪ Undo berhasil! Memutar ulang lagu sebelumnya.")


    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="btn_skip")
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Melewatkan lagu saat ini."""
        await interaction.response.defer()
        g_id = interaction.guild.id
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing(): 
            await interaction.followup.send("⚠️ Tidak ada musik yang diputar.", ephemeral=True)
            return

        # Hentikan dan panggil play_next untuk lagu berikutnya
        vc.stop()
        await asyncio.sleep(1) # Beri waktu ffmpeg berhenti
        await play_next(g_id, interaction.channel)

        await interaction.followup.send("⏭️ Melewati lagu.")


    @discord.ui.button(label="Random", emoji="🔀", style=discord.ButtonStyle.success, custom_id="btn_random")
    async def btn_random(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Mengacak urutan antrian."""
        await interaction.response.defer()
        g_id = interaction.guild.id
        q = get_queue(g_id)
        if len(q) > 1:
            random.shuffle(q) # Mengacak list di memory
            save_state_all()
            await update_player_message(g_id, interaction.channel)
            await interaction.followup.send("🔀 Antrian lagu berhasil diacak!")
        else:
            await interaction.followup.send("❌ Antrian kurang dari 2 lagu untuk diacak.", ephemeral=True)

    @discord.ui.button(label="Repeat", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="btn_repeat")
    async def btn_repeat(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Mengaktifkan/menonaktifkan Repeat."""
        await interaction.response.defer()
        g_id = interaction.guild.id
        repeat_status[g_id] = not repeat_status.get(g_id, False)
        save_state_all()
        await update_player_message(g_id, interaction.channel)
        status = "Aktif" if repeat_status[g_id] else "Nonaktif"
        await interaction.followup.send(f"🔁 Repeat mode sekarang: **{status}**.")

    @discord.ui.button(label="List", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="btn_list")
    async def btn_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Menampilkan daftar antrian lengkap."""
        g_id = interaction.guild.id
        q = get_queue(g_id)
        if not q:
            await interaction.response.send_message("Antrian kosong.", ephemeral=True)
            return
        teks = "**📜 Daftar Antrian Lengkap:**\n"
        for i, s in enumerate(q, 1): teks += f"`{i}.` {s['title']} `[{format_duration(s['duration'])}]`\n"
        await interaction.response.send_message(teks[:2000], ephemeral=True)

# --- SAVE & RESTORE STATE VIA JSON ---
def save_state_all():
    """Menyimpan semua status global ke file state."""
    data = {}
    all_guilds = set(queues.keys()).union(set(current_song.keys()))
    for guild_id in all_guilds:
        q = get_queue(guild_id)
        curr = current_song.get(guild_id)
        hq = history_queues.get(guild_id, [])
        if not curr and not q and guild_id not in active_channels: continue
        
        guild_str = str(guild_id)
        data[guild_str] = {
            "queue": q, 
            "history": hq, 
            "repeat": repeat_status.get(guild_id, False),
            "text_channel_id": active_channels.get(guild_id, {}).get('text'),
            "voice_channel_id": active_channels.get(guild_id, {}).get('voice'),
            "player_message_id": player_messages.get(guild_id)
        }
        if curr:
            curr_copy = curr.copy()
            # Pastikan waktu pencarian (seek) di-save
            current_elapsed = get_elapsed_time(guild_id) 
            curr_copy['seek'] = current_elapsed
            data[guild_str]["current_song"] = curr_copy
    try:
        with open(STATE_FILE, "w") as f: json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Gagal menyimpan state ke JSON: {e}")

async def state_saver_task():
    """Task yang berjalan berkala untuk save state dan update player message."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        if current_song or queues:
            save_state_all()
            for g_id, curr in list(current_song.items()):
                # Update UI hanya jika channel aktif dan lagu sedang diputar
                if g_id in active_channels and curr and curr.get('start_time') is not None:
                    tc = bot.get_channel(active_channels[g_id].get('text'))
                    if tc: 
                        await update_player_message(g_id, tc, resend=False)
        await asyncio.sleep(5)

# --- ENGINE PEMUTAR LAGU & AUTOPLAY ---
ytdl_format_options = {
    'format': 'bestaudio/best',
    'noplaylist': True, 
    'nocheckcertificate': True,
    'quiet': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'
}
ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

async def play_next(guild_id: int, text_channel=None):
    """Logika inti untuk memutar lagu berikutnya dari antrian."""
    queue = get_queue(guild_id)
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client: 
        print(f"[{guild_id}] Gagal play_next karena tidak ada Voice Client.")
        return
    vc = guild.voice_client
    
    curr = current_song.get(guild_id)
    
    # 1. Logic Repeat vs History
    if curr:
        if repeat_status.get(guild_id, False):
            # Jika repeat ON, masukkan lagu saat ini ke awal antrian (seek 0)
            curr['seek'] = 0 
            get_queue(guild_id).insert(0, curr)
        else:
            # Jika repeat OFF, pindahkan lagu saat ini ke history sebelum play next
            if guild_id not in history_queues: history_queues[guild_id] = []
            history_queues[guild_id].append(curr)
            if len(history_queues[guild_id]) > 10: history_queues[guild_id].pop(0)

    # 2. Logic Autoplay (Antrian Habis)
    if not queue and curr and not repeat_status.get(guild_id, False):
        print(f"[{guild_id}] Antrian kosong. Mencari rekomendasi...")
        try:
            data = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(f"ytsearch1:{curr['title']} related", download=False))
            if 'entries' in data and data['entries']:
                entry = data['entries'][0]
                # Tambahkan rekomendasi ke antrian
                queue.append({'webpage_url': entry['webpage_url'], 'title': entry['title'], 'duration': entry['duration'], 'seek': 0})
        except Exception as e: 
            print(f"Autoplay failed for {guild_id}: {e}")

    # 3. Cek Akhir Antrian (Shutdown)
    if not queue:
        current_song.pop(guild_id, None)
        save_state_all()
        await update_bot_presence()
        if text_channel and guild_id in player_messages:
            try:
                msg = await text_channel.fetch_message(player_messages[guild_id])
                await msg.delete()
            except Exception as e: 
                print(f"Gagal menghapus pesan panel saat selesai: {e}")
                player_messages.pop(guild_id, None) # Hapus ID meskipun gagal hapus
        return

    # Ambil lagu berikutnya
    next_info = queue.pop(0)
    seek_time = next_info.get('seek', 0)
    
    # Setup status streaming baru
    next_info['seek_offset'] = seek_time # Digunakan untuk display progress
    next_info['start_time'] = time.time() # Reset start waktu
    current_song[guild_id] = next_info
    
    await update_bot_presence()

    try:
        # Extract info YouTube secara sinkron di executor thread
        data = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(next_info['webpage_url'], download=False))
        
        before_opts = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
        # Jika ada waktu pencarian (seek), tambahkan opsi start time
        if seek_time > 0: before_opts = f'-ss {seek_time} {before_opts}'
            
        opts = {'before_options': before_opts, 'options': '-vn'}
        
        # !!! PASTIKAN PATH FFmpeg DI BAWAH INI BENAR !!!
        ffmpeg_path = r"F:\bot-discord\bin\ffmpeg\ffmpeg.exe" 

        audio_source = discord.FFmpegPCMAudio(data['url'], executable=ffmpeg_path, **opts)
        
        # Play audio dan atur callback untuk memanggil play_next saat selesai
        vc.play(discord.PCMVolumeTransformer(audio_source, volume=0.5), 
                after=lambda e: asyncio.run_coroutine_threadsafe(play_next(guild_id, text_channel), bot.loop))
        
        if text_channel: await update_player_message(guild_id, text_channel, resend=True)
    except Exception as e:
        print(f"Error saat memutar lagu {next_info['title']}: {e}. Mencoba skip.")
        # Jika terjadi error (misal link rusak), panggil play_next lagi untuk mencoba lagu berikutnya
        await asyncio.sleep(2)
        await play_next(guild_id, text_channel)

# --- STARTUP BOT & RESTORE STATE ---
@bot.event
async def on_ready():
    """Dipanggil saat bot berhasil login."""
    bot.add_view(MusicControlView()) # Daftarkan View/Tombol interaktif
    await bot.tree.sync()          # Sync slash commands
    print('======================================')
    print(f'Bot siap! (UI Interactive Mode) | User: {bot.user}')
    print('======================================')

    # Mulai task untuk saving state berkala
    bot.loop.create_task(state_saver_task()) 

    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f: saved_states = json.load(f)
            print("✅ Restoring state dari file JSON...")
            for guild_str, state in saved_states.items():
                guild_id = int(guild_str)
                guild = bot.get_guild(guild_id)
                if not guild: continue

                vc_id = state.get("voice_channel_id")
                tc_id = state.get("text_channel_id")
                if not vc_id and not tc_id: continue # Harus ada VC untuk play next

                vc = guild.get_channel(vc_id)
                tc = guild.get_channel(tc_id) if tc_id else None

                if vc:
                    try:
                        await vc.connect()
                        queues[guild_id] = state.get("queue", [])
                        history_queues[guild_id] = state.get("history", [])
                        repeat_status[guild_id] = state.get("repeat", False)
                        if state.get("player_message_id"): player_messages[guild_id] = state.get("player_message_id")
                        
                        saved_curr = state.get("current_song")
                        if saved_curr: queues[guild_id].insert(0, saved_curr)
                        active_channels[guild_id] = {'text': tc.id if tc else None, 'voice': vc.id}

                        print(f"[*] Menghubungkan ke Guild ID {guild_id}. Memutar ulang...")
                        await play_next(guild_id, tc) # Langsung memutar lagu yang terakhir
                    except Exception as e: 
                        print(f"🚨 Gagal me-restore state di guild {guild_id}: {e}")

    except json.JSONDecodeError:
        print("⚠️ File state JSON rusak atau kosong. Memulai dari awal.")
    except Exception as e:
        print(f"🛑 ERROR fatal saat startup/restore: {e}")
    finally:
        await update_bot_presence()

# --- COMMANDS (SLASH COMMANDS) ---

@bot.tree.command(name="chat", description="Ngobrol secara private dengan AI 🤖")
async def chat(interaction: discord.Interaction, pesan: str):
    """Fungsi untuk ngobrol menggunakan Ollama."""
    await interaction.response.defer(ephemeral=True) # Hanya pengirim yang bisa melihat output
    try:
        response = await ollama_client.chat(model=MODEL_NAME, messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': pesan},
        ])
        jawaban = response['message']['content']
        final_reply = f"✨ Bot (dibuat oleh @Rochwidias):\n{jawaban}"
        if len(final_reply) > 2000: final_reply = final_reply[:1997] + "..."
        await interaction.followup.send(final_reply, ephemeral=True)
        simpan_log(interaction.user.name, pesan, jawaban)
    except Exception as e:
        print(f"Chat error: {e}")
        await interaction.followup.send("Waduh, ada masalah dengan koneksi AI (Ollama). Pastikan service Ollama berjalan.")

@bot.tree.command(name="play", description="Putar lagu berdasarkan Judul atau URL 🎵")
async def play(interaction: discord.Interaction, query: str):
    """Memproses permintaan pemutaran lagu."""
    await interaction.response.defer(ephemeral=True)
    
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ Masuk voice channel dulu! 🎤", ephemeral=True)
    
    channel = interaction.user.voice.channel
    guild_id = interaction.guild.id
    voice_client = interaction.guild.voice_client 
    search_query = query if query.startswith("http") else f"ytsearch1:{query}"
    
    # 1. Pastikan Voice Client terhubung
    try:
        if not voice_client:
            voice_client = await channel.connect()
        elif voice_client.channel != channel:
            await voice_client.move_to(channel)
        
        # Update state channel aktif hanya jika koneksi berhasil
        active_channels[guild_id] = {'text': interaction.channel.id, 'voice': channel.id}
        
    except Exception as e:
        print(f"Error koneksi voice: {e}")
        return await interaction.followup.send("❌ Gagal mengelola Voice Client. Pastikan bot memiliki izin.", ephemeral=True)

    # 2. Ambil Info Lagu
    try:
        data = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
        
        if not data or ('entries' in data and not data['entries']):
            return await interaction.followup.send(f"❌ Lagu **'{query}'** tidak ditemukan.", ephemeral=True)

        if 'entries' in data:
            data = data['entries'][0]
        
        song_info = {
            'webpage_url': data.get('webpage_url'),
            'title': data.get('title', 'Unknown Title'),
            'duration': data.get('duration', 0),
            'seek': 0
        }
        
        # Tambahkan ke antrian
        get_queue(guild_id).append(song_info)
        save_state_all()

        # 3. Mulai/Update Pemutaran
        if not voice_client.is_playing() and not voice_client.is_paused():
            await play_next(guild_id, interaction.channel)
            await interaction.followup.send(f"🎵 Memulai: **{song_info['title']}**", ephemeral=True)
        else:
            await update_player_message(guild_id, interaction.channel, resend=False)
            await interaction.followup.send(f"✅ Masuk antrian: **{song_info['title']}**", ephemeral=True)

    except Exception as e:
        print(f"Error di /play: {e}")
        return await interaction.followup.send("❌ Gagal mengambil lagu karena error internal.", ephemeral=True)

@bot.tree.command(name="stop", description="Hentikan musik, bersihkan antrian, dan keluar dari VC.")
async def stop_music(interaction: discord.Interaction):
    """Menghentikan semua aktivitas musik."""
    g_id = interaction.guild.id
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("Gak lagi di Voice Channel.", ephemeral=True)

    # Bersihkan data state
    get_queue(g_id).clear()
    history_queues.pop(g_id, None)
    current_song.pop(g_id, None)
    vc = interaction.guild.voice_client # Here vc is defined first
    
    if not vc: 
        return await interaction.response.send_message("Gak lagi di Voice Channel.", ephemeral=True)
    
    if g_id in player_messages:
        try:
            msg = await interaction.channel.fetch_message(player_messages[g_id])
            await msg.delete()
        except Exception as e:
            print(f"Error deleting message on stop: {e}")
            pass
        player_messages.pop(g_id, None)
        
    save_state_all() 

    # Hentikan dan putuskan koneksi
    vc.stop() # Memastikan stream berhenti
    await vc.disconnect()
    await update_bot_presence()
    
    return await interaction.response.send_message("🛑 Musik dihentikan total, antrian dihapus.", ephemeral=True)


def main():
    """Fungsi utama yang menjalankan bot dan memastikan restart jika crash."""
    while True:
        try:
            # Bersihkan proses FFmpeg sebelum mencoba koneksi baru
            bersihkan_ffmpeg() 
            bot.run(TOKEN)
            break # Berhasil run, keluar dari loop forever
        except Exception as e:
            print(f"=====================")
            print(f"Bot mengalami crash atau error fatal: {e}")
            print("Mencoba restart bot dalam 5 detik...")
            bersihkan_ffmpeg()
            time.sleep(5)
            # Restart proses Python secara keseluruhan
            os.execv(sys.executable, ['python'] + sys.argv)

if __name__ == '__main__':
    main()