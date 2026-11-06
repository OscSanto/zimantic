"""Remote Kiwix search requests and result parsing."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from ...contracts import SourceInfo, SourceResult, encode_document_id
from ..base import SourceQuery
from .catalog import (
    DEFAULT_KIWIX_RESULT_LIMIT,
    DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
    _child_text,
    _local_name,
    _open_url,
    _parse_remote_xml,
    _read_limited_response,
    _safe_result_url,
    _validated_origin,
)

logger = logging.getLogger(__name__)


def search(
    source: SourceInfo,
    request: SourceQuery,
    *,
    server: str,
    max_response_bytes: int = DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
    source_timeout: float = 12.0,
    urlopen_fn: Callable[..., Any] | None = None,
) -> SourceResult:
    """Search one Kiwix book and return its external result documents."""
    try:
        origin = _validated_origin(server)
    except ValueError as error:
        return SourceResult(
            source=source,
            error=f"Kiwix server is invalid: {error}",
            status="error",
        )

    endpoint = server.rstrip("/") + "/kiwix/search"
    query = request.fulltext_query or request.query
    result_limit = min(max(1, int(request.limit)), DEFAULT_KIWIX_RESULT_LIMIT)
    book = (
        getattr(source, "_kiwix_book", None)
        or getattr(source, "book", None)
        or ""
    )
    params = urlencode(
        {
            "pattern": query,
            "books.name": book,
            "pageLength": result_limit,
            "format": "xml",
        }
    )
    timeout = float(source_timeout or 12.0)
    if request.deadline is not None:
        remaining = request.deadline - time.monotonic()
        if remaining <= 0:
            return SourceResult(
                source=source,
                error="Kiwix search deadline expired",
                status="cancelled",
            )
        timeout = max(0.1, min(timeout, remaining))
    try:
        http_request = Request(
            endpoint + "?" + params,
            headers={"Accept": "application/xml"},
        )
        with _open_url(
            urlopen_fn,
            http_request,
            timeout=timeout,
            origin=origin,
            deadline=request.deadline,
        ) as response:
            root = _parse_remote_xml(
                _read_limited_response(response, max_response_bytes)
            )
    except (
        HTTPError,
        URLError,
        TimeoutError,
        ValueError,
        OSError,
    ) as error:
        return SourceResult(
            source=source,
            error=f"Kiwix search failed: {error}",
            status="error",
        )

    docs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in root.iter():
        if _local_name(item) not in {"item", "entry"}:
            continue
        title = _child_text(item, "title")
        link = _child_text(item, "link")
        snippet = re.sub(r"\s+", " ", _child_text(item, "description")).strip()
        identity = (title.casefold(), link)
        if not title and not link or identity in seen:
            continue
        seen.add(identity)
        try:
            url = _safe_result_url(link, server) if link else ""
        except ValueError as error:
            logger.warning("zimantic: rejecting Kiwix result link: %s", error)
            continue
        # Prefix the reference so a URL and a title can never collide.
        reference = f"url:{url}" if url else f"title:{title}"
        docs.append(
            {
                "id": encode_document_id(source.key, reference),
                "source_key": source.key,
                "source": source.title or source.key,
                "source_icon": source.icon_url,
                "title": title or "(untitled)",
                "lead": snippet,
                "path": url,
                "url": url,
                "mimetype": None,
                "source_rank": len(docs) + 1,
            }
        )
    return SourceResult(
        source=source,
        items=docs,
        fulltext=[(rank, doc) for rank, doc in enumerate(docs, 1)],
    )


__all__ = ["search"]
