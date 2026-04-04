"""Application configuration and shared constants."""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values, load_dotenv, set_key


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DOWNLOADS_DIR = BASE_DIR / "downloads"
NEWS_DATA_DIR = BASE_DIR / "news_data"
TELETHON_SESSION_PATH = BASE_DIR / ".telethon_session"
TELETHON_BOT_MEDIA_SESSION_PATH = BASE_DIR / ".telethon_bot_media.session"
LOCAL_BOT_API_DATA_DIR = BASE_DIR / "telegram-bot-api-data"
LOCAL_BOT_API_RUNTIME_FILE = BASE_DIR / ".local_bot_api_runtime.json"
DEFAULT_LOCAL_BOT_API_SHARED_DOWNLOADS_PATH = "/downloads"
TELEGRAM_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
LOCAL_BOT_API_UPLOAD_LIMIT_BYTES = 2000 * 1024 * 1024
DEFAULT_NEWS_LIMIT = 30
AI_REFERENCE_POST_LIMIT = 200
YOUTUBE_RESULT_LIMIT = 10
CALLBACK_PREFIX = "yt"
DEFAULT_VIDEO_COMPRESS_MAX_INPUT_BYTES = 200 * 1024 * 1024


@dataclass(slots=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    telegram_bot_token: str
    telegram_api_id: int
    telegram_api_hash: str
    gemini_api_key: str
    gemini_api_keys: tuple[str, ...]
    telegram_phone: str | None = None
    keep_downloaded_videos: bool = False
    video_compress_max_input_bytes: int = DEFAULT_VIDEO_COMPRESS_MAX_INPUT_BYTES
    local_bot_api_url: str | None = None
    local_bot_api_file_url: str | None = None
    local_bot_api_shared_downloads_path: str | None = None
    gemini_chat_model: str = "gemini-flash-latest"
    gemini_live_search_enabled: bool = True
    gemini_live_search_model: str = "gemini-flash-latest"
    gemini_developer_model: str = "gemini-pro-latest"
    deepseek_api_base_url: str | None = None
    deepseek_api_timeout_seconds: float = 180.0
    admin_chat_id: int | None = None
    rubika_owner_ids: tuple[str, ...] = ()


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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_settings() -> Settings:
    """Load and validate required environment variables."""
    load_dotenv(ENV_FILE)
    runtime_config = load_local_bot_api_runtime_config()

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

    gemini_api_keys = _load_gemini_api_keys(required_keys["GEMINI_API_KEY"] or "")

    return Settings(
        telegram_bot_token=required_keys["TELEGRAM_BOT_TOKEN"] or "",
        telegram_api_id=int(required_keys["TELEGRAM_API_ID"] or "0"),
        telegram_api_hash=required_keys["TELEGRAM_API_HASH"] or "",
        gemini_api_key=required_keys["GEMINI_API_KEY"] or "",
        gemini_api_keys=gemini_api_keys,
        telegram_phone=os.getenv("TELEGRAM_PHONE"),
        keep_downloaded_videos=(os.getenv("KEEP_DOWNLOADED_VIDEOS", "false").lower() == "true"),
        video_compress_max_input_bytes=int(
            os.getenv(
                "VIDEO_COMPRESS_MAX_INPUT_BYTES",
                str(DEFAULT_VIDEO_COMPRESS_MAX_INPUT_BYTES),
            )
        ),
        gemini_chat_model=os.getenv("GEMINI_CHAT_MODEL", "gemini-flash-latest"),
        gemini_live_search_enabled=(
            os.getenv("GEMINI_LIVE_SEARCH_ENABLED", "true").lower() == "true"
        ),
        gemini_live_search_model=os.getenv(
            "GEMINI_LIVE_SEARCH_MODEL",
            "gemini-flash-latest",
        ),
        gemini_developer_model=os.getenv(
            "GEMINI_DEVELOPER_MODEL",
            "gemini-pro-latest",
        ),
        deepseek_api_base_url=(
            os.getenv("DEEPSEEK_API_BASE_URL", "").strip() or None
        ),
        deepseek_api_timeout_seconds=float(
            os.getenv("DEEPSEEK_API_TIMEOUT_SECONDS", "180").strip() or "180"
        ),
        admin_chat_id=(
            int(os.getenv("ADMIN_CHAT_ID", "").strip())
            if os.getenv("ADMIN_CHAT_ID", "").strip().isdigit()
            else None
        ),
        local_bot_api_url=os.getenv("LOCAL_BOT_API_URL"),
        local_bot_api_file_url=os.getenv("LOCAL_BOT_API_FILE_URL"),
        local_bot_api_shared_downloads_path=(
            runtime_config.get("shared_downloads_path")
            or os.getenv("LOCAL_BOT_API_SHARED_DOWNLOADS_PATH")
            or DEFAULT_LOCAL_BOT_API_SHARED_DOWNLOADS_PATH
        ),
        rubika_owner_ids=tuple(
            value.strip()
            for value in os.getenv("RUBIKA_OWNER_IDS", "").split(",")
            if value.strip()
        ),
    )


def _load_gemini_api_keys(primary_key: str) -> tuple[str, ...]:
    """Collect Gemini API keys from the primary key plus optional extra env vars."""
    candidates: list[str] = [primary_key]
    comma_list = os.getenv("GEMINI_API_KEYS", "")
    if comma_list.strip():
        candidates.extend(part.strip() for part in comma_list.split(","))
    for index in range(2, 10):
        candidates.append(os.getenv(f"GEMINI_API_KEY_{index}", "").strip())

    unique_keys: list[str] = []
    for key in candidates:
        if key and key not in unique_keys:
            unique_keys.append(key)
    return tuple(unique_keys)


def _normalize_deepseek_token_values(tokens: Iterable[str]) -> list[str]:
    """Normalize a user-provided DeepSeek token sequence."""
    unique_tokens: list[str] = []
    for token in tokens:
        cleaned = str(token or "").strip().strip("\"'")
        if cleaned and cleaned not in unique_tokens:
            unique_tokens.append(cleaned)
    return unique_tokens


def load_deepseek_chat_auth_tokens() -> tuple[str, ...]:
    """Read configured DeepSeek proxy auth tokens from the local env file."""
    raw_value = ""
    try:
        if ENV_FILE.is_file():
            payload = dotenv_values(ENV_FILE)
            env_value = payload.get("DEEPSEEK_CHAT_AUTH_TOKENS")
            if isinstance(env_value, str):
                raw_value = env_value
    except Exception:
        raw_value = ""

    if not raw_value:
        raw_value = os.getenv("DEEPSEEK_CHAT_AUTH_TOKENS", "")

    return tuple(_normalize_deepseek_token_values(raw_value.split(",")))


def save_deepseek_chat_auth_tokens(tokens: Iterable[str]) -> tuple[str, ...]:
    """Persist DeepSeek proxy auth tokens to the local env file."""
    normalized = _normalize_deepseek_token_values(tokens)
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.touch()
    set_key(
        str(ENV_FILE),
        "DEEPSEEK_CHAT_AUTH_TOKENS",
        ",".join(normalized),
        quote_mode="never",
    )
    os.environ["DEEPSEEK_CHAT_AUTH_TOKENS"] = ",".join(normalized)
    return tuple(normalized)


def load_local_bot_api_runtime_config() -> dict[str, str]:
    """Load ephemeral runtime overrides written by the local Bot API launchers."""
    if not LOCAL_BOT_API_RUNTIME_FILE.is_file():
        return {}

    try:
        content = json.loads(LOCAL_BOT_API_RUNTIME_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(content, dict):
        return {}

    return {
        key: value
        for key, value in content.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def get_yt_dlp_js_runtimes() -> dict[str, dict[str, str]] | None:
    """Return supported JS runtimes for yt-dlp if available on this machine."""
    node_path = shutil.which("node")
    if node_path:
        return {"node": {"path": node_path}}
    return None


def get_yt_dlp_remote_components() -> list[str]:
    """Return remote challenge-solver components to enable for yt-dlp."""
    return ["ejs:github"]
