"""Path-addressed content store for one ZIM source.

This is the reference local store.  It owns archive entry resolution, MIME and
size normalization, redirect collapsing, bounded reads, ranges, icons, and
random article selection.  The core and web layers only see the canonical
:class:`zimantic.content.store.ContentStore` contract.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...content.path import normalize_article_path
from ...content.serving import (
    HEAD_READ_BYTES,
    MAX_ICON_BYTES,
    RANDOM_ARTICLE_ATTEMPTS,
    random_fallback_article,
    random_indexed_article,
)
from ...content.store import (
    ArticleData,
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
    ContentEntry,
    ContentRange,
)
from ...content.zim_paths import (
    AmbiguousZimPathError,
    ZimPathError,
    ZimPathNotFoundError,
    resolve_zim_path,
)
from .metadata import find_archive_icon_path

logger = logging.getLogger(__name__)


def _normalize_mimetype(value: Any) -> str:
    return (
        (str(value or "application/octet-stream"))
        .split(";", 1)[0]
        .strip()
        .lower()
    )


def _item_size(item: Any) -> int | None:
    value = getattr(item, "size", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ArchiveContentStore:
    """Read articles, assets, icons, and random entries from one ZIM.

    libzim entry reads are thread-safe; ``index`` contributes a courtesy read
    lock so a concurrent reload cannot close the archive under an active read.
    The store itself is read-only.  ``close`` is idempotent and does not close
    an archive still owned by a runtime index.
    """

    def __init__(
        self,
        source: Any,
        archive: Any = None,
        *,
        path: str | Path | None = None,
        archive_factory: Any | None = None,
        index: Any | None = None,
        icon_data: bytes | None = None,
        icon_mime_type: str = "",
        icon_path: str = "",
        max_article_bytes: int,
        max_icon_bytes: int = MAX_ICON_BYTES,
        owns_archive: bool = False,
    ) -> None:
        self.source = source
        self.archive = archive
        self.path = str(path) if path is not None else None
        self.archive_factory = archive_factory
        self.index = index
        self.icon_data = icon_data
        self.icon_mime_type = icon_mime_type
        self.icon_path = icon_path
        self.max_article_bytes = max_article_bytes
        self.max_icon_bytes = max_icon_bytes
        self._owns_archive = owns_archive or archive is None
        self._closed = False

    def ensure_archive(self) -> Any:
        """Open the archive on first use, not at server startup."""
        if self.archive is not None:
            return self.archive
        if not self.path:
            raise ArticleUnavailableError("source store is unavailable")
        factory = self.archive_factory
        if factory is None or factory is object:
            from libzim.reader import Archive

            factory = Archive
        try:
            self.archive = factory(self.path)
        except Exception as error:
            raise ArticleUnavailableError(
                "source store could not be opened"
            ) from error
        return self.archive

    # -- lifecycle ---------------------------------------------------------

    def _acquire(self) -> None:
        if self.index is not None:
            try:
                self.index.acquire()
            except RuntimeError as error:
                raise ArticleUnavailableError(
                    "source store is being reloaded"
                ) from error

    def _release(self) -> None:
        if self.index is not None:
            self.index.release()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._owns_archive:
            return
        archive = self.archive
        self.archive = None
        close = getattr(archive, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    # -- resolution --------------------------------------------------------

    def _resolve_entry(self, path: str | None) -> Any:
        archive = self.ensure_archive()
        try:
            return resolve_zim_path(archive, path)
        except AmbiguousZimPathError as error:
            logger.warning(
                "zimantic: ambiguous content path %r in source %s",
                path or "",
                self.source.key,
            )
            raise ArticleNotFoundError("article not found") from error
        except ZimPathNotFoundError as error:
            raise ArticleNotFoundError("article not found") from error
        except ZimPathError as error:
            logger.warning(
                "zimantic: unverifiable content path %r in source %s",
                path or "",
                self.source.key,
            )
            raise ArticleNotFoundError("article not found") from error
        except (
            AttributeError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise ArticleUnavailableError(
                "article content is unavailable"
            ) from error

    def _entry_item(self, entry: Any) -> Any:
        try:
            return entry.get_item()
        except (
            AttributeError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise ArticleUnavailableError(
                "article content is unavailable"
            ) from error

    def main_path(self) -> str | None:
        if self.source.homepage:
            return self.source.homepage
        self._acquire()
        try:
            return self._resolve_entry(None).path
        except (ArticleNotFoundError, ArticleUnavailableError):
            return None
        finally:
            self._release()

    def resolve(self, path: str | None) -> ContentEntry:
        requested = None if path is None else normalize_article_path(path)
        self._acquire()
        try:
            entry = self._resolve_entry(requested)
            try:
                item = self._entry_item(entry)
                mimetype = _normalize_mimetype(getattr(item, "mimetype", None))
                size = _item_size(item)
            except ArticleUnavailableError:
                mimetype = "application/octet-stream"
                size = None
            try:
                final_path = str(entry.path)
            except (AttributeError, TypeError, ValueError) as error:
                raise ArticleUnavailableError(
                    "article content is unavailable"
                ) from error
            return ContentEntry(
                path=final_path,
                title=getattr(entry, "title", None) or final_path,
                mimetype=mimetype,
                size=size,
                redirect_path=(
                    requested
                    if requested is not None and requested != final_path
                    else None
                ),
            )
        finally:
            self._release()

    def exists(self, path: str) -> bool:
        try:
            self.resolve(path)
            return True
        except (ArticleNotFoundError, ArticleUnavailableError, ValueError):
            return False

    # -- reads -------------------------------------------------------------

    def read(
        self, path: str | None, *, head_only: bool, max_bytes: int
    ) -> ArticleData:
        self._acquire()
        try:
            requested = None if path is None else normalize_article_path(path)
            entry = self._resolve_entry(requested)
            item = self._entry_item(entry)
            size = _item_size(item)
            mimetype = _normalize_mimetype(getattr(item, "mimetype", None))
            limit = max(0, int(max_bytes))
            if head_only:
                prefix_limit = min(limit, HEAD_READ_BYTES)
                try:
                    content = bytes(item.content[:prefix_limit])
                except (
                    AttributeError,
                    KeyError,
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise ArticleUnavailableError(
                        "article content is unavailable"
                    ) from error
                total = size
                truncated = True if size is None else len(content) < size
            else:
                if size is not None and size > limit:
                    raise ArticleTooLargeError(
                        f"article exceeds the {limit} byte serving limit"
                    )
                try:
                    content = bytes(item.content)
                except (
                    AttributeError,
                    KeyError,
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise ArticleUnavailableError(
                        "article content is unavailable"
                    ) from error
                if len(content) > limit:
                    raise ArticleTooLargeError(
                        f"article exceeds the {limit} byte serving limit"
                    )
                total = size if size is not None else len(content)
                truncated = total is not None and len(content) < total
            return ArticleData(
                source_key=self.source.key,
                path=str(entry.path),
                title=getattr(entry, "title", None) or str(entry.path),
                mimetype=mimetype,
                content=content,
                total=total,
                truncated=truncated,
            )
        finally:
            self._release()

    def read_range(
        self, path: str, *, start: int, end: int, max_bytes: int
    ) -> ContentRange:
        start = int(start)
        end = int(end)
        if start < 0 or end < start:
            raise ValueError("invalid content range")
        self._acquire()
        try:
            requested = normalize_article_path(path)
            entry = self._resolve_entry(requested)
            item = self._entry_item(entry)
            total = _item_size(item)
            if total is None:
                raise ArticleUnavailableError(
                    "content length is unavailable"
                )
            if start >= total:
                raise ValueError("content range starts past the representation")
            end = min(end, total - 1)
            limit = max(0, int(max_bytes))
            # libzim exposes the decompressed entry as one buffer rather than a
            # seekable file.  Read it only when the whole representation fits the
            # configured limit; otherwise the honest answer is too-large.
            if total > limit:
                raise ArticleTooLargeError(
                    f"article exceeds the {limit} byte serving limit"
                )
            try:
                content = bytes(item.content)[start : end + 1]
            except (
                AttributeError,
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise ArticleUnavailableError(
                    "article content is unavailable"
                ) from error
            return ContentRange(
                source_key=self.source.key,
                path=str(entry.path),
                title=getattr(entry, "title", None) or str(entry.path),
                mimetype=_normalize_mimetype(getattr(item, "mimetype", None)),
                content=content,
                start=start,
                end=end,
                total=total,
            )
        finally:
            self._release()

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
                return random_fallback_article(
                    self.index,
                    attempts=RANDOM_ARTICLE_ATTEMPTS,
                )
            return random_fallback_article(
                _ArchiveOnly(self.ensure_archive()),
                attempts=RANDOM_ARTICLE_ATTEMPTS,
            )
        finally:
            self._release()

    def icon(self, max_bytes: int) -> ArticleData | None:
        limit = max(0, min(int(max_bytes), self.max_icon_bytes))
        # The discovery-time path already prefers "-/icon" and then homepage
        # links; only then does an embedded illustration participate.
        if self.icon_path and self.exists(self.icon_path):
            return self.read(self.icon_path, head_only=False, max_bytes=limit)
        homepage = self.main_path()
        try:
            discovered = find_archive_icon_path(self.ensure_archive(), homepage)
        except (
            AttributeError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            discovered = None
        if discovered and discovered != self.icon_path and self.exists(discovered):
            return self.read(discovered, head_only=False, max_bytes=limit)
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
                mimetype=_normalize_mimetype(self.icon_mime_type),
                content=data,
                total=len(data),
                truncated=False,
            )
        try:
            item = self.ensure_archive().get_illustration_item(48)
            data = bytes(item.content)
            mimetype = _normalize_mimetype(getattr(item, "mimetype", None))
        except (
            AttributeError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            return None
        if len(data) > limit:
            raise ArticleTooLargeError(
                f"source icon exceeds the {limit} byte serving limit"
            )
        return ArticleData(
            source_key=self.source.key,
            path=None,
            title=self.source.title or self.source.key,
            mimetype=mimetype,
            content=data,
            total=len(data),
            truncated=False,
        )


class _ArchiveOnly:
    """Small adapter so random helpers can reuse an already-open archive."""

    def __init__(self, archive: Any) -> None:
        self._archive = archive
        self.random_article_ids: list[int] | None = None

    def ensure_open(self) -> Any:
        return self._archive


__all__ = ["ArchiveContentStore"]
