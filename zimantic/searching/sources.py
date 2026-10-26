"""Source discovery, icon lookup, and bounded remote-response helpers."""

import logging
import posixpath
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from libzim.reader import Archive

from .query import INTENT_RULES

ICON_METADATA_PATH = "-/icon"
ICON_MAX_BYTES = 1 << 20
ICON_DISCOVERY_HTML_BYTES = 512 << 10
logger = logging.getLogger(__name__)


def _html_attribute(tag: str, name: str) -> str:
    match = re.search(
        rf"(?:^|[\s/]){re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
        tag,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return next((value for value in match.groups() if value is not None), "")


def _archive_path_from_href(base: str, href: str) -> str | None:
    parsed = urlsplit(href.strip())
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    path = unquote(parsed.path)
    if path.startswith("/"):
        candidate = path[1:]
    else:
        candidate = posixpath.join(posixpath.dirname(base), path)
    if not candidate or any(part == ".." for part in candidate.split("/")):
        return None
    normalized = posixpath.normpath(candidate)
    return normalized if normalized not in {"", "."} else None


def _icon_entry_path(archive: Archive, path: str) -> str | None:
    try:
        entry = archive.get_entry_by_path(path)
        item = entry.get_item()
        mimetype = (item.mimetype or "").split(";", 1)[0].strip().lower()
        size = int(getattr(item, "size", 0) or 0)
    except (KeyError, RuntimeError, ValueError):
        return None
    if size > ICON_MAX_BYTES:
        return None
    if mimetype.startswith("image/") or (
        path.casefold().endswith(".ico")
        and mimetype in {"application/octet-stream", "binary/octet-stream"}
    ):
        return path
    return None


def _find_archive_icon_path(archive: Archive, homepage: str | None) -> str | None:
    """Find a small image that represents a local ZIM source."""
    metadata_icon = _icon_entry_path(archive, ICON_METADATA_PATH)
    if metadata_icon:
        return metadata_icon

    linked_paths: list[str] = []
    if homepage:
        try:
            entry = (
                archive.main_entry
                if homepage == archive.main_entry.path
                else archive.get_entry_by_path(homepage)
            )
            item = entry.get_item()
            mimetype = (item.mimetype or "").split(";", 1)[0].strip().lower()
            size = int(getattr(item, "size", 0) or 0)
            if (
                mimetype in {"text/html", "application/xhtml+xml"}
                and size <= ICON_DISCOVERY_HTML_BYTES
            ):
                html = bytes(item.content).decode("utf-8", "ignore")
                for match in re.finditer(r"<link\b[^>]*>", html, re.IGNORECASE):
                    tag = match.group(0)
                    rel = _html_attribute(tag, "rel").casefold()
                    href = _html_attribute(tag, "href")
                    if "icon" in rel and href:
                        path = _archive_path_from_href(homepage, href)
                        if path:
                            linked_paths.append(path)
        except (KeyError, RuntimeError, ValueError) as error:
            logger.debug("could not inspect ZIM homepage %s for an icon: %s", homepage, error)

    candidates = linked_paths + [
        "favicon.ico",
        "favicon.png",
        "apple-touch-icon.png",
    ]
    if homepage:
        base = posixpath.dirname(homepage)
        candidates.extend(
            posixpath.join(base, name)
            for name in ("favicon.ico", "favicon.png", "apple-touch-icon.png")
            if base
        )
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        icon = _icon_entry_path(archive, candidate)
        if icon:
            return icon
    return None


def _source_icon_url(
    zim_name: str, has_icon: bool, base_path: str = ""
) -> str | None:
    if not has_icon:
        return None
    return f"{base_path}/source-icon/{quote(zim_name, safe='')}"


def _bounded_catalog_url(url: str, limit: int) -> str:
    """Replace an unbounded Kiwix catalog count with a large finite limit."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    bounded = []
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


def _parse_remote_xml(payload: bytes) -> ET.Element:
    """Parse trusted-shaped XML while rejecting DTD/entity expansion inputs."""
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", payload, re.IGNORECASE):
        raise ValueError("DTD and entity declarations are not supported")
    return ET.fromstring(payload)


def _zim_key(book: str) -> str:
    return re.sub(r"_\d{4}-\d{2}(-\d{2})?$", "", book)


def _intent_phrases(book: str) -> tuple[str, ...]:
    key = _zim_key(book)
    for pattern, phrases in INTENT_RULES:
        if pattern.search(key):
            return phrases
    return ()


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child) == name:
            return "".join(child.itertext()).strip()
    return ""
