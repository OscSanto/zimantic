import json
from pathlib import Path
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from .search import SearchQueryError

PAGE = Path(__file__).parent / "index.html"


def create_app(searchClass) -> FastAPI:
    app = FastAPI(title="zimantic")

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(PAGE)

    @app.get("/api/zims")
    def zims():
        return searchClass.local_names()

    @app.get("/api/sources")
    def sources(refresh: bool = Query(False)):
        if refresh:
            searchClass.refresh_sources()
        return searchClass.source_dicts()

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


def serve(searchClass, port: int) -> None:
    # 0.0.0.0: reachable from other devices on the network, not only this machine.
    app = create_app(searchClass)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
