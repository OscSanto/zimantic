"""Validated per-source policy, separate from process-wide resource limits."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

_POSITIVE_INTS = frozenset(
    {
        "max_entry_bytes",
        "max_html_bytes",
        "max_body_text_bytes",
        "max_preview_chars",
        "max_entries",
        "candidate_count",
        "nprobe",
    }
)
CACHE_KEYS = frozenset(
    {"cache_size", "cache_bytes", "max_derived_cache_bytes_per_source"}
)
FILTER_KEYS = frozenset({"index_include", "index_exclude"})
SEARCH_KEYS = frozenset(
    {
        "candidate_count",
        "nprobe",
        "min_cosine_similarity",
        "source_timeout",
        "max_preview_chars",
        "max_html_bytes",
    }
)
SCOPED_KEYS = _POSITIVE_INTS | CACHE_KEYS | FILTER_KEYS | SEARCH_KEYS


def parse_scoped_settings(data: Mapping[str, Any]) -> dict[str, Any]:
    values = {key: value for key, value in data.items() if key in SCOPED_KEYS}
    for key, value in values.items():
        if key in FILTER_KEYS:
            if not isinstance(value, (list, tuple)) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise ValueError(f"{key} must be a list of non-empty glob patterns")
            values[key] = tuple(item.strip() for item in value)
        elif key in _POSITIVE_INTS | CACHE_KEYS:
            minimum = 0 if key in CACHE_KEYS else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{key} must be an integer >= {minimum}")
        elif (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or (key == "source_timeout" and value <= 0)
            or (key == "min_cosine_similarity" and not -1 <= value <= 1)
        ):
            raise ValueError(f"{key} must be a finite number in its valid range")
    return values


def resolve_settings(
    cfg: Mapping[str, Any], source: Any, collection: Any = None
) -> dict[str, Any]:
    """Overlay only explicitly configured fields; never mutate shared settings."""
    source = getattr(source, "info", source)
    parts = str(getattr(source, "collection_key", "")).split(":")
    name = parts[1] if len(parts) > 1 else ""
    if collection is None and "collection_settings" not in cfg:
        collection = next(
            (entry for entry in cfg.get("collections", ()) if entry.name == name), None
        )
    if collection is not None:
        defaults = dict(getattr(collection, "settings", {}))
        cap = getattr(collection, "max_entries", None)
        if cap is not None:
            defaults.setdefault("max_entries", cap)
    else:
        defaults = (cfg.get("collection_settings") or {}).get(name, {})
    overrides = (cfg.get("source_settings") or {}).get(
        str(getattr(source, "key", "")), {}
    )
    return {**cfg, **defaults, **overrides}


def settings_key(cfg: Mapping[str, Any], source: Any) -> tuple:
    settings = resolve_settings(cfg, source)
    return tuple(
        (key, settings[key])
        for key in sorted(SEARCH_KEYS | CACHE_KEYS)
        if key in settings
    )


def search_limits(
    limits: Any, cfg: Mapping[str, Any], source: Any, collection: Any
) -> Any:
    settings = resolve_settings(cfg, source, collection)
    values = {
        key: settings[key]
        for key in SEARCH_KEYS
        if key in settings and key in limits.__dataclass_fields__
    }
    if "max_preview_chars" in settings:
        values["preview_chars"] = settings["max_preview_chars"]
    return replace(limits, **values, cfg=settings)
