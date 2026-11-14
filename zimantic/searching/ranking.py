"""Pure ranking and scoring helpers for hybrid search."""

import re
from dataclasses import dataclass
from typing import Any

from ..contracts import SourceInfo, SourceResult
from .query import (
    DEFAULT_DISAMBIGUATION_BOOST,
    DEFAULT_DISAMBIGUATION_PENALTY,
    DEFAULT_USER_PAGE_PENALTY,
    DISAMBIGUATION_TITLE,
    USER_PAGE_PATH,
    _content_terms,
    _density,
    _disambiguation_intent,
    _intent_match,
    _phrase,
    _query_terms,
    _user_page_intent,
)

DEFAULT_LONG_QUERY = 10
RRF_K = 60
SEMANTIC_WEIGHT = 1.0
KEYWORD_WEIGHT = 1.0
FULLTEXT_WEIGHT = 1.0
LONG_FULLTEXT_WEIGHT = 2.0


def _deduplicate_ranked(ranked):
    seen: set[str] = set()
    unique = []
    for item in ranked:
        identity = item[2]["id"]
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(item)
    return unique


def _collect_ranked_candidates(source_results: list[SourceResult]):
    semantic: list[tuple[float, str, dict[str, Any]]] = []
    keyword: list[tuple[float, str, dict[str, Any]]] = []
    fulltext: list[tuple[int, str, dict[str, Any]]] = []
    docs: dict[str, dict[str, Any]] = {}
    source_by_key = {result.source.key: result.source for result in source_results}

    for result in source_results:
        for doc in result.items:
            docs[doc["id"]] = doc
        for score, doc in result.semantic:
            semantic.append((score, result.source.key, doc))
        for score, doc in result.keyword:
            keyword.append((score, result.source.key, doc))
        for rank, doc in result.fulltext:
            fulltext.append((rank, result.source.key, doc))

    semantic.sort(
        key=lambda item: (
            -item[0],
            (source_by_key[item[1]].title or source_by_key[item[1]].key).casefold(),
            item[2]["path"],
        )
    )
    keyword.sort(
        key=lambda item: (
            item[0],
            (source_by_key[item[1]].title or source_by_key[item[1]].key).casefold(),
            item[2]["path"],
        )
    )
    fulltext.sort(
        key=lambda item: (
            item[0],
            (source_by_key[item[1]].title or source_by_key[item[1]].key).casefold(),
            item[2]["path"],
        )
    )
    return (
        _deduplicate_ranked(semantic),
        _deduplicate_ranked(keyword),
        _deduplicate_ranked(fulltext),
        docs,
        source_by_key,
    )


def _rrf_scores(
    ranked_lists,
    query: str,
    candidate_limit: int,
) -> tuple[dict[str, float], dict[str, dict[str, dict[str, Any]]], float]:
    scores: dict[str, float] = {}
    fulltext_weight = (
        LONG_FULLTEXT_WEIGHT
        if len(re.findall(r"\w+", query, re.UNICODE)) >= DEFAULT_LONG_QUERY
        else FULLTEXT_WEIGHT
    )
    details: dict[str, dict[str, dict[str, Any]]] = {}
    for ranked, method, weight in (
        (ranked_lists[0], "semantic", SEMANTIC_WEIGHT),
        (ranked_lists[1], "title", KEYWORD_WEIGHT),
        (ranked_lists[2], "fulltext", fulltext_weight),
    ):
        for rank, (raw_score, _, doc) in enumerate(ranked[:candidate_limit]):
            contribution = weight / (RRF_K + rank + 1)
            scores[doc["id"]] = scores.get(doc["id"], 0.0) + contribution
            details.setdefault(doc["id"], {})[method] = {
                "rank": rank + 1,
                "score": round(raw_score, 6),
                "weight": weight,
                "contribution": round(contribution, 6),
            }
    return scores, details, fulltext_weight


@dataclass(slots=True)
class _RankedAssessment:
    """Scored candidate plus everything needed to render its response dict."""

    identity: str
    doc: dict[str, Any]
    query_words: list[str]
    title_tokens: list[str]
    title_matches: int
    title_coverage: float
    title_density: float
    title_quality: float
    title_phrase_hit: bool
    content_phrase_hit: bool
    intent: float
    total: float
    lexical: float
    source_rank: int
    source_casefold: str
    path: str
    rrf_score: float
    rrf_details: dict[str, dict[str, dict[str, Any]]]
    semantic_hit: bool
    fulltext_weight: float
    disambig: dict[str, Any]
    user_page: dict[str, Any]


def _assess_ranked_document(
    identity: str,
    doc: dict[str, Any],
    query_words: list[str],
    query: str,
    source_by_key: dict[str, SourceInfo],
    scores: dict[str, float],
    rrf_details: dict[str, dict[str, dict[str, Any]]],
    fulltext_weight: float,
) -> _RankedAssessment:
    title_tokens = doc.get("_title_tokens")
    if title_tokens is None:
        title_tokens = _content_terms(doc["title"])
        doc["_title_tokens"] = title_tokens
    title_token_set = set(title_tokens)
    title_matches = sum(term in title_token_set for term in query_words)
    title_coverage = title_matches / len(query_words) if query_words else 0.0
    title_density = _density(query_words, title_tokens)
    title_quality = title_coverage * (1.0 + title_density) / 2.0
    title_phrase_hit = _phrase(query_words, title_tokens)
    content_phrase_hit = bool(doc.get("_content_phrase"))
    phrase_hit = max(title_phrase_hit, content_phrase_hit)
    source = source_by_key.get(doc["source_key"])
    intent = _intent_match(source, query) if source is not None else 0.0
    lexical = (5.0 * title_quality + 2.0 * phrase_hit + intent) / 8.0
    source_rank = int(doc.get("source_rank", 100000))
    rrf_score = scores.get(identity, 0.0)
    semantic_hit = "semantic" in rrf_details.get(identity, {})
    base_score = rrf_score + 0.02 * lexical
    total = base_score
    disambig = {
        "action": "none",
        "factor": 1.0,
        "adjustment": 0.0,
    }
    if DISAMBIGUATION_TITLE.search(doc["title"]):
        if _disambiguation_intent(query, query_words, doc["title"]):
            total += DEFAULT_DISAMBIGUATION_BOOST
            disambig = {
                "action": "boost",
                "factor": 1.0,
                "adjustment": round(DEFAULT_DISAMBIGUATION_BOOST, 6),
            }
        else:
            total *= DEFAULT_DISAMBIGUATION_PENALTY
            disambig = {
                "action": "demote",
                "factor": DEFAULT_DISAMBIGUATION_PENALTY,
                "adjustment": round(total - base_score, 6),
            }
    user_page = bool(USER_PAGE_PATH.search(str(doc.get("path", ""))))
    user_page_action = "none"
    user_page_factor = 1.0
    user_page_adjustment = 0.0
    if user_page and not _user_page_intent(query_words):
        before_user_page = total
        total *= DEFAULT_USER_PAGE_PENALTY
        user_page_action = "demote"
        user_page_factor = DEFAULT_USER_PAGE_PENALTY
        user_page_adjustment = total - before_user_page
    return _RankedAssessment(
        identity=identity,
        doc=doc,
        query_words=query_words,
        title_tokens=title_tokens,
        title_matches=title_matches,
        title_coverage=title_coverage,
        title_density=title_density,
        title_quality=title_quality,
        title_phrase_hit=title_phrase_hit,
        content_phrase_hit=content_phrase_hit,
        intent=intent,
        total=total,
        lexical=lexical,
        source_rank=source_rank,
        source_casefold=doc["source"].casefold(),
        path=doc["path"],
        rrf_score=rrf_score,
        rrf_details=rrf_details,
        semantic_hit=semantic_hit,
        fulltext_weight=fulltext_weight,
        disambig=disambig,
        user_page={
            "action": user_page_action,
            "factor": user_page_factor,
            "adjustment": round(user_page_adjustment, 6),
        },
    )


def _render_ranked_document(assessment: _RankedAssessment) -> dict[str, Any]:
    """Build the public/ranking-friendly dict for a surviving candidate."""
    a = assessment
    title_phrase_hit = a.title_phrase_hit
    phrase_hit = max(title_phrase_hit, a.content_phrase_hit)
    user_page_action = a.user_page["action"]
    explanation = (
        f"title {round(a.title_coverage * 100)}% "
        f"({a.title_matches}/{len(a.query_words) if a.query_words else 0}, "
        f"density {round(a.title_density * 100)}%), "
        f"phrase {'yes' if phrase_hit else 'no'}, "
        f"intent {'yes' if a.intent else 'no'}"
        f"{', disambiguation ' + a.disambig['action'] if a.disambig['action'] != 'none' else ''}"
        f"{', user page demote' if user_page_action == 'demote' else ''}"
    )
    output = dict(a.doc)
    output.pop("_content_phrase", None)
    output.pop("_title_tokens", None)
    output.update(
        {
            "score": a.total,
            "explain": explanation,
            "rank": a.source_rank,
            "ranking": {
                "final_score": round(a.total, 6),
                "rrf_score": round(a.rrf_score, 6),
                "rrf": {
                    method: a.rrf_details.get(a.identity, {}).get(
                        method,
                        {
                            "rank": None,
                            "score": None,
                            "weight": weight,
                            "contribution": 0.0,
                        },
                    )
                    for method, weight in (
                        ("semantic", SEMANTIC_WEIGHT),
                        ("title", KEYWORD_WEIGHT),
                        ("fulltext", a.fulltext_weight),
                    )
                },
                "title": {
                    "query_terms": a.query_words,
                    "matches": a.title_matches,
                    "term_count": len(a.query_words),
                    "coverage": round(a.title_coverage, 6),
                    "density": round(a.title_density, 6),
                    "quality": round(a.title_quality, 6),
                },
                "phrase": bool(phrase_hit),
                "content_phrase": a.content_phrase_hit,
                "semantic_hit": a.semantic_hit,
                "intent": bool(a.intent),
                "lexical_score": round(a.lexical, 6),
                "disambiguation": a.disambig,
                "user_page": a.user_page,
                "source_rank": a.source_rank,
            },
        }
    )
    return output


def rank_results(
    source_results: list[SourceResult],
    query: str,
    limit: int | None,
    candidate_count: int,
) -> list[dict[str, Any]]:
    """Rank candidates, optionally without cutting the result list.

    ``limit=None`` keeps every retrieved candidate and gives every channel hit
    an RRF contribution. Callers that need to order a complete candidate set
    before their own display cut can use that form.
    """
    query_words = _query_terms(query)
    semantic, keyword, fulltext, docs, source_by_key = _collect_ranked_candidates(
        source_results
    )
    scores, rrf_details, fulltext_weight = _rrf_scores(
        (semantic, keyword, fulltext),
        query,
        len(docs) if limit is None else max(candidate_count, limit),
    )
    assessed = [
        _assess_ranked_document(
            identity,
            doc,
            query_words,
            query,
            source_by_key,
            scores,
            rrf_details,
            fulltext_weight,
        )
        for identity, doc in docs.items()
    ]
    assessed.sort(
        key=lambda item: (
            -item.total,
            not item.semantic_hit,
            -item.lexical,
            -item.title_quality,
            item.source_rank,
            item.source_casefold,
            item.path,
        )
    )
    ranked = []
    selected = assessed if limit is None else assessed[:limit]
    for final_rank, assessment in enumerate(selected, 1):
        output = _render_ranked_document(assessment)
        output["ranking"]["final_rank"] = final_rank
        ranked.append(output)
    return ranked


def matched_by(pool: list[dict[str, Any]]) -> dict[str, int]:
    method_counts = {"semantic": 0, "title": 0, "fulltext": 0}
    for doc in pool:
        matches = doc.get("ranking", {}).get("rrf", {})
        for method in method_counts:
            if matches.get(method, {}).get("rank") is not None:
                method_counts[method] += 1
    return method_counts


def source_items(
    semantic: list[tuple[float, dict[str, Any]]],
    keyword: list[tuple[float, dict[str, Any]]],
    fulltext: list[tuple[int, dict[str, Any]]],
) -> list[dict[str, Any]]:
    order: dict[str, tuple[int, int, str]] = {}
    for rank, (_, doc) in enumerate(semantic):
        order.setdefault(doc["id"], (rank, 0, doc["path"]))
    for rank, (_, doc) in enumerate(keyword):
        order.setdefault(doc["id"], (rank, 1, doc["path"]))
    for rank, (_, doc) in enumerate(fulltext):
        order.setdefault(doc["id"], (rank, 2, doc["path"]))
    docs = {doc["id"]: doc for _, doc in semantic}
    docs.update({doc["id"]: doc for _, doc in keyword})
    docs.update({doc["id"]: doc for _, doc in fulltext})
    result = []
    for rank, (matching, _) in enumerate(
        sorted(order.items(), key=lambda item: item[1])
    ):
        item = dict(docs[matching])
        item["source_rank"] = rank + 1
        result.append(item)
    return result
