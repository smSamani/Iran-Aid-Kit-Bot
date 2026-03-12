"""Gemini-based news summarization."""

from __future__ import annotations

import asyncio
import logging

from google import genai


LOGGER = logging.getLogger(__name__)

SUMMARY_PROMPT = """Summarize the following Persian news in 2 concise lines.
Keep the key information and avoid opinions.

{news_text}

Return Persian output."""

DIGEST_SUMMARY_PROMPT = """You are given a structured file of the latest Telegram news posts from {source_name}.
Each entry includes date, time, post link, and the caption or text under the photo/video/text post.

Rewrite the output to be fully Telegram-friendly and clean.

Strict rules:
1. Use only Telegram-friendly plain text formatting.
2. Allowed: emojis, line breaks, simple numbering, bullet points using "•".
3. Do NOT use markdown headings, code blocks, raw markdown links, excessive symbols, or nested formatting.
4. Group related posts about the same event together and put them next to each other.
5. Keep the most important developments first.
6. Avoid opinions and avoid repeating near-duplicate posts as separate items.
7. Never show source file names.
8. Always end with exactly this style of source line:
📎 منبع: {source_name}

Each message must follow this structure:
📰 {source_name}

🧾 خلاصه کلی
[2 to 3 short sentences covering the main developments]
[In the summary itself, explicitly mention the date or date range of the covered posts]

📌 مهم‌ترین خبرها

1️⃣ [Short headline]
• bullet point explanation
• bullet point explanation
🗓 تاریخ: [Explicit Persian date or date range for this group]
🔗 لینک‌ها:
• full telegram post link
• full telegram post link

2️⃣ [Short headline]
• bullet point explanation
• bullet point explanation
🗓 تاریخ: [Explicit Persian date or date range for this group]
🔗 لینک‌ها:
• full telegram post link
• full telegram post link

3️⃣ [Short headline]
• bullet point explanation
• bullet point explanation
🗓 تاریخ: [Explicit Persian date or date range for this group]
🔗 لینک‌ها:
• full telegram post link
• full telegram post link

📎 منبع: {source_name}

Additional rules:
- If there are fewer than 3 real groups, return only the real groups.
- Dates are mandatory, not optional.
- Every numbered news group must contain exactly one visible date line starting with: 🗓 تاریخ:
- The overall summary must also mention the date or date range covered by the digest.
- Use the dates from the compiled source file. Do not invent dates.
- If multiple related posts belong to one group, show the date as a range when needed.
- Keep lines short enough to read well in Telegram.
- Use Persian output.
- Do not include any extra footer such as file name or post count.

Here is the compiled news file:

{compiled_news}
"""


class GeminiSummarizer:
    """Async wrapper around the Gemini SDK."""

    def __init__(self, api_key: str, model_name: str = "gemini-3.1-flash-lite-preview") -> None:
        self._client = genai.Client(api_key=api_key)
        self._model_name = model_name

    async def summarize_news(self, news_text: str) -> str:
        """Return a compact Persian summary for the provided news text."""
        cleaned_text = news_text.strip()
        if not cleaned_text:
            return "متن خبر برای خلاصه سازی موجود نیست."

        prompt = SUMMARY_PROMPT.format(news_text=cleaned_text)
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model_name,
                contents=prompt,
            )
        except Exception as exc:
            LOGGER.exception("Gemini summarization failed")
            raise RuntimeError("Failed to summarize news with Gemini.") from exc

        text = getattr(response, "text", "") or ""
        summary = text.strip()
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
            response = await self._client.aio.models.generate_content(
                model=self._model_name,
                contents=prompt,
            )
        except Exception as exc:
            LOGGER.exception("Gemini digest summarization failed")
            raise RuntimeError("Failed to summarize grouped news with Gemini.") from exc

        text = getattr(response, "text", "") or ""
        summary = text.strip()
        if summary:
            return summary
        return "خلاصه گروه بندی شده ای برای این خبرها تولید نشد."
