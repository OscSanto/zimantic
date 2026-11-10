"""Hybrid search core over provider runtimes."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import secrets
import sqlite3
import threading
import time
from collections import Counter, deque
from collections.abc import Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from .cache import DEFAULT_CACHE_BYTES, DEFAULT_CACHE_SIZE, QueryCache
from .content.path import normalize_article_path
from .content.reader import DEFAULT_MAX_HTML_BYTES, DEFAULT_PREVIEW_CHARS
from .content.store import (
    ArticleData,
    ArticleNotFoundError,
    ArticleSourceNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
    ContentEntry,
    ContentRange,
    ContentStore,
)
from .contracts import SourceInfo, SourceResult
from .diagnostics import private_locators, public_cause, sanitize_public_text
from .indexing.browse import (
    BROWSE_MODES,
    SEMANTIC_BROWSE_MODES,
    diversity_vectors,
    rank_browse_results,
    sort_scores,
)
from .providers.base import (
    CollectionInfo,
    CollectionRuntime,
    CollectionSnapshot,
    EnrichingRuntime,
    IndexedRuntime,
    IsolatedFulltextRuntime,
    ProviderContext,
    SourceQuery,
)
from .providers.config import ProviderConfig, RuntimeConfig
from .scoped_settings import resolve_settings, settings_key
from .searching.config import (
    DEFAULT_MAX_ARTICLE_BYTES,
    _float_config,
    _int_config,
)
from .searching.diversity import diverse_order
from .searching.query import (
    MAX_QUERY_LENGTH,
    _content_terms,
    _fulltext_query,
    _phrase,
    _phrase_words,
    _query_terms,
    _title_query,
)
from .searching.ranking import (
    _collect_ranked_candidates,
    matched_by,
    rank_results,
)
from .settings import (
    default_max_concurrent_searches,
    default_source_query_threads,
    normalize_web_base_path,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Search",
    "SearchBusyError",
    "SearchQueryError",
    "SourceNavigation",
]


DEFAULT_CANDIDATES = 16
DEFAULT_SOURCE_TIMEOUT = 12
DEFAULT_MAX_CONCURRENT_SEARCHES = 4
DEFAULT_MAX_STREAM_RANKS = 16
# A slow source must not leave the page looking frozen. While a search is
# running, snapshots are also emitted on this cadence (whenever at least one
# source finished since the last one), and a lightweight progress event keeps
# the status line moving when nothing has completed yet.
DEFAULT_STREAM_UPDATE_INTERVAL = 0.7
DEFAULT_MIN_COSINE_SIMILARITY = 0.85
DEFAULT_PAGE_SIZE = 10
DEFAULT_MAX_RESULTS = 100
# The UI shows this many source chips per collection group until the reader
# asks for the rest. It is a display budget, never a search-scope promotion.
DEFAULT_SOURCE_FILTER_LIMIT = 10
# An explicitly selected Kiwix collection is bounded by this many admitted
# source tasks. A single explicit source is never capped.
DEFAULT_MAX_EXPLICIT_COLLECTION_SOURCES = 500
MAX_PHRASE_CANDIDATES = 32
# Previews only need the opening of an article. Reading a bounded prefix keeps
# a few unusually large pages from dominating search latency; the preview path
# falls back to the full limit when the prefix has no text at all. Phrase
# checks deliberately still read the whole article.
PREVIEW_HTML_BYTES = 32 << 10
# How long an owner waits for a search slot before reporting busy. A cold
# search on a Pi can run for several seconds, and a superseded stream can take
# a moment to release its slot, so a short wait would turn normal queueing into
# a misleading 503. Absorbing that burst lets an in-flight search finish and the
# query be answered (or served from cache) instead of failing.
SLOT_WAIT_SECONDS = 10.0
# Bound how long an identical-query waiter (or an abandoned finalizer) waits
# for a source future. A hung source must not hang every following request.
SEARCH_CLAIM_WAIT_SECONDS = 60.0
HEALTH_CACHE_TTL_SECONDS = 2.0


class SearchQueryError(ValueError):
    """The requested query cannot be processed."""


class SearchBusyError(Exception):
    """Too many searches are already running; the client should retry."""


@dataclass(frozen=True)
class SourceNavigation:
    """One adjacent source in a scoped ``open`` process."""

    key: str
    title: str


@dataclass(frozen=True)
class _ResolvedScope:
    """One immutable search-scope resolution and its publication generation."""

    sources: list[SourceInfo]
    selective: bool
    cache_selection: tuple[str, ...] | None
    requested: int
    source_cap: bool
    generation: int


def _source_title(source: SourceInfo) -> str:
    return source.title or source.key


class Search:
    cfg: Mapping[str, Any] = {}
    semantic: bool = False
    open_scope: Any | None = None
    embedder: Any = None
    _defer_faiss: bool = False
    runtimes: ClassVar[dict[str, CollectionRuntime]] = {}
    collections: ClassVar[dict[str, CollectionInfo]] = {}
    _library_source_index: ClassVar[dict[str, SourceInfo]] = {}
    _discovery_batches: tuple[Any, ...] = ()
    _collection_snapshots: ClassVar[dict[str, CollectionSnapshot]] = {}
    runtime_config: RuntimeConfig
    providers: ClassVar[dict[str, Any]] = {}
    _model_fingerprint: str = ""
    _cache_generation: int = 0
    preview_cache: QueryCache | None = None
    _probe_cache: dict[tuple[str, ...], Any] | None = None
    _probe_cache_lock: threading.Lock | None = None
    page_size: int = DEFAULT_PAGE_SIZE
    preview_limit: int = DEFAULT_PAGE_SIZE
    candidate_count: int = DEFAULT_CANDIDATES
    max_results: int = DEFAULT_MAX_RESULTS
    source_timeout: int = DEFAULT_SOURCE_TIMEOUT
    # None means searches use the in-process libzim searcher. ``serve`` installs
    # a helper-process pool for crash isolation.
    fulltext_pool: Any | None = None

    @property
    def preview_chars(self) -> int:
        if "_preview_chars" in self.__dict__:
            return self.__dict__["_preview_chars"]
        return _int_config(self.cfg, "max_preview_chars", DEFAULT_PREVIEW_CHARS)

    @preview_chars.setter
    def preview_chars(self, value: int) -> None:
        self.__dict__["_preview_chars"] = value

    @property
    def max_stream_ranks(self) -> int:
        if "_max_stream_ranks" in self.__dict__:
            return self.__dict__["_max_stream_ranks"]
        return _int_config(self.cfg, "max_stream_ranks", DEFAULT_MAX_STREAM_RANKS)

    @max_stream_ranks.setter
    def max_stream_ranks(self, value: int) -> None:
        self.__dict__["_max_stream_ranks"] = value

    @property
    def stream_update_interval(self) -> float:
        if "_stream_update_interval" in self.__dict__:
            return self.__dict__["_stream_update_interval"]
        return _float_config(
            self.cfg,
            "stream_update_interval",
            DEFAULT_STREAM_UPDATE_INTERVAL,
            0.0,
            math.inf,
        )

    @stream_update_interval.setter
    def stream_update_interval(self, value: float) -> None:
        self.__dict__["_stream_update_interval"] = value

    @property
    def max_html_bytes(self) -> int:
        if "_max_html_bytes" in self.__dict__:
            return self.__dict__["_max_html_bytes"]
        return _int_config(self.cfg, "max_html_bytes", DEFAULT_MAX_HTML_BYTES)

    @max_html_bytes.setter
    def max_html_bytes(self, value: int) -> None:
        self.__dict__["_max_html_bytes"] = value

    @property
    def max_article_bytes(self) -> int:
        if "_max_article_bytes" in self.__dict__:
            return self.__dict__["_max_article_bytes"]
        return _int_config(self.cfg, "max_article_bytes", DEFAULT_MAX_ARTICLE_BYTES)

    @max_article_bytes.setter
    def max_article_bytes(self, value: int) -> None:
        self.__dict__["_max_article_bytes"] = value

    def __init__(
        self,
        config: RuntimeConfig,
        embedder=None,
        semantic: bool = True,
        archive_factory: Any | None = None,
        scope: Any | None = None,
    ):
        self.runtime_config = config
        self.cfg = cfg = config.settings
        self.embedder = embedder
        # An explicit, process-scoped ZIM selection for ``zimantic open``.
        # ``serve`` passes None and keeps configured-library discovery.
        self.open_scope = scope
        # ``None`` lets the ZIM provider open libzim archives normally; tests
        # may pass ``object`` to disable archive inspection.
        self.archive_factory = archive_factory
        # Fast mode skips FAISS and the model.
        self.semantic = bool(semantic)
        self._defer_faiss = self.semantic
        # Filled from the configured model before deferred vectors are opened.
        self._model_fingerprint = ""
        # `start_faiss` may need to embed probes before it can decide whether an
        # index built with another model file is still compatible. `start_embedder`
        # signals this event once the model has loaded (or failed to load).
        self._embedder_ready = threading.Event()
        self._embedder_started = False
        # Probe embeddings are identical for every index built with the same
        # probe list; compute them once per serve process.
        self._probe_cache: dict[tuple[str, ...], Any] = {}
        self._probe_cache_lock = threading.Lock()
        self.sources: dict[str, SourceInfo] = {}
        # None until serve enables crash isolation; tests and library callers
        # keep using the in-process searcher.
        self.fulltext_pool: Any | None = None
        self._state_lock = threading.RLock()
        self._reload_lock = threading.Lock()
        self.source_timeout = _int_config(cfg, "source_timeout", DEFAULT_SOURCE_TIMEOUT)
        self.candidate_count = _int_config(cfg, "candidate_count", DEFAULT_CANDIDATES)
        self.source_workers = _int_config(
            cfg, "source_query_threads", default_source_query_threads()
        )
        self.source_filter_limit = _int_config(
            cfg, "source_filter_limit", DEFAULT_SOURCE_FILTER_LIMIT
        )
        self.collection_source_limit = _int_config(
            cfg, "collection_source_limit", 1000
        )
        self.max_explicit_collection_sources = _int_config(
            cfg,
            "max_explicit_collection_sources",
            DEFAULT_MAX_EXPLICIT_COLLECTION_SOURCES,
        )
        self.preview_chars = _int_config(cfg, "max_preview_chars", DEFAULT_PREVIEW_CHARS)
        self.max_html_bytes = _int_config(
            cfg, "max_html_bytes", DEFAULT_MAX_HTML_BYTES
        )
        self.max_article_bytes = _int_config(
            cfg, "max_article_bytes", DEFAULT_MAX_ARTICLE_BYTES
        )
        self.max_results = _int_config(cfg, "max_results", DEFAULT_MAX_RESULTS)
        # A page can never be larger than the ranked pool it is sliced from.
        # Clamping once here keeps /config, the page headers, and the frontend
        # pager consistent even if a hand-edited config sets page_size higher.
        self.page_size = min(
            _int_config(cfg, "page_size", DEFAULT_PAGE_SIZE),
            self.max_results,
        )
        # Reading a preview means opening and parsing the article. The UI only
        # displays one page, so hydrating more than that is wasted work on the
        # request path; 0 turns previews off (the lead field stays empty).
        self.preview_limit = _int_config(cfg, "preview_limit", self.page_size, minimum=0)
        self.max_concurrent_searches = _int_config(
            cfg, "max_concurrent_searches", default_max_concurrent_searches()
        )
        self.slot_wait_seconds = _float_config(
            cfg, "slot_wait_seconds", SLOT_WAIT_SECONDS, 0.0, 120.0
        )
        self.max_stream_ranks = _int_config(
            cfg, "max_stream_ranks", DEFAULT_MAX_STREAM_RANKS
        )
        self.stream_update_interval = _float_config(
            cfg,
            "stream_update_interval",
            DEFAULT_STREAM_UPDATE_INTERVAL,
            0.0,
            math.inf,
        )
        self._search_slots = threading.BoundedSemaphore(self.max_concurrent_searches)
        # Bound the number of requests parked waiting for a search slot. Without
        # this, a burst of slower-than-usual searches could occupy every worker
        # thread in the server's sync thread pool while they wait.
        self._slot_waiters = threading.BoundedSemaphore(self.max_concurrent_searches)
        self._executor = ThreadPoolExecutor(
            max_workers=self.source_workers,
            thread_name_prefix="zimantic-search",
        )
        # Identical in-flight queries share one computation.
        self._inflight: dict[Any, Future] = {}
        self._inflight_lock = threading.Lock()
        self._cache_generation = 0
        self.cache = QueryCache(
            _int_config(cfg, "cache_size", DEFAULT_CACHE_SIZE, minimum=0),
            _int_config(cfg, "cache_bytes", DEFAULT_CACHE_BYTES, minimum=0),
        )
        self.preview_cache = QueryCache(
            _int_config(cfg, "cache_size", DEFAULT_CACHE_SIZE, minimum=0),
            _int_config(cfg, "cache_bytes", DEFAULT_CACHE_BYTES, minimum=0),
        )
        # Provider/collection state: every provider path goes through a runtime.
        from .providers.registry import builtin_providers

        self.providers = {
            provider.kind: provider for provider in builtin_providers()
        }
        self.runtimes: dict[str, CollectionRuntime] = {}
        self.collections: dict[str, CollectionInfo] = {}
        self._discovery_batches: list[Any] = []
        self._collection_snapshots: dict[str, CollectionSnapshot] = {}
        self._library_source_index: dict[str, SourceInfo] = {}
        self._process_token = secrets.token_hex(8)
        self._aggregate_revision = 0
        self._closed = False
        self._background_discovery_threads: list[threading.Thread] = []
        self.search_timeout_seconds = float(config.search_timeout_seconds)
        # Provider runtimes own their discovery.  Kiwix starts a bounded
        # background catalog refresh when its batch opens, so an unreachable
        # server cannot delay startup.
        self.refresh_collections()

    @property
    def web_base_path(self) -> str:
        """Normalized URL prefix for generated links (reverse-proxy support)."""
        return normalize_web_base_path(self.cfg.get("web_base_path"))

    def private_locators(self) -> tuple[str, ...]:
        """Configured filesystem locators that must never leave the process."""
        return private_locators(self.runtime_config)

    def public_error(self, error: BaseException) -> str:
        """Return a stable, locator-free cause for a provider failure."""
        return public_cause(error, self.private_locators())

    def public_text(self, value: Any) -> Any:
        return sanitize_public_text(value, self.private_locators())

    def start_embedder(self, factory) -> None:
        """Build the embedding model on a background thread.

        Loading the ONNX model and sentencepiece vocabulary costs most of
        `serve` startup, so it happens off the critical path: the server starts
        serving title and full-text results immediately and gains semantic search
        as soon as `factory()` returns. Searches issued meanwhile degrade
        gracefully; `_query_vector` treats a missing embedder as no vectors.
        """
        self._embedder_started = True

        def _load() -> None:
            try:
                embedder = factory()
            except Exception as error:  # missing/corrupt model must not kill serve
                logger.warning(
                    "zimantic: semantic search disabled: embedding model "
                    "unavailable (%s); falling back to title and full-text "
                    "(lexical) search",
                    error,
                )
            else:
                with self._state_lock:
                    self.embedder = embedder
                logger.info("zimantic: embedding model ready; semantic search enabled")
            finally:
                self._embedder_ready.set()

        threading.Thread(target=_load, name="zimantic-embedder", daemon=True).start()

    def _bind_runtime(self, runtime: CollectionRuntime) -> None:
        """Give a provider runtime the core-owned search services it needs."""
        if isinstance(runtime, IsolatedFulltextRuntime):
            runtime.set_fulltext_pool(self.fulltext_pool)
        if not isinstance(runtime, IndexedRuntime):
            return
        runtime.bind_search(
            semantic=self.semantic,
            defer_faiss=self._defer_faiss,
            embedder_getter=lambda: self.embedder,
            embedder_ready_getter=lambda: (
                self._embedder_ready,
                self._embedder_started,
            ),
            model_fingerprint_getter=lambda: self._model_fingerprint,
            preview_cache=self.preview_cache,
            fulltext_pool=self.fulltext_pool,
            candidate_count=self.candidate_count,
            preview_chars=self.preview_chars,
            max_html_bytes=self.max_html_bytes,
            max_article_bytes=self.max_article_bytes,
            min_cosine_similarity=_float_config(
                self.cfg,
                "min_cosine_similarity",
                DEFAULT_MIN_COSINE_SIMILARITY,
                -1.0,
                1.0,
            ),
            cfg=self.cfg,
        )

    def _start_runtime_faiss(self, runtime: CollectionRuntime) -> None:
        if not isinstance(runtime, IndexedRuntime):
            return
        try:
            runtime.start_faiss()
        except Exception as error:
            logger.warning(
                "zimantic: semantic search disabled for %s (%s); "
                "falling back to title and full-text",
                runtime.collection.label,
                self.public_error(error),
            )
            logger.debug("FAISS reopen error: %s", error)

    def start_faiss(self) -> None:
        """Load FAISS and vector indexes off the server startup path."""
        if not self.semantic or not self._defer_faiss:
            return

        def _load() -> None:
            try:
                import faiss  # noqa: F401 - load the native module on this worker
            except Exception as error:
                logger.warning(
                    "zimantic: semantic search disabled: FAISS unavailable (%s); "
                    "falling back to title and full-text (lexical) search",
                    self.public_error(error),
                )
                logger.debug("FAISS load error: %s", error)
                return

            model_dir = self.cfg.get("model_dir")
            if model_dir:
                try:
                    from .embed import model_fingerprint

                    self._model_fingerprint = model_fingerprint(model_dir)
                except Exception as error:
                    logger.warning(
                        "zimantic: could not fingerprint the embedding model (%s); "
                        "vectors will not be checked against it",
                        self.public_error(error),
                    )
                    logger.debug("model fingerprint error: %s", error)

            with self._state_lock:
                self._defer_faiss = False
                runtimes = list(self.runtimes.values())
                for runtime in runtimes:
                    self._bind_runtime(runtime)
            for runtime in runtimes:
                self._start_runtime_faiss(runtime)
            with self._state_lock:
                semantic_indexes = sum(
                    1
                    for runtime in runtimes
                    if isinstance(runtime, IndexedRuntime)
                    for index in runtime.indexes.values()
                    if index.semantic
                )
            logger.info(
                "zimantic: FAISS ready; semantic search enabled for %d index(es)",
                semantic_indexes,
            )

        threading.Thread(target=_load, name="zimantic-faiss", daemon=True).start()

    def start_fulltext_isolation(self) -> None:
        """Share one bounded native-search pool across ZIM collections."""
        from .fulltext import FulltextPool
        from .settings import default_fulltext_workers

        with self._state_lock:
            if self._closed:
                return
            runtimes = [
                runtime for runtime in self.runtimes.values()
                if isinstance(runtime, IsolatedFulltextRuntime)
            ]
            if self.fulltext_pool is None:
                configured = self.cfg.get("libzim_search_processes")
                workers = (
                    configured if isinstance(configured, int)
                    and not isinstance(configured, bool) and configured > 0
                    else default_fulltext_workers(source_workers=self.source_workers)
                )
                backend = self.cfg.get("libzim_search_backend", "processes")
                self.fulltext_pool = FulltextPool(workers, backend=backend)
                logger.info(
                    "zimantic: native full-text budget: %d shared %s slot(s)",
                    workers, backend,
                )
            for runtime in runtimes:
                runtime.set_fulltext_pool(self.fulltext_pool)

    def reload(
        self, *, scan_catalog: bool = True, refresh: bool = True
    ) -> dict[str, Any]:
        """Rescan provider sources without restarting.

        Runtimes reconcile cheap path stats, revalidate catalog locators, and
        preserve open indexes whose files did not change.  Cached answers are
        invalidated when the published source set or an index changed.
        """
        def _index_tokens() -> dict[str, tuple[Any, ...]]:
            with self._state_lock:
                return {
                    key: (
                        index.generation,
                        index.db_signature,
                        index.zim_signature,
                        index.done,
                    )
                    for runtime in self.runtimes.values()
                    if isinstance(runtime, IndexedRuntime)
                    for key, index in runtime.indexes.items()
                }

        with self._reload_lock:
            with self._state_lock:
                old_keys = set(self.sources)
                old_indexes = set(_index_tokens())
            old_tokens = _index_tokens()
            if refresh or scan_catalog:
                # Rerun entry discovery so a newly installed Kolibri channel or
                # an added/removed source is published atomically.  Provider
                # runtimes keep their own index generations, so unchanged
                # sources are reopened without rebuilding or rereading content.
                self.refresh_collections(wait=True)
            else:
                self._publish_runtime_sources()
            with self._state_lock:
                new_keys = set(self.sources)
                new_indexes = set(_index_tokens())
            new_tokens = _index_tokens()
            added = sorted(new_keys - old_keys)
            removed = sorted(old_keys - new_keys)
            upgraded = sorted(new_indexes - old_indexes)
            if added or removed or upgraded or new_tokens != old_tokens:
                # A replaced index can keep the same source key, so the source
                # set alone is not enough to invalidate cached pools or listing
                # snapshot tokens.
                with self._state_lock:
                    self._cache_generation += 1
                    self._aggregate_revision += 1
                self.cache.clear()
                preview_cache = self.preview_cache
                if preview_cache is not None:
                    preview_cache.clear()
            if self.fulltext_pool is not None:
                self.fulltext_pool.clear_broken()
            return {
                "indexes": sorted(new_indexes),
                "added": added,
                "removed": removed,
                "upgraded": upgraded,
            }

    def refresh_sources(self) -> list[dict[str, Any]]:
        """Refresh every provider runtime and republish the source set."""
        with self._state_lock:
            runtimes = list(self.runtimes.values())
        for runtime in runtimes:
            try:
                runtime.refresh(wait=True)
            except Exception as error:
                logger.warning(
                    "zimantic: source refresh failed for %s (%s)",
                    runtime.collection.label,
                    self.public_error(error),
                )
                logger.debug("source refresh error: %s", error)
        return self._publish_runtime_sources()

    def resolve_source(self, identifier: str) -> SourceInfo | None:
        """Resolve an exact canonical source key; no aliases or fallbacks."""
        with self._state_lock:
            source = self.sources.get(str(identifier))
            if source is not None:
                return source
            return self._library_source_index.get(str(identifier))

    def _configured_provider_entries(self) -> tuple[ProviderConfig, ...]:
        return self.runtime_config.collections

    def refresh_collections(self, *, wait: bool = False) -> list[dict[str, Any]]:
        """Rerun provider discovery for configured entries and publish snapshots."""
        if self.open_scope is not None:
            return self._refresh_open_scope(wait=wait)
        ctx = ProviderContext(
            config=self.runtime_config,
            index_dir=Path(self.cfg.get("index_dir") or "."),
            web_base_path=self.web_base_path,
            logger=logger,
            archive_factory=getattr(self, "archive_factory", None),
            persist_catalog=bool(self.cfg.get("index_dir")),
            on_sources_changed=self._publish_runtime_sources,
        )
        with self._state_lock:
            previous_runtimes = dict(self.runtimes)
            previous_batches = list(getattr(self, "_discovery_batches", []))
        new_runtimes: dict[str, CollectionRuntime] = {}
        new_collections: dict[str, CollectionInfo] = {}
        new_batches: list[Any] = []
        diagnostics: list[str] = []
        failed_entries: set[str] = set()

        def matches_entry(runtime: CollectionRuntime, entry: ProviderConfig) -> bool:
            runtime_config = runtime.config
            return (
                runtime_config.kind == entry.kind
                and runtime_config.name == entry.name
            )

        entries = self._configured_provider_entries()
        deferred_entries: tuple[ProviderConfig, ...] = ()
        if not wait and any(entry.kind == "zim" for entry in entries):
            deferred_entries = tuple(
                entry for entry in entries if entry.kind != "zim"
            )
            if deferred_entries:
                entries = tuple(entry for entry in entries if entry.kind == "zim")
        for entry in entries:
            provider = self.providers.get(entry.kind)
            if provider is None:
                diagnostics.append(f"no provider registered for {entry.kind!r}")
                failed_entries.add(entry.name)
                continue
            try:
                batch = provider.discover(entry, ctx)
            except Exception as error:
                diagnostics.append(f"{entry.name}: {entry.kind} discovery failed: {error}")
                failed_entries.add(entry.name)
                continue
            batch_error = getattr(batch, "error", None)
            if batch_error:
                diagnostics.append(
                    f"{entry.name}: {entry.kind} discovery: {batch_error}"
                )
            try:
                for collection in batch.collections:
                    runtime = batch.open(collection)
                    self._bind_runtime(runtime)
                    new_runtimes[collection.key] = runtime
                    new_collections[collection.key] = collection
                if batch_error and not batch.collections:
                    failed_entries.add(entry.name)
                new_batches.append(batch)
            except Exception as error:
                diagnostics.append(f"{entry.name}: {entry.kind} open failed: {error}")
                failed_entries.add(entry.name)
                for key, runtime in list(new_runtimes.items()):
                    if not matches_entry(runtime, entry):
                        continue
                    new_runtimes.pop(key, None)
                    new_collections.pop(key, None)
                    try:
                        runtime.close()
                    except Exception:
                        pass
                batch.close()

        kept_old_batches = False
        for entry in entries:
            if entry.name not in failed_entries:
                continue
            for key, runtime in previous_runtimes.items():
                if key in new_runtimes or not matches_entry(runtime, entry):
                    continue
                new_runtimes[key] = runtime
                new_collections[key] = runtime.collection
                kept_old_batches = True
        if kept_old_batches:
            new_batches.extend(previous_batches)
        if not new_runtimes and previous_runtimes and diagnostics:
            # A failed discovery scan must not erase the last complete
            # published collection/source set.  The next reload retries.
            for opened_batch in new_batches:
                try:
                    opened_batch.close()
                except Exception:
                    pass
            for message in diagnostics:
                logger.warning("zimantic: collection discovery: %s", message)
            sources = self._publish_runtime_sources()
            if deferred_entries:
                self._start_background_discovery(deferred_entries)
            return sources
        old_runtimes = previous_runtimes
        old_batches = previous_batches
        with self._state_lock:
            self.runtimes = new_runtimes
            self.collections = new_collections
            self._discovery_batches = new_batches
            self._aggregate_revision = getattr(self, "_aggregate_revision", 0) + 1
            load_faiss = self.semantic and not self._defer_faiss
        if load_faiss:
            for key, runtime in new_runtimes.items():
                if runtime is not previous_runtimes.get(key):
                    self._start_runtime_faiss(runtime)
        sources = self._publish_runtime_sources()
        kept_runtime_ids = {id(runtime) for runtime in new_runtimes.values()}
        kept_batch_ids = {id(batch) for batch in new_batches}
        for runtime in old_runtimes.values():
            if id(runtime) in kept_runtime_ids:
                continue
            try:
                runtime.close()
            except Exception:
                pass
        for batch in old_batches:
            if id(batch) in kept_batch_ids:
                continue
            try:
                batch.close()
            except Exception:
                pass
        if diagnostics:
            for message in diagnostics:
                logger.warning("zimantic: collection discovery: %s", message)
        if deferred_entries:
            self._start_background_discovery(deferred_entries)
        return sources

    def _refresh_open_scope(self, *, wait: bool = False) -> list[dict[str, Any]]:
        """Publish only the archives named by an ``open`` process scope."""
        from .providers.zim.provider import open_zim_scope_batch

        ctx = ProviderContext(
            config=self.runtime_config,
            index_dir=Path(self.cfg.get("index_dir") or "."),
            web_base_path=self.web_base_path,
            logger=logger,
            archive_factory=getattr(self, "archive_factory", None),
            persist_catalog=False,
            on_sources_changed=self._publish_runtime_sources,
            open_scope=self.open_scope,
        )
        batch = None
        new_runtimes: dict[str, CollectionRuntime] = {}
        new_collections: dict[str, CollectionInfo] = {}
        try:
            batch = open_zim_scope_batch(self.open_scope, ctx, self.cfg)
            for collection in batch.collections:
                runtime = batch.open(collection)
                self._bind_runtime(runtime)
                new_runtimes[collection.key] = runtime
                new_collections[collection.key] = collection
        except Exception as error:
            if batch is not None:
                try:
                    batch.close()
                except Exception:
                    pass
            for runtime in new_runtimes.values():
                try:
                    runtime.close()
                except Exception:
                    pass
            logger.warning(
                "zimantic: open scope discovery failed (%s)",
                self.public_error(error),
            )
            logger.debug("open scope discovery error: %s", error)
            return self.source_dicts()

        with self._state_lock:
            old_runtimes = dict(self.runtimes)
            old_batches = list(getattr(self, "_discovery_batches", []))
            self.runtimes = new_runtimes
            self.collections = new_collections
            self._discovery_batches = [batch] if batch is not None else []
            self._aggregate_revision = getattr(self, "_aggregate_revision", 0) + 1
            load_faiss = self.semantic and not self._defer_faiss
        if load_faiss:
            for runtime in new_runtimes.values():
                self._start_runtime_faiss(runtime)
        sources = self._publish_runtime_sources()
        for runtime in old_runtimes.values():
            try:
                runtime.close()
            except Exception:
                pass
        for old_batch in old_batches:
            try:
                old_batch.close()
            except Exception:
                pass
        return sources

    def _start_background_discovery(
        self, entries: tuple[ProviderConfig, ...]
    ) -> None:
        """Discover non-ZIM providers off the ZIM startup path.

        Kiwix already refreshes its catalog in the background.  Files and
        Kolibri discovery can also touch a slow filesystem, so a mixed library
        starts serving local ZIM results first and publishes the remaining
        collections when their bounded discovery completes.
        """
        from .discovery import discover_collections

        revision = getattr(self, "_aggregate_revision", 0)

        def _run() -> None:
            result = None
            try:
                result = discover_collections(
                    self.cfg,
                    entries=entries,
                    wait=True,
                    persist_catalog=bool(self.cfg.get("index_dir")),
                    on_sources_changed=self._publish_runtime_sources,
                )
                for message in result.diagnostics:
                    logger.warning("zimantic: collection discovery: %s", message)
                with self._state_lock:
                    if getattr(self, "_closed", False) or (
                        getattr(self, "_aggregate_revision", 0) != revision
                    ):
                        stale = True
                    else:
                        stale = False
                        load_faiss = self.semantic and not self._defer_faiss
                        for discovered in result.collections:
                            self._bind_runtime(discovered.runtime)
                            self.runtimes[discovered.collection.key] = (
                                discovered.runtime
                            )
                            self.collections[discovered.collection.key] = (
                                discovered.collection
                            )
                            if discovered.batch not in self._discovery_batches:
                                self._discovery_batches.append(discovered.batch)
                        self._aggregate_revision = revision + 1
                if stale:
                    result.close()
                    return
                if load_faiss:
                    for discovered in result.collections:
                        self._start_runtime_faiss(discovered.runtime)
                self._publish_runtime_sources()
            except Exception as error:
                logger.warning(
                    "zimantic: background provider discovery failed: %s",
                    self.public_error(error),
                )
                logger.debug("background provider discovery error: %s", error)
                if result is not None:
                    result.close()

        thread = threading.Thread(
            target=_run, name="zimantic-discovery", daemon=True
        )
        threads = getattr(self, "_background_discovery_threads", None)
        if threads is None:
            threads = []
            self._background_discovery_threads = threads
        threads.append(thread)
        thread.start()

    def _publish_runtime_sources(self) -> list[dict[str, Any]]:
        with self._state_lock:
            sources: dict[str, SourceInfo] = {}
            library: dict[str, SourceInfo] = {}
            for runtime in self.runtimes.values():
                for source in runtime.sources:
                    sources[source.key] = source
                for source in runtime.library_sources():
                    library[source.key] = source
            if self.open_scope is not None:
                position = {
                    key: index
                    for index, key in enumerate(self.open_scope.keys)
                }
                ordered = dict(
                    sorted(
                        sources.items(),
                        key=lambda pair: (
                            position.get(pair[0], len(position)),
                            _source_title(pair[1]).casefold(),
                            pair[1].key,
                        ),
                    )
                )
            else:
                ordered = dict(
                    sorted(
                        sources.items(),
                        key=lambda pair: (
                            _source_title(pair[1]).casefold(),
                            pair[1].key,
                        ),
                    )
                )
            old_sources = self.sources
            metadata_tokens = {
                key: runtime.result_metadata_token
                for key, runtime in self.runtimes.items()
                if isinstance(runtime, IndexedRuntime)
                and runtime.result_metadata_token is not None
            }
            changed = (
                ordered != old_sources
                or metadata_tokens != getattr(self, "_result_metadata_tokens", {})
            )
            self._result_metadata_tokens = metadata_tokens
            old_snapshots = self._collection_snapshots
            snapshots = {
                key: runtime.snapshot for key, runtime in self.runtimes.items()
            }
            snapshot_changed = snapshots != old_snapshots
            self.sources = ordered
            self._library_source_index = library
            self._collection_snapshots = snapshots
            if changed:
                self._cache_generation += 1
            if changed or snapshot_changed:
                self._aggregate_revision = (
                    getattr(self, "_aggregate_revision", 0) + 1
                )
            result = self.source_dicts()
        if changed:
            self._remote_limit_warned = False
            self.cache.clear()
            preview_cache = self.preview_cache
            if preview_cache is not None:
                preview_cache.clear()
        return result

    @staticmethod
    def _collection_sort_key(record: dict[str, Any]) -> tuple[str, str]:
        title = record.get("title") or record.get("label") or record.get("key") or ""
        return (str(title).casefold(), str(record.get("key") or ""))

    def collection_dicts(self) -> list[dict[str, Any]]:
        """Serialize the current collection snapshots in display order."""
        with self._state_lock:
            snapshots = dict(self._collection_snapshots)
        records = [snapshot.to_dict() for snapshot in snapshots.values()]
        for record in records:
            record["error"] = self.public_text(record.get("error"))
        return sorted(records, key=self._collection_sort_key)

    def snapshot_token(self) -> tuple[str, int]:
        """Process-instance identity plus aggregate publication revision."""
        with self._state_lock:
            return self._process_token, self._aggregate_revision

    def collection_version(self) -> tuple[str, int]:
        """Token invalidated by collection membership, state, or metadata."""
        return self.snapshot_token()

    def source_dicts(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return [
                source.to_dict(include_metadata=True)
                for source in self.sources.values()
            ]

    def listing_snapshot(self) -> dict[str, Any]:
        """Capture listing records and the aggregate token from one revision.

        The returned lists are detached copies. Filtering, sorting, and slicing
        happen on this snapshot, so a page cannot mix records from two
        different discovery revisions.
        """
        for _attempt in range(2):
            with self._state_lock:
                before = self._aggregate_revision
            library = self._library_records()
            with self._state_lock:
                if self._aggregate_revision != before:
                    continue
                return {
                    "sources": [
                        source.to_dict(include_metadata=True)
                        for source in self.sources.values()
                    ],
                    "library": library,
                    "collections": self.collection_dicts(),
                    "token": (self._process_token, self._aggregate_revision),
                }
        with self._state_lock:
            return {
                "sources": [
                    source.to_dict(include_metadata=True)
                    for source in self.sources.values()
                ],
                "library": library,
                "collections": self.collection_dicts(),
                "token": (self._process_token, self._aggregate_revision),
            }

    def _library_records(self) -> list[dict[str, Any]]:
        """Return a sorted, detached library record list.

        ZIM runtimes include readable-but-unsearchable and unreadable archives
        that are not in the searchable source set.  Discovery failures remain
        collection-level diagnostics, not per-source state. This reads only
        published metadata; discovery runs at startup or on explicit reload.
        """
        with self._state_lock:
            runtimes = list(self.runtimes.values())
            fallback = list(self.sources.values())
        library: dict[str, SourceInfo] = {}
        for runtime in runtimes:
            for source in runtime.library_sources():
                library[source.key] = source
        if not library:
            for source in fallback:
                if source.provider in {"zim", "files", "kolibri"}:
                    library[source.key] = source
        if self.open_scope is not None:
            position = {
                key: index for index, key in enumerate(self.open_scope.keys)
            }
            return [
                source.to_dict(include_metadata=True)
                for source in sorted(
                    library.values(),
                    key=lambda item: (
                        position.get(item.key, len(position)),
                        _source_title(item).casefold(),
                        item.key,
                    ),
                )
            ]
        return [
            source.to_dict(include_metadata=True)
            for source in sorted(
                library.values(),
                key=lambda item: (_source_title(item).casefold(), item.key),
            )
        ]

    def source_order(self) -> dict[str, int] | None:
        """Explicit open-process source order, or None for a normal server."""
        scope = self.open_scope
        if scope is None:
            return None
        return {key: index for index, key in enumerate(scope.keys)}

    def source_neighbors(
        self, key: str
    ) -> tuple[SourceNavigation, SourceNavigation] | None:
        """Previous and next scoped sources, wrapping at both ends."""
        scope = self.open_scope
        if scope is None or len(scope.keys) < 2:
            return None
        keys = list(scope.keys)
        try:
            position = keys.index(str(key))
        except ValueError:
            return None

        def navigation(target: str) -> SourceNavigation:
            with self._state_lock:
                source = self.sources.get(target) or self._library_source_index.get(
                    target
                )
            return SourceNavigation(
                key=target,
                title=_source_title(source) if source is not None else target,
            )

        return (
            navigation(keys[position - 1]),
            navigation(keys[(position + 1) % len(keys)]),
        )

    def source_homepage_path(self, key: str) -> str | None:
        """A navigable entry path for one scoped source, if one exists."""
        source = self.resolve_source(str(key))
        if source is not None and source.homepage:
            return source.homepage
        try:
            store = self.content_store(str(key))
        except (
            ArticleNotFoundError,
            ArticleSourceNotFoundError,
            ArticleTooLargeError,
            ArticleUnavailableError,
        ):
            store = None
        if store is not None:
            try:
                path = store.main_path()
            except Exception:
                path = None
            finally:
                store.close()
            if path:
                return path
        try:
            return self.random_article_path(str(key))
        except (
            ArticleNotFoundError,
            ArticleSourceNotFoundError,
            ArticleTooLargeError,
            ArticleUnavailableError,
            ValueError,
        ):
            return None

    def library_dicts(self) -> list[dict[str, Any]]:
        """Return metadata for every document-library source."""
        return self._library_records()

    def library_version(self) -> tuple[Any, ...]:
        """Return a cheap token that changes when the library contents change."""
        with self._state_lock:
            runtime_versions = tuple(
                (key, runtime.snapshot_version)
                for key, runtime in sorted(self.runtimes.items())
            )
            generation = self._cache_generation
        return (generation, runtime_versions)

    def source_version(self) -> tuple[int, ...]:
        """Token that changes whenever the source set or its metadata changes."""
        with self._state_lock:
            return (self._cache_generation,)

    def close(self) -> None:
        """Release the search thread pool and every provider runtime."""
        with self._state_lock:
            self._closed = True
        for thread in list(getattr(self, "_background_discovery_threads", [])):
            try:
                thread.join()
            except RuntimeError:
                continue
        pool = self.fulltext_pool
        self.fulltext_pool = None
        if pool is not None:
            pool.close()
        executor = getattr(self, "_executor", None)
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        with self._state_lock:
            runtimes = list(getattr(self, "runtimes", {}).values())
            batches = list(getattr(self, "_discovery_batches", []))
            self.runtimes = {}
            self.collections = {}
            self._collection_snapshots = {}
            self._discovery_batches = []
            self._library_source_index = {}
            self.sources = {}
        for runtime in runtimes:
            try:
                runtime.close()
            except Exception:
                pass
        for batch in batches:
            try:
                batch.close()
            except Exception:
                pass

    def source_keys(self) -> list[str]:
        """Canonical keys of the currently searchable sources."""
        with self._state_lock:
            return sorted(self.sources)

    def resolve_content_source(self, identifier: str) -> SourceInfo | None:
        """Resolve an exact canonical source key for content serving."""
        return self.resolve_source(identifier)

    def content_store(self, identifier: str) -> ContentStore:
        """Open the canonical content store for one source key.

        The caller owns the returned store and must close it.  Missing sources,
        missing stores, and opening failures stay distinct so the HTTP layer can
        map them to the documented status codes.
        """
        source = self.resolve_content_source(identifier)
        if source is None:
            raise ArticleSourceNotFoundError("source not found")
        with self._state_lock:
            runtime = self.runtimes.get(source.collection_key)
        if runtime is None:
            raise ArticleUnavailableError("source is unavailable")
        store = runtime.content_store(source)
        if store is None:
            raise ArticleNotFoundError("source has no content store")
        return store

    def resolve_content(
        self, identifier: str, path: str | None = None
    ) -> ContentEntry:
        """Resolve one source path to its final entry and metadata."""
        store = self.content_store(identifier)
        try:
            return store.resolve(path)
        finally:
            store.close()

    def read_article(
        self,
        source_key: str,
        path: str | None = None,
        head_only: bool = False,
    ) -> ArticleData:
        """Read one source entry through its runtime content store."""
        store = self.content_store(source_key)
        try:
            return store.read(
                path,
                head_only=head_only,
                max_bytes=self.max_article_bytes,
            )
        finally:
            store.close()

    def read_article_head(
        self, source_key: str, path: str | None = None
    ) -> ArticleData:
        return self.read_article(source_key, path, head_only=True)

    def read_content_range(
        self,
        source_key: str,
        path: str,
        *,
        start: int,
        end: int,
        max_bytes: int | None = None,
    ) -> ContentRange:
        """Read an inclusive byte range of one unchanged representation."""
        limit = (
            self.max_article_bytes
            if max_bytes is None
            else min(int(max_bytes), self.max_article_bytes)
        )
        store = self.content_store(source_key)
        try:
            return store.read_range(
                path,
                start=start,
                end=end,
                max_bytes=limit,
            )
        finally:
            store.close()

    def article_path_exists(self, source_key: str, path: str) -> bool:
        """Whether ``path`` resolves to a source entry."""
        try:
            store = self.content_store(source_key)
        except (
            ArticleSourceNotFoundError,
            ArticleNotFoundError,
            ArticleUnavailableError,
        ):
            return False
        try:
            return store.exists(path)
        except (ArticleNotFoundError, ArticleUnavailableError, ValueError):
            return False
        finally:
            store.close()

    def read_source_icon(self, source_key: str) -> ArticleData:
        """Read the illustration selected for a source."""
        store = self.content_store(source_key)
        try:
            icon = store.icon(self.max_article_bytes)
        finally:
            store.close()
        if icon is None:
            raise ArticleNotFoundError("source icon not found")
        return icon

    def random_local_source(self) -> SourceInfo:
        """Return a random readable local source that has a homepage."""
        local_providers = {"zim", "files", "kolibri"}
        with self._state_lock:
            runtimes = [
                runtime
                for runtime in self.runtimes.values()
                if isinstance(runtime, IndexedRuntime)
            ]
            fallback_by_key = {
                source.key: source
                for source in (
                    *self.sources.values(),
                    *self._library_source_index.values(),
                )
                if source.provider in local_providers and source.homepage
            }
            fallback_sources = list(fallback_by_key.values())
        candidates: list[SourceInfo] = []
        for runtime in runtimes:
            try:
                candidates.append(runtime.random_source())
            except ArticleNotFoundError:
                continue
        if not candidates:
            candidates = fallback_sources
        if not candidates:
            raise ArticleNotFoundError("no local source has a homepage")
        return secrets.choice(candidates)

    def random_article_path(self, source_key: str) -> str:
        """Return a random article path without changing search state."""
        store = self.content_store(source_key)
        try:
            path = store.random_path()
        except (KeyError, RuntimeError, TypeError, ValueError, IndexError) as error:
            raise ArticleUnavailableError(
                "random article selection is unavailable"
            ) from error
        finally:
            store.close()
        if not path:
            raise ArticleNotFoundError("source has no available articles")
        return normalize_article_path(path)

    def health(self) -> dict[str, Any]:
        """Return read-only index, archive, and capability diagnostics."""
        now = time.monotonic()
        with self._state_lock:
            source_count = len(self.sources)
            embedder_loaded = self.embedder is not None
            generation = self._cache_generation
            aggregate_revision = self._aggregate_revision
            deferred_faiss = self._defer_faiss
            runtimes = list(self.runtimes.values())
            snapshots = dict(self._collection_snapshots)
            searchable_by_collection = Counter(
                source.collection_key
                for source in self.sources.values()
                if source.searchable
            )
            cached = getattr(self, "_health_cache", None)
        if (
            cached is not None
            and now - cached[0] < HEALTH_CACHE_TTL_SECONDS
            and cached[1:5]
            == (generation, embedder_loaded, deferred_faiss, aggregate_revision)
        ):
            report = dict(cached[5])
            report["cache"] = self.cache.stats()
            preview_cache = self.preview_cache
            report["preview_cache"] = (
                preview_cache.stats() if preview_cache is not None else None
            )
            return report

        reports: list[dict[str, Any]] = []
        for runtime in runtimes:
            if not isinstance(runtime, IndexedRuntime):
                continue
            try:
                reports.extend(runtime.health())
            except Exception as error:
                logger.warning(
                    "zimantic: health report failed for %s (%s)",
                    runtime.collection.label,
                    self.public_error(error),
                )
                logger.debug("health report error: %s", error)
        status = "ok"
        if not reports or any(report["status"] != "ok" for report in reports):
            status = "degraded"
        collections = {}
        for key, snapshot in sorted(snapshots.items()):
            collections[key] = {
                "provider": snapshot.collection.provider,
                "sources": snapshot.source_count,
                "searchable": searchable_by_collection.get(key, 0),
                "discovery_state": snapshot.discovery_state,
                "error": self.public_text(snapshot.error),
                "snapshot_version": snapshot.snapshot_version,
                "last_refresh": snapshot.last_refresh,
                "collapsed": snapshot.collapsed,
                "index_count": snapshot.index_count,
                "index_states": list(snapshot.index_states),
                "indexed_count": snapshot.indexed_count,
                "truncated": snapshot.truncated,
                "body_truncated_count": snapshot.body_truncated_count,
            }
        report = {
            "status": status,
            "indexes": reports,
            "sources": source_count,
            "collections": collections,
            "embedding": {
                "enabled": self.semantic,
                "loaded": embedder_loaded,
            },
            "limits": {
                "search_timeout_seconds": getattr(
                    self, "search_timeout_seconds", 30.0
                ),
                "source_timeout": getattr(self, "source_timeout", 12),
                "slot_wait_seconds": getattr(self, "slot_wait_seconds", 10.0),
                "max_explicit_collection_sources": getattr(
                    self, "max_explicit_collection_sources", 500
                ),
                "max_body_text_bytes": getattr(self, "cfg", {}).get(
                    "max_body_text_bytes", 1_048_576
                ),
                "max_derived_cache_bytes_per_source": getattr(
                    self, "cfg", {}
                ).get("max_derived_cache_bytes_per_source", 536_870_912),
            },
            "cache": self.cache.stats(),
            "preview_cache": (
                self.preview_cache.stats()
                if self.preview_cache is not None
                else None
            ),
        }
        with self._state_lock:
            self._health_cache = (
                time.monotonic(),
                generation,
                embedder_loaded,
                deferred_faiss,
                aggregate_revision,
                report,
            )
        return report

    def health_version(self) -> tuple[int, bool, bool, int, int]:
        """Return a short-lived version token for the health response cache."""
        with self._state_lock:
            generation = self._cache_generation
            embedder_loaded = self.embedder is not None
            deferred_faiss = self._defer_faiss
            aggregate_revision = self._aggregate_revision
        bucket = int(time.monotonic() / HEALTH_CACHE_TTL_SECONDS)
        return generation, embedder_loaded, deferred_faiss, aggregate_revision, bucket

    def _bounded_available_sources(
        self, available: list[SourceInfo]
    ) -> list[SourceInfo]:
        """Apply each runtime's per-search source budget.

        The provider owns the policy: a runtime returns ``None`` when it has no
        special fan-out cap and a non-negative integer when unscoped searches
        must be bounded.  Each collection is budgeted independently so one
        large remote catalog cannot consume another server's allowance.
        """
        by_collection: dict[str, list[SourceInfo]] = {}
        for source in available:
            by_collection.setdefault(source.collection_key, []).append(source)
        with self._state_lock:
            runtimes = dict(self.runtimes)
        keep: set[str] = set()
        for collection_key, collection_sources in by_collection.items():
            runtime = runtimes.get(collection_key)
            limit = None
            if runtime is not None:
                limit = runtime.max_sources_per_search()
            if limit is None or len(collection_sources) <= limit:
                keep.update(source.key for source in collection_sources)
                continue
            keep.update(source.key for source in collection_sources[:limit])
            if not getattr(self, "_remote_limit_warned", False):
                logger.warning(
                    "zimantic: %d remote Kiwix sources available in %s; "
                    "searching the first %d (configure max_remote_sources to change this)",
                    len(collection_sources),
                    collection_key,
                    limit,
                )
                self._remote_limit_warned = True
        return [source for source in available if source.key in keep]

    @staticmethod
    def _source_selection(source: str | list[str] | tuple[str, ...] | None) -> list[str] | None:
        if source is None:
            return None
        values = [source] if isinstance(source, str) else list(source)
        return [str(value) for value in values if str(value)]

    def _search_scope(
        self,
        source: str | list[str] | tuple[str, ...] | None,
        collection: str | None = None,
    ) -> _ResolvedScope:
        """Resolve an explicit source/collection selection or the unscoped scope.

        An explicit selection always defines the resolved search scope; it is
        never promoted to a broader search. A known but unsearchable source
        resolves to an empty scope. Unknown source or collection keys are a
        client error, so a stale bookmark cannot silently search everything.
        """
        with self._state_lock:
            available = [
                source_info
                for source_info in self.sources.values()
                if source_info.searchable
            ]
            known_sources = dict(self.sources)
            known_library = dict(self._library_source_index)
            collections = dict(self.collections)
            generation = self._cache_generation

        selected_keys = self._source_selection(source)
        if selected_keys is not None:
            unknown = [
                key
                for key in selected_keys
                if key not in known_sources and key not in known_library
            ]
            if unknown:
                raise SearchQueryError(
                    "unknown source: " + ", ".join(sorted(set(unknown)))
                )
        if collection is not None and collection not in collections:
            raise SearchQueryError(f"unknown collection: {collection}")

        if source is None and collection is None:
            bounded = self._bounded_available_sources(available)
            return _ResolvedScope(
                sources=bounded,
                selective=False,
                cache_selection=None,
                requested=len(available),
                source_cap=len(bounded) < len(available),
                generation=generation,
            )

        candidates = available
        if collection is not None:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.collection_key == collection
            ]
        if selected_keys is not None:
            wanted = set(selected_keys)
            candidates = [
                candidate for candidate in candidates if candidate.key in wanted
            ]
        requested = len(candidates)
        capped = False
        collection_info = collections.get(collection) if collection is not None else None
        if (
            collection_info is not None
            and collection_info.provider == "kiwix"
        ):
            cap = max(1, int(getattr(self, "max_explicit_collection_sources", 500)))
            if len(candidates) > cap:
                candidates = candidates[:cap]
                capped = True
        return _ResolvedScope(
            sources=candidates,
            selective=True,
            cache_selection=tuple(
                sorted(candidate.key for candidate in candidates)
            ),
            requested=requested,
            source_cap=capped,
            generation=generation,
        )

    def validate_search_scope(
        self,
        source: str | list[str] | None = None,
        collection: str | None = None,
    ) -> None:
        """Raise ``SearchQueryError`` for unknown selectors before streaming."""
        self._search_scope(source, collection)

    @staticmethod
    def _validate_query(query: str, *, allow_empty: bool = False) -> str:
        if not isinstance(query, str):
            raise SearchQueryError("query must be text")
        query = query.strip()
        if not query and not allow_empty:
            raise SearchQueryError("query must not be empty")
        if len(query) > MAX_QUERY_LENGTH:
            raise SearchQueryError(f"query must be at most {MAX_QUERY_LENGTH} characters")
        return query

    def _validate_search(
        self, query: str, sort: str | None = None, seed: int = 0
    ) -> tuple[str, str]:
        query = self._validate_query(query, allow_empty=True)
        sort = sort or ("relevance" if query else "diverse")
        if not query and sort == "relevance":
            sort = "diverse"
        if sort != "relevance":
            self._validate_browse(sort, seed)
        return query, sort

    def _validate_browse(self, mode: str, seed: int = 0) -> None:
        if mode not in BROWSE_MODES:
            raise SearchQueryError("unknown browse mode")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
            raise SearchQueryError("browse seed must be an unsigned 32-bit integer")
        if mode in SEMANTIC_BROWSE_MODES and not self.semantic:
            raise SearchQueryError(
                "semantic browsing is unavailable when semantic search is disabled"
            )

    def _cache_key(
        self,
        query: str,
        selection: tuple[str, ...] | None,
        generation: int | None = None,
    ) -> tuple:
        """Key by query, expanded source keys, and publication generation.

        The ranked pool is independent of page size, offset and debug flag,
        so none of those belong in the key: every page of a query
        reuses one cached pool instead of fragmenting the cache per `limit`.
        A ``None`` selection is the shared unscoped pool; an empty tuple is a
        deliberately empty source scope. Including the generation invalidates
        an explicit scope when membership or an index changes without changing
        its keys.
        """
        if generation is None:
            with self._state_lock:
                generation = self._cache_generation
        normalized = re.sub(r"\s+", " ", query).strip().casefold()
        normalized_selection = (
            tuple(sorted(selection)) if selection is not None else None
        )
        with self._state_lock:
            sources = getattr(self, "sources", {})
            policies = tuple(
                (key, settings_key(self.cfg, sources[key]))
                for key in sorted(sources)
                if selection is None or key in selection
            )
        return (generation, normalized, normalized_selection, policies)

    def _page_size(self, limit: int | None) -> int:
        default = self.page_size
        ceiling = self.max_results
        if limit is None:
            return max(1, min(int(default), ceiling))
        try:
            return max(1, min(int(limit), ceiling))
        except (TypeError, ValueError):
            return default

    def _pool_target(self) -> int:
        """How many ranked candidates to retain and serve across all pages."""
        return max(
            self.candidate_count,
            self.max_results,
        )

    def _source_limit(
        self, total_sources: int, source: SourceInfo | None = None
    ) -> int:
        """Candidates to request from each source before fusion.

        The ranked pool is global, so a source only needs enough candidates to
        fill its share of that pool. Asking every one of hundreds of sources for
        the full pool size multiplies retrieval, fusion and ranking work for
        candidates that cannot survive the global cut. At least
        ``candidate_count`` is kept as a recall floor, so with many sources the
        floor can be larger than the mathematical share; a single source returns
        the whole pool.
        """
        pool_target = self._pool_target()
        if total_sources <= 1:
            return pool_target
        share = math.ceil(pool_target / total_sources)
        candidates = resolve_settings(self.cfg, source).get(
            "candidate_count", self.candidate_count
        )
        return min(pool_target, max(candidates, share))

    def _cache_quotas(self, sources) -> dict[str, tuple[int, int]]:
        cache = getattr(self, "cache", None)
        return {
            source.key: (
                settings.get(
                    "cache_size", getattr(cache, "maxsize", DEFAULT_CACHE_SIZE)
                ),
                settings.get(
                    "cache_bytes", getattr(cache, "max_bytes", DEFAULT_CACHE_BYTES)
                ),
            )
            for source in sources
            for settings in [resolve_settings(self.cfg, source)]
        }

    @staticmethod
    def _source_counts(pool: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for doc in pool:
            key = doc.get("source_key", "")
            counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _public(doc: dict[str, Any], debug: bool) -> dict[str, Any]:
        """A response copy. Ranking metadata is only shipped in debug mode.

        Private bookkeeping keys (those starting with ``_``) never leave the
        server, even in debug mode.
        """
        if debug:
            return {
                key: value for key, value in doc.items() if not key.startswith("_")
            }
        return {
            key: value
            for key, value in doc.items()
            if not key.startswith("_")
            and key not in ("explain", "ranking", "score")
        }

    @staticmethod
    def _matched_by(pool: list[dict[str, Any]]) -> dict[str, int]:
        return matched_by(pool)

    def _page_state(
        self,
        pool: list[dict[str, Any]],
        offset: int,
        page_size: int,
        debug: bool,
    ) -> dict[str, Any]:
        """Slice one page out of the ranked pool."""
        total = len(pool)
        # Clamp an out-of-range offset to the last real page here, where the
        # total is known. Callers can then show real results and report the
        # effective offset instead of fetching an empty page and correcting it
        # client-side.
        if total:
            effective_offset = min(offset, ((total - 1) // page_size) * page_size)
        else:
            effective_offset = 0
        page = pool[effective_offset:effective_offset + page_size]
        return {
            "results": [self._public(doc, debug) for doc in page],
            "total": total,
            "has_more": effective_offset + page_size < total,
            "offset": effective_offset,
            # The effective page is server-authoritative: the client may have
            # requested an out-of-range offset that was clamped above.
            "page": effective_offset // page_size,
            "page_count": max(1, math.ceil(total / page_size)),
            "counts": self._source_counts(pool),
        }

    @staticmethod
    def _event(
        event_type: str,
        state: dict[str, Any],
        completed: int,
        total_sources: int,
        offset: int,
        limit: int,
        matched_by: dict[str, int] | None = None,
        include_results: bool = True,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One streamed snapshot/done event shaped from a page state.

        ``include_results`` is False for an intermediate snapshot whose page is
        unchanged from the previous one: the client keeps the page it already
        has, so re-sending (and re-serializing) the result list would be pure
        duplication. The final ``done`` event always includes it.
        """
        event: dict[str, Any] = {
            "type": event_type,
            "results": state["results"] if include_results else None,
            "completed": completed,
            "total_sources": total_sources,
            "total": state["total"],
            "has_more": state["has_more"],
            "offset": state.get("offset", offset),
            "limit": limit,
            "page": state.get("page", state.get("offset", offset) // limit),
            "page_count": state.get(
                "page_count",
                max(1, math.ceil(state["total"] / limit)),
            ),
            "counts": state["counts"],
        }
        if matched_by is not None:
            event["matched_by"] = matched_by
        if scope is not None:
            event["scope"] = scope
        return event

    @staticmethod
    def _page_signature(state: dict[str, Any]) -> tuple[str, ...]:
        """Identity of the displayed page, used to elide unchanged result lists.

        Intermediate snapshots never change a result's content (leads are only
        hydrated for the final ``done``), so the ordered ids are enough to tell
        whether the page moved.
        """
        return tuple(doc.get("id", "") for doc in state["results"])

    @staticmethod
    def _unpack_cache(cached: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return list(cached.get("pool", [])), list(cached.get("sources", []))

    def _query_vector(self, sources: list[SourceInfo], query: str):
        """Embed the query only when a selected source actually has vectors.

        A failed embedding degrades to title + full-text rather than failing.
        """
        embedder = self.embedder
        if embedder is None or not self.semantic:
            return None
        with self._state_lock:
            runtimes = dict(self.runtimes)
        wants_semantic = False
        for source in sources:
            runtime = runtimes.get(source.collection_key)
            if not isinstance(runtime, IndexedRuntime):
                continue
            index = runtime.indexes.get(source.key)
            if index is not None and index.semantic:
                wants_semantic = True
                break
        if not wants_semantic:
            return None
        try:
            return embedder.embed([f"query: {query}"])
        except Exception as error:
            logger.warning(
                "zimantic: query embedding failed (%s); falling back to title "
                "and full-text (lexical) search",
                self.public_error(error),
            )
            logger.debug("query embedding failed: %s", error)
            return None

    def suggest(
        self,
        query: str,
        source: str | list[str] | None = None,
        limit: int = 8,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return title-completion candidates scoped to the given sources.

        Autocomplete must answer within a typing pause, so it deliberately
        stays title-only: no query embedding, no libzim full-text search and no
        article reads. Sources without a Zimantic title index simply contribute
        nothing; the caller can still fall back to a full search.
        """
        query = self._validate_query(query)
        try:
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            limit = 8
        with self._state_lock:
            runtimes = dict(self.runtimes)
        sources = self._search_scope(source, collection).sources
        suggestions: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for source_info in sources:
            remaining = limit - len(suggestions)
            if remaining <= 0:
                break
            runtime = runtimes.get(source_info.collection_key)
            if runtime is None:
                continue
            try:
                items = runtime.suggest(source_info, query, remaining)
            except Exception as error:
                logger.warning(
                    "zimantic: title suggestions unavailable for %s: %s",
                    source_info.key,
                    self.public_error(error),
                )
                logger.debug("suggest failed for %s: %s", source_info.key, error)
                continue
            for doc in items:
                doc_id = str(doc.get("id") or "")
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
                suggestions.append(doc)
        return suggestions[:limit]

    def search_page(
        self,
        query: str,
        source: str | list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        collection: str | None = None,
        debug: bool = False,
        *,
        browse: str | None = None,
        seed: int = 0,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """Return one page plus totals, using the streaming coordinator."""
        final: dict[str, Any] = {}
        for event in self.stream_search(
            query,
            source,
            limit,
            offset,
            collection,
            debug,
            browse=browse,
            seed=seed,
            sort=sort,
        ):
            if event["type"] == "error":
                if event.get("busy"):
                    raise SearchBusyError(event["error"])
                raise SearchQueryError(event["error"])
            if event["type"] == "done":
                final = event
        return {
            "results": final.get("results", []),
            "total": final.get("total", 0),
            "has_more": final.get("has_more", False),
            "offset": final.get("offset", max(0, int(offset))),
            "page": final.get("page", 0),
            "page_count": final.get("page_count", 1),
            "counts": final.get("counts", {}),
            "matched_by": final.get("matched_by"),
            "scope": final.get(
                "scope",
                {
                    "status": "complete",
                    "requested": 0,
                    "scheduled": 0,
                    "completed": 0,
                    "limit_reason": None,
                },
            ),
        }

    def _cached_events(
        self,
        cached: Any,
        query: str,
        sources: list[SourceInfo],
        offset: int,
        page_size: int,
        debug: bool,
        selective: bool = False,
        all_sources: bool = True,
    ) -> Iterator[dict[str, Any]]:
        """Replay a cached pool as a finished stream (progress + one page)."""
        pool, cached_sources = self._unpack_cache(cached)
        state = self._page_state(pool, offset, page_size, debug)
        self._hydrate_results(state["results"])
        total_sources = len(sources)
        yield {
            "type": "started",
            "query": query,
            "sources": [src.to_dict() for src in sources],
            "total_sources": total_sources,
            "selective": selective,
            "all_sources": all_sources,
            "offset": offset,
            "limit": page_size,
        }
        for completed, source_event in enumerate(cached_sources, 1):
            yield {
                "type": "source",
                **source_event,
                "completed": completed,
                "total_sources": total_sources,
            }
        matched_by = self._matched_by(pool) if debug else None
        scope = cached.get("scope") if isinstance(cached, dict) else None
        if scope is None:
            scope = {
                "status": "complete",
                "requested": total_sources,
                "scheduled": total_sources,
                "completed": total_sources,
                "limit_reason": None,
            }
        yield self._event(
            "done",
            state,
            total_sources,
            total_sources,
            offset,
            page_size,
            matched_by,
            scope=scope,
        )

    def _finish_inflight(self, key: Any, pending: Future, value: Any) -> None:
        with self._inflight_lock:
            if self._inflight.get(key) is pending:
                del self._inflight[key]
        if not pending.done():
            pending.set_result(value)

    def _cached_for_generation(self, key: Any, generation: int) -> Any | None:
        cached = self.cache.get(key)
        if (
            cached is not None
            and isinstance(cached, dict)
            and cached.get("generation") == generation
        ):
            return cached
        return None

    def _claim_search(
        self,
        cache_key: Any,
        inflight_key: Any,
        generation: int,
    ) -> tuple[Any | None, Future | None]:
        """Return a cached result or claim ownership of an in-flight query."""
        deadline = time.monotonic() + SEARCH_CLAIM_WAIT_SECONDS
        while True:
            cached = self._cached_for_generation(cache_key, generation)
            if cached is not None:
                return cached, None
            with self._inflight_lock:
                existing = self._inflight.get(inflight_key)
                if existing is None:
                    pending = Future()
                    self._inflight[inflight_key] = pending
                    return None, pending
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, None
            try:
                cached = existing.result(timeout=remaining)
            except TimeoutError:
                # The owner is still running. Report busy instead of waiting
                # forever; a retry can pick up the result once it is cached.
                return None, None
            except Exception:
                cached = None
            if (
                cached is not None
                and isinstance(cached, dict)
                and cached.get("generation") == generation
            ):
                return cached, None

    def stream_search(
        self,
        query: str,
        source: str | list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        collection: str | None = None,
        debug: bool = False,
        *,
        browse: str | None = None,
        seed: int = 0,
        sort: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield source progress and one page of the ranked pool.

        Ranking runs once to a fixed pool (`max_results`, at least
        `candidate_count`); `limit`/`offset` only slice that pool for the
        response. The pool is cached by query, expanded source keys, and the
        publication generation. An explicit source/collection selection defines
        the whole scope directly; it is never promoted to a broader search.
        Concurrent identical queries share a single computation.
        """
        if browse is None:
            query, sort = self._validate_search(query, sort, seed)
            if not query:
                browse = sort
            if sort != "random":
                seed = 0
        else:
            self._validate_browse(browse, seed)
            if not isinstance(query, str) or query.strip():
                raise SearchQueryError("browsing does not accept a search query")
            query = ""
            if browse != "random":
                seed = 0
        page_size = self._page_size(limit)
        offset = max(0, int(offset))
        scope = self._search_scope(source, collection)
        sources = scope.sources
        if browse is not None:
            with self._state_lock:
                runtimes = dict(self.runtimes)
            sources = [
                src
                for src in sources
                if isinstance(
                    runtime := runtimes.get(src.collection_key), IndexedRuntime
                )
                and (index := runtime.indexes.get(src.key)) is not None
                and (index.db is not None or index.db_path is not None)
                and (
                    browse not in SEMANTIC_BROWSE_MODES
                    or index.faiss_index is not None
                )
            ]
        generation = scope.generation
        selective = scope.selective
        scope_key = scope.cache_selection
        all_sources = scope_key is None
        cache_key = self._cache_key(query, scope_key, generation)
        if browse is not None:
            cache_key = (
                "browse", browse, seed,
                tuple(sorted(src.key for src in sources)), cache_key,
            )
        elif sort != "relevance":
            cache_key = ("sort", sort, seed, cache_key)
        if browse == "diverse" or sort == "diverse":
            with self._state_lock:
                runtimes = dict(self.runtimes)
                semantic_sources = tuple(sorted(
                    src.key for src in sources
                    if self.semantic
                    and isinstance(runtime := runtimes.get(src.collection_key), IndexedRuntime)
                    and (index := runtime.indexes.get(src.key)) is not None
                    and index.faiss_index is not None
                ))
            cache_key = ("diversity", semantic_sources, cache_key)
        inflight_key = (generation, cache_key)
        cached, pending = self._claim_search(cache_key, inflight_key, generation)
        if cached is not None:
            yield from self._cached_events(
                cached,
                query,
                sources,
                offset,
                page_size,
                debug,
                selective,
                all_sources,
            )
            return
        if pending is None:
            yield {
                "type": "error",
                "error": "search is taking too long; try again shortly",
                "busy": True,
            }
            return
        yield from self._run_search_owner(
            query,
            cache_key,
            generation,
            pending,
            sources,
            offset,
            page_size,
            debug,
            selective,
            all_sources,
            len(sources) if browse is not None else scope.requested,
            False if browse is not None else scope.source_cap,
            browse=browse,
            seed=seed,
            sort=sort,
        )

    def _finalize_search(
        self,
        query: str,
        source_results: list[SourceResult],
        offset: int,
        page_size: int,
        debug: bool,
        pool_target: int,
        reported_counts: dict[str, int | None],
        generation: int,
        cache_key: Any,
        scope: dict[str, Any],
        pool: list[dict[str, Any]] | None = None,
        cacheable: bool = True,
        deadline: float | None = None,
        sort: str | None = None,
        seed: int = 0,
        query_vector=None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Rank, enrich, hydrate and cache one finished search.

        Shared by the live owner and the abandoned-stream finalizer so the two
        paths cannot drift; it returns the page state used by the ``done`` event
        and the value stored in the query cache. The live owner passes its last
        ranked pool so the final page does not pay for one more ranking pass.
        """
        if pool is None:
            pool = self._rank_results(source_results, query, pool_target, deadline=deadline)
        pool = self._enrich_pool(source_results, query, pool, pool_target)
        if query and sort and sort != "relevance":
            pool = self._sort_results(pool, sort, seed, deadline, query_vector=query_vector)
        state = self._page_state(pool, offset, page_size, debug)
        self._hydrate_results(state["results"])
        hydrated = {doc["id"]: doc.get("lead", "") for doc in state["results"]}
        for doc in pool:
            if doc["id"] in hydrated:
                doc["lead"] = hydrated[doc["id"]]
        # The cache holds only the deduplicated ranked pool plus compact source
        # outcomes, never a copy of every source's item list. Source counts are
        # the ones the live `source` events reported (a source that finished
        # between rank passes reported none), so a cached replay shows the same
        # per-source numbers as the original stream.
        cache_value = {
            "generation": generation,
            "pool": pool,
            "sources": [
                result.to_event(reported_counts.get(result.source.key))
                for result in source_results
            ],
            "scope": dict(scope),
        }
        with self._state_lock:
            current_generation = self._cache_generation
        if (
            cacheable
            and current_generation == generation
            and (deadline is None or time.monotonic() < deadline)
            and not any(result.status == "cancelled" for result in source_results)
        ):
            self.cache.put(
                cache_key, cache_value,
                quotas=self._cache_quotas(result.source for result in source_results),
            )
        return state, cache_value

    def _run_search_owner(
        self,
        query: str,
        cache_key: Any,
        generation: int,
        pending: Future,
        sources: list[SourceInfo],
        offset: int,
        page_size: int,
        debug: bool,
        selective: bool = False,
        all_sources: bool = True,
        requested: int | None = None,
        source_cap: bool = False,
        *,
        browse: str | None = None,
        seed: int = 0,
        sort: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Run one uncached search and publish progress to waiting callers."""
        total_sources = len(sources)
        inflight_key = (generation, cache_key)
        acquired = False
        futures: list[Future[SourceResult]] = []
        completed: list[SourceResult] = []
        reported_counts: dict[str, int | None] = {}
        cache_value: Any = None
        stop = threading.Event()
        try:
            # Wait briefly for a slot so a burst of retries can be absorbed;
            # only give up (busy) when nothing frees within the budget. Done
            # inside the try so an interrupt still releases the inflight key.
            acquired = self._acquire_search_slot()
            if not acquired:
                yield {
                    "type": "error",
                    "error": "too many searches are running; try again shortly",
                    "busy": True,
                }
                return
            yield {
                "type": "started",
                "query": query,
                "sources": [src.to_dict() for src in sources],
                "total_sources": total_sources,
                "selective": selective,
                "all_sources": all_sources,
                "offset": offset,
                "limit": page_size,
            }
            if not sources:
                empty_scope = {
                    "status": "complete",
                    "requested": 0,
                    "scheduled": 0,
                    "completed": 0,
                    "limit_reason": None,
                }
                cache_value = {
                    "generation": generation,
                    "pool": [],
                    "sources": [],
                    "scope": empty_scope,
                }
                with self._state_lock:
                    current_generation = self._cache_generation
                if current_generation == generation:
                    self.cache.put(cache_key, cache_value)
                # The computation is complete; do not keep an admission slot
                # while the client reads the final event.
                if acquired:
                    self._release_search_slot()
                    acquired = False
                empty_state = {
                    "results": [],
                    "total": 0,
                    "has_more": False,
                    "offset": 0,
                    "page": 0,
                    "page_count": 1,
                    "counts": {},
                }
                yield self._event(
                    "done",
                    empty_state,
                    0,
                    0,
                    offset,
                    page_size,
                    {} if debug else None,
                    scope=empty_scope,
                )
                return

            query_vector = (
                None if browse is not None else self._query_vector(sources, query)
            )

            title_query = _title_query(query)
            fulltext_query = _fulltext_query(query)
            pool_target = self._pool_target()
            max_stream_ranks = max(1, self.max_stream_ranks)
            rank_interval = max(1, math.ceil(total_sources / max_stream_ranks))
            update_interval = self.stream_update_interval
            pool: list[dict[str, Any]] = []
            counts: dict[str, int] = {}
            started_at = time.monotonic()
            deadline = started_at + max(
                1.0, getattr(self, "search_timeout_seconds", 30.0)
            )

            def rank_pool() -> list[dict[str, Any]]:
                ranked = self._rank_results(completed, query, pool_target, deadline=deadline)
                if query and sort and sort != "relevance":
                    ranked = self._sort_results(ranked, sort, seed, deadline, query_vector=query_vector)
                return ranked

            provider_order: list[str] = []
            queues: dict[str, deque[SourceInfo]] = {}
            for src in sources:
                provider = src.provider or ""
                if provider not in queues:
                    queues[provider] = deque()
                    provider_order.append(provider)
                queues[provider].append(src)
            max_workers = max(1, int(getattr(self, "source_workers", 1)))
            executor_workers = getattr(self._executor, "_max_workers", None)
            if isinstance(executor_workers, int) and executor_workers > 0:
                max_workers = min(max_workers, executor_workers)
            active = {provider: 0 for provider in provider_order}
            active_lock = threading.Lock()
            cursor = 0
            future_sources: dict[Future, SourceInfo] = {}
            remaining: set[Future] = set()

            def _release_provider(provider: str) -> None:
                # Runs only when the callable actually exits (or a queued
                # future is cancelled before it ever starts).
                with active_lock:
                    active[provider] = max(0, active[provider] - 1)

            def _submit_available() -> None:
                nonlocal cursor
                while time.monotonic() < deadline:
                    if not any(queues.values()):
                        return
                    competing = (
                        sum(
                            1
                            for provider in provider_order
                            if queues[provider] or active[provider] > 0
                        )
                        > 1
                    )
                    provider_limit = (
                        max(1, max_workers - 1) if competing else max_workers
                    )
                    source = None
                    for _ in range(len(provider_order)):
                        provider = provider_order[cursor]
                        cursor = (cursor + 1) % len(provider_order)
                        if queues[provider] and active[provider] < provider_limit:
                            source = queues[provider].popleft()
                            break
                    if source is None:
                        return
                    with active_lock:
                        if sum(active.values()) >= max_workers:
                            # No free pool slot; put the source back and retry
                            # after a completion releases one.
                            queues[source.provider].appendleft(source)
                            return
                        active[source.provider] += 1
                    try:
                        future = self._executor.submit(
                            self._run_source_task,
                            stop,
                            source,
                            query,
                            title_query,
                            fulltext_query,
                            query_vector,
                            self._source_limit(total_sources, source),
                            deadline,
                            **({"browse": browse, "seed": seed} if browse else {}),
                        )
                    except Exception:
                        with active_lock:
                            active[source.provider] -= 1
                        raise
                    futures.append(future)
                    future_sources[future] = source
                    remaining.add(future)
                    future.add_done_callback(
                        lambda _future, provider=source.provider: _release_provider(
                            provider
                        )
                    )

            _submit_available()
            last_rank_at = started_at
            rank_passes = 0
            unranked = 0
            # Ids of the page last sent to the client, so a snapshot that didn't
            # move the page can omit its (large, unchanged) result list.
            last_page_signature: tuple[str, ...] | None = None
            while remaining or any(queues.values()):
                if time.monotonic() >= deadline:
                    break
                _submit_available()
                timed_out = False
                try:
                    for future in as_completed(
                        set(remaining),
                        timeout=(
                            max(0.0, min(update_interval, deadline - time.monotonic()))
                            if update_interval > 0
                            else max(0.0, deadline - time.monotonic())
                        ),
                    ):
                        remaining.discard(future)
                        src = future_sources[future]
                        try:
                            result = future.result()
                        except Exception as error:
                            result = SourceResult(
                                source=src,
                                error=f"search failed: {self.public_error(error)}",
                                status="error",
                            )
                        completed.append(result)
                        unranked += 1
                        now = time.monotonic()
                        count_due = (
                            len(completed) == 1
                            or len(completed) % rank_interval == 0
                        )
                        final = len(completed) == total_sources
                        timed_due = (
                            update_interval > 0
                            and now - last_rank_at >= update_interval
                        )
                        # Completion-spaced passes stay bounded by
                        # max_stream_ranks. Timed passes are bounded by the
                        # interval itself, so a long query keeps producing
                        # snapshots instead of going silent until the next
                        # completion batch.
                        count_rank = count_due and rank_passes < max_stream_ranks
                        should_rank = final or timed_due or count_rank
                        if should_rank:
                            pool = rank_pool()
                            counts = self._source_counts(pool)
                            # This source is part of the pool that was just
                            # ranked, so its count is current. Between rank
                            # passes a newly finished source is not in `counts`
                            # yet; omit its count instead of reporting a
                            # misleading 0. The snapshot that follows a rank
                            # pass always carries the full, current map.
                            reported_count = counts.get(src.key, 0)
                            if count_rank:
                                rank_passes += 1
                            last_rank_at = now
                            unranked = 0
                        else:
                            reported_count = None
                        reported_counts[src.key] = reported_count
                        yield {
                            "type": "source",
                            **result.to_event(reported_count),
                            "completed": len(completed),
                            "total_sources": total_sources,
                        }
                        if should_rank:
                            state = self._page_state(pool, offset, page_size, debug)
                            signature = self._page_signature(state)
                            include_results = debug or signature != last_page_signature
                            last_page_signature = signature
                            yield self._event(
                                "snapshot",
                                state,
                                len(completed),
                                total_sources,
                                offset,
                                page_size,
                                self._matched_by(pool) if debug else None,
                                include_results=include_results,
                            )
                        _submit_available()
                except TimeoutError:
                    timed_out = True
                if not remaining and not any(queues.values()):
                    break
                if not timed_out:
                    continue
                now = time.monotonic()
                if unranked:
                    # A source finished less than one interval after the last
                    # rank pass. Publish it now instead of waiting for the next
                    # completion, which may be a long way off.
                    pool = rank_pool()
                    last_rank_at = now
                    unranked = 0
                    state = self._page_state(pool, offset, page_size, debug)
                    signature = self._page_signature(state)
                    include_results = debug or signature != last_page_signature
                    last_page_signature = signature
                    yield self._event(
                        "snapshot",
                        state,
                        len(completed),
                        total_sources,
                        offset,
                        page_size,
                        self._matched_by(pool) if debug else None,
                        include_results=include_results,
                    )
                else:
                    # Nothing new to rank: keep the stream (and the status
                    # line) alive while a slow source is still working.
                    yield {
                        "type": "progress",
                        "completed": len(completed),
                        "total_sources": total_sources,
                        "elapsed": round(now - started_at, 1),
                    }

            deadline_hit = (
                bool(remaining)
                or any(queues.values())
                or time.monotonic() >= deadline
            )
            if deadline_hit:
                stop.set()
                for future in remaining:
                    future.cancel()
            scope = {
                "status": "partial" if deadline_hit else "complete",
                "requested": total_sources if requested is None else requested,
                "scheduled": len(futures),
                "completed": len(completed),
                "limit_reason": (
                    "deadline"
                    if deadline_hit
                    else "source_cap"
                    if source_cap
                    else None
                ),
            }
            if scope["limit_reason"] == "source_cap":
                scope["status"] = "partial"
            state, cache_value = self._finalize_search(
                query,
                completed,
                offset,
                page_size,
                debug,
                pool_target,
                reported_counts,
                generation,
                cache_key,
                scope,
                pool=None if unranked or (query and sort and sort != "relevance") else pool,
                cacheable=not deadline_hit,
                deadline=deadline,
                sort=sort,
                seed=seed,
                query_vector=query_vector,
            )
            if time.monotonic() >= deadline and not deadline_hit:
                scope["status"] = "partial"
                scope["limit_reason"] = "deadline"
            # Ranking and hydration are done. Release admission before the
            # final event so a slow or abandoned reader cannot occupy a search
            # slot that another user could use.
            if acquired:
                self._release_search_slot()
                acquired = False
            yield self._event(
                "done",
                state,
                len(completed),
                total_sources,
                offset,
                page_size,
                self._matched_by(cache_value["pool"]) if debug else None,
                scope=scope,
            )
        finally:
            # Skip any queued source work from an interrupted or finished
            # search, release the admission slot immediately, and wake any
            # identical-query waiters (who retry when cache_value is None).
            stop.set()
            for future in futures:
                future.cancel()
            if acquired:
                self._release_search_slot()
            self._finish_inflight(inflight_key, pending, cache_value)

    def _release_search_slot(self) -> None:
        self._search_slots.release()

    def _acquire_search_slot(self) -> bool:
        """Take a search slot, waiting up to ``slot_wait_seconds`` for one.

        Returns False when every slot stays busy for the whole budget, or when
        too many requests are already waiting. Test doubles without the
        waiter semaphore fall back to a simple timed acquire.
        """
        if self._search_slots.acquire(blocking=False):
            return True
        wait_seconds = max(0.0, getattr(self, "slot_wait_seconds", SLOT_WAIT_SECONDS))
        if wait_seconds <= 0:
            return False
        waiters = getattr(self, "_slot_waiters", None)
        if waiters is not None and not waiters.acquire(blocking=False):
            return False
        try:
            return self._search_slots.acquire(timeout=wait_seconds)
        finally:
            if waiters is not None:
                waiters.release()

    def _run_source_task(
        self,
        stop: threading.Event,
        source: SourceInfo,
        *args,
        browse: str | None = None,
        seed: int = 0,
    ) -> SourceResult:
        if stop.is_set():
            return SourceResult(
                source=source,
                error="search was cancelled",
                status="cancelled",
            )
        target = self._search_source
        if getattr(target, "__func__", None) is Search._search_source:
            if browse is not None:
                return target(source, *args, browse=browse, seed=seed)
            return target(source, *args)
        # Lightweight test doubles still expose the pre-deadline signature.
        return target(source, *args[:-1])

    def _search_source(
        self,
        source: SourceInfo,
        query: str,
        title_query: str,
        fulltext_query: str,
        query_vector,
        limit: int,
        deadline: float | None = None,
        *,
        browse: str | None = None,
        seed: int = 0,
    ) -> SourceResult:
        settings = resolve_settings(self.cfg, source)
        source_deadline = time.monotonic() + settings.get(
            "source_timeout", self.source_timeout
        )
        request = SourceQuery(
            query=query,
            title_query=title_query,
            fulltext_query=fulltext_query,
            query_vector=query_vector,
            limit=limit,
            deadline=min(deadline, source_deadline)
            if deadline is not None
            else source_deadline,
            browse=browse,
            seed=seed,
        )
        with self._state_lock:
            runtime = self.runtimes.get(source.collection_key)
        if runtime is None:
            return SourceResult(
                source=source,
                error=f"no runtime for collection {source.collection_key!r}",
                status="error",
            )
        try:
            result = runtime.search(source, request)
        except Exception as error:  # provider failure is source-scoped
            logger.debug("source search failed for %s: %s", source.key, error)
            return SourceResult(
                source=source,
                error=f"search failed: {self.public_error(error)}",
                status="error",
            )
        if time.monotonic() >= request.deadline:
            return SourceResult(
                source=source, error="search deadline expired", status="cancelled"
            )
        if result.error:
            result.error = str(self.public_text(result.error))
        return result

    def _hydrate_results(
        self,
        docs: list[dict[str, Any]],
    ) -> None:
        """Populate previews for the displayed page, in parallel by source."""
        if self.preview_limit <= 0:
            return
        docs = docs[: self.preview_limit]
        with self._state_lock:
            sources = dict(self.sources)
            runtimes = dict(self.runtimes)
        preview_cache = self.preview_cache
        if preview_cache is None:
            preview_cache = QueryCache(0, 0)
            self.preview_cache = preview_cache

        grouped: dict[str, list[dict[str, Any]]] = {}

        def cache_key(source: Any, doc: dict[str, Any]):
            runtime = runtimes.get(source.collection_key)
            index = (
                runtime.indexes.get(source.key)
                if isinstance(runtime, IndexedRuntime) else None
            )
            generation = (index.generation or "") if index is not None else ""
            return (source.key, generation, doc["path"], settings_key(self.cfg, source))

        for doc in docs:
            source = sources.get(doc.get("source_key"))
            if source is None:
                continue
            cached = preview_cache.get(cache_key(source, doc))
            if cached is not None:
                doc["lead"], cached_mimetype = cached
                if cached_mimetype and not doc.get("mimetype"):
                    doc["mimetype"] = cached_mimetype
                continue
            if doc.get("lead"):
                continue
            grouped.setdefault(source.key, []).append(doc)

        def _preview_group(source_key: str, source_docs: list[dict[str, Any]]):
            source = sources.get(source_key)
            runtime = runtimes.get(source.collection_key) if source else None
            if source is None or runtime is None:
                return source_key, source_docs, {}
            previews: dict[str, tuple[str, str | None]] = {}
            settings = resolve_settings(self.cfg, source)
            for doc in source_docs:
                try:
                    lead, mimetype = runtime.preview(
                        source,
                        doc,
                        max_chars=settings.get("max_preview_chars", self.preview_chars),
                        max_bytes=min(settings.get("max_html_bytes", PREVIEW_HTML_BYTES), PREVIEW_HTML_BYTES),
                        deadline=None,
                    )
                except Exception as error:
                    logger.warning(
                        "%s: preview extraction failed (%s)",
                        source.key,
                        self.public_error(error),
                    )
                    logger.debug("preview failed for %s: %s", source.key, error)
                    lead, mimetype = "", doc.get("mimetype")
                previews[doc["id"]] = (lead, mimetype)
            return source_key, source_docs, previews

        futures = {
            self._executor.submit(_preview_group, source_key, source_docs): (
                source_key,
                source_docs,
            )
            for source_key, source_docs in grouped.items()
        }
        for future in as_completed(futures):
            source_key, source_docs, previews = future.result()
            source = sources.get(source_key)
            for doc in source_docs:
                lead, mimetype = previews.get(doc["id"], ("", doc.get("mimetype")))
                doc["lead"] = lead
                if mimetype:
                    doc["mimetype"] = mimetype
                if source is not None:
                    preview_cache.put(
                        cache_key(source, doc),
                        (lead, doc.get("mimetype")),
                        quotas=self._cache_quotas([source]),
                    )

    def _enrich_pool(
        self,
        source_results: list[SourceResult],
        query: str,
        pool: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fill MIME types and run the content-phrase check through runtimes."""
        phrase_words = _phrase_words(query)
        query_terms = _query_terms(query)
        phrase_active = len(phrase_words) >= 2 and bool(pool)
        _, _, _, docs, source_by_key = _collect_ranked_candidates(source_results)
        with self._state_lock:
            runtimes = dict(self.runtimes)

        phrase_ids: set[str] = set()
        if phrase_active:
            for result in pool:
                if len(phrase_ids) >= MAX_PHRASE_CANDIDATES:
                    break
                source = source_by_key.get(result.get("source_key"))
                if source is None:
                    continue
                runtime = runtimes.get(source.collection_key)
                if not isinstance(runtime, IndexedRuntime):
                    continue
                index = runtime.indexes.get(source.key)
                if index is None or index.ensure_open() is None:
                    continue
                if _phrase(query_terms, _content_terms(result["title"])):
                    continue
                phrase_ids.add(result["id"])

        grouped: dict[str, list[dict[str, Any]]] = {}
        for result in pool:
            source = source_by_key.get(result.get("source_key"))
            if source is None:
                continue
            grouped.setdefault(source.key, []).append(result)

        def enrich_source(source_key: str, result_docs: list[dict[str, Any]]) -> bool:
            source = source_by_key.get(source_key)
            if source is None:
                return False
            runtime = runtimes.get(source.collection_key)
            if not isinstance(runtime, EnrichingRuntime):
                return False
            settings = resolve_settings(self.cfg, source)
            return bool(
                runtime.enrich(
                    source,
                    result_docs,
                    docs,
                    phrase_ids=phrase_ids,
                    phrase_words=phrase_words,
                    max_html_bytes=settings.get("max_html_bytes", self.max_html_bytes),
                    max_article_bytes=self.max_article_bytes,
                )
            )

        executor = getattr(self, "_executor", None)
        found = False
        if executor is not None and len(grouped) > 1:
            futures = [
                executor.submit(enrich_source, source_key, result_docs)
                for source_key, result_docs in grouped.items()
            ]
            for future in as_completed(futures):
                if future.result():
                    found = True
        else:
            for source_key, result_docs in grouped.items():
                if enrich_source(source_key, result_docs):
                    found = True
        return self._rank_results(source_results, query, limit) if found else pool

    def _sort_results(
        self,
        pool: list[dict[str, Any]],
        mode: str,
        seed: int,
        deadline: float | None,
        *,
        balance_sources: bool = False,
        limit: int | None = None,
        query_vector=None,
    ) -> list[dict[str, Any]]:
        scores: dict[str, float] = {}
        if mode == "diverse":
            try:
                ordered = self._diversity(
                    pool, deadline, query_vector=query_vector,
                    balance_sources=balance_sources, limit=limit,
                )
            except TimeoutError:
                return pool if limit is None else pool[:limit]
            scores = {doc["id"]: float(len(ordered) - rank) for rank, doc in enumerate(ordered)}
            pool = ordered
        elif mode == "random":
            scores = {
                doc["id"]: int.from_bytes(hashlib.blake2b(
                    f"{seed}:{doc['id']}".encode(), digest_size=8
                ).digest()) / 2**64
                for doc in pool
            }
        else:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for doc in pool:
                grouped.setdefault(doc["source_key"], []).append(doc)
            with self._state_lock:
                runtimes = dict(self.runtimes)
                sources = dict(self.sources)
            for key, docs in grouped.items():
                source = sources.get(key)
                runtime = runtimes.get(source.collection_key) if source else None
                if not isinstance(runtime, IndexedRuntime):
                    continue
                index = runtime.indexes.get(key)
                if index is None:
                    continue
                try:
                    by_path = sort_scores(index, docs, mode, deadline)
                except (TimeoutError, sqlite3.Error, ValueError, RuntimeError) as error:
                    logger.debug("sort unavailable for %s: %s", key, error)
                    continue
                scores.update({
                    doc["id"]: by_path[doc["path"]]
                    for doc in docs if doc["path"] in by_path
                })
        ordered = sorted(pool, key=lambda doc: -scores.get(doc["id"], -math.inf))
        return [
            {
                **doc,
                "ranking": {
                    **doc.get("ranking", {}),
                    "sort": {"mode": mode, "score": scores.get(doc["id"])},
                    "final_rank": rank,
                },
            }
            for rank, doc in enumerate(ordered, 1)
        ]

    def _diversity(
        self, pool, deadline, *, query_vector, balance_sources, limit,
    ):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for doc in pool:
            grouped.setdefault(doc["source_key"], []).append(doc)
        with self._state_lock:
            runtimes = dict(self.runtimes)
            sources = dict(self.sources)
        vectors, relevance = {}, {}
        for key, docs in grouped.items():
            if not self.semantic:
                break
            source = sources.get(key)
            runtime = runtimes.get(source.collection_key) if source else None
            if not isinstance(runtime, IndexedRuntime):
                continue
            index = runtime.indexes.get(key)
            if index is None:
                continue
            try:
                embeddings, centrality = diversity_vectors(
                    index, docs, deadline, with_centroid=balance_sources,
                )
            except (TimeoutError, sqlite3.Error, ValueError, RuntimeError) as error:
                logger.debug("semantic diversity unavailable for %s: %s", key, error)
                continue
            for doc in docs:
                path = doc["path"]
                if path in embeddings:
                    vectors[doc["id"]] = embeddings[path]
                    relevance[doc["id"]] = centrality.get(path, 0.0)
        if not vectors:
            order = diverse_order(
                [doc.get("title") or "" for doc in pool], deadline,
                sources=[doc["source_key"] for doc in pool] if balance_sources else None,
                limit=limit,
            )
            return [pool[position] for position in order]
        import numpy as np

        dimension = len(next(iter(vectors.values())))
        semantic = [doc["id"] in vectors and len(vectors[doc["id"]]) == dimension for doc in pool]
        matrix = np.zeros((len(pool), dimension), dtype=np.float32)
        for position, doc in enumerate(pool):
            if semantic[position]:
                matrix[position] = vectors[doc["id"]]
        if not balance_sources:
            maximum = max(max(0.0, float(doc.get("score") or 0.0)) for doc in pool)
            relevance = (
                {doc["id"]: max(0.0, float(doc.get("score") or 0.0)) / maximum for doc in pool}
                if maximum > 0 else
                {doc["id"]: 1 - rank / max(1, len(pool)) for rank, doc in enumerate(pool)}
            )
            prior = np.array([relevance[doc["id"]] for doc in pool], dtype=np.float32)
            if query_vector is not None and query_vector.shape[-1] == dimension:
                query = np.asarray(query_vector, dtype=np.float32).reshape(-1)
                norm = np.linalg.norm(query)
                # Raw cosine alone discards exact-title boosts and profile
                # penalties already applied by hybrid relevance ranking.
                base = (matrix @ (query / norm) + prior) / 2 if norm > 0 and np.isfinite(norm) else prior
            else:
                base = prior
        else:
            base = None
        if base is None:
            base = np.array([relevance.get(doc["id"], 0.0) for doc in pool])
        order = diverse_order(
            [doc.get("title") or "" for doc in pool], deadline,
            vectors=matrix, relevance=base, semantic_mask=semantic,
            sources=[doc["source_key"] for doc in pool] if balance_sources else None,
            limit=limit,
        )
        return [pool[position] for position in order]

    def _rank_results(
        self,
        source_results: list[SourceResult],
        query: str,
        limit: int,
        *,
        deadline: float | None = None,
    ) -> list[dict[str, Any]]:
        if not query:
            mode = next(
                (doc.get("ranking", {}).get("browse", {}).get("mode")
                 for result in source_results for doc in result.items),
                None,
            )
            if mode == "diverse":
                pool = rank_browse_results(
                    source_results, sum(len(result.items) for result in source_results)
                )
                return self._sort_results(
                    pool, mode, 0, deadline, balance_sources=True, limit=limit,
                )
            return rank_browse_results(source_results, limit)
        return rank_results(
            source_results,
            query,
            limit,
            self.candidate_count,
        )
