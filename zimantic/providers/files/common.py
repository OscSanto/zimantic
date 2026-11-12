"""Shared filesystem, path, glob, and MIME helpers for the files provider.

The module deliberately has no provider imports.  Discovery, indexing, and
serving all use the same path and MIME rules so a file cannot be classified one
way and served another.
"""

from __future__ import annotations

import codecs
import json
import mimetypes
import os
import posixpath
import re
import shutil
import stat as stat_module
import subprocess
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from ...content.serving import is_inline_mimetype
from ...media_types import MEDIA_TYPE_ORDER

# ---------------------------------------------------------------------------
# Path rules
# ---------------------------------------------------------------------------

_SYSTEM_NAMES = frozenset({"__pycache__"})
_TRACK_EXTENSIONS = frozenset({".srt", ".vtt"})


class FilesPathError(ValueError):
    """A path is not a valid collection-relative POSIX path."""


def normalize_relative_path(path: str, *, allow_empty: bool = False) -> str:
    """Validate a collection-relative POSIX path.

    Absolute paths, backslashes, NUL bytes, empty interior segments, ``.`` and
    ``..`` components are rejected.  A trailing slash is preserved for callers
    that need to distinguish a directory request.
    """
    text = str(path)
    if not text:
        if allow_empty:
            return ""
        raise FilesPathError("empty path")
    if (
        text.startswith(("/", "//")) or "\\" in text or "\x00" in text
    ):
        raise FilesPathError("invalid path")
    trailing = text.endswith("/")
    candidate = text[:-1] if trailing else text
    if not candidate:
        raise FilesPathError("invalid path")
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise FilesPathError("invalid path")
    normalized = posixpath.normpath(candidate)
    if normalized != candidate:
        raise FilesPathError("invalid path")
    return normalized + ("/" if trailing else "")


def relative_join(*parts: str) -> str:
    """Join collection-relative POSIX components without escaping the root."""
    joined = posixpath.join(*[part for part in parts if part not in {"", "."}])
    return "" if joined in {"", "."} else joined


def path_components(path: str) -> tuple[str, ...]:
    text = normalize_relative_path(path, allow_empty=True)
    if not text:
        return ()
    return tuple(part for part in text.strip("/").split("/") if part)


def is_hidden_name(name: str) -> bool:
    return name.startswith(".") or name in _SYSTEM_NAMES


def is_hidden_path(path: str) -> bool:
    return any(is_hidden_name(part) for part in path_components(path))


def is_track_sidecar(name: str) -> bool:
    return Path(name).suffix.casefold() in _TRACK_EXTENSIONS


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------

_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a POSIX glob with ``**`` support.

    ``**/`` matches zero or more complete directory components, so
    ``**/*.html`` also matches a root-level ``index.html``.  A bare ``**``
    matches anything including separators.  A single ``*`` and ``?`` never
    cross a directory separator.
    """
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    text = str(pattern)
    if not text:
        raise FilesPathError("empty glob pattern")
    if text.startswith("/"):
        text = text.lstrip("/")
    out: list[str] = ["^"]
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "*":
            if index + 1 < length and text[index + 1] == "*":
                # Consume a run of stars.
                while index < length and text[index] == "*":
                    index += 1
                if index < length and text[index] == "/":
                    out.append("(?:.*/)?")
                    index += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        if char == "[":
            end = index + 1
            if end < length and text[end] in {"!", "^"}:
                end += 1
            if end < length and text[end] == "]":
                end += 1
            while end < length and text[end] != "]":
                end += 1
            if end >= length:
                out.append(re.escape(char))
                index += 1
                continue
            body = text[index + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            index = end + 1
            continue
        out.append(re.escape(char))
        index += 1
    out.append("$")
    compiled = re.compile("".join(out))
    _GLOB_CACHE[pattern] = compiled
    return compiled


def matches_glob(pattern: str, path: str) -> bool:
    return bool(_glob_to_regex(pattern).match(path))


def matches_any(patterns: Sequence[str], path: str, *, directory: bool = False) -> bool:
    """Match a collection-relative path against glob patterns.

    A directory is tested both with and without a trailing slash, so
    ``**/node_modules/**`` excludes the directory itself as well as its
    descendants.
    """
    candidates = [path]
    if directory and not path.endswith("/"):
        candidates.append(path + "/")
    for pattern in patterns:
        if not pattern:
            continue
        for candidate in candidates:
            if matches_glob(pattern, candidate):
                return True
    return False


# ---------------------------------------------------------------------------
# MIME and media families
# ---------------------------------------------------------------------------

_MIME_BY_EXTENSION: dict[str, str] = {
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
    ".xht": "application/xhtml+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".mdown": "text/markdown",
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".cjs": "application/javascript",
    ".json": "application/json",
    ".jsonld": "application/ld+json",
    ".xml": "application/xml",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".ogg": "audio/ogg",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".zip": "application/zip",
}

_NATIVE_DOCUMENT_EXTENSIONS = frozenset(
    {".html", ".htm", ".xhtml", ".xht", ".pdf", ".txt", ".text", ".md", ".markdown", ".mdown"}
)
_OCR_IMAGE_MIMETYPES = frozenset(
    {
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
    }
)
_MEDIA_EXTENSIONS = {
    family: frozenset(
        extension
        for extension, mimetype in _MIME_BY_EXTENSION.items()
        if mimetype.startswith(family + "/")
    )
    for family in ("image", "audio", "video")
}

_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "application/octet-stream"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x00asm", "application/wasm"),
)


def extension_mimetype(path: str) -> str | None:
    suffix = Path(path).suffix.casefold()
    mapped = _MIME_BY_EXTENSION.get(suffix)
    if mapped:
        return mapped
    guessed, _encoding = mimetypes.guess_type(path, strict=False)
    if guessed:
        return guessed.split(";", 1)[0].strip().lower()
    return None


def sniff_mimetype(prefix: bytes) -> str | None:
    """Identify a small set of common magic numbers; never returns HTML."""
    for signature, mimetype in _MAGIC_SIGNATURES:
        if prefix.startswith(signature):
            if signature == b"RIFF" and len(prefix) >= 12:
                if prefix[8:12] == b"WEBP":
                    return "image/webp"
                if prefix[8:12] == b"WAVE":
                    return "audio/wav"
                if prefix[8:12] == b"AVI ":
                    return "video/x-msvideo"
            return mimetype
    if text_encoding(prefix) is not None:
        return "text/plain"
    return None


# Unknown files up to this size are shown as plain text when their bytes are
# text; larger ones remain downloads so a mislabeled dump cannot flood a tab.
TEXT_PREVIEW_MAX_BYTES = 5 << 20
_TEXT_BOMS = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
_TEXT_CONTROLS = frozenset("\t\n\v\f\r\x1b")


def text_encoding(prefix: bytes) -> str | None:
    """Encoding of a byte prefix that reads as text, or ``None`` for binary.

    BOM-marked UTF-16/32 is accepted; otherwise the prefix must be NUL-free
    UTF-8 (a multibyte sequence cut at the prefix end is fine) with almost no
    control characters.
    """
    for bom, encoding in _TEXT_BOMS:
        if prefix.startswith(bom):
            data = prefix[len(bom):]
            break
    else:
        if b"\x00" in prefix:
            return None
        encoding, data = "utf-8", prefix
    try:
        text = codecs.getincrementaldecoder(encoding)().decode(data, final=False)
    except UnicodeDecodeError:
        return None
    controls = sum(
        1 for char in text if ord(char) < 32 and char not in _TEXT_CONTROLS
    )
    return None if controls * 100 > len(text) else encoding


def _is_textual_mimetype(mimetype: str) -> bool:
    value = _normalize_mimetype(mimetype) or ""
    return value.startswith("text/") or value.endswith(("xml", "json", "javascript"))


_LSAR_MIME_BY_FORMAT = {
    "7-zip": "application/x-7z-compressed",
    "ar": "application/x-archive",
    "bzip2": "application/x-bzip2",
    "cpio": "application/x-cpio",
    "gzip": "application/gzip",
    "iso9660": "application/x-iso9660-image",
    "rar": "application/vnd.rar",
    "rar5": "application/vnd.rar",
    "tar": "application/x-tar",
    "xz": "application/x-xz",
    "zip": "application/zip",
}


def _normalize_mimetype(value: str | None) -> str | None:
    text = str(value or "").split(";", 1)[0].strip().lower()
    return text or None


def _puremagic_mimetype(prefix: bytes) -> str | None:
    """Best-effort magic sniffing when the optional puremagic package exists."""

    if not prefix:
        return None
    try:
        import puremagic  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        value = _normalize_mimetype(puremagic.from_string(prefix, mime=True))
    except puremagic.PureError:
        return None
    return value


def _lsar_mimetype(path: str) -> str | None:
    """Identify an archive container through the optional libarchive lsar tool."""

    executable = shutil.which("lsar")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "-j", "-nr", path],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    format_name = str(payload.get("lsarFormatName") or "").strip().casefold()
    return _LSAR_MIME_BY_FORMAT.get(format_name)


def detect_mimetype(
    path: str, prefix: bytes = b"", *, allow_archive_probe: bool = False
) -> str | None:
    """Extension first, then magic bytes, then an optional archive probe.

    The extension mapping stays authoritative so a known file keeps the MIME
    type the rest of the application expects.  Only unknown or missing
    extensions fall through to the built-in signatures, puremagic, and finally
    ``lsar`` for archive formats.  Discovery uses the archive probe; request
    serving does not, so no subprocess runs on the hot path.
    """

    mapped = extension_mimetype(path)
    if mapped:
        return mapped
    sniffed = sniff_mimetype(prefix)
    if sniffed and sniffed != "text/plain":
        return sniffed
    pure = _puremagic_mimetype(prefix)
    if pure and pure != "text/plain":
        return pure
    if allow_archive_probe:
        archive = _lsar_mimetype(path)
        if archive:
            return archive
    return pure or sniffed


def mimetype_for(
    path: str,
    prefix: bytes = b"",
    size: int | None = None,
    *,
    render_sniffed_html: bool = False,
) -> str:
    """The single serving decision for a file: render, show as text, or download.

    Detection reports what the bytes look like; this decides how to serve
    them. Known inline types keep their MIME. Unknown extensions may keep a
    sniffed binary media type. Sniffed HTML renders only with
    ``render_sniffed_html`` (off under secure browsing); otherwise any file
    whose prefix reads as text (and is at most ``TEXT_PREVIEW_MAX_BYTES``) is
    served as inert ``text/plain``. Everything else is an attachment.
    """

    mapped = extension_mimetype(path)
    if mapped and is_inline_mimetype(mapped):
        return mapped
    detected = mapped or detect_mimetype(path, prefix)
    if (
        not mapped
        and detected
        and is_inline_mimetype(detected)
        and not _is_textual_mimetype(detected)
    ):
        return detected
    if (
        render_sniffed_html
        and not mapped
        and _normalize_mimetype(detected) in {"text/html", "application/xhtml+xml"}
    ):
        return _normalize_mimetype(detected)
    encoding = text_encoding(prefix)
    if encoding is not None and (size is None or size <= TEXT_PREVIEW_MAX_BYTES):
        return "text/plain; charset=utf-8" if encoding == "utf-8" else "text/plain"
    if mapped or (detected and not _is_textual_mimetype(detected)):
        return detected or "application/octet-stream"
    return "application/octet-stream"


def media_family_for_path(path: str) -> str | None:
    suffix = Path(path).suffix.casefold()
    for family in ("image", "audio", "video"):
        if suffix in _MEDIA_EXTENSIONS[family]:
            return family
    return None


def media_family_for_mimetype(mimetype: str | None) -> str | None:
    value = _normalize_mimetype(mimetype)
    if not value:
        return None
    if value.startswith("image/"):
        return "image"
    if value.startswith("audio/"):
        return "audio"
    if value.startswith("video/"):
        return "video"
    return None


def is_ocr_candidate(mimetype: str | None, family: str | None = None) -> bool:
    """True for raster images and PDFs that the local OCR engine can read."""

    value = _normalize_mimetype(mimetype)
    if value == "application/pdf":
        return True
    if value in _OCR_IMAGE_MIMETYPES:
        return True
    return family == "image" and not value


def is_media_path(path: str) -> bool:
    return media_family_for_path(path) is not None


def is_native_document_path(path: str) -> bool:
    return Path(path).suffix.casefold() in _NATIVE_DOCUMENT_EXTENSIONS


def content_family(
    path: str, mimetype: str | None = None, prefix: bytes = b""
) -> str | None:
    """Indexing classification; incidental assets return ``None``.

    An explicit MIME type or a bounded prefix lets an extensionless image,
    audio, or video file keep its media family without treating ordinary
    stylesheets and archives as readable documents.
    """

    family = media_family_for_path(path)
    if family is not None:
        return family
    if is_native_document_path(path):
        return "text"
    detected = (
        mimetype
        if mimetype is not None
        else detect_mimetype(path, prefix)
    )
    family = media_family_for_mimetype(detected)
    if family is not None:
        return family
    if _normalize_mimetype(detected) in {
        "application/pdf",
        "application/xhtml+xml",
    }:
        return "text"
    return None


def library_family(
    path: str,
    mimetype: str | None = None,
    prefix: bytes = b"",
    *,
    allow_archive_probe: bool = False,
) -> str:
    """Content family for library filters, including servable ``other`` files."""

    detected = (
        mimetype
        if mimetype is not None
        else detect_mimetype(
            path, prefix, allow_archive_probe=allow_archive_probe
        )
    )
    return content_family(path, detected) or "other"


def ordered_families(families: Iterable[str]) -> tuple[str, ...]:
    present = {str(family) for family in families if family}
    return tuple(family for family in MEDIA_TYPE_ORDER if family in present)


# ---------------------------------------------------------------------------
# Bounded, no-symlink filesystem access
# ---------------------------------------------------------------------------


class FilesConfinedError(OSError):
    """A path cannot be opened without leaving the configured root."""


@dataclass(frozen=True)
class OpenedFile:
    handle: object
    stat: os.stat_result


def _no_follow_supported() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
    )


def _fallback_confined_path(
    root_path: Path, parts: tuple[str, ...]
) -> tuple[Path, os.stat_result]:
    """Best-effort path check where descriptor-relative opens are unavailable.

    Symlinks and reparse points are rejected, but path-based checks cannot
    protect against concurrent filesystem replacement.
    """
    try:
        root_info = os.lstat(root_path)
        if (
            not stat_module.S_ISDIR(root_info.st_mode)
            or _is_reparse_point(root_info)
        ):
            raise FilesConfinedError("root is not a real directory")
        root_real = Path(os.path.realpath(root_path))
        current = root_path
        info = root_info
        for index, part in enumerate(parts):
            current = current / part
            info = os.lstat(current)
            if _is_reparse_point(info):
                raise FilesConfinedError("symlink or reparse point is not allowed")
            if index < len(parts) - 1 and not stat_module.S_ISDIR(info.st_mode):
                raise FilesConfinedError("path component is not a directory")
        resolved = Path(os.path.realpath(current))
        if not is_within(resolved, root_real):
            raise FilesConfinedError("path resolves outside the configured root")
        return current, info
    except FilesConfinedError:
        raise
    except (OSError, ValueError) as error:
        raise FilesConfinedError(str(error)) from error


def _is_reparse_point(info: os.stat_result) -> bool:
    reparse_point = getattr(info, "st_file_attributes", 0) & 0x400
    return stat_module.S_ISLNK(info.st_mode) or bool(reparse_point)


def confined_stat(root: Path | str, relative_path: str) -> os.stat_result:
    """``stat`` one collection-relative path without following symlinks."""
    root_path = Path(root)
    parts = path_components(relative_path)
    if not _no_follow_supported():
        return _fallback_confined_path(root_path, parts)[1]
    if not parts:
        try:
            return os.stat(root_path, follow_symlinks=False)
        except OSError as error:
            raise FilesConfinedError(str(error)) from error
    if _no_follow_supported():
        try:
            root_fd = os.open(
                root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as error:
            raise FilesConfinedError(str(error)) from error
        current = root_fd
        try:
            for part in parts[:-1]:
                fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
                if current != root_fd:
                    os.close(current)
                current = fd
            return os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        except OSError as error:
            raise FilesConfinedError(str(error)) from error
        finally:
            if current != root_fd:
                os.close(current)
            os.close(root_fd)


def open_confined(root: Path | str, relative_path: str) -> OpenedFile:
    """Open a regular file below ``root`` without following symlinks."""
    root_path = Path(root)
    parts = path_components(relative_path)
    if not parts:
        raise FilesConfinedError("empty path")
    if _no_follow_supported():
        try:
            root_fd = os.open(
                root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as error:
            raise FilesConfinedError(str(error)) from error
        current = root_fd
        file_fd: int | None = None
        try:
            for part in parts[:-1]:
                fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
                if current != root_fd:
                    os.close(current)
                current = fd
            try:
                file_fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
            except OSError as error:
                raise FilesConfinedError(str(error)) from error
            info = os.fstat(file_fd)
            if not stat_module.S_ISREG(info.st_mode):
                raise FilesConfinedError("not a regular file")
            handle = os.fdopen(file_fd, "rb")
            file_fd = None
            return OpenedFile(handle, info)
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if current != root_fd:
                os.close(current)
            os.close(root_fd)
    checked_path, info = _fallback_confined_path(root_path, parts)
    if not stat_module.S_ISREG(info.st_mode):
        raise FilesConfinedError("not a regular file")
    try:
        fd = os.open(checked_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError as error:
        raise FilesConfinedError(str(error)) from error
    owned_fd: int | None = fd
    try:
        opened_info = os.fstat(fd)
        if (
            not stat_module.S_ISREG(opened_info.st_mode)
            or (opened_info.st_dev, opened_info.st_ino)
            != (info.st_dev, info.st_ino)
        ):
            raise FilesConfinedError("file changed while it was being opened")
        handle = os.fdopen(fd, "rb")
        owned_fd = None
        return OpenedFile(handle, opened_info)
    finally:
        if owned_fd is not None:
            os.close(owned_fd)


def list_directory(root: Path | str, relative_dir: str = "") -> list[os.DirEntry]:
    """Return immediate entries sorted by name, without following symlinks."""
    root_path = Path(root)
    parts = path_components(relative_dir)
    if not parts:
        directory = root_path
    else:
        directory = root_path / Path(*parts)
    with os.scandir(directory) as iterator:
        return sorted(iterator, key=lambda entry: entry.name)


def iter_confined_directory(
    root: Path | str, relative_dir: str = ""
) -> Iterator[tuple[str, os.stat_result]]:
    """Scan a directory through a no-follow descriptor where supported."""
    root_path = Path(root)
    parts = path_components(relative_dir)
    directory_fd: int | None = None
    try:
        if _no_follow_supported():
            directory_fd = os.open(
                root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            for part in parts:
                child_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = child_fd
            target = directory_fd
        else:
            target, info = _fallback_confined_path(root_path, parts)
            if not stat_module.S_ISDIR(info.st_mode):
                raise FilesConfinedError("not a directory")
        with os.scandir(target) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not _is_reparse_point(info):
                    yield entry.name, info
    except OSError as error:
        raise FilesConfinedError(str(error)) from error
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def regular_file_names(root: Path | str, relative_dir: str = "") -> list[str]:
    """Names of regular, non-symlink files directly inside a directory."""
    names: list[str] = []
    for entry in list_directory(root, relative_dir):
        try:
            if entry.is_symlink():
                continue
            if entry.is_file(follow_symlinks=False):
                names.append(entry.name)
        except OSError:
            continue
    return names


def file_signature(info: os.stat_result) -> tuple[int, int]:
    """``(size, mtime)`` with mtime rounded to the nearest whole second."""
    return (int(info.st_size), round(info.st_mtime_ns / 1_000_000_000))


__all__ = [
    "TEXT_PREVIEW_MAX_BYTES",
    "FilesConfinedError",
    "FilesPathError",
    "OpenedFile",
    "confined_stat",
    "content_family",
    "detect_mimetype",
    "extension_mimetype",
    "file_signature",
    "is_hidden_name",
    "is_hidden_path",
    "is_media_path",
    "is_native_document_path",
    "is_ocr_candidate",
    "is_track_sidecar",
    "is_within",
    "iter_confined_directory",
    "library_family",
    "list_directory",
    "matches_any",
    "matches_glob",
    "media_family_for_mimetype",
    "media_family_for_path",
    "mimetype_for",
    "normalize_relative_path",
    "open_confined",
    "ordered_families",
    "path_components",
    "regular_file_names",
    "relative_join",
    "sniff_mimetype",
    "text_encoding",
]
