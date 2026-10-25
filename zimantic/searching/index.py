"""State and native-index helpers for local search sources."""

from dataclasses import dataclass, field
import math
import threading
import sqlite3
from typing import Any

from libzim.reader import Archive
from libzim.search import Searcher

from .config import _int_config


DEFAULT_NPROBE_FRACTION = 0.06


@dataclass
class _LocalIndex:
    """Open resources and lifecycle state for one local ZIM index."""

    db: sqlite3.Connection | None
    faiss_index: Any | None
    archive: Archive | None
    searcher: Searcher | None
    lock: threading.Lock  # serializes libzim Searcher (not thread-safe)
    # Serializes bulk archive-entry scans (previews, MIME fills, phrase
    # checks, article reads) so they do not stall full-text search and vice
    # versa. libzim entry reads are thread-safe, so this is a courtesy bound,
    # not a correctness requirement.
    read_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    db_path: str | None = None  # read-only sqlite URI, used for per-thread connections
    db_signature: tuple[int, int, int, int] | None = None
    faiss_signature: tuple[int, int, int, int] | None = None
    _thread: threading.local = field(default_factory=threading.local)
    _conns: list[sqlite3.Connection] = field(default_factory=list)
    _lifecycle: threading.Condition = field(default_factory=threading.Condition, repr=False)
    _active_searches: int = 0
    _retired: bool = False
    _closed: bool = False
    done: str = "1"
    # Entry ids that /random accepts for an unindexed ZIM, built lazily on the
    # first request. Without a docs table there is no rowid set to sample, so a
    # sparse archive needs this one-time scan to remain both random and
    # reliable. Guarded by read_lock; reset whenever the index is reopened.
    random_article_ids: list[int] | None = field(
        default=None, repr=False, compare=False
    )
    source_size: int | None = None
    zim_path: str | None = None
    zim_signature: tuple[int, int, int, int] | None = None
    has_fulltext_index: bool | None = None
    error: str | None = None
    open_failed: bool = field(default=False, repr=False, compare=False)

    def ensure_open(self) -> Archive | None:
        """Open the archive on first use, not at server startup."""
        if self.archive is not None:
            return self.archive
        if self.open_failed:
            # Remember the failure: retrying on every search would reopen a
            # missing or corrupt archive each time.  reload() builds a fresh
            # _LocalIndex, which clears the flag.
            return None
        with self.lock:
            if self.archive is None:
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
