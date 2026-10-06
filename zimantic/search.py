"""Hybrid search over local ZIM indexes and optional Kiwix sources."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
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

from .contracts import SourceInfo, SourceResult


MAX_QUERY_LENGTH = 4096
DEFAULT_CANDIDATES = 16
DEFAULT_SOURCE_TIMEOUT = 12
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
    faiss_index: Any
    archive: Archive | None
    searcher: Searcher | None
    lock: threading.Lock


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
    def __init__(self, cfg: dict, embedder):
        self.cfg = cfg
        self.embedder = embedder
        self.indexes: dict[str, _LocalIndex] = {}
        self.sources: dict[str, SourceInfo] = {}
        self.source_timeout = _int_config(cfg, "source_timeout", DEFAULT_SOURCE_TIMEOUT)
        self.candidate_count = _int_config(cfg, "candidate_count", DEFAULT_CANDIDATES)
        self.source_workers = _int_config(cfg, "search_workers", 4)
        self.max_concurrent_searches = _int_config(cfg, "max_concurrent_searches", 1)
        self._search_slots = threading.BoundedSemaphore(self.max_concurrent_searches)
        self._executor = ThreadPoolExecutor(max_workers=self.source_workers, thread_name_prefix="zimantic-search")
        self._load_indexes()
        self.refresh_sources()

    def _load_indexes(self) -> None:
        for db_path in sorted(Path(self.cfg["index_dir"]).glob("*.sqlite")):
            db = sqlite3.connect(
                db_path.resolve().as_uri() + "?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            done = db.execute("SELECT value FROM meta WHERE key = 'done'").fetchone()
            if not done or str(done[0]) != "1":
                db.close()
                continue

            faiss_path = db_path.with_suffix(".faiss")
            if not faiss_path.exists():
                db.close()
                raise RuntimeError(
                    f"{db_path}: completed index is missing {faiss_path}; delete both files and rebuild"
                )

            faiss_index = faiss.read_index(
                str(faiss_path),
                faiss.IO_FLAG_MMAP_IFC | faiss.IO_FLAG_READ_ONLY,
            )
            if hasattr(faiss_index, "nprobe"):
                faiss_index.nprobe = _int_config(self.cfg, "nprobe", 64)

            zim_path = Path(self.cfg["zim_dir"]) / f"{db_path.stem}.zim"
            archive = Archive(str(zim_path)) if zim_path.exists() else None
            searcher = Searcher(archive) if archive and archive.has_fulltext_index else None
            self.indexes[db_path.stem] = _LocalIndex(
                db, faiss_index, archive, searcher, threading.Lock()
            )

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

    def search(self, query: str, zim: list[str] | None = None, limit: int = 20) -> list[dict]:
        """Return the final result set using the same coordinator as streaming."""
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
        sources = self._selected_sources(zim)
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
                yield {"type": "done", "results": [], "completed": 0, "total": 0}
                return

            try:
                query_vector = self.embedder.embed([f"query: {query}"])
            except Exception as error:
                message = f"query embedding failed: {error}"
                yield {"type": "error", "error": message}
                yield {"type": "done", "results": [], "completed": 0, "total": len(sources)}
                return

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

            yield {
                "type": "done",
                "results": self._rank_results(completed, query, limit),
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
        with index.lock:
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
        semantic: list[tuple[float, dict[str, Any]]] = []
        keyword: list[tuple[float, dict[str, Any]]] = []
        fulltext: list[tuple[int, dict[str, Any]]] = []
        rowids: set[int] = set()
        errors: list[str] = []

        similarities, ids = index.faiss_index.search(query_vector, count)
        semantic_ids = [(float(score), int(rowid)) for score, rowid in zip(similarities[0], ids[0]) if rowid >= 0]
        rowids.update(rowid for _, rowid in semantic_ids)

        if keyword_query:
            title_search = (
                "SELECT rowid, bm25(docs) FROM docs "
                "WHERE docs MATCH ? ORDER BY rank LIMIT ?"
            )
            for rowid, bm25 in index.db.execute(title_search, (keyword_query, count)):
                rowids.add(int(rowid))
                keyword.append((float(bm25), {"rowid": int(rowid)}))

        fulltext_paths: list[tuple[int, str]] = []
        if index.searcher and index.archive:
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

        docs = self._fetch_docs(source, index.db, rowids)
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
            for score, doc in result.semantic:
                docs[doc["id"]] = doc
                semantic.append((score, result.source.key, doc))
            for score, doc in result.keyword:
                docs[doc["id"]] = doc
                keyword.append((score, result.source.key, doc))
            for rank, doc in result.fulltext:
                docs[doc["id"]] = doc
                fulltext.append((rank, result.source.key, doc))
            for doc in result.items:
                docs.setdefault(doc["id"], doc)

        semantic.sort(key=lambda item: (-item[0], source_by_key[item[1]].name.casefold(), item[2]["path"]))
        keyword.sort(key=lambda item: (item[0], source_by_key[item[1]].name.casefold(), item[2]["path"]))
        fulltext.sort(key=lambda item: (item[0], source_by_key[item[1]].name.casefold(), item[2]["path"]))

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
            title_coverage = _coverage(query_words, title_tokens)
            phrase_hit = _phrase(query_words, title_tokens)
            snippet_coverage = _coverage(query_words, lead_tokens)
            intent = max(
                (_intent_match(source_by_key[key], query) for key in {doc["source_key"]} if key in source_by_key),
                default=0.0,
            )
            lexical = (
                4.0 * title_coverage
                + 2.0 * phrase_hit
                + intent
                + 0.5 * snippet_coverage
            ) / 7.5
            source_rank = int(doc.get("source_rank", 100000))
            total = scores.get(identity, 0.0) + 0.01 * lexical
            explanation = (
                f"title {round(title_coverage * 100)}%, "
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
                _density(query_words, title_tokens),
                source_rank,
                doc["source"].casefold(),
                doc["path"],
                output,
            ))

        scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3], item[4], item[5]))
        return [item[-1] for item in scored[:limit]]
