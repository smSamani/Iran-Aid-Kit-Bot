"""Gemini-based news summarization."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from google import genai
from google.genai import errors


LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = {429, 500, 503}

SUMMARY_PROMPT = """Summarize the following Persian news in 2 concise lines.
Keep the key information and avoid opinions.

{news_text}

Return Persian output."""

DIGEST_SUMMARY_PROMPT = """You are given a structured file of the latest Telegram news posts from {source_name}.
Each entry includes date, time, post link, and the caption or text under the photo/video/text post.

Rewrite the output to be fully Telegram-friendly and clean.
Keep the final message under 4,096 UTF-8 characters.

Strict rules:
1. Use only Telegram-friendly plain text formatting.
2. Allowed: emojis, line breaks, simple numbering, bullet points using "•".
3. Do NOT use markdown headings, code blocks, raw markdown links, excessive symbols, or nested formatting.
4. Group related posts about the same event together and put them next to each other.
5. Keep the most important developments first.
6. Avoid opinions and avoid repeating near-duplicate posts as separate items.
7. Never show source file names, source labels, or any link section.
8. Do not include a "🔗 لینک‌ها" section at all.
9. Do not include a source footer or any reference footer.
10. The very first line of the message must be exactly: 📰 {source_name}

Each message must follow this structure:
📰 {source_name}

📌 مهم‌ترین خبرها

1️⃣ [Short headline]
• bullet point explanation
• bullet point explanation
• bullet point explanation when there is meaningful additional detail
• include relevant numbers, locations, actors, and immediate consequences when available
🗓 تاریخ: [Explicit Persian date or date range for this group]

2️⃣ [Short headline]
• bullet point explanation
• bullet point explanation
• bullet point explanation when there is meaningful additional detail
• include relevant numbers, locations, actors, and immediate consequences when available
🗓 تاریخ: [Explicit Persian date or date range for this group]

3️⃣ [Short headline]
• bullet point explanation
• bullet point explanation
• bullet point explanation when there is meaningful additional detail
• include relevant numbers, locations, actors, and immediate consequences when available
🗓 تاریخ: [Explicit Persian date or date range for this group]

Additional rules:
- If there are fewer than 3 real groups, return only the real groups.
- Dates are mandatory, not optional.
- Every numbered news group must contain exactly one visible date line starting with: 🗓 تاریخ:
- Do not include any general summary section such as: 🧾 خلاصه کلی
- Start directly with: 📌 مهم‌ترین خبرها
- Use the space that would normally be spent on links and general summaries to provide richer factual detail inside the numbered news groups.
- Prefer 3 to 4 detailed bullet points per group when the source material supports it.
- Use the dates from the compiled source file. Do not invent dates.
- If multiple related posts belong to one group, show the date as a range when needed.
- Keep lines short enough to read well in Telegram.
- Use Persian output.
- Do not include any extra footer such as file name, post count, or references.

Here is the compiled news file:

{compiled_news}
"""


class GeminiSummarizer:
    """Async wrapper around the Gemini SDK."""

    def __init__(self, api_key: str, model_name: str = "gemini-3.1-flash-lite-preview") -> None:
        self._client = genai.Client(api_key=api_key)
        self._model_names = [
            model_name,
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ]

    async def _generate_text(self, prompt: str) -> str:
        """Call Gemini with retries and model fallbacks for transient failures."""
        last_error: Exception | None = None
        for model_name in self._model_names:
            for attempt in range(3):
                try:
                    response = await self._client.aio.models.generate_content(
                        model=model_name,
                        contents=prompt,
                    )
                    text = getattr(response, "text", "") or ""
                    if text.strip():
                        return text.strip()
                except errors.APIError as exc:
                    last_error = exc
                    status_code = getattr(exc, "status_code", None)
                    if status_code not in RETRYABLE_STATUS_CODES or attempt == 2:
                        LOGGER.warning(
                            "Gemini model %s failed with API error status %s on attempt %s",
                            model_name,
                            status_code,
                            attempt + 1,
                        )
                        break
                    await asyncio.sleep(1.5 * (attempt + 1))
                except Exception as exc:
                    last_error = exc
                    LOGGER.warning(
                        "Gemini model %s failed with non-retryable error on attempt %s",
                        model_name,
                        attempt + 1,
                        exc_info=True,
                    )
                    break

        if last_error is not None:
            raise RuntimeError("Failed to get a response from Gemini.") from last_error
        raise RuntimeError("Gemini returned an empty response.")

    @dataclass(slots=True)
    class _FallbackEntry:
        """Parsed entry for local digest fallback."""

        date_text: str
        text: str

    @classmethod
    def _parse_fallback_entries(cls, compiled_news: str) -> list[_FallbackEntry]:
        """Parse compiled Telegram news text into simple entries."""
        blocks = [block.strip() for block in compiled_news.split("=" * 60) if block.strip()]
        entries: list[GeminiSummarizer._FallbackEntry] = []
        for block in blocks:
            date_text = ""
            text = ""
            in_text = False
            for line in block.splitlines():
                stripped = line.strip()
                if stripped.startswith("Date (UTC):"):
                    date_text = stripped.removeprefix("Date (UTC):").strip()
                elif stripped == "Text:":
                    in_text = True
                elif in_text:
                    text += (stripped + " ")
            if text.strip():
                entries.append(cls._FallbackEntry(date_text=date_text, text=text.strip()))
        return entries

    @classmethod
    def _build_digest_fallback(cls, compiled_news: str, source_name: str) -> str:
        """Build a simple Telegram-ready digest when Gemini is unavailable."""
        entries = cls._parse_fallback_entries(compiled_news)
        if not entries:
            return f"📰 {source_name}\n\n📌 مهم‌ترین خبرها\n\nاطلاعات کافی برای ساخت خلاصه در دسترس نیست."

        lines = [f"📰 {source_name}", "", "📌 مهم‌ترین خبرها", ""]
        for index, entry in enumerate(entries[-3:], start=1):
            snippet = entry.text.replace("\n", " ").strip()
            snippet = " ".join(snippet.split())
            if len(snippet) > 450:
                snippet = snippet[:447].rstrip() + "..."
            lines.append(f"{index}️⃣ خبر {index}")
            lines.append(f"• {snippet}")
            if entry.date_text:
                lines.append(f"🗓 تاریخ: {entry.date_text}")
            lines.append("")
        lines.append("خلاصه با fallback محلی ساخته شد چون Gemini موقتاً در دسترس نبود.")
        return "\n".join(lines).strip()

    async def summarize_news(self, news_text: str) -> str:
        """Return a compact Persian summary for the provided news text."""
        cleaned_text = news_text.strip()
        if not cleaned_text:
            return "متن خبر برای خلاصه سازی موجود نیست."

        prompt = SUMMARY_PROMPT.format(news_text=cleaned_text)
        try:
            summary = await self._generate_text(prompt)
        except Exception as exc:
            LOGGER.exception("Gemini summarization failed")
            raise RuntimeError("Failed to summarize news with Gemini.") from exc

        if summary:
            return summary
        return "خلاصه ای برای این خبر تولید نشد."

    async def summarize_news_digest(self, compiled_news: str, source_name: str) -> str:
        """Summarize a compiled news file into grouped Persian news items."""
        cleaned_text = compiled_news.strip()
        if not cleaned_text:
            return "فایل خبر برای خلاصه سازی خالی است."

        prompt = DIGEST_SUMMARY_PROMPT.format(
            compiled_news=cleaned_text,
            source_name=source_name,
        )
        try:
            summary = await self._generate_text(prompt)
        except Exception as exc:
            LOGGER.exception("Gemini digest summarization failed")
            return self._build_digest_fallback(cleaned_text, source_name)

        if summary:
            return summary
        return self._build_digest_fallback(cleaned_text, source_name)
