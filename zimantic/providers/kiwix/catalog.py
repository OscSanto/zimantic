"""Kiwix catalog discovery: bounded fetch, parse, retry, and pagination.

The helpers in this module are Kiwix-specific.  ZIM/icon helpers stay in
:mod:`zimantic.searching.sources` until the content-store phase moves them.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import (
    parse_qsl,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlsplit,
    urlunsplit,
)
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
    urlopen,
)
from xml.parsers import expat

DEFAULT_MAX_KIWIX_RESPONSE_BYTES = 64 << 20
DEFAULT_KIWIX_CATALOG_LIMIT = 1_000
DEFAULT_KIWIX_CATALOG_ENTRIES = 50_000
DEFAULT_KIWIX_CATALOG_ATTEMPTS = 3
DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF = 1.0
MAX_KIWIX_CATALOG_RETRY_BACKOFF = 8.0
DEFAULT_KIWIX_RESULT_LIMIT = 1_000
DEFAULT_KIWIX_MAX_PAGES = 1_000
DEFAULT_KIWIX_CATALOG_PATH = "/kiwix/catalog/v2/entries"
DEFAULT_MAX_REMOTE_SOURCES = 100
DEFAULT_KIWIX_REDIRECT_HOPS = 5

_STDLIB_URLOPEN = urlopen


@dataclass(frozen=True)
class CatalogFetch:
    """One complete catalog attempt.

    ``entries`` is ``None`` for an incomplete or failed scan.  Callers retain
    their previous complete snapshot in that case.
    """

    entries: tuple[dict[str, str], ...] | None
    error: str | None = None
    # False for a configuration or protocol failure that a background retry
    # cannot fix (for example a 404 catalog URL or a malformed server URL).
    retryable: bool = True

    @property
    def complete(self) -> bool:
        return self.entries is not None


def _validated_origin(server: str) -> tuple[str, str]:
    """Validate a configured server and return its normalized origin."""
    parts = urlparse(str(server or "").strip())
    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Kiwix server must be an absolute HTTP(S) URL")
    if parts.username or parts.password:
        raise ValueError("Kiwix server must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("Kiwix server must not contain a query or fragment")
    return scheme, parts.netloc.casefold()


def _safe_result_url(link: str, server: str) -> str:
    """Resolve a result link and reject foreign, credential, or non-HTTP URLs.

    Relative links resolve against the configured server base (including its
    path prefix), while root-relative links resolve at the server origin.
    Scheme-relative links keep the configured server's scheme.
    """
    scheme, netloc = _validated_origin(server)
    text = str(link or "").strip()
    parsed = urlparse(text)
    if text.startswith("//"):
        resolved = urlparse(f"{scheme}:{text}")
    elif parsed.scheme or parsed.netloc:
        resolved = parsed
    elif text.startswith("/"):
        base = urlparse(server)
        resolved = urlparse(f"{base.scheme}://{base.netloc}{text}")
    else:
        resolved = urlparse(urljoin(server.rstrip("/") + "/", text))
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
    """Follow only bounded same-origin redirects, validating before the fetch."""

    def __init__(
        self,
        origin: tuple[str, str],
        max_hops: int = DEFAULT_KIWIX_REDIRECT_HOPS,
        deadline: float | None = None,
    ) -> None:
        super().__init__()
        self.origin = origin
        self.max_hops = max_hops
        self.deadline = deadline
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > self.max_hops:
            raise ValueError("too many Kiwix redirects")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError("Kiwix request deadline expired")
        parsed = urlparse(newurl)
        if parsed.username or parsed.password:
            raise ValueError("Kiwix redirect must not contain credentials")
        if (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
        ) != self.origin:
            raise ValueError("cross-origin Kiwix redirect rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(
    urlopen_fn: Callable[..., Any] | None,
    request: Request,
    *,
    timeout: float,
    origin: tuple[str, str],
    deadline: float | None = None,
):
    """Open a URL with one shared same-origin redirect policy.

    Standard-library callers get an opener whose redirect handler validates
    every ``Location`` *before* the redirected request is made.  Injected test
    callables are used as-is.
    """
    if urlopen_fn is None:
        urlopen_fn = urlopen
    if urlopen_fn is _STDLIB_URLOPEN:
        opener = build_opener(_SameOriginRedirectHandler(origin, deadline=deadline))
        return opener.open(request, timeout=timeout)
    return urlopen_fn(request, timeout=timeout)


def _bounded_catalog_url(url: str, limit: int) -> str:
    """Replace an unbounded Kiwix catalog count with a finite page size."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    bounded: list[tuple[str, str]] = []
    replaced = False
    for key, value in query:
        if key == "count":
            if not replaced:
                bounded.append((key, str(limit)))
                replaced = True
        else:
            bounded.append((key, value))
    if not replaced:
        bounded.append(("count", str(limit)))
    return urlunsplit(parts._replace(query=urlencode(bounded)))


def _read_limited_response(response, limit: int) -> bytes:
    """Read a remote response without allowing unbounded memory growth."""
    content_length = response.headers.get("Content-Length")
    try:
        advertised = int(content_length) if content_length else 0
    except (TypeError, ValueError):
        advertised = 0
    if advertised > limit:
        raise ValueError(f"response exceeds the {limit} byte limit")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"response exceeds the {limit} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_remote_xml(payload: bytes) -> str:
    """Decode common XML encodings so declarations are rejected first."""
    if payload.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return payload.decode("utf-32")
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16")
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig")
    # BOM-less UTF-16/32 declarations are detectable from the null-byte layout.
    if payload[:4] in {b"<\x00?\x00", b"\x00<\x00?"}:
        return payload.decode("utf-16")
    if payload[:4] in {b"<\x00\x00\x00", b"\x00\x00\x00<"}:
        return payload.decode("utf-32")
    return payload.decode("utf-8")


def _parse_remote_xml(payload: bytes) -> ET.Element:
    """Parse XML while rejecting DTD/entity declarations before expansion.

    Expat applies this guard before expanding the internal subset, so the
    encoding (UTF-8, UTF-16, UTF-32, with or without a BOM) does not create a
    bypass.  External entities are never fetched.
    """

    def _reject(*_args: Any) -> None:
        raise ValueError("DTD and entity declarations are not supported")

    payload_or_text: bytes | str = payload
    if payload.startswith(
        (b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")
    ) or payload[:4] in {
        b"<\x00?\x00",
        b"\x00<\x00?",
        b"<\x00\x00\x00",
        b"\x00\x00\x00<",
    }:
        # Expat handles UTF-8 and declared single-byte encodings directly;
        # decode BOM/BOM-less UTF-16/32 so those declarations cannot bypass
        # the DTD guard on a build that does not auto-detect them.
        payload_or_text = _decode_remote_xml(payload)
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartDoctypeDeclHandler = _reject
    parser.EntityDeclHandler = _reject
    parser.ExternalEntityRefHandler = lambda *_args: 0
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(payload_or_text, True)
    except expat.ExpatError as error:
        raise ValueError(f"invalid XML: {error}") from error
    return builder.close()


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child) == name:
            return "".join(child.itertext()).strip()
    return ""


def _catalog_book(href: str) -> str:
    parts = [part for part in urlparse(href).path.split("/") if part]
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "content" and index + 1 < len(parts):
            return unquote(parts[index + 1])
    return unquote(parts[-1]) if parts else ""


def _catalog_url(server: str, path: str | None, limit: int, start: int) -> str:
    base = server.rstrip("/")
    configured = path or DEFAULT_KIWIX_CATALOG_PATH
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
        icon_href = ""
        for child in entry:
            if _local_name(child) != "link":
                continue
            link_type = (child.attrib.get("type") or "").casefold()
            rel = (child.attrib.get("rel") or "").casefold()
            candidate = child.attrib.get("href") or ""
            is_image = (
                link_type.startswith("image/")
                or "thumbnail" in rel
                or "image" in rel
            )
            if link_type.startswith("text/html") or not href and not is_image:
                href = candidate
            if is_image:
                icon_href = candidate
        book = _catalog_book(href) or unquote(_child_text(entry, "name"))
        if not book:
            continue
        item = {"name": title or book, "book": book, "updated": updated}
        if href:
            item["homepage"] = href
        if icon_href:
            item["icon_url"] = icon_href
        items.append(item)
    return items


def _finalize_entries(
    items: list[dict[str, str]], *, max_entries: int
) -> list[dict[str, str]] | None:
    """Deduplicate exact remote slugs and disambiguate duplicate titles.

    Distinct dated or flavoured slugs are separate addressable books and are
    not merged by *identity*.  Only an exact repeat of the remote slug names
    the same endpoint.
    """
    newest: dict[str, dict[str, str]] = {}
    for item in items:
        book = item["book"]
        previous = newest.get(book)
        if not previous or (item.get("updated", ""), item.get("name", "")) > (
            previous.get("updated", ""),
            previous.get("name", ""),
        ):
            newest[book] = item
    entries = list(newest.values())
    entries.sort(key=lambda item: (item["name"].casefold(), item["book"]))
    if len(entries) > max_entries:
        return None
    title_counts: dict[str, int] = {}
    for item in entries:
        title_counts[item["name"]] = title_counts.get(item["name"], 0) + 1
    for item in entries:
        if title_counts[item["name"]] > 1:
            item["name"] = f"{item['name']} ({item['book']})"
    return entries


def fetch_catalog(
    server: str,
    *,
    catalog_path: str | None = None,
    catalog_limit: int = DEFAULT_KIWIX_CATALOG_LIMIT,
    max_entries: int = DEFAULT_KIWIX_CATALOG_ENTRIES,
    max_response_bytes: int = DEFAULT_MAX_KIWIX_RESPONSE_BYTES,
    timeout: float = 12.0,
    total_timeout: float | None = None,
    urlopen_fn: Callable[..., Any] | None = None,
    sleep_fn: Callable[[float], Any] | None = None,
) -> CatalogFetch:
    """Discover a complete catalog, paging within explicit budgets.

    ``catalog_limit`` is the per-page request size; ``max_entries`` is the hard
    total bound and is counted before exact-slug deduplication.  A catalog
    larger than the bound, or an incomplete scan, returns ``entries=None`` so
    the caller retains its previous complete snapshot.
    """
    if sleep_fn is None:
        sleep_fn = time.sleep
    page_size = max(1, int(catalog_limit))
    entry_bound = max(1, int(max_entries))
    try:
        origin = _validated_origin(server)
    except ValueError as error:
        return CatalogFetch(
            None, f"Kiwix server is invalid: {error}", retryable=False
        )

    budget = (
        float(total_timeout)
        if total_timeout is not None
        else max(1.0, float(timeout) * 4)
    )
    deadline = time.monotonic() + max(0.1, budget)
    collected: list[dict[str, str]] = []
    seen_books: set[str] = set()
    raw_count = 0
    start = 0

    for _page in range(DEFAULT_KIWIX_MAX_PAGES):
        if time.monotonic() >= deadline:
            return CatalogFetch(
                None,
                "Kiwix catalog discovery exceeded its time budget; "
                "keeping the last complete catalog",
            )
        url = _catalog_url(server, catalog_path, page_size, start)
        root: ET.Element | None = None
        for attempt in range(DEFAULT_KIWIX_CATALOG_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return CatalogFetch(
                    None,
                    "Kiwix catalog discovery exceeded its time budget; "
                    "keeping the last complete catalog",
                )
            try:
                request = Request(
                    url,
                    headers={"Accept": "application/atom+xml, application/xml"},
                )
                with _open_url(
                    urlopen_fn,
                    request,
                    timeout=max(0.1, min(float(timeout), remaining)),
                    origin=origin,
                    deadline=deadline,
                ) as response:
                    root = _parse_remote_xml(
                        _read_limited_response(response, max_response_bytes)
                    )
                break
            except (
                HTTPError,
                URLError,
                TimeoutError,
                ValueError,
                OSError,
            ) as error:
                retryable = True
                if isinstance(error, HTTPError) and (
                    400 <= error.code < 500 and error.code != 429
                ) or isinstance(error, ValueError):
                    retryable = False
                if not retryable:
                    return CatalogFetch(
                        None,
                        f"Kiwix catalog request failed: {error}",
                        retryable=False,
                    )
                if attempt + 1 == DEFAULT_KIWIX_CATALOG_ATTEMPTS:
                    return CatalogFetch(
                        None,
                        "Kiwix catalog request failed after "
                        f"{DEFAULT_KIWIX_CATALOG_ATTEMPTS} attempt(s): {error}",
                    )
                delay = min(
                    DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF * (2**attempt),
                    MAX_KIWIX_CATALOG_RETRY_BACKOFF,
                )
                if time.monotonic() + delay > deadline:
                    return CatalogFetch(
                        None,
                        "Kiwix catalog discovery exceeded its time budget; "
                        "keeping the last complete catalog",
                    )
                sleep_fn(delay)
        if root is None:  # pragma: no cover - the loop returns or breaks
            return CatalogFetch(
                None, "Kiwix catalog request failed", retryable=False
            )

        page = _parse_entries(root)
        if not page:
            break
        raw_count += len(page)
        if raw_count > entry_bound:
            return CatalogFetch(
                None,
                f"Kiwix catalog exceeds the {entry_bound} entry limit; "
                "keeping the last complete catalog",
                retryable=False,
            )
        new = [item for item in page if item["book"] not in seen_books]
        if not new:
            return CatalogFetch(
                None,
                "Kiwix catalog paging did not advance; "
                "keeping the last complete catalog",
                retryable=False,
            )
        for item in new:
            seen_books.add(item["book"])
            collected.append(item)
        start += len(page)
    else:
        return CatalogFetch(
            None,
            f"Kiwix catalog did not finish within {DEFAULT_KIWIX_MAX_PAGES} pages; "
            "keeping the last complete catalog",
            retryable=False,
        )

    entries = _finalize_entries(collected, max_entries=entry_bound)
    if entries is None:
        return CatalogFetch(
            None,
            f"Kiwix catalog exceeds the {entry_bound} entry limit; "
            "keeping the last complete catalog",
        )
    return CatalogFetch(tuple(entries))


__all__ = [
    "DEFAULT_KIWIX_CATALOG_ATTEMPTS",
    "DEFAULT_KIWIX_CATALOG_ENTRIES",
    "DEFAULT_KIWIX_CATALOG_LIMIT",
    "DEFAULT_KIWIX_CATALOG_PATH",
    "DEFAULT_KIWIX_CATALOG_RETRY_BACKOFF",
    "DEFAULT_KIWIX_MAX_PAGES",
    "DEFAULT_KIWIX_REDIRECT_HOPS",
    "DEFAULT_KIWIX_RESULT_LIMIT",
    "DEFAULT_MAX_KIWIX_RESPONSE_BYTES",
    "DEFAULT_MAX_REMOTE_SOURCES",
    "MAX_KIWIX_CATALOG_RETRY_BACKOFF",
    "CatalogFetch",
    "_bounded_catalog_url",
    "_catalog_book",
    "_child_text",
    "_decode_remote_xml",
    "_local_name",
    "_open_url",
    "_parse_remote_xml",
    "_read_limited_response",
    "_safe_result_url",
    "_validated_origin",
    "fetch_catalog",
]
