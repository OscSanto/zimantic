"""Cached discovery of the optional external tools used by file extractors.

Zimantic only needs Python for its core ZIM path.  Ordinary files are different:
some formats are best read with a small command-line helper (``pdftotext``,
``unrtf``, ``tesseract``, ``ffmpeg``, ``ebook-convert``, ...).  Those helpers
are optional, so probing ``PATH`` for every file would be both slow and noisy.
This module resolves each name once per process and remembers the answer.
"""
from __future__ import annotations

import logging
import shutil
import threading

logger = logging.getLogger(__name__)

# Tools referenced by the file extractors.  Keeping the list here makes the
# startup capability report predictable even when a format is never used.
KNOWN_TOOLS = (
    "7z",
    "antiword",
    "catdoc",
    "catppt",
    "convert",
    "ebook-convert",
    "extract_chm",
    "ffmpeg",
    "ffprobe",
    "lsar",
    "magick",
    "pdftotext",
    "ps2ascii",
    "pstotext",
    "tesseract",
    "unar",
    "unrar",
    "unrtf",
    "whisper",
    "xls2csv",
)

_LOCK = threading.Lock()
_CACHE: dict[str, str | None] = {}
_REPORTED = False


def tool_path(name: str) -> str | None:
    """Return the absolute path of ``name``, or None when it is unavailable."""
    with _LOCK:
        if name not in _CACHE:
            _CACHE[name] = shutil.which(name)
        return _CACHE[name]


def has_tool(name: str) -> bool:
    """True when an optional external tool is installed."""
    return tool_path(name) is not None


def capabilities() -> dict[str, bool]:
    """Return availability for every tool the extractors may use."""
    return {name: has_tool(name) for name in KNOWN_TOOLS}


def log_capabilities() -> None:
    """Log missing optional tools once, at info level.

    Missing tools are normal: most users only have one or two file families.
    Repeated warnings per file would drown the useful build progress, so this
    is intentionally a single startup line.
    """
    global _REPORTED
    with _LOCK:
        if _REPORTED:
            return
        _REPORTED = True
    available = capabilities()
    missing = [name for name, present in available.items() if not present]
    if missing:
        logger.info(
            "zimantic: optional file tools not found: %s "
            "(affected formats are skipped)",
            ", ".join(missing),
        )
