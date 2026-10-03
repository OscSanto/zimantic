import threading
from pathlib import Path
import uvicorn
#localhost:8090/docs
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

PAGE = Path(__file__).parent / "index.html"


def serve(searchClass, port: int) -> None:
    app = FastAPI(title="zimantic")
    lock = threading.Lock()  # one search at a time keeps RAM predictable on the Pi

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(PAGE)

    @app.get("/api/zims")
    def zims():
        return list(searchClass.indexes)  

    # TODO: limit has multiple duplicates in config.yaml, here, and server.py with conflicting logic
    @app.get("/api/search")
    def api_search(q: str, zim: list[str] | None = Query(None), limit: int = Query(searchClass.cfg["results"], ge=1, le=100)):
        with lock:
            return searchClass.search(q, zim, limit)

    # 0.0.0.0: reachable from other devices on the network, not only this machine.
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
