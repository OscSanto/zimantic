"""Query-free browsing and result ordering using immutable local indexes."""

from __future__ import annotations

import hashlib
import math
import random
import sqlite3
import time
from typing import Any

from ..contracts import SourceInfo, SourceResult
from ..searching.query import STOPWORDS
from .search import SearchLimits, fetch_docs, vector_search

BROWSE_MODES = ("random", "rare", "outliers", "representative")
SEMANTIC_BROWSE_MODES = frozenset({"outliers", "representative"})


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("browse deadline expired")


def _random_rows(
    db: sqlite3.Connection, count: int, seed: str, deadline: float | None
) -> list[tuple[float, int]]:
    first = db.execute("SELECT rowid FROM docs ORDER BY rowid LIMIT 1").fetchone()
    if first is None:
        return []
    last = db.execute(
        "SELECT rowid FROM docs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0]
    rng = random.Random(seed)
    chosen: set[int] = set()
    width = last - first[0] + 1
    for _ in range(8):
        _check_deadline(deadline)
        probes = rng.sample(
            range(first[0], last + 1), min(width, max(64, count * 4))
        )
        for start in range(0, len(probes), 900):
            batch = probes[start : start + 900]
            placeholders = ",".join("?" for _ in batch)
            chosen.update(
                int(row[0])
                for row in db.execute(
                    f"SELECT rowid FROM docs WHERE rowid IN ({placeholders}) "
                    "AND target IS NULL",
                    batch,
                )
            )
        if len(chosen) >= count or len(probes) == width:
            break
    if len(chosen) < count and len(probes) < width:
        # Sparse native ids can make rejection sampling ineffective. Reservoir
        # sampling stays uniform without retaining the whole rowid set.
        reservoir: list[int] = []
        seen = 0
        for (rowid,) in db.execute(
            "SELECT rowid FROM docs WHERE target IS NULL ORDER BY rowid"
        ):
            if seen % 256 == 0:
                _check_deadline(deadline)
            if rowid in chosen:
                continue
            seen += 1
            if len(reservoir) < count - len(chosen):
                reservoir.append(int(rowid))
            else:
                position = rng.randrange(seen)
                if position < len(reservoir):
                    reservoir[position] = int(rowid)
        chosen.update(reservoir)
    rowids = rng.sample(sorted(chosen), min(count, len(chosen)))
    return [(1.0 / rank, rowid) for rank, rowid in enumerate(rowids, 1)]


def _rare_rows(
    db: sqlite3.Connection, index: Any, count: int, rowids: list[int] | None = None
) -> list[tuple[float, int]]:
    cached = index.browse_rankings.get("rare")
    if rowids is None and cached is not None and cached[0] >= count:
        return cached[1][:count]
    db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS temp.zimantic_browse_words "
        "USING fts5vocab(main, docs, row)"
    )
    db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS temp.zimantic_browse_terms "
        "USING fts5vocab(main, docs, instance)"
    )
    total = db.execute("SELECT count(*) FROM docs").fetchone()[0]
    db.create_function(
        "zimantic_browse_idf",
        1,
        lambda frequency: math.log((total + 1) / (frequency + 1)),
        deterministic=True,
    )
    stopwords = sorted(STOPWORDS)
    placeholders = ",".join("?" for _ in stopwords)
    selection = (
        " WHERE doc IN (" + ",".join("?" for _ in rowids) + ")"
        if rowids is not None else ""
    )
    word_selection = " AND term IN (SELECT term FROM terms)" if rowids is not None else ""
    rows = db.execute(
        "WITH weights AS MATERIALIZED ("
        " SELECT term, zimantic_browse_idf(doc) AS weight"
        " FROM temp.zimantic_browse_words"
        f" WHERE term NOT IN ({placeholders}){word_selection}"
        "), terms AS ("
        f" SELECT DISTINCT term, doc FROM temp.zimantic_browse_terms{selection}"
        ") SELECT terms.doc, round(avg(weights.weight), 12) AS score"
        " FROM terms JOIN weights USING (term)"
        " JOIN docs ON docs.rowid = terms.doc"
        " WHERE docs.target IS NULL"
        " GROUP BY terms.doc ORDER BY score DESC, terms.doc LIMIT ?",
        [*stopwords, *(rowids or []), count],
    ).fetchall()
    ranked = [(float(score), int(rowid)) for rowid, score in rows]
    if rowids is None:
        index.browse_rankings["rare"] = (count, ranked)
    return ranked


def _centroid(index: Any, deadline: float | None):
    if index.browse_centroid is not None:
        return index.browse_centroid
    import faiss
    import numpy as np

    vectors = index.faiss_index
    total = np.zeros(vectors.d, dtype=np.float64)
    if hasattr(vectors, "invlists"):
        vector = np.empty(vectors.d, dtype=np.float32)
        position = 0
        for list_no in range(vectors.nlist):
            for offset in range(vectors.invlists.list_size(list_no)):
                if position % 256 == 0:
                    _check_deadline(deadline)
                vectors.reconstruct_from_offset(
                    list_no, offset, faiss.swig_ptr(vector)
                )
                norm = np.linalg.norm(vector)
                if norm > 0:
                    total += vector / norm
                position += 1
    else:
        base = (
            faiss.downcast_index(vectors.index)
            if hasattr(vectors, "index")
            else vectors
        )
        for start in range(0, base.ntotal, 2048):
            _check_deadline(deadline)
            batch = base.reconstruct_n(start, min(2048, base.ntotal - start))
            norms = np.linalg.norm(batch, axis=1, keepdims=True)
            np.divide(batch, norms, out=batch, where=norms != 0)
            total += batch.sum(axis=0, dtype=np.float64)
    norm = np.linalg.norm(total)
    if norm == 0 or not np.isfinite(norm):
        raise ValueError("the index has no mean embedding direction")
    index.browse_centroid = (total / norm).astype(np.float32).reshape(1, -1)
    return index.browse_centroid


def sort_scores(
    index: Any, docs: list[dict[str, Any]], mode: str, deadline: float | None = None
) -> dict[str, float]:
    """Score only the matched documents, not a truncated browse sample."""
    db = index.db_for_thread()
    if db is None or mode in SEMANTIC_BROWSE_MODES and index.faiss_index is None:
        return {}
    acquired = (
        index.browse_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if deadline is not None else index.browse_lock.acquire()
    )
    if not acquired:
        raise TimeoutError("sort preparation is busy")
    try:
        _check_deadline(deadline)
        db.set_progress_handler(
            lambda: deadline is not None and time.monotonic() >= deadline, 1000
        )
        rows = {doc["_rowid"]: doc["path"] for doc in docs if "_rowid" in doc}
        paths = [doc["path"] for doc in docs if "_rowid" not in doc]
        for start in range(0, len(paths), 900):
            batch = paths[start : start + 900]
            placeholders = ",".join("?" for _ in batch)
            rows.update(db.execute(
                f"SELECT rowid, path FROM docs WHERE path IN ({placeholders}) "
                "AND target IS NULL", batch,
            ))
        if not rows:
            return {}
        if mode == "rare":
            ranked = _rare_rows(db, index, len(rows), list(rows))
        else:
            import faiss
            import numpy as np

            centroid = _centroid(index, deadline)
            query = -centroid if mode == "outliers" else centroid
            selector = faiss.IDSelectorBatch(np.array(list(rows), dtype=np.int64))
            vectors = index.faiss_index
            params = (
                faiss.SearchParametersIVF(sel=selector, nprobe=vectors.nlist)
                if hasattr(vectors, "nlist")
                else faiss.SearchParameters(sel=selector)
            )
            scores, ids = vectors.search(query, len(rows), params=params)
            ranked = [
                (float(score), int(rowid))
                for score, rowid in zip(scores[0], ids[0])
                if rowid in rows and math.isfinite(float(score))
            ]
        _check_deadline(deadline)
        scores_by_path: dict[str, float] = {}
        for score, rowid in ranked:
            path = rows[rowid]
            scores_by_path[path] = max(score, scores_by_path.get(path, -math.inf))
        return scores_by_path
    finally:
        db.set_progress_handler(None, 0)
        index.browse_lock.release()


def browse(
    source: SourceInfo,
    index: Any,
    request: Any,
    *,
    limits: SearchLimits,
    url_builder=None,
) -> SourceResult:
    mode = request.browse
    deadline = request.deadline
    db = index.db_for_thread()
    if db is None:
        return SourceResult(
            source=source, error="browsing requires a local index", status="error"
        )
    count = max(limits.candidate_count, request.limit)
    acquired = False
    try:
        _check_deadline(deadline)
        acquired = (
            index.browse_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
            if deadline is not None
            else index.browse_lock.acquire()
        )
        if not acquired:
            raise TimeoutError("browse preparation is busy")
        db.set_progress_handler(
            lambda: deadline is not None and time.monotonic() >= deadline, 1000
        )
        source_order = hashlib.blake2b(
            f"{request.seed}:{source.key}".encode(), digest_size=8
        ).hexdigest()
        if mode == "random":
            ranked = _random_rows(db, count, source_order, deadline)
        elif mode == "rare":
            ranked = _rare_rows(db, index, count)
        elif mode in SEMANTIC_BROWSE_MODES:
            if index.faiss_index is None:
                raise ValueError("semantic browsing requires loaded vectors")
            centroid = _centroid(index, deadline)
            _check_deadline(deadline)
            # Negating the mean finds the lowest average embedding similarity.
            query = -centroid if mode == "outliers" else centroid
            scores, rowids = vector_search(
                index,
                query,
                min(index.faiss_index.ntotal, max(count * 4, 64)),
                limits.nprobe,
            )
            ranked = [
                (float(score) + (1.0 if mode == "outliers" else 0.0), int(rowid))
                for score, rowid in zip(scores[0], rowids[0])
                if rowid >= 0 and math.isfinite(float(score))
            ]
        else:
            raise ValueError("unknown browse mode")
        errors: list[str] = []
        docs = fetch_docs(
            source,
            db,
            [rowid for _, rowid in ranked],
            url_builder=url_builder,
            web_base_path=limits.web_base_path,
            ambiguous_out=errors,
        )
        items = []
        seen: set[str] = set()
        for score, rowid in ranked:
            doc = docs.get(rowid)
            if doc is None or doc["id"] in seen:
                continue
            seen.add(doc["id"])
            rank = len(items) + 1
            doc.update(
                source_rank=rank,
                score=score,
                rank=rank,
                explain=f"Browse: {mode}",
                ranking={"browse": {"mode": mode, "score": score}},
                _browse_order=source_order,
            )
            items.append(doc)
            if len(items) >= count:
                break
        _check_deadline(deadline)
        return SourceResult(
            source=source,
            items=items,
            error="; ".join(errors) or None,
            status="partial" if errors else "ok",
        )
    except (TimeoutError, sqlite3.Error, ValueError, RuntimeError) as error:
        expired = deadline is not None and time.monotonic() >= deadline
        return SourceResult(
            source=source,
            error="browse deadline expired" if expired else str(error),
            status="cancelled" if expired or isinstance(error, TimeoutError) else "error",
        )
    finally:
        if acquired:
            db.set_progress_handler(None, 0)
            index.browse_lock.release()


def rank_browse_results(
    source_results: list[SourceResult], limit: int
) -> list[dict[str, Any]]:
    # Scores describe source-local statistics, not comparable global units.
    # Interleave source ranks so one large or unusual source cannot dominate.
    items = sorted(
        (doc for result in source_results for doc in result.items),
        key=lambda doc: (
            doc["source_rank"],
            doc["_browse_order"],
            doc["id"],
        ),
    )
    ranked = []
    seen: set[str] = set()
    for doc in items:
        if doc["id"] in seen:
            continue
        seen.add(doc["id"])
        output = {key: value for key, value in doc.items() if key != "_browse_order"}
        output["ranking"] = {
            **doc["ranking"],
            "final_score": doc["score"],
            "final_rank": len(ranked) + 1,
        }
        ranked.append(output)
        if len(ranked) >= limit:
            break
    return ranked
