# telegram-news-ai-bot

Telegram bot for Persian news monitoring, AI-assisted summaries, source-aware news Q&A, and YouTube search/download.

## Features

- Fetches recent Telegram posts from:
  - Iran International
  - Vahid Online
  - Radio Farda
  - Independent Farsi
  - BBC Persian
- Groups and summarizes news with Gemini
- Supports AI chat with source-aware news references
- Searches YouTube and sends selected videos in Telegram
- Supports large uploads with a local Telegram Bot API server

## Requirements

- Python 3.11+
- Telegram bot token from BotFather
- Telegram API ID and API hash from my.telegram.org
- Gemini API key
- `ffmpeg` recommended for better video handling
- `node` recommended for more reliable YouTube extraction with `yt-dlp`

## Project Structure

```text
telegram-news-ai-bot/
├── bot.py
├── config.py
├── news_fetcher.py
├── youtube_search.py
├── video_downloader.py
├── ai_summary.py
├── ai_chat.py
├── auth_telethon.py
├── requirements.txt
├── .env.example
└── README.md
```

## Runtime Directories

These directories are created automatically when needed. Users do not need to create them manually.

- `downloads/`
  - Stores downloaded YouTube videos before sending them in Telegram
- `news_data/`
  - Stores generated text files for fetched Telegram news posts
- `.telethon_session.session`
  - Local Telethon login session for channel access
- `telegram-bot-api-data/`
  - Optional local Telegram Bot API server data

These files and folders are excluded from Git by `.gitignore`.

## Portability

This project does not depend on your personal filesystem path.

- Runtime paths are built dynamically from the project folder
- No hardcoded `/Users/...` paths are required for normal usage
- Another user can clone the repository anywhere on their machine and run it from that directory

The main configuration logic is based on the current project directory in `config.py`.

## Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Required variables:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
GEMINI_API_KEY=
```

Optional variables:

```env
TELEGRAM_PHONE=
KEEP_DOWNLOADED_VIDEOS=false
LOCAL_BOT_API_URL=
LOCAL_BOT_API_FILE_URL=
LOCAL_BOT_API_SHARED_DOWNLOADS_PATH=
```

## Setup

1. Clone the repository
2. Enter the project folder
3. Install dependencies
4. Create `.env`
5. Run the bot

```bash
pip install -r requirements.txt
python bot.py
```

## First-Time Telethon Authorization

For Telegram channel reading, Telethon may require a one-time user authorization:

```bash
python auth_telethon.py
```

This creates a local `.telethon_session.session` file in the project directory.

## Main Commands

- `/start` - show the main menu
- `/help` - show help
- `/youtube` - start YouTube search flow
- `/chat` - start AI chat mode
- `/endchat` - end AI chat mode
- `/news` - fetch the default source summary

## News Sources

The bot is configured to work with these Telegram sources:

- Iran International
- Vahid Online
- Radio Farda
- Independent Farsi
- BBC Persian

## Optional Local Bot API Setup

If you want to upload videos larger than 50 MB, use a local `telegram-bot-api` server and set:

- `LOCAL_BOT_API_URL`
- `LOCAL_BOT_API_FILE_URL`
- `LOCAL_BOT_API_SHARED_DOWNLOADS_PATH`

Without a local Bot API server, standard bot upload limits apply.

## Notes

- Do not commit your `.env` file
- Do not commit `.telethon_session.session`
- Do not commit generated `downloads/`, `news_data/`, or `telegram-bot-api-data/`
- If AI chat cannot answer from local reference files, it can ask for permission to search the internet

## Troubleshooting

### Bot cannot fetch Telegram news

Run:

```bash
python auth_telethon.py
```

### Large video upload fails

Check your local Telegram Bot API server configuration and the shared downloads path.

### YouTube extraction is incomplete

Make sure `yt-dlp` is up to date and `node` is installed.

### Gemini search fails with quota errors

Check your Gemini API quota, rate limits, and billing status.
