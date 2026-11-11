"""Accessors for the committed media sample data."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

MEDIA_ROOT = Path(__file__).resolve().parent / "fixtures" / "media"


def sample(relative_path: str) -> Path:
    """Return an absolute path to one committed sample file."""

    path = MEDIA_ROOT / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"missing media sample: {path}")
    return path


def copy_sample(relative_path: str, destination: Path | str) -> Path:
    """Copy one committed sample to ``destination`` and return the destination."""

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sample(relative_path), destination)
    return destination


def manifest() -> dict[str, object]:
    return json.loads((MEDIA_ROOT / "manifest.json").read_text(encoding="utf-8"))


__all__ = ["MEDIA_ROOT", "copy_sample", "manifest", "sample"]
