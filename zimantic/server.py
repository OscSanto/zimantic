import json
from pathlib import Path
import os
import signal
import threading
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from .search import SearchQueryError

PAGE = Path(__file__).parent / "index.html"
PID_FILE = Path("zimantic.pid")


def create_app(searchClass) -> FastAPI:
    app = FastAPI(title="zimantic")

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(PAGE)

    @app.get("/api/zims")
    def zims():
        return searchClass.local_names()

    @app.get("/api/sources")
    def sources():
        # Source discovery only: clients can never force a refresh. Rebuilding the
        # source set is an admin action carried out by `zimantic reload` or SIGHUP.
        return searchClass.source_dicts()

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "indexes": searchClass.local_names(),
            "sources": len(searchClass.source_dicts()),
            "cache": searchClass.cache.stats(),
        }

    @app.get("/api/search")
    def api_search(
        q: str = Query(..., min_length=1, max_length=4096),
        zim: list[str] | None = Query(None),
        limit: int = Query(searchClass.cfg["results"], ge=1, le=100),
    ):
        try:
            return searchClass.search(q, zim, limit)
        except SearchQueryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/search/stream")
    def api_search_stream(
        q: str = Query(..., min_length=1, max_length=4096),
        zim: list[str] | None = Query(None),
        limit: int = Query(searchClass.cfg["results"], ge=1, le=100),
    ):
        try:
            searchClass._validate_query(q)
        except SearchQueryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        def events():
            for event in searchClass.stream_search(q, zim, limit):
                yield json.dumps(event, separators=(",", ":")) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    return app


def _handle_reload_request(searchClass, guard: threading.Lock) -> None:
    """SIGHUP handler: reload in a worker thread so the event loop is not blocked
    while indexes are reopened. A second signal during a reload is ignored."""
    if not guard.acquire(blocking=False):
        print("zimantic: reload already in progress; ignoring signal", flush=True)
        return

    def _reload():
        try:
            summary = searchClass.reload()
            print(
                "zimantic: reloaded "
                f"{len(summary.get('indexes', []))} index(es) "
                f"(+{len(summary.get('added', []))} new, "
                f"~{len(summary.get('upgraded', []))} upgraded, "
                f"-{len(summary.get('removed', []))} removed)",
                flush=True,
            )
        except Exception as error:  # never let a reload take the process down
            print(f"zimantic: reload failed: {error}", flush=True)
        finally:
            guard.release()

    threading.Thread(target=_reload, name="zimantic-reload", daemon=True).start()


def serve(searchClass, port: int) -> None:
    # 0.0.0.0: reachable from other devices on the network, not only this machine.
    app = create_app(searchClass)
    try:
        PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError as error:
        print(f"zimantic: could not write PID file {PID_FILE}: {error}", flush=True)

    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        guard = threading.Lock()
        signal.signal(sighup, lambda _signum, _frame: _handle_reload_request(searchClass, guard))

    print(
        f"zimantic: server loaded on port {port} "
        f"(PID {os.getpid()}; reload with `zimantic reload` or `kill -HUP <pid>`)",
        flush=True,
    )
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass
