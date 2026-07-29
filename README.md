# 🎵 WhoTao Discord Bot

Multi-purpose Discord bot with music player + AI chat, powered by Gemini and Supabase.

> Created by [@rochwidias](https://github.com/rochwidias)

## ✨ Features

- **🎶 Music Player** — Play YouTube audio with interactive controls (Play/Pause, Skip, Undo, Shuffle, Repeat, Queue List)
- **🤖 AI Chat** — Private chat with Google Gemini AI
- **🔄 Auto-restart** — Automatic crash recovery
- **☁️ 24/7 Online** — Deployed on Railway with Supabase persistence

## 🚀 Commands

| Command | Description |
|---------|-------------|
| `/play <query/url>` | Play a song from YouTube |
| `/stop` | Stop music & disconnect |
| `/chat <message>` | Chat with AI privately |

## 🛠️ Tech Stack

- Python + discord.py
- Google Gemini AI (`google-genai`)
- yt-dlp + FFmpeg
- Supabase (PostgreSQL)
- Railway (Hosting)

## 📦 Self-Host

```bash
git clone https://github.com/rochwidias/bot-discord.git
cd bot-discord
python -m venv .venv
.venv\Scripts\activate    # Windows
pip install -r requirements.txt
```

Create `.env`:
```env
TOKEN_DISCORD_BOT = "your_discord_token"
TOKEN_GEMINI_API = "your_gemini_api_key"
SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_KEY = "your-anon-key"
```

Run:
```bash
python bot.py
```

## 📄 License

MIT
