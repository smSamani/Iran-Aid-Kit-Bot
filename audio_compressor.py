"""Compress audio files to MP3 with ffmpeg."""

from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


MAX_AUDIO_PART_BYTES = int(15.99 * 1024 * 1024)
SUPPORTED_AUDIO_SUFFIXES = {".m4a", ".mp3"}
MP3_VBR_QUALITY_LEVELS = (4, 5, 6, 7, 8, 9)


@dataclass(slots=True)
class AudioCompressionResult:
    """Metadata for a completed audio compression job."""

    output_path: Path
    original_size_bytes: int
    compressed_size_bytes: int

    @property
    def reduction_percent(self) -> float:
        """Return the percentage size reduction."""
        if self.original_size_bytes <= 0:
            return 0.0
        saved = self.original_size_bytes - self.compressed_size_bytes
        return max(0.0, (saved / self.original_size_bytes) * 100)


def compress_audio_to_mp3(input_path: Path, output_dir: Path) -> AudioCompressionResult:
    """Compress a supported audio file into an MP3 using ffmpeg VBR quality 4."""
    if not input_path.exists():
        raise FileNotFoundError(f"Input audio file not found: {input_path}")

    suffix = input_path.suffix.lower()
    if suffix not in SUPPORTED_AUDIO_SUFFIXES:
        raise ValueError("Unsupported audio format. Supported formats: .m4a, .mp3")

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed or not available in PATH.")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{input_path.stem}_compressed.mp3"
    if output_path.exists():
        output_path.unlink()

    original_size_bytes = input_path.stat().st_size
    selected_candidate: Path | None = None
    first_error: RuntimeError | None = None

    for quality in MP3_VBR_QUALITY_LEVELS:
        candidate_path = output_dir / f"{input_path.stem}_compressed_q{quality}.mp3"
        _encode_audio_with_quality(
            ffmpeg_path=ffmpeg_path,
            input_path=input_path,
            output_path=candidate_path,
            quality=quality,
        )
        if not candidate_path.exists():
            raise RuntimeError("Audio compression failed: output file was not created.")

        if candidate_path.stat().st_size < original_size_bytes:
            selected_candidate = candidate_path
            break

        if first_error is None:
            first_error = RuntimeError(
                "Audio compression did not reduce the file size with the default quality."
            )

    if selected_candidate is None:
        if first_error is not None:
            raise RuntimeError(
                "This audio file is already highly compressed and could not be reduced further."
            ) from first_error
        raise RuntimeError("Audio compression failed: output file was not created.")

    if selected_candidate != output_path:
        selected_candidate.replace(output_path)

    return AudioCompressionResult(
        output_path=output_path,
        original_size_bytes=original_size_bytes,
        compressed_size_bytes=output_path.stat().st_size,
    )


def split_audio_for_telegram(
    input_path: Path,
    output_dir: Path,
    *,
    max_size_bytes: int = MAX_AUDIO_PART_BYTES,
) -> list[Path]:
    """Split a compressed MP3 into the minimum number of Telegram-sized parts."""
    if not input_path.exists():
        raise FileNotFoundError(f"Compressed audio file not found: {input_path}")

    ffmpeg_path = _require_binary("ffmpeg")
    ffprobe_path = _require_binary("ffprobe")
    output_dir.mkdir(parents=True, exist_ok=True)

    if input_path.stat().st_size <= max_size_bytes:
        output_path = output_dir / "part_0.mp3"
        if input_path != output_path:
            shutil.copyfile(input_path, output_path)
        return [output_path]

    duration_seconds = _get_media_duration(input_path, ffprobe_path=ffprobe_path)
    if duration_seconds <= 0:
        raise RuntimeError("Audio split failed: invalid duration.")

    minimum_parts = math.ceil(input_path.stat().st_size / max_size_bytes)
    maximum_parts = max(minimum_parts * 2, minimum_parts + 16)

    for part_count in range(minimum_parts, maximum_parts + 1):
        outputs = _split_audio_into_parts(
            input_path,
            output_dir,
            part_count=part_count,
            duration_seconds=duration_seconds,
            ffmpeg_path=ffmpeg_path,
        )
        if outputs and all(path.stat().st_size <= max_size_bytes for path in outputs):
            return outputs

        for output_path in outputs:
            if output_path.exists():
                output_path.unlink()

    raise RuntimeError("Audio split failed: could not fit all parts below 15.99MB.")


def _encode_audio_with_quality(
    *,
    ffmpeg_path: str,
    input_path: Path,
    output_path: Path,
    quality: int,
) -> None:
    """Encode a candidate MP3 output at the requested VBR quality level."""
    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        str(quality),
        str(output_path),
    ]

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        error_text = stderr.splitlines()[-1] if stderr else "Unknown ffmpeg error."
        raise RuntimeError(f"Audio compression failed: {error_text}")


def _split_audio_into_parts(
    input_path: Path,
    output_dir: Path,
    *,
    part_count: int,
    duration_seconds: float,
    ffmpeg_path: str,
) -> list[Path]:
    """Split an MP3 into a fixed number of ordered parts without re-encoding."""
    part_duration = duration_seconds / part_count
    outputs: list[Path] = []

    for index in range(part_count):
        start_time = index * part_duration
        current_duration = (
            duration_seconds - start_time if index == part_count - 1 else part_duration
        )
        output_path = output_dir / f"part_{index}.mp3"
        if output_path.exists():
            output_path.unlink()

        command = [
            ffmpeg_path,
            "-y",
            "-ss",
            str(start_time),
            "-t",
            str(current_duration),
            "-i",
            str(input_path),
            "-vn",
            "-c",
            "copy",
            str(output_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            error_text = stderr.splitlines()[-1] if stderr else "Unknown ffmpeg error."
            raise RuntimeError(f"Audio split failed: {error_text}")
        if output_path.exists() and output_path.stat().st_size > 0:
            outputs.append(output_path)

    return outputs


def _get_media_duration(input_path: Path, *, ffprobe_path: str) -> float:
    """Read media duration via ffprobe."""
    completed = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        error_text = stderr.splitlines()[-1] if stderr else "Unknown ffprobe error."
        raise RuntimeError(f"Audio split failed: {error_text}")
    return float((completed.stdout or "0").strip() or "0")


def _require_binary(name: str) -> str:
    """Return a required binary path from PATH."""
    binary_path = shutil.which(name)
    if binary_path is None:
        raise RuntimeError(f"{name} is not installed or not available in PATH.")
    return binary_path
