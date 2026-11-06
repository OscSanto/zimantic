"""Provider protocols and provider/collection value objects.

Content types are imported from their canonical definition in
:mod:`zimantic.content.store` and re-exported here; this module never defines a
second copy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence, runtime_checkable

from ..content.store import ArticleData, ContentEntry, ContentRange, ContentStore
from ..indexing.types import EntryProducer
from ..media_types import MEDIA_TYPE_ORDER, ordered_media_types
from ..contracts import (
    RankedResultDocument,
    ResultDocument,
    SourceInfo,
    SourceResult,
    SourceResultDocument,
    SourceStatus,
)
from .config import ProviderConfig, ProviderKind, RuntimeConfig

DiscoveryState = Literal["loading", "ready", "error", "closed"]

__all__ = [
    "ArticleData",
    "BuildableRuntime",
    "CollectionInfo",
    "CollectionRuntime",
    "CollectionSnapshot",
    "ContentEntry",
    "ContentRange",
    "ContentStore",
    "DiscoveryBatch",
    "DiscoveryState",
    "MEDIA_TYPE_ORDER",
    "ProviderContext",
    "ProviderKind",
    "RankedResultDocument",
    "ResultDocument",
    "SourceInfo",
    "SourceProvider",
    "SourceQuery",
    "SourceResult",
    "SourceResultDocument",
    "SourceStatus",
]


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class ProviderContext:
    config: RuntimeConfig
    index_dir: Path
    web_base_path: str
    logger: logging.Logger
    archive_factory: Any | None = None
    persist_catalog: bool = True
    on_sources_changed: Callable[[], Any] | None = None


@dataclass(frozen=True)
class CollectionInfo:
    """Immutable collection descriptor (identity and display metadata only)."""

    key: str
    provider: ProviderKind
    label: str
    title: str | None = None
    description: str | None = None
    icon_url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        """Serialize only the immutable descriptor fields."""
        return {
            "key": self.key,
            "provider": self.provider,
            "label": self.label,
            "title": self.title,
            "description": self.description,
            "icon_url": self.icon_url,
        }


@dataclass(frozen=True)
class CollectionSnapshot:
    """One published, immutable collection snapshot."""

    collection: CollectionInfo
    source_count: int
    media_types: tuple[str, ...]
    error: str | None
    discovery_state: DiscoveryState
    snapshot_version: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "media_types", ordered_media_types(self.media_types)
        )

    def to_dict(self) -> dict[str, Any]:
        """Descriptor plus current runtime fields, never mutating either."""
        payload = self.collection.to_dict()
        payload.update(
            {
                "source_count": self.source_count,
                "media_types": list(self.media_types),
                "error": self.error,
                "discovery_state": self.discovery_state,
                "snapshot_version": self.snapshot_version,
            }
        )
        return payload


@dataclass(frozen=True)
class SourceQuery:
    query: str
    title_query: str
    fulltext_query: str
    query_vector: Any | None
    limit: int
    deadline: float | None = None  # monotonic; cap provider I/O to remaining time


class CollectionRuntime(Protocol):
    collection: CollectionInfo

    @property
    def snapshot(self) -> CollectionSnapshot: ...

    @property
    def sources(self) -> Sequence[SourceInfo]: ...

    def library_sources(self) -> Sequence[SourceInfo]: ...

    def search(self, source: SourceInfo, request: SourceQuery) -> SourceResult: ...

    def suggest(
        self, source: SourceInfo, query: str, limit: int
    ) -> Sequence[ResultDocument]: ...

    def preview(
        self,
        source: SourceInfo,
        document: ResultDocument,
        *,
        max_chars: int,
        max_bytes: int,
        deadline: float | None,
    ) -> tuple[str, str | None]: ...

    def content_store(self, source: SourceInfo) -> ContentStore | None: ...

    def max_sources_per_search(self) -> int | None: ...

    def refresh(self, *, wait: bool = True) -> bool: ...

    def wait_ready(self, timeout: float | None = None) -> bool: ...

    @property
    def discovery_state(self) -> DiscoveryState: ...

    @property
    def snapshot_version(self) -> int: ...

    def close(self) -> None: ...


@runtime_checkable
class BuildableRuntime(CollectionRuntime, Protocol):
    """A local runtime that can supply a bounded index build producer."""

    def entry_producer(self, source: SourceInfo, *, fast: bool) -> EntryProducer: ...


class DiscoveryBatch(Protocol):
    collections: Sequence[CollectionInfo]

    def open(self, collection: CollectionInfo) -> CollectionRuntime: ...

    def close(self) -> None: ...


class SourceProvider(Protocol):
    kind: ProviderKind

    def discover(
        self, config: ProviderConfig, ctx: ProviderContext
    ) -> DiscoveryBatch: ...
