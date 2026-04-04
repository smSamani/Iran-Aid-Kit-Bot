# Iran-Aid-Kit-Bot ⛑️ v3 Public Release

🚀 A production-oriented Telegram bot for Persian news workflows, AI chat, YouTube tools, and practical file utilities.


## ✨ What Changed From v2

- 🤖 Upgraded the AI layer with multi-key Gemini fallback, stronger prompt handling, cleaner Telegram formatting, and better resilience.
- 🔎 Expanded the AI menu beyond plain chat with direct Google Search and Google Images flows.
- 👨‍💻 Added a staff-only Developer Mode with model selection, DeepSeek reasoning models, smarter long-form replies, and optional generated code files.
- 📎 Added Developer Mode attachment support for text/code files, screenshots and images, PDF, CSV, and Excel.
- 📺 Expanded the YouTube experience with richer search results, saved channels, playlists, watch later, recent searches, and in-channel search.
- ⬇️ Improved download handling with clearer progress, cancellation support, and better media preparation behavior.
- 🎞️ Improved audio and video compression flows and Telegram-friendly splitting behavior for large outputs.
- 📦 Added a support-bundle workflow that can find and collect Slipnet, Vless, Telegram Proxy, and NPVTunnel configs from well-known Iranian channels and package them into a password-protected zip archive.
- 🛡️ Added owner/admin approval controls and a user-management workflow for keeping the bot private and manageable.
- 🧩 Improved local Telegram Bot API support with both containerized and native launcher options.

## 🧠 Core Features

- 📰 News summaries for Iran International, Vahid Online, Radio Farda, Independent Farsi, and BBC Persian
- 💬 News-aware AI chat
- 🌐 General AI chat
- 🔎 Google-grounded web search
- 🖼️ Google image search
- 👨‍💻 Staff-only Developer Mode
- 📺 YouTube search and direct download
- ⭐ Saved channels, playlists, watch later, and recent searches
- 🎵 Audio compression
- 🎬 Video compression
- 📄 PDF compression
- ✂️ PDF slicing
- 🧷 PDF merging
- 📦 VPN support bundle packaging

## ⚙️ Requirements

- Python 3.11+
- ffmpeg
- Ghostscript
- Telegram bot token from BotFather
- Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org)
- At least one Gemini API key
- `node` is recommended for more reliable `yt-dlp` extraction
- Docker is optional if you want a local Telegram Bot API server
- Docker Compose is optional if you want the local DeepSeek proxy
- `tesseract` is optional if you want OCR for Developer Mode image attachments

## 🛠️ Installation

```bash
git clone <your-repo-url>
cd telegram-news-ai-bot-v3-public-release
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then fill in `.env` with your own credentials.

## 🔑 Configuration Notes

- You can provide one Gemini key or multiple Gemini keys for fallback and rotation.
- DeepSeek Developer Mode is optional and only appears when the local DeepSeek-compatible endpoint is configured.
- `ADMIN_CHAT_ID` seeds the Telegram owner account.
- `VPN_ARCHIVE_PASSWORD` is required if you want the support bundle to be packaged as a password-protected zip.
- Local Telegram Bot API settings are optional and only needed for larger Telegram media workflows.

## 👨‍💻 Developer Mode

Developer Mode is intended for approved staff users.

- 💎 Supports Gemini Pro and optional DeepSeek reasoning models
- 📝 Can answer directly in chat or generate a code/config file when that is materially better
- 📂 Can ask for files mid-conversation when more context is needed
- 📎 Can read text/code files, screenshots, PDF, CSV, and Excel attachments
- 📱 Optimizes replies for chat readability instead of forcing overly short answers

## 🧠 Local DeepSeek Proxy

If you want DeepSeek reasoning models in Developer Mode, configure the local compatible endpoint in `.env` and start the provided Docker Compose helper.

This release package only keeps placeholder configuration. No real tokens are included.

## 📡 Local Telegram Bot API

If you want better handling for larger Telegram media flows, you can start the local Bot API helper with either the containerized launcher or the native launcher.

## 🔒 Privacy And Safety

- No real tokens, API keys, phone numbers, or session files are included
- No user manager data or approval records are included
- No runtime logs, generated downloads, or collected search analytics are included
- No vendored local Telegram Bot API source tree is included

## 📁 Runtime Data

Runtime directories and local state are created only when needed and are ignored by git in this release package.
