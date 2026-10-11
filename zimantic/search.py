"""Hybrid search over local ZIM indexes and optional Kiwix sources."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
import logging
import math
import posixpath
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, unquote, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from libzim.reader import Archive
from libzim.search import Query, Searcher

from .cache import DEFAULT_CACHE_BYTES, DEFAULT_CACHE_SIZE, QueryCache
from .contracts import SourceInfo, SourceResult
from .zim import DEFAULT_PREVIEW_CHARS, truncate_at_word_boundary

logger = logging.getLogger(__name__)


MAX_QUERY_LENGTH = 4096
DEFAULT_CANDIDATES = 16
DEFAULT_SOURCE_TIMEOUT = 12
DEFAULT_MAX_CONCURRENT_SEARCHES = 4
DEFAULT_MAX_STREAM_RANKS = 16
DEFAULT_NPROBE_FRACTION = 0.06
DEFAULT_MIN_COSINE_SIMILARITY = 0.85
DEFAULT_PAGE_SIZE = 10
DEFAULT_MAX_RESULTS = 100
DEFAULT_MAX_KIWIX_RESPONSE_BYTES = 64 << 20
DEFAULT_KIWIX_CATALOG_LIMIT = 50_000
DEFAULT_KIWIX_RESULT_LIMIT = 1_000
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "how", "what", "where", "when", "why", "who", "which", "can", "could",
    "do", "does", "did", "i", "you", "my", "me", "to", "of", "in", "on",
    "at", "for", "with", "about", "find", "some", "any", "there", "it",
    "that", "this", "and", "or", "if", "so", "will", "would", "should",
}
INTENT_RULES = (
    (re.compile(r"^wikihow", re.I), ("how to", "how do", "how can", "how should", "steps to")),
)

# Exact disambiguation-title queries are navigation; other matches are demoted.
DISAMBIGUATION_TITLE = re.compile(r"\s*\(disambiguation\)\s*$", re.I)
DEFAULT_DISAMBIGUATION_BOOST = 0.08
DEFAULT_DISAMBIGUATION_PENALTY = 0.5


def _exact_title_intent(query_words: list[str], title: str) -> bool:
    """True when the query names the full title exactly: navigation intent."""
    hub = _query_terms(title)
    return bool(query_words) and len(hub) == len(query_words) and set(hub) == set(query_words)


def _disambiguation_intent(query: str, query_words: list[str], title: str) -> bool:
    """True when a query explicitly requests a disambiguation page."""
    if _exact_title_intent(query_words, title):
        return True
    if not query.rstrip().endswith("?"):
        return False
    base_title = DISAMBIGUATION_TITLE.sub("", title)
    return _exact_title_intent(query_words, base_title)


@dataclass
class _LocalIndex:
    db: sqlite3.Connection | None
    faiss_index: Any | None
    archive: Archive | None
    searcher: Searcher | None
    lock: threading.Lock
    db_path: str | None = None  # read-only sqlite URI, used for per-thread connections
    db_signature: tuple[int, int, int, int] | None = None
    faiss_signature: tuple[int, int, int, int] | None = None
    _thread: threading.local = field(default_factory=threading.local)
    _conns: list[sqlite3.Connection] = field(default_factory=list)
    _lifecycle: threading.Condition = field(default_factory=threading.Condition, repr=False)
    _active_searches: int = 0
    _retired: bool = False
    _closed: bool = False
    done: str = "1"
    source_size: int | None = None
    zim_path: str | None = None
    zim_signature: tuple[int, int, int, int] | None = None

    @property
    def semantic(self) -> bool:
        """True when vectors are loaded and semantic search can run."""
        return self.faiss_index is not None


class SearchQueryError(ValueError):
    """The requested query cannot be processed."""


class SearchBusyError(Exception):
    """Too many searches are already running; the client should retry."""


class ArticleNotFoundError(Exception):
    """The requested local ZIM or article path does not exist."""


class ArticleUnavailableError(Exception):
    """A local ZIM exists but cannot currently serve the requested article."""


class ArticleTooLargeError(ArticleUnavailableError):
    """The requested entry exceeds the configured serving limit."""


def _int_config(cfg: dict, key: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(cfg.get(key, default)))
    except (TypeError, ValueError):
        return default


def _float_config(
    cfg: dict,
    key: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(maximum, max(minimum, value))


def _file_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _bounded_catalog_url(url: str, limit: int) -> str:
    """Replace an unbounded Kiwix catalog count with a large finite limit."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    bounded = []
    replaced = False
    for key, value in query:
        if key == "count":
            if not replaced:
                bounded.append((key, str(limit)))
                replaced = True
        else:
            bounded.append((key, value))
    if not replaced:
        bounded.append(("count", str(limit)))
    return urlunsplit(parts._replace(query=urlencode(bounded)))


def _read_limited_response(response, limit: int) -> bytes:
    """Read a remote response without allowing unbounded memory growth."""
    content_length = response.headers.get("Content-Length")
    try:
        advertised = int(content_length) if content_length else 0
    except (TypeError, ValueError):
        advertised = 0
    if advertised > limit:
        raise ValueError(f"response exceeds the {limit} byte limit")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"response exceeds the {limit} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_remote_xml(payload: bytes) -> ET.Element:
    """Parse trusted-shaped XML while rejecting DTD/entity expansion inputs."""
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", payload, re.IGNORECASE):
        raise ValueError("DTD and entity declarations are not supported")
    return ET.fromstring(payload)


def _nprobe(faiss_index: Any, cfg: dict) -> int:
    """Choose IVF probes while allowing an explicit fixed override."""
    nlist = getattr(faiss_index, "nlist", 0)
    if "nprobe" in cfg:
        probes = _int_config(cfg, "nprobe", 64)
    elif nlist:
        probes = math.ceil(nlist * DEFAULT_NPROBE_FRACTION)
    else:
        probes = 64
    return min(probes, nlist) if nlist else probes


def _zim_key(book: str) -> str:
    return re.sub(r"_\d{4}-\d{2}(-\d{2})?$", "", book)


def _intent_phrases(book: str) -> tuple[str, ...]:
    key = _zim_key(book)
    for pattern, phrases in INTENT_RULES:
        if pattern.search(key):
            return phrases
    return ()


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child) == name:
            return "".join(child.itertext()).strip()
    return ""


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _content_terms(text: str) -> list[str]:
    return [
        _stem(word)
        for word in re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)
        if word not in STOPWORDS | {"your", "their", "his", "her", "its", "our"}
    ]


def _query_terms(query: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for term in _content_terms(query):
        if term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def _query_words(query: str) -> list[str]:
    words = re.findall(r"[^\W_]+", query.casefold(), re.UNICODE)
    kept = [word for word in words if word not in STOPWORDS]
    return kept or words


def _title_query(query: str) -> str:
    """FTS5 MATCH expression: every word must appear as an exact title token."""
    return " AND ".join(f'"{word.replace(chr(34), chr(34) * 2)}"' for word in _query_words(query))


def _title_prefix_query(query: str) -> str:
    """FTS5 MATCH expression with every word as a prefix token.

    Used only as a fallback when exact title matching returns nothing, so a
    truncated or base-form word still matches its title (e.g. "chang" or
    "change" -> "Changes"). One-character words stay exact to avoid scanning
    the whole index for prefixes like "c*".
    """
    return " AND ".join(
        f"{word}*" if len(word) > 1 else f'"{word}"' for word in _query_words(query)
    )


def _fulltext_query(query: str) -> str:
    """Query for the ZIM full-text index (libzim/Xapian): bare words.

    libzim parses this with only Xapian's FLAG_CJK_NGRAM, so quotes, ``AND`` and
    ``OR`` are treated as literal text rather than operators. We therefore send
    plain words and rely on its default OP_AND to combine them.
    """
    return " ".join(_query_words(query))


def _coverage(query: list[str], tokens: list[str]) -> float:
    if not query:
        return 0.0
    present = set(tokens)
    return sum(term in present for term in query) / len(query)


def _phrase(query: list[str], tokens: list[str]) -> float:
    if not query:
        return 0.0
    width = len(query)
    return float(any(tokens[i:i + width] == query for i in range(len(tokens) - width + 1)))


def _density(query: list[str], tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    wanted = set(query)
    return sum(token in wanted for token in tokens) / len(tokens)


def _intent_match(source: SourceInfo, question: str) -> float:
    lowered = question.casefold()
    return float(any(re.search(r"\b" + re.escape(phrase) + r"\b", lowered) for phrase in source.intent_phrases))


class Search:
    def __init__(self, cfg: dict, embedder=None, semantic: bool = True):
        self.cfg = cfg
        self.embedder = embedder
        # Fast mode skips FAISS and the model.
        self.semantic = bool(semantic)
        self.indexes: dict[str, _LocalIndex] = {}
        self.sources: dict[str, SourceInfo] = {}
        self._state_lock = threading.RLock()
        self._reload_lock = threading.Lock()
        self.source_timeout = _int_config(cfg, "source_timeout", DEFAULT_SOURCE_TIMEOUT)
        self.candidate_count = _int_config(cfg, "candidate_count", DEFAULT_CANDIDATES)
        self.source_workers = _int_config(cfg, "search_workers", 4)
        self.preview_chars = _int_config(cfg, "max_preview_chars", DEFAULT_PREVIEW_CHARS)
        self.page_size = _int_config(cfg, "page_size", DEFAULT_PAGE_SIZE)
        self.max_results = _int_config(cfg, "max_results", DEFAULT_MAX_RESULTS)
        self.max_concurrent_searches = _int_config(
            cfg, "max_concurrent_searches", DEFAULT_MAX_CONCURRENT_SEARCHES
        )
        self.max_stream_ranks = _int_config(
            cfg, "max_stream_ranks", DEFAULT_MAX_STREAM_RANKS
        )
        self._search_slots = threading.BoundedSemaphore(self.max_concurrent_searches)
        self._executor = ThreadPoolExecutor(max_workers=self.source_workers, thread_name_prefix="zimantic-search")
        # Identical in-flight queries share one computation.
        self._inflight: dict[Any, Future] = {}
        self._inflight_lock = threading.Lock()
        self._cache_generation = 0
        self.cache = QueryCache(
            _int_config(cfg, "cache_size", DEFAULT_CACHE_SIZE, minimum=0),
            _int_config(cfg, "cache_bytes", DEFAULT_CACHE_BYTES, minimum=0),
        )
        self._load_indexes()
        # Local indexes are ready immediately; the optional Kiwix catalog is
        # refreshed in the background so an unreachable Kiwix server cannot
        # delay startup (see start_catalog_refresh).
        self.refresh_local_sources()

    @staticmethod
    def _index_uri(db_path: Path) -> str:
        return db_path.resolve().as_uri() + "?mode=ro"

    def _find_zim_path(self, stem: str, source_size: int | None) -> Path | None:
        """Find a matching archive anywhere below the configured ZIM directory."""
        zim_dir = self.cfg.get("zim_dir")
        if not zim_dir:
            return None
        try:
            candidates = sorted(
                path for path in Path(zim_dir).rglob("*.zim")
                if path.stem == stem
            )
        except OSError:
            return None

        matching: list[Path] = []
        for path in candidates:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if source_size is None or size == source_size:
                matching.append(path)
            elif source_size is not None:
                # A stem with a different size is a conflicting identity, not
                # a valid archive for this index.
                return None
        return matching[0] if matching else None

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
            if self.semantic and done_value == "1" and faiss_path.exists():
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

            archive = None
            searcher = None
            zim_path = self._find_zim_path(db_path.stem, source_size)
            if zim_path is not None:
                try:
                    archive = Archive(str(zim_path))
                    searcher = Searcher(archive) if archive.has_fulltext_index else None
                except Exception as error:  # unreadable ZIM: title search still works
                    logger.warning(
                        "%s: ZIM unavailable (%s); title search only",
                        db_path.stem,
                        error,
                    )
                    archive = None
                    searcher = None
            # The connection used for the one-off "done" check is not used for
            # searches (those use per-thread read-only connections); close it
            # rather than hold an idle descriptor open for the index lifetime.
            db.close()
            return _LocalIndex(
                None,
                faiss_index,
                archive,
                searcher,
                threading.Lock(),
                db_path=uri,
                db_signature=_file_signature(db_path),
                faiss_signature=_file_signature(faiss_path),
                done=done_value,
                source_size=source_size,
                zim_path=str(zim_path) if zim_path is not None else None,
                zim_signature=_file_signature(zim_path) if zim_path is not None else None,
            )
        except sqlite3.Error:
            db.close()
            return None

    def _load_indexes(self) -> None:
        for db_path in sorted(Path(self.cfg["index_dir"]).glob("*.sqlite")):
            index = self._open_index(db_path)
            if index:
                self.indexes[db_path.stem] = index

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

    def reload(self) -> dict[str, Any]:
        """Rescan index_dir without restarting. Picks up new finished indexes,
        forgets removed ones, upgrades fast indexes that gained vectors, and
        refreshes the Kiwix catalog. Cheap enough for systemd.path to trigger."""
        with self._reload_lock:
            found = {
                path.stem: path
                for path in sorted(Path(self.cfg["index_dir"]).glob("*.sqlite"))
            }
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

            for name, db_path in found.items():
                with self._state_lock:
                    current = self.indexes.get(name)
                db_signature = _file_signature(db_path)
                faiss_signature = (
                    _file_signature(db_path.with_suffix(".faiss"))
                    if self.semantic
                    else None
                )
                changed = (
                    current is not None
                    and (
                        current.db_signature != db_signature
                        or (
                            self.semantic
                            and current.faiss_signature != faiss_signature
                        )
                        or (
                            current.zim_path
                            != (
                                str(self._find_zim_path(name, current.source_size))
                                if current.source_size is not None
                                else current.zim_path
                            )
                        )
                    )
                )
                if current is not None and not changed:
                    continue

                fresh = self._open_index(db_path)
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
                with self._state_lock:
                    self._cache_generation = getattr(self, "_cache_generation", 0) + 1
                self.cache.clear()
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
        Search._close_connections(connections)

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
            else:
                connections = []
        if connections:
            Search._close_connections(connections)

    def _db_for(self, index: _LocalIndex) -> sqlite3.Connection:
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
        entries = self._catalog_entries(server) if server else []
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

    def _rebuild_sources(self, entries: list[dict[str, str]]) -> list[dict[str, Any]]:
        with self._state_lock:
            local: dict[str, SourceInfo] = {}
            for local_name in self.indexes:
                key = _zim_key(local_name)
                if key in local:
                    key = local_name
                homepage = None
                index = self.indexes[local_name]
                if index.archive is not None:
                    try:
                        with index.lock:
                            homepage = self._resolve_article_entry(index.archive).path
                    except (
                        ArticleNotFoundError,
                        ArticleUnavailableError,
                        RuntimeError,
                        ValueError,
                    ):
                        homepage = None
                local[key] = SourceInfo(
                    key=key,
                    name=local_name,
                    book=local_name,
                    mode="local",
                    local_name=local_name,
                    homepage=homepage,
                    intent_phrases=_intent_phrases(local_name),
                )

            merged = dict(local)
            for entry in entries:
                key = _zim_key(entry["book"])
                current = merged.get(key)
                if current and current.mode == "local":
                    merged[key] = replace(
                        current,
                        name=entry["name"],
                        book=entry["book"],
                        intent_phrases=_intent_phrases(entry["book"]),
                    )
                else:
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
            sources = self.source_dicts()
        if changed:
            # The set of searchable sources changed, so prior answers can be stale.
            # Keeping the cache while the source set is unchanged lets repeated
            # queries (e.g. page reloads) be answered without recomputing.
            with self._state_lock:
                self._cache_generation = getattr(self, "_cache_generation", 0) + 1
            self.cache.clear()
        return sources

    def _catalog_entries(self, server: str) -> list[dict[str, str]]:
        path = str(
            self.cfg.get(
                "kiwix_catalog_path",
                "/kiwix/catalog/v2/entries?count=50000",
            )
        )
        url = server + (path if path.startswith("/") else "/" + path)
        url = _bounded_catalog_url(
            url,
            _int_config(
                self.cfg,
                "kiwix_catalog_limit",
                DEFAULT_KIWIX_CATALOG_LIMIT,
            ),
        )
        response_limit = _int_config(
            self.cfg,
            "max_kiwix_response_bytes",
            DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
        )
        try:
            request = Request(url, headers={"Accept": "application/atom+xml, application/xml"})
            with urlopen(request, timeout=self.source_timeout) as response:
                root = _parse_remote_xml(_read_limited_response(response, response_limit))
        except (HTTPError, URLError, TimeoutError, ValueError, ET.ParseError, OSError):
            return []

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
            book = unquote(urlparse(href).path.rstrip("/").split("/")[-1])
            if not book:
                continue
            item = {"name": title or book, "book": book, "updated": updated}
            key = _zim_key(book)
            previous = newest.get(key)
            if not previous or (book, updated) > (previous["book"], previous["updated"]):
                newest[key] = item

        items = list(newest.values())
        title_counts: dict[str, int] = {}
        for item in items:
            title_counts[item["name"]] = title_counts.get(item["name"], 0) + 1
        for item in items:
            if title_counts[item["name"]] > 1:
                item["name"] = f"{item['name']} ({_zim_key(item['book'])})"
        return items

    def source_dicts(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return [source.to_dict() for source in self.sources.values()]

    def local_names(self) -> list[str]:
        with self._state_lock:
            return list(self.indexes)

    @staticmethod
    def _article_path(path: str) -> str:
        path = unquote(str(path))
        if not path or path.startswith("/") or "\\" in path:
            raise ArticleNotFoundError("invalid article path")
        if any(part == ".." for part in path.split("/")):
            raise ArticleNotFoundError("invalid article path")
        normalized = posixpath.normpath(path)
        if normalized in {"", "."} or normalized != path:
            raise ArticleNotFoundError("invalid article path")
        return path

    @staticmethod
    def _resolve_article_entry(archive: Archive, path: str | None = None):
        try:
            entry = archive.main_entry if path is None else archive.get_entry_by_path(path)
        except (KeyError, RuntimeError, ValueError) as error:
            raise ArticleNotFoundError("article not found") from error

        seen: set[str] = set()
        for _ in range(32):
            if not entry.is_redirect:
                return entry
            if entry.path in seen:
                raise ArticleUnavailableError("article redirect cycle")
            seen.add(entry.path)
            try:
                entry = entry.get_redirect_entry()
            except (KeyError, RuntimeError, ValueError) as error:
                raise ArticleUnavailableError("article redirect is unavailable") from error
        raise ArticleUnavailableError("article redirect chain is too long")

    def read_article(self, zim_name: str, path: str | None = None) -> dict[str, Any]:
        """Read one local ZIM entry without changing indexes or search state."""
        with self._state_lock:
            index = self.indexes.get(zim_name)
        if index is None:
            raise ArticleNotFoundError("ZIM not found")
        if index.archive is None:
            raise ArticleUnavailableError("ZIM archive is unavailable")

        acquired = False
        try:
            try:
                self._acquire_index(index)
            except RuntimeError as error:
                raise ArticleUnavailableError("ZIM is being reloaded") from error
            acquired = True
            requested_path = None if path is None else self._article_path(path)
            with index.lock:
                try:
                    entry = self._resolve_article_entry(index.archive, requested_path)
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
                    content = item.content
                except (ArticleNotFoundError, ArticleUnavailableError):
                    raise
                except (KeyError, RuntimeError, ValueError) as error:
                    raise ArticleUnavailableError("article content is unavailable") from error
            if len(content) > max_bytes:
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
        finally:
            if acquired:
                self._release_index(index)

    def random_article_path(self, zim_name: str) -> str:
        """Return a random indexed article path without changing search state."""
        with self._state_lock:
            index = self.indexes.get(zim_name)
        if index is None:
            raise ArticleNotFoundError("ZIM not found")
        if index.archive is None:
            raise ArticleUnavailableError("ZIM archive is unavailable")

        acquired = False
        try:
            try:
                self._acquire_index(index)
            except RuntimeError as error:
                raise ArticleUnavailableError("ZIM is being reloaded") from error
            acquired = True
            db = self._db_for(index)
            with index.lock:
                try:
                    count = int(
                        db.execute(
                            "SELECT count(*) FROM docs WHERE target IS NULL"
                        ).fetchone()[0]
                    )
                    if count < 1:
                        raise ArticleNotFoundError("ZIM has no indexed articles")
                    offset = secrets.randbelow(count)
                    row = db.execute(
                        "SELECT path FROM docs "
                        "WHERE target IS NULL ORDER BY rowid LIMIT 1 OFFSET ?",
                        (offset,),
                    ).fetchone()
                except ArticleNotFoundError:
                    raise
                except (sqlite3.Error, TypeError, ValueError) as error:
                    raise ArticleUnavailableError(
                        "random article selection is unavailable"
                    ) from error
            if not row or not row[0]:
                raise ArticleUnavailableError("random article selection is unavailable")
            return self._article_path(str(row[0]))
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

            archive_path = (
                Path(index.zim_path)
                if index.zim_path
                else self._find_zim_path(name, index.source_size)
            )
            archive_current = archive_path is not None and archive_path.exists() and index.archive is not None
            if not archive_current:
                reasons.append("ZIM archive unavailable")

            vectors_expected = self.semantic and index.done == "1"
            vectors_loaded = index.faiss_index is not None
            if vectors_expected and not vectors_loaded:
                reasons.append("semantic vectors unavailable")

            report: dict[str, Any] = {
                "name": name,
                "status": "ok" if not reasons else "degraded",
                "index_file": db_current,
                "archive": archive_current,
                "capabilities": {
                    "title": True,
                    "fulltext": index.searcher is not None,
                    "semantic": vectors_loaded,
                    "article": archive_current,
                },
            }
            if reasons:
                report["issues"] = reasons

            acquired = False
            if archive_current:
                try:
                    self._acquire_index(index)
                    acquired = True
                    with index.lock:
                        report["entry_count"] = int(index.archive.entry_count)
                        try:
                            report["homepage"] = self._resolve_article_entry(index.archive).path
                        except ArticleNotFoundError:
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
        }

    def _selected_sources(self, zim: list[str] | None) -> list[SourceInfo]:
        with self._state_lock:
            available = [source for source in self.sources.values() if source.available]
        if not zim:
            return available

        wanted = set(zim)
        selected: list[SourceInfo] = []
        for source in available:
            if wanted.intersection({source.key, source.name, source.book, source.local_name}):
                selected.append(source)
        return selected

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
        """
        selection = tuple(sorted(zim)) if zim else None
        normalized = re.sub(r"\s+", " ", query).strip().casefold()
        return (normalized, selection)

    def _page_size(self, limit: int | None) -> int:
        default = getattr(self, "page_size", DEFAULT_PAGE_SIZE)
        ceiling = getattr(self, "max_results", DEFAULT_MAX_RESULTS)
        if limit is None:
            return default
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

    @staticmethod
    def _source_counts(pool: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for doc in pool:
            key = doc.get("source_key", "")
            counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _public(doc: dict[str, Any], debug: bool) -> dict[str, Any]:
        """A response copy. Ranking metadata is only shipped in debug mode."""
        if debug:
            return dict(doc)
        return {key: value for key, value in doc.items() if key not in ("explain", "score")}

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
        scoped = pool if not selected else [
            doc for doc in pool if doc.get("source_key") in selected
        ]
        total = len(scoped)
        page = scoped[offset:offset + page_size]
        return {
            "results": [self._public(doc, debug) for doc in page],
            "total": total,
            "has_more": offset + page_size < total,
            "counts": self._source_counts(pool),
        }

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
            "counts": final.get("counts", {}),
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
    ) -> Iterator[dict[str, Any]]:
        """Replay a cached pool as a finished stream (progress + one page)."""
        pool, cached_sources = self._unpack_cache(cached)
        state = self._page_state(pool, source, offset, page_size, debug)
        total_sources = len(sources)
        yield {
            "type": "started",
            "query": query,
            "sources": [src.to_dict() for src in sources],
            "total_sources": total_sources,
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
        snapshot = {
            "results": state["results"],
            "completed": total_sources,
            "total_sources": total_sources,
            "total": state["total"],
            "has_more": state["has_more"],
            "offset": offset,
            "limit": page_size,
            "counts": state["counts"],
        }
        yield {"type": "snapshot", **snapshot}
        yield {"type": "done", **snapshot}

    def _finish_inflight(self, key: Any, pending: Future, value: Any) -> None:
        with self._inflight_lock:
            if self._inflight.get(key) is pending:
                del self._inflight[key]
        if not pending.done():
            pending.set_result(value)

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
        the response. The pool is cached by `(query, sources)`, so every page
        and display filter reuses one computation. Concurrent identical queries
        share a single computation.
        """
        query = self._validate_query(query)
        page_size = self._page_size(limit)
        offset = max(0, int(offset))
        cache_key = self._cache_key(query, zim)
        with self._state_lock:
            generation = getattr(self, "_cache_generation", 0)
        inflight_key = (generation, cache_key)
        sources = self._selected_sources(zim)
        total_sources = len(sources)

        # Coalesce identical in-flight searches: wait for the owner, or become
        # the owner and compute once.
        pending: Future | None = None
        while True:
            cached = self.cache.get(cache_key)
            if (
                cached is not None
                and isinstance(cached, dict)
                and cached.get("generation") == generation
            ):
                yield from self._cached_events(
                    cached, query, sources, source, offset, page_size, debug
                )
                return
            with self._inflight_lock:
                existing = self._inflight.get(inflight_key)
                if existing is None:
                    pending = Future()
                    self._inflight[inflight_key] = pending
                    break
            # Reuse another request's result when it finishes.
            try:
                cached = existing.result(timeout=self.source_timeout * 4)
            except Exception:
                cached = None
            if (
                cached is not None
                and isinstance(cached, dict)
                and cached.get("generation") == generation
            ):
                yield from self._cached_events(
                    cached, query, sources, source, offset, page_size, debug
                )
                return
            # Owner failed or was cancelled; loop to claim or read a newer result.

        # Fail fast when all search slots are occupied.
        acquired = self._search_slots.acquire(blocking=False)
        futures: list[Future[SourceResult]] = []
        completed: list[SourceResult] = []
        cache_value: Any = None
        try:
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
                "offset": offset,
                "limit": page_size,
            }
            if not sources:
                cache_value = {"generation": generation, "pool": [], "sources": []}
                with self._state_lock:
                    current_generation = getattr(self, "_cache_generation", 0)
                if current_generation == generation:
                    self.cache.put(cache_key, cache_value)
                yield {
                    "type": "done",
                    "results": [],
                    "completed": 0,
                    "total_sources": 0,
                    "total": 0,
                    "has_more": False,
                    "offset": offset,
                    "limit": page_size,
                    "counts": {},
                }
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
                        pool_target,
                    )
                )

            future_sources = dict(zip(futures, sources))
            for future in as_completed(futures):
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
                should_rank = (
                    len(completed) == 1
                    or len(completed) % rank_interval == 0
                    or len(completed) == total_sources
                )
                if should_rank:
                    pool = self._rank_results(completed, query, pool_target)
                    counts = self._source_counts(pool)
                yield {
                    "type": "source",
                    **result.to_event(counts.get(src.key, 0)),
                    "completed": len(completed),
                    "total_sources": total_sources,
                }
                if should_rank:
                    state = self._page_state(pool, source, offset, page_size, debug)
                    yield {
                        "type": "snapshot",
                        "results": state["results"],
                        "completed": len(completed),
                        "total_sources": total_sources,
                        "total": state["total"],
                        "has_more": state["has_more"],
                        "offset": offset,
                        "limit": page_size,
                        "counts": state["counts"],
                    }

            if len(completed) != total_sources:
                pool = self._rank_results(completed, query, pool_target)
                counts = self._source_counts(pool)
            self._populate_result_mimetypes(pool)
            counts = self._source_counts(pool)
            # The cache holds only the deduplicated ranked pool plus compact
            # source outcomes, never a copy of every source's item list.
            cache_value = {
                "generation": generation,
                "pool": pool,
                "sources": [
                    result.to_event(counts.get(result.source.key, 0))
                    for result in completed
                ],
            }
            with self._state_lock:
                current_generation = getattr(self, "_cache_generation", 0)
            if current_generation == generation:
                self.cache.put(cache_key, cache_value)
            state = self._page_state(pool, source, offset, page_size, debug)
            yield {
                "type": "done",
                "results": state["results"],
                "completed": len(completed),
                "total_sources": total_sources,
                "total": state["total"],
                "has_more": state["has_more"],
                "offset": offset,
                "limit": page_size,
                "counts": state["counts"],
            }
        finally:
            for future in futures:
                future.cancel()
            if acquired:
                self._search_slots.release()
            self._finish_inflight(inflight_key, pending, cache_value)

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
            return SourceResult(source=replace(source, available=False), error="source is not indexed", status="error")
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

        if title_query:
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
        if index.searcher and index.archive:
            # libzim's Searcher is not documented as thread-safe: serialise it.
            with index.lock:
                try:
                    paths = index.searcher.search(Query().set_query(fulltext_query or query)).getResults(0, count)
                    for rank, path in enumerate(paths):
                        try:
                            rowid = index.archive.get_entry_by_path(path)._index
                            rowids.add(rowid)
                            fulltext_rows.append((rank, rowid))
                        except (KeyError, RuntimeError, ValueError):
                            continue
                except (RuntimeError, ValueError) as error:
                    errors.append(f"full-text search unavailable: {error}")

        docs = self._fetch_docs(source, db, rowids)
        for score, rowid in semantic_ids:
            doc = docs.get(rowid)
            if doc:
                semantic.append((score, doc))
        keyword = [(score, docs[row["rowid"]]) for score, row in keyword if row["rowid"] in docs]
        if index.archive:
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

    def _fetch_docs(
        self,
        source: SourceInfo,
        db: sqlite3.Connection,
        rowids: set[int],
    ) -> dict[int, dict[str, Any]]:
        if not rowids:
            return {}
        rows: dict[int, tuple[Any, ...]] = {}
        values = list(rowids)
        for start in range(0, len(values), 900):
            batch = values[start:start + 900]
            placeholders = ",".join("?" for _ in batch)
            query = (
                "SELECT rowid, title, excerpt, path, target FROM docs "
                f"WHERE rowid IN ({placeholders})"
            )
            rows.update({int(row[0]): row for row in db.execute(query, batch)})

        target_ids = {int(row[4]) for row in rows.values() if row[4] is not None}
        for start in range(0, len(target_ids), 900):
            batch = list(target_ids)[start:start + 900]
            placeholders = ",".join("?" for _ in batch)
            query = (
                "SELECT rowid, title, excerpt, path, target FROM docs "
                f"WHERE rowid IN ({placeholders})"
            )
            rows.update({int(row[0]): row for row in db.execute(query, batch)})

        # kiwix_url is optional: without it results have no article link, so the
        # UI renders the title as plain text instead of building a broken URL.
        kiwix_url = str(self.cfg.get("kiwix_url") or "").strip().rstrip("/")
        docs: dict[int, dict[str, Any]] = {}
        for rowid in values:
            original = rows.get(rowid)
            if not original:
                continue
            target = int(original[4]) if original[4] is not None else rowid
            row = rows.get(target)
            if not row:
                continue
            _, title, excerpt, path, _ = row
            doc = {
                "id": f"{source.key}:{path}",
                "source_key": source.key,
                "source": source.name,
                "title": title,
                "lead": self._preview(excerpt or ""),
                "path": path,
                "mimetype": None,
                "url": (
                    f"/article/{quote(source.local_name or source.book, safe='')}/"
                    f"{quote(path, safe='/')}"
                    if source.mode == "local" and (source.local_name or source.book)
                    else (
                        f"{kiwix_url}/{quote(source.book)}/{quote(path)}"
                        if kiwix_url
                        else None
                    )
                ),
            }
            docs[rowid] = doc
        return docs

    def _populate_result_mimetypes(self, pool: list[dict[str, Any]]) -> None:
        """Read MIME metadata only for documents that survive global ranking."""
        if not pool:
            return
        with self._state_lock:
            sources = dict(self.sources)
            indexes = dict(self.indexes)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for doc in pool:
            source = sources.get(doc.get("source_key"))
            if source is None or source.mode != "local" or not source.local_name:
                continue
            index = indexes.get(source.local_name)
            if index is not None and index.archive is not None:
                grouped.setdefault(source.local_name, []).append(doc)

        for local_name, docs in grouped.items():
            index = indexes[local_name]
            try:
                self._acquire_index(index)
            except RuntimeError:
                continue
            try:
                with index.lock:
                    for doc in docs:
                        try:
                            item = index.archive.get_entry_by_path(doc["path"]).get_item()
                            doc["mimetype"] = (
                                item.mimetype or "application/octet-stream"
                            ).split(";", 1)[0].strip().lower()
                        except (KeyError, RuntimeError, ValueError):
                            continue
            finally:
                self._release_index(index)

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
        path = str(self.cfg.get("kiwix_search_path", "/kiwix/search"))
        endpoint = server + (path if path.startswith("/") else "/" + path)
        result_limit = min(
            max(self.candidate_count, limit),
            _int_config(
                self.cfg,
                "kiwix_result_limit",
                DEFAULT_KIWIX_RESULT_LIMIT,
            ),
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
            return SourceResult(source=source, error=f"Kiwix search failed: {error}", status="error")

        docs: list[dict[str, Any]] = []
        seen: set[str] = set()
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
            if link.startswith("/"):
                link = server + link
            docs.append({
                "id": f"{source.key}:{link or title}",
                "source_key": source.key,
                "source": source.name,
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
        semantic: list[tuple[float, str, dict[str, Any]]] = []
        keyword: list[tuple[float, str, dict[str, Any]]] = []
        fulltext: list[tuple[int, str, dict[str, Any]]] = []
        docs: dict[str, dict[str, Any]] = {}
        source_by_key = {result.source.key: result.source for result in source_results}

        for result in source_results:
            for doc in result.items:
                docs[doc["id"]] = doc
            for score, doc in result.semantic:
                semantic.append((score, result.source.key, doc))
            for score, doc in result.keyword:
                keyword.append((score, result.source.key, doc))
            for rank, doc in result.fulltext:
                fulltext.append((rank, result.source.key, doc))

        semantic.sort(key=lambda item: (-item[0], source_by_key[item[1]].name.casefold(), item[2]["path"]))
        keyword.sort(key=lambda item: (item[0], source_by_key[item[1]].name.casefold(), item[2]["path"]))
        fulltext.sort(key=lambda item: (item[0], source_by_key[item[1]].name.casefold(), item[2]["path"]))

        def deduplicate(ranked):
            seen: set[str] = set()
            unique = []
            for item in ranked:
                identity = item[2]["id"]
                if identity in seen:
                    continue
                seen.add(identity)
                unique.append(item)
            return unique

        semantic = deduplicate(semantic)
        keyword = deduplicate(keyword)
        fulltext = deduplicate(fulltext)

        scores: dict[str, float] = {}
        for ranked, weight in (
            (semantic, 1.0),
            (keyword, 1.0),
            (fulltext, 2.0 if len(re.findall(r"\w+", query, re.UNICODE)) >= _int_config(self.cfg, "long_query", 10) else 1.0),
        ):
            for rank, (_, _, doc) in enumerate(ranked[: max(self.candidate_count, limit)]):
                scores[doc["id"]] = scores.get(doc["id"], 0.0) + weight / (60 + rank)

        disambig_boost = _float_config(
            self.cfg, "disambiguation_boost", DEFAULT_DISAMBIGUATION_BOOST, -1.0, 1.0
        )
        disambig_penalty = _float_config(
            self.cfg, "disambiguation_penalty", DEFAULT_DISAMBIGUATION_PENALTY, 0.0, 1.0
        )
        scored: list[tuple[float, float, float, int, str, str, dict[str, Any]]] = []
        for identity, doc in docs.items():
            title_tokens = _content_terms(doc["title"])
            title_token_set = set(title_tokens)
            preview_tokens = _content_terms(doc.get("lead", ""))
            title_matches = sum(term in title_token_set for term in query_words)
            title_coverage = title_matches / len(query_words) if query_words else 0.0
            title_density = _density(query_words, title_tokens)
            # Coverage is primary; density rewards concise titles among equally
            # complete matches without letting a short partial match win.
            title_quality = title_coverage * (1.0 + title_density) / 2.0
            phrase_hit = _phrase(query_words, title_tokens)
            snippet_coverage = _coverage(query_words, preview_tokens)
            source = source_by_key.get(doc["source_key"])
            intent = _intent_match(source, query) if source is not None else 0.0
            lexical = (
                5.0 * title_quality
                + 2.0 * phrase_hit
                + intent
                + 0.5 * snippet_coverage
            ) / 8.5
            source_rank = int(doc.get("source_rank", 100000))
            total = scores.get(identity, 0.0) + 0.02 * lexical
            disambig = ""
            if DISAMBIGUATION_TITLE.search(doc["title"]):
                if _disambiguation_intent(query, query_words, doc["title"]):
                    total += disambig_boost
                    disambig = ", disambiguation exact"
                else:
                    total *= disambig_penalty
                    disambig = ", disambiguation demoted"
            explanation = (
                f"title {round(title_coverage * 100)}% "
                f"({title_matches}/{len(query_words) if query_words else 0}, "
                f"density {round(title_density * 100)}%), "
                f"phrase {'yes' if phrase_hit else 'no'}, "
                f"intent {'yes' if intent else 'no'}, "
                f"snippet {round(snippet_coverage * 100)}%"
                f"{disambig}"
            )
            output = dict(doc)
            output.update({
                "score": total,
                "explain": explanation,
                "rank": source_rank,
            })
            scored.append((
                total,
                lexical,
                title_quality,
                source_rank,
                doc["source"].casefold(),
                doc["path"],
                output,
            ))

        scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3], item[4], item[5]))
        return [item[-1] for item in scored[:limit]]
