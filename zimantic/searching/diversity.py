"""Stable greedy novelty ordering for bounded candidate pools."""

from __future__ import annotations

import re
import time

from .query import STOPWORDS

_TITLE_WORD = re.compile(r"\w+", re.UNICODE)


def diverse_order(
    titles: list[str],
    deadline: float | None = None,
    *,
    vectors=None,
    relevance=None,
    sources: list[str] | None = None,
    limit: int | None = None,
) -> list[int]:
    """Use stable greedy ordering, optionally balancing source exposure.

    Semantic ordering uses equal-weight MMR: relevance minus maximum cosine
    similarity to the selected prefix, preferring novelty on equal MMR.
    Lexical ordering minimizes maximum title-word Jaccard overlap, placing
    empty/stopword-only titles last.
    """
    count = len(titles)
    if limit is None:
        limit = count
    if not count or limit <= 0:
        return []
    if vectors is None:
        words = [set(_TITLE_WORD.findall(title.casefold())) - STOPWORDS for title in titles]
        nearest = [0.0] * count
    else:
        import numpy as np

        nearest = np.zeros(count, dtype=np.float32)
        relevance = np.asarray(relevance, dtype=np.float32)
    exposure = dict.fromkeys(sources or [], 0)
    remaining = set(range(count))
    selected = []
    while remaining and len(selected) < limit:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("diversity deadline expired")

        def priority(position):
            return (
                bool(words[position]) if vectors is None else True,
                -exposure[sources[position]] if sources is not None else 0,
                -nearest[position] if vectors is None else float(relevance[position] - nearest[position]),
                -nearest[position],
                -position,
            )

        chosen = max(remaining, key=priority)
        selected.append(chosen)
        remaining.remove(chosen)
        if sources is not None:
            exposure[sources[chosen]] += 1
        if vectors is None:
            for position in remaining:
                nearest[position] = max(
                    nearest[position],
                    len(words[position] & words[chosen])
                    / max(1, len(words[position] | words[chosen])),
                )
        else:
            np.maximum(nearest, vectors @ vectors[chosen], out=nearest)
    return selected
