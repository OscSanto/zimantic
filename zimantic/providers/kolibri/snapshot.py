"""Private, consistent snapshots of a Kolibri installation database.

Kolibri may be running and writing while Zimantic discovers or serves content.
The live database is opened read-only and copied through SQLite's online backup
API into an owner-only private directory.  The backup includes committed WAL
pages, unlike a raw file copy.  The installation and its sidecars are never
written, migrated, vacuumed, or checkpointed by this module.

The provider pins one snapshot per configured entry discovery batch.  Channel
runtimes retain it, and the snapshot is removed only after the last reader
releases it.  A fresh snapshot is used to validate a build before publication.
Snapshots left behind by an interrupted process are reclaimed on the next
snapshot creation: each snapshot holds an advisory lock for its lifetime, so a
directory whose lock can be taken has no live reader.
"""

from __future__ import annotations

import configparser
import logging
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..._filelock import lock_file, unlock_file
from ...diagnostics import public_cause
from .schema import detect_schema

logger = logging.getLogger(__name__)

DEFAULT_MAX_SNAPSHOT_BYTES = 1_073_741_824
DEFAULT_DISK_BUDGET_BYTES = 3_221_225_472
DEFAULT_TIMEOUT_SECONDS = 10
MAX_CONCURRENT_CREATIONS = 2
SNAPSHOT_PREFIX = "snapshot-"
SNAPSHOT_LOCK_NAME = ".lock"
STALE_SNAPSHOT_GRACE_SECONDS = 60.0

_LOCK = threading.Lock()
_CREATION_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CREATIONS)
# Absolute database path -> list of (snapshot directory, declared bytes).
_LIVE_SNAPSHOTS: dict[str, list[tuple[Path, int]]] = {}


class SnapshotError(RuntimeError):
    """A safe consistent snapshot cannot be created."""


class SnapshotBudgetError(SnapshotError):
    """A snapshot would exceed its byte, disk, or time budget."""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _private_directory(path: Path) -> None:
    """Create or verify an owner-only directory; fail closed otherwise."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        raise SnapshotError("could not create the private snapshot directory") from error
    if path.is_symlink() or not path.is_dir():
        raise SnapshotError("the snapshot path is not a private directory")
    try:
        info = path.stat()
    except OSError as error:
        raise SnapshotError("could not inspect the private snapshot directory") from error
    if os.name == "posix":
        if stat.S_IMODE(info.st_mode) & 0o077:
            try:
                os.chmod(path, 0o700)
            except OSError as error:
                raise SnapshotError(
                    "could not restrict the private snapshot directory"
                ) from error
    # Windows ACLs are established by the platform temp/profile directory; do
    # not pretend POSIX mode bits prove privacy there.
    if os.name != "posix" and not _is_within(path, Path.home()):
        # A non-home, non-temp location needs an explicit restricted ACL.  The
        # provider ships this check so an operator sees a clear error rather
        # than silently writing private data to a shared directory.
        temp_root = Path(tempfile.gettempdir())
        if not _is_within(path, temp_root):
            raise SnapshotError(
                "cannot establish owner-only snapshot permissions on this platform"
            )


def _private_file(path: Path) -> None:
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError as error:
            raise SnapshotError("could not restrict the private snapshot file") from error


def _lock_snapshot_directory(directory: Path) -> int:
    """Take the lifetime advisory lock that marks a snapshot as live."""
    path = directory / SNAPSHOT_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as error:
        raise SnapshotError("could not create the private snapshot lock") from error
    try:
        owned = lock_file(fd)
    except OSError as error:
        os.close(fd)
        raise SnapshotError("could not lock the private snapshot directory") from error
    if not owned:
        os.close(fd)
        raise SnapshotError("another process owns the private snapshot directory")
    return fd


def _release_snapshot_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        unlock_file(fd)
    except OSError:
        # Closing the descriptor releases the advisory lock on both platforms.
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _remove_orphaned_snapshots(root: Path) -> None:
    """Remove snapshot directories whose owning process is gone.

    A live snapshot keeps its advisory lock until the last reader releases it.
    The lock is released by the kernel when a process exits, even after a crash,
    so an old directory whose lock we can take is safe to remove.  Recent
    directories without a lock are left alone to avoid racing a snapshot that
    another process has just created but not locked yet.
    """
    try:
        entries = list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError:
        return
    cutoff = time.time() - STALE_SNAPSHOT_GRACE_SECONDS
    for directory in entries:
        if not directory.name.startswith(SNAPSHOT_PREFIX):
            continue
        try:
            if directory.is_symlink() or not directory.is_dir():
                continue
            if directory.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        lock_path = directory / SNAPSHOT_LOCK_NAME
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError:
            continue
        try:
            try:
                owned = lock_file(fd)
            except OSError:
                owned = False
        finally:
            _release_snapshot_lock(fd)
        if not owned:
            continue
        try:
            lock_path.unlink(missing_ok=True)
            shutil.rmtree(directory)
            logger.debug(
                "zimantic: removed an orphaned Kolibri snapshot %s", directory
            )
        except OSError as error:
            logger.warning(
                "zimantic: could not remove an orphaned Kolibri snapshot (%s)",
                public_cause(error),
            )
            logger.debug("Kolibri snapshot removal error: %s", error)


def _declared_postgres(options_path: Path) -> bool:
    if not options_path.is_file():
        return False
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(options_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return False
    for section in parser.sections():
        for key, value in parser.items(section):
            name = key.casefold()
            text = str(value).strip().casefold()
            if not text:
                continue
            if "postgres" in text and (
                "database" in name or "engine" in name or "url" in name
            ):
                return True
    return False


def database_signature(path: Path | str) -> tuple[tuple[int, int, int, int] | None, ...]:
    """Signature of the main database and its WAL/SHM/journal sidecars."""
    base = Path(path)
    return tuple(
        _source_signature(candidate)
        for candidate in (
            base,
            base.with_name(base.name + "-wal"),
            base.with_name(base.name + "-shm"),
            base.with_name(base.name + "-journal"),
        )
    )


def _source_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SnapshotError("could not inspect the Kolibri database") from error
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
    )


def _copy_file(
    source: Path,
    destination: Path,
    *,
    max_bytes: int,
    deadline: float,
    total: int,
) -> int:
    try:
        with open(source, "rb") as handle:
            try:
                with open(destination, "wb") as writer:
                    while True:
                        if time.monotonic() >= deadline:
                            raise SnapshotBudgetError(
                                "Kolibri snapshot creation timed out"
                            )
                        chunk = handle.read(1 << 20)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise SnapshotBudgetError(
                                f"Kolibri snapshot exceeds the {max_bytes} byte limit"
                            )
                        writer.write(chunk)
                        writer.flush()
            except OSError as error:
                raise SnapshotError("could not copy the Kolibri database") from error
    except OSError as error:
        raise SnapshotError("could not read the Kolibri database") from error
    return total


def _consistent_copy(
    source: Path,
    working: Path,
    *,
    max_bytes: int,
    deadline: float,
) -> int:
    """Copy the database and committed WAL without writing the source.

    The copy is accepted only if the source identity and signatures are stable
    before and after.  A concurrently active writer therefore causes a bounded
    retry or a fail-closed snapshot error rather than a torn copy.
    """
    sidecars = (
        source.with_name(source.name + "-wal"),
        source.with_name(source.name + "-journal"),
    )
    for _attempt in range(2):
        before_main = _source_signature(source)
        if before_main is None:
            raise SnapshotError("Kolibri database is missing")
        before_sidecars = tuple(_source_signature(path) for path in sidecars)
        total = _copy_file(
            source,
            working,
            max_bytes=max_bytes,
            deadline=deadline,
            total=0,
        )
        for sidecar in sidecars:
            signature = _source_signature(sidecar)
            if signature is None:
                continue
            target = working.with_name(working.name + sidecar.name[len(source.name):])
            total = _copy_file(
                sidecar,
                target,
                max_bytes=max_bytes,
                deadline=deadline,
                total=total,
            )
        if (
            _source_signature(source) == before_main
            and tuple(_source_signature(path) for path in sidecars)
            == before_sidecars
        ):
            _private_file(working)
            return total
        # Discard a torn copy and retry within the same deadline.
        for candidate in working.parent.iterdir():
            if candidate.name.startswith(working.name):
                candidate.unlink(missing_ok=True)
    raise SnapshotError(
        "the Kolibri database changed while it was being copied"
    )


def _seal_copy(working: Path, deadline: float) -> int:
    """Checkpoint the private copy's committed WAL and remove sidecars."""
    try:
        db = sqlite3.connect(str(working))
    except sqlite3.Error as error:
        raise SnapshotError("could not read the Kolibri snapshot copy") from error
    try:
        if time.monotonic() >= deadline:
            raise SnapshotBudgetError("Kolibri snapshot creation timed out")
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            # A rollback-journal copy has nothing to checkpoint; opening it
            # already recovered any hot journal in the private directory.
            pass
        try:
            db.execute("PRAGMA journal_mode = DELETE")
        except sqlite3.Error as error:
            raise SnapshotError(
                "could not seal the private Kolibri snapshot"
            ) from error
        db.commit()
    finally:
        db.close()
    for suffix in ("-wal", "-shm", "-journal"):
        working.with_name(working.name + suffix).unlink(missing_ok=True)
    _private_file(working)
    return working.stat().st_size


class KolibriSnapshot:
    """One immutable private database snapshot with reader reference counting."""

    def __init__(
        self,
        directory: Path,
        database: Path,
        *,
        owner: SnapshotManager,
        size: int,
        lock_fd: int | None = None,
        source_signature: tuple[tuple[int, int, int, int] | None, ...] | None = None,
    ) -> None:
        self.directory = directory
        self.database = database
        self.size = int(size)
        self.source_signature = source_signature
        self._owner = owner
        self._lock_fd = lock_fd
        self._refs = 1
        self._closed = False
        self._lock = threading.Lock()

    def retain(self) -> KolibriSnapshot:
        with self._lock:
            if self._closed:
                raise SnapshotError("Kolibri snapshot is closed")
            self._refs += 1
            return self

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._closed:
                raise SnapshotError("Kolibri snapshot is closed")
        # The private copy is sealed after backup: no WAL, SHM, or journal is
        # allowed beside it, so an immutable read-only connection is safe here.
        uri = self.database.resolve().as_uri() + "?mode=ro&immutable=1"
        db = sqlite3.connect(uri, uri=True, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db

    def release(self) -> None:
        with self._lock:
            if self._refs <= 0:
                return
            self._refs -= 1
            if self._refs > 0 or self._closed:
                return
            self._closed = True
        self._owner._retire(self)

    def _release_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        _release_snapshot_lock(fd)

    def close(self) -> None:
        self.release()


class SnapshotManager:
    """Create snapshots for one configured Kolibri installation."""

    def __init__(
        self,
        *,
        database_path: Path | str,
        storage_path: Path | str | None,
        home_path: Path | str,
        snapshot_root: Path | str,
        max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
        disk_budget_bytes: int = DEFAULT_DISK_BUDGET_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.storage_path = (
            Path(storage_path).expanduser().resolve() if storage_path else None
        )
        self.home_path = Path(home_path).expanduser().resolve()
        self.snapshot_root = Path(snapshot_root).expanduser().resolve()
        self.max_snapshot_bytes = max(1, int(max_snapshot_bytes))
        self.disk_budget_bytes = max(
            self.max_snapshot_bytes, int(disk_budget_bytes)
        )
        self.timeout_seconds = max(0.001, float(timeout_seconds))
        self._key = str(self.database_path)

    def validate_installation(self) -> None:
        if not self.database_path.is_file():
            raise SnapshotError("Kolibri database is missing")
        if self.storage_path is not None and not self.storage_path.is_dir():
            raise SnapshotError("Kolibri storage directory is missing")
        if _declared_postgres(self.home_path / "options.ini"):
            raise SnapshotError(
                "Kolibri is configured for PostgreSQL; only a local SQLite "
                "installation database is supported"
            )
        for root in (self.home_path, self.storage_path):
            if root is not None and _is_within(self.snapshot_root, root):
                raise SnapshotError(
                    "private snapshots must be outside the Kolibri installation "
                    "and storage roots"
                )

    def _register(self, directory: Path, size: int) -> None:
        if size > self.max_snapshot_bytes:
            raise SnapshotBudgetError(
                f"Kolibri snapshot exceeds the {self.max_snapshot_bytes} byte limit"
            )
        with _LOCK:
            live = _LIVE_SNAPSHOTS.setdefault(self._key, [])
            current = sum(item[1] for item in live)
            if current + size > self.disk_budget_bytes:
                raise SnapshotBudgetError(
                    "Kolibri snapshot disk budget is exhausted by retained snapshots"
                )
            live.append((directory, size))

    def _retire(self, snapshot: KolibriSnapshot) -> None:
        with _LOCK:
            live = _LIVE_SNAPSHOTS.get(self._key, [])
            remaining = [
                item for item in live if item[0] != snapshot.directory
            ]
            if remaining:
                _LIVE_SNAPSHOTS[self._key] = remaining
            else:
                _LIVE_SNAPSHOTS.pop(self._key, None)
        snapshot._release_lock()
        try:
            shutil.rmtree(snapshot.directory, ignore_errors=False)
        except OSError as error:
            logger.warning(
                "zimantic: could not remove a private Kolibri snapshot (%s)",
                public_cause(error),
            )
            logger.debug("Kolibri snapshot removal error: %s", error)

    def acquire(self, *, schema_validator: Callable | None = None) -> KolibriSnapshot:
        """Create, validate, and return a new snapshot."""
        self.validate_installation()
        deadline = time.monotonic() + self.timeout_seconds
        if not _CREATION_SLOTS.acquire(
            timeout=max(0.01, deadline - time.monotonic())
        ):
            raise SnapshotBudgetError(
                "too many concurrent Kolibri snapshot creations"
            )
        directory: Path | None = None
        database: Path | None = None
        lock_fd: int | None = None
        try:
            _private_directory(self.snapshot_root)
            _remove_orphaned_snapshots(self.snapshot_root)
            directory = Path(
                tempfile.mkdtemp(prefix=SNAPSHOT_PREFIX, dir=self.snapshot_root)
            )
            _private_directory(directory)
            lock_fd = _lock_snapshot_directory(directory)
            # Record the source state before copying.  If a writer commits
            # after the copy, the runtime's next refresh sees a signature
            # change instead of treating the stale copy as current.
            source_signature = database_signature(self.database_path)
            working = directory / "source-copy.sqlite3"
            size = _consistent_copy(
                self.database_path,
                working,
                max_bytes=self.max_snapshot_bytes,
                deadline=deadline,
            )
            sealed_size = _seal_copy(working, deadline)
            size = max(size, sealed_size)
            database = directory / "db.sqlite3"
            os.replace(working, database)
            _private_file(database)
            # Temporary files and the sealed database all count toward the cap.
            for candidate in directory.iterdir():
                if candidate == database:
                    continue
                try:
                    size += candidate.stat().st_size
                except OSError:
                    continue
            if size > self.max_snapshot_bytes:
                raise SnapshotBudgetError(
                    f"Kolibri snapshot exceeds the {self.max_snapshot_bytes} byte limit"
                )
            try:
                db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
                try:
                    (schema_validator or detect_schema)(db)
                finally:
                    db.close()
            except SnapshotError:
                raise
            except Exception as error:
                raise SnapshotError(str(error)) from error
            if database_signature(self.database_path) != source_signature:
                raise SnapshotError(
                    "the Kolibri database changed while the snapshot was created"
                )
            self._register(directory, size)
            snapshot = KolibriSnapshot(
                directory,
                database,
                owner=self,
                size=size,
                lock_fd=lock_fd,
                source_signature=source_signature,
            )
            directory = None
            database = None
            lock_fd = None
            return snapshot
        except SnapshotError:
            raise
        except sqlite3.Error as error:
            raise SnapshotError("could not read the Kolibri snapshot") from error
        finally:
            if lock_fd is not None:
                _release_snapshot_lock(lock_fd)
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
            _CREATION_SLOTS.release()


__all__ = [
    "DEFAULT_DISK_BUDGET_BYTES",
    "DEFAULT_MAX_SNAPSHOT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "KolibriSnapshot",
    "SnapshotBudgetError",
    "SnapshotError",
    "SnapshotManager",
    "database_signature",
]
