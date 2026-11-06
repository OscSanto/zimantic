"""Hybrid search core over provider runtimes."""

from __future__ import annotations

import logging
import math
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .cache import DEFAULT_CACHE_BYTES, DEFAULT_CACHE_SIZE, QueryCache
from .content.reader import DEFAULT_MAX_HTML_BYTES, DEFAULT_PREVIEW_CHARS
from .content.serving import (
    HEAD_READ_BYTES,
    RANDOM_ARTICLE_ATTEMPTS,
    ArticleNotFoundError,
    ArticleSourceNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
    is_random_article_entry,
    normalize_article_path,
    random_fallback_article,
    random_indexed_article,
    read_archive_article,
    read_archive_icon,
    resolve_article_entry,
)
from .contracts import SourceInfo, SourceResult
from .providers.base import CollectionInfo, CollectionSnapshot, ProviderContext, SourceQuery
from .providers.config import ProviderConfig, RuntimeConfig, ZimConfig
from .searching.config import (
    DEFAULT_MAX_ARTICLE_BYTES,
    _float_config,
    _int_config,
)
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
    DEFAULT_LONG_QUERY,
    FULLTEXT_WEIGHT,
    KEYWORD_WEIGHT,
    LONG_FULLTEXT_WEIGHT,
    RRF_K,
    SEMANTIC_WEIGHT,
    _RankedAssessment,
    _assess_ranked_document,
    _collect_ranked_candidates,
    _deduplicate_ranked,
    _render_ranked_document,
    _rrf_scores,
    matched_by,
    rank_results,
)
from .settings import normalize_web_base_path

logger = logging.getLogger(__name__)

__all__ = [
    "ArticleNotFoundError",
    "ArticleSourceNotFoundError",
    "ArticleTooLargeError",
    "ArticleUnavailableError",
    "DEFAULT_LONG_QUERY",
    "FULLTEXT_WEIGHT",
    "HEAD_READ_BYTES",
    "KEYWORD_WEIGHT",
    "LONG_FULLTEXT_WEIGHT",
    "RANDOM_ARTICLE_ATTEMPTS",
    "RRF_K",
    "SEMANTIC_WEIGHT",
    "Search",
    "_RankedAssessment",
    "_assess_ranked_document",
    "_collect_ranked_candidates",
    "_deduplicate_ranked",
    "_render_ranked_document",
    "_rrf_scores",
    "is_random_article_entry",
    "matched_by",
    "normalize_article_path",
    "random_fallback_article",
    "random_indexed_article",
    "rank_results",
    "read_archive_article",
    "read_archive_icon",
    "resolve_article_entry",
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
# A selection at or below this many sources is searched on its own. Larger
# selections are promoted to one full search so their pool can be shared.
SELECTIVE_SOURCE_LIMIT = 10
SELECTIVE_SOURCE_RATIO = 0.35
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
# The installed-library view recomputes its version on every conditional
# request. A short cache keeps that from walking the whole ZIM tree each time
# while still noticing files added or removed without a reload.
LIBRARY_SCAN_TTL_SECONDS = 120.0
HEALTH_CACHE_TTL_SECONDS = 2.0


class SearchQueryError(ValueError):
    """The requested query cannot be processed."""


class SearchBusyError(Exception):
    """Too many searches are already running; the client should retry."""


def _source_title(source: SourceInfo) -> str:
    return source.title or source.key


class Search:
    cfg: dict[str, Any] = {}
    semantic: bool = False
    embedder: Any = None
    _defer_faiss: bool = False
    runtimes: dict[str, Any] = {}
    collections: dict[str, CollectionInfo] = {}
    _library_source_index: dict[str, SourceInfo] = {}
    _discovery_batches: tuple[Any, ...] = ()
    _collection_snapshots: dict[str, CollectionSnapshot] = {}
    runtime_config: RuntimeConfig | None = None
    providers: dict[str, Any] = {}
    _library_scan_at: float = 0.0
    _library_version_at: float = 0.0
    _library_scan_token: int = 0
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
        cfg: dict,
        embedder=None,
        semantic: bool = True,
        archive_factory: Any | None = None,
    ):
        self.cfg = cfg
        self.embedder = embedder
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
        self.source_workers = _int_config(cfg, "search_workers", 4)
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
            cfg, "max_concurrent_searches", DEFAULT_MAX_CONCURRENT_SEARCHES
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
        self._library_scan_at = 0.0
        # Provider/collection state: every provider path goes through a runtime.
        from .providers.registry import get_provider

        self.providers = {kind: get_provider(kind) for kind in ("zim", "kiwix")}
        self.runtimes: dict[str, Any] = {}
        self.collections: dict[str, CollectionInfo] = {}
        self._discovery_batches: list[Any] = []
        self._collection_snapshots: dict[str, CollectionSnapshot] = {}
        self._library_source_index: dict[str, SourceInfo] = {}
        self._process_token = secrets.token_hex(8)
        self._aggregate_revision = 0
        runtime_config = cfg.get("runtime_config")
        if not isinstance(runtime_config, RuntimeConfig):
            runtime_config = RuntimeConfig(
                settings=dict(cfg),
                collections=tuple(cfg.get("collections") or ()),
                config_dir=Path(cfg.get("config_dir") or Path.cwd()),
                search_timeout_seconds=_float_config(
                    cfg, "search_timeout_seconds", 30.0, 0.1, math.inf
                ),
                page_size=_int_config(cfg, "page_size", DEFAULT_PAGE_SIZE),
            )
        self.runtime_config = runtime_config
        self.search_timeout_seconds = float(runtime_config.search_timeout_seconds)
        # Provider runtimes own their discovery.  Kiwix starts a bounded
        # background catalog refresh when its batch opens, so an unreachable
        # server cannot delay startup.
        self.refresh_collections()

    @property
    def web_base_path(self) -> str:
        """Normalized URL prefix for generated links (reverse-proxy support)."""
        return normalize_web_base_path(self.cfg.get("web_base_path"))

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

    def _bind_runtime(self, runtime: Any) -> None:
        """Give a provider runtime the core-owned search services it needs."""
        bind = getattr(runtime, "bind_search", None)
        if not callable(bind):
            return
        bind(
            semantic=self.semantic,
            defer_faiss=self._defer_faiss,
            embedder_getter=lambda: self.embedder,
            embedder_ready_getter=lambda: (
                getattr(self, "_embedder_ready", None),
                getattr(self, "_embedder_started", False),
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
                    error,
                )
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
                        error,
                    )

            with self._state_lock:
                self._defer_faiss = False
                runtimes = list(self.runtimes.values())
                for runtime in runtimes:
                    self._bind_runtime(runtime)
            for runtime in runtimes:
                starter = getattr(runtime, "start_faiss", None)
                if callable(starter):
                    try:
                        starter()
                    except Exception as error:
                        logger.warning(
                            "zimantic: semantic search disabled for %s (%s); "
                            "falling back to title and full-text",
                            getattr(runtime.collection, "label", "collection"),
                            error,
                        )
            with self._state_lock:
                semantic_indexes = sum(
                    sum(
                        1
                        for index in getattr(runtime, "indexes", {}).values()
                        if index.semantic
                    )
                    for runtime in runtimes
                )
            logger.info(
                "zimantic: FAISS ready; semantic search enabled for %d index(es)",
                semantic_indexes,
            )

        threading.Thread(target=_load, name="zimantic-faiss", daemon=True).start()

    def start_fulltext_isolation(self) -> None:
        """Run native full-text searches in helper processes, per runtime."""
        from .fulltext import DEFAULT_WORKERS

        workers = min(DEFAULT_WORKERS, max(1, self.source_workers))
        with self._state_lock:
            runtimes = list(self.runtimes.values())
        for runtime in runtimes:
            starter = getattr(runtime, "start_fulltext_isolation", None)
            if callable(starter):
                starter(workers)

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
                        getattr(index, "generation", None),
                        index.db_signature,
                        index.zim_signature,
                        index.done,
                    )
                    for runtime in self.runtimes.values()
                    for key, index in getattr(runtime, "indexes", {}).items()
                }

        with self._reload_lock:
            with self._state_lock:
                old_keys = set(self.sources)
                old_indexes = set(_index_tokens())
            old_tokens = _index_tokens()
            if refresh or scan_catalog:
                with self._state_lock:
                    runtimes = list(self.runtimes.values())
                for runtime in runtimes:
                    try:
                        runtime.refresh(wait=True)
                    except Exception as error:
                        logger.warning(
                            "zimantic: collection refresh failed for %s (%s)",
                            getattr(runtime.collection, "label", "collection"),
                            error,
                        )
            self._publish_runtime_sources()
            with self._state_lock:
                new_keys = set(self.sources)
                new_indexes = set(_index_tokens())
            new_tokens = _index_tokens()
            added = sorted(new_keys - old_keys)
            removed = sorted(old_keys - new_keys)
            upgraded = sorted(new_indexes - old_indexes)
            if added or removed or upgraded or new_tokens != old_tokens:
                self._invalidate_library_cache()
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
                    getattr(runtime.collection, "label", "collection"),
                    error,
                )
        return self._publish_runtime_sources()

    def resolve_source(self, identifier: str) -> SourceInfo | None:
        """Resolve an exact canonical source key; no aliases or fallbacks."""
        with self._state_lock:
            source = self.sources.get(str(identifier))
            if source is not None:
                return source
            return self._library_source_index.get(str(identifier))

    def _configured_provider_entries(self) -> tuple[ProviderConfig, ...]:
        runtime_config = getattr(self, "runtime_config", None)
        entries = list(getattr(runtime_config, "collections", ()))
        if entries:
            return tuple(entries)
        # Compatibility for direct Search callers/tests that still pass the
        # internal zim_dir setting. The TOML loader never accepts the flat key
        # from users.  A Kiwix collection exists only through an explicit
        # [[collections]] entry.
        computed: list[ProviderConfig] = []
        zim_dir = self.cfg.get("zim_dir")
        if zim_dir:
            computed.append(ZimConfig(name="library", path=str(zim_dir)))
        return tuple(computed)

    def refresh_collections(self, *, wait: bool = False) -> list[dict[str, Any]]:
        """Rerun provider discovery for configured entries and publish snapshots."""
        runtime_config = getattr(self, "runtime_config", None)
        if runtime_config is None:
            # Lightweight test doubles intentionally skip __init__; there is no
            # configured entry set to rediscover.
            return self.source_dicts()
        ctx = ProviderContext(
            config=runtime_config,
            index_dir=Path(self.cfg.get("index_dir") or "."),
            web_base_path=self.web_base_path,
            logger=logger,
            archive_factory=getattr(self, "archive_factory", None),
            persist_catalog=bool(self.cfg.get("index_dir")),
            on_sources_changed=self._publish_runtime_sources,
        )
        new_runtimes: dict[str, Any] = {}
        new_collections: dict[str, CollectionInfo] = {}
        new_batches: list[Any] = []
        diagnostics: list[str] = []
        for entry in self._configured_provider_entries():
            provider = self.providers.get(entry.kind)
            if provider is None:
                diagnostics.append(f"no provider registered for {entry.kind!r}")
                continue
            try:
                batch = provider.discover(entry, ctx)
            except Exception as error:
                diagnostics.append(f"{entry.name}: {entry.kind} discovery failed: {error}")
                continue
            try:
                for collection in batch.collections:
                    runtime = batch.open(collection)
                    self._bind_runtime(runtime)
                    new_runtimes[collection.key] = runtime
                    new_collections[collection.key] = collection
                new_batches.append(batch)
            except Exception as error:
                diagnostics.append(f"{entry.name}: {entry.kind} open failed: {error}")
                for runtime in new_runtimes.values():
                    try:
                        runtime.close()
                    except Exception:
                        pass
                for opened_batch in new_batches:
                    try:
                        opened_batch.close()
                    except Exception:
                        pass
                new_runtimes.clear()
                new_collections.clear()
                new_batches.clear()
                batch.close()
        with self._state_lock:
            old_runtimes = self.runtimes
            old_batches = getattr(self, "_discovery_batches", [])
            self.runtimes = new_runtimes
            self.collections = new_collections
            self._discovery_batches = new_batches
            self._aggregate_revision = getattr(self, "_aggregate_revision", 0) + 1
        sources = self._publish_runtime_sources()
        for runtime in old_runtimes.values():
            try:
                runtime.close()
            except Exception:
                pass
        for batch in old_batches:
            try:
                batch.close()
            except Exception:
                pass
        if diagnostics:
            for message in diagnostics:
                logger.warning("zimantic: collection discovery: %s", message)
        return sources

    def _publish_runtime_sources(self) -> list[dict[str, Any]]:
        with self._state_lock:
            sources: dict[str, SourceInfo] = {}
            library: dict[str, SourceInfo] = {}
            for runtime in self.runtimes.values():
                for source in runtime.sources:
                    sources[source.key] = source
                for source in runtime.library_sources():
                    library[source.key] = source
            ordered = dict(
                sorted(
                    sources.items(),
                    key=lambda pair: (_source_title(pair[1]).casefold(), pair[1].key),
                )
            )
            old_sources = self.sources
            changed = ordered != old_sources
            self.sources = ordered
            self._library_source_index = library
            self._collection_snapshots = {
                key: runtime.snapshot for key, runtime in self.runtimes.items()
            }
            if changed:
                self._cache_generation += 1
                self._aggregate_revision = (
                    getattr(self, "_aggregate_revision", 0) + 1
                )
            result = self.source_dicts()
        if changed:
            self._invalidate_library_cache()
            self._remote_limit_warned = False
            self.cache.clear()
            preview_cache = self.preview_cache
            if preview_cache is not None:
                preview_cache.clear()
        return result

    def collection_dicts(self) -> list[dict[str, Any]]:
        """Serialize the current collection snapshots."""
        with self._state_lock:
            snapshots = dict(self._collection_snapshots)
        return [
            snapshot.to_dict()
            for _key, snapshot in sorted(snapshots.items())
        ]

    def snapshot_token(self) -> tuple[str, int]:
        """Process-instance identity plus aggregate publication revision."""
        with self._state_lock:
            return self._process_token, self._aggregate_revision

    def source_dicts(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return [
                source.to_dict(include_metadata=True)
                for source in self.sources.values()
            ]

    def _library_cache_lock(self) -> threading.Lock:
        """Return the lock guarding the library scan/version caches."""
        with self._state_lock:
            lock = getattr(self, "_library_scan_lock", None)
            if lock is None:
                lock = threading.Lock()
                self._library_scan_lock = lock
            return lock

    def _invalidate_library_cache(self) -> None:
        """Invalidate the catalog and library response refresh intervals."""
        lock = self._library_cache_lock()
        with lock:
            self._library_scan_at = 0.0
            self._library_version_at = 0.0
            # A scan that started before this point must not publish its stale
            # result with a fresh timestamp.
            self._library_scan_token = getattr(self, "_library_scan_token", 0) + 1

    def library_dicts(self) -> list[dict[str, Any]]:
        """Return metadata for every document-library source.

        ZIM runtimes include readable-but-unsearchable and unreadable archives
        that are not in the searchable source set.  Discovery failures remain
        collection-level diagnostics, not per-source state.
        """
        with self._state_lock:
            runtimes = list(self.runtimes.values())
        for runtime in runtimes:
            stale = getattr(runtime, "refresh_if_stale", None)
            if callable(stale):
                try:
                    stale()
                except Exception:
                    pass
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
        return [
            source.to_dict(include_metadata=True)
            for source in sorted(
                library.values(),
                key=lambda item: (_source_title(item).casefold(), item.key),
            )
        ]

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

    def _runtime_lookup(self, identifier: str) -> tuple[SourceInfo, Any]:
        source = self.resolve_source(identifier)
        if source is None:
            raise ArticleSourceNotFoundError("source not found")
        with self._state_lock:
            runtime = self.runtimes.get(source.collection_key)
        if runtime is None:
            raise ArticleUnavailableError("source is unavailable")
        return source, runtime

    def read_article(
        self,
        zim_name: str,
        path: str | None = None,
        head_only: bool = False,
    ) -> dict[str, Any]:
        """Read one source entry through its runtime content store."""
        source, runtime = self._runtime_lookup(zim_name)
        reader = getattr(runtime, "read_article", None)
        if not callable(reader):
            raise ArticleNotFoundError("source content is unavailable")
        return reader(source.key, path, head_only)

    def read_article_head(
        self, zim_name: str, path: str | None = None
    ) -> dict[str, Any]:
        return self.read_article(zim_name, path, head_only=True)

    def article_path_exists(self, zim_name: str, path: str) -> bool:
        """Whether ``path`` resolves to a source entry."""
        try:
            _source, runtime = self._runtime_lookup(zim_name)
        except (ArticleSourceNotFoundError, ArticleUnavailableError):
            return False
        checker = getattr(runtime, "article_path_exists", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(zim_name, path))
        except (ArticleNotFoundError, ArticleUnavailableError):
            return False

    def read_source_icon(self, zim_name: str) -> dict[str, Any]:
        """Read the illustration selected for a source."""
        source, runtime = self._runtime_lookup(zim_name)
        reader = getattr(runtime, "read_source_icon", None)
        if not callable(reader):
            raise ArticleNotFoundError("source icon not found")
        return reader(source.key)

    def random_local_source(self) -> SourceInfo:
        """Return a random readable local source that has a homepage."""
        with self._state_lock:
            runtimes = [
                runtime
                for runtime in self.runtimes.values()
                if getattr(runtime.collection, "provider", None) == "zim"
            ]
        candidates: list[SourceInfo] = []
        for runtime in runtimes:
            chooser = getattr(runtime, "random_source", None)
            if not callable(chooser):
                continue
            try:
                candidates.append(chooser())
            except ArticleNotFoundError:
                continue
        if not candidates:
            raise ArticleNotFoundError("no local source has a homepage")
        return secrets.choice(candidates)

    def random_article_path(self, zim_name: str) -> str:
        """Return a random indexed article path without changing search state."""
        source, runtime = self._runtime_lookup(zim_name)
        chooser = getattr(runtime, "random_article_path", None)
        if not callable(chooser):
            raise ArticleNotFoundError("random articles unavailable")
        return chooser(source.key)

    def health(self) -> dict[str, Any]:
        """Return read-only index, archive, and capability diagnostics."""
        now = time.monotonic()
        with self._state_lock:
            source_count = len(self.sources)
            embedder_loaded = self.embedder is not None
            generation = self._cache_generation
            deferred_faiss = self._defer_faiss
            runtimes = list(self.runtimes.values())
            cached = getattr(self, "_health_cache", None)
        if (
            cached is not None
            and now - cached[0] < HEALTH_CACHE_TTL_SECONDS
            and cached[1:4] == (generation, embedder_loaded, deferred_faiss)
        ):
            report = dict(cached[4])
            report["cache"] = self.cache.stats()
            preview_cache = self.preview_cache
            report["preview_cache"] = (
                preview_cache.stats() if preview_cache is not None else None
            )
            return report

        reports: list[dict[str, Any]] = []
        for runtime in runtimes:
            reporter = getattr(runtime, "health", None)
            if not callable(reporter):
                continue
            try:
                reports.extend(reporter())
            except Exception as error:
                logger.warning(
                    "zimantic: health report failed for %s (%s)",
                    getattr(runtime.collection, "label", "collection"),
                    error,
                )
        status = "ok"
        if not reports or any(report["status"] != "ok" for report in reports):
            status = "degraded"
        report = {
            "status": status,
            "indexes": reports,
            "sources": source_count,
            "embedding": {
                "enabled": self.semantic,
                "loaded": embedder_loaded,
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
                report,
            )
        return report

    def health_version(self) -> tuple[int, bool, bool, int]:
        """Return a short-lived version token for the health response cache."""
        with self._state_lock:
            generation = self._cache_generation
            embedder_loaded = self.embedder is not None
            deferred_faiss = self._defer_faiss
        bucket = int(time.monotonic() / HEALTH_CACHE_TTL_SECONDS)
        return generation, embedder_loaded, deferred_faiss, bucket

    @staticmethod
    def _selective_source_limit(total_sources: int) -> int:
        """Largest selection that still searches only the selected sources.

        The ratio intentionally rounds down: a two-source library has a limit
        of zero, so selecting one of them is promoted to the shared full search
        (the display filter still narrows the visible results). This keeps the
        number of distinct cached pools small on tiny libraries.
        """
        return min(
            SELECTIVE_SOURCE_LIMIT,
            math.floor(total_sources * SELECTIVE_SOURCE_RATIO),
        )

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
            runtimes = dict(getattr(self, "runtimes", {}))
        keep: set[str] = set()
        for collection_key, collection_sources in by_collection.items():
            runtime = runtimes.get(collection_key)
            limit = None
            if runtime is not None:
                policy = getattr(runtime, "max_sources_per_search", None)
                if callable(policy):
                    try:
                        limit = policy()
                    except Exception:
                        limit = None
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

    def _select_sources(
        self, available: list[SourceInfo], zim: list[str]
    ) -> list[SourceInfo]:
        wanted = set(zim)
        return [source for source in available if source.key in wanted]

    def _search_scope(
        self, zim: list[str] | None
    ) -> tuple[list[SourceInfo], bool, tuple[str, ...] | None]:
        """Resolve which sources to search and how the pool may be cached.

        A small selection searches only those sources and is treated as a
        one-off scope: it is not cached, because users can try many different
        subsets. A larger selection is promoted to a full search over every
        available source (remote Kiwix fan-out is capped by
        ``max_remote_sources``); that pool is cached without a selection in its
        key so every large selection and every unfiltered search share it. An
        explicit selection that includes a source outside that cap is honored.
        """
        with self._state_lock:
            available = [source for source in self.sources.values() if source.searchable]
        bounded = self._bounded_available_sources(available)
        self._scope_requested = len(available)
        self._scope_source_cap = len(bounded) < len(available)
        if zim is None:
            return bounded, False, None
        if not zim:
            # An explicit empty list is a deliberately empty scope (the cache
            # key distinguishes it from None); it must not mean "all sources".
            self._scope_requested = 0
            self._scope_source_cap = False
            return [], True, ()

        selected = self._select_sources(available, zim)
        if not selected:
            # A stale bookmark or a source removed since the URL was saved must
            # not silently turn into a zero-source search. Fall back to the full
            # library and let the normal availability rules decide.
            logger.warning(
                "zimantic: ignoring unknown source selection (%s); searching all sources",
                ", ".join(sorted(set(zim))),
            )
            return bounded, False, None
        limit = self._selective_source_limit(len(available))
        bounded_keys = {source.key for source in bounded}
        if len(selected) <= limit and all(
            source.key in bounded_keys for source in selected
        ):
            self._scope_requested = len(selected)
            self._scope_source_cap = False
            return (
                selected,
                bool(selected),
                tuple(sorted(source.key for source in selected)),
            )
        if any(source.key not in bounded_keys for source in selected):
            # The user named a source that the unscoped remote cap would have
            # dropped. Honor the explicit selection instead of silently
            # searching a different set.
            self._scope_requested = len(selected)
            self._scope_source_cap = False
            return (
                selected,
                bool(selected),
                tuple(sorted(source.key for source in selected)),
            )
        return bounded, False, None

    @staticmethod
    def _validate_query(query: str) -> str:
        if not isinstance(query, str):
            raise SearchQueryError("query must be text")
        query = query.strip()
        if not query:
            raise SearchQueryError("query must not be empty")
        if len(query) > MAX_QUERY_LENGTH:
            raise SearchQueryError(f"query must be at most {MAX_QUERY_LENGTH} characters")
        return query

    @staticmethod
    def _cache_key(query: str, zim: list[str] | None) -> tuple:
        """Key by query and source selection only.

        The ranked pool is independent of page size, offset, display filter and
        debug flag, so none of those belong in the key: every page of a query
        reuses one cached pool instead of fragmenting the cache per `limit`.
        A ``None`` selection is the shared full-search pool; an empty tuple is a
        deliberately empty source scope.
        """
        selection = tuple(sorted(zim)) if zim is not None else None
        normalized = re.sub(r"\s+", " ", query).strip().casefold()
        return (normalized, selection)

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

    def _source_limit(self, total_sources: int) -> int:
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
        return min(pool_target, max(self.candidate_count, share))

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
        source: str | list[str] | None,
        offset: int,
        page_size: int,
        debug: bool,
    ) -> dict[str, Any]:
        """Slice one display page out of the ranked pool.

        `source` is a display-only filter: the pool was ranked over every
        selected source, so narrowing here never changes what was searched.
        """
        selected = {source} if isinstance(source, str) else set(source or [])
        if selected:
            # A stale bookmark may name a source that no longer exists. Treat it
            # like an absent display filter instead of returning an empty page.
            with self._state_lock:
                known = {item.key for item in self.sources.values()}
            selected &= known
        scoped = pool if not selected else [
            doc for doc in pool if doc.get("source_key") in selected
        ]
        total = len(scoped)
        # Clamp an out-of-range offset to the last real page here, where the
        # total is known. Callers can then show real results and report the
        # effective offset instead of fetching an empty page and correcting it
        # client-side.
        if total:
            effective_offset = min(offset, ((total - 1) // page_size) * page_size)
        else:
            effective_offset = 0
        page = scoped[effective_offset:effective_offset + page_size]
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
            if runtime is None:
                continue
            index = getattr(runtime, "indexes", {}).get(source.key)
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
                error,
            )
            return None

    def suggest(
        self,
        query: str,
        zim: list[str] | None = None,
        limit: int = 8,
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
            available = [
                source for source in self.sources.values() if source.searchable
            ]
            runtimes = dict(self.runtimes)
        sources = available if zim is None else self._select_sources(available, zim)
        suggestions: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for source in sources:
            remaining = limit - len(suggestions)
            if remaining <= 0:
                break
            runtime = runtimes.get(source.collection_key)
            if runtime is None:
                continue
            try:
                items = runtime.suggest(source, query, remaining)
            except Exception as error:
                logger.warning(
                    "zimantic: title suggestions unavailable for %s: %s",
                    source.key,
                    error,
                )
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
        zim: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        source: str | list[str] | None = None,
        debug: bool = False,
    ) -> dict[str, Any]:
        """Return one page plus totals, using the streaming coordinator."""
        final: dict[str, Any] = {}
        for event in self.stream_search(query, zim, limit, offset, source, debug):
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
        source: str | list[str] | None,
        offset: int,
        page_size: int,
        debug: bool,
        selective: bool = False,
        all_sources: bool = True,
    ) -> Iterator[dict[str, Any]]:
        """Replay a cached pool as a finished stream (progress + one page)."""
        pool, cached_sources = self._unpack_cache(cached)
        state = self._page_state(pool, source, offset, page_size, debug)
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
        yield self._event(
            "done",
            state,
            total_sources,
            total_sources,
            offset,
            page_size,
            matched_by,
            scope={
                "status": "complete",
                "requested": total_sources,
                "scheduled": total_sources,
                "completed": total_sources,
                "limit_reason": None,
            },
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
        zim: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        source: str | list[str] | None = None,
        debug: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Yield source progress and one page of the ranked pool.

        Ranking runs once to a fixed pool (`max_results`, at least
        `candidate_count`); `limit`/`offset`/`source` only slice that pool for
        the response. The pool is cached by query and resolved source scope, so
        every page and display filter reuses one computation. Small selected
        scopes are not cached; larger selections promote to the shared full
        search. Concurrent identical queries share a single computation.
        """
        query = self._validate_query(query)
        page_size = self._page_size(limit)
        offset = max(0, int(offset))
        # Snapshot the source set and the cache generation together. Reading
        # them in separate critical sections could pair the old sources with a
        # newer generation and cache pre-refresh results as current.
        with self._state_lock:
            sources, selective, scope_key = self._search_scope(zim)
            generation = self._cache_generation
        all_sources = scope_key is None
        cache_key = self._cache_key(query, scope_key)
        inflight_key = (generation, cache_key)
        cached, pending = self._claim_search(cache_key, inflight_key, generation)
        if cached is not None:
            yield from self._cached_events(
                cached,
                query,
                sources,
                source,
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
            source,
            offset,
            page_size,
            debug,
            selective,
            all_sources,
        )

    def _finalize_search(
        self,
        query: str,
        source_results: list[SourceResult],
        source: str | list[str] | None,
        offset: int,
        page_size: int,
        debug: bool,
        pool_target: int,
        reported_counts: dict[str, int | None],
        selective: bool,
        generation: int,
        cache_key: Any,
        pool: list[dict[str, Any]] | None = None,
        cacheable: bool = True,
        deadline: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Rank, enrich, hydrate and cache one finished search.

        Shared by the live owner and the abandoned-stream finalizer so the two
        paths cannot drift; it returns the page state used by the ``done`` event
        and the value stored in the query cache. The live owner passes its last
        ranked pool so the final page does not pay for one more ranking pass.
        """
        if pool is None:
            pool = self._rank_results(source_results, query, pool_target)
        pool = self._enrich_pool(source_results, query, pool, pool_target)
        state = self._page_state(pool, source, offset, page_size, debug)
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
        }
        with self._state_lock:
            current_generation = self._cache_generation
        if (
            not selective
            and cacheable
            and current_generation == generation
            and (deadline is None or time.monotonic() < deadline)
        ):
            self.cache.put(cache_key, cache_value)
        return state, cache_value

    def _run_search_owner(
        self,
        query: str,
        cache_key: Any,
        generation: int,
        pending: Future,
        sources: list[SourceInfo],
        source: str | list[str] | None,
        offset: int,
        page_size: int,
        debug: bool,
        selective: bool = False,
        all_sources: bool = True,
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
                cache_value = {"generation": generation, "pool": [], "sources": []}
                with self._state_lock:
                    current_generation = self._cache_generation
                if not selective and current_generation == generation:
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
                    scope={
                        "status": "complete",
                        "requested": 0,
                        "scheduled": 0,
                        "completed": 0,
                        "limit_reason": None,
                    },
                )
                return

            query_vector = self._query_vector(sources, query)

            title_query = _title_query(query)
            fulltext_query = _fulltext_query(query)
            pool_target = self._pool_target()
            max_stream_ranks = max(1, self.max_stream_ranks)
            rank_interval = max(1, math.ceil(total_sources / max_stream_ranks))
            update_interval = self.stream_update_interval
            source_limit = self._source_limit(total_sources)
            pool: list[dict[str, Any]] = []
            counts: dict[str, int] = {}
            started_at = time.monotonic()
            deadline = started_at + max(
                1.0, getattr(self, "search_timeout_seconds", 30.0)
            )
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
                            source_limit,
                            deadline,
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
                                error=f"search failed: {error}",
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
                            pool = self._rank_results(completed, query, pool_target)
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
                            state = self._page_state(pool, source, offset, page_size, debug)
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
                    pool = self._rank_results(completed, query, pool_target)
                    last_rank_at = now
                    unranked = 0
                    state = self._page_state(pool, source, offset, page_size, debug)
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
                "requested": getattr(self, "_scope_requested", total_sources),
                "scheduled": len(futures),
                "completed": len(completed),
                "limit_reason": (
                    "deadline"
                    if deadline_hit
                    else "source_cap"
                    if getattr(self, "_scope_source_cap", False)
                    else None
                ),
            }
            if scope["limit_reason"] == "source_cap":
                scope["status"] = "partial"
            state, cache_value = self._finalize_search(
                query,
                completed,
                source,
                offset,
                page_size,
                debug,
                pool_target,
                reported_counts,
                selective,
                generation,
                cache_key,
                pool=None if unranked else pool,
                cacheable=not deadline_hit,
                deadline=deadline,
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
    ) -> SourceResult:
        if stop.is_set():
            return SourceResult(
                source=source,
                error="search was cancelled",
                status="cancelled",
            )
        target = self._search_source
        if getattr(target, "__func__", None) is Search._search_source:
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
    ) -> SourceResult:
        request = SourceQuery(
            query=query,
            title_query=title_query,
            fulltext_query=fulltext_query,
            query_vector=query_vector,
            limit=limit,
            deadline=deadline,
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
            return runtime.search(source, request)
        except Exception as error:  # provider failure is source-scoped
            return SourceResult(
                source=source,
                error=f"search failed: {error}",
                status="error",
            )

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
        for doc in docs:
            source = sources.get(doc.get("source_key"))
            if source is None:
                continue
            cached = preview_cache.get((source.key, doc["path"]))
            if cached is not None:
                doc["lead"] = cached
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
            for doc in source_docs:
                try:
                    lead, mimetype = runtime.preview(
                        source,
                        doc,
                        max_chars=self.preview_chars,
                        max_bytes=PREVIEW_HTML_BYTES,
                        deadline=None,
                    )
                except Exception as error:
                    logger.warning(
                        "%s: preview extraction failed (%s)", source.key, error
                    )
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
                    preview_cache.put((source.key, doc["path"]), lead)

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
                if runtime is None:
                    continue
                index = getattr(runtime, "indexes", {}).get(source.key)
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
            if runtime is None:
                return False
            enricher = getattr(runtime, "enrich", None)
            if not callable(enricher):
                return False
            return bool(
                enricher(
                    source,
                    result_docs,
                    docs,
                    phrase_ids=phrase_ids,
                    phrase_words=phrase_words,
                    max_html_bytes=self.max_html_bytes,
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

    def _rank_results(
        self,
        source_results: list[SourceResult],
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        return rank_results(
            source_results,
            query,
            limit,
            self.candidate_count,
        )
