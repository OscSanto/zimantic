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
    lock: threading.Lock
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
    source_size: int | None = None
    zim_path: str | None = None
    zim_signature: tuple[int, int, int, int] | None = None

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
