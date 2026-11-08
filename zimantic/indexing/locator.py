"""Immutable, leased index generations shared by every local provider."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .._filelock import lock_file, unlock_file

MANIFEST_VERSION = 1
_SAFE = re.compile(r"[^a-z0-9._-]+")


class IndexLocatorError(RuntimeError):
    """An index generation cannot be resolved or validated safely."""


def _slug(value: str, *, limit: int = 48) -> str:
    text = _SAFE.sub("-", str(value).casefold()).strip("-._")
    return (text or "source")[:limit].strip("-._") or "source"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_path(path: Path) -> None:
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


def _lock_shared(fd: int) -> bool:
    try:
        return lock_file(fd, shared=True)
    except OSError:
        return False


def _lock_exclusive(fd: int) -> bool:
    try:
        return lock_file(fd)
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        unlock_file(fd)
    except OSError:
        pass


@dataclass
class GenerationLease:
    """An owned pin on one immutable generation."""

    generation: str
    directory: Path
    sqlite_path: Path
    faiss_path: Path | None
    sqlite_digest: str
    faiss_digest: str | None
    manifest: dict[str, Any]
    _fd: int | None = None
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._fd is not None:
            _unlock(self._fd)
            os.close(self._fd)
            self._fd = None


class IndexLocator:
    """Resolve, publish, and reclaim indexed source generations."""

    def __init__(self, index_dir: Path | str) -> None:
        self.index_dir = Path(index_dir)

    def source_directory(
        self, provider: str, collection_key: str, source_key: str
    ) -> Path:
        if not provider or not source_key:
            raise IndexLocatorError("provider and source key are required")
        collection_component = (
            f"{_slug(collection_key)}-{_digest(collection_key)}"
        )
        source_component = f"{_slug(source_key.split(':')[-1])}-{_digest(source_key)}"
        return (
            self.index_dir
            / _slug(provider)
            / collection_component
            / source_component
        )

    def _manifest_path(self, directory: Path) -> Path:
        return directory / "current"

    def _read_manifest(self, directory: Path) -> dict[str, Any] | None:
        path = self._manifest_path(directory)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise IndexLocatorError(f"unreadable index manifest: {error}") from error
        if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
            raise IndexLocatorError("unsupported index manifest version")
        return data

    def _validate_generation(
        self,
        directory: Path,
        manifest: dict[str, Any],
        *,
        verify_faiss: bool = True,
    ) -> tuple[Path, Path | None, str, str | None]:
        generation = str(manifest.get("generation") or "")
        if (
            not generation
            or generation in {".", ".."}
            or ".." in generation
            or "/" in generation
            or "\\" in generation
        ):
            raise IndexLocatorError("invalid generation identifier")
        generation_dir = directory / "generations" / generation
        sqlite_path = generation_dir / "index.sqlite"
        faiss_path = generation_dir / "index.faiss"
        if sqlite_path.is_symlink():
            raise IndexLocatorError("index.sqlite must not be a symlink")
        if not sqlite_path.is_file():
            raise IndexLocatorError("index generation is missing index.sqlite")
        sqlite_digest = str(manifest.get("sqlite_digest") or "")
        if not sqlite_digest or _file_digest(sqlite_path) != sqlite_digest:
            raise IndexLocatorError("index.sqlite digest does not match the manifest")
        faiss_digest = manifest.get("faiss_digest")
        if faiss_digest:
            if faiss_path.is_symlink():
                raise IndexLocatorError("index.faiss must not be a symlink")
            if not faiss_path.is_file():
                raise IndexLocatorError("index generation is missing index.faiss")
            if verify_faiss and _file_digest(faiss_path) != str(faiss_digest):
                raise IndexLocatorError("index.faiss digest does not match the manifest")
        else:
            faiss_path = None
        return sqlite_path, faiss_path, sqlite_digest, (
            str(faiss_digest) if faiss_digest else None
        )

    def _acquire(
        self,
        directory: Path,
        manifest: dict[str, Any],
        *,
        verify_faiss: bool = True,
    ) -> GenerationLease:
        generation = str(manifest.get("generation") or "")
        lease_path = directory / "generations" / generation / "lease"
        try:
            fd = os.open(lease_path, os.O_RDWR)
        except OSError as error:
            raise IndexLocatorError(f"generation lease is unavailable: {error}") from error
        try:
            if not _lock_shared(fd):
                raise IndexLocatorError("generation is being reclaimed")
            # Recheck the manifest after pinning: publication/cleanup may have
            # won the race before our lock.
            current = self._read_manifest(directory)
            if current != manifest:
                raise IndexLocatorError("index generation changed while pinning")
            sqlite_path, faiss_path, sqlite_digest, faiss_digest = (
                self._validate_generation(
                    directory, manifest, verify_faiss=verify_faiss
                )
            )
        except BaseException:
            os.close(fd)
            raise
        return GenerationLease(
            generation=generation,
            directory=directory / "generations" / generation,
            sqlite_path=sqlite_path,
            faiss_path=faiss_path,
            sqlite_digest=sqlite_digest,
            faiss_digest=faiss_digest,
            manifest=manifest,
            _fd=fd,
        )

    def paths(self, source: Any, *, verify_faiss: bool = True) -> GenerationLease | None:
        """Resolve and pin the current generation for one source, if any."""
        provider = getattr(source, "provider", None)
        collection_key = getattr(source, "collection_key", "")
        source_key = getattr(source, "key", None)
        if not provider or not source_key:
            return None
        directory = self.source_directory(provider, collection_key, source_key)
        manifest = self._read_manifest(directory)
        if manifest is None:
            return None
        for _attempt in range(3):
            try:
                return self._acquire(
                    directory, manifest, verify_faiss=verify_faiss
                )
            except IndexLocatorError:
                updated = self._read_manifest(directory)
                if updated is None:
                    return None
                manifest = updated
        raise IndexLocatorError("could not pin a stable index generation")

    def publish(
        self,
        source: Any,
        sqlite_source: Path | str,
        faiss_source: Path | str | None = None,
        *,
        metadata: dict[str, Any] | None = None,
        generation: str | None = None,
    ) -> GenerationLease:
        """Publish a complete SQLite/optional-FAISS pair as a new generation."""
        provider = getattr(source, "provider")
        collection_key = getattr(source, "collection_key", "")
        source_key = getattr(source, "key")
        directory = self.source_directory(provider, collection_key, source_key)
        generation = generation or str(uuid.uuid4())
        generation_dir = directory / "generations" / generation
        staging = generation_dir.with_name(generation + ".staging")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        sqlite_path = staging / "index.sqlite"
        shutil.copyfile(sqlite_source, sqlite_path)
        _fsync_path(sqlite_path)
        self._seal_sqlite(sqlite_path)
        sqlite_digest = _file_digest(sqlite_path)
        faiss_path = None
        faiss_digest = None
        if faiss_source is not None and Path(faiss_source).is_file():
            faiss_path = staging / "index.faiss"
            shutil.copyfile(faiss_source, faiss_path)
            _fsync_path(faiss_path)
            faiss_digest = _file_digest(faiss_path)
        (staging / "lease").touch()
        if generation_dir.exists():
            shutil.rmtree(staging, ignore_errors=True)
            raise IndexLocatorError(
                "refusing to reuse an existing index generation identifier"
            )
        os.replace(staging, generation_dir)
        _fsync_path(generation_dir.parent)
        manifest = {
            "version": MANIFEST_VERSION,
            "provider": provider,
            "collection_key": collection_key,
            "source_key": source_key,
            "generation": generation,
            "sqlite_digest": sqlite_digest,
            "faiss_digest": faiss_digest,
        }
        if metadata:
            manifest["metadata"] = dict(metadata)
            manifest["metadata"].setdefault("build_generation", generation)
        manifest_path = self._manifest_path(directory)
        temporary = manifest_path.with_name(".current.tmp")
        directory.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8"
        )
        _fsync_path(temporary)
        os.replace(temporary, manifest_path)
        _fsync_path(directory)
        return self._acquire(directory, manifest)

    @staticmethod
    def _seal_sqlite(path: Path) -> None:
        """Checkpoint and remove sidecars, then leave an immutable database."""
        import sqlite3

        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.commit()
        finally:
            connection.close()
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)

    def cleanup(self, source: Any, *, keep: str | None = None) -> list[Path]:
        """Remove non-current generations that no reader pins."""
        provider = getattr(source, "provider")
        collection_key = getattr(source, "collection_key", "")
        source_key = getattr(source, "key")
        directory = self.source_directory(provider, collection_key, source_key)
        manifest = self._read_manifest(directory) or {}
        current = keep or manifest.get("generation")
        removed: list[Path] = []
        generations = directory / "generations"
        if not generations.is_dir():
            return removed
        for generation_dir in sorted(generations.iterdir()):
            if not generation_dir.is_dir() or generation_dir.name == current:
                continue
            lease_path = generation_dir / "lease"
            try:
                fd = os.open(lease_path, os.O_RDWR)
            except OSError:
                continue
            removable = False
            try:
                if not _lock_exclusive(fd):
                    continue
                latest = self._read_manifest(directory) or {}
                if generation_dir.name == (latest.get("generation") or current):
                    continue
                removable = True
            finally:
                _unlock(fd)
                os.close(fd)
            if removable:
                # Windows does not allow deleting a file while its lock handle
                # is open. The manifest no longer points here, so after the
                # exclusive check/recheck readers can only observe a stale
                # generation and will retry against the current manifest.
                shutil.rmtree(generation_dir)
                removed.append(generation_dir)
        return removed


__all__ = [
    "GenerationLease",
    "IndexLocator",
    "IndexLocatorError",
    "MANIFEST_VERSION",
]
