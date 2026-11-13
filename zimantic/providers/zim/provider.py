"""ZIM provider and runtime.

This package is the reference provider implementation.  New providers should
implement :mod:`zimantic.providers.base` and must not import
``zimantic.providers.zim``; the ZIM provider may use shared contracts and
content helpers but the dependency direction never points back from core.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ...catalog import SourceCatalog, SourceRecord, catalog_path
from ...content.serving import MAX_ICON_BYTES
from ...content.store import (
    ArticleNotFoundError,
    ArticleUnavailableError,
)
from ...contracts import SourceInfo, SourceResult
from ...diagnostics import public_cause
from ...indexing.locator import IndexLocator
from ...media_types import ordered_media_types
from ...model_meta import (
    MODEL_PROBE_SHAPE_KEY,
    MODEL_PROBE_TEXTS_KEY,
    MODEL_PROBE_VECTORS_KEY,
)
from ...open_scope import OPEN_COLLECTION_KEY, OPEN_COLLECTION_LABEL, ZimOpenScope
from ...searching.config import _file_signature
from ...searching.index import IndexHandle
from ..base import (
    CollectionInfo,
    CollectionRuntime,
    CollectionSnapshot,
    ContentStore,
    DiscoveryBatch,
    EnrichingRuntime,
    IndexedRuntime,
    IsolatedFulltextRuntime,
    ProviderContext,
    SourceQuery,
)
from ..config import ConfigValidationError, ProviderConfig, ZimConfig
from . import search as zim_search
from .discovery import (
    ScanResult,
    ZimIndexOptions,
    discover_source_indexes,
    scan_collection,
    scan_scope_collection,
    zim_collection_key,
    zim_source_key,
)
from .store import ArchiveContentStore

logger = logging.getLogger(__name__)

class ZimRuntime(IndexedRuntime, EnrichingRuntime, IsolatedFulltextRuntime):
    """One configured ZIM root: catalog, archives, indexes, and serving."""

    def __init__(
        self,
        collection: CollectionInfo,
        config: ZimConfig,
        ctx: ProviderContext,
    ) -> None:
        self.collection = collection
        self.config = config
        self._ctx = ctx
        self._metadata_only = bool(ctx.metadata_only)
        self.index_dir = Path(ctx.index_dir)
        self.web_base_path = ctx.web_base_path
        self._logger = ctx.logger
        self._archive_factory = ctx.archive_factory
        self._open_scope: ZimOpenScope | None = getattr(ctx, "open_scope", None)
        self._open_entries: tuple[tuple[Path, str], ...] | None = None
        self._unsearchable_logged: set[str] = set()
        catalog_db = (
            catalog_path(self.index_dir)
            if (ctx.persist_catalog and ctx.archive_factory is not object)
            else None
        )
        try:
            self._catalog = SourceCatalog(catalog_db)
        except (OSError, sqlite3.Error) as error:
            if catalog_db is None:
                raise
            self._logger.warning(
                "zimantic: catalog unavailable for %s (%s); using an in-memory "
                "catalog for this collection",
                collection.label,
                public_cause(error),
            )
            logger.debug("ZIM catalog open error: %s", error)
            self._catalog = SourceCatalog(None)
        self._sources: tuple[SourceInfo, ...] = ()
        self._library_sources: tuple[SourceInfo, ...] = ()
        self._records: dict[str, SourceRecord] = {}
        self._unreadable: dict[str, str] = {}
        self.indexes: dict[str, IndexHandle] = {}
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._version = 1
        self._state = "loading"
        self._error: str | None = None
        self._closed = False
        self._last_refresh: float | None = None
        self._semantic = False
        self._defer_faiss = False
        self._model_fingerprint = ""
        self._embedder_getter: Callable[[], Any] | None = None
        self._embedder_ready_getter: Callable[[], Any] | None = None
        self._model_fingerprint_getter: Callable[[], str] | None = None
        self._probe_cache: dict[tuple[str, ...], Any] = {}
        self._probe_cache_lock = threading.Lock()
        self._fulltext_pool: Any | None = None
        self._preview_cache: Any | None = None
        self._candidate_count = 16
        self._preview_chars = 1000
        self._max_html_bytes = 512 << 10
        self._max_article_bytes = 1 << 30
        self._min_cosine_similarity = zim_search.DEFAULT_MIN_COSINE_SIMILARITY
        self._cfg: dict[str, Any] = {}

    # -- contracts ---------------------------------------------------------

    @property
    def snapshot(self) -> CollectionSnapshot:
        with self._lock:
            sources = self._library_sources
            indexes = list(self.indexes.values())
            indexed_values = [
                source.indexed_count
                for source in sources
                if source.indexed_count is not None
            ]
            body_values = [
                source.body_truncated_count
                for source in sources
                if source.body_truncated_count is not None
            ]
            return CollectionSnapshot(
                collection=self.collection,
                source_count=len(sources),
                media_types=ordered_media_types(
                    family for source in sources for family in source.media_types
                ),
                error=self._error,
                discovery_state=self._state,  # type: ignore[arg-type]
                snapshot_version=self._version,
                last_refresh=self._last_refresh,
                index_count=len(indexes),
                index_partial=any(
                    source.indexed_count is None or source.truncated is True
                    for source in sources
                ),
                index_states=tuple(
                    str(getattr(index, "done", "") or "") for index in indexes
                ),
                indexed_count=sum(indexed_values) if indexed_values else None,
                truncated=(
                    any(source.truncated is True for source in sources)
                    if sources
                    else None
                ),
                body_truncated_count=sum(body_values) if body_values else None,
            )

    @property
    def sources(self) -> Sequence[SourceInfo]:
        with self._lock:
            return self._sources

    def library_sources(self) -> Sequence[SourceInfo]:
        with self._lock:
            return self._library_sources

    def source_paths(self, source: SourceInfo) -> dict[str, Path]:
        record = self._record_for(source)
        return {"path": Path(record.path)} if record is not None and record.path else {}

    @property
    def catalog(self) -> SourceCatalog:
        return self._catalog

    def search(self, source: SourceInfo, request: SourceQuery) -> SourceResult:
        if self._closed:
            return SourceResult(
                source=source, error="ZIM collection is closed", status="error"
            )
        return zim_search.search(self, source, request, self._search_context(source))

    def suggest(
        self, source: SourceInfo, query: str, limit: int
    ) -> Sequence[Any]:
        if self._closed:
            return ()
        return zim_search.suggest(
            self, source, query, limit, self._search_context(source)
        )

    def preview(
        self,
        source: SourceInfo,
        document: Any,
        *,
        max_chars: int,
        max_bytes: int,
        deadline: float | None,
    ) -> tuple[str, str | None]:
        if self._closed:
            return "", document.get("mimetype")
        return zim_search.preview(
            self,
            source,
            dict(document),
            max_chars=max_chars,
            max_bytes=max_bytes,
            deadline=deadline,
            ctx=self._search_context(source),
        )

    def enrich(
        self,
        source: SourceInfo,
        result_docs: list[dict[str, Any]],
        source_docs: dict[str, dict[str, Any]],
        *,
        phrase_ids: set[str],
        phrase_words: list[str],
        max_html_bytes: int,
        max_article_bytes: int,
    ) -> bool:
        if self._closed:
            return False
        return zim_search.enrich(
            self,
            source,
            result_docs,
            source_docs,
            phrase_ids=phrase_ids,
            phrase_words=phrase_words,
            max_html_bytes=max_html_bytes,
            max_article_bytes=max_article_bytes,
        )

    def entry_producer(self, source: SourceInfo, *, fast: bool):
        """Return a bounded ZIM entry producer for the generic build pipeline."""
        from .entries import ArchiveEntryProducer

        record = self._record_for(source)
        if record is None or not record.path:
            raise ValueError("ZIM archive is unavailable for indexing")
        return ArchiveEntryProducer(
            source,
            record.path,
            fast=fast,
            cfg=self._cfg,
            max_entries=getattr(self.config, "max_entries", None),
            catalog_path=catalog_path(self.index_dir),
        )

    def content_store(self, source: SourceInfo) -> ContentStore | None:
        index = self.indexes.get(source.key)
        record = self._record_for(source)
        archive = None
        if index is not None:
            # Reuse the runtime index's already-open archive if it has one;
            # otherwise the store opens it lazily on its first read.
            archive = getattr(index, "archive", None)
        path = record.path if record is not None else None
        if index is None and not path:
            raise ArticleUnavailableError("source store is unavailable")
        return ArchiveContentStore(
            source,
            archive,
            path=path,
            archive_factory=self._archive_factory,
            index=index,
            icon_data=(
                None
                if record is None or record.icon_data == b"\x01"
                else record.icon_data
            ),
            icon_mime_type=record.icon_mime_type if record is not None else "",
            icon_path=record.icon_path if record is not None else "",
            max_article_bytes=self._max_article_bytes,
            max_icon_bytes=MAX_ICON_BYTES,
            owns_archive=index is None or archive is None,
        )

    def max_sources_per_search(self) -> int | None:
        return None

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._state in {"ready", "error", "closed"}

    @property
    def discovery_state(self) -> str:
        return self._state

    @property
    def snapshot_version(self) -> int:
        return self._version

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._state = "closed"
            indexes = list(self.indexes.values())
            self.indexes.clear()
        for index in indexes:
            index.close()
        self._fulltext_pool = None
        try:
            self._catalog.close()
        except Exception:
            pass

    # -- Search wiring -----------------------------------------------------

    def bind_search(
        self,
        *,
        semantic: bool,
        defer_faiss: bool,
        embedder_getter: Callable[[], Any] | None = None,
        embedder_ready_getter: Callable[[], Any] | None = None,
        model_fingerprint_getter: Callable[[], str] | None = None,
        preview_cache: Any | None = None,
        fulltext_pool: Any | None = None,
        candidate_count: int | None = None,
        preview_chars: int | None = None,
        max_html_bytes: int | None = None,
        max_article_bytes: int | None = None,
        min_cosine_similarity: float | None = None,
        cfg: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._semantic = bool(semantic)
            self._defer_faiss = bool(defer_faiss)
            if embedder_getter is not None:
                self._embedder_getter = embedder_getter
            if embedder_ready_getter is not None:
                self._embedder_ready_getter = embedder_ready_getter
            if model_fingerprint_getter is not None:
                self._model_fingerprint_getter = model_fingerprint_getter
            if preview_cache is not None:
                self._preview_cache = preview_cache
            if fulltext_pool is not None:
                self._fulltext_pool = fulltext_pool
            if candidate_count is not None:
                self._candidate_count = candidate_count
            if preview_chars is not None:
                self._preview_chars = preview_chars
            if max_html_bytes is not None:
                self._max_html_bytes = max_html_bytes
            if max_article_bytes is not None:
                self._max_article_bytes = max_article_bytes
            if min_cosine_similarity is not None:
                self._min_cosine_similarity = min_cosine_similarity
            if cfg is not None:
                self._cfg = dict(cfg)

    def _search_context(
        self, source: SourceInfo | None = None
    ) -> zim_search.ZimSearchContext:
        from ...scoped_settings import search_limits

        return search_limits(
            zim_search.ZimSearchContext(
                candidate_count=self._candidate_count,
                preview_chars=self._preview_chars,
                max_html_bytes=self._max_html_bytes,
                max_article_bytes=self._max_article_bytes,
                max_entry_bytes=self._max_article_bytes,
                min_cosine_similarity=self._min_cosine_similarity,
                web_base_path=self.web_base_path,
                fulltext_pool=self._fulltext_pool,
                preview_cache=self._preview_cache,
            ),
            self._cfg,
            source,
            self.config,
        )

    def set_fulltext_pool(self, pool: Any | None) -> None:
        with self._lock:
            self._fulltext_pool = pool

    def start_faiss(self) -> None:
        """Load deferred FAISS vectors for this runtime's indexes."""
        # The core calls bind_search(defer_faiss=False) before this method, so
        # gating on _defer_faiss would skip the load entirely.
        if not self._semantic:
            return
        try:
            import faiss  # noqa: F401
        except Exception as error:
            self._logger.warning(
                "zimantic: semantic search disabled: FAISS unavailable (%s); "
                "falling back to title and full-text (lexical) search",
                public_cause(error),
            )
            logger.debug("ZIM FAISS load error: %s", error)
            return
        getter = self._model_fingerprint_getter
        if getter is not None:
            try:
                self._model_fingerprint = getter()
            except Exception as error:
                self._logger.warning(
                    "zimantic: could not fingerprint the embedding model (%s); "
                    "vectors will not be checked against it",
                    public_cause(error),
                )
                logger.debug("ZIM model fingerprint error: %s", error)
        with self._lock:
            self._defer_faiss = False
        try:
            self._reopen_indexes()
        except Exception as error:
            self._logger.warning(
                "zimantic: semantic search disabled: FAISS indexes unavailable "
                "(%s); falling back to title and full-text (lexical) search",
                public_cause(error),
            )
            self._logger.debug("ZIM FAISS reopen error: %s", error)

    # -- discovery / lifecycle --------------------------------------------

    def refresh(self, *, wait: bool = True) -> bool:
        if wait:
            return self._refresh_once(wait=True)
        threading.Thread(
            target=self._refresh_once,
            kwargs={"wait": False},
            name=f"zimantic-zim-refresh-{self.collection.key}",
            daemon=True,
        ).start()
        return True

    def _refresh_once(self, *, wait: bool) -> bool:
        if not self._refresh_lock.acquire(blocking=wait):
            return False
        try:
            if self._closed:
                return False
            try:
                if self._open_entries is not None:
                    scan = scan_scope_collection(
                        self._catalog,
                        list(self._open_entries),
                        collection_key=self.collection.key,
                        archive_factory=self._archive_factory,
                    )
                else:
                    scan = scan_collection(
                        self._catalog,
                        self.config,
                        collection_key=self.collection.key,
                        archive_factory=self._archive_factory,
                        force=False,
                    )
            except Exception as error:
                with self._lock:
                    self._error = f"ZIM catalog scan failed: {public_cause(error)}"
                    self._state = "error"
                    self._last_refresh = time.time()
                self._logger.error(
                    "zimantic: ZIM catalog scan failed (%s)",
                    public_cause(error),
                )
                self._logger.debug("ZIM catalog scan error: %s", error)
                return False
            if not scan.complete:
                with self._lock:
                    self._error = "; ".join(scan.errors) or "ZIM discovery incomplete"
                    self._state = "error"
                    self._last_refresh = time.time()
                for message in scan.errors:
                    self._logger.warning(
                        "zimantic: collection %s: %s",
                        self.collection.label,
                        message,
                    )
                # Retain the last complete snapshot.  On first discovery with no
                # snapshot, expose catalog rows for the library but not search.
                if not self._library_sources:
                    self._publish(scan, reopen=False)
                return False
            self._publish(scan, reopen=True)
            return True
        finally:
            self._refresh_lock.release()

    def _publish(self, scan: ScanResult, *, reopen: bool) -> None:
        records = sorted(
            scan.records.values(),
            key=lambda record: (record.path or "").casefold(),
        )
        options = ZimIndexOptions(
            semantic=self._semantic,
            defer_faiss=self._defer_faiss or self._metadata_only,
            model_fingerprint=self._model_fingerprint,
            cfg=self._cfg,
            probe_compatible=self._probe_compatible,
            read_only=self._open_scope is not None,
        )
        order = (
            {
                key: position
                for position, key in enumerate(self._open_scope.keys)
            }
            if self._open_scope is not None
            else None
        )
        sources, library_sources, discovered = discover_source_indexes(
            records,
            collection_key=self.collection.key,
            collection_label=self.collection.label,
            index_dir=self.index_dir,
            web_base_path=self.web_base_path,
            options=options,
            archive_factory=self._archive_factory,
            unreadable=scan.unreadable,
            order=order,
            metadata_only=self._metadata_only,
        )
        with self._lock:
            by_key = {source.key: source for source in library_sources}
            installed: dict[str, IndexHandle] = {}
            discarded: list[IndexHandle] = []
            for key, fresh in discovered.items():
                source = by_key.get(key)
                record = self._record_by_key(scan, key)
                existing = self.indexes.get(key)
                if (
                    not self._metadata_only
                    and existing is not None
                    and source is not None
                    and self._can_reuse_index(existing, record, source)
                ):
                    # A locator-only change keeps the index and its caches but
                    # must drop the archive handle opened from the old path.
                    if record is not None and record.path:
                        try:
                            signature = _file_signature(Path(record.path))
                        except OSError:
                            signature = None
                        existing.refresh_locator(record.path, signature)
                    installed[key] = existing
                    discarded.append(fresh)
                else:
                    installed[key] = fresh
            for key, old in self.indexes.items():
                if key not in installed:
                    discarded.append(old)
            # A fresh index may replace an old one under the same key.
            for key, old in self.indexes.items():
                if key in installed and installed[key] is not old:
                    discarded.append(old)
            changed = (
                tuple(sources) != self._sources
                or tuple(library_sources) != self._library_sources
                or installed.keys() != self.indexes.keys()
                or any(
                    installed[key] is not self.indexes.get(key)
                    for key in installed
                )
            )
            self.indexes = installed
            self._sources = tuple(sources)
            self._library_sources = tuple(library_sources)
            self._records = {
                record.key: record for record in scan.records.values()
            }
            self._unreadable = dict(scan.unreadable)
            self._last_refresh = time.time()
            if scan.complete:
                # An incomplete first scan still publishes its catalog rows for
                # the library, but it must not erase the error that explains
                # why the collection is unavailable.
                self._error = None
                self._state = "ready"
            if changed:
                self._version += 1
        for index in discarded:
            try:
                index.close()
            except Exception:
                pass
        if self._open_scope is not None:
            for source in library_sources:
                if source.key in discovered or source.key in self._unsearchable_logged:
                    continue
                self._unsearchable_logged.add(source.key)
                self._logger.warning(
                    "zimantic: %s has no finished index and no native full-text "
                    "index; it is readable but not searchable",
                    source.key,
                )
        if reopen:
            self._logger.info(
                "zimantic: ZIM collection %s ready (%d source(s))",
                self.collection.label,
                len(sources),
            )

    @staticmethod
    def _record_by_key(scan: ScanResult, key: str) -> SourceRecord | None:
        for record in scan.records.values():
            if record.key == key:
                return record
        return None

    def _can_reuse_index(
        self, index: IndexHandle, record: SourceRecord | None, source: SourceInfo
    ) -> bool:
        if record is None or record.path is None:
            return False
        if (
            index.source_size is not None
            and record.size is not None
            and index.source_size != record.size
        ):
            return False
        if index.zim_signature is not None:
            try:
                current_signature = _file_signature(Path(record.path))
            except OSError:
                return False
            if index.zim_signature != current_signature:
                return False
        if index.generation is not None:
            try:
                lease = IndexLocator(self.index_dir).paths(
                    source, verify_faiss=False
                )
            except Exception:
                return False
            if lease is None:
                return False
            same = lease.generation == index.generation
            lease.close()
            return same
        if index.zim_path != record.path:
            return False
        try:
            return index.zim_signature == _file_signature(Path(record.path))
        except OSError:
            return index.zim_signature is None

    def _reopen_indexes(self) -> None:
        with self._lock:
            records = list(self._records.values())
            unreadable = dict(self._unreadable)
            old = dict(self.indexes)
            self.indexes = {}
        scan = ScanResult(complete=True, records={
            record.path or record.key: record for record in records
        }, unreadable=unreadable)
        try:
            self._publish(scan, reopen=False)
        finally:
            with self._lock:
                live = {id(index) for index in self.indexes.values()}
            for index in old.values():
                if id(index) not in live:
                    try:
                        index.close()
                    except Exception:
                        pass

    def _probe_compatible(
        self, db: sqlite3.Connection, db_path: Path
    ) -> tuple[bool, str]:
        """Compare an index's stored probes with the current embedding model."""
        rows = dict(
            db.execute(
                "SELECT key, value FROM meta WHERE key IN (?, ?, ?)",
                (
                    MODEL_PROBE_TEXTS_KEY,
                    MODEL_PROBE_VECTORS_KEY,
                    MODEL_PROBE_SHAPE_KEY,
                ),
            ).fetchall()
        )
        if not rows:
            return False, "no compatibility probes"
        ready_getter = getattr(self, "_embedder_ready_getter", None)
        if ready_getter is not None:
            state = ready_getter()
            if (
                isinstance(state, tuple)
                and len(state) == 2
                and state[1]
                and state[0] is not None
            ):
                if not state[0].wait(timeout=60):
                    return False, "timed out waiting for the embedding model"
        getter = self._embedder_getter
        embedder = getter() if getter is not None else None
        if embedder is None:
            return False, "embedding model is unavailable"
        from ...embed import embed_probe_texts, load_model_probes, model_probes_match

        probes = load_model_probes(rows)
        if probes is None:
            return False, "unreadable compatibility probes"
        texts, stored_vectors = probes
        key = tuple(texts)
        # Hold the cache lock across the embedding so concurrent index opens
        # share one probe computation instead of racing to repeat it.
        with self._probe_cache_lock:
            current = self._probe_cache.get(key)
            if current is None:
                try:
                    current = embed_probe_texts(embedder, texts)
                except Exception as error:
                    return False, f"probe embedding failed: {error}"
                self._probe_cache[key] = current
        try:
            compatible, mean_cosine, min_cosine = model_probes_match(
                texts, stored_vectors, current=current
            )
        except Exception as error:
            return False, f"probe comparison failed: {error}"
        return compatible, (
            f"mean cosine {mean_cosine:.4f}, minimum {min_cosine:.4f}"
        )

    # -- content serving ---------------------------------------------------

    def _record_for(self, source: SourceInfo) -> SourceRecord | None:
        with self._lock:
            record = self._records.get(source.key)
        if record is not None:
            return record
        return self._catalog.get_key(source.key)

    def random_source(self) -> SourceInfo:
        with self._lock:
            candidates = [
                source
                for source in self._library_sources
                if source.homepage
            ]
        if not candidates:
            raise ArticleNotFoundError("no local source has a homepage")
        return secrets.choice(candidates)

    # -- diagnostics -------------------------------------------------------

    def health(self) -> list[dict[str, Any]]:
        with self._lock:
            entries = sorted(self.indexes.items())
            records = dict(self._records)
        reports: list[dict[str, Any]] = []
        for key, index in entries:
            reasons: list[str] = []
            record = records.get(key)
            archive_path = (
                Path(index.zim_path)
                if index.zim_path
                else Path(record.path) if record is not None and record.path else None
            )
            archive_current = archive_path is not None and archive_path.exists()
            if not archive_current:
                reasons.append("ZIM archive unavailable")
            vectors_expected = (
                self._semantic and index.done == "full" and not self._defer_faiss
            )
            vectors_loaded = index.faiss_index is not None
            if vectors_expected and not vectors_loaded:
                reasons.append("semantic vectors unavailable")
            fulltext_capability = (
                bool(record.native_fulltext_available)
                if record is not None
                and record.native_fulltext_available is not None
                else index.ensure_searcher() is not None
            )
            report: dict[str, Any] = {
                "name": key,
                "status": "ok" if not reasons else "degraded",
                "index_file": index.db_path is not None,
                "archive": archive_current,
                "done": str(getattr(index, "done", "") or ""),
                "fast": str(getattr(index, "done", "") or "") == "fast",
                "capabilities": {
                    "title": index.db_path is not None,
                    "fulltext": fulltext_capability,
                    "semantic": vectors_loaded,
                },
            }
            if reasons:
                report["issues"] = reasons
            if record is not None:
                report["entry_count"] = record.item_count
                report["homepage"] = record.homepage or None
                report["article_count"] = record.item_count
                report["media_count"] = 0
            meta = getattr(index, "index_meta", None) or {}
            if meta:
                from ...indexing import schema

                try:
                    if schema.INDEXED_COUNT in meta:
                        report["indexed_count"] = schema.decode_int(
                            meta[schema.INDEXED_COUNT]
                        )
                    if schema.TRUNCATED in meta:
                        report["truncated"] = schema.decode_bool(
                            meta[schema.TRUNCATED]
                        )
                    if schema.BODY_TRUNCATED_COUNT in meta:
                        report["body_truncated_count"] = schema.decode_int(
                            meta[schema.BODY_TRUNCATED_COUNT]
                        )
                    if schema.DERIVED_TEXT in meta:
                        report["derived_text"] = schema.decode_json(
                            meta[schema.DERIVED_TEXT]
                        )
                except (KeyError, TypeError, ValueError):
                    pass
            reports.append(report)
        return reports

    def index_names(self) -> list[str]:
        with self._lock:
            return sorted(self.indexes)


class ZimDiscoveryBatch(DiscoveryBatch):
    def __init__(
        self,
        collection: CollectionInfo,
        config: ZimConfig,
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
        runtime = ZimRuntime(collection, self._config, self._ctx)
        runtime.refresh(wait=True)
        return runtime

    def close(self) -> None:
        self._closed = True


def _scope_collection_key(source_key: str) -> str:
    return str(source_key).rsplit(":", 1)[0] or OPEN_COLLECTION_KEY


def _scope_collection_label(
    collection_key: str, cfg: dict[str, Any]
) -> str:
    if collection_key == OPEN_COLLECTION_KEY:
        return OPEN_COLLECTION_LABEL
    name = collection_key.split(":", 1)[-1]
    for entry in cfg.get("collections") or ():
        if getattr(entry, "kind", None) != "zim":
            continue
        if getattr(entry, "name", None) == name:
            return getattr(entry, "label", None) or name
    return name or collection_key


class ZimOpenRuntime(ZimRuntime):
    """A read-only ZIM runtime over an explicit, ordered archive selection."""

    def __init__(
        self,
        collection: CollectionInfo,
        entries: list[tuple[Path, str]],
        ctx: ProviderContext,
    ) -> None:
        config = SimpleNamespace(
            name=collection.key.split(":", 1)[-1] or "open",
            path=str(entries[0][0].parent) if entries else ".",
            label=collection.label,
            max_entries=None,
        )
        super().__init__(collection, config, ctx)  # type: ignore[arg-type]
        self._open_entries = tuple(entries)


class ZimOpenDiscoveryBatch(DiscoveryBatch):
    """One discovery batch containing an open process's ordered ZIM groups."""

    def __init__(
        self,
        groups: list[tuple[CollectionInfo, list[tuple[Path, str]]]],
        ctx: ProviderContext,
    ) -> None:
        self._groups = groups
        self._ctx = ctx
        self._closed = False

    @property
    def collections(self) -> Sequence[CollectionInfo]:
        return tuple(collection for collection, _entries in self._groups)

    def open(self, collection: CollectionInfo) -> CollectionRuntime:
        if self._closed:
            raise RuntimeError("discovery batch is closed")
        for candidate, entries in self._groups:
            if candidate.key != collection.key:
                continue
            runtime = ZimOpenRuntime(candidate, entries, self._ctx)
            runtime.refresh(wait=True)
            return runtime
        raise KeyError(collection.key)

    def close(self) -> None:
        self._closed = True


def open_zim_scope_batch(
    scope: ZimOpenScope,
    ctx: ProviderContext,
    cfg: dict[str, Any],
) -> ZimOpenDiscoveryBatch:
    """Group an open scope by canonical collection, preserving CLI order."""
    grouped: dict[str, list[tuple[Path, str]]] = {}
    for path, key in zip(scope.paths, scope.keys):
        grouped.setdefault(_scope_collection_key(key), []).append((path, key))
    groups: list[tuple[CollectionInfo, list[tuple[Path, str]]]] = []
    for collection_key, entries in grouped.items():
        label = _scope_collection_label(collection_key, cfg)
        groups.append(
            (
                CollectionInfo(
                    key=collection_key,
                    provider="zim",
                    label=label,
                    title=label,
                ),
                entries,
            )
        )
    return ZimOpenDiscoveryBatch(groups, ctx)


class ZimProvider:
    kind = "zim"

    def discover(
        self, config: ProviderConfig, ctx: ProviderContext
    ) -> DiscoveryBatch:
        if not isinstance(config, ZimConfig):
            raise ConfigValidationError("zim provider requires a ZimConfig entry")
        label = config.label or config.name
        collection = CollectionInfo(
            key=zim_collection_key(config.name),
            provider="zim",
            label=label,
            title=label,
        )
        return ZimDiscoveryBatch(collection, config, ctx)


__all__ = [
    "ZimDiscoveryBatch",
    "ZimOpenDiscoveryBatch",
    "ZimOpenRuntime",
    "ZimProvider",
    "ZimRuntime",
    "open_zim_scope_batch",
    "zim_collection_key",
    "zim_source_key",
]
