"""Content readers, extractors, and the canonical content contracts.

The contract types are imported eagerly: they must remain importable without
libzim.  The reader/extractor helpers are resolved lazily so importing this
package (and ``zimantic.providers.base``) does not require libzim either.
"""

from .store import (
    ArticleData,
    ArticleNotFoundError,
    ArticleSourceNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
    ContentEntry,
    ContentRange,
    ContentStore,
)

_READER_EXPORTS = {
    "DEFAULT_MAX_BODY_TEXT_BYTES",
    "DEFAULT_MAX_ENTRY_BYTES",
    "DEFAULT_MAX_HTML_BYTES",
    "DEFAULT_PREVIEW_CHARS",
    "MIN_BLOCK_CHARS",
    "EntryRecord",
    "disambiguation_title",
    "extract_body",
    "extract_excerpt",
    "extract_pdf_excerpt",
    "is_disambiguation",
    "is_javascript_shell",
    "iter_text_blocks",
    "read_entry",
    "truncate_at_word_boundary",
    "truncate_utf8",
}

__all__ = [
    "DEFAULT_MAX_BODY_TEXT_BYTES",
    "DEFAULT_MAX_ENTRY_BYTES",
    "DEFAULT_MAX_HTML_BYTES",
    "DEFAULT_PREVIEW_CHARS",
    "MIN_BLOCK_CHARS",
    "ArticleData",
    "ArticleNotFoundError",
    "ArticleSourceNotFoundError",
    "ArticleTooLargeError",
    "ArticleUnavailableError",
    "ContentEntry",
    "ContentRange",
    "ContentStore",
    "EntryRecord",
    "disambiguation_title",
    "extract_body",
    "extract_excerpt",
    "extract_pdf_excerpt",
    "is_disambiguation",
    "is_javascript_shell",
    "iter_text_blocks",
    "read_entry",
    "truncate_at_word_boundary",
    "truncate_utf8",
]


def __getattr__(name: str):
    if name in _READER_EXPORTS:
        from . import reader

        return getattr(reader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
