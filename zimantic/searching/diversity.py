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
    semantic_mask: list[bool] | None = None,
    sources: list[str] | None = None,
    limit: int | None = None,
) -> list[int]:
    """Use stable greedy ordering, optionally balancing source exposure.

    Semantic ordering uses equal-weight MMR: relevance minus maximum cosine
    similarity to the selected prefix, preferring novelty on equal MMR.
    Lexical ordering minimizes maximum title-word Jaccard overlap, placing
    empty/stopword-only titles last. Mixed pools retain the input's semantic/
    lexical slot pattern rather than comparing unlike raw novelty scores.
    """
    count = len(titles)
    if limit is None:
        limit = count
    if not count or limit <= 0:
        return []
    words = [set(_TITLE_WORD.findall(title.casefold())) - STOPWORDS for title in titles]
    lexical_nearest = [0.0] * count
    semantic = [False] * count if vectors is None else (
        [True] * count if semantic_mask is None else semantic_mask
    )
    lexical_positions = {position for position in range(count) if not semantic[position]}
    if vectors is not None:
        import numpy as np

        nearest = np.zeros(count, dtype=np.float32)
        relevance = np.asarray(relevance, dtype=np.float32)
    exposure = dict.fromkeys(sources or [], 0)
    remaining = set(range(count))
    selected = []
    while remaining and len(selected) < limit:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("diversity deadline expired")
        candidates = {
            position for position in remaining if semantic[position] or words[position]
        } or remaining
        if sources is not None:
            minimum = min(exposure[sources[position]] for position in candidates)
            candidates = {position for position in candidates if exposure[sources[position]] == minimum}
        wanted = semantic[len(selected)]
        candidates = {position for position in candidates if semantic[position] == wanted} or candidates

        def priority(position):
            redundancy = max(float(nearest[position]), lexical_nearest[position]) if semantic[position] else lexical_nearest[position]
            return (
                float(relevance[position]) - redundancy if semantic[position] else -redundancy,
                -redundancy,
                -position,
            )

        chosen = max(candidates, key=priority)
        selected.append(chosen)
        remaining.remove(chosen)
        if sources is not None:
            exposure[sources[chosen]] += 1
        for position in remaining if not semantic[chosen] else remaining & lexical_positions:
            lexical_nearest[position] = max(
                lexical_nearest[position],
                len(words[position] & words[chosen])
                / max(1, len(words[position] | words[chosen])),
            )
        if semantic[chosen]:
            np.maximum(nearest, vectors @ vectors[chosen], out=nearest)
    return selected
