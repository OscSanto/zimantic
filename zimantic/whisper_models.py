"""Download and locate the Whisper checkpoints used by the ASR adapters.

Zimantic invokes local Whisper executables rather than importing their Python
packages.  This module mirrors the two checkpoint catalogs it needs:

* ``openai-whisper`` ``.pt`` files, read by the ``whisper`` command.
* ``whisper.cpp`` ``ggml-*.bin`` files, read by ``whisper-cli``.

``faster-whisper`` CTranslate2 snapshots are handled by
``faster_whisper_models`` instead.
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

OPENAI_WHISPER_BASE_URL = (
    "https://openaipublic.azureedge.net/main/whisper/models"
)
WHISPER_CPP_BASE_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
)


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
        url=f"{OPENAI_WHISPER_BASE_URL}/{sha256}/{filename}",
        sha256=sha256,
    )


def _ggml(name: str, sha256: str) -> WhisperModel:
    filename = f"ggml-{name}.bin"
    return WhisperModel(
        filename=filename,
        url=f"{WHISPER_CPP_BASE_URL}/{filename}",
        sha256=sha256,
    )


# The official openai-whisper checkpoints, mirrored from
# whisper/__init__.py::_MODELS.  The checksum is part of the download URL, so
# verifying it is the same check the CLI performs.
OPENAI_WHISPER_MODELS = {
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

# whisper.cpp ggml models published by the upstream download-ggml-model.sh
# script, plus the q5/q8 quantizations.  Checksums come from the Hugging Face
# LFS metadata for the same files.
WHISPER_CPP_MODELS = {
    "tiny": _ggml(
        "tiny",
        "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
    ),
    "tiny.en": _ggml(
        "tiny.en",
        "921e4cf8686fdd993dcd081a5da5b6c365bfde1162e72b08d75ac75289920b1f",
    ),
    "tiny-q5_1": _ggml(
        "tiny-q5_1",
        "818710568da3ca15689e31a743197b520007872ff9576237bda97bd1b469c3d7",
    ),
    "tiny.en-q5_1": _ggml(
        "tiny.en-q5_1",
        "c77c5766f1cef09b6b7d47f21b546cbddd4157886b3b5d6d4f709e91e66c7c2b",
    ),
    "tiny-q8_0": _ggml(
        "tiny-q8_0",
        "c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca",
    ),
    "tiny.en-q8_0": _ggml(
        "tiny.en-q8_0",
        "5bc2b3860aa151a4c6e7bb095e1fcce7cf12c7b020ca08dcec0c6d018bb7dd94",
    ),
    "base": _ggml(
        "base",
        "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
    ),
    "base.en": _ggml(
        "base.en",
        "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
    ),
    "base-q5_1": _ggml(
        "base-q5_1",
        "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898",
    ),
    "base.en-q5_1": _ggml(
        "base.en-q5_1",
        "4baf70dd0d7c4247ba2b81fafd9c01005ac77c2f9ef064e00dcf195d0e2fdd2f",
    ),
    "base-q8_0": _ggml(
        "base-q8_0",
        "c577b9a86e7e048a0b7eada054f4dd79a56bbfa911fbdacf900ac5b567cbb7d9",
    ),
    "base.en-q8_0": _ggml(
        "base.en-q8_0",
        "a4d4a0768075e13cfd7e19df3ae2dbc4a68d37d36a7dad45e8410c9a34f8c87e",
    ),
    "small": _ggml(
        "small",
        "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
    ),
    "small.en": _ggml(
        "small.en",
        "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
    ),
    "small-q5_1": _ggml(
        "small-q5_1",
        "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb",
    ),
    "small.en-q5_1": _ggml(
        "small.en-q5_1",
        "bfdff4894dcb76bbf647d56263ea2a96645423f1669176f4844a1bf8e478ad30",
    ),
    "small-q8_0": _ggml(
        "small-q8_0",
        "49c8fb02b65e6049d5fa6c04f81f53b867b5ec9540406812c643f177317f779f",
    ),
    "small.en-q8_0": _ggml(
        "small.en-q8_0",
        "67a179f608ea6114bd3fdb9060e762b588a3fb3bd00c4387971be4d177958067",
    ),
    "medium": _ggml(
        "medium",
        "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208",
    ),
    "medium.en": _ggml(
        "medium.en",
        "cc37e93478338ec7700281a7ac30a10128929eb8f427dda2e865faa8f6da4356",
    ),
    "medium-q5_0": _ggml(
        "medium-q5_0",
        "19fea4b380c3a618ec4723c3eef2eb785ffba0d0538cf43f8f235e7b3b34220f",
    ),
    "medium.en-q5_0": _ggml(
        "medium.en-q5_0",
        "76733e26ad8fe1c7a5bf7531a9d41917b2adc0f20f2e4f5531688a8c6cd88eb0",
    ),
    "medium-q8_0": _ggml(
        "medium-q8_0",
        "42a1ffcbe4167d224232443396968db4d02d4e8e87e213d3ee2e03095dea6502",
    ),
    "medium.en-q8_0": _ggml(
        "medium.en-q8_0",
        "43fa2cd084de5a04399a896a9a7a786064e221365c01700cea4666005218f11c",
    ),
    "large-v1": _ggml(
        "large-v1",
        "7d99f41a10525d0206bddadd86760181fa920438b6b33237e3118ff6c83bb53d",
    ),
    "large-v2": _ggml(
        "large-v2",
        "9a423fe4d40c82774b6af34115b8b935f34152246eb19e80e376071d3f999487",
    ),
    "large-v2-q5_0": _ggml(
        "large-v2-q5_0",
        "3a214837221e4530dbc1fe8d734f302af393eb30bd0ed046042ebf4baf70f6f2",
    ),
    "large-v2-q8_0": _ggml(
        "large-v2-q8_0",
        "fef54e6d898246a65c8285bfa83bd1807e27fadf54d5d4e81754c47634737e8c",
    ),
    "large-v3": _ggml(
        "large-v3",
        "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
    ),
    "large-v3-q5_0": _ggml(
        "large-v3-q5_0",
        "d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1",
    ),
    "large-v3-turbo": _ggml(
        "large-v3-turbo",
        "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
    ),
    "large-v3-turbo-q5_0": _ggml(
        "large-v3-turbo-q5_0",
        "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
    ),
    "large-v3-turbo-q8_0": _ggml(
        "large-v3-turbo-q8_0",
        "317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1",
    ),
    "large": _ggml(
        "large-v3",
        "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
    ),
    "turbo": _ggml(
        "large-v3-turbo",
        "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
    ),
}

_THREAD_LOCK = threading.Lock()


def _cache_root(*parts: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base.joinpath(*parts)


def openai_whisper_cache_root() -> Path:
    """Return the cache directory the openai-whisper CLI reads."""

    return _cache_root("whisper")


def whisper_cpp_cache_root() -> Path:
    """Return Zimantic's cache directory for whisper.cpp checkpoints."""

    return _cache_root("zimantic", "whisper-cpp")


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


def _cached_model(
    model: str,
    catalog: dict[str, WhisperModel],
    root: Path,
    fallback_filename: str,
) -> Path | None:
    if not model or Path(model).name != model:
        return None
    spec = catalog.get(model)
    filename = spec.filename if spec is not None else fallback_filename
    candidate = root / filename
    return candidate if candidate.is_file() else None


def _ensure_model(
    model: str,
    catalog: dict[str, WhisperModel],
    root: Path,
    fallback_filename: str,
    *,
    label: str,
) -> Path:
    if not model or Path(model).name != model:
        raise WhisperModelError(f"invalid whisper model name {model!r}")

    existing = _cached_model(model, catalog, root, fallback_filename)
    if existing is not None:
        return existing

    spec = catalog.get(model)
    if spec is None:
        supported = ", ".join(sorted(catalog))
        raise WhisperModelError(
            f"{label} model {model!r} is not cached and has no known download URL; "
            f"use one of {supported}, or place {fallback_filename} in {root}"
        )

    target = root / spec.filename
    with _THREAD_LOCK:
        if target.is_file():
            return target
        try:
            root.mkdir(parents=True, exist_ok=True)
            lease = FileLease(
                root / f".{spec.filename}.lock",
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


def cached_openai_whisper_model(
    model: str, cache_root: Path | None = None
) -> Path | None:
    """Return a locally cached openai-whisper checkpoint, or ``None``."""

    root = cache_root or openai_whisper_cache_root()
    return _cached_model(
        model, OPENAI_WHISPER_MODELS, root, f"{model}.pt"
    )


def ensure_openai_whisper_model(
    model: str, cache_root: Path | None = None
) -> Path:
    """Return an openai-whisper checkpoint, downloading it once if needed."""

    root = cache_root or openai_whisper_cache_root()
    return _ensure_model(
        model,
        OPENAI_WHISPER_MODELS,
        root,
        f"{model}.pt",
        label="openai-whisper",
    )


def cached_whisper_cpp_model(
    model: str, cache_root: Path | None = None
) -> Path | None:
    """Return a locally cached whisper.cpp checkpoint, or ``None``."""

    root = cache_root or whisper_cpp_cache_root()
    return _cached_model(
        model, WHISPER_CPP_MODELS, root, f"ggml-{model}.bin"
    )


def ensure_whisper_cpp_model(model: str, cache_root: Path | None = None) -> Path:
    """Return a whisper.cpp checkpoint, downloading it once if needed."""

    root = cache_root or whisper_cpp_cache_root()
    return _ensure_model(
        model,
        WHISPER_CPP_MODELS,
        root,
        f"ggml-{model}.bin",
        label="whisper.cpp",
    )


__all__ = [
    "OPENAI_WHISPER_MODELS",
    "WHISPER_CPP_MODELS",
    "WhisperModel",
    "WhisperModelError",
    "cached_openai_whisper_model",
    "cached_whisper_cpp_model",
    "ensure_openai_whisper_model",
    "ensure_whisper_cpp_model",
    "openai_whisper_cache_root",
    "whisper_cpp_cache_root",
]
