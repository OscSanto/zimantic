"""Compare deferred previews with selective eager snippets and identical leads.

Uses production native-pool scheduling/IPC, ranking and bounded preview reads,
but no model, SQLite channels, phrase enrichment, HTTP or finalized-query cache.
Requires the GIL-releasing reader binding, including include_snippets=False.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.metadata
import json
import os
import platform
import queue
import re
import statistics
import subprocess
import sys
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import libzim
from libzim.reader import Archive
from libzim.search import Query, Searcher
from libzim.version import get_versions

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from run_native import QUERIES, cgroup_stats, constrained_trial

from zimantic.cache import QueryCache
from zimantic.contracts import SourceInfo, SourceResult
from zimantic.fulltext import FulltextPool, _Worker
from zimantic.fulltext_worker import MAX_CACHED_ARCHIVES
from zimantic.hardware import detect
from zimantic.libzim_compat import supports_threaded_search
from zimantic.providers.zim.search import ZimSearchContext, preview, result_doc
from zimantic.searching.index import IndexHandle
from zimantic.searching.ranking import rank_results, source_items

SOURCE = SourceInfo(key="benchmark", provider="zim")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def lead(index: IndexHandle, path: str, chars: int) -> str:
    return preview(
        SimpleNamespace(indexes={SOURCE.key: index}), SOURCE, {"path": path},
        max_chars=chars, max_bytes=32 << 10, deadline=None,
        ctx=ZimSearchContext(
            preview_chars=chars, max_html_bytes=4 << 20,
            max_article_bytes=32 << 20,
        ),
    )[0]


class SnippetCache:
    """Benchmark-only alternative to ArchiveCache; worker ownership is unchanged."""

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.archives: OrderedDict = OrderedDict()

    def clear(self) -> None:
        self.archives.clear()

    def search_results(self, path, query, count, version=None):
        if isinstance(version, list):
            version = tuple(version)
        key = (path, version)
        cached = self.archives.get(key)
        if cached is None:
            archive = Archive(path)
            cached = (archive, Searcher(archive), IndexHandle(None, None, archive=archive))
            self.archives[key] = cached
            while len(self.archives) > MAX_CACHED_ARCHIVES:
                self.archives.popitem(last=False)
        self.archives.move_to_end(key)
        archive, searcher, index = cached
        if not archive.has_fulltext_index or not query:
            return []
        search = searcher.search(Query().set_query(query))
        strategy, _, budget = self.spec["strategy"].partition(":")
        early = min(count, int(budget or 0))
        if strategy == "native" and early:
            native = list(search.get_results(0, early))
            if early < count:
                native.extend(search.get_results(early, count - early, include_snippets=False))
        else:
            native = list(search.get_results(0, count, include_snippets=False))
        results = []
        for rank, result in enumerate(native):
            text = ""
            if rank < early:
                if strategy == "native":
                    text = html.unescape(re.sub(r"</?b>", "", result.snippet))
                else:
                    text = lead(index, result.path, self.spec["preview_chars"])
            results.append({
                "path": result.path, "title": result.title, "lead": text,
                "prefetched": rank < early,
            })
        return results


class SnippetWorker(_Worker):
    def __init__(self, closed, spec):
        super().__init__(closed)
        self.spec = spec

    def _ensure_process(self):
        with self._process_lock:
            if self._closed.is_set():
                raise OSError("native full-text pool is closed")
            if self._process is None or self._process.poll() is not None:
                self._process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--worker", json.dumps(self.spec)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    text=True, bufsize=1,
                )
            return self._process


def make_pool(spec: dict) -> FulltextPool:
    pool = FulltextPool(spec["workers"], backend=spec["backend"])
    if spec["strategy"] == "deferred":
        return pool
    if spec["backend"] == "threads":
        for worker in pool._all:
            worker._cache = SnippetCache(spec)
    else:
        pool._all = [SnippetWorker(pool._closed, spec) for _ in pool._all]
        pool._idle = queue.Queue()
        for worker in pool._all:
            pool._idle.put(worker)
    return pool


def worker_main(spec: dict) -> None:
    cache = SnippetCache(spec)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            results = cache.search_results(
                request["path"], request["query"], request["count"], request.get("version"),
            )
            response = {"results": results}
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            response = {"error": f"{type(error).__name__}: {error}"}
        print(json.dumps(response), flush=True)


def probe(spec: dict) -> dict:
    resident = bytearray(spec["resident_mib"] << 20)
    for offset in range(0, len(resident), 4096):
        resident[offset] = 1
    paths = spec["paths"]
    sources = [SourceInfo(key=str(i), provider="zim", title=Path(p).name) for i, p in enumerate(paths)]
    indexes = [IndexHandle(None, None, zim_path=p) for p in paths]
    cache = QueryCache(4096)
    jobs = (list(QUERIES) * ((spec["requests"] + len(QUERIES) - 1) // len(QUERIES)))[:spec["requests"]]
    pool = make_pool(spec)

    def search_source(i, query):
        rows = pool.search_results(paths[i], query, spec["candidates"])
        if rows is None:
            raise RuntimeError(f"Native search failed: {Path(paths[i]).name}")
        docs = []
        for row in rows:
            doc = result_doc(sources[i], row["path"], row["title"], web_base_path="")
            doc["lead"] = row.get("lead", "")
            doc["_prefetched"] = row.get("prefetched", False)
            docs.append(doc)
        fulltext = list(enumerate(docs, 1))
        return SourceResult(
            source=sources[i], fulltext=fulltext, items=source_items([], [], fulltext),
        )

    try:
        with (
            ThreadPoolExecutor(spec["source_workers"]) as executor,
            ThreadPoolExecutor(spec["clients"]) as clients,
        ):
            def search(query):
                started = time.perf_counter()
                futures = [executor.submit(search_source, i, query) for i in range(len(paths))]
                results = [future.result() for future in futures]
                docs = rank_results(results, query, 100, spec["candidates"])[:spec["page_size"]]
                searched = time.perf_counter()
                grouped = {}
                cache_hits = early_used = 0
                for doc in docs:
                    key = (doc["source_key"], doc["path"])
                    cached = cache.get(key) if spec["cache"] == "hit" else None
                    if cached is not None:
                        doc["lead"] = cached
                        cache_hits += 1
                    elif doc["lead"]:
                        early_used += 1
                    else:
                        grouped.setdefault(int(doc["source_key"]), []).append(doc)

                def hydrate(i, group):
                    for doc in group:
                        doc["lead"] = lead(indexes[i], doc["path"], spec["preview_chars"])

                futures = [executor.submit(hydrate, i, group) for i, group in grouped.items()]
                for future in futures:
                    future.result()
                finished = time.perf_counter()
                candidates = [
                    (result.source.key, [(doc["path"], doc["title"]) for doc in result.items])
                    for result in results
                ]
                generated = sum(doc.get("_prefetched", False) for r in results for doc in r.items)
                return {
                    "total_ms": (finished - started) * 1000,
                    "search_ms": (searched - started) * 1000,
                    "hydrate_ms": (finished - searched) * 1000,
                    "identity": digest([candidates, [(doc["id"], doc["title"]) for doc in docs]]),
                    "leads": digest([doc["lead"] for doc in docs]),
                    "generated": generated, "early_used": early_used,
                    "fallbacks": sum(map(len, grouped.values())), "cache_hits": cache_hits,
                    "displayed": len(docs), "nonempty": sum(bool(doc["lead"]) for doc in docs),
                    "chars": sum(len(doc["lead"]) for doc in docs),
                    "docs": docs, "query": query,
                }

            started = time.perf_counter()
            warm = list(clients.map(search, jobs))
            if spec["cache"] == "hit":
                for result in warm:
                    for doc in result["docs"]:
                        key = (doc["source_key"], doc["path"])
                        if cache.get(key) is None:
                            cache.put(key, lead(indexes[int(doc["source_key"])], doc["path"], spec["preview_chars"]))
            warmup_seconds = time.perf_counter() - started
            before = cgroup_stats()
            started = time.perf_counter()
            measured = []
            reference = None
            while True:
                batch = list(clients.map(search, jobs))
                signatures = [(result["identity"], result["leads"]) for result in batch]
                if reference is None:
                    reference = signatures
                if signatures != reference:
                    raise RuntimeError("Result identity or preview changed between batches")
                measured.extend(batch)
                if time.perf_counter() - started >= spec["min_seconds"]:
                    break
            elapsed = time.perf_counter() - started
            after = cgroup_stats()
        cpu = after.get("cpu.stat", {}).get("usage_usec", 0) - before.get("cpu.stat", {}).get("usage_usec", 0)
        displayed = sum(r["displayed"] for r in measured)
        report = {
            "status": "ok", "detected_hardware": vars(detect()),
            "warmup_seconds": warmup_seconds, "elapsed_seconds": elapsed,
            "requests": len(measured), "requests_per_second": len(measured) / elapsed,
            "cpu_ms_per_request": cpu / 1000 / len(measured),
            "average_cpu_cores": cpu / 1_000_000 / elapsed,
            "result_digest": digest([value[0] for value in reference]),
            "lead_digest": digest([value[1] for value in reference]),
            "mean_preview_chars": sum(r["chars"] for r in measured) / max(1, displayed),
            "cgroup": after,
            "samples": [
                {"query": r["query"], "path": d["path"], "lead": d["lead"]}
                for r in warm[:3] for d in r["docs"][:2]
            ],
        }
        for name in ("total_ms", "search_ms", "hydrate_ms"):
            values = sorted(r[name] for r in measured)
            report[f"p50_{name}"] = statistics.median(values)
            report[f"p95_{name}"] = values[int(0.95 * (len(values) - 1))]
        for name in ("generated", "early_used", "fallbacks", "cache_hits", "displayed", "nonempty"):
            report[name] = sum(r[name] for r in measured)
        report["wasted_prefetches"] = report["generated"] - report["early_used"]
        if spec["cache"] == "hit" and report["cache_hits"] != displayed:
            raise RuntimeError("Warm-preview case did not hit every displayed preview")
        if after.get("memory.events", {}).get("oom_kill", 0):
            report["status"] = "oom-recovered"
        return report
    finally:
        pool.close()
        for index in indexes:
            index.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strategies", nargs="+", default=["deferred", "native:4", "native:16", "lead:4", "lead:16"])
    parser.add_argument("--cache", nargs="+", choices=("miss", "hit"), default=["miss", "hit"])
    parser.add_argument("--backends", nargs="+", choices=("processes", "threads"), default=["processes", "threads"])
    parser.add_argument("--limits", nargs="+", default=["4:2048"])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--source-workers", type=int, default=4)
    parser.add_argument("--clients", type=int, default=1)
    parser.add_argument("--requests", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--min-seconds", type=float, default=1)
    parser.add_argument("--resident-mib", type=int, default=512)
    parser.add_argument("--candidates", type=int, default=16)
    parser.add_argument("--page-size", type=int, default=10)
    parser.add_argument("--preview-chars", type=int, default=1000)
    parser.add_argument("--probe", help=argparse.SUPPRESS)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker_main(json.loads(args.worker))
        return
    if not supports_threaded_search():
        parser.error("requires a reader binding supporting include_snippets=False")
    if args.probe:
        print(json.dumps({"pid": os.getpid()}), flush=True)
        print(json.dumps(probe(json.loads(args.probe))), flush=True)
        return
    if not args.paths or args.output is None:
        parser.error("archive paths and --output are required")
    if min(args.workers, args.source_workers, args.clients, args.requests, args.repeats,
           args.candidates, args.page_size, args.preview_chars) < 1:
        parser.error("worker, client, request, repeat and size counts must be positive")
    if args.resident_mib < 0 or not 0 <= args.min_seconds <= 30:
        parser.error("resident memory must be nonnegative and min-seconds between 0 and 30")
    if any(not re.fullmatch(r"deferred|(?:native|lead):[1-9]\d*", s) for s in args.strategies):
        parser.error("strategies must be deferred, native:N or lead:N")
    try:
        limits = [tuple(map(int, value.split(":"))) for value in args.limits]
        if any(len(pair) != 2 or min(pair) < 1 for pair in limits):
            raise ValueError
    except ValueError:
        parser.error("limits must be positive CPU-quota:RAM-MiB pairs")
    paths = [str(path.resolve(strict=True)) for path in args.paths]
    report = {
        "date": time.strftime("%Y-%m-%d"), "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        ).strip(),
        "python": platform.python_version(), "platform": platform.platform(),
        "binding_path": libzim.__file__, "libzim_distribution": importlib.metadata.version("libzim"),
        "native_versions": dict(get_versions()), "queries": QUERIES,
        "archives": [{"name": Path(p).name, "bytes": Path(p).stat().st_size} for p in paths],
        "options": {k: v for k, v in vars(args).items() if k not in {"paths", "output", "probe", "worker"}},
        "samples": {}, "rows": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repetition in range(args.repeats):
        cases = [
            (cpus, ram, backend, strategy, cache_mode)
            for cpus, ram in limits for backend in args.backends
            for strategy in args.strategies for cache_mode in args.cache
        ]
        if repetition % 2:
            cases.reverse()
        for cpus, ram, backend, strategy, cache_mode in cases:
            spec = {
                **report["options"], "paths": paths,
                "backend": backend, "strategy": strategy, "cache": cache_mode,
            }
            row = constrained_trial(spec, cpus, ram, probe_script=Path(__file__).resolve())
            samples = row.pop("samples", [])
            if cache_mode == "miss" and samples:
                report["samples"].setdefault(strategy, samples)
            row.update(
                repetition=repetition, backend=backend, strategy=strategy, cache=cache_mode,
            )
            report["rows"].append(row)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(
                f"{repetition} cpu={cpus} {backend:9} {strategy:10} cache={cache_mode:4} "
                f"{row['status']:12} {row.get('requests_per_second', 0):7.1f} query/s "
                f"p50={row.get('p50_total_ms', 0):7.2f}ms "
                f"PSS={row['peak_tree_pss_mib']:.1f}MiB", flush=True,
            )
    valid = [row for row in report["rows"] if row["status"] == "ok"]
    if len({row["result_digest"] for row in valid}) > 1:
        raise RuntimeError("Strategies returned different candidates or ranked identities")
    equivalent = [r for r in valid if r["strategy"].startswith(("deferred", "lead:")) or r["cache"] == "hit"]
    if len({row["lead_digest"] for row in equivalent}) > 1:
        raise RuntimeError("Equivalent lead-preview strategies returned different text")
    if len(valid) != len(report["rows"]):
        raise RuntimeError("One or more trials failed; see report")
    print(f"Saved {len(valid)} trials; identities and equivalent lead previews agree.")


if __name__ == "__main__":
    main()
