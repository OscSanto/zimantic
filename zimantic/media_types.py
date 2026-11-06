"""The frozen content-family taxonomy shared by every provider."""

from __future__ import annotations

from typing import Sequence

MEDIA_TYPE_ORDER: tuple[str, ...] = ("text", "video", "audio", "image", "other")


def ordered_media_types(values: Sequence[str]) -> tuple[str, ...]:
    """Return deduplicated media families in the frozen taxonomy order."""
    present = {str(value) for value in values}
    ordered = tuple(value for value in MEDIA_TYPE_ORDER if value in present)
    # Unknown families are retained at the end rather than silently discarded.
    ordered += tuple(sorted(present - set(MEDIA_TYPE_ORDER)))
    return ordered


def classify_mime_counts(value: str | None) -> tuple[str, ...]:
    """Best-effort media families from a ZIM ``mime=count;...`` counter."""
    families: set[str] = set()
    for entry in str(value or "").split(";"):
        mime = entry.split("=", 1)[0].strip().casefold()
        if not mime:
            continue
        if mime.startswith("video/"):
            families.add("video")
        elif mime.startswith("audio/"):
            families.add("audio")
        elif mime.startswith("image/"):
            families.add("image")
        elif mime.startswith("text/") or mime in {
            "application/xhtml+xml",
            "application/pdf",
        }:
            families.add("text")
        else:
            families.add("other")
    return ordered_media_types(tuple(families))


__all__ = [
    "MEDIA_TYPE_ORDER",
    "classify_mime_counts",
    "ordered_media_types",
]
