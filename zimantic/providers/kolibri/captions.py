"""Kolibri-specific caption candidate discovery.

Parsing, caching, provenance, deduplication, and failure handling are owned by
the shared :mod:`zimantic.indexing.derived` stage.  This module only identifies
caption files explicitly mapped to the selected content node.
"""

from __future__ import annotations

from typing import Iterable

from .schema import CAPTION_EXTENSIONS, CAPTION_PRESETS, FileRow, FileSelection


def is_caption_file(file: FileRow) -> bool:
    extension = "." + str(file.extension or "").casefold().lstrip(".")
    return (
        file.preset in CAPTION_PRESETS
        or extension in CAPTION_EXTENSIONS
    ) and not file.thumbnail


def caption_files(files: Iterable[FileRow]) -> tuple[FileRow, ...]:
    return tuple(
        sorted(
            (file for file in files if file.available and is_caption_file(file)),
            key=lambda file: (file.lang_id or "", file.id),
        )
    )


def associated_captions(selection: FileSelection) -> tuple[FileRow, ...]:
    """Captions associated with the selected primary representation.

    Kolibri stores all files for one content node in ``content_file``.  They are
    already scoped to the selected node, so the primary representation's caption
    rows are exactly the subtitle-role rows returned by file classification.
    """
    if selection.primary is None:
        return ()
    return selection.captions


discover_caption_files = caption_files

__all__ = [
    "associated_captions",
    "caption_files",
    "discover_caption_files",
    "is_caption_file",
]
