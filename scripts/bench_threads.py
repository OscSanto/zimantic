"""Measure CPU embedding throughput and isolated query latency.

Use a fresh process per configuration, keeping model, ZIM and CPU affinity fixed.
Example: python scripts/bench_threads.py --model model --threads 1 --workers 3 \
    --zim archive.zim
Without --workers, only serving latency is measured. No library indexes are written.
"""

import argparse
import json
import resource
import statistics
import sys
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

QUERY_TEXTS = [
    "query: how do moderators handle spam?",
    (
        "query: How can an online community encourage experienced members to help "
        "newcomers without lowering the quality of contributions?"
    ),
    "query: "
    + (
        "How should a community handle disagreement between volunteers and "
        "administrators while maintaining trust and welcoming new people? "
    )
    * 10,
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("model"))
    parser.add_argument("--threads", type=int, required=True)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--zim", type=Path)
    parser.add_argument("--articles", type=int, default=512)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--queries", type=int, default=50)
    args = parser.parse_args()
    if (
        min(args.threads, args.articles, args.repeats, args.queries) < 1
        or args.workers < 0
    ):
        parser.error("counts must be positive; workers may be zero for serving only")
    if args.workers and (args.zim is None or args.zim.stat().st_size >= 80_000_000):
        parser.error("building requires a ZIM smaller than 80 MB")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import importlib.metadata

    from zimantic.embed import Embedder
    from zimantic.hardware import detect
    from zimantic.indexing.pipeline import EmbeddingPipeline
    from zimantic.providers.zim.entries import ArchiveEntryProducer

    embedder = Embedder(args.model, threads=args.threads, device="cpu")
    result = {
        "hardware": detect().describe(),
        "threads": args.threads,
        "workers": args.workers,
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("onnxruntime", "libzim", "sentencepiece", "numpy")
        },
    }
    if args.workers:
        producer = ArchiveEntryProducer(
            SimpleNamespace(key="benchmark", provider="zim"), args.zim
        )
        try:
            articles = [
                row._replace(body="", body_truncated=False)
                for i in range(producer.count)
                if (row := producer.entry_at(i)) is not None and row.excerpt
            ]
        finally:
            producer.close()
        size = min(args.articles, len(articles))
        if size == 0:
            parser.error("the archive has no embeddable articles")
        selected = [
            articles[i * (len(articles) - 1) // max(1, size - 1)] for i in range(size)
        ]
        embedder.embed(["passage: warm up"])
        trials = []
        pool = EmbeddingPipeline(
            embedder, workers=args.workers, queue_size=2 * args.workers
        )
        try:
            for _ in range(args.repeats):
                pending = deque()
                embedded = 0
                began = time.perf_counter()
                for i in range(0, len(selected), 32):
                    pending.append(pool.submit(selected[i : i + 32]))
                    if len(pending) >= pool.max_pending_per_build:
                        embedded += len(pending.popleft().result())
                while pending:
                    embedded += len(pending.popleft().result())
                seconds = time.perf_counter() - began
                if embedded != size:
                    raise RuntimeError("benchmark lost vector identities")
                trials.append(seconds)
        finally:
            pool.close()
        result.update(
            articles=size,
            window_rows=32,
            batch_tokens=1000,
            build_seconds=trials,
            articles_per_second=size / statistics.median(trials),
        )
    else:
        results = []
        for text in QUERY_TEXTS:
            for _ in range(4):
                embedder.embed([text])
            timings = []
            for _ in range(args.queries):
                began = time.perf_counter()
                embedder.embed([text])
                timings.append(1000 * (time.perf_counter() - began))
            results.append(
                {
                    "tokens": len(embedder.encode([text])[0]),
                    "median_ms": statistics.median(timings),
                    "p95_ms": sorted(timings)[int(0.95 * (len(timings) - 1))],
                }
            )
        result["query_latency"] = results
    result["peak_rss_mb"] = (
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024)
        / 1_000_000
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
