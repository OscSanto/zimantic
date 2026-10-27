"""Hybrid search over local ZIM indexes and optional Kiwix sources."""
from __future__ import annotations

import logging
import math
import posixpath
import re
import secrets
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from libzim.reader import Archive
from libzim.search import Query

from .cache import DEFAULT_CACHE_BYTES, DEFAULT_CACHE_SIZE, QueryCache
from .catalog import (
    ZimCatalog,
    catalog_path,
    iter_zim_files,
    metadata_from_archive,
    perma_ref,
)
from .content.reader import (
    _READABLE_MIMETYPES,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_PARSE_TIMEOUT,
    DEFAULT_PREVIEW_CHARS,
    read_entry,
    truncate_at_word_boundary,
)
from .contracts import SourceInfo, SourceResult
from .redirects import RedirectError, resolve_redirect_entry
from .searching.config import _file_signature, _float_config, _int_config
from .searching.index import _LocalIndex, _nprobe
from .searching.query import (
    DEFAULT_DISAMBIGUATION_BOOST,
    DEFAULT_DISAMBIGUATION_PENALTY,
    DEFAULT_USER_PAGE_PENALTY,
    DISAMBIGUATION_TITLE,
    MAX_QUERY_LENGTH,
    USER_PAGE_PATH,
    _content_terms,
    _density,
    _disambiguation_intent,
    _fulltext_query,
    _intent_match,
    _phrase,
    _phrase_words,
    _query_terms,
    _title_prefix_query,
    _title_query,
    _user_page_intent,
)
from .searching.sources import (
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
DEFAULT_MAX_KIWIX_RESPONSE_BYTES = 64 << 20
DEFAULT_KIWIX_CATALOG_LIMIT = 50_000
DEFAULT_MAX_REMOTE_SOURCES = 100
DEFAULT_KIWIX_CATALOG_ATTEMPTS = 3
DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF = 1.0
MAX_KIWIX_CATALOG_RETRY_BACKOFF = 8.0
DEFAULT_KIWIX_RESULT_LIMIT = 1_000
DEFAULT_LONG_QUERY = 10
MAX_PHRASE_CANDIDATES = 32
# How many uniform rowid/entry-id probes /random tries before falling back to a
# guaranteed pick. Probes give every eligible article equal weight and usually
# succeed on the first try; the budget only matters for a moderately sparse
# index, where a larger value keeps the slower fallback off the common path.
RANDOM_ARTICLE_ATTEMPTS = 128
# How long an owner waits for a search slot before reporting busy. Absorbing a
# short burst of retries this way lets an in-flight search finish and the
# query be answered (or served from cache) instead of failing with 503.
SLOT_WAIT_SECONDS = 2.0
# Bound how long an identical-query waiter (or an abandoned finalizer) waits
# for a source future. A hung source must not hang every following request.
SEARCH_CLAIM_WAIT_SECONDS = 60.0
# Leading bytes returned by a head-only article read (shell title/favicon).
HEAD_READ_BYTES = 256 << 10
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


def _catalog_book(href: str) -> str:
    """Extract a ZIM book name from a catalog entry's HTML link.

    Kiwix normally links to ``/content/<book>``, but some servers link to an
    article below it (``/content/<book>/A/Main_Page``). Take the segment after
    ``content`` when present instead of assuming it is the last one.
    """
    parts = [part for part in urlparse(href).path.split("/") if part]
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "content" and index + 1 < len(parts):
            return unquote(parts[index + 1])
    return unquote(parts[-1]) if parts else ""


def _archive_metadata(archive: Archive, zim_path: Path) -> dict[str, Any]:
    """Read archive metadata through the shared catalog extractor.

    The catalog and the archive-scan fallback used to maintain separate field
    lists. Keeping the fallback on ``metadata_from_archive`` means both paths
    expose the same names, normalization, and tag handling.
    """
    try:
        record = metadata_from_archive(archive, zim_path)
    except (OSError, AttributeError, KeyError, RuntimeError, TypeError, ValueError) as error:
        logger.warning("zimantic: metadata unavailable for %s (%s)", zim_path, error)
        return {}
    tags = tuple(tag for tag in record["public_tags"].split(";") if tag)
    return {
        "Title": record["title"],
        "Description": record["description"],
        "LongDescription": record["long_description"],
        "Creator": record["creator"],
        "Publisher": record["publisher"],
        "Language": record["language"],
        "Date": record["date"],
        "Tags": tags,
        "Name": record["name"],
        "Flavour": record["flavour"],
        "License": record["license"],
        "Scraper": record["scraper"],
        "Source": record["source"],
        "Attribution": record["attribution"],
        "Relation": record["relation"],
        "Counter": record["counter"],
        "Homepage": record["homepage"],
        "HasFulltextIndex": record["has_fulltext_index"],
        "IconPath": record["icon_path"],
        "UUID": record["uuid"],
    }


def _archive_has_illustration(archive: Archive) -> bool:
    try:
        value = getattr(archive, "has_illustration", False)
        return bool(value() if callable(value) else value)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False


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


class ArticleNotFoundError(Exception):
    """The requested local ZIM or article path does not exist."""


class ArticleSourceNotFoundError(ArticleNotFoundError):
    """The named local source is not installed."""


class ArticleUnavailableError(Exception):
    """A local ZIM exists but cannot currently serve the requested article."""


class ArticleTooLargeError(ArticleUnavailableError):
    """The requested entry exceeds the configured serving limit."""


def _deduplicate_ranked(ranked):
    seen: set[str] = set()
    unique = []
    for item in ranked:
        identity = item[2]["id"]
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(item)
    return unique


def _collect_ranked_candidates(source_results: list[SourceResult]):
    semantic: list[tuple[float, str, dict[str, Any]]] = []
    keyword: list[tuple[float, str, dict[str, Any]]] = []
    fulltext: list[tuple[int, str, dict[str, Any]]] = []
    docs: dict[str, dict[str, Any]] = {}
    source_by_key = {
        result.source.key: result.source for result in source_results
    }

    for result in source_results:
        for doc in result.items:
            docs[doc["id"]] = doc
        for score, doc in result.semantic:
            semantic.append((score, result.source.key, doc))
        for score, doc in result.keyword:
            keyword.append((score, result.source.key, doc))
        for rank, doc in result.fulltext:
            fulltext.append((rank, result.source.key, doc))

    semantic.sort(
        key=lambda item: (
            -item[0],
            source_by_key[item[1]].name.casefold(),
            item[2]["path"],
        )
    )
    keyword.sort(
        key=lambda item: (
            item[0],
            source_by_key[item[1]].name.casefold(),
            item[2]["path"],
        )
    )
    fulltext.sort(
        key=lambda item: (
            item[0],
            source_by_key[item[1]].name.casefold(),
            item[2]["path"],
        )
    )
    return (
        _deduplicate_ranked(semantic),
        _deduplicate_ranked(keyword),
        _deduplicate_ranked(fulltext),
        docs,
        source_by_key,
    )


def _rrf_scores(
    ranked_lists,
    query: str,
    candidate_limit: int,
) -> tuple[dict[str, float], dict[str, dict[str, dict[str, Any]]], float]:
    scores: dict[str, float] = {}
    fulltext_weight = (
        2.0
        if len(re.findall(r"\w+", query, re.UNICODE)) >= DEFAULT_LONG_QUERY
        else 1.0
    )
    details: dict[str, dict[str, dict[str, Any]]] = {}
    for ranked, method, weight in (
        (ranked_lists[0], "semantic", 1.0),
        (ranked_lists[1], "title", 1.0),
        (ranked_lists[2], "fulltext", fulltext_weight),
    ):
        for rank, (raw_score, _, doc) in enumerate(ranked[:candidate_limit]):
            # Reciprocal Rank Fusion uses one-based positions.
            contribution = weight / (60 + rank + 1)
            scores[doc["id"]] = scores.get(doc["id"], 0.0) + contribution
            details.setdefault(doc["id"], {})[method] = {
                "rank": rank + 1,
                "score": round(raw_score, 6),
                "weight": weight,
                "contribution": round(contribution, 6),
            }
    return scores, details, fulltext_weight


@dataclass(slots=True)
class _RankedAssessment:
    """Scored candidate plus everything needed to render its response dict.

    Ranking computes one of these per candidate and sorts on the numeric
    fields; the heavy ``output`` dict (ranking metadata and explanation) is
    only rendered for the docs that survive the top-k cut.
    """

    identity: str
    doc: dict[str, Any]
    query_words: list[str]
    title_tokens: list[str]
    title_matches: int
    title_coverage: float
    title_density: float
    title_quality: float
    title_phrase_hit: bool
    content_phrase_hit: bool
    intent: float
    total: float
    lexical: float
    source_rank: int
    source_casefold: str
    path: str
    rrf_score: float
    rrf_details: dict[str, dict[str, dict[str, Any]]]
    semantic_hit: bool
    fulltext_weight: float
    disambig: dict[str, Any]
    user_page: dict[str, Any]


def _assess_ranked_document(
    identity: str,
    doc: dict[str, Any],
    query_words: list[str],
    query: str,
    source_by_key: dict[str, SourceInfo],
    scores: dict[str, float],
    rrf_details: dict[str, dict[str, dict[str, Any]]],
    fulltext_weight: float,
) -> _RankedAssessment:
    # A document is re-assessed on every intermediate ranking pass, but its
    # title never changes; tokenizing once per document instead of once per
    # pass removes most of the ranking cost on large pools.
    title_tokens = doc.get("_title_tokens")
    if title_tokens is None:
        title_tokens = _content_terms(doc["title"])
        doc["_title_tokens"] = title_tokens
    title_token_set = set(title_tokens)
    title_matches = sum(term in title_token_set for term in query_words)
    title_coverage = title_matches / len(query_words) if query_words else 0.0
    title_density = _density(query_words, title_tokens)
    # Coverage is primary; density rewards concise titles among equally
    # complete matches without letting a short partial match win.
    title_quality = title_coverage * (1.0 + title_density) / 2.0
    title_phrase_hit = _phrase(query_words, title_tokens)
    content_phrase_hit = bool(doc.get("_content_phrase"))
    phrase_hit = max(title_phrase_hit, content_phrase_hit)
    source = source_by_key.get(doc["source_key"])
    intent = _intent_match(source, query) if source is not None else 0.0
    lexical = (5.0 * title_quality + 2.0 * phrase_hit + intent) / 8.0
    source_rank = int(doc.get("source_rank", 100000))
    rrf_score = scores.get(identity, 0.0)
    semantic_hit = "semantic" in rrf_details.get(identity, {})
    base_score = rrf_score + 0.02 * lexical
    total = base_score
    disambig = {
        "action": "none",
        "factor": 1.0,
        "adjustment": 0.0,
    }
    if DISAMBIGUATION_TITLE.search(doc["title"]):
        if _disambiguation_intent(query, query_words, doc["title"]):
            total += DEFAULT_DISAMBIGUATION_BOOST
            disambig = {
                "action": "boost",
                "factor": 1.0,
                "adjustment": round(DEFAULT_DISAMBIGUATION_BOOST, 6),
            }
        else:
            total *= DEFAULT_DISAMBIGUATION_PENALTY
            disambig = {
                "action": "demote",
                "factor": DEFAULT_DISAMBIGUATION_PENALTY,
                "adjustment": round(total - base_score, 6),
            }
    user_page = bool(USER_PAGE_PATH.search(str(doc.get("path", ""))))
    user_page_action = "none"
    user_page_factor = 1.0
    user_page_adjustment = 0.0
    if user_page and not _user_page_intent(query_words):
        before_user_page = total
        total *= DEFAULT_USER_PAGE_PENALTY
        user_page_action = "demote"
        user_page_factor = DEFAULT_USER_PAGE_PENALTY
        user_page_adjustment = total - before_user_page
    return _RankedAssessment(
        identity=identity,
        doc=doc,
        query_words=query_words,
        title_tokens=title_tokens,
        title_matches=title_matches,
        title_coverage=title_coverage,
        title_density=title_density,
        title_quality=title_quality,
        title_phrase_hit=title_phrase_hit,
        content_phrase_hit=content_phrase_hit,
        intent=intent,
        total=total,
        lexical=lexical,
        source_rank=source_rank,
        source_casefold=doc["source"].casefold(),
        path=doc["path"],
        rrf_score=rrf_score,
        rrf_details=rrf_details,
        semantic_hit=semantic_hit,
        fulltext_weight=fulltext_weight,
        disambig=disambig,
        user_page={
            "action": user_page_action,
            "factor": user_page_factor,
            "adjustment": round(user_page_adjustment, 6),
        },
    )


def _render_ranked_document(assessment: _RankedAssessment) -> dict[str, Any]:
    """Build the public/ranking-friendly dict for a surviving candidate.

    Kept separate from ``_assess_ranked_document`` so ranking only pays for the
    nested ``ranking``/``explain`` structure on the docs that are actually kept.
    """
    a = assessment
    title_phrase_hit = a.title_phrase_hit
    phrase_hit = max(title_phrase_hit, a.content_phrase_hit)
    user_page_action = a.user_page["action"]
    explanation = (
        f"title {round(a.title_coverage * 100)}% "
        f"({a.title_matches}/{len(a.query_words) if a.query_words else 0}, "
        f"density {round(a.title_density * 100)}%), "
        f"phrase {'yes' if phrase_hit else 'no'}, "
        f"intent {'yes' if a.intent else 'no'}"
        f"{', disambiguation ' + a.disambig['action'] if a.disambig['action'] != 'none' else ''}"
        f"{', user page demote' if user_page_action == 'demote' else ''}"
    )
    output = dict(a.doc)
    output.pop("_content_phrase", None)
    output.pop("_title_tokens", None)
    output.update({
        "score": a.total,
        "explain": explanation,
        "rank": a.source_rank,
        "ranking": {
            "final_score": round(a.total, 6),
            "rrf_score": round(a.rrf_score, 6),
            "rrf": {
                method: a.rrf_details.get(a.identity, {}).get(
                    method,
                    {
                        "rank": None,
                        "score": None,
                        "weight": weight,
                        "contribution": 0.0,
                    },
                )
                for method, weight in (
                    ("semantic", 1.0),
                    ("title", 1.0),
                    ("fulltext", a.fulltext_weight),
                )
            },
            "title": {
                "query_terms": a.query_words,
                "matches": a.title_matches,
                "term_count": len(a.query_words),
                "coverage": round(a.title_coverage, 6),
                "density": round(a.title_density, 6),
                "quality": round(a.title_quality, 6),
            },
            "phrase": bool(phrase_hit),
            "content_phrase": a.content_phrase_hit,
            "semantic_hit": a.semantic_hit,
            "intent": bool(a.intent),
            "lexical_score": round(a.lexical, 6),
            "disambiguation": a.disambig,
            "user_page": a.user_page,
            "source_rank": a.source_rank,
        },
    })
    return output


class Search:
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
        self.archive_factory = archive_factory
        # Fast mode skips FAISS and the model.
        self.semantic = bool(semantic)
        self._defer_faiss = self.semantic
        self.indexes: dict[str, _LocalIndex] = {}
        self.sources: dict[str, SourceInfo] = {}
        self._state_lock = threading.RLock()
        self._reload_lock = threading.Lock()
        self.source_timeout = _int_config(cfg, "source_timeout", DEFAULT_SOURCE_TIMEOUT)
        self.candidate_count = _int_config(cfg, "candidate_count", DEFAULT_CANDIDATES)
        self.source_workers = _int_config(cfg, "search_workers", 4)
        self.max_remote_sources = _int_config(
            cfg, "max_remote_sources", DEFAULT_MAX_REMOTE_SOURCES, minimum=0
        )
        self.preview_chars = _int_config(cfg, "max_preview_chars", DEFAULT_PREVIEW_CHARS)
        self.max_results = _int_config(cfg, "max_results", DEFAULT_MAX_RESULTS)
        # A page can never be larger than the ranked pool it is sliced from.
        # Clamping once here keeps /config, the page headers, and the frontend
        # pager consistent even if a hand-edited config sets page_size higher.
        self.page_size = min(
            _int_config(cfg, "page_size", DEFAULT_PAGE_SIZE),
            self.max_results,
        )
        self.max_concurrent_searches = _int_config(
            cfg, "max_concurrent_searches", DEFAULT_MAX_CONCURRENT_SEARCHES
        )
        self.max_stream_ranks = _int_config(
            cfg, "max_stream_ranks", DEFAULT_MAX_STREAM_RANKS
        )
        self._search_slots = threading.BoundedSemaphore(self.max_concurrent_searches)
        self._slot_condition = threading.Condition()
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
        # Single metadata catalog. It is a disposable cache: the ZIM directory
        # remains the source of truth, and a missing/corrupt catalog falls back
        # to the archive-scanning behavior below.
        self.catalog: ZimCatalog | None = None
        index_dir = cfg.get("index_dir")
        if index_dir:
            try:
                catalog = ZimCatalog(
                    catalog_path(index_dir),
                    zim_dir=cfg.get("zim_dir"),
                    archive_factory=self.archive_factory,
                )
            except Exception as error:
                logger.warning(
                    "zimantic: catalog unavailable (%s); falling back to archive scans",
                    error,
                )
            else:
                try:
                    catalog.scan()
                except OSError as error:
                    # An unmounted ZIM directory is not a reason to throw away
                    # the cached metadata; reload() will scan again.
                    logger.warning(
                        "zimantic: catalog scan failed (%s); keeping the previous catalog",
                        error,
                    )
                except Exception as error:
                    logger.warning(
                        "zimantic: catalog unavailable (%s); falling back to archive scans",
                        error,
                    )
                    try:
                        catalog.close()
                    except Exception:
                        pass
                    catalog = None
                self.catalog = catalog
        self._load_indexes()
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
        return getattr(self, "catalog", None)

    @staticmethod
    def _index_uri(db_path: Path) -> str:
        return db_path.resolve().as_uri() + "?mode=ro"

    def _match_zim_path(
        self, stem: str, source_size: int | None
    ) -> tuple[Path | None, bool]:
        """Return ``(archive, exact)`` for ``stem`` below ``zim_dir``.

        An archive whose size equals the recorded ``source_size`` is an exact
        identity match and wins outright. When no candidate has that size the
        largest same-stem file is returned with ``exact=False``, so callers can
        decide whether a changed archive should replace a stale index. With no
        recorded size there is nothing to compare, so the largest candidate is
        reported as exact.
        """
        zim_dir = self.cfg.get("zim_dir")
        if not zim_dir:
            return None, False
        try:
            candidates = sorted(
                path for path in iter_zim_files(zim_dir) if path.stem == stem
            )
        except OSError:
            return None, False

        largest: Path | None = None
        largest_size = -1
        for path in candidates:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if source_size is not None and size == source_size:
                return path, True
            if size > largest_size:
                largest_size = size
                largest = path
        return largest, largest is not None and source_size is None

    def _find_zim_path(self, stem: str, source_size: int | None) -> Path | None:
        """Best archive for ``stem``: an exact-size match, else the largest."""
        return self._match_zim_path(stem, source_size)[0]

    def _scan_zim_paths(self) -> dict[str, Path]:
        """Find local archives without requiring a SQLite index.

        The shared recursive enumerator keeps this startup probe consistent with
        the catalog and ``build``. File sizes are read only when a stem repeats,
        so a normal library never pays for a ``stat``. When a stem repeats with
        different sizes (for example a truncated download beside the complete
        file), the larger file is kept rather than discarding the stem.
        """
        zim_dir = self.cfg.get("zim_dir")
        if not zim_dir:
            return {}

        found: dict[str, Path] = {}
        # Sizes of stems seen more than once; absent for unique stems so they are
        # never read with ``stat``.
        sizes: dict[str, int] = {}
        contested: set[str] = set()

        def record(path: Path) -> None:
            stem = path.stem
            existing = found.get(stem)
            if existing is None:
                found[stem] = path
                return
            # A repeated stem needs a size comparison; this is lazy so a unique
            # library never pays for a stat.
            try:
                kept_size = sizes.get(stem)
                if kept_size is None:
                    kept_size = existing.stat().st_size
                incoming_size = path.stat().st_size
            except OSError:
                return
            if incoming_size > kept_size:
                found[stem] = path
                sizes[stem] = incoming_size
            else:
                sizes[stem] = kept_size
            if incoming_size != kept_size:
                contested.add(stem)

        for path in iter_zim_files(zim_dir):
            try:
                if path.is_file():
                    record(path)
            except OSError:
                continue
        for stem in sorted(contested):
            logger.warning(
                "zimantic: %r has ZIM files of different sizes; using the largest %s",
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
            pass  # unreadable now; fall through and check the alternatives
        archive, exact = self._match_zim_path(stem, source_size)
        if archive is None or exact:
            return None
        return archive

    def _discover_local_indexes(self) -> dict[str, tuple[str, Path]]:
        """Return finished SQLite indexes plus filesystem-only ZIM fallbacks.

        A SQLite stem is authoritative only once its build is finished and its
        recorded source size still matches a same-stem archive. An in-progress
        build, one whose ZIM is gone, or one whose ZIM was replaced (size
        changed) is ignored so the archive's native full-text index keeps the
        source searchable instead of a stale or unopenable index.
        """
        db_paths = sorted(Path(self.cfg["index_dir"]).glob("*.sqlite"))
        finished: dict[str, Path] = {}
        recorded_sizes: dict[str, int | None] = {}
        for path in db_paths:
            done, source_size = self._read_index_state(path)
            if done:
                finished[path.stem] = path
                recorded_sizes[path.stem] = source_size

        zim_paths = self._scan_zim_paths()
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
        # one deterministic copy, preferring a readable one, then an indexed
        # one, then the larger file, then the shorter path.
        if self._catalog:
            chosen: dict[str, tuple[tuple, str]] = {}
            for stem, (kind, _path) in found.items():
                record = self._catalog.get_stem(stem)
                uuid_value = record.uuid if record is not None else ""
                if not uuid_value:
                    continue
                rank = (
                    bool(record.available),
                    kind == "sqlite",
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
                record = self._catalog.get_stem(stem)
                if record is not None and record.uuid and stem not in keep:
                    del found[stem]
        return found

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
            try:
                source_size = int(size_row[0]) if size_row else None
            except (TypeError, ValueError):
                source_size = None

            faiss_index = None
            faiss_path = db_path.with_suffix(".faiss")
            defer_faiss = getattr(self, "_defer_faiss", False)
            if (
                self.semantic
                and not defer_faiss
                and done_value == "1"
                and faiss_path.exists()
            ):
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
                        "%s: vectors unavailable (%s); using title and full-text search",
                        db_path.stem,
                        error,
                    )
                    faiss_index = None

            record = (
                self._catalog.get_stem_size(db_path.stem, source_size)
                if self._catalog
                else None
            )
            if record is not None:
                # Keep a valid SQLite index usable for title search even if the
                # archive is currently unreadable; opening is lazy and reports
                # the failure only for operations that need the archive.
                zim_path = Path(record.path)
                if source_size is None:
                    source_size = record.size
            else:
                zim_path = self._find_zim_path(db_path.stem, source_size)
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
        record = self._catalog.get_stem(zim_path.stem) if self._catalog else None
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

    def _load_indexes(self) -> None:
        found = self._discover_local_indexes()
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
        def _load() -> None:
            try:
                embedder = factory()
            except Exception as error:  # missing/corrupt model must not kill serve
                logger.warning(
                    "zimantic: embedding model unavailable (%s); "
                    "using title and full-text search",
                    error,
                )
                return
            with self._state_lock:
                self.embedder = embedder
            logger.info("zimantic: embedding model ready; semantic search enabled")

        threading.Thread(target=_load, name="zimantic-embedder", daemon=True).start()

    def start_faiss(self) -> None:
        """Load FAISS and vector indexes off the server startup path."""
        if not self.semantic or not getattr(self, "_defer_faiss", False):
            return

        def _load() -> None:
            try:
                import faiss  # noqa: F401 - load the native module on this worker
            except Exception as error:
                logger.warning(
                    "zimantic: FAISS unavailable (%s); using title and full-text search",
                    error,
                )
                return

            self._defer_faiss = False
            try:
                # __init__ just scanned the catalog and built the source set;
                # this reload only reopens index handles to attach their
                # vectors. Rescanning here would duplicate both at startup.
                self.reload(scan_catalog=False, refresh=False)
            except Exception as error:
                logger.warning(
                    "zimantic: FAISS indexes unavailable (%s); "
                    "using title and full-text search",
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
            if scan_catalog and self._catalog is not None:
                try:
                    self._catalog.scan()
                except Exception as error:
                    logger.warning("zimantic: catalog rescan failed (%s)", error)
            found = self._discover_local_indexes()
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
                defer_faiss = getattr(self, "_defer_faiss", False)
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
                    self._find_zim_path(name, current.source_size)
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
                    self._cache_generation = getattr(self, "_cache_generation", 0) + 1
                self.cache.clear()
                preview_cache = getattr(self, "preview_cache", None)
                if preview_cache is not None:
                    preview_cache.clear()
            if refresh:
                self.refresh_sources()
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
        return SourceInfo(
            key=key,
            name=local_name,
            book=local_name,
            mode="local",
            local_name=local_name,
            available=available,
            searchable=searchable,
            error=error,
            # An unreadable archive has no openable homepage.
            homepage=meta["homepage"] if available else None,
            icon_path=meta["icon_path"],
            icon_url=icon_url,
            intent_phrases=_intent_phrases(local_name),
            title=meta["title"],
            description=meta["description"],
            long_description=meta["long_description"],
            creator=meta["creator"],
            publisher=meta["publisher"],
            language=meta["language"],
            date=meta["date"],
            tags=meta["tags"],
            size=meta["size"],
            article_count=meta["article_count"],
            uuid=meta["uuid"],
            perma_ref=perma_ref(local_name),
            flavour=meta["flavour"],
            category=meta["category"],
            media_count=meta["media_count"],
            license=meta["license"],
            scraper=meta["scraper"],
            source=meta["source"],
            attribution=meta["attribution"],
            relation=meta["relation"],
            counter=meta["counter"],
            has_fulltext_index=meta["has_fulltext_index"],
        )

    def _rebuild_sources(self, entries: list[dict[str, str]]) -> list[dict[str, Any]]:
        web_base_path = self.web_base_path
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
                    logger.warning(
                        "zimantic: %s shares a source key with another local "
                        "source; exposing it as %r",
                        local_name,
                        key,
                    )
                record = (
                    self._catalog.get_stem_size(local_name, index.source_size)
                    if self._catalog
                    else None
                )
                if record is not None:
                    local[key] = self._source_from_catalog(
                        record,
                        key=key,
                        local_name=local_name,
                    )
                    continue

                # Fallback for archives without a catalog row (for example, a
                # catalog created before this version, or a build that could not
                # write it). This opens the archive, as prior versions always did.
                archive = index.ensure_open()
                if archive is None:
                    # A title-only index can still serve results when its ZIM
                    # exists but cannot be opened (the SQLite docs table is
                    # self-contained). Keep the source available, as prior
                    # versions did. A ZIM that is actually absent is filtered
                    # out earlier by _discover_local_indexes.
                    local[key] = SourceInfo(
                        key=key,
                        name=local_name,
                        book=local_name,
                        mode="local",
                        local_name=local_name,
                        intent_phrases=_intent_phrases(local_name),
                        size=index.source_size,
                    )
                    continue
                homepage = None
                icon_path = None
                metadata: dict[str, Any] = {}
                try:
                    with index.read_lock:
                        homepage = self._resolve_article_entry(archive).path
                        icon_path = _find_archive_icon_path(archive, homepage)
                        metadata = (
                            _archive_metadata(archive, Path(index.zim_path))
                            if index.zim_path
                            else {}
                        )
                except (
                    ArticleNotFoundError,
                    ArticleUnavailableError,
                    RuntimeError,
                    ValueError,
                ):
                    homepage = None
                has_icon = bool(icon_path) or _archive_has_illustration(archive)
                icon_url = _source_icon_url(local_name, has_icon, web_base_path)
                size = index.source_size
                if size is None and index.zim_path:
                    try:
                        size = Path(index.zim_path).stat().st_size
                    except OSError:
                        size = None
                article_count = None
                try:
                    article_count = getattr(archive, "article_count", None)
                    if callable(article_count):
                        article_count = article_count()
                    article_count = int(article_count or 0)
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    article_count = None
                local[key] = SourceInfo(
                    key=key,
                    name=local_name,
                    book=local_name,
                    mode="local",
                    local_name=local_name,
                    homepage=homepage,
                    icon_path=icon_path,
                    icon_url=icon_url,
                    intent_phrases=_intent_phrases(local_name),
                    title=metadata.get("Title"),
                    description=metadata.get("Description"),
                    long_description=metadata.get("LongDescription"),
                    creator=metadata.get("Creator"),
                    publisher=metadata.get("Publisher"),
                    language=metadata.get("Language"),
                    date=metadata.get("Date"),
                    tags=metadata.get("Tags", ()),
                    size=size,
                    article_count=article_count,
                    uuid=metadata.get("UUID") or None,
                    perma_ref=perma_ref(local_name),
                    flavour=metadata.get("Flavour") or None,
                    license=metadata.get("License") or None,
                    scraper=metadata.get("Scraper") or None,
                    source=metadata.get("Source") or None,
                    attribution=metadata.get("Attribution") or None,
                    relation=metadata.get("Relation") or None,
                    counter=metadata.get("Counter") or None,
                    has_fulltext_index=metadata.get("HasFulltextIndex"),
                )

            merged = dict(local)
            for entry in entries:
                key = _zim_key(entry["book"])
                if merged.get(key) is not None:
                    # A local source is authoritative for its own identity.
                    # Merging the remote catalog's display name here used to
                    # overwrite the local name (and made "is this indexed?"
                    # checks look at the wrong field); the local catalog and
                    # archive already provide the title.
                    continue
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
                self._cache_generation = getattr(self, "_cache_generation", 0) + 1
            sources = self.source_dicts()
        if changed:
            # The set of searchable sources changed, so prior answers can be stale.
            # Keeping the cache while the source set is unchanged lets repeated
            # queries (for example, page reloads) be answered without recomputing.
            self._invalidate_library_cache()
            self._remote_limit_warned = False
            self.cache.clear()
            preview_cache = getattr(self, "preview_cache", None)
            if preview_cache is not None:
                preview_cache.clear()
        return sources

    def _catalog_entries(self, server: str) -> list[dict[str, str]] | None:
        path = str(
            self.cfg.get(
                "kiwix_catalog_path",
                "/kiwix/catalog/v2/entries?count=50000",
            )
        )
        url = server + (path if path.startswith("/") else "/" + path)
        catalog_limit = _int_config(
            self.cfg,
            "kiwix_catalog_limit",
            DEFAULT_KIWIX_CATALOG_LIMIT,
        )
        url = _bounded_catalog_url(url, catalog_limit)
        response_limit = _int_config(
            self.cfg,
            "max_kiwix_response_bytes",
            DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
        )
        attempts = DEFAULT_KIWIX_CATALOG_ATTEMPTS
        for attempt in range(attempts):
            try:
                request = Request(
                    url,
                    headers={"Accept": "application/atom+xml, application/xml"},
                )
                with urlopen(request, timeout=self.source_timeout) as response:
                    root = _parse_remote_xml(
                        _read_limited_response(response, response_limit)
                    )
                break
            except (HTTPError, URLError, TimeoutError, ValueError, ET.ParseError, OSError) as error:
                retryable = True
                if isinstance(error, HTTPError) and (
                    400 <= error.code < 500 and error.code != 429
                ):
                    retryable = False
                elif isinstance(error, (ValueError, ET.ParseError)):
                    # Malformed XML, a DTD, or an over-limit response will not
                    # become valid on the next attempt.
                    retryable = False
                if not retryable:
                    logger.warning(
                        "zimantic: catalog request failed: %s",
                        error,
                    )
                    return None
                if attempt + 1 == attempts:
                    logger.warning(
                        "zimantic: catalog request failed after %d attempt(s): %s",
                        attempts,
                        error,
                    )
                    return None
                delay = min(
                    DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF * (2**attempt),
                    MAX_KIWIX_CATALOG_RETRY_BACKOFF,
                )
                logger.info(
                    "zimantic: catalog request failed (%s); retrying in %.1f seconds",
                    error,
                    delay,
                )
                time.sleep(delay)

        newest: dict[str, dict[str, str]] = {}
        for entry in root.iter():
            if _local_name(entry) != "entry":
                continue
            title = _child_text(entry, "title")
            updated = _child_text(entry, "updated")
            href = ""
            for child in entry:
                if _local_name(child) == "link" and (
                    (child.attrib.get("type") or "").startswith("text/html")
                    or not href
                ):
                    href = child.attrib.get("href") or ""
            book = _catalog_book(href) or unquote(_child_text(entry, "name"))
            if not book:
                continue
            item = {"name": title or book, "book": book, "updated": updated}
            key = _zim_key(book)
            previous = newest.get(key)
            if not previous or (book, updated) > (previous["book"], previous["updated"]):
                newest[key] = item

        items = list(newest.values())
        # The server may ignore the requested count or return more entries than
        # asked for. Enforce the configured ceiling locally as well, after
        # de-duplicating by book key, so a remote catalog cannot make the source
        # set unbounded.
        items.sort(key=lambda item: (item["name"].casefold(), item["book"]))
        items = items[:catalog_limit]
        title_counts: dict[str, int] = {}
        for item in items:
            title_counts[item["name"]] = title_counts.get(item["name"], 0) + 1
        for item in items:
            if title_counts[item["name"]] > 1:
                item["name"] = f"{item['name']} ({_zim_key(item['book'])})"
        return items

    def source_dicts(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return [
                source.to_dict(include_metadata=True)
                for source in self.sources.values()
            ]

    def _inspect_library_archive(self, zim_name: str, zim_path: Path) -> SourceInfo:
        """Build library metadata for a local archive not used by search."""
        record = self._catalog.get_stem(zim_path.stem) if self._catalog else None
        if record is not None:
            with self._state_lock:
                searchable = zim_name in self.indexes
            return self._source_from_catalog(
                record,
                key=_zim_key(zim_name),
                local_name=zim_name,
                searchable=searchable,
            )
        try:
            archive = Archive(zim_path)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            logger.warning("zimantic: library metadata unavailable for %s (%s)", zim_path, error)
            return SourceInfo(
                key=_zim_key(zim_name),
                name=zim_name,
                book=zim_name,
                mode="local",
                local_name=zim_name,
                available=False,
                searchable=False,
                error="ZIM archive is unavailable",
                size=zim_path.stat().st_size if zim_path.exists() else None,
            )

        homepage = None
        try:
            homepage = self._resolve_article_entry(archive).path
        except (ArticleNotFoundError, ArticleUnavailableError, RuntimeError, ValueError):
            pass
        icon_path = _find_archive_icon_path(archive, homepage)
        metadata = _archive_metadata(archive, zim_path)
        web_base_path = self.web_base_path
        has_icon = bool(icon_path) or _archive_has_illustration(archive)
        icon_url = _source_icon_url(zim_name, has_icon, web_base_path)
        try:
            size = zim_path.stat().st_size
        except OSError:
            size = None
        try:
            article_count = getattr(archive, "article_count", 0)
            if callable(article_count):
                article_count = article_count()
            article_count = int(article_count or 0)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            article_count = None
        return SourceInfo(
            key=_zim_key(zim_name),
            name=zim_name,
            book=zim_name,
            mode="local",
            local_name=zim_name,
            searchable=False,
            homepage=homepage,
            icon_path=icon_path,
            icon_url=icon_url,
            title=metadata.get("Title"),
            description=metadata.get("Description"),
            long_description=metadata.get("LongDescription"),
            creator=metadata.get("Creator"),
            publisher=metadata.get("Publisher"),
            language=metadata.get("Language"),
            date=metadata.get("Date"),
            tags=metadata.get("Tags", ()),
            size=size,
            article_count=article_count,
            uuid=metadata.get("UUID") or None,
            perma_ref=perma_ref(zim_name),
            flavour=metadata.get("Flavour") or None,
            license=metadata.get("License") or None,
            scraper=metadata.get("Scraper") or None,
            source=metadata.get("Source") or None,
            attribution=metadata.get("Attribution") or None,
            relation=metadata.get("Relation") or None,
            counter=metadata.get("Counter") or None,
            has_fulltext_index=metadata.get("HasFulltextIndex"),
        )

    def _library_cache_lock(self) -> threading.Lock:
        """Return the lock guarding the library scan/version caches."""
        with self._state_lock:
            lock = getattr(self, "_library_scan_lock", None)
            if lock is None:
                lock = threading.Lock()
                self._library_scan_lock = lock
            return lock

    def _invalidate_library_cache(self) -> None:
        """Drop cached library scans, including one currently being computed."""
        lock = self._library_cache_lock()
        with lock:
            self._library_scan_at = 0.0
            self._library_version_at = 0.0
            # A scan that started before this point must not publish its stale
            # result with a fresh timestamp.
            self._library_scan_token = getattr(self, "_library_scan_token", 0) + 1

    def _library_zim_paths(self) -> dict[str, Path]:
        """Scan ``zim_dir`` at most once per short interval for the library view."""
        lock = self._library_cache_lock()
        now = time.monotonic()
        with lock:
            cached = getattr(self, "_library_scan_cache", None)
            scanned_at = getattr(self, "_library_scan_at", 0.0)
            token = getattr(self, "_library_scan_token", 0)
            if cached is not None and now - scanned_at < LIBRARY_SCAN_TTL_SECONDS:
                return dict(cached)
        scanned = self._scan_zim_paths()
        with lock:
            if getattr(self, "_library_scan_token", 0) == token:
                self._library_scan_cache = dict(scanned)
                self._library_scan_at = time.monotonic()
        return scanned

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
            indexed_names = set(self.indexes)
        # One card per UUID: prefer a readable copy, then a searchable one, then
        # the larger file, then the shorter/lexicographically earlier name.
        chosen: dict[str, tuple[tuple, SourceInfo]] = {}
        for zim_name, zim_path in sorted(self._library_zim_paths().items()):
            source = indexed.get(zim_name)
            if source is None:
                source = self._inspect_library_archive(zim_name, zim_path)
            identity = source.uuid or f"stem:{source.name}"
            rank = (
                source.available,
                bool(source.local_name and source.local_name in indexed_names),
                source.size or 0,
                -len(source.name or ""),
                source.name or "",
            )
            current = chosen.get(identity)
            if current is None or rank > current[0]:
                chosen[identity] = (rank, source)
        return [
            source.to_dict(include_metadata=True)
            for _rank, source in chosen.values()
        ]

    def library_version(
        self,
    ) -> tuple[tuple[int, ...], tuple[int, ...], tuple[tuple[str, int, int], ...]]:
        """Return a cheap token that changes when the library contents change."""
        lock = self._library_cache_lock()
        now = time.monotonic()
        with lock:
            cached = getattr(self, "_library_version_cache", None)
            cached_at = getattr(self, "_library_version_at", 0.0)
            token = getattr(self, "_library_scan_token", 0)
            if cached is not None and now - cached_at < LIBRARY_SCAN_TTL_SECONDS:
                return cached
        signatures = []
        for zim_name, zim_path in sorted(self._library_zim_paths().items()):
            try:
                stat = zim_path.stat()
            except OSError:
                continue
            signatures.append((zim_name, stat.st_size, stat.st_mtime_ns))
        catalog = self._catalog
        # The catalog is read live for archives without a library source record,
        # so metadata-only writes must invalidate this payload too.
        catalog_version = catalog.version() if catalog is not None else ()
        version = (self.source_version(), catalog_version, tuple(signatures))
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
            return (getattr(self, "_cache_generation", 0),)

    def close(self) -> None:
        """Release the search thread pool and every open index."""
        executor = getattr(self, "_executor", None)
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        with self._state_lock:
            indexes = list(self.indexes.values())
            self.indexes.clear()
        for index in indexes:
            self._close_index(index)
        catalog = getattr(self, "catalog", None)
        if catalog is not None:
            catalog.close()
            self.catalog = None

    def local_names(self) -> list[str]:
        with self._state_lock:
            return list(self.indexes)

    @staticmethod
    def _article_path(path: str) -> str:
        # The ASGI server already percent-decodes the request path before it
        # gets here. Unquoting again corrupts entries whose names contain
        # literal escapes, such as the LibreTexts cover thumbnails stored as
        # ``.../mindtouch.page%23thumbnail?revision=2``.
        path = str(path)
        if not path or path.startswith("/") or "\\" in path:
            raise ArticleNotFoundError("invalid article path")
        if any(part == ".." for part in path.split("/")):
            raise ArticleNotFoundError("invalid article path")
        trailing_slash = path.endswith("/")
        candidate = path[:-1] if trailing_slash else path
        normalized = posixpath.normpath(candidate)
        if normalized in {"", "."} or normalized != candidate:
            raise ArticleNotFoundError("invalid article path")
        return normalized + "/" if trailing_slash else normalized

    @staticmethod
    def _resolve_article_entry(archive: Archive, path: str | None = None):
        try:
            entry = archive.main_entry if path is None else archive.get_entry_by_path(path)
        except (KeyError, RuntimeError, ValueError) as error:
            raise ArticleNotFoundError("article not found") from error
        try:
            return resolve_redirect_entry(entry)
        except RedirectError as error:
            raise ArticleUnavailableError(str(error)) from error

    def _read_archive_article(
        self,
        archive: Archive,
        zim_name: str,
        path: str | None,
        head_only: bool,
    ) -> dict[str, Any]:
        requested_path = None if path is None else self._article_path(path)
        try:
            entry = self._resolve_article_entry(archive, requested_path)
            item = entry.get_item()
            mimetype = (
                item.mimetype or "application/octet-stream"
            ).split(";", 1)[0].strip().lower()
            max_bytes = _int_config(
                self.cfg,
                "max_article_bytes",
                33_554_432,
                minimum=1,
            )
            size = int(getattr(item, "size", 0) or 0)
            if size > max_bytes:
                raise ArticleTooLargeError(
                    f"article exceeds the {max_bytes} byte serving limit"
                )
            content = item.content[:HEAD_READ_BYTES] if head_only else item.content
        except (ArticleNotFoundError, ArticleUnavailableError):
            raise
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as error:
            raise ArticleUnavailableError("article content is unavailable") from error
        if not head_only and len(content) > max_bytes:
            raise ArticleTooLargeError(
                f"article exceeds the {max_bytes} byte serving limit"
            )
        return {
            "zim": zim_name,
            "path": entry.path,
            "title": entry.title,
            "mimetype": mimetype,
            "content": content,
        }

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
            record = self._catalog.get_stem(zim_name) if self._catalog else None
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
                record = self._catalog.get_stem(zim_name) if self._catalog else None
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
        if icon_path:
            return self._read_archive_article(archive, zim_name, icon_path, False)
        try:
            item = archive.get_illustration_item(48)
            content = item.content
            mimetype = (
                item.mimetype or "application/octet-stream"
            ).split(";", 1)[0].strip().lower()
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as error:
            raise ArticleNotFoundError("source icon not found") from error
        if len(content) > 1 << 20:
            raise ArticleTooLargeError("source icon exceeds the 1048576 byte serving limit")
        return {
            "zim": zim_name,
            "path": None,
            "title": zim_name,
            "mimetype": mimetype,
            "content": content,
        }

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
        if self._catalog:
            # Consult the catalog even when the ZIM has no search source (for
            # example a library-only archive or a duplicate removed from
            # search). The favicon BLOB is the same illustration the discovery
            # code selected.
            data, mime = self._catalog.favicon(zim_name)
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
        """True when an archive entry is a readable, non-redirect article.

        The SQLite path only samples rows in ``docs`` (entries ``read_entry``
        accepted), so the filesystem fallback must reject redirects and
        non-article assets (images, stylesheets, scripts) to avoid redirecting
        /random to a binary file. Content-level checks (JS shells, block length)
        that ``read_entry`` also applies are not replicated here because they
        would require reading the article.
        """
        if entry.is_redirect or not entry.path:
            return False
        try:
            item = entry.get_item()
        except (KeyError, RuntimeError, ValueError):
            return False
        mimetype = (item.mimetype or "").split(";", 1)[0].strip().lower()
        return mimetype in _READABLE_MIMETYPES

    def _random_indexed_article(self, index: _LocalIndex, db: sqlite3.Connection) -> str | None:
        """Pick a uniformly random indexed article, or None when there are none."""
        if not db.execute(
            "SELECT 1 FROM docs WHERE target IS NULL LIMIT 1"
        ).fetchone():
            return None
        archive = index.ensure_open()
        upper = max(1, int(getattr(archive, "entry_count", 0) or 0)) if archive else 1
        # Entry rowids are dense entry ids, so sampling one and rejecting gaps,
        # unindexed entries and redirects gives every eligible row equal weight
        # in O(1) per probe. This is fine for normal indexes, but an archive can
        # be extremely sparse: an OSM ZIM has a few hundred articles among
        # ~800k tile entries, so a bounded probe budget would almost always miss.
        for _ in range(RANDOM_ARTICLE_ATTEMPTS):
            point = secrets.randbelow(upper)
            row = db.execute(
                "SELECT path FROM docs WHERE rowid = ? AND target IS NULL",
                (point,),
            ).fetchone()
            if row and row[0]:
                return str(row[0])
        # Sparse index: fall back to a uniform COUNT/OFFSET pick. The O(n) count
        # is only paid here, where the probe budget already showed the eligible
        # set is small relative to the index.
        count = int(
            db.execute(
                "SELECT COUNT(*) FROM docs WHERE target IS NULL"
            ).fetchone()[0]
        )
        if count <= 0:
            return None
        row = db.execute(
            "SELECT path FROM docs WHERE target IS NULL "
            "ORDER BY rowid LIMIT 1 OFFSET ?",
            (secrets.randbelow(count),),
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def _random_fallback_article(self, index: _LocalIndex) -> str | None:
        """Pick a uniformly random readable entry from an unindexed archive."""
        archive = index.ensure_open()
        if archive is None:
            return None
        upper = max(1, int(getattr(archive, "entry_count", 0) or 0))
        for _ in range(RANDOM_ARTICLE_ATTEMPTS):
            point = secrets.randbelow(upper)
            try:
                entry = archive._get_entry_by_id(point)
            except (KeyError, RuntimeError, ValueError, IndexError):
                continue
            if self._is_random_article_entry(entry):
                return entry.path
        # Sparse archive (for example, an OSM ZIM): probes miss, and there is no docs
        # table to COUNT/OFFSET against, so scan the readable entry ids once and
        # reuse the list. The scan is O(entry_count) and happens at most once per
        # open index; storing ids keeps it far smaller than storing paths.
        ids = index.random_article_ids
        if ids is None:
            ids = []
            for point in range(upper):
                try:
                    entry = archive._get_entry_by_id(point)
                except (KeyError, RuntimeError, ValueError, IndexError):
                    continue
                if self._is_random_article_entry(entry):
                    ids.append(point)
            index.random_article_ids = ids
        if not ids:
            return None
        try:
            entry = archive._get_entry_by_id(ids[secrets.randbelow(len(ids))])
        except (KeyError, RuntimeError, ValueError, IndexError):
            return None
        return entry.path

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

            record = (
                self._catalog.get_stem_size(name, index.source_size)
                if self._catalog
                else None
            )
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
                and not getattr(self, "_defer_faiss", False)
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
                if getattr(self, "preview_cache", None) is not None
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

        wanted = set(zim)
        # Match the same aliases that a Kiwix catalog link may use: a remote
        # book slug carries a date suffix, while a local index is identified by
        # its date-stripped key. Comparing base keys preserves those links
        # without overwriting the local source's own name/book metadata.
        wanted_keys = {_zim_key(value) for value in wanted}
        selected = [
            source
            for source in available
            if wanted.intersection({source.key, source.name, source.book, source.local_name})
            or source.key in wanted_keys
        ]
        if not selected:
            # A stale bookmark or a source removed since the URL was saved must
            # not silently turn into a zero-source search. Fall back to the full
            # library and let the normal availability rules decide.
            logger.warning(
                "zimantic: ignoring unknown source selection (%s); searching all sources",
                ", ".join(sorted(wanted)),
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
        default = getattr(self, "page_size", DEFAULT_PAGE_SIZE)
        ceiling = getattr(self, "max_results", DEFAULT_MAX_RESULTS)
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
            getattr(self, "max_results", DEFAULT_MAX_RESULTS),
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
        method_counts = {"semantic": 0, "title": 0, "fulltext": 0}
        for doc in pool:
            matches = doc.get("ranking", {}).get("rrf", {})
            for method in method_counts:
                if matches.get(method, {}).get("rank") is not None:
                    method_counts[method] += 1
        return method_counts

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
                "zimantic: query embedding failed (%s); using title and full-text search",
                error,
            )
            return None

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

    def search(
        self,
        query: str,
        zim: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        source: str | list[str] | None = None,
        debug: bool = False,
    ) -> list[dict]:
        """Return one page of the final result set."""
        page = self.search_page(query, zim, limit, offset, source, debug)
        return list(page["results"])

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
            "snapshot", state, total_sources, total_sources, offset, page_size, matched_by
        )
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
            generation = getattr(self, "_cache_generation", 0)
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
            current_generation = getattr(self, "_cache_generation", 0)
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
        completed_futures: set[Future[SourceResult]] = set()
        completed: list[SourceResult] = []
        reported_counts: dict[str, int | None] = {}
        cache_value: Any = None
        abandoned = False
        try:
            # Wait briefly for a slot so a burst of retries can be absorbed;
            # only give up (busy) when nothing frees within the budget. Done
            # inside the try so an interrupt still releases the inflight key.
            acquired = self._acquire_search_slot()
            if not acquired:
                yield {
                    "type": "error",
                    "error": "server is busy; try again shortly",
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
                    current_generation = getattr(self, "_cache_generation", 0)
                if not selective and current_generation == generation:
                    self.cache.put(cache_key, cache_value)
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
            max_stream_ranks = max(
                1,
                getattr(
                    self,
                    "max_stream_ranks",
                    _int_config(
                        self.cfg,
                        "max_stream_ranks",
                        DEFAULT_MAX_STREAM_RANKS,
                    ),
                ),
            )
            rank_interval = max(1, math.ceil(total_sources / max_stream_ranks))
            update_interval = max(
                0.0,
                getattr(
                    self,
                    "stream_update_interval",
                    DEFAULT_STREAM_UPDATE_INTERVAL,
                ),
            )
            source_limit = self._source_limit(total_sources)
            pool: list[dict[str, Any]] = []
            counts: dict[str, int] = {}
            for src in sources:
                futures.append(
                    self._executor.submit(
                        self._search_source,
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
                        completed_futures.add(future)
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
            yield self._event(
                "done",
                state,
                len(completed),
                total_sources,
                offset,
                page_size,
                self._matched_by(cache_value["pool"]) if debug else None,
            )
        except GeneratorExit:
            # A disconnected streaming response closes this generator while
            # source workers are still running. Let a background finalizer drain
            # them and populate the shared cache instead of starting duplicate
            # searches for the next page reload.
            abandoned = True
            raise
        finally:
            if abandoned and futures:
                threading.Thread(
                    target=self._finish_abandoned_search,
                    args=(
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
                        futures,
                        completed_futures,
                        completed,
                        reported_counts,
                        acquired,
                    ),
                    name="zimantic-abandoned-search",
                    daemon=True,
                ).start()
            else:
                for future in futures:
                    future.cancel()
                if acquired:
                    self._release_search_slot()
                self._finish_inflight(inflight_key, pending, cache_value)

    def _finish_abandoned_search(
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
        selective: bool,
        futures: list[Future[SourceResult]],
        completed_futures: set[Future[SourceResult]],
        completed: list[SourceResult],
        reported_counts: dict[str, int | None],
        acquired: bool,
    ) -> None:
        """Finish a search after its streaming client has disconnected."""
        inflight_key = (generation, cache_key)
        cache_value: Any = None
        try:
            future_sources = dict(zip(futures, sources))
            deadline = time.monotonic() + SEARCH_CLAIM_WAIT_SECONDS
            for future in futures:
                if future in completed_futures:
                    continue
                src = future_sources[future]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    completed.append(
                        SourceResult(
                            source=src,
                            error="search timed out",
                            status="error",
                        )
                    )
                    continue
                try:
                    result = future.result(timeout=remaining)
                except TimeoutError:
                    result = SourceResult(
                        source=src,
                        error="search timed out",
                        status="error",
                    )
                except Exception as error:
                    result = SourceResult(
                        source=src,
                        error=f"search failed: {error}",
                        status="error",
                    )
                completed.append(result)

            pool_target = self._pool_target()
            _, cache_value = self._finalize_search(
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
            )
        except Exception:
            logger.exception("zimantic: abandoned search finalization failed")
        finally:
            if acquired:
                self._release_search_slot()
            self._finish_inflight(inflight_key, pending, cache_value)

    def _release_search_slot(self) -> None:
        self._search_slots.release()
        condition = getattr(self, "_slot_condition", None)
        if condition is not None:
            with condition:
                condition.notify_all()

    def _acquire_search_slot(self) -> bool:
        """Take a search slot, waiting up to ``SLOT_WAIT_SECONDS`` for one.

        Returns False only when every slot stays busy for the whole budget, at
        which point the caller reports busy. Test doubles without a condition
        fall back to a non-blocking attempt.
        """
        if self._search_slots.acquire(blocking=False):
            return True
        condition = getattr(self, "_slot_condition", None)
        if condition is None:
            return False
        deadline = time.monotonic() + SLOT_WAIT_SECONDS
        with condition:
            while True:
                # Retry under the condition before waiting. A slot released
                # between the first attempt and this block has no waiter to
                # notify, so waiting first would sleep for the full budget.
                if self._search_slots.acquire(blocking=False):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                condition.wait(remaining)

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
                minimum_similarity = _float_config(
                    self.cfg,
                    "min_cosine_similarity",
                    DEFAULT_MIN_COSINE_SIMILARITY,
                    -1.0,
                    1.0,
                )
                semantic_ids = [
                    (float(score), int(rowid))
                    for score, rowid in zip(similarities[0], ids[0])
                    if rowid >= 0 and score >= minimum_similarity
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
        searcher = index.ensure_searcher()
        archive = index.ensure_open()
        if db is None and searcher is None and index.faiss_index is None:
            # Nothing can answer this source: no SQLite index, no libzim
            # full-text searcher and no vectors. Report an error instead of a
            # successful empty result so a missing/corrupt archive is visible.
            return SourceResult(
                source=replace(source, available=False),
                error=index.error or "ZIM archive is unavailable",
                status="error",
            )
        if searcher and archive:
            # libzim's Searcher is not documented as thread-safe: serialize it.
            with index.lock:
                try:
                    paths = searcher.search(
                        Query().set_query(fulltext_query or query)
                    ).getResults(0, count)
                    for rank, path in enumerate(paths):
                        try:
                            entry = archive.get_entry_by_path(path)
                            rowid = entry._index
                            rowids.add(rowid)
                            archive_entries[rowid] = entry
                            fulltext_rows.append((rank, rowid))
                        except (KeyError, RuntimeError, ValueError):
                            continue
                except (RuntimeError, ValueError) as error:
                    errors.append(f"full-text search unavailable: {error}")

        docs = (
            self._fetch_docs(source, db, rowids)
            if db is not None
            else self._fetch_archive_docs(source, archive, archive_entries)
        )
        for score, rowid in semantic_ids:
            doc = docs.get(rowid)
            if doc:
                semantic.append((score, doc))
        keyword = [(score, docs[row["rowid"]]) for score, row in keyword if row["rowid"] in docs]
        for rank, rowid in fulltext_rows:
            doc = docs.get(rowid)
            if doc:
                fulltext.append((rank, doc))

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
            path = target.path
            title = getattr(target, "title", None) or path
            docs[rowid] = {
                "id": f"{source.key}:{path}",
                "source_key": source.key,
                "source": source.name,
                "source_icon": source.icon_url,
                "title": title,
                "lead": "",
                "path": path,
                "mimetype": None,
                "url": (
                    f"{self.web_base_path}/content/"
                    f"{quote(source.local_name or source.book, safe='')}/"
                    f"{quote(path, safe='/')}"
                ),
            }
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
            doc = {
                "id": f"{source.key}:{path}",
                "source_key": source.key,
                "source": source.name,
                "source_icon": source.icon_url,
                "title": title,
                "lead": "",
                "path": path,
                "mimetype": None,
                "url": (
                    f"{self.web_base_path}/content/"
                    f"{quote(source.local_name or source.book, safe='')}/"
                    f"{quote(path, safe='/')}"
                ),
            }
            docs[rowid] = doc
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
                            max_html_bytes=_int_config(
                                self.cfg,
                                "max_html_bytes",
                                DEFAULT_MAX_HTML_BYTES,
                            ),
                            max_entry_bytes=_int_config(
                                self.cfg,
                                "max_article_bytes",
                                33_554_432,
                            ),
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
        """Populate only the displayed local results, in parallel by source."""
        with self._state_lock:
            sources = dict(self.sources)
        preview_cache = getattr(self, "preview_cache", None)
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
        max_html_bytes = _int_config(self.cfg, "max_html_bytes", DEFAULT_MAX_HTML_BYTES)
        max_phrase_bytes = _int_config(
            self.cfg, "max_article_bytes", 33_554_432
        )
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
        limit = getattr(
            self,
            "preview_chars",
            _int_config(self.cfg, "max_preview_chars", DEFAULT_PREVIEW_CHARS),
        )
        return truncate_at_word_boundary(text, limit)

    @staticmethod
    def _source_items(
        semantic: list[tuple[float, dict[str, Any]]],
        keyword: list[tuple[float, dict[str, Any]]],
        fulltext: list[tuple[int, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        order: dict[str, tuple[int, int, str]] = {}
        for rank, (_, doc) in enumerate(semantic):
            order.setdefault(doc["id"], (rank, 0, doc["path"]))
        for rank, (_, doc) in enumerate(keyword):
            order.setdefault(doc["id"], (rank, 1, doc["path"]))
        for rank, (_, doc) in enumerate(fulltext):
            order.setdefault(doc["id"], (rank, 2, doc["path"]))
        docs = {doc["id"]: doc for _, doc in semantic}
        docs.update({doc["id"]: doc for _, doc in keyword})
        docs.update({doc["id"]: doc for _, doc in fulltext})
        result = []
        for rank, (matching, _) in enumerate(sorted(order.items(), key=lambda item: item[1])):
            item = dict(docs[matching])
            item["source_rank"] = rank + 1
            result.append(item)
        return result

    def _search_kiwix(self, source: SourceInfo, query: str, limit: int) -> SourceResult:
        server = str(self.cfg.get("kiwix_server") or "").strip().rstrip("/")
        # The search endpoint is fixed by kiwix-serve itself, not per-install.
        endpoint = server + "/kiwix/search"
        result_limit = min(
            max(self.candidate_count, limit),
            DEFAULT_KIWIX_RESULT_LIMIT,
        )
        params = urlencode({
            "pattern": query,
            "books.name": source.book,
            "pageLength": result_limit,
            "format": "xml",
        })
        response_limit = _int_config(
            self.cfg,
            "max_kiwix_response_bytes",
            DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
        )
        try:
            request = Request(endpoint + "?" + params, headers={"Accept": "application/xml"})
            with urlopen(request, timeout=self.source_timeout) as response:
                root = _parse_remote_xml(_read_limited_response(response, response_limit))
        except (HTTPError, URLError, TimeoutError, ValueError, ET.ParseError, OSError) as error:
            return SourceResult(
                source=source,
                error=f"Kiwix search failed: {error}",
                status="error",
            )

        docs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in root.iter():
            if _local_name(item) not in {"item", "entry"}:
                continue
            title = _child_text(item, "title")
            link = _child_text(item, "link")
            snippet = self._preview(re.sub(r"\s+", " ", _child_text(item, "description")).strip())
            identity = (title.casefold(), link)
            if not title and not link or identity in seen:
                continue
            seen.add(identity)
            parsed_link = urlparse(link)
            if link and not parsed_link.scheme and not parsed_link.netloc:
                link = urljoin(server.rstrip("/") + "/", link)
            docs.append({
                "id": f"{source.key}:{link or title}",
                "source_key": source.key,
                "source": source.name,
                "source_icon": source.icon_url,
                "title": title or "(untitled)",
                "lead": snippet,
                "path": link,
                "url": link,
                "source_rank": len(docs) + 1,
            })
        return SourceResult(
            source=source,
            items=docs,
            fulltext=[(rank, doc) for rank, doc in enumerate(docs)],
        )

    def _rank_results(
        self,
        source_results: list[SourceResult],
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        query_words = _query_terms(query)
        ranked_lists = _collect_ranked_candidates(source_results)
        semantic, keyword, fulltext, docs, source_by_key = ranked_lists
        scores, rrf_details, fulltext_weight = _rrf_scores(
            (semantic, keyword, fulltext),
            query,
            max(self.candidate_count, limit),
        )
        assessed = [
            _assess_ranked_document(
                identity,
                doc,
                query_words,
                query,
                source_by_key,
                scores,
                rrf_details,
                fulltext_weight,
            )
            for identity, doc in docs.items()
        ]
        assessed.sort(key=lambda item: (
            -item.total,
            # Title/full-text-only entries still appear, but a semantic hit wins
            # an otherwise exact tie. This keeps title-only pages (for example
            # JavaScript loader shells) below comparable meaning matches.
            not item.semantic_hit,
            -item.lexical,
            -item.title_quality,
            item.source_rank,
            item.source_casefold,
            item.path,
        ))
        ranked = []
        for final_rank, assessment in enumerate(assessed[:limit], 1):
            output = _render_ranked_document(assessment)
            output["ranking"]["final_rank"] = final_rank
            ranked.append(output)
        return ranked
