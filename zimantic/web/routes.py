"""HTTP route registration for the ZIM search application."""

import hashlib
import json

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from ..search import (
    SELECTIVE_SOURCE_LIMIT,
    ArticleNotFoundError,
    ArticleSourceNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
    SearchBusyError,
    SearchQueryError,
)
from ..searching.query import MAX_QUERY_LENGTH, STOPWORDS
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

        _, payload, _, full_etag = web_app._memoized_json(
            memo_key,
            version,
            payload_factory,
            memos,
        )
        total = len(payload)
        # Clamp to the last real page, like /search does. Without this, an
        # out-of-range offset returns an empty page and a misleading X-Offset.
        effective_offset = (
            min(offset, ((total - 1) // limit) * limit) if total else 0
        )
        page = payload[effective_offset : effective_offset + limit]
        # Do not memoize page slices: a client can request unlimited
        # offset/limit combinations. The full payload above is the expensive
        # part and remains memoized; serializing one page is cheap.
        body = json.dumps(
            page,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        etag = '"' + hashlib.sha1(
            f"{full_etag}:{effective_offset}:{limit}".encode()
        ).hexdigest() + '"'
        headers = {
            "ETag": etag,
            "Cache-Control": "no-cache",
            "X-Total-Count": str(total),
            "X-Offset": str(effective_offset),
            "X-Has-More": str(
                effective_offset + len(page) < total
            ).lower(),
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(
            content=body,
            media_type="application/json",
            headers=headers,
        )

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(web_app.PAGE, headers=dev_cache_headers)

    @app.get("/library", include_in_schema=False)
    def library_page():
        return FileResponse(web_app.PAGE, headers=dev_cache_headers)

    static_routes = (
        ("/app.css", web_app.APP_CSS, "text/css"),
        ("/app.js", web_app.APP_JS, "application/javascript"),
        ("/indexeddb-shim.js", web_app.INDEXEDDB_SHIM, "application/javascript"),
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

    @app.get("/config")
    def config(request: Request):
        return web_app._json_response(
            request,
            "config",
            1 if debug else 0,
            lambda: {
                "page_size": getattr(search_class, "page_size", 10),
                "max_results": getattr(search_class, "max_results", 100),
                "max_query_length": MAX_QUERY_LENGTH,
                "stopwords": sorted(STOPWORDS),
                "source_filter_limit": SELECTIVE_SOURCE_LIMIT,
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

            def library_of():
                return [
                    source
                    for source in search_class.source_dicts()
                    if source.get("mode") == "local"
                    and (
                        not source.get("available", True)
                        or source.get("homepage")
                    )
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

    def requested_highlight(request: Request) -> str | None:
        value = (request.query_params.get("highlight") or "").strip()
        return value[:4096] or None

    def raw_article_response(
        zim: str, path: str | None = None, highlight: str | None = None
    ):
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
                    "Location": web_app._append_highlight(
                        web_app._article_url(zim, article["path"], base_path),
                        highlight,
                    )
                },
            )
        content = article["content"]
        if article["mimetype"] in web_app.HTML_MIMETYPES:
            content = web_app._decorate_article_bridge(
                content,
                zim,
                base_path,
                xhtml=article["mimetype"] == "application/xhtml+xml",
            )
        return web_app._article_content_response(content, article["mimetype"])

    def error_shell_article(
        requested_path: str | None,
        error: Exception,
        zim: str,
    ) -> dict:
        safe_path = "/".join(
            part
            for part in (requested_path or "").split("/")
            if part and part not in {".", ".."}
        )
        # The source itself is missing: there is no home/random link to offer.
        # Any other not-found error still belongs to an existing source.
        zim_found = not isinstance(error, ArticleSourceNotFoundError) and (
            not isinstance(error, ArticleUnavailableError)
            or isinstance(error, ArticleTooLargeError)
        )
        if isinstance(error, ArticleTooLargeError):
            title = "Article too large"
            detail = "This article is too large to display in the offline viewer."
        elif isinstance(error, ArticleUnavailableError):
            title = "Source unavailable"
            detail = "This offline source is not available right now."
        elif isinstance(error, ArticleSourceNotFoundError):
            title = "Source not found"
            detail = "This offline source is not installed on this device."
        elif str(error) == "invalid article path":
            title = "Article not found"
            detail = "The requested path is not valid."
        else:
            title = "Article not found"
            detail = "This page is not available in the offline archive."
        return {
            "path": safe_path,
            "title": title,
            "message": detail,
            "zim": zim,
            "zim_found": zim_found,
        }

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

    def content_shell_response(
        zim: str, path: str | None = None, highlight: str | None = None
    ):
        requested_path = None if path is None else path
        fallback_icon = zim_icon_url(zim)
        read_head = getattr(search_class, "read_article_head", None)
        check_path = getattr(search_class, "article_path_exists", None)

        def path_exists(parent_path: str) -> bool:
            """True when the path is a real entry, so breadcrumbs do not link to containers."""
            try:
                if callable(check_path):
                    return bool(check_path(zim, parent_path))
                if callable(read_head):
                    read_head(zim, parent_path)
                else:
                    search_class.read_article(zim, parent_path)
                return True
            except (
                ArticleNotFoundError,
                ArticleTooLargeError,
                ArticleUnavailableError,
            ):
                return False

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
            error_info = error_shell_article(requested_path, error, zim)
            return web_app._content_shell(
                zim,
                error_info,
                web_app._article_http_error(error).status_code,
                fallback_icon,
                base_path,
                highlight,
                path_exists=path_exists,
                error=error_info,
            )

        if requested_path != article["path"]:
            return Response(
                status_code=307,
                headers={
                    "Location": web_app._append_highlight(
                        web_app._content_url(zim, article["path"], base_path),
                        highlight,
                    )
                },
            )
        return web_app._content_shell(
            zim,
            article,
            fallback_icon=fallback_icon,
            base_path=base_path,
            highlight=highlight,
            path_exists=path_exists,
        )

    @app.get("/content/{zim}")
    def content_homepage(zim: str, request: Request):
        return content_shell_response(zim, highlight=requested_highlight(request))

    @app.get("/content/{zim}/{path:path}")
    def content_article(zim: str, path: str, request: Request):
        return content_shell_response(
            zim,
            path,
            highlight=requested_highlight(request),
        )

    @app.get("/article/{zim}")
    def article_homepage(zim: str, request: Request):
        return raw_article_response(zim, highlight=requested_highlight(request))

    @app.get("/article/{zim}/{path:path}")
    def article(zim: str, path: str, request: Request):
        return raw_article_response(
            zim,
            path,
            highlight=requested_highlight(request),
        )

    @app.get("/random")
    def random_article(zim: str | None = Query(None, min_length=1)):
        if not zim:
            choose_source = getattr(search_class, "random_local_source", None)
            if not callable(choose_source):
                raise HTTPException(
                    status_code=404, detail="random sources unavailable"
                )
            try:
                source = choose_source()
            except ArticleNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            return Response(
                status_code=307,
                headers={
                    "Location": web_app._content_url(
                        source.local_name or source.book, base_path=base_path
                    ),
                    "Cache-Control": "no-store",
                },
            )

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
            "X-Page": str(page["page"]),
            "X-Page-Count": str(page["page_count"]),
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
