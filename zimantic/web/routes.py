"""HTTP route registration for the ZIM search application."""

import json

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from ..search import (
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
    SearchBusyError,
    SearchQueryError,
)
from . import app as web_app


def register_routes(app: FastAPI, search_class, debug: bool = False) -> None:
    """Register browser, article, discovery, and search endpoints."""

    # Serialized-JSON memo owned by this app instance (see _json_response).
    memos: dict = {}
    base_path = web_app._search_base_path(search_class)
    dev_cache_headers = {"Cache-Control": "no-store"} if debug else None

    def paged_json_response(
        request: Request,
        memo_key: str,
        version,
        payload_factory,
        memos,
        limit: int | None,
        offset: int,
    ):
        if limit is None:
            response = web_app._json_response(
                request,
                memo_key,
                version,
                payload_factory,
                memos,
            )
            return response

        _, payload, _, _ = web_app._memoized_json(
            memo_key,
            version,
            payload_factory,
            memos,
        )
        page = payload[offset : offset + limit]
        response = web_app._json_response(
            request,
            f"{memo_key}:page:{offset}:{limit}",
            version,
            lambda: page,
            memos,
        )
        response.headers["X-Total-Count"] = str(len(payload))
        response.headers["X-Offset"] = str(offset)
        response.headers["X-Has-More"] = str(offset + len(page) < len(payload)).lower()
        return response

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(web_app.PAGE, headers=dev_cache_headers)

    @app.get("/library", include_in_schema=False)
    def library_page():
        return FileResponse(web_app.PAGE, headers=dev_cache_headers)

    static_routes = (
        ("/app.css", web_app.APP_CSS, "text/css"),
        ("/app.js", web_app.APP_JS, "application/javascript"),
        ("/favicon.ico", web_app.FAVICON, "image/x-icon"),
        ("/favicon-16x16.png", web_app.FAVICON_16, "image/png"),
        ("/favicon-32x32.png", web_app.FAVICON_32, "image/png"),
        ("/apple-touch-icon.png", web_app.APPLE_TOUCH_ICON, "image/png"),
        ("/android-chrome-192x192.png", web_app.ANDROID_CHROME_192, "image/png"),
        ("/android-chrome-512x512.png", web_app.ANDROID_CHROME_512, "image/png"),
        ("/site.webmanifest", web_app.WEB_MANIFEST, "application/manifest+json"),
    )
    for route, file, mime in static_routes:
        app.get(route, include_in_schema=False)(
            lambda file=file, mime=mime: FileResponse(
                file,
                media_type=mime,
                headers=dev_cache_headers,
            )
        )

    @app.get("/source-icon/{zim}", include_in_schema=False)
    def source_icon(zim: str):
        try:
            icon = search_class.read_source_icon(zim)
        except (
            ArticleNotFoundError,
            ArticleTooLargeError,
            ArticleUnavailableError,
        ) as error:
            raise web_app._article_http_error(error) from error
        return web_app._article_content_response(icon["content"], icon["mimetype"])

    @app.get("/zims")
    def zims():
        return search_class.local_names()

    @app.get("/config")
    def config(request: Request):
        return web_app._json_response(
            request,
            "config",
            1 if debug else 0,
            lambda: {
                "page_size": getattr(search_class, "page_size", 10),
                "debug": debug,
            },
            memos,
        )

    @app.get("/sources")
    def sources(
        request: Request,
        limit: int | None = Query(default=None, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ):
        version_of = getattr(search_class, "source_version", None)
        version = version_of() if callable(version_of) else 0
        return paged_json_response(
            request,
            "sources",
            version,
            search_class.source_dicts,
            memos,
            limit,
            offset,
        )

    @app.get("/library.json")
    def library(
        request: Request,
        limit: int | None = Query(default=None, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ):
        version_of = getattr(search_class, "library_version", None)
        version = version_of() if callable(version_of) else 0
        library_of = getattr(search_class, "library_dicts", None)
        if not callable(library_of):
            library_of = lambda: [
                source
                for source in search_class.source_dicts()
                if (
                    source.get("mode") == "local"
                    and source.get("available", True)
                    and source.get("homepage")
                )
            ]
        else:
            original_library_of = library_of

            def library_of():
                return [
                    source
                    for source in original_library_of()
                    if source.get("available", True) and source.get("homepage")
                ]

        return paged_json_response(
            request,
            "library",
            version,
            library_of,
            memos,
            limit,
            offset,
        )

    @app.get("/health")
    def health():
        health_report = getattr(search_class, "health", None)
        if callable(health_report):
            return health_report()
        return {
            "status": "ok",
            "indexes": search_class.local_names(),
            "sources": len(search_class.source_dicts()),
            "cache": search_class.cache.stats(),
        }

    def raw_article_response(zim: str, path: str | None = None):
        try:
            article = search_class.read_article(zim, path)
        except (
            ArticleNotFoundError,
            ArticleTooLargeError,
            ArticleUnavailableError,
        ) as error:
            raise web_app._article_http_error(error) from error

        requested_path = None if path is None else path
        if requested_path != article["path"]:
            return Response(
                status_code=307,
                headers={
                    "Location": web_app._article_url(zim, article["path"], base_path)
                },
            )
        content = article["content"]
        if article["mimetype"] == "text/html":
            content = web_app._decorate_article_bridge(content, zim, base_path)
        return web_app._article_content_response(content, article["mimetype"])

    def error_shell_article(requested_path: str | None) -> dict:
        safe_path = "/".join(
            part
            for part in (requested_path or "").split("/")
            if part and part not in {".", ".."}
        )
        return {"path": safe_path, "title": safe_path or "Article"}

    def zim_icon_url(zim: str) -> str | None:
        """Return the ZIM favicon used when an article declares none."""
        try:
            sources = search_class.source_dicts()
        except Exception:
            return None
        for source in sources:
            if not isinstance(source, dict) or source.get("local_name") != zim:
                continue
            icon_url = source.get("icon_url")
            if isinstance(icon_url, str) and icon_url:
                return icon_url
        return None

    def content_shell_response(zim: str, path: str | None = None):
        requested_path = None if path is None else path
        fallback_icon = zim_icon_url(zim)
        read_head = getattr(search_class, "read_article_head", None)
        try:
            if callable(read_head):
                article = read_head(zim, path)
            else:
                article = search_class.read_article(zim, path)
        except (
            ArticleNotFoundError,
            ArticleTooLargeError,
            ArticleUnavailableError,
        ) as error:
            return web_app._content_shell(
                zim,
                error_shell_article(requested_path),
                web_app._ARTICLE_ERROR_STATUS[type(error)],
                fallback_icon,
                base_path,
            )

        if requested_path != article["path"]:
            return Response(
                status_code=307,
                headers={
                    "Location": web_app._content_url(zim, article["path"], base_path)
                },
            )
        return web_app._content_shell(
            zim,
            article,
            fallback_icon=fallback_icon,
            base_path=base_path,
        )

    @app.get("/content/{zim}")
    def content_homepage(zim: str):
        return content_shell_response(zim)

    @app.get("/content/{zim}/{path:path}")
    def content_article(zim: str, path: str):
        return content_shell_response(zim, path)

    @app.get("/article/{zim}")
    def article_homepage(zim: str):
        return raw_article_response(zim)

    @app.get("/article/{zim}/{path:path}")
    def article(zim: str, path: str):
        return raw_article_response(zim, path)

    @app.get("/random")
    def random_article(zim: str = Query(..., min_length=1)):
        choose_article = getattr(search_class, "random_article_path", None)
        if not callable(choose_article):
            raise HTTPException(status_code=404, detail="random articles unavailable")
        try:
            path = choose_article(zim)
        except ArticleNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ArticleUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return Response(
            status_code=307,
            headers={
                "Location": web_app._content_url(zim, path, base_path),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/search")
    def api_search(
        q: str = Query(..., min_length=1, max_length=4096),
        zim: list[str] | None = Query(None),
        source: list[str] | None = Query(None),
        limit: int | None = Query(None, ge=1),
        offset: int = Query(0, ge=0),
    ):
        source_filter = source[0] if source and len(source) == 1 else source
        try:
            page = search_class.search_page(q, zim, limit, offset, source_filter, debug)
        except SearchBusyError as error:
            raise HTTPException(
                status_code=503,
                detail=str(error),
                headers={"Retry-After": "1"},
            ) from error
        except SearchQueryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        headers = {
            "X-Total-Count": str(page["total"]),
            "X-Has-More": "true" if page["has_more"] else "false",
            "X-Offset": str(page["offset"]),
            "X-Page-Size": str(len(page["results"])),
        }
        if debug and page.get("matched_by") is not None:
            headers["X-Matched-By"] = json.dumps(
                page["matched_by"],
                separators=(",", ":"),
                ensure_ascii=True,
            )
        return JSONResponse(content=page["results"], headers=headers)

    @app.get("/search/stream")
    def api_search_stream(
        q: str = Query(..., min_length=1, max_length=4096),
        zim: list[str] | None = Query(None),
        source: list[str] | None = Query(None),
        limit: int | None = Query(None, ge=1),
        offset: int = Query(0, ge=0),
    ):
        try:
            search_class._validate_query(q)
        except SearchQueryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        def events():
            source_filter = source[0] if source and len(source) == 1 else source
            stream = search_class.stream_search(
                q,
                zim,
                limit,
                offset,
                source_filter,
                debug,
            )
            try:
                for event in stream:
                    yield json.dumps(event, separators=(",", ":")) + "\n"
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={
                "Content-Encoding": "identity",
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )
