"""Extract searchable text from ordinary files.

The extractors are deliberately layered, in the same spirit as a media
toolchain: prefer a native reader, then a small command-line helper, then the
universal calibre conversion, and finally give up with a logged warning rather
than failing the build.  Every external command is optional and bounded by the
per-file deadline in :class:`ExtractOptions`.

A document is returned as one or more :class:`Section` objects.  A section is
the unit that becomes a search entry, so a PDF yields pages, an EPUB yields
chapters, a PPTX yields slides, and a video yields subtitle chunks.  The
``locator`` field carries the browser fragment for formats that support one
(``page=12``, ``t=91.4``, ``slide=3``).
"""
from __future__ import annotations

import json
import logging
import posixpath
import re
import subprocess
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from email import policy
from email.parser import BytesParser
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

from .encoding import decode_text
from .formats import (
    ARCHIVE_EXTENSIONS,
    AUDIO_EXTENSIONS,
    COMIC_EXTENSIONS,
    EBOOK_EXTENSIONS,
    EPUB_EXTENSIONS,
    HTML_EXTENSIONS,
    IMAGE_EXTENSIONS,
    IMAGE_SUBTITLE_CODECS,
    LEGACY_OFFICE_EXTENSIONS,
    MAN_EXTENSIONS,
    MARKDOWN_EXTENSIONS,
    PDF_EXTENSIONS,
    PS_EXTENSIONS,
    RTF_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TEXTUAL_EXTENSIONS,
    VIDEO_EXTENSIONS,
    ZIP_OFFICE_EXTENSIONS,
    file_extension,
    is_supported_file,
    is_text_sample,
)
from .reader import _markdown_excerpt, iter_text_blocks
from .sniff import looks_like_text, sniff_bytes, sniff_extension
from .tools import has_tool

logger = logging.getLogger(__name__)

DEFAULT_MAX_FILE_TEXT_CHARS = 2_000_000
DEFAULT_FILE_TIMEOUT = 120.0
DEFAULT_ARCHIVE_DEPTH = 0
DEFAULT_MAX_ARCHIVE_MEMBERS = 10_000
DEFAULT_MAX_ARCHIVE_BYTES = 256 << 20
DEFAULT_MAX_OCR_PAGES = 50
DEFAULT_OCR_LANGUAGES = "eng"
DEFAULT_ASR_MODEL = "tiny"

# A search entry should be small enough to embed well.  Long documents are cut
# at paragraph/sentence boundaries instead of mid-word.
MAX_SECTION_CHARS = 4_000
# Extractors already discard empty blocks; keep short table cells and subtitle
# cues rather than losing a whole sheet/page to a prose-oriented minimum.
MIN_SECTION_CHARS = 1
SUBTITLE_CHUNK_CHARS = 2_000
# These formats are parsed manually below; everything else in
# SUBTITLE_EXTENSIONS first gets a chance to be converted to WebVTT by FFmpeg,
# which understands far more subtitle dialects than we want to maintain by hand.
_MANUAL_SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt", ".ass", ".ssa", ".sub", ".lrc"})
_FFMPEG_SUBTITLE_EXTENSIONS = SUBTITLE_EXTENSIONS - _MANUAL_SUBTITLE_EXTENSIONS
# FFmpeg's subtitle demuxers are not chosen reliably from the file extension
# alone (a tiny .mpl2 can be mistaken for LRC), so name the demuxer explicitly.
_FFMPEG_SUBTITLE_FORMATS = {
    ".aqt": "aqtitle",
    ".jss": "jacosub",
    ".mpl2": "mpl2",
    ".mpsub": "mpsub",
    ".pjs": "pjs",
    ".rt": "realtext",
    ".sami": "sami",
    ".scc": "scc",
    ".smi": "sami",
    ".stl": "stl",
}
# Text/markup formats Calibre understands.  Native extraction normally wins;
# this list is only used when the native reader produced no sections.
_CALIBRE_TEXT_FALLBACK_EXTENSIONS = frozenset({
    ".md", ".markdown", ".html", ".htm", ".xhtml",
})
TEXT_SAMPLE_BYTES = 64 << 10
MAX_XML_BYTES = 32 << 20
MAX_METADATA_BYTES = 8 << 20

_IMAGE_MEMBER_EXTENSIONS = IMAGE_EXTENSIONS
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")
_HTML_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_ASS_TAG = re.compile(r"\{[^}]*\}")
_HTML_TAG = re.compile(r"<[^>]+>")
_RTF_CONTROL = re.compile(r"\\[a-zA-Z]+-?\d* ?")
_RTF_HEX = re.compile(r"\\'([0-9a-fA-F]{2})")
_MICRODVD = re.compile(r"^\{(\d+)\}\{(\d+)\}(.*)$")
_LRC = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")
_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_VTT_TIME = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*"
    r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})"
)


@dataclass(slots=True)
class Section:
    """One search entry's worth of text from a file."""

    title: str
    body: str
    locator: str = ""


@dataclass(slots=True)
class ExtractOptions:
    """Bounds and opt-in features for one file extraction."""

    max_text_chars: int = DEFAULT_MAX_FILE_TEXT_CHARS
    timeout: float | None = DEFAULT_FILE_TIMEOUT
    archive_depth: int = DEFAULT_ARCHIVE_DEPTH
    max_archive_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    max_ocr_pages: int = DEFAULT_MAX_OCR_PAGES
    ocr: bool = False
    ocr_languages: str = DEFAULT_OCR_LANGUAGES
    asr: bool = False
    asr_model: str = DEFAULT_ASR_MODEL


@dataclass(slots=True)
class Extraction:
    """Text and metadata extracted from one file."""

    title: str
    sections: list[Section] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


class _Deadline:
    """A monotonic budget shared by a whole file, including child commands."""

    __slots__ = ("end",)

    def __init__(self, timeout: float | None):
        try:
            seconds = float(timeout) if timeout is not None else 0.0
        except (TypeError, ValueError):
            seconds = 0.0
        self.end = time.monotonic() + seconds if seconds > 0 else None

    def expired(self) -> bool:
        return self.end is not None and time.monotonic() >= self.end

    def remaining(self, cap: float | None = None) -> float | None:
        if self.end is None:
            return cap
        left = max(0.0, self.end - time.monotonic())
        return min(left, cap) if cap is not None else left


def _read_limited(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(max(1, int(limit)))
    except OSError:
        return b""


def _run(
    argv: list[str],
    deadline: _Deadline,
    *,
    timeout: float | None = None,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess | None:
    """Run an optional helper, returning None on failure or timeout."""
    if deadline.expired():
        return None
    limit = deadline.remaining(timeout)
    if limit is not None and limit <= 0:
        return None
    try:
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=limit,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _command_text(
    argv: list[str],
    deadline: _Deadline,
    *,
    timeout: float | None = None,
    stdin: bytes | None = None,
) -> str:
    result = _run(argv, deadline, timeout=timeout, stdin=stdin)
    if result is None or result.returncode != 0:
        return ""
    raw = result.stdout or b""
    return _collapse(decode_text(raw))


def _collapse(text: str) -> str:
    return " ".join(str(text).split())


_NATURAL_PART = re.compile(r"(\d+)")


def _natural_key(value: object) -> tuple:
    """Sort names with embedded numbers numerically (page2 before page10)."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _NATURAL_PART.split(str(value))
    )


def _truncate(text: str, limit: int) -> str:
    limit = max(1, int(limit))
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    return cut[:boundary].rstrip() if boundary > 0 else cut


def _split_oversized(text: str, limit: int) -> list[str]:
    """Split text over ``limit`` characters without cutting normal words."""
    chunks: list[str] = []
    current = ""
    for word in text.split():
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= limit:
            current = f"{current} {word}"
        else:
            chunks.append(current)
            current = word
        # A single word longer than the limit still has to be split.
        while len(current) > limit:
            chunks.append(current[:limit])
            current = current[limit:]
    if current:
        chunks.append(current)
    return chunks


def _chunk_text(text: str, limit: int = MAX_SECTION_CHARS) -> list[str]:
    """Split prose at paragraph and sentence boundaries, never mid-word."""
    text = text.strip()
    if not text:
        return []
    limit = max(1, int(limit))
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n|\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current.strip())
            current = ""

    for paragraph in paragraphs:
        if len(paragraph) <= limit:
            if current and len(current) + 2 + len(paragraph) > limit:
                flush()
            current = f"{current}\n\n{paragraph}" if current else paragraph
            continue
        for sentence in _SENTENCE_END.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) > limit:
                if current:
                    flush()
                chunks.extend(_split_oversized(sentence, limit))
                continue
            if current and len(current) + 1 + len(sentence) > limit:
                flush()
            current = f"{current} {sentence}" if current else sentence
    flush()
    return [chunk for chunk in chunks if chunk]


def _section_bodies(
    sections: Iterable[Section],
    max_chars: int,
    deadline: _Deadline,
) -> list[Section]:
    """Normalize and cap sections, dropping empty and expired ones."""
    result: list[Section] = []
    remaining = max(0, int(max_chars))
    for section in sections:
        if remaining <= 0 or deadline.expired():
            break
        # Normalize runs of spaces but keep line breaks: archive listings and
        # table rows are easier to read (and preview) as lines.
        body = "\n".join(
            " ".join(line.split())
            for line in section.body.splitlines()
            if line.strip()
        )
        if len(body) < MIN_SECTION_CHARS:
            continue
        body = _truncate(body, remaining)
        result.append(Section(_collapse(section.title), body, section.locator))
        remaining -= len(body) + 1
    return result


def _finish(
    path: Path,
    sections: Iterable[Section],
    options: ExtractOptions,
    deadline: _Deadline,
    metadata: dict[str, str] | None = None,
) -> Extraction:
    metadata = dict(metadata or {})
    title = metadata.get("title") or path.stem
    bounded = _section_bodies(sections, options.max_text_chars, deadline)
    return Extraction(title=title, sections=bounded, metadata=metadata)


def _html_title(raw: bytes) -> str:
    match = _HTML_TITLE.search(raw)
    if not match:
        return ""
    return _collapse(decode_text(match.group(1)))


def _html_blocks(raw: bytes, max_chars: int, deadline: _Deadline) -> str:
    # File paragraphs are often short (a poem line, a comic caption, a slide
    # title). ZIM's article-oriented 50-character minimum would discard them.
    return "\n\n".join(
        iter_text_blocks(
            raw,
            max_chars=max_chars,
            deadline=deadline.end,
            min_block_chars=1,
        )
    )


_XML_EXTENSIONS = frozenset({
    ".xml", ".xsl", ".xsd", ".svg", ".opml", ".rss", ".atom", ".plist", ".fb2",
})


def _xml_text(raw: bytes) -> str:
    """Read the text nodes of an XML document, falling back to tag stripping."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return _collapse(_HTML_TAG.sub(" ", decode_text(raw)))
    selected = {
        "p", "h", "title", "description", "item", "entry",
        # SVG (and some other XML vocabularies) carry the visible text in
        # elements called "text"; without it an SVG with a <title> would
        # index only the caption and lose the diagram labels.
        "text",
    }

    def has_selected_descendant(element: ET.Element) -> bool:
        return any(
            _local_name(child.tag) in selected
            for child in element.iter()
            if child is not element
        )

    def walk(element: ET.Element):
        local = _local_name(element.tag)
        selected_here = local in selected
        selected_below = has_selected_descendant(element)
        if selected_here and not selected_below:
            # A leaf selected element is safe to emit whole; its descendants
            # are not themselves selected, so nothing is duplicated.
            text = _collapse("".join(element.itertext()))
            if text:
                yield text
            return
        if selected_here or selected_below:
            # Emit direct text in document order around the selected children.
            # Emitting itertext() for the container as well would duplicate
            # every child (for example an Atom <entry> and its <title>).
            leading = _collapse(element.text or "")
            if leading:
                yield leading
            for child in element:
                yield from walk(child)
                tail = _collapse(child.tail or "")
                if tail:
                    yield tail
            return
        for child in element:
            yield from walk(child)

    blocks = [block for block in walk(root) if block]
    if blocks:
        return "\n\n".join(blocks)
    return _collapse(" ".join(root.itertext()))


def _man_page_text(raw: bytes) -> str:
    """Strip troff requests and inline escapes from a man page."""
    lines: list[str] = []
    for line in decode_text(raw).splitlines():
        stripped = line.lstrip()
        if stripped.startswith((".", "'")):
            continue
        line = re.sub(r"\\f(?:\[[^\]]*\]|.)", "", line)
        line = re.sub(r"\\\(..", "", line)
        line = (
            line.replace("\\&", "")
            .replace("\\-", "-")
            .replace("\\ ", " ")
            .replace("\\e", "\\")
        )
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def _mime_text(
    raw: bytes,
    options: ExtractOptions,
    deadline: _Deadline,
) -> tuple[str, dict[str, str]]:
    """Extract searchable text and headers from an EML/MHTML message."""
    metadata: dict[str, str] = {}
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        return _collapse(decode_text(raw)), metadata

    subject = str(message.get("subject") or "").strip()
    sender = str(message.get("from") or "").strip()
    if subject:
        metadata["title"] = _collapse(subject)
    if sender:
        metadata["creator"] = _collapse(sender)

    blocks: list[str] = []
    html_blocks: list[str] = []
    plain_blocks: list[str] = []
    attachments: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if filename:
            attachments.append(str(filename))
        content_type = part.get_content_type().lower()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            continue
        if not isinstance(payload, bytes):
            payload = str(payload).encode("utf-8", "replace")
        if content_type == "text/html":
            body = _html_blocks(payload, options.max_text_chars, deadline)
            target = html_blocks
        else:
            head = payload[:4096].lstrip().lower()
            if head.startswith((b"<!doctype html", b"<html")) or b"<html" in head:
                body = _html_blocks(payload, options.max_text_chars, deadline)
                target = html_blocks
            else:
                body = decode_text(payload)
                target = plain_blocks
        if body.strip():
            target.append(body)
    # A multipart/alternative message carries the same text twice; prefer the
    # HTML rendition so the body is not indexed twice.
    blocks.extend(html_blocks or plain_blocks)
    if attachments:
        blocks.append("Attachments:\n" + "\n".join(attachments))
    return "\n\n".join(blocks), metadata


def _bdecode(data: bytes, index: int = 0, depth: int = 0):
    """Decode one bencoded value (used for torrent metadata)."""
    if depth > 32 or index >= len(data):
        raise ValueError("invalid bencode")
    marker = data[index:index + 1]
    if marker == b"i":
        end = data.index(b"e", index + 1)
        return int(data[index + 1:end]), end + 1
    if marker == b"l":
        values = []
        index += 1
        while data[index:index + 1] != b"e":
            value, index = _bdecode(data, index, depth + 1)
            values.append(value)
        return values, index + 1
    if marker == b"d":
        values = {}
        index += 1
        while data[index:index + 1] != b"e":
            key, index = _bdecode(data, index, depth + 1)
            value, index = _bdecode(data, index, depth + 1)
            if isinstance(key, bytes):
                values[key] = value
        return values, index + 1
    if marker.isdigit():
        colon = data.index(b":", index)
        length = int(data[index:colon])
        start = colon + 1
        end = start + length
        if end > len(data):
            raise ValueError("truncated bencode string")
        return data[start:end], end
    raise ValueError("invalid bencode marker")


def _torrent_document(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
) -> Extraction:
    """Index the name, comment and file list of a .torrent file."""
    raw = _read_limited(path, MAX_METADATA_BYTES)
    metadata: dict[str, str] = {}
    blocks: list[str] = []
    try:
        payload, _ = _bdecode(raw)
    except (ValueError, IndexError):
        payload = None
    if isinstance(payload, dict):
        info = payload.get(b"info")
        name = info.get(b"name") if isinstance(info, dict) else None
        if isinstance(name, bytes):
            metadata["title"] = _collapse(name.decode("utf-8", "replace"))
        for key, label in (
            (b"comment", "Comment"),
            (b"created by", "Created by"),
            (b"announce", "Tracker"),
        ):
            value = payload.get(key)
            if isinstance(value, bytes) and value.strip():
                blocks.append(
                    f"{label}: {_collapse(value.decode('utf-8', 'replace'))}"
                )
        files: list[str] = []
        if isinstance(info, dict):
            entries = info.get(b"files")
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    parts = entry.get(b"path.utf-8") or entry.get(b"path")
                    if isinstance(parts, list) and all(
                        isinstance(part, bytes) for part in parts
                    ):
                        files.append(
                            "/".join(part.decode("utf-8", "replace") for part in parts)
                        )
            elif isinstance(name, bytes):
                files.append(name.decode("utf-8", "replace"))
        if files:
            blocks.append("Files:\n" + "\n".join(files))
    if not blocks:
        return _finish(path, [], options, deadline, metadata)
    body = "\n\n".join(blocks)
    return _finish(
        path,
        [Section("", chunk) for chunk in _chunk_text(body)],
        options,
        deadline,
        metadata,
    )


def _text_document(
    path: Path,
    extension: str,
    options: ExtractOptions,
    deadline: _Deadline,
) -> Extraction:
    raw = _read_limited(path, options.max_text_chars * 4 + 1024)
    metadata: dict[str, str] = {}
    if extension in MARKDOWN_EXTENSIONS:
        text, front_title = _markdown_excerpt(
            raw, max_chars=options.max_text_chars, deadline=deadline.end
        )
        if front_title:
            metadata["title"] = front_title
    elif extension in {".eml", ".mhtml", ".mht"}:
        text, metadata = _mime_text(raw, options, deadline)
    elif extension in MAN_EXTENSIONS:
        text = _man_page_text(raw)
    elif extension in HTML_EXTENSIONS:
        text = _html_blocks(raw, options.max_text_chars, deadline)
        title = _html_title(raw)
        if title:
            metadata["title"] = title
    elif extension in _XML_EXTENSIONS:
        text = _xml_text(raw)
    else:
        text = decode_text(raw)
    return _finish(
        path,
        [Section("", chunk) for chunk in _chunk_text(text)],
        options,
        deadline,
        metadata,
    )


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _pymupdf():
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - declared dependency
        return None
    return pymupdf


_OCR_PASSTHROUGH = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"II*\x00",
    b"MM\x00*",
    b"BM",
    b"GIF87a",
    b"GIF89a",
)


def _png_for_ocr(raw: bytes, deadline: _Deadline) -> bytes:
    """Return image bytes in a format tesseract reliably reads.

    Tesseract/Leptonica handles PNG/JPEG/TIFF/BMP/GIF/PNM directly, but many
    formats Zimantic advertises (WebP, AVIF, HEIC, JPEG 2000, JPEG XL) are not
    reliably readable through the ``stdin`` interface.  Convert those to PNG
    with FFmpeg first and ImageMagick second, falling back to the original
    bytes when neither tool is installed.
    """
    if not raw:
        return raw
    if raw.startswith(_OCR_PASSTHROUGH):
        return raw
    if raw[:2] in {b"P1", b"P2", b"P3", b"P4", b"P5", b"P6"}:
        return raw
    if deadline.expired():
        return raw
    commands: list[list[str]] = []
    if has_tool("ffmpeg"):
        commands.append([
            "ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0",
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        ])
    if has_tool("magick"):
        commands.append(["magick", "-", "png:-"])
    if has_tool("convert"):
        commands.append(["convert", "-", "png:-"])
    for command in commands:
        result = _run(command, deadline, stdin=raw)
        if (
            result is not None
            and result.returncode == 0
            and (result.stdout or b"").startswith(b"\x89PNG\r\n\x1a\n")
        ):
            return result.stdout
    return raw


def _ocr_image_bytes(raw: bytes, options: ExtractOptions, deadline: _Deadline) -> str:
    """OCR one encoded image through tesseract's stdin/stdout interface."""
    if not options.ocr or not has_tool("tesseract") or deadline.expired():
        return ""
    languages = (options.ocr_languages or DEFAULT_OCR_LANGUAGES).strip() or DEFAULT_OCR_LANGUAGES
    return _command_text(
        ["tesseract", "stdin", "stdout", "-l", languages, "--psm", "3"],
        deadline,
        stdin=_png_for_ocr(raw, deadline),
    )


def _pdf_native(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
) -> list[Section]:
    pymupdf = _pymupdf()
    if pymupdf is None:
        return []
    sections: list[Section] = []
    ocr_pages = 0
    total = 0
    try:
        document = pymupdf.open(path)
    except Exception:
        return []
    try:
        if document.needs_pass:
            return []
        for number, page in enumerate(document, 1):
            if deadline.expired() or total >= options.max_text_chars:
                break
            try:
                text = _collapse(page.get_text("text"))
            except Exception:
                text = ""
            if not text and options.ocr and ocr_pages < options.max_ocr_pages:
                try:
                    pixmap = page.get_pixmap(dpi=150)
                    text = _ocr_image_bytes(pixmap.tobytes("png"), options, deadline)
                except Exception:
                    text = ""
                ocr_pages += 1
            if not text:
                continue
            sections.append(Section(f"Page {number}", text, f"page={number}"))
            total += len(text)
    finally:
        try:
            document.close()
        except Exception:
            pass
    return sections


def _pdf_via_pdftotext(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
) -> list[Section]:
    if not has_tool("pdftotext"):
        return []
    result = _run(["pdftotext", "-layout", "-q", str(path), "-"], deadline)
    if result is None or result.returncode != 0:
        return []
    pages = decode_text(result.stdout or b"").split("\f")
    sections: list[Section] = []
    total = 0
    for number, page in enumerate(pages, 1):
        text = _collapse(page)
        if not text:
            continue
        sections.append(Section(f"Page {number}", text, f"page={number}"))
        total += len(text)
        if total >= options.max_text_chars or deadline.expired():
            break
    return sections


def _pdf_document(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
) -> Extraction:
    metadata: dict[str, str] = {}
    pymupdf = _pymupdf()
    if pymupdf is not None:
        try:
            with pymupdf.open(path) as document:
                info = document.metadata or {}
                if info.get("title"):
                    metadata["title"] = str(info["title"]).strip()
                if info.get("author"):
                    metadata["creator"] = str(info["author"]).strip()
        except Exception:
            pass
    sections = _pdf_native(path, options, deadline)
    if not sections:
        sections = _pdf_via_pdftotext(path, options, deadline)
    if not sections:
        calibre = _calibre_document(path, options, deadline)
        if calibre is not None:
            calibre.metadata.update(metadata)
            return calibre
    return _finish(path, sections, options, deadline, metadata)


# ---------------------------------------------------------------------------
# EPUB
# ---------------------------------------------------------------------------


def _local_name(value: ET.Element | str) -> str:
    """Local XML name, accepting either an element or a tag string."""
    tag = value.tag if hasattr(value, "tag") else value
    return str(tag).rsplit("}", 1)[-1]


def _resolve(base: str, href: str) -> str:
    path = posixpath.normpath(
        posixpath.join(posixpath.dirname(base), href.split("#", 1)[0])
    )
    return path.lstrip("/")


def _zip_xml(archive: zipfile.ZipFile, name: str, max_bytes: int = MAX_XML_BYTES) -> ET.Element | None:
    try:
        info = archive.getinfo(name)
        if info.file_size > max_bytes:
            return None
        return ET.fromstring(archive.read(name))
    except (KeyError, ET.ParseError, OSError, RuntimeError, NotImplementedError):
        return None


def _zip_member(archive: zipfile.ZipFile, name: str, max_bytes: int) -> bytes:
    try:
        info = archive.getinfo(name)
        if info.file_size > max_bytes:
            return b""
        return archive.read(name)
    except (KeyError, OSError, RuntimeError, NotImplementedError):
        return b""


def _epub_nav_titles(
    archive: zipfile.ZipFile,
    base: str,
    href: str,
    deadline: _Deadline,
) -> dict[str, str]:
    name = _resolve(base, href)
    root = _zip_xml(archive, name)
    if root is None:
        return {}
    titles: dict[str, str] = {}
    for element in root.iter():
        if _local_name(element) != "a":
            continue
        target = element.get("href") or ""
        label = _collapse("".join(element.itertext()))
        if target and label:
            titles[_resolve(name, target)] = label
    for nav_point in root.iter():
        if _local_name(nav_point) != "navPoint":
            continue
        label = ""
        target = ""
        for child in nav_point.iter():
            local = _local_name(child)
            if local == "text" and not label:
                label = _collapse("".join(child.itertext()))
            elif local == "content" and not target:
                target = child.get("src") or ""
        if label and target:
            titles.setdefault(_resolve(name, target), label)
    return titles


class _EpubStructureError(Exception):
    """The EPUB container or package document is missing or unusable."""


def _epub_document(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
) -> Extraction:
    metadata: dict[str, str] = {}
    sections: list[Section] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            container = _zip_xml(archive, "META-INF/container.xml")
            if container is None:
                raise _EpubStructureError("container.xml is missing or malformed")
            rootfile = next(
                (
                    element.get("full-path")
                    for element in container.iter()
                    if _local_name(element) == "rootfile" and element.get("full-path")
                ),
                "",
            )
            if not rootfile:
                raise _EpubStructureError("container.xml has no rootfile")
            rootfile = rootfile.lstrip("/")
            opf = _zip_xml(archive, rootfile)
            if opf is None:
                raise _EpubStructureError("package document is missing or malformed")

            manifest: dict[str, tuple[str, str]] = {}
            spine: list[str] = []
            nav_href = ""
            ncx_href = ""
            for element in opf.iter():
                local = _local_name(element)
                if local == "title" and "title" not in metadata:
                    metadata["title"] = _collapse("".join(element.itertext()))
                elif local == "creator" and "creator" not in metadata:
                    metadata["creator"] = _collapse("".join(element.itertext()))
                elif local == "language" and "language" not in metadata:
                    metadata["language"] = _collapse("".join(element.itertext()))
                elif local == "description" and "description" not in metadata:
                    metadata["description"] = _collapse("".join(element.itertext()))
                elif local == "item":
                    identifier = element.get("id") or ""
                    href = element.get("href") or ""
                    media_type = (element.get("media-type") or "").lower()
                    properties = (element.get("properties") or "").lower()
                    if identifier and href:
                        manifest[identifier] = (href, media_type)
                    if "nav" in properties.split():
                        nav_href = href
                    if media_type == "application/x-dtbncx+xml":
                        ncx_href = href
                elif local == "spine":
                    ncx_href = ncx_href or (element.get("toc") or "")
                elif local == "itemref":
                    identifier = element.get("idref") or ""
                    if identifier:
                        spine.append(identifier)

            toc: dict[str, str] = {}
            if nav_href:
                toc.update(_epub_nav_titles(archive, rootfile, nav_href, deadline))
            if ncx_href and ncx_href in manifest:
                toc.update(
                    _epub_nav_titles(archive, rootfile, manifest[ncx_href][0], deadline)
                )

            identifiers = spine or [
                identifier
                for identifier, (_href, media_type) in manifest.items()
                if "html" in media_type or "xml" in media_type
            ]
            seen: set[str] = set()
            for index, identifier in enumerate(identifiers, 1):
                if deadline.expired():
                    break
                item = manifest.get(identifier)
                if item is None:
                    continue
                href, media_type = item
                if media_type and "html" not in media_type and "xml" not in media_type:
                    continue
                document = _resolve(rootfile, href)
                if document in seen or document not in names:
                    continue
                seen.add(document)
                raw = _zip_member(archive, document, MAX_XML_BYTES)
                if not raw:
                    continue
                body = _html_blocks(raw, options.max_text_chars, deadline)
                if not body:
                    continue
                heading = toc.get(document) or f"Chapter {index}"
                # Keep the chapter label in the searchable body as well as the
                # entry title: readers frequently search for "Chapter 7".
                sections.append(
                    Section(heading, f"{heading}\n\n{body}", f"chapter={index}")
                )
    except (OSError, zipfile.BadZipFile, RuntimeError, _EpubStructureError):
        pass
    if not sections:
        calibre = _calibre_document(path, options, deadline)
        if calibre is not None:
            calibre.metadata.update(metadata)
            return calibre
    return _finish(path, sections, options, deadline, metadata)


# ---------------------------------------------------------------------------
# Office / OpenDocument / iWork
# ---------------------------------------------------------------------------


def _element_text(element: ET.Element) -> str:
    return _collapse("".join(element.itertext()))


def _element_attribute(element: ET.Element, name: str) -> str:
    """Return an attribute by local name, ignoring its XML namespace."""
    for key, value in element.attrib.items():
        if _local_name(key) == name:
            return str(value).strip()
    return ""


def _heading_style(element: ET.Element) -> str:
    for child in element.iter():
        if _local_name(child.tag) not in {"pStyle", "style-name", "style"}:
            continue
        for key, value in child.attrib.items():
            if _local_name(key) in {"val", "name"} and value:
                return str(value).lower()
    return ""


def _heading_like(style: str) -> bool:
    return bool(
        style
        and (
            "heading" in style
            or re.fullmatch(r"h[\s_-]?[1-6]", style)
            or style in {"title", "subtitle"}
        )
    )


def _docx_sections(raw: bytes) -> list[Section]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    sections: list[Section] = []
    paragraphs: list[str] = []
    heading = ""

    def flush() -> None:
        nonlocal paragraphs
        text = "\n\n".join(part for part in paragraphs if part)
        if text:
            for chunk in _chunk_text(text):
                sections.append(Section(heading, chunk))
        paragraphs = []

    for element in root.iter():
        if _local_name(element.tag) != "p":
            continue
        text = _element_text(element)
        if not text:
            continue
        if _heading_like(_heading_style(element)):
            flush()
            heading = text
        else:
            paragraphs.append(text)
    flush()
    return sections


def _odf_sections(raw: bytes, kind: str) -> list[Section]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    sections: list[Section] = []
    blocks: list[str] = []
    heading = ""

    def flush() -> None:
        nonlocal blocks
        text = "\n\n".join(part for part in blocks if part)
        if text:
            for chunk in _chunk_text(text):
                sections.append(Section(heading, chunk))
        blocks = []

    if kind == "presentation":
        for index, page in enumerate(
            (element for element in root.iter() if _local_name(element.tag) == "page"),
            1,
        ):
            # ODF stores the human-readable name in the namespaced draw:name
            # attribute, not an unqualified "name" attribute.
            name = _element_attribute(page, "name")
            parts = [
                _element_text(item)
                for item in page.iter()
                if _local_name(item.tag) in {"p", "h"}
            ]
            text = "\n\n".join(part for part in parts if part)
            if not text:
                # Some generators put slide text outside text:p/text:h.
                text = _collapse(" ".join(page.itertext()))
            if text:
                sections.append(Section(name or "Slide", text, f"slide={index}"))
        return sections

    for element in root.iter():
        local = _local_name(element.tag)
        if local == "h":
            flush()
            heading = _element_text(element)
        elif local == "p":
            text = _element_text(element)
            if text:
                blocks.append(text)
    flush()
    return sections


def _pptx_sections(archive: zipfile.ZipFile) -> list[Section]:
    names = [
        name
        for name in archive.namelist()
        if name.startswith("ppt/slides/slide") and name.endswith(".xml")
    ]
    def number(name: str) -> int:
        match = re.search(r"(\d+)\.xml$", name)
        return int(match.group(1)) if match else 0
    sections: list[Section] = []
    for index, name in enumerate(sorted(names, key=number), 1):
        root = _zip_xml(archive, name)
        if root is None:
            continue
        parts = [
            _collapse("".join(element.itertext()))
            for element in root.iter()
            if _local_name(element.tag) == "t"
        ]
        text = "\n\n".join(part for part in parts if part)
        if not text:
            continue
        title = parts[0] if parts else f"Slide {index}"
        sections.append(Section(title, text, f"slide={index}"))
    return sections


def _xlsx_sections(archive: zipfile.ZipFile) -> list[Section]:
    shared: list[str] = []
    shared_root = _zip_xml(archive, "xl/sharedStrings.xml")
    if shared_root is not None:
        for element in shared_root.iter():
            if _local_name(element.tag) == "si":
                shared.append(_element_text(element))

    names = [
        name
        for name in archive.namelist()
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
    ]
    def number(name: str) -> int:
        match = re.search(r"(\d+)\.xml$", name)
        return int(match.group(1)) if match else 0
    sections: list[Section] = []
    for index, name in enumerate(sorted(names, key=number), 1):
        root = _zip_xml(archive, name)
        if root is None:
            continue
        lines: list[str] = []
        for row in root.iter():
            if _local_name(row.tag) != "row":
                continue
            values: list[str] = []
            for cell in row:
                if _local_name(cell.tag) != "c":
                    continue
                cell_type = (cell.get("t") or "").lower()
                value_element = next(
                    (child for child in cell if _local_name(child.tag) in {"v", "is"}),
                    None,
                )
                if value_element is None:
                    continue
                if cell_type == "s":
                    raw_value = _element_text(value_element)
                    try:
                        values.append(shared[int(raw_value)])
                    except (ValueError, IndexError):
                        values.append(raw_value)
                else:
                    values.append(_element_text(value_element))
            if values:
                lines.append("\t".join(values))
        body = "\n".join(lines)
        if body:
            sections.append(Section(f"Sheet {index}", body, f"sheet={index}"))
    return sections


def _zip_office_document(
    path: Path,
    extension: str,
    options: ExtractOptions,
    deadline: _Deadline,
) -> Extraction:
    sections: list[Section] = []
    try:
        with zipfile.ZipFile(path) as archive:
            if extension in {".docx", ".docm", ".dotx", ".dotm"}:
                raw = _zip_member(archive, "word/document.xml", MAX_XML_BYTES)
                sections = _docx_sections(raw)
            elif extension in {".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm"}:
                sections = _pptx_sections(archive)
            elif extension in {".xlsx", ".xlsm", ".xltx", ".xltm", ".xlsb"}:
                sections = _xlsx_sections(archive)
            elif extension in {".odt", ".ods", ".odg", ".odf", ".odm", ".ott", ".ots"}:
                raw = _zip_member(archive, "content.xml", MAX_XML_BYTES)
                sections = _odf_sections(raw, "text")
            elif extension in {".odp", ".otp"}:
                raw = _zip_member(archive, "content.xml", MAX_XML_BYTES)
                sections = _odf_sections(raw, "presentation")
            elif extension in {".pages", ".numbers", ".key"}:
                raw = (
                    _zip_member(archive, "index.xml", MAX_XML_BYTES)
                    or _zip_member(archive, "main.xml", MAX_XML_BYTES)
                )
                sections = _odf_sections(raw, "text")
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        sections = []
    if not sections:
        calibre = _calibre_document(path, options, deadline)
        if calibre is not None:
            return calibre
    return _finish(path, sections, options, deadline)


# ---------------------------------------------------------------------------
# Legacy formats and command-line helpers
# ---------------------------------------------------------------------------


def _strip_rtf(raw: bytes) -> str:
    text = raw.decode("cp1252", "ignore")
    text = _RTF_HEX.sub(
        lambda match: bytes([int(match.group(1), 16)]).decode("cp1252", "ignore"),
        text,
    )
    text = _RTF_CONTROL.sub(" ", text)
    return _collapse(text.replace("{", " ").replace("}", " "))


@lru_cache(maxsize=1)
def _calibre_version() -> tuple[int, int, int]:
    """Return the installed Calibre version, or ``(0, 0, 0)`` when unknown."""
    if not has_tool("ebook-convert"):
        return (0, 0, 0)
    result = _run(["ebook-convert", "--version"], _Deadline(10), timeout=10)
    if result is None or result.returncode != 0:
        return (0, 0, 0)
    output = (result.stdout or b"") + b"\n" + (result.stderr or b"")
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", decode_text(output))
    if not match:
        return (0, 0, 0)
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
    )


def _calibre_command(path: Path, output: Path) -> list[str]:
    """Build the extraction-oriented ``ebook-convert`` command."""
    command = [
        "ebook-convert",
        str(path),
        str(output),
        "--output-profile",
        "tablet",
        "--pretty-print",
        "--minimum-line-height=105",
        "--unsmarten-punctuation",
    ]
    # Calibre 7.19 added a much faster/more accurate PDF input path.  Without
    # this hint older and newer builds can spend a long time on the default
    # PDF engine and produce worse reading order.
    if file_extension(path) == ".pdf" and _calibre_version() >= (7, 19, 0):
        command += ["--pdf-engine", "pdftohtml"]
    return command


def _calibre_document(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
) -> Extraction | None:
    """Universal ebook/office fallback: convert to exploded HTML with calibre."""
    if not has_tool("ebook-convert") or deadline.expired():
        return None
    with tempfile.TemporaryDirectory(prefix="zimantic-calibre-") as temp_dir:
        output = Path(temp_dir) / "out"
        result = _run(_calibre_command(path, output), deadline)
        if result is None or result.returncode != 0 or not output.is_dir():
            return None
        sections: list[Section] = []
        files = sorted(
            (
                item
                for item in output.rglob("*")
                if item.is_file()
                and item.suffix.lower() in {".html", ".htm", ".xhtml"}
            ),
            key=_natural_key,
        )
        for index, item in enumerate(files, 1):
            if deadline.expired():
                break
            raw = _read_limited(item, MAX_XML_BYTES)
            if not raw:
                continue
            body = _html_blocks(raw, options.max_text_chars, deadline)
            if body:
                sections.append(Section(item.stem or f"Part {index}", body))
        return _finish(path, sections, options, deadline)


def _clean_unrtf(text: str) -> str:
    """Drop unrtf's banner and separator lines before indexing the body."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("###"):
            continue
        if "font table contains" in stripped:
            continue
        if stripped and set(stripped) <= {"-"}:
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _legacy_document(
    path: Path,
    extension: str,
    options: ExtractOptions,
    deadline: _Deadline,
) -> Extraction:
    sections: list[Section] = []
    if extension in RTF_EXTENSIONS:
        text = _command_text(["unrtf", "--text", str(path)], deadline) if has_tool("unrtf") else ""
        text = _clean_unrtf(text)
        if not text:
            text = _strip_rtf(_read_limited(path, MAX_XML_BYTES))
        if text:
            sections = [Section("", chunk) for chunk in _chunk_text(text)]
    elif extension in PS_EXTENSIONS:
        for command in ("ps2ascii", "pstotext"):
            if not has_tool(command):
                continue
            text = _command_text([command, str(path)], deadline)
            if text:
                sections = [Section("", chunk) for chunk in _chunk_text(text)]
                break
    elif extension == ".doc":
        for command in ("catdoc", "antiword"):
            if not has_tool(command):
                continue
            text = _command_text([command, str(path)], deadline)
            if text:
                sections = [Section("", chunk) for chunk in _chunk_text(text)]
                break
    elif extension == ".xls":
        for command in ("xls2csv", "catdoc"):
            if not has_tool(command):
                continue
            text = _command_text([command, str(path)], deadline)
            if text:
                sections = [Section("", chunk) for chunk in _chunk_text(text)]
                break
    elif extension == ".ppt":
        for command in ("catppt", "catdoc"):
            if not has_tool(command):
                continue
            text = _command_text([command, str(path)], deadline)
            if text:
                sections = [Section("", chunk) for chunk in _chunk_text(text)]
                break
    if not sections:
        calibre = _calibre_document(path, options, deadline)
        if calibre is not None:
            return calibre
    return _finish(path, sections, options, deadline)


def _chm_document(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
) -> Extraction:
    sections: list[Section] = []
    if has_tool("extract_chm") and not deadline.expired():
        with tempfile.TemporaryDirectory(prefix="zimantic-chm-") as temp_dir:
            result = _run(["extract_chm", "-l", temp_dir, str(path)], deadline)
            if result is not None and result.returncode == 0:
                for index, item in enumerate(
                    sorted(Path(temp_dir).rglob("*.htm*"), key=_natural_key), 1
                ):
                    if deadline.expired():
                        break
                    body = _html_blocks(
                        _read_limited(item, MAX_XML_BYTES),
                        options.max_text_chars,
                        deadline,
                    )
                    if body:
                        sections.append(Section(item.stem or f"Part {index}", body))
    if not sections:
        calibre = _calibre_document(path, options, deadline)
        if calibre is not None:
            return calibre
    return _finish(path, sections, options, deadline)


# ---------------------------------------------------------------------------
# Subtitles and media
# ---------------------------------------------------------------------------


def _parse_timestamp(hours: str, minutes: str, seconds: str, fraction: str = "0") -> float:
    try:
        # The fraction is a decimal fraction of a second, not a left-padded
        # integer. "5" means 500 ms and "50" means 500 ms in ASS (centiseconds);
        # padding on the left would turn those into 5 ms and 50 ms.
        milliseconds = float(f"0.{str(fraction).ljust(3, '0')[:3]}")
        return (
            int(hours or 0) * 3600
            + int(minutes) * 60
            + float(seconds)
            + milliseconds
        )
    except (TypeError, ValueError):
        return 0.0


def _strip_subtitle_markup(text: str) -> str:
    text = _ASS_TAG.sub(" ", text)
    text = text.replace("\\N", " ").replace("\\n", " ")
    text = _HTML_TAG.sub(" ", text)
    return _collapse(text)


def _subtitle_cues_via_ffmpeg(
    raw: bytes,
    extension: str,
    deadline: _Deadline | None,
) -> list[tuple[float, str]]:
    """Convert an arbitrary text subtitle dialect to WebVTT with FFmpeg."""
    if deadline is None or not raw or not has_tool("ffmpeg") or deadline.expired():
        return []
    with tempfile.TemporaryDirectory(prefix="zimantic-subtitle-") as temp_dir:
        source = Path(temp_dir) / f"input{extension}"
        try:
            source.write_bytes(raw)
        except OSError:
            return []
        command = ["ffmpeg", "-nostdin", "-v", "error"]
        demuxer = _FFMPEG_SUBTITLE_FORMATS.get(extension)
        if demuxer:
            command += ["-f", demuxer]
        command += ["-i", str(source), "-f", "webvtt", "-"]
        result = _run(command, deadline)
        if result is None or result.returncode != 0:
            return []
        return _subtitle_cues(result.stdout or b"", ".vtt")


def _subtitle_cues(
    raw: bytes,
    extension: str,
    deadline: _Deadline | None = None,
) -> list[tuple[float, str]]:
    """Return ``(start_seconds, text)`` cues for a text subtitle format."""
    if extension == ".sub" and b"\x00" in raw[:4096]:
        return []
    if extension in _FFMPEG_SUBTITLE_EXTENSIONS:
        converted = _subtitle_cues_via_ffmpeg(raw, extension, deadline)
        if converted:
            return converted
    text = decode_text(raw)
    cues: list[tuple[float, str]] = []
    if extension == ".srt":
        blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines:
                continue
            if lines[0].isdigit():
                lines = lines[1:]
            if not lines:
                continue
            match = _SRT_TIME.search(lines[0])
            if not match:
                continue
            start = _parse_timestamp(*match.groups()[:4])
            body = _strip_subtitle_markup(" ".join(lines[1:]))
            if body:
                cues.append((start, body))
    elif extension == ".vtt":
        text = re.sub(r"\A\ufeff?WEBVTT[^\n]*\n?", "", text, flags=re.IGNORECASE)
        blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")):
                continue
            timing_index = next(
                (index for index, line in enumerate(lines) if "-->" in line), None
            )
            if timing_index is None:
                continue
            match = _VTT_TIME.search(lines[timing_index])
            if not match:
                continue
            start = _parse_timestamp(
                match.group(1) or "0", match.group(2), match.group(3), match.group(4)
            )
            body = _strip_subtitle_markup(" ".join(lines[timing_index + 1:]))
            if body:
                cues.append((start, body))
    elif extension in {".ass", ".ssa"}:
        in_events = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("[events]"):
                in_events = True
                continue
            if not in_events:
                continue
            if not stripped.lower().startswith("dialogue:"):
                continue
            fields = stripped.split(",", 9)
            if len(fields) < 10:
                continue
            start_field = fields[1].strip()
            match = re.match(r"(\d+):(\d{2}):(\d{2})[.:](\d{1,3})", start_field)
            start = (
                _parse_timestamp(match.group(1), match.group(2), match.group(3), match.group(4))
                if match
                else 0.0
            )
            body = _strip_subtitle_markup(fields[9])
            if body:
                cues.append((start, body))
    elif extension == ".sub":
        for line in text.splitlines():
            match = _MICRODVD.match(line.strip())
            if not match:
                continue
            start = int(match.group(1)) / 25.0
            body = _strip_subtitle_markup(match.group(3).replace("|", " "))
            if body:
                cues.append((start, body))
    elif extension == ".lrc":
        for line in text.splitlines():
            matches = list(_LRC.finditer(line))
            if not matches:
                continue
            body = _strip_subtitle_markup(_LRC.sub("", line))
            for match in matches:
                start = int(match.group(1)) * 60 + float(match.group(2))
                if body:
                    cues.append((start, body))
    else:
        # SAMI and other tag-based formats: strip tags and timing markers.
        body = _strip_subtitle_markup(
            re.sub(r"(?im)^\s*\d+\s*$", " ", text)
        )
        if body:
            cues.append((0.0, body))
    return cues


def _subtitle_sections(
    cues: list[tuple[float, str]],
    title: str,
    *,
    locator_prefix: str = "t=",
    chunk_chars: int = SUBTITLE_CHUNK_CHARS,
) -> list[Section]:
    sections: list[Section] = []
    current: list[str] = []
    current_length = 0
    first_start: float | None = None
    for start, text in cues:
        if not text:
            continue
        if first_start is None:
            first_start = start
        if current_length + len(text) + 1 > chunk_chars and current:
            sections.append(
                Section(title, " ".join(current), f"{locator_prefix}{first_start:.3f}" if first_start is not None else "")
            )
            current = []
            current_length = 0
            first_start = start
        current.append(text)
        current_length += len(text) + 1
    if current:
        sections.append(
            Section(title, " ".join(current), f"{locator_prefix}{first_start:.3f}" if first_start is not None else "")
        )
    return sections


def _sidecar_subtitles(path: Path) -> list[Path]:
    """Find subtitle sidecars for ``path`` without matching ``lesson2.srt``."""
    directories = [path.parent]
    for name in ("subtitles", "subs"):
        directory = path.parent / name
        if directory.is_dir():
            directories.append(directory)
    entries: list[Path] = []
    seen: set[Path] = set()
    for directory in directories:
        try:
            candidates = list(directory.iterdir())
        except OSError:
            continue
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            entries.append(item)
    base = path.stem.casefold()
    candidates: list[Path] = []
    for item in entries:
        if not item.is_file():
            continue
        extension = file_extension(item)
        if extension not in SUBTITLE_EXTENSIONS:
            continue
        stem = item.name[: -len(extension)].casefold()
        if (
            stem == base
            or stem.startswith(base + ".")
            or stem.startswith(base + "_")
            or stem.startswith(base + " - ")
        ):
            candidates.append(item)
    candidates.sort(
        key=lambda item: (
            0 if re.search(r"(?:^|[._-])en(?:g|gl|glish)?(?:[._-]|$)", item.name, re.IGNORECASE) else 1,
            0 if item.stem.casefold() == base else 1,
            str(item),
        )
    )
    return candidates


def _embedded_subtitle_cues(
    path: Path,
    deadline: _Deadline,
) -> list[tuple[float, str]]:
    if not has_tool("ffprobe") or not has_tool("ffmpeg"):
        return []
    result = _run(
        [
            "ffprobe", "-v", "error", "-select_streams", "s",
            "-show_entries", "stream=index,codec_name:stream_tags=language,title",
            "-of", "json", str(path),
        ],
        deadline,
    )
    if result is None or result.returncode != 0:
        return []
    try:
        streams = json.loads(result.stdout or b"{}").get("streams", [])
    except (TypeError, ValueError):
        return []
    if not isinstance(streams, list):
        return []
    streams = [stream for stream in streams if isinstance(stream, dict)]
    def rank(stream: dict) -> tuple[int, int]:
        codec = str(stream.get("codec_name") or "").lower()
        tags = stream.get("tags") or {}
        language = str(tags.get("language") or "").lower() if isinstance(tags, dict) else ""
        return (1 if codec in IMAGE_SUBTITLE_CODECS else 0, 0 if language.startswith("en") else 1)
    for stream in sorted(streams, key=rank):
        if not isinstance(stream, dict):
            continue
        codec = str(stream.get("codec_name") or "").lower()
        if codec in IMAGE_SUBTITLE_CODECS:
            continue
        index = stream.get("index")
        if index is None:
            continue
        result = _run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                "-map", f"0:{index}", "-f", "webvtt", "-",
            ],
            deadline,
        )
        if result is None or result.returncode != 0:
            continue
        cues = _subtitle_cues(result.stdout or b"", ".vtt")
        if cues:
            return cues
    return []


def _asr_text(path: Path, options: ExtractOptions, deadline: _Deadline) -> str:
    if not options.asr or not has_tool("whisper") or deadline.expired():
        return ""
    with tempfile.TemporaryDirectory(prefix="zimantic-asr-") as temp_dir:
        result = _run(
            [
                "whisper", str(path), "--model", options.asr_model or DEFAULT_ASR_MODEL,
                "--output_format", "txt", "--output_dir", temp_dir,
                "--fp16", "False",
            ],
            deadline,
        )
        if result is None or result.returncode != 0:
            return ""
        for item in sorted(Path(temp_dir).glob("*.txt")):
            text = decode_text(_read_limited(item, MAX_METADATA_BYTES))
            if text.strip():
                return text
    return ""


def _media_document(
    path: Path,
    extension: str,
    options: ExtractOptions,
    deadline: _Deadline,
) -> Extraction:
    metadata: dict[str, str] = {}
    cues = _embedded_subtitle_cues(path, deadline)
    title = path.stem
    if not cues:
        sidecars = _sidecar_subtitles(path)
        for sidecar in sidecars:
            cues = _subtitle_cues(
                _read_limited(sidecar, options.max_text_chars * 4 + 1024),
                file_extension(sidecar),
                deadline,
            )
            if cues:
                title = f"{path.stem} [{sidecar.stem}]"
                break
    sections = _subtitle_sections(cues, title) if cues else []
    if not sections and extension in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
        text = _asr_text(path, options, deadline)
        if text:
            sections = [
                Section(title, chunk, f"t=0.000&part={index}")
                for index, chunk in enumerate(_chunk_text(text), 1)
            ]
    return _finish(path, sections, options, deadline, metadata)


# ---------------------------------------------------------------------------
# Images, comics and archives
# ---------------------------------------------------------------------------


def _image_document(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
) -> Extraction:
    text = ""
    if options.ocr:
        text = _ocr_image_bytes(
            _read_limited(path, MAX_METADATA_BYTES), options, deadline
        )
    return _finish(
        path,
        [Section("", chunk) for chunk in _chunk_text(text)],
        options,
        deadline,
    )


def _comic_member_sections(
    archive: zipfile.ZipFile,
    options: ExtractOptions,
    deadline: _Deadline,
) -> list[Section]:
    members = [
        info
        for info in archive.infolist()
        if not info.is_dir() and file_extension(info.filename) in _IMAGE_MEMBER_EXTENSIONS
    ]
    members.sort(key=lambda info: _natural_key(info.filename))
    sections: list[Section] = []
    for number, info in enumerate(members[: options.max_ocr_pages], 1):
        if deadline.expired():
            break
        try:
            raw = _zip_member(archive, info.filename, options.max_archive_bytes)
        except (OSError, RuntimeError):
            continue
        text = _ocr_image_bytes(raw, options, deadline) if options.ocr else ""
        if text:
            sections.append(Section(f"Page {number}", text, f"page={number}"))
    return sections


def _comic_document(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
    _seen: set[str] | None = None,
) -> Extraction:
    sections: list[Section] = []
    if file_extension(path) == ".cbz":
        try:
            with zipfile.ZipFile(path) as archive:
                sections = _comic_member_sections(archive, options, deadline)
        except (OSError, zipfile.BadZipFile):
            sections = []
    elif options.ocr:
        # CBR/CB7/CBA use an external helper; CBT is a tar archive and can be
        # handled by the standard library even when no helper is installed.
        with tempfile.TemporaryDirectory(prefix="zimantic-comic-") as temp_dir:
            destination = Path(temp_dir) / "contents"
            extension = file_extension(path)
            if extension in {
                ".cbt", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
                ".tbz", ".tar.xz", ".txz", ".tar.zst", ".tzst", ".taz",
            }:
                files = _extract_tar_members(path, destination, options, deadline)
                if not files:
                    files = _extract_external_archive(
                        path, destination, options, deadline
                    )
            else:
                files = _extract_external_archive(
                    path, destination, options, deadline
                )
            images = sorted(
                (
                    item
                    for item in files
                    if file_extension(item) in _IMAGE_MEMBER_EXTENSIONS
                ),
                key=_natural_key,
            )
            for number, item in enumerate(images[: options.max_ocr_pages], 1):
                if deadline.expired():
                    break
                text = _ocr_image_bytes(
                    _read_limited(item, MAX_METADATA_BYTES), options, deadline
                )
                if text:
                    sections.append(Section(f"Page {number}", text, f"page={number}"))
    if not sections:
        return _archive_document(path, options, deadline, _seen=_seen)
    return _finish(path, sections, options, deadline)


def _zip_members(path: Path, limit: int) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return [
                info.filename
                for info in archive.infolist()
                if not info.is_dir()
            ][:limit]
    except (OSError, zipfile.BadZipFile):
        return []


def _tar_members(path: Path, limit: int) -> list[str]:
    try:
        with tarfile.open(path) as archive:
            names: list[str] = []
            for member in archive:
                if member.isfile():
                    names.append(member.name)
                    if len(names) >= limit:
                        break
            return names
    except (OSError, tarfile.TarError):
        return []


def _external_archive_entries(
    path: Path, limit: int, deadline: _Deadline
) -> list[tuple[str, int | None]]:
    """Return ``(member, uncompressed_size)`` from an external archiver.

    Sizes are available from 7z and lsar; ``unrar lb`` only lists names, so its
    entries carry ``None`` and the post-extraction bound still applies.
    """
    if has_tool("7z"):
        result = _run(["7z", "l", "-ba", "-slt", str(path)], deadline)
        if result is not None and result.returncode == 0:
            entries: list[tuple[str, int | None]] = []
            current_name: str | None = None
            current_size: int | None = None
            current_directory = False

            def finish() -> None:
                nonlocal current_name, current_size, current_directory
                if (
                    current_name
                    and current_name != str(path)
                    and not current_directory
                ):
                    entries.append((current_name, current_size))
                current_name = None
                current_size = None
                current_directory = False

            for line in decode_text(result.stdout or b"").splitlines():
                if line.startswith("Path = "):
                    finish()
                    if len(entries) >= limit:
                        break
                    current_name = line[7:].strip()
                elif line.startswith("Size = ") and current_name is not None:
                    try:
                        current_size = int(line[7:].strip())
                    except ValueError:
                        current_size = None
                elif line.startswith("Attributes = ") and current_name is not None:
                    attributes = line[13:].strip().upper()
                    current_directory = "D" in attributes
            if len(entries) < limit:
                finish()
            if entries:
                return entries[:limit]
    if has_tool("unrar"):
        result = _run(["unrar", "lb", str(path)], deadline)
        if result is not None and result.returncode == 0:
            return [
                (line.strip(), None)
                for line in decode_text(result.stdout or b"").splitlines()
                if line.strip()
            ][:limit]
    if has_tool("lsar"):
        result = _run(["lsar", "-json", str(path)], deadline)
        if result is not None and result.returncode == 0:
            try:
                payload = json.loads(result.stdout or b"{}")
            except (TypeError, ValueError):
                return []
            contents = payload.get("lsarContents") or []
            entries = []
            for item in contents:
                if not isinstance(item, dict) or item.get("XADIsDirectory"):
                    continue
                name = str(item.get("XADFileName") or "")
                if not name:
                    continue
                try:
                    size = int(item.get("XADFileSize"))
                except (TypeError, ValueError):
                    size = None
                entries.append((name, size))
                if len(entries) >= limit:
                    break
            return entries
    return []


def _external_archive_members(path: Path, limit: int, deadline: _Deadline) -> list[str]:
    return [
        name
        for name, _size in _external_archive_entries(path, limit, deadline)
    ]


def _archive_members(path: Path, limit: int, deadline: _Deadline) -> list[str]:
    extension = file_extension(path)
    if extension in {
        ".zip", ".zipx", ".jar", ".apk", ".aar", ".ipa", ".cbz",
    }:
        members = _zip_members(path, limit)
        if members:
            return members
        # zipfile does not understand every method a zipx/zip alias may use.
        return _external_archive_members(path, limit, deadline)
    if (
        extension.startswith(".tar")
        or extension in {
            ".tgz", ".tbz2", ".tbz", ".txz", ".tzst", ".taz", ".tlz4", ".cbt",
        }
    ):
        members = _tar_members(path, limit)
        if members:
            return members
        # Python's tarfile does not know every compression wrapper (zstd,
        # lz4, ...); fall back to an installed extractor.
    return _external_archive_members(path, limit, deadline)


def _safe_member_path(root: Path, name: str) -> Path | None:
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _extract_external_archive(
    path: Path,
    destination: Path,
    options: ExtractOptions,
    deadline: _Deadline,
) -> list[Path]:
    """Extract an archive with the best available external helper.

    Member names are validated before extraction because 7z/unar/unrar do not
    all reject ``../`` entries themselves. Known uncompressed sizes are checked
    before extraction, and the extracted tree is checked again because some
    helpers do not expose member sizes. The same helper is used for comic
    archives so CBR no longer depends on ``unrar`` being installed.
    """
    entries = _external_archive_entries(
        path, options.max_archive_members + 1, deadline
    )
    if len(entries) > options.max_archive_members:
        logger.warning(
            "%s: archive has more than %d members; skipping extraction",
            path.name,
            options.max_archive_members,
        )
        return []
    listed = [name for name, _size in entries]
    if not listed:
        # Without a member listing the extractor's path handling cannot be
        # validated; extract only when the helper can tell us what it will
        # write. (Native ZIP/TAR extraction has its own safe-path checks.)
        logger.warning("%s: archive listing unavailable; skipping extraction", path.name)
        return []
    if any(_safe_member_path(destination, name) is None for name in listed):
        logger.warning("%s: refusing archive with unsafe member paths", path.name)
        return []
    known_total = sum(size for _name, size in entries if size is not None)
    if known_total > options.max_archive_bytes:
        logger.warning(
            "%s: archive declares %d bytes, over the %d byte limit; "
            "skipping extraction",
            path.name,
            known_total,
            options.max_archive_bytes,
        )
        return []

    commands: list[list[str]] = []
    if has_tool("7z"):
        commands.append(["7z", "x", "-y", f"-o{destination}", str(path)])
    if has_tool("unar"):
        commands.append([
            "unar", "-quiet", "-force-rename", "-o", str(destination), str(path),
        ])
    if has_tool("unrar"):
        commands.append([
            "unrar", "x", "-y", "-o+", str(path), str(destination) + "/",
        ])
    for command in commands:
        result = _run(command, deadline)
        if result is None or result.returncode != 0:
            continue
        extracted: list[Path] = []
        total = 0
        destination_root = destination.resolve()
        for item in sorted(destination.rglob("*"), key=_natural_key):
            if not item.is_file():
                continue
            try:
                resolved = item.resolve()
                resolved.relative_to(destination_root)
                size = item.stat().st_size
            except (OSError, ValueError):
                logger.warning(
                    "%s: refusing archive member outside the extraction root",
                    path.name,
                )
                return []
            if len(extracted) >= options.max_archive_members:
                logger.warning(
                    "%s: more than %d extracted members; skipping extraction",
                    path.name,
                    options.max_archive_members,
                )
                return []
            if total + size > options.max_archive_bytes:
                logger.warning(
                    "%s: extracted data exceeds the %d byte limit; "
                    "skipping extraction",
                    path.name,
                    options.max_archive_bytes,
                )
                return []
            total += size
            extracted.append(item)
        return extracted
    return []


def _extract_zip_members(
    path: Path,
    destination: Path,
    options: ExtractOptions,
    deadline: _Deadline,
) -> list[Path]:
    extracted: list[Path] = []
    total = 0
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if len(extracted) >= options.max_archive_members:
                    break
                if total + info.file_size > options.max_archive_bytes:
                    break
                target = _safe_member_path(destination, info.filename)
                if target is None:
                    continue
                try:
                    if file_extension(target) not in SUPPORTED_EXTENSIONS:
                        with archive.open(info) as probe:
                            sample = probe.read(min(info.file_size, TEXT_SAMPLE_BYTES))
                        if (
                            sniff_bytes(sample) not in SUPPORTED_EXTENSIONS
                            and not is_text_sample(sample)
                        ):
                            continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("wb") as output:
                        remaining = options.max_archive_bytes - total
                        data = source.read(min(info.file_size, remaining))
                        output.write(data)
                        total += len(data)
                except (
                    OSError,
                    RuntimeError,
                    NotImplementedError,
                    zipfile.BadZipFile,
                ):
                    # Encrypted members raise RuntimeError on open. Keep the
                    # members that did extract (and the archive listing) rather
                    # than failing the whole archive.
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                extracted.append(target)
                if deadline.expired():
                    break
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
        return extracted
    return extracted


def _extract_tar_members(
    path: Path,
    destination: Path,
    options: ExtractOptions,
    deadline: _Deadline,
) -> list[Path]:
    extracted: list[Path] = []
    total = 0
    try:
        with tarfile.open(path) as archive:
            for member in archive:
                if not member.isfile():
                    continue
                if len(extracted) >= options.max_archive_members:
                    break
                if total + member.size > options.max_archive_bytes:
                    break
                target = _safe_member_path(destination, member.name)
                if target is None:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    remaining = options.max_archive_bytes - total
                    data = source.read(min(member.size, remaining))
                    output.write(data)
                    total += len(data)
                if not is_supported_file(target):
                    target.unlink(missing_ok=True)
                    continue
                extracted.append(target)
                if deadline.expired():
                    break
    except (OSError, tarfile.TarError):
        return []
    return extracted


def _archive_document(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
    _seen: set[str] | None = None,
) -> Extraction:
    members = _archive_members(path, options.max_archive_members, deadline)
    sections: list[Section] = []
    if members:
        # Listing names is cheap, safe and immediately useful even when nested
        # extraction is disabled.
        listing = "\n".join(members)
        sections.extend(
            Section("Contents", chunk) for chunk in _chunk_text(listing, 8_000)
        )
    if options.archive_depth > 0 and not deadline.expired():
        seen = _seen if _seen is not None else set()
        identity = str(path.resolve())
        if identity in seen:
            # The same archive reached through another nested path would
            # otherwise be extracted and indexed again with no new content.
            return _finish(path, sections, options, deadline)
        seen.add(identity)
        extension = file_extension(path)
        with tempfile.TemporaryDirectory(prefix="zimantic-archive-") as temp_dir:
            destination = Path(temp_dir) / "contents"
            if extension in {
                ".zip", ".zipx", ".jar", ".apk", ".aar", ".ipa", ".cbz",
            }:
                extracted = _extract_zip_members(path, destination, options, deadline)
                if not extracted:
                    extracted = _extract_external_archive(
                        path, destination, options, deadline
                    )
            elif (
                extension.startswith(".tar")
                or extension in {
                    ".tgz", ".tbz2", ".tbz", ".txz", ".tzst", ".taz",
                    ".tlz4", ".cbt",
                }
            ):
                extracted = _extract_tar_members(path, destination, options, deadline)
                if not extracted:
                    extracted = _extract_external_archive(
                        path, destination, options, deadline
                    )
            else:
                extracted = _extract_external_archive(
                    path, destination, options, deadline
                )
            for item in extracted:
                if deadline.expired():
                    break
                if not is_supported_file(item):
                    continue
                nested_options = replace(
                    options, archive_depth=max(0, options.archive_depth - 1)
                )
                nested = _extract_document(
                    item, nested_options, deadline, _seen=seen
                )
                if nested is None:
                    continue
                for section in nested.sections:
                    locator = f"member={item.name}"
                    if section.locator:
                        locator += f"&{section.locator}"
                    sections.append(
                        Section(
                            f"{item.name} :: {section.title}" if section.title else item.name,
                            section.body,
                            locator,
                        )
                    )
    return _finish(path, sections, options, deadline)


# ---------------------------------------------------------------------------
# Unknown / extensionless files
# ---------------------------------------------------------------------------


def _looks_like_text(raw: bytes) -> bool:
    """Whether a sample is decodable text (see ``sniff.looks_like_text``)."""
    return looks_like_text(raw)


def _unknown_document(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
    extension: str,
) -> Extraction | None:
    # A handful of well-known but unlisted extensions still deserve a proper
    # fallback chain rather than a binary-to-text guess.
    if extension in EBOOK_EXTENSIONS:
        if extension == ".chm":
            return _chm_document(path, options, deadline)
        calibre = _calibre_document(path, options, deadline)
        if calibre is not None:
            return calibre
        # Without Calibre, an XML-based ebook (FB2) can still be read as text;
        # returning an empty successful extraction here used to hide it from
        # the content-sniffing fallback below.
        raw = _read_limited(path, TEXT_SAMPLE_BYTES)
        if _looks_like_text(raw):
            return _text_document(path, extension, options, deadline)
        return _finish(path, [], options, deadline)
    raw = _read_limited(path, TEXT_SAMPLE_BYTES)
    if not _looks_like_text(raw):
        return None
    return _text_document(path, extension, options, deadline)


def _extract_known(
    path: Path,
    extension: str,
    options: ExtractOptions,
    deadline: _Deadline,
    _seen: set[str] | None = None,
) -> Extraction | None:
    """Dispatch using an already-decided extension."""
    if extension == ".torrent":
        return _torrent_document(path, options, deadline)
    if extension == ".ts":
        # ".ts" is both TypeScript and an MPEG transport stream. The extension
        # alone cannot decide, so let the sync bytes route a real video to the
        # media reader; otherwise treat it as source code.
        sample = _read_limited(path, TEXT_SAMPLE_BYTES)
        if sniff_bytes(sample) == ".ts":
            return _media_document(path, extension, options, deadline)
    if extension in TEXTUAL_EXTENSIONS:
        return _text_document(path, extension, options, deadline)
    if extension in PDF_EXTENSIONS:
        return _pdf_document(path, options, deadline)
    if extension in EPUB_EXTENSIONS:
        return _epub_document(path, options, deadline)
    if extension in ZIP_OFFICE_EXTENSIONS:
        return _zip_office_document(path, extension, options, deadline)
    if extension in RTF_EXTENSIONS | PS_EXTENSIONS | LEGACY_OFFICE_EXTENSIONS:
        return _legacy_document(path, extension, options, deadline)
    if extension == ".chm":
        return _chm_document(path, options, deadline)
    if extension in COMIC_EXTENSIONS:
        return _comic_document(path, options, deadline, _seen=_seen)
    if extension in ARCHIVE_EXTENSIONS:
        return _archive_document(path, options, deadline, _seen=_seen)
    if extension in SUBTITLE_EXTENSIONS:
        raw = _read_limited(path, options.max_text_chars * 4 + 1024)
        cues = _subtitle_cues(raw, extension, deadline)
        if cues:
            sections = _subtitle_sections(cues, path.stem)
        elif _looks_like_text(raw):
            # A text dialect we could not parse (or a plain lyrics file) is
            # still searchable, but a binary VobSub/Matroska stream must not be
            # decoded into a wall of replacement characters.
            sections = [Section("", chunk) for chunk in _chunk_text(decode_text(raw))]
        else:
            sections = []
        return _finish(path, sections, options, deadline)
    if extension in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
        return _media_document(path, extension, options, deadline)
    if extension in IMAGE_EXTENSIONS:
        return _image_document(path, options, deadline)
    return _unknown_document(path, options, deadline, extension)


def _extract_document(
    path: Path,
    options: ExtractOptions,
    deadline: _Deadline,
    _seen: set[str] | None = None,
) -> Extraction | None:
    extension = file_extension(path)
    result: Extraction | None = None

    if extension in SUPPORTED_EXTENSIONS:
        result = _extract_known(path, extension, options, deadline, _seen)
        if result is not None and result.sections:
            return result

    # The name was unknown, or the parser for the claimed type produced no
    # text.  A quick content check recovers PDFs/EPUBs/Office files with a
    # missing or misleading extension, and redirects a mislabelled container
    # to its real reader instead of treating it as plain text.
    sniffed = sniff_extension(path)
    if (
        sniffed is not None
        and sniffed != extension
        and sniffed in SUPPORTED_EXTENSIONS
    ):
        retry = _extract_known(path, sniffed, options, deadline, _seen)
        if retry is not None and retry.sections:
            return retry
        if result is None:
            result = retry

    # Native HTML/Markdown/text readers handle the overwhelming majority of
    # files.  When they find nothing (malformed markup, an empty body with all
    # text in an unusual element, ...), let Calibre try before giving up.
    if (
        (result is None or not result.sections)
        and extension in _CALIBRE_TEXT_FALLBACK_EXTENSIONS
    ):
        calibre = _calibre_document(path, options, deadline)
        if calibre is not None and calibre.sections:
            return calibre
        if result is None:
            result = calibre

    if result is not None:
        return result
    return _unknown_document(path, options, deadline, extension)


def extract_document(
    path: Path | str,
    options: ExtractOptions | None = None,
    *,
    _deadline: _Deadline | None = None,
) -> Extraction | None:
    """Extract a file, isolating parser and helper failures per file.

    Returns None when the file has no usable text or cannot be read.  No
    exception from an individual format is allowed to abort a build.
    """
    source = Path(path)
    options = options or ExtractOptions()
    deadline = _deadline or _Deadline(options.timeout)
    try:
        if not source.is_file():
            return None
        return _extract_document(source, options, deadline)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        logger.warning("%s: file extraction failed (%s)", source.name, error)
        return None
