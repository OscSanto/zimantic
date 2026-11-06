"""ZIM provider: collection/source identity and the working runtime seam.

The remaining discovery and lifecycle code still lives beside ``Search`` in
phase 00; this module supplies the provider and runtime objects that dispatch
already uses, so phase 01 can move the implementation without renaming any
unchanged source.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Sequence

from ..catalog import header_uuid, is_zim_filename, iter_zim_files
from ..contracts import SourceInfo, SourceResult
from .base import (
    CollectionInfo,
    CollectionRuntime,
    CollectionSnapshot,
    ContentStore,
    DiscoveryBatch,
    ProviderContext,
    SourceQuery,
)
from .config import ProviderConfig, ZimConfig

logger = logging.getLogger(__name__)
_SAFE_SLUG = re.compile(r"[^a-z0-9._-]+")


def _slug(value: str, *, limit: int = 48) -> str:
    text = _SAFE_SLUG.sub("-", str(value).casefold()).strip("-._")
    return (text or "source")[:limit].strip("-._") or "source"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def zim_collection_key(name: str) -> str:
    return f"zim:{name}"


def zim_source_key(collection_name: str, relative_path: str) -> str:
    """Stable ZIM source key from the collection-relative archive path."""
    relative = str(relative_path).replace("\\", "/")
    stem = Path(relative).stem
    return f"zim:{collection_name}:{_slug(stem)}-{_digest(relative)}"


def _relative_archive(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _archive_paths(config: ZimConfig) -> list[Path]:
    root = Path(config.path)
    if root.is_file():
        return [root] if is_zim_filename(root.name) else []
    if root.is_dir():
        return list(iter_zim_files(root))
    return []


def _source_from_archive(
    config: ZimConfig,
    collection: CollectionInfo,
    root: Path,
    path: Path,
) -> SourceInfo:
    relative = _relative_archive(root, path)
    key = zim_source_key(config.name, relative)
    provider_id = None
    try:
        provider_id = header_uuid(path)
    except Exception:
        provider_id = None
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    return SourceInfo(
        key=key,
        provider="zim",
        title=path.stem,
        collection_key=collection.key,
        collection_label=collection.label,
        searchable=False,
        size=size,
        provider_id=provider_id,
    )


class ZimRuntime(CollectionRuntime):
    """Working runtime for one configured ZIM root."""

    def __init__(
        self,
        collection: CollectionInfo,
        sources: Sequence[SourceInfo],
        *,
        backend: Any | None = None,
    ) -> None:
        self.collection = collection
        self._sources = tuple(sources)
        self._backend = backend
        self._version = 1
        self._state = "ready" if self._sources else "ready"
        self._error: str | None = None

    @property
    def snapshot(self) -> CollectionSnapshot:
        from ..media_types import ordered_media_types

        return CollectionSnapshot(
            collection=self.collection,
            source_count=len(self._sources),
            media_types=ordered_media_types(
                family for source in self._sources for family in source.media_types
            ),
            error=self._error,
            discovery_state=self._state,  # type: ignore[arg-type]
            snapshot_version=self._version,
        )

    @property
    def sources(self) -> Sequence[SourceInfo]:
        return self._sources

    def library_sources(self) -> Sequence[SourceInfo]:
        return self._sources

    def search(self, source: SourceInfo, request: SourceQuery) -> SourceResult:
        if self._backend is None or not callable(
            getattr(self._backend, "search_source", None)
        ):
            return SourceResult(
                source=source,
                error="ZIM runtime is not connected to a search backend",
                status="error",
            )
        return self._backend.search_source(source, request)

    def suggest(
        self, source: SourceInfo, query: str, limit: int
    ) -> Sequence[Any]:
        if self._backend is None or not callable(
            getattr(self._backend, "suggest_source", None)
        ):
            return []
        return self._backend.suggest_source(source, query, limit)

    def preview(
        self,
        source: SourceInfo,
        document: Any,
        *,
        max_chars: int,
        max_bytes: int,
        deadline: float | None,
    ) -> tuple[str, str | None]:
        if self._backend is None or not callable(
            getattr(self._backend, "preview_source", None)
        ):
            return "", None
        return self._backend.preview_source(
            source,
            document,
            max_chars=max_chars,
            max_bytes=max_bytes,
            deadline=deadline,
        )

    def content_store(self, source: SourceInfo) -> ContentStore | None:
        # The ZIM store starts in phase 01 and is completed in phase 03.
        return None

    def max_sources_per_search(self) -> int | None:
        return None

    def refresh(self, *, wait: bool = True) -> bool:
        if self._backend is None or not callable(getattr(self._backend, "reload", None)):
            return True
        try:
            self._backend.reload()
        except Exception as error:  # discovery failure is collection-scoped
            self._error = str(error)
            self._state = "error"
            return False
        self._error = None
        self._state = "ready"
        self._version += 1
        return True

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._state in {"ready", "error", "closed"}

    @property
    def discovery_state(self) -> str:
        return self._state

    @property
    def snapshot_version(self) -> int:
        return self._version

    def close(self) -> None:
        self._state = "closed"


class ZimDiscoveryBatch(DiscoveryBatch):
    def __init__(
        self,
        collection: CollectionInfo,
        sources: Sequence[SourceInfo],
        *,
        backend: Any | None = None,
    ) -> None:
        self._collection = collection
        self._sources = tuple(sources)
        self._backend = backend
        self._closed = False

    @property
    def collections(self) -> Sequence[CollectionInfo]:
        return (self._collection,)

    def open(self, collection: CollectionInfo) -> CollectionRuntime:
        return ZimRuntime(collection, self._sources, backend=self._backend)

    def close(self) -> None:
        self._closed = True


class ZimProvider:
    kind = "zim"

    def discover(
        self, config: ProviderConfig, ctx: ProviderContext
    ) -> DiscoveryBatch:
        if not isinstance(config, ZimConfig):
            from .config import ConfigValidationError

            raise ConfigValidationError("zim provider requires a ZimConfig entry")
        label = config.label or config.name
        collection = CollectionInfo(
            key=zim_collection_key(config.name),
            provider="zim",
            label=label,
            title=label,
        )
        root = Path(config.path)
        sources = [
            _source_from_archive(config, collection, root, path)
            for path in _archive_paths(config)
        ]
        sources.sort(key=lambda source: (source.title or "").casefold())
        return ZimDiscoveryBatch(collection, sources, backend=ctx.config.get("search"))


__all__ = [
    "ZimDiscoveryBatch",
    "ZimProvider",
    "ZimRuntime",
    "zim_collection_key",
    "zim_source_key",
]
