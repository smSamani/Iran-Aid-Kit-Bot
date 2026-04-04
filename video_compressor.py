"""Production-ready video compression utilities for Telegram delivery."""

from __future__ import annotations

import math
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


MAX_OUTPUT_PART_BYTES = int(15.99 * 1024 * 1024)
CRF = "23"
PRESET = "fast"
COMPRESSION_RATIO = 0.6
PARALLEL_CHUNKS = 10
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
VIDEO_METHOD_CPU = "cpu"
VIDEO_METHOD_GPU = "gpu"
VIDEO_METHOD_PARALLEL = "parallel"


@dataclass(slots=True)
class VideoArtifact:
    """A single compressed video artifact before Telegram splitting."""

    output_path: Path
    engine_used: str


@dataclass(slots=True)
class VideoCompressionResult:
    """Final metadata for Telegram delivery."""

    output_paths: list[Path]
    original_size_bytes: int
    total_output_size_bytes: int
    engine_used: str

    @property
    def reduction_percent(self) -> float:
        """Return non-negative size reduction percentage."""
        if self.original_size_bytes <= 0:
            return 0.0
        saved = self.original_size_bytes - self.total_output_size_bytes
        return max(0.0, (saved / self.original_size_bytes) * 100)


def compress_video(
    input_path: Path,
    work_dir: Path,
    *,
    method: str,
) -> VideoCompressionResult:
    """Compress a video with the chosen engine and split it for Telegram."""
    artifact = compress_video_artifact(input_path, work_dir, method=method)
    outputs = split_final(artifact.output_path, work_dir)
    return VideoCompressionResult(
        output_paths=outputs,
        original_size_bytes=input_path.stat().st_size,
        total_output_size_bytes=sum(path.stat().st_size for path in outputs),
        engine_used=artifact.engine_used,
    )


def compress_video_artifact(
    input_path: Path,
    work_dir: Path,
    *,
    method: str,
) -> VideoArtifact:
    """Compress a source video and return the single output artifact."""
    _validate_input(input_path)
    ffmpeg_path = _require_binary("ffmpeg")
    ffprobe_path = _require_binary("ffprobe")
    work_dir.mkdir(parents=True, exist_ok=True)

    if method == VIDEO_METHOD_CPU:
        return compress_cpu(input_path, work_dir, ffmpeg_path=ffmpeg_path)
    if method == VIDEO_METHOD_GPU:
        return compress_gpu(
            input_path,
            work_dir,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )
    if method == VIDEO_METHOD_PARALLEL:
        return compress_parallel(
            input_path,
            work_dir,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )
    raise ValueError("Unsupported compression method. Use cpu, gpu, or parallel.")


def compress_cpu(input_path: Path, work_dir: Path, *, ffmpeg_path: str | None = None) -> VideoArtifact:
    """Compress using libx264 CRF mode."""
    ffmpeg_path = ffmpeg_path or _require_binary("ffmpeg")
    output = work_dir / "cpu_output.mp4"
    _run_ffmpeg(
        [
            ffmpeg_path,
            "-y",
            "-i",
            str(input_path),
            "-c:v",
            "libx264",
            "-preset",
            PRESET,
            "-crf",
            CRF,
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
        ],
        "Video compression failed",
    )
    return VideoArtifact(output_path=output, engine_used=VIDEO_METHOD_CPU)


def compress_gpu(
    input_path: Path,
    work_dir: Path,
    *,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
) -> VideoArtifact:
    """Compress using VideoToolbox when available, otherwise fall back to CPU."""
    ffmpeg_path = ffmpeg_path or _require_binary("ffmpeg")
    ffprobe_path = ffprobe_path or _require_binary("ffprobe")
    if not supports_gpu(ffmpeg_path):
        return compress_cpu(input_path, work_dir, ffmpeg_path=ffmpeg_path)

    output = work_dir / "gpu_output.mp4"
    bitrate = get_bitrate(input_path, ffprobe_path=ffprobe_path)
    bitrate = int(bitrate * COMPRESSION_RATIO) if bitrate else 2_500_000
    bitrate_k = f"{max(250, int(bitrate / 1000))}k"
    completed = subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-i",
            str(input_path),
            "-c:v",
            "h264_videotoolbox",
            "-b:v",
            bitrate_k,
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return compress_cpu(input_path, work_dir, ffmpeg_path=ffmpeg_path)
    return VideoArtifact(output_path=output, engine_used=VIDEO_METHOD_GPU)


def compress_parallel(
    input_path: Path,
    work_dir: Path,
    *,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
) -> VideoArtifact:
    """Compress by chunking, encoding chunks with CPU, and merging without re-encode."""
    ffmpeg_path = ffmpeg_path or _require_binary("ffmpeg")
    ffprobe_path = ffprobe_path or _require_binary("ffprobe")
    duration = get_duration(input_path, ffprobe_path=ffprobe_path)
    if duration <= 0:
        raise RuntimeError("Parallel compression failed: invalid duration.")

    part_duration = duration / PARALLEL_CHUNKS
    tasks: list[tuple[str, str, str, float, float]] = []
    start = 0.0
    for index in range(PARALLEL_CHUNKS):
        end = duration if index == PARALLEL_CHUNKS - 1 else start + part_duration
        output = work_dir / f"chunk_{index}.mp4"
        tasks.append((ffmpeg_path, str(input_path), str(output), start, end))
        start = end

    with ThreadPoolExecutor(max_workers=min(PARALLEL_CHUNKS, 4)) as executor:
        chunk_outputs = list(executor.map(_process_chunk, tasks))

    merged_output = work_dir / "parallel_output.mp4"
    merge_all(chunk_outputs, merged_output, ffmpeg_path=ffmpeg_path, work_dir=work_dir)
    return VideoArtifact(output_path=merged_output, engine_used=VIDEO_METHOD_PARALLEL)


def split_final(
    input_path: Path,
    work_dir: Path,
    *,
    max_size_bytes: int = MAX_OUTPUT_PART_BYTES,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
) -> list[Path]:
    """Split a compressed video into ordered Telegram-sized parts without re-encoding."""
    ffmpeg_path = ffmpeg_path or _require_binary("ffmpeg")
    ffprobe_path = ffprobe_path or _require_binary("ffprobe")
    size = input_path.stat().st_size
    if size <= max_size_bytes:
        output = work_dir / "part_0.mp4"
        if input_path != output:
            shutil.copyfile(input_path, output)
        return [output]

    duration = get_duration(input_path, ffprobe_path=ffprobe_path)
    if duration <= 0:
        raise RuntimeError("Video split failed: invalid duration.")

    minimum_parts = math.ceil(size / max_size_bytes)
    maximum_parts = max(minimum_parts * 2, minimum_parts + 16)

    for part_count in range(minimum_parts, maximum_parts + 1):
        part_duration = duration / part_count
        outputs: list[Path] = []
        for index in range(part_count):
            start = index * part_duration
            current_duration = duration - start if index == part_count - 1 else part_duration
            output = work_dir / f"part_{index}.mp4"
            if output.exists():
                output.unlink()
            _run_ffmpeg(
                [
                    ffmpeg_path,
                    "-y",
                    "-ss",
                    str(start),
                    "-t",
                    str(current_duration),
                    "-i",
                    str(input_path),
                    "-c",
                    "copy",
                    str(output),
                ],
                "Video split failed",
            )
            if output.exists() and output.stat().st_size > 0:
                outputs.append(output)

        if outputs and all(path.stat().st_size <= max_size_bytes for path in outputs):
            return outputs

        for output in outputs:
            output.unlink(missing_ok=True)

    raise RuntimeError("Video split failed: could not fit all parts below the size limit.")


def get_duration(input_path: Path, *, ffprobe_path: str | None = None) -> float:
    """Read video duration via ffprobe."""
    ffprobe_path = ffprobe_path or _require_binary("ffprobe")
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
        raise RuntimeError(f"Failed to read video duration: {_extract_ffmpeg_error(completed.stderr)}")
    return float((completed.stdout or "0").strip() or "0")


def get_bitrate(input_path: Path, *, ffprobe_path: str | None = None) -> int:
    """Read source video bitrate via ffprobe."""
    ffprobe_path = ffprobe_path or _require_binary("ffprobe")
    completed = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=bit_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return 0
    return int((completed.stdout or "0").strip() or "0")


def supports_gpu(ffmpeg_path: str | None = None) -> bool:
    """Return whether this ffmpeg build supports h264_videotoolbox."""
    ffmpeg_path = ffmpeg_path or _require_binary("ffmpeg")
    completed = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return False
    return "h264_videotoolbox" in (completed.stdout or "")


def merge_all(
    parts: list[Path],
    output_path: Path,
    *,
    ffmpeg_path: str | None = None,
    work_dir: Path,
) -> None:
    """Merge chunk outputs with ffmpeg concat."""
    ffmpeg_path = ffmpeg_path or _require_binary("ffmpeg")
    list_file = work_dir / "merge_list.txt"
    list_file.write_text(
        "".join(f"file '{part.resolve()}'\n" for part in parts),
        encoding="utf-8",
    )
    _run_ffmpeg(
        [
            ffmpeg_path,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(output_path),
        ],
        "Video merge failed",
    )


def _process_chunk(task: tuple[str, str, str, float, float]) -> Path:
    """Compress one chunk with CPU settings and single-threaded ffmpeg."""
    ffmpeg_path, input_path, output_path_text, start, end = task
    output_path = Path(output_path_text)
    duration = max(0.1, end - start)
    _run_ffmpeg(
        [
            ffmpeg_path,
            "-y",
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-i",
            input_path,
            "-c:v",
            "libx264",
            "-preset",
            PRESET,
            "-crf",
            CRF,
            "-c:a",
            "aac",
            "-threads",
            "1",
            str(output_path),
        ],
        "Parallel compression failed",
    )
    return output_path


def _validate_input(input_path: Path) -> None:
    """Validate supported input file types before processing."""
    if input_path.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
        raise ValueError(
            "Unsupported video format. Supported formats: .mp4, .mov, .mkv, .avi, .m4v"
        )


def _require_binary(binary_name: str) -> str:
    """Require an ffmpeg binary in PATH."""
    binary_path = shutil.which(binary_name)
    if binary_path is None:
        raise RuntimeError(f"{binary_name} is not installed or not available in PATH.")
    return binary_path


def _run_ffmpeg(command: list[str], error_prefix: str) -> None:
    """Run ffmpeg and raise a readable error when it fails."""
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"{error_prefix}: {_extract_ffmpeg_error(completed.stderr)}")


def _extract_ffmpeg_error(stderr: str | None) -> str:
    """Extract a readable ffmpeg error line."""
    text = (stderr or "").strip()
    if not text:
        return "Unknown ffmpeg error."
    return text.splitlines()[-1]
