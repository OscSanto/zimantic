"""Filtering and paging helpers for the collection/source listing APIs.

The filters operate on a detached discovery snapshot. They never walk the
filesystem and never expose a private locator: only the same display metadata
that the unfiltered listing already publishes participates in a text match.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Any

from ..media_types import MEDIA_TYPE_ORDER
from ..providers.config import BUILTIN_PROVIDERS


def visible_library_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    library = []
    hidden_counts: dict[str, int] = {}
    for source in snapshot.get("library") or []:
        if source.get("provider") != "kolibri" or source.get("item_count") != 0:
            library.append(source)
            continue
        collection = source.get("collection")
        if isinstance(collection, Mapping) and collection.get("key"):
            key = str(collection["key"])
            hidden_counts[key] = hidden_counts.get(key, 0) + 1
    collections = []
    for collection in snapshot.get("collections") or []:
        hidden = hidden_counts.get(str(collection.get("key") or ""), 0)
        if hidden:
            collection = {
                **collection,
                "source_count": max(0, int(collection.get("source_count") or 0) - hidden),
            }
        collections.append(collection)
    return {**snapshot, "library": library, "collections": collections}


def validate_provider(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in BUILTIN_PROVIDERS:
        raise ValueError(f"unknown provider: {value}")
    return value


def validate_media_type(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in MEDIA_TYPE_ORDER:
        raise ValueError(f"unknown media_type: {value}")
    return value


def validate_language(value: str | None) -> str | None:
    if value is None:
        return None
    language = value.strip().casefold()
    return language or None


def validate_collection(
    value: str | None, known_collections: Iterable[str]
) -> str | None:
    if value is None:
        return None
    if value not in set(known_collections):
        raise ValueError(f"unknown collection: {value}")
    return value


def _text_values(record: Mapping[str, Any]) -> list[str]:
    collection = record.get("collection")
    collection_values: list[str] = []
    if isinstance(collection, Mapping):
        collection_values = [
            str(collection.get("label") or ""),
            str(collection.get("title") or ""),
            str(collection.get("provider") or ""),
        ]
    return [
        str(record.get("title") or ""),
        str(record.get("label") or ""),
        str(record.get("description") or ""),
        str(record.get("long_description") or ""),
        str(record.get("provider") or ""),
        *collection_values,
    ]


@lru_cache(maxsize=64)
def _text_query(
    query: str,
) -> tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]:
    """Split a listing query into required and excluded word-prefix patterns.

    The browser applies the same rule: every bare term must match at the start
    of a word, and a ``-term`` removes records that match. Keeping both sides
    identical means an exclusion-only query such as ``-ted`` filters here
    instead of returning an empty page that the browser cannot recover from.
    """
    includes: list[re.Pattern[str]] = []
    excludes: list[re.Pattern[str]] = []
    for term in str(query).casefold().split():
        if term.startswith("-") and len(term) > 1:
            excludes.append(re.compile(r"(?<![\w])" + re.escape(term[1:])))
        elif term:
            includes.append(re.compile(r"(?<![\w])" + re.escape(term)))
    return tuple(includes), tuple(excludes)


def _matches_text(
    record: Mapping[str, Any],
    query: str | None,
    extra: Iterable[str] = (),
) -> bool:
    if not query:
        return True
    includes, excludes = _text_query(str(query))
    values = [
        str(value).casefold()
        for value in (*_text_values(record), *extra)
        if value
    ]

    def matches(pattern: re.Pattern[str]) -> bool:
        return any(pattern.search(value) for value in values)

    return all(matches(pattern) for pattern in includes) and not any(
        matches(pattern) for pattern in excludes
    )


def _matches_source(
    source: Mapping[str, Any],
    *,
    provider: str | None,
    media_type: str | None,
    language: str | None,
    collection_key: str | None,
    query: str | None,
    collection_record: Mapping[str, Any] | None = None,
) -> bool:
    if provider is not None and source.get("provider") != provider:
        return False
    if media_type is not None:
        families = source.get("media_types")
        if not isinstance(families, (list, tuple)) or media_type not in families:
            return False
    if language is not None:
        languages = {
            part.strip().casefold()
            for part in str(source.get("language") or "").split(",")
            if part.strip()
        }
        if language not in languages:
            return False
    if collection_key is not None:
        record = source.get("collection")
        if not isinstance(record, Mapping) or record.get("key") != collection_key:
            return False
    extra: list[str] = []
    if collection_record is not None:
        extra = _text_values(collection_record)
    return _matches_text(source, query, extra)


def _source_sort_key(record: Mapping[str, Any]) -> tuple[str, str]:
    title = record.get("title") or record.get("key") or ""
    return (str(title).casefold(), str(record.get("key") or ""))


def _collection_sort_key(record: Mapping[str, Any]) -> tuple[str, str]:
    title = record.get("title") or record.get("label") or record.get("key") or ""
    return (str(title).casefold(), str(record.get("key") or ""))


def filter_sources(
    sources: Iterable[Mapping[str, Any]],
    *,
    provider: str | None = None,
    media_type: str | None = None,
    language: str | None = None,
    collection: str | None = None,
    query: str | None = None,
    collections: Iterable[Mapping[str, Any]] = (),
    order: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    descriptors = {
        str(record.get("key")): record
        for record in collections
        if record.get("key")
    }
    records = []
    for source in sources:
        source_collection = source.get("collection")
        collection_key = (
            str(source_collection.get("key"))
            if isinstance(source_collection, Mapping) and source_collection.get("key")
            else None
        )
        if _matches_source(
            source,
            provider=provider,
            media_type=media_type,
            language=language,
            collection_key=collection,
            query=query,
            collection_record=descriptors.get(collection_key or ""),
        ):
            records.append(dict(source))
    if order is not None:
        fallback = len(order)
        return sorted(
            records,
            key=lambda record: (
                order.get(str(record.get("key") or ""), fallback),
                _source_sort_key(record),
            ),
        )
    return sorted(records, key=_source_sort_key)


def filter_collections(
    collections: Iterable[Mapping[str, Any]],
    sources: Iterable[Mapping[str, Any]],
    *,
    provider: str | None = None,
    media_type: str | None = None,
    language: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Filter collections using their full discovered member set.

    A nonempty collection matches when at least one member matches every source
    predicate. Empty/error collections can match their descriptor's provider
    and text, but never a specific media type because they have no members.
    """
    members: dict[str, list[Mapping[str, Any]]] = {}
    for source in sources:
        record = source.get("collection")
        if not isinstance(record, Mapping):
            continue
        key = record.get("key")
        if key:
            members.setdefault(str(key), []).append(source)

    records: list[dict[str, Any]] = []
    for collection in collections:
        record = dict(collection)
        key = str(record.get("key") or "")
        collection_members = members.get(key, [])
        if collection_members:
            matched = any(
                _matches_source(
                    member,
                    provider=provider,
                    media_type=media_type,
                    language=language,
                    collection_key=None,
                    query=query,
                    collection_record=record,
                )
                for member in collection_members
            )
        else:
            matched = (
                (provider is None or record.get("provider") == provider)
                and media_type is None
                and language is None
                and _matches_text(record, query)
            )
        if matched:
            records.append(record)
    return sorted(records, key=_collection_sort_key)


__all__ = [
    "filter_collections",
    "filter_sources",
    "validate_collection",
    "validate_language",
    "validate_media_type",
    "validate_provider",
    "visible_library_snapshot",
]
