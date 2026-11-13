"""Shared application of published-index metadata to source records.

ZIM, files, and Kolibri all publish the same index metadata keys. Keeping this
in one provider-neutral module prevents the providers from drifting on fields
such as ``BUILD_SCOPE`` and ``index_partial``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..indexing import schema


def apply_index_metadata(source: Any, meta: Mapping[str, str]) -> None:
    """Populate a source's published-index fields from validated metadata.

    Missing or malformed values leave the corresponding field unset rather than
    inventing a value.
    """
    try:
        if schema.INDEXED_COUNT in meta:
            object.__setattr__(
                source,
                "indexed_count",
                schema.decode_int(meta[schema.INDEXED_COUNT]),
            )
        if schema.BUILD_SCOPE in meta:
            coverage = schema.decode_json(meta[schema.BUILD_SCOPE])
            partial = (
                bool(coverage.get("partial"))
                if isinstance(coverage, dict)
                else False
            )
        else:
            # A published index without a scope manifest covers the source.
            partial = False
        object.__setattr__(source, "index_partial", partial)
        if schema.TRUNCATED in meta:
            object.__setattr__(
                source,
                "truncated",
                schema.decode_bool(meta[schema.TRUNCATED]),
            )
        if schema.BODY_TRUNCATED_COUNT in meta:
            object.__setattr__(
                source,
                "body_truncated_count",
                schema.decode_int(meta[schema.BODY_TRUNCATED_COUNT]),
            )
        if schema.DERIVED_TEXT in meta:
            value = schema.decode_json(meta[schema.DERIVED_TEXT])
            if isinstance(value, dict):
                object.__setattr__(source, "derived_text", value)
        object.__setattr__(source, "index_freshness", "unknown")
    except (KeyError, TypeError, ValueError):
        return


__all__ = ["apply_index_metadata"]
