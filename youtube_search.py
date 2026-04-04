"""YouTube search utilities backed by yt-dlp."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote_plus, urlparse

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
    channel_id: str | None = None
    channel_url: str | None = None
    uploader_id: str | None = None
    uploader_url: str | None = None
    published_timestamp: int | None = None

    @property
    def thumbnail_url(self) -> str:
        """Return the default thumbnail URL for the video."""
        return f"https://i.ytimg.com/vi/{self.video_id}/hqdefault.jpg"

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

    @property
    def published_label(self) -> str | None:
        """Format publish time in a compact relative form."""
        if self.published_timestamp is None:
            return None

        now = datetime.now(timezone.utc)
        published_at = datetime.fromtimestamp(self.published_timestamp, tz=timezone.utc)
        elapsed_seconds = max(int((now - published_at).total_seconds()), 0)

        if elapsed_seconds < 15:
            return "just now"
        if elapsed_seconds < 60:
            return f"{elapsed_seconds}s ago"

        elapsed_minutes = elapsed_seconds // 60
        if elapsed_minutes < 60:
            return f"{elapsed_minutes}m ago"

        elapsed_hours = elapsed_minutes // 60
        if elapsed_hours < 24:
            return f"{elapsed_hours}h ago"

        elapsed_days = elapsed_hours // 24
        if elapsed_days < 7:
            return f"{elapsed_days}d ago"

        elapsed_weeks = elapsed_days // 7
        if elapsed_days < 30:
            return f"{elapsed_weeks}w ago"

        elapsed_months = elapsed_days // 30
        if elapsed_days < 365:
            return f"{elapsed_months}mo ago"

        elapsed_years = elapsed_days // 365
        return f"{elapsed_years}y ago"


@dataclass(slots=True)
class YouTubeChannel:
    """Channel metadata used by the Telegram-only YouTube browser."""

    channel_id: str
    title: str
    channel_url: str
    uploader_id: str | None = None
    uploader_url: str | None = None
    webpage_url: str | None = None
    item_count: int | None = None

    @property
    def browse_url(self) -> str:
        """Return the best canonical base URL for browsing the channel."""
        return self.uploader_url or self.channel_url or self.webpage_url or ""

    @property
    def handle_label(self) -> str:
        """Return a stable handle-like label for the channel."""
        return self.uploader_id or self.channel_id


@dataclass(slots=True)
class YouTubePlaylist:
    """Playlist metadata used by the Telegram-only YouTube browser."""

    playlist_id: str
    title: str
    webpage_url: str
    channel: str
    channel_id: str | None = None
    uploader_id: str | None = None


class YouTubeSearcher:
    """Search YouTube via yt-dlp."""

    def __init__(self) -> None:
        self._publish_timestamp_cache: dict[str, int | None] = {}

    async def search(self, query: str, limit: int = YOUTUBE_RESULT_LIMIT) -> list[YouTubeVideo]:
        """Return the top YouTube videos for the query."""
        return await asyncio.to_thread(self._search_sync, query, limit)

    async def get_channel(
        self,
        channel_url: str,
    ) -> YouTubeChannel:
        """Return normalized channel metadata for a YouTube channel."""
        return await asyncio.to_thread(self._get_channel_sync, channel_url)

    async def browse_channel_videos(
        self,
        channel_url: str,
        *,
        sort: str = "latest",
    ) -> tuple[YouTubeChannel, list[YouTubeVideo]]:
        """Return channel metadata plus videos from one channel tab."""
        return await asyncio.to_thread(self._browse_channel_videos_sync, channel_url, sort)

    async def browse_channel_playlists(
        self,
        channel_url: str,
    ) -> tuple[YouTubeChannel, list[YouTubePlaylist]]:
        """Return channel metadata plus playlists from the channel."""
        return await asyncio.to_thread(self._browse_channel_playlists_sync, channel_url)

    async def search_in_channel(
        self,
        channel_url: str,
        query: str,
    ) -> tuple[YouTubeChannel, list[YouTubeVideo]]:
        """Return channel metadata plus in-channel search results."""
        return await asyncio.to_thread(self._search_in_channel_sync, channel_url, query)

    async def browse_playlist_videos(
        self,
        playlist_url: str,
    ) -> tuple[YouTubePlaylist, list[YouTubeVideo]]:
        """Return playlist metadata plus playlist videos."""
        return await asyncio.to_thread(self._browse_playlist_videos_sync, playlist_url)

    async def hydrate_publish_times(
        self,
        videos: list[YouTubeVideo],
    ) -> list[YouTubeVideo]:
        """Populate missing publish timestamps for the given videos."""
        pending_ids = [
            video.video_id
            for video in videos
            if video.published_timestamp is None and video.video_id
        ]
        unique_ids = list(dict.fromkeys(pending_ids))
        if unique_ids:
            semaphore = asyncio.Semaphore(4)

            async def load(video_id: str) -> tuple[str, int | None]:
                async with semaphore:
                    timestamp = await asyncio.to_thread(
                        self._fetch_video_publish_timestamp_sync,
                        video_id,
                    )
                    return video_id, timestamp

            for video_id, timestamp in await asyncio.gather(*(load(video_id) for video_id in unique_ids)):
                self._publish_timestamp_cache[video_id] = timestamp

        for video in videos:
            if video.published_timestamp is None and video.video_id in self._publish_timestamp_cache:
                video.published_timestamp = self._publish_timestamp_cache[video.video_id]
        return videos

    def _search_sync(self, query: str, limit: int) -> list[YouTubeVideo]:
        normalized_query = query.strip()
        options = self._build_options(default_search="ytsearch", noplaylist=True)

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

    def _get_channel_sync(self, channel_url: str) -> YouTubeChannel:
        info = self._extract_flat_info(self._build_channel_tab_url(channel_url, "latest"))
        return self._parse_channel(info)

    def _browse_channel_videos_sync(
        self,
        channel_url: str,
        sort: str,
    ) -> tuple[YouTubeChannel, list[YouTubeVideo]]:
        tab_name = "popular" if sort == "popular" else "latest"
        info = self._extract_flat_info(self._build_channel_tab_url(channel_url, tab_name))
        channel = self._parse_channel(info)
        return channel, self._parse_results(info, fallback_channel=channel)

    def _browse_channel_playlists_sync(
        self,
        channel_url: str,
    ) -> tuple[YouTubeChannel, list[YouTubePlaylist]]:
        info = self._extract_flat_info(self._build_channel_tab_url(channel_url, "playlists"))
        channel = self._parse_channel(info)
        return channel, self._parse_playlists(info, fallback_channel=channel)

    def _search_in_channel_sync(
        self,
        channel_url: str,
        query: str,
    ) -> tuple[YouTubeChannel, list[YouTubeVideo]]:
        info = self._extract_flat_info(
            self._build_channel_tab_url(channel_url, "search", query=query.strip())
        )
        channel = self._parse_channel(info)
        return channel, self._parse_results(info, fallback_channel=channel)

    def _browse_playlist_videos_sync(
        self,
        playlist_url: str,
    ) -> tuple[YouTubePlaylist, list[YouTubeVideo]]:
        info = self._extract_flat_info(playlist_url)
        playlist = self._parse_playlist(info)
        fallback_channel = YouTubeChannel(
            channel_id=playlist.channel_id or "",
            title=playlist.channel or "Unknown",
            channel_url=self._derive_channel_url(info) or "",
            uploader_id=playlist.uploader_id,
            uploader_url=info.get("uploader_url"),
            webpage_url=playlist.webpage_url,
            item_count=info.get("playlist_count"),
        )
        return playlist, self._parse_results(info, fallback_channel=fallback_channel)

    def _extract_flat_info(self, url: str) -> dict:
        """Extract a flat tab/search/playlist view via yt-dlp."""
        with yt_dlp.YoutubeDL(self._build_options(extract_flat=True)) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise RuntimeError("YouTube returned an unexpected result.")
        return info

    @staticmethod
    def _build_options(
        *,
        extract_flat: bool = True,
        default_search: str | None = None,
        noplaylist: bool = False,
    ) -> dict[str, object]:
        """Build shared yt-dlp options for YouTube metadata fetches."""
        options: dict[str, object] = {
            "quiet": True,
            "skip_download": True,
            "extract_flat": extract_flat,
            "nocheckcertificate": True,
            "geo_bypass": True,
        }
        if default_search:
            options["default_search"] = default_search
        if noplaylist:
            options["noplaylist"] = True

        js_runtimes = get_yt_dlp_js_runtimes()
        if js_runtimes:
            options["js_runtimes"] = js_runtimes
        remote_components = get_yt_dlp_remote_components()
        if remote_components:
            options["remote_components"] = remote_components
        return options

    @staticmethod
    def _build_channel_tab_url(
        channel_url: str,
        tab_name: str,
        *,
        query: str | None = None,
    ) -> str:
        """Build the best-effort YouTube channel tab URL for browsing."""
        base = channel_url.rstrip("/")
        if tab_name == "playlists":
            return f"{base}/playlists"
        if tab_name == "search":
            encoded = quote_plus(query or "")
            return f"{base}/search?query={encoded}"
        if tab_name == "popular":
            return f"{base}/videos?view=0&sort=p&flow=grid"
        return f"{base}/videos"

    def _parse_results(
        self,
        info: dict | None,
        *,
        fallback_channel: YouTubeChannel | None = None,
    ) -> list[YouTubeVideo]:
        """Normalize yt-dlp search output into result objects."""
        entries = info.get("entries", []) if isinstance(info, dict) else []
        results: list[YouTubeVideo] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not self._is_video_entry(entry):
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
                    channel=(
                        entry.get("channel")
                        or entry.get("uploader")
                        or (fallback_channel.title if fallback_channel else None)
                        or "Unknown"
                    ),
                    webpage_url=webpage_url,
                    channel_id=(
                        entry.get("channel_id")
                        or (fallback_channel.channel_id if fallback_channel else None)
                    ),
                    channel_url=(
                        entry.get("channel_url")
                        or entry.get("uploader_url")
                        or (fallback_channel.browse_url if fallback_channel else None)
                    ),
                    uploader_id=(
                        entry.get("uploader_id")
                        or (fallback_channel.uploader_id if fallback_channel else None)
                    ),
                    uploader_url=(
                        entry.get("uploader_url")
                        or (fallback_channel.uploader_url if fallback_channel else None)
                    ),
                    published_timestamp=self._normalize_timestamp(
                        entry.get("timestamp") or entry.get("release_timestamp"),
                        entry.get("upload_date"),
                    ),
                )
            )
        return results

    def _parse_channel(self, info: dict | None) -> YouTubeChannel:
        """Normalize yt-dlp channel-level metadata."""
        if not isinstance(info, dict):
            raise RuntimeError("Invalid YouTube channel metadata.")

        channel_id = str(info.get("channel_id") or info.get("id") or "")
        title = str(
            info.get("channel")
            or info.get("uploader")
            or self._strip_channel_suffix(info.get("title"))
            or "Unknown channel"
        )
        channel_url = self._derive_channel_url(info) or ""
        if not channel_id and not channel_url:
            raise RuntimeError("Missing YouTube channel identity.")

        return YouTubeChannel(
            channel_id=channel_id or channel_url,
            title=title,
            channel_url=channel_url or str(info.get("webpage_url") or ""),
            uploader_id=(
                str(info.get("uploader_id"))
                if info.get("uploader_id")
                else None
            ),
            uploader_url=(
                str(info.get("uploader_url"))
                if info.get("uploader_url")
                else None
            ),
            webpage_url=(
                str(info.get("webpage_url"))
                if info.get("webpage_url")
                else None
            ),
            item_count=self._normalize_duration(info.get("playlist_count")),
        )

    def _parse_playlists(
        self,
        info: dict | None,
        *,
        fallback_channel: YouTubeChannel | None = None,
    ) -> list[YouTubePlaylist]:
        """Normalize a channel playlist tab into playlist objects."""
        entries = info.get("entries", []) if isinstance(info, dict) else []
        results: list[YouTubePlaylist] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            playlist_id = str(entry.get("id") or self._extract_playlist_id(entry.get("url")) or "")
            title = str(entry.get("title") or "").strip()
            if not playlist_id or not title:
                continue
            webpage_url = str(
                entry.get("webpage_url")
                or entry.get("url")
                or f"https://www.youtube.com/playlist?list={playlist_id}"
            )
            if "playlist?list=" not in webpage_url:
                webpage_url = f"https://www.youtube.com/playlist?list={playlist_id}"
            results.append(
                YouTubePlaylist(
                    playlist_id=playlist_id,
                    title=title,
                    webpage_url=webpage_url,
                    channel=(
                        entry.get("channel")
                        or entry.get("uploader")
                        or (fallback_channel.title if fallback_channel else None)
                        or "Unknown"
                    ),
                    channel_id=(
                        str(entry.get("channel_id"))
                        if entry.get("channel_id")
                        else (fallback_channel.channel_id if fallback_channel else None)
                    ),
                    uploader_id=(
                        str(entry.get("uploader_id"))
                        if entry.get("uploader_id")
                        else (fallback_channel.uploader_id if fallback_channel else None)
                    ),
                )
            )
        return results

    def _parse_playlist(self, info: dict | None) -> YouTubePlaylist:
        """Normalize top-level playlist metadata."""
        if not isinstance(info, dict):
            raise RuntimeError("Invalid YouTube playlist metadata.")
        playlist_id = str(info.get("id") or self._extract_playlist_id(info.get("webpage_url")) or "")
        title = str(info.get("title") or "Playlist")
        webpage_url = str(
            info.get("webpage_url")
            or f"https://www.youtube.com/playlist?list={playlist_id}"
        )
        if not playlist_id:
            raise RuntimeError("Missing YouTube playlist id.")
        return YouTubePlaylist(
            playlist_id=playlist_id,
            title=title,
            webpage_url=webpage_url,
            channel=str(info.get("channel") or info.get("uploader") or "Unknown"),
            channel_id=(
                str(info.get("channel_id"))
                if info.get("channel_id")
                else None
            ),
            uploader_id=(
                str(info.get("uploader_id"))
                if info.get("uploader_id")
                else None
            ),
        )

    @staticmethod
    def _derive_channel_url(info: dict[str, object]) -> str | None:
        """Pick the best channel URL from yt-dlp metadata."""
        uploader_url = info.get("uploader_url")
        if isinstance(uploader_url, str) and uploader_url:
            return uploader_url
        channel_url = info.get("channel_url")
        if isinstance(channel_url, str) and channel_url:
            return channel_url
        webpage_url = info.get("webpage_url")
        if not isinstance(webpage_url, str) or not webpage_url:
            return None

        stripped = webpage_url.split("?", maxsplit=1)[0].rstrip("/")
        for suffix in ("/videos", "/playlists", "/search"):
            if stripped.endswith(suffix):
                return stripped[: -len(suffix)]
        return stripped

    @staticmethod
    def _strip_channel_suffix(title: object) -> str | None:
        """Remove tab suffixes from channel tab titles."""
        if not isinstance(title, str) or not title.strip():
            return None
        stripped = title.strip()
        for suffix in (" - Videos", " - Playlists"):
            if stripped.endswith(suffix):
                return stripped[: -len(suffix)]
        if " - Search - " in stripped:
            return stripped.split(" - Search - ", maxsplit=1)[0]
        return stripped

    @staticmethod
    def _extract_playlist_id(url: object) -> str | None:
        """Extract a playlist id from a YouTube playlist URL."""
        if not isinstance(url, str) or not url:
            return None
        parsed = urlparse(url)
        return parse_qs(parsed.query).get("list", [None])[0]

    def _fetch_video_publish_timestamp_sync(self, video_id: str) -> int | None:
        """Fetch one video's publish timestamp via a non-flat yt-dlp lookup."""
        if video_id in self._publish_timestamp_cache:
            return self._publish_timestamp_cache[video_id]

        source_url = f"https://www.youtube.com/watch?v={video_id}"
        options = self._build_options(extract_flat=False, noplaylist=True)
        options["socket_timeout"] = 30
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(source_url, download=False)
        except Exception:
            LOGGER.debug("Failed to fetch publish timestamp for %s", video_id, exc_info=True)
            self._publish_timestamp_cache[video_id] = None
            return None

        raw_timestamp = None
        raw_upload_date = None
        if isinstance(info, dict):
            raw_timestamp = info.get("timestamp") or info.get("release_timestamp")
            raw_upload_date = info.get("upload_date")
        timestamp = self._normalize_timestamp(
            raw_timestamp,
            raw_upload_date,
        )
        self._publish_timestamp_cache[video_id] = timestamp
        return timestamp

    @staticmethod
    def _is_video_entry(entry: dict[str, object]) -> bool:
        """Return whether a flat yt-dlp search entry represents a real video result."""
        ie_key = str(entry.get("ie_key") or "")
        if ie_key and ie_key != "Youtube":
            return False

        url = str(entry.get("webpage_url") or entry.get("url") or "")
        if "watch?v=" in url or url.startswith("/watch?v="):
            return True

        video_id = str(entry.get("id") or "")
        if not video_id:
            return False
        return not video_id.startswith(("UC", "PL", "OLAK"))

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

    @staticmethod
    def _normalize_timestamp(value: object, upload_date: object = None) -> int | None:
        """Convert yt-dlp timestamp/upload_date values to epoch seconds."""
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(upload_date, str) and len(upload_date) == 8 and upload_date.isdigit():
            try:
                parsed = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                return None
            return int(parsed.timestamp())
        return None
