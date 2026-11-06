"""Compatibility import surface for the generic index pipeline.

The implementation moved to :mod:`zimantic.indexing.pipeline` in phase 04.
There is deliberately no ``build()`` wrapper: callers obtain an
:class:`~zimantic.indexing.types.EntryProducer` from a runtime's
``entry_producer`` capability and call :func:`build_index`.
"""

from __future__ import annotations

from .indexing.pipeline import (
    BodyBudget,
    BuildError,
    EmbeddingPipeline,
    InsufficientSpaceError,
    SnapshotChangedError,
    _create_temp_path,
    _faiss_cluster_count,
    _faiss_training_step,
    _save,
    _write_faiss,
    build_index,
    shared_body_budget,
)
from .indexing.schema import SCHEMA

__all__ = [
    "BodyBudget",
    "BuildError",
    "EmbeddingPipeline",
    "InsufficientSpaceError",
    "SCHEMA",
    "SnapshotChangedError",
    "build_index",
    "shared_body_budget",
]
