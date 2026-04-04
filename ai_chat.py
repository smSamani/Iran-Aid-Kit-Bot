"""AI-powered conversational chat support."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx

from google.genai import types

from config import NEWS_SOURCES
from deepseek_client import DeepSeekDeveloperClient
from gemini_pool import GeminiClientPool

LOGGER = logging.getLogger(__name__)
GENERAL_CHAT_HISTORY_WINDOW = 5
NEWS_CHAT_HISTORY_WINDOW = 5
STORED_CHAT_TURNS = 10
TEHRAN_TIMEZONE = ZoneInfo("Asia/Tehran")
PERSIAN_WEEKDAYS = (
    "دوشنبه",
    "سه‌شنبه",
    "چهارشنبه",
    "پنجشنبه",
    "جمعه",
    "شنبه",
    "یکشنبه",
)
PERSIAN_MONTHS = (
    "",
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
)

CHAT_SYSTEM_PROMPT = """You are a helpful Telegram bot assistant focused on news and analysis.

General behavior:
- Answer clearly and naturally.
- If the user writes in Persian or Finglish, answer in Persian script.
- If the user writes in English, answer in English.
- Keep responses concise unless the user asks for detail.
- Optimize all answers for Telegram readability on mobile screens.
- Prefer short paragraphs, short lines, and visually scannable formatting.
- Do not use markdown syntax such as **bold**, __italic__, bullet markers like "*", or code fences unless the user explicitly asks for them.
- Use plain text with emoji section labels instead.
- When the topic is time-sensitive, include explicit absolute dates and times instead of relying only on words like today, tomorrow, now, or latest.
- Do not invent facts, dates, quotes, or source claims.
- If current source material is not available in the conversation context, say that clearly.

News behavior:
- When the user asks for news, prioritize these sources:
  1. Iran International: https://www.iranintl.com/
  2. Radio Farda: https://www.radiofarda.com/
  3. BBC Persian: https://www.bbc.com/persian
  4. Truth Social account: https://truthsocial.com/@realDonaldTrump
- Treat these as preferred first-hand or primary reference points for this bot's news summaries.
- If the user asks what is happening now, summarize the most important developments first.
- Highlight the biggest events, decisions, statements, escalations, and follow-up angles.
- Present news in a readable structure with light emoji markers such as:
  📰 headline
  📍 why it matters
  🗓 date/time
  🔗 source/reference
  👀 follow-up
- For Telegram, format news like this style:

📰 جمع‌بندی کوتاه
یک یا دو جمله خیلی کوتاه درباره وضعیت کلی

1) تیتر اول
📍 توضیح کوتاه
🗓 تاریخ یا زمان
🔗 منبع

2) تیتر دوم
📍 توضیح کوتاه
🗓 تاریخ یا زمان
🔗 منبع

👀 اگر خواستی:
- جزئیات بیشتر درباره هر مورد
- مقایسه روایت رسانه‌ها
- timeline اتفاقات
- fact-check یک ادعا

- If there is no confirmed event, say that in the first line very directly and simply.
- Avoid long disclaimers at the top.
- Avoid repeating the same caution more than once.
- Put the most useful answer first, then caveats.
- For each important item, include:
  - a short headline
  - 1-3 lines of explanation
  - explicit date or timeframe when available
  - source name
  - reference link if available in context
- End multi-item news answers with a short follow-up section telling the user what they can ask next, for example:
  - deeper detail on one item
  - comparison across sources
  - timeline of events
  - fact-check of a claim

Reliability rules:
- Separate verified reporting from analysis.
- If sources disagree, say that clearly.
- If information is incomplete, say it is incomplete.
- Never pretend you fetched live articles if you did not actually receive source content or retrieval results."""

GENERAL_CHAT_SYSTEM_PROMPT = """You are a helpful Telegram bot assistant.

General behavior:
- Answer clearly and naturally.
- If the user writes in Persian or Finglish, answer in Persian script.
- If the user writes in English, answer in English.
- Keep responses concise unless the user asks for detail.
- Optimize answers for Telegram readability on mobile screens.
- Prefer short paragraphs, short lines, and simple wording.
- Use light emoji section labels when helpful.
- Keep the answer visually clean inside a Telegram chat bubble.
- Do not use markdown syntax unless the user explicitly asks for it.
- For time-sensitive information, include explicit absolute dates and times when relevant.
- Avoid answering current-fact questions with only vague relative wording.
- Do not force news structure, source citations, or search-confirmation behavior.
- If you do not know something, say so simply instead of inventing facts."""

GENERAL_CHAT_LIVE_SEARCH_SYSTEM_PROMPT = """You are a helpful Telegram bot assistant.

General behavior:
- Answer clearly and naturally.
- If the user writes in Persian or Finglish, answer in Persian script.
- If the user writes in English, answer in English.
- Keep responses concise unless the user asks for detail.
- Optimize answers for Telegram readability on mobile screens.
- Prefer short paragraphs, short lines, and simple wording.
- Use light emoji section labels when helpful.
- Keep the answer visually clean inside a Telegram chat bubble.
- Do not use markdown syntax unless the user explicitly asks for it.
- If you do not know something, say so simply instead of inventing facts.

Live search behavior:
- Google Search grounding is available.
- For questions about current events, recent changes, live facts, prices, laws, product availability, releases, or anything that may have changed, use grounded Google Search results when helpful.
- Prefer recent sources when the topic is time-sensitive.
- For time-sensitive answers, include explicit absolute dates and times with timezone when relevant.
- Do not rely only on relative wording such as today, tomorrow, now, current, or latest without anchoring it to a real date or time.
- If live search results are used, keep the answer direct and practical.
- Do not dump long raw URLs into the main body of the answer.
- Keep source mentions compact and clean.
- For weather or forecast questions, use grounded live search automatically, prefer weather.com when available among grounded results, and answer in a clean structure like:
  🌦 عنوان کوتاه
  📍 location
  🗓 زمان بررسی یا زمان پیش‌بینی با تاریخ دقیق
  🌡 وضعیت و دما
  💨 باد
  💧 بارش/رطوبت اگر موجود بود
  👀 جمع‌بندی کوتاه."""

DEVELOPER_CHAT_SYSTEM_PROMPT = """You are an expert software engineer and programming assistant.

General behavior:
- Answer like a strong senior developer.
- If the user writes in Persian or Finglish, answer in Persian script.
- If the user writes in English, answer in English.
- Be technical, practical, and concise.
- Prefer actionable answers over theory.
- Think carefully through requirements, edge cases, dependencies, and failure modes before answering.
- The answer is shown inside a messenger chat, so keep it readable on mobile screens.
- Prefer short sections, short paragraphs, and clean structure inside a chat bubble.
- Choose the shortest complete answer that solves the user's need.
- Use roughly 1 short chat message when that is enough, 2 when more explanation materially helps, and 3 only when truly needed.
- When generating code, aim for something runnable, coherent, and production-leaning rather than pseudo-code.
- If assumptions are needed, choose pragmatic defaults and mention them briefly in the user-facing message.
- Generate a file only when it materially improves usefulness over a normal chat reply.
- If the user would benefit from sending a source file, screenshot, PDF, CSV, or Excel document for a better answer, ask for it briefly and clearly.
- Do not wrap code in markdown fences when returning file content.
- Keep the user-facing message practical and explain what the file is for and how to use it.
- If the request is conceptual and does not need a file, answer directly without creating one."""

DEVELOPER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "create_file": {"type": "boolean"},
        "request_files": {"type": "boolean"},
        "filename": {"type": "string"},
        "language": {"type": "string"},
        "code": {"type": "string"},
    },
    "required": ["message", "create_file"],
}

DEEPSEEK_DEVELOPER_SYSTEM_PROMPT = (
    DEVELOPER_CHAT_SYSTEM_PROMPT
    + "\n\n"
    + "Output contract:\n"
    + "- Return ONLY valid JSON.\n"
    + '- Use exactly these keys: "message", "create_file", "request_files", "filename", "language", "code".\n'
    + "- message must always be present.\n"
    + "- Optimize message for a mobile messenger chat.\n"
    + "- Prefer 1 short message when possible, 2 when useful, and 3 only when truly necessary.\n"
    + "- create_file must be true or false.\n"
    + "- request_files must be true or false.\n"
    + "- If request_files is true, explain briefly in message what file(s) the user should send.\n"
    + "- Do not set both create_file and request_files to true unless there is a very strong reason.\n"
    + "- If create_file is false, omit filename/language/code or leave them empty.\n"
    + "- If create_file is true, filename and code must both be present.\n"
    + "- Do not wrap the JSON in markdown fences.\n"
)

WEATHER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "location": {"type": "string"},
        "checked_at": {"type": "string"},
        "condition": {"type": "string"},
        "today_high": {"type": "string"},
        "today_low": {"type": "string"},
        "notable_change": {"type": "string"},
        "wind": {"type": "string"},
        "humidity_precipitation": {"type": "string"},
        "hourly_forecast": {"type": "array", "items": {"type": "string"}},
        "daily_forecast": {"type": "array", "items": {"type": "string"}},
        "short_outlook": {"type": "string"},
    },
    "required": [
        "title",
        "location",
        "checked_at",
        "condition",
        "today_high",
        "today_low",
        "wind",
        "short_outlook",
    ],
}


SOURCE_ALIAS_MAP = {
    "iranintl": ("iran international", "iranintl", "ایران اینترنشنال"),
    "vahid": ("vahid", "vahid online", "وحید", "وحید آنلاین"),
    "radiofarda": ("radio farda", "radiofarda", "رادیو فردا"),
    "indypersian": ("independent farsi", "indypersian", "ایندیپندنت فارسی", "ایندی"),
    "bbcpersian": ("bbc", "bbc persian", "bbcpersian", "بی‌بی‌سی", "بی بی سی", "بی‌بی‌سی فارسی"),
}

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u0600-\u06FF]+")
STOP_WORDS = {
    "the",
    "and",
    "for",
    "this",
    "that",
    "what",
    "when",
    "where",
    "about",
    "with",
    "from",
    "خبر",
    "اخبار",
    "چی",
    "چه",
    "درباره",
    "این",
    "آن",
    "که",
    "را",
    "از",
    "در",
    "با",
    "برای",
}

WEATHER_QUERY_PATTERN = re.compile(
    r"(weather|forecast|temperature|temp|humidity|wind|rain|snow|storm|air quality|"
    r"ab\s*o\s*hava|hava|dama|baran|barf|bad|ratubat|"
    r"آب[‌ ]?و[‌ ]?هوا|هواشناسی|دما|باران|برف|باد|رطوبت|پیش[‌ ]?بینی)",
    re.IGNORECASE,
)
TIME_SENSITIVE_QUERY_PATTERN = re.compile(
    r"(now|current|today|tonight|tomorrow|latest|recent|price|rate|time|date|"
    r"الان|امروز|امشب|فردا|آخرین|جدیدترین|قیمت|نرخ|ساعت|تاریخ)",
    re.IGNORECASE,
)
SEARCH_RESULTS_QUERY_PATTERN = re.compile(
    r"(search|google|results?|site|website|websites|link|links|download|downloads|source|sources|"
    r"جستجو|نتیجه|نتایج|سایت|سایت‌ها|سایتهای|لینک|لینک‌ها|لینکهای|دانلود|منبع|منابع|بگرد)",
    re.IGNORECASE,
)
IRANIAN_SITE_QUERY_PATTERN = re.compile(
    r"(ایرانی|ایرانی|iranian|persian|farsi|فارسی)",
    re.IGNORECASE,
)
FINGLISH_PERSIAN_PATTERN = re.compile(
    r"\b(chi|che|baram|begard|bede|kon|sait|saito|link|linko|irani|farsi|bebin|mikhad|mikham)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class NewsExcerpt:
    """Relevant excerpt retrieved from a source file."""

    source_key: str
    source_name: str
    date: str
    time: str
    link: str
    text: str
    score: int


@dataclass(slots=True)
class ChatResponse:
    """Structured response for chat handlers."""

    text: str
    needs_search_confirmation: bool = False
    extra_texts: list[str] = field(default_factory=list)
    request_files: bool = False
    artifact_filename: str | None = None
    artifact_text: str | None = None
    model_name: str | None = None


class GeminiChatManager:
    """Manage per-user chat sessions across Gemini and Developer backends."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = "gemini-flash-latest",
        *,
        pool: GeminiClientPool | None = None,
        developer_pool: GeminiClientPool | None = None,
        deepseek_client: DeepSeekDeveloperClient | None = None,
        live_search_enabled: bool = True,
        live_search_model_name: str = "gemini-flash-latest",
        developer_model_name: str = "gemini-pro-latest",
    ) -> None:
        self._pool = pool or GeminiClientPool([api_key or ""])
        self._developer_pool = developer_pool or self._pool
        self._deepseek_client = deepseek_client
        self._model_names = self._build_model_fallbacks(
            model_name,
            "gemini-3-flash-preview",
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
        )
        self._live_search_enabled = live_search_enabled
        self._live_search_model_names = self._build_model_fallbacks(
            live_search_model_name,
            "gemini-3-flash-preview",
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
        )
        self._developer_model_names = self._build_model_fallbacks(
            developer_model_name,
            "gemini-2.5-pro",
            "gemini-3-pro-preview",
            "gemini-3.1-pro-preview",
            "gemini-flash-latest",
            "gemini-2.5-flash",
        )
        self._sessions: dict[int, list[tuple[str, str]]] = {}
        self._awaiting_search: dict[int, str] = {}
        self._resolved_grounding_urls: dict[str, str] = {}

    @property
    def live_search_enabled(self) -> bool:
        """Return whether general AI chat can use live Google Search."""
        return self._live_search_enabled

    @property
    def deepseek_enabled(self) -> bool:
        """Return whether the DeepSeek developer backend is configured."""
        return self._deepseek_client is not None

    @staticmethod
    def _build_model_fallbacks(primary: str | None, *fallbacks: str) -> list[str]:
        """Return one ordered model list without duplicates or empty values."""
        ordered: list[str] = []
        for model_name in (primary, *fallbacks):
            cleaned = (model_name or "").strip()
            if cleaned and cleaned not in ordered:
                ordered.append(cleaned)
        return ordered

    @staticmethod
    def _is_resource_exhausted_error(exc: Exception) -> bool:
        """Return whether an exception chain indicates Gemini quota exhaustion."""
        current: BaseException | None = exc
        while current is not None:
            text = str(current)
            if "RESOURCE_EXHAUSTED" in text or "429" in text:
                return True
            current = current.__cause__ or current.__context__
        return False

    @staticmethod
    def _is_weather_query(message: str) -> bool:
        """Return whether the message is asking about weather-like data."""
        return bool(WEATHER_QUERY_PATTERN.search(message))

    @classmethod
    def _is_time_sensitive_query(cls, message: str) -> bool:
        """Return whether the message likely needs explicit date/time anchoring."""
        return cls._is_weather_query(message) or bool(
            TIME_SENSITIVE_QUERY_PATTERN.search(message)
        )

    @staticmethod
    def _wants_search_results(message: str) -> bool:
        """Return whether the user mainly wants search-engine-like results/links."""
        return bool(SEARCH_RESULTS_QUERY_PATTERN.search(message))

    @staticmethod
    def _prefers_iranian_sites(message: str) -> bool:
        """Return whether the query explicitly prefers Iranian/Persian sites."""
        return bool(IRANIAN_SITE_QUERY_PATTERN.search(message))

    @staticmethod
    def _should_answer_persian(message: str) -> bool:
        """Return whether the bot should prefer Persian phrasing for synthetic wrappers."""
        return bool(re.search(r"[\u0600-\u06FF]", message) or FINGLISH_PERSIAN_PATTERN.search(message))

    @classmethod
    def _build_general_prompt(
        cls,
        *,
        history_text: str,
        user_message: str,
        live_search: bool,
        force_live_search: bool = False,
    ) -> str:
        """Build the general chat prompt with query-specific guidance."""
        guidance: list[str] = []
        include_time_anchor = False
        if cls._is_weather_query(user_message):
            include_time_anchor = True
            guidance.extend(
                [
                    "This is a weather request.",
                    "Use grounded live search results.",
                    "Prefer weather.com when it appears in grounded results, but fall back to other grounded recent sources if needed.",
                    "Answer in a compact mobile weather-app style.",
                    "Treat words like today/امروز/this week relative to the current time anchor below.",
                    "Do not guess today's date. Use the current time anchor below as the meaning of today/امروز.",
                    "Do not start with a long paragraph introduction.",
                    "Use this structure as closely as the data allows:",
                    "🌦 [short title or city name]",
                    "📍 location",
                    "🗓 بروزرسانی: exact local date/time",
                    "☁️ وضعیت: main condition",
                    "🔝 بیشینه امروز: today's high",
                    "🔻 کمینه امروز: today's low",
                    "🌤️ تغییر مهم: next notable condition or time if available",
                    "💨 وزش باد: wind summary",
                    "💧 رطوبت/بارش: humidity or precipitation if available",
                    "⏰ پیش‌بینی ساعتی امروز: 6 to 10 short hourly lines if available",
                    "📅 پیش‌بینی روزهای آینده: 5 to 7 short daily lines if available",
                    "👀 جمع‌بندی کوتاه: one short outlook line",
                    "Always include the exact local date/time for the weather information you are reporting.",
                    "Do not include a raw source section inside the answer body.",
                ]
            )
        elif cls._wants_search_results(user_message):
            guidance.extend(
                [
                    "The user mainly wants search results, websites, or links rather than a long explanation.",
                    "Do not write a long essay.",
                    "Keep the main answer body to at most 2 to 4 short lines.",
                    "Use the first lines for a practical recommendation or quick conclusion.",
                    "Prefer 3 to 5 relevant results.",
                    "Keep the intro to one short line at most.",
                    "If the user explicitly asked for Iranian or Persian sites, prioritize those results.",
                    "Do not spend the answer on disclaimers if grounded results are available.",
                    "Do not dump raw links into the main answer body.",
                ]
            )
        elif cls._is_time_sensitive_query(user_message):
            include_time_anchor = True
            guidance.extend(
                [
                    "This is time-sensitive.",
                    "Treat words like today/امروز/now/الان relative to the current time anchor below.",
                    "Anchor the answer to explicit absolute dates and times with timezone when relevant.",
                    "Avoid answering with only relative wording like today, tomorrow, now, or latest.",
                ]
            )
        elif force_live_search:
            include_time_anchor = True
            guidance.append(
                "Because this answer is based on live search, include explicit dates or times when the result depends on current information."
            )

        prompt_parts = [
            "Continue the conversation naturally.",
            "",
            "Recent conversation:",
            history_text or "None",
            "",
        ]
        if include_time_anchor:
            prompt_parts.extend(
                [
                    "Current time anchor:",
                    cls._build_time_anchor(),
                    "",
                ]
            )
        if live_search and guidance:
            prompt_parts.extend(
                [
                    "Extra guidance:",
                    *[f"- {item}" for item in guidance],
                    "",
                ]
            )
        prompt_parts.extend(
            [
                "User message:",
                user_message,
            ]
        )
        return "\n".join(prompt_parts)

    @staticmethod
    def _gregorian_to_jalali(year: int, month: int, day: int) -> tuple[int, int, int]:
        """Convert a Gregorian date to Jalali without external dependencies."""
        g_day_offsets = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
        if year > 1600:
            jalali_year = 979
            year -= 1600
        else:
            jalali_year = 0
            year -= 621

        leap_base = year + 1 if month > 2 else year
        days = (
            365 * year
            + (leap_base + 3) // 4
            - (leap_base + 99) // 100
            + (leap_base + 399) // 400
            - 80
            + day
            + g_day_offsets[month - 1]
        )

        jalali_year += 33 * (days // 12053)
        days %= 12053
        jalali_year += 4 * (days // 1461)
        days %= 1461

        if days > 365:
            jalali_year += (days - 1) // 365
            days = (days - 1) % 365

        if days < 186:
            jalali_month = 1 + days // 31
            jalali_day = 1 + days % 31
        else:
            jalali_month = 7 + (days - 186) // 30
            jalali_day = 1 + (days - 186) % 30

        return jalali_year, jalali_month, jalali_day

    @classmethod
    def _build_time_anchor(cls) -> str:
        """Return a stable current-time anchor for time-sensitive prompts."""
        now_utc = datetime.now(timezone.utc)
        now_tehran = now_utc.astimezone(TEHRAN_TIMEZONE)
        jalali_year, jalali_month, jalali_day = cls._gregorian_to_jalali(
            now_tehran.year,
            now_tehran.month,
            now_tehran.day,
        )
        persian_weekday = PERSIAN_WEEKDAYS[now_tehran.weekday()]
        persian_month = PERSIAN_MONTHS[jalali_month]
        return "\n".join(
            [
                f"- UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                f"- Iran (Gregorian): {now_tehran.strftime('%A %Y-%m-%d %H:%M:%S %Z')}",
                (
                    "- Iran (Jalali): "
                    f"{persian_weekday} {jalali_day} {persian_month} {jalali_year} "
                    f"{now_tehran.strftime('%H:%M:%S %Z')}"
                ),
            ]
        )

    @classmethod
    def _build_search_prompt(
        cls,
        *,
        history_text: str,
        user_message: str,
    ) -> str:
        """Build a dedicated prompt for search-engine-like queries."""
        preference_line = (
            "Prioritize Iranian or Persian websites when relevant.\n"
            if cls._prefers_iranian_sites(user_message)
            else ""
        )
        return (
            "Use Google Search grounding.\n"
            "The user wants search-style results, not a long explanation.\n"
            "Return one very short conclusion paragraph first.\n"
            "Then surface 4 to 6 distinct website results if available.\n"
            "Prefer the official site first, then other distinct sites.\n"
            "For each result, favor direct page URLs over generic homepages.\n"
            "If the user wrote in Persian or Finglish, answer in Persian script.\n"
            f"{preference_line}"
            "Recent conversation:\n"
            f"{history_text or 'None'}\n\n"
            "User query:\n"
            f"{user_message}"
        )

    @classmethod
    def _build_weather_prompt(
        cls,
        *,
        history_text: str,
        user_message: str,
    ) -> str:
        """Build a dedicated prompt for structured weather responses."""
        answer_in_persian = cls._should_answer_persian(user_message)
        language_line = (
            "Write all field values in Persian script."
            if answer_in_persian
            else "Write all field values in English."
        )
        return (
            "Use Google Search grounding for this weather request.\n"
            "Return ONLY JSON matching the requested schema.\n"
            "Do not return markdown, headings, prose paragraphs, or a source section.\n"
            "Treat words like today/امروز/this week relative to the current time anchor below.\n"
            "Do not guess today's date. Use the current time anchor below.\n"
            f"{language_line}\n"
            "Keep every field compact and mobile-friendly.\n"
            "For checked_at, include an exact Iran local time/date reference.\n"
            "For hourly_forecast, provide 5 to 10 short lines if available.\n"
            "For daily_forecast, provide 5 to 7 short lines if available.\n\n"
            "Current time anchor:\n"
            f"{cls._build_time_anchor()}\n\n"
            "Recent conversation:\n"
            f"{history_text or 'None'}\n\n"
            "User query:\n"
            f"{user_message}"
        )

    @classmethod
    def _build_weather_normalization_prompt(
        cls,
        *,
        user_message: str,
        raw_weather_text: str,
    ) -> str:
        """Build a second-pass prompt to normalize a weather answer into JSON."""
        answer_in_persian = cls._should_answer_persian(user_message)
        language_line = (
            "Write all JSON field values in Persian script."
            if answer_in_persian
            else "Write all JSON field values in English."
        )
        return (
            "Convert the weather answer below into JSON matching the requested schema.\n"
            "Return ONLY JSON.\n"
            f"{language_line}\n"
            "Respect the current time anchor and do not guess today's date.\n\n"
            "Current time anchor:\n"
            f"{cls._build_time_anchor()}\n\n"
            "User query:\n"
            f"{user_message}\n\n"
            "Weather answer to normalize:\n"
            f"{raw_weather_text.strip()}"
        )

    async def _generate_response(
        self,
        *,
        prompt: str,
        system_instruction: str,
        model_names: list[str],
        tools: list[types.Tool] | None = None,
        response_mime_type: str | None = None,
        response_schema: dict[str, object] | None = None,
        pool: GeminiClientPool | None = None,
        thinking_config: types.ThinkingConfig | None = None,
    ) -> object:
        """Call Gemini through the shared key pool."""
        active_pool = pool or self._pool
        return await active_pool.generate_content(
            model_names=model_names,
            contents=prompt,
            system_instruction=system_instruction,
            tools=tools,
            response_mime_type=response_mime_type,
            response_schema=response_schema,
            thinking_config=thinking_config,
        )

    @staticmethod
    def _is_generic_source_title(title: str, uri: str) -> bool:
        """Return whether the title is too generic to use as a visible source label."""
        normalized_title = title.strip().casefold()
        host = urlparse(uri).netloc.removeprefix("www.").casefold()
        if not normalized_title:
            return True
        if normalized_title == host:
            return True
        if " " not in normalized_title and "." in normalized_title:
            return True
        return False

    @staticmethod
    def _summarize_support_text(text: str, *, limit: int = 64) -> str:
        """Turn a grounding support segment into a compact source label."""
        cleaned = text.strip().replace("**", "").replace("__", "")
        cleaned = re.sub(r"^\s*[*•-]\s*", "", cleaned)
        cleaned = " ".join(cleaned.split())
        if not cleaned:
            return ""
        head, separator, _tail = cleaned.partition(":")
        if separator and 2 <= len(head.strip()) <= limit:
            return head.strip()
        cleaned = cleaned.rstrip(".:;, ")
        if len(cleaned) <= limit:
            return cleaned
        truncated = cleaned[:limit].rsplit(" ", 1)[0].strip()
        return f"{truncated or cleaned[:limit].strip()}..."

    @staticmethod
    def _is_low_value_grounding_host(host: str) -> bool:
        """Return whether a grounding host is too generic/noisy to show by default."""
        normalized = host.casefold()
        return normalized in {
            "google.com",
            "www.google.com",
            "vertexaisearch.cloud.google.com",
        }

    @staticmethod
    def _display_host_name(host: str) -> str:
        """Return a compact user-facing site label."""
        normalized = host.casefold()
        if normalized == "weather.com":
            return "Weather.com"
        if normalized.endswith("bbc.com"):
            return "BBC"
        if normalized.endswith("wikipedia.org"):
            return "Wikipedia"
        if not host:
            return "Source"
        return host.removeprefix("www.")

    @classmethod
    def _clean_grounding_title(
        cls,
        *,
        raw_title: str,
        support_label: str,
        host: str,
        prefer_weather: bool,
        uri: str,
    ) -> str:
        """Build a more meaningful grounding source label."""
        display_host = cls._display_host_name(host)
        if prefer_weather and host.casefold().removeprefix("www.") == "weather.com":
            if support_label:
                return f"{display_host} | {support_label}"
            if raw_title and not cls._is_generic_source_title(raw_title, uri):
                return raw_title
            return display_host
        if raw_title and not cls._is_generic_source_title(raw_title, uri):
            return raw_title
        if support_label:
            return support_label
        return display_host

    @classmethod
    def _extract_grounding_sources(
        cls,
        response: object,
        *,
        max_sources: int = 5,
        prefer_weather: bool = False,
        prefer_iranian: bool = False,
    ) -> list[tuple[str, str]]:
        """Return grounded web sources from a Gemini response."""
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []

        grounding_metadata = getattr(candidates[0], "grounding_metadata", None)
        chunks = getattr(grounding_metadata, "grounding_chunks", None) or []
        supports = getattr(grounding_metadata, "grounding_supports", None) or []
        support_labels: dict[int, str] = {}
        for support in supports:
            segment = getattr(support, "segment", None)
            segment_text = cls._summarize_support_text(getattr(segment, "text", None) or "")
            if not segment_text:
                continue
            for chunk_index in getattr(support, "grounding_chunk_indices", None) or []:
                support_labels.setdefault(int(chunk_index), segment_text)

        preferred_sources: list[tuple[str, str, str]] = []
        fallback_sources: list[tuple[str, str, str]] = []
        seen_uris: set[str] = set()
        seen_generic_hosts: set[str] = set()
        for index, chunk in enumerate(chunks):
            web = getattr(chunk, "web", None)
            uri = (getattr(web, "uri", None) or "").strip()
            if not uri or uri in seen_uris:
                continue
            seen_uris.add(uri)
            host = urlparse(uri).netloc.removeprefix("www.")
            raw_title = (getattr(web, "title", None) or "").strip()
            support_label = support_labels.get(index, "")
            if cls._is_generic_source_title(raw_title, uri):
                if host.casefold() in seen_generic_hosts:
                    continue
                seen_generic_hosts.add(host.casefold())
            title = cls._clean_grounding_title(
                raw_title=raw_title,
                support_label=support_label,
                host=host,
                prefer_weather=prefer_weather,
                uri=uri,
            )
            target = (
                fallback_sources
                if cls._is_low_value_grounding_host(host)
                else preferred_sources
            )
            target.append((title, uri, host))

        if prefer_weather:
            preferred_sources.sort(
                key=lambda item: (
                    0 if item[2].casefold() == "weather.com" else 1,
                    item[2].casefold(),
                )
            )
        elif prefer_iranian:
            preferred_sources.sort(
                key=lambda item: (
                    0 if item[2].casefold().endswith(".ir") else 1,
                    item[2].casefold(),
                )
            )

        combined = preferred_sources
        if not combined and not prefer_weather:
            combined = fallback_sources
        return [(title, uri) for title, uri, _host in combined[:max_sources]]

    @classmethod
    def _append_grounding_sources(
        cls,
        answer: str,
        response: object,
        *,
        prefer_weather: bool = False,
        prefer_iranian: bool = False,
    ) -> str:
        """Append a short source section when grounded web sources are available."""
        sources = [
            (title, uri)
            for title, uri in cls._extract_grounding_sources(
                response,
                max_sources=2 if prefer_weather else 4,
                prefer_weather=prefer_weather,
                prefer_iranian=prefer_iranian,
            )
            if uri not in answer
        ]
        if not sources:
            return answer

        lines = [answer.rstrip(), "", "🌐 Sources:"]
        for title, uri in sources:
            lines.append(f"- {title}: {uri}")
        return "\n".join(lines)

    @classmethod
    def _build_search_results_answer(cls, query: str, response: object) -> str | None:
        """Build a search-engine-like response from grounded sources."""
        sources = cls._extract_grounding_sources(
            response,
            max_sources=5,
            prefer_iranian=cls._prefers_iranian_sites(query),
        )
        if not sources:
            return None

        is_persian = cls._should_answer_persian(query)
        if is_persian:
            intro = "🔎 چند نتیجه مرتبط پیدا کردم:"
            if cls._prefers_iranian_sites(query):
                intro = "🔎 چند نتیجه مرتبط پیدا کردم. اولویت با سایت‌های ایرانی/فارسی بود:"
        else:
            intro = "🔎 I found a few relevant results:"

        lines = [intro, "", "🌐 Sources:"]
        for title, uri in sources:
            lines.append(f"- {title}: {uri}")
        return "\n".join(lines)

    async def _resolve_grounding_url(self, uri: str) -> str:
        """Resolve Google grounding redirect URLs to their final destination when possible."""
        cached = self._resolved_grounding_urls.get(uri)
        if cached:
            return cached

        host = urlparse(uri).netloc.casefold()
        if host != "vertexaisearch.cloud.google.com":
            self._resolved_grounding_urls[uri] = uri
            return uri

        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=15.0,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as client:
                response = await client.get(uri)
            location = (response.headers.get("location") or "").strip()
            if location:
                resolved = str(httpx.URL(urljoin(uri, location)))
                self._resolved_grounding_urls[uri] = resolved
                return resolved
        except Exception:
            LOGGER.debug("Failed to resolve grounding redirect %s", uri, exc_info=True)

        self._resolved_grounding_urls[uri] = uri
        return uri

    @staticmethod
    def _strip_source_section(text: str) -> str:
        """Remove any appended source block from a model answer."""
        normalized = text.strip()
        body_text, _separator, _sources_text = normalized.partition("🌐 Sources:")
        return body_text.strip()

    @staticmethod
    def _trim_search_summary(text: str, *, limit: int = 900) -> str:
        """Keep only a short, recommendation-first search summary."""
        stripped = text.strip()
        if not stripped:
            return ""

        lines: list[str] = []
        for raw_line in stripped.splitlines():
            line = raw_line.strip()
            if not line:
                if lines:
                    break
                continue
            if re.match(r"^([*•-]|\d+[.)])\s+", line):
                break
            lines.append(line)

        cleaned = " ".join(lines or [stripped])
        if not cleaned:
            return ""
        if len(cleaned) <= limit:
            return cleaned
        truncated = cleaned[:limit].rsplit(" ", 1)[0].strip()
        return f"{truncated or cleaned[:limit].strip()}..."

    async def _build_search_results_messages(
        self,
        query: str,
        response: object,
    ) -> tuple[str, str] | None:
        """Build a compact two-message search response."""
        sources = self._extract_grounding_sources(
            response,
            max_sources=6,
            prefer_iranian=self._prefers_iranian_sites(query),
        )
        if not sources:
            return None

        resolved_urls = await asyncio.gather(
            *(self._resolve_grounding_url(uri) for _title, uri in sources)
        )
        resolved_sources: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for (title, _uri), resolved_uri in zip(sources, resolved_urls):
            final_uri = (resolved_uri or "").strip()
            if not final_uri or final_uri in seen_urls:
                continue
            seen_urls.add(final_uri)
            resolved_sources.append((title, final_uri))
        if not resolved_sources:
            return None

        raw_text = getattr(response, "text", "") or ""
        summary = self._trim_search_summary(self._strip_source_section(raw_text))
        is_persian = self._should_answer_persian(query)

        if not summary:
            summary = (
                "🔎 یک جمع‌بندی کوتاه: چند نتیجه مرتبط پیدا کردم و موارد مهم‌تر را در لینک‌های بعدی آوردم."
                if is_persian
                else "🔎 Quick take: I found a few relevant results and listed the most useful links next."
            )

        if is_persian:
            header = "🔗 نتایج پیشنهادی برای بررسی:"
            if self._prefers_iranian_sites(query):
                header = "🔗 نتایج پیشنهادی برای بررسی (با اولویت سایت‌های ایرانی/فارسی):"
        else:
            header = "🔗 Suggested results:"

        lines = [header, ""]
        for index, (title, uri) in enumerate(resolved_sources[:6], start=1):
            lines.append(f"{index}. {title}: {uri}")
        return summary, "\n".join(lines)

    @staticmethod
    def _parse_developer_response(response: object) -> ChatResponse:
        """Extract the structured developer-mode payload from a Gemini response."""
        payload = getattr(response, "parsed", None)
        if not isinstance(payload, dict):
            raw_text = (getattr(response, "text", "") or "").strip()
            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError:
                return ChatResponse(raw_text or "پاسخی تولید نشد.")

        if not isinstance(payload, dict):
            return ChatResponse("پاسخی تولید نشد.")

        message = str(payload.get("message") or "").strip() or "پاسخ آماده شد."
        create_file = bool(payload.get("create_file"))
        request_files = bool(payload.get("request_files"))
        filename = str(payload.get("filename") or "").strip()
        code = str(payload.get("code") or "")
        if create_file and filename and code.strip():
            return ChatResponse(
                text=message,
                request_files=request_files,
                artifact_filename=filename,
                artifact_text=code.rstrip() + "\n",
            )
        return ChatResponse(text=message, request_files=request_files)

    @staticmethod
    def _parse_json_payload(response: object) -> dict[str, object] | None:
        """Parse a structured JSON Gemini response."""
        payload = getattr(response, "parsed", None)
        if isinstance(payload, dict):
            return payload
        raw_text = (getattr(response, "text", "") or "").strip()
        if not raw_text:
            return None
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @classmethod
    def _format_weather_response(
        cls,
        payload: dict[str, object],
        *,
        user_message: str,
    ) -> str:
        """Render structured weather data into a stable chat-friendly layout."""
        is_persian = cls._should_answer_persian(user_message)

        def clean(value: object) -> str:
            return " ".join(str(value or "").strip().split())

        def lines_from(value: object, *, limit: int) -> list[str]:
            if not isinstance(value, list):
                return []
            items = [clean(item) for item in value if clean(item)]
            return items[:limit]

        title = clean(payload.get("title")) or ("آب و هوا" if is_persian else "Weather")
        location = clean(payload.get("location"))
        checked_at = clean(payload.get("checked_at"))
        condition = clean(payload.get("condition"))
        today_high = clean(payload.get("today_high"))
        today_low = clean(payload.get("today_low"))
        notable_change = clean(payload.get("notable_change"))
        wind = clean(payload.get("wind"))
        humidity_precipitation = clean(payload.get("humidity_precipitation"))
        hourly_forecast = lines_from(payload.get("hourly_forecast"), limit=10)
        daily_forecast = lines_from(payload.get("daily_forecast"), limit=7)
        short_outlook = clean(payload.get("short_outlook"))

        if is_persian:
            sections = [f"🌦 {title}"]
            if location:
                sections.append(f"📍 مکان: {location}")
            if checked_at:
                sections.append(f"🗓 بروزرسانی: {checked_at}")
            if condition:
                sections.append(f"☁️ وضعیت: {condition}")
            if today_high:
                sections.append(f"🔝 بیشینه امروز: {today_high}")
            if today_low:
                sections.append(f"🔻 کمینه امروز: {today_low}")
            if notable_change:
                sections.append(f"🌤️ تغییر مهم: {notable_change}")
            if wind:
                sections.append(f"💨 وزش باد: {wind}")
            if humidity_precipitation:
                sections.append(f"💧 رطوبت/بارش: {humidity_precipitation}")
            if hourly_forecast:
                sections.extend(["", "⏰ پیش‌بینی ساعتی امروز:"])
                sections.extend(hourly_forecast)
            if daily_forecast:
                sections.extend(["", "📅 پیش‌بینی روزهای آینده:"])
                sections.extend(daily_forecast)
            if short_outlook:
                sections.extend(["", f"👀 جمع‌بندی کوتاه: {short_outlook}"])
        else:
            sections = [f"🌦 {title}"]
            if location:
                sections.append(f"📍 Location: {location}")
            if checked_at:
                sections.append(f"🗓 Updated: {checked_at}")
            if condition:
                sections.append(f"☁️ Condition: {condition}")
            if today_high:
                sections.append(f"🔝 Today's high: {today_high}")
            if today_low:
                sections.append(f"🔻 Today's low: {today_low}")
            if notable_change:
                sections.append(f"🌤️ Notable change: {notable_change}")
            if wind:
                sections.append(f"💨 Wind: {wind}")
            if humidity_precipitation:
                sections.append(f"💧 Humidity/precipitation: {humidity_precipitation}")
            if hourly_forecast:
                sections.extend(["", "⏰ Hourly forecast:"])
                sections.extend(hourly_forecast)
            if daily_forecast:
                sections.extend(["", "📅 Next days:"])
                sections.extend(daily_forecast)
            if short_outlook:
                sections.extend(["", f"👀 Quick outlook: {short_outlook}"])

        return "\n".join(line for line in sections if line is not None).strip()

    def reset_session(self, user_id: int) -> None:
        """Remove any existing chat session for the user."""
        self._sessions.pop(user_id, None)
        self._awaiting_search.pop(user_id, None)

    def detect_sources(self, message: str) -> list[str]:
        """Return explicitly requested source keys mentioned in the user question."""
        lowered = message.casefold()
        selected = [
            key
            for key, aliases in SOURCE_ALIAS_MAP.items()
            if any(alias.casefold() in lowered for alias in aliases)
        ]
        return selected or list(NEWS_SOURCES.keys())

    def retrieve_relevant_excerpts(
        self,
        message: str,
        reference_payloads: dict[str, dict[str, str]],
        max_excerpts: int = 12,
    ) -> list[NewsExcerpt]:
        """Retrieve the most relevant news excerpts from the selected source files."""
        selected_sources = self.detect_sources(message)
        query_tokens = {
            token.casefold()
            for token in TOKEN_PATTERN.findall(message)
            if len(token) > 1 and token.casefold() not in STOP_WORDS
        }

        excerpts: list[NewsExcerpt] = []
        for source_key in selected_sources:
            payload = reference_payloads.get(source_key)
            if not payload:
                continue

            for excerpt in self._parse_reference_entries(
                source_key=source_key,
                source_name=payload["source_name"],
                raw_text=payload["raw_text"],
            ):
                text_tokens = {
                    token.casefold()
                    for token in TOKEN_PATTERN.findall(excerpt.text)
                    if len(token) > 1
                }
                score = len(query_tokens & text_tokens)
                if not query_tokens:
                    score = 1
                excerpt.score = score
                excerpts.append(excerpt)

        excerpts.sort(key=lambda item: (item.score, item.date, item.time), reverse=True)
        if query_tokens:
            excerpts = [excerpt for excerpt in excerpts if excerpt.score > 0] or excerpts
        return excerpts[:max_excerpts]

    def _parse_reference_entries(
        self,
        source_key: str,
        source_name: str,
        raw_text: str,
    ) -> list[NewsExcerpt]:
        """Parse structured reference files into searchable entries."""
        entries: list[NewsExcerpt] = []
        for block in raw_text.split("=" * 60):
            lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
            if not lines:
                continue

            date = ""
            time = ""
            link = ""
            text_lines: list[str] = []
            text_mode = False
            for line in lines:
                if line.startswith("Date (UTC):"):
                    date = line.split(":", maxsplit=1)[1].strip()
                elif line.startswith("Time (UTC):"):
                    time = line.split(":", maxsplit=1)[1].strip()
                elif line.startswith("Link:"):
                    link = line.split(":", maxsplit=1)[1].strip()
                elif line == "Text:":
                    text_mode = True
                elif text_mode:
                    text_lines.append(line)

            text = "\n".join(text_lines).strip()
            if not text:
                continue

            entries.append(
                NewsExcerpt(
                    source_key=source_key,
                    source_name=source_name,
                    date=date,
                    time=time,
                    link=link,
                    text=text,
                    score=0,
                )
            )
        return entries

    async def send_message(
        self,
        user_id: int,
        message: str,
        reference_payloads: dict[str, dict[str, str]] | None = None,
        *,
        mode: str = "news",
        attachments: Sequence[tuple[str, str, bool]] | None = None,
        developer_model: str | None = None,
    ) -> ChatResponse:
        """Answer a user message using source-specific reference files and short chat history."""
        cleaned_message = message.strip()
        if not cleaned_message:
            return ChatResponse("پیام خالی است.")

        history = self._sessions.setdefault(user_id, [])
        if mode == "developer":
            history_text = "\n".join(
                f"User: {user_text}\nAssistant: {assistant_text}"
                for user_text, assistant_text in history[-GENERAL_CHAT_HISTORY_WINDOW:]
            ).strip()
            attachment_sections: list[str] = []
            for attachment_name, attachment_text, attachment_truncated in attachments or ():
                attachment_sections.append(
                    "Attached file:\n"
                    f"File name: {attachment_name}\n"
                    f"Content{' (truncated)' if attachment_truncated else ''}:\n"
                    f"{attachment_text}"
                )
            attachment_section = (
                "\n\n" + "\n\n".join(attachment_sections)
                if attachment_sections
                else ""
            )
            prompt = (
                "Answer the programming request.\n"
                "If a code artifact would help, create exactly one primary file.\n\n"
                "Recent conversation:\n"
                f"{history_text or 'None'}\n\n"
                f"User message:\n{cleaned_message}"
                f"{attachment_section}"
            )
            selected_developer_model = str(developer_model or "").strip().lower() or "gemini-pro"
            if selected_developer_model.startswith("deepseek-"):
                if self._deepseek_client is None:
                    raise RuntimeError(
                        "DeepSeek Developer mode is not configured. Set DEEPSEEK_API_BASE_URL first."
                    )
                try:
                    response = await self._deepseek_client.generate_developer_response(
                        prompt=prompt,
                        system_instruction=DEEPSEEK_DEVELOPER_SYSTEM_PROMPT,
                        model_name=selected_developer_model,
                    )
                except Exception as exc:
                    raise RuntimeError(f"DeepSeek Developer mode failed: {exc}") from exc

                chat_response = self._parse_developer_response(response)
                chat_response.model_name = (
                    str(getattr(response, "model_version", "") or "").strip()
                    or selected_developer_model
                )
                history_user_text = cleaned_message
                attachment_names = [name for name, _text, _truncated in attachments or ()]
                if attachment_names:
                    history_user_text = (
                        f"{cleaned_message}\n[Attached files: {', '.join(attachment_names)}]"
                    )
                if chat_response.text:
                    history.append((history_user_text, chat_response.text))
                    self._sessions[user_id] = history[-STORED_CHAT_TURNS:]
                    return chat_response
                return ChatResponse("پاسخی تولید نشد.")
            try:
                response = await self._generate_response(
                    prompt=prompt,
                    system_instruction=DEVELOPER_CHAT_SYSTEM_PROMPT,
                    model_names=self._developer_model_names,
                    response_mime_type="application/json",
                    response_schema=DEVELOPER_RESPONSE_SCHEMA,
                    pool=self._developer_pool,
                    thinking_config=types.ThinkingConfig(
                        includeThoughts=False,
                        thinkingLevel=types.ThinkingLevel.HIGH,
                    ),
                )
            except Exception as exc:
                LOGGER.exception("Gemini developer chat request failed")
                if self._is_resource_exhausted_error(exc):
                    raise RuntimeError(
                        "Developer Mode is temporarily out of quota on its dedicated Gemini keys. Please retry in about a minute."
                    ) from exc
                raise RuntimeError("Failed to get a response from Gemini Developer mode.") from exc

            chat_response = self._parse_developer_response(response)
            chat_response.model_name = (
                str(getattr(response, "model_version", "") or "").strip()
                or self._developer_model_names[0]
            )
            history_user_text = cleaned_message
            attachment_names = [name for name, _text, _truncated in attachments or ()]
            if attachment_names:
                history_user_text = (
                    f"{cleaned_message}\n[Attached files: {', '.join(attachment_names)}]"
                )
            if chat_response.text:
                history.append((history_user_text, chat_response.text))
                self._sessions[user_id] = history[-STORED_CHAT_TURNS:]
                return chat_response
            return ChatResponse("پاسخی تولید نشد.")

        if mode != "news":
            history_text = "\n".join(
                f"User: {user_text}\nAssistant: {assistant_text}"
                for user_text, assistant_text in history[-GENERAL_CHAT_HISTORY_WINDOW:]
            ).strip()
            weather_intent = self._live_search_enabled and self._is_weather_query(cleaned_message)
            search_intent = (
                self._live_search_enabled
                and self._wants_search_results(cleaned_message)
                and not self._is_weather_query(cleaned_message)
            )
            if weather_intent:
                weather_prompt = self._build_weather_prompt(
                    history_text=history_text,
                    user_message=cleaned_message,
                )
                try:
                    response = await self._generate_response(
                        prompt=weather_prompt,
                        system_instruction=GENERAL_CHAT_LIVE_SEARCH_SYSTEM_PROMPT,
                        model_names=self._live_search_model_names,
                        tools=[types.Tool(google_search=types.GoogleSearch())],
                    )
                except Exception as exc:
                    LOGGER.exception("Gemini weather request failed")
                    raise RuntimeError("Failed to get a response from Gemini Weather.") from exc

                payload = self._parse_json_payload(response)
                if payload is None:
                    raw_weather_text = (getattr(response, "text", "") or "").strip()
                    if raw_weather_text:
                        normalization_prompt = self._build_weather_normalization_prompt(
                            user_message=cleaned_message,
                            raw_weather_text=raw_weather_text,
                        )
                        try:
                            normalization_response = await self._generate_response(
                                prompt=normalization_prompt,
                                system_instruction=GENERAL_CHAT_SYSTEM_PROMPT,
                                model_names=self._model_names,
                                response_mime_type="application/json",
                                response_schema=WEATHER_RESPONSE_SCHEMA,
                            )
                            payload = self._parse_json_payload(normalization_response)
                        except Exception:
                            LOGGER.exception("Gemini weather normalization failed")
                if payload:
                    answer = self._format_weather_response(
                        payload,
                        user_message=cleaned_message,
                    )
                    history.append((cleaned_message, answer))
                    self._sessions[user_id] = history[-STORED_CHAT_TURNS:]
                    return ChatResponse(answer)

            prompt = self._build_general_prompt(
                history_text=history_text,
                user_message=cleaned_message,
                live_search=self._live_search_enabled,
            )
            if search_intent:
                prompt = self._build_search_prompt(
                    history_text=history_text,
                    user_message=cleaned_message,
                )
            try:
                response = await self._generate_response(
                    prompt=prompt,
                    system_instruction=(
                        GENERAL_CHAT_LIVE_SEARCH_SYSTEM_PROMPT
                        if self._live_search_enabled
                        else GENERAL_CHAT_SYSTEM_PROMPT
                    ),
                    model_names=(
                        self._live_search_model_names
                        if self._live_search_enabled
                        else self._model_names
                    ),
                    tools=(
                        [types.Tool(google_search=types.GoogleSearch())]
                        if self._live_search_enabled
                        else None
                    ),
                )
            except Exception as exc:
                LOGGER.exception("Gemini general chat request failed")
                raise RuntimeError("Failed to get a response from Gemini.") from exc

            text = getattr(response, "text", "") or ""
            answer = text.strip()
            if search_intent:
                search_messages = await self._build_search_results_messages(
                    cleaned_message,
                    response,
                )
                if search_messages:
                    summary_text, results_text = search_messages
                    history_answer = f"{summary_text}\n\n{results_text}".strip()
                    history.append((cleaned_message, history_answer))
                    self._sessions[user_id] = history[-STORED_CHAT_TURNS:]
                    return ChatResponse(summary_text, extra_texts=[results_text])
                answer = self._build_search_results_answer(cleaned_message, response) or answer
            elif answer and self._live_search_enabled:
                answer = self._append_grounding_sources(
                    answer,
                    response,
                    prefer_weather=self._is_weather_query(cleaned_message),
                    prefer_iranian=self._prefers_iranian_sites(cleaned_message),
                )
            if answer:
                history.append((cleaned_message, answer))
                self._sessions[user_id] = history[-STORED_CHAT_TURNS:]
                return ChatResponse(answer)
            return ChatResponse("پاسخی تولید نشد.")

        reference_payloads = reference_payloads or {}
        selected_sources = self.detect_sources(cleaned_message)
        excerpts = self.retrieve_relevant_excerpts(cleaned_message, reference_payloads)
        if not excerpts:
            self._awaiting_search[user_id] = cleaned_message
            return ChatResponse(
                "در فایل های مرجع خبری من مطلب مرتبطی پیدا نشد. آیا می خواهید برایتان در اینترنت جستجو کنم؟",
                needs_search_confirmation=True,
            )
        source_lines = [
            f"- {reference_payloads[key]['source_name']}"
            for key in selected_sources
            if key in reference_payloads
        ]
        excerpt_lines = []
        for index, excerpt in enumerate(excerpts, start=1):
            excerpt_lines.extend(
                [
                    f"[{index}] {excerpt.source_name}",
                    f"Date: {excerpt.date}",
                    f"Time: {excerpt.time}",
                    f"Link: {excerpt.link}",
                    f"Text: {excerpt.text}",
                    "",
                ]
            )

        history_text = "\n".join(
            f"User: {user_text}\nAssistant: {assistant_text}"
            for user_text, assistant_text in history[-NEWS_CHAT_HISTORY_WINDOW:]
        ).strip()
        prompt = (
            "Use the provided reference excerpts as the primary basis for your answer.\n"
            "If the user explicitly named sources, rely only on those sources.\n"
            "If you cannot find any relevant information to answer the user's prompt in the reference excerpts, "
            "do NOT attempt to guess or answer with external knowledge. Instead, return EXACTLY the phrase: "
            "'NO_INFO_FOUND' and nothing else.\n\n"
            "Selected sources:\n"
            f"{chr(10).join(source_lines) or '- None'}\n\n"
            "Recent conversation:\n"
            f"{history_text or 'None'}\n\n"
            "Relevant reference excerpts:\n"
            f"{chr(10).join(excerpt_lines) or 'No matching excerpts were found.'}\n\n"
            f"User question:\n{cleaned_message}"
        )

        try:
            response = await self._generate_response(
                prompt=prompt,
                system_instruction=CHAT_SYSTEM_PROMPT,
                model_names=self._model_names,
            )
        except Exception as exc:
            LOGGER.exception("Gemini chat request failed")
            raise RuntimeError("Failed to get a response from Gemini.") from exc

        text = getattr(response, "text", "") or ""
        answer = text.strip()

        if answer == "NO_INFO_FOUND" or "NO_INFO_FOUND" in answer:
            self._awaiting_search[user_id] = cleaned_message
            return ChatResponse(
                "مطلبی مرتبط در منابع من پیدا نشد. آیا می‌خواهید برایتان در اینترنت جستجو کنم؟",
                needs_search_confirmation=True,
            )

        if answer:
            history.append((cleaned_message, answer))
            self._sessions[user_id] = history[-STORED_CHAT_TURNS:]
            return ChatResponse(answer)
        return ChatResponse("پاسخی تولید نشد.")

    def get_pending_search_query(self, user_id: int) -> str | None:
        """Return the pending internet-search query for the user, if any."""
        return self._awaiting_search.get(user_id)

    def clear_pending_search(self, user_id: int) -> None:
        """Clear pending internet-search confirmation state."""
        self._awaiting_search.pop(user_id, None)

    async def execute_internet_search(self, user_id: int, query: str) -> ChatResponse:
        """Perform a Google Search to answer the query."""
        history = self._sessions.setdefault(user_id, [])
        history_text = "\n".join(
            f"User: {user_text}\nAssistant: {assistant_text}"
            for user_text, assistant_text in history[-GENERAL_CHAT_HISTORY_WINDOW:]
        ).strip()
        if self._wants_search_results(query) and not self._is_weather_query(query):
            prompt = self._build_search_prompt(
                history_text=history_text,
                user_message=query,
            )
        else:
            prompt = (
                "Start your response exactly with this first line:\n"
                "🌍 نتیجه جستجوی زنده\n\n"
                + self._build_general_prompt(
                    history_text=history_text,
                    user_message=query,
                    live_search=True,
                    force_live_search=True,
                )
            )

        try:
            response = await self._generate_response(
                prompt=prompt,
                system_instruction=CHAT_SYSTEM_PROMPT,
                model_names=self._live_search_model_names,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            )
        except Exception as exc:
            LOGGER.exception("Gemini internet search request failed")
            raise RuntimeError("Failed to get a response from Gemini Search.") from exc

        text = getattr(response, "text", "") or ""
        if self._wants_search_results(query) and not self._is_weather_query(query):
            search_messages = await self._build_search_results_messages(query, response)
            if search_messages:
                summary_text, results_text = search_messages
                history.append((query, f"{summary_text}\n\n{results_text}".strip()))
                self._sessions[user_id] = history[-STORED_CHAT_TURNS:]
                return ChatResponse(summary_text, extra_texts=[results_text])

        answer = self._append_grounding_sources(
            text.strip(),
            response,
            prefer_weather=self._is_weather_query(query),
            prefer_iranian=self._prefers_iranian_sites(query),
        )
        if answer:
            history.append((query, answer))
            self._sessions[user_id] = history[-STORED_CHAT_TURNS:]
            return ChatResponse(answer)
        return ChatResponse("پاسخی از جستجوی اینترنت تولید نشد.")
