"""Shared configuration and filesystem helpers for search components."""

import math
from pathlib import Path


def _int_config(cfg: dict, key: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(cfg.get(key, default)))
    except (TypeError, ValueError):
        return default


def _float_config(
    cfg: dict,
    key: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(maximum, max(minimum, value))


def _file_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
