import hashlib
import json
from pathlib import Path
import os
import signal
import threading
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from .search import SearchBusyError, SearchQueryError

PAGE = Path(__file__).parent / "index.html"
PID_FILE = Path("zimantic.pid")
# Result payloads are text-heavy; gzip cuts them several-fold over the network.
GZIP_MIN_SIZE = 512


def _json_with_etag(request: Request, payload) -> Response:
    """JSON response with a content ETag, so a warm browser can revalidate
    source discovery cheaply with If-None-Match instead of re-downloading."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    etag = '"' + hashlib.sha1(body.encode("utf-8")).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def create_app(searchClass) -> FastAPI:
    app = FastAPI(title="zimantic")
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_SIZE)

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(PAGE)

    @app.get("/api/zims")
    def zims():
        return searchClass.local_names()

    @app.get("/api/config")
    def config(request: Request):
        return _json_with_etag(request, {
            "page_size": getattr(searchClass, "page_size", 10),
            "max_results": getattr(searchClass, "max_results", 100),
        })

    @app.get("/api/sources")
    def sources(request: Request):
        return _json_with_etag(request, searchClass.source_dicts())

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
        source: str | None = Query(None),
        limit: int | None = Query(None, ge=1),
        offset: int = Query(0, ge=0),
        debug: bool = Query(False),
    ):
        try:
            page = searchClass.search_page(q, zim, limit, offset, source, debug)
        except SearchBusyError as error:
            raise HTTPException(
                status_code=503, detail=str(error), headers={"Retry-After": "1"}
            ) from error
        except SearchQueryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        # Body stays the plain result list for compatibility; totals travel as
        # headers so clients can page without an envelope.
        return JSONResponse(
            content=page["results"],
            headers={
                "X-Total-Count": str(page["total"]),
                "X-Has-More": "true" if page["has_more"] else "false",
                "X-Offset": str(page["offset"]),
                "X-Page-Size": str(len(page["results"])),
            },
        )

    @app.get("/api/search/stream")
    def api_search_stream(
        q: str = Query(..., min_length=1, max_length=4096),
        zim: list[str] | None = Query(None),
        source: str | None = Query(None),
        limit: int | None = Query(None, ge=1),
        offset: int = Query(0, ge=0),
        debug: bool = Query(False),
    ):
        try:
            searchClass._validate_query(q)
        except SearchQueryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        def events():
            for event in searchClass.stream_search(q, zim, limit, offset, source, debug):
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
    app = create_app(searchClass)
    try:
        PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError as error:
        print(f"zimantic: could not write PID file {PID_FILE}: {error}", flush=True)

    # Optional Kiwix catalog discovery runs in the background so an unreachable
    # Kiwix server cannot delay binding the port.
    start_catalog = getattr(searchClass, "start_catalog_refresh", None)
    if callable(start_catalog):
        start_catalog()

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
