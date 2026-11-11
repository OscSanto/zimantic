"""Benchmark a complete small-ZIM build and labelled semantic retrieval.

Example: uv run python scripts/bench_build.py archive.zim --output /tmp/before
Repeat with --queries queries.json --reference /tmp/before for quality comparisons.
Outputs stay outside the source/index library chosen by the normal CLI.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import resource
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


def rows_digest(path):
    digest = hashlib.sha256()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE VIRTUAL TABLE temp.terms USING fts5vocab(main, body, instance)"
        )
        for sql in (
            "SELECT rowid, title, path, target FROM docs ORDER BY rowid",
            "SELECT doc, term, col, offset FROM terms ORDER BY doc, term, col, offset",
        ):
            for row in db.execute(sql):
                digest.update(json.dumps(row, ensure_ascii=False).encode())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zim", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=Path("model"))
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--tokens", type=int, default=1000)
    parser.add_argument("--queries", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--order", choices=["id", "efficient"], default="efficient")
    parser.add_argument(
        "--fixed-batch",
        type=int,
        help="ablate the token budget with fixed-size sorted batches",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        help="benchmark an older checkout with this same harness",
    )
    args = parser.parse_args()
    if min(args.threads, args.workers, args.batch, args.tokens) < 1 or (
        args.fixed_batch is not None and args.fixed_batch < 1
    ):
        parser.error("threads, workers, batch size and token budget must be positive")
    sys.path.insert(0, str(args.source_root or Path(__file__).resolve().parent.parent))
    from zimantic.embed import Embedder
    from zimantic.indexing import pipeline
    from zimantic.indexing.locator import IndexLocator
    from zimantic.providers.zim.entries import ArchiveEntryProducer

    if args.zim.stat().st_size >= 80_000_000:
        parser.error("use a ZIM smaller than 80 MB")
    args.output.mkdir(parents=True, exist_ok=False)
    source = SimpleNamespace(
        key=args.zim.stem, provider="zim", collection_key="benchmark"
    )
    locator = IndexLocator(args.output / "indexes")
    producer = ArchiveEntryProducer(source, args.zim)
    if args.order == "id" and hasattr(producer, "prepare_iteration"):
        prepare = producer.prepare_iteration
        producer.prepare_iteration = lambda _order: prepare("id")
    began = time.perf_counter()
    embedder = Embedder(args.model, threads=args.threads, device="cpu")
    load_seconds = time.perf_counter() - began
    embedder.embed(["passage: warm up"])
    pool = None
    if args.workers >= 1 and hasattr(pipeline, "_embedding_batches"):
        pool = pipeline.EmbeddingPipeline(
            embedder,
            workers=args.workers,
            queue_size=args.workers * 2,
            token_budget=args.tokens,
        )
    elif args.workers > 1:
        pool = pipeline.EmbeddingPipeline(
            embedder, workers=args.workers, queue_size=args.workers * 2
        )
    original = pipeline._write_faiss
    batching = getattr(pipeline, "_embedding_batches", None)
    if args.fixed_batch and batching is None:
        parser.error("this checkout does not support the fixed-batch ablation")

    def fixed_batches(embedder, rows, _budget):
        result = []
        for ids, inputs, encoded in batching(embedder, rows, sys.maxsize):
            for i in range(0, len(ids), args.fixed_batch):
                result.append(
                    (
                        ids[i : i + args.fixed_batch],
                        inputs[i : i + args.fixed_batch],
                        encoded,
                    )
                )
        return result

    def capture(db, path):
        rows = db.execute("SELECT id, v FROM vecs ORDER BY id").fetchall()
        ids, vectors = pipeline._load(rows)
        np.savez(args.output / "vectors.npz", ids=ids, vectors=vectors)
        original(db, path)

    began = time.perf_counter()
    cpu_began = time.process_time()
    try:
        with (
            patch.object(pipeline, "_write_faiss", side_effect=capture),
            patch.object(
                pipeline,
                "_embedding_batches",
                fixed_batches if args.fixed_batch else batching,
                create=True,
            ),
        ):
            report = pipeline.build_index(
                producer,
                locator,
                embedder,
                {
                    "batch_size": args.batch,
                    "embedding_batch_tokens": args.tokens,
                    "embedding_pipeline": pool,
                },
            )
    finally:
        if pool:
            pool.close()
    build_seconds = time.perf_counter() - began
    cpu_seconds = time.process_time() - cpu_began
    lease = locator.paths(source)
    faiss_path = lease.faiss_path
    digest = rows_digest(lease.sqlite_path)
    with sqlite3.connect(lease.sqlite_path) as db:
        docs = db.execute(
            "SELECT rowid, title, path FROM docs ORDER BY rowid"
        ).fetchall()
    lease.close()
    (args.output / "docs.json").write_text(json.dumps(docs, indent=2))
    data = np.load(args.output / "vectors.npz")
    ids, vectors = data["ids"], data["vectors"].astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    result = {
        "zim": args.zim.name,
        "zim_bytes": args.zim.stat().st_size,
        "entries": producer.count,
        "vectors": len(ids),
        "load_seconds": load_seconds,
        "build_seconds": build_seconds,
        "cpu_seconds": cpu_seconds,
        "libzim": importlib.metadata.version("libzim"),
        "affinity": sorted(os.sched_getaffinity(0)),
        "articles_per_second": len(ids) / build_seconds,
        "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024)
        / 1_000_000,
        "rows_sha256": digest,
        "settings": {
            "threads": args.threads,
            "workers": args.workers,
            "batch": args.batch,
            "tokens": args.tokens,
            "order": args.order,
            "fixed_batch": args.fixed_batch,
        },
        "report": report.to_dict(),
    }
    if args.queries:
        import faiss

        queries = json.loads(args.queries.read_text())
        qvectors = embedder.embed(["query: " + q["query"] for q in queries])
        index = faiss.read_index(str(faiss_path))
        _, rankings = index.search(qvectors, 10)
        ranks = [
            next(
                (i + 1 for i, entry in enumerate(top) if int(entry) in q["relevant"]), 0
            )
            for q, top in zip(queries, rankings)
        ]
        result["quality"] = {
            "queries": len(queries),
            **{
                f"recall_at_{k}": sum(0 < r <= k for r in ranks) / len(ranks)
                for k in (1, 5, 10)
            },
            "mrr_at_10": sum(1 / r if r else 0 for r in ranks) / len(ranks),
        }
        np.savez(args.output / "queries.npz", vectors=qvectors, rankings=rankings)
    if args.reference:
        before = np.load(args.reference / "vectors.npz")
        if not np.array_equal(ids, before["ids"]):
            raise ValueError("before/after vector identities differ")
        bv = before["vectors"].astype(np.float32)
        bv /= np.linalg.norm(bv, axis=1, keepdims=True)
        cosines = np.sum(bv * vectors, axis=1)
        result["comparison"] = {
            "mean_vector_cosine": float(cosines.mean()),
            "min_vector_cosine": float(cosines.min()),
            "identical_rows_and_body_postings": digest
            == rows_digest(
                next((args.reference / "indexes").glob("**/generations/*/index.sqlite"))
            ),
        }
        if args.queries:
            old_index = faiss.read_index(
                str(
                    next(
                        (args.reference / "indexes").glob(
                            "**/generations/*/index.faiss"
                        )
                    )
                )
            )
            old_qvectors = np.load(args.reference / "queries.npz")["vectors"]
            _, old = old_index.search(old_qvectors, 10)
            result["comparison"].update(
                top1_agreement=float(np.mean(old[:, 0] == rankings[:, 0])),
                top10_overlap=float(
                    np.mean([len(set(a) & set(b)) / 10 for a, b in zip(old, rankings)])
                ),
            )
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
