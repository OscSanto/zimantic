"""The frozen content-family taxonomy shared by every provider."""

from __future__ import annotations

from collections.abc import Sequence

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
    documents = fallback_documents = audiovisual = 0
    incomplete_documents = incomplete_fallback = incomplete_audiovisual = False
    for entry in str(value or "").split(";"):
        mime, separator, raw_count = entry.partition("=")
        mime = mime.strip().casefold()
        if not mime:
            continue
        is_audiovisual = mime.startswith(("video/", "audio/"))
        is_document = mime in {
            "text/html",
            "application/xhtml+xml",
            "application/pdf",
        }
        is_fallback_document = mime in {"text/plain", "text/markdown"}
        if is_audiovisual or is_document or is_fallback_document:
            count_text = raw_count.strip()
            try:
                count = int(count_text) if separator and count_text.isdecimal() else None
            except ValueError:
                count = None
            if is_audiovisual:
                if count is None:
                    incomplete_audiovisual = True
                else:
                    audiovisual += count
            elif is_document:
                if count is None:
                    incomplete_documents = True
                else:
                    documents += count
            elif count is None:
                incomplete_fallback = True
            else:
                fallback_documents += count

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
    if not documents:
        documents = fallback_documents
        incomplete_documents |= incomplete_fallback
    if (
        audiovisual
        and not incomplete_documents
        and not incomplete_audiovisual
        and audiovisual * 2 >= documents
    ):
        families.discard("text")
    return ordered_media_types(tuple(families))


__all__ = [
    "MEDIA_TYPE_ORDER",
    "classify_mime_counts",
    "ordered_media_types",
]
