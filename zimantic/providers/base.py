"""Provider protocols and provider/collection value objects.

Content types are imported from their canonical definition in
:mod:`zimantic.content.store` and re-exported here; this module never defines a
second copy.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

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

if TYPE_CHECKING:
    from ..searching.index import IndexHandle

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
    "EnrichingRuntime",
    "IndexedRuntime",
    "IsolatedFulltextRuntime",
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
    # A process-scoped ZIM selection.  Providers that support an ephemeral
    # scope (currently ZIM) resolve only these sources and never scan a
    # configured collection.  Other providers ignore it.
    open_scope: Any | None = None


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
    # Observability fields added by phase 09.  Providers that do not own local
    # indexes (Kiwix) leave the index fields empty rather than inventing zeroes.
    last_refresh: float | None = None
    collapsed: bool = False
    index_count: int | None = None
    index_states: tuple[str, ...] = ()
    indexed_count: int | None = None
    truncated: bool | None = None
    body_truncated_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "media_types", ordered_media_types(self.media_types)
        )
        object.__setattr__(
            self, "index_states", tuple(sorted(str(item) for item in self.index_states))
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
                "last_refresh": self.last_refresh,
                "collapsed": self.collapsed,
                "index_count": self.index_count,
                "index_states": list(self.index_states),
                "indexed_count": self.indexed_count,
                "truncated": self.truncated,
                "body_truncated_count": self.body_truncated_count,
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
    config: ProviderConfig

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


class IndexedRuntime(CollectionRuntime, ABC):
    """Declared capability for locally indexed search and source maintenance."""

    indexes: Mapping[str, IndexHandle]

    def source_paths(self, source: SourceInfo) -> Mapping[str, Path]:
        """Private backing locations, when available, for CLI use only."""
        return {}

    @abstractmethod
    def bind_search(
        self,
        *,
        semantic: bool,
        defer_faiss: bool,
        embedder_getter: Callable[[], Any] | None = None,
        embedder_ready_getter: Callable[[], Any] | None = None,
        model_fingerprint_getter: Callable[[], str] | None = None,
        preview_cache: Any | None = None,
        fulltext_pool: Any | None = None,
        candidate_count: int | None = None,
        preview_chars: int | None = None,
        max_html_bytes: int | None = None,
        max_article_bytes: int | None = None,
        min_cosine_similarity: float | None = None,
        cfg: dict[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    def start_faiss(self) -> None: ...

    @abstractmethod
    def refresh_if_stale(self) -> bool: ...

    @abstractmethod
    def random_source(self) -> SourceInfo: ...

    @abstractmethod
    def health(self) -> list[dict[str, Any]]: ...


class EnrichingRuntime(ABC):
    """Declared capability for content-based result enrichment."""

    @abstractmethod
    def enrich(
        self,
        source: SourceInfo,
        result_docs: list[dict[str, Any]],
        source_docs: dict[str, dict[str, Any]],
        *,
        phrase_ids: set[str],
        phrase_words: list[str],
        max_html_bytes: int,
        max_article_bytes: int,
    ) -> bool: ...


class IsolatedFulltextRuntime(ABC):
    """Declared capability for helper-process native full-text search."""

    @abstractmethod
    def start_fulltext_isolation(self, workers: int | None = None) -> None: ...


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
