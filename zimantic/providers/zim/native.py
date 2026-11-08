"""libzim native full-text adapter for the generic indexing contract."""

from __future__ import annotations

import threading
from typing import Any, Sequence

from ...indexing.types import NativeHit


class ZimNativeSearcher:
    """Bounded wrapper around a libzim ``Searcher``.

    libzim searchers are not thread-safe, so one lock serializes every call.
    Process isolation remains the caller's responsibility through
    :mod:`zimantic.fulltext` when crash containment is required.
    """

    def __init__(self, archive: Any, searcher: Any | None = None) -> None:
        self.archive = archive
        self._searcher = searcher
        self._lock = threading.Lock()

    @classmethod
    def open(cls, archive: Any) -> "ZimNativeSearcher | None":
        if not getattr(archive, "has_fulltext_index", False):
            return None
        try:
            from libzim.search import Searcher

            return cls(archive, Searcher(archive))
        except Exception:
            return None

    def search(
        self,
        query: str,
        limit: int,
        *,
        deadline: float | None = None,
    ) -> Sequence[NativeHit]:
        if deadline is not None:
            import time

            if time.monotonic() >= deadline:
                return ()
        if self._searcher is None:
            return ()
        count = max(1, int(limit))
        from libzim.search import Query

        with self._lock:
            search = self._searcher.search(Query().set_query(query))
            results = search.get_results(0, count)
        return [
            NativeHit(
                path=str(result.path or ""),
                title=str(result.title or ""),
                rank=position,
            )
            for position, result in enumerate(results, 1)
            if result.path
        ]

    def close(self) -> None:
        self._searcher = None
        self.archive = None


__all__ = ["ZimNativeSearcher"]
