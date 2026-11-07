"""Download and locate the whisper checkpoints used by the ASR adapter.

Zimantic invokes the ``whisper`` command from openai-whisper, but does not
import its Python package: the package may live in a different interpreter, and
importing it pulls in PyTorch.  Mirror the official checkpoint list here so a
missing model can be fetched into the same cache the CLI reads, without making
the optional package importable from Zimantic.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .locking import BuildLockError, FileLease
from .model_files import DOWNLOAD_CHUNK_BYTES, DOWNLOAD_TIMEOUT_SECONDS, sha256_file

logger = logging.getLogger(__name__)

WHISPER_BASE_URL = "https://openaipublic.azureedge.net/main/whisper/models"


class WhisperModelError(RuntimeError):
    """A whisper checkpoint is unknown, unavailable, or could not be verified."""


@dataclass(frozen=True)
class WhisperModel:
    filename: str
    url: str
    sha256: str


def _model(filename: str, sha256: str) -> WhisperModel:
    return WhisperModel(
        filename=filename,
        url=f"{WHISPER_BASE_URL}/{sha256}/{filename}",
        sha256=sha256,
    )


# The official openai-whisper checkpoints, mirrored from
# whisper/__init__.py::_MODELS.  The checksum is part of the download URL, so
# verifying it is the same check the CLI performs.  Update this list when
# upstream adds or replaces checkpoints.
WHISPER_MODELS = {
    "tiny.en": _model(
        "tiny.en.pt",
        "d3dd57d32accea0b295c96e26691aa14d8822fac7d9d27d5dc00b4ca2826dd03",
    ),
    "tiny": _model(
        "tiny.pt",
        "65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9",
    ),
    "base.en": _model(
        "base.en.pt",
        "25a8566e1d0c1e2231d1c762132cd20e0f96a85d16145c3a00adf5d1ac670ead",
    ),
    "base": _model(
        "base.pt",
        "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e",
    ),
    "small.en": _model(
        "small.en.pt",
        "f953ad0fd29cacd07d5a9eda5624af0f6bcf2258be67c92b79389873d91e0872",
    ),
    "small": _model(
        "small.pt",
        "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794",
    ),
    "medium.en": _model(
        "medium.en.pt",
        "d7440d1dc186f76616474e0ff0b3b6b879abc9d1a4926b7adfa41db2d497ab4f",
    ),
    "medium": _model(
        "medium.pt",
        "345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1",
    ),
    "large-v1": _model(
        "large-v1.pt",
        "e4b87e7e0bf463eb8e6956e646f1e277e901512310def2c24bf0e11bd3c28e9a",
    ),
    "large-v2": _model(
        "large-v2.pt",
        "81f7c96c852ee8fc832187b0132e569d6c3065a3252ed18e56effd0b6a73e524",
    ),
    "large-v3": _model(
        "large-v3.pt",
        "e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb",
    ),
    "large": _model(
        "large-v3.pt",
        "e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb",
    ),
    "large-v3-turbo": _model(
        "large-v3-turbo.pt",
        "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a",
    ),
    "turbo": _model(
        "large-v3-turbo.pt",
        "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a",
    ),
}

_THREAD_LOCK = threading.Lock()


def whisper_cache_root() -> Path:
    """Return the cache directory the whisper CLI reads by default."""

    return Path(
        os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    ) / "whisper"


def _model_filename(model: str) -> str | None:
    if not model or Path(model).name != model:
        return None
    spec = WHISPER_MODELS.get(model)
    return spec.filename if spec is not None else f"{model}.pt"


def cached_whisper_model(
    model: str, cache_root: Path | None = None
) -> Path | None:
    """Return a locally cached checkpoint, or ``None``.

    Unknown names are allowed when a matching ``<name>.pt`` file already exists,
    so a manually installed checkpoint keeps working.
    """

    filename = _model_filename(model)
    if filename is None:
        return None
    candidate = (cache_root or whisper_cache_root()) / filename
    return candidate if candidate.is_file() else None


def _progress(total: int | None, description: str):
    from tqdm import tqdm

    return tqdm(
        total=total,
        desc=description,
        unit="B",
        unit_scale=True,
        file=sys.stderr,
        disable=not sys.stderr.isatty(),
    )


def _download(spec: WhisperModel, destination: Path, model: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(spec.url, headers={"User-Agent": "zimantic"})
    logger.info("zimantic: downloading whisper model %s", spec.url)
    try:
        with urllib.request.urlopen(
            request, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            length = response.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else None
            with open(partial, "wb") as output:
                with _progress(total, destination.name) as progress:
                    while chunk := response.read(DOWNLOAD_CHUNK_BYTES):
                        output.write(chunk)
                        progress.update(len(chunk))
    except (urllib.error.URLError, OSError, ValueError) as error:
        partial.unlink(missing_ok=True)
        raise WhisperModelError(
            f"could not download whisper model {model!r} from {spec.url}: {error}"
        ) from error

    try:
        actual_sha256 = sha256_file(partial)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise WhisperModelError(
            f"could not verify whisper model {model!r}: {error}"
        ) from error
    if actual_sha256 != spec.sha256:
        partial.unlink(missing_ok=True)
        raise WhisperModelError(
            f"downloaded whisper model {model!r} does not match the expected checksum"
        )
    os.replace(partial, destination)


def ensure_whisper_model(model: str, cache_root: Path | None = None) -> Path:
    """Return a cached checkpoint, downloading it once when it is missing.

    A per-model file lock serializes concurrent builds while the checkpoint is
    fetched.  Waiting processes re-check the cache after acquiring the lock, so
    only the first caller performs the download.
    """

    filename = _model_filename(model)
    if filename is None:
        raise WhisperModelError(f"invalid whisper model name {model!r}")

    root = cache_root or whisper_cache_root()
    existing = cached_whisper_model(model, root)
    if existing is not None:
        return existing

    spec = WHISPER_MODELS.get(model)
    if spec is None:
        supported = ", ".join(sorted(WHISPER_MODELS))
        raise WhisperModelError(
            f"whisper model {model!r} is not cached and has no known download URL; "
            f"use one of {supported}, or place {filename} in {root}"
        )

    target = root / filename
    with _THREAD_LOCK:
        if target.is_file():
            return target
        try:
            root.mkdir(parents=True, exist_ok=True)
            lease = FileLease(
                root / f".{filename}.lock",
                description="whisper model download lock",
            )
            lease.acquire()
        except (OSError, BuildLockError) as error:
            raise WhisperModelError(
                f"could not lock the whisper model cache {root}: {error}"
            ) from error
        try:
            if target.is_file():
                return target
            _download(spec, target, model)
        finally:
            lease.release()
    return target


__all__ = [
    "WHISPER_MODELS",
    "WhisperModel",
    "WhisperModelError",
    "cached_whisper_model",
    "ensure_whisper_model",
    "whisper_cache_root",
]
