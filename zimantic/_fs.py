"""Low-level filesystem helpers shared across storage paths."""

from __future__ import annotations

import os
from pathlib import Path


def fsync_path(path: Path) -> None:
    """Best-effort fsync for a file or directory."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
