"""Telegram channel news retrieval and grouped summarization."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import RPCError

from ai_summary import GeminiSummarizer
from config import (
    AI_REFERENCE_POST_LIMIT,
    DEFAULT_NEWS_LIMIT,
    NEWS_DATA_DIR,
    NEWS_SOURCES,
    TELETHON_SESSION_PATH,
    NewsSource,
)


LOGGER = logging.getLogger(__name__)


class NewsFetcherAuthorizationError(RuntimeError):
    """Raised when the Telethon client requires a user login."""


@dataclass(slots=True)
class NewsDigest:
    """Compiled news file plus its grouped summary."""

    file_path: Path
    raw_text: str
    grouped_summary: str
    post_count: int
    source_name: str


@dataclass(slots=True)
class NewsReferenceFile:
    """Compiled raw posts file for a single news source."""

    file_path: Path
    raw_text: str
    post_count: int
    source_key: str
    source_name: str


class NewsFetcher:
    """Fetch the latest news posts from a public Telegram channel."""

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        summarizer: GeminiSummarizer,
    ) -> None:
        self._client = TelegramClient(str(TELETHON_SESSION_PATH), api_id, api_hash)
        self._summarizer = summarizer

    async def _connect_and_fetch_messages(
        self,
        source: NewsSource,
        limit: int,
    ) -> list:
        """Return the latest Telegram messages for a source."""
        try:
            await self._client.connect()
            if not await self._client.is_user_authorized():
                raise NewsFetcherAuthorizationError(
                    "Telethon session is not authorized. Run `python auth_telethon.py` first."
                )

            return await self._client.get_messages(source.username, limit=limit)
        except NewsFetcherAuthorizationError:
            raise
        except RPCError as exc:
            LOGGER.exception("Telethon RPC failure while fetching channel posts")
            raise RuntimeError("Failed to fetch channel posts from Telegram.") from exc
        except Exception as exc:
            LOGGER.exception("Unexpected failure while fetching channel posts")
            raise RuntimeError("Unexpected error while fetching Telegram news.") from exc
        finally:
            await self._client.disconnect()

    @staticmethod
    def _compile_messages(source: NewsSource, messages: list) -> tuple[str, int]:
        """Convert Telegram messages into a structured plain-text file."""
        text_messages = [message for message in messages if (message.message or "").strip()]
        structured_lines: list[str] = []

        for index, message in enumerate(reversed(text_messages), start=1):
            timestamp = message.date.astimezone(timezone.utc)
            text = (message.message or "").strip()
            structured_lines.extend(
                [
                    f"Source: {source.display_name}",
                    f"Post {index}",
                    f"Date (UTC): {timestamp:%Y-%m-%d}",
                    f"Time (UTC): {timestamp:%H:%M:%S}",
                    f"Link: {source.channel_url}/{message.id}",
                    "Text:",
                    text,
                    "",
                    "=" * 60,
                    "",
                ]
            )

        return "\n".join(structured_lines).strip(), len(text_messages)

    async def build_reference_file(
        self,
        source: NewsSource,
        limit: int = AI_REFERENCE_POST_LIMIT,
    ) -> NewsReferenceFile:
        """Fetch the latest posts and store them as a source-specific reference file."""
        messages = await self._connect_and_fetch_messages(source, limit)
        compiled_text, post_count = self._compile_messages(source, messages)
        file_path = NEWS_DATA_DIR / f"{source.key}_latest_{limit}.txt"
        file_path.write_text(compiled_text, encoding="utf-8")
        return NewsReferenceFile(
            file_path=file_path,
            raw_text=compiled_text,
            post_count=post_count,
            source_key=source.key,
            source_name=source.display_name,
        )

    async def refresh_reference_files(
        self,
        limit: int = AI_REFERENCE_POST_LIMIT,
    ) -> dict[str, NewsReferenceFile]:
        """Build reference files for all configured news sources."""
        reference_files: dict[str, NewsReferenceFile] = {}
        for key, source in NEWS_SOURCES.items():
            reference_files[key] = await self.build_reference_file(source, limit=limit)
        return reference_files

    async def fetch_latest_news(
        self,
        source: NewsSource | None = None,
        limit: int = DEFAULT_NEWS_LIMIT,
    ) -> NewsDigest:
        """Fetch the latest posts, write a structured file, then summarize the compiled feed."""
        selected_source = source or NEWS_SOURCES["iranintl"]
        reference_file = await self.build_reference_file(selected_source, limit=limit)
        grouped_summary = await self._summarizer.summarize_news_digest(
            reference_file.raw_text,
            selected_source.display_name,
        )

        return NewsDigest(
            file_path=reference_file.file_path,
            raw_text=reference_file.raw_text,
            grouped_summary=grouped_summary,
            post_count=reference_file.post_count,
            source_name=selected_source.display_name,
        )
