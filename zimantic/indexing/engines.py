"""Optional local engines for build-time derived text.

Every engine here is best-effort and local-only.  The module is imported by the
build path, never by ``serve``.  A missing executable raises ``RuntimeError``;
the shared derived-text stage turns that into an operation-scoped ``partial``
or ``unavailable`` state instead of failing the whole build.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..whisper_models import cached_whisper_model, ensure_whisper_model
from .derived import parse_caption_bytes

_DEFAULT_TIMEOUT = 7200


def _positive_int(options: Mapping[str, Any], key: str, default: int) -> int:
    value = options.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return int(default)
    return int(value)


def _cap_utf8(text: str, max_bytes: int) -> str:
    encoded = str(text).encode("utf-8", "ignore")
    if len(encoded) <= max_bytes:
        return str(text)
    return encoded[: max(1, max_bytes)].decode("utf-8", "ignore")


def _media_suffix(payload: bytes) -> str:
    if payload.startswith(b"RIFF"):
        return ".wav"
    if payload.startswith(b"ID3") or payload[:2] in {b"\xff\xfb", b"\xff\xf3"}:
        return ".mp3"
    if payload.startswith(b"OggS"):
        return ".ogg"
    if payload.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"
    if payload.startswith(b"fLaC"):
        return ".flac"
    if payload[4:8] == b"ftyp":
        return ".mp4"
    if payload.startswith(b"%PDF"):
        return ".pdf"
    return ".media"


def _probe_duration(path: Path, timeout: int) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return float(completed.stdout.strip())
    except (TypeError, ValueError):
        return None


def extract_embedded_captions(payload: bytes, **options: Any) -> str:
    """Extract the first embedded text subtitle track with ffmpeg/ffprobe."""

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("embedded caption extraction needs ffmpeg and ffprobe")
    timeout = _positive_int(options, "task_timeout_seconds", _DEFAULT_TIMEOUT)
    cap = _positive_int(options, "max_caption_bytes", 1 << 20)
    with tempfile.TemporaryDirectory(prefix="zimantic-captions-") as directory:
        media = Path(directory) / f"input{_media_suffix(payload)}"
        media.write_bytes(payload)
        try:
            probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "s",
                    "-show_entries",
                    "stream=index,codec_name",
                    "-of",
                    "json",
                    str(media),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"ffprobe failed: {error}") from error
        if probe.returncode != 0 or '"index"' not in probe.stdout:
            return ""
        try:
            extracted = subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    str(media),
                    "-map",
                    "0:s:0",
                    "-f",
                    "srt",
                    "-",
                ],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"ffmpeg caption extraction failed: {error}") from error
        if extracted.returncode != 0:
            detail = extracted.stderr.decode("utf-8", "ignore").strip()
            raise RuntimeError(f"ffmpeg caption extraction failed: {detail[:300]}")
        return _cap_utf8(parse_caption_bytes(extracted.stdout), cap)


def _tesseract_image(payload: bytes, language: str, timeout: int) -> str:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        raise RuntimeError("OCR needs tesseract")
    try:
        completed = subprocess.run(
            [tesseract, "stdin", "stdout", "-l", language, "--psm", "3"],
            input=payload,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"tesseract failed: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "ignore").strip()
        raise RuntimeError(f"tesseract failed: {detail[:300]}")
    return completed.stdout.decode("utf-8", "ignore")


def extract_ocr(payload: bytes, **options: Any) -> str:
    """Run local Tesseract over an image or every page of a PDF."""

    languages = options.get("ocr_languages") or ("eng",)
    if isinstance(languages, str):
        languages = (languages,)
    language = "+".join(str(item) for item in languages if str(item).strip())
    if not language:
        language = "eng"
    timeout = _positive_int(options, "task_timeout_seconds", _DEFAULT_TIMEOUT)
    max_pages = _positive_int(options, "max_ocr_pages", 20)
    max_pixels = _positive_int(options, "max_ocr_pixels", 50_000_000)
    max_output = _positive_int(options, "max_ocr_output_bytes", 256 << 10)

    if not payload.startswith(b"%PDF"):
        _check_image_pixels(payload, max_pixels)
        return _cap_utf8(_tesseract_image(payload, language, timeout), max_output)

    try:
        import pymupdf
    except Exception as error:
        raise RuntimeError(f"OCR PDF support needs PyMuPDF: {error}") from error
    pages: list[str] = []
    try:
        document = pymupdf.open(stream=payload, filetype="pdf")
    except Exception as error:
        raise RuntimeError(f"cannot open PDF for OCR: {error}") from error
    try:
        for number, page in enumerate(document):
            if number >= max_pages:
                break
            pixmap = page.get_pixmap(dpi=200)
            if pixmap.width * pixmap.height > max_pixels:
                continue
            pages.append(
                _tesseract_image(pixmap.tobytes("png"), language, timeout)
            )
    finally:
        document.close()
    return _cap_utf8("\n\n".join(part for part in pages if part.strip()), max_output)


def _check_image_pixels(payload: bytes, max_pixels: int) -> None:
    try:
        import pymupdf
    except Exception:
        return
    try:
        pixmap = pymupdf.Pixmap(payload)
    except Exception:
        return
    if pixmap.width * pixmap.height > max_pixels:
        raise RuntimeError("image exceeds max_ocr_pixels")


def _whisper_model_path(model: str) -> Path | None:
    """Return a locally cached whisper model, or ``None``."""

    return cached_whisper_model(model)


def extract_asr(payload: bytes, **options: Any) -> str:
    """Transcribe one audio/video file with the local whisper CLI."""

    whisper = shutil.which("whisper")
    if not whisper:
        raise RuntimeError("ASR needs the whisper executable")
    model = str(options.get("asr_model") or "base.en")
    timeout = _positive_int(options, "task_timeout_seconds", _DEFAULT_TIMEOUT)
    max_bytes = _positive_int(options, "max_asr_bytes", 1 << 30)
    max_seconds = _positive_int(options, "max_asr_seconds", 3600)
    max_output = _positive_int(options, "max_asr_output_bytes", 1 << 20)
    if len(payload) > max_bytes:
        raise RuntimeError("ASR input exceeds max_asr_bytes")

    with tempfile.TemporaryDirectory(prefix="zimantic-asr-") as directory:
        root = Path(directory)
        media = root / f"input{_media_suffix(payload)}"
        media.write_bytes(payload)
        duration = _probe_duration(media, timeout)
        if duration is not None and duration > max_seconds:
            raise RuntimeError("ASR input exceeds max_asr_seconds")
        if _whisper_model_path(model) is None:
            try:
                ensure_whisper_model(model)
            except Exception as error:
                raise RuntimeError(
                    f"ASR model {model!r} is unavailable: {error}"
                ) from error
        output = root / "out"
        output.mkdir()
        try:
            completed = subprocess.run(
                [
                    whisper,
                    str(media),
                    "--model",
                    model,
                    "--output_format",
                    "txt",
                    "--output_dir",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"whisper failed: {error}") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"whisper failed: {detail[-300:]}")
        transcript = output / f"{media.stem}.txt"
        try:
            text = transcript.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError(f"whisper produced no transcript: {error}") from error
        return _cap_utf8(text.strip(), max_output)


def available_derived_engines() -> dict[str, Any]:
    """Return the engines whose local executables are installed.

    The names match the provider config contract: ``captions`` is always the
    built-in parser, ``ocr`` is Tesseract, ``asr`` is whisper, and ``ffmpeg``
    is the embedded-caption adapter.
    """

    engines: dict[str, Any] = {}
    if shutil.which("tesseract"):
        engines["ocr"] = extract_ocr
    if shutil.which("whisper"):
        engines["asr"] = extract_asr
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        engines["ffmpeg"] = extract_embedded_captions
    return engines


__all__ = [
    "available_derived_engines",
    "extract_asr",
    "extract_embedded_captions",
    "extract_ocr",
]
