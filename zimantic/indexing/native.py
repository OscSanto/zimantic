"""Optional provider-native full-text search contracts.

``NativeSearcher`` is defined in :mod:`zimantic.indexing.types` so the
dependency-free types module can be imported on its own.  This module provides
the small adapter helpers used by the generic search path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .types import NativeHit, NativeSearcher

__all__ = ["NativeHit", "NativeSearcher", "coerce_native_hits"]


def coerce_native_hits(
    hits: Sequence[NativeHit | Mapping[str, Any]] | None,
    *,
    limit: int | None = None,
) -> list[NativeHit]:
    """Normalize provider-native hits without trusting arbitrary mappings."""
    normalized: list[NativeHit] = []
    for position, hit in enumerate(hits or (), 1):
        if isinstance(hit, NativeHit):
            item = hit
        elif isinstance(hit, Mapping):
            path = str(hit.get("path") or "")
            if not path:
                continue
            item = NativeHit(
                path=path,
                title=str(hit.get("title") or ""),
                rank=int(hit.get("rank") or position),
            )
        else:
            path = getattr(hit, "path", "")
            if not path:
                continue
            item = NativeHit(
                path=str(path),
                title=str(getattr(hit, "title", "") or ""),
                rank=int(getattr(hit, "rank", position) or position),
            )
        if not item.path:
            continue
        normalized.append(item)
        if limit is not None and len(normalized) >= limit:
            break
    return normalized
