"""Generic local search over a provider-neutral :class:`IndexHandle`.

Inputs are one opened index handle, one ``SourceInfo``, one ``SourceQuery``,
configuration limits, a preview hook, and a runtime-supplied URL builder.  The
builder is the provider's path-verification seam: it must raise or return an
empty value when a path is ambiguous/unverifiable, and the core omits that hit
and reports the source as partial rather than opening a guessed row.

The module keeps the existing four ``SourceResult`` channels: semantic,
keyword, full-text, and items.  Body lexical hits and optional native full-text
hits are fused into the existing full-text channel by reciprocal rank; only
their separate candidate orders contribute, not comparable raw scores.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..contracts import SourceInfo, SourceResult, encode_document_id
from ..searching.query import _query_words, _title_prefix_query, _title_query
from ..searching.ranking import source_items
from .native import coerce_native_hits
from .types import NativeHit

RRF_K = 60
DEFAULT_MIN_COSINE_SIMILARITY = 0.85
logger = logging.getLogger(__name__)


class AmbiguousPathError(ValueError):
    """A path does not identify exactly one permitted final item."""


@dataclass
class SearchLimits:
    candidate_count: int = 16
    preview_chars: int = 1000
    max_html_bytes: int = 512 << 10
    max_article_bytes: int = 1 << 30
    min_cosine_similarity: float = DEFAULT_MIN_COSINE_SIMILARITY
    web_base_path: str = ""
    nprobe: int | None = None
    cfg: Mapping[str, Any] | None = None


def vector_search(index: Any, query: Any, count: int, nprobe: int | None):
    """Apply an IVF override without leaking it to another concurrent request."""
    vectors = index.faiss_index
    if nprobe is None or not hasattr(vectors, "nprobe"):
        return vectors.search(query, count)
    with getattr(index, "lock", nullcontext()):
        previous = vectors.nprobe
        vectors.nprobe = max(1, min(nprobe, getattr(vectors, "nlist", nprobe) or nprobe))
        try:
            return vectors.search(query, count)
        finally:
            vectors.nprobe = previous


def _source_title(source: SourceInfo) -> str:
    return source.title or source.key


def default_url_builder(web_base_path: str = "") -> Callable[[SourceInfo, str], str]:
    def build(source: SourceInfo, path: str) -> str:
        return (
            f"{web_base_path}/content/"
            f"{quote(source.key, safe='')}/"
            f"{quote(path, safe='/')}"
        )

    return build


def result_doc(
    source: SourceInfo,
    path: str,
    title: str | None,
    *,
    url_builder: Callable[[SourceInfo, str], str] | None = None,
    web_base_path: str = "",
) -> dict[str, Any]:
    """One local result document shared by indexed and native channels."""
    builder = url_builder or default_url_builder(web_base_path)
    return {
        "id": encode_document_id(source.key, path),
        "source_key": source.key,
        "source": _source_title(source),
        "source_icon": source.icon_url,
        "title": title or path,
        "lead": "",
        "path": path,
        "mimetype": None,
        "url": builder(source, path),
    }


def _resolve_final_row(
    rows: dict[int, tuple[Any, ...]], rowid: int
) -> tuple[int, tuple[Any, ...]] | None:
    current_id = rowid
    visited: set[int] = set()
    for _ in range(32):
        row = rows.get(current_id)
        if row is None:
            return None
        target = row[3]
        if target is None:
            return current_id, row
        if current_id in visited:
            return None
        visited.add(current_id)
        next_id = int(target)
        if next_id not in rows:
            # The final target may lie outside a capped manifest.  The redirect
            # row already carries its final target path, so keep that bounded
            # metadata instead of dropping the alias.
            return current_id, row
        current_id = next_id
    return None


def fetch_docs(
    source: SourceInfo,
    db: sqlite3.Connection,
    rowids: Iterable[int],
    *,
    url_builder: Callable[[SourceInfo, str], str] | None = None,
    web_base_path: str = "",
    ambiguous_out: list[str] | None = None,
) -> dict[int, dict[str, Any]]:
    """Fetch result documents and resolve redirect chains to final targets."""
    values = list(rowids)
    if not values:
        return {}
    rows: dict[int, tuple[Any, ...]] = {}

    def fetch(ids: Iterable[int]) -> None:
        batch = list(ids)
        for start in range(0, len(batch), 900):
            chunk = batch[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            query = (
                "SELECT rowid, title, path, target FROM docs "
                f"WHERE rowid IN ({placeholders})"
            )
            rows.update(
                {int(row[0]): row for row in db.execute(query, chunk)}
            )

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

    resolved_rows: dict[int, tuple[int, tuple[Any, ...]]] = {}
    path_ids: dict[str, set[int]] = {}
    for rowid in values:
        resolved = _resolve_final_row(rows, rowid)
        if resolved is None:
            continue
        final_id, row = resolved
        resolved_rows[rowid] = (final_id, row)
        path_ids.setdefault(str(row[2]), set()).add(int(final_id))
    ambiguous_paths = {
        path for path, identifiers in path_ids.items() if len(identifiers) > 1
    }
    docs: dict[int, dict[str, Any]] = {}
    for rowid, (final_id, row) in resolved_rows.items():
        path = str(row[2])
        if path in ambiguous_paths:
            if ambiguous_out is not None:
                ambiguous_out.append(
                    f"ambiguous path {path!r} identifies multiple final items"
                )
            continue
        _, title, _, _ = row
        docs[rowid] = result_doc(
            source,
            path,
            str(title),
            url_builder=url_builder,
            web_base_path=web_base_path,
        )
    return docs


def body_search(
    db: sqlite3.Connection, query: str, limit: int
) -> list[tuple[int, float]]:
    """Return ``(rowid, position)`` body matches in candidate order."""
    words = _query_words(query)
    if not words:
        return []
    expression = " AND ".join(f'"{word}"' for word in words)
    try:
        rows = db.execute(
            "SELECT rowid FROM body WHERE body MATCH ? LIMIT ?",
            (expression, max(1, int(limit))),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(int(row[0]), float(position)) for position, row in enumerate(rows, 1)]


def fuse_fulltext(
    body_hits: Sequence[tuple[float, dict[str, Any]]],
    native_hits: Sequence[tuple[float, dict[str, Any]]],
) -> list[tuple[int, dict[str, Any]]]:
    """Fuse body/native hits by reciprocal rank and assign 1-based ranks.

    Candidates deduplicate by document identity.  Body and native raw scores are
    not compared; only their separate rank positions contribute.
    """
    scores: dict[str, float] = {}
    docs: dict[str, dict[str, Any]] = {}
    order: dict[str, tuple[str, str]] = {}
    for ranked in (body_hits, native_hits):
        for rank, (_score, doc) in enumerate(ranked, 1):
            identity = str(doc.get("id") or "")
            path = str(doc.get("path") or "")
            if not identity:
                continue
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (RRF_K + rank)
            docs.setdefault(identity, doc)
            order.setdefault(identity, (str(doc.get("source_key") or ""), path))
    ranked_ids = sorted(
        scores,
        key=lambda identity: (-scores[identity], order[identity][0], order[identity][1]),
    )
    return [(rank, docs[identity]) for rank, identity in enumerate(ranked_ids, 1)]


def native_result_docs(
    source: SourceInfo,
    hits: Sequence[NativeHit | Mapping[str, Any]],
    *,
    url_builder: Callable[[SourceInfo, str], str] | None = None,
    web_base_path: str = "",
) -> tuple[list[tuple[int, dict[str, Any]]], list[str]]:
    """Convert native hits to local docs, omitting ambiguous/unverifiable paths."""
    docs: list[tuple[int, dict[str, Any]]] = []
    errors: list[str] = []
    builder = url_builder or default_url_builder(web_base_path)
    seen: set[str] = set()
    for position, hit in enumerate(coerce_native_hits(hits), 1):
        try:
            doc = result_doc(
                source,
                hit.path,
                hit.title or hit.path,
                url_builder=builder,
            )
        except AmbiguousPathError as error:
            errors.append(f"ambiguous native result path {hit.path!r}: {error}")
            continue
        except Exception as error:
            errors.append(f"unverifiable native result path {hit.path!r}: {error}")
            continue
        identity = str(doc["id"])
        if identity in seen:
            continue
        seen.add(identity)
        docs.append((hit.rank or position, doc))
    return docs, errors


def search(
    source: SourceInfo,
    index: Any,
    request: Any,
    *,
    limits: SearchLimits | None = None,
    url_builder: Callable[[SourceInfo, str], str] | None = None,
    preview_hook: Callable[..., tuple[str, str | None]] | None = None,
    native: Any | None = None,
) -> SourceResult:
    """Search one local source through an already-opened index handle."""
    limits = limits or SearchLimits()
    deadline = getattr(request, "deadline", None)
    if deadline is not None and time.monotonic() >= deadline:
        return SourceResult(
            source=source, error="search deadline expired", status="cancelled"
        )
    db = index.db_for_thread()
    count = max(limits.candidate_count, int(getattr(request, "limit", 10)))
    semantic: list[tuple[float, dict[str, Any]]] = []
    keyword: list[tuple[float, dict[str, Any]]] = []
    errors: list[str] = []
    rowids: set[int] = set()
    semantic_ids: list[tuple[float, int]] = []

    faiss_index = getattr(index, "faiss_index", None)
    query_vector = getattr(request, "query_vector", None)
    if query_vector is not None and faiss_index is not None:
        try:
            similarities, ids = vector_search(index, query_vector, count, limits.nprobe)
            semantic_ids = [
                (float(score), int(rowid))
                for score, rowid in zip(similarities[0], ids[0])
                if rowid >= 0
            ]
            rowids.update(rowid for _, rowid in semantic_ids)
        except Exception as error:
            errors.append(f"semantic search unavailable: {error}")

    title_query = str(getattr(request, "title_query", "") or "")
    if title_query and db is not None:
        title_search = (
            "SELECT rowid, bm25(docs) FROM docs "
            "WHERE docs MATCH ? ORDER BY rank LIMIT ?"
        )
        title_rows = db.execute(title_search, (title_query, count)).fetchall()
        if not title_rows:
            query_text = str(getattr(request, "query", "") or "")
            title_rows = db.execute(
                title_search, (_title_prefix_query(query_text), count)
            ).fetchall()
        for rowid, bm25 in title_rows:
            rowids.add(int(rowid))
            keyword.append((float(bm25), {"rowid": int(rowid)}))

    body_docs: list[tuple[float, dict[str, Any]]] = []
    body_rows: list[tuple[int, float]] = []
    query_text = str(
        getattr(request, "fulltext_query", "")
        or getattr(request, "query", "")
        or ""
    )
    if db is not None and query_text:
        body_rows = body_search(db, query_text, count)
        rowids.update(rowid for rowid, _ in body_rows)

    docs = (
        fetch_docs(
            source,
            db,
            rowids,
            url_builder=url_builder,
            web_base_path=limits.web_base_path,
            ambiguous_out=errors,
        )
        if db is not None
        else {}
    )
    keyword = [
        (score, docs[row["rowid"]])
        for score, row in keyword
        if row["rowid"] in docs
    ]
    for rank, (rowid, bm25) in enumerate(body_rows, 1):
        doc = docs.get(rowid)
        if doc:
            body_docs.append((float(bm25), doc))

    native_docs: list[tuple[float, dict[str, Any]]] = []
    native_searcher = native if native is not None else getattr(index, "native", None)
    if native_searcher is not None and query_text:
        try:
            hits = native_searcher.search(
                query_text, count, deadline=deadline
            )
        except Exception as error:
            errors.append(f"full-text search unavailable: {error}")
        else:
            converted, native_errors = native_result_docs(
                source,
                hits,
                url_builder=url_builder,
                web_base_path=limits.web_base_path,
            )
            errors.extend(native_errors)
            native_docs = [(float(rank), doc) for rank, doc in converted]

    fulltext = fuse_fulltext(body_docs, native_docs)
    if keyword or fulltext:
        minimum = limits.min_cosine_similarity
        semantic_ids = [
            (score, rowid) for score, rowid in semantic_ids if score >= minimum
        ]
    for score, rowid in semantic_ids:
        doc = docs.get(rowid)
        if doc:
            semantic.append((score, doc))

    if db is None and faiss_index is None and native_searcher is None:
        return SourceResult(
            source=source,
            error=getattr(index, "error", None) or "local index is unavailable",
            status="error",
        )

    items = source_items(semantic, keyword, fulltext)
    if preview_hook is not None:
        for doc in items:
            try:
                lead, mimetype = preview_hook(
                    source,
                    doc,
                    max_chars=limits.preview_chars,
                    max_bytes=limits.max_html_bytes,
                    deadline=deadline,
                )
            except Exception as error:
                logger.debug("preview unavailable for %s: %s", source.key, error)
                continue
            if lead:
                doc["lead"] = lead
            if mimetype and not doc.get("mimetype"):
                doc["mimetype"] = mimetype
    return SourceResult(
        source=source,
        items=items,
        semantic=semantic,
        keyword=keyword,
        fulltext=fulltext,
        error="; ".join(errors) or None,
        status="partial" if errors else "ok",
    )


def suggest(
    source: SourceInfo,
    index: Any,
    query: str,
    limit: int,
    *,
    url_builder: Callable[[SourceInfo, str], str] | None = None,
    web_base_path: str = "",
    seen_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Title-prefix completion over one local index handle."""
    if not query:
        return []
    db = index.db_for_thread()
    if db is None:
        return []
    prefix_query = _title_prefix_query(query)
    exact_query = _title_query(query)
    remaining = max(1, int(limit))
    suggestions: list[dict[str, Any]] = []
    seen = seen_ids if seen_ids is not None else set()
    query_text = prefix_query
    window = remaining
    max_window = max(remaining * 8, 64)
    while True:
        rows = db.execute(
            "SELECT rowid FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT ?",
            (query_text, window),
        ).fetchall()
        if not rows and query_text == prefix_query:
            query_text = exact_query
            continue
        rowids = [int(row[0]) for row in rows]
        docs = fetch_docs(
            source,
            db,
            rowids,
            url_builder=url_builder,
            web_base_path=web_base_path,
        )
        for rowid in rowids:
            doc = docs.get(rowid)
            if doc is None:
                continue
            doc_id = str(doc.get("id") or "")
            if not doc_id or doc_id in seen:
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
    return suggestions[:remaining]


def preview(
    source: SourceInfo,
    index: Any,
    document: Mapping[str, Any],
    *,
    max_chars: int,
    max_bytes: int,
    deadline: float | None,
    hook: Callable[..., tuple[str, str | None]] | None = None,
) -> tuple[str, str | None]:
    """Hydrate a bounded preview through the provider's preview hook."""
    lead = str(document.get("lead") or "")
    mimetype = document.get("mimetype")
    if hook is None or deadline is not None and time.monotonic() >= deadline:
        return lead, mimetype
    try:
        return hook(
            source,
            document,
            max_chars=max_chars,
            max_bytes=max_bytes,
            deadline=deadline,
        )
    except Exception:
        return lead, mimetype


__all__ = [
    "DEFAULT_MIN_COSINE_SIMILARITY",
    "RRF_K",
    "AmbiguousPathError",
    "SearchLimits",
    "body_search",
    "default_url_builder",
    "fetch_docs",
    "fuse_fulltext",
    "native_result_docs",
    "preview",
    "result_doc",
    "search",
    "suggest",
]
