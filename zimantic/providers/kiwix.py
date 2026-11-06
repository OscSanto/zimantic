"""Kiwix provider: remote catalog collections and the working runtime seam."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import Any, Sequence

from ..contracts import SourceInfo, SourceResult
from ..media_types import ordered_media_types
from .base import (
    CollectionInfo,
    CollectionRuntime,
    CollectionSnapshot,
    ContentStore,
    DiscoveryBatch,
    ProviderContext,
    SourceQuery,
)
from .config import KiwixConfig, ProviderConfig

logger = logging.getLogger(__name__)
_SAFE_SLUG = re.compile(r"[^a-z0-9._-]+")


def _slug(value: str, *, limit: int = 48) -> str:
    text = _SAFE_SLUG.sub("-", str(value).casefold()).strip("-._")
    return (text or "book")[:limit].strip("-._") or "book"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def kiwix_collection_key(name: str) -> str:
    return f"kiwix:{name}"


def kiwix_source_key(collection_name: str, book: str) -> str:
    return f"kiwix:{collection_name}:{_slug(book)}-{_digest(book)}"


def _config_mapping(config: KiwixConfig) -> dict[str, Any]:
    return {
        "kiwix_server": config.server,
        "kiwix_catalog_path": config.catalog_path,
        "kiwix_catalog_limit": config.catalog_limit,
        "max_kiwix_response_bytes": config.max_response_bytes,
        "max_catalog_entries": config.max_catalog_entries,
        "max_remote_sources": config.max_remote_sources,
    }


def source_from_entry(collection: CollectionInfo, entry: dict[str, str]) -> SourceInfo:
    book = entry["book"]
    source = SourceInfo(
        key=kiwix_source_key(collection.key.split(":", 1)[-1], book),
        provider="kiwix",
        title=entry.get("name") or book,
        collection_key=collection.key,
        collection_label=collection.label,
        searchable=True,
        homepage=None,
    )
    # The exact remote slug is a private runtime locator, never serialized.
    object.__setattr__(source, "_kiwix_book", book)
    return source


class KiwixRuntime(CollectionRuntime):
    """Remote collection runtime with a last-complete-catalog cache."""

    def __init__(
        self,
        collection: CollectionInfo,
        config: KiwixConfig,
        *,
        backend: Any | None = None,
    ) -> None:
        self.collection = collection
        self.config = config
        self._backend = backend
        self._sources: tuple[SourceInfo, ...] = ()
        self._version = 1
        self._state = "loading"
        self._error: str | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._retry_thread: threading.Thread | None = None

    @property
    def snapshot(self) -> CollectionSnapshot:
        with self._lock:
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
        with self._lock:
            return self._sources

    def library_sources(self) -> Sequence[SourceInfo]:
        return self.sources

    def search(self, source: SourceInfo, request: SourceQuery) -> SourceResult:
        from ..searching.kiwix import search_kiwix

        if self._closed:
            return SourceResult(
                source=source, error="Kiwix collection is closed", status="error"
            )
        return search_kiwix(
            source,
            request.fulltext_query or request.query,
            request.limit,
            _config_mapping(self.config),
            request.limit,
            request.deadline,
            lambda text: text,
        )

    def suggest(
        self, source: SourceInfo, query: str, limit: int
    ) -> Sequence[Any]:
        # Kiwix has no title-prefix suggestion backend.
        return ()

    def preview(
        self,
        source: SourceInfo,
        document: Any,
        *,
        max_chars: int,
        max_bytes: int,
        deadline: float | None,
    ) -> tuple[str, str | None]:
        # Existing result metadata already carries the snippet; no new request.
        lead = str(document.get("lead") or "")
        if max_chars > 0:
            lead = lead[:max_chars]
        return lead, document.get("mimetype")

    def content_store(self, source: SourceInfo) -> ContentStore | None:
        return None

    def max_sources_per_search(self) -> int | None:
        return (
            self.config.max_remote_sources
            if self.config.max_remote_sources is not None
            else 100
        )

    def _apply_entries(self, entries: Sequence[dict[str, str]]) -> None:
        sources = tuple(source_from_entry(self.collection, entry) for entry in entries)
        sources = tuple(
            sorted(sources, key=lambda source: (source.title or "").casefold())
        )
        with self._lock:
            self._sources = sources
            self._version += 1
            self._error = None
            self._state = "ready"

    def refresh(self, *, wait: bool = True) -> bool:
        from ..searching.kiwix import catalog_entries

        if self._closed:
            return False
        with self._lock:
            self._state = "loading"

        def _fetch() -> bool:
            entries = catalog_entries(
                _config_mapping(self.config),
                self.config.server,
                12.0,
                catalog_path=self.config.catalog_path,
                catalog_limit=self.config.catalog_limit or 1_000,
                max_entries=self.config.max_catalog_entries or 50_000,
                max_response_bytes=self.config.max_response_bytes,
            )
            if entries is None:
                with self._lock:
                    self._error = "Kiwix catalog refresh failed"
                    self._state = "error"
                return False
            self._apply_entries(entries)
            return True

        if not wait:
            thread = threading.Thread(
                target=_fetch, name="zimantic-kiwix-catalog", daemon=True
            )
            thread.start()
            self._retry_thread = thread
            return True
        with self._lock:
            self._state = "loading"
        return _fetch()

    def wait_ready(self, timeout: float | None = None) -> bool:
        thread = self._retry_thread
        if thread is not None:
            thread.join(timeout)
        return self._state in {"ready", "error"}

    @property
    def discovery_state(self) -> str:
        return self._state

    @property
    def snapshot_version(self) -> int:
        return self._version

    def close(self) -> None:
        self._closed = True
        self._state = "closed"


class KiwixDiscoveryBatch(DiscoveryBatch):
    def __init__(self, collection: CollectionInfo, config: KiwixConfig) -> None:
        self._collection = collection
        self._config = config
        self._closed = False

    @property
    def collections(self) -> Sequence[CollectionInfo]:
        return (self._collection,)

    def open(self, collection: CollectionInfo) -> CollectionRuntime:
        if self._closed:
            raise RuntimeError("discovery batch is closed")
        return KiwixRuntime(collection, self._config)

    def close(self) -> None:
        self._closed = True


class KiwixProvider:
    kind = "kiwix"

    def discover(
        self, config: ProviderConfig, ctx: ProviderContext
    ) -> DiscoveryBatch:
        if not isinstance(config, KiwixConfig):
            from .config import ConfigValidationError

            raise ConfigValidationError(
                "kiwix provider requires a KiwixConfig entry"
            )
        label = config.label or "Kiwix"
        collection = CollectionInfo(
            key=kiwix_collection_key(config.name),
            provider="kiwix",
            label=label,
            title=label,
            metadata={"server": config.server},
        )
        return KiwixDiscoveryBatch(collection, config)


__all__ = [
    "KiwixDiscoveryBatch",
    "KiwixProvider",
    "KiwixRuntime",
    "kiwix_collection_key",
    "kiwix_source_key",
    "source_from_entry",
]
