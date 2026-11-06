"""Dependency-free indexing contracts shared by every local provider.

This module is deliberately free of libzim, FAISS, FastAPI, and provider
imports.  A synthetic producer can therefore exercise the whole build and
search pipeline in tests without any of those optional dependencies.
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Protocol, Sequence, runtime_checkable

# SQLite row ids written to docs.rowid, vecs.id, and FAISS are non-negative
# signed 63-bit integers.  Keeping the bound in one place lets providers reject
# an unsafe native identity instead of silently merging rows.
MAX_ROW_ID = (1 << 63) - 1


class EntryRecord(NamedTuple):
    """One indexable row supplied by an :class:`EntryProducer`.

    ``excerpt`` is the short build-time embedding text.  ``body`` is the
    separately bounded lexical text indexed by the contentless FTS table; it
    may be empty for title-only rows.  Neither field promises that raw text is
    retained in the published index.

    ``id`` is the canonical SQLite row id, not an arbitrary provider-native
    identifier.  ``target_id`` is the final redirect target's row id (or
    ``None`` for a non-redirect row), while ``path`` is the final target path
    for redirects and the row's own path otherwise.
    """

    id: int
    title: str
    excerpt: str = ""
    path: str = ""
    target_id: int | None = None
    body: str = ""
    body_truncated: bool = False


@runtime_checkable
class EntryProducer(Protocol):
    """A bounded, resumable sequence of :class:`EntryRecord` values.

    The producer owns traversal order and native identity mapping.  The core
    never walks a filesystem, opens a ZIM, or queries a provider database.
    """

    key: str
    provider: str
    fingerprint: str
    count: int

    def entry_at(self, index: int) -> EntryRecord | None: ...

    def validate_snapshot(self) -> bool: ...

    def close(self) -> None: ...


class NativeHit(NamedTuple):
    """One native full-text hit before local URL/path verification."""

    path: str
    title: str = ""
    rank: int = 0


@runtime_checkable
class NativeSearcher(Protocol):
    """Optional provider-native full-text search surface.

    Implementations may isolate themselves in a subprocess; the core only sees
    bounded hits.  A provider without a native searcher simply returns none.
    """

    def search(
        self,
        query: str,
        limit: int,
        *,
        deadline: float | None = None,
    ) -> Sequence[NativeHit | Mapping[str, Any]]: ...

    def close(self) -> None: ...


@runtime_checkable
class DerivedTextInput(Protocol):
    """One provider-confined candidate input for the optional derived stage."""

    kind: str  # "captions" | "image" | "audio" | "video"
    mimetype: str
    size: int

    def open(self) -> Any: ...


@runtime_checkable
class DerivedTextProducer(Protocol):
    """Optional hook for providers that have local content bytes to enrich."""

    def derived_text_inputs(self, index: int) -> Sequence[DerivedTextInput]: ...


__all__ = [
    "DerivedTextInput",
    "DerivedTextProducer",
    "EntryProducer",
    "EntryRecord",
    "MAX_ROW_ID",
    "NativeHit",
    "NativeSearcher",
]
