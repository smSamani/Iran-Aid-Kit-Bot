"""YouTube search utilities backed by yt-dlp."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import quote_plus

import yt_dlp

from config import (
    YOUTUBE_RESULT_LIMIT,
    get_yt_dlp_js_runtimes,
    get_yt_dlp_remote_components,
)


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class YouTubeVideo:
    """YouTube search result item."""

    video_id: str
    title: str
    duration_seconds: int | None
    channel: str
    webpage_url: str

    @property
    def duration_label(self) -> str:
        """Format the duration as HH:MM:SS or MM:SS."""
        if self.duration_seconds is None:
            return "Unknown"

        total_seconds = max(int(self.duration_seconds), 0)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


class YouTubeSearcher:
    """Search YouTube via yt-dlp."""

    async def search(self, query: str, limit: int = YOUTUBE_RESULT_LIMIT) -> list[YouTubeVideo]:
        """Return the top YouTube videos for the query."""
        return await asyncio.to_thread(self._search_sync, query, limit)

    def _search_sync(self, query: str, limit: int) -> list[YouTubeVideo]:
        normalized_query = query.strip()
        options = {
            "quiet": True,
            "skip_download": True,
            "extract_flat": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "default_search": "ytsearch",
            "geo_bypass": True,
        }
        js_runtimes = get_yt_dlp_js_runtimes()
        if js_runtimes:
            options["js_runtimes"] = js_runtimes
        remote_components = get_yt_dlp_remote_components()
        if remote_components:
            options["remote_components"] = remote_components

        search_targets = [
            f"ytsearch{limit}:{normalized_query}",
            f"https://www.youtube.com/results?search_query={quote_plus(normalized_query)}",
        ]

        last_error: Exception | None = None
        for target in search_targets:
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(target, download=False)
            except Exception as exc:
                last_error = exc
                LOGGER.warning("YouTube search attempt failed for target %s: %s", target, exc)
                continue

            results = self._parse_results(info)
            if results:
                return results[:limit]

        LOGGER.exception("YouTube search failed", exc_info=last_error)
        raise RuntimeError("Failed to search YouTube.")

    def _parse_results(self, info: dict | None) -> list[YouTubeVideo]:
        """Normalize yt-dlp search output into result objects."""
        entries = info.get("entries", []) if isinstance(info, dict) else []
        results: list[YouTubeVideo] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            video_id = entry.get("id") or self._extract_video_id(entry.get("url"))
            title = entry.get("title")
            if not video_id or not title:
                continue

            webpage_url = (
                entry.get("webpage_url")
                or entry.get("url")
                or f"https://www.youtube.com/watch?v={video_id}"
            )
            if "watch?v=" not in webpage_url:
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"

            results.append(
                YouTubeVideo(
                    video_id=video_id,
                    title=title,
                    duration_seconds=self._normalize_duration(entry.get("duration")),
                    channel=entry.get("channel") or entry.get("uploader") or "Unknown",
                    webpage_url=webpage_url,
                )
            )
        return results

    @staticmethod
    def _extract_video_id(url: str | None) -> str | None:
        """Extract a YouTube video ID from a flat search result URL."""
        if not url:
            return None
        if "watch?v=" in url:
            return url.split("watch?v=", maxsplit=1)[1].split("&", maxsplit=1)[0]
        if url.startswith("/watch?v="):
            return url.split("/watch?v=", maxsplit=1)[1].split("&", maxsplit=1)[0]
        return None

    @staticmethod
    def _normalize_duration(value: object) -> int | None:
        """Convert yt-dlp duration values to integer seconds."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return max(int(value), 0)
        return None
