"""Application configuration and shared constants."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
NEWS_DATA_DIR = BASE_DIR / "news_data"
TELETHON_SESSION_PATH = BASE_DIR / ".telethon_session"
TELEGRAM_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
LOCAL_BOT_API_UPLOAD_LIMIT_BYTES = 2000 * 1024 * 1024
DEFAULT_NEWS_LIMIT = 30
AI_REFERENCE_POST_LIMIT = 200
YOUTUBE_RESULT_LIMIT = 10
CALLBACK_PREFIX = "yt"


@dataclass(slots=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    telegram_bot_token: str
    telegram_api_id: int
    telegram_api_hash: str
    gemini_api_key: str
    telegram_phone: str | None = None
    keep_downloaded_videos: bool = False
    local_bot_api_url: str | None = None
    local_bot_api_file_url: str | None = None
    local_bot_api_shared_downloads_path: str | None = None


@dataclass(frozen=True, slots=True)
class NewsSource:
    """Telegram news source configuration."""

    key: str
    display_name: str
    username: str

    @property
    def channel_url(self) -> str:
        return f"https://t.me/{self.username}"


NEWS_SOURCES: dict[str, NewsSource] = {
    "iranintl": NewsSource("iranintl", "Iran International", "IranintlTV"),
    "vahid": NewsSource("vahid", "Vahid Online", "VahidOnline"),
    "radiofarda": NewsSource("radiofarda", "Radio Farda", "radiofarda"),
    "indypersian": NewsSource("indypersian", "Independent Farsi", "Indypersian"),
    "bbcpersian": NewsSource("bbcpersian", "BBC Persian", "bbcpersian"),
}


def configure_logging() -> None:
    """Configure the root logger for the bot."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def load_settings() -> Settings:
    """Load and validate required environment variables."""
    load_dotenv(BASE_DIR / ".env")

    required_keys = {
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_API_ID": os.getenv("TELEGRAM_API_ID"),
        "TELEGRAM_API_HASH": os.getenv("TELEGRAM_API_HASH"),
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY"),
    }

    missing = [key for key, value in required_keys.items() if not value]
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(f"Missing required environment variables: {missing_list}")

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    NEWS_DATA_DIR.mkdir(parents=True, exist_ok=True)

    return Settings(
        telegram_bot_token=required_keys["TELEGRAM_BOT_TOKEN"] or "",
        telegram_api_id=int(required_keys["TELEGRAM_API_ID"] or "0"),
        telegram_api_hash=required_keys["TELEGRAM_API_HASH"] or "",
        gemini_api_key=required_keys["GEMINI_API_KEY"] or "",
        telegram_phone=os.getenv("TELEGRAM_PHONE"),
        keep_downloaded_videos=(os.getenv("KEEP_DOWNLOADED_VIDEOS", "false").lower() == "true"),
        local_bot_api_url=os.getenv("LOCAL_BOT_API_URL"),
        local_bot_api_file_url=os.getenv("LOCAL_BOT_API_FILE_URL"),
        local_bot_api_shared_downloads_path=os.getenv("LOCAL_BOT_API_SHARED_DOWNLOADS_PATH"),
    )


def get_yt_dlp_js_runtimes() -> dict[str, dict[str, str]] | None:
    """Return supported JS runtimes for yt-dlp if available on this machine."""
    node_path = shutil.which("node")
    if node_path:
        return {"node": {"path": node_path}}
    return None


def get_yt_dlp_remote_components() -> list[str]:
    """Return remote challenge-solver components to enable for yt-dlp."""
    return ["ejs:github"]
