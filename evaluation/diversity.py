"""Compare browsing and matched-pool diversity on isolated, reusable ZIM indexes.

Run --phase baseline before changing ranking, then --phase lexical and semantic
with the same --output. Raw rankings, metrics, corpus fingerprints and timings
are saved separately for all four variants; no configured library is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import faiss
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimantic.contracts import SourceInfo, encode_document_id
from zimantic.embed import Embedder
from zimantic.indexing.browse import browse
from zimantic.indexing.locator import IndexLocator
from zimantic.indexing.pipeline import build_index
from zimantic.indexing.search import SearchLimits, search
from zimantic.providers.base import IndexedRuntime, SourceQuery
from zimantic.providers.config import RuntimeConfig
from zimantic.providers.zim.entries import ArchiveEntryProducer
from zimantic.search import Search
from zimantic.searching.index import IndexHandle
from zimantic.searching.query import (
    STOPWORDS,
    USER_PAGE_PATH,
    _fulltext_query,
    _title_query,
)


class BenchmarkRuntime(IndexedRuntime):
    def __init__(self, indexes):
        self.indexes = indexes

    def bind_search(self, **kwargs):
        pass

    def start_faiss(self):
        pass

    def random_source(self):
        raise NotImplementedError

    def health(self):
        return []


def normalized(vectors):
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms > 0)


def all_vectors(index):
    if hasattr(index, "invlists"):
        ids, vectors = [], []
        vector = np.empty(index.d, dtype=np.float32)
        for list_no in range(index.nlist):
            size = index.invlists.list_size(list_no)
            pointer = index.invlists.get_ids(list_no)
            try:
                rowids = faiss.rev_swig_ptr(pointer, size).copy()
            finally:
                index.invlists.release_ids(list_no, pointer)
            for offset, rowid in enumerate(rowids):
                index.reconstruct_from_offset(list_no, offset, faiss.swig_ptr(vector))
                ids.append(int(rowid))
                vectors.append(vector.copy())
        return np.array(ids), normalized(np.array(vectors))
    base = faiss.downcast_index(index.index)
    return faiss.vector_to_array(index.id_map), normalized(base.reconstruct_n(0, base.ntotal))


def quality(items, vectors, reference, centroids, *, pool=None, k=20):
    """Coverage is a proxy, not labelled relevance or a universal quality score."""
    top = items[:k]
    valid = [doc for doc in top if doc["id"] in vectors]
    matrix = np.array([vectors[doc["id"]] for doc in valid])
    metrics = {"items": len(top), "vector_fraction": len(valid) / max(1, len(top))}
    if len(matrix):
        similarities = matrix @ matrix.T
        upper = similarities[np.triu_indices(len(matrix), 1)]
        np.fill_diagonal(similarities, -np.inf)
        metrics.update(
            semantic_distance=float(1 - upper.mean()) if len(upper) else None,
            semantic_redundancy=float(similarities.max(axis=1).mean()) if len(matrix) > 1 else None,
            centrality=float(np.mean([
                vectors[doc["id"]] @ centroids[doc["source_key"]] for doc in valid
            ])),
        )
        selected = {doc["id"] for doc in top}
        coverage = []
        for source_reference in reference.values():
            heldout = np.array([vector for identity, vector in source_reference if identity not in selected])
            if len(heldout):
                coverage.append(float((heldout @ matrix.T).max(axis=1).mean()))
        metrics["coverage"] = statistics.mean(coverage) if coverage else None
    words = [set(re.findall(r"\w+", doc["title"].casefold())) - STOPWORDS for doc in top]
    overlaps = [
        max(
            (len(left & right) / max(1, len(left | right)) for j, right in enumerate(words) if i != j),
            default=0.0,
        )
        for i, left in enumerate(words)
    ]
    counts = Counter(doc["source_key"] for doc in top)
    metrics.update(
        lexical_redundancy=statistics.mean(overlaps) if overlaps else None,
        empty_title_fraction=sum(not terms for terms in words) / max(1, len(top)),
        source_counts=dict(counts),
        source_entropy=(
            -sum((n / len(top)) * math.log(n / len(top)) for n in counts.values())
            / math.log(len(reference)) if len(reference) > 1 and top else None
        ),
    )
    if pool is not None:
        original_rank = {doc["id"]: rank for rank, doc in enumerate(pool, 1)}
        metrics["mean_relevance_rank"] = statistics.mean(original_rank[doc["id"]] for doc in top) if top else None
        metrics["relevance_top20_retention"] = len(
            {doc["id"] for doc in top} & {doc["id"] for doc in pool[:k]}
        ) / max(1, min(k, len(pool)))
    return metrics


def run(args):
    code_hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "zimantic/indexing/browse.py", "zimantic/search.py",
            "zimantic/searching/diversity.py",
        )
        if (ROOT / name).exists()
    }
    faiss.omp_set_num_threads(args.threads)
    args.output.mkdir(parents=True, exist_ok=True)
    cfg = {
        "batch_size": 32, "embedding_batch_tokens": 1000,
        "candidate_count": 16, "max_results": 100, "preview_limit": 0,
        "cache_size": 0, "source_query_threads": 1,
    }
    embedder = Embedder(args.model, threads=args.threads, device="cpu")
    print(f"Model: {embedder.fingerprint}", flush=True)
    locator = IndexLocator(args.output / "indexes")
    sources, indexes, vectors, reference, centroids, metadata = [], {}, {}, {}, {}, []
    for path in args.zims:
        source = SourceInfo(
            key="zim:diversity:" + path.stem, provider="zim",
            collection_key="zim:diversity", title=path.stem, searchable=True,
        )
        producer = ArchiveEntryProducer(source, path)
        try:
            report = build_index(producer, locator, embedder, cfg)
        finally:
            producer.close()
        lease = locator.paths(source)
        db = sqlite3.connect(f"{lease.sqlite_path.as_uri()}?mode=ro", uri=True)
        index = IndexHandle(db, faiss.read_index(str(lease.faiss_path)), lease=lease)
        rowids, embeddings = all_vectors(index.faiss_index)
        paths = dict(db.execute("SELECT rowid, path FROM docs WHERE target IS NULL"))
        source_vectors = [
            (encode_document_id(source.key, paths[int(rowid)]), vector)
            for rowid, vector in zip(rowids, embeddings) if int(rowid) in paths
        ]
        vectors.update(source_vectors)
        rng = np.random.default_rng(42)
        reference[source.key] = [
            source_vectors[i] for i in rng.choice(
                len(source_vectors), min(256, len(source_vectors)), replace=False
            )
        ]
        centroids[source.key] = normalized(embeddings.mean(axis=0, keepdims=True))[0]
        indexes[source.key] = index
        sources.append(source)
        metadata.append({
            "name": path.name, "bytes": path.stat().st_size,
            "vectors": len(source_vectors), "report": report.to_dict(),
            "manifest": json.loads((lease.sqlite_path.parent.parent.parent / "current").read_text()),
        })
        print(f"{path.name}: {len(source_vectors)} vectors ({report.status})", flush=True)
    engine = Search(RuntimeConfig(settings=cfg), embedder=embedder)
    engine.sources = {source.key: source for source in sources}
    engine.runtimes = {"zim:diversity": BenchmarkRuntime(indexes)}
    cases = [{"name": "browse/all", "query": "", "sources": [s.key for s in sources]}]
    cases.extend({"name": "browse/" + s.title, "query": "", "sources": [s.key]} for s in sources)
    cases.extend(
        {"name": "query/" + query, "query": query, "sources": [s.key for s in sources]}
        for query in ("community", "online community", "moderation", "water", "beer", "linux", "software", "installation")
    )
    pools_path = args.output / "pools.json"
    if pools_path.exists():
        pools = json.loads(pools_path.read_text())
    else:
        if args.phase != "baseline":
            raise ValueError("capture --phase baseline before comparing changes")
        pools = {}
        for case in cases:
            if not case["query"]:
                continue
            query = case["query"]
            query_vector = embedder.embed(["query: " + query])
            results = [
                search(
                    source, indexes[source.key],
                    SourceQuery(
                        query, _title_query(query), _fulltext_query(query), query_vector,
                        engine._source_limit(len(sources), source),
                    ),
                    limits=SearchLimits(candidate_count=16),
                )
                for source in sources
            ]
            if any(result.status != "ok" for result in results):
                raise RuntimeError([result.error for result in results])
            pools[case["name"]] = engine._rank_results(results, query, 100)
        pools_path.write_text(json.dumps(pools, ensure_ascii=False))
    modes = {
        "baseline": [("diverse-initial", "diverse"), ("representative-initial", "representative")],
        "lexical": [("diverse-corrected", "diverse")],
        "semantic": [("semantic-diversity", "semantic-diverse")],
    }[args.phase]
    output = {
        "meta": {
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "model": embedder.fingerprint, "sources": metadata, "k": args.k,
            "settings": cfg,
            "reference_seed": 42, "reference_per_source": 256, "threads": args.threads,
            "code_sha256": code_hashes,
            "pools_sha256": hashlib.sha256(pools_path.read_bytes()).hexdigest(),
            "timing": "ranking plus browse retrieval, excluding model loading, query retrieval, previews and HTTP",
        },
        "variants": {},
    }
    try:
        for label, mode in modes:
            output["variants"][label] = {}
            for case in cases:
                scope = [s for s in sources if s.key in case["sources"]]
                times = []
                query = case["query"]
                query_vector = embedder.embed(["query: " + query]) if query else None
                for _ in range(3):
                    started = time.perf_counter()
                    if query:
                        kwargs = {"query_vector": query_vector} if mode == "semantic-diverse" else {}
                        items = engine._sort_results(pools[case["name"]], mode, 0, None, **kwargs)
                    else:
                        results = [
                            browse(
                                source, indexes[source.key],
                                SourceQuery("", "", "", None, engine._source_limit(len(scope), source), browse=mode),
                                limits=SearchLimits(candidate_count=16),
                            )
                            for source in scope
                        ]
                        if any(result.status != "ok" for result in results):
                            raise RuntimeError([result.error for result in results])
                        items = engine._rank_results(results, "", 100)
                    times.append((time.perf_counter() - started) * 1000)
                scoped_reference = {s.key: reference[s.key] for s in scope}
                metrics = quality(
                    items, vectors, scoped_reference, centroids,
                    pool=pools[case["name"]] if query else None, k=args.k,
                )
                record = {
                    "metrics": metrics, "first_ms": times[0], "warm_ms": statistics.median(times[1:]),
                    "items": [
                        {key: doc[key] for key in ("id", "source_key", "path", "title")}
                        for doc in items[:args.k]
                    ],
                }
                output["variants"][label][case["name"]] = record
                print(label, case["name"], json.dumps(metrics, ensure_ascii=False), flush=True)
    finally:
        for index in indexes.values():
            index.close()
        engine.close()
    (args.output / (args.phase + ".json")).write_text(json.dumps(output, ensure_ascii=False, indent=2))
    summarize(args.output)
    qualitative_samples(args.output, {source.key: path for source, path in zip(sources, args.zims)})


def summarize(output):
    runs = [
        json.loads(path.read_text())
        for phase in ("baseline", "lexical", "semantic")
        if (path := output / (phase + ".json")).exists()
    ]
    identity = lambda run: (
        run["meta"]["model"], run["meta"]["pools_sha256"], run["meta"]["k"],
        [(source["name"], source["manifest"]) for source in run["meta"]["sources"]],
    )
    if any(identity(run) != identity(runs[0]) for run in runs[1:]):
        raise ValueError("comparison requires identical models, indexes, query pools and k")
    summary = {}
    for run in runs:
        for label, cases in run["variants"].items():
            summary[label] = {}
            for kind in ("browse", "query"):
                selected = [case for name, case in cases.items() if name.startswith(kind + "/")]
                keys = set().union(*(case["metrics"] for case in selected))
                means = {}
                for key in sorted(keys):
                    values = [case["metrics"].get(key) for case in selected]
                    values = [value for value in values if isinstance(value, (float, int))]
                    if values:
                        means[key] = statistics.mean(values)
                means["median_warm_ms"] = statistics.median(case["warm_ms"] for case in selected)
                summary[label][kind] = means
    (output / "comparison.json").write_text(json.dumps(summary, indent=2))


def qualitative_samples(output, paths):
    from libzim.reader import Archive

    from zimantic.content.reader import read_entry

    archives, excerpts, samples = {}, {}, {}
    for phase in ("baseline", "lexical", "semantic"):
        result = output / (phase + ".json")
        if not result.exists():
            continue
        run = json.loads(result.read_text())
        for label, cases in run["variants"].items():
            samples[label] = {}
            for name in ("browse/all", "query/community", "query/linux", "query/beer"):
                items = cases[name]["items"]
                top = []
                for doc in items[:5]:
                    identity, source = doc["id"], doc["source_key"]
                    if identity not in excerpts:
                        if source not in archives:
                            archives[source] = Archive(str(paths[source]))
                        archive = archives[source]
                        entry = archive.get_entry_by_path(doc["path"])
                        row = read_entry(
                            archive, entry._index, max_preview_chars=300,
                            max_html_bytes=512 << 10, max_entry_bytes=4 << 20,
                            max_body_text_bytes=1024,
                        )
                        excerpts[identity] = row.excerpt if row is not None else ""
                    top.append({**doc, "excerpt": excerpts[identity]})
                samples[label][name] = {
                    "profile_pages": sum(bool(USER_PAGE_PATH.search(doc["path"])) for doc in items),
                    "extra_title_copies": len(items) - len({doc["title"].casefold() for doc in items}),
                    "top5": top,
                }
    (output / "qualitative.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zims", nargs="+", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=("baseline", "lexical", "semantic"), required=True)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--k", type=int, default=20)
    args = parser.parse_args()
    if args.threads < 1 or not 1 <= args.k <= 100:
        parser.error("--threads must be positive and --k must be between 1 and 100")
    run(args)


if __name__ == "__main__":
    main()
