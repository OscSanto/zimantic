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

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "book": self.book,
            "mode": self.mode,
            "local_name": self.local_name,
            "available": self.available,
            "error": self.error,
            "intent_phrases": list(self.intent_phrases),
        }


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
