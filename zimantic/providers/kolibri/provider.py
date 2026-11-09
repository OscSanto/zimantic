"""Kolibri provider and per-channel runtimes.

First the provider creates one discovery batch for a configured installation.
The batch owns a private consistent snapshot; each installed channel opens as a
collection runtime that borrows the snapshot.  Runtimes discover sources, open
published generations through the provider-neutral locator, and serve content
through :class:`KolibriContentStore`.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ...content.reader import (
    extract_excerpt,
    extract_markdown_document,
    extract_pdf_excerpt,
    extract_plain_text_document,
    truncate_at_word_boundary,
)
from ...content.store import (
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
)
from ...contracts import SourceInfo, SourceResult
from ...diagnostics import public_cause
from ...indexing import search as generic_search
from ...indexing.derived import derived_text_preview
from ...indexing.locator import IndexLocator
from ..base import (
    CollectionInfo,
    CollectionRuntime,
    CollectionSnapshot,
    ContentStore,
    DiscoveryBatch,
    IndexedRuntime,
    ProviderContext,
    SourceQuery,
)
from ..config import ConfigValidationError, KolibriConfig, ProviderConfig
from ..files.discovery import (
    FilesIndexOptions,
    apply_index_metadata,
    open_files_index,
)
from .discovery import (
    ChannelRow,
    KolibriSource,
    discover_channel_sources,
    kolibri_collection_key,
    load_channel,
    load_channels,
)
from .entries import KolibriEntryProducer
from .snapshot import SnapshotError, SnapshotManager, database_signature
from .store import KolibriContentStore

logger = logging.getLogger(__name__)

_PHRASE_MIMETYPES = frozenset(
    {"text/plain", "text/html", "application/xhtml+xml"}
)
_MEDIA_PREFIXES = ("image/", "audio/", "video/")


def _settings(ctx: ProviderContext) -> dict[str, Any]:
    return dict(getattr(ctx.config, "settings", None) or {})


def _int_setting(settings: dict[str, Any], key: str, default: int) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return int(default)
    return int(value)


def _float_setting(settings: dict[str, Any], key: str, default: float) -> float:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(default)
    return float(value)


def _paths(config: KolibriConfig) -> tuple[Path, Path, Path]:
    home = Path(config.path).expanduser().resolve()
    database = (
        Path(config.database_path).expanduser().resolve()
        if config.database_path
        else home / "db.sqlite3"
    )
    storage = (
        Path(config.storage).expanduser().resolve()
        if config.storage
        else home / "content" / "storage"
    )
    return home, database, storage


class KolibriRuntime(IndexedRuntime):
    """One installed channel: sources, indexes, and content serving."""

    def __init__(
        self,
        collection: CollectionInfo,
        channel: ChannelRow,
        config: KolibriConfig,
        ctx: ProviderContext,
        snapshot: Any,
        manager: SnapshotManager,
    ) -> None:
        self.collection = collection
        self.channel = channel
        self.config = config
        self._ctx = ctx
        self._snapshot = snapshot
        self._manager = manager
        _home, _database, storage = _paths(config)
        self.storage_root = storage
        self.index_dir = Path(ctx.index_dir)
        self.web_base_path = ctx.web_base_path
        self._logger = ctx.logger
        settings = _settings(ctx)
        self._cfg: dict[str, Any] = settings
        self._library: tuple[KolibriSource, ...] = ()
        self._searchable: tuple[KolibriSource, ...] = ()
        self._by_key: dict[str, KolibriSource] = {}
        self.indexes: dict[str, Any] = {}
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
        self._candidate_count = _int_setting(settings, "candidate_count", 16)
        self._preview_chars = _int_setting(settings, "max_preview_chars", 1000)
        self._max_html_bytes = _int_setting(settings, "max_html_bytes", 512 << 10)
        self._max_article_bytes = _int_setting(
            settings, "max_article_bytes", 1 << 30
        )
        self._min_cosine_similarity = _float_setting(
            settings,
            "min_cosine_similarity",
            generic_search.DEFAULT_MIN_COSINE_SIMILARITY,
        )
        self._stale_logged: set[str] = set()
        self._was_collapsed = False

    @property
    def snapshot(self) -> CollectionSnapshot:
        with self._lock:
            sources = self._library
            indexes = list(self.indexes.values())
            indexed_values = [
                source.info.indexed_count
                for source in sources
                if source.info.indexed_count is not None
            ]
            body_values = [
                source.info.body_truncated_count
                for source in sources
                if source.info.body_truncated_count is not None
            ]
            return CollectionSnapshot(
                collection=self.collection,
                source_count=len(sources),
                media_types=tuple(
                    family
                    for source in sources
                    for family in source.info.media_types
                ),
                error=self._error,
                discovery_state=self._state,  # type: ignore[arg-type]
                snapshot_version=self._version,
                last_refresh=self._last_refresh,
                collapsed=self._was_collapsed,
                index_count=len(indexes),
                index_partial=any(
                    source.info.indexed_count is None
                    or source.info.index_partial is True
                    or source.info.truncated is True
                    for source in sources
                ),
                index_states=tuple(
                    str(getattr(index, "done", "") or "") for index in indexes
                ),
                indexed_count=sum(indexed_values) if indexed_values else None,
                truncated=(
                    any(source.info.truncated is True for source in sources)
                    if sources
                    else None
                ),
                body_truncated_count=sum(body_values) if body_values else None,
            )

    @property
    def sources(self) -> Sequence[SourceInfo]:
        with self._lock:
            return tuple(source.info for source in self._searchable)

    def library_sources(self) -> Sequence[SourceInfo]:
        with self._lock:
            return tuple(source.info for source in self._library)

    def source_paths(self, source: SourceInfo) -> dict[str, Path]:
        with self._lock:
            if source.key not in self._by_key:
                return {}
        home, database, storage = _paths(self.config)
        return {"path": home, "database_path": database, "storage": storage}

    def search(self, source: SourceInfo, request: SourceQuery) -> SourceResult:
        if self._closed:
            return SourceResult(
                source=source, error="kolibri collection is closed", status="error"
            )
        index = self.indexes.get(source.key)
        if index is None:
            return SourceResult(
                source=source, error="source is not indexed", status="error"
            )
        try:
            index.acquire()
        except RuntimeError as error:
            return SourceResult(source, error=str(error), status="cancelled")
        try:
            return generic_search.search(
                source,
                index,
                request,
                limits=self._search_limits(source),
                url_builder=generic_search.default_url_builder(
                    self.web_base_path
                ),
            )
        finally:
            index.release()

    def suggest(
        self, source: SourceInfo, query: str, limit: int
    ) -> Sequence[Any]:
        if self._closed:
            return ()
        index = self.indexes.get(source.key)
        if index is None:
            return ()
        try:
            index.acquire()
        except RuntimeError:
            return ()
        try:
            return generic_search.suggest(
                source,
                index,
                query,
                limit,
                url_builder=generic_search.default_url_builder(
                    self.web_base_path
                ),
                web_base_path=self.web_base_path,
            )
        finally:
            index.release()

    def _derived_preview(self, source: SourceInfo, path: str) -> str:
        with self._lock:
            index = self.indexes.get(source.key)
        if index is None:
            return ""
        try:
            source_directory = IndexLocator(self.index_dir).source_directory(
                source.provider, source.collection_key, source.key
            )
        except Exception:
            return ""
        return derived_text_preview(
            index, source_directory / "derived-cache", path
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
        lead = str(document.get("lead") or "")
        mimetype = document.get("mimetype")
        path = str(document.get("path") or "")
        if not path or (deadline is not None and time.monotonic() >= deadline):
            return lead, mimetype
        try:
            store = self.content_store(source)
        except (ArticleNotFoundError, ArticleUnavailableError):
            return lead, mimetype
        try:
            try:
                entry = store.resolve(path)
            except (ArticleNotFoundError, ArticleUnavailableError, OSError, ValueError):
                return lead, mimetype
            mimetype = entry.mimetype
            if mimetype.startswith(_MEDIA_PREFIXES):
                derived = self._derived_preview(source, path)
                if derived:
                    return truncate_at_word_boundary(derived, max_chars), mimetype
                return lead, mimetype
            if mimetype == "application/pdf":
                data = store.read(
                    path,
                    head_only=False,
                    max_bytes=min(
                        self._max_article_bytes, max(int(max_bytes), 1 << 20)
                    ),
                )
                text = extract_pdf_excerpt(data.content, max_chars)
            else:
                data = store.read(
                    path,
                    head_only=True,
                    max_bytes=min(
                        self._max_article_bytes, max(int(max_bytes), 64 << 10)
                    ),
                )
                if mimetype in {"text/html", "application/xhtml+xml"}:
                    text = extract_excerpt(data.content, max_chars)
                elif mimetype == "text/markdown":
                    _title, text, _body, _truncated = extract_markdown_document(
                        data.content, max_chars, max_chars
                    )
                elif mimetype == "text/plain":
                    text, _body, _truncated = extract_plain_text_document(
                        data.content, max_chars, max_chars
                    )
                else:
                    text = ""
            return truncate_at_word_boundary(text, max_chars), mimetype
        except (
            ArticleNotFoundError,
            ArticleTooLargeError,
            ArticleUnavailableError,
            OSError,
            ValueError,
        ):
            return lead, mimetype
        finally:
            store.close()

    def content_store(self, source: SourceInfo) -> ContentStore | None:
        with self._lock:
            kolibri_source = self._by_key.get(source.key)
            index = self.indexes.get(source.key)
        if kolibri_source is None:
            raise ArticleUnavailableError("source is unavailable")
        return KolibriContentStore(
            kolibri_source,
            self._snapshot,
            storage_root=self.storage_root,
            channel=kolibri_source.channel,
            scope=kolibri_source.scope,
            config=self.config,
            max_article_bytes=self._max_article_bytes,
            index=index,
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
            if self._closed:
                return
            self._closed = True
            self._state = "closed"
            indexes = list(self.indexes.values())
            self.indexes = {}
            self._library = ()
            self._searchable = ()
            self._by_key = {}
        for index in indexes:
            try:
                index.close()
            except Exception:
                pass
        try:
            self._snapshot.close()
        except Exception:
            pass

    # -- search wiring -----------------------------------------------------

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

    def _search_limits(
        self, source: SourceInfo | None = None
    ) -> generic_search.SearchLimits:
        from ...scoped_settings import search_limits

        return search_limits(
            generic_search.SearchLimits(
                candidate_count=self._candidate_count,
                preview_chars=self._preview_chars,
                max_html_bytes=self._max_html_bytes,
                max_article_bytes=self._max_article_bytes,
                min_cosine_similarity=self._min_cosine_similarity,
                web_base_path=self.web_base_path,
            ),
            self._cfg,
            source,
            self.config,
        )

    def start_faiss(self) -> None:
        # bind_search(defer_faiss=False) runs before this, so do not gate on it.
        if not self._semantic:
            return
        try:
            import faiss  # noqa: F401
        except Exception as error:
            self._logger.warning(
                "zimantic: semantic search disabled: FAISS unavailable (%s); "
                "falling back to lexical search",
                public_cause(error),
            )
            logger.debug("Kolibri FAISS load error: %s", error)
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
                logger.debug("Kolibri model fingerprint error: %s", error)
        with self._lock:
            self._defer_faiss = False
        try:
            self._reopen_indexes()
        except Exception as error:
            self._logger.warning(
                "zimantic: semantic search disabled for %s (%s); falling back "
                "to lexical search",
                self.collection.label,
                public_cause(error),
            )
            self._logger.debug("Kolibri FAISS reopen error: %s", error)

    # -- discovery / lifecycle --------------------------------------------

    def refresh(self, *, wait: bool = True) -> bool:
        if wait:
            return self._refresh_once(wait=True)
        threading.Thread(
            target=self._refresh_once,
            kwargs={"wait": False},
            name=f"zimantic-kolibri-refresh-{self.collection.key}",
            daemon=True,
        ).start()
        return True

    def _refresh_once(self, *, wait: bool) -> bool:
        if not self._refresh_lock.acquire(blocking=wait):
            return False
        replacement = None
        try:
            if self._closed:
                return False
            try:
                signature = database_signature(self._manager.database_path)
                if signature != self._snapshot.source_signature:
                    replacement = self._manager.acquire()
            except (OSError, SnapshotError) as error:
                with self._lock:
                    if self._library:
                        self._error = f"Kolibri refresh failed: {error}"
                        return False
                raise
            snapshot = replacement or self._snapshot
            db = snapshot.connect()
            try:
                discovery = discover_channel_sources(
                    db,
                    self.config,
                    self.channel,
                    web_base_path=self.web_base_path,
                )
            except Exception as error:
                discovery = None
                message = f"Kolibri channel discovery failed: {public_cause(error)}"
                self._logger.debug("Kolibri channel discovery error: %s", error)
            finally:
                db.close()
            if discovery is None and replacement is not None:
                # A failed fresh scan retains the last complete published batch.
                with self._lock:
                    if self._library:
                        self._error = message
                        self._last_refresh = time.time()
                        return False
            if discovery is None or (not discovery.sources and discovery.error):
                message = (
                    discovery.error
                    if discovery is not None and discovery.error
                    else message
                )
                with self._lock:
                    if self._library:
                        self._error = message
                        self._last_refresh = time.time()
                        return False
                    old = list(self.indexes.values())
                    self.indexes = {}
                    self._library = ()
                    self._searchable = ()
                    self._by_key = {}
                    self._error = message
                    self._state = "error"
                    self._last_refresh = time.time()
                    self._version += 1
                for index in old:
                    try:
                        index.close()
                    except Exception:
                        pass
                self._logger.warning(
                    "zimantic: Kolibri collection %s: %s",
                    self.collection.label,
                    message,
                )
                return False
            if self._was_collapsed and not any(
                source.scope.kind == "collapsed" for source in discovery.sources
            ):
                self._logger.warning(
                    "zimantic: Kolibri collection %s is no longer collapsed; "
                    "source keys changed and a rebuild is required",
                    self.collection.label,
                )
            self._was_collapsed = any(
                source.scope.kind == "collapsed" for source in discovery.sources
            )
            options = FilesIndexOptions(
                semantic=self._semantic,
                defer_faiss=self._defer_faiss,
                model_fingerprint=self._model_fingerprint,
                cfg=self._cfg,
            )
            new_indexes: dict[str, Any] = {}
            searchable: list[KolibriSource] = []
            library: list[KolibriSource] = []
            discarded: list[Any] = []
            for kolibri_source in discovery.sources:
                info = kolibri_source.info
                existing = self.indexes.get(info.key)
                index = None
                if existing is not None and self._can_reuse_index(existing, info):
                    index = existing
                else:
                    try:
                        index = open_files_index(
                            info, index_dir=self.index_dir, options=options
                        )
                    except Exception as error:
                        self._logger.warning(
                            "%s: index open failed (%s); serving content without "
                            "an index until it is rebuilt",
                            info.key,
                            public_cause(error),
                        )
                        self._logger.debug(
                            "Kolibri index open error for %s: %s", info.key, error
                        )
                        index = None
                if index is not None:
                    object.__setattr__(info, "searchable", True)
                    apply_index_metadata(info, index.index_meta)
                    new_indexes[info.key] = index
                    searchable.append(kolibri_source)
                else:
                    object.__setattr__(info, "searchable", False)
                library.append(kolibri_source)
            for key, old in self.indexes.items():
                index_replacement = new_indexes.get(key)
                if index_replacement is not old:
                    discarded.append(old)
            old_snapshot = None
            with self._lock:
                changed = (
                    tuple(source.info for source in library)
                    != tuple(source.info for source in self._library)
                    or set(new_indexes) != set(self.indexes)
                    or any(
                        new_indexes[key] is not self.indexes.get(key)
                        for key in new_indexes
                    )
                )
                if replacement is not None:
                    old_snapshot = self._snapshot
                    self._snapshot = replacement
                    replacement = None
                self.indexes = new_indexes
                self._library = tuple(library)
                self._searchable = tuple(searchable)
                self._by_key = {
                    source.info.key: source for source in self._library
                }
                self._error = discovery.error
                self._state = "ready"
                self._last_refresh = time.time()
                if changed:
                    self._version += 1
            if old_snapshot is not None:
                old_snapshot.release()
            for index in discarded:
                try:
                    index.close()
                except Exception:
                    pass
            if discovery.error:
                self._logger.warning(
                    "zimantic: Kolibri collection %s: %s",
                    self.collection.label,
                    discovery.error,
                )
            self._report_stale_overrides()
            return True
        finally:
            if replacement is not None:
                try:
                    replacement.release()
                except Exception:
                    pass
            self._refresh_lock.release()

    def _can_reuse_index(self, index: Any, source: SourceInfo) -> bool:
        if getattr(index, "generation", None) is None:
            return False
        try:
            lease = IndexLocator(self.index_dir).paths(
                source, verify_faiss=False
            )
        except Exception:
            return False
        if lease is None:
            return False
        try:
            return lease.generation == index.generation
        finally:
            lease.close()

    def _reopen_indexes(self) -> None:
        with self._lock:
            old = dict(self.indexes)
            self.indexes = {}
        try:
            self._refresh_once(wait=True)
        finally:
            with self._lock:
                live = {id(index) for index in self.indexes.values()}
            for index in old.values():
                if id(index) not in live:
                    try:
                        index.close()
                    except Exception:
                        pass

    def _report_stale_overrides(self) -> None:
        operations = self._cfg.get("derived_source_operations") or {}
        prefix = f"kolibri:{self.config.name}:"
        with self._lock:
            known = set(self._by_key)
        for key in operations:
            if not str(key).startswith(prefix) or key in known:
                continue
            if key in self._stale_logged:
                continue
            self._stale_logged.add(key)
            self._logger.warning(
                "zimantic: Kolibri collection %s has a stale source override %r; "
                "it was not applied (source keys change after collapse or "
                "recovery; rebuild and update the key)",
                self.collection.label,
                key,
            )

    # -- build / content ---------------------------------------------------

    def entry_producer(self, source: SourceInfo, *, fast: bool):
        with self._lock:
            kolibri_source = self._by_key.get(source.key)
        if kolibri_source is None:
            raise ValueError("Kolibri source is unavailable for indexing")
        snapshot = self._snapshot
        try:
            signature = database_signature(self._manager.database_path)
            if signature != snapshot.source_signature:
                replacement = self._manager.acquire()
                with self._lock:
                    old = self._snapshot
                    self._snapshot = replacement
                old.release()
                snapshot = replacement
        except (OSError, SnapshotError):
            # Build validation will fail closed later if freshness cannot be
            # established; keep the last pinned view for the initial manifest.
            snapshot = self._snapshot
        return KolibriEntryProducer(
            kolibri_source,
            snapshot,
            channel=kolibri_source.channel,
            scope=kolibri_source.scope,
            storage_root=self.storage_root,
            config=self.config,
            fast=fast,
            cfg=self._cfg,
            snapshot_factory=self._manager.acquire,
        )

    def random_source(self) -> SourceInfo:
        with self._lock:
            candidates = [
                source.info for source in self._library if source.info.homepage
            ]
        if not candidates:
            raise ArticleNotFoundError("no Kolibri source has a homepage")
        return secrets.choice(candidates)

    def health(self) -> list[dict[str, Any]]:
        with self._lock:
            entries = sorted(self.indexes.items())
            sources = {source.info.key: source.info for source in self._library}
        reports: list[dict[str, Any]] = []
        from ...indexing import schema

        for key, index in entries:
            meta = getattr(index, "index_meta", None) or {}
            report: dict[str, Any] = {
                "name": key,
                "status": "ok",
                "index_file": index.db_path is not None,
                "archive": True,
                "done": str(getattr(index, "done", "") or ""),
                "fast": str(getattr(index, "done", "") or "") == "fast",
                "capabilities": {
                    "title": index.db_path is not None,
                    "fulltext": index.db_path is not None,
                    "semantic": index.faiss_index is not None,
                },
            }
            source = sources.get(key)
            if source is not None:
                report["homepage"] = source.homepage
                report["item_count"] = source.item_count
            try:
                for field, decoder in (
                    (schema.INDEXED_COUNT, schema.decode_int),
                    (schema.TRUNCATED, schema.decode_bool),
                    (schema.BODY_TRUNCATED_COUNT, schema.decode_int),
                ):
                    if field in meta:
                        report[field] = decoder(meta[field])
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


class KolibriDiscoveryBatch(DiscoveryBatch):
    def __init__(
        self,
        config: KolibriConfig,
        ctx: ProviderContext,
    ) -> None:
        self._config = config
        self._ctx = ctx
        self._closed = False
        self._snapshot = None
        self._error: str | None = None
        self._collections: tuple[CollectionInfo, ...] = ()
        self._channels: dict[str, ChannelRow] = {}
        home, database, storage = _paths(config)
        snapshot_root = (
            Path(ctx.index_dir)
            / "kolibri-snapshots"
            / re.sub(r"[^A-Za-z0-9._-]+", "-", config.name)
        )
        self._manager = SnapshotManager(
            database_path=database,
            storage_path=storage,
            home_path=home,
            snapshot_root=snapshot_root,
            max_snapshot_bytes=config.max_snapshot_bytes,
            disk_budget_bytes=config.snapshot_disk_budget_bytes,
            timeout_seconds=config.snapshot_timeout_seconds,
        )
        try:
            self._snapshot = self._manager.acquire()
            db = self._snapshot.connect()
            try:
                channels = load_channels(db)
            finally:
                db.close()
            collections: list[CollectionInfo] = []
            seen: set[str] = set()
            for channel in channels:
                key = kolibri_collection_key(config.name, channel.id)
                if key in seen:
                    self._error = "duplicate Kolibri channel identity"
                    continue
                seen.add(key)
                self._channels[channel.id] = channel
                collections.append(
                    CollectionInfo(
                        key=key,
                        provider="kolibri",
                        label=channel.name or config.label or config.name,
                        title=channel.name or None,
                        description=channel.description or None,
                        metadata={
                            "channel_id": channel.id,
                            "granularity": config.granularity,
                        },
                    )
                )
            self._collections = tuple(collections)
        except SnapshotError as error:
            self._error = str(error)
            self._logger = ctx.logger
        except Exception as error:
            self._error = f"Kolibri discovery failed: {public_cause(error)}"
            logger.debug("Kolibri discovery error: %s", error)
            self._logger = ctx.logger

    @property
    def collections(self) -> Sequence[CollectionInfo]:
        return self._collections

    @property
    def error(self) -> str | None:
        return self._error

    def open(self, collection: CollectionInfo) -> CollectionRuntime:
        if self._closed:
            raise RuntimeError("discovery batch is closed")
        if self._snapshot is None:
            raise RuntimeError(self._error or "Kolibri discovery is unavailable")
        channel_id = str(collection.metadata.get("channel_id") or "")
        channel = self._channels.get(channel_id)
        if channel is None:
            db = self._snapshot.connect()
            try:
                channel = load_channel(db, channel_id)
            finally:
                db.close()
        if channel is None:
            raise RuntimeError("Kolibri channel identity is unavailable")
        self._snapshot.retain()
        runtime = None
        try:
            runtime = KolibriRuntime(
                collection,
                channel,
                self._config,
                self._ctx,
                self._snapshot,
                self._manager,
            )
            runtime.refresh(wait=True)
            return runtime
        except Exception:
            if runtime is not None:
                runtime.close()
            else:
                self._snapshot.release()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._snapshot is not None:
            self._snapshot.close()
            self._snapshot = None


class KolibriProvider:
    kind = "kolibri"

    def discover(
        self, config: ProviderConfig, ctx: ProviderContext
    ) -> DiscoveryBatch:
        if not isinstance(config, KolibriConfig):
            raise ConfigValidationError(
                "kolibri provider requires a KolibriConfig entry"
            )
        return KolibriDiscoveryBatch(config, ctx)


__all__ = [
    "KolibriDiscoveryBatch",
    "KolibriProvider",
    "KolibriRuntime",
]
