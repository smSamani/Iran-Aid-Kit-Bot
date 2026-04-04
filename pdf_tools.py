"""PDF compression, slicing, and merging utilities for the Telegram bot."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pikepdf
from PIL import Image
from PyPDF2 import PdfReader, PdfWriter


SUPPORTED_PDF_SUFFIXES = {".pdf"}
PDF_COMPRESSION_LEVEL_LOW = "low"
PDF_COMPRESSION_LEVEL_MEDIUM = "medium"
PDF_COMPRESSION_LEVEL_HIGH = "high"
LOW_TARGET_REDUCTION_MIN = 0.20
LOW_TARGET_REDUCTION_MAX = 0.30
LOW_TARGET_REDUCTION = 0.25
LOW_IMAGE_RECOMPRESS_QUALITIES = (84, 80, 76, 72, 70, 68, 66)
MEDIUM_TARGET_REDUCTION_MIN = 0.50
MEDIUM_TARGET_REDUCTION_MAX = 0.60
MEDIUM_TARGET_REDUCTION = 0.55
MEDIUM_IMAGE_RECOMPRESS_PROFILES = (
    {"scale": 0.70, "jpeg_quality": 68},
    {"scale": 0.70, "jpeg_quality": 66},
    {"scale": 0.70, "jpeg_quality": 64},
    {"scale": 0.68, "jpeg_quality": 72},
    {"scale": 0.68, "jpeg_quality": 68},
    {"scale": 0.68, "jpeg_quality": 64},
    {"scale": 0.66, "jpeg_quality": 72},
    {"scale": 0.66, "jpeg_quality": 68},
    {"scale": 0.64, "jpeg_quality": 72},
    {"scale": 0.64, "jpeg_quality": 68},
)
PDF_COMPRESSION_PROFILES = {
    PDF_COMPRESSION_LEVEL_LOW: (
        {
            "name": "low_gs_soft",
            "pdfsettings": "/printer",
            "downsample_images": False,
            "jpeg_quality": 92,
        },
        {
            "name": "low_gs_fallback",
            "pdfsettings": "/printer",
            "downsample_images": True,
            "color_resolution": 220,
            "gray_resolution": 220,
            "mono_resolution": 280,
            "jpeg_quality": 88,
        },
    ),
    PDF_COMPRESSION_LEVEL_MEDIUM: (
        {
            "name": "medium_soft",
            "pdfsettings": "/ebook",
            "downsample_images": True,
            "color_resolution": 140,
            "gray_resolution": 140,
            "mono_resolution": 240,
            "jpeg_quality": 78,
        },
        {
            "name": "medium_balanced",
            "pdfsettings": "/ebook",
            "downsample_images": True,
            "color_resolution": 120,
            "gray_resolution": 120,
            "mono_resolution": 220,
            "jpeg_quality": 68,
        },
    ),
    PDF_COMPRESSION_LEVEL_HIGH: (
        {
            "name": "high_balanced",
            "pdfsettings": "/screen",
            "downsample_images": True,
            "color_resolution": 105,
            "gray_resolution": 105,
            "mono_resolution": 180,
            "jpeg_quality": 62,
        },
        {
            "name": "high_aggressive",
            "pdfsettings": "/screen",
            "downsample_images": True,
            "color_resolution": 96,
            "gray_resolution": 96,
            "mono_resolution": 160,
            "jpeg_quality": 55,
        },
    ),
}


@dataclass(slots=True)
class PDFToolResult:
    """Metadata for a PDF processing result."""

    output_path: Path
    original_size_bytes: int
    output_size_bytes: int

    @property
    def reduction_percent(self) -> float:
        """Return non-negative size reduction percentage."""
        if self.original_size_bytes <= 0:
            return 0.0
        saved = self.original_size_bytes - self.output_size_bytes
        return max(0.0, (saved / self.original_size_bytes) * 100)


def compress_pdf(
    input_path: Path,
    work_dir: Path,
    *,
    level: str = PDF_COMPRESSION_LEVEL_MEDIUM,
) -> PDFToolResult:
    """Compress a PDF using the requested compression level."""
    _validate_pdf(input_path)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir / f"{input_path.stem}_compressed.pdf"
    candidates: list[Path] = [input_path]
    selected_profiles = PDF_COMPRESSION_PROFILES.get(level)
    if selected_profiles is None:
        raise ValueError("Unsupported PDF compression level.")

    if level == PDF_COMPRESSION_LEVEL_LOW:
        pikepdf_candidate = work_dir / f"{input_path.stem}_pikepdf_low.pdf"
        try:
            _optimize_pdf_with_pikepdf(input_path, pikepdf_candidate)
        except Exception:
            pass
        else:
            if _is_valid_pdf(pikepdf_candidate):
                candidates.append(pikepdf_candidate)
        for jpeg_quality in LOW_IMAGE_RECOMPRESS_QUALITIES:
            candidate_path = work_dir / f"{input_path.stem}_low_q{jpeg_quality}.pdf"
            try:
                _recompress_pdf_images_with_pikepdf(
                    input_path,
                    candidate_path,
                    jpeg_quality=jpeg_quality,
                )
            except Exception:
                continue
            if _is_valid_pdf(candidate_path):
                candidates.append(candidate_path)
    elif level == PDF_COMPRESSION_LEVEL_MEDIUM:
        pikepdf_candidate = work_dir / f"{input_path.stem}_pikepdf_medium.pdf"
        try:
            _optimize_pdf_with_pikepdf(input_path, pikepdf_candidate)
        except Exception:
            pass
        else:
            if _is_valid_pdf(pikepdf_candidate):
                candidates.append(pikepdf_candidate)
        for profile in MEDIUM_IMAGE_RECOMPRESS_PROFILES:
            candidate_path = work_dir / (
                f"{input_path.stem}_medium_"
                f"s{int(float(profile['scale']) * 100)}_"
                f"q{int(profile['jpeg_quality'])}.pdf"
            )
            try:
                _recompress_pdf_images_with_pikepdf(
                    input_path,
                    candidate_path,
                    jpeg_quality=int(profile["jpeg_quality"]),
                    scale=float(profile["scale"]),
                )
            except Exception:
                continue
            if _is_valid_pdf(candidate_path):
                candidates.append(candidate_path)

    gs_path = shutil.which("gs")
    if gs_path is not None and level != PDF_COMPRESSION_LEVEL_LOW:
        for profile in selected_profiles:
            candidate_path = work_dir / f"{input_path.stem}_{profile['name']}.pdf"
            try:
                _run_command(
                    _build_ghostscript_command(
                        gs_path,
                        input_path=input_path,
                        output_path=candidate_path,
                        profile=profile,
                    ),
                    "PDF compression failed",
                )
            except RuntimeError:
                continue

            if _is_valid_pdf(candidate_path):
                candidates.append(candidate_path)

    rewritten_path = work_dir / f"{input_path.stem}_rewritten.pdf"
    _rewrite_pdf(input_path, rewritten_path)
    if _is_valid_pdf(rewritten_path) and level == PDF_COMPRESSION_LEVEL_LOW:
        candidates.append(rewritten_path)

    if not candidates:
        raise RuntimeError("No valid compressed PDF output was produced.")

    selected_candidate = _select_output_candidate(
        candidates,
        original_size_bytes=input_path.stat().st_size,
        level=level,
    )
    if selected_candidate != output_path:
        shutil.copyfile(selected_candidate, output_path)

    return PDFToolResult(
        output_path=output_path,
        original_size_bytes=input_path.stat().st_size,
        output_size_bytes=output_path.stat().st_size,
    )


def get_pdf_page_count(input_path: Path) -> int:
    """Return the total number of pages in a PDF."""
    _validate_pdf(input_path)
    with input_path.open("rb") as pdf_file:
        reader = PdfReader(pdf_file)
        return len(reader.pages)


def slice_pdf(
    input_path: Path,
    work_dir: Path,
    *,
    start_page: int,
    end_page: int,
) -> PDFToolResult:
    """Export a page range from the source PDF."""
    _validate_pdf(input_path)
    work_dir.mkdir(parents=True, exist_ok=True)

    with input_path.open("rb") as pdf_file:
        reader = PdfReader(pdf_file)
        page_count = len(reader.pages)
        if start_page < 1 or end_page > page_count or start_page > end_page:
            raise ValueError(f"Invalid page range. This PDF has {page_count} pages.")

        writer = PdfWriter()
        for page_index in range(start_page - 1, end_page):
            writer.add_page(reader.pages[page_index])

        output_path = work_dir / f"sliced_{start_page}_to_{end_page}_{input_path.stem}.pdf"
        with output_path.open("wb") as output_file:
            writer.write(output_file)

    return PDFToolResult(
        output_path=output_path,
        original_size_bytes=input_path.stat().st_size,
        output_size_bytes=output_path.stat().st_size,
    )


def merge_pdfs(input_paths: list[Path], work_dir: Path) -> PDFToolResult:
    """Merge one or more PDFs into a single output file."""
    if not input_paths:
        raise ValueError("At least one PDF file is required for merging.")

    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir / "merged_pdfs.pdf"
    writer = PdfWriter()

    for input_path in input_paths:
        _validate_pdf(input_path)
        with input_path.open("rb") as pdf_file:
            reader = PdfReader(pdf_file)
            for page in reader.pages:
                writer.add_page(page)

    with output_path.open("wb") as output_file:
        writer.write(output_file)

    total_input_size = sum(path.stat().st_size for path in input_paths)
    return PDFToolResult(
        output_path=output_path,
        original_size_bytes=total_input_size,
        output_size_bytes=output_path.stat().st_size,
    )


def _rewrite_pdf(input_path: Path, output_path: Path) -> None:
    """Rewrite the PDF and compress content streams where possible."""
    with input_path.open("rb") as pdf_file:
        reader = PdfReader(pdf_file)
        writer = PdfWriter()
        for page in reader.pages:
            try:
                page.compress_content_streams()
            except Exception:
                pass
            writer.add_page(page)
        with output_path.open("wb") as output_file:
            writer.write(output_file)


def _optimize_pdf_with_pikepdf(input_path: Path, output_path: Path) -> None:
    """Run a gentle structural optimization with pikepdf."""
    with pikepdf.open(input_path) as pdf:
        pdf.remove_unreferenced_resources()
        pdf.save(
            output_path,
            compress_streams=True,
            recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )


def _recompress_pdf_images_with_pikepdf(
    input_path: Path,
    output_path: Path,
    *,
    jpeg_quality: int,
    scale: float = 1.0,
) -> None:
    """Gently recompress embedded images while preserving page geometry."""
    processed_images = 0
    with pikepdf.open(input_path) as pdf:
        seen_objects: set[tuple[int, int]] = set()
        for page in pdf.pages:
            for raw_image in page.images.values():
                object_key = raw_image.objgen
                if object_key in seen_objects:
                    continue
                seen_objects.add(object_key)

                try:
                    pdf_image = pikepdf.PdfImage(raw_image)
                except Exception:
                    continue

                if pdf_image.image_mask or "/SMask" in raw_image or "/Mask" in raw_image:
                    continue

                try:
                    image = pdf_image.as_pil_image()
                except Exception:
                    continue

                normalised_image = _normalise_pdf_image(image)
                if normalised_image is None:
                    continue

                if scale != 1.0:
                    resized_width = max(1, round(normalised_image.width * scale))
                    resized_height = max(1, round(normalised_image.height * scale))
                    normalised_image = normalised_image.resize(
                        (resized_width, resized_height),
                        Image.Resampling.LANCZOS,
                    )

                image_bytes = BytesIO()
                normalised_image.save(
                    image_bytes,
                    format="JPEG",
                    quality=jpeg_quality,
                    optimize=True,
                )
                raw_image.write(
                    image_bytes.getvalue(),
                    filter=pikepdf.Name("/DCTDecode"),
                )
                raw_image.ColorSpace = pikepdf.Name(
                    "/DeviceGray"
                    if normalised_image.mode == "L"
                    else "/DeviceRGB"
                )
                raw_image.BitsPerComponent = 8
                raw_image.Width = normalised_image.width
                raw_image.Height = normalised_image.height
                processed_images += 1

        if processed_images == 0:
            raise RuntimeError("No recompressible images found in PDF.")

        pdf.save(
            output_path,
            compress_streams=True,
            recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )


def _normalise_pdf_image(image: Image.Image) -> Image.Image | None:
    """Convert supported embedded images into a safe format for JPEG recompression."""
    if image.mode in {"1", "LA", "RGBA"}:
        image = image.convert("RGB")
    elif image.mode == "P":
        image = image.convert("RGB")
    elif image.mode == "CMYK":
        image = image.convert("RGB")
    elif image.mode not in {"L", "RGB"}:
        return None
    return image


def _build_ghostscript_command(
    gs_path: str,
    *,
    input_path: Path,
    output_path: Path,
    profile: dict[str, object],
) -> list[str]:
    """Build a Ghostscript compression command from a profile."""
    command = [
        gs_path,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS={profile['pdfsettings']}",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dAutoFilterColorImages=false",
        "-dColorImageFilter=/DCTEncode",
        "-dAutoFilterGrayImages=false",
        "-dGrayImageFilter=/DCTEncode",
        f"-dJPEGQ={profile['jpeg_quality']}",
    ]
    if bool(profile.get("downsample_images", True)):
        command.extend(
            [
                "-dDownsampleColorImages=true",
                "-dColorImageDownsampleType=/Bicubic",
                f"-dColorImageResolution={profile['color_resolution']}",
                "-dDownsampleGrayImages=true",
                "-dGrayImageDownsampleType=/Bicubic",
                f"-dGrayImageResolution={profile['gray_resolution']}",
                "-dDownsampleMonoImages=true",
                "-dMonoImageDownsampleType=/Subsample",
                f"-dMonoImageResolution={profile['mono_resolution']}",
            ]
        )
    else:
        command.extend(
            [
                "-dDownsampleColorImages=false",
                "-dDownsampleGrayImages=false",
                "-dDownsampleMonoImages=false",
            ]
        )
    command.extend([f"-sOutputFile={output_path}", str(input_path)])
    return command


def _validate_pdf(input_path: Path) -> None:
    """Validate the input PDF path."""
    if input_path.suffix.lower() not in SUPPORTED_PDF_SUFFIXES:
        raise ValueError("Unsupported PDF format. Only .pdf is supported.")
    if not input_path.exists():
        raise FileNotFoundError(f"PDF file not found: {input_path}")


def _is_valid_pdf(input_path: Path) -> bool:
    """Return whether the produced PDF can be opened and has at least one page."""
    try:
        with input_path.open("rb") as pdf_file:
            reader = PdfReader(pdf_file)
            return len(reader.pages) >= 1
    except Exception:
        return False


def _select_output_candidate(
    candidates: list[Path],
    *,
    original_size_bytes: int,
    level: str,
) -> Path:
    """Pick the output file that best matches the intended compression level."""
    if level == PDF_COMPRESSION_LEVEL_HIGH:
        return min(candidates, key=lambda path: path.stat().st_size)

    compressed_candidates = [
        path for path in candidates if path.stat().st_size < original_size_bytes
    ]
    if not compressed_candidates:
        return min(candidates, key=lambda path: path.stat().st_size)

    if level == PDF_COMPRESSION_LEVEL_LOW:
        in_band = [
            path
            for path in compressed_candidates
            if LOW_TARGET_REDUCTION_MIN
            <= _reduction_ratio(path.stat().st_size, original_size_bytes)
            <= LOW_TARGET_REDUCTION_MAX
        ]
        if in_band:
            return min(
                in_band,
                key=lambda path: abs(
                    _reduction_ratio(path.stat().st_size, original_size_bytes)
                    - LOW_TARGET_REDUCTION
                ),
            )

        above_band = [
            path
            for path in compressed_candidates
            if _reduction_ratio(path.stat().st_size, original_size_bytes)
            > LOW_TARGET_REDUCTION_MAX
        ]
        if above_band:
            return min(
                above_band,
                key=lambda path: _reduction_ratio(path.stat().st_size, original_size_bytes),
            )

        return max(
            compressed_candidates,
            key=lambda path: _reduction_ratio(path.stat().st_size, original_size_bytes),
        )

    if level == PDF_COMPRESSION_LEVEL_MEDIUM:
        in_band = [
            path
            for path in compressed_candidates
            if MEDIUM_TARGET_REDUCTION_MIN
            <= _reduction_ratio(path.stat().st_size, original_size_bytes)
            <= MEDIUM_TARGET_REDUCTION_MAX
        ]
        if in_band:
            return min(
                in_band,
                key=lambda path: abs(
                    _reduction_ratio(path.stat().st_size, original_size_bytes)
                    - MEDIUM_TARGET_REDUCTION
                ),
            )

        above_band = [
            path
            for path in compressed_candidates
            if _reduction_ratio(path.stat().st_size, original_size_bytes)
            > MEDIUM_TARGET_REDUCTION_MAX
        ]
        if above_band:
            return min(
                above_band,
                key=lambda path: _reduction_ratio(path.stat().st_size, original_size_bytes),
            )

        return max(
            compressed_candidates,
            key=lambda path: _reduction_ratio(path.stat().st_size, original_size_bytes),
        )

    target_reduction = 0.45
    return min(
        compressed_candidates,
        key=lambda path: (
            abs(_reduction_ratio(path.stat().st_size, original_size_bytes) - target_reduction),
            path.stat().st_size,
        ),
    )


def _reduction_ratio(output_size_bytes: int, original_size_bytes: int) -> float:
    """Return the size reduction ratio as a 0..1 value."""
    if original_size_bytes <= 0:
        return 0.0
    return max(0.0, (original_size_bytes - output_size_bytes) / original_size_bytes)


def _run_command(command: list[str], error_prefix: str) -> None:
    """Run a shell command and raise a readable error if it fails."""
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        message = stderr.splitlines()[-1] if stderr else "Unknown command failure."
        raise RuntimeError(f"{error_prefix}: {message}")
