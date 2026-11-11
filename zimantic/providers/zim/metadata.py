"""ZIM metadata extraction, icons, and ``SourceInfo`` construction.

This module is the reference implementation for provider-owned metadata.  It
may use libzim because it lives inside the ZIM provider; shared catalog storage
and core search must not import it.
"""

from __future__ import annotations

import logging
import posixpath
import re
import time
import uuid
from pathlib import Path
from typing import Any

from ...catalog import absolute_path
from ...content.reader import resolve_archive_href
from ...contracts import SourceInfo
from ...media_types import classify_mime_counts, ordered_media_types
from ...redirects import resolve_redirect_entry
from ...searching.query import INTENT_RULES

logger = logging.getLogger(__name__)

ILLUSTRATION_SIZE = 48
MAX_FAVICON_BYTES = 1 << 20
ICON_METADATA_PATH = "-/icon"
ICON_MAX_BYTES = 1 << 20
ICON_DISCOVERY_HTML_BYTES = 512 << 10

META_KEYS = (
    "Title",
    "Description",
    "LongDescription",
    "Language",
    "Creator",
    "Publisher",
    "Date",
    "Tags",
    "Name",
    "Flavour",
    "License",
    "Scraper",
    "Source",
    "Attribution",
    "Relation",
    "Counter",
)

_SAFE_SLUG = re.compile(r"[^a-z0-9._-]+")
_HTML_LINK = re.compile(r"<link\b[^>]*>", re.IGNORECASE)


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value).strip()


def _call_maybe(value: Any) -> Any:
    return value() if callable(value) else value


def normalize_creator(value: Any) -> str:
    """Treat placeholder creator metadata as missing."""
    value = _text(value)
    return "" if value == "-" else value


def normalize_date(value: Any) -> str:
    value = _text(value)
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
    return match.group(1) if match else ""


def normalize_counter(value: Any) -> str:
    """Sort a ZIM Counter (mime=count;...) from highest to lowest count."""
    entries: list[tuple[str, int]] = []
    invalid: list[str] = []
    for entry in _text(value).split(";"):
        entry = entry.strip()
        if not entry:
            continue
        mime, sep, count = entry.partition("=")
        mime = mime.strip()
        count = count.strip()
        if sep and mime and count.isdigit():
            entries.append((mime, int(count)))
        else:
            invalid.append(entry)
    entries.sort(key=lambda item: (-item[1], item[0]))
    return ";".join(
        [f"{mime}={count}" for mime, count in entries] + invalid
    )


def parse_tags(tags: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for tag in _text(tags).split(";"):
        tag = tag.strip()
        if not tag:
            continue
        key, sep, value = tag.partition(":")
        result[key] = value if sep else ""
    return result


def normalize_tags(tags: str) -> str:
    """Normalize tags exactly like libkiwix's ``convertTags()``."""
    parts = [tag.strip() for tag in _text(tags).split(";") if tag.strip()]
    result: list[str] = []
    pic_seen = vid_seen = det_seen = index_seen = False
    for tag in parts:
        pic_seen = pic_seen or tag == "nopic" or tag.startswith("_pictures:")
        vid_seen = vid_seen or tag == "novid" or tag.startswith("_videos:")
        det_seen = det_seen or tag == "nodet" or tag.startswith("_details:")
        index_seen = index_seen or tag.startswith("_ftindex")
        if tag == "nopic":
            result.append("_pictures:no")
        elif tag == "novid":
            result.append("_videos:no")
        elif tag == "nodet":
            result.append("_details:no")
        elif tag == "_ftindex":
            result.append("_ftindex:yes")
        else:
            result.append(tag)
    if not index_seen:
        result.append("_ftindex:no")
    if not pic_seen:
        result.append("_pictures:yes")
    if not vid_seen:
        result.append("_videos:yes")
    if not det_seen:
        result.append("_details:yes")
    return ";".join(result)


def public_tags(tags: str) -> str:
    """Tags suitable for display (private ``_`` tags removed)."""
    return ";".join(
        tag for tag in _text(tags).split(";") if tag and not tag.startswith("_")
    )


def category_from_tags(tags: str) -> str:
    return parse_tags(tags).get("_category", "")


def perma_ref(stem: str) -> str:
    match = re.search(r"_\d{4}-\d{2}(?:-\d{2})?$", stem)
    return stem[: match.start()] if match else stem


def header_uuid(zim_path: Path | str) -> str:
    """Read the ZIM UUID from the archive header (works without libzim)."""
    with open(zim_path, "rb") as fp:
        header = fp.read(24)
    if len(header) < 24 or header[:4] != b"ZIM\x04":
        raise ValueError("not a ZIM file")
    return str(uuid.UUID(bytes=header[8:24]))


def resolve_homepage(archive: Any) -> str:
    """Return the resolved main-entry path (following ``mainPage`` redirects)."""
    try:
        if not _call_maybe(getattr(archive, "has_main_entry", False)):
            return ""
        return resolve_redirect_entry(archive.main_entry).path or ""
    except Exception:
        return ""


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
    return resolve_archive_href(base, href)


def _icon_entry_path(archive: Any, path: str) -> str | None:
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


def find_archive_icon_path(archive: Any, homepage: str | None) -> str | None:
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
                for match in _HTML_LINK.finditer(html):
                    tag = match.group(0)
                    rel = _html_attribute(tag, "rel").casefold()
                    href = _html_attribute(tag, "href")
                    if "icon" in rel and href:
                        path = _archive_path_from_href(homepage, href)
                        if path:
                            linked_paths.append(path)
        except (KeyError, RuntimeError, ValueError) as error:
            logger.debug(
                "could not inspect ZIM homepage %s for an icon: %s",
                homepage,
                error,
            )

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


def source_icon_url(source_key: str, has_icon: bool, base_path: str = "") -> str | None:
    if not has_icon:
        return None
    from urllib.parse import quote

    return f"{base_path}/source-icon/{quote(source_key, safe='')}"


def intent_phrases(name: str) -> tuple[str, ...]:
    """Return configured intent phrases for one ZIM name."""
    key = perma_ref(name)
    for pattern, phrases in INTENT_RULES:
        if pattern.search(key):
            return phrases
    return ()


def metadata_from_archive(
    archive: Any,
    zim_path: Path | str,
    *,
    key: str,
    collection_key: str,
) -> dict[str, Any]:
    """Extract a generic catalog row for one open libzim ``Archive``."""
    path = Path(zim_path)
    stat = path.stat()
    try:
        available = {
            key.decode("utf-8", "replace") if isinstance(key, bytes) else str(key)
            for key in archive.metadata_keys
        }
    except Exception:
        available = set()

    metadata: dict[str, str] = {}
    for meta_key in META_KEYS:
        if available and meta_key not in available:
            continue
        try:
            metadata[meta_key] = _text(archive.get_metadata(meta_key))
        except Exception as error:
            logger.debug("metadata %s unavailable for %s: %s", meta_key, path, error)
            continue

    tags = normalize_tags(metadata.get("Tags", ""))
    stem = path.stem

    try:
        entry_count: int | None = int(_call_maybe(archive.entry_count) or 0)
    except Exception:
        entry_count = None

    try:
        has_fulltext_index: bool | None = bool(
            _call_maybe(archive.has_fulltext_index)
        )
    except Exception:
        has_fulltext_index = None

    homepage = resolve_homepage(archive)
    creator = normalize_creator(metadata.get("Creator", ""))

    favicon = None
    favicon_mime = ""
    try:
        item = archive.get_illustration_item(ILLUSTRATION_SIZE)
        data = bytes(item.content)
        if len(data) <= MAX_FAVICON_BYTES:
            favicon = data
            favicon_mime = item.mimetype or ""
        else:
            logger.warning(
                "ignoring %s illustration of %d bytes (limit %d)",
                path.name,
                len(data),
                MAX_FAVICON_BYTES,
            )
    except Exception:
        pass

    icon_path = ""
    if favicon is None:
        # No embedded illustration: fall back to Zimantic's icon discovery once,
        # at build/scan time, rather than rediscovering it at every startup.
        try:
            icon_path = find_archive_icon_path(archive, homepage or None) or ""
        except Exception:
            icon_path = ""

    try:
        zim_uuid = str(archive.uuid)
    except Exception:
        try:
            zim_uuid = header_uuid(path)
        except Exception:
            zim_uuid = ""

    name = metadata.get("Name", "") or perma_ref(stem)
    title = metadata.get("Title", "") or name

    return {
        "provider": "zim",
        "collection_key": collection_key,
        "key": key,
        "path": str(absolute_path(path)),
        "provider_id": zim_uuid,
        "size": stat.st_size,
        "mtime": round(stat.st_mtime_ns / 1_000_000_000),
        "title": title,
        "description": metadata.get("Description", ""),
        "long_description": metadata.get("LongDescription", ""),
        "language": metadata.get("Language", ""),
        "creator": creator,
        "publisher": metadata.get("Publisher", ""),
        "date": normalize_date(metadata.get("Date", "")),
        "tags": tags,
        "public_tags": public_tags(tags),
        "category": category_from_tags(tags),
        "item_count": entry_count,
        "license": metadata.get("License", ""),
        "origin": metadata.get("Source", ""),
        "attribution": metadata.get("Attribution", ""),
        "relation": metadata.get("Relation", ""),
        "mime_counts": normalize_counter(metadata.get("Counter", "")),
        "homepage": homepage,
        "native_fulltext_available": has_fulltext_index,
        "icon_data": favicon,
        "icon_mime_type": favicon_mime,
        "icon_path": icon_path,
        "scanned_at": time.time(),
        "provider_metadata": {
            "name": metadata.get("Name", ""),
            "flavour": metadata.get("Flavour", ""),
            "scraper": metadata.get("Scraper", ""),
        },
    }


def _source_tags(metadata: dict[str, Any]) -> tuple[str, ...]:
    value = metadata.get("public_tags")
    if value is None:
        value = metadata.get("tags", ())
    if isinstance(value, str):
        return tuple(tag for tag in public_tags(value).split(";") if tag)
    return tuple(value or ())


def source_info_from_metadata(
    metadata: dict[str, Any],
    *,
    key: str,
    provider: str,
    collection_key: str = "",
    collection_label: str | None = None,
    searchable: bool,
    icon_url: str | None,
    title: str | None = None,
    homepage: str | None = None,
    media_types: tuple[str, ...] = (),
    item_count: int | None = None,
    provider_id: str | None = None,
) -> SourceInfo:
    """Build one canonical ``SourceInfo`` from catalog/archive metadata."""
    raw_size = metadata.get("size")
    raw_items = metadata.get("item_count", metadata.get("entry_count"))
    raw_fulltext = metadata.get("native_fulltext_available")
    if raw_fulltext is None:
        raw_fulltext = metadata.get("has_fulltext_index")
    display_name = metadata.get("name") or title or key
    return SourceInfo(
        key=key,
        provider=provider,
        title=title or metadata.get("title") or metadata.get("name") or key,
        collection_key=collection_key,
        collection_label=collection_label,
        searchable=searchable,
        homepage=(
            homepage if homepage is not None else metadata.get("homepage") or None
        ),
        icon_url=icon_url,
        size=int(raw_size) if raw_size is not None else None,
        intent_phrases=intent_phrases(display_name),
        media_types=tuple(media_types),
        item_count=int(raw_items) if raw_items is not None else None,
        description=metadata.get("description") or None,
        long_description=metadata.get("long_description") or None,
        creator=metadata.get("creator") or None,
        publisher=metadata.get("publisher") or None,
        language=metadata.get("language") or None,
        date=metadata.get("date") or None,
        tags=_source_tags(metadata),
        category=metadata.get("category") or None,
        license=metadata.get("license") or None,
        origin=metadata.get("origin") or None,
        attribution=metadata.get("attribution") or None,
        relation=metadata.get("relation") or None,
        mime_counts=metadata.get("mime_counts") or metadata.get("counter") or None,
        native_fulltext_available=(
            None if raw_fulltext is None else bool(raw_fulltext)
        ),
        provider_id=provider_id or metadata.get("provider_id") or metadata.get("uuid") or None,
    )


def source_info_from_record(
    record: Any,
    *,
    collection_key: str,
    collection_label: str,
    icon_url: str | None,
    searchable: bool,
    web_base_path: str = "",
) -> SourceInfo:
    """Build a ZIM ``SourceInfo`` from one generic catalog record."""
    meta = record.to_source_metadata()
    try:
        import json

        provider_metadata = json.loads(record.provider_metadata or "{}")
        if isinstance(provider_metadata, dict) and provider_metadata.get("name"):
            meta["name"] = provider_metadata["name"]
    except (TypeError, ValueError):
        pass
    if icon_url is None:
        icon_url = source_icon_url(
            record.key,
            bool(record.icon_data or record.icon_path),
            web_base_path,
        )
    return source_info_from_metadata(
        meta,
        key=record.key,
        provider="zim",
        collection_key=collection_key,
        collection_label=collection_label,
        searchable=searchable,
        icon_url=icon_url,
        title=meta.get("title"),
        homepage=meta.get("homepage"),
        media_types=classify_mime_counts(meta.get("mime_counts")),
        item_count=meta.get("item_count"),
        provider_id=meta.get("provider_id"),
    )


def collection_media_types(sources: Any) -> tuple[str, ...]:
    return ordered_media_types(
        family for source in sources for family in source.media_types
    )


__all__ = [
    "ICON_DISCOVERY_HTML_BYTES",
    "ICON_METADATA_PATH",
    "ILLUSTRATION_SIZE",
    "MAX_FAVICON_BYTES",
    "META_KEYS",
    "category_from_tags",
    "collection_media_types",
    "find_archive_icon_path",
    "header_uuid",
    "intent_phrases",
    "metadata_from_archive",
    "normalize_counter",
    "normalize_creator",
    "normalize_date",
    "normalize_tags",
    "parse_tags",
    "perma_ref",
    "public_tags",
    "resolve_homepage",
    "source_icon_url",
    "source_info_from_metadata",
    "source_info_from_record",
]
