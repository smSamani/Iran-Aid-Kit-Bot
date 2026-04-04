"""Telegram bot entrypoint."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import mimetypes
import re
import shutil
import socket
import threading
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from ai_chat import GeminiChatManager
from ai_summary import GeminiSummarizer
from audio_compressor import (
    MAX_AUDIO_PART_BYTES,
    SUPPORTED_AUDIO_SUFFIXES,
    compress_audio_to_mp3,
    split_audio_for_telegram,
)
from config import (
    BASE_DIR,
    DOWNLOADS_DIR,
    LOCAL_BOT_API_DATA_DIR,
    NEWS_SOURCES,
    TELEGRAM_UPLOAD_LIMIT_BYTES,
    configure_logging,
    load_deepseek_chat_auth_tokens,
    load_settings,
    save_deepseek_chat_auth_tokens,
)
from deepseek_client import DeepSeekDeveloperClient
from developer_input import (
    developer_input_support_text,
    is_supported_developer_input_file as is_supported_developer_attachment,
    read_developer_input_attachment,
)
from gemini_pool import GeminiClientPool
from news_fetcher import NewsFetcher, NewsFetcherAuthorizationError
from pdf_tools import (
    PDF_COMPRESSION_LEVEL_HIGH,
    PDF_COMPRESSION_LEVEL_LOW,
    PDF_COMPRESSION_LEVEL_MEDIUM,
    SUPPORTED_PDF_SUFFIXES,
    compress_pdf,
    get_pdf_page_count,
    merge_pdfs,
    slice_pdf,
)
from rubika_access_control import RubikaAccessControl
from telegram_media_downloader import TelegramMediaDownloader
from telegram_vpn_config_collector import (
    TelegramVpnConfigAuthorizationError,
    TelegramVpnConfigCollector,
)
from youtube_library import YouTubeLibraryStore
from video_downloader import (
    DownloadCancelledError,
    MEDIA_KIND_AUDIO,
    MEDIA_KIND_VIDEO,
    DownloadOption,
    DownloadedVideo,
    VideoDownloader,
)
from video_compressor import (
    SUPPORTED_VIDEO_SUFFIXES,
    VIDEO_METHOD_CPU,
    VIDEO_METHOD_GPU,
    VIDEO_METHOD_PARALLEL,
    compress_video_artifact,
    split_final,
)
from youtube_search import (
    YouTubeChannel,
    YouTubePlaylist,
    YouTubeSearcher,
    YouTubeVideo,
)
from web_search_tools import (
    GoogleWebSearchService,
    ImageSearchResult,
    ImageSearchService,
    WebSearchResult,
)


LOGGER = logging.getLogger(__name__)
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
CLOUD_BOT_API_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024
RUBIKA_MULTIPART_LIMIT_BYTES = 49 * 1024 * 1024
YOUTUBE_QUERY = 1
YOUTUBE_SEARCH_PAGE_SIZE = 5
WEB_SEARCH_PAGE_SIZE = 6
IMAGE_SEARCH_ALBUM_SIZE = 10
RUBIKA_IMAGE_SEARCH_BATCH_SIZE = 5
IMAGE_DOWNLOAD_MAX_BYTES = 7_500_000
AI_CHAT = 2
AUDIO_COMPRESS = 3
VIDEO_COMPRESS_METHOD = 4
VIDEO_COMPRESS_FILE = 5
PDF_COMPRESS_LEVEL = 6
PDF_COMPRESS = 7
PDF_SPLIT_FILE = 8
PDF_SPLIT_RANGE = 9
PDF_MERGE = 10
WEB_SEARCH_QUERY = 11
IMAGE_SEARCH_QUERY = 12
DEEPSEEK_SETTINGS_INPUT = 13
KEEP_DOWNLOADED_VIDEOS = False
LOCAL_BOT_API_SHARED_DOWNLOADS_PATH: Path | None = None
AI_END_CHAT_CALLBACK = "chat:end"
AI_SEARCH_YES_CALLBACK = "chat:search_yes"
AI_SEARCH_NO_CALLBACK = "chat:search_no"
WEB_SEARCH_MORE_CALLBACK = "ws:more"
QUALITY_CALLBACK_PREFIX = "ytq"
QUALITY_MODE_CALLBACK_PREFIX = "ytqm"
YOUTUBE_CANCEL_CALLBACK_PREFIX = "ytcx"
AI_SOURCES_HEADER = "🌐 Sources:"
AI_URL_PATTERN = re.compile(r"https?://[^\s<>()]+")
AI_SOURCE_LINE_PATTERN = re.compile(
    r"^\s*(?:(?P<prefix>[-•]|\d+[.)])\s*)?(?P<title>.+?):\s*(?P<url>https?://\S+)\s*$"
)
AI_MARKDOWN_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")
AI_MARKDOWN_BULLET_PATTERN = re.compile(r"^\s*\*\s+", re.MULTILINE)
AI_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
AI_PERSIAN_TEXT_PATTERN = re.compile(r"[\u0600-\u06FF]")
DEEPSEEK_TOKEN_ASSIGNMENT_PATTERN = re.compile(
    r"^[A-Za-z0-9_ -]*token[A-Za-z0-9_ -]*[:=]\s*(.+)$",
    re.IGNORECASE,
)
BTN_IRAN_INTL = "📰 ایران اینترنشنال"
BTN_VAHID = "📰 وحید آنلاین"
BTN_RADIO_FARDA = "📰 رادیو فردا"
BTN_INDY = "📰 ایندیپندنت فارسی"
BTN_BBC = "📰 بی‌بی‌سی فارسی"
BTN_MAIN_NEWS = "📰 اخبار"
BTN_MAIN_YOUTUBE = "📺 یوتیوب"
BTN_MAIN_FILE_TOOLS = "🛠 ابزار فایل"
BTN_MAIN_GENERAL_AI = "🌐 Web+Ai"
BTN_MAIN_SETTINGS = "⚙️ تنظیمات"
BTN_MAIN_SUPPORT_BUNDLE = "📦 بسته‌ی حمایتی"
BTN_NEWS_AI = "🤖 از AI درباره خبرها بپرس"
BTN_BACK = "🔙 بازگشت"
BTN_YOUTUBE_SEARCH = "🔎 جستجوی ویدیو"
BTN_YOUTUBE_DOWNLOAD = "⬇️ دانلود ویدیو"
BTN_YOUTUBE_MORE_RESULTS = "➡️ موارد بیشتر"
BTN_YOUTUBE_NEW_SEARCH = "🔎 جستجوی جدید"
BTN_YOUTUBE_CHANNEL_SEARCH = "📺 جستجو در کانال"
BTN_YOUTUBE_SAVED_CHANNELS = "📺 کانال‌های ذخیره‌شده"
BTN_YOUTUBE_PLAYLISTS = "📚 پلی‌لیست‌ها"
BTN_YOUTUBE_WATCH_LATER = "🕒 Watch Later"
BTN_YOUTUBE_WATCH_LATER_SEARCH = "🔎 جستجو در Watch Later"
BTN_YOUTUBE_RECENT_SEARCHES = "🕘 جستجوهای اخیر"
BTN_AUDIO_COMPRESSOR = "🎵 فشرده‌سازی صوت"
BTN_VIDEO_COMPRESSOR = "🎬 فشرده‌سازی ویدیو"
BTN_PDF_COMPRESSOR = "🗜️ فشرده‌سازی PDF"
BTN_PDF_SPLITTER = "✂️ تقسیم PDF"
BTN_PDF_MERGER = "🧩 ادغام PDF"
BTN_VPN_CONFIG_BUNDLE = BTN_MAIN_SUPPORT_BUNDLE
BTN_PDF_MERGE_RUN = "✅ شروع ادغام"
BTN_PDF_COMPRESS_LOW = "🟢 کم"
BTN_PDF_COMPRESS_MEDIUM = "🟡 متوسط"
BTN_PDF_COMPRESS_HIGH = "🔴 زیاد"
BTN_GENERAL_AI_START = "💬 شروع چت"
BTN_GENERAL_AI_WEB_SEARCH = "🔎 Google Search"
BTN_GENERAL_AI_IMAGE_SEARCH = "🖼 Google Images"
BTN_GENERAL_AI_MORE_RESULTS = "➡️ 6 نتیجه بعدی"
BTN_GENERAL_AI_MORE_IMAGES = "➡️ 10 تصویر بعدی"
BTN_GENERAL_AI_MORE_IMAGES_5 = "➡️ 5 تصویر بعدی"
BTN_GENERAL_AI_MORE_IMAGES_10 = "⏩ 10 تصویر بعدی"
BTN_GENERAL_AI_DEVELOPER = "🧑‍💻 حالت برنامه‌نویس"
BTN_AI_BACK_TO_MENU = "🔙 بازگشت به منو"
BTN_DEVELOPER_MODEL_GEMINI = "💎 Gemini Pro"
BTN_DEVELOPER_MODEL_DEEPSEEK_R1 = "🧠 DeepSeek R1"
BTN_DEVELOPER_MODEL_DEEPSEEK_R1_SEARCH = "🔎 DeepSeek R1 Search"
BTN_DEVELOPER_MODEL_DEEPSEEK_R1_SILENT = "🤫 DeepSeek R1 Silent"
BTN_DEVELOPER_PROMPT_ONLY = "📝 فقط پرامپت"
BTN_DEVELOPER_WITH_FILES = "📎 فایل + پرامپت"
BTN_DEVELOPER_ADD_FILES = "📎 افزودن فایل"
BTN_DEVELOPER_FILES_DONE = "✅ فایل‌ها کامل شد"
BTN_DEVELOPER_FILES_CLEAR = "🗑 پاک کردن فایل‌ها"
BTN_VIDEO_METHOD_CPU = "🧠 CPU"
BTN_VIDEO_METHOD_GPU = "⚡ GPU"
BTN_VIDEO_METHOD_PARALLEL = "🚀 Parallel"
MENU_MAIN = "main"
MENU_NEWS = "news"
MENU_YOUTUBE = "youtube"
MENU_YOUTUBE_BROWSE = "youtube_browse"
MENU_FILE_TOOLS = "file_tools"
MENU_GENERAL_AI = "general_ai"
MENU_AI_CHAT = "ai_chat"
MENU_SETTINGS = "settings"
MENU_SETTINGS_BROADCAST = "settings_broadcast"
MENU_SETTINGS_DEEPSEEK = "settings_deepseek"
DEVELOPER_STAGE_CHOOSE_MODEL = "choose_model"
DEVELOPER_STAGE_CHOOSE_INPUT = "choose_input"
DEVELOPER_STAGE_PROMPT_ONLY = "prompt_only"
DEVELOPER_STAGE_COLLECT_FILES = "collect_files"
DEVELOPER_STAGE_PROMPT_WITH_FILES = "prompt_with_files"
DEVELOPER_MODEL_GEMINI_PRO = "gemini-pro"
DEVELOPER_MODEL_DEEPSEEK_R1 = "deepseek-r1"
DEVELOPER_MODEL_DEEPSEEK_R1_SEARCH = "deepseek-r1-search"
DEVELOPER_MODEL_DEEPSEEK_R1_SILENT = "deepseek-r1-silent"
YOUTUBE_MODE_SEARCH = "search"
YOUTUBE_MODE_DOWNLOAD = "download"
YOUTUBE_MODE_CHANNEL_SEARCH = "channel_search"
YOUTUBE_MODE_WATCH_LATER_SEARCH = "watch_later_search"
YOUTUBE_RESULT_SOURCE_WATCH_LATER = "watch_later"
YOUTUBE_QUALITY_MODE_LOW = "low"
YOUTUBE_QUALITY_MODE_HIGH = "high"
YOUTUBE_QUALITY_MODE_AUDIO = "audio"
RUBIKA_USER_STATUS_PENDING = "pending"
RUBIKA_USER_STATUS_ALLOWED = "allowed"
RUBIKA_USER_STATUS_SUSPENDED = "suspended"
RUBIKA_USER_STATUS_BLOCKED = "blocked"
RUBIKA_ACCESS_COMMANDS = {
    "rubika_access",
    "rubika_users",
    "rubika_pending",
    "rubika_admins",
    "rubika_allow",
    "rubika_suspend",
    "rubika_block",
    "rubika_admin_add",
    "rubika_admin_remove",
}
RUBIKA_OWNER_ONLY_COMMANDS = {
    "rubika_admin_add",
    "rubika_admin_remove",
}
TELEGRAM_ACCESS_COMMANDS = {
    "telegram_access",
    "telegram_users",
    "telegram_pending",
    "telegram_admins",
    "telegram_allow",
    "telegram_suspend",
    "telegram_block",
    "telegram_admin_add",
    "telegram_admin_remove",
}
TELEGRAM_OWNER_ONLY_COMMANDS = {
    "telegram_admin_add",
    "telegram_admin_remove",
}
BTN_SETTINGS_PENDING = "🕓 درخواست‌ها"
BTN_SETTINGS_USERS = "👥 کاربران"
BTN_SETTINGS_ADMINS = "🛡 ادمین‌ها"
BTN_SETTINGS_DEEPSEEK = "🧠 DeepSeek token"
BTN_SETTINGS_BROADCAST = "📣 پیام همگانی"
BTN_SETTINGS_RESET = "♻️ ریست دسترسی‌ها"
BTN_REVIEW_APPROVE = "✅ بله"
BTN_REVIEW_REJECT = "❌ نه"
BTN_REVIEW_ROLE_USER = "👤 کاربر عادی"
BTN_REVIEW_ROLE_ADMIN = "🛡 ادمین"
BTN_REVIEW_CANCEL = "↩️ لغو بررسی"
RUBIKA_SETTINGS_VIEW_CALLBACK_PREFIX = "rbsv"
RUBIKA_SETTINGS_USER_CALLBACK_PREFIX = "rbsu"
RUBIKA_SETTINGS_ACTION_CALLBACK_PREFIX = "rbsa"
RUBIKA_SETTINGS_RESET_CALLBACK_PREFIX = "rbsr"
YOUTUBE_SEARCH_PAGE_CALLBACK_PREFIX = "ytsp"
YOUTUBE_SEARCH_RESULT_CALLBACK_PREFIX = "ytsr"
YOUTUBE_OPEN_CHANNEL_CALLBACK_PREFIX = "ytoch"
YOUTUBE_LIBRARY_CALLBACK_PREFIX = "ytlib"
YOUTUBE_BROWSER_ACTION_CALLBACK_PREFIX = "ytbra"
YOUTUBE_BROWSER_ITEM_CALLBACK_PREFIX = "ytbri"
YOUTUBE_BROWSER_PAGE_CALLBACK_PREFIX = "ytbrp"
RUBIKA_SETTINGS_VIEW_PENDING = "pending"
RUBIKA_SETTINGS_VIEW_ALL = "all"
RUBIKA_SETTINGS_VIEW_ADMINS = "admins"
RUBIKA_BROADCAST_STAGE_AWAITING = "awaiting_message"
FILTER_AUDIO_UPLOAD = (
    filters.AUDIO
    | filters.Document.FileExtension("m4a")
    | filters.Document.FileExtension("mp3")
    | filters.Document.MimeType("audio/mp4")
    | filters.Document.MimeType("audio/x-m4a")
    | filters.Document.MimeType("audio/mpeg")
    | filters.Document.MimeType("audio/mp3")
)
FILTER_VIDEO_UPLOAD = (
    filters.VIDEO
    | filters.Document.FileExtension("mp4")
    | filters.Document.FileExtension("mov")
    | filters.Document.FileExtension("mkv")
    | filters.Document.FileExtension("avi")
    | filters.Document.FileExtension("m4v")
    | filters.Document.MimeType("video/mp4")
    | filters.Document.MimeType("video/quicktime")
    | filters.Document.MimeType("video/x-matroska")
    | filters.Document.MimeType("video/x-msvideo")
)
FILTER_PDF_UPLOAD = (
    filters.Document.FileExtension("pdf")
    | filters.Document.MimeType("application/pdf")
)
KEEP_REPLY_MARKUP = object()
DEVELOPER_MAX_INPUT_FILE_BYTES = 8 * 1024 * 1024
DEVELOPER_MAX_INPUT_TEXT_CHARS = 120_000
DEVELOPER_MAX_REPLY_MESSAGES = 3
DEVELOPER_REPLY_MESSAGE_LIMIT = MAX_TELEGRAM_MESSAGE_LENGTH
AI_CHAT_ENDED_TEXT = "گفتگو با AI تمام شد.\nبه منوی اصلی برگشتی."
DEVELOPER_MAX_INPUT_FILES = 5
DEVELOPER_MAX_TOTAL_INPUT_TEXT_CHARS = 180_000
DEVELOPER_TEXT_FILE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".dart",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".kts",
    ".lua",
    ".md",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".svg",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
    ".zsh",
}
DEVELOPER_TEXT_MIME_PREFIXES = ("text/",)
DEVELOPER_TEXT_MIME_TYPES = {
    "application/javascript",
    "application/json",
    "application/sql",
    "application/toml",
    "application/typescript",
    "application/x-httpd-php",
    "application/x-python-code",
    "application/xml",
}


def _button_match_key(text: str) -> str:
    """Return a normalized lookup key for reply-keyboard labels."""
    normalized = text.replace("\ufe0f", "").replace("\u200d", "")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.casefold()


def _button_plain_text(text: str) -> str:
    """Strip leading emoji/symbol prefix from a button label."""
    cleaned = text.replace("\ufe0f", "").replace("\u200d", "").strip()
    for index, char in enumerate(cleaned):
        if char.isalnum() or "\u0600" <= char <= "\u06FF":
            return cleaned[index:].strip()
    return cleaned


BUTTON_LABELS = (
    BTN_IRAN_INTL,
    BTN_VAHID,
    BTN_RADIO_FARDA,
    BTN_INDY,
    BTN_BBC,
    BTN_MAIN_NEWS,
    BTN_MAIN_YOUTUBE,
    BTN_MAIN_FILE_TOOLS,
    BTN_MAIN_GENERAL_AI,
    BTN_MAIN_SETTINGS,
    BTN_NEWS_AI,
    BTN_BACK,
    BTN_YOUTUBE_SEARCH,
    BTN_YOUTUBE_DOWNLOAD,
    BTN_YOUTUBE_MORE_RESULTS,
    BTN_YOUTUBE_NEW_SEARCH,
    BTN_YOUTUBE_CHANNEL_SEARCH,
    BTN_YOUTUBE_SAVED_CHANNELS,
    BTN_YOUTUBE_PLAYLISTS,
    BTN_YOUTUBE_WATCH_LATER,
    BTN_YOUTUBE_WATCH_LATER_SEARCH,
    BTN_YOUTUBE_RECENT_SEARCHES,
    BTN_AUDIO_COMPRESSOR,
    BTN_VIDEO_COMPRESSOR,
    BTN_PDF_COMPRESSOR,
    BTN_PDF_SPLITTER,
    BTN_PDF_MERGER,
    BTN_VPN_CONFIG_BUNDLE,
    BTN_PDF_MERGE_RUN,
    BTN_PDF_COMPRESS_LOW,
    BTN_PDF_COMPRESS_MEDIUM,
    BTN_PDF_COMPRESS_HIGH,
    BTN_GENERAL_AI_START,
    BTN_GENERAL_AI_WEB_SEARCH,
    BTN_GENERAL_AI_IMAGE_SEARCH,
    BTN_GENERAL_AI_MORE_RESULTS,
    BTN_GENERAL_AI_MORE_IMAGES,
    BTN_GENERAL_AI_MORE_IMAGES_5,
    BTN_GENERAL_AI_MORE_IMAGES_10,
    BTN_GENERAL_AI_DEVELOPER,
    BTN_AI_BACK_TO_MENU,
    BTN_DEVELOPER_MODEL_GEMINI,
    BTN_DEVELOPER_MODEL_DEEPSEEK_R1,
    BTN_DEVELOPER_MODEL_DEEPSEEK_R1_SEARCH,
    BTN_DEVELOPER_MODEL_DEEPSEEK_R1_SILENT,
    BTN_DEVELOPER_PROMPT_ONLY,
    BTN_DEVELOPER_WITH_FILES,
    BTN_DEVELOPER_FILES_DONE,
    BTN_DEVELOPER_FILES_CLEAR,
    BTN_VIDEO_METHOD_CPU,
    BTN_VIDEO_METHOD_GPU,
    BTN_VIDEO_METHOD_PARALLEL,
    BTN_SETTINGS_PENDING,
    BTN_SETTINGS_USERS,
    BTN_SETTINGS_ADMINS,
    BTN_SETTINGS_BROADCAST,
    BTN_SETTINGS_RESET,
    BTN_REVIEW_APPROVE,
    BTN_REVIEW_REJECT,
    BTN_REVIEW_ROLE_USER,
    BTN_REVIEW_ROLE_ADMIN,
    BTN_REVIEW_CANCEL,
)
BUTTON_CANONICAL_BY_KEY = {
    _button_match_key(variant): label
    for label in BUTTON_LABELS
    for variant in {label, _button_plain_text(label)}
    if variant
}
DEVELOPER_MODEL_BUTTON_TO_VALUE = {
    BTN_DEVELOPER_MODEL_GEMINI: DEVELOPER_MODEL_GEMINI_PRO,
    BTN_DEVELOPER_MODEL_DEEPSEEK_R1: DEVELOPER_MODEL_DEEPSEEK_R1,
    BTN_DEVELOPER_MODEL_DEEPSEEK_R1_SEARCH: DEVELOPER_MODEL_DEEPSEEK_R1_SEARCH,
    BTN_DEVELOPER_MODEL_DEEPSEEK_R1_SILENT: DEVELOPER_MODEL_DEEPSEEK_R1_SILENT,
}
DEVELOPER_MODEL_VALUE_TO_BUTTON = {
    value: label for label, value in DEVELOPER_MODEL_BUTTON_TO_VALUE.items()
}


def button_pattern(*labels: str) -> str:
    """Build a safe regex for exact reply-keyboard label matches."""
    return "^(" + "|".join(re.escape(label) for label in labels) + ")$"


def canonicalize_menu_button_text(text: str) -> str:
    """Map plain/no-emoji Rubika button text back to the canonical button label."""
    cleaned = re.sub(r"\s+", " ", text.strip())
    if not cleaned:
        return cleaned
    for candidate in (cleaned, _button_plain_text(cleaned)):
        mapped = BUTTON_CANONICAL_BY_KEY.get(_button_match_key(candidate))
        if mapped:
            return mapped
    return cleaned

NEWS_BUTTON_SOURCES = {
    BTN_IRAN_INTL: NEWS_SOURCES["iranintl"],
    BTN_VAHID: NEWS_SOURCES["vahid"],
    BTN_RADIO_FARDA: NEWS_SOURCES["radiofarda"],
    BTN_INDY: NEWS_SOURCES["indypersian"],
    BTN_BBC: NEWS_SOURCES["bbcpersian"],
}

START_TEXT = """سلام.

از منوی پایین یکی از بخش‌ها را انتخاب کن:

📰 اخبار
منابع خبری فعلی را می‌بینی و می‌توانی خلاصه هر منبع را بگیری یا درباره خبرها با AI حرف بزنی.

📺 یوتیوب
بخش جستجو و دانلود ویدیو از اینجا در دسترس است.

🛠 ابزار فایل
ابزارهای آینده برای فایل‌ها اینجا قرار می‌گیرند.

🌐 Web+Ai
قابلیت‌های هوشمند ربات از اینجا در دسترس است.

برای حفظ کیفیت ربات:
- لطفاً از پخش کردن گسترده ربات خودداری کن.
- درخواست‌های پشت سر هم و رگباری نفرست.
- بین درخواست‌ها کمی فاصله بگذار تا بقیه کاربران هم روان‌تر استفاده کنند.

اگر خواستی هنوز می‌توانی از دستورهای /news /youtube /tools /ai /help هم استفاده کنی.
"""

SEARCH_ACTIVITY_LOG_PATH = Path(__file__).resolve().parent / "runtime_logs" / "user_search_activity.jsonl"
YOUTUBE_LIBRARY_STORAGE_PATH = Path(__file__).resolve().parent / "runtime_logs" / "youtube_user_library.json"
TELEGRAM_ACCESS_CONTROL_PATH = Path(__file__).resolve().parent / "telegram_access_control.json"
SEARCH_ACTIVITY_LOG_LOCK = threading.Lock()

BOT_COMMANDS: list[tuple[str, str]] = [
    ("start", "بازگشت به منوی اصلی"),
    ("news", "بخش اخبار"),
    ("youtube", "بخش یوتیوب"),
    ("tools", "بخش ابزار فایل"),
    ("ai", "چت مستقیم با Gemini"),
    ("help", "راهنمای ربات"),
]


def build_reply_menu(
    rows: list[list[str]],
    *,
    placeholder: str,
) -> ReplyKeyboardMarkup:
    """Build a persistent reply keyboard from button labels."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text) for text in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=placeholder,
    )


def build_main_menu(
    *,
    include_settings: bool = False,
    include_support_bundle: bool = True,
) -> ReplyKeyboardMarkup:
    """Build the main menu keyboard."""
    rows = [
        [BTN_MAIN_NEWS, BTN_MAIN_YOUTUBE],
        [BTN_MAIN_FILE_TOOLS, BTN_MAIN_GENERAL_AI],
    ]
    if include_support_bundle:
        rows.append([BTN_MAIN_SUPPORT_BUNDLE])
    if include_settings:
        rows.append([BTN_MAIN_SETTINGS])
    return build_reply_menu(rows, placeholder="یکی از بخش‌ها را انتخاب کن...")


def build_news_menu() -> ReplyKeyboardMarkup:
    """Build the news submenu keyboard."""
    return build_reply_menu(
        [
            [BTN_IRAN_INTL, BTN_VAHID],
            [BTN_RADIO_FARDA, BTN_INDY],
            [BTN_BBC],
            [BTN_NEWS_AI],
            [BTN_BACK],
        ],
        placeholder="منبع خبری یا گزینه دلخواه را انتخاب کن...",
    )


def build_youtube_menu() -> ReplyKeyboardMarkup:
    """Build the YouTube submenu keyboard."""
    return build_reply_menu(
        [
            [BTN_YOUTUBE_DOWNLOAD, BTN_YOUTUBE_SEARCH],
            [BTN_YOUTUBE_SAVED_CHANNELS, BTN_YOUTUBE_PLAYLISTS],
            [BTN_YOUTUBE_WATCH_LATER, BTN_YOUTUBE_RECENT_SEARCHES],
            [BTN_BACK],
        ],
        placeholder="یکی از گزینه‌های یوتیوب را انتخاب کن...",
    )


def build_youtube_results_menu(
    *,
    has_more_results: bool,
    include_watch_later_search: bool = False,
) -> ReplyKeyboardMarkup:
    """Build the reply keyboard used while browsing YouTube search results."""
    rows = []
    if has_more_results:
        rows.append([BTN_YOUTUBE_MORE_RESULTS])
    if include_watch_later_search:
        rows.append([BTN_YOUTUBE_WATCH_LATER_SEARCH])
    rows.append([BTN_YOUTUBE_NEW_SEARCH, BTN_BACK])
    return build_reply_menu(rows, placeholder="یکی از گزینه‌های یوتیوب را انتخاب کن...")


def build_youtube_browse_menu() -> ReplyKeyboardMarkup:
    """Build the reply keyboard used while browsing a YouTube channel."""
    return build_reply_menu(
        [
            [BTN_YOUTUBE_CHANNEL_SEARCH, BTN_YOUTUBE_NEW_SEARCH],
            [BTN_BACK],
        ],
        placeholder="کانال را مرور کن یا داخل آن جستجو کن...",
    )


def build_file_tools_menu() -> ReplyKeyboardMarkup:
    """Build the file tools submenu keyboard."""
    return build_reply_menu(
        [
            [BTN_AUDIO_COMPRESSOR, BTN_VIDEO_COMPRESSOR],
            [BTN_PDF_COMPRESSOR, BTN_PDF_SPLITTER],
            [BTN_PDF_MERGER],
            [BTN_BACK],
        ],
        placeholder="ابزار فایل موردنظر را انتخاب کن...",
    )


def build_file_tools_menu_for_context(context: ContextTypes.DEFAULT_TYPE) -> ReplyKeyboardMarkup:
    """Build the file tools submenu."""
    rows = [
        [BTN_AUDIO_COMPRESSOR, BTN_VIDEO_COMPRESSOR],
        [BTN_PDF_COMPRESSOR, BTN_PDF_SPLITTER],
        [BTN_PDF_MERGER],
    ]
    rows.append([BTN_BACK])
    return build_reply_menu(rows, placeholder="ابزار فایل موردنظر را انتخاب کن...")


def build_general_ai_menu() -> ReplyKeyboardMarkup:
    """Build the general AI submenu keyboard."""
    rows = [[BTN_GENERAL_AI_START]]
    rows.append([BTN_BACK])
    return build_reply_menu(rows, placeholder="یکی از گزینه‌های Web+Ai را انتخاب کن...")


def build_general_ai_menu_for_context(context: ContextTypes.DEFAULT_TYPE) -> ReplyKeyboardMarkup:
    """Build the AI submenu, exposing developer mode only to allowed admins."""
    rows = [[BTN_GENERAL_AI_START]]
    rows.append([BTN_GENERAL_AI_WEB_SEARCH, BTN_GENERAL_AI_IMAGE_SEARCH])
    if is_developer_mode_allowed(context):
        rows.append([BTN_GENERAL_AI_DEVELOPER])
    rows.append([BTN_BACK])
    return build_reply_menu(rows, placeholder="یکی از گزینه‌های Web+Ai را انتخاب کن...")


def build_web_search_results_menu(*, has_more_results: bool) -> ReplyKeyboardMarkup:
    """Build the keyboard used while browsing compact web-search results."""
    rows = []
    if has_more_results:
        rows.append([BTN_GENERAL_AI_MORE_RESULTS])
    rows.append([BTN_BACK])
    return build_reply_menu(
        rows,
        placeholder="عبارت جدید را بنویس یا نتیجه بعدی را بزن...",
    )


def build_image_search_results_menu(
    *,
    transport_name: str,
    has_more_results: bool,
    has_ten_more_results: bool = False,
) -> ReplyKeyboardMarkup:
    """Build the keyboard used while browsing image search results."""
    rows = []
    if transport_name == "rubika":
        if has_more_results and has_ten_more_results:
            rows.append([BTN_GENERAL_AI_MORE_IMAGES_5, BTN_GENERAL_AI_MORE_IMAGES_10])
        elif has_more_results:
            rows.append([BTN_GENERAL_AI_MORE_IMAGES_5])
    elif has_more_results:
        rows.append([BTN_GENERAL_AI_MORE_IMAGES])
    rows.append([BTN_BACK])
    if transport_name == "rubika":
        placeholder = "عبارت جدید را بنویس یا 5/10 تصویر بعدی را بزن..."
    else:
        placeholder = "عبارت جدید را بنویس یا 10 تصویر بعدی را بزن..."
    return build_reply_menu(
        rows,
        placeholder=placeholder,
    )


def build_developer_prompt_only_menu() -> ReplyKeyboardMarkup:
    """Build the default Developer Mode keyboard while chatting without files."""
    return build_reply_menu(
        [
            [BTN_DEVELOPER_ADD_FILES],
            [BTN_BACK],
        ],
        placeholder="پرامپتت را برای Developer Mode بنویس...",
    )


def build_developer_model_menu(
    model_rows: list[list[str]],
) -> ReplyKeyboardMarkup:
    """Build the model picker shown before Telegram Developer Mode starts."""
    rows = [list(row) for row in model_rows]
    rows.append([BTN_BACK])
    return build_reply_menu(rows, placeholder="مدل Developer Mode را انتخاب کن...")


def build_developer_file_collection_menu() -> ReplyKeyboardMarkup:
    """Build the keyboard used while collecting Developer Mode files."""
    return build_reply_menu(
        [
            [BTN_DEVELOPER_FILES_DONE, BTN_DEVELOPER_FILES_CLEAR],
            [BTN_BACK],
        ],
        placeholder="فایل‌ها را یکی‌یکی بفرست...",
    )


def build_developer_prompt_with_files_menu() -> ReplyKeyboardMarkup:
    """Build the keyboard used while prompting against uploaded files."""
    return build_reply_menu(
        [
            [BTN_DEVELOPER_ADD_FILES, BTN_DEVELOPER_FILES_CLEAR],
            [BTN_BACK],
        ],
        placeholder="پرامپتت را درباره فایل‌ها بنویس...",
    )


def build_settings_menu(*, is_owner: bool) -> ReplyKeyboardMarkup:
    """Build the shared staff settings keyboard."""
    rows = [
        [BTN_SETTINGS_PENDING, BTN_SETTINGS_USERS],
        [BTN_SETTINGS_ADMINS],
        [BTN_SETTINGS_DEEPSEEK],
    ]
    if is_owner:
        rows.append([BTN_SETTINGS_BROADCAST])
        rows.append([BTN_SETTINGS_RESET])
    rows.append([BTN_BACK])
    return build_reply_menu(rows, placeholder="یکی از گزینه‌های تنظیمات را انتخاب کن...")


def build_rubika_broadcast_menu() -> ReplyKeyboardMarkup:
    """Build the owner-only broadcast compose keyboard."""
    return build_reply_menu(
        [[BTN_BACK]],
        placeholder="پیام همگانی را بنویس...",
    )


def build_deepseek_settings_menu() -> ReplyKeyboardMarkup:
    """Build the keyboard used while collecting a DeepSeek token from staff."""
    return build_reply_menu(
        [[BTN_BACK]],
        placeholder="توکن DeepSeek را بفرست...",
    )


def build_rubika_review_decision_menu() -> ReplyKeyboardMarkup:
    """Build the reply keyboard for approving/rejecting a new Rubika join request."""
    return build_reply_menu(
        [
            [BTN_REVIEW_APPROVE, BTN_REVIEW_REJECT],
            [BTN_REVIEW_CANCEL],
        ],
        placeholder="درخواست دسترسی را بررسی کن...",
    )


def build_rubika_review_role_menu(*, is_owner: bool) -> ReplyKeyboardMarkup:
    """Build the reply keyboard for choosing the approved user's access level."""
    rows = [[BTN_REVIEW_ROLE_USER]]
    if is_owner:
        rows[0].append(BTN_REVIEW_ROLE_ADMIN)
    rows.append([BTN_REVIEW_CANCEL])
    return build_reply_menu(rows, placeholder="سطح دسترسی را انتخاب کن...")


def build_ai_chat_menu() -> ReplyKeyboardMarkup:
    """Build the reply keyboard for an active AI chat session."""
    return build_reply_menu(
        [[BTN_AI_BACK_TO_MENU]],
        placeholder="پیام‌ات را برای AI بنویس...",
    )


def build_video_method_menu() -> ReplyKeyboardMarkup:
    """Build the video compression method keyboard."""
    return build_reply_menu(
        [
            [BTN_VIDEO_METHOD_CPU, BTN_VIDEO_METHOD_GPU],
            [BTN_VIDEO_METHOD_PARALLEL],
            [BTN_BACK],
        ],
        placeholder="روش فشرده‌سازی ویدیو را انتخاب کن...",
    )


def build_pdf_merge_menu() -> ReplyKeyboardMarkup:
    """Build the PDF merge action keyboard."""
    return build_reply_menu(
        [
            [BTN_PDF_MERGE_RUN],
            [BTN_BACK],
        ],
        placeholder="فایل‌های PDF را بفرست یا ادغام را شروع کن...",
    )


def build_pdf_compression_menu() -> ReplyKeyboardMarkup:
    """Build the PDF compression level keyboard."""
    return build_reply_menu(
        [
            [BTN_PDF_COMPRESS_LOW, BTN_PDF_COMPRESS_MEDIUM],
            [BTN_PDF_COMPRESS_HIGH],
            [BTN_BACK],
        ],
        placeholder="شدت فشرده‌سازی PDF را انتخاب کن...",
    )


def build_back_only_menu(placeholder: str) -> ReplyKeyboardMarkup:
    """Build a minimal keyboard with only a back button."""
    return build_reply_menu(
        [[BTN_BACK]],
        placeholder=placeholder,
    )


def truncate_button_label(text: str, limit: int = 42) -> str:
    """Keep inline button labels compact enough for Telegram and Rubika."""
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1].rstrip()}…"


def serialize_web_search_results(results: list[WebSearchResult]) -> list[dict[str, str]]:
    """Convert result objects into user-data-safe dictionaries."""
    return [
        {
            "title": result.title,
            "url": result.url,
            "snippet": result.snippet,
        }
        for result in results
    ]


def serialize_image_search_results(results: list[ImageSearchResult]) -> list[dict[str, str]]:
    """Convert image search result objects into user-data-safe dictionaries."""
    return [
        {
            "image_url": result.image_url,
            "thumbnail_url": result.thumbnail_url,
            "source_url": result.source_url,
            "title": result.title,
        }
        for result in results
    ]


def deserialize_image_search_results(results: list[dict[str, str]]) -> list[ImageSearchResult]:
    """Restore image result objects from user-data-safe dictionaries."""
    restored: list[ImageSearchResult] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        restored.append(
            ImageSearchResult(
                image_url=str(item.get("image_url") or "").strip(),
                thumbnail_url=str(item.get("thumbnail_url") or "").strip(),
                source_url=str(item.get("source_url") or "").strip(),
                title=str(item.get("title") or "").strip(),
            )
        )
    return restored


def build_web_search_results_text(
    query: str,
    results: list[dict[str, str]],
    *,
    page: int,
    use_html: bool,
    include_urls: bool = True,
) -> str:
    """Render one compact page of web search results."""
    start = max(page, 0) * WEB_SEARCH_PAGE_SIZE
    page_items = results[start : start + WEB_SEARCH_PAGE_SIZE]
    end = start + len(page_items)

    if use_html:
        lines = [
            f"🔎 <b>Google Search:</b> {html.escape(query)}",
            f"نتایج {start + 1} تا {end} از {len(results)}",
            "",
        ]
        for index, item in enumerate(page_items, start=start + 1):
            url = html.escape(str(item.get('url') or '').strip(), quote=True)
            title = html.escape(str(item.get('title') or '').strip() or "Result")
            snippet = html.escape(str(item.get('snippet') or '').strip())
            lines.append(f"{index}. <a href=\"{url}\">{title}</a>")
            if snippet:
                lines.append(snippet)
            lines.append("")
        return "\n".join(lines).strip()

    lines = [
        f"🔎 Google Search: {query}",
        f"نتایج {start + 1} تا {end} از {len(results)}",
        "",
    ]
    if not include_urls:
        lines.append("برای باز کردن نتیجه‌ها از دکمه‌های زیر استفاده کن.")
        lines.append("")
    for index, item in enumerate(page_items, start=start + 1):
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip() or "Result"
        snippet = str(item.get("snippet") or "").strip()
        lines.append(f"{index}. {title}")
        if url and include_urls:
            lines.append(url)
        if snippet:
            lines.append(snippet)
        lines.append("")
    return "\n".join(lines).strip()


def build_rubika_web_search_inline_keyboard(
    results: list[dict[str, str]],
    *,
    page: int,
) -> InlineKeyboardMarkup:
    """Build Rubika-friendly link buttons for one web search result page."""
    start = max(page, 0) * WEB_SEARCH_PAGE_SIZE
    page_items = results[start : start + WEB_SEARCH_PAGE_SIZE]
    has_more_results = start + WEB_SEARCH_PAGE_SIZE < len(results)
    rows: list[list[InlineKeyboardButton]] = []

    for index, item in enumerate(page_items, start=start + 1):
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        title = str(item.get("title") or "").strip() or "Result"
        rows.append(
            [
                InlineKeyboardButton(
                    text=truncate_button_label(f"{index}. {title}", limit=44),
                    url=url,
                )
            ]
        )

    if has_more_results:
        rows.append(
            [
                InlineKeyboardButton(
                    text=BTN_GENERAL_AI_MORE_RESULTS,
                    callback_data=WEB_SEARCH_MORE_CALLBACK,
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def _utf16_unit_length(text: str) -> int:
    """Return the UTF-16 code-unit length Rubika metadata expects."""
    return len(text.encode("utf-16-le")) // 2


def build_rubika_web_search_text_and_metadata(
    query: str,
    results: list[dict[str, str]],
    *,
    page: int,
) -> tuple[str, dict[str, object] | None]:
    """Build Rubika result text plus hyperlink metadata for visible titles."""
    start = max(page, 0) * WEB_SEARCH_PAGE_SIZE
    page_items = results[start : start + WEB_SEARCH_PAGE_SIZE]
    end = start + len(page_items)

    parts: list[dict[str, object]] = []
    chunks: list[str] = []

    def append(text: str) -> None:
        chunks.append(text)

    append(f"🔎 Google Search: {query}\n")
    append(f"نتایج {start + 1} تا {end} از {len(results)}\n\n")

    for index, item in enumerate(page_items, start=start + 1):
        title = str(item.get("title") or "").strip() or "Result"
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("snippet") or "").strip()

        append(f"{index}. ")
        title_offset = _utf16_unit_length("".join(chunks))
        append(title)
        if url:
            parts.append(
                {
                    "type": "Link",
                    "from_index": title_offset,
                    "length": _utf16_unit_length(title),
                    "link_url": url,
                    "link": {
                        "type": "hyperlink",
                        "hyperlink_data": {"url": url},
                    },
                }
            )
        append("\n")
        if snippet:
            append(f"{snippet}\n")
        append("\n")

    text = "".join(chunks).strip()
    metadata = {"meta_data_parts": parts} if parts else None
    return text, metadata


def build_rubika_web_search_navigation_keyboard(*, has_more_results: bool) -> InlineKeyboardMarkup | None:
    """Build the minimal Rubika inline keyboard for search pagination."""
    if not has_more_results:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text=BTN_GENERAL_AI_MORE_RESULTS, callback_data=WEB_SEARCH_MORE_CALLBACK)]]
    )


def guess_image_extension(content_type: str) -> str:
    """Map a MIME type to a safe image filename extension."""
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ".jpg"
    if guessed == ".jpe":
        return ".jpg"
    return guessed


async def download_image_candidate(
    client: httpx.AsyncClient,
    result: ImageSearchResult,
) -> tuple[bytes, str] | None:
    """Download a single image candidate, falling back to its thumbnail when needed."""
    for candidate_url in (result.image_url, result.thumbnail_url):
        url = str(candidate_url or "").strip()
        if not url:
            continue
        headers = {"User-Agent": "Mozilla/5.0"}
        if result.source_url:
            headers["Referer"] = result.source_url
        try:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").strip()
                if not content_type.startswith("image/"):
                    continue
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > IMAGE_DOWNLOAD_MAX_BYTES:
                    continue
                chunks: list[bytes] = []
                total_bytes = 0
                async for chunk in response.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > IMAGE_DOWNLOAD_MAX_BYTES:
                        chunks = []
                        break
                    chunks.append(chunk)
                if chunks:
                    return b"".join(chunks), content_type
        except Exception:
            LOGGER.debug("Failed to download image candidate %s", url, exc_info=True)
            continue
    return None


async def build_image_search_media_group(
    results: list[ImageSearchResult],
    *,
    query: str,
    limit: int = IMAGE_SEARCH_ALBUM_SIZE,
) -> list[InputMediaPhoto]:
    """Download enough image results to build one Telegram album."""
    media_items: list[InputMediaPhoto] = []
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=20.0,
    ) as client:
        for index, result in enumerate(results, start=1):
            downloaded = await download_image_candidate(client, result)
            if downloaded is None:
                continue
            image_bytes, content_type = downloaded
            extension = guess_image_extension(content_type)
            image_buffer = BytesIO(image_bytes)
            image_buffer.name = f"image_search_{index}{extension}"
            caption = None
            if not media_items:
                caption = f"🖼 نتایج تصویری برای: {query}"
            media_items.append(InputMediaPhoto(media=image_buffer, caption=caption))
            if len(media_items) >= limit:
                break
    return media_items


async def build_image_search_uploads(
    results: list[ImageSearchResult],
    *,
    query: str,
    limit: int = IMAGE_SEARCH_ALBUM_SIZE,
) -> list[tuple[BytesIO, str | None]]:
    """Download enough image results to build one transport-neutral page."""
    uploads: list[tuple[BytesIO, str | None]] = []
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=20.0,
    ) as client:
        for index, result in enumerate(results, start=1):
            downloaded = await download_image_candidate(client, result)
            if downloaded is None:
                continue
            image_bytes, content_type = downloaded
            extension = guess_image_extension(content_type)
            image_buffer = BytesIO(image_bytes)
            image_buffer.name = f"image_search_{index}{extension}"
            caption = None
            if not uploads:
                caption = f"🖼 نتایج تصویری برای: {query}"
            uploads.append((image_buffer, caption))
            if len(uploads) >= limit:
                break
    return uploads


def format_video_duration_label(duration_seconds: int | None) -> str:
    """Format a duration label from an integer second count."""
    if duration_seconds is None:
        return "Unknown"
    total_seconds = max(int(duration_seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def serialize_youtube_video(video: YouTubeVideo) -> dict[str, str | int | None]:
    """Convert a YouTube search result into a callback-safe dict."""
    return {
        "video_id": video.video_id,
        "title": video.title,
        "duration_seconds": video.duration_seconds,
        "channel": video.channel,
        "webpage_url": video.webpage_url,
        "thumbnail_url": video.thumbnail_url,
        "channel_id": video.channel_id,
        "channel_url": video.channel_url,
        "uploader_id": video.uploader_id,
        "uploader_url": video.uploader_url,
        "published_timestamp": video.published_timestamp,
    }


def deserialize_youtube_video(payload: dict[str, str | int | None]) -> YouTubeVideo:
    """Convert a stored search-result payload back into a YouTubeVideo object."""
    duration_seconds = payload.get("duration_seconds")
    published_timestamp = payload.get("published_timestamp")
    return YouTubeVideo(
        video_id=str(payload.get("video_id") or ""),
        title=str(payload.get("title") or ""),
        duration_seconds=(
            int(duration_seconds)
            if isinstance(duration_seconds, (int, float))
            else None
        ),
        channel=str(payload.get("channel") or "Unknown"),
        webpage_url=str(payload.get("webpage_url") or ""),
        channel_id=(
            str(payload.get("channel_id"))
            if payload.get("channel_id")
            else None
        ),
        channel_url=(
            str(payload.get("channel_url"))
            if payload.get("channel_url")
            else None
        ),
        uploader_id=(
            str(payload.get("uploader_id"))
            if payload.get("uploader_id")
            else None
        ),
        uploader_url=(
            str(payload.get("uploader_url"))
            if payload.get("uploader_url")
            else None
        ),
        published_timestamp=(
            int(published_timestamp)
            if isinstance(published_timestamp, (int, float))
            else None
        ),
    )


def serialize_youtube_channel(channel: YouTubeChannel) -> dict[str, str | int | None]:
    """Convert channel metadata into a callback-safe dict."""
    return {
        "channel_id": channel.channel_id,
        "title": channel.title,
        "channel_url": channel.channel_url,
        "uploader_id": channel.uploader_id,
        "uploader_url": channel.uploader_url,
        "webpage_url": channel.webpage_url,
        "item_count": channel.item_count,
    }


def deserialize_youtube_channel(payload: dict[str, str | int | None]) -> YouTubeChannel:
    """Convert stored channel metadata back into a YouTubeChannel object."""
    item_count = payload.get("item_count")
    return YouTubeChannel(
        channel_id=str(payload.get("channel_id") or ""),
        title=str(payload.get("title") or "Unknown channel"),
        channel_url=str(payload.get("channel_url") or ""),
        uploader_id=(
            str(payload.get("uploader_id"))
            if payload.get("uploader_id")
            else None
        ),
        uploader_url=(
            str(payload.get("uploader_url"))
            if payload.get("uploader_url")
            else None
        ),
        webpage_url=(
            str(payload.get("webpage_url"))
            if payload.get("webpage_url")
            else None
        ),
        item_count=(
            int(item_count)
            if isinstance(item_count, (int, float))
            else None
        ),
    )


def serialize_youtube_playlist(playlist: YouTubePlaylist) -> dict[str, str | int | None]:
    """Convert playlist metadata into a callback-safe dict."""
    return {
        "playlist_id": playlist.playlist_id,
        "title": playlist.title,
        "webpage_url": playlist.webpage_url,
        "channel": playlist.channel,
        "channel_id": playlist.channel_id,
        "uploader_id": playlist.uploader_id,
    }


def deserialize_youtube_playlist(payload: dict[str, str | int | None]) -> YouTubePlaylist:
    """Convert stored playlist metadata back into a YouTubePlaylist object."""
    return YouTubePlaylist(
        playlist_id=str(payload.get("playlist_id") or ""),
        title=str(payload.get("title") or "Playlist"),
        webpage_url=str(payload.get("webpage_url") or ""),
        channel=str(payload.get("channel") or "Unknown"),
        channel_id=(
            str(payload.get("channel_id"))
            if payload.get("channel_id")
            else None
        ),
        uploader_id=(
            str(payload.get("uploader_id"))
            if payload.get("uploader_id")
            else None
        ),
    )


def serialize_youtube_result_item(
    video: YouTubeVideo,
    *,
    playlist: YouTubePlaylist | None = None,
    watch_later_key: str | None = None,
) -> dict[str, object]:
    """Serialize one search-result card context."""
    payload: dict[str, object] = {
        "video": serialize_youtube_video(video),
    }
    if playlist is not None:
        payload["playlist"] = serialize_youtube_playlist(playlist)
    if watch_later_key:
        payload["watch_later_key"] = watch_later_key
    return payload


def deserialize_youtube_result_item(
    payload: dict[str, object],
) -> tuple[YouTubeVideo, YouTubePlaylist | None, str | None]:
    """Deserialize one stored search-result card context."""
    video_payload = payload.get("video")
    if not isinstance(video_payload, dict):
        raise RuntimeError("Invalid YouTube result payload.")
    playlist_payload = payload.get("playlist")
    playlist = (
        deserialize_youtube_playlist(playlist_payload)
        if isinstance(playlist_payload, dict)
        else None
    )
    watch_later_key = payload.get("watch_later_key")
    return (
        deserialize_youtube_video(video_payload),
        playlist,
        str(watch_later_key) if isinstance(watch_later_key, str) and watch_later_key else None,
    )


def get_youtube_channel_identity_label(video: YouTubeVideo) -> str:
    """Return the best channel identity label for user-facing cards."""
    for candidate in (video.uploader_id, video.channel_id):
        if not candidate:
            continue
        stripped = candidate.strip()
        if not stripped:
            continue
        if stripped.startswith("@"):
            return stripped
        if stripped.startswith("UC"):
            continue
        return f"@{stripped}"
    return video.channel_id or "Unknown"


def build_youtube_channel_from_video(video: YouTubeVideo) -> YouTubeChannel | None:
    """Build a channel object from one video payload when enough metadata is present."""
    browse_url = video.uploader_url or video.channel_url
    if not browse_url and not video.channel_id:
        return None
    return YouTubeChannel(
        channel_id=video.channel_id or browse_url or "",
        title=video.channel or "Unknown channel",
        channel_url=video.channel_url or browse_url or "",
        uploader_id=video.uploader_id,
        uploader_url=video.uploader_url,
        webpage_url=browse_url,
    )


def build_search_keyboard(
    session_token: str,
    results: list[dict[str, object]],
    *,
    page: int,
) -> InlineKeyboardMarkup:
    """Build the paginated inline keyboard for YouTube search results."""
    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_results = results[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    rows = []
    for index, result in enumerate(page_results, start=start + 1):
        video_payload = result.get("video") if isinstance(result, dict) else None
        title = truncate_button_label(
            str(video_payload.get("title") or "Video")
            if isinstance(video_payload, dict)
            else "Video"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{index}. {title}",
                    callback_data=f"{YOUTUBE_SEARCH_RESULT_CALLBACK_PREFIX}:{session_token}:{index - 1}",
                )
            ]
        )

    navigation_buttons = []
    if page > 0:
        navigation_buttons.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=f"{YOUTUBE_SEARCH_PAGE_CALLBACK_PREFIX}:{session_token}:{page - 1}",
            )
        )
    if start + YOUTUBE_SEARCH_PAGE_SIZE < len(results):
        navigation_buttons.append(
            InlineKeyboardButton(
                text="➡️ موارد بیشتر",
                callback_data=f"{YOUTUBE_SEARCH_PAGE_CALLBACK_PREFIX}:{session_token}:{page + 1}",
            )
        )
    if navigation_buttons:
        rows.append(navigation_buttons)
    return InlineKeyboardMarkup(rows)


def build_selected_result_keyboard(
    video_id: str,
    *,
    card_token: str,
    session_token: str | None = None,
    allow_channel_open: bool,
    show_save_channel: bool,
    is_watch_later: bool,
    show_save_playlist: bool,
) -> InlineKeyboardMarkup:
    """Build the card keyboard shown under a selected YouTube search result."""
    quality_keyboard = build_quality_mode_keyboard(video_id, session_token=session_token)
    rows = [list(row) for row in quality_keyboard.inline_keyboard]
    action_row: list[InlineKeyboardButton] = []
    if show_save_channel:
        action_row.append(
            InlineKeyboardButton(
                text="⭐ Save Channel",
                callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:savech:{card_token}",
            )
        )
    action_row.append(
        InlineKeyboardButton(
            text="🗑 Remove Watch Later" if is_watch_later else "🕒 Watch Later",
            callback_data=(
                f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:watchrm:{card_token}"
                if is_watch_later
                else f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:watch:{card_token}"
            ),
        )
    )
    rows.append(action_row)
    extra_row: list[InlineKeyboardButton] = []
    if allow_channel_open:
        extra_row.append(
            InlineKeyboardButton(
                text="📺 Open Channel",
                callback_data=f"{YOUTUBE_OPEN_CHANNEL_CALLBACK_PREFIX}:{card_token}",
            )
        )
    if show_save_playlist:
        extra_row.append(
            InlineKeyboardButton(
                text="📚 Save Playlist",
                callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:savepl:{card_token}",
            )
        )
    if extra_row:
        rows.append(extra_row)
    return InlineKeyboardMarkup(rows)


def format_youtube_search_page(
    results: list[dict[str, object]],
    *,
    page: int,
) -> str:
    """Render one paginated page of YouTube search results."""
    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_results = results[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    total_pages = max((len(results) - 1) // YOUTUBE_SEARCH_PAGE_SIZE + 1, 1)
    lines = [f"نتایج جستجوی یوتیوب · صفحه {page + 1} از {total_pages}", ""]
    for index, result in enumerate(page_results, start=start + 1):
        video_payload = result.get("video") if isinstance(result, dict) else None
        if not isinstance(video_payload, dict):
            lines.append(f"{index}. Video")
            lines.append("کانال: Unknown")
            lines.append("مدت: Unknown")
            lines.append("")
            continue
        lines.append(f"{index}. {video_payload.get('title') or 'Video'}")
        lines.append(f"کانال: {video_payload.get('channel') or 'Unknown'}")
        lines.append(
            "مدت: "
            f"{format_video_duration_label(video_payload.get('duration_seconds') if isinstance(video_payload.get('duration_seconds'), (int, float)) else None)}"
        )
        lines.append("")
    lines.append("یکی از نتیجه‌ها را انتخاب کن.")
    return "\n".join(lines).strip()


def get_youtube_quality_mode_title(quality_mode: str) -> str:
    """Return the user-facing title for a YouTube quality mode."""
    if quality_mode == YOUTUBE_QUALITY_MODE_LOW:
        return "Low Quality"
    if quality_mode == YOUTUBE_QUALITY_MODE_AUDIO:
        return "Audio Only"
    return "High Quality"


def get_youtube_quality_mode_description(quality_mode: str) -> str:
    """Return the short Persian description for a YouTube quality mode."""
    if quality_mode == YOUTUBE_QUALITY_MODE_LOW:
        return "فقط گزینه‌های سبک‌تر و مناسب ارسال مستقیم"
    if quality_mode == YOUTUBE_QUALITY_MODE_AUDIO:
        return "فقط گزینه‌های صوتی"
    return "همه کیفیت‌ها، شامل گزینه‌های حجیم و چندبخشی"


def build_quality_mode_keyboard(
    video_id: str,
    *,
    session_token: str | None = None,
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for choosing between low/high quality flows."""
    def mode_callback(mode: str) -> str:
        if session_token:
            return f"{QUALITY_MODE_CALLBACK_PREFIX}:{session_token}:{video_id}:{mode}"
        return f"{QUALITY_MODE_CALLBACK_PREFIX}:{video_id}:{mode}"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="🟢 Low Quality",
                    callback_data=mode_callback(YOUTUBE_QUALITY_MODE_LOW),
                ),
                InlineKeyboardButton(
                    text="🔴 High Quality",
                    callback_data=mode_callback(YOUTUBE_QUALITY_MODE_HIGH),
                ),
            ]
            ,
            [
                InlineKeyboardButton(
                    text="🎵 Audio Only",
                    callback_data=mode_callback(YOUTUBE_QUALITY_MODE_AUDIO),
                )
            ],
        ]
    )


def build_quality_keyboard(
    video_id: str,
    options: list[DownloadOption],
    *,
    quality_mode: str,
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for quality selection."""
    rows = [
        [
            InlineKeyboardButton(
                text=option.label,
                callback_data=f"{QUALITY_CALLBACK_PREFIX}:{video_id}:{quality_mode}:{index}",
            )
        ]
        for index, option in enumerate(options)
    ]
    if quality_mode == YOUTUBE_QUALITY_MODE_AUDIO:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🟢 Low Quality",
                    callback_data=f"{QUALITY_MODE_CALLBACK_PREFIX}:{video_id}:{YOUTUBE_QUALITY_MODE_LOW}",
                ),
                InlineKeyboardButton(
                    text="🔴 High Quality",
                    callback_data=f"{QUALITY_MODE_CALLBACK_PREFIX}:{video_id}:{YOUTUBE_QUALITY_MODE_HIGH}",
                ),
            ]
        )
    else:
        switch_mode = (
            YOUTUBE_QUALITY_MODE_HIGH
            if quality_mode == YOUTUBE_QUALITY_MODE_LOW
            else YOUTUBE_QUALITY_MODE_LOW
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔁 Switch to {get_youtube_quality_mode_title(switch_mode)}",
                    callback_data=f"{QUALITY_MODE_CALLBACK_PREFIX}:{video_id}:{switch_mode}",
                ),
                InlineKeyboardButton(
                    text="🎵 Audio Only",
                    callback_data=f"{QUALITY_MODE_CALLBACK_PREFIX}:{video_id}:{YOUTUBE_QUALITY_MODE_AUDIO}",
                ),
            ]
        )
    return InlineKeyboardMarkup(rows)


def build_download_cancel_keyboard(job_token: str) -> InlineKeyboardMarkup:
    """Build the inline keyboard for cancelling an active YouTube download."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="❌ لغو دانلود", callback_data=f"{YOUTUBE_CANCEL_CALLBACK_PREFIX}:{job_token}")]]
    )


def extract_youtube_video_id(url_text: str) -> str | None:
    """Extract a YouTube video ID from a user-provided URL."""
    candidate = url_text.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    host = parsed.netloc.lower().replace("www.", "")
    path_parts = [part for part in parsed.path.split("/") if part]

    if host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
            return video_id or None
        if path_parts and path_parts[0] in {"shorts", "embed", "live"} and len(path_parts) >= 2:
            return path_parts[1]
        return None

    if host == "youtu.be" and path_parts:
        return path_parts[0]

    return None


def store_youtube_quality_options(
    context: ContextTypes.DEFAULT_TYPE,
    video_id: str,
    quality_mode: str,
    options: list[DownloadOption],
) -> None:
    """Persist quality options for later callback selection."""
    context.user_data.setdefault("youtube_quality_options", {}).setdefault(video_id, {})[
        quality_mode
    ] = [
        {
            "format_selector": option.format_selector,
            "label": option.label,
            "media_kind": option.media_kind,
        }
        for option in options
    ]


def get_stored_youtube_quality_options(
    context: ContextTypes.DEFAULT_TYPE,
    video_id: str,
    quality_mode: str,
) -> list[dict[str, str]]:
    """Return the stored options for a given video and quality mode."""
    stored_options = context.user_data.get("youtube_quality_options", {}).get(video_id, {})
    if isinstance(stored_options, list):
        return stored_options
    return list(stored_options.get(quality_mode, []))


def store_youtube_search_results(
    context: ContextTypes.DEFAULT_TYPE,
    results: list[dict[str, object]],
) -> str:
    """Persist a search session so inline pagination can resolve results later."""
    session_token = uuid.uuid4().hex[:12]
    context.user_data.setdefault("youtube_search_sessions", {})[session_token] = list(results)
    return session_token


def transport_supports_youtube_browser(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return whether the richer YouTube browser should be exposed."""
    return get_transport_name(context) in {"telegram", "rubika"}


def store_youtube_card_context(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    video: YouTubeVideo,
    playlist: YouTubePlaylist | None = None,
    watch_later_key: str | None = None,
    session_token: str | None = None,
) -> str:
    """Persist one video-card context for later channel browsing callbacks."""
    token = uuid.uuid4().hex[:12]
    context.user_data.setdefault("youtube_card_contexts", {})[token] = {
        "video": serialize_youtube_video(video),
        "playlist": serialize_youtube_playlist(playlist) if playlist is not None else None,
        "watch_later_key": watch_later_key,
        "session_token": session_token,
    }
    return token


def get_youtube_card_context(
    context: ContextTypes.DEFAULT_TYPE,
    token: str,
) -> dict[str, object] | None:
    """Return a stored video-card context payload."""
    payload = context.user_data.get("youtube_card_contexts", {}).get(token)
    return payload if isinstance(payload, dict) else None


def store_youtube_browser_view(
    context: ContextTypes.DEFAULT_TYPE,
    payload: dict[str, object],
) -> str:
    """Persist one YouTube browser view and return its token."""
    token = uuid.uuid4().hex[:12]
    context.user_data.setdefault("youtube_browser_views", {})[token] = payload
    return token


def get_youtube_browser_view(
    context: ContextTypes.DEFAULT_TYPE,
    token: str,
) -> dict[str, object] | None:
    """Read one stored YouTube browser view payload."""
    payload = context.user_data.get("youtube_browser_views", {}).get(token)
    return payload if isinstance(payload, dict) else None


def set_pending_youtube_channel_search(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    channel: YouTubeChannel,
    home_view_token: str,
) -> None:
    """Remember which channel should receive the next in-channel search query."""
    context.user_data["youtube_pending_channel_search"] = serialize_youtube_channel(channel)
    context.user_data["youtube_pending_channel_home"] = home_view_token


def get_pending_youtube_channel_search(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[YouTubeChannel | None, str | None]:
    """Return the pending in-channel search target, if any."""
    payload = context.user_data.get("youtube_pending_channel_search")
    home_view_token = context.user_data.get("youtube_pending_channel_home")
    channel = (
        deserialize_youtube_channel(payload)
        if isinstance(payload, dict)
        else None
    )
    token = str(home_view_token) if isinstance(home_view_token, str) and home_view_token else None
    return channel, token


def clear_pending_youtube_channel_search(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear the pending in-channel search target."""
    context.user_data.pop("youtube_pending_channel_search", None)
    context.user_data.pop("youtube_pending_channel_home", None)


def register_youtube_search_message(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    session_token: str,
    chat_id: int | str,
    message_id: int,
) -> None:
    """Track a message that belongs to a YouTube search session."""
    sessions = context.user_data.setdefault("youtube_search_session_messages", {})
    payload = sessions.setdefault(
        session_token,
        {
            "chat_id": chat_id,
            "message_ids": [],
        },
    )
    if not isinstance(payload, dict):
        payload = {
            "chat_id": chat_id,
            "message_ids": [],
        }
        sessions[session_token] = payload
    payload["chat_id"] = chat_id
    message_ids = payload.setdefault("message_ids", [])
    if isinstance(message_ids, list) and message_id not in message_ids:
        message_ids.append(message_id)


async def clear_youtube_search_messages(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    session_token: str,
    keep_message_id: int | None = None,
) -> None:
    """Delete tracked messages for a YouTube search session."""
    sessions = context.user_data.get("youtube_search_session_messages", {})
    payload = sessions.pop(session_token, None)
    if not isinstance(payload, dict):
        return

    chat_id = payload.get("chat_id")
    message_ids = payload.get("message_ids", [])
    if chat_id is None or not isinstance(message_ids, list):
        return

    for message_id in message_ids:
        if not isinstance(message_id, int) or message_id == keep_message_id:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            LOGGER.debug(
                "Failed to delete YouTube search message chat_id=%s message_id=%s",
                chat_id,
                message_id,
                exc_info=True,
            )


def get_stored_youtube_search_results(
    context: ContextTypes.DEFAULT_TYPE,
    session_token: str,
) -> list[dict[str, object]]:
    """Return the stored videos for a YouTube search session."""
    sessions = context.user_data.get("youtube_search_sessions", {})
    stored = sessions.get(session_token, [])
    return stored if isinstance(stored, list) else []


def set_active_youtube_search_state(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    session_token: str,
    page: int,
    source: str = YOUTUBE_MODE_SEARCH,
) -> None:
    """Persist the active YouTube search session/page for reply-keyboard actions."""
    context.user_data["youtube_active_search_session"] = session_token
    context.user_data["youtube_active_search_page"] = page
    context.user_data["youtube_active_search_source"] = source


def get_active_youtube_search_state(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str | None, int, str]:
    """Return the active YouTube search session token and page index."""
    session_token = context.user_data.get("youtube_active_search_session")
    page = context.user_data.get("youtube_active_search_page", 0)
    source = context.user_data.get("youtube_active_search_source", YOUTUBE_MODE_SEARCH)
    return (
        str(session_token) if isinstance(session_token, str) and session_token else None,
        int(page),
        str(source) if isinstance(source, str) and source else YOUTUBE_MODE_SEARCH,
    )


def clear_active_youtube_search_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear the active YouTube search browsing state."""
    context.user_data.pop("youtube_active_search_session", None)
    context.user_data.pop("youtube_active_search_page", None)
    context.user_data.pop("youtube_active_search_source", None)


def clear_active_youtube_browser_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear the active YouTube channel browser state."""
    clear_pending_youtube_channel_search(context)


def set_active_web_search_state(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    query: str,
    results: list[dict[str, str]],
    page: int,
) -> None:
    """Persist the active compact web-search result set."""
    context.user_data["web_search_query"] = query
    context.user_data["web_search_results"] = list(results)
    context.user_data["web_search_page"] = page


def get_active_web_search_state(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str | None, list[dict[str, str]], int]:
    """Return the active web-search query, results, and page index."""
    query = context.user_data.get("web_search_query")
    results = context.user_data.get("web_search_results", [])
    page = context.user_data.get("web_search_page", 0)
    return (
        str(query).strip() if isinstance(query, str) and query.strip() else None,
        list(results) if isinstance(results, list) else [],
        int(page),
    )


def clear_active_web_search_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop any cached compact web-search results for the user."""
    context.user_data.pop("web_search_query", None)
    context.user_data.pop("web_search_results", None)
    context.user_data.pop("web_search_page", None)


def set_active_image_search_state(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    query: str,
    results: list[dict[str, str]],
    index: int,
) -> None:
    """Persist the active image-search result set."""
    context.user_data["image_search_query"] = query
    context.user_data["image_search_results"] = list(results)
    context.user_data["image_search_index"] = max(int(index), 0)


def get_active_image_search_state(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str | None, list[dict[str, str]], int]:
    """Return the active image-search query, results, and current cursor index."""
    query = context.user_data.get("image_search_query")
    results = context.user_data.get("image_search_results", [])
    index = context.user_data.get("image_search_index")
    if index is None:
        legacy_page = context.user_data.get("image_search_page", 0)
        index = int(legacy_page) * IMAGE_SEARCH_ALBUM_SIZE
    return (
        str(query).strip() if isinstance(query, str) and query.strip() else None,
        list(results) if isinstance(results, list) else [],
        max(int(index), 0),
    )


def clear_active_image_search_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop any cached image-search results for the user."""
    context.user_data.pop("image_search_query", None)
    context.user_data.pop("image_search_results", None)
    context.user_data.pop("image_search_index", None)
    context.user_data.pop("image_search_page", None)


def build_youtube_results_menu_for_source(
    source: str,
    *,
    has_more_results: bool,
) -> ReplyKeyboardMarkup:
    """Return the correct results keyboard for the active source type."""
    return build_youtube_results_menu(
        has_more_results=has_more_results,
        include_watch_later_search=(source == YOUTUBE_RESULT_SOURCE_WATCH_LATER),
    )


def remove_youtube_result_item_by_watch_later_key(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    session_token: str,
    watch_later_key: str,
) -> None:
    """Drop one watch-later result from an active in-memory session."""
    sessions = context.user_data.get("youtube_search_sessions", {})
    stored = sessions.get(session_token)
    if not isinstance(stored, list):
        return
    sessions[session_token] = [
        item
        for item in stored
        if not (
            isinstance(item, dict)
            and str(item.get("watch_later_key") or "") == watch_later_key
        )
    ]


def get_youtube_library_store(context: ContextTypes.DEFAULT_TYPE) -> YouTubeLibraryStore:
    """Return the shared YouTube library store."""
    store = context.application.bot_data.get("youtube_library_store")
    if not isinstance(store, YouTubeLibraryStore):
        raise RuntimeError("YouTube library store is not configured.")
    return store


def get_google_web_search_service(
    context: ContextTypes.DEFAULT_TYPE,
) -> GoogleWebSearchService:
    """Return the shared Google-grounded web search service."""
    service = context.application.bot_data.get("google_web_search_service")
    if not isinstance(service, GoogleWebSearchService):
        raise RuntimeError("Google web search service is not configured.")
    return service


def get_image_search_service(context: ContextTypes.DEFAULT_TYPE) -> ImageSearchService:
    """Return the shared image search service."""
    service = context.application.bot_data.get("image_search_service")
    if not isinstance(service, ImageSearchService):
        raise RuntimeError("Image search service is not configured.")
    return service


def get_youtube_library_user_key(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> str | None:
    """Return the stable transport-aware user key for YouTube library features."""
    transport_name = get_transport_name(context)
    if transport_name == "rubika":
        user_id, chat_id, _display_name = resolve_rubika_actor_identity(update, context)
        identity = user_id or chat_id
        return f"rubika:{identity}" if identity else None

    effective_user = update.effective_user
    effective_chat = update.effective_chat
    identity = (
        getattr(effective_user, "id", None)
        or getattr(effective_chat, "id", None)
    )
    return f"{transport_name}:{identity}" if identity is not None else None


def get_youtube_library_snapshot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[str, list[dict[str, object]]] | None:
    """Return the current user's YouTube library snapshot."""
    user_key = get_youtube_library_user_key(update, context)
    if user_key is None:
        return None
    return get_youtube_library_store(context).load_user_library(user_key)


def summarize_youtube_library_counts(snapshot: dict[str, list[dict[str, object]]] | None) -> str:
    """Render a short one-line YouTube library summary."""
    if snapshot is None:
        return "ذخیره‌ها در دسترس نیست."
    saved_channels = len(snapshot.get("saved_channels", []))
    saved_playlists = len(snapshot.get("saved_playlists", []))
    watch_later = len(snapshot.get("watch_later", []))
    recent_searches = len(snapshot.get("recent_searches", []))
    return (
        f"کانال: {saved_channels} | پلی‌لیست: {saved_playlists} | "
        f"Watch later: {watch_later} | اخیر: {recent_searches}"
    )


def get_saved_channel_handle(payload: dict[str, object]) -> str:
    """Return the best saved-channel handle label."""
    uploader_id = str(payload.get("uploader_id") or "").strip()
    if uploader_id:
        return uploader_id if uploader_id.startswith("@") else f"@{uploader_id}"
    channel_id = str(payload.get("channel_id") or "").strip()
    return channel_id or "-"


def render_saved_channels_text(
    snapshot: dict[str, list[dict[str, object]]],
    *,
    page: int,
) -> str:
    """Render one page of saved channels."""
    channels = [entry for entry in snapshot.get("saved_channels", []) if isinstance(entry, dict)]
    if not channels:
        return "📺 هنوز کانالی ذخیره نکرده‌ای."
    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_channels = channels[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    total_pages = max((len(channels) - 1) // YOUTUBE_SEARCH_PAGE_SIZE + 1, 1)
    lines = [f"📺 کانال‌های ذخیره‌شده · صفحه {page + 1} از {total_pages}", ""]
    for index, entry in enumerate(page_channels, start=start + 1):
        title = str(entry.get("title") or "Channel")
        lines.append(f"{index}. {title}")
        lines.append(f"Handle: {get_saved_channel_handle(entry)}")
        lines.append("")
    lines.append("روی نام کانال بزن تا باز شود یا آن را حذف کن.")
    return "\n".join(lines).strip()


def build_saved_channels_keyboard(
    snapshot: dict[str, list[dict[str, object]]],
    *,
    page: int,
) -> InlineKeyboardMarkup:
    """Build the paginated saved-channels keyboard."""
    channels = [entry for entry in snapshot.get("saved_channels", []) if isinstance(entry, dict)]
    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_channels = channels[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for index, entry in enumerate(page_channels, start=start + 1):
        key = str(entry.get("key") or "")
        title = truncate_button_label(str(entry.get("title") or "Channel"))
        if not key:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{index}. {title}",
                    callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:channels:open:{key}",
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:channels:delete:{page}:{key}",
                ),
            ]
        )
    max_page = max((len(channels) - 1) // YOUTUBE_SEARCH_PAGE_SIZE, 0)
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:channels:page:{page - 1}",
            )
        )
    if page < max_page:
        nav_row.append(
            InlineKeyboardButton(
                text="➡️ بعدی",
                callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:channels:page:{page + 1}",
            )
        )
    if nav_row:
        rows.append(nav_row)
    return InlineKeyboardMarkup(rows)


def render_saved_playlists_text(
    snapshot: dict[str, list[dict[str, object]]],
    *,
    page: int,
) -> str:
    """Render one page of saved playlists."""
    playlists = [entry for entry in snapshot.get("saved_playlists", []) if isinstance(entry, dict)]
    if not playlists:
        return "📚 هنوز پلی‌لیستی ذخیره نکرده‌ای."
    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_playlists = playlists[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    total_pages = max((len(playlists) - 1) // YOUTUBE_SEARCH_PAGE_SIZE + 1, 1)
    lines = [f"📚 پلی‌لیست‌های ذخیره‌شده · صفحه {page + 1} از {total_pages}", ""]
    for index, entry in enumerate(page_playlists, start=start + 1):
        title = str(entry.get("title") or "Playlist")
        channel = str(entry.get("channel") or "Unknown")
        lines.append(f"{index}. {title}")
        lines.append(f"Channel: {channel}")
        lines.append("")
    lines.append("روی پلی‌لیست بزن تا ویدیوهایش باز شود یا آن را حذف کن.")
    return "\n".join(lines).strip()


def build_saved_playlists_keyboard(
    snapshot: dict[str, list[dict[str, object]]],
    *,
    page: int,
) -> InlineKeyboardMarkup:
    """Build the paginated saved-playlists keyboard."""
    playlists = [entry for entry in snapshot.get("saved_playlists", []) if isinstance(entry, dict)]
    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_playlists = playlists[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for index, entry in enumerate(page_playlists, start=start + 1):
        key = str(entry.get("key") or "")
        title = truncate_button_label(str(entry.get("title") or "Playlist"))
        if not key:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{index}. {title}",
                    callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:playlists:open:{key}",
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:playlists:delete:{page}:{key}",
                ),
            ]
        )
    max_page = max((len(playlists) - 1) // YOUTUBE_SEARCH_PAGE_SIZE, 0)
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:playlists:page:{page - 1}",
            )
        )
    if page < max_page:
        nav_row.append(
            InlineKeyboardButton(
                text="➡️ بعدی",
                callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:playlists:page:{page + 1}",
            )
        )
    if nav_row:
        rows.append(nav_row)
    return InlineKeyboardMarkup(rows)


def _youtube_saved_source_label(prefix: str, title: str, subtitle: str | None = None) -> str:
    """Build a compact inline-button label for a saved source."""
    if subtitle:
        return truncate_button_label(f"{prefix} {title} · {subtitle}")
    return truncate_button_label(f"{prefix} {title}")


def build_youtube_saved_sources_keyboard(
    snapshot: dict[str, list[dict[str, object]]],
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for the saved sources/watch-later panel."""
    rows: list[list[InlineKeyboardButton]] = []
    for entry in snapshot.get("saved_channels", [])[:4]:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "")
        title = str(entry.get("title") or "Channel")
        handle = str(entry.get("uploader_id") or entry.get("channel_id") or "").strip() or None
        if key:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=_youtube_saved_source_label("📺", title, handle),
                        callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:opench:{key}",
                    )
                ]
            )
    for entry in snapshot.get("saved_playlists", [])[:4]:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "")
        title = str(entry.get("title") or "Playlist")
        channel = str(entry.get("channel") or "").strip() or None
        if key:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=_youtube_saved_source_label("📚", title, channel),
                        callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:openpl:{key}",
                    )
                ]
            )
    for entry in snapshot.get("watch_later", [])[:4]:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "")
        video_payload = entry.get("video")
        if not key or not isinstance(video_payload, dict):
            continue
        title = str(video_payload.get("title") or "Video")
        channel = str(video_payload.get("channel") or "").strip() or None
        rows.append(
            [
                InlineKeyboardButton(
                    text=_youtube_saved_source_label("🕒", title, channel),
                    callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:watchopen:{key}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⭐ جستجو در ذخیره‌ها",
                callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:savedsearch",
            ),
            InlineKeyboardButton(
                text="🕘 جستجوهای اخیر",
                callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:showrecent",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_youtube_recent_searches_keyboard(
    snapshot: dict[str, list[dict[str, object]]],
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for the recent-search panel."""
    rows: list[list[InlineKeyboardButton]] = []
    for entry in snapshot.get("recent_searches", [])[:6]:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "")
        query = str(entry.get("query") or "").strip()
        scope = str(entry.get("scope") or "global")
        if not key or not query:
            continue
        prefix = "🕒" if scope == YOUTUBE_MODE_WATCH_LATER_SEARCH else "🔎"
        rows.append(
            [
                InlineKeyboardButton(
                    text=truncate_button_label(f"{prefix} {query}"),
                    callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:recent:{key}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="📺 کانال‌ها",
                callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:channels:page:0",
            ),
            InlineKeyboardButton(
                text="📚 پلی‌لیست‌ها",
                callback_data=f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:playlists:page:0",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_youtube_channel_browser_keyboard(view_token: str) -> InlineKeyboardMarkup:
    """Build the inline keyboard for a channel home panel."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="🎬 Latest",
                    callback_data=f"{YOUTUBE_BROWSER_ACTION_CALLBACK_PREFIX}:latest:{view_token}",
                ),
                InlineKeyboardButton(
                    text="🔥 Popular",
                    callback_data=f"{YOUTUBE_BROWSER_ACTION_CALLBACK_PREFIX}:popular:{view_token}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📚 Playlists",
                    callback_data=f"{YOUTUBE_BROWSER_ACTION_CALLBACK_PREFIX}:playlists:{view_token}",
                ),
                InlineKeyboardButton(
                    text="🔎 Search in channel",
                    callback_data=f"{YOUTUBE_BROWSER_ACTION_CALLBACK_PREFIX}:search:{view_token}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Close",
                    callback_data=f"{YOUTUBE_BROWSER_ACTION_CALLBACK_PREFIX}:close:{view_token}",
                )
            ],
        ]
    )


def build_youtube_browser_nav_keyboard(
    view_token: str,
    *,
    page: int,
    total_items: int,
    parent_token: str | None,
) -> list[list[InlineKeyboardButton]]:
    """Build shared navigation rows for browser list panels."""
    rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    max_page = max((total_items - 1) // YOUTUBE_SEARCH_PAGE_SIZE, 0)
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ Prev",
                callback_data=f"{YOUTUBE_BROWSER_PAGE_CALLBACK_PREFIX}:{view_token}:{page - 1}",
            )
        )
    if page < max_page:
        nav_row.append(
            InlineKeyboardButton(
                text="➡️ More",
                callback_data=f"{YOUTUBE_BROWSER_PAGE_CALLBACK_PREFIX}:{view_token}:{page + 1}",
            )
        )
    if nav_row:
        rows.append(nav_row)

    action_row: list[InlineKeyboardButton] = []
    if parent_token:
        action_row.append(
            InlineKeyboardButton(
                text="🔙 Back",
                callback_data=f"{YOUTUBE_BROWSER_ACTION_CALLBACK_PREFIX}:back:{view_token}",
            )
        )
    action_row.append(
        InlineKeyboardButton(
            text="❌ Close",
            callback_data=f"{YOUTUBE_BROWSER_ACTION_CALLBACK_PREFIX}:close:{view_token}",
        )
    )
    rows.append(action_row)
    return rows


def build_youtube_browser_video_list_keyboard(
    view_token: str,
    videos: list[dict[str, str | int | None]],
    *,
    page: int,
    parent_token: str | None,
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for a browser video list."""
    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_videos = videos[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for index, payload in enumerate(page_videos, start=start):
        title = truncate_button_label(str(payload.get("title") or "Video"))
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{index + 1}. {title}",
                    callback_data=f"{YOUTUBE_BROWSER_ITEM_CALLBACK_PREFIX}:{view_token}:{index}",
                )
            ]
        )
    rows.extend(
        build_youtube_browser_nav_keyboard(
            view_token,
            page=page,
            total_items=len(videos),
            parent_token=parent_token,
        )
    )
    return InlineKeyboardMarkup(rows)


def build_youtube_browser_playlist_list_keyboard(
    view_token: str,
    playlists: list[dict[str, str | int | None]],
    *,
    page: int,
    parent_token: str | None,
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for a browser playlist list."""
    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_playlists = playlists[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for index, payload in enumerate(page_playlists, start=start):
        title = truncate_button_label(str(payload.get("title") or "Playlist"))
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{index + 1}. {title}",
                    callback_data=f"{YOUTUBE_BROWSER_ITEM_CALLBACK_PREFIX}:{view_token}:{index}",
                )
            ]
        )
    rows.extend(
        build_youtube_browser_nav_keyboard(
            view_token,
            page=page,
            total_items=len(playlists),
            parent_token=parent_token,
        )
    )
    return InlineKeyboardMarkup(rows)


def build_youtube_browser_video_cards_control_keyboard(
    view_token: str,
    *,
    page: int,
    total_items: int,
    parent_token: str | None,
) -> InlineKeyboardMarkup:
    """Build the inline control keyboard shown under Telegram browser video cards."""
    return InlineKeyboardMarkup(
        build_youtube_browser_nav_keyboard(
            view_token,
            page=page,
            total_items=total_items,
            parent_token=parent_token,
        )
    )


def render_youtube_channel_browser_text(channel: YouTubeChannel) -> str:
    """Render the YouTube channel home panel."""
    lines = [
        f"📺 {channel.title}",
        f"Handle: {channel.handle_label}",
    ]
    if channel.item_count:
        lines.append(f"Items: {channel.item_count}")
    if channel.browse_url:
        lines.append(f"Link: {channel.browse_url}")
    lines.append("")
    lines.append("Choose what you want to browse.")
    return "\n".join(lines)


def render_youtube_browser_video_list_text(
    title: str,
    videos: list[dict[str, str | int | None]],
    *,
    page: int,
    empty_text: str,
) -> str:
    """Render one page of a browser video list."""
    if not videos:
        return empty_text

    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_videos = videos[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    total_pages = max((len(videos) - 1) // YOUTUBE_SEARCH_PAGE_SIZE + 1, 1)
    lines = [f"{title} · Page {page + 1} of {total_pages}", ""]
    for index, payload in enumerate(page_videos, start=start + 1):
        video = deserialize_youtube_video(payload)
        lines.append(f"{index}. {video.title}")
        lines.append(f"Duration: {video.duration_label}")
        lines.append(f"Channel: {video.channel}")
        lines.append("")
    lines.append("Select one item to open its download card.")
    return "\n".join(lines).strip()


def render_youtube_browser_video_cards_header(
    title: str,
    videos: list[dict[str, str | int | None]],
    *,
    page: int,
    empty_text: str,
) -> str:
    """Render the small control header for a Telegram browser card page."""
    if not videos:
        return empty_text

    total_pages = max((len(videos) - 1) // YOUTUBE_SEARCH_PAGE_SIZE + 1, 1)
    return f"{title} · Page {page + 1} of {total_pages}\nChoose a card or browse more."


def render_youtube_browser_playlist_list_text(
    title: str,
    playlists: list[dict[str, str | int | None]],
    *,
    page: int,
    empty_text: str,
) -> str:
    """Render one page of a browser playlist list."""
    if not playlists:
        return empty_text

    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_playlists = playlists[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    total_pages = max((len(playlists) - 1) // YOUTUBE_SEARCH_PAGE_SIZE + 1, 1)
    lines = [f"{title} · Page {page + 1} of {total_pages}", ""]
    for index, payload in enumerate(page_playlists, start=start + 1):
        playlist = deserialize_youtube_playlist(payload)
        lines.append(f"{index}. {playlist.title}")
        lines.append(f"Channel: {playlist.channel}")
        lines.append("")
    lines.append("Open one playlist to browse its videos.")
    return "\n".join(lines).strip()


def render_youtube_saved_sources_text(
    snapshot: dict[str, list[dict[str, object]]],
) -> str:
    """Render the saved sources/watch-later summary panel."""
    channel_count = len(snapshot.get("saved_channels", []))
    playlist_count = len(snapshot.get("saved_playlists", []))
    watch_later_count = len(snapshot.get("watch_later", []))
    lines = [
        "📚 ذخیره‌های یوتیوب",
        f"کانال‌ها: {channel_count}",
        f"پلی‌لیست‌ها: {playlist_count}",
        f"Watch later: {watch_later_count}",
        "",
    ]

    if channel_count:
        lines.append("کانال‌های اخیر:")
        for entry in snapshot.get("saved_channels", [])[:3]:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "Channel")
            channel_id = str(entry.get("channel_id") or entry.get("uploader_id") or "-")
            lines.append(f"• {title} ({channel_id})")
        lines.append("")

    if playlist_count:
        lines.append("پلی‌لیست‌های اخیر:")
        for entry in snapshot.get("saved_playlists", [])[:3]:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "Playlist")
            channel = str(entry.get("channel") or "Unknown")
            lines.append(f"• {title} | {channel}")
        lines.append("")

    if watch_later_count:
        lines.append("Watch later:")
        for entry in snapshot.get("watch_later", [])[:3]:
            if not isinstance(entry, dict):
                continue
            video_payload = entry.get("video")
            if not isinstance(video_payload, dict):
                continue
            title = str(video_payload.get("title") or "Video")
            channel = str(video_payload.get("channel") or "Unknown")
            lines.append(f"• {title} | {channel}")
        lines.append("")

    lines.append("برای باز کردن مورد دلخواه از دکمه‌های زیر استفاده کن.")
    return "\n".join(lines).strip()


def render_youtube_recent_searches_text(
    snapshot: dict[str, list[dict[str, object]]],
) -> str:
    """Render the recent-search summary panel."""
    recent_searches = snapshot.get("recent_searches", [])
    if not recent_searches:
        return "🕘 هنوز جستجوی اخیری ثبت نشده است."

    lines = ["🕘 جستجوهای اخیر", ""]
    for entry in recent_searches[:6]:
        if not isinstance(entry, dict):
            continue
        query = str(entry.get("query") or "").strip()
        scope = str(entry.get("scope") or "global")
        if not query:
            continue
        scope_label = (
            "فقط داخل Watch Later"
            if scope == YOUTUBE_MODE_WATCH_LATER_SEARCH
            else "کل یوتیوب"
        )
        lines.append(f"• {query} | {scope_label}")
    lines.append("")
    lines.append("یکی را بزن تا همان جستجو دوباره اجرا شود.")
    return "\n".join(lines).strip()


def build_youtube_browser_view_payload(
    *,
    kind: str,
    title: str,
    parent_token: str | None = None,
    channel: YouTubeChannel | None = None,
    videos: list[YouTubeVideo] | None = None,
    playlists: list[YouTubePlaylist] | None = None,
    playlist: YouTubePlaylist | None = None,
    empty_text: str | None = None,
) -> dict[str, object]:
    """Construct one browser view payload."""
    payload: dict[str, object] = {
        "kind": kind,
        "title": title,
        "parent_token": parent_token,
        "empty_text": empty_text or "No items found.",
    }
    if channel is not None:
        payload["channel"] = serialize_youtube_channel(channel)
    if videos is not None:
        payload["videos"] = [serialize_youtube_video(video) for video in videos]
    if playlists is not None:
        payload["playlists"] = [serialize_youtube_playlist(playlist) for playlist in playlists]
    if playlist is not None:
        payload["playlist"] = serialize_youtube_playlist(playlist)
    return payload


def render_youtube_browser_view(
    view_token: str,
    view_payload: dict[str, object],
    *,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render one browser view into text plus inline keyboard."""
    kind = str(view_payload.get("kind") or "")
    parent_token = (
        str(view_payload.get("parent_token"))
        if view_payload.get("parent_token")
        else None
    )

    if kind == "channel_home":
        channel_payload = view_payload.get("channel")
        if not isinstance(channel_payload, dict):
            raise RuntimeError("Invalid channel browser payload.")
        channel = deserialize_youtube_channel(channel_payload)
        return (
            render_youtube_channel_browser_text(channel),
            build_youtube_channel_browser_keyboard(view_token),
        )

    if kind == "video_list":
        videos = view_payload.get("videos", [])
        if not isinstance(videos, list):
            raise RuntimeError("Invalid YouTube video list payload.")
        title = str(view_payload.get("title") or "Videos")
        empty_text = str(view_payload.get("empty_text") or "No videos found.")
        return (
            render_youtube_browser_video_list_text(
                title,
                videos,
                page=page,
                empty_text=empty_text,
            ),
            build_youtube_browser_video_list_keyboard(
                view_token,
                videos,
                page=page,
                parent_token=parent_token,
            ),
        )

    if kind == "video_cards":
        videos = view_payload.get("videos", [])
        if not isinstance(videos, list):
            raise RuntimeError("Invalid YouTube video cards payload.")
        title = str(view_payload.get("title") or "Videos")
        empty_text = str(view_payload.get("empty_text") or "No videos found.")
        return (
            render_youtube_browser_video_cards_header(
                title,
                videos,
                page=page,
                empty_text=empty_text,
            ),
            build_youtube_browser_video_cards_control_keyboard(
                view_token,
                page=page,
                total_items=len(videos),
                parent_token=parent_token,
            ),
        )

    if kind == "playlist_list":
        playlists = view_payload.get("playlists", [])
        if not isinstance(playlists, list):
            raise RuntimeError("Invalid YouTube playlist list payload.")
        title = str(view_payload.get("title") or "Playlists")
        empty_text = str(view_payload.get("empty_text") or "No playlists found.")
        return (
            render_youtube_browser_playlist_list_text(
                title,
                playlists,
                page=page,
                empty_text=empty_text,
            ),
            build_youtube_browser_playlist_list_keyboard(
                view_token,
                playlists,
                page=page,
                parent_token=parent_token,
            ),
        )

    raise RuntimeError(f"Unsupported YouTube browser view: {kind}")


async def download_youtube_thumbnail(video: YouTubeVideo) -> Path | None:
    """Download a YouTube thumbnail to a temporary local file."""
    candidates = [
        f"https://i.ytimg.com/vi/{video.video_id}/maxresdefault.jpg",
        f"https://i.ytimg.com/vi/{video.video_id}/hqdefault.jpg",
        f"https://i.ytimg.com/vi/{video.video_id}/mqdefault.jpg",
    ]
    thumbnail_dir = DOWNLOADS_DIR / "youtube-thumbnails"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for url in candidates:
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    continue
                content_type = response.headers.get("content-type", "").lower()
                if "image" not in content_type:
                    continue
                suffix = ".jpg"
                if "png" in content_type:
                    suffix = ".png"
                destination = thumbnail_dir / f"{video.video_id}-{uuid.uuid4().hex[:6]}{suffix}"
                destination.write_bytes(response.content)
                return destination
            except Exception:
                LOGGER.debug("Failed to fetch thumbnail candidate %s", url, exc_info=True)
    return None


async def hydrate_youtube_videos_for_cards(
    context: ContextTypes.DEFAULT_TYPE,
    videos: list[YouTubeVideo],
) -> list[YouTubeVideo]:
    """Populate publish-time metadata used on YouTube cards."""
    if not videos:
        return videos
    searcher: YouTubeSearcher = context.application.bot_data["youtube_searcher"]
    try:
        return await searcher.hydrate_publish_times(videos)
    except Exception:
        LOGGER.debug("Failed to hydrate YouTube publish times", exc_info=True)
        return videos


async def send_youtube_result_card(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int | str,
    video: YouTubeVideo,
    playlist: YouTubePlaylist | None = None,
    watch_later_key: str | None = None,
    session_token: str | None = None,
) -> Message | None:
    """Send a thumbnail card for a YouTube result to a specific chat."""
    await hydrate_youtube_videos_for_cards(context, [video])
    publish_line = f"آپلود: {video.published_label}\n" if video.published_label else ""
    channel_identity = get_youtube_channel_identity_label(video)
    playlist_line = f"پلی‌لیست: {playlist.title}\n" if playlist is not None else ""
    caption = (
        f"{video.title}\n"
        f"کانال: {video.channel}\n"
        f"هندل کانال: {channel_identity}\n"
        f"{playlist_line}"
        f"مدت: {video.duration_label}\n"
        f"{publish_line}"
        f"لینک: {video.webpage_url}\n\n"
        "نوع دانلود را انتخاب کن:"
    )
    card_token = store_youtube_card_context(
        context,
        video=video,
        playlist=playlist,
        watch_later_key=watch_later_key,
        session_token=session_token,
    )

    reply_markup = build_selected_result_keyboard(
        video.video_id,
        card_token=card_token,
        session_token=session_token,
        allow_channel_open=(
            transport_supports_youtube_browser(context)
            and bool(video.channel_url or video.uploader_url or video.channel_id)
        ),
        show_save_channel=playlist is None,
        is_watch_later=watch_later_key is not None,
        show_save_playlist=playlist is not None,
    )
    thumbnail_path = await download_youtube_thumbnail(video)
    try:
        if thumbnail_path is not None:
            with thumbnail_path.open("rb") as thumbnail_file:
                return await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=thumbnail_file,
                    caption=caption,
                    filename=thumbnail_path.name,
                    reply_markup=reply_markup,
                )
        return await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            disable_web_page_preview=False,
            reply_markup=reply_markup,
        )
    finally:
        cleanup_file(thumbnail_path)


async def send_youtube_search_result_page(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int | str,
    session_token: str,
    page: int,
) -> tuple[int, int]:
    """Send one page of YouTube result cards and return page statistics."""
    results = get_stored_youtube_search_results(context, session_token)
    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_payloads = results[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    page_items = [deserialize_youtube_result_item(payload) for payload in page_payloads]
    page_videos = [video for video, _playlist, _watch_later_key in page_items]
    await hydrate_youtube_videos_for_cards(context, page_videos)
    for offset, (video, playlist, watch_later_key) in enumerate(page_items, start=start):
        results[offset] = serialize_youtube_result_item(
            video,
            playlist=playlist,
            watch_later_key=watch_later_key,
        )
        sent_message = await send_youtube_result_card(
            context,
            chat_id=chat_id,
            video=video,
            playlist=playlist,
            watch_later_key=watch_later_key,
            session_token=session_token,
        )
        if sent_message is not None:
            register_youtube_search_message(
                context,
                session_token=session_token,
                chat_id=chat_id,
                message_id=sent_message.message_id,
            )
    total_pages = max((len(results) - 1) // YOUTUBE_SEARCH_PAGE_SIZE + 1, 1)
    return len(page_items), total_pages


async def send_youtube_result_card_from_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    video: YouTubeVideo,
    playlist: YouTubePlaylist | None = None,
    watch_later_key: str | None = None,
) -> None:
    """Send a thumbnail card for a selected YouTube search result."""
    query = update.callback_query
    if query is None or query.message is None:
        return
    await send_youtube_result_card(
        context,
        chat_id=query.message.chat_id,
        video=video,
        playlist=playlist,
        watch_later_key=watch_later_key,
    )


async def deliver_youtube_search_results(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int | str,
    result_items: list[dict[str, object]],
    control_text: str | None = None,
    source: str = YOUTUBE_MODE_SEARCH,
) -> None:
    """Persist and send one page of YouTube result cards plus the control message."""
    session_token = store_youtube_search_results(context, result_items)
    set_active_youtube_search_state(
        context,
        session_token=session_token,
        page=0,
        source=source,
    )
    page_count, total_pages = await send_youtube_search_result_page(
        context,
        chat_id=chat_id,
        session_token=session_token,
        page=0,
    )
    if control_text is None:
        control_text = (
            f"{page_count} نتیجه اول ارسال شد."
            if total_pages > 1
            else f"{page_count} نتیجه پیدا شد."
        )
    control_message = await context.bot.send_message(
        chat_id=chat_id,
        text=control_text,
        reply_markup=build_youtube_results_menu_for_source(
            source,
            has_more_results=total_pages > 1,
        ),
    )
    register_youtube_search_message(
        context,
        session_token=session_token,
        chat_id=chat_id,
        message_id=control_message.message_id,
    )


def youtube_title_matches_query(title: str, query: str) -> bool:
    """Return whether a video title is a reasonable local match for a saved-source query."""
    normalized_title = re.sub(r"\s+", " ", title.strip()).casefold()
    normalized_query = re.sub(r"\s+", " ", query.strip()).casefold()
    if not normalized_title or not normalized_query:
        return False
    if normalized_query in normalized_title:
        return True
    terms = [term for term in normalized_query.split(" ") if len(term) >= 2]
    return bool(terms) and all(term in normalized_title for term in terms)


def build_watch_later_result_items(
    snapshot: dict[str, list[dict[str, object]]],
    *,
    query: str | None = None,
) -> list[dict[str, object]]:
    """Build card payloads for watch-later videos, optionally filtered by a local query."""
    normalized_query = " ".join((query or "").strip().split()).casefold()
    result_items: list[dict[str, object]] = []
    for entry in snapshot.get("watch_later", []):
        if not isinstance(entry, dict):
            continue
        video_payload = entry.get("video")
        if not isinstance(video_payload, dict):
            continue
        playlist_payload = entry.get("playlist")
        playlist = (
            deserialize_youtube_playlist(playlist_payload)
            if isinstance(playlist_payload, dict)
            else None
        )
        video = deserialize_youtube_video(video_payload)
        if normalized_query:
            haystacks = [
                video.title,
                video.channel,
                playlist.title if playlist is not None else "",
            ]
            if not any(youtube_title_matches_query(text, normalized_query) for text in haystacks if text):
                continue
        result_items.append(
            serialize_youtube_result_item(
                video,
                playlist=playlist,
                watch_later_key=str(entry.get("key") or ""),
            )
        )
    return result_items


async def search_saved_youtube_sources(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    snapshot: dict[str, list[dict[str, object]]],
    query: str,
) -> list[dict[str, object]]:
    """Search inside saved channels and playlists with a small concurrency limit."""
    searcher: YouTubeSearcher = context.application.bot_data["youtube_searcher"]
    seen_video_ids: set[str] = set()
    results: list[dict[str, object]] = []
    semaphore = asyncio.Semaphore(3)

    async def load_channel(channel_payload: dict[str, object]) -> list[dict[str, object]]:
        channel = deserialize_youtube_channel(channel_payload)
        if not channel.browse_url:
            return []
        async with semaphore:
            try:
                _resolved_channel, videos = await searcher.search_in_channel(channel.browse_url, query)
            except Exception:
                LOGGER.debug("Saved channel search failed for %s", channel.title, exc_info=True)
                return []
        return [serialize_youtube_result_item(video) for video in videos]

    async def load_playlist(playlist_payload: dict[str, object]) -> list[dict[str, object]]:
        playlist = deserialize_youtube_playlist(playlist_payload)
        if not playlist.webpage_url:
            return []
        async with semaphore:
            try:
                _resolved_playlist, videos = await searcher.browse_playlist_videos(playlist.webpage_url)
            except Exception:
                LOGGER.debug("Saved playlist browse failed for %s", playlist.title, exc_info=True)
                return []
        matched_videos = [
            video
            for video in videos
            if youtube_title_matches_query(video.title, query)
        ]
        return [
            serialize_youtube_result_item(video, playlist=playlist)
            for video in matched_videos
        ]

    channel_payloads = [
        entry
        for entry in snapshot.get("saved_channels", [])[:6]
        if isinstance(entry, dict)
    ]
    playlist_payloads = [
        entry
        for entry in snapshot.get("saved_playlists", [])[:6]
        if isinstance(entry, dict)
    ]
    gathered = await asyncio.gather(
        *(load_channel(payload) for payload in channel_payloads),
        *(load_playlist(payload) for payload in playlist_payloads),
    )
    for batch in gathered:
        for item in batch:
            if not isinstance(item, dict):
                continue
            video_payload = item.get("video")
            if not isinstance(video_payload, dict):
                continue
            video_id = str(video_payload.get("video_id") or "")
            if not video_id or video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            results.append(item)
            if len(results) >= 15:
                return results
    return results


async def send_youtube_browser_panel(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int | str,
    view_token: str,
    page: int = 0,
) -> Message:
    """Send a new Telegram browser panel message."""
    view_payload = get_youtube_browser_view(context, view_token)
    if view_payload is None:
        raise RuntimeError("The requested YouTube browser view is no longer available.")
    text, reply_markup = render_youtube_browser_view(view_token, view_payload, page=page)
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    register_youtube_search_message(
        context,
        session_token=view_token,
        chat_id=chat_id,
        message_id=message.message_id,
    )
    return message


async def edit_youtube_browser_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    view_token: str,
    page: int = 0,
) -> None:
    """Edit the current Telegram browser panel message in-place."""
    query = update.callback_query
    if query is None:
        return
    view_payload = get_youtube_browser_view(context, view_token)
    if view_payload is None:
        await query.edit_message_text("This YouTube browser view expired. Start again.")
        return
    text, reply_markup = render_youtube_browser_view(view_token, view_payload, page=page)
    await query.edit_message_text(
        text=text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def send_youtube_browser_video_cards_view(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int | str,
    view_token: str,
    page: int = 0,
) -> None:
    """Send one page of Telegram browser video cards plus a compact control message."""
    view_payload = get_youtube_browser_view(context, view_token)
    if view_payload is None:
        raise RuntimeError("The requested YouTube browser view is no longer available.")

    videos_payload = view_payload.get("videos", [])
    if not isinstance(videos_payload, list):
        raise RuntimeError("Invalid YouTube browser video cards payload.")
    playlist_payload = view_payload.get("playlist")
    playlist = (
        deserialize_youtube_playlist(playlist_payload)
        if isinstance(playlist_payload, dict)
        else None
    )

    start = page * YOUTUBE_SEARCH_PAGE_SIZE
    page_videos = [
        deserialize_youtube_video(payload)
        for payload in videos_payload[start : start + YOUTUBE_SEARCH_PAGE_SIZE]
    ]
    await hydrate_youtube_videos_for_cards(context, page_videos)
    for offset, video in enumerate(page_videos, start=start):
        videos_payload[offset] = serialize_youtube_video(video)
        sent_message = await send_youtube_result_card(
            context,
            chat_id=chat_id,
            video=video,
            playlist=playlist,
            session_token=view_token,
        )
        if sent_message is not None:
            register_youtube_search_message(
                context,
                session_token=view_token,
                chat_id=chat_id,
                message_id=sent_message.message_id,
            )

    text, reply_markup = render_youtube_browser_view(view_token, view_payload, page=page)
    control_message = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    register_youtube_search_message(
        context,
        session_token=view_token,
        chat_id=chat_id,
        message_id=control_message.message_id,
    )


async def show_youtube_browser_view(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int | str,
    view_token: str,
    page: int = 0,
) -> None:
    """Send a browser view in the appropriate Telegram presentation."""
    view_payload = get_youtube_browser_view(context, view_token)
    if view_payload is None:
        raise RuntimeError("The requested YouTube browser view is no longer available.")
    if str(view_payload.get("kind") or "") == "video_cards":
        await send_youtube_browser_video_cards_view(
            context,
            chat_id=chat_id,
            view_token=view_token,
            page=page,
        )
        return
    await send_youtube_browser_panel(
        context,
        chat_id=chat_id,
        view_token=view_token,
        page=page,
    )


async def get_youtube_download_options(
    context: ContextTypes.DEFAULT_TYPE,
    video_id: str,
    *,
    quality_mode: str,
) -> tuple[str, list[DownloadOption]]:
    """Fetch YouTube download options for the requested quality mode."""
    downloader: VideoDownloader = context.application.bot_data["video_downloader"]
    downloader.set_allow_large_uploads(
        quality_mode in {YOUTUBE_QUALITY_MODE_HIGH, YOUTUBE_QUALITY_MODE_AUDIO}
        and transport_supports_large_uploads(context)
    )
    title, options = await downloader.get_download_options(video_id)
    if quality_mode == YOUTUBE_QUALITY_MODE_AUDIO:
        return title, [option for option in options if option.media_kind == MEDIA_KIND_AUDIO]
    return title, [option for option in options if option.media_kind != MEDIA_KIND_AUDIO]


async def show_youtube_quality_mode_prompt(
    target_message: Message,
    *,
    video_id: str,
    prompt_text: str,
) -> None:
    """Ask the user whether they want low or high quality options."""
    await target_message.reply_text(
        prompt_text,
        reply_markup=build_quality_mode_keyboard(video_id),
    )


def get_active_youtube_download_jobs(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[str, dict[str, object]]:
    """Return the active YouTube download job registry."""
    return context.application.bot_data.setdefault("youtube_download_jobs", {})


def register_active_youtube_download_job(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    job_token: str,
    owner_user_id: int | None,
    controller_task: asyncio.Task[object] | None,
    cancel_event: threading.Event,
) -> None:
    """Track an active YouTube download so it can be cancelled later."""
    if controller_task is None:
        return
    get_active_youtube_download_jobs(context)[job_token] = {
        "owner_user_id": owner_user_id,
        "controller_task": controller_task,
        "cancel_event": cancel_event,
    }


def pop_active_youtube_download_job(
    context: ContextTypes.DEFAULT_TYPE,
    job_token: str,
) -> dict[str, object] | None:
    """Remove and return an active YouTube download job."""
    return get_active_youtube_download_jobs(context).pop(job_token, None)


def build_end_chat_inline_keyboard() -> InlineKeyboardMarkup:
    """Build an inline button shown under AI replies."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="🔚 پایان گفتگو", callback_data=AI_END_CHAT_CALLBACK)]]
    )


def build_search_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Build inline buttons for internet-search confirmation."""
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(text="🌍 جستجو در اینترنت", callback_data=AI_SEARCH_YES_CALLBACK),
            InlineKeyboardButton(text="❌ لغو", callback_data=AI_SEARCH_NO_CALLBACK),
        ]]
    )


def get_menu_markup(menu_name: str) -> ReplyKeyboardMarkup:
    """Return the keyboard markup for a named menu."""
    if menu_name == MENU_NEWS:
        return build_news_menu()
    if menu_name == MENU_YOUTUBE:
        return build_youtube_menu()
    if menu_name == MENU_YOUTUBE_BROWSE:
        return build_youtube_browse_menu()
    if menu_name == MENU_FILE_TOOLS:
        return build_file_tools_menu()
    if menu_name == MENU_AI_CHAT:
        return build_ai_chat_menu()
    if menu_name == MENU_GENERAL_AI:
        return build_general_ai_menu()
    if menu_name == MENU_SETTINGS:
        return build_settings_menu(is_owner=False)
    if menu_name == MENU_SETTINGS_BROADCAST:
        return build_rubika_broadcast_menu()
    if menu_name == MENU_SETTINGS_DEEPSEEK:
        return build_deepseek_settings_menu()
    return build_main_menu()


def get_current_menu_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Read the current menu name from user state."""
    return str(context.user_data.get("menu_name", MENU_MAIN))


def is_rubika_staff_context(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return whether the current Rubika user is staff."""
    return bool(context.user_data.get("rubika_is_staff", False))


def is_rubika_owner_context(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return whether the current Rubika user is the owner."""
    return bool(context.user_data.get("rubika_is_owner", False))


def is_telegram_staff_context(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return whether the current Telegram user is staff."""
    return bool(context.user_data.get("telegram_is_staff", False))


def is_telegram_owner_context(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return whether the current Telegram user is the owner."""
    return bool(context.user_data.get("telegram_is_owner", False))


def is_telegram_admin_context(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Backward-compatible alias for the Telegram staff flag."""
    return bool(context.user_data.get("telegram_is_admin", False)) or is_telegram_staff_context(context)


def is_staff_context(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return whether the current actor is staff on the active transport."""
    transport_name = get_transport_name(context)
    if transport_name == "rubika":
        return is_rubika_staff_context(context)
    if transport_name == "telegram":
        return is_telegram_staff_context(context)
    return False


def is_owner_context(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return whether the current actor is the owner on the active transport."""
    transport_name = get_transport_name(context)
    if transport_name == "rubika":
        return is_rubika_owner_context(context)
    if transport_name == "telegram":
        return is_telegram_owner_context(context)
    return False


def get_transport_label(transport_name: str) -> str:
    """Return a short display label for one transport."""
    if transport_name == "rubika":
        return "Rubika"
    if transport_name == "telegram":
        return "Telegram"
    return transport_name.title() or "Bot"


def is_developer_mode_allowed(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return whether the current user can access the developer AI mode."""
    return is_staff_context(context)


def is_deepseek_developer_available(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return whether the DeepSeek Developer backend is available."""
    chat_manager = context.application.bot_data.get("gemini_chat_manager")
    return isinstance(chat_manager, GeminiChatManager) and chat_manager.deepseek_enabled


def should_prompt_developer_model_selection(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return whether the current transport should expose the model picker."""
    return (
        get_transport_name(context) in {"telegram", "rubika"}
        and is_deepseek_developer_available(context)
    )


def get_available_developer_models(
    context: ContextTypes.DEFAULT_TYPE,
) -> list[tuple[str, str]]:
    """Return the currently selectable Developer Mode models."""
    models = [(BTN_DEVELOPER_MODEL_GEMINI, DEVELOPER_MODEL_GEMINI_PRO)]
    if should_prompt_developer_model_selection(context):
        models.extend(
            [
                (BTN_DEVELOPER_MODEL_DEEPSEEK_R1, DEVELOPER_MODEL_DEEPSEEK_R1),
                (
                    BTN_DEVELOPER_MODEL_DEEPSEEK_R1_SEARCH,
                    DEVELOPER_MODEL_DEEPSEEK_R1_SEARCH,
                ),
                (
                    BTN_DEVELOPER_MODEL_DEEPSEEK_R1_SILENT,
                    DEVELOPER_MODEL_DEEPSEEK_R1_SILENT,
                ),
            ]
        )
    return models


def build_available_developer_model_rows(
    context: ContextTypes.DEFAULT_TYPE,
) -> list[list[str]]:
    """Return Developer Mode model buttons laid out in compact rows."""
    buttons = [label for label, _model_name in get_available_developer_models(context)]
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]


def get_selected_developer_model(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return the chosen Developer Mode backend/model for the current session."""
    selected = context.user_data.get("developer_selected_model")
    available_models = {
        model_name for _label, model_name in get_available_developer_models(context)
    }
    if isinstance(selected, str) and selected in available_models:
        return selected
    return DEVELOPER_MODEL_GEMINI_PRO


def set_selected_developer_model(
    context: ContextTypes.DEFAULT_TYPE,
    model_name: str | None,
) -> None:
    """Persist the chosen Developer Mode model."""
    if model_name is None:
        context.user_data.pop("developer_selected_model", None)
        return
    context.user_data["developer_selected_model"] = model_name


def get_selected_developer_model_label(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return the button label for the currently selected Developer Mode model."""
    selected_model = get_selected_developer_model(context)
    return DEVELOPER_MODEL_VALUE_TO_BUTTON.get(selected_model, BTN_DEVELOPER_MODEL_GEMINI)


def get_developer_stage(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Return the current Developer Mode substage."""
    stage = context.user_data.get("developer_stage")
    return str(stage) if isinstance(stage, str) else None


def set_developer_stage(context: ContextTypes.DEFAULT_TYPE, stage: str | None) -> None:
    """Persist the current Developer Mode substage."""
    if stage is None:
        context.user_data.pop("developer_stage", None)
        return
    context.user_data["developer_stage"] = stage


def get_developer_input_files(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, object]]:
    """Return the currently staged Developer Mode files."""
    files = context.user_data.setdefault("developer_input_files", [])
    if isinstance(files, list):
        return files
    files = []
    context.user_data["developer_input_files"] = files
    return files


def clear_developer_input_files(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear the staged Developer Mode files."""
    context.user_data.pop("developer_input_files", None)


def clear_developer_mode_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset all transient Developer Mode state."""
    set_developer_stage(context, None)
    set_selected_developer_model(context, None)
    clear_developer_input_files(context)


def get_rubika_broadcast_stage(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Return the current Rubika broadcast stage."""
    stage = context.user_data.get("rubika_broadcast_stage")
    return str(stage) if isinstance(stage, str) else None


def set_rubika_broadcast_stage(
    context: ContextTypes.DEFAULT_TYPE,
    stage: str | None,
) -> None:
    """Persist the current Rubika broadcast stage."""
    if stage is None:
        context.user_data.pop("rubika_broadcast_stage", None)
        return
    context.user_data["rubika_broadcast_stage"] = stage


def clear_rubika_broadcast_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset transient Rubika broadcast compose state."""
    set_rubika_broadcast_stage(context, None)


def parse_deepseek_token_input(text: str) -> list[str]:
    """Extract one or more DeepSeek tokens from an admin-entered message."""
    candidates: list[str] = []
    for part in re.split(r"[\n,]+", text):
        candidate = part.strip()
        if not candidate:
            continue
        assignment_match = DEEPSEEK_TOKEN_ASSIGNMENT_PATTERN.match(candidate)
        if assignment_match:
            candidate = assignment_match.group(1).strip()
        candidate = candidate.strip().strip("\"'`")
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def build_deepseek_settings_prompt(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Render the shared DeepSeek token settings prompt."""
    token_count = len(load_deepseek_chat_auth_tokens())
    deepseek_client = context.application.bot_data.get("deepseek_developer_client")
    backend_url = (
        str(getattr(deepseek_client, "base_url", "") or "").strip()
        if deepseek_client is not None
        else ""
    )
    lines = [
        "🧠 تنظیمات DeepSeek",
        "",
        f"توکن‌های ثبت‌شده: {token_count}",
    ]
    if backend_url:
        lines.append(f"آدرس backend: {backend_url}")
    else:
        lines.append("آدرس backend هنوز برای DeepSeek تنظیم نشده است.")
    lines.extend(
        [
            "",
            "یک token جدید یا چند token را با جداکننده comma یا خط جدید بفرست.",
            "اگر فرمتت چیزی مثل `token 3 = ...` باشد هم تشخیصش می‌دهم.",
        ]
    )
    return "\n".join(lines)


async def refresh_deepseek_proxy_service() -> tuple[bool, str]:
    """Recreate the local DeepSeek proxy so newly saved tokens take effect."""
    docker_path = shutil.which("docker")
    if not docker_path:
        return False, "docker روی این سیستم پیدا نشد."

    process = await asyncio.create_subprocess_exec(
        docker_path,
        "compose",
        "-f",
        "docker-compose.deepseek.yml",
        "up",
        "-d",
        "--build",
        "--force-recreate",
        cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    output = "\n".join(
        part.strip()
        for part in (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
        if part and part.strip()
    ).strip()
    if process.returncode == 0:
        return True, output
    return False, output or "docker compose failed."


def format_developer_input_files(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Render the staged Developer Mode file list."""
    files = get_developer_input_files(context)
    if not files:
        return "هیچ فایلی ثبت نشده است."
    lines = ["فایل‌های ثبت‌شده:"]
    for index, file_info in enumerate(files, start=1):
        lines.append(f"{index}. {file_info.get('name', 'file')}")
    return "\n".join(lines)


def build_developer_prompt_intro(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return the default Developer Mode prompt instructions."""
    return (
        f"مدل انتخاب‌شده: {get_selected_developer_model_label(context)}\n"
        "پرامپتت را بفرست.\n"
        "اگر وسط گفتگو لازم شد، با «📎 افزودن فایل» فایل یا عکس اضافه کن.\n"
        "اگر لازم باشد برایت فایل کد هم تولید می‌کنم."
    )


def set_current_menu(context: ContextTypes.DEFAULT_TYPE, menu_name: str) -> None:
    """Persist the current menu name in user state."""
    context.user_data["menu_name"] = menu_name


def get_current_menu_markup(context: ContextTypes.DEFAULT_TYPE) -> ReplyKeyboardMarkup:
    """Build the keyboard for the user's current menu."""
    menu_name = get_current_menu_name(context)
    if menu_name == MENU_SETTINGS:
        return build_settings_menu(is_owner=is_owner_context(context))
    if menu_name == MENU_SETTINGS_BROADCAST:
        return build_rubika_broadcast_menu()
    if menu_name == MENU_SETTINGS_DEEPSEEK:
        return build_deepseek_settings_menu()
    if menu_name == MENU_MAIN:
        return build_main_menu(
            include_settings=is_staff_context(context),
            include_support_bundle=True,
        )
    if menu_name == MENU_FILE_TOOLS:
        return build_file_tools_menu_for_context(context)
    if menu_name == MENU_GENERAL_AI:
        return build_general_ai_menu_for_context(context)
    if menu_name == MENU_AI_CHAT and context.user_data.get("ai_chat_mode") == "developer":
        stage = get_developer_stage(context)
        if stage == DEVELOPER_STAGE_CHOOSE_MODEL:
            return build_developer_model_menu(build_available_developer_model_rows(context))
        if stage in {DEVELOPER_STAGE_CHOOSE_INPUT, DEVELOPER_STAGE_PROMPT_ONLY}:
            return build_developer_prompt_only_menu()
        if stage == DEVELOPER_STAGE_COLLECT_FILES:
            return build_developer_file_collection_menu()
        if stage == DEVELOPER_STAGE_PROMPT_WITH_FILES:
            return build_developer_prompt_with_files_menu()
    return get_menu_markup(menu_name)


def format_news_message(summaries: list[str]) -> str:
    """Render summarized news items for Telegram."""
    lines = ["📰 Latest News", ""]
    lines.extend(summaries)
    return "\n".join(lines)


def ensure_news_source_title(text: str, source_name: str) -> str:
    """Make sure the digest starts with the source title."""
    stripped = text.lstrip()
    expected_prefix = f"📰 {source_name}"
    if stripped.startswith(expected_prefix):
        return stripped
    return f"{expected_prefix}\n\n{stripped}"


def _split_long_text_block(text: str, limit: int) -> list[str]:
    """Split an oversized block on natural boundaries."""
    remaining = text.strip()
    pieces: list[str] = []
    preferred_breaks = ("\n", "\n\n", ". ", "? ", "! ", "؟ ", "، ", "; ", ": ", " ")

    while len(remaining) > limit:
        split_index = -1
        for marker in preferred_breaks:
            candidate = remaining.rfind(marker, 0, limit + 1)
            if candidate > split_index:
                split_index = candidate + (0 if marker == " " else len(marker.strip()))
        if split_index <= max(limit // 3, 0):
            split_index = limit
        piece = remaining[:split_index].rstrip()
        if not piece:
            piece = remaining[:limit].rstrip()
            split_index = len(piece)
        pieces.append(piece)
        remaining = remaining[split_index:].lstrip()

    if remaining:
        pieces.append(remaining)
    return pieces


def chunk_text(text: str, limit: int = MAX_TELEGRAM_MESSAGE_LENGTH) -> list[str]:
    """Split large messages on paragraph and sentence boundaries when possible."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    paragraphs = re.split(r"\n{2,}", text)
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        parts = (
            [paragraph]
            if len(paragraph) <= limit
            else _split_long_text_block(paragraph, limit)
        )
        for part in parts:
            candidate = f"{current}\n\n{part}".strip() if current else part
            if current and len(candidate) > limit:
                chunks.append(current.strip())
                current = part
            else:
                current = candidate

    if current:
        chunks.append(current.strip())
    return chunks


def chunk_text_with_cap(
    text: str,
    *,
    limit: int,
    max_chunks: int,
) -> list[str]:
    """Split text into at most ``max_chunks`` chunks, truncating only if needed."""
    cleaned = text.strip()
    if not cleaned:
        return []
    if max_chunks <= 0:
        return []
    if len(cleaned) <= limit:
        return [cleaned]

    preferred_breaks = ("\n\n", "\n", ". ", "? ", "! ", "؟ ", "، ", "; ", ": ", " ")
    chunks: list[str] = []
    remaining = cleaned
    truncation_suffix = "\n\n...[truncated]"

    while remaining and len(chunks) < max_chunks:
        slots_left = max_chunks - len(chunks)
        if len(remaining) <= limit:
            chunks.append(remaining.strip())
            break

        if slots_left == 1:
            available = max(limit - len(truncation_suffix), 0)
            if len(remaining) <= limit:
                chunks.append(remaining.strip())
            elif available <= 0:
                chunks.append(remaining[:limit].rstrip())
            else:
                chunks.append(remaining[:available].rstrip() + truncation_suffix)
            break

        target = min(limit, max(limit // 2, -(-len(remaining) // slots_left)))
        search_limit = min(len(remaining), limit)
        split_index = -1
        for marker in preferred_breaks:
            candidate = remaining.rfind(marker, 0, min(target, search_limit) + 1)
            if candidate > split_index:
                split_index = candidate + (0 if marker == " " else len(marker.strip()))
        if split_index <= max(limit // 3, 0):
            for marker in preferred_breaks:
                candidate = remaining.find(marker, min(target, search_limit // 2), search_limit + 1)
                if candidate != -1:
                    split_index = candidate + (0 if marker == " " else len(marker.strip()))
                    break
        if split_index <= max(limit // 3, 0):
            split_index = search_limit

        piece = remaining[:split_index].rstrip()
        if not piece:
            piece = remaining[:search_limit].rstrip()
            split_index = len(piece)
        chunks.append(piece)
        remaining = remaining[split_index:].lstrip()

    return chunks


def normalize_ai_reply_text(text: str) -> str:
    """Remove noisy markdown-like formatting from Gemini replies."""
    normalized = text.replace("\r\n", "\n").strip()
    normalized = AI_MARKDOWN_LINK_PATTERN.sub(r"\1: \2", normalized)
    normalized = AI_MARKDOWN_BOLD_PATTERN.sub(r"\1", normalized)
    normalized = AI_MARKDOWN_BULLET_PATTERN.sub("• ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized


def shorten_url_label(url: str) -> str:
    """Build a compact visible label for a hyperlink."""
    parsed = urlparse(url)
    host = (parsed.netloc or "link").removeprefix("www.")
    path = parsed.path.strip("/")
    if path:
        tail = path.split("/")[-1]
        if tail and len(tail) <= 24 and len(path) <= 32:
            return f"{host}/{tail}"
    return host


def escape_and_linkify_html(text: str) -> str:
    """Escape HTML and convert raw URLs into compact clickable links."""
    parts: list[str] = []
    last_index = 0
    for match in AI_URL_PATTERN.finditer(text):
        start, end = match.span()
        url = match.group(0).rstrip(").,]")
        trimmed_end = start + len(url)
        parts.append(html.escape(text[last_index:start]))
        parts.append(
            f'<a href="{html.escape(url, quote=True)}">{html.escape(shorten_url_label(url))}</a>'
        )
        if trimmed_end < end:
            parts.append(html.escape(text[trimmed_end:end]))
        last_index = end
    parts.append(html.escape(text[last_index:]))
    return "".join(parts)


def format_ai_reply_for_telegram(text: str) -> str:
    """Render a Gemini reply as clean Telegram HTML."""
    normalized = normalize_ai_reply_text(text)
    body_text, separator, sources_text = normalized.partition(AI_SOURCES_HEADER)
    is_persian = bool(AI_PERSIAN_TEXT_PATTERN.search(body_text or sources_text))

    body_lines = [escape_and_linkify_html(line) for line in body_text.strip().splitlines()]
    sections = ["\n".join(body_lines).strip()] if body_lines else []

    if separator:
        source_lines: list[str] = []
        for raw_line in sources_text.strip().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = AI_SOURCE_LINE_PATTERN.match(line)
            if match:
                prefix = (match.group("prefix") or "•").strip()
                title = match.group("title")
                url = match.group("url")
                if prefix in {"-", "•"}:
                    prefix = "•"
                source_lines.append(
                    f'{html.escape(prefix)} <a href="{html.escape(url, quote=True)}">{html.escape(title.strip())}</a>'
                )
            else:
                source_lines.append(escape_and_linkify_html(line))
        if source_lines:
            sections.append(
                ("🔗 منابع" if is_persian else "🔗 Sources") + "\n" + "\n".join(source_lines)
            )

    return "\n\n".join(section for section in sections if section).strip()


def format_ai_reply_for_plain(text: str) -> str:
    """Render a Gemini reply as clean plain text for non-Telegram transports."""
    normalized = normalize_ai_reply_text(text)
    body_text, separator, sources_text = normalized.partition(AI_SOURCES_HEADER)
    is_persian = bool(AI_PERSIAN_TEXT_PATTERN.search(body_text or sources_text))

    sections = [body_text.strip()] if body_text.strip() else []
    if separator:
        source_lines: list[str] = []
        for raw_line in sources_text.strip().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = AI_SOURCE_LINE_PATTERN.match(line)
            if match:
                prefix = (match.group("prefix") or "•").strip()
                title = match.group("title")
                url = match.group("url")
                if prefix in {"-", "•"}:
                    prefix = "•"
                source_lines.append(f"{prefix} {title.strip()}\n{url}")
            else:
                source_lines.append(line)
        if source_lines:
            sections.append(
                ("🔗 منابع" if is_persian else "🔗 Sources")
                + "\n"
                + "\n\n".join(source_lines)
            )
    return "\n\n".join(section for section in sections if section).strip()


def prepare_ai_reply(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> tuple[list[str], str | None]:
    """Format one AI reply block for the active transport and split it into chunks."""
    raw_chunks = chunk_text(text)
    if get_transport_name(context) == "telegram":
        return [format_ai_reply_for_telegram(chunk) for chunk in raw_chunks], "HTML"
    return [format_ai_reply_for_plain(chunk) for chunk in raw_chunks], None


def prepare_ai_reply_messages(
    context: ContextTypes.DEFAULT_TYPE,
    texts: Sequence[str],
    *,
    max_chunks: int | None = None,
) -> tuple[list[str], str | None]:
    """Format one or more AI reply blocks and flatten them into sendable messages."""
    if max_chunks is not None:
        combined_text = "\n\n".join(text.strip() for text in texts if text and text.strip())
        if not combined_text:
            return [], "HTML" if get_transport_name(context) == "telegram" else None
        raw_chunks = chunk_text_with_cap(
            combined_text,
            limit=DEVELOPER_REPLY_MESSAGE_LIMIT,
            max_chunks=max_chunks,
        )
        if get_transport_name(context) == "telegram":
            return [format_ai_reply_for_telegram(chunk) for chunk in raw_chunks], "HTML"
        return [format_ai_reply_for_plain(chunk) for chunk in raw_chunks], None

    all_chunks: list[str] = []
    parse_mode: str | None = None
    for text in texts:
        if not text or not text.strip():
            continue
        chunks, parse_mode = prepare_ai_reply(context, text)
        all_chunks.extend(chunks)
    return all_chunks, parse_mode


def sanitize_generated_filename(filename: str) -> str:
    """Return a safe attachment filename for developer-mode artifacts."""
    candidate = Path(filename).name.strip()
    if not candidate:
        candidate = "generated_file.txt"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate).strip("._")
    if not cleaned:
        cleaned = "generated_file.txt"
    if "." not in cleaned:
        cleaned = f"{cleaned}.txt"
    return cleaned


def build_model_attribution_comment(filename: str, model_name: str) -> str | None:
    """Return a language-appropriate attribution comment for generated code files."""
    suffix = Path(filename).suffix.lower()
    if suffix in {".json"}:
        return None
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".cc", ".cpp", ".cs", ".go", ".kt", ".kts", ".php", ".rs", ".swift"}:
        return f"// Generated with model: {model_name}"
    if suffix in {".sql", ".lua", ".hs"}:
        return f"-- Generated with model: {model_name}"
    if suffix in {".css"}:
        return f"/* Generated with model: {model_name} */"
    if suffix in {".html", ".xml", ".svg"}:
        return f"<!-- Generated with model: {model_name} -->"
    return f"# Generated with model: {model_name}"


def add_model_attribution_to_artifact(
    *,
    filename: str,
    file_text: str,
    model_name: str | None,
) -> str:
    """Prepend a model attribution comment, preserving an initial shebang if present."""
    if not model_name:
        return file_text

    comment_line = build_model_attribution_comment(filename, model_name)
    if not comment_line:
        return file_text
    stripped_text = file_text.lstrip()
    if stripped_text.startswith(comment_line):
        return file_text

    if file_text.startswith("#!"):
        first_line, separator, remainder = file_text.partition("\n")
        body = remainder if separator else ""
        parts = [first_line, comment_line]
        if body:
            parts.append(body)
        return "\n".join(parts).rstrip() + "\n"

    return f"{comment_line}\n{file_text.lstrip()}"


def is_supported_developer_input_file(file_name: str, mime_type: str | None) -> bool:
    """Return whether a document looks like a text/code file Developer Mode can read."""
    return is_supported_developer_attachment(file_name, mime_type)


def read_developer_input_text(path: Path) -> tuple[str, bool]:
    """Read a developer input file as text, truncating when necessary."""
    return read_developer_input_attachment(
        path,
        file_name=path.name,
        max_chars=DEVELOPER_MAX_INPUT_TEXT_CHARS,
    )


def _developer_total_input_chars(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return the total staged text size for Developer Mode inputs."""
    total = 0
    for file_info in get_developer_input_files(context):
        total += len(str(file_info.get("text") or ""))
    return total


def _developer_attachments_payload(
    context: ContextTypes.DEFAULT_TYPE,
) -> list[tuple[str, str, bool]]:
    """Return staged Developer Mode files in ai_chat payload format."""
    attachments: list[tuple[str, str, bool]] = []
    for file_info in get_developer_input_files(context):
        attachments.append(
            (
                str(file_info.get("name") or "file"),
                str(file_info.get("text") or ""),
                bool(file_info.get("truncated", False)),
            )
        )
    return attachments


async def send_ai_artifact(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    filename: str,
    file_text: str,
    model_name: str | None = None,
) -> None:
    """Write a generated text artifact to disk and send it to the user."""
    message = update.effective_message
    if message is None:
        return

    artifact_dir = DOWNLOADS_DIR / "developer_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_generated_filename(filename)
    artifact_path = artifact_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    artifact_text = add_model_attribution_to_artifact(
        filename=safe_name,
        file_text=file_text,
        model_name=model_name,
    )
    artifact_path.write_text(artifact_text, encoding="utf-8")
    upload_bot = get_available_upload_bot(context)
    try:
        with artifact_path.open("rb") as artifact_file:
            await upload_bot.send_document(
                chat_id=message.chat_id,
                document=artifact_file,
                filename=safe_name,
                caption=f"Generated file: {safe_name}",
            )
    finally:
        try:
            artifact_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.debug("Failed to remove generated artifact %s", artifact_path, exc_info=True)


def format_file_size_mb(size_bytes: int) -> str:
    """Format bytes as megabytes."""
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def cleanup_directory(directory: Path | None) -> None:
    """Delete a temporary directory tree if it exists."""
    if directory is None:
        return
    try:
        shutil.rmtree(directory, ignore_errors=True)
    except OSError:
        LOGGER.warning("Failed to remove temporary directory: %s", directory)


async def send_process_status(
    message,
    text: str,
    *,
    reply_markup: ReplyKeyboardMarkup | None = None,
    process_messages: list,
) -> object | None:
    """Replace the previous progress message with a new one."""
    if process_messages:
        try:
            await process_messages[-1].delete()
        except TelegramError:
            LOGGER.debug("Failed to delete previous process message", exc_info=True)
        process_messages.clear()

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            status_message = await message.reply_text(text, reply_markup=reply_markup)
            process_messages.append(status_message)
            return status_message
        except NetworkError as exc:
            last_error = exc
            LOGGER.warning(
                "Failed to send process status on attempt %s/3",
                attempt + 1,
                exc_info=True,
            )
            if attempt < 2:
                await asyncio.sleep(attempt + 1)
        except TelegramError:
            LOGGER.warning("Failed to send process status", exc_info=True)
            return None

    LOGGER.warning("Giving up on process status update after repeated network failures: %s", last_error)
    return None


async def delete_process_messages(process_messages: list) -> None:
    """Delete all progress messages associated with a processing flow."""
    for process_message in process_messages:
        try:
            await process_message.delete()
        except TelegramError:
            LOGGER.debug("Failed to delete process message", exc_info=True)
    process_messages.clear()


def get_file_tool_max_input_bytes(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return the configured max input size for file-processing tools."""
    return int(context.application.bot_data.get("video_compress_max_input_bytes", 200 * 1024 * 1024))


def parse_pdf_page_range(text: str, total_pages: int) -> tuple[int, int]:
    """Parse a human-entered page range like 3-7 or 3,7."""
    cleaned = text.strip()
    match = re.fullmatch(r"(\d+)\s*[-,:\s]\s*(\d+)", cleaned)
    if match is None:
        raise ValueError("Use a range like 3-7.")

    start_page = int(match.group(1))
    end_page = int(match.group(2))
    if start_page < 1 or end_page < 1 or start_page > end_page or end_page > total_pages:
        raise ValueError(f"Invalid range. This PDF has {total_pages} pages.")
    return start_page, end_page


def cleanup_pdf_split_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove temporary PDF splitter files and user state."""
    cleanup_directory(context.user_data.pop("pdf_split_temp_dir", None))
    context.user_data.pop("pdf_split_input_path", None)
    context.user_data.pop("pdf_split_page_count", None)
    context.user_data.pop("pdf_split_file_name", None)


def init_pdf_merge_state(context: ContextTypes.DEFAULT_TYPE) -> Path:
    """Create a fresh PDF merge session for the user."""
    cleanup_pdf_merge_state(context)
    temp_dir = DOWNLOADS_DIR / f"pdf-merger-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    context.user_data["pdf_merge_temp_dir"] = temp_dir
    context.user_data["pdf_merge_input_paths"] = []
    context.user_data["pdf_merge_names"] = []
    return temp_dir


def cleanup_pdf_merge_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove temporary PDF merger files and user state."""
    cleanup_directory(context.user_data.pop("pdf_merge_temp_dir", None))
    context.user_data.pop("pdf_merge_input_paths", None)
    context.user_data.pop("pdf_merge_names", None)


def format_pdf_merge_queue(file_names: list[str]) -> str:
    """Render the current PDF merge queue for the user."""
    if not file_names:
        return "هنوز هیچ PDFی برای ادغام اضافه نشده است."
    lines = ["فایل‌های آماده برای ادغام:"]
    for index, file_name in enumerate(file_names, start=1):
        lines.append(f"{index}. {file_name}")
    return "\n".join(lines)


def get_available_upload_bot(context: ContextTypes.DEFAULT_TYPE) -> Bot:
    """Return a reachable bot instance for media uploads."""
    local_upload_bot: Bot | None = context.application.bot_data.get("local_upload_bot")
    local_bot_api_url: str | None = context.application.bot_data.get("local_bot_api_url")
    if local_upload_bot is not None and is_local_bot_api_reachable(local_bot_api_url):
        return local_upload_bot
    return context.bot


def get_transport_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return the active transport name for this application."""
    return str(context.application.bot_data.get("transport_name", "telegram"))


def record_search_activity(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    feature: str,
    query: str,
    update: Update,
    extra: dict[str, object] | None = None,
) -> None:
    """Release packages do not persist user search or AI activity analytics."""
    return


def transport_supports_large_uploads(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return whether this transport should expose download options above 50MB."""
    if get_transport_name(context) != "telegram":
        return True

    local_upload_bot: Bot | None = context.application.bot_data.get("local_upload_bot")
    local_bot_api_url: str | None = context.application.bot_data.get("local_bot_api_url")
    return bool(local_upload_bot is not None and is_local_bot_api_reachable(local_bot_api_url))


async def create_download_status_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    initial_text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Create the message used for YouTube download progress updates."""
    query = update.callback_query
    if query is None or query.message is None:
        return None

    if get_transport_name(context) == "rubika":
        try:
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
            )
        except Exception:
            LOGGER.debug("Failed to delete Rubika quality picker message", exc_info=True)
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                LOGGER.debug("Failed to clear Rubika quality picker markup", exc_info=True)
        return await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=initial_text,
            reply_markup=reply_markup,
        )

    if query.message.photo or query.message.video or query.message.document:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                reply_markup=None,
            )
        except Exception:
            LOGGER.debug("Failed to clear source media card markup", exc_info=True)
        return await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=initial_text,
            reply_markup=reply_markup,
        )

    await query.edit_message_text(initial_text, reply_markup=reply_markup)
    return query.message


async def update_download_status_message(
    status_message: Message | None,
    text: str,
    *,
    disable_web_page_preview: bool | None = None,
    reply_markup: InlineKeyboardMarkup | None | object = KEEP_REPLY_MARKUP,
) -> None:
    """Edit the active YouTube status message when possible."""
    if status_message is None:
        return
    edit_kwargs: dict[str, object] = {"text": text}
    if disable_web_page_preview is not None:
        edit_kwargs["disable_web_page_preview"] = disable_web_page_preview
    if reply_markup is not KEEP_REPLY_MARKUP:
        edit_kwargs["reply_markup"] = reply_markup
    await status_message.edit_text(**edit_kwargs)


def resolve_local_bot_api_input_file_path(
    file_path_text: str | None,
    *,
    bot_token: str | None = None,
) -> Path | None:
    """Map a local Bot API file path to the host-visible file stored on disk."""
    if not file_path_text:
        return None

    file_path = Path(file_path_text)
    if file_path.exists():
        return file_path

    container_prefix = Path("/var/lib/telegram-bot-api")
    try:
        relative_path = file_path.relative_to(container_prefix)
    except ValueError:
        if file_path.is_absolute():
            return None
        relative_path = file_path

    candidates = [LOCAL_BOT_API_DATA_DIR / relative_path]
    if bot_token:
        candidates.insert(0, LOCAL_BOT_API_DATA_DIR / bot_token / relative_path)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def find_local_bot_api_uploaded_file(
    bot_token: str,
    *,
    suffix: str,
    expected_size: int | None = None,
    received_at: datetime | None = None,
) -> Path | None:
    """Find the most recent uploaded file stored by the local Bot API server."""
    token_directory = LOCAL_BOT_API_DATA_DIR / bot_token
    if not token_directory.exists():
        return None

    min_mtime = (received_at.timestamp() - 120) if received_at is not None else None
    files = [
        path
        for path in token_directory.rglob("*")
        if path.is_file()
        and path.name not in {"td.binlog", ".DS_Store"}
        and (min_mtime is None or path.stat().st_mtime >= min_mtime)
    ]
    if not files:
        return None

    def pick(paths: list[Path]) -> Path | None:
        if not paths:
            return None
        paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return paths[0]

    exact_match = pick(
        [
            path
            for path in files
            if path.suffix.lower() == suffix
            and (expected_size is None or path.stat().st_size == expected_size)
        ]
    )
    if exact_match is not None:
        return exact_match

    size_match = pick(
        [
            path
            for path in files
            if expected_size is not None and path.stat().st_size == expected_size
        ]
    )
    if size_match is not None:
        return size_match

    if expected_size is None:
        suffix_match = pick([path for path in files if path.suffix.lower() == suffix])
        if suffix_match is not None:
            return suffix_match

    return None


async def download_media_to_local_path(
    context: ContextTypes.DEFAULT_TYPE,
    media,
    *,
    suffix: str,
    destination: Path,
    received_at: datetime | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
) -> None:
    """Download a Telegram media file to a local path, preferring local Bot API storage."""
    local_upload_bot: Bot | None = context.application.bot_data.get("local_upload_bot")
    local_bot_api_url: str | None = context.application.bot_data.get("local_bot_api_url")
    local_download_error: Exception | None = None
    file_size = getattr(media, "file_size", None)
    telethon_media_downloader: TelegramMediaDownloader | None = context.application.bot_data.get(
        "telegram_media_downloader"
    )
    local_bot_api_reachable = bool(
        local_upload_bot is not None and is_local_bot_api_reachable(local_bot_api_url)
    )
    if local_upload_bot is not None:
        local_input_path: Path | None = None
        allow_local_storage_lookup = file_size is not None

        if local_bot_api_reachable:
            try:
                telegram_file = await local_upload_bot.get_file(media.file_id)
            except BadRequest as exc:
                if "file is too big" not in str(exc).lower() or not allow_local_storage_lookup:
                    local_download_error = exc
                else:
                    local_input_path = find_local_bot_api_uploaded_file(
                        local_upload_bot.token,
                        suffix=suffix,
                        expected_size=file_size,
                        received_at=received_at,
                    )
            except Exception as exc:
                local_download_error = exc
            else:
                local_input_path = resolve_local_bot_api_input_file_path(
                    telegram_file.file_path,
                    bot_token=local_upload_bot.token,
                )
                if local_input_path is not None:
                    LOGGER.info("Using local Bot API file for download: %s", local_input_path)
                    await asyncio.to_thread(shutil.copyfile, local_input_path, destination)
                    return
                try:
                    await telegram_file.download_to_drive(custom_path=str(destination))
                    return
                except Exception as exc:
                    local_download_error = exc
        elif getattr(media, "file_size", 0) > CLOUD_BOT_API_DOWNLOAD_LIMIT_BYTES:
            LOGGER.warning(
                "Local Bot API is not reachable, trying local storage fallback for large file download."
            )

        if local_input_path is None and allow_local_storage_lookup:
            local_input_path = find_local_bot_api_uploaded_file(
                local_upload_bot.token,
                suffix=suffix,
                expected_size=file_size,
                received_at=received_at,
            )

        if local_input_path is not None:
            LOGGER.info("Using fallback local Bot API file for download: %s", local_input_path)
            await asyncio.to_thread(shutil.copyfile, local_input_path, destination)
            return

    if (
        getattr(media, "file_size", 0) > CLOUD_BOT_API_DOWNLOAD_LIMIT_BYTES
        and telethon_media_downloader is not None
        and chat_id is not None
        and message_id is not None
    ):
        try:
            LOGGER.info(
                "Trying Telethon media download fallback for chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            await telethon_media_downloader.download_message_media(
                chat_id=chat_id,
                message_id=message_id,
                destination=destination,
            )
            return
        except Exception as exc:
            local_download_error = exc

    if getattr(media, "file_size", 0) and getattr(media, "file_size", 0) > CLOUD_BOT_API_DOWNLOAD_LIMIT_BYTES:
        raise RuntimeError(
            "دریافت فایل انجام نشد.\n"
            "نه local Bot API در دسترس بود و نه fallback دانلود از طریق Telethon موفق شد."
        ) from local_download_error

    cloud_download_bot: Bot = context.application.bot_data.get("cloud_download_bot") or context.bot
    telegram_file = await cloud_download_bot.get_file(media.file_id)
    await telegram_file.download_to_drive(custom_path=str(destination))


def load_ai_reference_payloads(context: ContextTypes.DEFAULT_TYPE) -> dict[str, dict[str, str]]:
    """Load current AI reference files from disk so deleted files are not served from stale memory."""
    payloads: dict[str, dict[str, str]] = {}
    stored_payloads: dict[str, dict[str, str]] = context.application.bot_data.get(
        "ai_reference_payloads",
        {},
    )
    for key, payload in stored_payloads.items():
        file_path_text = payload.get("file_path")
        raw_text = ""
        if file_path_text:
            file_path = Path(file_path_text)
            if file_path.exists():
                raw_text = file_path.read_text(encoding="utf-8")
        payloads[key] = {
            "file_path": file_path_text or "",
            "raw_text": raw_text,
            "source_name": payload.get("source_name", ""),
        }
    return payloads


def get_rubika_access_control(context: ContextTypes.DEFAULT_TYPE) -> RubikaAccessControl | None:
    """Return the Rubika access-control store when running behind the Rubika bridge."""
    access_control = context.application.bot_data.get("rubika_access_control")
    return access_control if isinstance(access_control, RubikaAccessControl) else None


def get_telegram_access_control(context: ContextTypes.DEFAULT_TYPE) -> RubikaAccessControl | None:
    """Return the Telegram access-control store when running the Telegram app."""
    access_control = context.application.bot_data.get("telegram_access_control")
    return access_control if isinstance(access_control, RubikaAccessControl) else None


def get_transport_access_control(context: ContextTypes.DEFAULT_TYPE) -> RubikaAccessControl | None:
    """Return the access-control store for the active transport."""
    transport_name = get_transport_name(context)
    if transport_name == "rubika":
        return get_rubika_access_control(context)
    if transport_name == "telegram":
        return get_telegram_access_control(context)
    return None


def resolve_rubika_target_chat_id(
    context: ContextTypes.DEFAULT_TYPE,
    rubika_chat_id: str,
) -> int | str:
    """Map an actual Rubika chat id to the synthetic PTB chat id used by the bridge."""
    bridge_runtime = context.application.bot_data.get("bridge_runtime")
    if bridge_runtime is None:
        return rubika_chat_id
    try:
        return bridge_runtime.register_chat_id(rubika_chat_id)
    except Exception:
        LOGGER.debug("Failed to register synthetic Rubika chat id", exc_info=True)
        return rubika_chat_id


def resolve_telegram_target_chat_id(telegram_chat_id: str) -> int | str:
    """Normalize a stored Telegram chat id for PTB send calls."""
    cleaned = str(telegram_chat_id or "").strip()
    if cleaned.lstrip("-").isdigit():
        return int(cleaned)
    return cleaned


def resolve_transport_target_chat_id(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str,
) -> int | str:
    """Normalize one stored chat id for the active transport."""
    if get_transport_name(context) == "rubika":
        return resolve_rubika_target_chat_id(context, chat_id)
    return resolve_telegram_target_chat_id(chat_id)


def parse_command_parts(update: Update) -> tuple[str | None, list[str]]:
    """Parse the current message as a slash command plus arguments."""
    message = update.effective_message
    if message is None or not message.text:
        return None, []
    text = message.text.strip()
    if not text.startswith("/"):
        return None, []
    parts = text.split()
    command = parts[0][1:].split("@", maxsplit=1)[0].lower()
    return command or None, parts[1:]


def resolve_telegram_actor_identity(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str | None, str | None, str | None]:
    """Resolve the Telegram user id, preferred private-chat id, and display name."""
    if get_transport_name(context) != "telegram":
        return None, None, None

    effective_user = update.effective_user
    effective_chat = update.effective_chat
    user_id = str(effective_user.id) if effective_user is not None else None
    chat_id = str(effective_user.id) if effective_user is not None else None
    if chat_id is None and effective_chat is not None:
        chat_id = str(effective_chat.id)

    display_name = None
    if effective_user is not None:
        candidate = " ".join(
            part.strip()
            for part in (effective_user.first_name or "", effective_user.last_name or "")
            if part and part.strip()
        ).strip()
        if candidate:
            display_name = candidate
        elif effective_user.username:
            display_name = f"@{effective_user.username}"

    return user_id, chat_id, display_name


def resolve_rubika_actor_identity(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str | None, str | None, str | None]:
    """Resolve the actual Rubika user/chat ids plus the best available display name."""
    if get_transport_name(context) != "rubika":
        return None, None, None

    bridge_runtime = context.application.bot_data.get("bridge_runtime")
    effective_user = update.effective_user
    effective_chat = update.effective_chat
    user_id: str | None = None
    chat_id: str | None = None
    if bridge_runtime is not None:
        try:
            if effective_user is not None:
                user_id = bridge_runtime.resolve_rubika_user_id(effective_user.id)
        except Exception:
            LOGGER.debug("Failed to resolve actual Rubika user id", exc_info=True)
        try:
            if effective_chat is not None:
                chat_id = bridge_runtime.resolve_rubika_chat_id(effective_chat.id)
        except Exception:
            LOGGER.debug("Failed to resolve actual Rubika chat id", exc_info=True)

    if chat_id is None and effective_chat is not None:
        chat_id = str(effective_chat.id)
    if user_id is None and effective_user is not None:
        user_id = str(effective_user.id)
    if user_id is None:
        user_id = chat_id

    display_name = None
    if effective_user is not None:
        parts = [effective_user.first_name or "", effective_user.last_name or ""]
        candidate = " ".join(part.strip() for part in parts if part.strip()).strip()
        if candidate and candidate.lower() != "rubika user":
            display_name = candidate
        elif effective_user.username:
            display_name = f"@{effective_user.username}"

    return user_id, chat_id, display_name


def resolve_transport_actor_identity(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str | None, str | None, str | None]:
    """Resolve the current actor identity for the active transport."""
    if get_transport_name(context) == "rubika":
        return resolve_rubika_actor_identity(update, context)
    return resolve_telegram_actor_identity(update, context)


def resolve_transport_access_actor_id(
    access_control: RubikaAccessControl | None,
    *,
    user_id: str | None,
    chat_id: str | None,
) -> str | None:
    """Resolve the canonical access-control actor id for the active transport."""
    if access_control is None:
        return user_id or chat_id
    return access_control.resolve_user_id(user_id=user_id, chat_id=chat_id)


def set_transport_actor_flags(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    transport_name: str,
    actor_id: str,
    is_owner: bool,
    is_staff: bool,
) -> None:
    """Persist per-transport owner/admin flags for the current actor."""
    context.user_data[f"{transport_name}_actor_id"] = actor_id
    context.user_data[f"{transport_name}_is_owner"] = is_owner
    context.user_data[f"{transport_name}_is_staff"] = is_staff
    if transport_name == "telegram":
        context.user_data["telegram_is_admin"] = is_staff


def get_rubika_role_label(access_control: RubikaAccessControl, user_id: str) -> str:
    """Return the effective access role for a user."""
    if access_control.is_owner(user_id):
        return "owner"
    if access_control.is_admin(user_id):
        return "admin"
    return "user"


def format_rubika_user_summary(
    access_control: RubikaAccessControl,
    record: dict[str, str | int],
) -> str:
    """Render one access-control user line for list/admin messages."""
    user_id = str(record.get("user_id") or "-")
    status = str(record.get("status") or RUBIKA_USER_STATUS_PENDING)
    role = get_rubika_role_label(access_control, user_id)
    display_name = str(record.get("display_name") or "").strip() or "بدون نام"
    last_seen_at = str(record.get("last_seen_at") or "-")
    return (
        f"• {display_name}\n"
        f"  role: {role} | status: {status}\n"
        f"  user_id: {user_id}\n"
        f"  chat_id: {record.get('chat_id') or user_id}\n"
        f"  last_seen: {last_seen_at}"
    )


def normalize_rubika_requested_name(text: str | None) -> str | None:
    """Validate and normalize the name a pending Rubika user sends for approval."""
    if text is None:
        return None
    cleaned = re.sub(r"\s+", " ", text.strip())
    if not cleaned or cleaned.startswith("/"):
        return None
    if len(cleaned) > 60:
        return None
    return cleaned


def build_rubika_start_notice(
    access_control: RubikaAccessControl,
    *,
    transport_name: str,
    rubika_user_id: str,
    rubika_chat_id: str,
    display_name: str | None,
    record: dict[str, Any],
) -> str:
    """Render the staff notice for a transport access request or /start event."""
    role = get_rubika_role_label(access_control, rubika_user_id)
    status = (
        RUBIKA_USER_STATUS_ALLOWED
        if access_control.is_allowed(rubika_user_id)
        else str(record.get("status") or RUBIKA_USER_STATUS_PENDING)
    )
    transport_label = get_transport_label(transport_name)
    notice = (
        f"{transport_label} /start received\n"
        f"user_id: {rubika_user_id}\n"
        f"chat_id: {rubika_chat_id}\n"
        f"role: {role}\n"
        f"status: {status}\n"
        f"start_count: {record.get('start_count', 0)}"
    )
    if display_name:
        notice = f"{notice}\nname: {display_name}"
    notice += "\n\nبرای مدیریت از بخش تنظیمات استفاده کن."
    return notice


def build_access_name_prompt(transport_name: str) -> str:
    """Return the pending-user prompt for sending a display name."""
    if transport_name == "telegram":
        return (
            "برای این‌که سرعت ربات پایین نیاید و دست هر کسی نیفتد، "
            "فعال‌سازی دسترسی فقط بعد از تایید ادمین انجام می‌شود.\n"
            "لطفا اسم خودت را در یک پیام کوتاه بفرست."
        )
    return "قبل از بررسی دسترسی، لطفا اسم خودت را در یک پیام کوتاه بفرست."


def build_access_request_submitted_text(transport_name: str) -> str:
    """Return the confirmation shown after a pending user sends a name."""
    if transport_name == "telegram":
        return (
            "اسم شما ثبت شد و درخواست دسترسی برای ادمین‌ها ارسال شد.\n"
            "بعد از تایید دوباره /start را بزن."
        )
    return (
        "اسم شما ثبت شد و درخواست دسترسی برای ادمین‌ها ارسال شد.\n"
        "بعد از تایید دوباره /start را بزن."
    )


def build_access_owner_bootstrap_text(transport_name: str) -> str:
    """Return the confirmation shown when an owner account is registered."""
    transport_label = get_transport_label(transport_name)
    return (
        f"این حساب به‌عنوان Owner {transport_label} ثبت شد.\n"
        "از این به بعد فقط Owner می‌تواند ادمین جدید تعیین کند."
    )


def build_access_denied_text(transport_name: str, status: str) -> str:
    """Return the access-denied text for the given transport and status."""
    if status == RUBIKA_USER_STATUS_PENDING:
        if transport_name == "telegram":
            return (
                "درخواست دسترسی شما ثبت شد و منتظر تایید ادمین است.\n"
                "بعد از تایید دوباره /start را بزن."
            )
        return (
            "درخواست دسترسی شما ثبت شد و منتظر تایید ادمین است.\n"
            "بعد از تایید دوباره /start را بزن."
        )
    if status == RUBIKA_USER_STATUS_SUSPENDED:
        return (
            "دسترسی شما فعلا تعلیق شده است.\n"
            "اگر فکر می‌کنی اشتباه شده، با ادمین هماهنگ کن."
        )
    if status == RUBIKA_USER_STATUS_BLOCKED:
        return "دسترسی شما به این ربات مسدود شده است."
    return "دسترسی شما به این ربات فعال نیست."


def build_access_join_review_prompt(transport_name: str, requester_name: str) -> str:
    """Return the guided approval prompt sent to staff."""
    transport_label = get_transport_label(transport_name)
    return (
        f"{requester_name} می‌خواهد از ربات {transport_label} استفاده کند.\n"
        "اجازه می‌دهی؟"
    )


def get_rubika_settings_view_title(view: str) -> str:
    """Return the display label for a settings user list view."""
    if view == RUBIKA_SETTINGS_VIEW_PENDING:
        return "درخواست‌ها"
    if view == RUBIKA_SETTINGS_VIEW_ADMINS:
        return "Owner/Adminها"
    return "همه کاربران"


def get_rubika_records_for_view(
    access_control: RubikaAccessControl,
    view: str,
) -> list[dict[str, Any]]:
    """Return the correct user collection for a settings view."""
    if view == RUBIKA_SETTINGS_VIEW_PENDING:
        return access_control.list_users(status=RUBIKA_USER_STATUS_PENDING)
    if view == RUBIKA_SETTINGS_VIEW_ADMINS:
        return access_control.list_staff()
    return access_control.list_users()


def build_rubika_user_list_keyboard(
    records: list[dict[str, Any]],
    *,
    view: str,
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for selecting a Rubika user from settings."""
    rows: list[list[InlineKeyboardButton]] = []
    for record in records[:20]:
        user_id = str(record.get("user_id") or "")
        if not user_id:
            continue
        display_name = str(record.get("display_name") or "").strip() or user_id[-8:]
        status = str(record.get("status") or RUBIKA_USER_STATUS_PENDING)
        label = f"{display_name} | {status}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:55],
                    callback_data=f"{RUBIKA_SETTINGS_USER_CALLBACK_PREFIX}:{view}:{user_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Refresh",
                callback_data=f"{RUBIKA_SETTINGS_VIEW_CALLBACK_PREFIX}:{view}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_rubika_user_actions_keyboard(
    access_control: RubikaAccessControl,
    record: dict[str, Any],
    *,
    view: str,
    is_owner: bool,
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for managing one Rubika user."""
    user_id = str(record.get("user_id") or "")
    status = str(record.get("status") or RUBIKA_USER_STATUS_PENDING)
    rows: list[list[InlineKeyboardButton]] = []

    action_row: list[InlineKeyboardButton] = []
    if status != RUBIKA_USER_STATUS_ALLOWED:
        action_row.append(
            InlineKeyboardButton(
                text="✅ Allow",
                callback_data=f"{RUBIKA_SETTINGS_ACTION_CALLBACK_PREFIX}:{view}:allow:{user_id}",
            )
        )
    if status != RUBIKA_USER_STATUS_SUSPENDED:
        action_row.append(
            InlineKeyboardButton(
                text="⏸ Suspend",
                callback_data=f"{RUBIKA_SETTINGS_ACTION_CALLBACK_PREFIX}:{view}:suspend:{user_id}",
            )
        )
    if action_row:
        rows.append(action_row)

    if status != RUBIKA_USER_STATUS_BLOCKED:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⛔ Block",
                    callback_data=f"{RUBIKA_SETTINGS_ACTION_CALLBACK_PREFIX}:{view}:block:{user_id}",
                )
            ]
        )

    if is_owner and not access_control.is_owner(user_id):
        if access_control.is_admin(user_id):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="➖ Remove Admin",
                        callback_data=f"{RUBIKA_SETTINGS_ACTION_CALLBACK_PREFIX}:{view}:admin_remove:{user_id}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="➕ Make Admin",
                        callback_data=f"{RUBIKA_SETTINGS_ACTION_CALLBACK_PREFIX}:{view}:admin_add:{user_id}",
                    )
                ]
            )

    rows.append(
        [
            InlineKeyboardButton(
                text="↩️ Back to list",
                callback_data=f"{RUBIKA_SETTINGS_VIEW_CALLBACK_PREFIX}:{view}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_rubika_reset_keyboard() -> InlineKeyboardMarkup:
    """Build the confirmation keyboard for resetting Rubika access state."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="✅ Confirm Reset",
                    callback_data=f"{RUBIKA_SETTINGS_RESET_CALLBACK_PREFIX}:confirm",
                ),
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data=f"{RUBIKA_SETTINGS_RESET_CALLBACK_PREFIX}:cancel",
                ),
            ]
        ]
    )


async def send_rubika_staff_notice(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text: str,
    exclude_chat_id: str | None = None,
) -> None:
    """Broadcast a staff notification to known staff chats on the active transport."""
    access_control = get_transport_access_control(context)
    if access_control is None:
        return
    targets = access_control.notification_chat_ids()
    for target_chat_id in targets:
        if exclude_chat_id is not None and target_chat_id == exclude_chat_id:
            continue
        try:
            await context.bot.send_message(
                chat_id=resolve_transport_target_chat_id(context, target_chat_id),
                text=text,
            )
        except Exception:
            LOGGER.warning(
                "Failed to send %s staff notice to chat_id=%s",
                get_transport_label(get_transport_name(context)),
                target_chat_id,
                exc_info=True,
            )


def get_rubika_review_queues(context: ContextTypes.DEFAULT_TYPE) -> dict[str, list[str]]:
    """Return the in-memory per-staff approval queues for the active transport."""
    return context.application.bot_data.setdefault(f"{get_transport_name(context)}_review_queues", {})


def get_rubika_review_stages(context: ContextTypes.DEFAULT_TYPE) -> dict[str, str]:
    """Return the in-memory per-staff approval stages for the active transport."""
    return context.application.bot_data.setdefault(f"{get_transport_name(context)}_review_stages", {})


def get_rubika_active_review_target(
    context: ContextTypes.DEFAULT_TYPE,
    staff_user_id: str,
) -> str | None:
    """Return the current review target for a staff user."""
    queue = get_rubika_review_queues(context).get(staff_user_id, [])
    return queue[0] if queue else None


def pop_rubika_review_target(
    context: ContextTypes.DEFAULT_TYPE,
    staff_user_id: str,
) -> str | None:
    """Remove and return the current review target for a staff user."""
    queues = get_rubika_review_queues(context)
    queue = queues.get(staff_user_id, [])
    if not queue:
        return None
    removed = queue.pop(0)
    if queue:
        queues[staff_user_id] = queue
    else:
        queues.pop(staff_user_id, None)
    get_rubika_review_stages(context).pop(staff_user_id, None)
    return removed


async def prompt_rubika_join_review(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    access_control: RubikaAccessControl,
    requester_user_id: str,
) -> None:
    """Send or refresh the guided approval prompt for all staff on the active transport."""
    requester = access_control.get_user(requester_user_id)
    if requester is None:
        return
    transport_name = get_transport_name(context)
    requester_name = str(requester.get("display_name") or "").strip() or "کاربر جدید"
    queues = get_rubika_review_queues(context)
    stages = get_rubika_review_stages(context)
    for staff_user_id in access_control.staff_ids():
        queue = queues.setdefault(staff_user_id, [])
        if requester_user_id not in queue:
            queue.append(requester_user_id)
        if len(queue) != 1:
            continue
        stages[staff_user_id] = "decision"
        prompt_text = build_access_join_review_prompt(transport_name, requester_name)
        staff_record = access_control.get_user(staff_user_id)
        target_chat_id = str(staff_record.get("chat_id") or staff_user_id) if staff_record else staff_user_id
        try:
            await context.bot.send_message(
                chat_id=resolve_transport_target_chat_id(context, target_chat_id),
                text=prompt_text,
                reply_markup=build_rubika_review_decision_menu(),
            )
        except Exception:
            LOGGER.warning(
                "Failed to send guided %s approval prompt to staff=%s",
                get_transport_label(transport_name),
                staff_user_id,
                exc_info=True,
            )


async def advance_rubika_join_review(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    access_control: RubikaAccessControl,
    staff_user_id: str,
) -> None:
    """Show the next queued review request to a Rubika staff user, if any."""
    next_target = get_rubika_active_review_target(context, staff_user_id)
    message = update.effective_message
    if message is None:
        return
    if next_target is None:
        get_rubika_review_stages(context).pop(staff_user_id, None)
        set_current_menu(context, MENU_SETTINGS)
        await message.reply_text(
            "درخواست معلق دیگری برای بررسی نیست.",
            reply_markup=get_current_menu_markup(context),
        )
        return

    requester = access_control.get_user(next_target)
    if requester is None:
        pop_rubika_review_target(context, staff_user_id)
        await advance_rubika_join_review(
            update,
            context,
            access_control=access_control,
            staff_user_id=staff_user_id,
        )
        return

    requester_name = str(requester.get("display_name") or "").strip() or "کاربر جدید"
    get_rubika_review_stages(context)[staff_user_id] = "decision"
    await message.reply_text(
        build_access_join_review_prompt(get_transport_name(context), requester_name),
        reply_markup=build_rubika_review_decision_menu(),
    )


async def reply_rubika_access_denied(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    status: str,
) -> None:
    """Send the appropriate access-denied message to the current chat."""
    message_text = build_access_denied_text(get_transport_name(context), status)
    message = update.effective_message
    if message is not None:
        await message.reply_text(message_text)
        return
    if update.effective_chat is not None:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=message_text)


async def enforce_transport_access_guard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    transport_name: str,
) -> None:
    """Enforce allow/block/admin access before normal handlers run."""
    if get_transport_name(context) != transport_name:
        return

    access_control = get_transport_access_control(context)
    if access_control is None:
        return

    actor_user_id, actor_chat_id, display_name = resolve_transport_actor_identity(update, context)
    actor_access_id = resolve_transport_access_actor_id(
        access_control,
        user_id=actor_user_id,
        chat_id=actor_chat_id,
    )
    if actor_access_id is None or actor_chat_id is None:
        return

    message = update.effective_message
    message_text = message.text if message is not None and message.text else None
    command, _args = parse_command_parts(update)
    is_start_command = command == "start"

    bootstrapped_owner = (
        access_control.bootstrap_owner(
            user_id=actor_access_id,
            chat_id=actor_chat_id,
            display_name=display_name,
        )
        if is_start_command
        else False
    )
    record = access_control.record_seen(
        user_id=actor_access_id,
        chat_id=actor_chat_id,
        display_name=display_name,
        started=is_start_command,
    )
    actor_is_owner = access_control.is_owner(actor_access_id)
    actor_is_staff = access_control.is_staff(actor_access_id)
    set_transport_actor_flags(
        context,
        transport_name=transport_name,
        actor_id=actor_access_id,
        is_owner=actor_is_owner,
        is_staff=actor_is_staff,
    )

    stored_display_name = str(record.get("display_name") or "").strip() or None
    if (
        not access_control.is_allowed(actor_access_id)
        and not stored_display_name
        and not actor_is_staff
    ):
        submitted_name = normalize_rubika_requested_name(message_text) if not command else None
        if submitted_name is not None:
            record = access_control.record_seen(
                user_id=actor_access_id,
                chat_id=actor_chat_id,
                display_name=submitted_name,
                started=False,
            )
            stored_display_name = submitted_name
            await prompt_rubika_join_review(
                context,
                access_control=access_control,
                requester_user_id=actor_access_id,
            )
            if message is not None:
                await message.reply_text(build_access_request_submitted_text(transport_name))
            raise ApplicationHandlerStop

        if message is not None:
            await message.reply_text(build_access_name_prompt(transport_name))
        raise ApplicationHandlerStop

    should_announce_owner = is_start_command and (
        bootstrapped_owner
        or (
            transport_name == "telegram"
            and actor_is_owner
            and int(record.get("start_count") or 0) == 1
        )
    )
    if should_announce_owner and update.effective_message is not None:
        await update.effective_message.reply_text(
            build_access_owner_bootstrap_text(transport_name)
        )

    if is_start_command:
        if not access_control.is_allowed(actor_access_id) and not actor_is_staff:
            await prompt_rubika_join_review(
                context,
                access_control=access_control,
                requester_user_id=actor_access_id,
            )
        else:
            await send_rubika_staff_notice(
                context,
                text=build_rubika_start_notice(
                    access_control,
                    transport_name=transport_name,
                    rubika_user_id=actor_access_id,
                    rubika_chat_id=actor_chat_id,
                    display_name=stored_display_name or display_name,
                    record=record,
                ),
                exclude_chat_id=actor_chat_id if should_announce_owner else None,
            )

    if access_control.is_allowed(actor_access_id):
        return

    await reply_rubika_access_denied(
        update,
        context,
        status=str(record.get("status") or RUBIKA_USER_STATUS_PENDING),
    )
    raise ApplicationHandlerStop


async def rubika_access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enforce Rubika allow/block/admin access before normal handlers run."""
    await enforce_transport_access_guard(update, context, transport_name="rubika")


async def telegram_access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enforce Telegram allow/block/admin access before normal handlers run."""
    await enforce_transport_access_guard(update, context, transport_name="telegram")


async def register_user_if_needed(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Refresh lightweight transport-specific actor flags."""
    if get_transport_name(context) == "rubika":
        access_control = get_rubika_access_control(context)
        rubika_user_id, rubika_chat_id, _display_name = resolve_rubika_actor_identity(update, context)
        actor_id = resolve_transport_access_actor_id(
            access_control,
            user_id=rubika_user_id,
            chat_id=rubika_chat_id,
        )
        if access_control is not None and actor_id is not None:
            set_transport_actor_flags(
                context,
                transport_name="rubika",
                actor_id=actor_id,
                is_owner=access_control.is_owner(actor_id),
                is_staff=access_control.is_staff(actor_id),
            )
            return

    if get_transport_name(context) == "telegram":
        access_control = get_telegram_access_control(context)
        telegram_user_id, _telegram_chat_id, _display_name = resolve_telegram_actor_identity(update, context)
        if access_control is not None and telegram_user_id is not None:
            set_transport_actor_flags(
                context,
                transport_name="telegram",
                actor_id=telegram_user_id,
                is_owner=access_control.is_owner(telegram_user_id),
                is_staff=access_control.is_staff(telegram_user_id),
            )
            return
        admin_chat_id = context.application.bot_data.get("telegram_admin_chat_id")
        effective_chat = update.effective_chat
        effective_user = update.effective_user
        candidate_ids = {
            value
            for value in (
                effective_chat.id if effective_chat is not None else None,
                effective_user.id if effective_user is not None else None,
            )
            if isinstance(value, int)
        }
        is_admin = isinstance(admin_chat_id, int) and admin_chat_id in candidate_ids
        context.user_data["telegram_is_admin"] = is_admin
        context.user_data["telegram_is_staff"] = is_admin
        context.user_data["telegram_is_owner"] = is_admin
    return


async def safe_edit_or_reply(
    status_message,
    fallback_message,
    text: str,
    *,
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
    reply_markup: ReplyKeyboardMarkup | InlineKeyboardMarkup | None = None,
) -> None:
    """Edit a status message when possible, otherwise send a new reply."""
    inline_reply_markup = (
        reply_markup if isinstance(reply_markup, InlineKeyboardMarkup) else None
    )
    try:
        await status_message.edit_text(
            text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            reply_markup=inline_reply_markup,
        )
    except BadRequest as exc:
        if "can't be edited" not in str(exc).lower():
            raise
        await fallback_message.reply_text(
            text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            reply_markup=reply_markup,
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_MAIN)
    await update.effective_message.reply_text(START_TEXT, reply_markup=get_current_menu_markup(context))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help."""
    await register_user_if_needed(update, context)
    help_text = """راهنمای ربات

📰 /news
ورود به بخش اخبار.

📺 /youtube
ورود به بخش یوتیوب.

🛠 /tools
ورود به بخش ابزار فایل.

🌐 /ai
شروع چت مستقیم با Gemini.
"""
    await update.effective_message.reply_text(
        help_text,
        reply_markup=get_current_menu_markup(context),
    )


async def require_transport_staff(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    expected_transport: str | None = None,
    owner_only: bool = False,
) -> tuple[RubikaAccessControl | None, str | None]:
    """Validate that the current actor may run a staff command."""
    transport_name = get_transport_name(context)
    resolved_transport = expected_transport or transport_name
    if transport_name != resolved_transport:
        if update.effective_message is not None:
            await update.effective_message.reply_text(
                f"این دستور فقط در نسخه {get_transport_label(resolved_transport)} فعال است."
            )
        return None, None

    access_control = get_transport_access_control(context)
    actor_user_id, actor_chat_id, _display_name = resolve_transport_actor_identity(update, context)
    actor_id = resolve_transport_access_actor_id(
        access_control,
        user_id=actor_user_id,
        chat_id=actor_chat_id,
    )
    if access_control is None or actor_id is None:
        return None, None

    allowed = access_control.is_owner(actor_id) if owner_only else access_control.is_staff(actor_id)
    if allowed:
        return access_control, actor_id

    transport_label = get_transport_label(resolved_transport)
    message = (
        "فقط Owner می‌تواند این دستور را اجرا کند."
        if owner_only
        else f"این دستور مخصوص ادمین‌های {transport_label} است."
    )
    if update.effective_message is not None:
        await update.effective_message.reply_text(message)
    return None, None


async def require_current_transport_staff(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    owner_only: bool = False,
) -> tuple[RubikaAccessControl | None, str | None]:
    """Validate that the current actor may run a staff action on this transport."""
    return await require_transport_staff(
        update,
        context,
        expected_transport=None,
        owner_only=owner_only,
    )


async def require_rubika_staff(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    owner_only: bool = False,
) -> tuple[RubikaAccessControl | None, str | None]:
    """Validate that the current Rubika user may run a Rubika-only staff command."""
    return await require_transport_staff(
        update,
        context,
        expected_transport="rubika",
        owner_only=owner_only,
    )


async def require_telegram_staff(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    owner_only: bool = False,
) -> tuple[RubikaAccessControl | None, str | None]:
    """Validate that the current Telegram user may run a Telegram-only staff command."""
    return await require_transport_staff(
        update,
        context,
        expected_transport="telegram",
        owner_only=owner_only,
    )


def get_target_user_id_from_args(update: Update) -> str | None:
    """Extract the target user id from a staff command."""
    _command, args = parse_command_parts(update)
    if not args:
        return None
    return args[0].strip()


async def rubika_access_help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show the Rubika admin command help."""
    access_control, _actor_id = await require_rubika_staff(update, context)
    if access_control is None or update.effective_message is None:
        return
    help_text = (
        "Rubika access commands\n\n"
        "/rubika_users - لیست همه کاربران Rubika\n"
        "/rubika_pending - لیست درخواست‌های در انتظار تایید\n"
        "/rubika_admins - لیست Owner/Adminها\n"
        "/rubika_allow <user_id> - اجازه استفاده\n"
        "/rubika_suspend <user_id> - تعلیق دسترسی\n"
        "/rubika_block <user_id> - مسدودسازی\n"
        "/rubika_admin_add <user_id> - افزودن ادمین (فقط Owner)\n"
        "/rubika_admin_remove <user_id> - حذف ادمین (فقط Owner)"
    )
    await update.effective_message.reply_text(help_text)


async def rubika_users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """List known Rubika users and their current statuses."""
    access_control, _actor_id = await require_rubika_staff(update, context)
    if access_control is None or update.effective_message is None:
        return
    users = access_control.list_users()
    if not users:
        await update.effective_message.reply_text("هنوز هیچ کاربر Rubika ثبت نشده است.")
        return
    lines = ["لیست کاربران Rubika:"]
    for record in users[:50]:
        lines.append(format_rubika_user_summary(access_control, record))
    await update.effective_message.reply_text("\n\n".join(lines))


async def rubika_pending_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """List Rubika users waiting for approval."""
    access_control, _actor_id = await require_rubika_staff(update, context)
    if access_control is None or update.effective_message is None:
        return
    users = access_control.list_users(status=RUBIKA_USER_STATUS_PENDING)
    if not users:
        await update.effective_message.reply_text("درخواستی در صف تایید نیست.")
        return
    lines = ["درخواست‌های در انتظار تایید:"]
    for record in users[:50]:
        lines.append(format_rubika_user_summary(access_control, record))
    await update.effective_message.reply_text("\n\n".join(lines))


async def rubika_admins_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """List Rubika owners/admins."""
    access_control, _actor_id = await require_rubika_staff(update, context)
    if access_control is None or update.effective_message is None:
        return
    staff_records = access_control.list_staff()
    if not staff_records:
        await update.effective_message.reply_text("هنوز هیچ Owner/Adminی ثبت نشده است.")
        return
    lines = ["لیست Owner/Adminهای Rubika:"]
    for record in staff_records:
        lines.append(format_rubika_user_summary(access_control, record))
    await update.effective_message.reply_text("\n\n".join(lines))


async def rubika_allow_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Approve a Rubika user for access."""
    access_control, _actor_id = await require_rubika_staff(update, context)
    target_user_id = get_target_user_id_from_args(update)
    if access_control is None or update.effective_message is None:
        return
    if not target_user_id:
        await update.effective_message.reply_text("استفاده: /rubika_allow <user_id>")
        return
    if access_control.is_staff(target_user_id):
        await update.effective_message.reply_text("این شناسه Owner/Admin است و همیشه مجاز محسوب می‌شود.")
        return
    record = access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_ALLOWED)
    await update.effective_message.reply_text(
        f"دسترسی این کاربر فعال شد:\n{format_rubika_user_summary(access_control, record)}",
    )
    try:
        await context.bot.send_message(
            chat_id=resolve_rubika_target_chat_id(
                context,
                str(record.get("chat_id") or target_user_id),
            ),
            text="دسترسی شما به ربات Rubika فعال شد.\nدوباره /start را بزن.",
        )
    except Exception:
        LOGGER.debug("Failed to send Rubika allow notice", exc_info=True)


async def rubika_suspend_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Suspend a Rubika user."""
    access_control, _actor_id = await require_rubika_staff(update, context)
    target_user_id = get_target_user_id_from_args(update)
    if access_control is None or update.effective_message is None:
        return
    if not target_user_id:
        await update.effective_message.reply_text("استفاده: /rubika_suspend <user_id>")
        return
    if access_control.is_staff(target_user_id):
        await update.effective_message.reply_text(
            "برای محدود کردن یک ادمین، اول با /rubika_admin_remove حذفش کن."
        )
        return
    record = access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_SUSPENDED)
    await update.effective_message.reply_text(
        f"دسترسی این کاربر تعلیق شد:\n{format_rubika_user_summary(access_control, record)}",
    )


async def rubika_block_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Block a Rubika user."""
    access_control, _actor_id = await require_rubika_staff(update, context)
    target_user_id = get_target_user_id_from_args(update)
    if access_control is None or update.effective_message is None:
        return
    if not target_user_id:
        await update.effective_message.reply_text("استفاده: /rubika_block <user_id>")
        return
    if access_control.is_staff(target_user_id):
        await update.effective_message.reply_text(
            "برای محدود کردن یک ادمین، اول با /rubika_admin_remove حذفش کن."
        )
        return
    record = access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_BLOCKED)
    await update.effective_message.reply_text(
        f"این کاربر مسدود شد:\n{format_rubika_user_summary(access_control, record)}",
    )


async def rubika_admin_add_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Grant Rubika admin privileges. Owner only."""
    access_control, _actor_id = await require_rubika_staff(update, context, owner_only=True)
    target_user_id = get_target_user_id_from_args(update)
    if access_control is None or update.effective_message is None:
        return
    if not target_user_id:
        await update.effective_message.reply_text("استفاده: /rubika_admin_add <user_id>")
        return
    record = access_control.add_admin(target_user_id)
    await update.effective_message.reply_text(
        f"این کاربر ادمین شد:\n{format_rubika_user_summary(access_control, record)}",
    )
    try:
        await context.bot.send_message(
            chat_id=resolve_rubika_target_chat_id(
                context,
                str(record.get("chat_id") or target_user_id),
            ),
            text="شما به‌عنوان Admin روبیکا ثبت شدید.",
        )
    except Exception:
        LOGGER.debug("Failed to send Rubika admin-add notice", exc_info=True)


async def rubika_admin_remove_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Remove Rubika admin privileges. Owner only."""
    access_control, _actor_id = await require_rubika_staff(update, context, owner_only=True)
    target_user_id = get_target_user_id_from_args(update)
    if access_control is None or update.effective_message is None:
        return
    if not target_user_id:
        await update.effective_message.reply_text("استفاده: /rubika_admin_remove <user_id>")
        return
    if not access_control.remove_admin(target_user_id):
        await update.effective_message.reply_text("این شناسه ادمین نبود یا Owner است و قابل حذف نیست.")
        return
    await update.effective_message.reply_text(f"ادمین حذف شد: {target_user_id}")


async def telegram_access_help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show the Telegram admin command help."""
    access_control, _actor_id = await require_telegram_staff(update, context)
    if access_control is None or update.effective_message is None:
        return
    help_text = (
        "Telegram access commands\n\n"
        "/telegram_users - لیست همه کاربران Telegram\n"
        "/telegram_pending - لیست درخواست‌های در انتظار تایید\n"
        "/telegram_admins - لیست Owner/Adminها\n"
        "/telegram_allow <user_id> - اجازه استفاده\n"
        "/telegram_suspend <user_id> - تعلیق دسترسی\n"
        "/telegram_block <user_id> - مسدودسازی\n"
        "/telegram_admin_add <user_id> - افزودن ادمین (فقط Owner)\n"
        "/telegram_admin_remove <user_id> - حذف ادمین (فقط Owner)"
    )
    await update.effective_message.reply_text(help_text)


async def telegram_users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """List known Telegram users and their current statuses."""
    access_control, _actor_id = await require_telegram_staff(update, context)
    if access_control is None or update.effective_message is None:
        return
    users = access_control.list_users()
    if not users:
        await update.effective_message.reply_text("هنوز هیچ کاربر Telegram ثبت نشده است.")
        return
    lines = ["لیست کاربران Telegram:"]
    for record in users[:50]:
        lines.append(format_rubika_user_summary(access_control, record))
    await update.effective_message.reply_text("\n\n".join(lines))


async def telegram_pending_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """List Telegram users waiting for approval."""
    access_control, _actor_id = await require_telegram_staff(update, context)
    if access_control is None or update.effective_message is None:
        return
    users = access_control.list_users(status=RUBIKA_USER_STATUS_PENDING)
    if not users:
        await update.effective_message.reply_text("درخواستی در صف تایید نیست.")
        return
    lines = ["درخواست‌های در انتظار تایید:"]
    for record in users[:50]:
        lines.append(format_rubika_user_summary(access_control, record))
    await update.effective_message.reply_text("\n\n".join(lines))


async def telegram_admins_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """List Telegram owners/admins."""
    access_control, _actor_id = await require_telegram_staff(update, context)
    if access_control is None or update.effective_message is None:
        return
    staff_records = access_control.list_staff()
    if not staff_records:
        await update.effective_message.reply_text("هنوز هیچ Owner/Adminی ثبت نشده است.")
        return
    lines = ["لیست Owner/Adminهای Telegram:"]
    for record in staff_records:
        lines.append(format_rubika_user_summary(access_control, record))
    await update.effective_message.reply_text("\n\n".join(lines))


async def telegram_allow_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Approve a Telegram user for access."""
    access_control, _actor_id = await require_telegram_staff(update, context)
    target_user_id = get_target_user_id_from_args(update)
    if access_control is None or update.effective_message is None:
        return
    if not target_user_id:
        await update.effective_message.reply_text("استفاده: /telegram_allow <user_id>")
        return
    if access_control.is_staff(target_user_id):
        await update.effective_message.reply_text("این شناسه Owner/Admin است و همیشه مجاز محسوب می‌شود.")
        return
    record = access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_ALLOWED)
    await update.effective_message.reply_text(
        f"دسترسی این کاربر فعال شد:\n{format_rubika_user_summary(access_control, record)}",
    )
    try:
        await context.bot.send_message(
            chat_id=resolve_transport_target_chat_id(
                context,
                str(record.get("chat_id") or target_user_id),
            ),
            text="دسترسی شما به ربات Telegram فعال شد.\nدوباره /start را بزن.",
        )
    except Exception:
        LOGGER.debug("Failed to send Telegram allow notice", exc_info=True)


async def telegram_suspend_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Suspend a Telegram user."""
    access_control, _actor_id = await require_telegram_staff(update, context)
    target_user_id = get_target_user_id_from_args(update)
    if access_control is None or update.effective_message is None:
        return
    if not target_user_id:
        await update.effective_message.reply_text("استفاده: /telegram_suspend <user_id>")
        return
    if access_control.is_staff(target_user_id):
        await update.effective_message.reply_text(
            "برای محدود کردن یک ادمین، اول با /telegram_admin_remove حذفش کن."
        )
        return
    record = access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_SUSPENDED)
    await update.effective_message.reply_text(
        f"دسترسی این کاربر تعلیق شد:\n{format_rubika_user_summary(access_control, record)}",
    )


async def telegram_block_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Block a Telegram user."""
    access_control, _actor_id = await require_telegram_staff(update, context)
    target_user_id = get_target_user_id_from_args(update)
    if access_control is None or update.effective_message is None:
        return
    if not target_user_id:
        await update.effective_message.reply_text("استفاده: /telegram_block <user_id>")
        return
    if access_control.is_staff(target_user_id):
        await update.effective_message.reply_text(
            "برای محدود کردن یک ادمین، اول با /telegram_admin_remove حذفش کن."
        )
        return
    record = access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_BLOCKED)
    await update.effective_message.reply_text(
        f"این کاربر مسدود شد:\n{format_rubika_user_summary(access_control, record)}",
    )


async def telegram_admin_add_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Grant Telegram admin privileges. Owner only."""
    access_control, _actor_id = await require_telegram_staff(update, context, owner_only=True)
    target_user_id = get_target_user_id_from_args(update)
    if access_control is None or update.effective_message is None:
        return
    if not target_user_id:
        await update.effective_message.reply_text("استفاده: /telegram_admin_add <user_id>")
        return
    record = access_control.add_admin(target_user_id)
    await update.effective_message.reply_text(
        f"این کاربر ادمین شد:\n{format_rubika_user_summary(access_control, record)}",
    )
    try:
        await context.bot.send_message(
            chat_id=resolve_transport_target_chat_id(
                context,
                str(record.get("chat_id") or target_user_id),
            ),
            text="شما به‌عنوان Admin تلگرام ثبت شدید.",
        )
    except Exception:
        LOGGER.debug("Failed to send Telegram admin-add notice", exc_info=True)


async def telegram_admin_remove_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Remove Telegram admin privileges. Owner only."""
    access_control, _actor_id = await require_telegram_staff(update, context, owner_only=True)
    target_user_id = get_target_user_id_from_args(update)
    if access_control is None or update.effective_message is None:
        return
    if not target_user_id:
        await update.effective_message.reply_text("استفاده: /telegram_admin_remove <user_id>")
        return
    if not access_control.remove_admin(target_user_id):
        await update.effective_message.reply_text("این شناسه ادمین نبود یا Owner است و قابل حذف نیست.")
        return
    await update.effective_message.reply_text(f"ادمین حذف شد: {target_user_id}")


async def show_rubika_settings_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text: str | None = None,
) -> None:
    """Show the staff settings menu for the active transport."""
    access_control, actor_id = await require_current_transport_staff(update, context)
    message = update.effective_message
    if access_control is None or actor_id is None or message is None:
        return
    clear_rubika_broadcast_state(context)
    set_current_menu(context, MENU_SETTINGS)
    menu_text = text or f"بخش تنظیمات {get_transport_label(get_transport_name(context))}"
    await message.reply_text(
        menu_text,
        reply_markup=build_settings_menu(is_owner=access_control.is_owner(actor_id)),
    )


async def show_deepseek_settings_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Prompt a staff user to append DeepSeek tokens."""
    access_control, actor_id = await require_current_transport_staff(update, context)
    message = update.effective_message
    if access_control is None or actor_id is None or message is None:
        return ConversationHandler.END
    set_current_menu(context, MENU_SETTINGS_DEEPSEEK)
    await message.reply_text(
        build_deepseek_settings_prompt(context),
        reply_markup=get_current_menu_markup(context),
    )
    return DEEPSEEK_SETTINGS_INPUT


async def send_rubika_settings_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    view: str,
    edit_message: Message | None = None,
) -> None:
    """Render a settings user list for the active transport."""
    access_control, _actor_id = await require_current_transport_staff(update, context)
    message = update.effective_message
    if access_control is None:
        return
    records = get_rubika_records_for_view(access_control, view)
    title = get_rubika_settings_view_title(view)
    if not records:
        text = f"{title}\n\nموردی برای نمایش نیست."
        if edit_message is not None:
            await edit_message.edit_text(text=text)
        elif message is not None:
            await message.reply_text(text)
        return

    text = f"{title}\n\nیکی از کاربران زیر را انتخاب کن:"
    reply_markup = build_rubika_user_list_keyboard(records, view=view)
    if edit_message is not None:
        await edit_message.edit_text(text=text, reply_markup=reply_markup)
    elif message is not None:
        await message.reply_text(text, reply_markup=reply_markup)


async def show_rubika_user_actions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: str,
    view: str,
    edit_message: Message,
) -> None:
    """Show details and available actions for one user on the active transport."""
    access_control, actor_id = await require_current_transport_staff(update, context)
    if access_control is None or actor_id is None:
        return
    record = access_control.get_user(user_id)
    if record is None:
        await edit_message.edit_text("این کاربر دیگر در لیست وجود ندارد.")
        return
    text = f"مدیریت کاربر\n\n{format_rubika_user_summary(access_control, record)}"
    await edit_message.edit_text(
        text=text,
        reply_markup=build_rubika_user_actions_keyboard(
            access_control,
            record,
            view=view,
            is_owner=access_control.is_owner(actor_id),
        ),
    )


async def rubika_settings_menu_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle shared settings keyboard buttons."""
    message = update.effective_message
    if message is None or not message.text:
        return
    if message.text == BTN_MAIN_SETTINGS:
        await show_rubika_settings_menu(update, context)
        raise ApplicationHandlerStop
    if message.text == BTN_SETTINGS_PENDING:
        await send_rubika_settings_list(update, context, view=RUBIKA_SETTINGS_VIEW_PENDING)
        raise ApplicationHandlerStop
    if message.text == BTN_SETTINGS_USERS:
        await send_rubika_settings_list(update, context, view=RUBIKA_SETTINGS_VIEW_ALL)
        raise ApplicationHandlerStop
    if message.text == BTN_SETTINGS_ADMINS:
        await send_rubika_settings_list(update, context, view=RUBIKA_SETTINGS_VIEW_ADMINS)
        raise ApplicationHandlerStop
    if message.text == BTN_SETTINGS_BROADCAST:
        access_control, actor_id = await require_current_transport_staff(
            update,
            context,
            owner_only=True,
        )
        if access_control is None or actor_id is None:
            raise ApplicationHandlerStop
        transport_label = get_transport_label(get_transport_name(context))
        set_rubika_broadcast_stage(context, RUBIKA_BROADCAST_STAGE_AWAITING)
        set_current_menu(context, MENU_SETTINGS_BROADCAST)
        await message.reply_text(
            "پیام همگانی را بفرست.\n"
            f"همین متن برای همه کاربران ثبت‌شده {transport_label} ارسال می‌شود.\n"
            "برای لغو «🔙 بازگشت» را بزن.",
            reply_markup=get_current_menu_markup(context),
        )
        raise ApplicationHandlerStop


async def deepseek_settings_message_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Handle DeepSeek token entry from the shared staff settings menu."""
    access_control, actor_id = await require_current_transport_staff(update, context)
    message = update.effective_message
    if access_control is None or actor_id is None or message is None or not message.text:
        return ConversationHandler.END

    if message.text == BTN_BACK:
        await show_rubika_settings_menu(update, context)
        return ConversationHandler.END

    new_tokens = parse_deepseek_token_input(message.text)
    if not new_tokens:
        await message.reply_text(
            "توکن معتبری پیدا نشد.\n"
            "خود token را بفرست یا چند token را با comma یا خط جدید جدا کن.",
            reply_markup=get_current_menu_markup(context),
        )
        return DEEPSEEK_SETTINGS_INPUT

    existing_tokens = list(load_deepseek_chat_auth_tokens())
    merged_tokens = list(existing_tokens)
    added_count = 0
    for token in new_tokens:
        if token not in merged_tokens:
            merged_tokens.append(token)
            added_count += 1

    if added_count == 0:
        set_current_menu(context, MENU_SETTINGS)
        await message.reply_text(
            f"این tokenها قبلا ثبت شده‌اند.\n"
            f"تعداد کل tokenها: {len(existing_tokens)}",
            reply_markup=get_current_menu_markup(context),
        )
        return ConversationHandler.END

    status_message = await message.reply_text(
        "در حال ثبت tokenهای DeepSeek و به‌روزرسانی سرویس...",
        reply_markup=get_current_menu_markup(context),
    )
    save_deepseek_chat_auth_tokens(merged_tokens)
    restart_ok, restart_output = await refresh_deepseek_proxy_service()
    set_current_menu(context, MENU_SETTINGS)

    summary_lines = [
        f"{added_count} token جدید ثبت شد.",
        f"تعداد کل tokenها: {len(merged_tokens)}",
    ]
    if restart_ok:
        summary_lines.append("سرویس محلی DeepSeek هم با تنظیمات جدید refresh شد.")
    else:
        summary_lines.append("tokenها ذخیره شدند، ولی refresh سرویس DeepSeek ناموفق بود.")
        if restart_output:
            summary_lines.append(f"جزئیات: {restart_output[:700]}")

    deepseek_client = context.application.bot_data.get("deepseek_developer_client")
    if deepseek_client is None:
        summary_lines.append(
            "خود backend DeepSeek هنوز در این bot فعال نیست؛ برای فعال شدن، DEEPSEEK_API_BASE_URL هم باید تنظیم باشد."
        )

    await safe_edit_or_reply(
        status_message,
        message,
        "\n".join(summary_lines),
        reply_markup=get_current_menu_markup(context),
    )
    return ConversationHandler.END
    if message.text == BTN_SETTINGS_RESET:
        access_control, actor_id = await require_current_transport_staff(
            update,
            context,
            owner_only=True,
        )
        if access_control is None or actor_id is None:
            raise ApplicationHandlerStop
        transport_label = get_transport_label(get_transport_name(context))
        await message.reply_text(
            f"این کار همه کاربران و ادمین‌های {transport_label} را پاک می‌کند و فقط شما Owner می‌مانید.\n"
            "اگر مطمئن هستی تایید کن.",
            reply_markup=build_rubika_reset_keyboard(),
        )
        raise ApplicationHandlerStop


def get_rubika_broadcast_chat_ids(
    access_control: RubikaAccessControl,
    *,
    exclude_chat_id: str | None = None,
) -> list[str]:
    """Return broadcastable chat ids for all known non-blocked users."""
    targets: list[str] = []
    for record in access_control.list_users():
        status = str(record.get("status") or "")
        if status == RUBIKA_USER_STATUS_BLOCKED:
            continue
        chat_id = str(record.get("chat_id") or "").strip()
        if not chat_id or chat_id == exclude_chat_id or chat_id in targets:
            continue
        targets.append(chat_id)
    return targets


async def rubika_broadcast_message_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle owner-entered broadcast text."""
    if get_current_menu_name(context) != MENU_SETTINGS_BROADCAST:
        return
    if get_rubika_broadcast_stage(context) != RUBIKA_BROADCAST_STAGE_AWAITING:
        return

    message = update.effective_message
    if message is None or not message.text:
        return
    if message.text in {BTN_BACK, BTN_SETTINGS_BROADCAST}:
        return

    access_control, actor_id = await require_current_transport_staff(update, context, owner_only=True)
    if access_control is None or actor_id is None:
        raise ApplicationHandlerStop

    broadcast_text = message.text.strip()
    if not broadcast_text:
        await message.reply_text(
            "پیام خالی است. متن پیام همگانی را بفرست یا «🔙 بازگشت» را بزن.",
            reply_markup=get_current_menu_markup(context),
        )
        raise ApplicationHandlerStop

    actor_record = access_control.get_user(actor_id)
    actor_chat_id = str(actor_record.get("chat_id") or "").strip() if actor_record else None
    target_chat_ids = get_rubika_broadcast_chat_ids(
        access_control,
        exclude_chat_id=actor_chat_id,
    )
    if not target_chat_ids:
        clear_rubika_broadcast_state(context)
        set_current_menu(context, MENU_SETTINGS)
        await message.reply_text(
            "کاربری برای ارسال پیام همگانی پیدا نشد.",
            reply_markup=get_current_menu_markup(context),
        )
        raise ApplicationHandlerStop

    sent_count = 0
    failed_count = 0
    status_message = await message.reply_text(
        f"در حال ارسال پیام همگانی برای {len(target_chat_ids)} کاربر...",
        reply_markup=get_current_menu_markup(context),
    )
    for chat_id in target_chat_ids:
        try:
            await context.bot.send_message(
                chat_id=resolve_transport_target_chat_id(context, chat_id),
                text=broadcast_text,
            )
            sent_count += 1
        except Exception:
            failed_count += 1
            LOGGER.exception(
                "%s broadcast send failed for chat_id=%s",
                get_transport_label(get_transport_name(context)),
                chat_id,
            )

    clear_rubika_broadcast_state(context)
    set_current_menu(context, MENU_SETTINGS)
    await safe_edit_or_reply(
        status_message,
        message,
        (
            "پیام همگانی ارسال شد.\n"
            f"موفق: {sent_count}\n"
            f"ناموفق: {failed_count}"
        ),
        reply_markup=get_current_menu_markup(context),
    )
    raise ApplicationHandlerStop


async def rubika_join_review_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle the guided join-review flow for staff via reply keyboard."""
    access_control, actor_id = await require_current_transport_staff(update, context)
    message = update.effective_message
    if access_control is None or actor_id is None or message is None or not message.text:
        return

    target_user_id = get_rubika_active_review_target(context, actor_id)
    if target_user_id is None:
        set_current_menu(context, MENU_SETTINGS)
        await message.reply_text(
            "درخواست فعالی برای بررسی وجود ندارد.",
            reply_markup=get_current_menu_markup(context),
        )
        return

    stages = get_rubika_review_stages(context)
    stage = stages.get(actor_id, "decision")
    requester = access_control.get_user(target_user_id)
    if requester is None:
        pop_rubika_review_target(context, actor_id)
        await message.reply_text(
            "این درخواست دیگر وجود ندارد.",
            reply_markup=get_current_menu_markup(context),
        )
        return
    current_status = str(requester.get("status") or RUBIKA_USER_STATUS_PENDING)
    if current_status != RUBIKA_USER_STATUS_PENDING:
        pop_rubika_review_target(context, actor_id)
        await message.reply_text(
            f"این درخواست قبلا تعیین‌تکلیف شده است. وضعیت فعلی: {current_status}",
            reply_markup=get_current_menu_markup(context),
        )
        await advance_rubika_join_review(
            update,
            context,
            access_control=access_control,
            staff_user_id=actor_id,
        )
        return
    requester_name = (
        str(requester.get("display_name") or "").strip()
        if requester is not None
        else ""
    ) or "کاربر جدید"
    text = message.text

    if text == BTN_REVIEW_CANCEL:
        set_current_menu(context, MENU_SETTINGS)
        stages.pop(actor_id, None)
        await message.reply_text(
            "بررسی درخواست متوقف شد. از بخش تنظیمات می‌توانی دوباره ادامه بدهی.",
            reply_markup=get_current_menu_markup(context),
        )
        return

    if stage == "decision":
        if text == BTN_REVIEW_REJECT:
            access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_SUSPENDED)
            pop_rubika_review_target(context, actor_id)
            await message.reply_text(
                f"درخواست {requester_name} رد شد.",
                reply_markup=get_current_menu_markup(context),
            )
            try:
                await context.bot.send_message(
                    chat_id=resolve_transport_target_chat_id(
                        context,
                        str(requester.get("chat_id") or target_user_id),
                    ),
                    text="درخواست دسترسی شما فعلا تایید نشد.",
                )
            except Exception:
                LOGGER.debug("Failed to send rejection notice", exc_info=True)
            await advance_rubika_join_review(
                update,
                context,
                access_control=access_control,
                staff_user_id=actor_id,
            )
            return
        if text == BTN_REVIEW_APPROVE:
            stages[actor_id] = "role"
            await message.reply_text(
                f"برای {requester_name} چه سطح دسترسی می‌خواهی؟",
                reply_markup=build_rubika_review_role_menu(
                    is_owner=access_control.is_owner(actor_id),
                ),
            )
            return
        return

    if stage == "role":
        if text == BTN_REVIEW_ROLE_ADMIN:
            if not access_control.is_owner(actor_id):
                await message.reply_text(
                    "فقط Owner می‌تواند دسترسی Admin بدهد.",
                    reply_markup=build_rubika_review_role_menu(is_owner=False),
                )
                return
            access_control.add_admin(target_user_id)
            access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_ALLOWED)
            pop_rubika_review_target(context, actor_id)
            await message.reply_text(
                f"{requester_name} با نقش Admin تایید شد.",
                reply_markup=get_current_menu_markup(context),
            )
            try:
                await context.bot.send_message(
                    chat_id=resolve_transport_target_chat_id(
                        context,
                        str(requester.get("chat_id") or target_user_id),
                    ),
                    text="درخواست شما تایید شد و به‌عنوان Admin ثبت شد.\nدوباره /start را بزن.",
                )
            except Exception:
                LOGGER.debug("Failed to send admin approval notice", exc_info=True)
            await advance_rubika_join_review(
                update,
                context,
                access_control=access_control,
                staff_user_id=actor_id,
            )
            return
        if text == BTN_REVIEW_ROLE_USER:
            access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_ALLOWED)
            pop_rubika_review_target(context, actor_id)
            await message.reply_text(
                f"{requester_name} با نقش کاربر عادی تایید شد.",
                reply_markup=get_current_menu_markup(context),
            )
            try:
                await context.bot.send_message(
                    chat_id=resolve_transport_target_chat_id(
                        context,
                        str(requester.get("chat_id") or target_user_id),
                    ),
                    text="درخواست شما تایید شد.\nدوباره /start را بزن.",
                )
            except Exception:
                LOGGER.debug("Failed to send user approval notice", exc_info=True)
            await advance_rubika_join_review(
                update,
                context,
                access_control=access_control,
                staff_user_id=actor_id,
            )
            return


async def rubika_settings_view_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle inline callbacks for Rubika settings list views."""
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{RUBIKA_SETTINGS_VIEW_CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست نامعتبر است.")
        return
    _prefix, view = payload.split(":", maxsplit=1)
    if view not in {RUBIKA_SETTINGS_VIEW_PENDING, RUBIKA_SETTINGS_VIEW_ALL, RUBIKA_SETTINGS_VIEW_ADMINS}:
        await query.edit_message_text("درخواست نامعتبر است.")
        return
    await send_rubika_settings_list(update, context, view=view, edit_message=query.message)


async def rubika_settings_user_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle inline callbacks for selecting a Rubika user from settings."""
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{RUBIKA_SETTINGS_USER_CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست نامعتبر است.")
        return
    try:
        _prefix, view, user_id = payload.split(":", maxsplit=2)
    except ValueError:
        await query.edit_message_text("درخواست نامعتبر است.")
        return
    await show_rubika_user_actions(
        update,
        context,
        user_id=user_id,
        view=view,
        edit_message=query.message,
    )


async def rubika_settings_action_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle inline callbacks for user-management actions."""
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{RUBIKA_SETTINGS_ACTION_CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست نامعتبر است.")
        return
    try:
        _prefix, view, action, target_user_id = payload.split(":", maxsplit=3)
    except ValueError:
        await query.edit_message_text("درخواست نامعتبر است.")
        return

    access_control, actor_id = await require_current_transport_staff(update, context)
    if access_control is None or actor_id is None:
        return

    if access_control.is_owner(target_user_id) and action != "allow":
        await query.answer("Owner قابل تغییر نیست.", show_alert=True)
        return

    if action == "allow":
        access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_ALLOWED)
    elif action == "suspend":
        if access_control.is_staff(target_user_id):
            await query.answer("برای این کاربر اول دسترسی ادمینی را حذف کن.", show_alert=True)
            return
        access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_SUSPENDED)
    elif action == "block":
        if access_control.is_staff(target_user_id):
            await query.answer("برای این کاربر اول دسترسی ادمینی را حذف کن.", show_alert=True)
            return
        access_control.set_user_status(target_user_id, status=RUBIKA_USER_STATUS_BLOCKED)
    elif action == "admin_add":
        if not access_control.is_owner(actor_id):
            await query.answer("فقط Owner می‌تواند ادمین تعیین کند.", show_alert=True)
            return
        access_control.add_admin(target_user_id)
    elif action == "admin_remove":
        if not access_control.is_owner(actor_id):
            await query.answer("فقط Owner می‌تواند ادمین را حذف کند.", show_alert=True)
            return
        if not access_control.remove_admin(target_user_id):
            await query.answer("این کاربر ادمین نبود.", show_alert=True)
            return
    else:
        await query.edit_message_text("درخواست نامعتبر است.")
        return

    await show_rubika_user_actions(
        update,
        context,
        user_id=target_user_id,
        view=view,
        edit_message=query.message,
    )


async def rubika_settings_reset_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle reset confirmation/cancel callbacks for settings."""
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{RUBIKA_SETTINGS_RESET_CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست نامعتبر است.")
        return
    _prefix, action = payload.split(":", maxsplit=1)
    if action == "cancel":
        await query.edit_message_text("ریست دسترسی‌ها لغو شد.")
        return

    access_control, actor_id = await require_current_transport_staff(update, context, owner_only=True)
    principal_id, chat_id, display_name = resolve_transport_actor_identity(update, context)
    access_actor_id = resolve_transport_access_actor_id(
        access_control,
        user_id=principal_id,
        chat_id=chat_id,
    )
    if access_control is None or actor_id is None or access_actor_id is None or chat_id is None:
        return
    access_control.reset_for_owner(
        owner_id=access_actor_id,
        chat_id=chat_id,
        display_name=display_name,
    )
    get_rubika_review_queues(context).clear()
    get_rubika_review_stages(context).clear()
    set_transport_actor_flags(
        context,
        transport_name=get_transport_name(context),
        actor_id=access_actor_id,
        is_owner=True,
        is_staff=True,
    )
    transport_label = get_transport_label(get_transport_name(context))
    await query.edit_message_text(
        f"سیستم دسترسی {transport_label} ریست شد.\n"
        "همه کاربران و ادمین‌ها پاک شدند و فقط شما Owner ماندی."
    )


async def show_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text: str = START_TEXT,
) -> None:
    """Show the main menu and reset menu state."""
    message = update.effective_message
    if message is None:
        return
    await register_user_if_needed(update, context)
    clear_rubika_broadcast_state(context)
    set_current_menu(context, MENU_MAIN)
    await message.reply_text(text, reply_markup=get_current_menu_markup(context))


async def show_news_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the news submenu."""
    message = update.effective_message
    if message is None:
        return
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_NEWS)
    await message.reply_text(
        "📰 بخش اخبار\nمنبع خبری را انتخاب کن یا از AI درباره خبرها بپرس.",
        reply_markup=build_news_menu(),
    )


async def show_youtube_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the YouTube submenu."""
    message = update.effective_message
    if message is None:
        return
    await register_user_if_needed(update, context)
    clear_active_youtube_browser_state(context)
    set_current_menu(context, MENU_YOUTUBE)
    snapshot = get_youtube_library_snapshot(update, context)
    await message.reply_text(
        "📺 بخش یوتیوب\n"
        "گزینه موردنظرت را انتخاب کن.\n"
        f"{summarize_youtube_library_counts(snapshot)}",
        reply_markup=build_youtube_menu(),
    )


async def show_file_tools_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the file tools submenu."""
    message = update.effective_message
    if message is None:
        return
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_FILE_TOOLS)
    await message.reply_text(
        "🛠 بخش ابزار فایل\nابزار موردنظرت را انتخاب کن.",
        reply_markup=build_file_tools_menu_for_context(context),
    )


async def show_general_ai_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the general AI submenu."""
    message = update.effective_message
    if message is None:
        return
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_GENERAL_AI)
    description = (
        "🌐 بخش Web+Ai\n"
        "چت Gemini، Google Search و Google Images از اینجا در دسترس است."
    )
    await message.reply_text(
        description,
        reply_markup=get_current_menu_markup(context),
    )


async def news_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the news section from the slash-command menu."""
    await register_user_if_needed(update, context)
    await show_news_menu(update, context)


async def youtube_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the YouTube section from the slash-command menu."""
    await register_user_if_needed(update, context)
    await show_youtube_menu(update, context)


async def file_tools_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the file tools section from the slash-command menu."""
    await register_user_if_needed(update, context)
    await show_file_tools_menu(update, context)


async def vpn_config_bundle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Collect recent VPN configs from Telegram sources and send a passworded bundle."""
    await register_user_if_needed(update, context)
    message = update.effective_message
    if message is None:
        return

    transport_name = get_transport_name(context)
    collector: TelegramVpnConfigCollector = context.application.bot_data["vpn_config_collector"]
    status_message = await message.reply_text(
        "در حال کادو پیچ کردن بسته ... 🎁",
        reply_markup=get_current_menu_markup(context),
    )
    bundle = None
    try:
        bundle = await collector.collect_bundle()
        total_items = (
            bundle.slipnet_count
            + bundle.proxy_count
            + bundle.vless_count
            + bundle.npvt_count
        )
        if total_items == 0:
            await safe_edit_or_reply(
                status_message,
                message,
                "در ۴۸ ساعت اخیر هدیه تازه‌ای پیدا نشد.",
                reply_markup=get_current_menu_markup(context),
            )
            return

        if transport_name == "telegram":
            await safe_edit_or_reply(
                status_message,
                message,
                (
                    "بسته آماده شد و در حال ارسال است...\n"
                    f"Slipnet: {bundle.slipnet_count}\n"
                    f"Proxy: {bundle.proxy_count}\n"
                    f"VLESS: {bundle.vless_count}\n"
                    f".npvt: {bundle.npvt_count}"
                ),
                reply_markup=get_current_menu_markup(context),
            )

        upload_bot = get_available_upload_bot(context)
        caption = (
            f"{total_items} تا هدیه ارسال شد 🚚"
            if transport_name == "rubika"
            else (
                "📦 بسته‌ی حمایتی\n"
                f"Slipnet: {bundle.slipnet_count}\n"
                f"Proxy: {bundle.proxy_count}\n"
                f"VLESS: {bundle.vless_count}\n"
                f".npvt: {bundle.npvt_count}"
            )
        )
        with bundle.archive_path.open("rb") as archive_file:
            await upload_bot.send_document(
                chat_id=message.chat_id,
                document=archive_file,
                filename=bundle.archive_path.name,
                caption=caption,
            )
    except TelegramVpnConfigAuthorizationError as exc:
        await safe_edit_or_reply(
            status_message,
            message,
            f"دسترسی Telethon آماده نیست.\n{exc}",
            reply_markup=get_current_menu_markup(context),
        )
        return
    except Exception as exc:
        LOGGER.exception("VPN config bundle collection failed")
        await safe_edit_or_reply(
            status_message,
            message,
            f"خطا در آماده‌سازی بسته: {exc}",
            reply_markup=get_current_menu_markup(context),
        )
        return
    finally:
        if bundle is not None:
            cleanup_directory(bundle.staging_dir)
            try:
                bundle.archive_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.debug("Failed to remove VPN bundle archive %s", bundle.archive_path, exc_info=True)


async def coming_soon_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    feature_name: str,
) -> None:
    """Reply with a consistent coming-soon message for placeholder features."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        f"«{feature_name}» به‌زودی اضافه می‌شود.",
        reply_markup=get_current_menu_markup(context),
    )


async def audio_compressor_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the audio compression flow."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_FILE_TOOLS)
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    await message.reply_text(
        "فایل صوتی‌ات را بفرست.\nفرمت‌های پشتیبانی‌شده: `.m4a` و `.mp3`",
        parse_mode="Markdown",
        reply_markup=get_current_menu_markup(context),
    )
    return AUDIO_COMPRESS


async def audio_compressor_back_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Return from audio compression to the file tools menu."""
    await show_file_tools_menu(update, context)
    return ConversationHandler.END


async def audio_compressor_file_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive, compress, and return an audio file."""
    message = update.effective_message
    if message is None:
        return AUDIO_COMPRESS

    media = message.audio or message.document
    if media is None:
        await message.reply_text(
            "فایل صوتی معتبر پیدا نشد. لطفاً یک فایل `.m4a` یا `.mp3` بفرست.",
            parse_mode="Markdown",
            reply_markup=get_current_menu_markup(context),
        )
        return AUDIO_COMPRESS

    file_name = getattr(media, "file_name", None)
    if not file_name:
        guessed_suffix = ".mp3"
        mime_type = getattr(media, "mime_type", "") or ""
        if "mp4" in mime_type or "m4a" in mime_type:
            guessed_suffix = ".m4a"
        file_name = f"{getattr(media, 'file_unique_id', 'audio_file')}{guessed_suffix}"

    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_AUDIO_SUFFIXES:
        await message.reply_text(
            "فرمت فایل پشتیبانی نمی‌شود. فقط `.m4a` و `.mp3` قابل قبول هستند.",
            parse_mode="Markdown",
            reply_markup=get_current_menu_markup(context),
        )
        return AUDIO_COMPRESS

    temp_dir = DOWNLOADS_DIR / f"audio-compressor-{uuid.uuid4().hex}"
    original_stem = Path(file_name).stem.strip() or getattr(media, "file_unique_id", "audio_file")
    input_path = temp_dir / f"{original_stem}{suffix}"
    process_messages: list = []
    await send_process_status(
        message,
        "در حال دریافت فایل... 10%",
        reply_markup=get_current_menu_markup(context),
        process_messages=process_messages,
    )

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        await download_media_to_local_path(
            context,
            media,
            suffix=suffix,
            destination=input_path,
            received_at=message.date,
            chat_id=message.chat_id,
            message_id=message.id,
        )

        await send_process_status(
            message,
            "فایل دریافت شد.\nدر حال فشرده‌سازی... 65%",
            reply_markup=get_current_menu_markup(context),
            process_messages=process_messages,
        )

        result = await asyncio.to_thread(compress_audio_to_mp3, input_path, temp_dir)

        output_paths = [result.output_path]
        total_output_size_bytes = result.compressed_size_bytes
        if result.compressed_size_bytes > MAX_AUDIO_PART_BYTES:
            await send_process_status(
                message,
                "در حال تقسیم فایل فشرده به بخش‌های زیر 15.99MB... 80%",
                reply_markup=get_current_menu_markup(context),
                process_messages=process_messages,
            )
            output_paths = await asyncio.to_thread(
                split_audio_for_telegram,
                result.output_path,
                temp_dir,
            )
            total_output_size_bytes = sum(path.stat().st_size for path in output_paths)

        await send_process_status(
            message,
            "در حال ارسال فایل فشرده... 90%",
            reply_markup=get_current_menu_markup(context),
            process_messages=process_messages,
        )

        upload_bot = get_available_upload_bot(context)
        total_parts = len(output_paths)
        reduction_percent = (
            max(
                0.0,
                ((result.original_size_bytes - total_output_size_bytes) / result.original_size_bytes)
                * 100,
            )
            if result.original_size_bytes
            else 0.0
        )
        for index, output_path in enumerate(output_paths, start=1):
            caption_parts = [
                "فشرده‌سازی کامل شد" if index == 1 else f"بخش {index} از {total_parts}",
            ]
            if index == 1:
                caption_parts.extend(
                    [
                        f"حجم اولیه: {format_file_size_mb(result.original_size_bytes)}",
                        f"حجم نهایی: {format_file_size_mb(total_output_size_bytes)}",
                        f"کاهش حجم: {reduction_percent:.1f}٪",
                    ]
                )
                if total_parts > 1:
                    caption_parts.append(f"تعداد بخش‌ها: {total_parts}")

            with output_path.open("rb") as compressed_audio:
                await upload_bot.send_audio(
                    chat_id=message.chat_id,
                    audio=compressed_audio,
                    filename=output_path.name,
                    caption="\n".join(caption_parts),
                    title=output_path.stem,
                )
        await delete_process_messages(process_messages)
        await show_file_tools_menu(update, context)
        return ConversationHandler.END
    except RuntimeError as exc:
        await delete_process_messages(process_messages)
        await message.reply_text(
            f"خطا در فشرده‌سازی فایل: {exc}",
            reply_markup=get_current_menu_markup(context),
        )
    except Exception as exc:
        LOGGER.exception("Audio compression failed")
        await delete_process_messages(process_messages)
        await message.reply_text(
            f"خطای غیرمنتظره در پردازش فایل: {exc}",
            reply_markup=get_current_menu_markup(context),
        )
    finally:
        cleanup_directory(temp_dir)

    return AUDIO_COMPRESS


async def video_compressor_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the video compression flow."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_FILE_TOOLS)
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    await message.reply_text(
        "کدام موتور فشرده‌سازی را می‌خواهی؟\n"
        "🧠 CPU: کیفیت بهتر\n"
        "⚡ GPU: سریع‌تر\n"
        "🚀 Parallel: آزمایشی / سریع‌ترین",
        reply_markup=build_video_method_menu(),
    )
    return VIDEO_COMPRESS_METHOD


async def video_compressor_method_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the selected video compression method and ask for the file."""
    message = update.effective_message
    if message is None or not message.text:
        return VIDEO_COMPRESS_METHOD

    method_map = {
        BTN_VIDEO_METHOD_CPU: VIDEO_METHOD_CPU,
        BTN_VIDEO_METHOD_GPU: VIDEO_METHOD_GPU,
        BTN_VIDEO_METHOD_PARALLEL: VIDEO_METHOD_PARALLEL,
    }
    method = method_map.get(message.text)
    if method is None:
        await message.reply_text(
            "یکی از روش‌های CPU، GPU یا Parallel را انتخاب کن.",
            reply_markup=build_video_method_menu(),
        )
        return VIDEO_COMPRESS_METHOD

    context.user_data["video_compress_method"] = method
    max_input_bytes = int(
        context.application.bot_data.get("video_compress_max_input_bytes", 200 * 1024 * 1024)
    )
    await message.reply_text(
        "ویدیویت را بفرست.\n"
        "فرمت‌های پشتیبانی‌شده: `.mp4`، `.mov`، `.mkv`، `.avi` و `.m4v`\n"
        f"حداکثر حجم مجاز: {format_file_size_mb(max_input_bytes)}",
        parse_mode="Markdown",
        reply_markup=build_video_method_menu(),
    )
    return VIDEO_COMPRESS_FILE


async def video_compressor_back_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Return from video compression to the file tools menu."""
    await show_file_tools_menu(update, context)
    return ConversationHandler.END


async def video_compressor_file_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive, compress, and return a video file."""
    message = update.effective_message
    if message is None:
        return VIDEO_COMPRESS_FILE

    media = message.video or message.document
    if media is None:
        await message.reply_text(
            "فایل ویدیویی معتبر پیدا نشد. لطفاً یک ویدیو بفرست.",
            reply_markup=build_video_method_menu(),
        )
        return VIDEO_COMPRESS_FILE

    file_name = getattr(media, "file_name", None)
    if not file_name:
        guessed_suffix = ".mp4"
        mime_type = getattr(media, "mime_type", "") or ""
        if "quicktime" in mime_type:
            guessed_suffix = ".mov"
        elif "x-matroska" in mime_type:
            guessed_suffix = ".mkv"
        file_name = f"{getattr(media, 'file_unique_id', 'video_file')}{guessed_suffix}"

    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_VIDEO_SUFFIXES:
        await message.reply_text(
            "فرمت ویدیو پشتیبانی نمی‌شود. فقط `.mp4`، `.mov`، `.mkv`، `.avi` و `.m4v` قابل قبول هستند.",
            parse_mode="Markdown",
            reply_markup=build_video_method_menu(),
        )
        return VIDEO_COMPRESS_FILE

    file_size = getattr(media, "file_size", 0) or 0
    max_input_bytes = int(
        context.application.bot_data.get("video_compress_max_input_bytes", 200 * 1024 * 1024)
    )
    if file_size > max_input_bytes:
        await message.reply_text(
            f"حجم ویدیو بیشتر از حد مجاز است.\nحداکثر مجاز: {format_file_size_mb(max_input_bytes)}",
            reply_markup=build_video_method_menu(),
        )
        return VIDEO_COMPRESS_FILE

    temp_dir = DOWNLOADS_DIR / f"video-compressor-{uuid.uuid4().hex}"
    original_stem = Path(file_name).stem.strip() or getattr(media, "file_unique_id", "video_file")
    input_path = temp_dir / f"{original_stem}{suffix}"
    process_messages: list = []
    await send_process_status(
        message,
        "در حال دریافت ویدیو... 10%",
        reply_markup=build_video_method_menu(),
        process_messages=process_messages,
    )

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        await download_media_to_local_path(
            context,
            media,
            suffix=suffix,
            destination=input_path,
            received_at=message.date,
            chat_id=message.chat_id,
            message_id=message.id,
        )

        await send_process_status(
            message,
            "ویدیو دریافت شد.\nدر حال فشرده‌سازی... 60%",
            reply_markup=build_video_method_menu(),
            process_messages=process_messages,
        )

        method = str(context.user_data.get("video_compress_method", VIDEO_METHOD_CPU))
        artifact = await asyncio.to_thread(
            compress_video_artifact,
            input_path,
            temp_dir,
            method=method,
        )

        await send_process_status(
            message,
            "در حال تقسیم ویدیو به بخش‌های زیر 15.99MB... 80%",
            reply_markup=build_video_method_menu(),
            process_messages=process_messages,
        )

        output_paths = await asyncio.to_thread(split_final, artifact.output_path, temp_dir)
        total_output_size_bytes = sum(path.stat().st_size for path in output_paths)

        await send_process_status(
            message,
            "در حال ارسال بخش‌های ویدیوی فشرده... 90%",
            reply_markup=build_video_method_menu(),
            process_messages=process_messages,
        )

        upload_bot = get_available_upload_bot(context)
        total_parts = len(output_paths)
        for index, output_path in enumerate(output_paths, start=1):
            caption_parts = [
                "فشرده‌سازی ویدیو کامل شد" if index == 1 else f"بخش {index} از {total_parts}",
            ]
            if index == 1:
                engine_title = (
                    "GPU"
                    if artifact.engine_used == VIDEO_METHOD_GPU
                    else "Parallel"
                    if artifact.engine_used == VIDEO_METHOD_PARALLEL
                    else "CPU"
                )
                caption_parts.extend(
                    [
                        f"موتور: {engine_title}",
                        f"حجم اولیه: {format_file_size_mb(file_size)}",
                        f"حجم نهایی: {format_file_size_mb(total_output_size_bytes)}",
                        f"کاهش حجم: {max(0.0, ((file_size - total_output_size_bytes) / file_size) * 100) if file_size else 0.0:.1f}٪",
                    ]
                )
                if total_parts > 1:
                    caption_parts.append(f"تعداد بخش‌ها: {total_parts}")

            with output_path.open("rb") as compressed_video:
                await upload_bot.send_video(
                    chat_id=message.chat_id,
                    video=compressed_video,
                    filename=output_path.name,
                    caption="\n".join(caption_parts),
                    supports_streaming=True,
                )
        await delete_process_messages(process_messages)
        await show_file_tools_menu(update, context)
        return ConversationHandler.END
    except RuntimeError as exc:
        await delete_process_messages(process_messages)
        await message.reply_text(
            f"خطا در فشرده‌سازی ویدیو: {exc}",
            reply_markup=build_video_method_menu(),
        )
    except Exception as exc:
        LOGGER.exception("Video compression failed")
        await delete_process_messages(process_messages)
        await message.reply_text(
            f"خطای غیرمنتظره در پردازش ویدیو: {exc}",
            reply_markup=build_video_method_menu(),
        )
    finally:
        cleanup_directory(temp_dir)

    return VIDEO_COMPRESS_FILE


async def video_compressor_invalid_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Prompt the user to choose a valid video compression method."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "یکی از روش‌های CPU، GPU یا Parallel را انتخاب کن، یا «🔙 بازگشت» را بزن.",
            reply_markup=build_video_method_menu(),
        )
    return VIDEO_COMPRESS_METHOD


async def video_compressor_invalid_file_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Prompt the user to send a supported video while the compressor is active."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "اول روش فشرده‌سازی را انتخاب کن یا ویدیوت را بفرست، یا «🔙 بازگشت» را بزن.",
            reply_markup=build_video_method_menu(),
        )
    return VIDEO_COMPRESS_FILE


async def audio_compressor_invalid_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Prompt the user to send a supported audio file while the compressor is active."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "لطفاً یک فایل صوتی `.m4a` یا `.mp3` بفرست، یا «🔙 بازگشت» را بزن.",
            parse_mode="Markdown",
            reply_markup=get_current_menu_markup(context),
        )
    return AUDIO_COMPRESS


async def pdf_compressor_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the PDF compression flow."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_FILE_TOOLS)
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    await message.reply_text(
        "شدت فشرده‌سازی PDF را انتخاب کن:\n"
        "🟢 کم: کیفیت بهتر\n"
        "🟡 متوسط: متعادل\n"
        "🔴 زیاد: حجم کمتر / افت کیفیت بیشتر",
        reply_markup=build_pdf_compression_menu(),
    )
    return PDF_COMPRESS_LEVEL


async def pdf_compressor_back_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Return from PDF compression to the file tools menu."""
    context.user_data.pop("pdf_compress_level", None)
    await show_file_tools_menu(update, context)
    return ConversationHandler.END


async def pdf_compressor_level_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the selected PDF compression level and ask for the PDF file."""
    message = update.effective_message
    if message is None or not message.text:
        return PDF_COMPRESS_LEVEL

    level_map = {
        BTN_PDF_COMPRESS_LOW: PDF_COMPRESSION_LEVEL_LOW,
        BTN_PDF_COMPRESS_MEDIUM: PDF_COMPRESSION_LEVEL_MEDIUM,
        BTN_PDF_COMPRESS_HIGH: PDF_COMPRESSION_LEVEL_HIGH,
    }
    level = level_map.get(message.text)
    if level is None:
        await message.reply_text(
            "یکی از گزینه‌های کم، متوسط یا زیاد را انتخاب کن.",
            reply_markup=build_pdf_compression_menu(),
        )
        return PDF_COMPRESS_LEVEL

    context.user_data["pdf_compress_level"] = level
    await message.reply_text(
        "فایل PDF را بفرست.\n"
        f"حداکثر حجم مجاز: {format_file_size_mb(get_file_tool_max_input_bytes(context))}",
        reply_markup=build_pdf_compression_menu(),
    )
    return PDF_COMPRESS


async def pdf_compressor_file_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive, compress, and return a PDF file."""
    message = update.effective_message
    if message is None:
        return PDF_COMPRESS

    media = message.document
    if media is None:
        await message.reply_text(
            "فایل PDF معتبر پیدا نشد. لطفاً یک PDF بفرست.",
            reply_markup=get_current_menu_markup(context),
        )
        return PDF_COMPRESS

    file_name = getattr(media, "file_name", None) or f"{getattr(media, 'file_unique_id', 'pdf_file')}.pdf"
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_PDF_SUFFIXES:
        await message.reply_text(
            "فرمت فایل پشتیبانی نمی‌شود. فقط `.pdf` قابل قبول است.",
            parse_mode="Markdown",
            reply_markup=get_current_menu_markup(context),
        )
        return PDF_COMPRESS

    file_size = getattr(media, "file_size", 0) or 0
    max_input_bytes = get_file_tool_max_input_bytes(context)
    if file_size > max_input_bytes:
        await message.reply_text(
            f"حجم فایل بیشتر از حد مجاز است.\nحداکثر مجاز: {format_file_size_mb(max_input_bytes)}",
            reply_markup=get_current_menu_markup(context),
        )
        return PDF_COMPRESS

    temp_dir = DOWNLOADS_DIR / f"pdf-compressor-{uuid.uuid4().hex}"
    original_stem = Path(file_name).stem.strip() or getattr(media, "file_unique_id", "pdf_file")
    input_path = temp_dir / f"{original_stem}.pdf"
    process_messages: list = []
    await send_process_status(
        message,
        "در حال دریافت PDF... 10%",
        reply_markup=build_pdf_compression_menu(),
        process_messages=process_messages,
    )

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        await download_media_to_local_path(
            context,
            media,
            suffix=".pdf",
            destination=input_path,
            received_at=message.date,
            chat_id=message.chat_id,
            message_id=message.id,
        )

        await send_process_status(
            message,
            "PDF دریافت شد.\nدر حال فشرده‌سازی... 65%",
            reply_markup=build_pdf_compression_menu(),
            process_messages=process_messages,
        )

        level = str(context.user_data.get("pdf_compress_level", PDF_COMPRESSION_LEVEL_MEDIUM))
        result = await asyncio.to_thread(compress_pdf, input_path, temp_dir, level=level)

        await send_process_status(
            message,
            "در حال ارسال PDF فشرده... 90%",
            reply_markup=build_pdf_compression_menu(),
            process_messages=process_messages,
        )

        level_title = (
            "کم"
            if level == PDF_COMPRESSION_LEVEL_LOW
            else "زیاد"
            if level == PDF_COMPRESSION_LEVEL_HIGH
            else "متوسط"
        )
        caption = (
            "فشرده‌سازی PDF کامل شد\n"
            f"شدت فشرده‌سازی: {level_title}\n"
            f"حجم اولیه: {format_file_size_mb(result.original_size_bytes)}\n"
            f"حجم نهایی: {format_file_size_mb(result.output_size_bytes)}\n"
            f"کاهش حجم: {result.reduction_percent:.1f}٪"
        )
        upload_bot = get_available_upload_bot(context)
        with result.output_path.open("rb") as compressed_pdf:
            await upload_bot.send_document(
                chat_id=message.chat_id,
                document=compressed_pdf,
                filename=result.output_path.name,
                caption=caption,
            )
        await delete_process_messages(process_messages)
        context.user_data.pop("pdf_compress_level", None)
        await show_file_tools_menu(update, context)
        return ConversationHandler.END
    except RuntimeError as exc:
        await delete_process_messages(process_messages)
        await message.reply_text(
            f"خطا در فشرده‌سازی PDF: {exc}",
            reply_markup=build_pdf_compression_menu(),
        )
    except Exception as exc:
        LOGGER.exception("PDF compression failed")
        await delete_process_messages(process_messages)
        await message.reply_text(
            f"خطای غیرمنتظره در پردازش PDF: {exc}",
            reply_markup=build_pdf_compression_menu(),
        )
    finally:
        cleanup_directory(temp_dir)

    return PDF_COMPRESS


async def pdf_splitter_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the PDF slicing flow."""
    await register_user_if_needed(update, context)
    cleanup_pdf_split_state(context)
    set_current_menu(context, MENU_FILE_TOOLS)
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    await message.reply_text(
        "PDF را بفرست تا بعد بازه صفحه را انتخاب کنی.\n"
        f"حداکثر حجم مجاز: {format_file_size_mb(get_file_tool_max_input_bytes(context))}",
        reply_markup=get_current_menu_markup(context),
    )
    return PDF_SPLIT_FILE


async def pdf_splitter_back_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Return from the PDF slicing flow to the file tools menu."""
    cleanup_pdf_split_state(context)
    await show_file_tools_menu(update, context)
    return ConversationHandler.END


async def pdf_splitter_file_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive the PDF to be sliced and then ask for the page range."""
    message = update.effective_message
    if message is None:
        return PDF_SPLIT_FILE

    media = message.document
    if media is None:
        await message.reply_text(
            "فایل PDF معتبر پیدا نشد. لطفاً یک PDF بفرست.",
            reply_markup=get_current_menu_markup(context),
        )
        return PDF_SPLIT_FILE

    file_name = getattr(media, "file_name", None) or f"{getattr(media, 'file_unique_id', 'pdf_file')}.pdf"
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_PDF_SUFFIXES:
        await message.reply_text(
            "فرمت فایل پشتیبانی نمی‌شود. فقط `.pdf` قابل قبول است.",
            parse_mode="Markdown",
            reply_markup=get_current_menu_markup(context),
        )
        return PDF_SPLIT_FILE

    file_size = getattr(media, "file_size", 0) or 0
    max_input_bytes = get_file_tool_max_input_bytes(context)
    if file_size > max_input_bytes:
        await message.reply_text(
            f"حجم فایل بیشتر از حد مجاز است.\nحداکثر مجاز: {format_file_size_mb(max_input_bytes)}",
            reply_markup=get_current_menu_markup(context),
        )
        return PDF_SPLIT_FILE

    cleanup_pdf_split_state(context)
    temp_dir = DOWNLOADS_DIR / f"pdf-splitter-{uuid.uuid4().hex}"
    input_path = temp_dir / file_name
    process_messages: list = []
    await send_process_status(
        message,
        "در حال دریافت PDF... 10%",
        reply_markup=build_back_only_menu("PDF را بفرست یا بازگشت را بزن..."),
        process_messages=process_messages,
    )

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        await download_media_to_local_path(
            context,
            media,
            suffix=".pdf",
            destination=input_path,
            received_at=message.date,
            chat_id=message.chat_id,
            message_id=message.id,
        )

        await send_process_status(
            message,
            "در حال بررسی تعداد صفحات PDF... 50%",
            reply_markup=build_back_only_menu("بازه صفحه را وارد کن..."),
            process_messages=process_messages,
        )

        page_count = await asyncio.to_thread(get_pdf_page_count, input_path)
        await delete_process_messages(process_messages)

        context.user_data["pdf_split_temp_dir"] = temp_dir
        context.user_data["pdf_split_input_path"] = input_path
        context.user_data["pdf_split_page_count"] = page_count
        context.user_data["pdf_split_file_name"] = file_name

        await message.reply_text(
            f"PDF دریافت شد و {page_count} صفحه دارد.\n"
            "بازه صفحه را به این شکل بفرست: `3-7`",
            parse_mode="Markdown",
            reply_markup=build_back_only_menu("بازه صفحه را مثل 3-7 بفرست..."),
        )
        return PDF_SPLIT_RANGE
    except RuntimeError as exc:
        await delete_process_messages(process_messages)
        cleanup_pdf_split_state(context)
        await message.reply_text(
            f"خطا در آماده‌سازی PDF: {exc}",
            reply_markup=get_current_menu_markup(context),
        )
    except Exception as exc:
        LOGGER.exception("PDF slicing preparation failed")
        await delete_process_messages(process_messages)
        cleanup_pdf_split_state(context)
        await message.reply_text(
            f"خطای غیرمنتظره در آماده‌سازی PDF: {exc}",
            reply_markup=get_current_menu_markup(context),
        )
    return PDF_SPLIT_FILE


async def pdf_splitter_range_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Slice the uploaded PDF using the requested page range."""
    message = update.effective_message
    if message is None or not message.text:
        return PDF_SPLIT_RANGE

    input_path: Path | None = context.user_data.get("pdf_split_input_path")
    page_count = int(context.user_data.get("pdf_split_page_count", 0))
    temp_dir: Path | None = context.user_data.get("pdf_split_temp_dir")
    if input_path is None or temp_dir is None or page_count <= 0:
        await message.reply_text(
            "اول PDF را بفرست.",
            reply_markup=get_current_menu_markup(context),
        )
        return PDF_SPLIT_FILE

    try:
        start_page, end_page = parse_pdf_page_range(message.text, page_count)
    except ValueError:
        await message.reply_text(
            f"بازه نامعتبر است. این PDF {page_count} صفحه دارد.\nبازه را مثل `3-7` بفرست.",
            parse_mode="Markdown",
            reply_markup=build_back_only_menu("بازه صفحه را مثل 3-7 بفرست..."),
        )
        return PDF_SPLIT_RANGE

    process_messages: list = []
    await send_process_status(
        message,
        "در حال برش PDF... 70%",
        reply_markup=build_back_only_menu("در حال برش PDF..."),
        process_messages=process_messages,
    )

    try:
        result = await asyncio.to_thread(
            slice_pdf,
            input_path,
            temp_dir,
            start_page=start_page,
            end_page=end_page,
        )

        await send_process_status(
            message,
            "در حال ارسال PDF برش‌خورده... 90%",
            reply_markup=build_back_only_menu("در حال ارسال PDF..."),
            process_messages=process_messages,
        )

        caption = (
            "برش PDF کامل شد\n"
            f"صفحات: {start_page} تا {end_page}\n"
            f"حجم خروجی: {format_file_size_mb(result.output_size_bytes)}"
        )
        upload_bot = get_available_upload_bot(context)
        with result.output_path.open("rb") as sliced_pdf:
            await upload_bot.send_document(
                chat_id=message.chat_id,
                document=sliced_pdf,
                filename=result.output_path.name,
                caption=caption,
            )
        await delete_process_messages(process_messages)
        cleanup_pdf_split_state(context)
        await show_file_tools_menu(update, context)
        return ConversationHandler.END
    except Exception as exc:
        LOGGER.exception("PDF slicing failed")
        await delete_process_messages(process_messages)
        await message.reply_text(
            f"خطا در برش PDF: {exc}",
            reply_markup=build_back_only_menu("بازه صفحه را بفرست یا بازگشت را بزن..."),
        )
        return PDF_SPLIT_RANGE


async def pdf_merger_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the PDF merge flow."""
    await register_user_if_needed(update, context)
    init_pdf_merge_state(context)
    set_current_menu(context, MENU_FILE_TOOLS)
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    await message.reply_text(
        "فایل‌های PDF را یکی‌یکی بفرست.\n"
        "بعد از هر فایل، لیست صف ادغام را می‌بینی.\n"
        "هر وقت آماده بودی، دکمه «✅ شروع ادغام» را بزن.\n\n"
        f"{format_pdf_merge_queue([])}",
        reply_markup=build_pdf_merge_menu(),
    )
    return PDF_MERGE


async def pdf_merger_back_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Return from the PDF merge flow to the file tools menu."""
    cleanup_pdf_merge_state(context)
    await show_file_tools_menu(update, context)
    return ConversationHandler.END


async def pdf_merger_file_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Queue a PDF file for merging."""
    message = update.effective_message
    if message is None:
        return PDF_MERGE

    media = message.document
    if media is None:
        await message.reply_text(
            "فایل PDF معتبر پیدا نشد. لطفاً یک PDF بفرست.",
            reply_markup=build_pdf_merge_menu(),
        )
        return PDF_MERGE

    file_name = getattr(media, "file_name", None) or f"{getattr(media, 'file_unique_id', 'pdf_file')}.pdf"
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_PDF_SUFFIXES:
        await message.reply_text(
            "فرمت فایل پشتیبانی نمی‌شود. فقط `.pdf` قابل قبول است.",
            parse_mode="Markdown",
            reply_markup=build_pdf_merge_menu(),
        )
        return PDF_MERGE

    file_size = getattr(media, "file_size", 0) or 0
    max_input_bytes = get_file_tool_max_input_bytes(context)
    if file_size > max_input_bytes:
        await message.reply_text(
            f"حجم فایل بیشتر از حد مجاز است.\nحداکثر مجاز: {format_file_size_mb(max_input_bytes)}",
            reply_markup=build_pdf_merge_menu(),
        )
        return PDF_MERGE

    temp_dir: Path = context.user_data.get("pdf_merge_temp_dir") or init_pdf_merge_state(context)
    queued_paths: list[Path] = context.user_data.setdefault("pdf_merge_input_paths", [])
    queued_names: list[str] = context.user_data.setdefault("pdf_merge_names", [])
    input_path = temp_dir / f"{len(queued_paths) + 1:02d}_{Path(file_name).name}"

    process_messages: list = []
    await send_process_status(
        message,
        "در حال دریافت PDF... 20%",
        reply_markup=build_pdf_merge_menu(),
        process_messages=process_messages,
    )

    try:
        await download_media_to_local_path(
            context,
            media,
            suffix=".pdf",
            destination=input_path,
            received_at=message.date,
            chat_id=message.chat_id,
            message_id=message.id,
        )
        queued_paths.append(input_path)
        queued_names.append(file_name)
        await delete_process_messages(process_messages)
        await message.reply_text(
            "PDF اضافه شد.\n"
            f"{format_pdf_merge_queue(queued_names)}\n\n"
            "اگر آماده‌ای، «✅ شروع ادغام» را بزن. اگر نه، فایل بعدی را بفرست.",
            reply_markup=build_pdf_merge_menu(),
        )
    except Exception as exc:
        await delete_process_messages(process_messages)
        cleanup_file(input_path)
        await message.reply_text(
            f"خطا در دریافت PDF: {exc}",
            reply_markup=build_pdf_merge_menu(),
        )
    return PDF_MERGE


async def pdf_merger_run_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Merge the queued PDF files and return the result."""
    message = update.effective_message
    if message is None:
        return PDF_MERGE

    input_paths: list[Path] = context.user_data.get("pdf_merge_input_paths", [])
    if not input_paths:
        await message.reply_text(
            "هنوز هیچ PDFی برای ادغام اضافه نشده است.",
            reply_markup=build_pdf_merge_menu(),
        )
        return PDF_MERGE

    temp_dir: Path = context.user_data.get("pdf_merge_temp_dir") or init_pdf_merge_state(context)
    process_messages: list = []
    await send_process_status(
        message,
        "در حال ادغام PDFها... 70%",
        reply_markup=build_pdf_merge_menu(),
        process_messages=process_messages,
    )

    try:
        result = await asyncio.to_thread(merge_pdfs, input_paths, temp_dir)

        await send_process_status(
            message,
            "در حال ارسال PDF ادغام‌شده... 90%",
            reply_markup=build_pdf_merge_menu(),
            process_messages=process_messages,
        )

        caption = (
            "ادغام PDF کامل شد\n"
            f"تعداد فایل‌ها: {len(input_paths)}\n"
            f"حجم خروجی: {format_file_size_mb(result.output_size_bytes)}"
        )
        upload_bot = get_available_upload_bot(context)
        with result.output_path.open("rb") as merged_pdf:
            await upload_bot.send_document(
                chat_id=message.chat_id,
                document=merged_pdf,
                filename=result.output_path.name,
                caption=caption,
            )
        await delete_process_messages(process_messages)
        cleanup_pdf_merge_state(context)
        await show_file_tools_menu(update, context)
        return ConversationHandler.END
    except Exception as exc:
        LOGGER.exception("PDF merge failed")
        await delete_process_messages(process_messages)
        await message.reply_text(
            f"خطا در ادغام PDFها: {exc}",
            reply_markup=build_pdf_merge_menu(),
        )
    return PDF_MERGE


async def pdf_compressor_invalid_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Prompt the user to send a PDF while compression is active."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "لطفاً یک PDF بفرست، یا «🔙 بازگشت» را بزن.",
            reply_markup=build_pdf_compression_menu(),
        )
    return PDF_COMPRESS


async def pdf_compressor_invalid_level_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Prompt the user to choose a valid PDF compression level."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "یکی از گزینه‌های کم، متوسط یا زیاد را انتخاب کن، یا «🔙 بازگشت» را بزن.",
            reply_markup=build_pdf_compression_menu(),
        )
    return PDF_COMPRESS_LEVEL


async def pdf_splitter_invalid_file_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Prompt the user to send a PDF while the slicer waits for a file."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "لطفاً یک PDF بفرست، یا «🔙 بازگشت» را بزن.",
            reply_markup=get_current_menu_markup(context),
        )
    return PDF_SPLIT_FILE


async def pdf_splitter_invalid_range_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Prompt the user to enter a valid PDF page range."""
    message = update.effective_message
    page_count = int(context.user_data.get("pdf_split_page_count", 0))
    if message is not None:
        suffix = f" این PDF {page_count} صفحه دارد." if page_count else ""
        await message.reply_text(
            f"بازه را مثل `3-7` بفرست.{suffix}",
            parse_mode="Markdown",
            reply_markup=build_back_only_menu("بازه صفحه را بفرست یا بازگشت را بزن..."),
        )
    return PDF_SPLIT_RANGE


async def pdf_merger_invalid_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Prompt the user to send PDFs or press the merge action."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "فایل‌های PDF را یکی‌یکی بفرست یا دکمه «✅ شروع ادغام» را بزن.\n\n"
            f"{format_pdf_merge_queue(context.user_data.get('pdf_merge_names', []))}",
            reply_markup=build_pdf_merge_menu(),
        )
    return PDF_MERGE


async def news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source_key: str = "iranintl",
) -> None:
    """Handle source-specific grouped news fetching."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_NEWS)
    fetcher: NewsFetcher = context.application.bot_data["news_fetcher"]
    message = update.effective_message
    if message is None:
        return
    source = NEWS_SOURCES[source_key]

    await message.reply_chat_action(ChatAction.TYPING)
    current_menu_markup = get_current_menu_markup(context)
    status_message = await message.reply_text(
        f"در حال جمع‌آوری 30 پست آخر از {source.display_name} و ساخت خلاصه گروه‌بندی‌شده...",
        reply_markup=current_menu_markup,
    )

    try:
        items = await fetcher.fetch_latest_news(source=source)
    except NewsFetcherAuthorizationError:
        await safe_edit_or_reply(
            status_message,
            message,
            "برای دریافت خبرها، ابتدا باید Telethon را یک بار با اکانت تلگرام خودتان وارد کنید.\n"
            "در ترمینال پروژه این دستور را اجرا کنید:\n"
            "`python auth_telethon.py`",
            parse_mode="Markdown",
            reply_markup=current_menu_markup,
        )
        return
    except Exception as exc:
        LOGGER.exception("/news failed")
        await safe_edit_or_reply(
            status_message,
            message,
            f"خطا در دریافت خبرها: {exc}",
            reply_markup=current_menu_markup,
        )
        return

    if items.post_count == 0:
        await safe_edit_or_reply(
            status_message,
            message,
            f"هیچ متن یا کپشن جدیدی در 30 پست آخر {source.display_name} پیدا نشد.",
            reply_markup=current_menu_markup,
        )
        return

    summary_text = items.grouped_summary
    summary_text = ensure_news_source_title(summary_text, source.display_name)
    chunks = chunk_text(summary_text)
    await safe_edit_or_reply(
        status_message,
        message,
        chunks[0],
        disable_web_page_preview=True,
        reply_markup=current_menu_markup,
    )
    for chunk in chunks[1:]:
        await message.reply_text(
            chunk,
            disable_web_page_preview=True,
            reply_markup=current_menu_markup,
        )


def cleanup_file(file_path: Path | None) -> None:
    """Remove a temporary download if it exists."""
    if file_path is None or KEEP_DOWNLOADED_VIDEOS:
        return

    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("Failed to remove temporary file: %s", file_path)


def build_multipart_media_filename(file_path: Path, *, index: int, total_parts: int) -> str:
    """Build a stable, user-visible part filename for multipart Rubika delivery."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", file_path.stem).strip("._")
    stem = stem[:60] if stem else "youtube_media"
    return f"{stem}_part_{index:02d}_of_{total_parts:02d}{file_path.suffix.lower()}"


def build_multipart_media_caption(
    result: DownloadedVideo,
    *,
    index: int,
    total_parts: int,
) -> str:
    """Build the caption shown under each multipart media attachment."""
    media_title = "فایل صوتی" if result.media_kind == MEDIA_KIND_AUDIO else "ویدیو"
    return f"{result.title}\n{media_title} - بخش {index} از {total_parts}"


async def send_rubika_multipart_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    result: DownloadedVideo,
    *,
    status_message: Message | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Split an oversized YouTube download and send it to Rubika as labeled attachments."""
    query = update.callback_query
    if query is None or query.message is None or result.file_path is None:
        return

    work_dir = DOWNLOADS_DIR / f"rubika-youtube-parts-{uuid.uuid4().hex}"
    is_audio_upload = result.media_kind == MEDIA_KIND_AUDIO
    part_paths: list[Path] = []
    audio_suffixes = {".m4a", ".mp3", ".aac", ".wav", ".ogg", ".opus"}

    try:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download cancelled by user.")
        work_dir.mkdir(parents=True, exist_ok=True)
        if is_audio_upload:
            part_paths = await asyncio.to_thread(
                split_audio_for_telegram,
                result.file_path,
                work_dir,
                max_size_bytes=RUBIKA_MULTIPART_LIMIT_BYTES,
                output_suffix=result.file_path.suffix.lower(),
            )
        else:
            part_paths = await asyncio.to_thread(
                split_final,
                result.file_path,
                work_dir,
                max_size_bytes=RUBIKA_MULTIPART_LIMIT_BYTES,
            )

        total_parts = len(part_paths)
        if total_parts <= 0:
            raise RuntimeError("No output parts were produced.")

        await update_download_status_message(
            status_message,
            (
                f"فایل صوتی دانلود شد و در {total_parts} بخش ارسال می‌شود..."
                if is_audio_upload
                else f"ویدیو دانلود شد و در {total_parts} بخش ارسال می‌شود..."
            ),
        )

        for index, part_path in enumerate(part_paths, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("Download cancelled by user.")
            await update_download_status_message(
                status_message,
                (
                    f"در حال ارسال بخش {index} از {total_parts} فایل صوتی..."
                    if is_audio_upload
                    else f"در حال ارسال بخش {index} از {total_parts} ویدیو..."
                ),
            )
            part_caption = build_multipart_media_caption(
                result,
                index=index,
                total_parts=total_parts,
            )
            part_filename = build_multipart_media_filename(
                result.file_path,
                index=index,
                total_parts=total_parts,
            )
            with part_path.open("rb") as media_part:
                if is_audio_upload:
                    await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_DOCUMENT)
                    if part_path.suffix.lower() in audio_suffixes:
                        await context.bot.send_audio(
                            chat_id=query.message.chat_id,
                            audio=media_part,
                            caption=part_caption,
                            filename=part_filename,
                            title=f"{result.title} ({index}/{total_parts})",
                        )
                    else:
                        await context.bot.send_document(
                            chat_id=query.message.chat_id,
                            document=media_part,
                            caption=part_caption,
                            filename=part_filename,
                        )
                else:
                    await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)
                    await context.bot.send_video(
                        chat_id=query.message.chat_id,
                        video=media_part,
                        caption=part_caption,
                        filename=part_filename,
                        supports_streaming=True,
                    )

        await update_download_status_message(
            status_message,
            (
                f"فایل صوتی در {total_parts} بخش ارسال شد."
                if is_audio_upload
                else f"ویدیو در {total_parts} بخش ارسال شد."
            ),
            reply_markup=None,
        )
    finally:
        cleanup_directory(work_dir)


def resolve_local_bot_api_file_path(file_path: Path) -> str | None:
    """Map a local download path to the path visible inside the Bot API container."""
    if LOCAL_BOT_API_SHARED_DOWNLOADS_PATH is None:
        return None
    container_path = LOCAL_BOT_API_SHARED_DOWNLOADS_PATH / file_path.name
    return container_path.as_uri()


def is_local_bot_api_reachable(base_url: str | None) -> bool:
    """Check whether the configured local Bot API endpoint is reachable."""
    if not base_url:
        return False

    parsed = urlparse(base_url)
    host = parsed.hostname
    if host is None:
        return False

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        LOGGER.warning(
            "Local Bot API is configured but unreachable at %s. Falling back to Telegram cloud Bot API.",
            base_url,
        )
        return False


async def youtube_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the two-step YouTube search flow."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_YOUTUBE)
    clear_active_youtube_search_state(context)
    clear_active_youtube_browser_state(context)
    context.user_data["youtube_mode"] = YOUTUBE_MODE_SEARCH
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    await message.reply_text(
        "چی واست تو یوتیوب پیدا کنم؟\nمثال: اخبار ایران، آموزش پایتون، AI agents",
        reply_markup=build_back_only_menu("عبارت جستجو را بنویس یا بازگشت را بزن..."),
    )
    return YOUTUBE_QUERY


async def show_youtube_saved_channels(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show the current user's saved channels."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_YOUTUBE)
    message = update.effective_message
    if message is None:
        return
    snapshot = get_youtube_library_snapshot(update, context)
    if snapshot is None:
        await message.reply_text(
            "فعلاً به ذخیره‌های یوتیوب دسترسی ندارم.",
            reply_markup=get_current_menu_markup(context),
        )
        return
    await message.reply_text(
        render_saved_channels_text(snapshot, page=0),
        reply_markup=build_saved_channels_keyboard(snapshot, page=0),
        disable_web_page_preview=True,
    )


async def show_youtube_saved_playlists(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show the current user's saved playlists."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_YOUTUBE)
    message = update.effective_message
    if message is None:
        return
    snapshot = get_youtube_library_snapshot(update, context)
    if snapshot is None:
        await message.reply_text(
            "فعلاً به پلی‌لیست‌های ذخیره‌شده دسترسی ندارم.",
            reply_markup=get_current_menu_markup(context),
        )
        return
    await message.reply_text(
        render_saved_playlists_text(snapshot, page=0),
        reply_markup=build_saved_playlists_keyboard(snapshot, page=0),
        disable_web_page_preview=True,
    )


async def show_youtube_recent_searches(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show the current user's recent YouTube searches."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_YOUTUBE)
    message = update.effective_message
    if message is None:
        return
    snapshot = get_youtube_library_snapshot(update, context)
    if snapshot is None:
        await message.reply_text(
            "فعلاً به جستجوهای اخیر دسترسی ندارم.",
            reply_markup=get_current_menu_markup(context),
        )
        return
    await message.reply_text(
        render_youtube_recent_searches_text(snapshot),
        reply_markup=build_youtube_recent_searches_keyboard(snapshot),
        disable_web_page_preview=True,
    )


async def youtube_download_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the YouTube direct download flow from a pasted URL."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_YOUTUBE)
    clear_active_youtube_search_state(context)
    clear_active_youtube_browser_state(context)
    context.user_data["youtube_mode"] = YOUTUBE_MODE_DOWNLOAD
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    await message.reply_text(
        "لینک یوتیوب را بفرست تا کیفیت‌های قابل دانلود را بهت نشان بدهم.",
        reply_markup=build_back_only_menu("لینک یوتیوب را بفرست یا بازگشت را بزن..."),
    )
    return YOUTUBE_QUERY


async def youtube_channel_search_entry(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Prompt for an in-channel YouTube search query."""
    await register_user_if_needed(update, context)
    channel, _home_view_token = get_pending_youtube_channel_search(context)
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    if channel is None or not channel.browse_url:
        set_current_menu(context, MENU_YOUTUBE)
        await message.reply_text(
            "اول از داخل یک ویدیو، کانال را باز کن.",
            reply_markup=get_current_menu_markup(context),
        )
        return ConversationHandler.END

    set_current_menu(context, MENU_YOUTUBE_BROWSE)
    context.user_data["youtube_mode"] = YOUTUBE_MODE_CHANNEL_SEARCH
    await message.reply_text(
        f"عبارت جستجو را برای کانال {channel.title} بنویس.",
        reply_markup=get_current_menu_markup(context),
    )
    return YOUTUBE_QUERY


async def youtube_saved_search_entry(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Prompt for a query that should search only inside Watch Later."""
    await register_user_if_needed(update, context)
    message = update.effective_message
    if message is None:
        return ConversationHandler.END
    snapshot = get_youtube_library_snapshot(update, context)
    if snapshot is None:
        await message.reply_text(
            "فعلاً به ذخیره‌های یوتیوب دسترسی ندارم.",
            reply_markup=get_current_menu_markup(context),
        )
        return ConversationHandler.END
    if not snapshot.get("watch_later"):
        await message.reply_text(
            "Watch Later تو هنوز خالی است.",
            reply_markup=get_current_menu_markup(context),
        )
        return ConversationHandler.END

    set_current_menu(context, MENU_YOUTUBE)
    context.user_data["youtube_mode"] = YOUTUBE_MODE_WATCH_LATER_SEARCH
    await message.reply_text(
        "عبارت را بنویس تا فقط داخل Watch Later بگردم.",
        reply_markup=build_back_only_menu("عبارت جستجو را بنویس یا بازگشت را بزن..."),
    )
    return YOUTUBE_QUERY


async def show_youtube_watch_later(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show watch-later videos as paginated cards."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_YOUTUBE)
    message = update.effective_message
    if message is None:
        return
    snapshot = get_youtube_library_snapshot(update, context)
    if snapshot is None:
        await message.reply_text(
            "فعلاً به Watch Later دسترسی ندارم.",
            reply_markup=get_current_menu_markup(context),
        )
        return
    result_items = build_watch_later_result_items(snapshot)
    if not result_items:
        await message.reply_text(
            "Watch Later تو هنوز خالی است.",
            reply_markup=get_current_menu_markup(context),
        )
        return
    await deliver_youtube_search_results(
        context,
        chat_id=message.chat_id,
        result_items=result_items,
        control_text=(
            f"{min(len(result_items), YOUTUBE_SEARCH_PAGE_SIZE)} ویدیو از Watch Later ارسال شد."
            if len(result_items) > YOUTUBE_SEARCH_PAGE_SIZE
            else f"{len(result_items)} ویدیو در Watch Later داری."
        ),
        source=YOUTUBE_RESULT_SOURCE_WATCH_LATER,
    )


async def start_gemini_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    with_news_context: bool,
) -> int:
    """Start either a general Gemini chat or the news-aware AI chat."""
    await register_user_if_needed(update, context)
    chat_manager: GeminiChatManager = context.application.bot_data["gemini_chat_manager"]
    fetcher: NewsFetcher = context.application.bot_data["news_fetcher"]
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return ConversationHandler.END

    chat_manager.reset_session(user.id)
    context.user_data["ai_chat_active"] = True
    context.user_data["ai_chat_mode"] = "news" if with_news_context else "general"
    clear_developer_mode_state(context)
    set_current_menu(context, MENU_AI_CHAT)
    current_menu_markup = get_current_menu_markup(context)

    if not with_news_context:
        live_search_line = (
            "\nدر صورت نیاز از جستجوی زنده گوگل هم استفاده می‌کند."
            if chat_manager.live_search_enabled
            else ""
        )
        await message.reply_text(
            "گفتگو با Gemini شروع شد."
            f"{live_search_line}\n"
            "پیام‌ات را بفرست.\nبرای پایان، «🔙 بازگشت به منو» را بزن.",
            reply_markup=current_menu_markup,
        )
        return AI_CHAT

    status_message = await message.reply_text(
        "در حال آماده‌سازی 5 منبع خبری برای گفتگو...",
        reply_markup=current_menu_markup,
    )
    try:
        reference_files = await fetcher.refresh_reference_files()
    except Exception as exc:
        LOGGER.exception("Failed to refresh AI reference files")
        await safe_edit_or_reply(
            status_message,
            message,
            f"خطا در آماده‌سازی منابع خبری: {exc}",
            reply_markup=current_menu_markup,
        )
        return ConversationHandler.END

    context.application.bot_data["ai_reference_payloads"] = {
        key: {
            "file_path": str(reference.file_path),
            "raw_text": reference.raw_text,
            "source_name": reference.source_name,
        }
        for key, reference in reference_files.items()
    }
    await safe_edit_or_reply(
        status_message,
        message,
        "گفتگو با Gemini درباره خبرها شروع شد.\nپیام‌ات را بفرست.\nبرای پایان، «🔙 بازگشت به منو» را بزن.",
        reply_markup=current_menu_markup,
    )
    return AI_CHAT


async def general_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start a direct Gemini chat session from the main AI entry."""
    return await start_gemini_chat(update, context, with_news_context=False)


async def web_search_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt the user for a compact Google-style web search query."""
    await register_user_if_needed(update, context)
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    set_current_menu(context, MENU_GENERAL_AI)
    clear_active_web_search_state(context)
    await message.reply_text(
        "عبارتت را بفرست تا در وب بگردم و 6 نتیجه اول را جمع‌وجور نشانت بدهم.",
        reply_markup=build_back_only_menu("عبارت جستجو را بنویس یا بازگشت را بزن..."),
    )
    return WEB_SEARCH_QUERY


async def image_search_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt the user for an image search query."""
    await register_user_if_needed(update, context)
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    set_current_menu(context, MENU_GENERAL_AI)
    clear_active_image_search_state(context)
    await message.reply_text(
        "عبارتت را بفرست تا 5 تصویر اول را یکجا برایت بفرستم.",
        reply_markup=build_back_only_menu("عبارت جستجوی تصویر را بنویس یا بازگشت را بزن..."),
    )
    return IMAGE_SEARCH_QUERY


async def web_search_back_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return from compact web search to the Web+Ai menu."""
    clear_active_web_search_state(context)
    await show_general_ai_menu(update, context)
    return ConversationHandler.END


async def image_search_back_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return from image search to the Web+Ai menu."""
    clear_active_image_search_state(context)
    await show_general_ai_menu(update, context)
    return ConversationHandler.END


async def send_web_search_results_page(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    *,
    query: str,
    results: list[dict[str, str]],
    page: int,
) -> None:
    """Send one paginated block of compact Google-style search results."""
    start = max(page, 0) * WEB_SEARCH_PAGE_SIZE
    has_more_results = start + WEB_SEARCH_PAGE_SIZE < len(results)
    use_html = get_transport_name(context) == "telegram"
    if get_transport_name(context) == "rubika":
        rubika_text, rubika_metadata = build_rubika_web_search_text_and_metadata(
            query,
            results,
            page=page,
        )
        rubika_reply_markup = build_rubika_web_search_navigation_keyboard(
            has_more_results=has_more_results
        )
        try:
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=rubika_text,
                metadata=rubika_metadata,
                disable_notification=None,
                reply_markup=rubika_reply_markup,
            )
        except Exception:
            LOGGER.exception("Rubika metadata hyperlink send failed; falling back to link buttons.")
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=build_web_search_results_text(
                    query,
                    results,
                    page=page,
                    use_html=False,
                    include_urls=False,
                ),
                disable_notification=None,
                reply_markup=build_rubika_web_search_inline_keyboard(results, page=page),
            )
        return
    await message.reply_text(
        build_web_search_results_text(
            query,
            results,
            page=page,
            use_html=use_html,
            include_urls=True,
        ),
        parse_mode="HTML" if use_html else None,
        disable_web_page_preview=True,
        reply_markup=build_web_search_results_menu(has_more_results=has_more_results),
    )


async def send_image_search_results_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    query: str,
    results: list[dict[str, str]],
    start_index: int,
) -> None:
    """Send one page of image search results for the active transport."""
    message = update.effective_message
    if message is None:
        return

    transport_name = get_transport_name(context)
    batch_size = RUBIKA_IMAGE_SEARCH_BATCH_SIZE if transport_name == "rubika" else IMAGE_SEARCH_ALBUM_SIZE
    start = max(start_index, 0)
    page_results = deserialize_image_search_results(results[start : start + batch_size])
    has_more_results = start + 5 < len(results) if transport_name == "rubika" else start + batch_size < len(results)
    has_ten_more_results = transport_name == "rubika" and start + 10 < len(results)
    sent_count = 0

    if transport_name == "telegram":
        media_items = await build_image_search_media_group(page_results, query=query)
        if media_items:
            if len(media_items) == 1:
                await context.bot.send_photo(
                    chat_id=message.chat_id,
                    photo=media_items[0].media,
                    caption=media_items[0].caption,
                )
            else:
                await context.bot.send_media_group(chat_id=message.chat_id, media=media_items)
            sent_count = len(media_items)
    else:
        uploads = await build_image_search_uploads(page_results, query=query)
        for photo, caption in uploads:
            await context.bot.send_photo(
                chat_id=message.chat_id,
                photo=photo,
                caption=caption,
            )
        sent_count = len(uploads)

    if sent_count:
        status_text = f"{sent_count} تصویر از نتایج {start + 1} به بعد ارسال شد."
    else:
        status_text = "در این دسته تصویر قابل ارسالی پیدا نشد."
    await message.reply_text(
        status_text,
        reply_markup=build_image_search_results_menu(
            transport_name=transport_name,
            has_more_results=has_more_results,
            has_ten_more_results=has_ten_more_results,
        ),
    )


async def web_search_query_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Run a grounded web search or page through the current result set."""
    message = update.effective_message
    if message is None or not message.text:
        return WEB_SEARCH_QUERY

    text = message.text.strip()
    if text == BTN_GENERAL_AI_MORE_RESULTS:
        query, results, current_page = get_active_web_search_state(context)
        if not query or not results:
            await message.reply_text(
                "اول یک عبارت جستجو بفرست.",
                reply_markup=build_back_only_menu("عبارت جستجو را بنویس یا بازگشت را بزن..."),
            )
            return WEB_SEARCH_QUERY

        next_page = current_page + 1
        start = next_page * WEB_SEARCH_PAGE_SIZE
        if start >= len(results):
            await message.reply_text(
                "نتیجه بیشتری برای این جستجو ندارم.",
                reply_markup=build_web_search_results_menu(has_more_results=False),
            )
            return WEB_SEARCH_QUERY

        set_active_web_search_state(context, query=query, results=results, page=next_page)
        await send_web_search_results_page(
            context,
            message,
            query=query,
            results=results,
            page=next_page,
        )
        return WEB_SEARCH_QUERY

    try:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
        results = await get_google_web_search_service(context).search(text, limit=18)
    except Exception:
        LOGGER.exception("Web search failed")
        await message.reply_text(
            "جستجوی وب موقتاً در دسترس نیست. کمی بعد دوباره امتحان کن.",
            reply_markup=build_back_only_menu("عبارت جستجو را بنویس یا بازگشت را بزن..."),
        )
        return WEB_SEARCH_QUERY

    if not results:
        clear_active_web_search_state(context)
        await message.reply_text(
            "نتیجه‌ای پیدا نشد. یک عبارت دقیق‌تر بفرست.",
            reply_markup=build_back_only_menu("عبارت جستجو را بنویس یا بازگشت را بزن..."),
        )
        return WEB_SEARCH_QUERY

    serialized_results = serialize_web_search_results(results)
    set_active_web_search_state(
        context,
        query=text,
        results=serialized_results,
        page=0,
    )
    record_search_activity(
        context,
        feature="web_search",
        query=text,
        update=update,
        extra={"result_count": len(serialized_results)},
    )
    await send_web_search_results_page(
        context,
        message,
        query=text,
        results=serialized_results,
        page=0,
    )
    return WEB_SEARCH_QUERY


async def web_search_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Rubika inline pagination for web search results."""
    query = update.callback_query
    if query is None or query.message is None:
        return ConversationHandler.END

    await query.answer()
    search_query, results, current_page = get_active_web_search_state(context)
    if not search_query or not results:
        await query.edit_message_text(
            "نتایج این جستجو دیگر در دسترس نیست. دوباره جستجو کن.",
            reply_markup=None,
        )
        return WEB_SEARCH_QUERY

    next_page = current_page + 1
    start = next_page * WEB_SEARCH_PAGE_SIZE
    if start >= len(results):
        await query.answer("نتیجه بیشتری نیست.", show_alert=False)
        return WEB_SEARCH_QUERY

    set_active_web_search_state(
        context,
        query=search_query,
        results=results,
        page=next_page,
    )
    await send_web_search_results_page(
        context,
        query.message,
        query=search_query,
        results=results,
        page=next_page,
    )
    return WEB_SEARCH_QUERY


async def image_search_query_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Run an image search or page through the current result set."""
    message = update.effective_message
    if message is None or not message.text:
        return IMAGE_SEARCH_QUERY

    text = message.text.strip()
    if text in {BTN_GENERAL_AI_MORE_IMAGES, BTN_GENERAL_AI_MORE_IMAGES_5, BTN_GENERAL_AI_MORE_IMAGES_10}:
        query, results, current_index = get_active_image_search_state(context)
        if not query or not results:
            await message.reply_text(
                "اول یک عبارت جستجوی تصویر بفرست.",
                reply_markup=build_back_only_menu("عبارت جستجوی تصویر را بنویس یا بازگشت را بزن..."),
            )
            return IMAGE_SEARCH_QUERY

        increment = 10 if text in {BTN_GENERAL_AI_MORE_IMAGES, BTN_GENERAL_AI_MORE_IMAGES_10} else 5
        next_index = current_index + increment
        if next_index >= len(results):
            await message.reply_text(
                "تصویر بیشتری برای این جستجو ندارم.",
                reply_markup=build_image_search_results_menu(
                    transport_name=get_transport_name(context),
                    has_more_results=False,
                ),
            )
            return IMAGE_SEARCH_QUERY

        set_active_image_search_state(context, query=query, results=results, index=next_index)
        await send_image_search_results_page(
            update,
            context,
            query=query,
            results=results,
            start_index=next_index,
        )
        return IMAGE_SEARCH_QUERY

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.UPLOAD_PHOTO,
        )
        image_results = await get_image_search_service(context).search(text, limit=60)
    except Exception:
        LOGGER.exception("Image search failed")
        await message.reply_text(
            "جستجوی تصویر موقتاً در دسترس نیست. کمی بعد دوباره امتحان کن.",
            reply_markup=build_back_only_menu("عبارت جستجوی تصویر را بنویس یا بازگشت را بزن..."),
        )
        return IMAGE_SEARCH_QUERY

    if not image_results:
        clear_active_image_search_state(context)
        await message.reply_text(
            "تصویر قابل ارسالی پیدا نشد. یک عبارت دیگر امتحان کن.",
            reply_markup=build_back_only_menu("عبارت جستجوی تصویر را بنویس یا بازگشت را بزن..."),
        )
        return IMAGE_SEARCH_QUERY

    serialized_results = serialize_image_search_results(image_results)
    set_active_image_search_state(
        context,
        query=text,
        results=serialized_results,
        index=0,
    )
    record_search_activity(
        context,
        feature="image_search",
        query=text,
        update=update,
        extra={"result_count": len(serialized_results)},
    )
    await send_image_search_results_page(
        update,
        context,
        query=text,
        results=serialized_results,
        start_index=0,
    )
    return IMAGE_SEARCH_QUERY


async def developer_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the admin-only developer chat mode."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return ConversationHandler.END

    if not is_developer_mode_allowed(context):
        await message.reply_text(
            "Developer Mode فقط برای ادمین‌های مجاز فعال است.",
            reply_markup=get_current_menu_markup(context),
        )
        return ConversationHandler.END

    chat_manager: GeminiChatManager = context.application.bot_data["gemini_chat_manager"]
    chat_manager.reset_session(user.id)
    context.user_data["ai_chat_active"] = True
    context.user_data["ai_chat_mode"] = "developer"
    clear_developer_mode_state(context)
    set_current_menu(context, MENU_AI_CHAT)
    if should_prompt_developer_model_selection(context):
        set_developer_stage(context, DEVELOPER_STAGE_CHOOSE_MODEL)
        await message.reply_text(
            "🧑‍💻 Developer Mode فعال شد.\n"
            "اول مدل موردنظرت را انتخاب کن.\n"
            "بعد مستقیم می‌توانی گفتگو را شروع کنی.\n"
            "اگر وسط کار فایل لازم شد، همان‌جا فایل اضافه کن.\n"
            "برای پایان، «🔙 بازگشت به منو» را بزن.",
            reply_markup=get_current_menu_markup(context),
        )
    else:
        set_selected_developer_model(context, DEVELOPER_MODEL_GEMINI_PRO)
        set_developer_stage(context, DEVELOPER_STAGE_PROMPT_ONLY)
        await message.reply_text(
            "🧑‍💻 Developer Mode فعال شد.\n"
            f"{build_developer_prompt_intro(context)}\n"
            "برای پایان، «🔙 بازگشت به منو» را بزن.",
            reply_markup=get_current_menu_markup(context),
        )
    return AI_CHAT


async def developer_mode_control_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Handle Developer Mode workflow buttons."""
    message = update.effective_message
    if message is None or not message.text:
        return AI_CHAT
    if context.user_data.get("ai_chat_mode") != "developer":
        return AI_CHAT

    text = message.text
    selected_model = dict(get_available_developer_models(context)).get(text)
    if selected_model is not None:
        set_selected_developer_model(context, selected_model)
        set_developer_stage(context, DEVELOPER_STAGE_PROMPT_ONLY)
        await message.reply_text(
            f"مدل Developer Mode روی «{text}» تنظیم شد.\n"
            f"{build_developer_prompt_intro(context)}",
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    if text == BTN_DEVELOPER_PROMPT_ONLY:
        set_developer_stage(context, DEVELOPER_STAGE_PROMPT_ONLY)
        await message.reply_text(
            build_developer_prompt_intro(context),
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    if text in {BTN_DEVELOPER_WITH_FILES, BTN_DEVELOPER_ADD_FILES}:
        set_developer_stage(context, DEVELOPER_STAGE_COLLECT_FILES)
        await message.reply_text(
            f"مدل انتخاب‌شده: {get_selected_developer_model_label(context)}\n"
            "فایل‌ها را یکی‌یکی بفرست.\n"
            f"پشتیبانی‌شده: {developer_input_support_text()}\n"
            f"حداکثر {DEVELOPER_MAX_INPUT_FILES} فایل.\n"
            f"حداکثر حجم هر فایل: {format_file_size_mb(DEVELOPER_MAX_INPUT_FILE_BYTES)}\n\n"
            "وقتی تمام شد دکمه «✅ فایل‌ها کامل شد» را بزن.",
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    if text == BTN_DEVELOPER_FILES_CLEAR:
        clear_developer_input_files(context)
        set_developer_stage(context, DEVELOPER_STAGE_PROMPT_ONLY)
        await message.reply_text(
            f"فایل‌های Developer Mode پاک شدند.\n"
            f"مدل فعلی: {get_selected_developer_model_label(context)}\n"
            "اگر دوباره فایل لازم شد، «📎 افزودن فایل» را بزن.",
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    if text == BTN_DEVELOPER_FILES_DONE:
        if not get_developer_input_files(context):
            await message.reply_text(
                "هنوز فایلی ثبت نشده است.\nاول فایل‌ها را بفرست، بعد این دکمه را بزن.",
                reply_markup=get_current_menu_markup(context),
            )
            return AI_CHAT
        set_developer_stage(context, DEVELOPER_STAGE_PROMPT_WITH_FILES)
        await message.reply_text(
            f"{format_developer_input_files(context)}\n\nحالا پرامپتت را درباره این فایل‌ها بفرست.",
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    return AI_CHAT


async def developer_document_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Handle uploaded source files during Developer Mode file-collection flows."""
    message = update.effective_message
    if message is None or message.document is None:
        return AI_CHAT

    document = message.document
    file_name = getattr(document, "file_name", None) or f"{getattr(document, 'file_unique_id', 'uploaded_file')}.bin"
    mime_type = getattr(document, "mime_type", None)
    file_size = getattr(document, "file_size", 0) or 0
    return await stage_developer_input_media(
        update,
        context,
        media=document,
        file_name=file_name,
        mime_type=mime_type,
        file_size=file_size,
    )


async def developer_photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Handle uploaded photos during Developer Mode file-collection flows."""
    message = update.effective_message
    if message is None or not message.photo:
        return AI_CHAT

    photo = message.photo[-1]
    file_name = f"{getattr(photo, 'file_unique_id', 'uploaded_photo')}.jpg"
    file_size = getattr(photo, "file_size", 0) or 0
    return await stage_developer_input_media(
        update,
        context,
        media=photo,
        file_name=file_name,
        mime_type="image/jpeg",
        file_size=file_size,
    )


async def stage_developer_input_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    media,
    file_name: str,
    mime_type: str | None,
    file_size: int,
) -> int:
    """Download and stage one Developer Mode attachment."""
    message = update.effective_message
    if message is None:
        return AI_CHAT

    if context.user_data.get("ai_chat_mode") != "developer":
        await message.reply_text(
            "در این گفتگو فعلاً فقط پیام متنی پشتیبانی می‌شود.",
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    stage = get_developer_stage(context)
    if stage == DEVELOPER_STAGE_PROMPT_ONLY:
        set_developer_stage(context, DEVELOPER_STAGE_COLLECT_FILES)
        stage = DEVELOPER_STAGE_COLLECT_FILES

    if stage not in {DEVELOPER_STAGE_COLLECT_FILES, DEVELOPER_STAGE_PROMPT_WITH_FILES}:
        await message.reply_text(
            "اول Developer Mode را شروع کن و مدل را انتخاب کن.",
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    if not is_supported_developer_input_file(file_name, mime_type):
        await message.reply_text(
            "Developer Mode فعلاً فقط این ورودی‌ها را می‌پذیرد:\n"
            f"{developer_input_support_text()}",
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    if file_size > DEVELOPER_MAX_INPUT_FILE_BYTES:
        await message.reply_text(
            f"حجم فایل برای Developer Mode زیاد است.\nحداکثر مجاز: {format_file_size_mb(DEVELOPER_MAX_INPUT_FILE_BYTES)}",
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    staged_files = get_developer_input_files(context)
    if len(staged_files) >= DEVELOPER_MAX_INPUT_FILES:
        await message.reply_text(
            f"حداکثر {DEVELOPER_MAX_INPUT_FILES} فایل می‌توانی ثبت کنی.\n"
            "اگر این‌ها کافی است «✅ فایل‌ها کامل شد» را بزن.",
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    temp_dir = DOWNLOADS_DIR / f"developer-input-{uuid.uuid4().hex}"
    input_path = temp_dir / sanitize_generated_filename(file_name)
    status_message = await message.reply_text("در حال دریافت فایل برای Developer Mode...")
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        await download_media_to_local_path(
            context,
            media,
            suffix=Path(file_name).suffix or ".bin",
            destination=input_path,
            received_at=message.date,
            chat_id=message.chat_id,
            message_id=message.id,
        )
        file_text, truncated = await asyncio.to_thread(
            read_developer_input_attachment,
            input_path,
            file_name=Path(file_name).name,
            mime_type=mime_type,
            max_chars=DEVELOPER_MAX_INPUT_TEXT_CHARS,
        )
        if _developer_total_input_chars(context) + len(file_text) > DEVELOPER_MAX_TOTAL_INPUT_TEXT_CHARS:
            await safe_edit_or_reply(
                status_message,
                message,
                "مجموع حجم متنی فایل‌ها برای Developer Mode زیاد شد.\n"
                "فایل‌های کمتری بفرست یا بعضی‌ها را پاک کن.",
                reply_markup=get_current_menu_markup(context),
            )
            cleanup_directory(temp_dir)
            return AI_CHAT
        staged_files.append(
            {
                "name": Path(file_name).name,
                "text": file_text,
                "truncated": truncated,
            }
        )
        await safe_edit_or_reply(
            status_message,
            message,
            f"فایل «{Path(file_name).name}» ثبت شد.\n\n{format_developer_input_files(context)}\n\n"
                "اگر فایل دیگری داری بفرست، وگرنه «✅ فایل‌ها کامل شد» را بزن.",
                reply_markup=get_current_menu_markup(context),
            )
    except Exception as exc:
        LOGGER.exception("Developer attachment handling failed")
        await safe_edit_or_reply(
            status_message,
            message,
            f"خطا در پردازش فایل Developer Mode: {exc}",
            reply_markup=get_current_menu_markup(context),
        )
        cleanup_directory(temp_dir)
        return AI_CHAT
    cleanup_directory(temp_dir)
    return AI_CHAT


async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the news-aware Gemini chat session."""
    return await start_gemini_chat(update, context, with_news_context=True)


async def chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle free-form chat messages for the AI conversation."""
    chat_manager: GeminiChatManager = context.application.bot_data["gemini_chat_manager"]
    reference_payloads = (
        load_ai_reference_payloads(context)
        if context.user_data.get("ai_chat_mode") == "news"
        else {}
    )
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not message.text:
        return AI_CHAT

    if not context.user_data.get("ai_chat_active", False):
        set_current_menu(context, MENU_MAIN)
        await message.reply_text(
            "گفتگوی AI تمام شده است. از منوی اصلی دوباره گزینه دلخواهت را انتخاب کن.",
            reply_markup=get_current_menu_markup(context),
        )
        return ConversationHandler.END

    if context.user_data.get("ai_chat_mode") == "developer":
        stage = get_developer_stage(context)
        if stage == DEVELOPER_STAGE_CHOOSE_MODEL:
            await message.reply_text(
                "اول مدل Developer Mode را از روی کیبورد انتخاب کن.",
                reply_markup=get_current_menu_markup(context),
            )
            return AI_CHAT
        if stage == DEVELOPER_STAGE_COLLECT_FILES:
            await message.reply_text(
                "اول فایل‌ها را بفرست، بعد «✅ فایل‌ها کامل شد» را بزن.",
                reply_markup=get_current_menu_markup(context),
            )
            return AI_CHAT

    await message.reply_chat_action(ChatAction.TYPING)
    try:
        attachments = (
            _developer_attachments_payload(context)
            if context.user_data.get("ai_chat_mode") == "developer"
            and get_developer_stage(context) == DEVELOPER_STAGE_PROMPT_WITH_FILES
            else None
        )
        record_search_activity(
            context,
            feature="ai_chat",
            query=message.text,
            update=update,
            extra={
                "mode": str(context.user_data.get("ai_chat_mode", "general")),
                "attachment_count": len(attachments or []),
                "developer_model": (
                    get_selected_developer_model(context)
                    if context.user_data.get("ai_chat_mode") == "developer"
                    else None
                ),
            },
        )
        response = await chat_manager.send_message(
            user.id,
            message.text,
            reference_payloads,
            mode=str(context.user_data.get("ai_chat_mode", "general")),
            attachments=attachments,
            developer_model=(
                get_selected_developer_model(context)
                if context.user_data.get("ai_chat_mode") == "developer"
                else None
            ),
        )
    except Exception as exc:
        LOGGER.exception("/chat failed")
        await message.reply_text(
            f"خطا در پاسخ AI: {exc}",
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    if context.user_data.get("ai_chat_mode") == "developer" and response.request_files:
        set_developer_stage(context, DEVELOPER_STAGE_COLLECT_FILES)

    chunks, parse_mode = prepare_ai_reply_messages(
        context,
        [response.text, *response.extra_texts],
        max_chunks=(
            DEVELOPER_MAX_REPLY_MESSAGES
            if context.user_data.get("ai_chat_mode") == "developer"
            else None
        ),
    )
    first_reply_markup = (
        build_search_confirmation_keyboard()
        if response.needs_search_confirmation
        else get_current_menu_markup(context)
    )
    await message.reply_text(
        chunks[0],
        disable_web_page_preview=True,
        reply_markup=first_reply_markup,
        parse_mode=parse_mode,
    )
    for chunk in chunks[1:]:
        await message.reply_text(
            chunk,
            disable_web_page_preview=True,
            reply_markup=get_current_menu_markup(context),
            parse_mode=parse_mode,
        )
    if response.artifact_filename and response.artifact_text:
        await send_ai_artifact(
            update,
            context,
            filename=response.artifact_filename,
            file_text=response.artifact_text,
            model_name=response.model_name,
        )
    return AI_CHAT


async def end_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End the active Gemini chat session."""
    chat_manager: GeminiChatManager = context.application.bot_data["gemini_chat_manager"]
    message = update.effective_message
    user = update.effective_user
    if user is not None:
        chat_manager.reset_session(user.id)
    context.user_data["ai_chat_active"] = False
    context.user_data.pop("ai_chat_mode", None)
    clear_developer_mode_state(context)
    if message is not None:
        set_current_menu(context, MENU_MAIN)
        await message.reply_text(
            AI_CHAT_ENDED_TEXT,
            reply_markup=get_current_menu_markup(context),
        )
    return ConversationHandler.END


async def ai_chat_back_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End the active Gemini session when the user taps the back-to-menu button."""
    return await end_chat_command(update, context)


async def youtube_query_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle either YouTube search text or a direct download URL."""
    searcher: YouTubeSearcher = context.application.bot_data["youtube_searcher"]
    library_store = get_youtube_library_store(context)
    message = update.effective_message
    if message is None or not message.text:
        return YOUTUBE_QUERY

    query = message.text.strip()
    if not query:
        await message.reply_text("عبارت جستجو خالی است. دوباره بنویس.")
        return YOUTUBE_QUERY

    youtube_mode = context.user_data.get("youtube_mode", YOUTUBE_MODE_SEARCH)
    if youtube_mode == YOUTUBE_MODE_CHANNEL_SEARCH:
        channel, home_view_token = get_pending_youtube_channel_search(context)
        if channel is None or home_view_token is None or not channel.browse_url:
            context.user_data.pop("youtube_mode", None)
            set_current_menu(context, MENU_YOUTUBE)
            await message.reply_text(
                "مرور کانال فعالی برای جستجو وجود ندارد.",
                reply_markup=get_current_menu_markup(context),
            )
            return ConversationHandler.END

        status_message = await message.reply_text(f"در حال جستجو داخل کانال {channel.title}...")
        try:
            record_search_activity(
                context,
                feature="youtube_channel_search",
                query=query,
                update=update,
                extra={
                    "channel_title": channel.title,
                    "channel_url": channel.browse_url,
                },
            )
            fetched_channel, videos = await searcher.search_in_channel(channel.browse_url, query)
        except Exception as exc:
            LOGGER.exception("Channel YouTube search failed")
            context.user_data.pop("youtube_mode", None)
            await safe_edit_or_reply(
                status_message,
                message,
                f"خطا در جستجو داخل کانال: {exc}",
                reply_markup=get_current_menu_markup(context),
            )
            return ConversationHandler.END

        context.user_data.pop("youtube_mode", None)
        try:
            await status_message.delete()
        except TelegramError:
            LOGGER.debug("Failed to delete channel search status message", exc_info=True)

        view_token = store_youtube_browser_view(
            context,
            build_youtube_browser_view_payload(
                kind="video_cards",
                title=f"{fetched_channel.title} · Search · {query}",
                parent_token=home_view_token,
                channel=fetched_channel,
                videos=videos,
                empty_text=f"No results found inside {fetched_channel.title}.",
            ),
        )
        set_pending_youtube_channel_search(
            context,
            channel=fetched_channel,
            home_view_token=home_view_token,
        )
        set_current_menu(context, MENU_YOUTUBE_BROWSE)
        await show_youtube_browser_view(
            context,
            chat_id=message.chat_id,
            view_token=view_token,
        )
        return ConversationHandler.END

    if youtube_mode == YOUTUBE_MODE_WATCH_LATER_SEARCH:
        snapshot = get_youtube_library_snapshot(update, context)
        if snapshot is None:
            context.user_data.pop("youtube_mode", None)
            await message.reply_text(
                "فعلاً به Watch Later دسترسی ندارم.",
                reply_markup=get_current_menu_markup(context),
            )
            return ConversationHandler.END

        status_message = await message.reply_text("در حال جستجو داخل Watch Later...")
        try:
            record_search_activity(
                context,
                feature="youtube_watch_later_search",
                query=query,
                update=update,
            )
            result_items = build_watch_later_result_items(
                snapshot,
                query=query,
            )
        except Exception as exc:
            LOGGER.exception("Watch-later search failed")
            context.user_data.pop("youtube_mode", None)
            await safe_edit_or_reply(
                status_message,
                message,
                f"خطا در جستجو داخل Watch Later: {exc}",
                reply_markup=build_youtube_results_menu(has_more_results=False),
            )
            return ConversationHandler.END

        if not result_items:
            context.user_data.pop("youtube_mode", None)
            await safe_edit_or_reply(
                status_message,
                message,
                "داخل Watch Later نتیجه‌ای پیدا نشد.",
                reply_markup=build_youtube_results_menu(
                    has_more_results=False,
                    include_watch_later_search=True,
                ),
            )
            return ConversationHandler.END

        user_key = get_youtube_library_user_key(update, context)
        if user_key is not None:
            library_store.add_recent_search(
                user_key,
                query,
                scope=YOUTUBE_MODE_WATCH_LATER_SEARCH,
            )
        try:
            await status_message.delete()
        except TelegramError:
            LOGGER.debug("Failed to delete watch-later search status message", exc_info=True)
        await deliver_youtube_search_results(
            context,
            chat_id=message.chat_id,
            result_items=result_items,
            control_text=(
                f"{min(len(result_items), YOUTUBE_SEARCH_PAGE_SIZE)} نتیجه از Watch Later ارسال شد."
                if len(result_items) > YOUTUBE_SEARCH_PAGE_SIZE
                else f"{len(result_items)} نتیجه در Watch Later پیدا شد."
            ),
            source=YOUTUBE_RESULT_SOURCE_WATCH_LATER,
        )
        context.user_data.pop("youtube_mode", None)
        return ConversationHandler.END

    if youtube_mode == YOUTUBE_MODE_DOWNLOAD:
        video_id = extract_youtube_video_id(query)
        if video_id is None:
            await message.reply_text(
                "لینک یوتیوب معتبر نیست.\n"
                "یک لینک مثل `https://www.youtube.com/watch?v=...` یا `https://youtu.be/...` بفرست.",
                parse_mode="Markdown",
                reply_markup=build_back_only_menu("لینک یوتیوب را بفرست یا بازگشت را بزن..."),
            )
            return YOUTUBE_QUERY

        context.user_data.pop("youtube_mode", None)
        await show_youtube_quality_mode_prompt(
            message,
            video_id=video_id,
            prompt_text=(
                "لینک دریافت شد.\n"
                "نوع دانلود را انتخاب کن: Low Quality، High Quality یا Audio Only."
            ),
        )
        return ConversationHandler.END

    await message.reply_chat_action(ChatAction.TYPING)
    status_message = await message.reply_text("در حال جستجو در یوتیوب...")
    search_retry_markup = build_youtube_results_menu(has_more_results=False)

    try:
        record_search_activity(
            context,
            feature="youtube_search",
            query=query,
            update=update,
        )
        videos = await searcher.search(query)
    except Exception as exc:
        LOGGER.exception("/youtube failed")
        context.user_data.pop("youtube_mode", None)
        await safe_edit_or_reply(
            status_message,
            message,
            f"خطا در جستجوی یوتیوب: {exc}",
            reply_markup=search_retry_markup,
        )
        return ConversationHandler.END

    if not videos:
        context.user_data.pop("youtube_mode", None)
        await safe_edit_or_reply(
            status_message,
            message,
            "نتیجه‌ای برای جستجوی شما پیدا نشد.",
            reply_markup=search_retry_markup,
        )
        return ConversationHandler.END

    user_key = get_youtube_library_user_key(update, context)
    if user_key is not None:
        library_store.add_recent_search(
            user_key,
            query,
            scope=YOUTUBE_MODE_SEARCH,
        )

    try:
        await status_message.delete()
    except TelegramError:
        LOGGER.debug("Failed to delete YouTube search status message", exc_info=True)

    await deliver_youtube_search_results(
        context,
        chat_id=message.chat_id,
        result_items=[serialize_youtube_result_item(video) for video in videos],
    )
    context.user_data.pop("youtube_mode", None)
    return ConversationHandler.END


async def cancel_youtube_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel the active YouTube search prompt."""
    context.user_data.pop("youtube_mode", None)
    message = update.effective_message
    if message is not None:
        await message.reply_text("جستجوی یوتیوب لغو شد.", reply_markup=get_current_menu_markup(context))
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unexpected bot errors and notify the user when possible."""
    LOGGER.exception("Unhandled bot error", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message is not None:
        await update.effective_message.reply_text(
            "یک خطای موقت رخ داد. دوباره تلاش کن.",
            reply_markup=get_current_menu_markup(context),
        )


async def news_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle source-specific news menu buttons."""
    message = update.effective_message
    if message is None or not message.text:
        return

    source = NEWS_BUTTON_SOURCES.get(message.text)
    if source is None:
        return
    set_current_menu(context, MENU_NEWS)
    await news_command(update, context, source_key=source.key)


async def main_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route main menu button clicks to the correct submenu."""
    message = update.effective_message
    if message is None or not message.text:
        return

    if message.text == BTN_MAIN_NEWS:
        await show_news_menu(update, context)
        return
    if message.text == BTN_MAIN_YOUTUBE:
        await show_youtube_menu(update, context)
        return
    if message.text == BTN_MAIN_FILE_TOOLS:
        await show_file_tools_menu(update, context)
        return
    if message.text == BTN_MAIN_GENERAL_AI:
        await show_general_ai_menu(update, context)
        return
    if message.text == BTN_MAIN_SUPPORT_BUNDLE:
        await vpn_config_bundle_command(update, context)
        return
    if message.text == BTN_MAIN_SETTINGS:
        await show_rubika_settings_menu(update, context)
        return


async def back_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return from a submenu to the main menu."""
    user = update.effective_user
    chat_manager: GeminiChatManager | None = context.application.bot_data.get(
        "gemini_chat_manager"
    )
    if user is not None and chat_manager is not None:
        chat_manager.reset_session(user.id)
    context.user_data["ai_chat_active"] = False
    context.user_data.pop("ai_chat_mode", None)
    clear_developer_mode_state(context)
    clear_rubika_broadcast_state(context)
    clear_active_youtube_search_state(context)
    clear_active_youtube_browser_state(context)
    clear_active_web_search_state(context)
    clear_active_image_search_state(context)
    context.user_data.pop("youtube_mode", None)
    await show_main_menu(update, context, text="به منوی اصلی برگشتی.")


async def youtube_back_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return from the YouTube prompt to the main menu and close the conversation."""
    clear_active_youtube_search_state(context)
    clear_active_youtube_browser_state(context)
    context.user_data.pop("youtube_mode", None)
    await show_main_menu(update, context, text="به منوی اصلی برگشتی.")
    return ConversationHandler.END


async def youtube_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle YouTube submenu buttons."""
    message = update.effective_message
    if message is None or not message.text:
        return ConversationHandler.END

    if message.text == BTN_YOUTUBE_DOWNLOAD:
        return await youtube_download_entry(update, context)
    return ConversationHandler.END


async def youtube_more_results_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Send the next page of YouTube result cards for the active search session."""
    message = update.effective_message
    if message is None:
        return

    session_token, current_page, source = get_active_youtube_search_state(context)
    if session_token is None:
        await message.reply_text(
            "جستجوی فعالی وجود ندارد. از گزینه «جستجوی جدید» استفاده کن.",
            reply_markup=build_youtube_results_menu_for_source(
                source,
                has_more_results=False,
            ),
        )
        return

    videos = get_stored_youtube_search_results(context, session_token)
    if not videos:
        clear_active_youtube_search_state(context)
        await message.reply_text(
            "نتایج این جستجو دیگر در دسترس نیست. دوباره جستجو کن.",
            reply_markup=build_youtube_results_menu_for_source(
                source,
                has_more_results=False,
            ),
        )
        return

    max_page = max((len(videos) - 1) // YOUTUBE_SEARCH_PAGE_SIZE, 0)
    next_page = current_page + 1
    if next_page > max_page:
        await message.reply_text(
            "مورد بیشتری برای این جستجو وجود ندارد.",
            reply_markup=build_youtube_results_menu_for_source(
                source,
                has_more_results=False,
            ),
        )
        return

    page_count, _total_pages = await send_youtube_search_result_page(
        context,
        chat_id=message.chat_id,
        session_token=session_token,
        page=next_page,
    )
    set_active_youtube_search_state(
        context,
        session_token=session_token,
        page=next_page,
        source=source,
    )
    control_message = await message.reply_text(
        f"{page_count} نتیجه بعدی ارسال شد.",
        reply_markup=build_youtube_results_menu_for_source(
            source,
            has_more_results=next_page < max_page,
        ),
    )
    register_youtube_search_message(
        context,
        session_token=session_token,
        chat_id=message.chat_id,
        message_id=control_message.message_id,
    )


async def file_tools_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file tools submenu actions that are not covered by conversations."""
    message = update.effective_message
    if message is None or not message.text:
        return
    if message.text == BTN_VPN_CONFIG_BUNDLE:
        await vpn_config_bundle_command(update, context)
        return
    await coming_soon_action(update, context, message.text)


async def general_ai_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle general AI submenu placeholder buttons."""
    message = update.effective_message
    if message is None or not message.text:
        return
    if message.text == BTN_GENERAL_AI_START:
        await general_ai_command(update, context)


async def end_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the inline end-chat button under AI replies."""
    query = update.callback_query
    user = update.effective_user
    chat_manager: GeminiChatManager = context.application.bot_data["gemini_chat_manager"]
    if query is None:
        return ConversationHandler.END

    if user is not None:
        chat_manager.reset_session(user.id)
    context.user_data["ai_chat_active"] = False
    context.user_data.pop("ai_chat_mode", None)
    await query.answer("گفتگو تمام شد.")
    await query.edit_message_reply_markup(reply_markup=None)
    if query.message is not None:
        set_current_menu(context, MENU_MAIN)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=AI_CHAT_ENDED_TEXT,
            reply_markup=get_current_menu_markup(context),
        )
    return ConversationHandler.END


async def search_confirmation_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle inline internet-search confirmation buttons."""
    query = update.callback_query
    user = update.effective_user
    chat_manager: GeminiChatManager = context.application.bot_data["gemini_chat_manager"]
    if query is None or user is None:
        return

    pending_query = chat_manager.get_pending_search_query(user.id)
    if not pending_query:
        await query.answer("درخواستی برای جستجو وجود ندارد.", show_alert=False)
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if query.data == AI_SEARCH_NO_CALLBACK:
        chat_manager.clear_pending_search(user.id)
        await query.answer("جستجو لغو شد.", show_alert=False)
        await query.edit_message_text("باشه، جستجو لغو شد. سوال دیگری دارید؟")
        return

    await query.answer("در حال جستجوی اینترنتی...", show_alert=False)
    await query.edit_message_text("در حال جستجوی اینترنتی...")
    try:
        response = await chat_manager.execute_internet_search(user.id, pending_query)
    except Exception as exc:
        LOGGER.exception("Internet search callback failed")
        await query.edit_message_text(f"خطا در جستجوی اینترنتی: {exc}")
        return
    finally:
        chat_manager.clear_pending_search(user.id)

    chunks, parse_mode = prepare_ai_reply_messages(
        context,
        [response.text, *response.extra_texts],
    )
    await query.edit_message_text(
        chunks[0],
        disable_web_page_preview=True,
        parse_mode=parse_mode,
    )
    for chunk in chunks[1:]:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=chunk,
            disable_web_page_preview=True,
            reply_markup=get_current_menu_markup(context),
            parse_mode=parse_mode,
        )


async def send_download_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    result: DownloadedVideo,
    *,
    status_message: Message | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Send the downloaded video/audio or a fallback link."""
    query = update.callback_query
    if query is None or query.message is None:
        return

    local_upload_bot: Bot | None = context.application.bot_data.get("local_upload_bot")
    local_bot_api_url: str | None = context.application.bot_data.get("local_bot_api_url")
    transport_name = get_transport_name(context)
    media_title = "فایل صوتی" if result.media_kind == MEDIA_KIND_AUDIO else "ویدیو"
    is_audio_upload = result.media_kind == MEDIA_KIND_AUDIO
    audio_suffixes = {".m4a", ".mp3", ".aac", ".wav", ".ogg", ".opus"}
    audio_ready_for_send = (
        result.file_path is not None and result.file_path.suffix.lower() in audio_suffixes
    )

    if result.file_path is None:
        cleanup_file(result.file_path)
        await update_download_status_message(
            status_message,
            f"حجم این {media_title} بیشتر از حد مجاز است. لینک مستقیم:\n"
            f"{result.source_url}",
            disable_web_page_preview=False,
            reply_markup=None,
        )
        return

    caption = html.escape(result.title)
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download cancelled by user.")
        if result.exceeds_telegram_limit:
            if transport_name != "telegram":
                try:
                    await send_rubika_multipart_download(
                        update,
                        context,
                        result,
                        status_message=status_message,
                        cancel_event=cancel_event,
                    )
                except DownloadCancelledError:
                    raise
                except Exception as exc:
                    LOGGER.exception("Multipart Rubika delivery failed")
                    await update_download_status_message(
                        status_message,
                        f"خطا در تقسیم و ارسال {media_title}: {exc}",
                        reply_markup=None,
                    )
                return

            if local_upload_bot is None:
                await update_download_status_message(
                    status_message,
                    f"{media_title} روی دستگاه دانلود شد، اما حجم آن بیشتر از 50MB است و local Bot API برای ارسال در دسترس نیست.\n"
                    "برای فرستادن این کیفیت‌ها باید سرور local Bot API روی localhost:8081 در حال اجرا باشد.\n"
                    f"لینک مستقیم:\n{result.source_url}",
                    disable_web_page_preview=False,
                    reply_markup=None,
                )
                return

            if not is_local_bot_api_reachable(local_bot_api_url):
                await update_download_status_message(
                    status_message,
                    f"{media_title} روی دستگاه دانلود شد اما سرور local Bot API در دسترس نیست.\n"
                    "بررسی کن که سرور روی localhost:8081 در حال اجرا باشد تا فایل‌های بالای 50MB ارسال شوند.",
                    disable_web_page_preview=True,
                    reply_markup=None,
                )
                return

            container_visible_path = resolve_local_bot_api_file_path(result.file_path)
            if container_visible_path is None:
                await update_download_status_message(
                    status_message,
                    "سرور محلی Telegram فعال است اما مسیر shared downloads در دسترس نیست.\n"
                    "بهتر است local Bot API را با `./start_local_bot_api.sh` اجرا کنی.\n"
                    "اگر تنظیمات سفارشی داری، مقدار `LOCAL_BOT_API_SHARED_DOWNLOADS_PATH` را بررسی کن.",
                    reply_markup=None,
                )
                return

            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelledError("Download cancelled by user.")
                if is_audio_upload:
                    await local_upload_bot.send_chat_action(
                        query.message.chat_id, ChatAction.UPLOAD_DOCUMENT
                    )
                    LOGGER.info(
                        "Uploading large audio via local Bot API using %s",
                        container_visible_path,
                    )
                    if audio_ready_for_send:
                        await local_upload_bot.send_audio(
                            chat_id=query.message.chat_id,
                            audio=container_visible_path,
                            caption=caption,
                            read_timeout=600,
                            write_timeout=600,
                        )
                    else:
                        await local_upload_bot.send_document(
                            chat_id=query.message.chat_id,
                            document=container_visible_path,
                            caption=caption,
                            read_timeout=600,
                            write_timeout=600,
                        )
                else:
                    await local_upload_bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)
                    LOGGER.info(
                        "Uploading large video via local Bot API using %s",
                        container_visible_path,
                    )
                    await local_upload_bot.send_video(
                        chat_id=query.message.chat_id,
                        video=container_visible_path,
                        caption=caption,
                        supports_streaming=True,
                        read_timeout=600,
                        write_timeout=600,
                    )
            except Exception as exc:
                LOGGER.exception("Large media upload through local Bot API failed")
                await update_download_status_message(
                    status_message,
                    f"{media_title} روی دستگاه دانلود شد اما ارسال حجیم با local Bot API انجام نشد.\n"
                    "بررسی کن که سرور local Bot API روی localhost:8081 در حال اجرا باشد.\n"
                    f"جزئیات خطا: {exc}",
                    disable_web_page_preview=True,
                    reply_markup=None,
                )
                return
            await update_download_status_message(
                status_message,
                "فایل صوتی حجیم با سرور محلی Telegram ارسال شد."
                if is_audio_upload
                else "ویدیوی حجیم با سرور محلی Telegram ارسال شد."
                ,
                reply_markup=None,
            )
            return

        with result.file_path.open("rb") as media_file:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("Download cancelled by user.")
            if is_audio_upload:
                if audio_ready_for_send:
                    await context.bot.send_chat_action(
                        query.message.chat_id, ChatAction.UPLOAD_DOCUMENT
                    )
                    await context.bot.send_audio(
                        chat_id=query.message.chat_id,
                        audio=media_file,
                        caption=caption,
                        title=result.file_path.stem,
                    )
                else:
                    await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_DOCUMENT)
                    await context.bot.send_document(
                        chat_id=query.message.chat_id,
                        document=media_file,
                        caption=caption,
                    )
            else:
                await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)
                await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=media_file,
                    caption=caption,
                    supports_streaming=True,
                )
    finally:
        cleanup_file(result.file_path)

    await update_download_status_message(
        status_message,
        "فایل صوتی دانلود و ارسال شد." if is_audio_upload else "ویدیو دانلود و ارسال شد.",
        reply_markup=None,
    )


async def youtube_search_page_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Move between paginated YouTube search result pages."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{YOUTUBE_SEARCH_PAGE_CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست نامعتبر است.")
        return

    try:
        _prefix, session_token, page_text = payload.split(":", maxsplit=2)
        page = int(page_text)
    except ValueError:
        await query.edit_message_text("صفحه نامعتبر است.")
        return

    results = get_stored_youtube_search_results(context, session_token)
    if not results:
        await query.edit_message_text("نتایج این جستجو دیگر در دسترس نیست. دوباره جستجو کن.")
        return

    max_page = max((len(results) - 1) // YOUTUBE_SEARCH_PAGE_SIZE, 0)
    page = max(0, min(page, max_page))
    await query.edit_message_text(
        format_youtube_search_page(results, page=page),
        disable_web_page_preview=False,
        reply_markup=build_search_keyboard(session_token, results, page=page),
    )


async def video_selection_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send a thumbnail card for the selected YouTube search result."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{YOUTUBE_SEARCH_RESULT_CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست نامعتبر است.")
        return

    try:
        _prefix, session_token, index_text = payload.split(":", maxsplit=2)
        index = int(index_text)
    except ValueError:
        await query.edit_message_text("نتیجه انتخاب‌شده نامعتبر است.")
        return

    results = get_stored_youtube_search_results(context, session_token)
    if index < 0 or index >= len(results):
        await query.edit_message_text("این نتیجه دیگر در دسترس نیست. دوباره جستجو کن.")
        return

    video, playlist, watch_later_key = deserialize_youtube_result_item(results[index])
    await send_youtube_result_card_from_callback(
        update,
        context,
        video=video,
        playlist=playlist,
        watch_later_key=watch_later_key,
    )


async def youtube_open_channel_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Open the channel browser for a selected video card."""
    query = update.callback_query
    if query is None or query.message is None:
        return

    await query.answer()
    if not transport_supports_youtube_browser(context):
        await query.answer("Channel browsing is not available here yet.", show_alert=True)
        return

    payload = query.data or ""
    if not payload.startswith(f"{YOUTUBE_OPEN_CHANNEL_CALLBACK_PREFIX}:"):
        await query.edit_message_text("Invalid channel request.")
        return

    try:
        _prefix, card_token = payload.split(":", maxsplit=1)
    except ValueError:
        await query.edit_message_text("Invalid channel request.")
        return

    card_context = get_youtube_card_context(context, card_token)
    if card_context is None:
        await query.answer("This card expired. Search again.", show_alert=True)
        return

    video_payload = card_context.get("video")
    if not isinstance(video_payload, dict):
        await query.answer("This card is missing channel data.", show_alert=True)
        return

    video = deserialize_youtube_video(video_payload)
    channel = build_youtube_channel_from_video(video)
    if channel is None or not channel.browse_url:
        await query.answer("I could not resolve that channel.", show_alert=True)
        return

    session_token = card_context.get("session_token")
    if isinstance(session_token, str) and session_token:
        await clear_youtube_search_messages(
            context,
            session_token=session_token,
            keep_message_id=query.message.message_id,
        )
        clear_active_youtube_search_state(context)

    home_view_token = store_youtube_browser_view(
        context,
        build_youtube_browser_view_payload(
            kind="channel_home",
            title=channel.title,
            channel=channel,
        ),
    )
    set_pending_youtube_channel_search(
        context,
        channel=channel,
        home_view_token=home_view_token,
    )
    await send_youtube_browser_panel(
        context,
        chat_id=query.message.chat_id,
        view_token=home_view_token,
    )


async def youtube_library_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle save/open/delete/recent actions for the lightweight YouTube library."""
    query = update.callback_query
    if query is None or query.message is None:
        return

    payload = query.data or ""
    if not payload.startswith(f"{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست کتابخانه یوتیوب نامعتبر است.")
        return

    parts = payload.split(":")
    if len(parts) < 2:
        await query.edit_message_text("درخواست کتابخانه یوتیوب نامعتبر است.")
        return

    user_key = get_youtube_library_user_key(update, context)
    if user_key is None:
        await query.answer("شناسه کاربر برای ذخیره‌سازی پیدا نشد.", show_alert=True)
        return

    library_store = get_youtube_library_store(context)
    action = parts[1]

    if action == "showrecent":
        snapshot = library_store.load_user_library(user_key)
        await query.answer()
        await query.edit_message_text(
            render_youtube_recent_searches_text(snapshot),
            reply_markup=build_youtube_recent_searches_keyboard(snapshot),
            disable_web_page_preview=True,
        )
        return

    if action in {"savech", "savepl", "watch", "watchrm"}:
        if len(parts) < 3 or not parts[2]:
            await query.answer("شناسه کارت نامعتبر است.", show_alert=True)
            return
        card_token = parts[2]
        card_context = get_youtube_card_context(context, card_token)
        if card_context is None:
            await query.answer("این کارت منقضی شده است. دوباره جستجو کن.", show_alert=True)
            return
        video_payload = card_context.get("video")
        playlist_payload = card_context.get("playlist")
        watch_later_key = card_context.get("watch_later_key")
        session_token = card_context.get("session_token")
        if not isinstance(video_payload, dict):
            await query.answer("اطلاعات ویدیو ناقص است.", show_alert=True)
            return
        video = deserialize_youtube_video(video_payload)
        if action == "savech":
            channel = build_youtube_channel_from_video(video)
            if channel is None:
                await query.answer("اطلاعات کانال کامل نیست.", show_alert=True)
                return
            added = library_store.save_channel(user_key, serialize_youtube_channel(channel))
            await query.answer("کانال ذخیره شد." if added else "کانال از قبل ذخیره شده بود.")
            return
        if action == "savepl":
            if not isinstance(playlist_payload, dict):
                await query.answer("این کارت پلی‌لیست مشخصی ندارد.", show_alert=True)
                return
            playlist = deserialize_youtube_playlist(playlist_payload)
            added = library_store.save_playlist(user_key, serialize_youtube_playlist(playlist))
            await query.answer("پلی‌لیست ذخیره شد." if added else "پلی‌لیست از قبل ذخیره شده بود.")
            return
        if action == "watch":
            added = library_store.save_watch_later(
                user_key,
                video_payload=video_payload,
                playlist_payload=playlist_payload if isinstance(playlist_payload, dict) else None,
            )
            await query.answer("به Watch Later اضافه شد." if added else "از قبل در Watch Later بود.")
            return
        if not isinstance(watch_later_key, str) or not watch_later_key:
            await query.answer("این کارت از Watch Later باز نشده است.", show_alert=True)
            return
        removed = library_store.remove_watch_later(user_key, watch_later_key)
        if removed and isinstance(session_token, str) and session_token:
            remove_youtube_result_item_by_watch_later_key(
                context,
                session_token=session_token,
                watch_later_key=watch_later_key,
            )
        await query.answer("از Watch Later حذف شد." if removed else "این مورد قبلاً حذف شده بود.")
        return

    if action == "channels" and len(parts) >= 4:
        operation = parts[2]
        if operation == "page":
            page = max(int(parts[3]), 0) if parts[3].isdigit() else 0
            snapshot = library_store.load_user_library(user_key)
            await query.answer()
            await query.edit_message_text(
                render_saved_channels_text(snapshot, page=page),
                reply_markup=build_saved_channels_keyboard(snapshot, page=page),
                disable_web_page_preview=True,
            )
            return
        if operation == "open":
            item_key = parts[3]
            entry = library_store.get_saved_channel(user_key, item_key)
            if entry is None:
                await query.answer("این کانال دیگر در ذخیره‌ها نیست.", show_alert=True)
                return
            channel = deserialize_youtube_channel(entry)
            home_view_token = store_youtube_browser_view(
                context,
                build_youtube_browser_view_payload(
                    kind="channel_home",
                    title=channel.title,
                    channel=channel,
                ),
            )
            set_pending_youtube_channel_search(
                context,
                channel=channel,
                home_view_token=home_view_token,
            )
            await query.answer()
            await send_youtube_browser_panel(
                context,
                chat_id=query.message.chat_id,
                view_token=home_view_token,
            )
            return
        if operation == "delete" and len(parts) >= 5:
            page_text, item_key = parts[3], parts[4]
            page = max(int(page_text), 0) if page_text.isdigit() else 0
            removed = library_store.remove_saved_channel(user_key, item_key)
            snapshot = library_store.load_user_library(user_key)
            channel_count = len(snapshot.get("saved_channels", []))
            max_page = max((channel_count - 1) // YOUTUBE_SEARCH_PAGE_SIZE, 0)
            page = min(page, max_page)
            await query.answer("کانال حذف شد." if removed else "این کانال قبلاً حذف شده بود.")
            await query.edit_message_text(
                render_saved_channels_text(snapshot, page=page),
                reply_markup=build_saved_channels_keyboard(snapshot, page=page),
                disable_web_page_preview=True,
            )
            return

    if action == "playlists" and len(parts) >= 4:
        operation = parts[2]
        if operation == "page":
            page = max(int(parts[3]), 0) if parts[3].isdigit() else 0
            snapshot = library_store.load_user_library(user_key)
            await query.answer()
            await query.edit_message_text(
                render_saved_playlists_text(snapshot, page=page),
                reply_markup=build_saved_playlists_keyboard(snapshot, page=page),
                disable_web_page_preview=True,
            )
            return
        if operation == "open":
            item_key = parts[3]
            entry = library_store.get_saved_playlist(user_key, item_key)
            if entry is None:
                await query.answer("این پلی‌لیست دیگر در ذخیره‌ها نیست.", show_alert=True)
                return
            playlist = deserialize_youtube_playlist(entry)
            searcher: YouTubeSearcher = context.application.bot_data["youtube_searcher"]
            await query.answer("در حال باز کردن پلی‌لیست...")
            try:
                _playlist_meta, videos = await searcher.browse_playlist_videos(playlist.webpage_url)
            except Exception as exc:
                LOGGER.exception("Saved playlist browse failed")
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"خطا در باز کردن پلی‌لیست: {exc}",
                )
                return
            next_view_token = store_youtube_browser_view(
                context,
                build_youtube_browser_view_payload(
                    kind="video_cards",
                    title=f"Playlist · {playlist.title}",
                    videos=videos,
                    playlist=playlist,
                    empty_text="This playlist has no visible videos.",
                ),
            )
            await show_youtube_browser_view(
                context,
                chat_id=query.message.chat_id,
                view_token=next_view_token,
            )
            return
        if operation == "delete" and len(parts) >= 5:
            page_text, item_key = parts[3], parts[4]
            page = max(int(page_text), 0) if page_text.isdigit() else 0
            removed = library_store.remove_saved_playlist(user_key, item_key)
            snapshot = library_store.load_user_library(user_key)
            playlist_count = len(snapshot.get("saved_playlists", []))
            max_page = max((playlist_count - 1) // YOUTUBE_SEARCH_PAGE_SIZE, 0)
            page = min(page, max_page)
            await query.answer("پلی‌لیست حذف شد." if removed else "این پلی‌لیست قبلاً حذف شده بود.")
            await query.edit_message_text(
                render_saved_playlists_text(snapshot, page=page),
                reply_markup=build_saved_playlists_keyboard(snapshot, page=page),
                disable_web_page_preview=True,
            )
            return

    if action == "recent":
        if len(parts) < 3:
            await query.answer("جستجوی اخیر نامعتبر است.", show_alert=True)
            return
        item_key = parts[2]
        entry = library_store.get_recent_search(user_key, item_key)
        if entry is None:
            await query.answer("این جستجو دیگر در لیست اخیر نیست.", show_alert=True)
            return

        search_query_text = str(entry.get("query") or "").strip()
        search_scope = str(entry.get("scope") or YOUTUBE_MODE_SEARCH)
        if not search_query_text:
            await query.answer("عبارت جستجو نامعتبر است.", show_alert=True)
            return

        await query.answer("در حال اجرای دوباره جستجو...")
        status_message = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "در حال جستجو داخل Watch Later..."
                if search_scope == YOUTUBE_MODE_WATCH_LATER_SEARCH
                else "در حال جستجو در یوتیوب..."
            ),
        )
        if search_scope == YOUTUBE_MODE_WATCH_LATER_SEARCH:
            snapshot = library_store.load_user_library(user_key)
            try:
                record_search_activity(
                    context,
                    feature="youtube_watch_later_search",
                    query=search_query_text,
                    update=update,
                    extra={
                        "source": "recent_search",
                    },
                )
                result_items = build_watch_later_result_items(
                    snapshot,
                    query=search_query_text,
                )
            except Exception as exc:
                LOGGER.exception("Recent watch-later search failed")
                await status_message.edit_text(f"خطا در جستجو داخل Watch Later: {exc}")
                return
            if not result_items:
                await status_message.edit_text("داخل Watch Later نتیجه‌ای پیدا نشد.")
                return
            library_store.add_recent_search(
                user_key,
                search_query_text,
                scope=YOUTUBE_MODE_WATCH_LATER_SEARCH,
            )
            try:
                await status_message.delete()
            except TelegramError:
                LOGGER.debug("Failed to delete recent watch-later search status message", exc_info=True)
            await deliver_youtube_search_results(
                context,
                chat_id=query.message.chat_id,
                result_items=result_items,
                control_text=(
                    f"{min(len(result_items), YOUTUBE_SEARCH_PAGE_SIZE)} نتیجه از Watch Later ارسال شد."
                    if len(result_items) > YOUTUBE_SEARCH_PAGE_SIZE
                    else f"{len(result_items)} نتیجه در Watch Later پیدا شد."
                ),
                source=YOUTUBE_RESULT_SOURCE_WATCH_LATER,
            )
            return

        searcher: YouTubeSearcher = context.application.bot_data["youtube_searcher"]
        try:
            record_search_activity(
                context,
                feature="youtube_search",
                query=search_query_text,
                update=update,
                extra={
                    "source": "recent_search",
                },
            )
            videos = await searcher.search(search_query_text)
        except Exception as exc:
            LOGGER.exception("Recent YouTube search failed")
            await status_message.edit_text(f"خطا در جستجوی یوتیوب: {exc}")
            return
        if not videos:
            await status_message.edit_text("نتیجه‌ای برای این جستجو پیدا نشد.")
            return
        library_store.add_recent_search(
            user_key,
            search_query_text,
            scope=YOUTUBE_MODE_SEARCH,
        )
        try:
            await status_message.delete()
        except TelegramError:
            LOGGER.debug("Failed to delete recent search status message", exc_info=True)
        await deliver_youtube_search_results(
            context,
            chat_id=query.message.chat_id,
            result_items=[serialize_youtube_result_item(video) for video in videos],
        )
        return

    if action == "watchopen" and len(parts) >= 3:
        item_key = parts[2]
        entry = library_store.get_watch_later(user_key, item_key)
        if entry is None:
            await query.answer("این ویدیو دیگر در Watch Later نیست.", show_alert=True)
            return
        video_payload = entry.get("video")
        if not isinstance(video_payload, dict):
            await query.answer("اطلاعات ویدیو ناقص است.", show_alert=True)
            return
        playlist_payload = entry.get("playlist")
        playlist = (
            deserialize_youtube_playlist(playlist_payload)
            if isinstance(playlist_payload, dict)
            else None
        )
        await query.answer()
        await send_youtube_result_card(
            context,
            chat_id=query.message.chat_id,
            video=deserialize_youtube_video(video_payload),
            playlist=playlist,
            watch_later_key=item_key,
        )
        return

    await query.answer("عملیات پشتیبانی نمی‌شود.", show_alert=True)


async def youtube_browser_action_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle YouTube browser actions."""
    query = update.callback_query
    if query is None or query.message is None:
        return

    await query.answer()
    if not transport_supports_youtube_browser(context):
        await query.answer("Channel browsing is not available here yet.", show_alert=True)
        return

    payload = query.data or ""
    if not payload.startswith(f"{YOUTUBE_BROWSER_ACTION_CALLBACK_PREFIX}:"):
        await query.edit_message_text("Invalid browser action.")
        return

    try:
        _prefix, action, view_token = payload.split(":", maxsplit=2)
    except ValueError:
        await query.edit_message_text("Invalid browser action.")
        return

    view_payload = get_youtube_browser_view(context, view_token)
    if view_payload is None:
        await query.edit_message_text("This browser view expired. Open it again.")
        return

    if action == "close":
        await clear_youtube_search_messages(context, session_token=view_token)
        clear_pending_youtube_channel_search(context)
        set_current_menu(context, MENU_YOUTUBE)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="YouTube browser closed.",
            reply_markup=get_current_menu_markup(context),
        )
        return

    if action == "back":
        parent_token = view_payload.get("parent_token")
        if isinstance(parent_token, str) and parent_token:
            await clear_youtube_search_messages(context, session_token=view_token)
            parent_view = get_youtube_browser_view(context, parent_token)
            if (
                isinstance(parent_view, dict)
                and parent_view.get("kind") == "channel_home"
                and isinstance(parent_view.get("channel"), dict)
            ):
                set_pending_youtube_channel_search(
                    context,
                    channel=deserialize_youtube_channel(parent_view["channel"]),
                    home_view_token=parent_token,
                )
            await show_youtube_browser_view(
                context,
                chat_id=query.message.chat_id,
                view_token=parent_token,
            )
            return

        await clear_youtube_search_messages(context, session_token=view_token)
        clear_pending_youtube_channel_search(context)
        set_current_menu(context, MENU_YOUTUBE)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Back to the YouTube menu.",
            reply_markup=get_current_menu_markup(context),
        )
        return

    channel_payload = view_payload.get("channel")
    if not isinstance(channel_payload, dict):
        await query.answer("This browser view is missing channel data.", show_alert=True)
        return
    channel = deserialize_youtube_channel(channel_payload)
    searcher: YouTubeSearcher = context.application.bot_data["youtube_searcher"]

    if action == "search":
        set_pending_youtube_channel_search(
            context,
            channel=channel,
            home_view_token=view_token,
        )
        set_current_menu(context, MENU_YOUTUBE_BROWSE)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"برای جستجو داخل کانال {channel.title}، دکمه «{BTN_YOUTUBE_CHANNEL_SEARCH}» "
                "را از کیبورد پایین بزن."
            ),
            reply_markup=get_current_menu_markup(context),
        )
        return

    try:
        if action in {"latest", "popular"}:
            fetched_channel, videos = await searcher.browse_channel_videos(
                channel.browse_url,
                sort=action,
            )
            next_view_token = store_youtube_browser_view(
                context,
                build_youtube_browser_view_payload(
                    kind="video_cards",
                    title=f"{fetched_channel.title} · {'Popular' if action == 'popular' else 'Latest'}",
                    parent_token=view_token,
                    channel=fetched_channel,
                    videos=videos,
                    empty_text="No videos were found in this tab.",
                ),
            )
            set_pending_youtube_channel_search(
                context,
                channel=fetched_channel,
                home_view_token=view_token,
            )
            await clear_youtube_search_messages(context, session_token=view_token)
            await show_youtube_browser_view(
                context,
                chat_id=query.message.chat_id,
                view_token=next_view_token,
            )
            return

        if action == "playlists":
            fetched_channel, playlists = await searcher.browse_channel_playlists(channel.browse_url)
            next_view_token = store_youtube_browser_view(
                context,
                build_youtube_browser_view_payload(
                    kind="playlist_list",
                    title=f"{fetched_channel.title} · Playlists",
                    parent_token=view_token,
                    channel=fetched_channel,
                    playlists=playlists,
                    empty_text="No playlists were found for this channel.",
                ),
            )
            set_pending_youtube_channel_search(
                context,
                channel=fetched_channel,
                home_view_token=view_token,
            )
            await clear_youtube_search_messages(context, session_token=view_token)
            await show_youtube_browser_view(
                context,
                chat_id=query.message.chat_id,
                view_token=next_view_token,
            )
            return
    except Exception as exc:
        LOGGER.exception("YouTube browser action failed")
        await query.answer(f"Browser action failed: {exc}", show_alert=True)


async def youtube_browser_item_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle item selection inside Telegram browser list panels."""
    query = update.callback_query
    if query is None or query.message is None:
        return

    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{YOUTUBE_BROWSER_ITEM_CALLBACK_PREFIX}:"):
        await query.edit_message_text("Invalid browser selection.")
        return

    try:
        _prefix, view_token, index_text = payload.split(":", maxsplit=2)
        index = int(index_text)
    except ValueError:
        await query.edit_message_text("Invalid browser selection.")
        return

    view_payload = get_youtube_browser_view(context, view_token)
    if view_payload is None:
        await query.edit_message_text("This browser view expired. Open it again.")
        return

    kind = str(view_payload.get("kind") or "")
    if kind == "video_list":
        videos = view_payload.get("videos", [])
        if not isinstance(videos, list) or index < 0 or index >= len(videos):
            await query.answer("This video is no longer available.", show_alert=True)
            return
        video = deserialize_youtube_video(videos[index])
        playlist_payload = view_payload.get("playlist")
        playlist = (
            deserialize_youtube_playlist(playlist_payload)
            if isinstance(playlist_payload, dict)
            else None
        )
        channel_payload = view_payload.get("channel")
        if isinstance(channel_payload, dict):
            channel = deserialize_youtube_channel(channel_payload)
            if not video.channel_url:
                video = YouTubeVideo(
                    video_id=video.video_id,
                    title=video.title,
                    duration_seconds=video.duration_seconds,
                    channel=video.channel or channel.title,
                    webpage_url=video.webpage_url,
                    channel_id=video.channel_id or channel.channel_id,
                    channel_url=video.channel_url or channel.channel_url or channel.browse_url,
                    uploader_id=video.uploader_id or channel.uploader_id,
                    uploader_url=video.uploader_url or channel.uploader_url,
                )
        await send_youtube_result_card(
            context,
            chat_id=query.message.chat_id,
            video=video,
            playlist=playlist,
        )
        return

    if kind == "playlist_list":
        playlists = view_payload.get("playlists", [])
        if not isinstance(playlists, list) or index < 0 or index >= len(playlists):
            await query.answer("This playlist is no longer available.", show_alert=True)
            return
        playlist = deserialize_youtube_playlist(playlists[index])
        searcher: YouTubeSearcher = context.application.bot_data["youtube_searcher"]
        try:
            _playlist_meta, videos = await searcher.browse_playlist_videos(playlist.webpage_url)
        except Exception as exc:
            LOGGER.exception("Playlist browse failed")
            await query.answer(f"Playlist browse failed: {exc}", show_alert=True)
            return

        channel_payload = view_payload.get("channel")
        channel = (
            deserialize_youtube_channel(channel_payload)
            if isinstance(channel_payload, dict)
            else None
        )
        next_view_token = store_youtube_browser_view(
            context,
            build_youtube_browser_view_payload(
                kind="video_cards",
                title=f"Playlist · {playlist.title}",
                parent_token=view_token,
                channel=channel,
                videos=videos,
                playlist=playlist,
                empty_text="This playlist has no visible videos.",
            ),
        )
        await clear_youtube_search_messages(context, session_token=view_token)
        await show_youtube_browser_view(
            context,
            chat_id=query.message.chat_id,
            view_token=next_view_token,
        )
        return

    await query.answer("Unsupported browser item.", show_alert=True)


async def youtube_browser_page_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Move between paginated Telegram browser pages."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{YOUTUBE_BROWSER_PAGE_CALLBACK_PREFIX}:"):
        await query.edit_message_text("Invalid page request.")
        return

    try:
        _prefix, view_token, page_text = payload.split(":", maxsplit=2)
        page = max(int(page_text), 0)
    except ValueError:
        await query.edit_message_text("Invalid page request.")
        return

    view_payload = get_youtube_browser_view(context, view_token)
    if view_payload is None:
        await query.edit_message_text("This browser view expired. Open it again.")
        return

    if str(view_payload.get("kind") or "") == "video_cards":
        await clear_youtube_search_messages(context, session_token=view_token)
        await show_youtube_browser_view(
            context,
            chat_id=query.message.chat_id,
            view_token=view_token,
            page=page,
        )
        return

    await edit_youtube_browser_panel(
        update,
        context,
        view_token=view_token,
        page=page,
    )


async def quality_mode_selection_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the YouTube download options for the selected quality mode."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{QUALITY_MODE_CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست کیفیت نامعتبر است.")
        return

    payload_parts = payload.split(":")
    session_token: str | None = None
    if len(payload_parts) == 4:
        _prefix, session_token, video_id, quality_mode = payload_parts
    elif len(payload_parts) == 3:
        _prefix, video_id, quality_mode = payload_parts
    else:
        await query.edit_message_text("درخواست کیفیت نامعتبر است.")
        return

    if quality_mode not in {
        YOUTUBE_QUALITY_MODE_LOW,
        YOUTUBE_QUALITY_MODE_HIGH,
        YOUTUBE_QUALITY_MODE_AUDIO,
    }:
        await query.answer("حالت کیفیت نامعتبر است.", show_alert=True)
        return

    if session_token and query.message is not None:
        await clear_youtube_search_messages(
            context,
            session_token=session_token,
            keep_message_id=query.message.message_id,
        )
        clear_active_youtube_search_state(context)

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise

    status_message = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"در حال بررسی گزینه‌های {get_youtube_quality_mode_title(quality_mode)}...",
    )

    try:
        title, options = await get_youtube_download_options(
            context,
            video_id,
            quality_mode=quality_mode,
        )
    except Exception as exc:
        LOGGER.exception("Quality lookup failed")
        await update_download_status_message(
            status_message,
            f"خطا در بررسی کیفیت‌های ویدیو: {exc}",
        )
        return

    if not options:
        await update_download_status_message(
            status_message,
            "هیچ کیفیت قابل دانلودی برای این حالت پیدا نشد.",
            reply_markup=build_quality_mode_keyboard(video_id),
        )
        return

    store_youtube_quality_options(context, video_id, quality_mode, options)
    await update_download_status_message(
        status_message,
        f"{get_youtube_quality_mode_title(quality_mode)}\n"
        f"{get_youtube_quality_mode_description(quality_mode)}\n\n"
        f"کیفیت موردنظر را انتخاب کن:\n{title}",
        reply_markup=build_quality_keyboard(video_id, options, quality_mode=quality_mode),
    )


async def quality_selection_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Download the selected YouTube video quality and deliver it to the user."""
    downloader: VideoDownloader = context.application.bot_data["video_downloader"]
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{QUALITY_CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست کیفیت نامعتبر است.")
        return

    parts = payload.split(":")
    if len(parts) == 4:
        _prefix, video_id, quality_mode, option_index_text = parts
    elif len(parts) == 3:
        _prefix, video_id, option_index_text = parts
        quality_mode = YOUTUBE_QUALITY_MODE_HIGH
    else:
        await query.edit_message_text("درخواست کیفیت نامعتبر است.")
        return

    try:
        option_index = int(option_index_text)
    except ValueError:
        await query.edit_message_text("درخواست کیفیت نامعتبر است.")
        return

    downloader.set_allow_large_uploads(
        quality_mode == YOUTUBE_QUALITY_MODE_HIGH and transport_supports_large_uploads(context)
    )
    stored_options = get_stored_youtube_quality_options(context, video_id, quality_mode)
    if option_index < 0 or option_index >= len(stored_options):
        await query.edit_message_text("این گزینه دیگر در دسترس نیست. دوباره جستجو کن.")
        return

    selected_option = stored_options[option_index]
    selected_media_kind = selected_option.get("media_kind", MEDIA_KIND_VIDEO)
    selected_media_title = "فایل صوتی" if selected_media_kind == MEDIA_KIND_AUDIO else "ویدیو"
    cancel_token = uuid.uuid4().hex
    cancel_event = threading.Event()
    cancel_keyboard = build_download_cancel_keyboard(cancel_token)
    initial_status_text = (
        f"در حال دانلود و آماده‌سازی {selected_media_title}...\nکیفیت انتخاب‌شده: {selected_option['label']}"
    )
    status_message = await create_download_status_message(
        update,
        context,
        initial_text=initial_status_text,
        reply_markup=cancel_keyboard,
    )
    register_active_youtube_download_job(
        context,
        job_token=cancel_token,
        owner_user_id=query.from_user.id if query.from_user is not None else None,
        controller_task=asyncio.current_task(),
        cancel_event=cancel_event,
    )

    loop = asyncio.get_running_loop()
    progress_state = {
        "last_text": initial_status_text,
        "last_progress_at": loop.time(),
    }

    async def report_download_progress(text: str) -> None:
        if text == progress_state["last_text"]:
            return
        progress_state["last_text"] = text
        progress_state["last_progress_at"] = loop.time()
        try:
            await update_download_status_message(status_message, text)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            raise

    download_task = asyncio.create_task(
        downloader.download_video(
            video_id,
            format_selector=selected_option["format_selector"],
            media_kind=selected_media_kind,
            progress_callback=report_download_progress,
            cancel_event=cancel_event,
        )
    )

    try:
        while True:
            if cancel_event.is_set():
                raise DownloadCancelledError("Download cancelled by user.")
            try:
                result = await asyncio.wait_for(asyncio.shield(download_task), timeout=12)
                break
            except asyncio.TimeoutError:
                if cancel_event.is_set():
                    raise DownloadCancelledError("Download cancelled by user.")
                try:
                    await context.bot.send_chat_action(
                        query.message.chat_id,
                        ChatAction.UPLOAD_DOCUMENT
                        if selected_media_kind == MEDIA_KIND_AUDIO
                        else ChatAction.UPLOAD_VIDEO,
                    )
                except Exception:
                    LOGGER.debug("Failed to send upload heartbeat", exc_info=True)

                if loop.time() - float(progress_state["last_progress_at"]) >= 15:
                    heartbeat_text = (
                        f"{progress_state['last_text']}\n\n"
                        f"هنوز در حال دانلود و آماده‌سازی {selected_media_title} هستم..."
                    )
                    if heartbeat_text != progress_state["last_text"]:
                        try:
                            await update_download_status_message(status_message, heartbeat_text)
                            progress_state["last_text"] = heartbeat_text
                        except BadRequest as exc:
                            if "message is not modified" not in str(exc).lower():
                                raise
            except DownloadCancelledError:
                raise
            except asyncio.CancelledError:
                cancel_event.set()
                raise DownloadCancelledError("Download cancelled by user.")
            except Exception as exc:
                LOGGER.exception("Video callback failed")
                await update_download_status_message(
                    status_message,
                    f"خطا در دانلود {'فایل صوتی' if selected_media_kind == MEDIA_KIND_AUDIO else 'ویدیو'}: {exc}",
                    reply_markup=None,
                )
                return

        if cancel_event.is_set():
            raise DownloadCancelledError("Download cancelled by user.")
        await send_download_result(
            update,
            context,
            result,
            status_message=status_message,
            cancel_event=cancel_event,
        )
    except DownloadCancelledError:
        cancel_event.set()
        if not download_task.done():
            download_task.cancel()
        try:
            await update_download_status_message(
                status_message,
                f"{selected_media_title} لغو شد.",
                reply_markup=None,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        return
    finally:
        pop_active_youtube_download_job(context, cancel_token)


async def youtube_cancel_download_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Cancel an active YouTube download from the inline status message."""
    query = update.callback_query
    if query is None:
        return

    payload = query.data or ""
    if not payload.startswith(f"{YOUTUBE_CANCEL_CALLBACK_PREFIX}:"):
        await query.answer("درخواست نامعتبر است.", show_alert=True)
        return

    job_token = payload.split(":", maxsplit=1)[1]
    job = get_active_youtube_download_jobs(context).get(job_token)
    if job is None:
        await query.answer("این دانلود دیگر فعال نیست.", show_alert=False)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        return

    owner_user_id = job.get("owner_user_id")
    if (
        owner_user_id is not None
        and query.from_user is not None
        and query.from_user.id != owner_user_id
    ):
        await query.answer("فقط همان کاربر می‌تواند این دانلود را لغو کند.", show_alert=True)
        return

    cancel_event = job.get("cancel_event")
    controller_task = job.get("controller_task")
    if isinstance(cancel_event, threading.Event):
        cancel_event.set()
    if isinstance(controller_task, asyncio.Task) and controller_task is not asyncio.current_task():
        controller_task.cancel()

    try:
        await query.edit_message_text("در حال لغو دانلود...", reply_markup=None)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await query.answer("در حال لغو...", show_alert=False)


async def post_init(application: Application) -> None:
    """Set bot command list after startup."""
    await application.bot.set_my_commands(BOT_COMMANDS)


def get_supported_commands() -> list[tuple[str, str]]:
    """Return the bot commands shared across Telegram and Rubika."""
    return list(BOT_COMMANDS)


def configure_application_services(
    application: Application,
    *,
    settings,
    allow_large_uploads: bool,
    local_upload_bot: Bot | None,
    cloud_download_bot: Bot | None,
    local_bot_api_url: str | None,
    use_local_bot_api: bool,
    transport_name: str,
) -> None:
    """Attach shared services and transport capabilities to an application."""
    global KEEP_DOWNLOADED_VIDEOS, LOCAL_BOT_API_SHARED_DOWNLOADS_PATH

    KEEP_DOWNLOADED_VIDEOS = settings.keep_downloaded_videos
    LOCAL_BOT_API_SHARED_DOWNLOADS_PATH = (
        Path(settings.local_bot_api_shared_downloads_path)
        if settings.local_bot_api_shared_downloads_path
        else None
    )

    general_keys = settings.gemini_api_keys[:3] or settings.gemini_api_keys
    developer_keys = settings.gemini_api_keys[3:5] or general_keys

    gemini_general_pool = GeminiClientPool(general_keys)
    gemini_developer_pool = GeminiClientPool(developer_keys)
    LOGGER.info(
        "Gemini pools configured: general_keys=%s developer_keys=%s",
        gemini_general_pool.key_count(),
        gemini_developer_pool.key_count(),
    )

    summarizer = GeminiSummarizer(pool=gemini_general_pool)
    deepseek_client = (
        DeepSeekDeveloperClient(
            base_url=settings.deepseek_api_base_url,
            timeout_seconds=settings.deepseek_api_timeout_seconds,
        )
        if settings.deepseek_api_base_url
        else None
    )
    if deepseek_client is not None:
        LOGGER.info("DeepSeek developer backend configured at %s", deepseek_client.base_url)
    else:
        LOGGER.info("DeepSeek developer backend disabled")
    gemini_chat_manager = GeminiChatManager(
        pool=gemini_general_pool,
        developer_pool=gemini_developer_pool,
        deepseek_client=deepseek_client,
        model_name=settings.gemini_chat_model,
        live_search_enabled=settings.gemini_live_search_enabled,
        live_search_model_name=settings.gemini_live_search_model,
        developer_model_name=settings.gemini_developer_model,
    )
    google_web_search_service = GoogleWebSearchService(
        pool=gemini_general_pool,
        model_names=[settings.gemini_live_search_model, settings.gemini_chat_model],
    )
    news_fetcher = NewsFetcher(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        summarizer=summarizer,
    )
    youtube_searcher = YouTubeSearcher()
    youtube_library_store = YouTubeLibraryStore(YOUTUBE_LIBRARY_STORAGE_PATH)
    video_downloader = VideoDownloader(allow_large_uploads=allow_large_uploads)
    telegram_media_downloader = TelegramMediaDownloader(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        bot_token=settings.telegram_bot_token,
    )
    image_search_service = ImageSearchService()
    vpn_config_collector = TelegramVpnConfigCollector(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
    )

    application.bot_data["news_fetcher"] = news_fetcher
    application.bot_data["youtube_searcher"] = youtube_searcher
    application.bot_data["youtube_library_store"] = youtube_library_store
    application.bot_data["video_downloader"] = video_downloader
    application.bot_data["telegram_media_downloader"] = telegram_media_downloader
    application.bot_data["vpn_config_collector"] = vpn_config_collector
    application.bot_data["gemini_chat_manager"] = gemini_chat_manager
    application.bot_data["google_web_search_service"] = google_web_search_service
    application.bot_data["image_search_service"] = image_search_service
    application.bot_data["gemini_client_pool"] = gemini_general_pool
    application.bot_data["gemini_general_pool"] = gemini_general_pool
    application.bot_data["gemini_developer_pool"] = gemini_developer_pool
    application.bot_data["deepseek_developer_client"] = deepseek_client
    application.bot_data["telegram_admin_chat_id"] = settings.admin_chat_id
    application.bot_data["local_upload_bot"] = local_upload_bot
    application.bot_data["cloud_download_bot"] = cloud_download_bot
    application.bot_data["local_bot_api_url"] = local_bot_api_url
    application.bot_data["use_local_bot_api"] = use_local_bot_api
    application.bot_data["video_compress_max_input_bytes"] = settings.video_compress_max_input_bytes
    application.bot_data["transport_name"] = transport_name
    if transport_name == "telegram":
        owner_ids = (
            (str(settings.admin_chat_id),)
            if settings.admin_chat_id is not None
            else ()
        )
        application.bot_data["telegram_access_control"] = RubikaAccessControl(
            file_path=TELEGRAM_ACCESS_CONTROL_PATH,
            owner_ids=owner_ids,
        )
    if transport_name == "rubika":
        application.bot_data["rubika_access_control"] = RubikaAccessControl(
            owner_ids=settings.rubika_owner_ids,
        )


def add_application_handlers(application: Application) -> None:
    """Register the shared PTB handlers used by both Telegram and Rubika."""
    application.add_handler(TypeHandler(Update, telegram_access_guard), group=-1)
    application.add_handler(TypeHandler(Update, rubika_access_guard), group=-1)
    chat_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("ai", general_ai_command),
            CommandHandler("chat", chat_command),
            MessageHandler(filters.Regex(button_pattern(BTN_NEWS_AI)), chat_command),
            MessageHandler(
                filters.Regex(button_pattern(BTN_GENERAL_AI_START)),
                general_ai_command,
            ),
            MessageHandler(
                filters.Regex(button_pattern(BTN_GENERAL_AI_DEVELOPER)),
                developer_ai_command,
            ),
        ],
        states={
            AI_CHAT: [
                MessageHandler(
                    filters.Regex(button_pattern(BTN_AI_BACK_TO_MENU, BTN_BACK)),
                    ai_chat_back_action,
                ),
                MessageHandler(
                    filters.Regex(
                        button_pattern(
                            BTN_DEVELOPER_MODEL_GEMINI,
                            BTN_DEVELOPER_MODEL_DEEPSEEK_R1,
                            BTN_DEVELOPER_MODEL_DEEPSEEK_R1_SEARCH,
                            BTN_DEVELOPER_MODEL_DEEPSEEK_R1_SILENT,
                            BTN_DEVELOPER_PROMPT_ONLY,
                            BTN_DEVELOPER_WITH_FILES,
                            BTN_DEVELOPER_ADD_FILES,
                            BTN_DEVELOPER_FILES_DONE,
                            BTN_DEVELOPER_FILES_CLEAR,
                        )
                    ),
                    developer_mode_control_action,
                ),
                MessageHandler(filters.PHOTO, developer_photo_message),
                MessageHandler(filters.Document.ALL, developer_document_message),
                MessageHandler(filters.TEXT & ~filters.COMMAND, chat_message),
                CallbackQueryHandler(
                    search_confirmation_callback,
                    pattern=f"^({AI_SEARCH_YES_CALLBACK}|{AI_SEARCH_NO_CALLBACK})$",
                ),
                CallbackQueryHandler(end_chat_callback, pattern=f"^{AI_END_CHAT_CALLBACK}$"),
            ],
        },
        fallbacks=[
            CommandHandler("endchat", end_chat_command),
            CallbackQueryHandler(end_chat_callback, pattern=f"^{AI_END_CHAT_CALLBACK}$"),
        ],
        allow_reentry=True,
    )

    web_search_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(button_pattern(BTN_GENERAL_AI_WEB_SEARCH)),
                web_search_entry,
            ),
        ],
        states={
            WEB_SEARCH_QUERY: [
                CallbackQueryHandler(web_search_page_callback, pattern=f"^{WEB_SEARCH_MORE_CALLBACK}$"),
                MessageHandler(filters.Regex(button_pattern(BTN_BACK)), web_search_back_action),
                MessageHandler(filters.TEXT & ~filters.COMMAND, web_search_query_message),
            ]
        },
        fallbacks=[
            MessageHandler(filters.Regex(button_pattern(BTN_BACK)), web_search_back_action),
        ],
        allow_reentry=True,
    )

    image_search_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(button_pattern(BTN_GENERAL_AI_IMAGE_SEARCH)),
                image_search_entry,
            ),
        ],
        states={
            IMAGE_SEARCH_QUERY: [
                MessageHandler(filters.Regex(button_pattern(BTN_BACK)), image_search_back_action),
                MessageHandler(filters.TEXT & ~filters.COMMAND, image_search_query_message),
            ]
        },
        fallbacks=[
            MessageHandler(filters.Regex(button_pattern(BTN_BACK)), image_search_back_action),
        ],
        allow_reentry=True,
    )

    deepseek_settings_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(button_pattern(BTN_SETTINGS_DEEPSEEK)),
                show_deepseek_settings_menu,
            ),
        ],
        states={
            DEEPSEEK_SETTINGS_INPUT: [
                MessageHandler(
                    filters.Regex(button_pattern(BTN_BACK)),
                    deepseek_settings_message_action,
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    deepseek_settings_message_action,
                ),
            ]
        },
        fallbacks=[
            MessageHandler(
                filters.Regex(button_pattern(BTN_BACK)),
                deepseek_settings_message_action,
            ),
        ],
        allow_reentry=True,
    )

    youtube_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("youtube", youtube_command),
            MessageHandler(filters.Regex(f"^{BTN_YOUTUBE_SEARCH}$"), youtube_command),
            MessageHandler(filters.Regex(f"^{BTN_YOUTUBE_NEW_SEARCH}$"), youtube_command),
            MessageHandler(filters.Regex(f"^{BTN_YOUTUBE_CHANNEL_SEARCH}$"), youtube_channel_search_entry),
            MessageHandler(filters.Regex(f"^{BTN_YOUTUBE_WATCH_LATER_SEARCH}$"), youtube_saved_search_entry),
            MessageHandler(filters.Regex(f"^{BTN_YOUTUBE_DOWNLOAD}$"), youtube_download_entry),
        ],
        states={
            YOUTUBE_QUERY: [
                MessageHandler(filters.Regex(f"^{BTN_BACK}$"), youtube_back_menu_action),
                MessageHandler(filters.TEXT & ~filters.COMMAND, youtube_query_message)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_youtube_command)],
        allow_reentry=True,
    )

    audio_compression_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_AUDIO_COMPRESSOR}$"), audio_compressor_entry),
        ],
        states={
            AUDIO_COMPRESS: [
                MessageHandler(filters.Regex(f"^{BTN_BACK}$"), audio_compressor_back_action),
                MessageHandler(
                    FILTER_AUDIO_UPLOAD,
                    audio_compressor_file_message,
                ),
                MessageHandler(filters.ALL & ~filters.COMMAND, audio_compressor_invalid_message),
            ]
        },
        fallbacks=[
            MessageHandler(filters.Regex(f"^{BTN_BACK}$"), audio_compressor_back_action),
            CommandHandler("cancel", audio_compressor_back_action),
        ],
        allow_reentry=True,
    )

    video_compression_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_VIDEO_COMPRESSOR}$"), video_compressor_entry),
        ],
        states={
            VIDEO_COMPRESS_METHOD: [
                MessageHandler(filters.Regex(f"^{BTN_BACK}$"), video_compressor_back_action),
                MessageHandler(
                    filters.Regex(
                        f"^({BTN_VIDEO_METHOD_CPU}|{BTN_VIDEO_METHOD_GPU}|{BTN_VIDEO_METHOD_PARALLEL})$"
                    ),
                    video_compressor_method_message,
                ),
                MessageHandler(filters.ALL & ~filters.COMMAND, video_compressor_invalid_message),
            ],
            VIDEO_COMPRESS_FILE: [
                MessageHandler(filters.Regex(f"^{BTN_BACK}$"), video_compressor_back_action),
                MessageHandler(
                    FILTER_VIDEO_UPLOAD,
                    video_compressor_file_message,
                ),
                MessageHandler(filters.ALL & ~filters.COMMAND, video_compressor_invalid_file_message),
            ]
        },
        fallbacks=[
            MessageHandler(filters.Regex(f"^{BTN_BACK}$"), video_compressor_back_action),
            CommandHandler("cancel", video_compressor_back_action),
        ],
        allow_reentry=True,
    )

    pdf_compression_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_PDF_COMPRESSOR}$"), pdf_compressor_entry),
        ],
        states={
            PDF_COMPRESS_LEVEL: [
                MessageHandler(filters.Regex(f"^{BTN_BACK}$"), pdf_compressor_back_action),
                MessageHandler(
                    filters.Regex(
                        f"^({BTN_PDF_COMPRESS_LOW}|{BTN_PDF_COMPRESS_MEDIUM}|{BTN_PDF_COMPRESS_HIGH})$"
                    ),
                    pdf_compressor_level_message,
                ),
                MessageHandler(filters.ALL & ~filters.COMMAND, pdf_compressor_invalid_level_message),
            ],
            PDF_COMPRESS: [
                MessageHandler(filters.Regex(f"^{BTN_BACK}$"), pdf_compressor_back_action),
                MessageHandler(FILTER_PDF_UPLOAD, pdf_compressor_file_message),
                MessageHandler(filters.ALL & ~filters.COMMAND, pdf_compressor_invalid_message),
            ]
        },
        fallbacks=[
            MessageHandler(filters.Regex(f"^{BTN_BACK}$"), pdf_compressor_back_action),
            CommandHandler("cancel", pdf_compressor_back_action),
        ],
        allow_reentry=True,
    )

    pdf_splitter_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_PDF_SPLITTER}$"), pdf_splitter_entry),
        ],
        states={
            PDF_SPLIT_FILE: [
                MessageHandler(filters.Regex(f"^{BTN_BACK}$"), pdf_splitter_back_action),
                MessageHandler(FILTER_PDF_UPLOAD, pdf_splitter_file_message),
                MessageHandler(filters.ALL & ~filters.COMMAND, pdf_splitter_invalid_file_message),
            ],
            PDF_SPLIT_RANGE: [
                MessageHandler(filters.Regex(f"^{BTN_BACK}$"), pdf_splitter_back_action),
                MessageHandler(filters.TEXT & ~filters.COMMAND, pdf_splitter_range_message),
                MessageHandler(filters.ALL & ~filters.COMMAND, pdf_splitter_invalid_range_message),
            ],
        },
        fallbacks=[
            MessageHandler(filters.Regex(f"^{BTN_BACK}$"), pdf_splitter_back_action),
            CommandHandler("cancel", pdf_splitter_back_action),
        ],
        allow_reentry=True,
    )

    pdf_merger_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{BTN_PDF_MERGER}$"), pdf_merger_entry),
        ],
        states={
            PDF_MERGE: [
                MessageHandler(filters.Regex(f"^{BTN_BACK}$"), pdf_merger_back_action),
                MessageHandler(filters.Regex(f"^{BTN_PDF_MERGE_RUN}$"), pdf_merger_run_action),
                MessageHandler(FILTER_PDF_UPLOAD, pdf_merger_file_message),
                MessageHandler(filters.ALL & ~filters.COMMAND, pdf_merger_invalid_message),
            ]
        },
        fallbacks=[
            MessageHandler(filters.Regex(f"^{BTN_BACK}$"), pdf_merger_back_action),
            CommandHandler("cancel", pdf_merger_back_action),
        ],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("rubika_access", rubika_access_help_command))
    application.add_handler(CommandHandler("rubika_users", rubika_users_command))
    application.add_handler(CommandHandler("rubika_pending", rubika_pending_command))
    application.add_handler(CommandHandler("rubika_admins", rubika_admins_command))
    application.add_handler(CommandHandler("rubika_allow", rubika_allow_command))
    application.add_handler(CommandHandler("rubika_suspend", rubika_suspend_command))
    application.add_handler(CommandHandler("rubika_block", rubika_block_command))
    application.add_handler(CommandHandler("rubika_admin_add", rubika_admin_add_command))
    application.add_handler(CommandHandler("rubika_admin_remove", rubika_admin_remove_command))
    application.add_handler(CommandHandler("telegram_access", telegram_access_help_command))
    application.add_handler(CommandHandler("telegram_users", telegram_users_command))
    application.add_handler(CommandHandler("telegram_pending", telegram_pending_command))
    application.add_handler(CommandHandler("telegram_admins", telegram_admins_command))
    application.add_handler(CommandHandler("telegram_allow", telegram_allow_command))
    application.add_handler(CommandHandler("telegram_suspend", telegram_suspend_command))
    application.add_handler(CommandHandler("telegram_block", telegram_block_command))
    application.add_handler(CommandHandler("telegram_admin_add", telegram_admin_add_command))
    application.add_handler(CommandHandler("telegram_admin_remove", telegram_admin_remove_command))
    application.add_handler(CommandHandler("news", news_menu_command))
    application.add_handler(CommandHandler("youtube", youtube_menu_command))
    application.add_handler(CommandHandler("tools", file_tools_command))
    application.add_handler(CommandHandler("vpnconfigs", vpn_config_bundle_command))
    application.add_handler(CommandHandler("endchat", end_chat_command))
    application.add_handler(
        MessageHandler(
            filters.Regex(
                button_pattern(
                    BTN_MAIN_NEWS,
                    BTN_MAIN_YOUTUBE,
                    BTN_MAIN_FILE_TOOLS,
                    BTN_MAIN_GENERAL_AI,
                    BTN_MAIN_SUPPORT_BUNDLE,
                    BTN_MAIN_SETTINGS,
                )
            ),
            main_menu_action,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(
                f"^({BTN_REVIEW_APPROVE}|{BTN_REVIEW_REJECT}|{BTN_REVIEW_ROLE_USER}|{BTN_REVIEW_ROLE_ADMIN}|{BTN_REVIEW_CANCEL})$"
            ),
            rubika_join_review_action,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(
                f"^({BTN_SETTINGS_PENDING}|{BTN_SETTINGS_USERS}|{BTN_SETTINGS_ADMINS}|{BTN_SETTINGS_BROADCAST}|{BTN_SETTINGS_RESET})$"
            ),
            rubika_settings_menu_action,
        )
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, rubika_broadcast_message_action),
        group=1,
    )
    application.add_handler(chat_conversation)
    application.add_handler(web_search_conversation)
    application.add_handler(image_search_conversation)
    application.add_handler(deepseek_settings_conversation)
    application.add_handler(youtube_conversation)
    application.add_handler(audio_compression_conversation)
    application.add_handler(video_compression_conversation)
    application.add_handler(pdf_compression_conversation)
    application.add_handler(pdf_splitter_conversation)
    application.add_handler(pdf_merger_conversation)
    application.add_handler(
        MessageHandler(filters.Regex(f"^{BTN_YOUTUBE_MORE_RESULTS}$"), youtube_more_results_action)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{BTN_YOUTUBE_SAVED_CHANNELS}$"), show_youtube_saved_channels)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{BTN_YOUTUBE_PLAYLISTS}$"), show_youtube_saved_playlists)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{BTN_YOUTUBE_WATCH_LATER}$"), show_youtube_watch_later)
    )
    application.add_handler(
        MessageHandler(filters.Regex(f"^{BTN_YOUTUBE_RECENT_SEARCHES}$"), show_youtube_recent_searches)
    )
    application.add_handler(
        MessageHandler(filters.Regex(button_pattern(BTN_VPN_CONFIG_BUNDLE)), file_tools_menu_action)
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(button_pattern(BTN_BACK, BTN_AI_BACK_TO_MENU)),
            back_menu_action,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(
                f"^({BTN_IRAN_INTL}|{BTN_VAHID}|{BTN_RADIO_FARDA}|{BTN_INDY}|{BTN_BBC})$"
            ),
            news_menu_action,
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            rubika_settings_reset_callback,
            pattern=f"^{RUBIKA_SETTINGS_RESET_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            rubika_settings_action_callback,
            pattern=f"^{RUBIKA_SETTINGS_ACTION_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            rubika_settings_user_callback,
            pattern=f"^{RUBIKA_SETTINGS_USER_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            rubika_settings_view_callback,
            pattern=f"^{RUBIKA_SETTINGS_VIEW_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            youtube_cancel_download_callback,
            pattern=f"^{YOUTUBE_CANCEL_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            youtube_search_page_callback,
            pattern=f"^{YOUTUBE_SEARCH_PAGE_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            youtube_browser_page_callback,
            pattern=f"^{YOUTUBE_BROWSER_PAGE_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            youtube_browser_item_callback,
            pattern=f"^{YOUTUBE_BROWSER_ITEM_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            youtube_browser_action_callback,
            pattern=f"^{YOUTUBE_BROWSER_ACTION_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            youtube_open_channel_callback,
            pattern=f"^{YOUTUBE_OPEN_CHANNEL_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            youtube_library_callback,
            pattern=f"^{YOUTUBE_LIBRARY_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            quality_mode_selection_callback,
            pattern=f"^{QUALITY_MODE_CALLBACK_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(quality_selection_callback, pattern=f"^{QUALITY_CALLBACK_PREFIX}:")
    )
    application.add_handler(
        CallbackQueryHandler(
            video_selection_callback,
            pattern=f"^{YOUTUBE_SEARCH_RESULT_CALLBACK_PREFIX}:",
        )
    )
    application.add_error_handler(error_handler)


def build_application() -> Application:
    """Create the Telegram application and shared services."""
    settings = load_settings()
    local_upload_configured = bool(settings.local_bot_api_url and settings.local_bot_api_file_url)
    use_local_bot_api = bool(
        local_upload_configured and is_local_bot_api_reachable(settings.local_bot_api_url)
    )
    local_upload_bot: Bot | None = None
    cloud_download_bot = Bot(token=settings.telegram_bot_token)

    if local_upload_configured:
        local_upload_bot = Bot(
            token=settings.telegram_bot_token,
            base_url=settings.local_bot_api_url,
            base_file_url=settings.local_bot_api_file_url,
            local_mode=True,
        )

    application_builder = Application.builder().token(
        settings.telegram_bot_token
    ).post_init(post_init)

    if use_local_bot_api:
        application_builder = application_builder.base_url(
            settings.local_bot_api_url
        ).base_file_url(settings.local_bot_api_file_url).local_mode(True)

    application = application_builder.build()
    configure_application_services(
        application,
        settings=settings,
        allow_large_uploads=use_local_bot_api,
        local_upload_bot=local_upload_bot,
        cloud_download_bot=cloud_download_bot,
        local_bot_api_url=settings.local_bot_api_url,
        use_local_bot_api=use_local_bot_api,
        transport_name="telegram",
    )
    add_application_handlers(application)
    return application


def build_bridge_application(bridge_bot: Bot) -> Application:
    """Create a PTB application that reuses the bot logic behind a bridge transport."""
    settings = load_settings()
    application = Application.builder().bot(bridge_bot).updater(None).build()
    configure_application_services(
        application,
        settings=settings,
        allow_large_uploads=True,
        local_upload_bot=None,
        cloud_download_bot=None,
        local_bot_api_url=None,
        use_local_bot_api=False,
        transport_name="rubika",
    )
    add_application_handlers(application)
    return application


def main() -> None:
    """Run the bot."""
    configure_logging()
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
