"""Process-based ZIM entry reading for CPU-bound HTML extraction.

libzim access stays in the parent producer thread, but the traversal that turns
one decompressed entry into an :class:`EntryRecord` is Python-heavy.  Moving the
whole read-and-extract step into helper processes scales it across cores, where
threads only contend for the GIL.  Each worker opens its own read-only archive.
"""

from __future__ import annotations

import logging
import multiprocessing
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Any

from ...indexing.types import EntryRecord
from .entries import _validate_entry_record

logger = logging.getLogger(__name__)

_WORKER_ARCHIVE = None
_WORKER_OPTIONS: dict[str, Any] = {}


def _initialize_worker(path: str, options: dict[str, Any]) -> None:
    from libzim.reader import Archive

    global _WORKER_ARCHIVE, _WORKER_OPTIONS
    _WORKER_ARCHIVE = Archive(path)
    _WORKER_OPTIONS = options


def _read_chunk(entry_ids: list[int]) -> list[EntryRecord | None]:
    from ...content.reader import read_entry

    archive = _WORKER_ARCHIVE
    options = _WORKER_OPTIONS
    return [
        read_entry(archive, entry_id, fast=False, **options) for entry_id in entry_ids
    ]


class ParallelEntryReader:
    """Ordered, bounded read-ahead over a ZIM producer's entry order."""

    def __init__(
        self,
        producer: Any,
        *,
        workers: int,
        start: int = 0,
        chunk_size: int = 16,
    ) -> None:
        self.producer = producer
        self.count = int(getattr(producer, "count", 0) or 0)
        self.chunk_size = max(1, int(chunk_size))
        self.origin = max(0, min(int(start), self.count))
        options = {
            "max_html_bytes": producer.max_html_bytes,
            "max_entry_bytes": producer.max_entry_bytes,
            "max_preview_chars": producer.max_preview_chars,
            "max_body_text_bytes": producer.max_body_text_bytes,
        }
        self._executor = ProcessPoolExecutor(
            max_workers=max(1, int(workers)),
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_initialize_worker,
            initargs=(str(producer.path), options),
        )
        self._max_pending = max(1, int(workers))
        self._pending: dict[int, Future] = {}
        self._ready: dict[int, EntryRecord | None] = {}
        self._next_start = self.origin
        self._closed = False

    def _chunk_start(self, index: int) -> int:
        return (
            self.origin + ((index - self.origin) // self.chunk_size) * self.chunk_size
        )

    def _entry_ids(self, start: int, end: int) -> list[int]:
        entry_id_at = getattr(self.producer, "entry_id_at", None)
        if not callable(entry_id_at):
            raise TypeError("parallel ZIM reader requires entry_id_at()")
        return [int(entry_id_at(index)) for index in range(start, end)]

    def _submit_one(self) -> None:
        start = self._next_start
        end = min(start + self.chunk_size, self.count)
        if start >= end:
            return
        self._pending[start] = self._executor.submit(
            _read_chunk, self._entry_ids(start, end)
        )
        self._next_start = end

    def _top_up(self) -> None:
        while len(self._pending) < self._max_pending and self._next_start < self.count:
            self._submit_one()

    def _consume(self, start: int, future: Future) -> None:
        rows = future.result()
        for offset, row in enumerate(rows):
            index = start + offset
            if row is not None:
                _validate_entry_record(row, index)
            self._ready[index] = row

    def read(self, index: int) -> EntryRecord | None:
        if not isinstance(index, int) or index < 0 or index >= self.count:
            return None
        chunk_start = self._chunk_start(index)
        while index not in self._ready:
            while (
                self._next_start <= chunk_start
                and self._next_start < self.count
                and len(self._pending) < self._max_pending
            ):
                self._submit_one()
            self._top_up()
            future = self._pending.pop(chunk_start, None)
            if future is not None:
                self._consume(chunk_start, future)
                break
            if not self._pending:
                raise RuntimeError("parallel entry reader lost its pending chunk")
            # The requested chunk is still queued behind earlier work. Consume
            # the oldest result so submission can advance without unbounded
            # memory growth.
            earliest = min(self._pending)
            self._consume(earliest, self._pending.pop(earliest))
        row = self._ready.pop(index)
        self._top_up()
        return row

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for future in self._pending.values():
            future.cancel()
        self._pending.clear()
        self._ready.clear()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        processes = list(getattr(self._executor, "_processes", {}).values())
        self._executor.shutdown(wait=False, cancel_futures=True)
        for process in processes:
            try:
                process.terminate()
            except Exception:
                logger.debug("could not terminate reader helper", exc_info=True)
        for process in processes:
            try:
                process.join(timeout=5)
            except Exception:
                logger.debug("could not join reader helper", exc_info=True)


__all__ = ["ParallelEntryReader"]
