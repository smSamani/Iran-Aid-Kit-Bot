"""Telegram bot entrypoint."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import socket
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
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
    CALLBACK_PREFIX,
    DOWNLOADS_DIR,
    LOCAL_BOT_API_DATA_DIR,
    NEWS_SOURCES,
    TELEGRAM_UPLOAD_LIMIT_BYTES,
    configure_logging,
    load_settings,
)
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
from telegram_media_downloader import TelegramMediaDownloader
from video_downloader import (
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
from youtube_search import YouTubeSearcher, YouTubeVideo


LOGGER = logging.getLogger(__name__)
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
CLOUD_BOT_API_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024
YOUTUBE_QUERY = 1
AI_CHAT = 2
AUDIO_COMPRESS = 3
VIDEO_COMPRESS_METHOD = 4
VIDEO_COMPRESS_FILE = 5
PDF_COMPRESS_LEVEL = 6
PDF_COMPRESS = 7
PDF_SPLIT_FILE = 8
PDF_SPLIT_RANGE = 9
PDF_MERGE = 10
KEEP_DOWNLOADED_VIDEOS = False
LOCAL_BOT_API_SHARED_DOWNLOADS_PATH: Path | None = None
AI_END_CHAT_CALLBACK = "chat:end"
AI_SEARCH_YES_CALLBACK = "chat:search_yes"
AI_SEARCH_NO_CALLBACK = "chat:search_no"
QUALITY_CALLBACK_PREFIX = "ytq"
BTN_IRAN_INTL = "📰 ایران اینترنشنال"
BTN_VAHID = "📰 وحید آنلاین"
BTN_RADIO_FARDA = "📰 رادیو فردا"
BTN_INDY = "📰 ایندیپندنت فارسی"
BTN_BBC = "📰 بی‌بی‌سی فارسی"
BTN_MAIN_NEWS = "📰 اخبار"
BTN_MAIN_YOUTUBE = "📺 یوتیوب"
BTN_MAIN_FILE_TOOLS = "🛠 ابزار فایل"
BTN_MAIN_GENERAL_AI = "🤖 هوش مصنوعی"
BTN_NEWS_AI = "🤖 از AI درباره خبرها بپرس"
BTN_BACK = "🔙 بازگشت"
BTN_YOUTUBE_SEARCH = "🔎 جستجوی ویدیو"
BTN_YOUTUBE_DOWNLOAD = "⬇️ دانلود ویدیو"
BTN_AUDIO_COMPRESSOR = "🎵 فشرده‌سازی صوت"
BTN_VIDEO_COMPRESSOR = "🎬 فشرده‌سازی ویدیو"
BTN_PDF_COMPRESSOR = "🗜️ فشرده‌سازی PDF"
BTN_PDF_SPLITTER = "✂️ تقسیم PDF"
BTN_PDF_MERGER = "🧩 ادغام PDF"
BTN_PDF_MERGE_RUN = "✅ شروع ادغام"
BTN_PDF_COMPRESS_LOW = "🟢 کم"
BTN_PDF_COMPRESS_MEDIUM = "🟡 متوسط"
BTN_PDF_COMPRESS_HIGH = "🔴 زیاد"
BTN_GENERAL_AI_START = "💬 شروع چت"
BTN_AI_BACK_TO_MENU = "🔙 بازگشت به منو"
BTN_VIDEO_METHOD_CPU = "🧠 CPU"
BTN_VIDEO_METHOD_GPU = "⚡ GPU"
BTN_VIDEO_METHOD_PARALLEL = "🚀 Parallel"
MENU_MAIN = "main"
MENU_NEWS = "news"
MENU_YOUTUBE = "youtube"
MENU_FILE_TOOLS = "file_tools"
MENU_GENERAL_AI = "general_ai"
MENU_AI_CHAT = "ai_chat"
YOUTUBE_MODE_SEARCH = "search"
YOUTUBE_MODE_DOWNLOAD = "download"
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

🤖 هوش مصنوعی
چت مستقیم با Gemini از اینجا در دسترس است.

اگر خواستی هنوز می‌توانی از دستورهای /news /youtube /tools /ai /help هم استفاده کنی.
"""


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


def build_main_menu() -> ReplyKeyboardMarkup:
    """Build the main menu keyboard."""
    return build_reply_menu(
        [
            [BTN_MAIN_NEWS, BTN_MAIN_YOUTUBE],
            [BTN_MAIN_FILE_TOOLS, BTN_MAIN_GENERAL_AI],
        ],
        placeholder="یکی از بخش‌ها را انتخاب کن...",
    )


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
            [BTN_BACK],
        ],
        placeholder="یکی از گزینه‌های یوتیوب را انتخاب کن...",
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


def build_general_ai_menu() -> ReplyKeyboardMarkup:
    """Build the general AI submenu keyboard."""
    return build_reply_menu(
        [
            [BTN_GENERAL_AI_START],
            [BTN_BACK],
        ],
        placeholder="یکی از گزینه‌های هوش مصنوعی را انتخاب کن...",
    )


def build_ai_chat_menu() -> ReplyKeyboardMarkup:
    """Build the reply keyboard for an active Gemini chat session."""
    return build_reply_menu(
        [[BTN_AI_BACK_TO_MENU]],
        placeholder="پیام‌ات را برای Gemini بنویس...",
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


def build_search_keyboard(videos: list[YouTubeVideo]) -> InlineKeyboardMarkup:
    """Build the inline keyboard for YouTube search results."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"انتخاب {index}",
                callback_data=f"{CALLBACK_PREFIX}:{video.video_id}",
            )
        ]
        for index, video in enumerate(videos, start=1)
    ]
    return InlineKeyboardMarkup(rows)


def build_quality_keyboard(video_id: str, options: list[DownloadOption]) -> InlineKeyboardMarkup:
    """Build the inline keyboard for quality selection."""
    rows = [
        [
            InlineKeyboardButton(
                text=option.label,
                callback_data=f"{QUALITY_CALLBACK_PREFIX}:{video_id}:{index}",
            )
        ]
        for index, option in enumerate(options)
    ]
    return InlineKeyboardMarkup(rows)


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
    options: list[DownloadOption],
) -> None:
    """Persist quality options for later callback selection."""
    context.user_data.setdefault("youtube_quality_options", {})[video_id] = [
        {
            "format_selector": option.format_selector,
            "label": option.label,
            "media_kind": option.media_kind,
        }
        for option in options
    ]


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


def format_youtube_results(videos: list[YouTubeVideo]) -> str:
    """Render full YouTube results in message text so titles are not truncated."""
    lines = ["نتایج جستجوی یوتیوب:", ""]
    for index, video in enumerate(videos, start=1):
        lines.append(f"{index}. {video.title}")
        lines.append(f"کانال: {video.channel}")
        lines.append(f"مدت: {video.duration_label}")
        lines.append(f"لینک: {video.webpage_url}")
        lines.append("")
    return "\n".join(lines).strip()


def get_menu_markup(menu_name: str) -> ReplyKeyboardMarkup:
    """Return the keyboard markup for a named menu."""
    if menu_name == MENU_NEWS:
        return build_news_menu()
    if menu_name == MENU_YOUTUBE:
        return build_youtube_menu()
    if menu_name == MENU_FILE_TOOLS:
        return build_file_tools_menu()
    if menu_name == MENU_AI_CHAT:
        return build_ai_chat_menu()
    if menu_name == MENU_GENERAL_AI:
        return build_general_ai_menu()
    return build_main_menu()


def get_current_menu_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Read the current menu name from user state."""
    return str(context.user_data.get("menu_name", MENU_MAIN))


def set_current_menu(context: ContextTypes.DEFAULT_TYPE, menu_name: str) -> None:
    """Persist the current menu name in user state."""
    context.user_data["menu_name"] = menu_name


def get_current_menu_markup(context: ContextTypes.DEFAULT_TYPE) -> ReplyKeyboardMarkup:
    """Build the keyboard for the user's current menu."""
    return get_menu_markup(get_current_menu_name(context))


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


def chunk_text(text: str, limit: int = MAX_TELEGRAM_MESSAGE_LENGTH) -> list[str]:
    """Split large Telegram messages on line boundaries when possible."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line

    if current:
        chunks.append(current.rstrip())
    return chunks


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


async def register_user_if_needed(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """No-op in the public release build."""
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
    await update.effective_message.reply_text(START_TEXT, reply_markup=build_main_menu())


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

🤖 /ai
شروع چت مستقیم با Gemini.
"""
    await update.effective_message.reply_text(
        help_text,
        reply_markup=get_current_menu_markup(context),
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
    set_current_menu(context, MENU_MAIN)
    await message.reply_text(text, reply_markup=build_main_menu())


async def show_news_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the news submenu."""
    message = update.effective_message
    if message is None:
        return
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
    set_current_menu(context, MENU_YOUTUBE)
    await message.reply_text(
        "📺 بخش یوتیوب\nگزینه موردنظرت را انتخاب کن.",
        reply_markup=build_youtube_menu(),
    )


async def show_file_tools_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the file tools submenu."""
    message = update.effective_message
    if message is None:
        return
    set_current_menu(context, MENU_FILE_TOOLS)
    await message.reply_text(
        "🛠 بخش ابزار فایل\nابزار موردنظرت را انتخاب کن.",
        reply_markup=build_file_tools_menu(),
    )


async def show_general_ai_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the general AI submenu."""
    message = update.effective_message
    if message is None:
        return
    await general_ai_command(update, context)


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
    context.user_data["youtube_mode"] = YOUTUBE_MODE_SEARCH
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    await message.reply_text(
        "چی واست تو یوتیوب پیدا کنم؟\nمثال: اخبار ایران، آموزش پایتون، AI agents",
        reply_markup=get_current_menu_markup(context),
    )
    return YOUTUBE_QUERY


async def youtube_download_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the YouTube direct download flow from a pasted URL."""
    await register_user_if_needed(update, context)
    set_current_menu(context, MENU_YOUTUBE)
    context.user_data["youtube_mode"] = YOUTUBE_MODE_DOWNLOAD
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    await message.reply_text(
        "لینک یوتیوب را بفرست تا کیفیت‌های قابل دانلود را بهت نشان بدهم.",
        reply_markup=get_current_menu_markup(context),
    )
    return YOUTUBE_QUERY


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
    set_current_menu(context, MENU_AI_CHAT)
    current_menu_markup = get_current_menu_markup(context)

    if not with_news_context:
        await message.reply_text(
            "گفتگو با Gemini شروع شد.\nپیام‌ات را بفرست.\nبرای پایان، «🔙 بازگشت به منو» را بزن.",
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
            reply_markup=build_main_menu(),
        )
        return ConversationHandler.END

    await message.reply_chat_action(ChatAction.TYPING)
    try:
        response = await chat_manager.send_message(
            user.id,
            message.text,
            reference_payloads,
            mode=str(context.user_data.get("ai_chat_mode", "general")),
        )
    except Exception as exc:
        LOGGER.exception("/chat failed")
        await message.reply_text(
            f"خطا در پاسخ AI: {exc}",
            reply_markup=get_current_menu_markup(context),
        )
        return AI_CHAT

    chunks = chunk_text(response.text)
    first_reply_markup = (
        build_search_confirmation_keyboard()
        if response.needs_search_confirmation
        else get_current_menu_markup(context)
    )
    await message.reply_text(chunks[0], disable_web_page_preview=True, reply_markup=first_reply_markup)
    for chunk in chunks[1:]:
        await message.reply_text(
            chunk,
            disable_web_page_preview=True,
            reply_markup=get_current_menu_markup(context),
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
    if message is not None:
        set_current_menu(context, MENU_MAIN)
        await message.reply_text(
            "گفتگو با Gemini تمام شد.\nبه منوی اصلی برگشتی.",
            reply_markup=build_main_menu(),
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
    downloader: VideoDownloader = context.application.bot_data["video_downloader"]
    downloader.set_allow_large_uploads(
        bool(
            context.application.bot_data.get("local_upload_bot") is not None
            and is_local_bot_api_reachable(context.application.bot_data.get("local_bot_api_url"))
        )
    )
    message = update.effective_message
    if message is None or not message.text:
        return YOUTUBE_QUERY

    query = message.text.strip()
    if not query:
        await message.reply_text("عبارت جستجو خالی است. دوباره بنویس.")
        return YOUTUBE_QUERY

    youtube_mode = context.user_data.get("youtube_mode", YOUTUBE_MODE_SEARCH)
    if youtube_mode == YOUTUBE_MODE_DOWNLOAD:
        video_id = extract_youtube_video_id(query)
        if video_id is None:
            await message.reply_text(
                "لینک یوتیوب معتبر نیست.\n"
                "یک لینک مثل `https://www.youtube.com/watch?v=...` یا `https://youtu.be/...` بفرست.",
                parse_mode="Markdown",
                reply_markup=get_current_menu_markup(context),
            )
            return YOUTUBE_QUERY

        await message.reply_chat_action(ChatAction.TYPING)
        status_message = await message.reply_text("در حال بررسی کیفیت‌های قابل دانلود...")
        current_menu_markup = get_current_menu_markup(context)

        try:
            title, options = await downloader.get_download_options(video_id)
        except Exception as exc:
            LOGGER.exception("Direct YouTube quality lookup failed")
            context.user_data.pop("youtube_mode", None)
            await safe_edit_or_reply(
                status_message,
                message,
                f"خطا در بررسی کیفیت‌های ویدیو: {exc}",
                reply_markup=current_menu_markup,
            )
            return ConversationHandler.END

        if not options:
            context.user_data.pop("youtube_mode", None)
            await safe_edit_or_reply(
                status_message,
                message,
                "هیچ کیفیت قابل دانلودی برای این لینک پیدا نشد.",
                reply_markup=current_menu_markup,
            )
            return ConversationHandler.END

        store_youtube_quality_options(context, video_id, options)
        context.user_data.pop("youtube_mode", None)
        await safe_edit_or_reply(
            status_message,
            message,
            f"کیفیت‌های قابل دانلود برای:\n{title}",
            reply_markup=current_menu_markup,
        )
        await message.reply_text(
            "یکی از کیفیت‌های زیر را انتخاب کن:",
            reply_markup=build_quality_keyboard(video_id, options),
        )
        return ConversationHandler.END

    await message.reply_chat_action(ChatAction.TYPING)
    status_message = await message.reply_text("در حال جستجو در یوتیوب...")
    current_menu_markup = get_current_menu_markup(context)

    try:
        videos = await searcher.search(query)
    except Exception as exc:
        LOGGER.exception("/youtube failed")
        context.user_data.pop("youtube_mode", None)
        await safe_edit_or_reply(
            status_message,
            message,
            f"خطا در جستجوی یوتیوب: {exc}",
            reply_markup=current_menu_markup,
        )
        return ConversationHandler.END

    if not videos:
        context.user_data.pop("youtube_mode", None)
        await safe_edit_or_reply(
            status_message,
            message,
            "نتیجه‌ای برای جستجوی شما پیدا نشد.",
            reply_markup=current_menu_markup,
        )
        return ConversationHandler.END

    chunks = chunk_text(format_youtube_results(videos))
    await safe_edit_or_reply(
        status_message,
        message,
        chunks[0],
        disable_web_page_preview=True,
        reply_markup=current_menu_markup,
    )
    await message.reply_text(
        "روی یکی از دکمه های زیر بزن تا ویدیو دانلود شود:",
        reply_markup=build_search_keyboard(videos),
    )
    for chunk in chunks[1:]:
        await message.reply_text(
            chunk,
            disable_web_page_preview=True,
            reply_markup=current_menu_markup,
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


async def back_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return from a submenu to the main menu."""
    await show_main_menu(update, context, text="به منوی اصلی برگشتی.")


async def youtube_back_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return from the YouTube prompt to the main menu and close the conversation."""
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


async def file_tools_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file tools submenu placeholder buttons."""
    message = update.effective_message
    if message is None or not message.text:
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
            text="گفتگو با Gemini تمام شد.\nبه منوی اصلی برگشتی.",
            reply_markup=build_main_menu(),
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
        answer = await chat_manager.execute_internet_search(user.id, pending_query)
    except Exception as exc:
        LOGGER.exception("Internet search callback failed")
        await query.edit_message_text(f"خطا در جستجوی اینترنتی: {exc}")
        return
    finally:
        chat_manager.clear_pending_search(user.id)

    chunks = chunk_text(answer)
    await query.edit_message_text(chunks[0], disable_web_page_preview=True)
    for chunk in chunks[1:]:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=chunk,
            disable_web_page_preview=True,
            reply_markup=get_current_menu_markup(context),
        )


async def send_download_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    result: DownloadedVideo,
) -> None:
    """Send the downloaded video/audio or a fallback link."""
    query = update.callback_query
    if query is None or query.message is None:
        return

    local_upload_bot: Bot | None = context.application.bot_data.get("local_upload_bot")
    local_bot_api_url: str | None = context.application.bot_data.get("local_bot_api_url")
    media_title = "فایل صوتی" if result.media_kind == MEDIA_KIND_AUDIO else "ویدیو"
    is_audio_upload = result.media_kind == MEDIA_KIND_AUDIO
    audio_suffixes = {".m4a", ".mp3", ".aac", ".wav", ".ogg", ".opus"}
    audio_ready_for_send = (
        result.file_path is not None and result.file_path.suffix.lower() in audio_suffixes
    )

    if result.file_path is None:
        cleanup_file(result.file_path)
        await query.edit_message_text(
            f"حجم این {media_title} بیشتر از حد مجاز است. لینک مستقیم:\n"
            f"{result.source_url}",
            disable_web_page_preview=False,
        )
        return

    caption = html.escape(result.title)
    try:
        if result.exceeds_telegram_limit:
            if local_upload_bot is None:
                await query.edit_message_text(
                    f"{media_title} روی دستگاه دانلود شد، اما حجم آن بیشتر از 50MB است و local Bot API برای ارسال در دسترس نیست.\n"
                    "برای فرستادن این کیفیت‌ها باید سرور local Bot API روی localhost:8081 در حال اجرا باشد.\n"
                    f"لینک مستقیم:\n{result.source_url}",
                    disable_web_page_preview=False,
                )
                return

            if not is_local_bot_api_reachable(local_bot_api_url):
                await query.edit_message_text(
                    f"{media_title} روی دستگاه دانلود شد اما سرور local Bot API در دسترس نیست.\n"
                    "بررسی کن که سرور روی localhost:8081 در حال اجرا باشد تا فایل‌های بالای 50MB ارسال شوند.",
                    disable_web_page_preview=True,
                )
                return

            container_visible_path = resolve_local_bot_api_file_path(result.file_path)
            if container_visible_path is None:
                await query.edit_message_text(
                    "سرور محلی Telegram فعال است اما مسیر shared downloads در دسترس نیست.\n"
                    "بهتر است local Bot API را با `./start_local_bot_api.sh` اجرا کنی.\n"
                    "اگر تنظیمات سفارشی داری، مقدار `LOCAL_BOT_API_SHARED_DOWNLOADS_PATH` را بررسی کن.",
                    parse_mode="Markdown",
                )
                return

            try:
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
                await query.edit_message_text(
                    f"{media_title} روی دستگاه دانلود شد اما ارسال حجیم با local Bot API انجام نشد.\n"
                    "بررسی کن که سرور local Bot API روی localhost:8081 در حال اجرا باشد.\n"
                    f"جزئیات خطا: {exc}",
                    disable_web_page_preview=True,
                )
                return
            await query.edit_message_text(
                "فایل صوتی حجیم با سرور محلی Telegram ارسال شد."
                if is_audio_upload
                else "ویدیوی حجیم با سرور محلی Telegram ارسال شد."
            )
            return

        with result.file_path.open("rb") as media_file:
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

    await query.edit_message_text(
        "فایل صوتی دانلود و ارسال شد." if is_audio_upload else "ویدیو دانلود و ارسال شد."
    )


async def video_selection_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show download quality options for the selected YouTube video."""
    downloader: VideoDownloader = context.application.bot_data["video_downloader"]
    downloader.set_allow_large_uploads(
        bool(
            context.application.bot_data.get("local_upload_bot") is not None
            and is_local_bot_api_reachable(context.application.bot_data.get("local_bot_api_url"))
        )
    )
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست نامعتبر است.")
        return

    video_id = payload.split(":", maxsplit=1)[1]
    await query.edit_message_text("در حال بررسی کیفیت‌های قابل دانلود...")

    try:
        title, options = await downloader.get_download_options(video_id)
    except Exception as exc:
        LOGGER.exception("Quality lookup failed")
        await query.edit_message_text(f"خطا در بررسی کیفیت‌های ویدیو: {exc}")
        return

    if not options:
        await query.edit_message_text("هیچ کیفیت قابل دانلودی برای این ویدیو پیدا نشد.")
        return

    store_youtube_quality_options(context, video_id, options)

    await query.edit_message_text(
        f"کیفیت موردنظر را انتخاب کن:\n{title}",
        reply_markup=build_quality_keyboard(video_id, options),
    )


async def quality_selection_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Download the selected YouTube video quality and deliver it to the user."""
    downloader: VideoDownloader = context.application.bot_data["video_downloader"]
    downloader.set_allow_large_uploads(
        bool(
            context.application.bot_data.get("local_upload_bot") is not None
            and is_local_bot_api_reachable(context.application.bot_data.get("local_bot_api_url"))
        )
    )
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{QUALITY_CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست کیفیت نامعتبر است.")
        return

    try:
        _prefix, video_id, option_index_text = payload.split(":", maxsplit=2)
        option_index = int(option_index_text)
    except ValueError:
        await query.edit_message_text("درخواست کیفیت نامعتبر است.")
        return

    stored_options = context.user_data.get("youtube_quality_options", {}).get(video_id, [])
    if option_index < 0 or option_index >= len(stored_options):
        await query.edit_message_text("این گزینه دیگر در دسترس نیست. دوباره جستجو کن.")
        return

    selected_option = stored_options[option_index]
    selected_media_kind = selected_option.get("media_kind", MEDIA_KIND_VIDEO)
    selected_media_title = "فایل صوتی" if selected_media_kind == MEDIA_KIND_AUDIO else "ویدیو"
    await query.edit_message_text(
        f"در حال دانلود و آماده‌سازی {selected_media_title}...\nکیفیت انتخاب‌شده: {selected_option['label']}"
    )

    loop = asyncio.get_running_loop()
    progress_state = {
        "last_text": (
            f"در حال دانلود و آماده‌سازی {selected_media_title}...\n"
            f"کیفیت انتخاب‌شده: {selected_option['label']}"
        ),
        "last_progress_at": loop.time(),
    }

    async def report_download_progress(text: str) -> None:
        if text == progress_state["last_text"]:
            return
        progress_state["last_text"] = text
        progress_state["last_progress_at"] = loop.time()
        try:
            await query.edit_message_text(text)
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
        )
    )

    while True:
        try:
            result = await asyncio.wait_for(asyncio.shield(download_task), timeout=12)
            break
        except asyncio.TimeoutError:
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
                        await query.edit_message_text(heartbeat_text)
                        progress_state["last_text"] = heartbeat_text
                    except BadRequest as exc:
                        if "message is not modified" not in str(exc).lower():
                            raise
        except Exception as exc:
            LOGGER.exception("Video callback failed")
            await query.edit_message_text(
                f"خطا در دانلود {'فایل صوتی' if selected_media_kind == MEDIA_KIND_AUDIO else 'ویدیو'}: {exc}"
            )
            return

    await send_download_result(update, context, result)


async def post_init(application: Application) -> None:
    """Set bot command list after startup."""
    await application.bot.set_my_commands(
        [
            ("start", "بازگشت به منوی اصلی"),
            ("news", "بخش اخبار"),
            ("youtube", "بخش یوتیوب"),
            ("tools", "بخش ابزار فایل"),
            ("ai", "چت مستقیم با Gemini"),
            ("help", "راهنمای ربات"),
        ]
    )


def build_application() -> Application:
    """Create the Telegram application and shared services."""
    global KEEP_DOWNLOADED_VIDEOS, LOCAL_BOT_API_SHARED_DOWNLOADS_PATH
    settings = load_settings()
    KEEP_DOWNLOADED_VIDEOS = settings.keep_downloaded_videos
    LOCAL_BOT_API_SHARED_DOWNLOADS_PATH = (
        Path(settings.local_bot_api_shared_downloads_path)
        if settings.local_bot_api_shared_downloads_path
        else None
    )
    local_upload_configured = bool(settings.local_bot_api_url and settings.local_bot_api_file_url)
    use_local_bot_api = bool(
        local_upload_configured and is_local_bot_api_reachable(settings.local_bot_api_url)
    )
    summarizer = GeminiSummarizer(settings.gemini_api_key)
    gemini_chat_manager = GeminiChatManager(settings.gemini_api_key)
    news_fetcher = NewsFetcher(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        summarizer=summarizer,
    )
    youtube_searcher = YouTubeSearcher()
    video_downloader = VideoDownloader(allow_large_uploads=use_local_bot_api)
    telegram_media_downloader = TelegramMediaDownloader(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        bot_token=settings.telegram_bot_token,
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

    application.bot_data["news_fetcher"] = news_fetcher
    application.bot_data["youtube_searcher"] = youtube_searcher
    application.bot_data["video_downloader"] = video_downloader
    application.bot_data["telegram_media_downloader"] = telegram_media_downloader
    application.bot_data["gemini_chat_manager"] = gemini_chat_manager
    application.bot_data["local_upload_bot"] = local_upload_bot
    application.bot_data["cloud_download_bot"] = cloud_download_bot
    application.bot_data["local_bot_api_url"] = settings.local_bot_api_url
    application.bot_data["use_local_bot_api"] = use_local_bot_api
    application.bot_data["video_compress_max_input_bytes"] = settings.video_compress_max_input_bytes

    chat_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("ai", general_ai_command),
            CommandHandler("chat", chat_command),
            MessageHandler(filters.Regex(f"^{BTN_MAIN_GENERAL_AI}$"), general_ai_command),
            MessageHandler(filters.Regex(f"^{BTN_NEWS_AI}$"), chat_command),
            MessageHandler(filters.Regex(f"^{BTN_GENERAL_AI_START}$"), general_ai_command),
        ],
        states={
            AI_CHAT: [
                MessageHandler(
                    filters.Regex(f"^({BTN_AI_BACK_TO_MENU}|{BTN_BACK})$"),
                    ai_chat_back_action,
                ),
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

    youtube_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("youtube", youtube_command),
            MessageHandler(filters.Regex(f"^{BTN_YOUTUBE_SEARCH}$"), youtube_command),
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
    application.add_handler(CommandHandler("news", news_menu_command))
    application.add_handler(CommandHandler("youtube", youtube_menu_command))
    application.add_handler(CommandHandler("tools", file_tools_command))
    application.add_handler(CommandHandler("endchat", end_chat_command))
    application.add_handler(
        MessageHandler(
            filters.Regex(
                f"^({BTN_MAIN_NEWS}|{BTN_MAIN_YOUTUBE}|{BTN_MAIN_FILE_TOOLS})$"
            ),
            main_menu_action,
        )
    )
    application.add_handler(chat_conversation)
    application.add_handler(youtube_conversation)
    application.add_handler(audio_compression_conversation)
    application.add_handler(video_compression_conversation)
    application.add_handler(pdf_compression_conversation)
    application.add_handler(pdf_splitter_conversation)
    application.add_handler(pdf_merger_conversation)
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_BACK}$"), back_menu_action))
    application.add_handler(
        MessageHandler(
            filters.Regex(
                f"^({BTN_IRAN_INTL}|{BTN_VAHID}|{BTN_RADIO_FARDA}|{BTN_INDY}|{BTN_BBC})$"
            ),
            news_menu_action,
        )
    )
    application.add_handler(
        CallbackQueryHandler(quality_selection_callback, pattern=f"^{QUALITY_CALLBACK_PREFIX}:")
    )
    application.add_handler(CallbackQueryHandler(video_selection_callback, pattern=f"^{CALLBACK_PREFIX}:"))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    """Run the bot."""
    configure_logging()
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
