"""Advisory lock that serializes index builds.

Two builds writing the same ``index_dir`` can interleave updates to the shared
catalog and, on a small host, both load the embedding model and FAISS at once.
The lock lives beside ``index_dir`` (next to ``catalog.sqlite``) so every
entry point -- the IIAB helper script, a systemd timer, or a manual
``zimantic build`` -- contends for the same file.

The lock is released by the kernel when the process exits, so a crashed or
killed build leaves no stale lock behind.
"""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt

# sysexits.h EX_TEMPFAIL: the operation could not run now, but retrying later
# may succeed. Callers such as a systemd.path trigger treat this as "skip".
BUILD_BUSY_EXIT = 75

LOCK_FILENAME = ".build.lock"
POLL_SECONDS = 0.2


def build_lock_path(index_dir: Path | str) -> Path:
    """Return the lock path beside ``index_dir``, next to ``catalog.sqlite``."""
    return Path(index_dir).parent / LOCK_FILENAME


class BuildLockError(RuntimeError):
    """The lock file exists but cannot be opened or locked."""


class BuildLock:
    """A non-reentrant, cross-process lock on one file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def try_acquire(self) -> bool:
        """Take the lock without waiting; return False when another build holds it."""
        if self._fd is not None:
            return True
        fd = self._open()
        if self._lock(fd):
            self._fd = fd
            return True
        os.close(fd)
        return False

    def acquire(self, timeout: float | None = None) -> bool:
        """Wait for the lock, or return False after ``timeout`` seconds.

        ``timeout=None`` waits indefinitely.
        """
        if self.try_acquire():
            return True
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(POLL_SECONDS)
            if self.try_acquire():
                return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            self._unlock(fd)
        finally:
            os.close(fd)

    def _open(self) -> int:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as error:
            raise BuildLockError(
                f"zimantic: cannot open the build lock {self.path}: "
                f"{error.strerror or error}. Another user may own the file "
                "(for example after running a build with sudo). Remove it, "
                "make it writable by the user running Zimantic, or run as the "
                "user that owns it."
            ) from error

    @staticmethod
    def _lock(fd: int) -> bool:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise

    @staticmethod
    def _unlock(fd: int) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            # Closing the descriptor releases the lock on both platforms; a
            # failed explicit unlock must not hide the build's real result.
            pass
