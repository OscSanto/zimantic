"""Provider-neutral content helpers.

Archive-specific parsing and extraction now live in the provider store that
owns the backing content.  What remains here is the bounded random-article
policy and shared serving limits, which several stores use.
"""

from __future__ import annotations

import secrets
import sqlite3
from typing import Any
from urllib.parse import quote

from ..libzim_compat import get_entry_by_id
from .reader import _READABLE_MIMETYPES

RANDOM_ARTICLE_ATTEMPTS = 128
HEAD_READ_BYTES = 256 << 10
MAX_ICON_BYTES = 1 << 20

# The response policy is provider-neutral.  Files, ZIM, and Kolibri stores all
# return a MIME type; the web layer decides whether it is safe to render inline.
INLINE_MIME_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "text/css",
        "text/javascript",
        "application/javascript",
        "application/json",
        "application/ld+json",
        "application/manifest+json",
        "application/wasm",
        "image/svg+xml",
    }
)
INLINE_MIME_PREFIXES = ("image/", "audio/", "video/", "font/")


def is_inline_mimetype(mimetype: str | None) -> bool:
    """True when a representation may be rendered in place."""
    value = str(mimetype or "").split(";", 1)[0].strip().lower()
    if not value:
        return False
    return value in INLINE_MIME_TYPES or value.startswith(INLINE_MIME_PREFIXES)


def content_disposition(mimetype: str | None, filename: str | None = None) -> str:
    """Return the response disposition for one MIME type."""
    if is_inline_mimetype(mimetype):
        return "inline"
    if not filename:
        return "attachment"
    safe = str(filename).replace("\\", "_").replace('"', "_").replace("\n", "_")
    ascii_name = safe.encode("ascii", "replace").decode("ascii")
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(safe, safe='')}"
    )


def is_random_article_entry(entry: Any) -> bool:
    """True for a readable, non-redirect article entry."""
    if entry.is_redirect or not entry.path:
        return False
    try:
        item = entry.get_item()
    except (KeyError, RuntimeError, ValueError):
        return False
    mimetype = (item.mimetype or "").split(";", 1)[0].strip().lower()
    return mimetype in _READABLE_MIMETYPES


def random_indexed_article(
    index: Any,
    db: sqlite3.Connection,
    attempts: int = RANDOM_ARTICLE_ATTEMPTS,
) -> str | None:
    """Pick a uniformly random indexed article, or None when there are none."""
    if not db.execute("SELECT 1 FROM docs WHERE target IS NULL LIMIT 1").fetchone():
        return None
    archive = index.ensure_open()
    upper = max(1, int(getattr(archive, "entry_count", 0) or 0)) if archive else 1
    for _ in range(attempts):
        point = secrets.randbelow(upper)
        row = db.execute(
            "SELECT path FROM docs WHERE rowid = ? AND target IS NULL",
            (point,),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    count = int(
        db.execute("SELECT COUNT(*) FROM docs WHERE target IS NULL").fetchone()[0]
    )
    if count <= 0:
        return None
    row = db.execute(
        "SELECT path FROM docs WHERE target IS NULL ORDER BY rowid LIMIT 1 OFFSET ?",
        (secrets.randbelow(count),),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def random_fallback_article(
    index: Any,
    attempts: int = RANDOM_ARTICLE_ATTEMPTS,
    get_entry_fn: Any = get_entry_by_id,
) -> str | None:
    """Pick a uniformly random readable entry from an unindexed archive."""
    archive = index.ensure_open()
    if archive is None:
        return None
    upper = max(1, int(getattr(archive, "entry_count", 0) or 0))
    for _ in range(attempts):
        point = secrets.randbelow(upper)
        try:
            entry = get_entry_fn(archive, point)
        except (KeyError, RuntimeError, ValueError, IndexError):
            continue
        if is_random_article_entry(entry):
            return entry.path
    ids = index.random_article_ids
    if ids is None:
        ids = []
        for point in range(upper):
            try:
                entry = get_entry_fn(archive, point)
            except (KeyError, RuntimeError, ValueError, IndexError):
                continue
            if is_random_article_entry(entry):
                ids.append(point)
        index.random_article_ids = ids
    if not ids:
        return None
    try:
        entry = get_entry_fn(archive, ids[secrets.randbelow(len(ids))])
    except (KeyError, RuntimeError, ValueError, IndexError):
        return None
    return entry.path


__all__ = [
    "HEAD_READ_BYTES",
    "INLINE_MIME_PREFIXES",
    "INLINE_MIME_TYPES",
    "MAX_ICON_BYTES",
    "RANDOM_ARTICLE_ATTEMPTS",
    "content_disposition",
    "is_inline_mimetype",
    "is_random_article_entry",
    "random_fallback_article",
    "random_indexed_article",
]
