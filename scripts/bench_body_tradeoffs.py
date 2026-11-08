"""Compare HEAD body FTS, compact postings, and ZIM-native-only search.

Example: uv run python scripts/bench_body_tradeoffs.py archive.zim --output /tmp/fts
Variants are process-local patches, never production schema/config changes.
Results and experimental indexes remain in --output, outside the library.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import random
import resource
import sqlite3
import statistics
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VARIANTS = ("head", "compact", "native-write-only", "native-only")
TOKENIZERS = {
    "unicode61": "unicode61",
    "ascii": "ascii",
    "porter": "porter unicode61 remove_diacritics 2",
    "trigram": "trigram",
}
ALL_VARIANTS = (
    *VARIANTS, "stored", "stored-default", "head-bm25", "stored-bm25", "trigram",
    *(f"compact-{tokenizer}" for tokenizer in TOKENIZERS if tokenizer != "trigram"),
    *(f"bm25-{tokenizer}" for tokenizer in TOKENIZERS),
)
# Keep the historical positional control independent of the production default.
BODY_SCHEMA = """CREATE VIRTUAL TABLE IF NOT EXISTS body USING fts5(
    text, content='', tokenize='unicode61 remove_diacritics 2');"""
COMPACT_BODY_SCHEMA = BODY_SCHEMA.replace(
    "content='',", "content='', detail=none, columnsize=0,"
)
QUERIES = [
    "moderation", "spam", "community", "conflict", "volunteers", "reputation",
    "moderator", "moderators", "troll", "trolling", "ban", "banning",
    "welcoming newcomers", "experienced members", "code of conduct",
    "handling disagreements", "encouraging participation", "community guidelines",
    "how do moderators handle spam?", "how can I grow an online community?",
    "what should we do about inactive members?", "the", "a", "zyxnonexistent",
]


def summary(values):
    ordered = sorted(values)
    return {
        "median": statistics.median(values),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "min": ordered[0],
        "max": ordered[-1],
    }


def compact_search(db, query, limit):
    from zimantic.searching.query import _query_words

    words = _query_words(query)
    if not words:
        return []
    expression = " AND ".join(f'"{word}"' for word in words)
    rows = db.execute(
        "SELECT rowid FROM body WHERE body MATCH ? LIMIT ?", (expression, limit)
    ).fetchall()
    return [(int(row[0]), float(rank)) for rank, row in enumerate(rows, 1)]


def bm25_search(db, query, limit):
    from zimantic.searching.query import _query_words

    words = _query_words(query)
    if not words:
        return []
    expression = " AND ".join(f'"{word}"' for word in words)
    return [
        (int(rowid), float(score))
        for rowid, score in db.execute(
            "SELECT rowid, bm25(body) AS score FROM body "
            "WHERE body MATCH ? ORDER BY score, rowid LIMIT ?",
            (expression, limit),
        )
    ]


def body_query(variant):
    from zimantic.indexing.search import body_search

    if variant.endswith("-bm25") or variant.startswith("bm25-"):
        return bm25_search
    if variant.startswith("compact") or variant == "trigram":
        return compact_search
    return body_search


@contextmanager
def variant_patches(variant, metrics=None):
    from zimantic.content import reader
    from zimantic.indexing import pipeline, schema
    from zimantic.providers.zim import search

    if BODY_SCHEMA not in schema.SCHEMA and COMPACT_BODY_SCHEMA not in schema.SCHEMA:
        raise RuntimeError("body schema changed; update the benchmark ablations")
    if variant not in ALL_VARIANTS:
        raise ValueError(f"unknown benchmark variant: {variant}")
    save = pipeline._save_rows
    finish = pipeline._finish_build
    rows_hash = hashlib.sha256()
    vectors_hash = hashlib.sha256()

    def no_body(*_args, **_kwargs):
        return "", False

    def save_rows(db, rows, next_entry, vectors, **kwargs):
        if metrics is not None:
            for row in rows:
                rows_hash.update(json.dumps(list(row[:5]), ensure_ascii=False).encode())
                if row.id in vectors:
                    vectors_hash.update(str(row.id).encode())
                    vectors_hash.update(vectors[row.id].astype("float16").tobytes())
        if variant.startswith("native"):
            rows = [row._replace(body="", body_truncated=False) for row in rows]
        if metrics is not None:
            metrics["body_docs"] += sum(bool(row.body) for row in rows)
        began = time.perf_counter()
        result = save(db, rows, next_entry, vectors, **kwargs)
        if metrics is not None:
            metrics["save_seconds"] += time.perf_counter() - began
        return result

    def finish_build(state, **kwargs):
        began = time.perf_counter()
        finish(state, **kwargs)
        if variant.startswith("native"):
            with state.db:
                schema.write_meta(state.db, {
                    schema.INDEXED_CHANNELS: schema.encode_channels(
                        (schema.CHANNEL_TITLE, schema.CHANNEL_SEMANTIC)
                    )
                })
                schema.delete_meta(state.db, [schema.BODY_TRUNCATED_COUNT])
        if metrics is not None:
            metrics["finish_seconds"] = time.perf_counter() - began
            metrics["rows_sha256"] = rows_hash.hexdigest()
            metrics["vectors_sha256"] = vectors_hash.hexdigest()

    with ExitStack() as stack:
        stack.enter_context(patch.object(
            schema, "SCHEMA", schema.SCHEMA.replace(COMPACT_BODY_SCHEMA, BODY_SCHEMA)
        ))
        if variant.startswith("compact"):
            compact = COMPACT_BODY_SCHEMA
            if variant != "compact":
                compact = compact.replace(
                    "unicode61 remove_diacritics 2", TOKENIZERS[variant.removeprefix("compact-")]
                )
            stack.enter_context(patch.object(schema, "SCHEMA", schema.SCHEMA.replace(
                BODY_SCHEMA, compact
            )))
        elif variant in ("stored", "stored-default", "stored-bm25"):
            stored = BODY_SCHEMA.replace("content='', ", "")
            if variant == "stored-default":
                stored = stored.replace(", tokenize='unicode61 remove_diacritics 2'", "")
            stack.enter_context(patch.object(schema, "SCHEMA", schema.SCHEMA.replace(
                BODY_SCHEMA, stored
            )))
        elif variant == "trigram" or variant.startswith("bm25-"):
            tokenizer = TOKENIZERS[
                variant.removeprefix("bm25-") if variant.startswith("bm25-") else "trigram"
            ]
            changed = BODY_SCHEMA.replace("unicode61 remove_diacritics 2", tokenizer)
            if variant == "trigram":
                changed = changed.replace("content='',", "content='', columnsize=0,")
            stack.enter_context(patch.object(
                schema, "SCHEMA", schema.SCHEMA.replace(BODY_SCHEMA, changed)
            ))
        elif variant.startswith("native"):
            stack.enter_context(patch.object(schema, "SCHEMA", schema.SCHEMA.replace(
                BODY_SCHEMA, ""
            )))
            stack.enter_context(patch.object(
                schema, "EXPECTED_TABLES", schema.EXPECTED_TABLES - {"body"}
            ))
            # Preserve excerpt/title parsing; only skip the separate HTML body pass.
            if variant == "native-only":
                stack.enter_context(patch.object(
                    reader, "extract_body", no_body
                ))
        stack.enter_context(patch.object(search, "body_search", body_query(variant)))
        stack.enter_context(patch.object(pipeline, "_save_rows", save_rows))
        stack.enter_context(patch.object(pipeline, "_finish_build", finish_build))
        yield


def source_info():
    from zimantic.contracts import SourceInfo

    return SourceInfo(key="benchmark", provider="zim", collection_key="benchmark")


def build(args):
    from zimantic.indexing import pipeline
    from zimantic.indexing.locator import IndexLocator
    from zimantic.providers.zim.entries import ArchiveEntryProducer

    metrics = {"save_seconds": 0.0, "body_docs": 0}
    embedder = None
    if args.embedding:
        from zimantic.embed import Embedder

        began = time.perf_counter()
        embedder = Embedder(args.model, threads=args.threads, device="cpu")
        metrics["model_load_seconds"] = time.perf_counter() - began
        embedder.embed(["passage: warm up"])
    locator = IndexLocator(args.output / "indexes")
    source = source_info()
    began = time.perf_counter()
    cpu_began = time.process_time()
    with variant_patches(args.variant, metrics):
        producer = ArchiveEntryProducer(source, args.zim)
        try:
            report = pipeline.build_index(producer, locator, embedder, {
                "batch_size": 32, "embedding_batch_tokens": 1000,
            })
        finally:
            producer.close()
    metrics.update(
        build_seconds=time.perf_counter() - began,
        cpu_seconds=time.process_time() - cpu_began,
        peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        report=report.to_dict(),
    )
    lease = locator.paths(source)
    try:
        metrics["sqlite_path"] = str(lease.sqlite_path)
        metrics["sqlite_bytes"] = lease.sqlite_path.stat().st_size
        metrics["faiss_bytes"] = (
            lease.faiss_path.stat().st_size
            if lease.faiss_path is not None and lease.faiss_path.is_file() else 0
        )
        with sqlite3.connect(lease.sqlite_path) as db:
            metrics["table_bytes"] = dict(db.execute(
                "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
            ))
            metrics["body_schema"] = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='body'"
            ).fetchone()
            metrics["docs"] = db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    finally:
        lease.close()
    return metrics


def requests(queries, limit):
    from zimantic.providers.base import SourceQuery
    from zimantic.searching.query import _fulltext_query, _title_query

    return [
        SourceQuery(q["query"], _title_query(q["query"]), _fulltext_query(q["query"]),
                    None, limit)
        for q in queries
    ]


def runtime(args):
    from libzim.reader import Archive
    from libzim.search import Query, Searcher

    from zimantic.fulltext import FulltextPool
    from zimantic.libzim_compat import search_paths
    from zimantic.providers.zim.search import ZimSearchContext, _search_locked
    from zimantic.searching.index import IndexHandle

    queries = json.loads(args.queries.read_text())
    reqs = requests(queries, args.limit)
    source = source_info()
    metrics = {}
    with variant_patches(args.variant):
        for helper in (False, True):
            pool = FulltextPool(workers=1) if helper else None
            handle = IndexHandle(
                db=sqlite3.connect(f"file:{args.database}?mode=ro", uri=True),
                faiss_index=None, zim_path=str(args.zim), has_fulltext_index=True,
            )
            ctx = ZimSearchContext(candidate_count=args.limit, fulltext_pool=pool)
            try:
                began = time.perf_counter()
                first = _search_locked(source, handle, reqs[0], ctx)
                first_ms = 1000 * (time.perf_counter() - began)
                if first.status != "ok":
                    raise RuntimeError(first.error)
                traces = []
                for query, request in zip(queries, reqs):
                    result = _search_locked(source, handle, request, ctx)
                    if result.status != "ok":
                        raise RuntimeError(result.error)
                    traces.append({
                        **query,
                        "fulltext": [doc["path"] for _, doc in result.fulltext],
                        "items": [doc["path"] for doc in result.items],
                    })
                timings = []
                order = list(range(len(reqs)))
                rng = random.Random(7)
                for _ in range(args.query_repeats):
                    rng.shuffle(order)
                    for index in order:
                        began = time.perf_counter()
                        _search_locked(source, handle, reqs[index], ctx)
                        timings.append(1000 * (time.perf_counter() - began))
                metrics["helper" if helper else "direct"] = {
                    "first_query_ms": first_ms, "warm_ms": summary(timings),
                    "queries": len(reqs), "calls": len(timings), "results": traces,
                }
            finally:
                handle.close()
                if pool:
                    pool.close()
        db = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
        try:
            archive = Archive(str(args.zim))
            searcher = Searcher(archive)
            body = body_query(args.variant)
            timings = {"body": [], "native": []}
            components = []
            for query, request in zip(queries, reqs):
                hits = (
                    body(db, request.fulltext_query, args.limit)
                    if not args.variant.startswith("native") else []
                )
                body_paths = []
                for rowid, _ in hits:
                    body_paths.append(db.execute(
                        "SELECT path FROM docs WHERE rowid=?", (rowid,)
                    ).fetchone()[0])
                paths = search_paths(
                    searcher.search(Query().set_query(request.fulltext_query)),
                    0, args.limit,
                )
                components.append({**query, "body": body_paths, "native": paths})
            for _ in range(args.query_repeats):
                for request in reqs:
                    if not args.variant.startswith("native"):
                        began = time.perf_counter()
                        body(db, request.fulltext_query, args.limit)
                        timings["body"].append(1000 * (time.perf_counter() - began))
                    began = time.perf_counter()
                    search_paths(
                        searcher.search(Query().set_query(request.fulltext_query)),
                        0, args.limit,
                    )
                    timings["native"].append(1000 * (time.perf_counter() - began))
            metrics["components"] = {
                key + "_ms": summary(values) for key, values in timings.items() if values
            }
            metrics["components"]["results"] = components
        finally:
            db.close()
    metrics["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    return metrics


def tokenizer_probes(variants):
    from zimantic.indexing import schema

    cases = [
        ("accent folding", "CAF\u00c9", "cafe", True),
        ("multiple Latin diacritics", "\u1ed9", "o", True),
        ("English inflection", "corrected", "correction", True),
        ("overstemming", "university", "universe", False),
        ("substring", "bookkeeping", "keep", True),
        ("two-character term", "AI", "AI", True),
        ("Unicode punctuation", "we\u2019re ready", "we're", True),
        ("Unicode word boundary", "cat\U0001f600dog", "cat dog", True),
    ]
    result = {}
    for variant in variants:
        db = sqlite3.connect(":memory:")
        try:
            with variant_patches(variant):
                schema.ensure_schema(db)
                if variant.startswith("native"):
                    result[variant] = {"body_index": False}
                    continue
                db.executemany(
                    "INSERT INTO body(rowid,text) VALUES (?,?)",
                    [(i, text) for i, (_, text, _, _) in enumerate(cases, 1)]
                    + [(100, "needle " + "filler " * 100),
                       (101, "needle needle needle"), (102, "red blue")],
                )
                matches = []
                for rowid, (name, _, query, desired) in enumerate(cases, 1):
                    hits = body_query(variant)(db, query, 100)
                    matches.append({
                        "case": name, "query": query, "desired": desired,
                        "matched": rowid in {identifier for identifier, _ in hits},
                    })
                capabilities = {}
                for name, sql, params in (
                    ("raw_content", "SELECT text FROM body WHERE body MATCH ?", ("needle",)),
                    ("snippet", "SELECT snippet(body,0,'[',']','...',5) FROM body "
                     "WHERE body MATCH ?", ("needle",)),
                    ("phrase", "SELECT rowid FROM body WHERE body MATCH ?", ('"red blue"',)),
                ):
                    try:
                        capabilities[name] = db.execute(sql, params).fetchall()
                    except sqlite3.Error as error:
                        capabilities[name] = {"error": str(error)}
                try:
                    capabilities["bm25_scores"] = bm25_search(db, "needle", 16)
                except sqlite3.Error as error:
                    capabilities["bm25_scores"] = {"error": str(error)}
                result[variant] = {"matches": matches, "capabilities": capabilities}
        finally:
            db.close()
    return result


def make_queries(zim, database):
    from zimantic.providers.zim.entries import ArchiveEntryProducer
    from zimantic.searching.query import _query_words

    queries = [{"query": query, "kind": "manual"} for query in QUERIES]
    with sqlite3.connect(database) as db:
        db.execute("CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main,body,row)")
        db.execute("CREATE VIRTUAL TABLE temp.instances USING fts5vocab(main,body,instance)")
        rare = {row[0] for row in db.execute(
            "SELECT term FROM vocab WHERE doc BETWEEN 1 AND 20 AND length(term)>=5"
        )}
        ids = [row[0] for row in db.execute("SELECT DISTINCT doc FROM instances ORDER BY doc")]
    random.Random(7).shuffle(ids)
    producer = ArchiveEntryProducer(source_info(), zim)
    try:
        for rowid in ids:
            row = producer.entry_at(rowid)
            if row is None or len(row.body) <= 1200:
                continue
            excluded = set(_query_words(row.title + " " + row.excerpt))
            terms = [
                term for term in _query_words(row.body[1000:])
                if term in rare and term not in excluded
            ]
            unique = list(dict.fromkeys(terms))
            if len(unique) < 2:
                continue
            queries.append({
                "query": " ".join(unique[:2]), "kind": "deep-body-probe",
                "target": row.path,
            })
            if len(queries) == len(QUERIES) + 24:
                break
    finally:
        producer.close()
    return queries


def compare(reference, candidate):
    before = reference["helper"]["results"]
    after = candidate["helper"]["results"]
    overlaps = []
    changed = 0
    lost_queries = []
    target_before = target_after = targets = 0
    for old, new in zip(before, after):
        a, b = old["fulltext"][:10], new["fulltext"][:10]
        union = set(a) | set(b)
        overlaps.append(len(set(a) & set(b)) / len(union) if union else 1.0)
        changed += old["items"][:10] != new["items"][:10]
        if a and not b:
            lost_queries.append(old["query"])
        if "target" in old:
            targets += 1
            target_before += old["target"] in old["fulltext"]
            target_after += old["target"] in new["fulltext"]
    return {
        "identical_fulltext_rankings": all(
            a["fulltext"] == b["fulltext"] for a, b in zip(before, after)
        ),
        "mean_fulltext_top10_jaccard": statistics.mean(overlaps),
        "changed_items_top10_queries": changed,
        "lost_fulltext_queries": lost_queries,
        "deep_body_targets": targets,
        "head_target_hits": target_before, "candidate_target_hits": target_after,
    }


def run_worker(args, action, variant, output, extra=()):
    command = [
        sys.executable, str(Path(__file__).resolve()), str(args.zim),
        "--worker", action, "--variant", variant, "--output", str(output),
        "--threads", str(args.threads), "--model", str(args.model),
        "--query-repeats", str(args.query_repeats), "--limit", str(args.limit),
        *extra,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        result.check_returncode()
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zim", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=Path("model"))
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--build-repeats", type=int, default=3)
    parser.add_argument("--full-repeats", type=int, default=2)
    parser.add_argument("--query-repeats", type=int, default=10)
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--worker", choices=("build", "runtime"))
    parser.add_argument("--variant", choices=ALL_VARIANTS, default="head")
    parser.add_argument("--variants", nargs="+", choices=ALL_VARIANTS,
                        help="variants to compare; first is the baseline")
    parser.add_argument("--embedding", action="store_true")
    parser.add_argument("--queries", type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    args.zim = args.zim.resolve()
    args.output = args.output.resolve()
    variants = tuple(args.variants or VARIANTS)
    if args.zim.stat().st_size >= 80_000_000:
        parser.error("use a ZIM smaller than 80 MB")
    if min(args.threads, args.build_repeats, args.query_repeats, args.limit) < 1:
        parser.error("counts must be positive")
    if args.full_repeats < 0:
        parser.error("full repeats cannot be negative")
    if args.worker == "runtime" and (args.queries is None or args.database is None):
        parser.error("runtime worker requires --queries and --database")
    cpus = sorted(os.sched_getaffinity(0))[:args.threads]
    os.sched_setaffinity(0, cpus)
    os.environ["OMP_NUM_THREADS"] = str(args.threads)
    if args.worker:
        if args.worker == "build":
            args.output.mkdir(parents=True, exist_ok=False)
        result = build(args) if args.worker == "build" else runtime(args)
        (args.output / f"{args.worker}.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
        return
    args.output.mkdir(parents=True, exist_ok=False)
    from libzim.reader import Archive

    archive = Archive(str(args.zim))
    if not archive.has_fulltext_index:
        parser.error("native-only comparison requires a ZIM with native full-text")
    result = {
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "zim": args.zim.name, "zim_bytes": args.zim.stat().st_size,
        "entries": archive.entry_count, "articles": archive.article_count,
        "affinity": cpus, "threads": args.threads,
        "baseline_variant": variants[0],
        "versions": {"sqlite": sqlite3.sqlite_version, **{
            name: importlib.metadata.version(name)
            for name in ("libzim", "onnxruntime", "faiss-cpu")
        }},
        "method": {
            "build": "fresh process/index each trial; warmed OS cache; one embedding worker",
            "runtime": "provider search + native FTS; no query embedding, previews or HTTP",
            "cold": "first query with unopened native handle; OS cache is not evicted",
            "quality": "overlap and known term-presence probes, not labelled relevance",
        },
        "builds": {}, "runtime": {}, "comparisons": {},
    }
    queries_path = args.queries.resolve() if args.queries else args.output / "queries.json"
    for trial in range(args.build_repeats):
        # Rotate ordering to reduce systematic warm-cache/host-load bias.
        position = trial % len(variants)
        order = variants[position:] + variants[:position]
        for variant in order:
            print(f"lexical build {trial + 1}: {variant}", file=sys.stderr, flush=True)
            output = args.output / f"lexical-{trial}-{variant}"
            data = run_worker(args, "build", variant, output)
            result["builds"].setdefault(variant, {"lexical": [], "full": []})[
                "lexical"
            ].append(data)
            if not queries_path.exists():
                queries_path.write_text(json.dumps(
                    make_queries(args.zim, data["sqlite_path"]), indent=2
                ))
            runtime_data = run_worker(args, "runtime", variant, output, (
                "--queries", str(queries_path), "--database", data["sqlite_path"],
            ))
            result["runtime"].setdefault(variant, []).append(runtime_data)
            (args.output / "results.json").write_text(json.dumps(result, indent=2))
    for trial in range(args.full_repeats):
        full_variants = tuple(v for v in variants if v != "native-write-only")
        if not full_variants:
            break
        position = trial % len(full_variants)
        order = full_variants[position:] + full_variants[:position]
        for variant in order:
            print(f"full build {trial + 1}: {variant}", file=sys.stderr, flush=True)
            data = run_worker(
                args, "build", variant, args.output / f"full-{trial}-{variant}",
                ("--embedding",),
            )
            result["builds"][variant]["full"].append(data)
            (args.output / "results.json").write_text(json.dumps(result, indent=2))
    baseline = variants[0]
    reference = result["runtime"][baseline][0]
    for variant in variants:
        builds = result["builds"][variant]
        runtimes = result["runtime"][variant]
        result["comparisons"][variant] = {
            **compare(reference, runtimes[0]),
            "identical_input_rows": all(
                row["rows_sha256"] == result["builds"][baseline]["lexical"][0]["rows_sha256"]
                for group in builds.values() for row in group
            ),
            "identical_vectors": all(
                row["vectors_sha256"] == result["builds"][baseline]["full"][0]["vectors_sha256"]
                for row in builds["full"]
            ) if builds["full"] else None,
            "lexical_seconds": summary([r["build_seconds"] for r in builds["lexical"]]),
            "full_seconds": summary([r["build_seconds"] for r in builds["full"]])
            if builds["full"] else None,
            "runtime_helper_median_ms": statistics.median(
                r["helper"]["warm_ms"]["median"] for r in runtimes
            ),
            "runtime_direct_median_ms": statistics.median(
                r["direct"]["warm_ms"]["median"] for r in runtimes
            ),
            "sqlite_bytes": builds["lexical"][0]["sqlite_bytes"],
        }
    (args.output / "results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result["comparisons"], indent=2))


if __name__ == "__main__":
    main()
