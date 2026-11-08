"""Evaluate body-index variants through the existing HTTP evaluation runner.

Example: uv run python scripts/bench_body_quality.py archive.zim --output /tmp/body-quality
Creates fixed English query files, isolated production servers/indexes, and
evaluation-compatible results. No configured library or system cache is changed.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.bench_body_tradeoffs import ALL_VARIANTS, tokenizer_probes, variant_patches

VARIANTS = ("head", "compact", "native-only")


def prepare_queries(zim, output):
    from libzim.reader import Archive

    from evaluation import make_queries, queries
    from zimantic.libzim_compat import get_entry_by_id
    from zimantic.searching.query import _query_words

    archive = Archive(str(zim))
    labelled = json.loads((ROOT / "tests/benchmarks/community_queries.json").read_text())
    if zim.name != "communitybuilding.stackexchange.com_en_all_2026-08.zim":
        raise ValueError("the fixed labelled suite requires the Community Building ZIM")
    rows = []
    for index, query in enumerate(labelled):
        entries = [get_entry_by_id(archive, rowid) for rowid in query["relevant"]]
        answers = [entry.path for entry in entries]
        title = entries[0].title
        words = title.split()
        longest = max(range(len(words)), key=lambda i: len(words[i]))
        word = words[longest]
        middle = len(word) // 2
        words[longest] = word[:middle] + word[middle + 1:]
        cases = (
            ("question", query["query"], "hand"),
            ("exact title", title, "generated"),
            ("keywords", " ".join(_query_words(title)[:3]), "generated"),
            ("misspelling", " ".join(words), "generated"),
        )
        for kind, text, source in cases:
            rows.append({
                "id": f"body-quality/{kind}/{index:03d}",
                "zim": zim.stem, "suite": "body-quality", "set": kind,
                "lang": "en", "query": text, "answers": answers, "source": source,
            })
    queries.write(output / zim.stem / "body-quality.en.jsonl", rows)
    make_queries.generate(SimpleNamespace(
        zim=str(zim), n=20, seed=0, attempts=400,
        sets=["first sentence", "deep in article"], suite="body-generated",
        lang="en", queries=str(output),
    ))
    loaded, _ = queries.load(output)
    for query in loaded:
        for answer in query["answers"]:
            entry = archive.get_entry_by_path(answer)
            if entry.is_redirect or not entry.get_item().mimetype.startswith("text/html"):
                raise ValueError(f"invalid answer: {answer}")
    return len(loaded)


def serve(args):
    import uvicorn
    from fastapi import FastAPI

    from zimantic.embed import Embedder
    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index
    from zimantic.providers.config import RuntimeConfig, ZimConfig
    from zimantic.providers.zim.entries import ArchiveEntryProducer
    from zimantic.search import Search
    from zimantic.settings import DESKTOP
    from zimantic.web.app import create_app

    cfg = {
        **DESKTOP, "zim_dir": str(args.output.parent / "zims"),
        "index_dir": str(args.output / "indexes"), "model_dir": str(args.model),
        "embed_device": "cpu", "embed_threads": args.threads,
        "build_embed_threads": args.threads, "embedding_workers": 1,
        "cache_size": 0, "cache_bytes": 0,
    }
    metrics = {"save_seconds": 0.0, "body_docs": 0}
    collections = (ZimConfig(name="library", path=cfg["zim_dir"]),)
    with variant_patches(args.variant, metrics):
        embedder = Embedder(args.model, threads=args.threads, device="cpu")
        embedder.embed(["passage: warm up"])
        search = Search(
            RuntimeConfig(settings=cfg, collections=collections),
            embedder=embedder, semantic=True,
        )
        lexical = None
        try:
            sources = [
                source for runtime in search.runtimes.values()
                for source in runtime.library_sources()
            ]
            if len(sources) != 1:
                raise RuntimeError(f"expected one source, found {len(sources)}")
            source = sources[0]
            locator = IndexLocator(cfg["index_dir"])
            producer = ArchiveEntryProducer(source, args.zim, cfg=cfg)
            try:
                report = build_index(
                    producer, locator, embedder, cfg,
                )
            finally:
                producer.close()
            search.refresh_sources()
            search.start_embedder(lambda: embedder)
            search.start_faiss()
            deadline = time.monotonic() + 60
            while True:
                indexes = [
                    index for runtime in search.runtimes.values()
                    for index in runtime.indexes.values()
                ]
                if indexes and all(index.semantic for index in indexes):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("semantic indexes did not become ready")
                time.sleep(0.02)
            search.start_fulltext_isolation()
            lexical = Search(
                RuntimeConfig(
                    settings={**cfg, "web_base_path": "/lexical"},
                    collections=collections,
                ),
                semantic=False,
            )
            lexical.start_fulltext_isolation()
            lease = locator.paths(source)
            try:
                artifact_bytes = lease.sqlite_path.stat().st_size + lease.faiss_path.stat().st_size
                metrics["sqlite_bytes"] = lease.sqlite_path.stat().st_size
                metrics["index_bytes"] = artifact_bytes
                metrics["index_source_ratio"] = artifact_bytes / args.zim.stat().st_size
                with sqlite3.connect(lease.sqlite_path) as db:
                    metrics["body_schema"] = db.execute(
                        "SELECT sql FROM sqlite_master WHERE name='body'"
                    ).fetchone()
            finally:
                lease.close()
            app = FastAPI()
            app.mount("/lexical", create_app(lexical))
            app.mount("/", create_app(search))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                sock.listen(128)
                ready = {
                    "url": f"http://127.0.0.1:{sock.getsockname()[1]}",
                    "source_key": source.key, "variant": args.variant,
                    "config": cfg, "model_fingerprint": embedder.fingerprint,
                    "build": report.to_dict(), "metrics": metrics,
                }
                (args.output / "ready.json").write_text(json.dumps(ready, indent=2))
                server = uvicorn.Server(uvicorn.Config(
                    app, host="127.0.0.1", log_level="warning", access_log=False,
                ))
                server.run(sockets=[sock])
        finally:
            if lexical is not None:
                lexical.close()
            search.close()


def wait_ready(process, output):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError((output / "server.log").read_text())
        path = output / "ready.json"
        if path.exists():
            ready = json.loads(path.read_text())
            try:
                with urllib.request.urlopen(ready["url"] + "/sources", timeout=2) as response:
                    sources = json.load(response)
                if any(source["key"] == ready["source_key"] for source in sources):
                    return ready
            except (urllib.error.URLError, TimeoutError):
                pass
        time.sleep(0.1)
    raise RuntimeError("evaluation server did not become ready")


def qualitative_changes(output, variants=VARIANTS, split="test"):
    from evaluation import queries

    selected, _ = queries.load(output / "queries", split=split)
    comparisons = {}
    for mode in ("hybrid", "lexical"):
        baseline = json.loads(
            (output / "results" / f"{variants[0]}-{mode}.json").read_text()
        )["results"]
        for variant in variants[1:]:
            candidate = json.loads(
                (output / "results" / f"{variant}-{mode}.json").read_text()
            )["results"]
            changes = []
            for query in selected:
                key = f"{query['zim']}|{query['id']}"
                before, after = baseline[key], candidate[key]
                if before["rank"] != after["rank"]:
                    changes.append({
                        **query, "baseline": variants[0], "head_rank": before["rank"],
                        "candidate_rank": after["rank"],
                        "head_top": before["top"], "candidate_top": after["top"],
                    })
            comparisons[f"{variant}-{mode}"] = {
                "queries": len(selected), "answer_rank_changes": changes,
                "empty_before": sum(
                    not baseline[f"{q['zim']}|{q['id']}"]["top"] for q in selected
                ),
                "empty_after": sum(
                    not candidate[f"{q['zim']}|{q['id']}"]["top"] for q in selected
                ),
            }
    (output / "qualitative.json").write_text(json.dumps(comparisons, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zim", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=ROOT / "model")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--variant", choices=ALL_VARIANTS, default="head")
    parser.add_argument("--variants", nargs="+", choices=ALL_VARIANTS,
                        help="variants to compare; first is the baseline")
    parser.add_argument("--report-split", choices=("dev", "test", "all"), default="test")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    args.zim = args.zim.resolve()
    args.output = args.output.resolve()
    args.model = args.model.resolve()
    variants = tuple(args.variants or VARIANTS)
    if args.threads < 1 or args.zim.stat().st_size >= 80_000_000:
        parser.error("use positive threads and a ZIM smaller than 80 MB")
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:args.threads])
    os.environ["OMP_NUM_THREADS"] = str(args.threads)
    if args.serve:
        serve(args)
        return
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "zims").mkdir()
    (args.output / "zims" / args.zim.name).symlink_to(args.zim)
    query_root = args.output / "queries"
    count = prepare_queries(args.zim, query_root)
    (args.output / "tokenizer-probes.json").write_text(json.dumps(
        tokenizer_probes(variants), indent=2
    ))
    print(f"{count} fixed queries; dev/test split unchanged", flush=True)
    results = args.output / "results"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    readiness = []
    for variant in variants:
        output = args.output / variant
        output.mkdir()
        print(f"building and evaluating {variant}", flush=True)
        with (output / "server.log").open("w") as log:
            process = subprocess.Popen([
                sys.executable, str(Path(__file__).resolve()), str(args.zim),
                "--serve", "--variant", variant, "--output", str(output),
                "--model", str(args.model), "--threads", str(args.threads),
            ], stdout=log, stderr=subprocess.STDOUT)
            try:
                ready = wait_ready(process, output)
                readiness.append(ready)
                for mode, suffix in (("hybrid", ""), ("lexical", "/lexical")):
                    subprocess.run([
                        sys.executable, str(ROOT / "evaluation/run.py"),
                        "--engine", "zimantic", "--url", ready["url"] + suffix,
                        "--label", f"{variant}-{mode}", "--commit", commit,
                        "--queries", str(query_root), "--results-dir", str(results),
                        "--zims", args.zim.stem, "--source-key", ready["source_key"],
                        "--zim-dir", str(args.output / "zims"), "--k", "20",
                    ], check=True)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    baseline = readiness[0]["metrics"]
    for ready in readiness:
        for key in ("rows_sha256", "vectors_sha256"):
            if ready["metrics"][key] != baseline[key]:
                raise RuntimeError(f"{ready['variant']}: differing {key}")
    (args.output / "method.json").write_text(json.dumps({
        "commit": commit, "zim": str(args.zim), "zim_bytes": args.zim.stat().st_size,
        "queries": count, "servers": readiness,
        "limitations": "English one-ZIM smoke comparison; synthetic, incomplete labels; no tuning",
    }, indent=2))
    qualitative_changes(args.output, variants, args.report_split)
    for mode in ("hybrid", "lexical"):
        command = [
            sys.executable, str(ROOT / "evaluation/compare.py"),
            *(str(results / f"{variant}-{mode}.json") for variant in variants),
            "--queries", str(query_root), "--split", args.report_split, "--by", "set", "suite",
        ]
        report = subprocess.check_output(command, text=True)
        (args.output / f"{mode}-comparison.txt").write_text(report)
        print(report)


if __name__ == "__main__":
    main()
