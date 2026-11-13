"""Files provider and runtime.

A configured directory is one collection.  Its runtime discovers sources,
opens published indexes, delegates generic search/suggest to
:mod:`zimantic.indexing.search`, and serves files through
:class:`~zimantic.providers.files.store.FilesContentStore`.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ...catalog import SourceCatalog, catalog_path
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
    EnrichingRuntime,
    IndexedRuntime,
    ProviderContext,
    SourceQuery,
)
from ..config import ConfigValidationError, FilesConfig, ProviderConfig
from ..local_runtime import LocalIndexedRuntimeMixin, prepare_local_indexes
from .common import phrase_in_text
from .discovery import (
    FilesIndexOptions,
    FilesSource,
    discover_sources,
    files_collection_key,
)
from .entries import FilesEntryProducer
from .store import FilesContentStore

logger = logging.getLogger(__name__)

_PHRASE_MIMETYPES = frozenset(
    {"text/plain", "text/html", "application/xhtml+xml", "text/markdown"}
)
_MEDIA_PREFIXES = ("image/", "audio/", "video/")


def _settings(ctx: ProviderContext) -> dict[str, Any]:
    settings = getattr(ctx.config, "settings", None)
    return dict(settings or {})


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


class FilesRuntime(LocalIndexedRuntimeMixin, IndexedRuntime, EnrichingRuntime):
    """One configured files directory: sources, indexes, and serving."""

    _provider_label = "files"
    _serving_label = "files"

    def __init__(
        self,
        collection: CollectionInfo,
        config: FilesConfig,
        ctx: ProviderContext,
    ) -> None:
        self.collection = collection
        self.config = config
        self._ctx = ctx
        self.root = Path(os.path.realpath(Path(config.path).expanduser()))
        self.index_dir = Path(ctx.index_dir)
        self._metadata_only = bool(ctx.metadata_only)
        self.web_base_path = ctx.web_base_path
        self._logger = ctx.logger
        settings = _settings(ctx)
        self._cfg: dict[str, Any] = settings
        self._library: tuple[FilesSource, ...] = ()
        self._searchable: tuple[FilesSource, ...] = ()
        self._by_key: dict[str, FilesSource] = {}
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
        self._secure_browsing = bool(settings.get("secure_browsing", False))
        self._min_cosine_similarity = _float_setting(
            settings, "min_cosine_similarity", generic_search.DEFAULT_MIN_COSINE_SIMILARITY
        )
        self._stale_logged: set[str] = set()
        self._was_collapsed = False
        catalog_db = catalog_path(self.index_dir) if ctx.persist_catalog else None
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
            self._catalog = SourceCatalog(None)

    # -- contracts ---------------------------------------------------------

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
                    family for source in sources for family in source.info.media_types
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
            member = self._by_key.get(source.key)
        return {"path": self.root / member.membership_root} if member is not None else {}

    def search(self, source: SourceInfo, request: SourceQuery) -> SourceResult:
        if self._closed:
            return SourceResult(
                source=source, error="files collection is closed", status="error"
            )
        index = self.indexes.get(source.key)
        if index is None:
            return SourceResult(
                source=source, error="source is not indexed", status="error"
            )
        try:
            index.acquire()
        except RuntimeError as error:
            return SourceResult(source=source, error=str(error), status="cancelled")
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
                # Filename/native metadata only for the media body itself, but
                # build-time captions/OCR/ASR can provide a useful preview.
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
        if self._closed or not result_docs:
            return False
        try:
            store = self.content_store(source)
        except (ArticleNotFoundError, ArticleUnavailableError):
            return False
        found = False
        try:
            for result in result_docs:
                path = str(result.get("path") or "")
                if not path:
                    continue
                try:
                    entry = store.resolve(path)
                except (ArticleNotFoundError, ArticleUnavailableError, OSError, ValueError):
                    continue
                result["mimetype"] = entry.mimetype
                source_doc = source_docs.get(result.get("id"))
                if source_doc is not None:
                    source_doc["mimetype"] = entry.mimetype
                if result.get("id") not in phrase_ids:
                    continue
                if entry.mimetype not in _PHRASE_MIMETYPES:
                    continue
                if entry.size is not None and entry.size > max_article_bytes:
                    continue
                try:
                    data = store.read(
                        path,
                        head_only=True,
                        max_bytes=min(max_html_bytes, max_article_bytes),
                    )
                except (
                    ArticleNotFoundError,
                    ArticleTooLargeError,
                    ArticleUnavailableError,
                    OSError,
                    ValueError,
                ):
                    continue
                text = data.content.decode("utf-8", "ignore")
                if entry.mimetype in {"text/html", "application/xhtml+xml"}:
                    text = re.sub(
                        r"<(script|style)\b[^>]*>.*?</\1>",
                        " ",
                        text,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    text = re.sub(r"<[^>]+>", " ", text)
                hit = phrase_in_text(text.casefold(), phrase_words)
                if source_doc is not None:
                    source_doc["_content_phrase"] = hit
                if hit:
                    found = True
        finally:
            store.close()
        return found

    def content_store(self, source: SourceInfo) -> ContentStore | None:
        with self._lock:
            files_source = self._by_key.get(source.key)
            index = self.indexes.get(source.key)
        if files_source is None:
            raise ArticleUnavailableError("source is unavailable")
        return FilesContentStore(
            source,
            self.root,
            membership_root=files_source.membership_root,
            loose_only=files_source.loose_only,
            config=self.config,
            max_article_bytes=self._max_article_bytes,
            index=index,
            web_base_path=self.web_base_path,
            secure_browsing=self._secure_browsing,
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
        with self._refresh_lock, self._lock:
            if self._closed:
                return
            self._closed = True
            self._state = "closed"
            indexes = list(self.indexes.values())
            self.indexes = {}
            self._library = ()
            self._searchable = ()
            self._by_key = {}
            try:
                self._catalog.close()
            except sqlite3.Error as error:
                self._logger.warning(
                    "zimantic: catalog close failed for %s (%s)",
                    self.collection.label, public_cause(error),
                )
        for index in indexes:
            try:
                index.close()
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

    def refresh(self, *, wait: bool = True) -> bool:
        if wait:
            return self._refresh_once(wait=True)
        threading.Thread(
            target=self._refresh_once,
            kwargs={"wait": False},
            name=f"zimantic-files-refresh-{self.collection.key}",
            daemon=True,
        ).start()
        return True

    def _refresh_once(self, *, wait: bool) -> bool:
        if not self._refresh_lock.acquire(blocking=wait):
            return False
        try:
            if self._closed:
                return False
            discovery = discover_sources(
                self.config, web_base_path=self.web_base_path, catalog=self._catalog
            )
            if not discovery.errors:
                try:
                    cached_keys = {
                        record.key for record in self._catalog.rows(
                            collection_key=self.collection.key
                        )
                    }
                    self._catalog.delete_keys(
                        self.collection.key,
                        cached_keys - {source.key for source in discovery.sources},
                    )
                except sqlite3.Error as error:
                    self._logger.warning(
                        "zimantic: summary cache cleanup failed for %s (%s)",
                        self.collection.label, public_cause(error),
                    )
            if not discovery.sources and discovery.errors:
                with self._lock:
                    old = list(self.indexes.values())
                    self.indexes = {}
                    self._library = ()
                    self._searchable = ()
                    self._by_key = {}
                    self._error = "; ".join(discovery.errors)
                    self._state = "error"
                    self._last_refresh = time.time()
                    self._version += 1
                for index in old:
                    try:
                        index.close()
                    except Exception:
                        pass
                for message in discovery.errors:
                    self._logger.warning(
                        "zimantic: files collection %s: %s",
                        self.collection.label,
                        message,
                    )
                return False
            if self._was_collapsed and not discovery.collapsed:
                self._logger.warning(
                    "zimantic: files collection %s is no longer collapsed; "
                    "source keys changed and a rebuild is required",
                    self.collection.label,
                )
            self._was_collapsed = discovery.collapsed
            options = FilesIndexOptions(
                semantic=self._semantic,
                defer_faiss=self._defer_faiss or self._metadata_only,
                model_fingerprint=self._model_fingerprint,
                cfg=self._cfg,
                probe_compatible=self._probe_compatible,
            )
            prepared = prepare_local_indexes(
                list(discovery.sources),
                existing=self.indexes,
                index_dir=self.index_dir,
                options=options,
                metadata_only=self._metadata_only,
                can_reuse=self._can_reuse_index,
                provider_label=self._provider_label,
                serving_label=self._serving_label,
                logger=self._logger,
            )
            new_indexes = prepared.indexes
            searchable = prepared.searchable
            library = prepared.library
            discarded = prepared.discarded
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
                self.indexes = new_indexes
                self._library = tuple(library)
                self._searchable = tuple(searchable)
                self._by_key = {
                    source.info.key: source for source in self._library
                }
                self._error = "; ".join(discovery.errors) or None
                self._state = "ready"
                self._last_refresh = time.time()
                if changed:
                    self._version += 1
            for index in discarded:
                try:
                    index.close()
                except Exception:
                    pass
            for message in discovery.errors:
                self._logger.warning(
                    "zimantic: files collection %s: %s",
                    self.collection.label,
                    message,
                )
            self._report_stale_overrides()
            return True
        finally:
            self._refresh_lock.release()

    def entry_producer(self, source: SourceInfo, *, fast: bool):
        with self._lock:
            files_source = self._by_key.get(source.key)
        if files_source is None:
            raise ValueError("files source is unavailable for indexing")
        return FilesEntryProducer(
            source,
            self.root,
            membership_root=files_source.membership_root,
            loose_only=files_source.loose_only,
            config=self.config,
            fast=fast,
            cfg=self._cfg,
        )


class FilesDiscoveryBatch(DiscoveryBatch):
    def __init__(
        self,
        collection: CollectionInfo,
        config: FilesConfig,
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
        runtime = FilesRuntime(collection, self._config, self._ctx)
        runtime.refresh(wait=True)
        return runtime

    def close(self) -> None:
        self._closed = True


class FilesProvider:
    kind = "files"

    def discover(
        self, config: ProviderConfig, ctx: ProviderContext
    ) -> DiscoveryBatch:
        if not isinstance(config, FilesConfig):
            raise ConfigValidationError("files provider requires a FilesConfig entry")
        label = config.label or config.name
        collection = CollectionInfo(
            key=files_collection_key(config.name),
            provider="files",
            label=label,
            title=label,
            metadata={"granularity": config.granularity},
        )
        return FilesDiscoveryBatch(collection, config, ctx)


__all__ = [
    "FilesDiscoveryBatch",
    "FilesProvider",
    "FilesRuntime",
]
