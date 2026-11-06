"""Canonical content-store contracts.

This module is intentionally free of third-party imports.  Every content
provider and the provider seam import these types from here; ``providers.base``
re-exports them rather than defining a second copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ContentEntry:
    """Identity and metadata for one item addressed by a content store."""

    path: str
    title: str
    mimetype: str
    size: int | None = None
    redirect_path: str | None = None


@dataclass(frozen=True)
class ArticleData:
    """One complete or bounded (prefixed) content representation."""

    source_key: str
    path: str
    title: str
    mimetype: str
    content: bytes
    total: int | None
    truncated: bool


@dataclass(frozen=True)
class ContentRange:
    """An inclusive byte range of one content representation."""

    source_key: str
    path: str
    title: str
    mimetype: str
    content: bytes
    start: int
    end: int
    total: int


@runtime_checkable
class ContentStore(Protocol):
    """Path-addressed read-only content for one source.

    Items are addressed by path within a source; the source key distinguishes
    identical paths in different sources.  ``head_only`` is a bounded content
    prefix read for preview/metadata extraction, not an HTTP HEAD response.
    """

    def main_path(self) -> str | None: ...

    def resolve(self, path: str | None) -> ContentEntry: ...

    def exists(self, path: str) -> bool: ...

    def read(
        self, path: str | None, *, head_only: bool, max_bytes: int
    ) -> ArticleData: ...

    def read_range(
        self, path: str, *, start: int, end: int, max_bytes: int
    ) -> ContentRange: ...

    def random_path(self) -> str | None: ...

    def icon(self, max_bytes: int) -> ArticleData | None: ...

    def close(self) -> None: ...


__all__ = [
    "ArticleData",
    "ContentEntry",
    "ContentRange",
    "ContentStore",
]
