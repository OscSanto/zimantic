"""Path-addressed content store for one ZIM source.

This is the reference local store.  Range responses are completed in phase 03;
until then ``read_range`` reports unsupported without changing the stable
``ContentStore`` contract.
"""

from __future__ import annotations

from typing import Any

from ...content.serving import (
    MAX_ICON_BYTES,
    RANDOM_ARTICLE_ATTEMPTS,
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
    normalize_article_path,
    random_fallback_article,
    random_indexed_article,
    read_archive_article,
    read_archive_icon,
    resolve_article_entry,
)
from ...content.store import ArticleData, ContentEntry
from ...libzim_compat import get_entry_by_id
from .metadata import find_archive_icon_path


class ArchiveContentStore:
    """Read articles, assets, icons, and random entries from one ZIM."""

    def __init__(
        self,
        source: Any,
        archive: Any,
        *,
        index: Any | None = None,
        icon_data: bytes | None = None,
        icon_mime_type: str = "",
        icon_path: str = "",
        max_article_bytes: int,
        max_icon_bytes: int = MAX_ICON_BYTES,
    ) -> None:
        self.source = source
        self.archive = archive
        self.index = index
        self.icon_data = icon_data
        self.icon_mime_type = icon_mime_type
        self.icon_path = icon_path
        self.max_article_bytes = max_article_bytes
        self.max_icon_bytes = max_icon_bytes
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def _acquire(self) -> None:
        if self.index is not None:
            self.index.acquire()

    def _release(self) -> None:
        if self.index is not None:
            self.index.release()

    def close(self) -> None:
        self._closed = True

    # -- content -----------------------------------------------------------

    def main_path(self) -> str | None:
        if self.source.homepage:
            return self.source.homepage
        try:
            return resolve_article_entry(self.archive, None).path
        except (ArticleNotFoundError, ArticleUnavailableError):
            return None

    def resolve(self, path: str | None) -> ContentEntry:
        requested = None if path is None else normalize_article_path(path)
        entry = resolve_article_entry(self.archive, requested)
        try:
            item = entry.get_item()
            mimetype = (
                (item.mimetype or "application/octet-stream")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            size = int(getattr(item, "size", 0) or 0)
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
            mimetype = "application/octet-stream"
            size = None
        return ContentEntry(
            path=entry.path,
            title=getattr(entry, "title", None) or entry.path,
            mimetype=mimetype,
            size=size,
            redirect_path=(
                requested if requested is not None and requested != entry.path else None
            ),
        )

    def exists(self, path: str) -> bool:
        try:
            self.resolve(path)
            return True
        except (ArticleNotFoundError, ArticleUnavailableError, ValueError):
            return False

    def read(
        self, path: str | None, *, head_only: bool, max_bytes: int
    ) -> ArticleData:
        self._acquire()
        try:
            article = read_archive_article(
                self.archive,
                self.source.key,
                path,
                head_only,
                min(max_bytes, self.max_article_bytes),
            )
        finally:
            self._release()
        content = bytes(article["content"])
        total = None
        try:
            resolved = resolve_article_entry(
                self.archive,
                None if path is None else normalize_article_path(path),
            )
            total = int(resolved.get_item().size)
        except Exception:
            total = len(content) if not head_only else None
        return ArticleData(
            source_key=self.source.key,
            path=article["path"],
            title=article["title"],
            mimetype=article["mimetype"],
            content=content,
            total=total,
            truncated=bool(head_only),
        )

    def read_range(
        self, path: str, *, start: int, end: int, max_bytes: int
    ):
        raise NotImplementedError("range requests are implemented in phase 03")

    def random_path(self) -> str | None:
        self._acquire()
        try:
            if self.index is not None:
                db = self.index.db_for_thread()
                if db is not None:
                    try:
                        path = random_indexed_article(
                            self.index,
                            db,
                            attempts=RANDOM_ARTICLE_ATTEMPTS,
                        )
                    except Exception:
                        path = None
                    if path:
                        return path
            if self.index is not None:
                return random_fallback_article(
                    self.index,
                    attempts=RANDOM_ARTICLE_ATTEMPTS,
                    get_entry_fn=get_entry_by_id,
                )
            return random_fallback_article(
                _ArchiveOnly(self.archive),
                attempts=RANDOM_ARTICLE_ATTEMPTS,
                get_entry_fn=get_entry_by_id,
            )
        finally:
            self._release()

    def icon(self, max_bytes: int) -> ArticleData | None:
        limit = min(max_bytes, self.max_icon_bytes)
        if self.icon_data:
            data = self.icon_data
            if len(data) > limit:
                raise ArticleTooLargeError(
                    f"source icon exceeds the {limit} byte serving limit"
                )
            return ArticleData(
                source_key=self.source.key,
                path=None,
                title=self.source.title or self.source.key,
                mimetype=(self.icon_mime_type or "application/octet-stream")
                .split(";", 1)[0]
                .strip()
                .lower(),
                content=data,
                total=len(data),
                truncated=False,
            )
        if self.icon_path and self.exists(self.icon_path):
            return self.read(self.icon_path, head_only=False, max_bytes=limit)
        try:
            icon = read_archive_icon(
                self.archive,
                self.source.key,
                None,
                min(limit, self.max_article_bytes),
            )
        except (ArticleNotFoundError, ArticleTooLargeError):
            icon = None
        if icon is not None:
            data = bytes(icon["content"])
            return ArticleData(
                source_key=self.source.key,
                path=None,
                title=self.source.title or self.source.key,
                mimetype=icon["mimetype"],
                content=data,
                total=len(data),
                truncated=False,
            )
        try:
            homepage = self.main_path()
        except (ArticleNotFoundError, ArticleUnavailableError):
            homepage = None
        path = find_archive_icon_path(self.archive, homepage)
        if not path:
            return None
        return self.read(path, head_only=False, max_bytes=limit)


class _ArchiveOnly:
    """Small adapter so random helpers can reuse an already-open archive."""

    def __init__(self, archive: Any) -> None:
        self._archive = archive
        self.random_article_ids: list[int] | None = None

    def ensure_open(self) -> Any:
        return self._archive


__all__ = ["ArchiveContentStore"]
