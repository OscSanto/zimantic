"""Hybrid search over local ZIM indexes and optional Kiwix sources."""
from __future__ import annotations

import logging
import math
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen

from libzim.reader import Archive
from libzim.search import Query

from .cache import DEFAULT_CACHE_BYTES, DEFAULT_CACHE_SIZE, QueryCache
from .catalog import (
    ZimCatalog,
    catalog_path,
    largest_zims_by_stem,
    perma_ref,
    public_tags,
)
from .content.reader import (
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_PARSE_TIMEOUT,
    DEFAULT_PREVIEW_CHARS,
    read_entry,
    truncate_at_word_boundary,
)
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
from .libzim_compat import get_entry_by_id, search_paths
from .model_meta import (
    MODEL_PROBE_SHAPE_KEY,
    MODEL_PROBE_TEXTS_KEY,
    MODEL_PROBE_VECTORS_KEY,
)
from .searching.config import (
    DEFAULT_MAX_ARTICLE_BYTES,
    _file_signature,
    _float_config,
    _int_config,
)
from .searching.index import _LocalIndex, _nprobe
from .searching.kiwix import (
    DEFAULT_KIWIX_CATALOG_ATTEMPTS,
    DEFAULT_KIWIX_CATALOG_LIMIT,
    DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF,
    DEFAULT_KIWIX_RESULT_LIMIT,
    DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
    MAX_KIWIX_CATALOG_RETRY_BACKOFF,
    _catalog_book,
    catalog_entries,
    search_kiwix,
)
from .searching.query import (
    MAX_QUERY_LENGTH,
    _content_terms,
    _fulltext_query,
    _phrase,
    _phrase_words,
    _query_terms,
    _title_prefix_query,
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
    source_items,
)
from .searching.sources import (
    _book_identity,
    _bounded_catalog_url,
    _child_text,
    _find_archive_icon_path,
    _intent_phrases,
    _local_name,
    _parse_remote_xml,
    _read_limited_response,
    _source_icon_url,
    _zim_key,
)
from .settings import normalize_web_base_path

logger = logging.getLogger(__name__)

__all__ = [
    "ArticleNotFoundError",
    "ArticleSourceNotFoundError",
    "ArticleTooLargeError",
    "ArticleUnavailableError",
    "DEFAULT_KIWIX_CATALOG_ATTEMPTS",
    "DEFAULT_KIWIX_CATALOG_LIMIT",
    "DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF",
    "DEFAULT_KIWIX_RESULT_LIMIT",
    "DEFAULT_LONG_QUERY",
    "DEFAULT_MAX_KIWIX_RESPONSE_BYTES",
    "FULLTEXT_WEIGHT",
    "HEAD_READ_BYTES",
    "KEYWORD_WEIGHT",
    "LONG_FULLTEXT_WEIGHT",
    "MAX_KIWIX_CATALOG_RETRY_BACKOFF",
    "RANDOM_ARTICLE_ATTEMPTS",
    "RRF_K",
    "SEMANTIC_WEIGHT",
    "Search",
    "_RankedAssessment",
    "_assess_ranked_document",
    "_bounded_catalog_url",
    "_catalog_book",
    "_child_text",
    "_collect_ranked_candidates",
    "_deduplicate_ranked",
    "_local_name",
    "_parse_remote_xml",
    "_read_limited_response",
    "_render_ranked_document",
    "_rrf_scores",
    "catalog_entries",
    "is_random_article_entry",
    "matched_by",
    "normalize_article_path",
    "random_fallback_article",
    "random_indexed_article",
    "rank_results",
    "read_archive_article",
    "read_archive_icon",
    "resolve_article_entry",
    "search_kiwix",
    "source_items",
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
DEFAULT_MAX_REMOTE_SOURCES = 100
MAX_PHRASE_CANDIDATES = 32
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


_HTML_ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]+);")
# Script and style bodies are not visible article text. Treat a complete block
# as one separator, and an unclosed opener as swallowing the rest of the page.
_SCRIPT_STYLE_BLOCK = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?</\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_SCRIPT_STYLE_OPEN = re.compile(r"<\s*(?:script|style)\b", re.IGNORECASE)

# Entries whose readable text can be searched for an exact query phrase.
_PHRASE_MIMETYPES = frozenset({"text/plain", "text/html", "application/xhtml+xml"})


def _source_tags(metadata: dict[str, Any]) -> tuple[str, ...]:
    value = metadata.get("public_tags")
    if value is None:
        value = metadata.get("tags", ())
    if isinstance(value, str):
        return tuple(tag for tag in public_tags(value).split(";") if tag)
    return tuple(value or ())


def _source_info_from_metadata(
    metadata: dict[str, Any],
    *,
    key: str,
    local_name: str,
    available: bool,
    searchable: bool,
    icon_url: str | None,
    error: str | None = None,
) -> SourceInfo:
    """Build one local ``SourceInfo`` from canonical catalog/archive metadata.

    Keeping this mapping in one place prevents the catalog-backed and
    archive-scan fallback paths from drifting apart (for example, one returning
    a bool ``has_fulltext_index`` and the other returning ``0``/``1``).
    """
    raw_size = metadata.get("size")
    raw_articles = metadata.get("article_count")
    raw_media = metadata.get("media_count")
    raw_fulltext = metadata.get("has_fulltext_index")
    return SourceInfo(
        key=key,
        name=local_name,
        book=local_name,
        mode="local",
        local_name=local_name,
        available=available,
        searchable=bool(searchable) and available,
        error=error,
        homepage=metadata.get("homepage") if available else None,
        icon_path=metadata.get("icon_path") or None,
        icon_url=icon_url,
        intent_phrases=_intent_phrases(local_name),
        title=metadata.get("title") or metadata.get("name") or None,
        description=metadata.get("description") or None,
        long_description=metadata.get("long_description") or None,
        creator=metadata.get("creator") or None,
        publisher=metadata.get("publisher") or None,
        language=metadata.get("language") or None,
        date=metadata.get("date") or None,
        tags=_source_tags(metadata),
        size=int(raw_size) if raw_size is not None else None,
        article_count=int(raw_articles) if raw_articles is not None else None,
        uuid=metadata.get("uuid") or None,
        perma_ref=perma_ref(local_name),
        flavour=metadata.get("flavour") or None,
        category=metadata.get("category") or None,
        media_count=int(raw_media) if raw_media is not None else None,
        license=metadata.get("license") or None,
        scraper=metadata.get("scraper") or None,
        source=metadata.get("source") or None,
        attribution=metadata.get("attribution") or None,
        relation=metadata.get("relation") or None,
        counter=metadata.get("counter") or None,
        has_fulltext_index=(
            None if raw_fulltext is None else bool(raw_fulltext)
        ),
    )


def _readable_words(text: str) -> Iterator[str]:
    """Yield words from text while treating tags and entities as separators."""
    position = 0
    length = len(text)
    tags_possible = True
    while position < length:
        if tags_possible and text[position] == "<":
            block = _SCRIPT_STYLE_BLOCK.match(text, position)
            if block is not None:
                position = block.end()
                continue
            if _SCRIPT_STYLE_OPEN.match(text, position):
                # An unclosed script/style has no reliable end; everything
                # after it is code, not article text.
                return
            end = text.find(">", position + 1)
            if end >= 0:
                position = end + 1
                continue
            tags_possible = False
        if text[position] == "&":
            entity = _HTML_ENTITY.match(text, position)
            if entity is not None:
                position = entity.end()
                continue
        if text[position] == "_" or text[position].isalnum():
            start = position
            position += 1
            while position < length and (
                text[position] == "_" or text[position].isalnum()
            ):
                position += 1
            yield text[start:position]
            continue
        position += 1


def _phrase_in_word_stream(text: str, phrase_words: list[str]) -> bool:
    """True when ``phrase_words`` appears as a contiguous run of readable words.

    Streams ``_readable_words`` and matches with a KMP failure table in one
    linear pass, so no joined copy of the whole article's words is ever
    materialized (a few MB per candidate on large pages) and overlapping repeats
    such as ``"very very very good"`` still match ``"very very good"``.
    """
    width = len(phrase_words)
    if width == 0:
        return True
    # KMP prefix table: on a mismatch, fall back to the longest phrase prefix
    # that is also a suffix of what has matched so far. A naive rolling window
    # would drop a valid match when the phrase itself repeats a word.
    failure = [0] * width
    overlap = 0
    for index in range(1, width):
        while overlap and phrase_words[index] != phrase_words[overlap]:
            overlap = failure[overlap - 1]
        if phrase_words[index] == phrase_words[overlap]:
            overlap += 1
        failure[index] = overlap
    matched = 0
    for word in _readable_words(text):
        while matched and word != phrase_words[matched]:
            matched = failure[matched - 1]
        if word == phrase_words[matched]:
            matched += 1
            if matched == width:
                return True
    return False


class SearchQueryError(ValueError):
    """The requested query cannot be processed."""


class SearchBusyError(Exception):
    """Too many searches are already running; the client should retry."""


def _local_source_rank(source: SourceInfo) -> tuple:
    """Preference order for one copy of a local book.

    Readability and searchability come first, then the larger archive. A bigger
    copy is usually the more complete download and may contain larger artwork
    or media, so it wins over a newer but smaller flavour of the same book.
    """
    try:
        article_count = int(source.article_count or 0)
    except (TypeError, ValueError):
        article_count = 0
    try:
        size = int(source.size or 0)
    except (TypeError, ValueError):
        size = 0
    name = source.name or ""
    return (
        bool(source.available),
        bool(source.searchable),
        size,
        article_count,
        str(source.date or ""),
        -len(name),
        name,
    )


def _choose_sources(sources: list[SourceInfo], identity) -> list[SourceInfo]:
    """Keep the best source for each identity, preserving first-seen order."""
    chosen: dict[str, tuple[tuple, SourceInfo]] = {}
    for source in sources:
        key = identity(source)
        if not key:
            key = f"name:{source.name or source.local_name or source.key}"
        rank = _local_source_rank(source)
        current = chosen.get(key)
        if current is None or rank > current[0]:
            chosen[key] = (rank, source)
    return [source for _rank, source in chosen.values()]


class Search:
    cfg: dict[str, Any] = {}
    semantic: bool = False
    embedder: Any = None
    catalog: ZimCatalog | None = None
    _defer_faiss: bool = False
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
        # Tests pass ``object`` to keep the catalog from opening real archives.
        self.archive_factory = Archive if archive_factory is None else archive_factory
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
        self.indexes: dict[str, _LocalIndex] = {}
        self.sources: dict[str, SourceInfo] = {}
        # None until serve enables crash isolation; tests and library callers
        # keep using the in-process searcher.
        self.fulltext_pool: Any | None = None
        self._state_lock = threading.RLock()
        self._reload_lock = threading.Lock()
        self.source_timeout = _int_config(cfg, "source_timeout", DEFAULT_SOURCE_TIMEOUT)
        self.candidate_count = _int_config(cfg, "candidate_count", DEFAULT_CANDIDATES)
        self.source_workers = _int_config(cfg, "search_workers", 4)
        self.max_remote_sources = _int_config(
            cfg, "max_remote_sources", DEFAULT_MAX_REMOTE_SOURCES, minimum=0
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
        self._catalog_entries_cache: list[dict[str, str]] = []
        self._catalog_server: str | None = None
        self.cache = QueryCache(
            _int_config(cfg, "cache_size", DEFAULT_CACHE_SIZE, minimum=0),
            _int_config(cfg, "cache_bytes", DEFAULT_CACHE_BYTES, minimum=0),
        )
        self.preview_cache = QueryCache(
            _int_config(cfg, "cache_size", DEFAULT_CACHE_SIZE, minimum=0),
            _int_config(cfg, "cache_bytes", DEFAULT_CACHE_BYTES, minimum=0),
        )
        self._library_scan_at = 0.0
        self._catalog_initialized = True
        self.catalog = self._ensure_catalog()
        if self.catalog.zim_dir is not None and self.catalog.zim_dir.is_dir():
            try:
                self.catalog.scan()
            except (OSError, sqlite3.Error) as error:
                # An unmounted ZIM directory must not discard cached metadata.
                logger.warning(
                    "zimantic: catalog scan failed (%s); keeping the previous catalog",
                    error,
                )
            else:
                self._library_scan_at = time.monotonic()
        self._load_indexes(refresh_catalog=False)
        # Local indexes are ready immediately; the optional Kiwix catalog is
        # refreshed in the background so an unreachable Kiwix server cannot
        # delay startup (see start_catalog_refresh).
        self.refresh_local_sources()

    @property
    def web_base_path(self) -> str:
        """Normalized URL prefix for generated links (reverse-proxy support)."""
        return normalize_web_base_path(self.cfg.get("web_base_path"))

    @property
    def _catalog(self) -> ZimCatalog | None:
        """Tolerate Search instances built without ``__init__`` (tests)."""
        return self.catalog

    def _ensure_catalog(self) -> ZimCatalog:
        """Return the shared catalog, creating an in-memory one when needed."""
        cfg = self.cfg or {}
        zim_dir = cfg.get("zim_dir")
        catalog = self.catalog
        if catalog is not None:
            catalog.zim_dir = Path(zim_dir).resolve() if zim_dir else None
            return catalog

        index_dir = cfg.get("index_dir")
        initialized = getattr(self, "_catalog_initialized", False)
        archive_factory = (
            getattr(self, "archive_factory", None) if initialized else object
        )
        persist = initialized and archive_factory is not object
        db_path = (
            catalog_path(index_dir)
            if persist and index_dir and archive_factory is not object
            else None
        )
        try:
            catalog = ZimCatalog(
                db_path,
                zim_dir=zim_dir,
                archive_factory=archive_factory,
            )
        except Exception as error:
            if db_path is None:
                raise
            logger.warning(
                "zimantic: catalog unavailable (%s); using an in-memory catalog",
                error,
            )
            catalog = ZimCatalog(
                None,
                zim_dir=zim_dir,
                archive_factory=archive_factory,
            )
        self.catalog = catalog
        return catalog

    @staticmethod
    def _index_uri(db_path: Path) -> str:
        return db_path.resolve().as_uri() + "?mode=ro"

    def _scan_catalog(self, *, force: bool = True) -> bool:
        """Refresh catalog records, throttling library requests to the scan TTL."""
        catalog = self._ensure_catalog()
        if catalog.zim_dir is None or not catalog.zim_dir.is_dir():
            return False
        lock = self._library_cache_lock()
        with lock:
            now = time.monotonic()
            scanned_at = getattr(self, "_library_scan_at", 0.0)
            if not force and now - scanned_at < LIBRARY_SCAN_TTL_SECONDS:
                return True
            try:
                catalog.scan()
            except (OSError, sqlite3.Error) as error:
                logger.error("zimantic: catalog scan failed (%s)", error)
                self._library_scan_at = 0.0
                return False
            self._library_scan_at = time.monotonic()
            self._library_version_at = 0.0
        return True

    def _find_zim_path(
        self,
        stem: str,
        source_size: int | None,
        *,
        refresh: bool = True,
    ) -> Path | None:
        """Best archive for ``stem``: an exact-size match, else the largest."""
        if refresh:
            if not self._scan_catalog(force=True):
                return None
        return self._ensure_catalog().find_path(stem, source_size)

    def _match_zim_path(
        self,
        stem: str,
        source_size: int | None,
        *,
        refresh: bool = True,
    ) -> tuple[Path | None, bool]:
        """Return the matching archive and whether its size matches the index."""
        path = self._find_zim_path(stem, source_size, refresh=refresh)
        if path is None:
            return None, False
        if source_size is None:
            return path, True
        try:
            return path, path.stat().st_size == source_size
        except OSError:
            return path, False

    def _scan_zim_paths(self, *, refresh: bool = True) -> dict[str, Path]:
        """Return one catalogued archive per stem, preferring the largest copy."""
        catalog = self._ensure_catalog()
        if catalog.zim_dir is None or not catalog.zim_dir.is_dir():
            return {}
        if refresh and not self._scan_catalog(force=True):
            return {}
        records = catalog.rows()
        sizes_by_path = {Path(record.path): record.size for record in records}
        found = largest_zims_by_stem(sizes_by_path)
        sizes_by_stem: dict[str, set[int]] = {}
        for record in records:
            sizes_by_stem.setdefault(record.stem, set()).add(record.size)
        for stem, sizes in sizes_by_stem.items():
            if len(sizes) > 1:
                logger.warning(
                    "zimantic: %r has ZIM files of different sizes; "
                    "using the largest %s",
                    stem,
                    found[stem],
                )
        return found

    @staticmethod
    def _read_index_state(db_path: Path) -> tuple[bool, int | None]:
        """Return ``(finished, recorded_source_size)`` for one SQLite index.

        An index still being written, or one that cannot be opened, reports
        ``False`` so its archive's native full-text index keeps the source
        searchable until the database is ready. The recorded source size is
        ``None`` when absent or unparsable.
        """
        try:
            db = sqlite3.connect(
                Search._index_uri(db_path), uri=True, check_same_thread=False
            )
        except sqlite3.Error:
            return False, None
        try:
            done = db.execute("SELECT value FROM meta WHERE key = 'done'").fetchone()
            size_row = db.execute(
                "SELECT value FROM meta WHERE key = 'filesize'"
            ).fetchone()
        except sqlite3.Error:
            return False, None
        finally:
            db.close()
        finished = done is not None and str(done[0]) in {"1", "fast"}
        try:
            source_size = int(size_row[0]) if size_row else None
        except (TypeError, ValueError):
            source_size = None
        return finished, source_size

    def _stale_zim_archive(
        self, stem: str, source_size: int | None, scanned: Path
    ) -> Path | None:
        """Archive to serve unindexed when an index is stale, else ``None``.

        ``scanned`` is the scan's chosen (largest) same-stem archive. A size
        match there, or a missing recorded size, means the index still matches.
        Only a mismatch triggers the extra walk that checks the stem's other
        files, so a valid library pays no extra directory scan.
        """
        if source_size is None:
            return None
        try:
            if scanned.stat().st_size == source_size:
                return None
        except OSError:
            pass
        archive, exact = self._match_zim_path(
            stem, source_size, refresh=False
        )
        return None if archive is None or exact else archive

    def _discover_local_indexes(
        self, *, refresh_catalog: bool = True
    ) -> dict[str, tuple[str, Path]]:
        """Return finished SQLite indexes plus filesystem-only ZIM fallbacks.

        A SQLite stem is authoritative only once its build is finished and its
        recorded source size still matches a same-stem archive. An in-progress
        build, one whose ZIM is gone, or one whose ZIM was replaced (size
        changed) is ignored so the archive's native full-text index keeps the
        source searchable instead of a stale or unopenable index.
        """
        index_dir = self.cfg.get("index_dir")
        db_paths = sorted(Path(index_dir).glob("*.sqlite")) if index_dir else []
        finished: dict[str, Path] = {}
        recorded_sizes: dict[str, int | None] = {}
        for path in db_paths:
            done, source_size = self._read_index_state(path)
            if done:
                finished[path.stem] = path
                recorded_sizes[path.stem] = source_size

        zim_paths = self._scan_zim_paths(refresh=refresh_catalog)
        found: dict[str, tuple[str, Path]] = {}
        for stem, db_path in finished.items():
            scanned = zim_paths.get(stem)
            if scanned is None:
                # The archive this index was built from is gone. Without a local
                # ZIM the index cannot serve articles, so ignore it. A book that
                # is only available remotely is discovered separately as a
                # kiwix source by the catalog refresh.
                logger.info(
                    "zimantic: %s index has no local ZIM; ignoring it",
                    stem,
                )
                continue
            stale = self._stale_zim_archive(stem, recorded_sizes[stem], scanned)
            if stale is None:
                found[stem] = ("sqlite", db_path)
                continue
            logger.warning(
                "zimantic: %s index (recorded source %s bytes) does not match %s; "
                "using the ZIM unindexed until the index is rebuilt",
                stem,
                recorded_sizes[stem],
                stale,
            )
            found[stem] = ("zim", stale)
        for stem, path in zim_paths.items():
            if stem not in finished:
                found[stem] = ("zim", path)

        # The UUID is the book identity. Two copies of the same ZIM (same
        # UUID, different filename/path) must not appear as two sources. Keep
        # one deterministic copy: readable first, then one that can serve a
        # search, then the larger file, then the shorter path.
        catalog = self._ensure_catalog()
        chosen: dict[str, tuple[tuple, str]] = {}
        for stem, (kind, _path) in found.items():
            record = catalog.get_stem(stem)
            uuid_value = record.uuid if record is not None else ""
            if not uuid_value:
                continue
            usable = kind == "sqlite" or bool(record.has_fulltext_index)
            rank = (
                bool(record.available),
                usable,
                record.size,
                -len(record.path),
            )
            current = chosen.get(uuid_value)
            if (
                current is None
                or rank > current[0]
                or (rank == current[0] and stem < current[1])
            ):
                if current is not None:
                    logger.warning(
                        "zimantic: %s duplicates %s (UUID %s); using %s",
                        stem,
                        current[1],
                        uuid_value,
                        stem,
                    )
                chosen[uuid_value] = (rank, stem)
            else:
                logger.warning(
                    "zimantic: %s duplicates %s (UUID %s); skipping %s",
                    stem,
                    current[1],
                    uuid_value,
                    stem,
                )
        keep = {stem for _rank, stem in chosen.values()}
        for stem in list(found):
            record = catalog.get_stem(stem)
            if record is not None and record.uuid and stem not in keep:
                del found[stem]
        return found

    def _model_probes_compatible(self, db, db_path: Path) -> tuple[bool, str]:
        """Compare an index's stored probes with the current embedding model.

        A mismatched file fingerprint is not necessarily a different model:
        FP32 and int8 exports of the same encoder are different files but
        produce nearly identical vectors. The probes stored by a full build let
        serving distinguish that case from a genuinely different model.
        """
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
        # Probe comparisons need the current model. Wait for `start_embedder`
        # so an FP32-built index can be accepted when serving with the int8
        # export of the same encoder. The server is already answering title and
        # full-text searches on other threads.
        ready = getattr(self, "_embedder_ready", None)
        if getattr(self, "_embedder_started", False) and ready is not None:
            if not ready.wait(timeout=60):
                return False, "timed out waiting for the embedding model"
        # Imported lazily: fast mode never needs the ONNX Runtime model stack.
        from .embed import embed_probe_texts, load_model_probes, model_probes_match

        probes = load_model_probes(rows)
        if probes is None:
            return False, "unreadable compatibility probes"
        embedder = self.embedder
        if embedder is None:
            return False, "embedding model is unavailable"
        texts, stored_vectors = probes
        key = tuple(texts)
        cache = self._probe_cache
        lock = self._probe_cache_lock
        current = None
        if cache is not None and lock is not None:
            with lock:
                current = cache.get(key)
                if current is None:
                    try:
                        current = embed_probe_texts(embedder, texts)
                    except Exception as error:  # a broken probe must not hide the index
                        return False, f"probe embedding failed: {error}"
                    cache[key] = current
        elif current is None:
            try:
                current = embed_probe_texts(embedder, texts)
            except Exception as error:  # a broken probe must not hide the index
                return False, f"probe embedding failed: {error}"
        try:
            compatible, mean_cosine, min_cosine = model_probes_match(
                texts, stored_vectors, current=current
            )
        except Exception as error:  # a broken probe must not hide the index
            return False, f"probe comparison failed: {error}"
        return compatible, (
            f"mean cosine {mean_cosine:.4f}, minimum {min_cosine:.4f}"
        )

    def _open_index(self, db_path: Path) -> _LocalIndex | None:
        """Open one finished index. Missing or unreadable vectors degrade the
        index to title + full-text only instead of failing the whole server."""
        try:
            uri = self._index_uri(db_path)
            db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        except sqlite3.Error:
            return None

        try:
            done = db.execute("SELECT value FROM meta WHERE key = 'done'").fetchone()
            if not done or str(done[0]) not in {"1", "fast"}:
                db.close()
                return None
            done_value = str(done[0])
            size_row = db.execute(
                "SELECT value FROM meta WHERE key = 'filesize'"
            ).fetchone()
            fingerprint_row = db.execute(
                "SELECT value FROM meta WHERE key = 'model_fingerprint'"
            ).fetchone()
            try:
                source_size = int(size_row[0]) if size_row else None
            except (TypeError, ValueError):
                source_size = None
            stored_fingerprint = str(fingerprint_row[0]) if fingerprint_row else ""

            faiss_index = None
            faiss_path = db_path.with_suffix(".faiss")
            defer_faiss = self._defer_faiss
            if (
                self.semantic
                and not defer_faiss
                and done_value == "1"
                and faiss_path.exists()
            ):
                current_fingerprint = self._model_fingerprint
                model_changed = bool(
                    stored_fingerprint
                    and current_fingerprint
                    and stored_fingerprint != current_fingerprint
                )
                if model_changed:
                    compatible, detail = self._model_probes_compatible(db, db_path)
                    if compatible:
                        logger.warning(
                            "%s: vectors were built with a different model file, "
                            "but embedding probes match (%s); using them",
                            db_path.stem,
                            detail,
                        )
                        model_changed = False
                    else:
                        logger.warning(
                            "%s: vectors were built with a different model; "
                            "falling back to title and full-text (lexical) search until it is rebuilt "
                            "(%s)",
                            db_path.stem,
                            detail,
                        )
                if not model_changed:
                    try:
                        # Fast mode does not need the FAISS import.
                        import faiss

                        faiss_index = faiss.read_index(
                            str(faiss_path),
                            faiss.IO_FLAG_MMAP_IFC | faiss.IO_FLAG_READ_ONLY,
                        )
                        if hasattr(faiss_index, "nprobe"):
                            faiss_index.nprobe = _nprobe(faiss_index, self.cfg)
                    except Exception as error:  # corrupt/unreadable vectors: keep going
                        logger.warning(
                            "%s: vectors unavailable (%s); falling back to title and full-text (lexical) search",
                            db_path.stem,
                            error,
                        )
                        faiss_index = None

            catalog = self._ensure_catalog()
            record = catalog.get_stem_size(db_path.stem, source_size)
            if record is not None:
                # Keep a valid SQLite index usable for title search even if the
                # archive is currently unreadable; opening is lazy and reports
                # the failure only for operations that need the archive.
                zim_path = Path(record.path)
                if source_size is None:
                    source_size = record.size
            else:
                zim_path = self._find_zim_path(
                    db_path.stem, source_size, refresh=False
                )
            # The archive and its libzim Searcher are opened on first use. A
            # 5000-ZIM library therefore no longer opens 5000 archives (or
            # creates 5000 Searchers) at startup.
            db.close()
            return _LocalIndex(
                None,
                faiss_index,
                None,
                None,
                threading.Lock(),
                db_path=uri,
                db_signature=_file_signature(db_path),
                faiss_signature=(
                    _file_signature(faiss_path)
                    if self.semantic and not defer_faiss
                    else None
                ),
                done=done_value,
                source_size=source_size,
                zim_path=str(zim_path) if zim_path is not None else None,
                zim_signature=_file_signature(zim_path) if zim_path is not None else None,
                has_fulltext_index=(
                    bool(record.has_fulltext_index) if record is not None else None
                ),
            )
        except sqlite3.Error:
            db.close()
            return None

    def _open_zim_index(self, zim_path: Path) -> _LocalIndex | None:
        """Open an unindexed ZIM using only its native full-text index.

        A ZIM with no native full-text index has nothing to offer a search
        source (no SQLite title index either), so it is skipped rather than
        advertised as an available-but-empty source.
        """
        record = self._ensure_catalog().get_stem(zim_path.stem)
        if record is not None:
            if not record.available:
                return None
            if not record.has_fulltext_index:
                logger.debug(
                    "%s: no SQLite or native full-text index; skipping",
                    zim_path.stem,
                )
                return None
            archive = None
            source_size = record.size
        else:
            try:
                archive = Archive(zim_path)
                if not archive.has_fulltext_index:
                    logger.debug(
                        "%s: no SQLite or native full-text index; skipping",
                        zim_path.stem,
                    )
                    return None
                source_size = zim_path.stat().st_size
            except Exception as error:  # a corrupt archive must not abort discovery
                logger.warning(
                    "%s: ZIM fallback unavailable (%s)",
                    zim_path.stem,
                    error,
                )
                return None
        return _LocalIndex(
            None,
            None,
            archive,
            None,
            threading.Lock(),
            done="filesystem",
            source_size=source_size,
            zim_path=str(zim_path),
            zim_signature=_file_signature(zim_path),
            has_fulltext_index=True,
        )

    def _load_indexes(self, *, refresh_catalog: bool = True) -> None:
        found = self._discover_local_indexes(refresh_catalog=refresh_catalog)
        if not found:
            return
        workers = min(8, len(found))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            opened = executor.map(
                lambda item: (
                    item[0],
                    item[1],
                    self._open_index(item[2])
                    if item[1] == "sqlite"
                    else self._open_zim_index(item[2]),
                ),
                sorted(
                    (
                        (stem, kind, path)
                        for stem, (kind, path) in found.items()
                    ),
                    key=lambda item: item[0],
                ),
            )
            for stem, _kind, index in opened:
                if index:
                    self.indexes[stem] = index

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

            self._defer_faiss = False
            try:
                # __init__ just scanned the catalog and built the source set;
                # this reload only reopens index handles to attach their
                # vectors. Rescanning here would duplicate both at startup.
                self.reload(scan_catalog=False, refresh=False)
            except Exception as error:
                logger.warning(
                    "zimantic: semantic search disabled: FAISS indexes "
                    "unavailable (%s); falling back to title and full-text "
                    "(lexical) search",
                    error,
                )
                return
            with self._state_lock:
                semantic_indexes = sum(
                    index.semantic for index in self.indexes.values()
                )
            logger.info(
                "zimantic: FAISS ready; semantic search enabled for %d index(es)",
                semantic_indexes,
            )

        threading.Thread(target=_load, name="zimantic-faiss", daemon=True).start()

    def start_fulltext_isolation(self) -> None:
        """Run native full-text searches in helper processes.

        python-libzim aborts the interpreter when a ZIM full-text index points
        at a missing entry, so the server must not iterate those results in its
        own process. The helpers are lazy (they open archives on demand) and only
        serve native full-text lookups; title, semantic and article paths stay
        in the main process.
        """
        if self.fulltext_pool is not None:
            return
        from .fulltext import DEFAULT_WORKERS, FulltextPool

        workers = min(DEFAULT_WORKERS, max(1, self.source_workers))
        self.fulltext_pool = FulltextPool(workers)

    def reload(
        self, *, scan_catalog: bool = True, refresh: bool = True
    ) -> dict[str, Any]:
        """Rescan local indexes and ZIM files without restarting.

        Picks up new finished indexes and unindexed archives, forgets removed
        sources, upgrades fast indexes that gained vectors, and refreshes the
        Kiwix catalog. Cheap enough for systemd.path to trigger.

        ``start_faiss`` passes ``scan_catalog=False`` and ``refresh=False``
        because it runs moments after ``__init__`` scanned the catalog and built
        the source set; only the index handles need reopening for their vectors.
        """
        with self._reload_lock:
            if scan_catalog:
                self._scan_catalog(force=True)
            found = self._discover_local_indexes(refresh_catalog=False)
            with self._state_lock:
                current_names = set(self.indexes)
            removed = sorted(current_names - set(found))
            added = []
            upgraded = []
            any_changed = False

            for name in removed:
                with self._state_lock:
                    current = self.indexes.pop(name, None)
                if current is not None:
                    self._close_index(current)
                    any_changed = True

            for name, (kind, path) in found.items():
                with self._state_lock:
                    current = self.indexes.get(name)
                db_signature = _file_signature(path) if kind == "sqlite" else None
                defer_faiss = self._defer_faiss
                faiss_signature = (
                    _file_signature(path.with_suffix(".faiss"))
                    if kind == "sqlite" and self.semantic and not defer_faiss
                    else None
                )
                zim_signature = (
                    _file_signature(path)
                    if kind == "zim"
                    else None
                )
                expected_db_path = (
                    self._index_uri(path) if kind == "sqlite" else None
                )
                matching_zim = (
                    self._find_zim_path(
                        name, current.source_size, refresh=False
                    )
                    if kind == "sqlite" and current is not None
                    else None
                )
                expected_zim_path = (
                    str(path)
                    if kind == "zim"
                    else str(matching_zim) if matching_zim is not None else None
                )
                changed = (
                    current is not None
                    and (
                        current.db_path != expected_db_path
                        or current.db_signature != db_signature
                        or (
                            self.semantic
                            and kind == "sqlite"
                            and not defer_faiss
                            and current.faiss_signature != faiss_signature
                        )
                        or (
                            current.zim_path != expected_zim_path
                        )
                        or (
                            kind == "zim"
                            and current.zim_signature != zim_signature
                        )
                    )
                )
                if current is not None and not changed:
                    # A failed archive open is not part of the index signature
                    # (it may have been transient), so give retained indexes
                    # another chance instead of latching the failure forever.
                    current.allow_reopen()
                    continue

                fresh = (
                    self._open_index(path)
                    if kind == "sqlite"
                    else self._open_zim_index(path)
                )
                if fresh is None:
                    continue

                old = None
                close_fresh = False
                with self._state_lock:
                    existing = self.indexes.get(name)
                    if existing is None:
                        self.indexes[name] = fresh
                        added.append(name)
                        any_changed = True
                    elif existing is current:
                        self.indexes[name] = fresh
                        old = existing
                        if self.semantic and not existing.semantic and fresh.semantic:
                            upgraded.append(name)
                        any_changed = True
                    else:
                        close_fresh = True
                if old is not None:
                    self._close_index(old)
                if close_fresh:
                    self._close_index(fresh)

            if any_changed:
                # Index content, and therefore what is searchable, changed;
                # cached answers under the same names can no longer be fresh.
                self._invalidate_library_cache()
                with self._state_lock:
                    self._cache_generation += 1
                self.cache.clear()
                preview_cache = self.preview_cache
                if preview_cache is not None:
                    preview_cache.clear()
            if refresh:
                self.refresh_sources()
            if self.fulltext_pool is not None:
                # A reload is the natural moment to retry an archive that was
                # repaired in place after its full-text helper aborted.
                self.fulltext_pool.clear_broken()
            with self._state_lock:
                indexes = sorted(self.indexes)
            return {
                "indexes": indexes,
                "added": sorted(added),
                "removed": removed,
                "upgraded": sorted(upgraded),
            }

    @staticmethod
    def _close_index(index: _LocalIndex) -> None:
        with index._lifecycle:
            index._retired = True
            if index._active_searches or index._closed:
                return
            index._closed = True
            connections = [index.db, *index._conns]
            Search._release_native_resources(index)
        Search._close_connections(connections)

    @staticmethod
    def _release_native_resources(index: _LocalIndex) -> None:
        """Drop references to native handles held by a closed index.

        libzim archives and FAISS indexes close when their last Python
        reference disappears. Clearing them here matters on Windows, where an
        open file cannot be deleted or replaced.
        """
        index.searcher = None
        index.archive = None
        index.faiss_index = None
        index.random_article_ids = None

    @staticmethod
    def _close_connections(connections: list[sqlite3.Connection | None]) -> None:
        for conn in connections:
            if conn is None:
                continue
            try:
                conn.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def _acquire_index(index: _LocalIndex) -> None:
        with index._lifecycle:
            if index._retired:
                raise RuntimeError("index was retired during reload")
            index._active_searches += 1

    @staticmethod
    def _release_index(index: _LocalIndex) -> None:
        with index._lifecycle:
            index._active_searches -= 1
            should_close = (
                index._retired
                and index._active_searches == 0
                and not index._closed
            )
            if should_close:
                index._closed = True
                connections = [index.db, *index._conns]
                Search._release_native_resources(index)
            else:
                connections = []
        if connections:
            Search._close_connections(connections)

    def _db_for(self, index: _LocalIndex) -> sqlite3.Connection | None:
        """A read-only SQLite connection per worker thread, so concurrent
        searches on the same index do not share one connection."""
        if index.db_path is None:
            return index.db
        conn = getattr(index._thread, "conn", None)
        if conn is None:
            conn = sqlite3.connect(index.db_path, uri=True, check_same_thread=False)
            index._thread.conn = conn
            with index.lock:
                index._conns.append(conn)
        return conn

    def refresh_local_sources(self) -> list[dict[str, Any]]:
        """Rebuild the source set from local indexes only (never touches the network)."""
        return self._rebuild_sources([])

    def refresh_sources(self) -> list[dict[str, Any]]:
        """Refresh source metadata without making local indexes unavailable.

        This includes the optional Kiwix catalog, so it performs a blocking HTTP
        request when ``kiwix_server`` is configured. Startup calls
        ``refresh_local_sources`` instead and refreshes the catalog in the
        background via ``start_catalog_refresh``.
        """
        server = str(self.cfg.get("kiwix_server") or "").strip().rstrip("/")
        if not server:
            with self._state_lock:
                self._catalog_entries_cache = []
                self._catalog_server = None
            entries = []
        else:
            fetched = self._catalog_entries(server)
            if fetched is None:
                with self._state_lock:
                    cached_entries = getattr(self, "_catalog_entries_cache", [])
                    entries = (
                        list(cached_entries)
                        if getattr(self, "_catalog_server", None) == server
                        else []
                    )
                logger.warning(
                    "zimantic: catalog unavailable; preserving %d cached source(s)",
                    len(entries),
                )
            else:
                with self._state_lock:
                    self._catalog_entries_cache = [dict(entry) for entry in fetched]
                    self._catalog_server = server
                entries = fetched
        return self._rebuild_sources(entries)

    def start_catalog_refresh(self) -> None:
        """Fetch the optional Kiwix catalog off the startup path.

        Catalog sources are only needed for books without a local index, so the
        server must not wait (up to ``source_timeout``) for Kiwix to answer.
        """
        server = str(self.cfg.get("kiwix_server") or "").strip()
        if not server:
            return

        def _refresh() -> None:
            try:
                sources = self.refresh_sources()
            except Exception as error:  # a catalog failure must not affect serving
                logger.warning("zimantic: catalog refresh failed (%s)", error)
                return
            logger.info("zimantic: Kiwix catalog ready (%d source(s))", len(sources))

        threading.Thread(target=_refresh, name="zimantic-catalog", daemon=True).start()

    def _source_from_catalog(
        self,
        record,
        *,
        key: str,
        local_name: str,
        searchable: bool | None = None,
    ) -> SourceInfo:
        """Build one local ``SourceInfo`` from a catalog row.

        Used by both the searchable-source rebuild and the installed-library
        view so metadata, availability, icon, and perma-ref handling cannot
        drift between the two.
        """
        meta = record.to_source_metadata()
        available = bool(record.available)
        error = None
        if not available:
            error = record.last_error or "ZIM archive is unavailable"
        if searchable is None:
            searchable = available
        searchable = bool(searchable) and available
        icon_url = _source_icon_url(
            local_name,
            bool(record.favicon or meta["icon_path"]),
            self.web_base_path,
        )
        return _source_info_from_metadata(
            meta,
            key=key,
            local_name=local_name,
            available=available,
            searchable=searchable,
            icon_url=icon_url,
            error=error,
        )

    def _rebuild_sources(self, entries: list[dict[str, str]]) -> list[dict[str, Any]]:
        with self._state_lock:
            local: dict[str, SourceInfo] = {}
            for local_name in self.indexes:
                index = self.indexes[local_name]
                key = _zim_key(local_name)
                if key in local:
                    # Two local ZIMs can share a base name (a dated archive and
                    # an undated one). Fall back to the unique index name, and
                    # add a numeric suffix if even that is taken.
                    key = local_name
                    suffix = 2
                    while key in local:
                        key = f"{local_name}-{suffix}"
                        suffix += 1
                    logger.info(
                        "zimantic: %s shares a base name with another local "
                        "source; exposing the variant as %r",
                        local_name,
                        key,
                    )
                record = self._ensure_catalog().get_stem_size(
                    local_name, index.source_size
                )
                if record is not None:
                    local[key] = self._source_from_catalog(
                        record,
                        key=key,
                        local_name=local_name,
                    )
                    continue

                # The catalog is authoritative for archive metadata. Keep a
                # minimal searchable source if its row is unavailable.
                local[key] = SourceInfo(
                    key=key,
                    name=local_name,
                    book=local_name,
                    mode="local",
                    local_name=local_name,
                    intent_phrases=_intent_phrases(local_name),
                    size=index.source_size,
                )

            if local:
                chosen = _choose_sources(
                    list(local.values()),
                    lambda source: _book_identity(
                        source.local_name or source.name or source.key
                    ),
                )
                # Reassign keys after choosing a winner so the surviving
                # version gets the canonical date-stripped key instead of a
                # collision suffix assigned to a now-hidden sibling.
                local = {}
                used_keys: set[str] = set()
                for source in chosen:
                    local_name = source.local_name or source.name or source.key
                    key = _zim_key(local_name)
                    if key in used_keys:
                        key = source.key or local_name
                    suffix = 2
                    while key in used_keys:
                        key = f"{local_name}-{suffix}"
                        suffix += 1
                    used_keys.add(key)
                    local[key] = replace(source, key=key)

            merged = dict(local)
            local_identities = {
                _book_identity(
                    source.local_name or source.name or source.key
                )
                for source in local.values()
            }
            remote_identities: set[str] = set()
            for entry in entries:
                identity = _book_identity(entry["book"])
                key = _zim_key(entry["book"])
                if (
                    identity in local_identities
                    or identity in remote_identities
                    or merged.get(key) is not None
                ):
                    # A local source is authoritative for its own identity.
                    # Merging the remote catalog's display name here used to
                    # overwrite the local name (and made "is this indexed?"
                    # checks look at the wrong field); the local catalog and
                    # archive already provide the title.
                    continue
                remote_identities.add(identity)
                merged[key] = SourceInfo(
                    key=key,
                    name=entry["name"],
                    book=entry["book"],
                    mode="kiwix",
                    available=True,
                    intent_phrases=_intent_phrases(entry["book"]),
                )

            old_sources = self.sources
            self.sources = dict(sorted(merged.items(), key=lambda pair: pair[1].name.casefold()))
            changed = self.sources != old_sources
            if changed:
                # Bump the generation in the same critical section that swaps
                # the source snapshot, so a concurrent search can never pair
                # the old source list with the new generation.
                self._cache_generation += 1
            sources = self.source_dicts()
        if changed:
            # The set of searchable sources changed, so prior answers can be stale.
            # Keeping the cache while the source set is unchanged lets repeated
            # queries (for example, page reloads) be answered without recomputing.
            self._invalidate_library_cache()
            self._remote_limit_warned = False
            self.cache.clear()
            preview_cache = self.preview_cache
            if preview_cache is not None:
                preview_cache.clear()
        return sources

    def _catalog_entries(self, server: str) -> list[dict[str, str]] | None:
        return catalog_entries(
            self.cfg,
            server,
            self.source_timeout,
            urlopen_fn=urlopen,
            sleep_fn=time.sleep,
        )

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
        """Return metadata for every local ZIM in the library.

        Unreadable archives are included with ``available: false`` so the UI can
        explain why a ZIM cannot be opened. Readable archives without a
        resolvable homepage are also included; the frontend can offer a random
        article instead of a homepage link.
        """
        with self._state_lock:
            indexed = {
                source.local_name: source
                for source in self.sources.values()
                if source.mode == "local" and source.local_name
            }
            searchable_names = set(self.indexes)
        self._scan_catalog(force=False)
        catalog = self._ensure_catalog()
        # One card per UUID, then one per book identity (dated and
        # flavour variants of the same local book collapse together). Prefer a
        # readable, searchable copy, then the newest, fuller archive.
        all_sources: list[SourceInfo] = []
        for zim_name, _zim_path in sorted(
            self._scan_zim_paths(refresh=False).items()
        ):
            record = catalog.get_stem(zim_name)
            source = (
                self._source_from_catalog(
                    record,
                    key=_zim_key(zim_name),
                    local_name=zim_name,
                    searchable=zim_name in searchable_names,
                )
                if record is not None
                else indexed.get(zim_name)
            )
            if source is None:
                continue
            all_sources.append(source)
        chosen = _choose_sources(
            all_sources,
            lambda source: (
                f"uuid:{source.uuid}"
                if source.uuid
                else f"name:{source.name or source.local_name or source.key}"
            ),
        )
        chosen = _choose_sources(
            chosen,
            lambda source: _book_identity(
                source.local_name or source.name or source.key
            ),
        )
        return [
            source.to_dict(include_metadata=True)
            for source in chosen
        ]

    def library_version(
        self,
    ) -> tuple[tuple[int, ...], tuple[int, ...], tuple[tuple[str, int, int], ...]]:
        """Return a cheap token that changes when the library contents change."""
        self._scan_catalog(force=False)
        lock = self._library_cache_lock()
        now = time.monotonic()
        with lock:
            cached = getattr(self, "_library_version_cache", None)
            cached_at = getattr(self, "_library_version_at", 0.0)
            token = getattr(self, "_library_scan_token", 0)
            if cached is not None and now - cached_at < LIBRARY_SCAN_TTL_SECONDS:
                return cached
        catalog = self._ensure_catalog()
        records_by_path = {
            Path(record.path): record for record in catalog.rows()
        }
        selected = largest_zims_by_stem(
            {path: record.size for path, record in records_by_path.items()}
        )
        signatures = tuple(
            (
                name,
                records_by_path[path].size,
                int(records_by_path[path].mtime * 1_000_000_000),
            )
            for name, path in sorted(selected.items())
        )
        version = (self.source_version(), catalog.version(), signatures)
        with lock:
            # A reload during the scan already invalidated the previous token;
            # do not let this older signature set be cached under the new one.
            if getattr(self, "_library_scan_token", 0) == token:
                self._library_version_cache = version
                self._library_version_at = time.monotonic()
        return version

    def source_version(self) -> tuple[int, ...]:
        """Token that changes whenever the source set or its metadata changes."""
        with self._state_lock:
            return (self._cache_generation,)

    def close(self) -> None:
        """Release the search thread pool and every open index."""
        pool = self.fulltext_pool
        self.fulltext_pool = None
        if pool is not None:
            pool.close()
        executor = getattr(self, "_executor", None)
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        with self._state_lock:
            indexes = list(self.indexes.values())
            self.indexes.clear()
        for index in indexes:
            self._close_index(index)
        catalog = self.catalog
        if catalog is not None:
            catalog.close()
            self.catalog = None

    def local_names(self) -> list[str]:
        with self._state_lock:
            return list(self.indexes)

    @staticmethod
    def _article_path(path: str) -> str:
        return normalize_article_path(path)

    @staticmethod
    def _resolve_article_entry(archive: Archive, path: str | None = None):
        return resolve_article_entry(archive, path)

    def _read_archive_article(
        self,
        archive: Archive,
        zim_name: str,
        path: str | None,
        head_only: bool,
    ) -> dict[str, Any]:
        return read_archive_article(
            archive,
            zim_name,
            path,
            head_only,
            self.max_article_bytes,
        )

    def read_article(
        self,
        zim_name: str,
        path: str | None = None,
        head_only: bool = False,
    ) -> dict[str, Any]:
        """Read one local ZIM entry without changing indexes or search state.

        ``head_only`` returns just the leading bytes of the entry, which is all
        the ``/content/`` shell needs to read a title and favicon from the
        ``<head>``; the shell's iframe fetches the full entry separately, so
        loading the whole (possibly tens-of-MB) article for the shell would be
        a wasted read.
        """
        with self._state_lock:
            index = self.indexes.get(zim_name)
        if index is None:
            record = self._ensure_catalog().get_stem(zim_name)
            zim_path = (
                Path(record.path)
                if record is not None and record.available
                else self._scan_zim_paths().get(zim_name)
            )
            if zim_path is None:
                raise ArticleSourceNotFoundError("ZIM not found")
            try:
                archive = Archive(zim_path)
            except (OSError, RuntimeError, ValueError) as error:
                raise ArticleUnavailableError("ZIM archive is unavailable") from error
            return self._read_archive_article(archive, zim_name, path, head_only)
        archive = index.ensure_open()
        if archive is None:
            raise ArticleUnavailableError("ZIM archive is unavailable")

        acquired = False
        try:
            try:
                self._acquire_index(index)
            except RuntimeError as error:
                raise ArticleUnavailableError("ZIM is being reloaded") from error
            acquired = True
            with index.read_lock:
                return self._read_archive_article(
                    archive,
                    zim_name,
                    path,
                    head_only,
                )
        finally:
            if acquired:
                self._release_index(index)

    def read_article_head(
        self, zim_name: str, path: str | None = None
    ) -> dict[str, Any]:
        """Read only the leading bytes of one entry (see ``read_article``)."""
        return self.read_article(zim_name, path, head_only=True)

    def article_path_exists(self, zim_name: str, path: str) -> bool:
        """Whether ``path`` resolves to a local ZIM entry.

        The article toolbar uses this to avoid linking breadcrumb containers
        such as ``A`` in ``A/Article``: those path segments are namespaces or
        directories, not articles, so opening them would show a 404.
        """
        try:
            requested_path = self._article_path(path)
            with self._state_lock:
                index = self.indexes.get(zim_name)
            if index is None:
                record = self._ensure_catalog().get_stem(zim_name)
                zim_path = (
                    Path(record.path)
                    if record is not None and record.available
                    else self._scan_zim_paths().get(zim_name)
                )
                if zim_path is None:
                    return False
                try:
                    archive = Archive(zim_path)
                except (OSError, RuntimeError, ValueError):
                    return False
                self._resolve_article_entry(archive, requested_path)
                return True
            archive = index.ensure_open()
            if archive is None:
                return False

            acquired = False
            try:
                try:
                    self._acquire_index(index)
                except RuntimeError:
                    return False
                acquired = True
                with index.read_lock:
                    self._resolve_article_entry(archive, requested_path)
                    return True
            finally:
                if acquired:
                    self._release_index(index)
        except (ArticleNotFoundError, ArticleUnavailableError):
            return False

    def _read_archive_icon(
        self,
        archive: Archive,
        zim_name: str,
        icon_path: str | None,
    ) -> dict[str, Any]:
        return read_archive_icon(
            archive,
            zim_name,
            icon_path,
            self.max_article_bytes,
        )

    def read_source_icon(self, zim_name: str) -> dict[str, Any]:
        """Read the illustration selected for a local ZIM."""
        with self._state_lock:
            source = next(
                (
                    source
                    for source in self.sources.values()
                    if source.local_name == zim_name
                ),
                None,
            )
        # Consult the catalog even when the ZIM has no search source (for
        # example a library-only archive or a duplicate removed from search).
        # The favicon BLOB is the same illustration selected during discovery.
        data, mime = self._ensure_catalog().favicon(zim_name)
        if data:
            return {
                "zim": zim_name,
                "path": None,
                "title": (
                    source.title if source is not None and source.title else zim_name
                ),
                "mimetype": (mime or "application/octet-stream")
                .split(";", 1)[0]
                .strip()
                .lower(),
                "content": data,
            }
        if source is not None and source.mode == "local" and source.icon_path:
            return self.read_article(zim_name, source.icon_path)
        if source is None:
            zim_path = self._scan_zim_paths().get(zim_name)
            if zim_path is None:
                raise ArticleNotFoundError("source icon not found")
            try:
                archive = Archive(zim_path)
            except (OSError, RuntimeError, ValueError) as error:
                raise ArticleUnavailableError("source icon unavailable") from error
            try:
                homepage = self._resolve_article_entry(archive).path
            except (ArticleNotFoundError, ArticleUnavailableError, RuntimeError, ValueError):
                homepage = None
            icon_path = _find_archive_icon_path(archive, homepage)
            return self._read_archive_icon(archive, zim_name, icon_path)
        if source.mode != "local":
            raise ArticleNotFoundError("source icon not found")
        with self._state_lock:
            index = self.indexes.get(zim_name)
        if index is None:
            raise ArticleUnavailableError("source icon unavailable")
        archive = index.ensure_open()
        if archive is None:
            raise ArticleUnavailableError("source icon unavailable")
        acquired = False
        try:
            try:
                self._acquire_index(index)
            except RuntimeError as error:
                raise ArticleUnavailableError("ZIM is being reloaded") from error
            acquired = True
            with index.read_lock:
                return self._read_archive_icon(archive, zim_name, None)
        finally:
            if acquired:
                self._release_index(index)

    @staticmethod
    def _is_random_article_entry(entry: Any) -> bool:
        return is_random_article_entry(entry)

    def _random_indexed_article(self, index: _LocalIndex, db: sqlite3.Connection) -> str | None:
        return random_indexed_article(
            index,
            db,
            attempts=RANDOM_ARTICLE_ATTEMPTS,
        )

    def _random_fallback_article(self, index: _LocalIndex) -> str | None:
        return random_fallback_article(
            index,
            attempts=RANDOM_ARTICLE_ATTEMPTS,
            get_entry_fn=get_entry_by_id,
        )

    def random_local_source(self) -> SourceInfo:
        """Return a random readable local source that has a homepage.

        Used by the article toolbar's alt-click Random action and by the library
        fallback for archives without a main entry. The caller redirects to the
        chosen source's homepage.
        """
        with self._state_lock:
            candidates = [
                source
                for source in self.sources.values()
                if source.mode == "local"
                and source.available
                and source.local_name
                and source.homepage
            ]
        if not candidates:
            raise ArticleNotFoundError("no local source has a homepage")
        return secrets.choice(candidates)

    def random_article_path(self, zim_name: str) -> str:
        """Return a random indexed article path without changing search state."""
        with self._state_lock:
            index = self.indexes.get(zim_name)
        if index is None:
            raise ArticleSourceNotFoundError("ZIM not found")
        if index.ensure_open() is None:
            raise ArticleUnavailableError("ZIM archive is unavailable")

        acquired = False
        try:
            try:
                self._acquire_index(index)
            except RuntimeError as error:
                raise ArticleUnavailableError("ZIM is being reloaded") from error
            acquired = True
            db = self._db_for(index)
            with index.read_lock:
                try:
                    if db is None:
                        path = self._random_fallback_article(index)
                    else:
                        path = self._random_indexed_article(index, db)
                except (sqlite3.Error, KeyError, RuntimeError, TypeError, ValueError, IndexError) as error:
                    raise ArticleUnavailableError(
                        "random article selection is unavailable"
                    ) from error
            if not path:
                raise ArticleNotFoundError("ZIM has no available articles")
            return self._article_path(path)
        finally:
            if acquired:
                self._release_index(index)

    def health(self) -> dict[str, Any]:
        """Return read-only index, archive, and capability diagnostics."""
        with self._state_lock:
            indexes = sorted(self.indexes.items())
            source_count = len(self.sources)
            embedder_loaded = self.embedder is not None

        reports: list[dict[str, Any]] = []
        for name, index in indexes:
            reasons: list[str] = []
            index_dir = self.cfg.get("index_dir")
            db_path = Path(index_dir) / f"{name}.sqlite" if index_dir else None
            db_current = (
                index.db_path is None
                or db_path is None
                or _file_signature(db_path) == index.db_signature
            )
            if not db_current:
                reasons.append("index file changed since load")

            record = self._ensure_catalog().get_stem_size(name, index.source_size)
            archive_path = (
                Path(index.zim_path)
                if index.zim_path
                else self._find_zim_path(name, index.source_size)
            )
            archive_current = (
                archive_path is not None
                and archive_path.exists()
                and (record is None or bool(record.available))
            )
            if not archive_current:
                reasons.append("ZIM archive unavailable")

            vectors_expected = (
                self.semantic
                and index.done == "1"
                and not self._defer_faiss
            )
            vectors_loaded = index.faiss_index is not None
            if vectors_expected and not vectors_loaded:
                reasons.append("semantic vectors unavailable")

            fulltext_capability = (
                bool(record.has_fulltext_index) and bool(record.available)
                if record is not None
                else index.ensure_searcher() is not None
            )
            report: dict[str, Any] = {
                "name": name,
                "status": "ok" if not reasons else "degraded",
                "index_file": index.db_path is not None and db_current,
                "archive": archive_current,
                "capabilities": {
                    "title": index.db_path is not None,
                    "fulltext": fulltext_capability,
                    "semantic": vectors_loaded,
                    "article": archive_current,
                },
            }
            if reasons:
                report["issues"] = reasons

            if record is not None:
                report["entry_count"] = record.entry_count
                report["homepage"] = record.homepage or None
                report["article_count"] = record.article_count
                report["media_count"] = record.media_count
                if record.last_error:
                    report["issues"] = [*reasons, record.last_error]
                    report["status"] = "degraded"
            elif archive_current:
                acquired = False
                try:
                    self._acquire_index(index)
                    acquired = True
                    with index.read_lock:
                        archive = index.ensure_open()
                        if archive is not None:
                            report["entry_count"] = int(archive.entry_count)
                            try:
                                report["homepage"] = self._resolve_article_entry(archive).path
                            except (ArticleNotFoundError, ArticleUnavailableError):
                                report["homepage"] = None
                except (RuntimeError, ValueError):
                    report["status"] = "degraded"
                    report.setdefault("issues", []).append("archive check failed")
                finally:
                    if acquired:
                        self._release_index(index)
            reports.append(report)

        status = "ok"
        if not reports or any(report["status"] != "ok" for report in reports):
            status = "degraded"
        return {
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
        """Limit remote catalog fan-out for an unscoped search."""
        limit = getattr(
            self,
            "max_remote_sources",
            _int_config(
                self.cfg,
                "max_remote_sources",
                DEFAULT_MAX_REMOTE_SOURCES,
                minimum=0,
            ),
        )
        remote = [source for source in available if source.mode == "kiwix"]
        if len(remote) <= limit:
            return available
        kept = {source.key for source in remote[:limit]}
        if not getattr(self, "_remote_limit_warned", False):
            logger.warning(
                "zimantic: %d remote Kiwix sources available; searching the first %d "
                "(set max_remote_sources to change this)",
                len(remote),
                limit,
            )
            self._remote_limit_warned = True
        return [
            source
            for source in available
            if source.mode != "kiwix" or source.key in kept
        ]

    @staticmethod
    def _select_sources(
        available: list[SourceInfo], zim: list[str]
    ) -> list[SourceInfo]:
        wanted = set(zim)
        # Match the same aliases that a Kiwix catalog link may use: a remote
        # book slug carries a date suffix, while a local index is identified by
        # its date-stripped key. Comparing base keys preserves those links
        # without overwriting the local source's own name/book metadata.
        wanted_keys = {_zim_key(value) for value in wanted}
        wanted_books = {
            _book_identity(value) for value in wanted if str(value or "").strip()
        }
        return [
            source
            for source in available
            if wanted.intersection({source.key, source.name, source.book, source.local_name})
            or source.key in wanted_keys
            or _book_identity(
                source.local_name or source.name or source.key
            ) in wanted_books
        ]

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
            available = [source for source in self.sources.values() if source.available]
        bounded = self._bounded_available_sources(available)
        if zim is None:
            return bounded, False, None
        if not zim:
            # An explicit empty list is a deliberately empty scope (the cache
            # key distinguishes it from None); it must not mean "all sources".
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
            return (
                selected,
                bool(selected),
                tuple(sorted(source.key for source in selected)),
            )
        if any(source.key not in bounded_keys for source in selected):
            # The user named a source that the unscoped remote cap would have
            # dropped. Honor the explicit selection instead of silently
            # searching a different set.
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
        """Embed the query only when a selected local index actually has vectors.
        A failed embedding degrades to title + full-text rather than failing."""
        # Read once: start_embedder may swap this in from a worker thread.
        embedder = self.embedder
        if embedder is None or not self.semantic:
            return None
        with self._state_lock:
            indexes = dict(self.indexes)
        wants_semantic = any(
            source.mode == "local"
            and source.local_name in indexes
            and indexes[source.local_name].semantic
            for source in sources
        )
        if not wants_semantic:
            return None
        try:
            return embedder.embed([f"query: {query}"])
        except Exception as error:
            logger.warning(
                "zimantic: query embedding failed (%s); falling back to title and full-text (lexical) search",
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
        prefix_query = _title_prefix_query(query)
        exact_query = _title_query(query)
        if not prefix_query:
            return []
        with self._state_lock:
            available = [
                source for source in self.sources.values() if source.available
            ]
        # Honor the requested ZIM scope directly instead of going through
        # `_search_scope`, which promotes small-library selections to every
        # source for cache sharing and falls back to all sources when a name is
        # unknown.
        sources = available if zim is None else self._select_sources(available, zim)
        suggestions: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for source in sources:
            remaining = limit - len(suggestions)
            if remaining <= 0:
                break
            if source.mode != "local" or not source.local_name:
                continue
            with self._state_lock:
                index = self.indexes.get(source.local_name)
            if index is None:
                continue
            try:
                self._acquire_index(index)
            except RuntimeError:
                continue
            try:
                db = self._db_for(index)
                if db is None:
                    continue
                # Redirect rows and their targets both match the title query
                # but resolve to the same article. Fetch extra candidates and
                # widen the window until the limit is filled or the index runs
                # out, so collapsing duplicates cannot leave the list short.
                query_text = prefix_query
                window = remaining
                max_window = max(remaining * 8, 64)
                while True:
                    rows = db.execute(
                        "SELECT rowid FROM docs WHERE docs MATCH ? "
                        "ORDER BY rank LIMIT ?",
                        (query_text, window),
                    ).fetchall()
                    if not rows and query_text == prefix_query:
                        # Exact token matching catches truncated or base forms
                        # only when the prefix form finds nothing.
                        query_text = exact_query
                        continue
                    rowids = [int(row[0]) for row in rows]
                    docs = self._fetch_docs(source, db, set(rowids))
                    for rowid in rowids:
                        doc = docs.get(rowid)
                        if doc is None:
                            continue
                        doc_id = str(doc.get("id") or "")
                        if doc_id in seen_ids:
                            continue
                        seen_ids.add(doc_id)
                        suggestions.append(doc)
                    if (
                        len(rows) < window
                        or len(suggestions) >= limit
                        or window >= max_window
                    ):
                        break
                    window = min(max_window, window * 2)
            except sqlite3.Error as error:
                logger.warning(
                    "zimantic: title suggestions unavailable for %s: %s",
                    source.local_name,
                    error,
                )
            finally:
                self._release_index(index)
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
            "done", state, total_sources, total_sources, offset, page_size, matched_by
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
        if not selective and current_generation == generation:
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
            for src in sources:
                futures.append(
                    self._executor.submit(
                        self._run_source_task,
                        stop,
                        src,
                        query,
                        title_query,
                        fulltext_query,
                        query_vector,
                        source_limit,
                    )
                )

            future_sources = dict(zip(futures, sources))
            started_at = time.monotonic()
            last_rank_at = started_at
            rank_passes = 0
            unranked = 0
            # Ids of the page last sent to the client, so a snapshot that didn't
            # move the page can omit its (large, unchanged) result list.
            last_page_signature: tuple[str, ...] | None = None
            remaining = set(futures)
            while remaining:
                timed_out = False
                try:
                    for future in as_completed(
                        remaining,
                        timeout=update_interval if update_interval > 0 else None,
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
                except TimeoutError:
                    timed_out = True
                if not remaining:
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
            )
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
            return SourceResult(source=source, status="cancelled")
        return self._search_source(source, *args)

    def _search_source(
        self,
        source: SourceInfo,
        query: str,
        title_query: str,
        fulltext_query: str,
        query_vector,
        limit: int,
    ) -> SourceResult:
        if source.mode == "kiwix":
            return self._search_kiwix(source, fulltext_query, limit)
        if not source.local_name:
            return SourceResult(
                source=replace(source, available=False),
                error="source is not indexed",
                status="error",
            )
        return self._search_local(source, query, title_query, fulltext_query, query_vector, limit)

    def _search_local(
        self,
        source: SourceInfo,
        query: str,
        title_query: str,
        fulltext_query: str,
        query_vector,
        limit: int,
    ) -> SourceResult:
        with self._state_lock:
            index = self.indexes.get(source.local_name)
            if index is None:
                return SourceResult(
                    source=replace(source, available=False),
                    error="source is not indexed",
                    status="error",
                )
            self._acquire_index(index)
        try:
            return self._search_local_locked(
                source,
                query,
                title_query,
                fulltext_query,
                query_vector,
                limit,
                index,
            )
        finally:
            self._release_index(index)

    def _search_local_locked(
        self,
        source: SourceInfo,
        query: str,
        title_query: str,
        fulltext_query: str,
        query_vector,
        limit: int,
        index: _LocalIndex,
    ) -> SourceResult:
        count = max(self.candidate_count, limit)
        db = self._db_for(index)
        semantic: list[tuple[float, dict[str, Any]]] = []
        keyword: list[tuple[float, dict[str, Any]]] = []
        fulltext: list[tuple[int, dict[str, Any]]] = []
        rowids: set[int] = set()
        archive_entries: dict[int, Any] = {}
        errors: list[str] = []

        # FAISS search is read-only and thread-safe; skip it when this index has
        # no vectors or the query could not be embedded (fast mode).
        if query_vector is not None and index.faiss_index is not None:
            try:
                similarities, ids = index.faiss_index.search(query_vector, count)
                semantic_ids = [
                    (float(score), int(rowid))
                    for score, rowid in zip(similarities[0], ids[0])
                    if rowid >= 0
                ]
                rowids.update(rowid for _, rowid in semantic_ids)
            except Exception as error:
                errors.append(f"semantic search unavailable: {error}")
                semantic_ids = []
        else:
            semantic_ids = []

        if title_query and db is not None:
            title_search = (
                "SELECT rowid, bm25(docs) FROM docs "
                "WHERE docs MATCH ? ORDER BY rank LIMIT ?"
            )
            title_rows = db.execute(title_search, (title_query, count)).fetchall()
            if not title_rows:
                # Exact title matching found nothing; retry with prefix tokens so a
                # truncated or base-form word ("chang", "change" -> "Changes") still
                # surfaces its title without broadening the common full-word case.
                title_rows = db.execute(
                    title_search, (_title_prefix_query(query), count)
                ).fetchall()
            for rowid, bm25 in title_rows:
                rowids.add(int(rowid))
                keyword.append((float(bm25), {"rowid": int(rowid)}))

        fulltext_rows: list[tuple[int, int]] = []
        searcher = None
        archive = index.ensure_open()
        if self.fulltext_pool is not None:
            fulltext_capable = bool(
                archive is not None
                and index.zim_path
                and index.has_fulltext_index is not False
                and getattr(archive, "has_fulltext_index", False)
            )
        else:
            searcher = index.ensure_searcher()
            fulltext_capable = searcher is not None and archive is not None
        if db is None and not fulltext_capable and index.faiss_index is None:
            # Nothing can answer this source: no SQLite index, no libzim
            # full-text searcher and no vectors. Report an error instead of a
            # successful empty result so a missing/corrupt archive is visible.
            return SourceResult(
                source=replace(source, available=False),
                error=index.error or "ZIM archive is unavailable",
                status="error",
            )
        if fulltext_capable:
            paths: list[str] = []
            if self.fulltext_pool is not None:
                # Native libzim iteration aborts the interpreter on some
                # corrupt full-text indexes, so it runs in a helper process.
                paths = (
                    self.fulltext_pool.search(
                        index.zim_path or "",
                        fulltext_query or query,
                        count,
                        index.zim_signature,
                    )
                    or []
                )
            elif searcher is not None:
                # libzim's Searcher is not documented as thread-safe: serialize it.
                with index.lock:
                    try:
                        search = searcher.search(
                            Query().set_query(fulltext_query or query)
                        )
                        paths = search_paths(search, 0, count)
                    except (RuntimeError, ValueError) as error:
                        errors.append(f"full-text search unavailable: {error}")
            if archive is not None:
                for rank, path in enumerate(paths):
                    try:
                        entry = archive.get_entry_by_path(path)
                        rowid = entry._index
                        rowids.add(rowid)
                        archive_entries[rowid] = entry
                        fulltext_rows.append((rank, rowid))
                    except (KeyError, RuntimeError, ValueError):
                        continue

        docs = (
            self._fetch_docs(source, db, rowids)
            if db is not None
            else self._fetch_archive_docs(source, archive, archive_entries)
        )
        keyword = [(score, docs[row["rowid"]]) for score, row in keyword if row["rowid"] in docs]
        for rank, rowid in fulltext_rows:
            doc = docs.get(rowid)
            if doc:
                fulltext.append((rank, doc))
        # Keep weak meaning matches only when this source has no lexical
        # candidates. With title or full-text hits the cosine floor removes
        # noise; without them it used to turn a meaning-only query into an
        # empty result list, because every semantic candidate was discarded.
        if keyword or fulltext:
            minimum_similarity = _float_config(
                self.cfg,
                "min_cosine_similarity",
                DEFAULT_MIN_COSINE_SIMILARITY,
                -1.0,
                1.0,
            )
            semantic_ids = [
                (score, rowid)
                for score, rowid in semantic_ids
                if score >= minimum_similarity
            ]
        for score, rowid in semantic_ids:
            doc = docs.get(rowid)
            if doc:
                semantic.append((score, doc))

        items = self._source_items(semantic, keyword, fulltext)
        return SourceResult(
            source=source,
            items=items,
            semantic=semantic,
            keyword=keyword,
            fulltext=fulltext,
            error="; ".join(errors) or None,
            status="partial" if errors else "ok",
        )

    def _result_doc(
        self, source: SourceInfo, path: str, title: str | None
    ) -> dict[str, Any]:
        """One local result document shared by indexed and archive fallbacks."""
        return {
            "id": f"{source.key}:{path}",
            "source_key": source.key,
            "source": source.name,
            "source_icon": source.icon_url,
            "title": title or path,
            "lead": "",
            "path": path,
            "mimetype": None,
            "url": (
                f"{self.web_base_path}/content/"
                f"{quote(source.local_name or source.book, safe='')}/"
                f"{quote(path, safe='/')}"
            ),
        }

    def _fetch_archive_docs(
        self,
        source: SourceInfo,
        archive: Archive | None,
        entries: dict[int, Any],
    ) -> dict[int, dict[str, Any]]:
        """Build result documents directly from native ZIM search hits."""
        if archive is None:
            return {}
        docs: dict[int, dict[str, Any]] = {}
        for rowid, entry in entries.items():
            try:
                target = self._resolve_article_entry(archive, entry.path)
            except (ArticleNotFoundError, ArticleUnavailableError):
                continue
            page_path = target.path
            title = getattr(target, "title", None) or page_path
            docs[rowid] = self._result_doc(source, page_path, title)
        return docs

    def _fetch_docs(
        self,
        source: SourceInfo,
        db: sqlite3.Connection,
        rowids: set[int],
    ) -> dict[int, dict[str, Any]]:
        if not rowids:
            return {}
        rows: dict[int, tuple[Any, ...]] = {}
        def fetch(ids) -> None:
            values = list(ids)
            for start in range(0, len(values), 900):
                batch = values[start:start + 900]
                placeholders = ",".join("?" for _ in batch)
                query = (
                    "SELECT rowid, title, path, target FROM docs "
                    f"WHERE rowid IN ({placeholders})"
                )
                rows.update({int(row[0]): row for row in db.execute(query, batch)})

        values = list(rowids)
        fetch(values)
        # Older indexes may contain redirect-to-redirect rows, so follow every
        # target chain before rendering the result. Indexing now stores the
        # final target, but this keeps existing indexes correct too.
        pending = {
            int(row[3]) for row in rows.values() if row[3] is not None
        } - set(rows)
        seen: set[int] = set()
        for _ in range(32):
            if not pending:
                break
            fetch(pending)
            seen.update(pending)
            pending = {
                int(row[3]) for row in rows.values() if row[3] is not None
            } - set(rows) - seen

        def final_row(rowid: int):
            current_id = rowid
            visited: set[int] = set()
            for _ in range(32):
                row = rows.get(current_id)
                if row is None:
                    return None
                if row[3] is None:
                    return current_id, row
                visited.add(current_id)
                next_id = int(row[3])
                if next_id in visited:
                    return None
                current_id = next_id
            return None

        docs: dict[int, dict[str, Any]] = {}
        for rowid in values:
            resolved = final_row(rowid)
            if not resolved:
                continue
            _, (_, title, path, _) = resolved
            docs[rowid] = self._result_doc(source, path, title)
        return docs

    def _extract_previews(
        self,
        local_name: str,
        docs: list[dict[str, Any]],
    ) -> dict[str, str]:
        """Extract previews for one local source while holding its archive lock."""
        with self._state_lock:
            index = self.indexes.get(local_name)
        if index is None:
            return {doc["id"]: "" for doc in docs}
        archive = index.ensure_open()
        if archive is None:
            return {doc["id"]: "" for doc in docs}

        try:
            self._acquire_index(index)
        except RuntimeError:
            return {doc["id"]: "" for doc in docs}

        previews: dict[str, str] = {}
        try:
            with index.read_lock:
                for doc in docs:
                    try:
                        entry = archive.get_entry_by_path(doc["path"])
                        row = read_entry(
                            archive,
                            entry._index,
                            max_html_bytes=self.max_html_bytes,
                            max_entry_bytes=self.max_article_bytes,
                            max_preview_chars=self.preview_chars,
                            timeout=DEFAULT_PARSE_TIMEOUT,
                        )
                    except (KeyError, OSError, RuntimeError, ValueError) as error:
                        logger.warning(
                            "%s: preview unavailable for %s (%s)",
                            local_name,
                            doc["path"],
                            error,
                        )
                        previews[doc["id"]] = ""
                        continue
                    if row:
                        excerpt = getattr(row, "excerpt", row[2])
                    else:
                        excerpt = ""
                    previews[doc["id"]] = self._preview(excerpt)
        finally:
            self._release_index(index)
        return previews

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
        preview_cache = self.preview_cache
        if preview_cache is None:
            preview_cache = QueryCache(0, 0)
            self.preview_cache = preview_cache

        grouped: dict[str, list[dict[str, Any]]] = {}
        for doc in docs:
            source = sources.get(doc.get("source_key"))
            if source is None or source.mode != "local" or not source.local_name:
                continue
            key = (source.key, doc["path"])
            cached = preview_cache.get(key)
            if cached is not None:
                doc["lead"] = cached
                continue
            if doc.get("lead"):
                continue
            grouped.setdefault(source.local_name, []).append(doc)

        futures = {
            self._executor.submit(
                self._extract_previews, local_name, source_docs
            ): (local_name, source_docs)
            for local_name, source_docs in grouped.items()
        }
        for future in as_completed(futures):
            local_name, source_docs = futures[future]
            try:
                previews = future.result()
            except (RuntimeError, ValueError, OSError, sqlite3.Error) as error:
                logger.warning("%s: preview extraction failed (%s)", local_name, error)
                previews = {doc["id"]: "" for doc in source_docs}
            for doc in source_docs:
                lead = previews.get(doc["id"], "")
                doc["lead"] = lead
                source = sources.get(doc.get("source_key"))
                if source is not None:
                    preview_cache.put((source.key, doc["path"]), lead)

    def _enrich_pool(
        self,
        source_results: list[SourceResult],
        query: str,
        pool: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fill MIME types and run the content-phrase check in one archive pass.

        Reading each local pool entry once (instead of a phrase pass followed
        by a separate MIME pass) is what keeps a multi-word search from
        re-reading the same articles. Metadata and the phrase flag are written
        onto the source-level docs too, so a re-rank triggered by a phrase hit
        inherits them without touching the archive again. Returns a re-ranked
        pool when any phrase matched, otherwise the pool unchanged.
        """
        phrase_words = _phrase_words(query)
        query_terms = _query_terms(query)
        phrase_active = len(phrase_words) >= 2 and bool(pool)
        _, _, _, docs, source_by_key = _collect_ranked_candidates(source_results)
        max_html_bytes = self.max_html_bytes
        max_phrase_bytes = self.max_article_bytes
        indexes = getattr(self, "indexes", {})

        # Check the phrase in article text for the highest-ranked candidates,
        # whichever retrieval list found them. Previously only full-text hits
        # were checked, so an exact phrase in a semantic/title hit was ignored.
        phrase_ids: set[str] = set()
        if phrase_active:
            for result in pool:
                if len(phrase_ids) >= MAX_PHRASE_CANDIDATES:
                    break
                source = source_by_key.get(result.get("source_key"))
                if source is None or not source.local_name:
                    continue
                if _phrase(query_terms, _content_terms(result["title"])):
                    # The title already carries the phrase; that signal is
                    # already in the ranking, so skip the content read.
                    continue
                index = indexes.get(source.local_name)
                if index is None:
                    continue
                if index.ensure_open() is None:
                    continue
                phrase_ids.add(result["id"])

        grouped: dict[str, list[dict[str, Any]]] = {}
        for result in pool:
            source = source_by_key.get(result.get("source_key"))
            if source is None or not source.local_name:
                continue
            grouped.setdefault(source.local_name, []).append(result)

        found = False
        for local_name, result_docs in grouped.items():
            index = indexes.get(local_name)
            if index is None:
                continue
            archive = index.ensure_open()
            if archive is None:
                continue
            try:
                self._acquire_index(index)
            except RuntimeError:
                continue
            try:
                with index.read_lock:
                    for result in result_docs:
                        is_phrase_candidate = result["id"] in phrase_ids
                        if not is_phrase_candidate and result.get("mimetype"):
                            continue  # nothing left to learn about this doc
                        try:
                            item = archive.get_entry_by_path(result["path"]).get_item()
                            mimetype = (
                                item.mimetype or "application/octet-stream"
                            ).split(";", 1)[0].strip().lower()
                        except (KeyError, RuntimeError, ValueError):
                            continue
                        source_doc = docs.get(result["id"])
                        result["mimetype"] = mimetype
                        if source_doc is not None:
                            source_doc["mimetype"] = mimetype
                        if not is_phrase_candidate or mimetype not in _PHRASE_MIMETYPES:
                            continue
                        # libzim decompresses the whole item when .content is
                        # read; slice after that is too late. Honor the same
                        # serving limit as /article so one huge entry cannot
                        # monopolize memory during a phrase check.
                        if int(getattr(item, "size", 0) or 0) > max_phrase_bytes:
                            continue
                        try:
                            raw = bytes(item.content[:max_html_bytes])
                        except (KeyError, RuntimeError, TypeError, ValueError):
                            continue
                        hit = _phrase_in_word_stream(
                            raw.decode("utf-8", "ignore").casefold(), phrase_words
                        )
                        if source_doc is not None:
                            source_doc["_content_phrase"] = hit
                        if hit:
                            found = True
            finally:
                self._release_index(index)
        return self._rank_results(source_results, query, limit) if found else pool

    def _preview(self, text: str) -> str:
        return truncate_at_word_boundary(text, self.preview_chars)

    @staticmethod
    def _source_items(
        semantic: list[tuple[float, dict[str, Any]]],
        keyword: list[tuple[float, dict[str, Any]]],
        fulltext: list[tuple[int, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        return source_items(semantic, keyword, fulltext)

    def _search_kiwix(self, source: SourceInfo, query: str, limit: int) -> SourceResult:
        return search_kiwix(
            source,
            query,
            limit,
            self.cfg,
            self.candidate_count,
            self.source_timeout,
            self._preview,
            urlopen_fn=urlopen,
        )

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
