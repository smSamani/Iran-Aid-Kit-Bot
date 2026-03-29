"""Gemini-powered conversational chat support."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from google import genai
from google.genai import types

from config import NEWS_SOURCES

LOGGER = logging.getLogger(__name__)

CHAT_SYSTEM_PROMPT = """You are a helpful Telegram bot assistant focused on news and analysis.

General behavior:
- Answer clearly and naturally.
- If the user writes in Persian, answer in Persian.
- If the user writes in English, answer in English.
- Keep responses concise unless the user asks for detail.
- Optimize all answers for Telegram readability on mobile screens.
- Prefer short paragraphs, short lines, and visually scannable formatting.
- Do not use markdown syntax such as **bold**, __italic__, bullet markers like "*", or code fences unless the user explicitly asks for them.
- Use plain text with emoji section labels instead.
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
- If the user writes in Persian, answer in Persian.
- If the user writes in English, answer in English.
- Keep responses concise unless the user asks for detail.
- Optimize answers for Telegram readability on mobile screens.
- Prefer short paragraphs and simple wording.
- Do not use markdown syntax unless the user explicitly asks for it.
- Do not force news structure, source citations, or search-confirmation behavior.
- If you do not know something, say so simply instead of inventing facts."""


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


class GeminiChatManager:
    """Manage per-user Gemini chat sessions."""

    def __init__(self, api_key: str, model_name: str = "gemini-3.1-flash-lite-preview") -> None:
        self._client = genai.Client(api_key=api_key)
        self._model_name = model_name
        self._sessions: dict[int, list[tuple[str, str]]] = {}
        self._awaiting_search: dict[int, str] = {}

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
    ) -> ChatResponse:
        """Answer a user message using source-specific reference files and short chat history."""
        cleaned_message = message.strip()
        if not cleaned_message:
            return ChatResponse("پیام خالی است.")

        history = self._sessions.setdefault(user_id, [])
        if mode != "news":
            history_text = "\n".join(
                f"User: {user_text}\nAssistant: {assistant_text}"
                for user_text, assistant_text in history[-6:]
            ).strip()
            prompt = (
                "Continue the conversation naturally.\n\n"
                "Recent conversation:\n"
                f"{history_text or 'None'}\n\n"
                f"User message:\n{cleaned_message}"
            )
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=GENERAL_CHAT_SYSTEM_PROMPT,
                    ),
                )
            except Exception as exc:
                LOGGER.exception("Gemini general chat request failed")
                raise RuntimeError("Failed to get a response from Gemini.") from exc

            text = getattr(response, "text", "") or ""
            answer = text.strip()
            if answer:
                history.append((cleaned_message, answer))
                self._sessions[user_id] = history[-10:]
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
            for user_text, assistant_text in history[-4:]
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
            response = await self._client.aio.models.generate_content(
                model=self._model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=CHAT_SYSTEM_PROMPT,
                ),
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
            self._sessions[user_id] = history[-6:]
            return ChatResponse(answer)
        return ChatResponse("پاسخی تولید نشد.")

    def get_pending_search_query(self, user_id: int) -> str | None:
        """Return the pending internet-search query for the user, if any."""
        return self._awaiting_search.get(user_id)

    def clear_pending_search(self, user_id: int) -> None:
        """Clear pending internet-search confirmation state."""
        self._awaiting_search.pop(user_id, None)

    async def execute_internet_search(self, user_id: int, query: str) -> str:
        """Perform a Google Search to answer the query."""
        history = self._sessions.setdefault(user_id, [])
        history_text = "\n".join(
            f"User: {user_text}\nAssistant: {assistant_text}"
            for user_text, assistant_text in history[-4:]
        ).strip()
        
        prompt = (
            "You MUST start your response exactly with this header:\n\n"
            "🌍 **[نتیجه جستجوی زنده اینترنت]**\n\n"
            "Answer the user's question using the Google Search tool to find up-to-date information.\n\n"
            "IMPORTANT: Include inline links/URLs to the sources you found via search so I can verify the information.\n\n"
            "Recent conversation:\n"
            f"{history_text or 'None'}\n\n"
            f"User question:\n{query}"
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=CHAT_SYSTEM_PROMPT,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
        except Exception as exc:
            LOGGER.exception("Gemini internet search request failed")
            raise RuntimeError("Failed to get a response from Gemini Search.") from exc

        text = getattr(response, "text", "") or ""
        answer = text.strip()
        if answer:
            history.append((query, answer))
            self._sessions[user_id] = history[-6:]
            return answer
        return "پاسخی از جستجوی اینترنت تولید نشد."
