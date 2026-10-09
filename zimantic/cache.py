"""Small thread-safe LRU cache for finalized search results.

The cache is deliberately cheap: it stores the ranked result pool, source
outcomes and counts for an exact (query, source selection) key, so a repeated
search is served without touching SQLite, FAISS or the embedding model. Results
are treated as read-only by callers.

It is bounded two ways: by entry count (``maxsize``) and by an approximate
total byte budget (``max_bytes``), because a single entry can be hundreds of
kilobytes once previews and explanations are included. On a Raspberry Pi the
byte bound is what keeps the cache from ballooning.
"""
from __future__ import annotations

from collections import OrderedDict
import sys
import threading
from typing import Any

DEFAULT_CACHE_SIZE = 256
# 32 MiB. Previews dominate entry size, so a byte budget is the real limit.
DEFAULT_CACHE_BYTES = 32 * 1024 * 1024


def _estimate_size(value: Any, _depth: int = 0) -> int:
    """Approximate the resident size of a cached value.

    Walks only the structures the search cache actually holds (dicts, lists,
    strings, numbers). It deliberately over-counts rather than under-counts;
    the byte budget is a safety limit, not an accounting ledger.
    """
    if _depth > 32:
        return 0
    if isinstance(value, str):
        return sys.getsizeof(value)
    if isinstance(value, (bytes, bytearray)):
        return sys.getsizeof(value)
    if isinstance(value, dict):
        total = sys.getsizeof(value)
        for key, item in value.items():
            total += _estimate_size(key, _depth + 1) + _estimate_size(item, _depth + 1)
        return total
    if isinstance(value, (list, tuple, set, frozenset)):
        return sys.getsizeof(value) + sum(_estimate_size(item, _depth + 1) for item in value)
    if value is None:
        return 16
    return sys.getsizeof(value)


class QueryCache:
    def __init__(
        self,
        maxsize: int = DEFAULT_CACHE_SIZE,
        max_bytes: int = DEFAULT_CACHE_BYTES,
    ):
        self.maxsize = max(0, int(maxsize))
        # A non-positive byte budget means "no byte limit" (count only).
        self.max_bytes = max(0, int(max_bytes))
        self._data: OrderedDict[Any, Any] = OrderedDict()
        self._sizes: dict[Any, int] = {}
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def disabled(self) -> bool:
        return self.maxsize <= 0

    def get(self, key: Any) -> Any | None:
        if self.disabled:
            return None
        with self._lock:
            if key not in self._data:
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return self._data[key]

    def put(self, key: Any, value: Any, size: int | None = None) -> None:
        if self.disabled:
            return
        if size is None:
            size = _estimate_size(value)
        size = max(0, int(size))
        with self._lock:
            previous = self._data.pop(key, None)
            if previous is not None:
                self._bytes -= self._sizes.pop(key, 0)
            self._data[key] = value
            self._sizes[key] = size
            self._bytes += size
            while self._data and (
                len(self._data) > self.maxsize
                or (self.max_bytes and self._bytes > self.max_bytes)
            ):
                old_key, _ = self._data.popitem(last=False)
                self._bytes -= self._sizes.pop(old_key, 0)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._sizes.clear()
            self._bytes = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._data),
                "maxsize": self.maxsize,
                "bytes": self._bytes,
                "max_bytes": self.max_bytes,
                "hits": self.hits,
                "misses": self.misses,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
