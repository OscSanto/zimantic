"""Shared data contracts for source discovery and progressive search."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, TypedDict

from .media_types import ordered_media_types

SourceStatus = Literal["ok", "partial", "error", "cancelled"]


class ResultDocument(TypedDict):
    id: str
    source_key: str
    source: str
    source_icon: str | None
    title: str
    lead: str
    path: str
    url: str
    mimetype: str | None


class SourceResultDocument(ResultDocument, total=False):
    source_rank: int


class RankedResultDocument(SourceResultDocument, total=False):
    score: float
    rank: int
    explain: str
    ranking: Mapping[str, Any]


def encode_document_id(source_key: str, reference: str) -> str:
    """Return the unambiguous ``(source_key, reference)`` document identity."""
    return f"{len(source_key.encode('utf-8'))}:{source_key}{reference}"


_LEGACY_FIELD_NAMES = frozenset(
    {
        "name",
        "book",
        "mode",
        "local_name",
        "available",
        "error",
        "icon_path",
        "uuid",
        "article_count",
        "media_count",
        "has_fulltext_index",
        "perma_ref",
        "flavour",
        "scraper",
        "source",
        "counter",
    }
)


@dataclass(frozen=True, init=False)
class SourceInfo:
    """Source metadata and the canonical identity used by result documents.

    ``provider`` is the only discriminator and ``key`` the only identity.
    ``name``, ``book``, ``local_name``, ``mode``, and ``available`` are not
    fields and are never emitted by new runtimes.  The legacy constructor/access
    forms exist only so older direct callers fail soft while the phase's own
    tests and code migrate; providers in this tree never use them.
    """

    key: str
    provider: str
    title: str | None = None
    collection_key: str = ""
    collection_label: str | None = None
    searchable: bool = False
    homepage: str | None = None
    icon_url: str | None = None
    size: int | None = None
    intent_phrases: tuple[str, ...] = ()
    media_types: tuple[str, ...] = ()
    item_count: int | None = None
    indexed_count: int | None = None
    truncated: bool | None = None
    body_truncated_count: int | None = None
    index_freshness: str | None = None
    derived_text: Mapping[str, str] | None = None
    description: str | None = None
    long_description: str | None = None
    creator: str | None = None
    publisher: str | None = None
    language: str | None = None
    date: str | None = None
    tags: tuple[str, ...] = ()
    category: str | None = None
    license: str | None = None
    origin: str | None = None
    attribution: str | None = None
    relation: str | None = None
    mime_counts: str | None = None
    native_fulltext_available: bool | None = None
    provider_id: str | None = None

    def __init__(
        self,
        key: str,
        name: str | None = None,
        book: str | None = None,
        mode: str | None = None,
        local_name: str | None = None,
        available: bool = True,
        searchable: bool | None = None,
        *,
        provider: str | None = None,
        title: str | None = None,
        collection_key: str = "",
        collection_label: str | None = None,
        homepage: str | None = None,
        icon_url: str | None = None,
        size: int | None = None,
        intent_phrases: tuple[str, ...] = (),
        media_types: tuple[str, ...] = (),
        item_count: int | None = None,
        indexed_count: int | None = None,
        truncated: bool | None = None,
        body_truncated_count: int | None = None,
        index_freshness: str | None = None,
        derived_text: Mapping[str, str] | None = None,
        description: str | None = None,
        long_description: str | None = None,
        creator: str | None = None,
        publisher: str | None = None,
        language: str | None = None,
        date: str | None = None,
        tags: tuple[str, ...] = (),
        category: str | None = None,
        license: str | None = None,
        origin: str | None = None,
        attribution: str | None = None,
        relation: str | None = None,
        mime_counts: str | None = None,
        native_fulltext_available: bool | None = None,
        provider_id: str | None = None,
        **legacy: Any,
    ) -> None:
        legacy_mode = mode
        if provider is None:
            if legacy_mode is None:
                raise TypeError(
                    "SourceInfo requires an explicit provider; there is no "
                    "fallback provider"
                )
            provider = "kiwix" if legacy_mode == "kiwix" else "zim"
        if provider not in {"zim", "kiwix", "files", "kolibri"}:
            raise ValueError(f"unknown provider {provider!r}")
        if title is None:
            title = name if name is not None else legacy.get("title")
        values = {
            "key": key,
            "provider": provider,
            "title": title,
            "collection_key": collection_key,
            "collection_label": collection_label,
            "searchable": (
                (True if legacy_mode else False)
                if searchable is None
                else bool(searchable)
            )
            and (available if legacy_mode else True),
            "homepage": homepage,
            "icon_url": icon_url,
            "size": size,
            "intent_phrases": tuple(intent_phrases or ()),
            "media_types": tuple(media_types or ()),
            "item_count": item_count,
            "indexed_count": indexed_count,
            "truncated": truncated,
            "body_truncated_count": body_truncated_count,
            "index_freshness": index_freshness,
            "derived_text": (
                MappingProxyType(dict(derived_text))
                if derived_text is not None
                else None
            ),
            "description": description,
            "long_description": long_description,
            "creator": creator,
            "publisher": publisher,
            "language": language,
            "date": date,
            "tags": tuple(tags or ()),
            "category": category,
            "license": license,
            "origin": origin,
            "attribution": attribution,
            "relation": relation,
            "mime_counts": mime_counts,
            "native_fulltext_available": native_fulltext_available,
            "provider_id": provider_id,
        }
        for name_, value in values.items():
            object.__setattr__(self, name_, value)
        # Legacy-only values are kept off the dataclass field list so canonical
        # serialization and introspection expose only the phase-00 contract.
        object.__setattr__(self, "_legacy", legacy_mode is not None)
        object.__setattr__(self, "_legacy_name", name)
        object.__setattr__(self, "_legacy_book", book)
        object.__setattr__(self, "_legacy_local_name", local_name)
        object.__setattr__(self, "_legacy_mode", legacy_mode)
        object.__setattr__(self, "_legacy_available", bool(available))
        object.__setattr__(self, "_legacy_error", legacy.get("error"))
        for name_ in _LEGACY_FIELD_NAMES:
            if name_ in {
                "name",
                "book",
                "mode",
                "local_name",
                "available",
                "error",
            }:
                continue
            object.__setattr__(self, f"_legacy_{name_}", legacy.get(name_))

    # -- legacy accessors (not dataclass fields, never used by new runtimes) --
    def _legacy_value(self, name: str) -> Any:
        if not self._legacy:
            raise AttributeError(name)
        return self.__dict__.get(f"_legacy_{name}")

    @property
    def name(self) -> str | None:
        return self._legacy_value("name")

    @property
    def book(self) -> str | None:
        return self._legacy_value("book")

    @property
    def local_name(self) -> str | None:
        return self._legacy_value("local_name")

    @property
    def mode(self) -> str | None:
        return self._legacy_value("mode")

    @property
    def available(self) -> bool:
        return self._legacy_value("available")

    @property
    def error(self) -> str | None:
        return self._legacy_value("error")

    def __getattr__(self, name: str) -> Any:
        if name in _LEGACY_FIELD_NAMES:
            return self._legacy_value(name)
        raise AttributeError(name)

    def to_dict(self, include_metadata: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "provider": self.provider,
            "collection": (
                {
                    "key": self.collection_key,
                    "label": self.collection_label,
                    "provider": self.provider,
                }
                if self.collection_key or self.collection_label
                else None
            ),
            "title": self.title,
            "searchable": self.searchable,
            "homepage": self.homepage,
            "icon_url": self.icon_url,
            "size": self.size,
            "intent_phrases": list(self.intent_phrases),
            "media_types": list(ordered_media_types(self.media_types)),
            "item_count": self.item_count,
            "indexed_count": self.indexed_count,
            "truncated": self.truncated,
            "body_truncated_count": self.body_truncated_count,
            "index_freshness": self.index_freshness,
            "derived_text": (
                dict(self.derived_text) if self.derived_text is not None else None
            ),
        }
        if include_metadata:
            payload.update(
                {
                    "description": self.description,
                    "long_description": self.long_description,
                    "creator": self.creator,
                    "publisher": self.publisher,
                    "language": self.language,
                    "date": self.date,
                    "tags": list(self.tags),
                    "category": self.category,
                    "license": self.license,
                    "origin": self.origin,
                    "attribution": self.attribution,
                    "relation": self.relation,
                    "mime_counts": self.mime_counts,
                    "native_fulltext_available": self.native_fulltext_available,
                    "provider_id": self.provider_id,
                }
            )
        if getattr(self, "_legacy", False):
            # Legacy-constructed test doubles only. Runtime sources never take
            # this branch, so no provider emits these fields.
            payload.update(
                {
                    "name": self._legacy_name,
                    "book": self._legacy_book,
                    "mode": self._legacy_mode,
                    "local_name": self._legacy_local_name,
                    "available": self._legacy_available,
                    "error": self._legacy_error,
                }
            )
            if include_metadata:
                for name in (
                    "uuid",
                    "article_count",
                    "media_count",
                    "has_fulltext_index",
                    "perma_ref",
                    "flavour",
                    "scraper",
                    "source",
                    "counter",
                    "icon_path",
                ):
                    payload[name] = getattr(self, f"_legacy_{name}", None)
        return payload


@dataclass
class SourceResult:
    """Results and status returned by one source worker."""

    source: SourceInfo
    items: list[SourceResultDocument] = field(default_factory=list)
    semantic: list[tuple[float, ResultDocument]] = field(default_factory=list)
    keyword: list[tuple[float, ResultDocument]] = field(default_factory=list)
    fulltext: list[tuple[int, ResultDocument]] = field(default_factory=list)
    error: str | None = None
    status: SourceStatus = "ok"

    def __post_init__(self) -> None:
        if self.status not in {"ok", "partial", "error", "cancelled"}:
            raise ValueError(f"invalid source status {self.status!r}")
        if self.status == "ok" and self.error is not None:
            raise ValueError("a successful source result must not carry an error")
        if self.status != "ok" and self.error is None:
            self.error = self.status

    def to_event(self, count: int | None = None) -> dict[str, Any]:
        """Compact progress event for one source."""
        event: dict[str, Any] = {
            "source": self.source.to_dict(),
            "error": self.error,
            "status": self.status,
        }
        if count is not None:
            event["count"] = count
        return event
