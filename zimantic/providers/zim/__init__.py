"""ZIM provider package.

This is the reference implementation of the provider seam.  New providers must
not import ``zimantic.providers.zim``; use :mod:`zimantic.providers.base` and the
shared content/catalog contracts instead.
"""

from .provider import (
    ZimDiscoveryBatch,
    ZimProvider,
    ZimRuntime,
    zim_collection_key,
    zim_source_key,
)

__all__ = [
    "ZimDiscoveryBatch",
    "ZimProvider",
    "ZimRuntime",
    "zim_collection_key",
    "zim_source_key",
]
