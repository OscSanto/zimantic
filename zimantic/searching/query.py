"""Query parsing and deterministic ranking signals."""

import re

from ..contracts import SourceInfo


MAX_QUERY_LENGTH = 4096
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "how", "what", "where", "when", "why", "who", "which", "can", "could",
    "do", "does", "did", "i", "you", "my", "me", "to", "of", "in", "on",
    "at", "for", "with", "about", "find", "some", "any", "there", "it",
    "that", "this", "and", "or", "if", "so", "will", "would", "should",
}
INTENT_RULES = (
    (
        re.compile(r"^wikihow", re.I),
        ("how to", "how do", "how can", "how should", "steps to"),
    ),
)
DISAMBIGUATION_TITLE = re.compile(r"\s*\(disambiguation\)\s*$", re.I)
USER_PAGE_PATH = re.compile(r"(?:^|/)users(?:/|$)", re.I)
DEFAULT_DISAMBIGUATION_BOOST = 0.08
DEFAULT_DISAMBIGUATION_PENALTY = 0.5
DEFAULT_USER_PAGE_PENALTY = 0.25


def _exact_title_intent(query_words: list[str], title: str) -> bool:
    """True when the query names the full title exactly: navigation intent."""
    hub = _query_terms(title)
    return bool(query_words) and len(hub) == len(query_words) and set(hub) == set(query_words)


def _disambiguation_intent(query: str, query_words: list[str], title: str) -> bool:
    """True when a query explicitly requests a disambiguation page."""
    if _exact_title_intent(query_words, title):
        return True
    if not query.rstrip().endswith("?"):
        return False
    base_title = DISAMBIGUATION_TITLE.sub("", title)
    return _exact_title_intent(query_words, base_title)


def _user_page_intent(query_words: list[str]) -> bool:
    """True when the query explicitly asks for a user or profile page."""
    return bool({"user", "profile"} & set(query_words))


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _content_terms(text: str) -> list[str]:
    return [
        _stem(word)
        for word in re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)
        if word not in STOPWORDS | {"your", "their", "his", "her", "its", "our"}
    ]


def _query_terms(query: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for term in _content_terms(query):
        if term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def _query_words(query: str) -> list[str]:
    words = re.findall(r"[^\W_]+", query.casefold(), re.UNICODE)
    kept = [word for word in words if word not in STOPWORDS]
    return kept or words


def _title_query(query: str) -> str:
    """FTS5 MATCH expression: every word must appear as an exact title token."""
    return " AND ".join(f'"{word.replace(chr(34), chr(34) * 2)}"' for word in _query_words(query))


def _title_prefix_query(query: str) -> str:
    """FTS5 MATCH expression with every word as a prefix token."""
    return " AND ".join(
        f"{word}*" if len(word) > 1 else f'"{word}"' for word in _query_words(query)
    )


def _fulltext_query(query: str) -> str:
    """Query for the ZIM full-text index using plain words."""
    return " ".join(_query_words(query))


def _coverage(query: list[str], tokens: list[str]) -> float:
    if not query:
        return 0.0
    present = set(tokens)
    return sum(term in present for term in query) / len(query)


def _phrase(query: list[str], tokens: list[str]) -> float:
    if not query:
        return 0.0
    width = len(query)
    return float(any(tokens[i:i + width] == query for i in range(len(tokens) - width + 1)))


def _density(query: list[str], tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    wanted = set(query)
    return sum(token in wanted for token in tokens) / len(tokens)


def _intent_match(source: SourceInfo, question: str) -> float:
    lowered = question.casefold()
    return float(
        any(
            re.search(r"\b" + re.escape(phrase) + r"\b", lowered)
            for phrase in source.intent_phrases
        )
    )
