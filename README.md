# telegram-news-ai-bot v2

A production-oriented Telegram bot for Persian news workflows, Gemini-powered chat, YouTube search/download, and practical file tools.

## What is new in V2

- Multi-level Persian Telegram menu with improved UX
- Direct Gemini chat from the main AI section
- Source-aware news summaries with denser, more useful message formatting
- YouTube quality picker with separate audio-only download support
- File tools:
  - Audio Compressor
  - Video Compressor
  - PDF Compressor
  - PDF Slicer
  - PDF Merger
- Automatic splitting of oversized compressed audio/video outputs into sub-16 MB parts for easier delivery in messaging apps with strict file limits

## Core Features

- News source summaries for:
  - Iran International
  - Vahid Online
  - Radio Farda
  - Independent Farsi
  - BBC Persian
- News-aware AI chat from the News section
- General Gemini chat from the main AI section
- YouTube search
- YouTube direct URL download
- YouTube quality selection
- Separate audio-only YouTube download
- Audio compression to MP3 with Telegram-safe splitting when needed
- Video compression with CPU, GPU, and Parallel modes
- PDF compression with low / medium / high levels
- PDF slicing by page range
- PDF merging for multiple uploaded files

## Requirements

- Python 3.11+
- ffmpeg
- Ghostscript
- Telegram bot token from BotFather
- Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org)
- Gemini API key
- `node` is recommended for more reliable `yt-dlp` extraction
- Docker is optional if you want a local Telegram Bot API server for larger media workflows
- You can also run the local Telegram Bot API natively without Docker

## Installation

```bash
git clone <your-repo-url>
cd telegram-news-ai-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then fill in `.env` with your own credentials.

## Running the Bot

```bash
python bot.py
```

## First-Time Telegram Authorization

For Telegram channel reading, Telethon may require a one-time authorization:

```bash
python auth_telethon.py
```

## Commands

- `/start` - open the main menu
- `/news` - open the news section
- `/youtube` - open the YouTube section
- `/tools` - open the file tools section
- `/ai` - start direct Gemini chat
- `/help` - show help

Compatibility aliases kept in code:

- `/chat` - start news-aware AI chat
- `/endchat` - end the active AI chat

## Runtime Directories

These are created automatically when needed and should not be committed:

- `downloads/`
- `news_data/`
- `telegram-bot-api-data/`
- `.telethon_session.session`
- `.telethon_bot_media.session`

## Path Portability

This project is portable by default:

- Runtime paths are derived from the project directory
- No personal `/Users/...` paths are required
- Another user can clone the project anywhere and run it there
- The local Bot API helper script mounts the downloads directory automatically, so users do not need to manually fix the shared downloads path

## Local Telegram Bot API (Optional)

If you want better handling for larger Telegram media flows, start the local Bot API server with:

```bash
./start_local_bot_api.sh
```

By default, the script automatically mounts:

- `telegram-bot-api-data/` -> Telegram Bot API storage
- `downloads/` -> `/downloads` inside the container

So the default `.env.example` works without manual path edits.

If Docker Desktop is unavailable on your machine, you can also run the local Bot API server natively:

```bash
./start_local_bot_api_native.sh
```

The native launcher automatically writes the runtime path mapping needed for large local uploads from `downloads/`.

## Security Notes

- Never commit `.env`
- Never commit real API keys or bot tokens
- Never commit runtime sessions or generated media

## Troubleshooting

### Telegram news fetching fails

Run:

```bash
python auth_telethon.py
```

### Large media sending fails

Make sure your local Telegram Bot API server is actually running:

```bash
./start_local_bot_api.sh
```

Or, if you are using the native binary instead of Docker:

```bash
./start_local_bot_api_native.sh
```

### YouTube extraction is unstable

Update `yt-dlp` and make sure `node` is installed.
