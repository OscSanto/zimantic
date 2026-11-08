#!/usr/bin/env python3
"""Benchmark shared native-search budgets in transient systemd user services."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zimantic.fulltext import FulltextPool
from zimantic.hardware import _cgroup_paths, detect
from zimantic.settings import MAX_AUTO_FULLTEXT_WORKERS, default_fulltext_workers

QUERIES = (
    "water", "history", "science", "population", "island", "city", "book",
    "school", "statistics", "probability", '"new york"', "zzzimantic_no_match_93847",
)


def counters(path: Path) -> dict[str, int]:
    try:
        return {
            key: int(value)
            for key, value in (line.split() for line in path.read_text().splitlines())
        }
    except (OSError, ValueError):
        return {}


def cgroup_stats() -> dict:
    paths = [path for path in _cgroup_paths() if (path / "memory.peak").is_file()]
    if not paths:
        return {}
    path = max(paths, key=lambda value: len(value.parts))
    values = {}
    for key in ("memory.peak", "memory.current", "memory.max", "memory.swap.peak"):
        try:
            values[key] = int((path / key).read_text())
        except (OSError, ValueError):
            pass
    values["memory.events"] = counters(path / "memory.events")
    values["cpu.stat"] = counters(path / "cpu.stat")
    values["memory.stat"] = {
        key: value for key, value in counters(path / "memory.stat").items()
        if key in {"anon", "file", "kernel", "pagetables"}
    }
    return values


def probe(spec: dict) -> dict:
    hw = detect()
    workers = (
        default_fulltext_workers(source_workers=spec["source_workers"])
        if spec["workers"] == "auto" else int(spec["workers"])
    )
    paths = spec["paths"]
    mode = spec["mode"]
    resident = bytearray(spec.get("resident_mib", 0) << 20)
    for offset in range(0, len(resident), 4096):
        resident[offset] = 1
    pools = [FulltextPool(workers) for _ in range(len(paths) if mode == "per-collection" else 1)]
    jobs = [(index, path, query) for query in QUERIES for index, path in enumerate(paths)]
    jobs = (jobs * ((spec["requests"] + len(jobs) - 1) // len(jobs)))[:spec["requests"]]
    report = {
        "workers": workers, "mode": mode, "pool_count": len(pools),
        "worker_budget": workers * len(pools), "source_workers": spec["source_workers"],
        "detected_hardware": vars(hw), "requested_workers": spec["workers"],
        "resident_mib": spec.get("resident_mib", 0),
        "status": "ok",
    }

    def search(job):
        index, path, query = job
        start = time.perf_counter()
        pool = pools[index if mode == "per-collection" else 0]
        results = pool.search_results(path, query, 16)
        duration = (time.perf_counter() - start) * 1000
        digest = None if results is None else hashlib.sha256(
            json.dumps(results, sort_keys=True).encode()
        ).hexdigest()
        return duration, digest, 0 if results is None else len(results)

    try:
        with ThreadPoolExecutor(spec["source_workers"]) as executor:
            started = time.perf_counter()
            warm = list(executor.map(search, jobs))
            report["warmup_seconds"] = time.perf_counter() - started
            if any(item[1] is None for item in warm):
                report["status"] = "failed-warmup"
                report["failed_searches"] = sum(item[1] is None for item in warm)
            else:
                before = cgroup_stats()
                started = time.perf_counter()
                results = list(executor.map(search, jobs))
                elapsed = time.perf_counter() - started
                after = cgroup_stats()
                latencies = sorted(item[0] for item in results)
                failures = sum(item[1] is None for item in results)
                report.update(
                    status="failed-search" if failures else "ok",
                    requests=len(jobs), elapsed_seconds=elapsed,
                    requests_per_second=len(jobs) / elapsed,
                    p50_ms=statistics.median(latencies),
                    p95_ms=latencies[int(0.95 * (len(latencies) - 1))],
                    failed_searches=failures,
                    returned_hits=sum(item[2] for item in results),
                    result_digest=None if failures else hashlib.sha256(
                        "".join(item[1] for item in results).encode()
                    ).hexdigest(),
                    measured_cpu_usage_usec=after.get("cpu.stat", {}).get("usage_usec", 0)
                    - before.get("cpu.stat", {}).get("usage_usec", 0),
                    measured_throttled_usec=after.get("cpu.stat", {}).get("throttled_usec", 0)
                    - before.get("cpu.stat", {}).get("throttled_usec", 0),
                )
        report["started_helpers"] = sum(
            worker._process is not None for pool in pools for worker in pool._all
        )
        report["blacklisted_archives"] = [
            Path(path).name for path in paths if any(pool.is_broken(path) for pool in pools)
        ]
        report["cgroup"] = cgroup_stats()
        if report["status"] == "ok" and report["cgroup"].get("memory.events", {}).get("oom_kill", 0):
            report["status"] = "oom-recovered"
    finally:
        for pool in pools:
            pool.close()
    return report


def process_tree(pid: int) -> set[int]:
    found = set()
    pending = [pid]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        try:
            for task in Path(f"/proc/{current}/task").iterdir():
                try:
                    pending.extend(map(int, (task / "children").read_text().split()))
                except (OSError, ValueError):
                    pass
        except OSError:
            pass
    return found


def pss_kib(pid: int) -> int:
    total = 0
    for current in process_tree(pid):
        try:
            for line in Path(f"/proc/{current}/smaps_rollup").read_text().splitlines():
                if line.startswith("Pss:"):
                    total += int(line.split()[1])
        except (OSError, ValueError):
            pass
    return total


def constrained_trial(spec: dict, cpus: int, ram_mib: int) -> dict:
    unit = f"zimantic-native-bench-{uuid.uuid4().hex}"
    command = [
        "systemd-run", "--user", "--wait", "--pipe", "--collect",
        f"--unit={unit}", "-p", f"CPUQuota={cpus * 100}%",
        "-p", f"MemoryMax={ram_mib}M", "-p", "MemorySwapMax=0",
        "-p", "RuntimeMaxSec=90s", "-p", "OOMPolicy=continue",
        "-p", "LimitCORE=0", "-p", f"WorkingDirectory={ROOT}",
        sys.executable, str(Path(__file__).resolve()), "--probe", json.dumps(spec),
    ]
    with tempfile.TemporaryFile(mode="w+t") as errors, subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=errors, text=True
    ) as process:
        # The service isn't a child of systemd-run. Its PID is supplied by the
        # probe before native work begins, so the monitor stays outside its quota.
        line = process.stdout.readline()
        peak_pss = 0
        try:
            hello = json.loads(line)
            service_pid = int(hello["pid"])
        except (ValueError, KeyError, TypeError):
            service_pid = 0
        while process.poll() is None:
            if service_pid:
                peak_pss = max(peak_pss, pss_kib(service_pid))
            time.sleep(0.025)
        stdout, _ = process.communicate()
        errors.seek(0)
        stderr = errors.read()
    try:
        report = json.loads(stdout)
    except (ValueError, TypeError):
        report = {"status": "unit-failed", "output": line + stdout}
    report.update(
        cpu_quota=cpus, memory_max_mib=ram_mib, unit_exit=process.returncode,
        peak_tree_pss_mib=peak_pss / 1024,
    )
    if stderr:
        report["stderr"] = stderr
        result = re.search(r"Finished with result:\s*(\S+)", stderr)
        if result:
            report["unit_result"] = result[1]
            if report["status"] == "unit-failed":
                report["status"] = result[1]
    if report.get("status") == "ok":
        observed = report["detected_hardware"]
        if observed["threads"] > cpus or observed["ram_total"] > ram_mib * (1 << 20):
            raise RuntimeError(f"Resource limits weren't detected: {report}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workers", nargs="+", default=["1", "2", "4", "8", "auto"])
    parser.add_argument(
        "--limits", nargs="+",
        default=["1:256", "1:512", "2:512", "2:1024", "4:1024", "8:2048"],
        help="CPU-quota:RAM-MiB pairs",
    )
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--requests", type=int, default=96)
    parser.add_argument("--source-workers", type=int, default=8)
    parser.add_argument(
        "--resident-mib", type=int, default=0,
        help="touch and retain this many MiB to simulate server/model headroom",
    )
    parser.add_argument("--legacy", action="store_true", help="also test two helpers per collection")
    parser.add_argument("--probe", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe:
        print(json.dumps({"pid": os.getpid()}), flush=True)
        print(json.dumps(probe(json.loads(args.probe))), flush=True)
        return
    if not args.paths or args.output is None:
        parser.error("archive paths and --output are required")
    if min(args.requests, args.source_workers, args.repeats) < 1:
        parser.error("request, source-worker and repeat counts must be positive")
    if args.resident_mib < 0:
        parser.error("--resident-mib must not be negative")
    if any(value != "auto" and (not value.isdigit() or int(value) < 1) for value in args.workers):
        parser.error("--workers values must be positive integers or auto")
    try:
        limits = [tuple(map(int, value.split(":"))) for value in args.limits]
        if any(len(pair) != 2 or min(pair) < 1 for pair in limits):
            raise ValueError
    except ValueError:
        parser.error("--limits values must be positive CPU-quota:RAM-MiB pairs")
    paths = [str(path.resolve(strict=True)) for path in args.paths]
    output = {
        "date": time.strftime("%Y-%m-%d"),
        "automatic_worker_ceiling": MAX_AUTO_FULLTEXT_WORKERS,
        "python": platform.python_version(), "libzim": importlib.metadata.version("libzim"),
        "platform": platform.platform(), "queries": QUERIES,
        "archives": [{"name": Path(path).name, "bytes": Path(path).stat().st_size} for path in paths],
        "result_limit": 16, "requests_per_trial": args.requests,
        "resident_mib": args.resident_mib,
        "rows": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repetition in range(args.repeats):
        for cpus, ram in limits:
            modes = [("shared", workers) for workers in args.workers]
            if args.legacy:
                modes.append(("per-collection", "2"))
            if repetition % 2:
                modes.reverse()
            for mode, workers in modes:
                spec = {
                    "paths": paths, "workers": workers, "mode": mode,
                    "requests": args.requests, "source_workers": args.source_workers,
                    "resident_mib": args.resident_mib,
                }
                row = constrained_trial(spec, cpus, ram)
                row.update(repetition=repetition, mode=mode, requested_workers=workers)
                output["rows"].append(row)
                args.output.write_text(json.dumps(output, indent=2) + "\n")
                speed = row.get("requests_per_second", 0)
                peak = row.get("cgroup", {}).get("memory.peak")
                peak_text = f"{peak / (1 << 20):6.1f}" if peak is not None else "   n/a"
                print(
                    f"{repetition} cpu={cpus} ram={ram:4} {mode:14} workers={workers:4} "
                    f"{row['status']:14} {speed:7.1f} req/s "
                    f"cgroup={peak_text}MiB PSS={row['peak_tree_pss_mib']:6.1f}MiB",
                    flush=True,
                )
    valid = [row for row in output["rows"] if row["status"] == "ok"]
    if len({row["result_digest"] for row in valid}) > 1:
        raise RuntimeError("Successful configurations returned different results")
    print(f"Saved {len(output['rows'])} trials; successful result digests agree.")


if __name__ == "__main__":
    main()
