"""Shared filesystem, path, glob, and MIME helpers for the files provider.

The module deliberately has no provider imports.  Discovery, indexing, and
serving all use the same path and MIME rules so a file cannot be classified one
way and served another.
"""

from __future__ import annotations

import mimetypes
import os
import posixpath
import re
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from ...media_types import MEDIA_TYPE_ORDER

# ---------------------------------------------------------------------------
# Path rules
# ---------------------------------------------------------------------------

_HIDDEN_NAME = re.compile(r"^\.+$")
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
        text.startswith("/")
        or "\\" in text
        or "\x00" in text
        or text.startswith("//")
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
}

_NATIVE_DOCUMENT_EXTENSIONS = frozenset(
    {".html", ".htm", ".xhtml", ".xht", ".pdf", ".txt", ".text", ".md", ".markdown", ".mdown"}
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
    if b"\x00" not in prefix[:4096]:
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "text/plain"
    return None


def mimetype_for(path: str, prefix: bytes = b"") -> str:
    """Extension first, then a constrained prefix sniff.

    Unknown types are an attachment, never ``text/html``.  An unknown
    extension whose bytes happen to look like HTML therefore stays
    ``application/octet-stream``.
    """
    mapped = extension_mimetype(path)
    if mapped:
        return mapped
    if prefix:
        sniffed = sniff_mimetype(prefix)
        if sniffed and sniffed != "text/plain":
            return sniffed
    return "application/octet-stream"


def media_family_for_path(path: str) -> str | None:
    suffix = Path(path).suffix.casefold()
    for family in ("image", "audio", "video"):
        if suffix in _MEDIA_EXTENSIONS[family]:
            return family
    return None


def is_media_path(path: str) -> bool:
    return media_family_for_path(path) is not None


def is_native_document_path(path: str) -> bool:
    return Path(path).suffix.casefold() in _NATIVE_DOCUMENT_EXTENSIONS


def content_family(path: str) -> str | None:
    """Classification used by discovery; incidental assets return ``None``."""
    family = media_family_for_path(path)
    if family is not None:
        return family
    if is_native_document_path(path):
        return "text"
    return None


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


def confined_stat(root: Path | str, relative_path: str) -> os.stat_result:
    """``stat`` one collection-relative path without following symlinks."""
    root_path = Path(root)
    parts = path_components(relative_path)
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
    # A check-then-open realpath fallback is not race-safe.  Fail closed rather
    # than pretend confinement is enforced on a platform without descriptor-
    # relative no-follow opens.
    raise FilesConfinedError(
        "race-safe filesystem confinement is unavailable on this platform"
    )


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
    info = confined_stat(root_path, relative_path)
    if not stat_module.S_ISREG(info.st_mode):
        raise FilesConfinedError("not a regular file")
    path = Path(os.path.realpath(root_path)) / Path(*parts)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError as error:
        raise FilesConfinedError(str(error)) from error
    return OpenedFile(os.fdopen(fd, "rb"), os.fstat(fd))


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
    "FilesConfinedError",
    "FilesPathError",
    "OpenedFile",
    "confined_stat",
    "content_family",
    "extension_mimetype",
    "file_signature",
    "is_hidden_name",
    "is_hidden_path",
    "is_media_path",
    "is_native_document_path",
    "is_track_sidecar",
    "is_within",
    "list_directory",
    "matches_any",
    "matches_glob",
    "media_family_for_path",
    "mimetype_for",
    "normalize_relative_path",
    "open_confined",
    "ordered_families",
    "path_components",
    "regular_file_names",
    "relative_join",
    "sniff_mimetype",
]
