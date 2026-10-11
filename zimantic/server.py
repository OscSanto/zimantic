import hashlib
from html import escape as html_escape
import json
import logging
from pathlib import Path
import os
import re
import signal
import threading
from urllib.parse import quote as quote_path
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from .search import (
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
    SearchBusyError,
    SearchQueryError,
)

PAGE = Path(__file__).parent / "index.html"
PID_FILE = Path("zimantic.pid")
logger = logging.getLogger(__name__)

# Result payloads are text-heavy; gzip cuts them several-fold over the network.
GZIP_MIN_SIZE = 512
ARTICLE_STREAM_THRESHOLD = 512 * 1024
ARTICLE_STREAM_CHUNK = 64 * 1024


ARTICLE_TOOLBAR_STYLE = """<style data-zimantic-toolbar>
[data-zimantic-toolbar] {
  align-items: center !important;
  background: #171a21 !important;
  border-bottom: 1px solid #2a2f3a !important;
  box-shadow: 0 1px 5px rgba(0, 0, 0, .25) !important;
  color: #e6e8eb !important;
  display: flex !important;
  flex-wrap: wrap !important;
  font: 14px/1.3 system-ui, sans-serif !important;
  gap: 6px !important;
  margin: 0 0 16px !important;
  padding: 6px 10px !important;
  position: sticky !important;
  top: 0 !important;
  width: 100% !important;
  z-index: 2147483647 !important;
}
[data-zimantic-toolbar],
[data-zimantic-toolbar] * { box-sizing: border-box !important; }
[data-zimantic-toolbar] a,
[data-zimantic-toolbar] button {
  background: transparent !important;
  border: 1px solid #46505f !important;
  border-radius: 4px !important;
  color: #d7e5f7 !important;
  cursor: pointer !important;
  font: inherit !important;
  padding: 4px 8px !important;
  text-decoration: none !important;
}
[data-zimantic-toolbar] a:hover,
[data-zimantic-toolbar] button:hover {
  background: #263449 !important;
  border-color: #4f9cf9 !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-nav,
[data-zimantic-toolbar] .zimantic-toolbar-actions {
  align-items: center !important;
  display: flex !important;
  flex-wrap: nowrap !important;
  gap: 6px !important;
  min-width: 0 !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-actions {
  margin-left: auto !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-home {
  max-width: min(32vw, 360px) !important;
  min-width: 0 !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  white-space: nowrap !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-article {
  max-width: min(32vw, 360px) !important;
  min-width: 0 !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  white-space: nowrap !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-home-short {
  display: none !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-home.compact .zimantic-toolbar-home-full {
  display: none !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-home.compact .zimantic-toolbar-home-short {
  display: inline !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-article-short {
  display: none !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-article.compact .zimantic-toolbar-article-full {
  display: none !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-article.compact .zimantic-toolbar-article-short {
  display: inline !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-breadcrumbs {
  align-items: center !important;
  display: flex !important;
  gap: 6px !important;
  min-width: 0 !important;
  overflow: hidden !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-breadcrumb {
  max-width: 18vw !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  white-space: nowrap !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-separator {
  color: #9aa3af !important;
  flex: 0 0 auto !important;
}
[data-zimantic-toolbar] .zimantic-toolbar-status {
  clip: rect(0 0 0 0) !important;
  clip-path: inset(50%) !important;
  height: 1px !important;
  margin: -1px !important;
  overflow: hidden !important;
  position: absolute !important;
  white-space: nowrap !important;
  width: 1px !important;
}
@media (prefers-color-scheme: light) {
  [data-zimantic-toolbar] {
    background: #fff !important;
    border-color: #dde1e7 !important;
    box-shadow: 0 1px 5px rgba(0, 0, 0, .12) !important;
    color: #1b1f24 !important;
  }
  [data-zimantic-toolbar] a,
  [data-zimantic-toolbar] button {
    border-color: #b8c0cc !important;
    color: #1f5fae !important;
  }
  [data-zimantic-toolbar] a:hover,
  [data-zimantic-toolbar] button:hover { background: #eef5ff !important; }
  [data-zimantic-toolbar] .zimantic-toolbar-separator { color: #5d6673 !important; }
}
</style>"""

ARTICLE_TOOLBAR_SCRIPT = """<script data-zimantic-toolbar>
(() => {
  const markModalRoot = () => {
    history.replaceState(
      Object.assign({}, history.state || {}, {zimanticModalRoot: true}),
      "",
      location.href
    );
  };
  window.addEventListener("message", event => {
    if (event.source !== window.parent) return;
    if (event.data?.type === "zimantic-modal-root") {
      markModalRoot();
    }
  });
  const initialize = () => {
    const toolbar = document.querySelector("nav[data-zimantic-toolbar]");
    if (!toolbar) return;
    const status = toolbar.querySelector("[data-zimantic-toolbar-status]");
    const home = toolbar.querySelector("[data-zimantic-home]");
    const permalink = toolbar.querySelector("[data-zimantic-permalink]");
    const actions = toolbar.querySelector(".zimantic-toolbar-actions");
    const announce = message => {
      if (status) status.textContent = message;
    };
    const fitToolbarLabels = () => {
      if (!actions) return;
      for (const control of [home, permalink]) {
        if (!control) continue;
        control.classList.remove("compact");
        const available = actions.getBoundingClientRect().left -
          control.getBoundingClientRect().left - 12;
        control.classList.toggle("compact", control.scrollWidth > available);
      }
    };
    toolbar.addEventListener("click", event => {
      const control = event.target.closest("[data-zimantic-action]");
      if (!control) return;
      const action = control.dataset.zimanticAction;
      if (action === "back") {
        if (window.parent !== window) {
          if (history.state?.zimanticModalRoot) {
            window.parent.postMessage({type: "zimantic-article-close"}, "*");
          } else {
            history.back();
          }
        } else if (history.length > 1) {
          history.back();
        } else {
          window.close();
        }
      } else if (action === "copy") {
        if (
          event.button !== 0 ||
          event.ctrlKey ||
          event.metaKey ||
          event.shiftKey ||
          event.altKey
        ) {
          return;
        }
        event.preventDefault();
        if (!navigator.clipboard || !navigator.clipboard.writeText) {
          announce("Copy unavailable");
        } else {
          navigator.clipboard.writeText(location.href).then(
            () => {
              control.textContent = "Link copied";
              announce("Link copied");
            },
            () => announce("Copy unavailable")
          );
        }
      } else if (action === "print") {
        window.print();
      }
    });
    fitToolbarLabels();
    window.addEventListener("resize", fitToolbarLabels);
    if (window.ResizeObserver) {
      new ResizeObserver(fitToolbarLabels).observe(toolbar);
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    initialize();
  }
})();
</script>"""


def _article_toolbar(zim: str, article: dict) -> str:
    zim_url = f"/article/{quote_path(zim, safe='')}"
    random_url = f"/random?zim={quote_path(zim, safe='')}"
    zim_name = html_escape(str(zim), quote=True)
    article_path = str(article.get("path") or "").strip("/")
    path_parts = [part for part in article_path.split("/") if part]
    permalink_url = (
        f"/article/{quote_path(zim, safe='')}/"
        f"{quote_path(article_path, safe='/')}"
        if article_path
        else zim_url
    )
    article_title = html_escape(
        str(article.get("title") or article_path or "Permalink"),
        quote=False,
    )
    breadcrumbs = []
    for depth in range(1, len(path_parts)):
        parent_path = "/".join(path_parts[:depth])
        parent_url = (
            f"/article/{quote_path(zim, safe='')}/"
            f"{quote_path(parent_path, safe='/')}"
        )
        breadcrumbs.append(
            f'<span class="zimantic-toolbar-separator" aria-hidden="true">/</span>'
            f'<a class="zimantic-toolbar-breadcrumb" href="{parent_url}" '
            f'title="Open {html_escape(parent_path, quote=True)}">'
            f'{html_escape(path_parts[depth - 1])}</a>'
        )
    breadcrumb_html = "".join(breadcrumbs) + (
        '<span class="zimantic-toolbar-separator" aria-hidden="true">/</span>'
        f'<a class="zimantic-toolbar-article" data-zimantic-permalink '
        f'data-zimantic-action="copy" href="{permalink_url}" '
        f'title="Open or copy this article\'s permalink">'
        f'<span class="zimantic-toolbar-article-full">{article_title}</span>'
        '<span class="zimantic-toolbar-article-short" aria-hidden="true">'
        "Permalink</span></a>"
    )
    return f"""<nav data-zimantic-toolbar aria-label="Article navigation">
  <div class="zimantic-toolbar-nav">
    <button type="button" data-zimantic-action="back" title="Go back or close this tab">← Back</button>
    <a href="{random_url}" title="Open a random article">Random</a>
    <a class="zimantic-toolbar-home" data-zimantic-home href="{zim_url}" title="Open this ZIM's homepage">
      <span class="zimantic-toolbar-home-full">{zim_name}</span>
      <span class="zimantic-toolbar-home-short" aria-hidden="true">Home</span>
    </a>
    <span class="zimantic-toolbar-breadcrumbs">{breadcrumb_html}</span>
  </div>
  <div class="zimantic-toolbar-actions">
    <button type="button" data-zimantic-action="print" title="Print this article">Print</button>
    <a href="/" title="Search your library">Zimantic</a>
  </div>
  <span class="zimantic-toolbar-status" data-zimantic-toolbar-status aria-live="polite"></span>
</nav>"""


def _decorate_html_article(content: bytes | memoryview, zim: str, article: dict) -> bytes:
    """Add navigation controls without changing non-HTML archive entries."""
    try:
        document = bytes(content).decode("utf-8")
    except UnicodeDecodeError:
        return content
    if "data-zimantic-toolbar" in document:
        return content

    toolbar = _article_toolbar(zim, article)
    head_close = re.search(r"</head\s*>", document, re.IGNORECASE)
    if head_close:
        position = head_close.start()
        document = (
            document[:position]
            + ARTICLE_TOOLBAR_STYLE
            + ARTICLE_TOOLBAR_SCRIPT
            + document[position:]
        )
    else:
        document = ARTICLE_TOOLBAR_STYLE + ARTICLE_TOOLBAR_SCRIPT + document

    body_open = re.search(r"<body\b[^>]*>", document, re.IGNORECASE)
    if body_open:
        position = body_open.end()
        document = document[:position] + toolbar + document[position:]
    else:
        document = toolbar + document
    return document.encode("utf-8")


def _article_content_response(content: bytes | memoryview, mimetype: str) -> Response:
    if len(content) > ARTICLE_STREAM_THRESHOLD:
        # Python-libzim exposes the decompressed item as one memoryview.
        # Chunking here avoids another whole-entry response copy and lets
        # the client consume large assets incrementally.
        def chunks():
            for start in range(0, len(content), ARTICLE_STREAM_CHUNK):
                yield bytes(content[start:start + ARTICLE_STREAM_CHUNK])

        return StreamingResponse(
            chunks(),
            media_type=mimetype,
            headers={
                "Cache-Control": "no-cache",
                "Content-Length": str(len(content)),
            },
        )
    return Response(
        content=content,
        media_type=mimetype,
        headers={"Cache-Control": "no-cache"},
    )


def _json_with_etag(request: Request, payload) -> Response:
    """JSON response with a content ETag, so a warm browser can revalidate
    source discovery cheaply with If-None-Match instead of re-downloading."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    etag = '"' + hashlib.sha1(body.encode("utf-8")).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def create_app(searchClass, debug: bool = False) -> FastAPI:
    app = FastAPI(title="zimantic")
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_SIZE)

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(PAGE)

    @app.get("/zims")
    def zims():
        return searchClass.local_names()

    @app.get("/config")
    def config(request: Request):
        return _json_with_etag(request, {
            "page_size": getattr(searchClass, "page_size", 10),
            "max_results": getattr(searchClass, "max_results", 100),
            "max_article_bytes": getattr(searchClass, "cfg", {}).get(
                "max_article_bytes", 33_554_432
            ),
            "debug": debug,
        })

    @app.get("/sources")
    def sources(request: Request):
        return _json_with_etag(request, searchClass.source_dicts())

    @app.get("/health")
    def health():
        health_report = getattr(searchClass, "health", None)
        if callable(health_report):
            return health_report()
        return {
            "status": "ok",
            "indexes": searchClass.local_names(),
            "sources": len(searchClass.source_dicts()),
            "cache": searchClass.cache.stats(),
        }

    def article_response(zim: str, path: str | None = None):
        try:
            article = searchClass.read_article(zim, path)
        except ArticleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ArticleTooLargeError as error:
            raise HTTPException(status_code=413, detail=str(error)) from error
        except ArticleUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

        requested_path = None if path is None else path.strip("/")
        if requested_path != article["path"]:
            location = (
                f"/article/{quote_path(zim, safe='')}/"
                f"{quote_path(article['path'], safe='/')}"
            )
            return Response(status_code=307, headers={"Location": location})
        content = article["content"]
        if article["mimetype"] == "text/html":
            content = _decorate_html_article(content, zim, article)
        return _article_content_response(content, article["mimetype"])

    @app.get("/article/{zim}")
    def article_homepage(zim: str):
        return article_response(zim)

    @app.get("/article/{zim}/{path:path}")
    def article(zim: str, path: str):
        return article_response(zim, path)

    @app.get("/random")
    def random_article(zim: str = Query(..., min_length=1)):
        choose_article = getattr(searchClass, "random_article_path", None)
        if not callable(choose_article):
            raise HTTPException(status_code=404, detail="random articles unavailable")
        try:
            path = choose_article(zim)
        except ArticleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ArticleUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        location = (
            f"/article/{quote_path(zim, safe='')}/"
            f"{quote_path(path, safe='/')}"
        )
        return Response(
            status_code=307,
            headers={"Location": location, "Cache-Control": "no-store"},
        )

    @app.get("/search")
    def api_search(
        q: str = Query(..., min_length=1, max_length=4096),
        zim: list[str] | None = Query(None),
        source: str | None = Query(None),
        limit: int | None = Query(None, ge=1),
        offset: int = Query(0, ge=0),
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

    @app.get("/search/stream")
    def api_search_stream(
        q: str = Query(..., min_length=1, max_length=4096),
        zim: list[str] | None = Query(None),
        source: str | None = Query(None),
        limit: int | None = Query(None, ge=1),
        offset: int = Query(0, ge=0),
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
        logger.warning("zimantic: reload already in progress; ignoring signal")
        return

    def _reload():
        try:
            summary = searchClass.reload()
            logger.info(
                "zimantic: reloaded "
                f"{len(summary.get('indexes', []))} index(es) "
                f"(+{len(summary.get('added', []))} new, "
                f"~{len(summary.get('upgraded', []))} upgraded, "
                f"-{len(summary.get('removed', []))} removed)"
            )
        except Exception as error:  # never let a reload take the process down
            logger.error("zimantic: reload failed: %s", error)
        finally:
            guard.release()

    threading.Thread(target=_reload, name="zimantic-reload", daemon=True).start()


def serve(searchClass, port: int, debug: bool = False) -> None:
    app = create_app(searchClass, debug=debug)
    try:
        PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError as error:
        logger.error("zimantic: could not write PID file %s: %s", PID_FILE, error)

    # Optional Kiwix catalog discovery runs in the background so an unreachable
    # Kiwix server cannot delay binding the port.
    start_catalog = getattr(searchClass, "start_catalog_refresh", None)
    if callable(start_catalog):
        start_catalog()

    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        guard = threading.Lock()
        signal.signal(sighup, lambda _signum, _frame: _handle_reload_request(searchClass, guard))

    logger.info(
        f"zimantic: server loaded at http://localhost:{port}\n"
        f"zimantic: PID {os.getpid()}; reload with `zimantic reload` "
    )
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass
