"""Small thread-safe LRU cache for finalized search results.

The cache is deliberately tiny and cheap: it stores the final ranked list and
completed source outcomes for an exact (query, source selection, limit) key, so
a repeated search is served without touching SQLite, FAISS or the embedding
model. Results are treated as read-only by callers.
"""
from __future__ import annotations

from collections import OrderedDict
import threading
from typing import Any

DEFAULT_CACHE_SIZE = 256


class QueryCache:
    def __init__(self, maxsize: int = DEFAULT_CACHE_SIZE):
        self.maxsize = max(0, int(maxsize))
        self._data: OrderedDict[Any, Any] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Any) -> Any | None:
        if self.maxsize <= 0:
            return None
        with self._lock:
            if key not in self._data:
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return self._data[key]

    def put(self, key: Any, value: Any) -> None:
        if self.maxsize <= 0:
            return
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"size": len(self._data), "maxsize": self.maxsize, "hits": self.hits, "misses": self.misses}

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
