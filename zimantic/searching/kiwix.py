"""Kiwix catalog discovery and remote search."""

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from ..contracts import SourceInfo, SourceResult
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
DEFAULT_KIWIX_CATALOG_LIMIT = 50_000
DEFAULT_KIWIX_CATALOG_ATTEMPTS = 3
DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF = 1.0
MAX_KIWIX_CATALOG_RETRY_BACKOFF = 8.0
DEFAULT_KIWIX_RESULT_LIMIT = 1_000
logger = logging.getLogger(__name__)


def _catalog_book(href: str) -> str:
    parts = [part for part in urlparse(href).path.split("/") if part]
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "content" and index + 1 < len(parts):
            return unquote(parts[index + 1])
    return unquote(parts[-1]) if parts else ""


def catalog_entries(
    cfg: dict[str, Any],
    server: str,
    source_timeout: float,
    *,
    urlopen_fn: Callable[..., Any] = urlopen,
    sleep_fn: Callable[[float], Any] = time.sleep,
) -> list[dict[str, str]] | None:
    path = str(
        cfg.get(
            "kiwix_catalog_path",
            "/kiwix/catalog/v2/entries?count=50000",
        )
    )
    url = server + (path if path.startswith("/") else "/" + path)
    catalog_limit = _int_config(
        cfg,
        "kiwix_catalog_limit",
        DEFAULT_KIWIX_CATALOG_LIMIT,
    )
    url = _bounded_catalog_url(url, catalog_limit)
    response_limit = _int_config(
        cfg,
        "max_kiwix_response_bytes",
        DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
    )
    for attempt in range(DEFAULT_KIWIX_CATALOG_ATTEMPTS):
        try:
            request = Request(
                url,
                headers={"Accept": "application/atom+xml, application/xml"},
            )
            with urlopen_fn(request, timeout=source_timeout) as response:
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
            sleep_fn(delay)

    newest: dict[str, dict[str, str]] = {}
    for entry in root.iter():
        if _local_name(entry) != "entry":
            continue
        title = _child_text(entry, "title")
        updated = _child_text(entry, "updated")
        href = ""
        for child in entry:
            if _local_name(child) == "link" and (
                (child.attrib.get("type") or "").startswith("text/html") or not href
            ):
                href = child.attrib.get("href") or ""
        book = _catalog_book(href) or unquote(_child_text(entry, "name"))
        if not book:
            continue
        item = {"name": title or book, "book": book, "updated": updated}
        key = _book_identity(book)
        previous = newest.get(key)
        if not previous or (updated, book) > (
            previous["updated"],
            previous["book"],
        ):
            newest[key] = item

    items = list(newest.values())
    items.sort(key=lambda item: (item["name"].casefold(), item["book"]))
    items = items[:catalog_limit]
    title_counts: dict[str, int] = {}
    for item in items:
        title_counts[item["name"]] = title_counts.get(item["name"], 0) + 1
    for item in items:
        if title_counts[item["name"]] > 1:
            item["name"] = f"{item['name']} ({_zim_key(item['book'])})"
    return items


def search_kiwix(
    source: SourceInfo,
    query: str,
    limit: int,
    cfg: dict[str, Any],
    candidate_count: int,
    source_timeout: float,
    preview_fn: Callable[[str], str],
    *,
    urlopen_fn: Callable[..., Any] = urlopen,
) -> SourceResult:
    server = str(cfg.get("kiwix_server") or "").strip().rstrip("/")
    endpoint = server + "/kiwix/search"
    result_limit = min(
        max(candidate_count, limit),
        DEFAULT_KIWIX_RESULT_LIMIT,
    )
    params = urlencode(
        {
            "pattern": query,
            "books.name": source.book,
            "pageLength": result_limit,
            "format": "xml",
        }
    )
    response_limit = _int_config(
        cfg,
        "max_kiwix_response_bytes",
        DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
    )
    try:
        request = Request(
            endpoint + "?" + params,
            headers={"Accept": "application/xml"},
        )
        with urlopen_fn(request, timeout=source_timeout) as response:
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

    docs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in root.iter():
        if _local_name(item) not in {"item", "entry"}:
            continue
        title = _child_text(item, "title")
        link = _child_text(item, "link")
        snippet = preview_fn(
            re.sub(r"\s+", " ", _child_text(item, "description")).strip()
        )
        identity = (title.casefold(), link)
        if not title and not link or identity in seen:
            continue
        seen.add(identity)
        parsed_link = urlparse(link)
        if link and not parsed_link.scheme and not parsed_link.netloc:
            link = urljoin(server.rstrip("/") + "/", link)
        docs.append(
            {
                "id": f"{source.key}:{link or title}",
                "source_key": source.key,
                "source": source.name,
                "source_icon": source.icon_url,
                "title": title or "(untitled)",
                "lead": snippet,
                "path": link,
                "url": link,
                "source_rank": len(docs) + 1,
            }
        )
    return SourceResult(
        source=source,
        items=docs,
        fulltext=[(rank, doc) for rank, doc in enumerate(docs)],
    )
