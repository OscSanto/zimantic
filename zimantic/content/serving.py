"""Archive-level helpers for serving ZIM articles and random entries."""

import posixpath
import secrets
import sqlite3
from typing import Any

from ..libzim_compat import get_entry_by_id
from ..redirects import RedirectError, resolve_redirect_entry
from .reader import _READABLE_MIMETYPES

RANDOM_ARTICLE_ATTEMPTS = 128
HEAD_READ_BYTES = 256 << 10
MAX_ICON_BYTES = 1 << 20


class ArticleNotFoundError(Exception):
    """The requested local ZIM or article path does not exist."""


class ArticleSourceNotFoundError(ArticleNotFoundError):
    """The named local source is not installed."""


class ArticleUnavailableError(Exception):
    """A local ZIM exists but cannot currently serve the requested article."""


class ArticleTooLargeError(ArticleUnavailableError):
    """The requested entry exceeds the configured serving limit."""


def normalize_article_path(path: str) -> str:
    # The ASGI server already percent-decodes the request path before it
    # gets here. Unquoting again corrupts entries whose names contain
    # literal escapes, such as the LibreTexts cover thumbnails stored as
    # ``.../mindtouch.page%23thumbnail?revision=2``.
    path = str(path)
    if not path or path.startswith("/") or "\\" in path:
        raise ArticleNotFoundError("invalid article path")
    if any(part == ".." for part in path.split("/")):
        raise ArticleNotFoundError("invalid article path")
    trailing_slash = path.endswith("/")
    candidate = path[:-1] if trailing_slash else path
    normalized = posixpath.normpath(candidate)
    if normalized in {"", "."} or normalized != candidate:
        raise ArticleNotFoundError("invalid article path")
    return normalized + "/" if trailing_slash else normalized


def resolve_article_entry(archive: Any, requested_path: str | None = None):
    try:
        entry = (
            archive.main_entry
            if requested_path is None
            else archive.get_entry_by_path(requested_path)
        )
    except (KeyError, RuntimeError, ValueError) as error:
        raise ArticleNotFoundError("article not found") from error
    try:
        return resolve_redirect_entry(entry)
    except RedirectError as error:
        raise ArticleUnavailableError(str(error)) from error


def read_archive_article(
    archive: Any,
    zim_name: str,
    path: str | None,
    head_only: bool,
    max_bytes: int,
) -> dict[str, Any]:
    requested_path = None if path is None else normalize_article_path(path)
    try:
        entry = resolve_article_entry(archive, requested_path)
        item = entry.get_item()
        mimetype = (
            (item.mimetype or "application/octet-stream")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        size = int(getattr(item, "size", 0) or 0)
        if size > max_bytes:
            raise ArticleTooLargeError(
                f"article exceeds the {max_bytes} byte serving limit"
            )
        content = item.content[:HEAD_READ_BYTES] if head_only else item.content
    except (ArticleNotFoundError, ArticleUnavailableError):
        raise
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as error:
        raise ArticleUnavailableError("article content is unavailable") from error
    if not head_only and len(content) > max_bytes:
        raise ArticleTooLargeError(
            f"article exceeds the {max_bytes} byte serving limit"
        )
    return {
        "zim": zim_name,
        "path": entry.path,
        "title": entry.title,
        "mimetype": mimetype,
        "content": content,
    }


def read_archive_icon(
    archive: Any,
    zim_name: str,
    icon_path: str | None,
    max_article_bytes: int,
) -> dict[str, Any]:
    if icon_path:
        return read_archive_article(
            archive,
            zim_name,
            icon_path,
            False,
            max_article_bytes,
        )
    try:
        item = archive.get_illustration_item(48)
        content = item.content
        mimetype = (
            (item.mimetype or "application/octet-stream")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as error:
        raise ArticleNotFoundError("source icon not found") from error
    if len(content) > MAX_ICON_BYTES:
        raise ArticleTooLargeError(
            f"source icon exceeds the {MAX_ICON_BYTES} byte serving limit"
        )
    return {
        "zim": zim_name,
        "path": None,
        "title": zim_name,
        "mimetype": mimetype,
        "content": content,
    }


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
