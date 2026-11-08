"""One-source ZIM search, suggestions, previews, and enrichment."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ...content.reader import (
    DEFAULT_PARSE_TIMEOUT,
    read_entry,
    truncate_at_word_boundary,
)
from ...content.zim_paths import ZimPathError, resolve_zim_path
from ...diagnostics import public_cause
from ...contracts import SourceInfo, SourceResult, encode_document_id
from ...indexing.search import body_search, fuse_fulltext, vector_search
from ...searching.query import _title_prefix_query, _title_query
from ...searching.ranking import source_items

logger = logging.getLogger(__name__)

PREVIEW_HTML_BYTES = 32 << 10
DEFAULT_MIN_COSINE_SIMILARITY = 0.85
MAX_PHRASE_CANDIDATES = 32

_PHRASE_MIMETYPES = frozenset(
    {"text/plain", "text/html", "application/xhtml+xml"}
)
_HTML_ENTITY = re.compile(
    r"&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]+);"
)
_SCRIPT_STYLE_BLOCK = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?</\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_SCRIPT_STYLE_OPEN = re.compile(
    r"<\s*(?:script|style)\b", re.IGNORECASE
)


@dataclass
class ZimSearchContext:
    candidate_count: int = 16
    preview_chars: int = 1000
    max_html_bytes: int = 512 << 10
    max_article_bytes: int = 1 << 30
    max_entry_bytes: int | None = None
    nprobe: int | None = None
    min_cosine_similarity: float = DEFAULT_MIN_COSINE_SIMILARITY
    web_base_path: str = ""
    fulltext_pool: Any | None = None
    preview_cache: Any | None = None
    cfg: dict[str, Any] | None = None


def _source_title(source: SourceInfo) -> str:
    return source.title or source.key


def result_doc(
    source: SourceInfo, path: str, title: str | None, *, web_base_path: str
) -> dict[str, Any]:
    """One local result document shared by indexed and archive fallbacks."""
    return {
        "id": encode_document_id(source.key, path),
        "source_key": source.key,
        "source": _source_title(source),
        "source_icon": source.icon_url,
        "title": title or path,
        "lead": "",
        "path": path,
        "mimetype": None,
        "url": (
            f"{web_base_path}/content/"
            f"{quote(source.key, safe='')}/"
            f"{quote(path, safe='/')}"
        ),
    }


def _entry_excerpt(row: Any) -> str:
    if not row:
        return ""
    try:
        return getattr(row, "excerpt", row[2])
    except (AttributeError, IndexError, TypeError):
        return ""


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


def phrase_in_word_stream(text: str, phrase_words: list[str]) -> bool:
    """True when ``phrase_words`` appears as a contiguous run of words."""
    width = len(phrase_words)
    if width == 0:
        return True
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


def search(
    runtime: Any,
    source: SourceInfo,
    request: Any,
    ctx: ZimSearchContext,
) -> SourceResult:
    """Search one ZIM source through its local index."""
    if request.deadline is not None and time.monotonic() >= request.deadline:
        return SourceResult(
            source=source, error="search deadline expired", status="cancelled"
        )
    index = runtime.indexes.get(source.key)
    if index is None:
        return SourceResult(
            source=source,
            error="source is not indexed",
            status="error",
        )
    try:
        index.acquire()
    except RuntimeError as error:
        return SourceResult(source=source, error=str(error), status="cancelled")
    try:
        return _search_locked(source, index, request, ctx)
    finally:
        index.release()


def _search_locked(
    source: SourceInfo,
    index: Any,
    request: Any,
    ctx: ZimSearchContext,
) -> SourceResult:
    count = max(ctx.candidate_count, request.limit)
    db = index.db_for_thread()
    semantic: list[tuple[float, dict[str, Any]]] = []
    keyword: list[tuple[float, dict[str, Any]]] = []
    fulltext: list[tuple[int, dict[str, Any]]] = []
    rowids: set[int] = set()
    archive_entries: dict[int, Any] = {}
    fulltext_hits: list[tuple[int, dict[str, Any]]] = []
    errors: list[str] = []

    if request.query_vector is not None and index.faiss_index is not None:
        try:
            similarities, ids = vector_search(index, request.query_vector, count, ctx.nprobe)
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

    if request.title_query and db is not None:
        title_search = (
            "SELECT rowid, bm25(docs) FROM docs "
            "WHERE docs MATCH ? ORDER BY rank LIMIT ?"
        )
        title_rows = db.execute(
            title_search, (request.title_query, count)
        ).fetchall()
        if not title_rows:
            title_rows = db.execute(
                title_search, (_title_prefix_query(request.query), count)
            ).fetchall()
        for rowid, bm25 in title_rows:
            rowids.add(int(rowid))
            keyword.append((float(bm25), {"rowid": int(rowid)}))

    body_rows: list[tuple[int, float]] = []
    if db is not None and (request.fulltext_query or request.query):
        body_rows = body_search(
            db, request.fulltext_query or request.query, count
        )
        rowids.update(rowid for rowid, _ in body_rows)

    fulltext_rows: list[tuple[int, int]] = []
    searcher = None
    fulltext_pool = ctx.fulltext_pool
    helper_results = fulltext_pool is not None and callable(
        getattr(fulltext_pool, "search_results", None)
    )
    archive = None if helper_results else index.ensure_open()
    if helper_results:
        fulltext_capable = bool(
            index.zim_path and index.has_fulltext_index is not False
        )
    elif fulltext_pool is not None:
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
        return SourceResult(
            source=source,
            error=index.error or "ZIM archive is unavailable",
            status="error",
        )
    if fulltext_capable:
        paths: list[str] = []
        if helper_results:
            native_results = fulltext_pool.search_results(
                index.zim_path or "",
                request.fulltext_query or request.query,
                count,
                index.zim_signature,
            )
            if native_results is None:
                errors.append("full-text search unavailable")
            else:
                for rank, result in enumerate(native_results, 1):
                    if not isinstance(result, dict):
                        continue
                    path = str(result.get("path") or "")
                    if not path:
                        continue
                    fulltext_hits.append(
                        (
                            rank,
                            result_doc(
                                source,
                                path,
                                str(result.get("title") or path),
                                web_base_path=ctx.web_base_path,
                            ),
                        )
                    )
        elif fulltext_pool is not None:
            native_paths = fulltext_pool.search(
                index.zim_path or "",
                request.fulltext_query or request.query,
                count,
                index.zim_signature,
            )
            if native_paths is None:
                errors.append("full-text search unavailable")
            else:
                paths = native_paths
        elif searcher is not None:
            with index.lock:
                try:
                    search = searcher.search(
                        _query_object(request.fulltext_query or request.query)
                    )
                    paths = [
                        str(result.path)
                        for result in search.get_results(0, count)
                    ]
                except (RuntimeError, ValueError) as error:
                    errors.append(f"full-text search unavailable: {error}")
        if archive is not None:
            for rank, path in enumerate(paths, 1):
                try:
                    entry = archive.get_entry_by_path(path)
                    rowid = entry._index
                    rowids.add(rowid)
                    archive_entries[rowid] = entry
                    fulltext_rows.append((rank, rowid))
                except (KeyError, RuntimeError, ValueError):
                    continue

    docs = (
        fetch_docs(source, db, rowids, web_base_path=ctx.web_base_path)
        if db is not None
        else fetch_archive_docs(
            source,
            archive,
            archive_entries,
            web_base_path=ctx.web_base_path,
        )
    )
    keyword = [
        (score, docs[row["rowid"]])
        for score, row in keyword
        if row["rowid"] in docs
    ]
    body_fulltext = [
        (float(bm25), docs[rowid])
        for rowid, bm25 in body_rows
        if rowid in docs
    ]
    for rank, rowid in fulltext_rows:
        doc = docs.get(rowid)
        if doc:
            fulltext.append((rank, doc))
    fulltext.extend(fulltext_hits)
    fulltext = fuse_fulltext(body_fulltext, fulltext)
    if keyword or fulltext:
        minimum = ctx.min_cosine_similarity
        semantic_ids = [
            (score, rowid)
            for score, rowid in semantic_ids
            if score >= minimum
        ]
    for score, rowid in semantic_ids:
        doc = docs.get(rowid)
        if doc:
            semantic.append((score, doc))

    items = source_items(semantic, keyword, fulltext)
    return SourceResult(
        source=source,
        items=items,
        semantic=semantic,
        keyword=keyword,
        fulltext=fulltext,
        error="; ".join(errors) or None,
        status="partial" if errors else "ok",
    )


def _query_object(text: str) -> Any:
    from libzim.search import Query

    return Query().set_query(text)


def fetch_archive_docs(
    source: SourceInfo,
    archive: Any | None,
    entries: dict[int, Any],
    *,
    web_base_path: str,
) -> dict[int, dict[str, Any]]:
    if archive is None:
        return {}
    docs: dict[int, dict[str, Any]] = {}
    for rowid, entry in entries.items():
        try:
            target = resolve_zim_path(archive, entry.path)
        except ZimPathError:
            continue
        page_path = target.path
        title = getattr(target, "title", None) or page_path
        docs[rowid] = result_doc(
            source, page_path, title, web_base_path=web_base_path
        )
    return docs


def fetch_docs(
    source: SourceInfo,
    db: sqlite3.Connection,
    rowids: set[int],
    *,
    web_base_path: str,
) -> dict[int, dict[str, Any]]:
    if not rowids:
        return {}
    rows: dict[int, tuple[Any, ...]] = {}

    def fetch(ids: Any) -> None:
        values = list(ids)
        for start in range(0, len(values), 900):
            batch = values[start : start + 900]
            placeholders = ",".join("?" for _ in batch)
            query = (
                "SELECT rowid, title, path, target FROM docs "
                f"WHERE rowid IN ({placeholders})"
            )
            rows.update(
                {int(row[0]): row for row in db.execute(query, batch)}
            )

    values = list(rowids)
    fetch(values)
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
            if next_id not in rows:
                # A truncated manifest can omit the target; the redirect row's
                # stored path is already the final target path.
                return current_id, row
            current_id = next_id
        return None

    resolved_rows: dict[int, tuple[int, tuple[Any, ...]]] = {}
    path_ids: dict[str, set[int]] = {}
    for rowid in values:
        resolved = final_row(rowid)
        if not resolved:
            continue
        final_id, row = resolved
        resolved_rows[rowid] = (final_id, row)
        path_ids.setdefault(str(row[2]), set()).add(int(final_id))
    ambiguous = {
        path for path, identifiers in path_ids.items() if len(identifiers) > 1
    }
    docs: dict[int, dict[str, Any]] = {}
    for rowid, (_final_id, row) in resolved_rows.items():
        _, title, path, _ = row
        if str(path) in ambiguous:
            continue
        docs[rowid] = result_doc(
            source, str(path), title, web_base_path=web_base_path
        )
    return docs


def suggest(
    runtime: Any,
    source: SourceInfo,
    query: str,
    limit: int,
    ctx: ZimSearchContext,
    *,
    seen_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Title-prefix completion for one ZIM source."""
    if source.provider != "zim":
        return []
    index = runtime.indexes.get(source.key)
    if index is None:
        return []
    try:
        index.acquire()
    except RuntimeError:
        return []
    try:
        db = index.db_for_thread()
        if db is None:
            return []
        prefix_query = _title_prefix_query(query)
        exact_query = _title_query(query)
        remaining = max(1, limit)
        suggestions: list[dict[str, Any]] = []
        seen = seen_ids if seen_ids is not None else set()
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
                query_text = exact_query
                continue
            rowids = [int(row[0]) for row in rows]
            docs = fetch_docs(
                source, db, set(rowids), web_base_path=ctx.web_base_path
            )
            for rowid in rowids:
                doc = docs.get(rowid)
                if doc is None:
                    continue
                doc_id = str(doc.get("id") or "")
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                suggestions.append(doc)
            if (
                len(rows) < window
                or len(suggestions) >= remaining
                or window >= max_window
            ):
                break
            window = min(max_window, window * 2)
        return suggestions
    except sqlite3.Error as error:
        logger.warning(
            "zimantic: title suggestions unavailable for %s: %s",
            source.key,
            public_cause(error),
        )
        logger.debug("title suggestion database error for %s: %s", source.key, error)
        return []
    finally:
        index.release()


def preview(
    runtime: Any,
    source: SourceInfo,
    document: dict[str, Any],
    *,
    max_chars: int,
    max_bytes: int,
    deadline: float | None,
    ctx: ZimSearchContext,
) -> tuple[str, str | None]:
    """Extract a bounded preview for one result document."""
    path = str(document.get("path") or "")
    if not path or (deadline is not None and time.monotonic() >= deadline):
        return str(document.get("lead") or ""), document.get("mimetype")
    index = runtime.indexes.get(source.key)
    if index is None:
        return str(document.get("lead") or ""), document.get("mimetype")
    archive = index.ensure_open()
    if archive is None:
        return str(document.get("lead") or ""), document.get("mimetype")
    try:
        index.acquire()
    except RuntimeError:
        return str(document.get("lead") or ""), document.get("mimetype")
    try:
        with index.read_lock:
            try:
                entry = archive.get_entry_by_path(path)
                mimetype = document.get("mimetype")
                try:
                    item = entry.get_item()
                    entry_size = int(getattr(item, "size", 0) or 0)
                    if not mimetype:
                        mimetype = (
                            getattr(item, "mimetype", None)
                            or "application/octet-stream"
                        ).split(";", 1)[0].strip().lower()
                except (AttributeError, TypeError, ValueError):
                    entry_size = 0
                preview_bytes = min(ctx.max_html_bytes, max_bytes)
                row = read_entry(
                    archive,
                    entry._index,
                    max_html_bytes=preview_bytes,
                    max_entry_bytes=ctx.max_article_bytes,
                    max_preview_chars=max_chars,
                    timeout=DEFAULT_PARSE_TIMEOUT,
                )
                excerpt = _entry_excerpt(row)
                if (
                    not excerpt
                    and entry_size > preview_bytes
                    and preview_bytes < ctx.max_html_bytes
                ):
                    row = read_entry(
                        archive,
                        entry._index,
                        max_html_bytes=ctx.max_html_bytes,
                        max_entry_bytes=ctx.max_article_bytes,
                        max_preview_chars=max_chars,
                        timeout=DEFAULT_PARSE_TIMEOUT,
                    )
                    excerpt = _entry_excerpt(row)
            except (KeyError, OSError, RuntimeError, ValueError) as error:
                logger.warning(
                    "%s: preview unavailable for %s (%s)",
                    source.key,
                    path,
                    error,
                )
                return "", document.get("mimetype")
        lead = truncate_at_word_boundary(excerpt, ctx.preview_chars)
        if max_chars > 0:
            lead = lead[:max_chars]
        return lead, mimetype
    finally:
        index.release()


def enrich(
    runtime: Any,
    source: SourceInfo,
    result_docs: list[dict[str, Any]],
    source_docs: dict[str, dict[str, Any]],
    *,
    phrase_ids: set[str],
    phrase_words: list[str],
    max_html_bytes: int,
    max_article_bytes: int,
) -> bool:
    """Fill MIME types and check a content phrase for one source."""
    index = runtime.indexes.get(source.key)
    if index is None:
        return False
    archive = index.ensure_open()
    if archive is None:
        return False
    try:
        index.acquire()
    except RuntimeError:
        return False
    found = False
    try:
        with index.read_lock:
            for result in result_docs:
                is_phrase_candidate = result["id"] in phrase_ids
                if not is_phrase_candidate and result.get("mimetype"):
                    continue
                try:
                    item = archive.get_entry_by_path(result["path"]).get_item()
                    mimetype = (item.mimetype or "application/octet-stream").split(
                        ";", 1
                    )[0].strip().lower()
                except (KeyError, RuntimeError, ValueError):
                    continue
                source_doc = source_docs.get(result["id"])
                result["mimetype"] = mimetype
                if source_doc is not None:
                    source_doc["mimetype"] = mimetype
                if not is_phrase_candidate or mimetype not in _PHRASE_MIMETYPES:
                    continue
                if int(getattr(item, "size", 0) or 0) > max_article_bytes:
                    continue
                try:
                    raw = bytes(item.content[:max_html_bytes])
                except (KeyError, RuntimeError, TypeError, ValueError):
                    continue
                hit = phrase_in_word_stream(
                    raw.decode("utf-8", "ignore").casefold(), phrase_words
                )
                if source_doc is not None:
                    source_doc["_content_phrase"] = hit
                if hit:
                    found = True
    finally:
        index.release()
    return found


__all__ = [
    "PREVIEW_HTML_BYTES",
    "ZimSearchContext",
    "enrich",
    "fetch_archive_docs",
    "fetch_docs",
    "phrase_in_word_stream",
    "preview",
    "result_doc",
    "search",
    "suggest",
]
