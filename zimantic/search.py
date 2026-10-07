"""Hybrid search over local ZIM indexes and optional Kiwix sources."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, unquote, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import faiss
from libzim.reader import Archive
from libzim.search import Query, Searcher

from .cache import DEFAULT_CACHE_SIZE, QueryCache
from .contracts import SourceInfo, SourceResult


MAX_QUERY_LENGTH = 4096
DEFAULT_CANDIDATES = 16
DEFAULT_SOURCE_TIMEOUT = 12
DEFAULT_MAX_CONCURRENT_SEARCHES = 4
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


@dataclass
class _LocalIndex:
    db: sqlite3.Connection
    faiss_index: Any | None
    archive: Archive | None
    searcher: Searcher | None
    lock: threading.Lock
    db_path: str | None = None  # read-only sqlite URI, used for per-thread connections
    _thread: threading.local = field(default_factory=threading.local)
    _conns: list[sqlite3.Connection] = field(default_factory=list)

    @property
    def semantic(self) -> bool:
        """True when vectors are loaded and meaning search can run."""
        return self.faiss_index is not None


class SearchQueryError(ValueError):
    """The requested query cannot be processed."""


def _int_config(cfg: dict, key: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(cfg.get(key, default)))
    except (TypeError, ValueError):
        return default


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


def _keyword_query(query: str) -> str:
    words = re.findall(r"[^\W_]+", query.casefold(), re.UNICODE)
    kept = [word for word in words if word not in STOPWORDS]
    words = kept or words
    return " AND ".join(f'"{word.replace(chr(34), chr(34) * 2)}"' for word in words)


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
        # semantic=False is the "fast" mode: start without loading FAISS or the
        # model and serve title + ZIM full-text results only.
        self.semantic = bool(semantic)
        self.indexes: dict[str, _LocalIndex] = {}
        self.sources: dict[str, SourceInfo] = {}
        self.source_timeout = _int_config(cfg, "source_timeout", DEFAULT_SOURCE_TIMEOUT)
        self.candidate_count = _int_config(cfg, "candidate_count", DEFAULT_CANDIDATES)
        self.source_workers = _int_config(cfg, "search_workers", 4)
        self.max_concurrent_searches = _int_config(
            cfg, "max_concurrent_searches", DEFAULT_MAX_CONCURRENT_SEARCHES
        )
        self._search_slots = threading.BoundedSemaphore(self.max_concurrent_searches)
        self._executor = ThreadPoolExecutor(max_workers=self.source_workers, thread_name_prefix="zimantic-search")
        self.cache = QueryCache(_int_config(cfg, "cache_size", DEFAULT_CACHE_SIZE, minimum=0))
        self._load_indexes()
        self.refresh_sources()

    @staticmethod
    def _index_uri(db_path: Path) -> str:
        return db_path.resolve().as_uri() + "?mode=ro"

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

            faiss_index = None
            faiss_path = db_path.with_suffix(".faiss")
            if self.semantic and done_value == "1" and faiss_path.exists():
                try:
                    faiss_index = faiss.read_index(
                        str(faiss_path),
                        faiss.IO_FLAG_MMAP_IFC | faiss.IO_FLAG_READ_ONLY,
                    )
                    if hasattr(faiss_index, "nprobe"):
                        faiss_index.nprobe = _int_config(self.cfg, "nprobe", 64)
                except Exception as error:  # corrupt/unreadable vectors: keep going
                    print(f"{db_path.stem}: vectors unavailable ({error}); using title and full-text search")
                    faiss_index = None

            archive = None
            searcher = None
            zim_path = Path(self.cfg["zim_dir"]) / f"{db_path.stem}.zim"
            if zim_path.exists():
                try:
                    archive = Archive(str(zim_path))
                    searcher = Searcher(archive) if archive.has_fulltext_index else None
                except Exception as error:  # unreadable ZIM: title search still works
                    print(f"{db_path.stem}: ZIM unavailable ({error}); title search only")
                    archive = None
                    searcher = None
            return _LocalIndex(db, faiss_index, archive, searcher, threading.Lock(), db_path=uri)
        except sqlite3.Error:
            db.close()
            return None

    def _load_indexes(self) -> None:
        for db_path in sorted(Path(self.cfg["index_dir"]).glob("*.sqlite")):
            index = self._open_index(db_path)
            if index:
                self.indexes[db_path.stem] = index

    def reload(self) -> dict[str, Any]:
        """Rescan index_dir without restarting. Picks up new finished indexes,
        forgets removed ones, upgrades fast indexes that gained vectors, and
        refreshes the Kiwix catalog. Cheap enough for systemd.path to trigger."""
        found = {path.stem: path for path in sorted(Path(self.cfg["index_dir"]).glob("*.sqlite"))}
        removed = [name for name in self.indexes if name not in found]
        added = []
        upgraded = []

        for name in removed:
            self._close_index(self.indexes.pop(name))

        for name, db_path in found.items():
            current = self.indexes.get(name)
            if current is None:
                index = self._open_index(db_path)
                if index:
                    self.indexes[name] = index
                    added.append(name)
                continue
            if self.semantic and not current.semantic and db_path.with_suffix(".faiss").exists():
                fresh = self._open_index(db_path)
                if fresh and fresh.semantic:
                    self._close_index(current)
                    self.indexes[name] = fresh
                    upgraded.append(name)

        self.cache.clear()
        self.refresh_sources()
        return {
            "indexes": sorted(self.indexes),
            "added": sorted(added),
            "removed": sorted(removed),
            "upgraded": sorted(upgraded),
        }

    @staticmethod
    def _close_index(index: _LocalIndex) -> None:
        for conn in [index.db, *index._conns]:
            try:
                conn.close()
            except sqlite3.Error:
                pass

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

    def refresh_sources(self) -> list[dict[str, Any]]:
        """Refresh source metadata without making local indexes unavailable."""
        local: dict[str, SourceInfo] = {}
        for local_name in self.indexes:
            key = _zim_key(local_name)
            if key in local:
                key = local_name
            local[key] = SourceInfo(
                key=key,
                name=local_name,
                book=local_name,
                mode="local",
                local_name=local_name,
                intent_phrases=_intent_phrases(local_name),
            )

        merged = dict(local)
        server = str(self.cfg.get("kiwix_server") or "").strip().rstrip("/")
        if server:
            for entry in self._catalog_entries(server):
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

        self.sources = dict(sorted(merged.items(), key=lambda pair: pair[1].name.casefold()))
        # Availability or catalog may have changed, so cached answers can be stale.
        self.cache.clear()
        return self.source_dicts()

    def _catalog_entries(self, server: str) -> list[dict[str, str]]:
        path = str(self.cfg.get("kiwix_catalog_path", "/kiwix/catalog/v2/entries?count=-1"))
        url = server + (path if path.startswith("/") else "/" + path)
        try:
            request = Request(url, headers={"Accept": "application/atom+xml, application/xml"})
            with urlopen(request, timeout=self.source_timeout) as response:
                root = ET.fromstring(response.read())
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
        return [source.to_dict() for source in self.sources.values()]

    def local_names(self) -> list[str]:
        return list(self.indexes)

    def _selected_sources(self, zim: list[str] | None) -> list[SourceInfo]:
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
    def _cache_key(query: str, zim: list[str] | None, limit: int) -> tuple:
        selection = tuple(sorted(zim)) if zim else None
        normalized = re.sub(r"\s+", " ", query).strip().casefold()
        return (normalized, selection, int(limit))

    def _query_vector(self, sources: list[SourceInfo], query: str):
        """Embed the query only when a selected local index actually has vectors.
        A failed embedding degrades to title + full-text rather than failing."""
        if self.embedder is None or not self.semantic:
            return None
        wants_semantic = any(
            source.mode == "local"
            and source.local_name in self.indexes
            and self.indexes[source.local_name].semantic
            for source in sources
        )
        if not wants_semantic:
            return None
        try:
            return self.embedder.embed([f"query: {query}"])
        except Exception as error:
            print(f"zimantic: query embedding failed ({error}); using title and full-text search")
            return None

    def search(self, query: str, zim: list[str] | None = None, limit: int = 20) -> list[dict]:
        """Return the final result set using the same coordinator as streaming."""
        query = self._validate_query(query)
        limit = max(1, min(int(limit), 100))
        cached = self.cache.get(self._cache_key(query, zim, limit))
        if cached is not None:
            return list(cached)
        events = self.stream_search(query, zim, limit)
        final: list[dict] = []
        for event in events:
            if event["type"] == "error":
                raise SearchQueryError(event["error"])
            if event["type"] == "done":
                final = event["results"]
        return final

    def stream_search(
        self,
        query: str,
        zim: list[str] | None = None,
        limit: int = 20,
    ) -> Iterator[dict[str, Any]]:
        """Yield source progress and provisional ranked snapshots."""
        query = self._validate_query(query)
        limit = max(1, min(int(limit), 100))
        key = self._cache_key(query, zim, limit)
        sources = self._selected_sources(zim)

        cached = self.cache.get(key)
        if cached is not None:
            total = len(sources)
            yield {
                "type": "started",
                "query": query,
                "sources": [source.to_dict() for source in sources],
                "total": total,
            }
            yield {"type": "snapshot", "results": list(cached), "completed": total, "total": total}
            yield {"type": "done", "results": list(cached), "completed": total, "total": total}
            return

        acquired = self._search_slots.acquire()
        futures: list[Future[SourceResult]] = []
        completed: list[SourceResult] = []
        try:
            yield {
                "type": "started",
                "query": query,
                "sources": [source.to_dict() for source in sources],
                "total": len(sources),
            }
            if not sources:
                self.cache.put(key, [])
                yield {"type": "done", "results": [], "completed": 0, "total": 0}
                return

            query_vector = self._query_vector(sources, query)

            keyword_query = _keyword_query(query)
            for source in sources:
                futures.append(
                    self._executor.submit(
                        self._search_source,
                        source,
                        query,
                        keyword_query,
                        query_vector,
                        limit,
                    )
                )

            future_sources = dict(zip(futures, sources))
            for future in as_completed(futures):
                source = future_sources[future]
                try:
                    result = future.result()
                except Exception as error:
                    result = SourceResult(
                        source=source,
                        error=f"search failed: {error}",
                        status="error",
                    )
                completed.append(result)
                ranked = self._rank_results(completed, query, limit)
                yield {
                    "type": "source",
                    **result.to_event(),
                    "completed": len(completed),
                    "total": len(sources),
                }
                yield {
                    "type": "snapshot",
                    "results": ranked,
                    "completed": len(completed),
                    "total": len(sources),
                }

            final = self._rank_results(completed, query, limit)
            # A finished stream is cached for the regular JSON endpoint too.
            self.cache.put(key, final)
            yield {
                "type": "done",
                "results": final,
                "completed": len(completed),
                "total": len(sources),
            }
        finally:
            for future in futures:
                future.cancel()
            if acquired:
                self._search_slots.release()

    def _search_source(
        self,
        source: SourceInfo,
        query: str,
        keyword_query: str,
        query_vector,
        limit: int,
    ) -> SourceResult:
        if source.mode == "kiwix":
            return self._search_kiwix(source, keyword_query, limit)
        if not source.local_name or source.local_name not in self.indexes:
            return SourceResult(source=replace(source, available=False), error="source is not indexed", status="error")
        return self._search_local(source, query, keyword_query, query_vector, limit)

    def _search_local(
        self,
        source: SourceInfo,
        query: str,
        keyword_query: str,
        query_vector,
        limit: int,
    ) -> SourceResult:
        index = self.indexes[source.local_name]
        return self._search_local_locked(source, query, keyword_query, query_vector, limit, index)

    def _search_local_locked(
        self,
        source: SourceInfo,
        query: str,
        keyword_query: str,
        query_vector,
        limit: int,
        index: _LocalIndex,
    ) -> SourceResult:
        count = max(self.candidate_count, limit * 2)
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
                semantic_ids = [
                    (float(score), int(rowid))
                    for score, rowid in zip(similarities[0], ids[0])
                    if rowid >= 0
                ]
                rowids.update(rowid for _, rowid in semantic_ids)
            except Exception as error:
                errors.append(f"meaning search unavailable: {error}")
                semantic_ids = []
        else:
            semantic_ids = []

        if keyword_query:
            title_search = (
                "SELECT rowid, bm25(docs) FROM docs "
                "WHERE docs MATCH ? ORDER BY rank LIMIT ?"
            )
            for rowid, bm25 in db.execute(title_search, (keyword_query, count)):
                rowids.add(int(rowid))
                keyword.append((float(bm25), {"rowid": int(rowid)}))

        fulltext_paths: list[tuple[int, str]] = []
        if index.searcher and index.archive:
            # libzim's Searcher is not documented as thread-safe: serialise it.
            with index.lock:
                try:
                    paths = index.searcher.search(Query().set_query(keyword_query or query)).getResults(0, count)
                    fulltext_paths = [(rank, path) for rank, path in enumerate(paths)]
                    for _, path in fulltext_paths:
                        try:
                            rowids.add(index.archive.get_entry_by_path(path)._index)
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
            for rank, path in fulltext_paths:
                try:
                    rowid = index.archive.get_entry_by_path(path)._index
                except (KeyError, RuntimeError):
                    continue
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
                "SELECT rowid, title, lead, path, target FROM docs "
                f"WHERE rowid IN ({placeholders})"
            )
            rows.update({int(row[0]): row for row in db.execute(query, batch)})

        target_ids = {int(row[4]) for row in rows.values() if row[4] is not None}
        for start in range(0, len(target_ids), 900):
            batch = list(target_ids)[start:start + 900]
            placeholders = ",".join("?" for _ in batch)
            query = (
                "SELECT rowid, title, lead, path, target FROM docs "
                f"WHERE rowid IN ({placeholders})"
            )
            rows.update({int(row[0]): row for row in db.execute(query, batch)})

        docs: dict[int, dict[str, Any]] = {}
        for rowid in values:
            original = rows.get(rowid)
            if not original:
                continue
            target = int(original[4]) if original[4] is not None else rowid
            row = rows.get(target)
            if not row:
                continue
            _, title, lead, path, _ = row
            docs[rowid] = {
                "id": f"{source.key}:{path}",
                "source_key": source.key,
                "source": source.name,
                "title": title,
                "lead": lead or "",
                "path": path,
                "url": (
                    f"{self.cfg['kiwix_url'].rstrip('/')}/"
                    f"{quote(source.book)}/{quote(path)}"
                ),
            }
        return docs

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
        params = urlencode({
            "pattern": query,
            "books.name": source.book,
            "pageLength": max(self.candidate_count, limit * 2),
            "format": "xml",
        })
        try:
            request = Request(endpoint + "?" + params, headers={"Accept": "application/xml"})
            with urlopen(request, timeout=self.source_timeout) as response:
                root = ET.fromstring(response.read())
        except (HTTPError, URLError, TimeoutError, ValueError, ET.ParseError, OSError) as error:
            return SourceResult(source=source, error=f"Kiwix search failed: {error}", status="error")

        docs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in root.iter():
            if _local_name(item) not in {"item", "entry"}:
                continue
            title = _child_text(item, "title")
            link = _child_text(item, "link")
            snippet = re.sub(r"\s+", " ", _child_text(item, "description")).strip()
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
            for rank, (_, _, doc) in enumerate(ranked[: max(self.candidate_count, limit * 2)]):
                scores[doc["id"]] = scores.get(doc["id"], 0.0) + weight / (60 + rank)

        scored: list[tuple[float, float, float, int, str, str, dict[str, Any]]] = []
        for identity, doc in docs.items():
            title_tokens = _content_terms(doc["title"])
            lead_tokens = _content_terms(doc.get("lead", ""))
            title_matches = sum(term in set(title_tokens) for term in query_words)
            title_coverage = title_matches / len(query_words) if query_words else 0.0
            title_density = _density(query_words, title_tokens)
            # Coverage is primary; density rewards concise titles among equally
            # complete matches without letting a short partial match win.
            title_quality = title_coverage * (1.0 + title_density) / 2.0
            phrase_hit = _phrase(query_words, title_tokens)
            snippet_coverage = _coverage(query_words, lead_tokens)
            intent = max(
                (_intent_match(source_by_key[key], query) for key in {doc["source_key"]} if key in source_by_key),
                default=0.0,
            )
            lexical = (
                5.0 * title_quality
                + 2.0 * phrase_hit
                + intent
                + 0.5 * snippet_coverage
            ) / 8.5
            source_rank = int(doc.get("source_rank", 100000))
            total = scores.get(identity, 0.0) + 0.02 * lexical
            explanation = (
                f"title {round(title_coverage * 100)}% "
                f"({title_matches}/{len(query_words) if query_words else 0}, "
                f"density {round(title_density * 100)}%), "
                f"phrase {'yes' if phrase_hit else 'no'}, "
                f"intent {'yes' if intent else 'no'}, "
                f"snippet {round(snippet_coverage * 100)}%"
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
