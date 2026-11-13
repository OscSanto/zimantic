"""Ephemeral, process-scoped ZIM selection for ``zimantic open``.

An ``open`` process addresses a named set of archives, never a configured
library.  This module owns the two things that must happen before a server
exists: bounded command-line expansion and the read-only identity lookup that
lets a selected archive keep its normal phase-00 source key.

Nothing here writes user state.  The persistent catalog is copied to a private
temporary directory and read there; the original database, its sidecars, and
the phase-00 catalog lease are only ever opened through read-only descriptors.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._filelock import lock_file, unlock_file
from .catalog import (
    SCHEMA_VERSION,
    SOURCE_COLUMNS,
    absolute_path,
    catalog_path,
)
from .diagnostics import public_cause
from .locking import catalog_lease_path

logger = logging.getLogger(__name__)

DEFAULT_MAX_SOURCES = 64
DEFAULT_EXPANSION_MAX_ENTRIES = 1_000_000
DEFAULT_EXPANSION_TIMEOUT_SECONDS = 300.0
OPEN_COLLECTION_KEY = "zim:open"
OPEN_COLLECTION_LABEL = "Open ZIMs"
_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")
_RECOVERY_MARKERS = (".catalog.recovery", "catalog.recovery", "catalog.sqlite.recovery")


class OpenScopeError(ValueError):
    """A named ZIM set cannot be expanded or resolved safely."""


@dataclass(frozen=True)
class ZimOpenScope:
    """The ordered archive selection of one ``zimantic open`` process."""

    paths: tuple[Path, ...]
    keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.paths) != len(self.keys):
            raise ValueError("open scope paths and keys must have equal length")


@dataclass(frozen=True)
class _CatalogMatch:
    key: str
    collection_key: str


def _safe_stem(value: str) -> str:
    text = _SAFE_STEM.sub("-", str(value).casefold()).strip("-._")
    return (text or "zim")[:48].strip("-._") or "zim"


def expand_open_paths(
    paths: Iterable[Path | str],
    *,
    max_sources: int = DEFAULT_MAX_SOURCES,
    max_visited_entries: int = DEFAULT_EXPANSION_MAX_ENTRIES,
    timeout_seconds: float = DEFAULT_EXPANSION_TIMEOUT_SECONDS,
) -> list[Path]:
    """Expand files and folders into ordered, deduplicated resolved ZIM paths.

    File arguments keep their command-line order.  Folders contribute their ZIM
    files in recursive, sorted order.  The traversal budget counts every
    visited entry (not only ZIM files) so a huge or slow folder fails explicitly
    instead of producing a silently incomplete scope.
    """
    from .providers.zim.discovery import is_zim_filename

    if max_sources < 1:
        raise OpenScopeError("--max-sources must be a positive integer")
    started = time.monotonic()
    visited = 0
    expanded: list[Path] = []
    seen: set[Path] = set()

    def note_visit() -> None:
        nonlocal visited
        visited += 1
        if visited > max_visited_entries:
            raise OpenScopeError(
                "ZIM folder expansion exceeded the "
                f"{max_visited_entries}-entry limit; use a narrower path or "
                "`zimantic serve` for a whole library"
            )
        if time.monotonic() - started > timeout_seconds:
            raise OpenScopeError(
                "ZIM folder expansion exceeded the "
                f"{timeout_seconds:g}-second limit; use a narrower path or "
                "`zimantic serve` for a whole library"
            )

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        expanded.append(resolved)
        if len(expanded) > max_sources:
            raise OpenScopeError(
                f"{len(expanded)} ZIM files were selected, but --max-sources is "
                f"{max_sources}; use `zimantic serve` for a whole library"
            )

    for source in paths:
        source_path = Path(source)
        if source_path.is_dir():
            for directory, dirnames, filenames in os.walk(source_path):
                dirnames.sort()
                for name in dirnames:
                    note_visit()
                for name in sorted(filenames):
                    note_visit()
                    if not is_zim_filename(name):
                        continue
                    candidate = Path(directory, name)
                    if candidate.is_file():
                        add(candidate)
        elif source_path.is_file():
            note_visit()
            if not is_zim_filename(source_path.name):
                raise OpenScopeError(f"not a ZIM file: {source_path}")
            add(source_path)
        else:
            raise OpenScopeError(f"path not found: {source_path}")
    if not expanded:
        raise OpenScopeError("no ZIM files were found in the selected paths")
    return expanded


def _recovery_marker(catalog: Path) -> Path | None:
    for name in _RECOVERY_MARKERS:
        candidate = catalog.parent / name
        if candidate.exists():
            return candidate
    return None


def _lock_shared(fd: int) -> None:
    lock_file(fd, shared=True, blocking=True)


def _unlock(fd: int) -> None:
    try:
        unlock_file(fd)
    except OSError:
        pass


@contextmanager
def _existing_shared_lease(lease_path: Path) -> Iterator[None]:
    """Hold an existing catalog lease through a read-only descriptor.

    Shared locking does not need write access; a read-only descriptor is what
    lets ``zimantic open`` reuse canonical keys when the index directory is on
    read-only media.
    """
    fd = os.open(lease_path, os.O_RDONLY)
    try:
        _lock_shared(fd)
        yield
    finally:
        _unlock(fd)
        os.close(fd)


def _copy_catalog(source: Path, destination: Path) -> None:
    """Copy a live WAL database into a private, consistent snapshot."""
    shutil.copyfile(source, destination)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{source}{suffix}")
        if sidecar.is_file():
            shutil.copyfile(sidecar, Path(f"{destination}{suffix}"))


def _read_catalog_matches(
    catalog: Path,
    wanted: dict[str, tuple[Path, int, str]],
) -> dict[str, list[_CatalogMatch]]:
    """Return catalog rows matching each live path, size, and UUID."""
    if not catalog.is_file():
        raise OpenScopeError("catalog is absent")
    marker = _recovery_marker(catalog)
    if marker is not None:
        raise OpenScopeError(f"phase-00 recovery marker exists ({marker.name})")
    lease = catalog_lease_path(catalog)
    if not lease.is_file():
        raise OpenScopeError("catalog lease is absent")

    matches: dict[str, list[_CatalogMatch]] = {}
    with (
        _existing_shared_lease(lease),
        tempfile.TemporaryDirectory(prefix="zimantic-open-catalog-") as tmp,
    ):
        snapshot = Path(tmp) / "catalog.sqlite"
        _copy_catalog(catalog, snapshot)
        # A normal read-write connection to our private copy folds a live
        # WAL (or rolls back a hot journal) back into the snapshot without
        # touching the user's database.
        if any(
            Path(f"{snapshot}{suffix}").is_file()
            for suffix in ("-wal", "-journal")
        ):
            connection = sqlite3.connect(snapshot)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.commit()
            finally:
                connection.close()
        connection = sqlite3.connect(
            Path(snapshot).resolve().as_uri() + "?mode=ro&immutable=1",
            uri=True,
        )
        try:
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(sources)"
                ).fetchall()
            }
            if version != SCHEMA_VERSION or columns != set(SOURCE_COLUMNS):
                raise OpenScopeError("catalog schema is incompatible")
            rows = connection.execute(
                "SELECT key, collection_key, path, size, provider_id "
                "FROM sources WHERE provider = 'zim'"
            ).fetchall()
        finally:
            connection.close()

    for key, collection_key, path, size, provider_id in rows:
        if not path or path not in wanted:
            continue
        _path, expected_size, expected_uuid = wanted[path]
        if size is None or int(size) != expected_size:
            continue
        if not provider_id or str(provider_id).casefold() != expected_uuid.casefold():
            continue
        matches.setdefault(path, []).append(
            _CatalogMatch(str(key), str(collection_key))
        )
    return matches


def _live_identity(path: Path) -> tuple[int, str] | None:
    from .providers.zim.metadata import header_uuid

    try:
        size = path.stat().st_size
    except OSError:
        return None
    try:
        uuid = header_uuid(path)
    except (OSError, TypeError, ValueError):
        try:
            from libzim.reader import Archive

            uuid = str(Archive(str(path)).uuid)
        except Exception:
            return None
    uuid = str(uuid).strip()
    if not uuid:
        return None
    return size, uuid


def build_open_scope(paths: Iterable[Path | str], cfg: dict[str, Any]) -> ZimOpenScope:
    """Resolve ordered archive paths to canonical or ephemeral source keys."""
    resolved_paths = tuple(Path(path).resolve() for path in paths)
    if not resolved_paths:
        raise OpenScopeError("at least one ZIM path is required")

    index_dir = cfg.get("index_dir")
    catalog = catalog_path(index_dir) if index_dir else None
    wanted: dict[str, tuple[Path, int, str]] = {}
    for path in resolved_paths:
        identity = _live_identity(path)
        if identity is None:
            logger.debug("zimantic: open: no live UUID for %s", path)
            continue
        size, uuid = identity
        wanted[str(absolute_path(path))] = (path, size, uuid)

    matches: dict[str, list[_CatalogMatch]] = {}
    if catalog is not None and wanted:
        try:
            matches = _read_catalog_matches(catalog, wanted)
        except (OpenScopeError, OSError, sqlite3.Error) as error:
            logger.warning(
                "zimantic: open: using ephemeral source keys (%s)",
                public_cause(error) if not isinstance(error, OpenScopeError) else error,
            )
            logger.debug("open catalog lookup error: %s", error)
    elif not wanted:
        logger.warning(
            "zimantic: open: using ephemeral source keys (no live archive UUID "
            "could be read)"
        )

    keys: list[str] = []
    used: set[str] = set()
    for position, path in enumerate(resolved_paths, start=1):
        candidates = matches.get(str(absolute_path(path)), ())
        if len(candidates) == 1:
            key = candidates[0].key
        else:
            stem = _safe_stem(path.stem)
            key = f"{OPEN_COLLECTION_KEY}:{stem}-{position}"
        while key in used:
            key = f"{key}-x"
        used.add(key)
        keys.append(key)
    return ZimOpenScope(paths=resolved_paths, keys=tuple(keys))


__all__ = [
    "DEFAULT_EXPANSION_MAX_ENTRIES",
    "DEFAULT_EXPANSION_TIMEOUT_SECONDS",
    "DEFAULT_MAX_SOURCES",
    "OPEN_COLLECTION_KEY",
    "OPEN_COLLECTION_LABEL",
    "OpenScopeError",
    "ZimOpenScope",
    "build_open_scope",
    "expand_open_paths",
]
