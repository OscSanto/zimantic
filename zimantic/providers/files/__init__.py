"""Files provider: one configured directory tree as one collection."""

from .discovery import (
    FilesDiscovery,
    FilesSource,
    discover_sources,
    files_collapsed_key,
    files_collection_key,
    files_dir_key,
    files_loose_key,
    files_root_key,
    files_source_key,
)
from .entries import FilesEntryProducer
from .provider import FilesProvider, FilesRuntime
from .store import FilesContentStore

__all__ = [
    "FilesContentStore",
    "FilesDiscovery",
    "FilesEntryProducer",
    "FilesProvider",
    "FilesRuntime",
    "FilesSource",
    "discover_sources",
    "files_collapsed_key",
    "files_collection_key",
    "files_dir_key",
    "files_loose_key",
    "files_root_key",
    "files_source_key",
]
