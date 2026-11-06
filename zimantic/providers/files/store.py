"""Path-addressed content store for one files source.

All sources in a files collection share one collection-relative namespace.  A
source key selects the store, but the store may serve any permitted descendant
of the collection root, including shared assets or files belonging to another
source.  Membership only scopes discovery, indexing, search, and library
grouping; it is not a serving authorization boundary.

Every open is descriptor-relative and no-follow on POSIX, so a symlink swap
cannot escape the resolved root.  Hidden and excluded paths are denied here as
well as during discovery and indexing.
"""

from __future__ import annotations

import logging
import os
import secrets
import stat as stat_module
from pathlib import Path
from typing import Any

from ...content.serving import HEAD_READ_BYTES, MAX_ICON_BYTES
from ...content.store import (
    ArticleData,
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
    ContentEntry,
    ContentRange,
)
from .common import (
    FilesConfinedError,
    confined_stat,
    extension_mimetype,
    is_hidden_name,
    is_hidden_path,
    is_track_sidecar,
    matches_any,
    mimetype_for,
    normalize_relative_path,
    open_confined,
    relative_join,
)

logger = logging.getLogger(__name__)

_INDEX_NAMES = ("index.html", "index.htm")
_ICON_NAMES = ("favicon.ico", "favicon.png", "apple-touch-icon.png")
_RANDOM_SAMPLE_LIMIT = 256


def _title(path: str) -> str:
    name = Path(path).name
    stem = Path(name).stem
    return stem or name or path


class FilesContentStore:
    """Read files from one collection root without following symlinks."""

    def __init__(
        self,
        source: Any,
        root: Path | str,
        *,
        membership_root: str = "",
        loose_only: bool = False,
        config: Any | None = None,
        max_article_bytes: int = 1 << 30,
        max_icon_bytes: int = MAX_ICON_BYTES,
        index: Any | None = None,
    ) -> None:
        self.source = source
        self.root = Path(os.path.realpath(Path(root)))
        self.membership_root = (
            normalize_relative_path(membership_root) if membership_root else ""
        )
        self.loose_only = bool(loose_only)
        self.exclude = tuple(getattr(config, "exclude", ()) or ())
        self.max_article_bytes = max(0, int(max_article_bytes))
        self.max_icon_bytes = max(0, int(max_icon_bytes))
        self.index = index
        self._closed = False
        try:
            info = os.stat(self.root, follow_symlinks=False)
        except OSError as error:
            raise ArticleUnavailableError("files source root is unavailable") from error
        if not stat_module.S_ISDIR(info.st_mode):
            raise ArticleUnavailableError("files source root is not a directory")

    # -- path policy -------------------------------------------------------

    def _normalize(self, path: str) -> tuple[str, bool]:
        try:
            normalized = normalize_relative_path(path)
        except ValueError as error:
            raise ArticleNotFoundError("invalid article path") from error
        trailing = normalized.endswith("/")
        candidate = normalized.rstrip("/") if trailing else normalized
        if is_hidden_path(candidate):
            raise ArticleNotFoundError("article not found")
        if matches_any(self.exclude, candidate, directory=trailing):
            raise ArticleNotFoundError("article not found")
        return candidate, trailing

    def _stat(self, path: str) -> os.stat_result | None:
        try:
            info = confined_stat(self.root, path)
        except (FilesConfinedError, OSError, ValueError):
            return None
        if stat_module.S_ISLNK(info.st_mode):
            return None
        return info

    def _regular(self, path: str) -> bool:
        info = self._stat(path)
        return info is not None and stat_module.S_ISREG(info.st_mode)

    def _directory_index(self, directory: str) -> str | None:
        for name in _INDEX_NAMES:
            candidate = relative_join(directory, name)
            if self._regular(candidate):
                return candidate
        return None

    def _resolve_file(self, path: str | None) -> str:
        if path is None or path == "":
            main = self.main_path()
            if main is None:
                raise ArticleNotFoundError("source has no homepage")
            return main
        candidate, trailing = self._normalize(path)
        info = self._stat(candidate)
        if info is None:
            raise ArticleNotFoundError("article not found")
        if stat_module.S_ISDIR(info.st_mode):
            if matches_any(self.exclude, candidate, directory=True):
                raise ArticleNotFoundError("article not found")
            index = self._directory_index(candidate)
            if index is None:
                raise ArticleNotFoundError("article not found")
            return index
        if trailing or not stat_module.S_ISREG(info.st_mode):
            raise ArticleNotFoundError("article not found")
        return candidate

    def _mimetype(self, path: str, prefix: bytes = b"") -> str:
        mapped = extension_mimetype(path)
        if mapped:
            return mapped
        if prefix:
            return mimetype_for(path, prefix)
        try:
            opened = open_confined(self.root, path)
        except FilesConfinedError:
            return "application/octet-stream"
        try:
            prefix = opened.handle.read(4096)
        except OSError:
            prefix = b""
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()
        return mimetype_for(path, prefix)

    # -- contract ----------------------------------------------------------

    def main_path(self) -> str | None:
        return self._directory_index(self.membership_root)

    def resolve(self, path: str | None) -> ContentEntry:
        requested = path
        final = self._resolve_file(path)
        info = self._stat(final)
        if info is None or not stat_module.S_ISREG(info.st_mode):
            raise ArticleNotFoundError("article not found")
        return ContentEntry(
            path=final,
            title=_title(final),
            mimetype=self._mimetype(final),
            size=int(info.st_size),
            redirect_path=(
                requested if requested is not None and requested != final else None
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
        final = self._resolve_file(path)
        info = self._stat(final)
        if info is None or not stat_module.S_ISREG(info.st_mode):
            raise ArticleNotFoundError("article not found")
        size = int(info.st_size)
        limit = max(0, int(max_bytes))
        try:
            opened = open_confined(self.root, final)
        except FilesConfinedError as error:
            # A confinement failure is a traversal/symlink denial, not a
            # transient store outage: never reveal whether the target exists.
            raise ArticleNotFoundError("article not found") from error
        try:
            if head_only:
                prefix_limit = min(limit, HEAD_READ_BYTES)
                try:
                    content = opened.handle.read(prefix_limit)
                except OSError as error:
                    raise ArticleUnavailableError(
                        "article content is unavailable"
                    ) from error
                total = size
                truncated = len(content) < total
            else:
                if size > limit:
                    raise ArticleTooLargeError(
                        f"article exceeds the {limit} byte serving limit"
                    )
                try:
                    content = opened.handle.read(limit + 1)
                except OSError as error:
                    raise ArticleUnavailableError(
                        "article content is unavailable"
                    ) from error
                if len(content) > limit:
                    raise ArticleTooLargeError(
                        f"article exceeds the {limit} byte serving limit"
                    )
                total = size if size is not None else len(content)
                truncated = total is not None and len(content) < total
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()
        return ArticleData(
            source_key=self.source.key,
            path=final,
            title=_title(final),
            mimetype=self._mimetype(final, content[:4096]),
            content=content,
            total=total,
            truncated=truncated,
        )

    def read_range(
        self, path: str, *, start: int, end: int, max_bytes: int
    ) -> ContentRange:
        start = int(start)
        end = int(end)
        if start < 0 or end < start:
            raise ValueError("invalid content range")
        final = self._resolve_file(path)
        info = self._stat(final)
        if info is None or not stat_module.S_ISREG(info.st_mode):
            raise ArticleNotFoundError("article not found")
        total = int(info.st_size)
        if start >= total:
            raise ValueError("content range starts past the representation")
        end = min(end, total - 1)
        limit = max(0, int(max_bytes))
        if limit <= 0:
            raise ArticleTooLargeError("article range exceeds the serving limit")
        if end - start + 1 > limit:
            end = start + limit - 1
        try:
            opened = open_confined(self.root, final)
        except FilesConfinedError as error:
            raise ArticleNotFoundError("article not found") from error
        try:
            try:
                opened.handle.seek(start)
                content = opened.handle.read(end - start + 1)
            except OSError as error:
                raise ArticleUnavailableError(
                    "article content is unavailable"
                ) from error
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()
        if not content:
            raise ArticleNotFoundError("article not found")
        actual_end = start + len(content) - 1
        return ContentRange(
            source_key=self.source.key,
            path=final,
            title=_title(final),
            mimetype=self._mimetype(final, content[:4096]),
            content=content,
            start=start,
            end=actual_end,
            total=total,
        )

    def random_path(self) -> str | None:
        if self.index is not None:
            acquired = False
            try:
                self.index.acquire()
                acquired = True
            except RuntimeError:
                acquired = False
            if acquired:
                try:
                    path = self._random_indexed_path()
                finally:
                    self.index.release()
                if path:
                    return path
        return self._random_shallow_path()

    def _random_indexed_path(self) -> str | None:
        try:
            db = self.index.db_for_thread()
        except Exception:
            return None
        if db is None:
            return None
        try:
            count = int(
                db.execute(
                    "SELECT COUNT(*) FROM docs WHERE target IS NULL"
                ).fetchone()[0]
            )
            if count <= 0:
                return None
            row = db.execute(
                "SELECT path FROM docs WHERE target IS NULL "
                "ORDER BY rowid LIMIT 1 OFFSET ?",
                (secrets.randbelow(count),),
            ).fetchone()
            if row and row[0]:
                try:
                    candidate, _trailing = self._normalize(str(row[0]))
                except ArticleNotFoundError:
                    return None
                return candidate
        except Exception:
            return None
        return None

    def _random_shallow_path(self) -> str | None:
        candidates: list[str] = []
        directory = self.membership_root
        target = self.root / Path(*directory.split("/")) if directory else self.root
        try:
            with os.scandir(target) as entries:
                for examined, entry in enumerate(entries):
                    if (
                        examined >= _RANDOM_SAMPLE_LIMIT
                        or len(candidates) >= _RANDOM_SAMPLE_LIMIT
                    ):
                        break
                    name = entry.name
                    if is_hidden_name(name):
                        continue
                    relative = relative_join(directory, name)
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if self.loose_only:
                                continue
                            if matches_any(
                                self.exclude, relative, directory=True
                            ):
                                continue
                            index = self._directory_index(relative)
                            if index is not None:
                                candidates.append(index)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if matches_any(self.exclude, relative) or is_track_sidecar(
                        name
                    ):
                        continue
                    candidates.append(relative)
        except OSError:
            return None
        if not candidates:
            return None
        return secrets.choice(candidates)

    def icon(self, max_bytes: int) -> ArticleData | None:
        limit = max(0, min(int(max_bytes), self.max_icon_bytes))
        for name in _ICON_NAMES:
            candidate = relative_join(self.membership_root, name)
            if not self._regular(candidate):
                continue
            data = self.read(candidate, head_only=False, max_bytes=limit)
            return data
        return None

    def close(self) -> None:
        self._closed = True


__all__ = ["FilesContentStore"]
