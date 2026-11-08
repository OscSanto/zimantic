"""Kiwix provider package.

Remote Kiwix is metadata-only: catalog discovery is bounded and in-memory, and
both search and article serving stay with ``kiwix-serve``.  Result documents
carry absolute external links; this provider never exposes a local
``ContentStore``.
"""

from .catalog import (
    CatalogFetch,
    fetch_catalog,
)
from .provider import (
    KiwixDiscoveryBatch,
    KiwixProvider,
    KiwixRuntime,
    kiwix_collection_key,
    kiwix_source_key,
    source_from_entry,
)

__all__ = [
    "CatalogFetch",
    "KiwixDiscoveryBatch",
    "KiwixProvider",
    "KiwixRuntime",
    "fetch_catalog",
    "kiwix_collection_key",
    "kiwix_source_key",
    "source_from_entry",
]
