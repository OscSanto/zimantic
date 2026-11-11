"""Shared data contracts for source discovery and progressive search."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, NotRequired, TypedDict

from .media_types import ordered_media_types

SourceStatus = Literal["ok", "partial", "error", "cancelled"]


class ResourceMetadata(TypedDict, total=False):
    kind: str
    description: str
    author: str
    language: str
    grade_levels: list[str]
    duration: int
    tags: list[str]
    learning_activities: list[str]
    assessment_count: int
    topic_position: int
    topic_total: int


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
    breadcrumbs: NotRequired[list[str]]
    resource: NotRequired[ResourceMetadata]


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


@dataclass(frozen=True)
class SourceInfo:
    """Source metadata and the canonical identity used by result documents."""

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
    index_partial: bool | None = None
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

    def __post_init__(self) -> None:
        if self.provider not in {"zim", "kiwix", "files", "kolibri"}:
            raise ValueError(f"unknown provider {self.provider!r}")
        object.__setattr__(self, "intent_phrases", tuple(self.intent_phrases or ()))
        object.__setattr__(self, "media_types", tuple(self.media_types or ()))
        object.__setattr__(self, "tags", tuple(self.tags or ()))
        if self.derived_text is not None:
            object.__setattr__(
                self, "derived_text", MappingProxyType(dict(self.derived_text))
            )

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
            "index_partial": self.index_partial,
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
