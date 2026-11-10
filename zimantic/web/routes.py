"""HTTP route registration for the ZIM search application."""

import hashlib
import json
import re
from typing import Annotated, Literal
from urllib.parse import unquote, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.background import BackgroundTask

from ..content.path import has_encoded_separator, raw_request_path
from ..content.store import (
    ArticleNotFoundError,
    ArticleSourceNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
)
from ..indexing.browse import BROWSE_MODES, SEMANTIC_BROWSE_MODES
from ..providers.config import BUILTIN_PROVIDERS
from ..search import (
    DEFAULT_SOURCE_FILTER_LIMIT,
    SearchBusyError,
    SearchQueryError,
)
from ..searching.query import MAX_QUERY_LENGTH, STOPWORDS
from . import app as web_app
from .listing import (
    filter_collections,
    filter_sources,
    validate_collection,
    validate_language,
    validate_media_type,
    validate_provider,
)

_ARTICLE_METHODS = "GET, HEAD, OPTIONS"
_ARTICLE_REQUEST_HEADERS = (
    "Range, If-Range, If-None-Match, If-Modified-Since"
)
_RANGE_RE = re.compile(
    r"^\s*bytes\s*=\s*(?P<start>\d*)\s*-\s*(?P<end>\d*)\s*$",
    re.IGNORECASE,
)
_DEFAULT_CONTENT_LIMIT = 1 << 30


_CAPTURE_ASSET_PREFIXES = (
    "w/",
    "assets/",
    "static/",
    "images/",
    "skins/",
    "extensions/",
    "resources/",
    "wiki/",
)


def _capture_asset_candidates(path: str) -> list[str]:
    """Return archive paths worth trying for a root-absolute capture asset."""
    # The route parameter is already decoded exactly once by the ASGI server.
    # Decoding again would turn a literal %2F filename into a traversal step.
    rel = str(path or "").lstrip("/")
    if not rel:
        return []
    if not (
        rel == "load.php"
        or any(rel.startswith(prefix) for prefix in _CAPTURE_ASSET_PREFIXES)
    ):
        return []
    candidates: list[str] = []

    def add(value: str) -> None:
        value = value.lstrip("/")
        if value and value not in candidates:
            candidates.append(value)

    add(rel)
    # mwoffliner captures keep MediaWiki resources under -/mw/. CMS pages often
    # emit root-absolute /w/images/... URLs with an extra hash directory while
    # the archive stores the file by basename.
    if rel.startswith("w/images/"):
        add("-/mw/" + rel.rsplit("/", 1)[-1])
    if rel.startswith("w/"):
        add("-/mw/" + rel[len("w/") :])
    if rel.startswith("images/"):
        add("-/mw/" + rel.rsplit("/", 1)[-1])
    if rel.startswith("assets/"):
        add("-/assets/" + rel[len("assets/") :])
    if rel.startswith("static/"):
        add("-/static/" + rel[len("static/") :])
    if rel.startswith(("skins/", "extensions/", "resources/")):
        add("-/mw/" + rel)
    return candidates


def _referer_source_key(referer: str | None) -> str | None:
    """Extract the canonical source key from an article/content referrer."""
    if not referer:
        return None
    path = urlsplit(referer).path
    for marker in ("/article/", "/content/"):
        index = path.find(marker)
        if index < 0:
            continue
        remainder = path[index + len(marker) :]
        key = remainder.split("/", 1)[0]
        # The referrer preserves the encoded source segment.  Decode it exactly
        # once so keys with colons match the canonical SourceInfo.key.
        return unquote(key) if key else None
    return None


def _raw_path_is_safe(request: Request) -> bool:
    """Reject encoded separators before the decoded route path is trusted.

    A trustworthy ASGI server preserves ``raw_path``.  During the transition to
    a reverse-proxy deployment that strips it, path-bearing content requests
    fail closed rather than guessing: a decoded path cannot reveal whether a
    separator was encoded.
    """
    raw_path = raw_request_path(request)
    if raw_path is None:
        return False
    return not has_encoded_separator(raw_path)


def _content_error_code(error: Exception) -> tuple[int, str]:
    if isinstance(error, ArticleTooLargeError):
        return 413, "article_too_large"
    if isinstance(error, ArticleSourceNotFoundError):
        return 404, "source_not_found"
    if isinstance(error, ArticleUnavailableError):
        return 503, "store_unavailable"
    if isinstance(error, ArticleNotFoundError):
        return 404, "not_found"
    return 500, "content_error"


def _article_error_response(
    error: Exception, source_key: str | None
) -> JSONResponse:
    status, code = _content_error_code(error)
    payload: dict[str, str] = {"error": code}
    if source_key:
        payload["source"] = source_key
    headers = dict(web_app.ARTICLE_CORS_HEADERS)
    if status == 503:
        headers["Retry-After"] = "1"
    return JSONResponse(
        status_code=status,
        content=payload,
        headers=headers,
    )


def _if_none_match_star(value: str | None) -> bool:
    if not value:
        return False
    return any(part.strip() == "*" for part in value.split(","))


def _parse_range(
    value: str | None, total: int | None
) -> tuple[int, int, bool] | None:
    """Parse one satisfiable range, or return None to serve the full body.

    ``None`` covers malformed/multiple ranges, unknown totals, and values the
    plan says to ignore.  A syntactically valid range that starts at or past the
    representation is returned as ``(-1, -1, False)`` so the caller can send a
    416 with the total length.
    """
    if not value or total is None:
        return None
    match = _RANGE_RE.match(value)
    if match is None:
        return None
    start_text = match.group("start")
    end_text = match.group("end")
    if not start_text and not end_text:
        return None
    total = int(total)
    if total <= 0:
        return (-1, -1, False)
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            return (-1, -1, False)
        start = max(0, total - suffix)
        end = total - 1
        return start, end, True
    start = int(start_text)
    if end_text and int(end_text) < start:
        return None
    if start >= total:
        return (-1, -1, False)
    end = total - 1 if not end_text else int(end_text)
    return start, min(end, total - 1), False


def _bounded_range(
    start: int, end: int, suffix: bool, max_bytes: int
) -> tuple[int, int]:
    """Clamp a requested range to the configured returned-slice limit."""
    limit = max(0, int(max_bytes))
    length = end - start + 1
    if length <= limit:
        return start, end
    if suffix:
        # A suffix request keeps the end of the representation.
        return end - limit + 1, end
    return start, start + limit - 1


def register_routes(app: FastAPI, search_class, debug: bool = False) -> None:
    """Register browser, article, discovery, and search endpoints."""

    # Serialized-JSON memo owned by this app instance (see _json_response).
    memos: dict = {}
    base_path = web_app._search_base_path(search_class)
    cfg = getattr(search_class, "cfg", {}) or {}
    secure_browsing = bool(cfg.get("secure_browsing", False))

    def _snapshot_version() -> str:
        token_of = getattr(search_class, "collection_version", None)
        if not callable(token_of):
            token_of = getattr(search_class, "snapshot_token", None)
        if callable(token_of):
            token = token_of()
            return f"{token[0]}:{token[1]}"
        version_of = getattr(search_class, "source_version", None)
        version = version_of() if callable(version_of) else 0
        return str(version)

    def _capture_listing() -> dict:
        capture = getattr(search_class, "listing_snapshot", None)
        if callable(capture):
            return capture()
        source_of = getattr(search_class, "source_dicts", None)
        library_of = getattr(search_class, "library_dicts", None)
        collection_of = getattr(search_class, "collection_dicts", None)
        token_of = getattr(search_class, "collection_version", None)
        if not callable(token_of):
            token_of = getattr(search_class, "snapshot_token", None)
        token = token_of() if callable(token_of) else ("", 0)
        return {
            "sources": source_of() if callable(source_of) else [],
            "library": (
                library_of() if callable(library_of) else (
                    source_of() if callable(source_of) else []
                )
            ),
            "collections": collection_of() if callable(collection_of) else [],
            "token": token,
        }

    def _token_string(token) -> str:
        try:
            return f"{token[0]}:{token[1]}"
        except (TypeError, IndexError, KeyError):
            return str(token)

    def paged_json_response(
        request: Request,
        memo_key: str,
        version,
        payload_factory,
        memos,
        limit: int | None,
        offset: int,
        *,
        snapshot: str | None = None,
        cacheable: bool = True,
    ):
        snapshot = _snapshot_version() if snapshot is None else snapshot
        if limit is None:
            if cacheable:
                response = web_app._json_response(
                    request,
                    memo_key,
                    version,
                    payload_factory,
                    memos,
                )
            else:
                payload = payload_factory()
                body = json.dumps(
                    payload,
                    separators=(",", ":"),
                    sort_keys=True,
                    default=str,
                )
                etag = '"' + hashlib.sha1(body.encode("utf-8")).hexdigest() + '"'
                if request.headers.get("if-none-match") == etag:
                    return Response(
                        status_code=304,
                        headers={
                            "ETag": etag,
                            "Cache-Control": "no-cache",
                            "X-Snapshot-Version": snapshot,
                        },
                    )
                return Response(
                    content=body,
                    media_type="application/json",
                    headers={
                        "ETag": etag,
                        "Cache-Control": "no-cache",
                        "X-Snapshot-Version": snapshot,
                    },
                )
            response.headers["X-Snapshot-Version"] = snapshot
            return response

        if cacheable:
            _, payload, _, full_etag = web_app._memoized_json(
                memo_key,
                version,
                payload_factory,
                memos,
            )
        else:
            # Arbitrary filter combinations must not grow an unbounded memo.
            payload = payload_factory()
            body = json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            )
            full_etag = '"' + hashlib.sha1(body.encode("utf-8")).hexdigest() + '"'
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
            "X-Snapshot-Version": snapshot,
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
    def page(request: Request):
        return web_app.page_response(request, debug=debug)

    @app.get("/library", include_in_schema=False)
    def library_page(request: Request):
        return web_app.page_response(request, debug=debug)

    static_routes = (
        ("/app.css", web_app.APP_CSS, "text/css", True),
        ("/app.js", web_app.APP_JS, "application/javascript", True),
        ("/i18n.js", web_app.I18N, "application/javascript", True),
        (
            "/indexeddb-shim.js",
            web_app.INDEXEDDB_SHIM,
            "application/javascript",
            False,
        ),
        ("/favicon.ico", web_app.FAVICON, "image/x-icon", False),
        ("/favicon.svg", web_app.FAVICON_SVG, "image/svg+xml", False),
        ("/favicon-16x16.png", web_app.FAVICON_16, "image/png", False),
        ("/favicon-32x32.png", web_app.FAVICON_32, "image/png", False),
        ("/apple-touch-icon.png", web_app.APPLE_TOUCH_ICON, "image/png", False),
        (
            "/android-chrome-192x192.png",
            web_app.ANDROID_CHROME_192,
            "image/png",
            False,
        ),
        (
            "/android-chrome-512x512.png",
            web_app.ANDROID_CHROME_512,
            "image/png",
            False,
        ),
        (
            "/site.webmanifest",
            web_app.WEB_MANIFEST,
            "application/manifest+json",
            False,
        ),
    )
    for route, file, mime, versioned in static_routes:

        def static_asset(
            request: Request,
            file=file,
            mime=mime,
            versioned=versioned,
        ):
            return web_app.static_response(
                request,
                file,
                mime,
                versioned=versioned,
                debug=debug,
            )

        app.get(route, include_in_schema=False)(static_asset)

    @app.get("/source-icon/{source}", include_in_schema=False)
    def source_icon(source: str, request: Request):
        if not _raw_path_is_safe(request):
            return _article_error_response(
                ArticleNotFoundError("invalid article path"), source
            )
        try:
            icon = search_class.read_source_icon(source)
        except (
            ArticleNotFoundError,
            ArticleTooLargeError,
            ArticleUnavailableError,
        ) as error:
            return _article_error_response(error, source)
        if isinstance(icon, dict):
            content = icon.get("content") or b""
            mimetype = icon.get("mimetype") or "application/octet-stream"
        else:
            content = icon.content
            mimetype = icon.mimetype
        return web_app._article_content_response(content, mimetype)

    @app.get("/config")
    def config(request: Request):
        source_filter_limit = int(
            getattr(search_class, "source_filter_limit", DEFAULT_SOURCE_FILTER_LIMIT)
        )
        return web_app._json_response(
            request,
            "config",
            1 if debug else 0,
            lambda: {
                "page_size": getattr(search_class, "page_size", 10),
                "max_results": getattr(search_class, "max_results", 100),
                "max_query_length": MAX_QUERY_LENGTH,
                "stopwords": sorted(STOPWORDS),
                "source_filter_limit": source_filter_limit,
                "collection_source_limit": int(
                    getattr(
                        search_class,
                        "collection_source_limit",
                        1000,
                    )
                ),
                "max_explicit_collection_sources": int(
                    getattr(search_class, "max_explicit_collection_sources", 500)
                ),
                "providers": list(BUILTIN_PROVIDERS),
                "browse_modes": [
                    mode
                    for mode in BROWSE_MODES
                    if mode not in SEMANTIC_BROWSE_MODES
                    or getattr(search_class, "semantic", False)
                ],
                "debug": debug,
            },
            memos,
        )

    def _known_collections(snapshot: dict) -> set[str]:
        known = {
            str(record.get("key"))
            for record in snapshot.get("collections") or []
            if record.get("key")
        }
        for record in [
            *(snapshot.get("sources") or []),
            *(snapshot.get("library") or []),
        ]:
            collection = record.get("collection")
            if isinstance(collection, dict) and collection.get("key"):
                known.add(str(collection["key"]))
        return known

    def _validate_listing_filters(
        snapshot: dict,
        provider: str | None,
        media_type: str | None,
        language: str | None,
        collection: str | None,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        try:
            return (
                validate_provider(provider),
                validate_media_type(media_type),
                validate_language(language),
                validate_collection(collection, _known_collections(snapshot)),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    def _filtered_page(
        request: Request,
        snapshot: dict,
        records,
        *,
        providers_filter: str | None,
        media_type: str | None,
        language: str | None,
        collection: str | None,
        query: str | None,
        limit: int | None,
        offset: int,
        memo_key: str,
        version,
        filter_sources_fn,
        max_page_size: int = 200,
    ):
        snapshot_token = _token_string(
            snapshot.get("token") or _snapshot_version()
        )
        order_of = getattr(search_class, "source_order", None)
        source_order = order_of() if callable(order_of) else None
        filtered = filter_sources_fn(
            records,
            provider=providers_filter,
            media_type=media_type,
            language=language,
            collection=collection,
            query=query,
            collections=snapshot.get("collections") or (),
            order=source_order,
        )
        cacheable = not any(
            (providers_filter, media_type, language, collection, query)
        )
        default_page = int(getattr(search_class, "page_size", 10) or 10)
        return paged_json_response(
            request,
            memo_key,
            (snapshot_token, version),
            lambda: filtered,
            memos,
            min(limit or default_page, max_page_size),
            offset,
            snapshot=snapshot_token,
            cacheable=cacheable,
        )

    @app.get("/collections.json")
    def collections(
        request: Request,
        provider: str | None = Query(default=None),
        media_type: str | None = Query(default=None),
        language: str | None = Query(default=None, max_length=64),
        q: str | None = Query(default=None, max_length=4096),
        limit: int | None = Query(default=None, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        snapshot = _capture_listing()
        provider_filter, media_filter, language_filter, _collection = (
            _validate_listing_filters(snapshot, provider, media_type, language, None)
        )
        members = snapshot.get("library") or snapshot.get("sources") or []
        snapshot_token = _token_string(
            snapshot.get("token") or _snapshot_version()
        )
        filtered = filter_collections(
            snapshot.get("collections") or [],
            members,
            provider=provider_filter,
            media_type=media_filter,
            language=language_filter,
            query=q or None,
        )
        cacheable = not any((provider_filter, media_filter, language_filter, q))
        default_page = int(getattr(search_class, "page_size", 10) or 10)
        return paged_json_response(
            request,
            "collections",
            snapshot_token,
            lambda: filtered,
            memos,
            min(limit or default_page, 200),
            offset,
            snapshot=snapshot_token,
            cacheable=cacheable,
        )

    @app.get("/sources")
    def sources(
        request: Request,
        provider: str | None = Query(default=None),
        media_type: str | None = Query(default=None),
        language: str | None = Query(default=None, max_length=64),
        collection: str | None = Query(default=None),
        q: str | None = Query(default=None, max_length=4096),
        limit: int | None = Query(default=None, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        snapshot = _capture_listing()
        provider_filter, media_filter, language_filter, collection_filter = (
            _validate_listing_filters(snapshot, provider, media_type, language, collection)
        )
        version_of = getattr(search_class, "source_version", None)
        version = version_of() if callable(version_of) else 0
        return _filtered_page(
            request,
            snapshot,
            snapshot.get("sources") or [],
            providers_filter=provider_filter,
            media_type=media_filter,
            language=language_filter,
            collection=collection_filter,
            query=q or None,
            limit=limit,
            offset=offset,
            memo_key="sources",
            version=version,
            filter_sources_fn=filter_sources,
        )

    @app.get("/library.json")
    def library(
        request: Request,
        provider: str | None = Query(default=None),
        media_type: str | None = Query(default=None),
        language: str | None = Query(default=None, max_length=64),
        collection: str | None = Query(default=None),
        q: str | None = Query(default=None, max_length=4096),
        limit: int | None = Query(default=None, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ):
        snapshot = _capture_listing()
        provider_filter, media_filter, language_filter, collection_filter = (
            _validate_listing_filters(snapshot, provider, media_type, language, collection)
        )
        version_of = getattr(search_class, "library_version", None)
        version = version_of() if callable(version_of) else 0
        return _filtered_page(
            request,
            snapshot,
            snapshot.get("library") or [],
            providers_filter=provider_filter,
            media_type=media_filter,
            language=language_filter,
            collection=collection_filter,
            query=q or None,
            limit=limit,
            offset=offset,
            memo_key="library",
            version=version,
            filter_sources_fn=filter_sources,
            max_page_size=1000,
        )

    @app.get("/health")
    def health(request: Request):
        health_report = getattr(search_class, "health", None)
        if callable(health_report):
            version_of = getattr(search_class, "health_version", None)
            version = version_of() if callable(version_of) else 0
            return web_app._json_response(
                request,
                "health",
                version,
                health_report,
                memos,
            )
        return {
            "status": "ok",
            "indexes": search_class.source_keys(),
            "sources": len(search_class.source_dicts()),
            "cache": search_class.cache.stats(),
        }

    def requested_highlight(request: Request) -> str | None:
        value = (request.query_params.get("highlight") or "").strip()
        return value[:4096] or None

    def _article_limit() -> int:
        return int(
            getattr(search_class, "max_article_bytes", _DEFAULT_CONTENT_LIMIT)
        )

    def _redirect_article(
        route: str,
        source_key: str,
        path: str,
        highlight: str | None,
        request: Request | None = None,
    ) -> Response:
        target = (
            web_app._article_url(source_key, path, base_path)
            if route == "article"
            else web_app._content_url(source_key, path, base_path)
        )
        if request is not None:
            extras = [
                (name, value)
                for name, value in request.query_params.multi_items()
                if name != "highlight"
            ]
            if extras:
                separator = "&" if "?" in target else "?"
                target += separator + urlencode(extras, doseq=True)
        return Response(
            status_code=307,
            headers={
                "Location": web_app._append_highlight(target, highlight),
                "Cache-Control": "no-cache",
                **web_app.ARTICLE_CORS_HEADERS,
            },
        )

    def _decorated_article(
        content: bytes, source_key: str, mimetype: str
    ) -> bytes:
        return web_app._decorate_article_bridge(
            content,
            source_key,
            base_path,
            xhtml=mimetype == "application/xhtml+xml",
        )

    def raw_article_response(
        source: str,
        request: Request,
        path: str | None = None,
        highlight: str | None = None,
    ):
        store = None
        try:
            opener = getattr(search_class, "content_store", None)
            if not callable(opener):
                article = search_class.read_article(source, path)
                requested_path = None if path is None else path
                article_path = _article_path(article)
                if requested_path != article_path:
                    return _redirect_article(
                        "article", source, article_path, highlight, request
                    )
                content = _article_content(article)
                mimetype = web_app.browser_mimetype(_article_mimetype(article))
                if mimetype in web_app.HTML_MIMETYPES:
                    content = _decorated_article(content, source, mimetype)
                if request.method == "HEAD":
                    return Response(
                        content=b"",
                        media_type=mimetype,
                        headers={
                            "Cache-Control": "no-cache",
                            "Content-Length": str(len(content)),
                            "X-Content-Type-Options": "nosniff",
                            **web_app.ARTICLE_CORS_HEADERS,
                        },
                    )
                return web_app._article_content_response(
                    content, mimetype, filename=article_path
                )

            store = opener(source)
            entry = store.resolve(path)

            requested_path = None if path is None else path
            if (requested_path or "") != entry.path:
                return _redirect_article(
                    "article", source, entry.path, highlight, request
                )

            if _if_none_match_star(request.headers.get("if-none-match")):
                return Response(
                    status_code=304,
                    headers={
                        "Cache-Control": "no-cache",
                        **web_app.ARTICLE_CORS_HEADERS,
                    },
                )

            limit = _article_limit()
            mimetype = web_app.browser_mimetype(entry.mimetype)
            if mimetype in web_app.HTML_MIMETYPES:
                # Byte offsets and validators for stored HTML do not describe
                # the decorated response, so ranges are ignored here.
                data = store.read(path, head_only=False, max_bytes=limit)
                content = _decorated_article(
                    bytes(data.content), source, mimetype
                )
                if request.method == "HEAD":
                    return Response(
                        content=b"",
                        media_type=mimetype,
                        headers={
                            "Cache-Control": "no-cache",
                            "Content-Length": str(len(content)),
                            "X-Content-Type-Options": "nosniff",
                            **web_app.ARTICLE_CORS_HEADERS,
                        },
                    )
                return web_app._article_content_response(content, mimetype)

            if request.method == "HEAD":
                headers = web_app._article_response_headers(
                    mimetype, filename=entry.path
                )
                if entry.size is not None:
                    headers["Content-Length"] = str(entry.size)
                    headers["Accept-Ranges"] = "bytes"
                return Response(
                    content=b"",
                    media_type=mimetype,
                    headers=headers,
                )

            open_stream = getattr(store, "open_stream", None)
            parsed = None
            range_header = request.headers.get("range")
            # The store exposes no validators yet, so If-Range always
            # mismatches and the full representation is served.
            if range_header and not request.headers.get("if-range"):
                parsed = _parse_range(range_header, entry.size)
                if parsed is not None:
                    start, end, suffix = parsed
                    if start < 0:
                        return Response(
                            status_code=416,
                            headers={
                                "Cache-Control": "no-cache",
                                "Content-Range": f"bytes */{entry.size}",
                                "Accept-Ranges": "bytes",
                                "Content-Encoding": "identity",
                                "X-Content-Type-Options": "nosniff",
                                **web_app.ARTICLE_CORS_HEADERS,
                            },
                        )
                    if not callable(open_stream):
                        start, end = _bounded_range(start, end, suffix, limit)
                        data = store.read_range(
                            path,
                            start=start,
                            end=end,
                            max_bytes=limit,
                        )
                        return web_app._article_content_response(
                            data.content,
                            data.mimetype,
                            status_code=206,
                            headers={
                                "Content-Range": (
                                    f"bytes {data.start}-{data.end}/{data.total}"
                                ),
                                "Accept-Ranges": "bytes",
                                # A partial body must never pass through gzip.
                                "Content-Encoding": "identity",
                            },
                            filename=data.path,
                        )

            if callable(open_stream):
                opened = open_stream(entry.path)
                total = int(opened.stat.st_size)
                start, end = (0, total - 1) if parsed is None else parsed[:2]
                try:
                    opened.handle.seek(start)
                except OSError as error:
                    opened.handle.close()
                    raise ArticleUnavailableError(
                        "article content is unavailable"
                    ) from error
                headers = {
                    "Content-Length": str(end - start + 1),
                    "Accept-Ranges": "bytes",
                    "Content-Encoding": "identity",
                }
                if parsed is not None:
                    headers["Content-Range"] = f"bytes {start}-{end}/{total}"

                def file_chunks():
                    remaining = end - start + 1
                    try:
                        while remaining:
                            chunk = opened.handle.read(
                                min(remaining, web_app.ARTICLE_STREAM_CHUNK)
                            )
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            yield chunk
                    finally:
                        opened.handle.close()

                return StreamingResponse(
                    file_chunks(),
                    media_type=mimetype,
                    status_code=200 if parsed is None else 206,
                    headers=web_app._article_response_headers(
                        mimetype, headers=headers, filename=entry.path
                    ),
                    background=BackgroundTask(opened.handle.close),
                )

            data = store.read(path, head_only=False, max_bytes=limit)
            headers = (
                {"Accept-Ranges": "bytes"}
                if entry.size is not None
                else {}
            )
            return web_app._article_content_response(
                data.content,
                data.mimetype,
                headers=headers,
                filename=data.path,
            )
        except ArticleNotFoundError as error:
            # Some Stack Exchange captures reference MathJax fonts the ZIM did
            # not save. Serve the vendored MathJax 2.7.5 WOFF set for those
            # known paths instead of failing the whole equation.
            font = web_app._vendored_mathjax_font(path or "")
            if font is not None:
                return web_app._article_content_response(
                    font.read_bytes(), "font/woff"
                )
            return _article_error_response(error, source)
        except (
            ArticleTooLargeError,
            ArticleUnavailableError,
        ) as error:
            return _article_error_response(error, source)
        finally:
            if store is not None:
                store.close()

    def error_shell_article(
        requested_path: str | None,
        error: Exception,
        source: str,
    ) -> dict:
        safe_path = "/".join(
            part
            for part in (requested_path or "").split("/")
            if part and part not in {".", ".."}
        )
        # The source itself is missing: there is no home/random link to offer.
        # Any other not-found error still belongs to an existing source.
        source_found = not isinstance(error, ArticleSourceNotFoundError) and (
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
            "source": source,
            "source_found": source_found,
        }

    def source_icon_url(source_key: str) -> str | None:
        """Return the favicon used when an article declares none."""
        try:
            resolve = getattr(search_class, "resolve_content_source", None)
            source = (
                resolve(source_key) if callable(resolve) else None
            )
            sources = search_class.source_dicts()
        except Exception:
            return None
        for candidate in sources:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("key") != source_key and (
                source is None or candidate.get("key") != source.key
            ):
                continue
            icon_url = candidate.get("icon_url")
            if isinstance(icon_url, str) and icon_url:
                return icon_url
        return None

    def source_search_available(source_key: str) -> bool:
        """True when this identifier resolves to a searchable source."""
        try:
            resolve = getattr(search_class, "resolve_content_source", None)
            source = resolve(source_key) if callable(resolve) else None
            sources = search_class.source_dicts()
        except Exception:
            return False
        if source is not None:
            return bool(source.searchable)
        for candidate in sources:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("key") == source_key and candidate.get("searchable"):
                return True
        return False

    def _article_path(article) -> str:
        if isinstance(article, dict):
            return str(article.get("path") or "")
        return str(getattr(article, "path", "") or "")

    def _article_title(article) -> str:
        if isinstance(article, dict):
            return str(article.get("title") or "")
        return str(getattr(article, "title", "") or "")

    def _article_mimetype(article) -> str:
        if isinstance(article, dict):
            return str(article.get("mimetype") or "application/octet-stream")
        return str(
            getattr(article, "mimetype", None) or "application/octet-stream"
        )

    def _article_content(article) -> bytes:
        if isinstance(article, dict):
            return bytes(article.get("content") or b"")
        return bytes(getattr(article, "content", b"") or b"")

    def _article_dict(article) -> dict:
        return {
            "path": _article_path(article),
            "title": _article_title(article),
            "mimetype": _article_mimetype(article),
            "content": _article_content(article),
        }

    def content_shell_response(
        source: str,
        path: str | None = None,
        highlight: str | None = None,
        request: Request | None = None,
    ):
        requested_path = None if path is None else path
        fallback_icon = source_icon_url(source)
        show_source_search = source_search_available(source)
        read_head = getattr(search_class, "read_article_head", None)
        check_path = getattr(search_class, "article_path_exists", None)

        source_previous = None
        source_next = None
        neighbors_of = getattr(search_class, "source_neighbors", None)
        if callable(neighbors_of):
            try:
                neighbors = neighbors_of(source)
            except Exception:
                neighbors = None
            if neighbors is not None:
                homepage_of = getattr(search_class, "source_homepage_path", None)

                def navigation_target(entry) -> dict:
                    key = str(getattr(entry, "key", entry))
                    title = str(getattr(entry, "title", "") or key)
                    entry_path = None
                    if callable(homepage_of):
                        try:
                            entry_path = homepage_of(key)
                        except Exception:
                            entry_path = None
                    url = (
                        web_app._content_url(key, entry_path, base_path)
                        if entry_path
                        else web_app._content_url(key, base_path=base_path)
                    )
                    return {"key": key, "title": title, "url": url}

                source_previous, source_next = (
                    navigation_target(entry) for entry in neighbors
                )

        def path_exists(parent_path: str) -> bool:
            """True when the path is a real entry, so breadcrumbs do not link to containers."""
            try:
                if callable(check_path):
                    return bool(check_path(source, parent_path))
                if callable(read_head):
                    read_head(source, parent_path)
                else:
                    search_class.read_article(source, parent_path)
                return True
            except (
                ArticleNotFoundError,
                ArticleTooLargeError,
                ArticleUnavailableError,
            ):
                return False

        resolve = getattr(search_class, "resolve_content", None)
        try:
            if callable(resolve):
                entry = resolve(source, path)
                external_url = getattr(entry, "external_url", None)
                if external_url:
                    return RedirectResponse(external_url, status_code=302)
                article_dict = {
                    "path": entry.path,
                    "title": entry.title,
                    "mimetype": entry.mimetype,
                    "content": b"",
                }
                # The shell only needs the head for HTML metadata.  Large
                # non-HTML media must not be rejected because a whole-body read
                # would exceed max_article_bytes.
                if entry.mimetype in web_app.HTML_MIMETYPES:
                    if callable(read_head):
                        head = read_head(source, path)
                    else:
                        head = search_class.read_article(source, path)
                    article_dict.update(_article_dict(head))
            elif callable(read_head):
                article_dict = _article_dict(read_head(source, path))
            else:
                article_dict = _article_dict(
                    search_class.read_article(source, path)
                )
        except (
            ArticleNotFoundError,
            ArticleTooLargeError,
            ArticleUnavailableError,
        ) as error:
            error_info = error_shell_article(requested_path, error, source)
            return web_app._content_shell(
                source,
                error_info,
                web_app._article_http_error(error).status_code,
                fallback_icon,
                base_path,
                highlight,
                path_exists=path_exists,
                error=error_info,
                secure_browsing=secure_browsing,
                show_zim_search=show_source_search,
                source_previous=source_previous,
                source_next=source_next,
            )

        if (requested_path or "") != article_dict["path"]:
            return _redirect_article(
                "content", source, article_dict["path"], highlight, request
            )
        return web_app._content_shell(
            source,
            article_dict,
            fallback_icon=fallback_icon,
            base_path=base_path,
            highlight=highlight,
            path_exists=path_exists,
            secure_browsing=secure_browsing,
            show_zim_search=show_source_search,
            source_previous=source_previous,
            source_next=source_next,
        )

    def _safe_content_request(request: Request) -> bool:
        return _raw_path_is_safe(request)

    @app.get("/content/{source}")
    def content_homepage(source: str, request: Request):
        if not _safe_content_request(request):
            raise HTTPException(status_code=404, detail="not found")
        return content_shell_response(
            source,
            highlight=requested_highlight(request),
            request=request,
        )

    @app.get("/content/{source}/", include_in_schema=False)
    def content_homepage_slash(source: str, request: Request):
        return content_homepage(source, request)

    @app.head("/content/{source}")
    def content_homepage_head(source: str, request: Request):
        return content_homepage(source, request)

    @app.head("/content/{source}/", include_in_schema=False)
    def content_homepage_slash_head(source: str, request: Request):
        return content_homepage(source, request)

    @app.get("/content/{source}/{path:path}")
    def content_article(source: str, path: str, request: Request):
        if not _safe_content_request(request):
            raise HTTPException(status_code=404, detail="not found")
        return content_shell_response(
            source,
            path,
            highlight=requested_highlight(request),
            request=request,
        )

    @app.head("/content/{source}/{path:path}")
    def content_article_head(source: str, path: str, request: Request):
        return content_article(source, path, request)

    @app.get("/article/{source}")
    def article_homepage(source: str, request: Request):
        if not _safe_content_request(request):
            return _article_error_response(
                ArticleNotFoundError("invalid article path"), source
            )
        return raw_article_response(
            source,
            request,
            highlight=requested_highlight(request),
        )

    @app.head("/article/{source}")
    def article_homepage_head(source: str, request: Request):
        return article_homepage(source, request)

    @app.get("/article/{source}/{path:path}")
    def article(source: str, path: str, request: Request):
        if not _safe_content_request(request):
            return _article_error_response(
                ArticleNotFoundError("invalid article path"), source
            )
        return raw_article_response(
            source,
            request,
            path,
            highlight=requested_highlight(request),
        )

    @app.head("/article/{source}/{path:path}")
    def article_head(source: str, path: str, request: Request):
        return article(source, path, request)

    def _article_preflight(request: Request) -> Response:
        if not _raw_path_is_safe(request):
            return Response(
                status_code=404,
                headers={**web_app.ARTICLE_CORS_HEADERS},
            )
        return Response(
            status_code=204,
            headers={
                **web_app.ARTICLE_CORS_HEADERS,
                "Access-Control-Allow-Methods": _ARTICLE_METHODS,
                "Access-Control-Allow-Headers": _ARTICLE_REQUEST_HEADERS,
                "Access-Control-Expose-Headers": (
                    "Content-Range, Accept-Ranges, ETag, Last-Modified"
                ),
                "Access-Control-Max-Age": "600",
                "Vary": "Access-Control-Request-Headers",
            },
        )

    @app.options("/article/{source}")
    def article_preflight_homepage(source: str, request: Request):
        return _article_preflight(request)

    @app.options("/article/{source}/{path:path}")
    def article_preflight(source: str, path: str, request: Request):
        return _article_preflight(request)

    @app.get("/random")
    def random_article(source: str | None = Query(None, min_length=1)):
        if not source:
            choose_source = getattr(search_class, "random_local_source", None)
            if not callable(choose_source):
                raise HTTPException(
                    status_code=404, detail="random sources unavailable"
                )
            try:
                chosen = choose_source()
            except ArticleNotFoundError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            return Response(
                status_code=307,
                headers={
                    "Location": web_app._content_url(
                        chosen.key, base_path=base_path
                    ),
                    "Cache-Control": "no-store",
                },
            )

        choose_article = getattr(search_class, "random_article_path", None)
        if not callable(choose_article):
            raise HTTPException(status_code=404, detail="random articles unavailable")
        try:
            path = choose_article(source)
        except ArticleNotFoundError as error:
            return _article_error_response(error, source)
        except ArticleUnavailableError as error:
            return _article_error_response(error, source)
        return Response(
            status_code=307,
            headers={
                "Location": web_app._content_url(source, path, base_path),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/suggest")
    def api_suggest(
        q: Annotated[str, Query(min_length=1, max_length=4096)],
        source: Annotated[list[str] | None, Query()] = None,
        collection: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=20)] = 8,
    ):
        try:
            items = search_class.suggest(q, source, limit, collection)
        except SearchBusyError as error:
            raise HTTPException(
                status_code=503,
                detail=str(error),
                headers={"Retry-After": "1"},
            ) from error
        except SearchQueryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return JSONResponse(content=items)

    def search_page_response(page):
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
        if page.get("scope") is not None:
            headers["X-Search-Scope"] = json.dumps(
                page["scope"],
                separators=(",", ":"),
                ensure_ascii=True,
            )
        return JSONResponse(content=page["results"], headers=headers)

    def search_stream_response(stream):
        def events():
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

    @app.get("/search")
    def api_search(
        q: Annotated[str, Query(max_length=4096)],
        source: Annotated[list[str] | None, Query()] = None,
        collection: Annotated[str | None, Query()] = None,
        limit: Annotated[int | None, Query(ge=1)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        sort: Literal["relevance", "random", "rare", "outliers", "diverse", "recency"] | None = None,
        seed: Annotated[int, Query(ge=0, lt=2**32)] = 0,
    ):
        source_filter = source[0] if source and len(source) == 1 else source
        try:
            q, sort = search_class._validate_search(q, sort, seed)
            page = search_class.search_page(
                q, source_filter, limit, offset, collection, debug,
                sort=sort, seed=seed,
            )
        except SearchBusyError as error:
            raise HTTPException(
                status_code=503,
                detail=str(error),
                headers={"Retry-After": "1"},
            ) from error
        except SearchQueryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return search_page_response(page)

    @app.get("/search/stream")
    def api_search_stream(
        q: Annotated[str, Query(max_length=4096)],
        source: Annotated[list[str] | None, Query()] = None,
        collection: Annotated[str | None, Query()] = None,
        limit: Annotated[int | None, Query(ge=1)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        sort: Literal["relevance", "random", "rare", "outliers", "diverse", "recency"] | None = None,
        seed: Annotated[int, Query(ge=0, lt=2**32)] = 0,
    ):
        try:
            q, sort = search_class._validate_search(q, sort, seed)
            source_filter = source[0] if source and len(source) == 1 else source
            validate_scope = getattr(search_class, "validate_search_scope", None)
            if callable(validate_scope):
                validate_scope(source_filter, collection)
        except SearchQueryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return search_stream_response(
            search_class.stream_search(
                q, source_filter, limit, offset, collection, debug,
                sort=sort, seed=seed,
            )
        )

    @app.get("/browse")
    def api_browse(
        mode: Literal["random", "rare", "outliers", "diverse", "recency"] = "random",
        seed: Annotated[int, Query(ge=0, lt=2**32)] = 0,
        source: Annotated[list[str] | None, Query()] = None,
        collection: Annotated[str | None, Query()] = None,
        limit: Annotated[int | None, Query(ge=1)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        source_filter = source[0] if source and len(source) == 1 else source
        try:
            page = search_class.search_page(
                "",
                source_filter,
                limit,
                offset,
                collection,
                debug,
                browse=mode,
                seed=seed,
            )
        except SearchBusyError as error:
            raise HTTPException(
                status_code=503,
                detail=str(error),
                headers={"Retry-After": "1"},
            ) from error
        except SearchQueryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return search_page_response(page)

    @app.get("/browse/stream")
    def api_browse_stream(
        mode: Literal["random", "rare", "outliers", "diverse", "recency"] = "random",
        seed: Annotated[int, Query(ge=0, lt=2**32)] = 0,
        source: Annotated[list[str] | None, Query()] = None,
        collection: Annotated[str | None, Query()] = None,
        limit: Annotated[int | None, Query(ge=1)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        source_filter = source[0] if source and len(source) == 1 else source
        try:
            search_class._validate_browse(mode, seed)
            validate_scope = getattr(search_class, "validate_search_scope", None)
            if callable(validate_scope):
                validate_scope(source_filter, collection)
        except SearchQueryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return search_stream_response(
            search_class.stream_search(
                "",
                source_filter,
                limit,
                offset,
                collection,
                debug,
                browse=mode,
                seed=seed,
            )
        )

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    def capture_asset_fallback(path: str, request: Request):
        """Serve root-absolute capture assets from the referring article's ZIM.

        Some CMS and MediaWiki captures emit ``/w/...``, ``/assets/...`` and
        similar URLs. Inside the article iframe those resolve against the app
        origin instead of the ZIM. When such a request misses every app route,
        use the article referrer to find the ZIM and try the capture's common
        archived equivalents.
        """
        if request.method not in {"GET", "HEAD"}:
            raise HTTPException(status_code=404, detail="not found")
        if not _raw_path_is_safe(request):
            raise HTTPException(status_code=404, detail="not found")
        source = _referer_source_key(request.headers.get("referer"))
        if not source:
            raise HTTPException(status_code=404, detail="not found")
        for candidate in _capture_asset_candidates(path):
            try:
                article = search_class.read_article(source, candidate)
            except (
                ArticleNotFoundError,
                ArticleTooLargeError,
                ArticleUnavailableError,
            ):
                continue
            content = (
                article.get("content")
                if isinstance(article, dict)
                else getattr(article, "content", None)
            )
            if content is None:
                continue
            mimetype = (
                article.get("mimetype")
                if isinstance(article, dict)
                else getattr(article, "mimetype", None)
            )
            return web_app._article_content_response(
                content,
                mimetype or "application/octet-stream",
                filename=candidate,
            )
        raise HTTPException(status_code=404, detail="not found")
