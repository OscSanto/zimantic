"""State and native-index helpers for local search sources."""

import math
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from libzim.reader import Archive
from libzim.search import Searcher

from .config import _int_config

DEFAULT_NPROBE_FRACTION = 0.06


@dataclass
class IndexLocks:
    """Lifecycle, SQLite-read, and close locks for one index handle."""

    lifecycle: threading.Condition = field(
        default_factory=threading.Condition, repr=False
    )
    # Bulk archive reads are serialized so they do not stall full-text search.
    read: threading.Lock = field(default_factory=threading.Lock, repr=False)
    close: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class IndexHandle:
    """Open resources and lifecycle state for one provider-neutral index.

    ``store`` is borrowed from the runtime; an ``IndexHandle`` never closes it.
    ``generation``/``lease`` pin one immutable generation for this handle's
    lifetime.  ``read_only=True`` additionally forbids repair, lease creation,
    cleanup, and cache writes on the read/reuse path.
    """

    db: sqlite3.Connection | None
    faiss_index: Any | None
    archive: Any | None = None
    searcher: Any | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    db_path: str | Path | None = None  # read-only sqlite URI or path
    db_signature: tuple[int, int, int, int] | None = None
    faiss_signature: tuple[int, int, int, int] | None = None
    _thread: threading.local = field(default_factory=threading.local)
    _conns: list[sqlite3.Connection] = field(default_factory=list)
    _active_searches: int = 0
    _retired: bool = False
    _closed: bool = False
    done: str = "full"
    # Entry ids that /random accepts for an unindexed ZIM, built lazily on the
    # first request. Without a docs table there is no rowid set to sample, so a
    # sparse archive needs this one-time scan to remain both random and
    # reliable. Guarded by locks.read; reset whenever the index is reopened.
    random_article_ids: list[int] | None = field(
        default=None, repr=False, compare=False
    )
    browse_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )
    browse_rankings: dict[str, tuple[int, list[tuple[float, int]]]] = field(
        default_factory=dict, repr=False, compare=False
    )
    browse_total: int | None = field(default=None, repr=False, compare=False)
    browse_centroid: Any | None = field(default=None, repr=False, compare=False)
    source_size: int | None = None
    zim_path: str | None = None
    zim_signature: tuple[int, int, int, int] | None = None
    has_fulltext_index: bool | None = None
    error: str | None = None
    open_failed: bool = field(default=False, repr=False, compare=False)
    # Immutable generation lease pinned for the lifetime of this index handle.
    lease: Any | None = field(default=None, repr=False, compare=False)
    generation: str | None = None
    # Provider-neutral additions.  ``store`` is borrowed, never closed here.
    store: Any | None = field(default=None, repr=False, compare=False)
    native: Any | None = None
    faiss_path: Path | None = None
    signatures: tuple[int, int, int, int] | None = None
    locks: IndexLocks = field(default_factory=IndexLocks, repr=False)
    read_only: bool = False
    index_meta: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.signatures is None:
            db_stat = self._artifact_signature(self.db_path)
            faiss_stat = self._artifact_signature(self.faiss_path)
            self.signatures = (db_stat[0], db_stat[1], faiss_stat[0], faiss_stat[1])

    @staticmethod
    def _artifact_signature(path: Any) -> tuple[int, int]:
        if path is None:
            return (0, 0)
        text = str(path)
        if "?" in text or text.startswith("file:"):
            return (0, 0)
        try:
            stat = Path(text).stat()
        except (OSError, TypeError, ValueError):
            return (0, 0)
        return (stat.st_size, round(stat.st_mtime_ns / 1_000_000_000))

    def ensure_open(self) -> Archive | None:
        """Open the archive on first use, not at server startup."""
        if self.archive is not None:
            return self.archive
        if self.open_failed:
            # Remember the failure: retrying on every search would reopen a
            # missing or corrupt archive each time. reload() calls
            # allow_reopen() so an explicit rescan retries.
            return None
        with self.lock:
            if self.archive is None:
                if self.open_failed:
                    return None
                if not self.zim_path:
                    self.open_failed = True
                    return None
                try:
                    self.archive = Archive(self.zim_path)
                except Exception as error:
                    self.error = str(error)
                    self.open_failed = True
                    return None
            return self.archive

    def allow_reopen(self) -> None:
        """Clear a latched open failure so the next use retries.

        ``reload()`` keeps index objects whose files did not change, so this is
        the recovery path after a transient failure (fixed permissions, a
        remounted drive, or an archive repaired in place).
        """
        self.open_failed = False
        self.error = None

    def ensure_searcher(self) -> Searcher | None:
        """Create the libzim full-text searcher on first use."""
        if self.has_fulltext_index is False:
            return None
        archive = self.ensure_open()
        if archive is None:
            return None
        if self.searcher is not None:
            return self.searcher
        with self.lock:
            if self.searcher is None:
                try:
                    if getattr(archive, "has_fulltext_index", False):
                        self.searcher = Searcher(archive)
                except Exception as error:
                    self.error = str(error)
                    return None
            return self.searcher

    @property
    def semantic(self) -> bool:
        """True when vectors are loaded and semantic search can run."""
        return self.faiss_index is not None

    def acquire(self) -> None:
        """Pin this index for one active reader."""
        with self.locks.lifecycle:
            if self._retired:
                raise RuntimeError("index was retired during reload")
            self._active_searches += 1

    def release(self) -> None:
        """Release one active reader, closing a retired index when idle."""
        with self.locks.lifecycle:
            self._active_searches -= 1
            should_close = (
                self._retired
                and self._active_searches == 0
                and not self._closed
            )
            if should_close:
                self._closed = True
                connections = [self.db, *self._conns]
                self._release_native_resources()
            else:
                connections = []
        if connections:
            self._close_connections(connections)
            self._release_lease()

    def close(self) -> None:
        """Retire and close this index after active readers finish."""
        with self.locks.lifecycle:
            self._retired = True
            if self._active_searches or self._closed:
                return
            self._closed = True
            connections = [self.db, *self._conns]
            self._release_native_resources()
        self._close_connections(connections)
        self._release_lease()

    def _release_native_resources(self) -> None:
        """Drop native handles so Windows can replace the files."""
        self.searcher = None
        self.archive = None
        self.faiss_index = None
        self.random_article_ids = None
        self.browse_rankings.clear()
        self.browse_total = None
        self.browse_centroid = None

    @staticmethod
    def _close_connections(connections: list[sqlite3.Connection | None]) -> None:
        for conn in connections:
            if conn is None:
                continue
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _release_lease(self) -> None:
        lease = self.lease
        self.lease = None
        if lease is not None:
            try:
                lease.close()
            except Exception:
                pass

    def db_for_thread(self) -> sqlite3.Connection | None:
        """A read-only SQLite connection per worker thread."""
        if self.db_path is None:
            return self.db
        conn = getattr(self._thread, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path, uri=True, check_same_thread=False
            )
            self._thread.conn = conn
            with self.lock:
                self._conns.append(conn)
        return conn


def _nprobe(faiss_index: Any, cfg: dict) -> int:
    """Choose IVF probes while allowing an explicit fixed override."""
    nlist = getattr(faiss_index, "nlist", 0)
    if "nprobe" in cfg:
        probes = _int_config(cfg, "nprobe", 64)
    elif nlist:
        probes = math.ceil(nlist * DEFAULT_NPROBE_FRACTION)
    else:
        probes = 64
    return min(probes, nlist) if nlist else probes


__all__ = [
    "DEFAULT_NPROBE_FRACTION",
    "IndexHandle",
    "IndexLocks",
    "_nprobe",
]
