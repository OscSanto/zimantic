"""Provider-neutral indexing: contracts, schema, pipeline, and local search.

The package must import without libzim, FAISS, or web dependencies at module
scope (FAISS is imported lazily inside the vector loader/writer).
"""

from .types import (
    DerivedTextInput,
    DerivedTextProducer,
    EntryProducer,
    EntryRecord,
    NativeHit,
    NativeSearcher,
)

__all__ = [
    "DerivedTextInput",
    "DerivedTextProducer",
    "EntryProducer",
    "EntryRecord",
    "NativeHit",
    "NativeSearcher",
]
