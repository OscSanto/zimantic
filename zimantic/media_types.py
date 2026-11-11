"""The frozen content-family taxonomy shared by every provider."""

from __future__ import annotations

from collections.abc import Sequence

MEDIA_TYPE_ORDER: tuple[str, ...] = ("text", "video", "audio", "image", "other")
_FOUNDATION_MIMES = frozenset(
    {
        "application/font-woff2",
        "application/javascript",
        "application/vnd.ms-fontobject",
        "application/x-javascript",
        "font/otf",
        "font/sfnt",
        "font/ttf",
        "font/woff",
        "font/woff2",
        "image/vnd.microsoft.icon",
        "image/x-icon",
        "text/css",
        "text/html",
        "text/javascript",
        "application/xhtml+xml",
    }
)
_FOUNDATION_WEIGHT = 1
_CONTENT_WEIGHT = 10
_DOCUMENT_MIMES = frozenset({"text/html", "application/xhtml+xml", "application/pdf"})
_FALLBACK_DOCUMENT_MIMES = frozenset({"text/plain", "text/markdown"})


def _mime_family(mime: str) -> str:
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("text/") or mime in {
        "application/xhtml+xml",
        "application/pdf",
    }:
        return "text"
    return "other"


def _presence_fallback(mimes: Sequence[str]) -> tuple[str, ...]:
    families = ordered_media_types(tuple(_mime_family(mime) for mime in mimes))
    return families[:1]


def ordered_media_types(values: Sequence[str]) -> tuple[str, ...]:
    """Return deduplicated media families in the frozen taxonomy order."""
    present = {str(value) for value in values}
    ordered = tuple(value for value in MEDIA_TYPE_ORDER if value in present)
    # Unknown families are retained at the end rather than silently discarded.
    ordered += tuple(sorted(present - set(MEDIA_TYPE_ORDER)))
    return ordered


def classify_mime_counts(value: str | None) -> tuple[str, ...]:
    """Choose one media family from a ZIM ``mime=count;...`` counter."""
    counts: dict[str, int] = {}
    present: list[str] = []
    incomplete_documents = incomplete_fallback = incomplete_audiovisual = False
    has_numeric_counts = False
    for entry in str(value or "").split(";"):
        mime, separator, raw_count = entry.partition("=")
        mime = mime.strip().casefold()
        if not mime:
            continue
        present.append(mime)
        count_text = raw_count.strip()
        try:
            count = int(count_text) if separator and count_text.isdecimal() else None
        except ValueError:
            count = None
        if count is not None:
            has_numeric_counts = True
            counts[mime] = counts.get(mime, 0) + count
        if mime in _DOCUMENT_MIMES and count is None:
            incomplete_documents = True
        elif mime in _FALLBACK_DOCUMENT_MIMES and count is None:
            incomplete_fallback = True
        elif mime.startswith(("video/", "audio/")) and count is None:
            incomplete_audiovisual = True

    if not present:
        return ()

    documents = sum(counts.get(mime, 0) for mime in _DOCUMENT_MIMES)
    if not documents:
        documents = sum(counts.get(mime, 0) for mime in _FALLBACK_DOCUMENT_MIMES)
        incomplete_documents |= incomplete_fallback
    audiovisual = {
        family: sum(
            count for mime, count in counts.items() if _mime_family(mime) == family
        )
        for family in ("video", "audio")
    }
    if (
        audiovisual["video"] + audiovisual["audio"]
        and documents
        and not incomplete_documents
        and not incomplete_audiovisual
        and (audiovisual["video"] + audiovisual["audio"]) * 2 >= documents
    ):
        strongest = max(audiovisual, key=audiovisual.get)
        return (strongest,)

    ranked = sorted(
        (
            (
                count
                * (
                    _FOUNDATION_WEIGHT if mime in _FOUNDATION_MIMES else _CONTENT_WEIGHT
                ),
                mime,
            )
            for mime, count in counts.items()
            if count > 0
        ),
        key=lambda item: (-item[0], item[1]),
    )[:2]
    if not ranked:
        return () if has_numeric_counts else _presence_fallback(present)

    scores: dict[str, int] = {}
    substantive: set[str] = set()
    for score, mime in ranked:
        family = _mime_family(mime)
        scores[family] = scores.get(family, 0) + score
        if mime not in _FOUNDATION_MIMES:
            substantive.add(family)
    best_score = max(scores.values())
    tied = [family for family, score in scores.items() if score == best_score]
    order = {family: index for index, family in enumerate(MEDIA_TYPE_ORDER)}
    winner = min(tied, key=lambda family: (family not in substantive, order[family]))
    return (winner,)


__all__ = [
    "MEDIA_TYPE_ORDER",
    "classify_mime_counts",
    "ordered_media_types",
]
