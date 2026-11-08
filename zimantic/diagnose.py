"""On-demand ZIM checks, with all native work confined to a disposable helper."""

from __future__ import annotations

import faulthandler
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

DEFAULT_QUERIES = ("water", "history", "science")


def _text(value: str | bytes | None) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""


def diagnose_archive(
    path: Path,
    *,
    queries: Sequence[str] = DEFAULT_QUERIES,
    checksum: bool = False,
    timeout: float = 30.0,
    count: int = 16,
) -> dict[str, Any]:
    """Check one archive without exposing the caller to native crashes."""
    started = time.monotonic()
    report: dict[str, Any] = {"path": str(path), "name": path.name}
    request = {"path": str(path), "queries": list(queries), "checksum": checksum, "count": count}
    try:
        process = subprocess.run(
            [sys.executable, "-m", "zimantic.diagnose"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        report.update(
            status="timeout",
            error=f"checks exceeded {timeout:g} seconds",
            diagnostics=_text(error.stderr),
        )
    except OSError as error:
        report.update(status="error", error=f"could not start helper: {error}")
    else:
        if process.returncode < 0:
            try:
                reason = signal.Signals(-process.returncode).name
            except ValueError:
                reason = f"signal {-process.returncode}"
            killed = -process.returncode == getattr(signal, "SIGKILL", None)
            report.update(
                status="killed" if killed else "crash",
                error=f"helper terminated by {reason}"
                + (" (possible resource limit; archive not validated)" if killed else ""),
                diagnostics=process.stderr,
            )
        else:
            try:
                result = json.loads(process.stdout)
                if not isinstance(result, dict) or result.get("status") not in {"ok", "error"}:
                    raise ValueError("invalid diagnostic response")
            except (ValueError, TypeError):
                report.update(
                    status="error",
                    error=f"helper exited {process.returncode} without a valid report",
                    diagnostics=process.stderr,
                )
            else:
                report.update(result)
                if process.returncode and report["status"] == "ok":
                    report.update(
                        status="error",
                        error=f"helper exited {process.returncode} after reporting success",
                    )
                if report["status"] != "ok":
                    report["diagnostics"] = process.stderr
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def _worker_main() -> None:
    from libzim.reader import Archive
    from libzim.search import Query, Searcher

    from .libzim_compat import search_results

    faulthandler.enable()
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, AttributeError, OSError, ValueError):
        pass
    request = json.loads(sys.stdin.read())
    path = Path(request["path"])
    phase = "open"
    report: dict[str, Any] = {"checksum_checked": False, "searches": []}

    def note(operation: str) -> None:
        print(f"zimantic diagnose: {path.name}: {operation}", file=sys.stderr, flush=True)

    try:
        note(phase)
        archive = Archive(path)
        report["entry_count"] = archive.entry_count
        report["has_fulltext_index"] = bool(archive.has_fulltext_index)
        if request["checksum"]:
            phase = "checksum"
            note(phase)
            if not archive.check():
                raise ValueError("checksum failed or archive has no checksum")
            report["checksum_checked"] = True
        if report["has_fulltext_index"]:
            phase = "create searcher"
            note(phase)
            searcher = Searcher(archive)
            for query in request["queries"]:
                phase = f"search {query!r}"
                note(phase)
                search = searcher.search(Query().set_query(query))
                results = search_results(search, 0, request["count"], archive=archive)
                report["searches"].append({"query": query, "hits": len(results)})
    except Exception as error:
        report.update(status="error", operation=phase, error=f"{type(error).__name__}: {error}")
    else:
        report["status"] = "ok"
    print(json.dumps(report), flush=True)
    raise SystemExit(0 if report["status"] == "ok" else 1)


if __name__ == "__main__":
    _worker_main()
