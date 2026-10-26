"""Read ordinary files as searchable sources.

Zimantic's indexes are built from an archive-like object.  Normal files do not
come in a ZIM container, so this module exposes a small ``Archive``-shaped
wrapper (``FileArchive``) around one file.  A file becomes one or more search
entries: an EPUB chapter, a PDF page, a presentation slide, a subtitle chunk or
a whole plain-text document.

``extract.py`` owns the format-specific readers.  This wrapper only maps their
sections onto the narrow interface Zimantic's builder and preview paths use, so
a file index runs through the same SQLite/FAISS pipeline as a ZIM.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .extract import (
    DEFAULT_MAX_FILE_TEXT_CHARS,
    Extraction,
    ExtractOptions,
    Section,
    extract_document,
)
from .formats import (  # re-exported for existing callers/tests
    SUBTITLE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TEXT_EXTENSIONS,
    VIDEO_EXTENSIONS,
    file_extension,
    is_supported_file,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_FILE_TEXT_CHARS",
    "SUBTITLE_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    "TEXT_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "FileArchive",
    "FileEntry",
    "file_extension",
    "is_supported_file",
]


class _FileItem:
    """The small part of a libzim item that the builder and previews read."""

    mimetype = "text/plain"

    def __init__(self, content: bytes):
        self.content = content
        self.size = len(content)


class FileEntry:
    """One searchable entry backed by a section of a normal file."""

    is_redirect = False
    mimetype = "text/plain"

    def __init__(
        self,
        index: int,
        title: str,
        path: str,
        body: str,
        locator: str = "",
    ):
        self._index = index
        self.title = title
        self.path = path
        self.body = body
        self.locator = locator
        self._content = body.encode("utf-8")

    def get_item(self) -> _FileItem:
        return _FileItem(self._content)

    @property
    def preview(self) -> str:
        return self.body


class FileArchive:
    """A minimal ``libzim.reader.Archive``-shaped view of one ordinary file.

    ``entry_count`` is zero when the file has no indexable text (for example a
    video without subtitles or a scanned PDF while OCR is disabled).  ``build``
    treats that as an unindexable source instead of creating an empty index.
    """

    def __init__(
        self,
        path: Path | str,
        max_text_chars: int = DEFAULT_MAX_FILE_TEXT_CHARS,
        options: ExtractOptions | None = None,
        extraction: Extraction | None = None,
    ):
        self.path = Path(path)
        if options is None:
            options = ExtractOptions(max_text_chars=max_text_chars)
        elif max_text_chars != DEFAULT_MAX_FILE_TEXT_CHARS:
            # Keep the legacy keyword meaningful for callers that still pass it.
            options = ExtractOptions(
                max_text_chars=max_text_chars,
                timeout=options.timeout,
                archive_depth=options.archive_depth,
                max_archive_members=options.max_archive_members,
                max_archive_bytes=options.max_archive_bytes,
                max_ocr_pages=options.max_ocr_pages,
                ocr=options.ocr,
                ocr_languages=options.ocr_languages,
                asr=options.asr,
                asr_model=options.asr_model,
            )
        self.options = options

        if extraction is None:
            try:
                extraction = extract_document(self.path, options)
            except Exception as error:  # extract.py should already isolate
                logger.warning("%s: file extraction failed (%s)", self.path.name, error)
                extraction = None
        self._metadata = dict(extraction.metadata if extraction else {})
        self.title = self._metadata.get("title") or self.path.stem
        self.description = self._metadata.get("description", "")
        self.language = self._metadata.get("language", "")
        self.creator = self._metadata.get("creator", "")
        self.media_count = 0
        self.has_fulltext_index = False
        self.has_illustration = False
        self.uuid = ""

        sections = extraction.sections if extraction else []
        self._entries = self._build_entries(sections)
        self.entry_count = len(self._entries)
        self.article_count = self.entry_count
        self.metadata_keys = (
            "Title", "Description", "Language", "Creator",
        )

    def _build_entries(self, sections: list[Section]) -> list[FileEntry]:
        entries: list[FileEntry] = []
        multiple = len(sections) > 1
        used_paths: set[str] = set()
        for index, section in enumerate(sections):
            title = section.title or self.title
            locator = section.locator
            if multiple and not locator:
                # Formats without a browser anchor (DOCX/ODT chunks) still
                # need unique result paths; the fragment is harmless in a
                # browser and keeps previews and ids unambiguous.
                locator = f"section={index + 1}"
            if multiple and not section.title:
                title = f"{self.title} ({index + 1})"
            path = self.path.name
            if locator:
                path = f"{path}#{locator}"
            if path in used_paths:
                # Defensive: two sections can share a locator (identical cue
                # start times, repeated headings from a parser without one).
                locator = f"{locator}&part={index + 1}" if locator else f"section={index + 1}"
                path = f"{self.path.name}#{locator}"
            used_paths.add(path)
            entries.append(
                FileEntry(index, title, path, section.body, locator)
            )
        return entries

    @property
    def main_entry(self) -> FileEntry:
        if not self._entries:
            raise KeyError("file has no indexable entries")
        return self._entries[0]

    def _get_entry_by_id(self, entry_id: int, /) -> FileEntry:
        try:
            return self._entries[int(entry_id)]
        except (IndexError, TypeError, ValueError) as error:
            raise KeyError(entry_id) from error

    def get_entry_by_path(self, path: str) -> FileEntry:
        for entry in self._entries:
            if entry.path == path:
                return entry
        # A bare filename addresses the first entry, which keeps links and
        # older indexes working for multi-section files.
        if path == self.path.name and self._entries:
            return self._entries[0]
        raise KeyError(path)

    def has_entry_by_path(self, path: str) -> bool:
        try:
            self.get_entry_by_path(path)
        except KeyError:
            return False
        return True

    def get_metadata(self, key: str) -> str:
        values = {
            "Title": self.title,
            "Description": self.description,
            "Language": self.language,
            "Creator": self.creator,
        }
        if key not in values:
            raise KeyError(key)
        return values[key]

    def get_illustration_item(self, _size: int) -> Any:
        raise KeyError("file has no illustration")
