"""Download CTranslate2 Whisper models for the faster-whisper adapter.

``whisper-ctranslate2`` normally downloads through the Hugging Face Hub.  Doing
that ourselves gives Zimantic the same single-download lock, atomic writes, and
offline reuse as the other ASR backends, and lets the CLI run with
``--local_files_only`` on every later build.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .locking import BuildLockError, FileLease
from .model_files import DOWNLOAD_CHUNK_BYTES, DOWNLOAD_TIMEOUT_SECONDS, sha256_file

logger = logging.getLogger(__name__)

HF_API_BASE = "https://huggingface.co/api/models"
HF_FILE_BASE = "https://huggingface.co"
MANIFEST_NAME = ".zimantic-model.json"
REQUIRED_FILES = frozenset(
    {"config.json", "model.bin", "tokenizer.json"}
)

# Mirrors faster_whisper/utils.py::_MODELS.
FASTER_WHISPER_MODELS = {
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "tiny": "Systran/faster-whisper-tiny",
    "base.en": "Systran/faster-whisper-base.en",
    "base": "Systran/faster-whisper-base",
    "small.en": "Systran/faster-whisper-small.en",
    "small": "Systran/faster-whisper-small",
    "medium.en": "Systran/faster-whisper-medium.en",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
    "distil-large-v2": "Systran/faster-distil-whisper-large-v2",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

_THREAD_LOCK = threading.Lock()


class FasterWhisperModelError(RuntimeError):
    """A faster-whisper model is unknown, unavailable, or could not be verified."""


def faster_whisper_cache_root() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "zimantic" / "faster-whisper"


def _request_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "zimantic"})
    with urllib.request.urlopen(
        request, timeout=DOWNLOAD_TIMEOUT_SECONDS
    ) as response:
        return json.load(response)


def _allowed(path: str) -> bool:
    if not path or Path(path).name != path:
        return False
    return path in {
        "config.json",
        "preprocessor_config.json",
        "model.bin",
        "tokenizer.json",
    } or path.startswith("vocabulary.")


def _model_files(repo: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        info = _request_json(f"{HF_API_BASE}/{repo}")
        revision = str(info.get("sha") or "")
        if not revision:
            raise FasterWhisperModelError(
                f"Hugging Face did not report a revision for {repo}"
            )
        tree = _request_json(f"{HF_API_BASE}/{repo}/tree/{revision}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise FasterWhisperModelError(
            f"could not read the Hugging Face model metadata for {repo}: {error}"
        ) from error
    if not isinstance(tree, list):
        raise FasterWhisperModelError(
            f"Hugging Face returned no file list for {repo}"
        )

    files: list[dict[str, Any]] = []
    for item in tree:
        if not isinstance(item, dict) or item.get("type") != "file":
            continue
        path = str(item.get("path") or "")
        if not _allowed(path):
            continue
        size = item.get("size")
        if not isinstance(size, int) or size < 0:
            continue
        lfs = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
        files.append(
            {
                "path": path,
                "size": size,
                "sha256": str(lfs.get("oid") or "") or None,
                "git_oid": str(item.get("oid") or "") or None,
            }
        )

    present = {item["path"] for item in files}
    missing = sorted(REQUIRED_FILES - present)
    if missing:
        raise FasterWhisperModelError(
            f"{repo} is missing required model file(s): {', '.join(missing)}"
        )
    return revision, files


def _git_blob_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {path.stat().st_size}\0".encode("ascii"))
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_file(
    url: str,
    destination: Path,
    *,
    size: int,
    sha256: str | None,
    git_oid: str | None,
    label: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "zimantic"})
    logger.info("zimantic: downloading faster-whisper file %s", url)
    try:
        with urllib.request.urlopen(
            request, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            length = response.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else size
            with open(partial, "wb") as output:
                with _progress(total, destination.name) as progress:
                    while chunk := response.read(DOWNLOAD_CHUNK_BYTES):
                        output.write(chunk)
                        progress.update(len(chunk))
    except (urllib.error.URLError, OSError, ValueError) as error:
        partial.unlink(missing_ok=True)
        raise FasterWhisperModelError(
            f"could not download faster-whisper model {label!r} from {url}: {error}"
        ) from error

    try:
        actual_size = partial.stat().st_size
        verified = actual_size == size
        if verified and sha256:
            verified = sha256_file(partial) == sha256
        elif verified and git_oid:
            verified = _git_blob_sha1(partial) == git_oid
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise FasterWhisperModelError(
            f"could not verify faster-whisper model {label!r}: {error}"
        ) from error
    if not verified:
        partial.unlink(missing_ok=True)
        raise FasterWhisperModelError(
            f"downloaded faster-whisper model {label!r} does not match the "
            "expected size or checksum"
        )
    os.replace(partial, destination)


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


def _manifest_path(target: Path) -> Path:
    return target / MANIFEST_NAME


def _read_manifest(target: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(_manifest_path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    files = data["files"]
    if not REQUIRED_FILES <= set(files):
        return None
    for path, meta in files.items():
        if not _allowed(str(path)) or not isinstance(meta, dict):
            return None
        size = meta.get("size")
        candidate = target / path
        if not isinstance(size, int) or size < 0 or not candidate.is_file():
            return None
        try:
            if candidate.stat().st_size != size:
                return None
        except OSError:
            return None
    return data


def _write_manifest(
    target: Path,
    repo: str,
    revision: str,
    files: list[dict[str, Any]],
) -> None:
    data = {
        "repo": repo,
        "revision": revision,
        "files": {
            item["path"]: {
                "size": item["size"],
                "sha256": item["sha256"],
                "git_oid": item["git_oid"],
            }
            for item in files
        },
    }
    partial = _manifest_path(target).with_name(MANIFEST_NAME + ".part")
    partial.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(partial, _manifest_path(target))


def _local_directory(model: str) -> Path | None:
    if not model:
        return None
    candidate = Path(model).expanduser()
    if candidate.is_dir() and (candidate / "model.bin").is_file():
        return candidate
    return None


def ensure_faster_whisper_model(
    model: str, cache_root: Path | None = None
) -> Path:
    """Return a local CTranslate2 model directory, downloading it once."""

    local = _local_directory(model)
    if local is not None:
        return local

    if not model or Path(model).name != model:
        raise FasterWhisperModelError(f"invalid faster-whisper model name {model!r}")

    repo = FASTER_WHISPER_MODELS.get(model)
    if repo is None:
        supported = ", ".join(sorted(FASTER_WHISPER_MODELS))
        raise FasterWhisperModelError(
            f"faster-whisper model {model!r} is not cached and has no known "
            f"download repository; use one of {supported}, or pass a local "
            "CTranslate2 model directory"
        )

    root = cache_root or faster_whisper_cache_root()
    target = root / model
    with _THREAD_LOCK:
        manifest = _read_manifest(target)
        if manifest is not None:
            return target
        try:
            target.mkdir(parents=True, exist_ok=True)
            lease = FileLease(
                target / ".download.lock",
                description="faster-whisper model download lock",
            )
            lease.acquire()
        except (OSError, BuildLockError) as error:
            raise FasterWhisperModelError(
                f"could not lock the faster-whisper model cache {target}: {error}"
            ) from error
        try:
            manifest = _read_manifest(target)
            if manifest is not None:
                return target
            revision, files = _model_files(repo)
            for item in files:
                path = str(item["path"])
                destination = target / path
                url = (
                    f"{HF_FILE_BASE}/{repo}/resolve/{revision}/"
                    f"{urllib.parse.quote(path)}"
                )
                _download_file(
                    url,
                    destination,
                    size=int(item["size"]),
                    sha256=item.get("sha256"),
                    git_oid=item.get("git_oid"),
                    label=f"{model}/{path}",
                )
            _write_manifest(target, repo, revision, files)
        finally:
            lease.release()
    return target


__all__ = [
    "FASTER_WHISPER_MODELS",
    "FasterWhisperModelError",
    "ensure_faster_whisper_model",
    "faster_whisper_cache_root",
]
