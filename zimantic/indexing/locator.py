"""Provider-scoped index locations and immutable generation leases.

The implementation lives in :mod:`zimantic.indexes.locator` (introduced by the
phase-00 contracts work).  This module is the canonical import path for the
generic indexing pipeline; keeping one implementation means ``serve``,
``build``, ``reload``, cleanup, health, and future providers all share the same
layout and lease protocol.
"""

from ..indexes.locator import (
    MANIFEST_VERSION,
    GenerationLease,
    IndexLocator,
    IndexLocatorError,
)

__all__ = [
    "GenerationLease",
    "IndexLocator",
    "IndexLocatorError",
    "MANIFEST_VERSION",
]
