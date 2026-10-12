"""Content readers and extractors for ZIM entries."""

from .reader import (
    DEFAULT_MAX_ENTRY_BYTES,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_PREVIEW_CHARS,
    EntryRecord,
    MIN_BLOCK_CHARS,
    disambiguation_title,
    extract_excerpt,
    extract_pdf_excerpt,
    is_disambiguation,
    is_javascript_shell,
    iter_text_blocks,
    read_entry,
    truncate_at_word_boundary,
)

__all__ = [
    "DEFAULT_MAX_ENTRY_BYTES",
    "DEFAULT_MAX_HTML_BYTES",
    "DEFAULT_PREVIEW_CHARS",
    "EntryRecord",
    "MIN_BLOCK_CHARS",
    "disambiguation_title",
    "extract_excerpt",
    "extract_pdf_excerpt",
    "is_disambiguation",
    "is_javascript_shell",
    "iter_text_blocks",
    "read_entry",
    "truncate_at_word_boundary",
]
