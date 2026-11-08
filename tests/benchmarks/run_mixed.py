#!/usr/bin/env python3
"""Run the phase-09 mixed-library release benchmark.

This harness is intentionally dependency-free.  It starts a real Zimantic
server over the generated fixture, measures startup, search latency, RSS, and
the library payload, and prints a JSON report plus a Markdown summary suitable
for pasting into a release record.

Example on a Raspberry Pi 4:

    python tests/fixtures/mixed_library/generate.py --root /tmp/zimantic-mixed
    python tests/benchmarks/run_mixed.py --root /tmp/zimantic-mixed \
        --device-model "Raspberry Pi 4 Model B 4 GB" --ram 4GB \
        --os "Raspberry Pi OS 64-bit" --storage "USB SSD" \
        --commit "$(git rev-parse --short HEAD)" \
        --fixture-revision phase09-v1

Without a result file the gate is unverified, not passed.  A search against an
unreachable Kiwix entry is reported as fixture error, never hidden.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUERIES = 100


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _get(url: str, timeout: float = 30.0) -> tuple[bytes, float]:
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read()
    return body, time.perf_counter() - started


def wait_for_server(url: str, process: subprocess.Popen, timeout: float = 120.0) -> float:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        if process.poll() is not None:
            raise RuntimeError(
                f"server exited during startup with status {process.returncode}"
            )
        try:
            with urllib.request.urlopen(url + "/health", timeout=1.0) as response:
                if response.status == 200:
                    return time.perf_counter() - started
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.1)
    raise TimeoutError(f"server did not become ready within {timeout} seconds")


def read_rss_kib(pid: int) -> int:
    status = Path(f"/proc/{pid}/status")
    try:
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


def load_queries(root: Path, limit: int) -> list[str]:
    path = root / "queries.txt"
    if not path.is_file():
        raise FileNotFoundError(
            f"fixed query set is missing: {path}; regenerate the fixture"
        )
    queries = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not queries:
        raise ValueError(f"fixed query set is empty: {path}")
    return queries[:limit]


def run(
    root: Path,
    *,
    queries_limit: int = DEFAULT_QUERIES,
    fast: bool = True,
    build: str | None = None,
    startup_timeout: float = 120.0,
) -> dict:
    root = root.resolve()
    if not (root / "config.toml").is_file():
        raise FileNotFoundError(
            f"fixture config is missing under {root}; run the fixture generator first"
        )
    queries = load_queries(root, queries_limit)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    xdg_config = tempfile.TemporaryDirectory()
    config_dir = Path(xdg_config.name) / "zimantic"
    try:
        config_dir.mkdir()
        (config_dir / "config.toml").symlink_to(root / "config.toml")
    except BaseException:
        xdg_config.cleanup()
        raise
    env["XDG_CONFIG_HOME"] = xdg_config.name
    env["XDG_DATA_HOME"] = str(root)
    command = [sys.executable, "-m", "zimantic", "serve"]
    if fast:
        command.append("--fast")

    build_result = None
    if build:
        build_command = [sys.executable, "-m", "zimantic", "build"]
        if build == "fast":
            build_command.append("--fast")
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                build_command,
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        except BaseException:
            xdg_config.cleanup()
            raise
        build_result = {
            "mode": build,
            "seconds": time.perf_counter() - started,
            "returncode": completed.returncode,
            "stderr_tail": completed.stderr.splitlines()[-10:],
        }

    try:
        server = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except BaseException:
        xdg_config.cleanup()
        raise
    try:
        startup = wait_for_server("http://127.0.0.1:8099", server, startup_timeout)
        base = "http://127.0.0.1:8099"
        # One warm-up that is not part of the latency sample.
        _get(base + "/search?q=library%20article")
        latencies: list[float] = []
        failures = 0
        for query in queries:
            url = base + "/search?q=" + urllib.parse.quote(query)
            try:
                _, elapsed = _get(url)
            except (urllib.error.URLError, TimeoutError, OSError):
                failures += 1
                continue
            latencies.append(elapsed)
        library_bytes, library_seconds = _get(base + "/library")
        rss_kib = read_rss_kib(server.pid)
        return {
            "fixture": str(root),
            "startup_seconds": round(startup, 3),
            "library_payload_bytes": len(library_bytes),
            "library_render_seconds": round(library_seconds, 4),
            "search": {
                "queries": len(queries),
                "successful": len(latencies),
                "failures": failures,
                "p50_seconds": round(statistics.median(latencies), 4)
                if latencies
                else None,
                "p95_seconds": round(percentile(latencies, 0.95), 4)
                if latencies
                else None,
                "max_seconds": round(max(latencies), 4) if latencies else None,
            },
            "peak_rss_mib": round(rss_kib / 1024.0, 1) if rss_kib else None,
            "build": build_result,
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
        }
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        xdg_config.cleanup()


def markdown(report: dict, identity: dict) -> str:
    search = report["search"]
    lines = [
        "## Mixed-library benchmark result",
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    for key, value in identity.items():
        lines.append(f"| {key} | {value or 'not recorded'} |")
    lines.extend(
        [
            f"| Cold startup to HTTP-ready | {report['startup_seconds']} s |",
            f"| Search p50 ({search['queries']} fixed queries) | "
            f"{search['p50_seconds']} s |",
            f"| Search p95 | {search['p95_seconds']} s |",
            f"| Search failures | {search['failures']} |",
            f"| Peak RSS | {report['peak_rss_mib']} MiB |",
            f"| `/library` payload | {report['library_payload_bytes']} bytes |",
            f"| `/library` render | {report['library_render_seconds']} s |",
            "",
            "Targets: startup < 120 s, mixed search p95 < 8 s, peak RSS < 1 GiB.",
            "A missing fixture or hardware result is an unverified gate, not a pass.",
        ]
    )
    if report.get("build"):
        lines.append(
            f"Build ({report['build']['mode']}): "
            f"{report['build']['seconds']:.3f} s, "
            f"exit {report['build']['returncode']}."
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--queries", type=int, default=DEFAULT_QUERIES)
    parser.add_argument(
        "--semantic",
        action="store_true",
        help="load the model/FAISS instead of using the --fast profile",
    )
    parser.add_argument(
        "--build",
        choices=("fast", "full"),
        help="run a build before measuring startup",
    )
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--device-model", default="")
    parser.add_argument("--ram", default="")
    parser.add_argument("--os", dest="os_name", default="")
    parser.add_argument("--storage", default="")
    parser.add_argument("--commit", default="")
    parser.add_argument("--fixture-revision", default="")
    args = parser.parse_args(argv)

    report = run(
        args.root,
        queries_limit=args.queries,
        fast=not args.semantic,
        build=args.build,
        startup_timeout=args.startup_timeout,
    )
    identity = {
        "Device model/RAM": " / ".join(
            part for part in (args.device_model, args.ram) if part
        ),
        "OS": args.os_name,
        "Storage": args.storage,
        "Python": report["environment"]["python"],
        "Commit": args.commit,
        "Fixture revision": args.fixture_revision,
    }
    output = {"identity": identity, **report}
    text = json.dumps(output, indent=2) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(markdown(report, identity), encoding="utf-8")
    print(text, end="")
    return 0 if report["search"]["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
