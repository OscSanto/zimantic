"""Kiwix catalog discovery and remote search."""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urljoin, urlparse, parse_qsl
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
    urlopen,
)

from ..contracts import SourceInfo, SourceResult, encode_document_id
from .config import _int_config
from .sources import (
    _bounded_catalog_url,
    _book_identity,
    _child_text,
    _local_name,
    _parse_remote_xml,
    _read_limited_response,
    _zim_key,
)

DEFAULT_MAX_KIWIX_RESPONSE_BYTES = 64 << 20
DEFAULT_KIWIX_CATALOG_LIMIT = 1_000
DEFAULT_KIWIX_CATALOG_ENTRIES = 50_000
DEFAULT_KIWIX_CATALOG_ATTEMPTS = 3
DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF = 1.0
MAX_KIWIX_CATALOG_RETRY_BACKOFF = 8.0
DEFAULT_KIWIX_RESULT_LIMIT = 1_000
DEFAULT_KIWIX_MAX_PAGES = 1_000
_STDLIB_URLOPEN = urlopen
logger = logging.getLogger(__name__)


def _catalog_book(href: str) -> str:
    parts = [part for part in urlparse(href).path.split("/") if part]
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "content" and index + 1 < len(parts):
            return unquote(parts[index + 1])
    return unquote(parts[-1]) if parts else ""


def _origin(server: str) -> tuple[str, str]:
    parts = urlparse(server)
    return (parts.scheme.casefold(), parts.netloc.casefold())


def _validated_origin(server: str) -> tuple[str, str]:
    scheme, netloc = _origin(server)
    parts = urlparse(server)
    if scheme not in {"http", "https"} or not netloc:
        raise ValueError("Kiwix server must be an absolute HTTP(S) URL")
    if parts.username or parts.password:
        raise ValueError("Kiwix server must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("Kiwix server must not contain a query or fragment")
    return scheme, netloc


def _safe_result_url(link: str, server: str) -> str:
    """Resolve a result link and reject foreign, credential, or non-HTTP URLs."""
    scheme, netloc = _validated_origin(server)
    parsed = urlparse(link)
    if not parsed.scheme and not parsed.netloc:
        if link.startswith("//"):
            resolved = urlparse("http:" + link)
        elif link.startswith("/"):
            base = urlparse(server)
            resolved = urlparse(f"{base.scheme}://{base.netloc}{link}")
        else:
            resolved = urlparse(urljoin(server.rstrip("/") + "/", link))
    else:
        resolved = parsed
    if resolved.scheme.casefold() not in {"http", "https"}:
        raise ValueError("unsupported result URL scheme")
    if resolved.username or resolved.password:
        raise ValueError("result URL must not contain credentials")
    if (
        resolved.scheme.casefold(),
        resolved.netloc.casefold(),
    ) != (scheme, netloc):
        raise ValueError("result URL points at a foreign origin")
    return resolved.geturl()


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def __init__(self, origin: tuple[str, str], max_hops: int = 5) -> None:
        super().__init__()
        self.origin = origin
        self.max_hops = max_hops
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > self.max_hops:
            raise ValueError("too many Kiwix redirects")
        parsed = urlparse(newurl)
        if (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
        ) != self.origin:
            raise ValueError("cross-origin Kiwix redirect rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(urlopen_fn, request, *, timeout: float, origin: tuple[str, str]):
    if urlopen_fn is _STDLIB_URLOPEN:
        opener = build_opener(_SameOriginRedirectHandler(origin))
        return opener.open(request, timeout=timeout)
    return urlopen_fn(request, timeout=timeout)


def _catalog_url(server: str, path: str | None, limit: int, start: int) -> str:
    base = server.rstrip("/")
    configured = path or "/kiwix/catalog/v2/entries?count=50000"
    url = base + (configured if configured.startswith("/") else "/" + configured)
    url = _bounded_catalog_url(url, limit)
    if start <= 0:
        return url
    parts = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "start"
    ]
    query.append(("start", str(start)))
    return parts._replace(query=urlencode(query)).geturl()


def _parse_entries(root: ET.Element) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for entry in root.iter():
        if _local_name(entry) != "entry":
            continue
        title = _child_text(entry, "title")
        updated = _child_text(entry, "updated")
        href = ""
        for child in entry:
            if _local_name(child) == "link" and (
                (child.attrib.get("type") or "").startswith("text/html")
                or not href
            ):
                href = child.attrib.get("href") or ""
        book = _catalog_book(href) or unquote(_child_text(entry, "name"))
        if not book:
            continue
        items.append({"name": title or book, "book": book, "updated": updated})
    return items


def _finalize_entries(
    items: list[dict[str, str]], *, max_entries: int, legacy_limit: int | None
) -> list[dict[str, str]] | None:
    newest: dict[str, dict[str, str]] = {}
    for item in items:
        key = _book_identity(item["book"])
        previous = newest.get(key)
        if not previous or (item["updated"], item["book"]) > (
            previous["updated"],
            previous["book"],
        ):
            newest[key] = item
    entries = list(newest.values())
    entries.sort(key=lambda item: (item["name"].casefold(), item["book"]))
    if len(entries) > max_entries:
        # Never publish a bounded prefix as a complete catalog.
        return None
    if legacy_limit is not None:
        entries = entries[:legacy_limit]
    title_counts: dict[str, int] = {}
    for item in entries:
        title_counts[item["name"]] = title_counts.get(item["name"], 0) + 1
    for item in entries:
        if title_counts[item["name"]] > 1:
            item["name"] = f"{item['name']} ({_zim_key(item['book'])})"
    return entries


def catalog_entries(
    cfg: dict[str, Any],
    server: str,
    source_timeout: float,
    *,
    urlopen_fn: Callable[..., Any] | None = None,
    sleep_fn: Callable[[float], Any] | None = None,
    catalog_path: str | None = None,
    catalog_limit: int | None = None,
    max_entries: int | None = None,
    max_response_bytes: int | None = None,
) -> list[dict[str, str]] | None:
    """Discover a complete catalog, paging within explicit budgets.

    ``catalog_limit`` is the per-page request size; ``max_entries`` is the hard
    total bound.  A catalog larger than the bound, or an incomplete scan, returns
    ``None`` so the caller can retain its previous complete snapshot.
    """
    if urlopen_fn is None:
        urlopen_fn = urlopen
    if sleep_fn is None:
        sleep_fn = time.sleep
    path = catalog_path
    if path is None:
        path = (
            cfg.get("kiwix_catalog_path")
            or "/kiwix/catalog/v2/entries?count=50000"
        )
    path = str(path)
    legacy_limit = None
    if max_entries is None and catalog_limit is None:
        # Pre-collections callers used kiwix_catalog_limit as a total cutoff.
        legacy_limit = _int_config(
            cfg, "kiwix_catalog_limit", DEFAULT_KIWIX_CATALOG_LIMIT
        )
    if catalog_limit is not None:
        page_size = catalog_limit
    elif legacy_limit is not None:
        # The legacy cutoff is a total, not a page size.
        page_size = DEFAULT_KIWIX_CATALOG_LIMIT
    else:
        page_size = _int_config(
            cfg, "kiwix_catalog_limit", DEFAULT_KIWIX_CATALOG_LIMIT
        )
    entry_bound = (
        max_entries
        if max_entries is not None
        else _int_config(
            cfg, "max_catalog_entries", DEFAULT_KIWIX_CATALOG_ENTRIES
        )
    )
    response_limit = (
        max_response_bytes
        if max_response_bytes is not None
        else _int_config(
            cfg,
            "max_kiwix_response_bytes",
            DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
        )
    )
    origin = _validated_origin(server)
    collected: list[dict[str, str]] = []
    seen_books: set[str] = set()
    start = 0
    deadline = time.monotonic() + max(1.0, float(source_timeout) * 4)
    for _page in range(DEFAULT_KIWIX_MAX_PAGES):
        url = _catalog_url(server, path, page_size, start)
        response = None
        for attempt in range(DEFAULT_KIWIX_CATALOG_ATTEMPTS):
            try:
                request = Request(
                    url,
                    headers={"Accept": "application/atom+xml, application/xml"},
                )
                with _open_url(
                    urlopen_fn,
                    request,
                    timeout=source_timeout,
                    origin=origin,
                ) as response:
                    root = _parse_remote_xml(
                        _read_limited_response(response, response_limit)
                    )
                break
            except (
                HTTPError,
                URLError,
                TimeoutError,
                ValueError,
                ET.ParseError,
                OSError,
            ) as error:
                retryable = True
                if isinstance(error, HTTPError) and (
                    400 <= error.code < 500 and error.code != 429
                ):
                    retryable = False
                elif isinstance(error, (ValueError, ET.ParseError)):
                    retryable = False
                if not retryable:
                    logger.warning("zimantic: catalog request failed: %s", error)
                    return None
                if attempt + 1 == DEFAULT_KIWIX_CATALOG_ATTEMPTS:
                    logger.warning(
                        "zimantic: catalog request failed after %d attempt(s): %s",
                        DEFAULT_KIWIX_CATALOG_ATTEMPTS,
                        error,
                    )
                    return None
                delay = min(
                    DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF * (2**attempt),
                    MAX_KIWIX_CATALOG_RETRY_BACKOFF,
                )
                logger.info(
                    "zimantic: catalog request failed (%s); retrying in %.1f seconds",
                    error,
                    delay,
                )
                if time.monotonic() + delay > deadline:
                    return None
                sleep_fn(delay)
        else:  # pragma: no cover - loop always breaks or returns
            return None
        if root is None:
            return None
        page = _parse_entries(root)
        new = [item for item in page if item["book"] not in seen_books]
        if not new:
            break
        for item in new:
            seen_books.add(item["book"])
            collected.append(item)
        if len(collected) > entry_bound:
            logger.warning(
                "zimantic: Kiwix catalog exceeds the %d entry limit; "
                "keeping the last complete catalog",
                entry_bound,
            )
            return None
        if len(page) < page_size:
            break
        start += len(page)
        if time.monotonic() >= deadline:
            logger.warning("zimantic: Kiwix catalog discovery exceeded its time budget")
            return None
    else:
        logger.warning("zimantic: Kiwix catalog did not finish within %d pages", DEFAULT_KIWIX_MAX_PAGES)
        return None
    return _finalize_entries(
        collected, max_entries=entry_bound, legacy_limit=legacy_limit
    )


def search_kiwix(
    source: SourceInfo,
    query: str,
    limit: int,
    cfg: dict[str, Any],
    candidate_count: int,
    source_timeout: float | None,
    preview_fn: Callable[[str], str] | None = None,
    *,
    urlopen_fn: Callable[..., Any] | None = None,
    deadline: float | None = None,
) -> SourceResult:
    if urlopen_fn is None:
        urlopen_fn = urlopen
    server = str(cfg.get("kiwix_server") or "").strip().rstrip("/")
    try:
        origin = _validated_origin(server)
    except ValueError as error:
        return SourceResult(
            source=source, error=f"Kiwix server is invalid: {error}", status="error"
        )
    endpoint = server + "/kiwix/search"
    result_limit = min(
        max(candidate_count, limit),
        DEFAULT_KIWIX_RESULT_LIMIT,
    )
    book = getattr(source, "_kiwix_book", None) or getattr(source, "book", None) or ""
    params = urlencode(
        {
            "pattern": query,
            "books.name": book,
            "pageLength": result_limit,
            "format": "xml",
        }
    )
    response_limit = _int_config(
        cfg,
        "max_kiwix_response_bytes",
        DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
    )
    timeout = float(source_timeout or 12.0)
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return SourceResult(
                source=source,
                error="Kiwix search deadline expired",
                status="cancelled",
            )
        timeout = max(0.1, min(timeout, remaining))
    try:
        request = Request(
            endpoint + "?" + params,
            headers={"Accept": "application/xml"},
        )
        with _open_url(
            urlopen_fn, request, timeout=timeout, origin=origin
        ) as response:
            root = _parse_remote_xml(_read_limited_response(response, response_limit))
    except (
        HTTPError,
        URLError,
        TimeoutError,
        ValueError,
        ET.ParseError,
        OSError,
    ) as error:
        return SourceResult(
            source=source,
            error=f"Kiwix search failed: {error}",
            status="error",
        )

    preview = preview_fn or (lambda text: text)
    docs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in root.iter():
        if _local_name(item) not in {"item", "entry"}:
            continue
        title = _child_text(item, "title")
        link = _child_text(item, "link")
        snippet = preview(
            re.sub(r"\s+", " ", _child_text(item, "description")).strip()
        )
        identity = (title.casefold(), link)
        if not title and not link or identity in seen:
            continue
        seen.add(identity)
        try:
            url = _safe_result_url(link, server) if link else ""
        except ValueError as error:
            logger.warning("zimantic: rejecting Kiwix result link: %s", error)
            continue
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
