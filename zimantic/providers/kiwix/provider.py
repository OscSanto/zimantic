"""Kiwix provider: configured remote servers, catalogs, and runtimes."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

from ...contracts import SourceInfo, SourceResult
from ...diagnostics import public_cause
from ...media_types import ordered_media_types
from ..base import (
    CollectionInfo,
    CollectionRuntime,
    CollectionSnapshot,
    ContentStore,
    DiscoveryBatch,
    ProviderContext,
    SourceQuery,
)
from ..config import ConfigValidationError, KiwixConfig, ProviderConfig
from . import search as kiwix_search
from .catalog import (
    DEFAULT_KIWIX_CATALOG_ENTRIES,
    DEFAULT_KIWIX_CATALOG_LIMIT,
    DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF,
    DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
    DEFAULT_MAX_REMOTE_SOURCES,
    MAX_KIWIX_CATALOG_RETRY_BACKOFF,
    _safe_result_url,
    fetch_catalog,
)

logger = logging.getLogger(__name__)
_SAFE_SLUG = re.compile(r"[^a-z0-9._-]+")
_DEFAULT_SOURCE_TIMEOUT = 12.0


def _slug(value: str, *, limit: int = 48) -> str:
    text = _SAFE_SLUG.sub("-", str(value).casefold()).strip("-._")
    return (text or "book")[:limit].strip("-._") or "book"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def kiwix_collection_key(name: str) -> str:
    return f"kiwix:{name}"


def kiwix_source_key(collection_name: str, book: str) -> str:
    return f"kiwix:{collection_name}:{_slug(book)}-{_digest(book)}"


def _safe_external_url(value: str | None, server: str) -> str | None:
    if not value:
        return None
    try:
        return _safe_result_url(value, server)
    except ValueError as error:
        logger.warning("zimantic: ignoring unsafe Kiwix catalog URL: %s", error)
        return None


def source_from_entry(
    collection: CollectionInfo,
    entry: dict[str, str],
    *,
    server: str | None = None,
) -> SourceInfo:
    book = entry["book"]
    homepage = None
    icon_url = None
    if server is not None:
        homepage = _safe_external_url(entry.get("homepage"), server)
        icon_url = _safe_external_url(entry.get("icon_url"), server)
    source = SourceInfo(
        key=kiwix_source_key(collection.key.split(":", 1)[-1], book),
        provider="kiwix",
        title=entry.get("name") or book,
        collection_key=collection.key,
        collection_label=collection.label,
        searchable=True,
        homepage=homepage,
        icon_url=icon_url,
    )
    # The exact remote slug is a private runtime locator, never serialized.
    object.__setattr__(source, "_kiwix_book", book)
    return source


def _sources_from_entries(
    collection: CollectionInfo,
    entries: Sequence[dict[str, str]],
    *,
    server: str,
) -> tuple[SourceInfo, ...]:
    """Build a deterministic source tuple and reject key collisions."""
    by_key: dict[str, SourceInfo] = {}
    for position, entry in enumerate(entries):
        source = source_from_entry(collection, entry, server=server)
        previous = by_key.get(source.key)
        if previous is not None:
            if getattr(previous, "_kiwix_book", None) != entry["book"]:
                raise ValueError(
                    f"Kiwix source key collision for {source.key!r}"
                )
            continue
        # Preserve the catalog position separately from the display title sort
        # that the library applies later. Explicit collection caps are defined
        # in catalog order, not title order.
        object.__setattr__(source, "_catalog_order", position)
        by_key[source.key] = source
    return tuple(
        sorted(
            by_key.values(),
            key=lambda source: ((source.title or "").casefold(), source.key),
        )
    )


class KiwixRuntime(CollectionRuntime):
    """One configured Kiwix server with a last-complete-catalog cache."""

    def __init__(
        self,
        collection: CollectionInfo,
        config: KiwixConfig,
        *,
        backend: Any | None = None,
        source_timeout: float = _DEFAULT_SOURCE_TIMEOUT,
        cfg: Mapping[str, Any] | None = None,
        urlopen_fn: Any | None = None,
        sleep_fn: Any | None = None,
        retry_backoff: float = DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF,
        max_retry_backoff: float = MAX_KIWIX_CATALOG_RETRY_BACKOFF,
        on_change: Any | None = None,
    ) -> None:
        self.collection = collection
        self.config = config
        self._backend = backend
        self._on_change = on_change
        self._source_timeout = max(0.1, float(source_timeout))
        self._cfg = dict(cfg or {})
        self._urlopen = urlopen_fn
        self._sleep = sleep_fn
        self._retry_backoff = max(0.01, float(retry_backoff))
        self._max_retry_backoff = max(
            self._retry_backoff, float(max_retry_backoff)
        )
        self._sources: tuple[SourceInfo, ...] = ()
        self._version = 1
        self._state = "loading"
        self._error: str | None = None
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._retry_wake = threading.Event()
        self._state_changed = threading.Event()
        self._closed = False
        self._failure_logged = False
        self._last_refresh: float | None = None
        self._retryable = True

    # -- contracts ---------------------------------------------------------

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
                last_refresh=self._last_refresh,
            )

    @property
    def sources(self) -> Sequence[SourceInfo]:
        with self._lock:
            return self._sources

    def library_sources(self) -> Sequence[SourceInfo]:
        return self.sources

    def search(self, source: SourceInfo, request: SourceQuery) -> SourceResult:
        if self._closed:
            return SourceResult(
                source=source, error="Kiwix collection is closed", status="error"
            )
        from ...scoped_settings import resolve_settings

        settings = resolve_settings(self._cfg, source, self.config)
        return kiwix_search.search(
            source,
            request,
            server=self.config.server,
            max_response_bytes=(
                self.config.max_response_bytes
                or DEFAULT_MAX_KIWIX_RESPONSE_BYTES
            ),
            source_timeout=settings.get("source_timeout", self._source_timeout),
            urlopen_fn=self._urlopen,
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
        if self.config.max_remote_sources is not None:
            return self.config.max_remote_sources
        return DEFAULT_MAX_REMOTE_SOURCES

    # -- discovery / lifecycle --------------------------------------------

    @property
    def discovery_state(self) -> str:
        return self._state

    @property
    def snapshot_version(self) -> int:
        return self._version

    def _notify(self) -> None:
        callback = self._on_change
        if callback is None:
            return
        try:
            callback()
        except Exception as error:  # a listener must not break discovery
            logger.warning(
                "zimantic: Kiwix catalog listener failed for %s (%s)",
                self.collection.label,
                public_cause(error),
            )
            logger.debug("Kiwix catalog listener error: %s", error)

    def _set_state(self, state: str, error: str | None) -> None:
        with self._lock:
            if self._closed and state != "closed":
                return
            if (self._state, self._error) == (state, error):
                return
            self._state = state
            self._error = error
            self._version += 1
            self._state_changed.set()

    def _publish_sources(self, sources: Sequence[SourceInfo]) -> None:
        with self._lock:
            if self._closed:
                return
            self._sources = tuple(sources)
            self._version += 1
            self._error = None
            self._state = "ready"
            self._last_refresh = time.time()
            self._state_changed.set()
        if self._failure_logged:
            logger.info(
                "zimantic: Kiwix catalog recovered for %s (%d source(s))",
                self.collection.label,
                len(sources),
            )
        self._failure_logged = False
        self._retry_wake.set()
        self._notify()

    def _publish_failure(self, error: str) -> None:
        with self._lock:
            if self._closed:
                return
        with self._lock:
            self._last_refresh = time.time()
        self._set_state("error", error)
        if not self._failure_logged:
            logger.warning(
                "zimantic: Kiwix catalog refresh failed for %s: %s",
                self.collection.label,
                error,
            )
            self._failure_logged = True
        self._notify()

    def _refresh_once(self) -> bool:
        if self._closed:
            return False
        with self._refresh_lock:
            if self._closed:
                return False
            self._set_state("loading", self._error)
            timeout = self._source_timeout
            traversal = self.config.traversal
            total_timeout = min(
                max(1.0, timeout * 4),
                float(traversal.traversal_timeout_seconds),
            )
            entry_bound = min(
                self.config.max_catalog_entries
                or DEFAULT_KIWIX_CATALOG_ENTRIES,
                traversal.max_visited_entries,
            )
            result = fetch_catalog(
                self.config.server,
                catalog_path=self.config.catalog_path,
                catalog_limit=(
                    self.config.catalog_limit or DEFAULT_KIWIX_CATALOG_LIMIT
                ),
                max_entries=entry_bound,
                max_response_bytes=(
                    self.config.max_response_bytes
                    or DEFAULT_MAX_KIWIX_RESPONSE_BYTES
                ),
                timeout=timeout,
                total_timeout=total_timeout,
                urlopen_fn=self._urlopen,
                sleep_fn=self._sleep,
            )
            if self._closed:
                return False
            self._retryable = result.retryable
            if result.entries is None:
                self._publish_failure(
                    result.error or "Kiwix catalog refresh failed"
                )
                return False
            try:
                sources = _sources_from_entries(
                    self.collection,
                    result.entries,
                    server=self.config.server,
                )
            except ValueError as error:
                self._retryable = False
                self._publish_failure(str(error))
                return False
            self._publish_sources(sources)
            return True

    def _retry_loop(self, first_attempt: bool) -> None:
        delay = self._retry_backoff
        if not first_attempt:
            if self._retry_wake.wait(delay):
                return
            delay = min(delay * 2, self._max_retry_backoff)
        while not self._closed:
            if self._refresh_once():
                return
            if self._closed or not self._retryable:
                return
            if self._retry_wake.wait(delay):
                return
            delay = min(delay * 2, self._max_retry_backoff)

    def _start_retry_loop(self, *, first_attempt: bool) -> None:
        with self._worker_lock:
            if self._closed:
                return
            if self._worker is not None and self._worker.is_alive():
                return
            self._retry_wake.clear()
            self._worker = threading.Thread(
                target=self._retry_loop,
                args=(first_attempt,),
                name=f"zimantic-kiwix-{self.collection.key}",
                daemon=True,
            )
            self._worker.start()

    def refresh(self, *, wait: bool = True) -> bool:
        """Refresh the catalog; non-blocking mode keeps retrying in the background."""
        if self._closed:
            return False
        if not wait:
            self._start_retry_loop(first_attempt=True)
            return True
        ok = self._refresh_once()
        if not ok and not self._closed and self._retryable:
            self._start_retry_loop(first_attempt=False)
        return ok

    def wait_ready(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                state = self._state
            if state in {"ready", "error", "closed"}:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not self._state_changed.wait(remaining) and (
                deadline is not None and time.monotonic() >= deadline
            ):
                return False
            self._state_changed.clear()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._state = "closed"
            self._version += 1
            self._state_changed.set()
        self._retry_wake.set()


class KiwixDiscoveryBatch(DiscoveryBatch):
    def __init__(
        self,
        collection: CollectionInfo,
        config: KiwixConfig,
        ctx: ProviderContext,
    ) -> None:
        self._collection = collection
        self._config = config
        self._ctx = ctx
        self._closed = False

    @property
    def collections(self) -> Sequence[CollectionInfo]:
        return (self._collection,)

    def open(self, collection: CollectionInfo) -> CollectionRuntime:
        if self._closed:
            raise RuntimeError("discovery batch is closed")
        settings = self._ctx.config.settings
        try:
            source_timeout = float(settings.get("source_timeout", _DEFAULT_SOURCE_TIMEOUT))
        except (TypeError, ValueError):
            source_timeout = _DEFAULT_SOURCE_TIMEOUT
        runtime = KiwixRuntime(
            collection,
            self._config,
            source_timeout=source_timeout,
            cfg=settings,
            on_change=self._ctx.on_sources_changed,
        )
        # Discovery never waits for the network.  The runtime serves its last
        # complete in-memory catalog (initially empty) while this refresh runs.
        runtime.refresh(wait=False)
        return runtime

    def close(self) -> None:
        self._closed = True


class KiwixProvider:
    kind = "kiwix"

    def discover(
        self, config: ProviderConfig, ctx: ProviderContext
    ) -> DiscoveryBatch:
        if not isinstance(config, KiwixConfig):
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
        return KiwixDiscoveryBatch(collection, config, ctx)


__all__ = [
    "KiwixDiscoveryBatch",
    "KiwixProvider",
    "KiwixRuntime",
    "kiwix_collection_key",
    "kiwix_source_key",
    "source_from_entry",
]
