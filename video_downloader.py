"""Download and package YouTube videos for Telegram delivery."""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yt_dlp

from config import (
    DOWNLOADS_DIR,
    LOCAL_BOT_API_UPLOAD_LIMIT_BYTES,
    TELEGRAM_UPLOAD_LIMIT_BYTES,
    get_yt_dlp_js_runtimes,
    get_yt_dlp_remote_components,
)


LOGGER = logging.getLogger(__name__)
MEDIA_KIND_VIDEO = "video"
MEDIA_KIND_AUDIO = "audio"


class DownloadCancelledError(RuntimeError):
    """Raised when a YouTube download is cancelled by the user."""


@dataclass(slots=True)
class DownloadedVideo:
    """Download result for a requested video."""

    title: str
    source_url: str
    file_path: Path | None = None
    file_size_bytes: int | None = None
    exceeds_telegram_limit: bool = False
    media_kind: str = MEDIA_KIND_VIDEO


@dataclass(slots=True)
class DownloadOption:
    """A user-selectable download quality option."""

    format_selector: str
    label: str
    estimated_size_bytes: int
    media_kind: str = MEDIA_KIND_VIDEO


class VideoDownloader:
    """Download YouTube videos as MP4 files."""

    def __init__(self, allow_large_uploads: bool = False) -> None:
        self._allow_large_uploads = allow_large_uploads

    def set_allow_large_uploads(self, allow_large_uploads: bool) -> None:
        """Update whether large Telegram uploads are currently available."""
        self._allow_large_uploads = allow_large_uploads

    async def download_video(
        self,
        video_id: str,
        format_selector: str | None = None,
        media_kind: str = MEDIA_KIND_VIDEO,
        progress_callback: Callable[[str], object] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> DownloadedVideo:
        """Download a YouTube video and return its local metadata."""
        loop = asyncio.get_running_loop()

        def report_progress(message: str) -> None:
            if progress_callback is None:
                return
            future = asyncio.run_coroutine_threadsafe(progress_callback(message), loop)
            future.add_done_callback(self._log_progress_callback_failure)

        return await asyncio.to_thread(
            self._download_sync,
            video_id,
            format_selector,
            media_kind,
            report_progress,
            cancel_event,
        )

    async def get_download_options(self, video_id: str) -> tuple[str, list[DownloadOption]]:
        """Return the title and quality options available for a video."""
        return await asyncio.to_thread(self._get_download_options_sync, video_id)

    def _download_sync(
        self,
        video_id: str,
        format_selector: str | None = None,
        media_kind: str = MEDIA_KIND_VIDEO,
        report_progress: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> DownloadedVideo:
        source_url = f"https://www.youtube.com/watch?v={video_id}"
        template = str(DOWNLOADS_DIR / "%(id)s.%(ext)s")
        ffmpeg_path = shutil.which("ffmpeg")

        info_options = {
            "quiet": True,
            "skip_download": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "socket_timeout": 30,
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
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
        }
        if ffmpeg_path:
            download_options["ffmpeg_location"] = ffmpeg_path
        js_runtimes = get_yt_dlp_js_runtimes()
        if js_runtimes:
            info_options["js_runtimes"] = js_runtimes
            download_options["js_runtimes"] = js_runtimes
        remote_components = get_yt_dlp_remote_components()
        if remote_components:
            info_options["remote_components"] = remote_components
            download_options["remote_components"] = remote_components

        if report_progress is not None:
            report_progress("در حال بررسی کیفیت‌های ویدیو...")
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download cancelled by user.")

        info = self._fetch_video_info(source_url, info_options)
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download cancelled by user.")
        title = info.get("title") or video_id
        allow_format_merging = bool(ffmpeg_path)
        candidates = self._collect_format_candidates(
            info,
            allow_format_merging=allow_format_merging,
        )
        audio_only_candidate = self._collect_audio_only_candidate(info)
        selectable_candidates = list(candidates)
        if audio_only_candidate is not None:
            selectable_candidates.append(
                (
                    audio_only_candidate[0],
                    audio_only_candidate[1],
                    audio_only_candidate[2],
                    MEDIA_KIND_AUDIO,
                )
            )
        if format_selector is None:
            selected_option = self._resolve_selected_option(
                selectable_candidates,
                format_selector=None,
            )
            if selected_option is None:
                return DownloadedVideo(
                    title=title,
                    source_url=source_url,
                    exceeds_telegram_limit=True,
                    media_kind=media_kind,
                )
            selected_format_id, selected_size_bytes, _selected_label, selected_media_kind = (
                selected_option
            )
        else:
            selected_format_id = format_selector
            selected_media_kind = media_kind
            selected_size_bytes = self._estimate_selected_option_size(
                selectable_candidates,
                format_selector=format_selector,
            )

        download_options["format"] = selected_format_id
        if report_progress is not None:
            media_title = "فایل صوتی" if selected_media_kind == MEDIA_KIND_AUDIO else "ویدیو"
            progress_text = (
                f"دانلود {media_title} شروع شد...\nکیفیت انتخاب‌شده: {selected_format_id}"
            )
            if isinstance(selected_size_bytes, int) and selected_size_bytes > 0:
                estimated_mb = selected_size_bytes / (1024 * 1024)
                progress_text += f"\nحجم تقریبی: {estimated_mb:.1f} MB"
            report_progress(progress_text)
            download_options["progress_hooks"] = [
                self._build_progress_hook(
                    report_progress,
                    media_kind=selected_media_kind,
                    cancel_event=cancel_event,
                )
            ]
        try:
            with yt_dlp.YoutubeDL(download_options) as ydl:
                ydl.extract_info(source_url, download=True)
        except DownloadCancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Video download failed")
            raise RuntimeError("Failed to download the selected video.") from exc

        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download cancelled by user.")
        if report_progress is not None:
            if selected_media_kind == MEDIA_KIND_AUDIO:
                report_progress("دانلود فایل صوتی کامل شد. در حال آماده‌سازی برای ارسال...")
            else:
                report_progress("دانلود کامل شد. در حال آماده‌سازی فایل برای ارسال...")

        candidates = sorted(
            path
            for path in DOWNLOADS_DIR.glob(f"{video_id}*")
            if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".info.json"}
        )
        if not candidates:
            raise RuntimeError("The media was downloaded, but the output file was not found.")

        file_path = max(candidates, key=lambda path: path.stat().st_mtime)
        file_size = file_path.stat().st_size
        return DownloadedVideo(
            title=title,
            source_url=source_url,
            file_path=file_path,
            file_size_bytes=file_size,
            exceeds_telegram_limit=file_size > TELEGRAM_UPLOAD_LIMIT_BYTES,
            media_kind=selected_media_kind,
        )

    def _get_download_options_sync(self, video_id: str) -> tuple[str, list[DownloadOption]]:
        """Return quality options for a given YouTube video."""
        source_url = f"https://www.youtube.com/watch?v={video_id}"
        ffmpeg_path = shutil.which("ffmpeg")

        try:
            info = self._fetch_video_info(
                source_url,
                {
                    "quiet": True,
                    "skip_download": True,
                    "noplaylist": True,
                    "nocheckcertificate": True,
                    "socket_timeout": 30,
                    **(
                        {"js_runtimes": get_yt_dlp_js_runtimes()}
                        if get_yt_dlp_js_runtimes()
                        else {}
                    ),
                    **(
                        {"remote_components": get_yt_dlp_remote_components()}
                        if get_yt_dlp_remote_components()
                        else {}
                    ),
                },
            )
        except Exception as exc:
            LOGGER.exception("Failed to fetch video metadata")
            message = str(exc)
            if "Video unavailable" in message:
                raise RuntimeError("This YouTube result is unavailable right now.") from exc
            raise RuntimeError("Failed to fetch video metadata.") from exc

        title = info.get("title") or video_id
        candidates = self._collect_format_candidates(
            info,
            allow_format_merging=bool(ffmpeg_path),
        )
        audio_only_candidate = self._collect_audio_only_candidate(info)
        if not candidates:
            if audio_only_candidate is None:
                return title, []
            return title, [
                DownloadOption(
                    format_selector=audio_only_candidate[0],
                    estimated_size_bytes=audio_only_candidate[1],
                    label=audio_only_candidate[2],
                    media_kind=MEDIA_KIND_AUDIO,
                )
            ]

        options: list[DownloadOption] = []
        seen_labels: set[str] = set()
        max_video_options = 5 if audio_only_candidate is not None else 6
        for format_selector, estimated_size, label, media_kind in candidates:
            if label in seen_labels:
                continue
            seen_labels.add(label)
            options.append(
                DownloadOption(
                    format_selector=format_selector,
                    estimated_size_bytes=estimated_size,
                    label=label,
                    media_kind=media_kind,
                )
            )
            if len(options) >= max_video_options:
                break
        if audio_only_candidate is not None and audio_only_candidate[2] not in seen_labels:
            options.append(
                DownloadOption(
                    format_selector=audio_only_candidate[0],
                    estimated_size_bytes=audio_only_candidate[1],
                    label=audio_only_candidate[2],
                    media_kind=MEDIA_KIND_AUDIO,
                )
            )
        return title, options

    @staticmethod
    def _fetch_video_info(source_url: str, info_options: dict[str, Any]) -> dict[str, Any]:
        """Fetch video metadata from yt-dlp."""
        with yt_dlp.YoutubeDL(info_options) as ydl:
            info = ydl.extract_info(source_url, download=False)
        if not isinstance(info, dict):
            raise RuntimeError("Unexpected video metadata format.")
        return info

    def _collect_format_candidates(
        self,
        info: dict[str, Any],
        *,
        allow_format_merging: bool,
    ) -> list[tuple[str, int, str, str]]:
        """Collect downloadable format candidates that fit the active upload cap."""
        formats = info.get("formats") or []
        candidates: list[tuple[tuple[int, float, int, int, int], str, int, str]] = []
        upload_limit = (
            LOCAL_BOT_API_UPLOAD_LIMIT_BYTES
            if self._allow_large_uploads
            else TELEGRAM_UPLOAD_LIMIT_BYTES
        )

        audio_formats: list[tuple[dict[str, Any], int]] = []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            size = self._estimate_format_size_bytes(fmt, info.get("duration"))
            if not isinstance(size, int):
                continue
            if fmt.get("vcodec") in {None, "none"} and fmt.get("acodec") not in {None, "none"}:
                audio_formats.append((fmt, size))

        for fmt in formats:
            if not isinstance(fmt, dict):
                continue

            format_id = fmt.get("format_id")
            if not format_id:
                continue

            ext = fmt.get("ext") or ""
            height = int(fmt.get("height") or 0)
            fps = int(fmt.get("fps") or 0)
            tbr = float(fmt.get("tbr") or 0.0)
            size = self._estimate_format_size_bytes(fmt, info.get("duration"))
            if not isinstance(size, int):
                continue

            has_video = fmt.get("vcodec") not in {None, "none"}
            has_audio = fmt.get("acodec") not in {None, "none"}
            compatibility_bonus = 2 if ext == "mp4" else 1 if ext in {"webm", "mkv"} else 0

            if has_video and has_audio and size <= upload_limit:
                label = self._format_quality_label(height, fps, size, merged=False)
                candidates.append(
                    (
                        (height, fps, int(tbr), compatibility_bonus, size),
                        format_id,
                        size,
                        label,
                        MEDIA_KIND_VIDEO,
                    )
                )
                continue

            if not has_video or has_audio:
                continue

            if not allow_format_merging:
                continue

            best_audio = self._pick_best_audio_for_video(
                audio_formats=audio_formats,
                remaining_bytes=upload_limit - size,
            )
            if best_audio is None:
                continue

            audio_fmt, audio_size = best_audio
            audio_id = audio_fmt.get("format_id")
            if not audio_id:
                continue

            total_size = size + audio_size
            audio_tbr = float(audio_fmt.get("tbr") or 0.0)
            pair_bonus = compatibility_bonus + (
                2 if (ext == "mp4" and (audio_fmt.get("ext") or "") == "m4a") else 0
            )
            label = self._format_quality_label(height, fps, total_size, merged=True)
            candidates.append(
                (
                    (height, fps, int(tbr + audio_tbr), pair_bonus, total_size),
                    f"{format_id}+{audio_id}",
                    total_size,
                    label,
                    MEDIA_KIND_VIDEO,
                )
            )

        if not candidates:
            LOGGER.warning("No downloadable format under %s bytes", upload_limit)
            return []

        candidates.sort(reverse=True)
        return [
            (format_selector, estimated_size, label, media_kind)
            for _score, format_selector, estimated_size, label, media_kind in candidates
        ]

    @staticmethod
    def _resolve_selected_option(
        candidates: list[tuple[str, int, str, str]],
        *,
        format_selector: str | None,
    ) -> tuple[str, int, str, str] | None:
        """Resolve the user-selected format or default to the best available candidate."""
        if not candidates:
            return None
        if format_selector is None:
            return candidates[0]
        for candidate in candidates:
            if candidate[0] == format_selector:
                return candidate
        return None

    @staticmethod
    def _estimate_selected_option_size(
        candidates: list[tuple[str, int, str, str]],
        *,
        format_selector: str,
    ) -> int | None:
        """Return the stored estimated size for a selected format when available."""
        for candidate in candidates:
            if candidate[0] == format_selector:
                return candidate[1]
        return None

    @staticmethod
    def _format_quality_label(height: int, fps: int, size_bytes: int, *, merged: bool) -> str:
        """Render a human-readable quality label for the Telegram buttons."""
        size_mb = size_bytes / (1024 * 1024)
        resolution = f"{height}p" if height > 0 else "کیفیت نامشخص"
        fps_label = f" {fps}fps" if fps > 0 else ""
        merge_label = " (HD)" if merged else ""
        return f"{resolution}{fps_label}{merge_label} • {size_mb:.0f}MB"

    @staticmethod
    def _format_audio_label(ext: str, size_bytes: int) -> str:
        """Render a human-readable label for the audio-only option."""
        size_mb = size_bytes / (1024 * 1024)
        ext_label = ext.upper() if ext else "Audio"
        return f"🎵 Audio ({ext_label}) • {size_mb:.0f}MB"

    @staticmethod
    def _log_progress_callback_failure(future: asyncio.Future[object]) -> None:
        """Log background progress callback failures without blocking the download thread."""
        try:
            future.result()
        except Exception:
            LOGGER.debug("Progress callback update failed", exc_info=True)

    @staticmethod
    def _pick_best_audio_for_video(
        audio_formats: list[tuple[dict[str, Any], int]],
        remaining_bytes: int,
    ) -> tuple[dict[str, Any], int] | None:
        """Pick the best-fitting audio stream for a given video-only format."""
        candidates: list[tuple[tuple[int, int], dict[str, Any], int]] = []
        for audio_fmt, audio_size in audio_formats:
            if audio_size > remaining_bytes:
                continue
            ext = audio_fmt.get("ext") or ""
            tbr = int(float(audio_fmt.get("tbr") or 0.0))
            ext_bonus = 1 if ext == "m4a" else 0
            candidates.append(((tbr, ext_bonus), audio_fmt, audio_size))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        _score, audio_fmt, audio_size = candidates[0]
        return audio_fmt, audio_size

    @staticmethod
    def _build_progress_hook(
        report_progress: Callable[[str], None],
        *,
        media_kind: str = MEDIA_KIND_VIDEO,
        cancel_event: threading.Event | None = None,
    ) -> Callable[[dict[str, Any]], None]:
        """Create a throttled yt-dlp progress hook."""
        state = {"last_percent": -10, "reported_finished": False}
        media_label = "فایل صوتی" if media_kind == MEDIA_KIND_AUDIO else "ویدیو"

        def hook(progress: dict[str, Any]) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("Download cancelled by user.")
            status = progress.get("status")
            if status == "downloading":
                total_bytes = progress.get("total_bytes") or progress.get("total_bytes_estimate")
                downloaded_bytes = progress.get("downloaded_bytes") or 0
                if not isinstance(total_bytes, (int, float)) or total_bytes <= 0:
                    return
                percent = int((float(downloaded_bytes) / float(total_bytes)) * 100)
                if percent < state["last_percent"] + 10 and percent < 100:
                    return
                state["last_percent"] = percent
                speed = progress.get("speed")
                eta = progress.get("eta")
                speed_mb = (
                    f"{float(speed) / (1024 * 1024):.1f} MB/s"
                    if isinstance(speed, (int, float)) and speed > 0
                    else "نامشخص"
                )
                eta_text = f"{int(eta)} ثانیه" if isinstance(eta, (int, float)) and eta >= 0 else "نامشخص"
                report_progress(
                    f"در حال دانلود و آماده‌سازی {media_label}...\n"
                    f"پیشرفت: {percent}%\nسرعت: {speed_mb}\nزمان باقی‌مانده: {eta_text}"
                )
            elif status == "finished" and not state["reported_finished"]:
                state["reported_finished"] = True
                if media_kind == MEDIA_KIND_AUDIO:
                    report_progress("دانلود فایل صوتی کامل شد. در حال آماده‌سازی برای ارسال...")
                else:
                    report_progress("دانلود کامل شد. در حال ترکیب صدا و تصویر...")

        return hook

    def _collect_audio_only_candidate(self, info: dict[str, Any]) -> tuple[str, int, str] | None:
        """Pick one audio-only option that fits the active upload limit."""
        formats = info.get("formats") or []
        upload_limit = (
            LOCAL_BOT_API_UPLOAD_LIMIT_BYTES
            if self._allow_large_uploads
            else TELEGRAM_UPLOAD_LIMIT_BYTES
        )
        candidates: list[tuple[tuple[int, int], str, int, str]] = []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            if fmt.get("vcodec") not in {None, "none"}:
                continue
            if fmt.get("acodec") in {None, "none"}:
                continue
            size = self._estimate_format_size_bytes(fmt, info.get("duration"))
            if not isinstance(size, int) or size > upload_limit:
                continue
            format_id = fmt.get("format_id")
            if not format_id:
                continue
            ext = (fmt.get("ext") or "").lower()
            tbr = int(float(fmt.get("tbr") or 0.0))
            ext_bonus = 2 if ext == "m4a" else 1 if ext == "mp3" else 0
            candidates.append(
                (
                    (tbr, ext_bonus),
                    format_id,
                    size,
                    self._format_audio_label(ext, size),
                )
            )

        if not candidates:
            return None

        candidates.sort(reverse=True)
        _score, format_id, size, label = candidates[0]
        return format_id, size, label

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
