"""Shared data contracts for source discovery and progressive search."""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceInfo:
    """A searchable collection and the backend currently serving it."""

    key: str
    name: str
    book: str
    mode: str
    local_name: str | None = None
    available: bool = True
    error: str | None = None
    intent_phrases: tuple[str, ...] = ()
    homepage: str | None = None
    icon_path: str | None = None
    icon_url: str | None = None
    title: str | None = None
    description: str | None = None
    long_description: str | None = None
    creator: str | None = None
    language: str | None = None
    date: str | None = None
    tags: tuple[str, ...] = ()
    size: int | None = None
    article_count: int | None = None

    def to_dict(self, include_metadata: bool = False) -> dict[str, Any]:
        # ``icon_path`` is deliberately not exposed: it is a server-side lookup
        # key used to serve ``/source-icon/{zim}`` and is meaningless to
        # clients. The derived ``icon_url`` is the public form.
        payload = {
            "key": self.key,
            "name": self.name,
            "book": self.book,
            "mode": self.mode,
            "local_name": self.local_name,
            "available": self.available,
            "error": self.error,
            "intent_phrases": list(self.intent_phrases),
            "homepage": self.homepage,
            "icon_url": self.icon_url,
        }
        if include_metadata:
            payload.update({
                "title": self.title,
                "description": self.description,
                "long_description": self.long_description,
                "creator": self.creator,
                "language": self.language,
                "date": self.date,
                "tags": list(self.tags),
                "size": self.size,
                "article_count": self.article_count,
            })
        return payload


@dataclass
class SourceResult:
    """Results and status returned by one source worker."""

    source: SourceInfo
    items: list[dict[str, Any]] = field(default_factory=list)
    semantic: list[tuple[float, dict[str, Any]]] = field(default_factory=list)
    keyword: list[tuple[float, dict[str, Any]]] = field(default_factory=list)
    fulltext: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    error: str | None = None
    status: str = "ok"

    def to_event(self, count: int | None = None) -> dict[str, Any]:
        """Compact progress event for one source.

        Full per-source items are intentionally omitted: the client renders the
        ranked pool page-by-page and filters on the server, so shipping every
        source's candidate list would only duplicate the ranked payload and
        bloat the cache.
        """
        event: dict[str, Any] = {
            "source": self.source.to_dict(),
            "error": self.error,
            "status": self.status,
        }
        if count is not None:
            event["count"] = count
        return event
