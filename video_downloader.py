"""Download and package YouTube videos for Telegram delivery."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from config import (
    DOWNLOADS_DIR,
    LOCAL_BOT_API_UPLOAD_LIMIT_BYTES,
    TELEGRAM_UPLOAD_LIMIT_BYTES,
    get_yt_dlp_js_runtimes,
    get_yt_dlp_remote_components,
)


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DownloadedVideo:
    """Download result for a requested video."""

    title: str
    source_url: str
    file_path: Path | None = None
    file_size_bytes: int | None = None
    exceeds_telegram_limit: bool = False


class VideoDownloader:
    """Download YouTube videos as MP4 files."""

    def __init__(self, allow_large_uploads: bool = False) -> None:
        self._allow_large_uploads = allow_large_uploads

    async def download_video(self, video_id: str) -> DownloadedVideo:
        """Download a YouTube video and return its local metadata."""
        return await asyncio.to_thread(self._download_sync, video_id)

    def _download_sync(self, video_id: str) -> DownloadedVideo:
        source_url = f"https://www.youtube.com/watch?v={video_id}"
        template = str(DOWNLOADS_DIR / "%(id)s.%(ext)s")

        info_options = {
            "quiet": True,
            "skip_download": True,
            "noplaylist": True,
            "nocheckcertificate": True,
        }
        download_options = {
            "quiet": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": template,
            "restrictfilenames": True,
            "overwrites": True,
        }
        js_runtimes = get_yt_dlp_js_runtimes()
        if js_runtimes:
            info_options["js_runtimes"] = js_runtimes
            download_options["js_runtimes"] = js_runtimes
        remote_components = get_yt_dlp_remote_components()
        if remote_components:
            info_options["remote_components"] = remote_components
            download_options["remote_components"] = remote_components

        try:
            with yt_dlp.YoutubeDL(info_options) as ydl:
                info = ydl.extract_info(source_url, download=False)
        except Exception as exc:
            LOGGER.exception("Failed to fetch video metadata")
            raise RuntimeError("Failed to fetch video metadata.") from exc

        title = info.get("title") or video_id
        selected_format_id = self._select_best_format(info)
        if selected_format_id is None:
            return DownloadedVideo(
                title=title,
                source_url=source_url,
                exceeds_telegram_limit=True,
            )

        download_options["format"] = selected_format_id
        try:
            with yt_dlp.YoutubeDL(download_options) as ydl:
                ydl.extract_info(source_url, download=True)
        except Exception as exc:
            LOGGER.exception("Video download failed")
            raise RuntimeError("Failed to download the selected video.") from exc

        candidates = sorted(DOWNLOADS_DIR.glob(f"{video_id}*.mp4"))
        if not candidates:
            raise RuntimeError("The video was downloaded, but the MP4 output was not found.")

        file_path = candidates[-1]
        file_size = file_path.stat().st_size
        return DownloadedVideo(
            title=title,
            source_url=source_url,
            file_path=file_path,
            file_size_bytes=file_size,
            exceeds_telegram_limit=file_size > TELEGRAM_UPLOAD_LIMIT_BYTES,
        )

    def _select_best_format(self, info: dict[str, Any]) -> str | None:
        """Pick the best downloadable muxed format that fits the active upload cap."""
        formats = info.get("formats") or []
        candidates: list[tuple[tuple[int, float, int, int], str]] = []
        upload_limit = (
            LOCAL_BOT_API_UPLOAD_LIMIT_BYTES
            if self._allow_large_uploads
            else TELEGRAM_UPLOAD_LIMIT_BYTES
        )

        for fmt in formats:
            if not isinstance(fmt, dict):
                continue

            if fmt.get("vcodec") in {None, "none"} or fmt.get("acodec") in {None, "none"}:
                continue

            size = self._estimate_format_size_bytes(fmt, info.get("duration"))
            if not isinstance(size, int) or size > upload_limit:
                continue

            format_id = fmt.get("format_id")
            if not format_id:
                continue

            ext = fmt.get("ext") or ""
            height = int(fmt.get("height") or 0)
            tbr = float(fmt.get("tbr") or 0.0)
            mp4_bonus = 1 if ext == "mp4" else 0
            candidates.append(((height, tbr, mp4_bonus, size), format_id))

        if not candidates:
            LOGGER.warning("No downloadable format under %s bytes", upload_limit)
            return None

        candidates.sort(reverse=True)
        return candidates[0][1]

    @staticmethod
    def _estimate_format_size_bytes(fmt: dict[str, Any], duration: Any) -> int | None:
        """Estimate the final file size for a format using the best available metadata."""
        exact_size = fmt.get("filesize")
        if isinstance(exact_size, int):
            return exact_size

        approx_size = fmt.get("filesize_approx")
        if isinstance(approx_size, int):
            return approx_size

        if not isinstance(duration, (int, float)) or duration <= 0:
            return None

        tbr = fmt.get("tbr")
        if not isinstance(tbr, (int, float)) or tbr <= 0:
            return None

        estimated_bytes = int((float(tbr) * 1000 / 8) * float(duration))
        return estimated_bytes if estimated_bytes > 0 else None
