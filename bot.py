"""Telegram bot entrypoint."""

from __future__ import annotations

import html
import logging
from pathlib import Path

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
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
from config import CALLBACK_PREFIX, NEWS_SOURCES, configure_logging, load_settings
from news_fetcher import NewsFetcher, NewsFetcherAuthorizationError
from video_downloader import DownloadedVideo, VideoDownloader
from youtube_search import YouTubeSearcher, YouTubeVideo


LOGGER = logging.getLogger(__name__)
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
YOUTUBE_QUERY = 1
AI_CHAT = 2
KEEP_DOWNLOADED_VIDEOS = False
LOCAL_BOT_API_SHARED_DOWNLOADS_PATH: Path | None = None
AI_END_CHAT_CALLBACK = "chat:end"
AI_SEARCH_YES_CALLBACK = "chat:search_yes"
AI_SEARCH_NO_CALLBACK = "chat:search_no"
BTN_IRAN_INTL = "📰 ایران اینترنشنال"
BTN_VAHID = "📰 وحید آنلاین"
BTN_RADIO_FARDA = "📰 رادیو فردا"
BTN_INDY = "📰 ایندیپندنت فارسی"
BTN_BBC = "📰 بی‌بی‌سی فارسی"
BTN_YOUTUBE = "▶️ جستجوی یوتیوب"
BTN_CHAT = "🤖 گفتگو با AI"
BTN_END_CHAT = "🔚 پایان گفتگو"
BTN_HELP = "ℹ️ راهنما"

NEWS_BUTTON_SOURCES = {
    BTN_IRAN_INTL: NEWS_SOURCES["iranintl"],
    BTN_VAHID: NEWS_SOURCES["vahid"],
    BTN_RADIO_FARDA: NEWS_SOURCES["radiofarda"],
    BTN_INDY: NEWS_SOURCES["indypersian"],
    BTN_BBC: NEWS_SOURCES["bbcpersian"],
}

START_TEXT = """سلام.

از منو پایین یکی از این کارها را انتخاب کن:

📰 یکی از 5 کانال خبری را انتخاب کن
از هر منبع، 30 پست آخر را در فایل جداگانه جمع می کنم و به صورت گروه بندی شده خلاصه می کنم.

▶️ جستجوی یوتیوب
عبارت جستجو را می گیرم، نتیجه ها را نشان می دهم و ویدیو را برایت می فرستم.

🤖 گفتگو با AI
با Gemini درباره خبرها صحبت می کنی و پاسخ ها بر اساس 200 پست آخر 5 منبع خبری ساخته می شوند.

🔚 پایان گفتگو
چت فعال با AI را ریست می کند.

اگر خواستی هنوز می توانی از دستورهای /news /youtube /chat /endchat /help هم استفاده کنی.
"""


def build_main_menu() -> ReplyKeyboardMarkup:
    """Build a persistent reply-keyboard menu for the bot."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(BTN_IRAN_INTL), KeyboardButton(BTN_VAHID)],
            [KeyboardButton(BTN_RADIO_FARDA), KeyboardButton(BTN_INDY)],
            [KeyboardButton(BTN_BBC), KeyboardButton(BTN_YOUTUBE)],
            [KeyboardButton(BTN_CHAT), KeyboardButton(BTN_END_CHAT)],
            [KeyboardButton(BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="یک گزینه را انتخاب کن یا دستور بنویس...",
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


def format_news_message(summaries: list[str]) -> str:
    """Render summarized news items for Telegram."""
    lines = ["📰 Latest News", ""]
    lines.extend(summaries)
    return "\n".join(lines)


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


async def safe_edit_or_reply(
    status_message,
    fallback_message,
    text: str,
    *,
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
) -> None:
    """Edit a status message when possible, otherwise send a new reply."""
    try:
        await status_message.edit_text(
            text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
    except BadRequest as exc:
        if "can't be edited" not in str(exc).lower():
            raise
        await fallback_message.reply_text(
            text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            reply_markup=build_main_menu(),
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start."""
    await update.effective_message.reply_text(START_TEXT, reply_markup=build_main_menu())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help."""
    help_text = """راهنمای ربات

📰 /news
به طور پیش فرض آخرین 30 پست ایران اینترنشنال را جمع آوری و گروه بندی می کند.

▶️ /youtube
ابتدا موضوع را می پرسد، بعد نتیجه ها را نشان می دهد.

🤖 /chat
گفتگو با Gemini را شروع می کند و 200 پست آخر 5 منبع خبری را به عنوان مرجع آماده می کند.

🔚 /endchat
گفتگوی فعال را تمام می کند.
"""
    await update.effective_message.reply_text(help_text, reply_markup=build_main_menu())


async def news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source_key: str = "iranintl",
) -> None:
    """Handle source-specific grouped news fetching."""
    fetcher: NewsFetcher = context.application.bot_data["news_fetcher"]
    message = update.effective_message
    if message is None:
        return
    source = NEWS_SOURCES[source_key]

    await message.reply_chat_action(ChatAction.TYPING)
    status_message = await message.reply_text(
        f"در حال جمع آوری 30 پست آخر از {source.display_name} و ساخت خلاصه گروه بندی شده..."
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
        )
        return
    except Exception as exc:
        LOGGER.exception("/news failed")
        await safe_edit_or_reply(status_message, message, f"خطا در دریافت خبرها: {exc}")
        return

    if items.post_count == 0:
        await safe_edit_or_reply(
            status_message,
            message,
            f"هیچ متن یا کپشن جدیدی در 30 پست آخر {source.display_name} پیدا نشد.",
        )
        return

    summary_text = items.grouped_summary
    chunks = chunk_text(summary_text)
    await safe_edit_or_reply(
        status_message,
        message,
        chunks[0],
        disable_web_page_preview=True,
    )
    for chunk in chunks[1:]:
        await message.reply_text(
            chunk,
            disable_web_page_preview=True,
            reply_markup=build_main_menu(),
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


async def youtube_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the two-step YouTube search flow."""
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    await message.reply_text(
        "چی واست تو یوتیوب پیدا کنم؟\nمثال: اخبار ایران، آموزش پایتون، AI agents",
        reply_markup=build_main_menu(),
    )
    return YOUTUBE_QUERY


async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start a Gemini chat session."""
    chat_manager: GeminiChatManager = context.application.bot_data["gemini_chat_manager"]
    fetcher: NewsFetcher = context.application.bot_data["news_fetcher"]
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return ConversationHandler.END

    chat_manager.reset_session(user.id)
    context.user_data["ai_chat_active"] = True
    status_message = await message.reply_text("در حال آماده سازی 5 منبع خبری برای گفتگو...")
    try:
        reference_files = await fetcher.refresh_reference_files()
    except Exception as exc:
        LOGGER.exception("Failed to refresh AI reference files")
        await safe_edit_or_reply(status_message, message, f"خطا در آماده سازی منابع خبری: {exc}")
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
        "گفتگو با Gemini شروع شد.\nپیام‌ات را بفرست.\nبرای پایان از /endchat یا دکمه «🔚 پایان گفتگو» استفاده کن.",
    )
    return AI_CHAT


async def chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle free-form chat messages for the AI conversation."""
    chat_manager: GeminiChatManager = context.application.bot_data["gemini_chat_manager"]
    reference_payloads = load_ai_reference_payloads(context)
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not message.text:
        return AI_CHAT

    if not context.user_data.get("ai_chat_active", False):
        await message.reply_text(
            "گفتگوی AI تمام شده است. از منوی اصلی دوباره گزینه دلخواهت را انتخاب کن.",
            reply_markup=build_main_menu(),
        )
        return ConversationHandler.END

    await message.reply_chat_action(ChatAction.TYPING)
    try:
        response = await chat_manager.send_message(user.id, message.text, reference_payloads)
    except Exception as exc:
        LOGGER.exception("/chat failed")
        await message.reply_text(f"خطا در پاسخ AI: {exc}")
        return AI_CHAT

    chunks = chunk_text(response.text)
    reply_markup = (
        build_search_confirmation_keyboard()
        if response.needs_search_confirmation
        else build_end_chat_inline_keyboard()
    )
    await message.reply_text(
        chunks[0],
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )
    for chunk in chunks[1:]:
        await message.reply_text(
            chunk,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
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
    if message is not None:
        await message.reply_text(
            "گفتگو با Gemini تمام شد.\nبه منوی اصلی برگشتی.",
            reply_markup=build_main_menu(),
        )
        await message.reply_text(START_TEXT, reply_markup=build_main_menu())
    return ConversationHandler.END


async def youtube_query_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Search YouTube after collecting the user's free-text query."""
    searcher: YouTubeSearcher = context.application.bot_data["youtube_searcher"]
    message = update.effective_message
    if message is None or not message.text:
        return YOUTUBE_QUERY

    query = message.text.strip()
    if not query:
        await message.reply_text("عبارت جستجو خالی است. دوباره بنویس.")
        return YOUTUBE_QUERY

    await message.reply_chat_action(ChatAction.TYPING)
    status_message = await message.reply_text("در حال جستجو در یوتیوب...")

    try:
        videos = await searcher.search(query)
    except Exception as exc:
        LOGGER.exception("/youtube failed")
        await safe_edit_or_reply(status_message, message, f"خطا در جستجوی یوتیوب: {exc}")
        return ConversationHandler.END

    if not videos:
        await safe_edit_or_reply(status_message, message, "نتیجه ای برای جستجوی شما پیدا نشد.")
        return ConversationHandler.END

    chunks = chunk_text(format_youtube_results(videos))
    await safe_edit_or_reply(
        status_message,
        message,
        chunks[0],
        disable_web_page_preview=True,
    )
    await message.reply_text(
        "روی یکی از دکمه های زیر بزن تا ویدیو دانلود شود:",
        reply_markup=build_search_keyboard(videos),
    )
    for chunk in chunks[1:]:
        await message.reply_text(
            chunk,
            disable_web_page_preview=True,
            reply_markup=build_main_menu(),
        )
    return ConversationHandler.END


async def cancel_youtube_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel the active YouTube search prompt."""
    message = update.effective_message
    if message is not None:
        await message.reply_text("جستجوی یوتیوب لغو شد.", reply_markup=build_main_menu())
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unexpected bot errors and notify the user when possible."""
    LOGGER.exception("Unhandled bot error", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message is not None:
        await update.effective_message.reply_text(
            "یک خطای موقت رخ داد. دوباره تلاش کن.",
            reply_markup=build_main_menu(),
        )


async def news_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle source-specific news menu buttons."""
    message = update.effective_message
    if message is None or not message.text:
        return

    source = NEWS_BUTTON_SOURCES.get(message.text)
    if source is None:
        return
    await news_command(update, context, source_key=source.key)


async def end_chat_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the end-chat menu button."""
    return await end_chat_command(update, context)


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
    await query.answer("گفتگو تمام شد.")
    await query.edit_message_reply_markup(reply_markup=None)
    if query.message is not None:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="گفتگو با Gemini تمام شد.\nبه منوی اصلی برگشتی.",
            reply_markup=build_main_menu(),
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=START_TEXT,
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
            reply_markup=build_end_chat_inline_keyboard(),
        )


async def send_download_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    result: DownloadedVideo,
) -> None:
    """Send the downloaded video or a fallback link."""
    query = update.callback_query
    if query is None or query.message is None:
        return

    local_upload_bot: Bot | None = context.application.bot_data.get("local_upload_bot")

    if result.file_path is None:
        cleanup_file(result.file_path)
        await query.edit_message_text(
            "حجم این ویدیو بیشتر از 50MB است. لینک مستقیم:\n"
            f"{result.source_url}",
            disable_web_page_preview=False,
        )
        return

    caption = html.escape(result.title)
    try:
        if result.exceeds_telegram_limit:
            if local_upload_bot is None:
                await query.edit_message_text(
                    "حجم این ویدیو بیشتر از 50MB است و سرور محلی Bot API تنظیم نشده است.\n"
                    f"لینک مستقیم:\n{result.source_url}",
                    disable_web_page_preview=False,
                )
                return

            container_visible_path = resolve_local_bot_api_file_path(result.file_path)
            if container_visible_path is None:
                await query.edit_message_text(
                    "سرور محلی Telegram فعال است اما مسیر دانلود مشترک برای Docker تنظیم نشده است.\n"
                    "متغیر `LOCAL_BOT_API_SHARED_DOWNLOADS_PATH` را تنظیم کن.",
                    parse_mode="Markdown",
                )
                return

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
            await query.edit_message_text("ویدیوی حجیم با سرور محلی Telegram ارسال شد.")
            return

        with result.file_path.open("rb") as video_file:
            await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)
            await context.bot.send_video(
                chat_id=query.message.chat_id,
                video=video_file,
                caption=caption,
                supports_streaming=True,
            )
    finally:
        cleanup_file(result.file_path)

    await query.edit_message_text("ویدیو دانلود و ارسال شد.")


async def video_selection_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Download the selected YouTube video and deliver it to the user."""
    downloader: VideoDownloader = context.application.bot_data["video_downloader"]
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    payload = query.data or ""
    if not payload.startswith(f"{CALLBACK_PREFIX}:"):
        await query.edit_message_text("درخواست نامعتبر است.")
        return

    video_id = payload.split(":", maxsplit=1)[1]
    await query.edit_message_text("در حال دانلود و آماده سازی ویدیو...")

    try:
        result = await downloader.download_video(video_id)
    except Exception as exc:
        LOGGER.exception("Video callback failed")
        await query.edit_message_text(f"خطا در دانلود ویدیو: {exc}")
        return

    await send_download_result(update, context, result)


async def post_init(application: Application) -> None:
    """Set bot command list after startup."""
    await application.bot.set_my_commands(
        [
            ("start", "بازگشت به منوی اصلی"),
            ("news", "خلاصه ایران اینترنشنال"),
            ("youtube", "جستجو و دانلود از یوتیوب"),
            ("chat", "گفتگو با AI خبری"),
            ("endchat", "خروج از گفتگوی AI"),
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
    summarizer = GeminiSummarizer(settings.gemini_api_key)
    gemini_chat_manager = GeminiChatManager(settings.gemini_api_key)
    news_fetcher = NewsFetcher(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        summarizer=summarizer,
    )
    youtube_searcher = YouTubeSearcher()
    video_downloader = VideoDownloader(
        allow_large_uploads=bool(settings.local_bot_api_url and settings.local_bot_api_file_url)
    )
    local_upload_bot: Bot | None = None

    if settings.local_bot_api_url and settings.local_bot_api_file_url:
        local_upload_bot = Bot(
            token=settings.telegram_bot_token,
            base_url=settings.local_bot_api_url,
            base_file_url=settings.local_bot_api_file_url,
            local_mode=True,
        )

    application_builder = Application.builder().token(
        settings.telegram_bot_token
    ).post_init(post_init)

    if settings.local_bot_api_url and settings.local_bot_api_file_url:
        application_builder = application_builder.base_url(
            settings.local_bot_api_url
        ).base_file_url(settings.local_bot_api_file_url).local_mode(True)

    application = application_builder.build()

    application.bot_data["news_fetcher"] = news_fetcher
    application.bot_data["youtube_searcher"] = youtube_searcher
    application.bot_data["video_downloader"] = video_downloader
    application.bot_data["gemini_chat_manager"] = gemini_chat_manager
    application.bot_data["local_upload_bot"] = local_upload_bot

    chat_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("chat", chat_command),
            MessageHandler(filters.Regex(f"^{BTN_CHAT}$"), chat_command),
        ],
        states={
            AI_CHAT: [
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
            MessageHandler(filters.Regex(f"^{BTN_END_CHAT}$"), end_chat_menu_action),
            CallbackQueryHandler(end_chat_callback, pattern=f"^{AI_END_CHAT_CALLBACK}$"),
        ],
        allow_reentry=True,
    )

    youtube_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("youtube", youtube_command),
            MessageHandler(filters.Regex(f"^{BTN_YOUTUBE}$"), youtube_command),
        ],
        states={
            YOUTUBE_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, youtube_query_message)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_youtube_command)],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("news", news_command))
    application.add_handler(CommandHandler("endchat", end_chat_command))
    application.add_handler(chat_conversation)
    application.add_handler(youtube_conversation)
    application.add_handler(
        MessageHandler(
            filters.Regex(
                f"^({BTN_IRAN_INTL}|{BTN_VAHID}|{BTN_RADIO_FARDA}|{BTN_INDY}|{BTN_BBC})$"
            ),
            news_menu_action,
        )
    )
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_END_CHAT}$"), end_chat_menu_action))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_HELP}$"), help_command))
    application.add_handler(CallbackQueryHandler(video_selection_callback))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    """Run the bot."""
    configure_logging()
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
