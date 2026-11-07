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
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from ...contracts import SourceInfo, SourceResult
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
from ...diagnostics import public_cause
from ...indexing.locator import IndexLocator
from ...indexing import search as generic_search
from ...indexing.derived import derived_text_preview
from ...model_meta import (
    MODEL_PROBE_SHAPE_KEY,
    MODEL_PROBE_TEXTS_KEY,
    MODEL_PROBE_VECTORS_KEY,
)
from ..base import (
    CollectionInfo,
    CollectionRuntime,
    CollectionSnapshot,
    ContentStore,
    DiscoveryBatch,
    ProviderContext,
    SourceQuery,
)
from ..config import ConfigValidationError, FilesConfig, ProviderConfig
from .discovery import (
    FilesIndexOptions,
    FilesSource,
    apply_index_metadata,
    discover_sources,
    files_collection_key,
    open_files_index,
)
from .entries import FilesEntryProducer
from .store import FilesContentStore

logger = logging.getLogger(__name__)

DEFAULT_FILES_REFRESH_TTL = 120.0
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


class FilesRuntime(CollectionRuntime):
    """One configured files directory: sources, indexes, and serving."""

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
        self._scanned_at = 0.0
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
            settings, "min_cosine_similarity", generic_search.DEFAULT_MIN_COSINE_SIMILARITY
        )
        self._stale_logged: set[str] = set()
        self._was_collapsed = False

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
                limits=self._search_limits(),
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
                hit = _phrase_in_text(text.casefold(), phrase_words)
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
            self.indexes = {}
            self._library = ()
            self._searchable = ()
            self._by_key = {}
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

    def _search_limits(self) -> generic_search.SearchLimits:
        return generic_search.SearchLimits(
            candidate_count=self._candidate_count,
            preview_chars=self._preview_chars,
            max_html_bytes=self._max_html_bytes,
            max_article_bytes=self._max_article_bytes,
            min_cosine_similarity=self._min_cosine_similarity,
            web_base_path=self.web_base_path,
            nprobe=(
                int(self._cfg["nprobe"])
                if self._cfg.get("nprobe") is not None
                else None
            ),
            cfg=self._cfg,
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
            logger.debug("files FAISS load error: %s", error)
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
                logger.debug("files model fingerprint error: %s", error)
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
            self._logger.debug("files FAISS reopen error: %s", error)

    def _probe_compatible(
        self, db: sqlite3.Connection, db_path: Path
    ) -> tuple[bool, str]:
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
        ready_getter = self._embedder_ready_getter
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

    # -- discovery / lifecycle --------------------------------------------

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

    def refresh_if_stale(self, ttl: float = DEFAULT_FILES_REFRESH_TTL) -> bool:
        with self._lock:
            scanned_at = self._scanned_at
        if scanned_at and time.monotonic() - scanned_at < ttl:
            return True
        return self.refresh(wait=True)

    def _refresh_once(self, *, wait: bool) -> bool:
        if not self._refresh_lock.acquire(blocking=wait):
            return False
        try:
            if self._closed:
                return False
            discovery = discover_sources(
                self.config, web_base_path=self.web_base_path
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
                    self._scanned_at = time.monotonic()
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
                defer_faiss=self._defer_faiss,
                model_fingerprint=self._model_fingerprint,
                cfg=self._cfg,
                probe_compatible=self._probe_compatible,
            )
            new_indexes: dict[str, Any] = {}
            searchable: list[FilesSource] = []
            library: list[FilesSource] = []
            discarded: list[Any] = []
            for files_source in discovery.sources:
                info = files_source.info
                existing = self.indexes.get(info.key)
                index = None
                if existing is not None and self._can_reuse_index(
                    existing, info
                ):
                    index = existing
                else:
                    try:
                        index = open_files_index(
                            info, index_dir=self.index_dir, options=options
                        )
                    except Exception as error:
                        self._logger.warning(
                            "%s: index open failed (%s); serving files without "
                            "an index until it is rebuilt",
                            info.key,
                            public_cause(error),
                        )
                        self._logger.debug(
                            "files index open error for %s: %s", info.key, error
                        )
                        index = None
                if index is not None:
                    object.__setattr__(info, "searchable", True)
                    apply_index_metadata(info, index.index_meta)
                    new_indexes[info.key] = index
                    searchable.append(files_source)
                else:
                    object.__setattr__(info, "searchable", False)
                library.append(files_source)
            for key, old in self.indexes.items():
                replacement = new_indexes.get(key)
                if replacement is not old:
                    discarded.append(old)
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
                self._scanned_at = time.monotonic()
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
        prefix = f"files:{self.config.name}:"
        with self._lock:
            known = set(self._by_key)
        for key in operations:
            if not str(key).startswith(prefix) or key in known:
                continue
            if key in self._stale_logged:
                continue
            self._stale_logged.add(key)
            self._logger.warning(
                "zimantic: files collection %s has a stale source override %r; "
                "it was not applied (source keys change after collapse or "
                "recovery; rebuild and update the key)",
                self.collection.label,
                key,
            )

    # -- build / content ---------------------------------------------------

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

    def random_source(self) -> SourceInfo:
        with self._lock:
            candidates = [
                source.info for source in self._library if source.info.homepage
            ]
        if not candidates:
            raise ArticleNotFoundError("no files source has a homepage")
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
                    "article": True,
                },
            }
            source = sources.get(key)
            if source is not None:
                report["homepage"] = source.homepage
                report["item_count"] = source.item_count
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


def _phrase_in_text(text: str, phrase_words: list[str]) -> bool:
    if not phrase_words:
        return True
    pattern = r"(?<!\w)" + r"\s+".join(
        re.escape(word) for word in phrase_words
    ) + r"(?!\w)"
    return re.search(pattern, text) is not None


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
    "DEFAULT_FILES_REFRESH_TTL",
    "FilesDiscoveryBatch",
    "FilesProvider",
    "FilesRuntime",
]
