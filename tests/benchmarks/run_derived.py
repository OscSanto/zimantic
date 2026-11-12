"""Benchmark the derived-text OCR worker pool against in-process extraction.

The pool is shared by every source build in one command. This synthetic run
measures the engine dispatch itself: the same local Tesseract fixture is
extracted sequentially and through increasing process-pool sizes. It does not
model filesystem reads or pipeline windows, so use it to answer "does the pool
help?" rather than as a whole-build prediction.

Run with the committed OCR fixture:

    uv run python tests/benchmarks/run_derived.py --count 32 --repeats 3

Save a report under ``tests/benchmarks/results/`` with ``--json-out``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zimantic.indexing.derived import DerivedWorkerPool
from zimantic.indexing.engines import extract_ocr

_DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/media/image/scan.png"
)


def _run_sequential(payload: bytes, count: int, options: dict) -> tuple[float, str]:
    started = time.perf_counter()
    digest = hashlib.sha256()
    for _ in range(count):
        digest.update(extract_ocr(payload, **options).encode("utf-8"))
    return time.perf_counter() - started, digest.hexdigest()


def _run_pool(
    payload: bytes, count: int, options: dict, workers: int
) -> tuple[float, str]:
    pool = DerivedWorkerPool(workers)
    started = time.perf_counter()
    try:
        futures = [
            pool.submit("ocr", "tesseract", payload, options) for _ in range(count)
        ]
        digest = hashlib.sha256()
        for future in futures:
            digest.update(future.result().encode("utf-8"))
        return time.perf_counter() - started, digest.hexdigest()
    finally:
        pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=_DEFAULT_FIXTURE)
    parser.add_argument("--count", type=int, default=32, help="OCR tasks per trial")
    parser.add_argument(
        "--workers",
        default="2,4,8",
        help="comma-separated process-pool sizes",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--languages", default="eng")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    if not args.fixture.is_file():
        parser.error(f"fixture not found: {args.fixture}")
    if shutil.which("tesseract") is None:
        parser.error("tesseract is not installed")
    payload = args.fixture.read_bytes()
    options = {"ocr_languages": args.languages.split("+")}
    worker_sizes = [int(value) for value in args.workers.split(",") if value]

    trials: dict[str, list[float]] = {"sequential": []}
    for workers in worker_sizes:
        trials[f"pool-{workers}"] = []
    digests: dict[str, str] = {}
    for _ in range(max(1, args.repeats)):
        elapsed, digest = _run_sequential(payload, args.count, options)
        trials["sequential"].append(elapsed)
        digests.setdefault("sequential", digest)
        for workers in worker_sizes:
            elapsed, digest = _run_pool(payload, args.count, options, workers)
            trials[f"pool-{workers}"].append(elapsed)
            digests.setdefault(f"pool-{workers}", digest)

    baseline = statistics.median(trials["sequential"])
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "fixture": str(args.fixture),
        "fixture_bytes": len(payload),
        "tasks": args.count,
        "repeats": max(1, args.repeats),
        "languages": options["ocr_languages"],
        "results": {
            name: {
                "median_seconds": round(statistics.median(values), 3),
                "min_seconds": round(min(values), 3),
                "max_seconds": round(max(values), 3),
                "speedup_vs_sequential": round(baseline / statistics.median(values), 2),
                "digest": digests.get(name),
            }
            for name, values in trials.items()
        },
    }
    print(json.dumps(report, indent=2))
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
