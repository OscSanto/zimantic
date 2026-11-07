"""Crash-isolated libzim full-text search.

python-libzim declares the ``SearchIterator`` getters without ``except +`` (see
openzim/python-libzim#266). When a ZIM's full-text index references an entry
that is no longer in the archive, those getters throw ``zim::EntryNotFound``
through the CPython evaluation loop and abort the whole process. Python cannot
catch that, so native full-text searches run in helper processes: a corrupt
archive can take down a helper, not the server.

The helpers also keep opened archives and searchers cached, so repeated
searches do not pay the archive-open cost again.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import subprocess
import sys
import threading
from pathlib import Path

from .diagnostics import sanitize_public_text

logger = logging.getLogger(__name__)


def _archive_name(path: str | None) -> str:
    """A log-safe archive name; the full locator stays at debug level."""
    return Path(str(path or "")).name or "<archive>"

_WORKER_MODULE = "zimantic.fulltext_worker"
# Worker processes are relatively cheap (one archive cache each), but they are
# still extra interpreters. Four is enough to hide most of the IPC latency
# without duplicating every archive on a small host.
DEFAULT_WORKERS = 4
FULLTEXT_TIMEOUT_SECONDS = 30.0


class _Worker:
    """One helper process, used by one search thread at a time."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._archive_failure = False

    @property
    def archive_failure(self) -> bool:
        return self._archive_failure

    @staticmethod
    def _process_was_aborted(process: subprocess.Popen[str] | None) -> bool:
        return (
            process is not None
            and process.poll() is not None
            and process.returncode is not None
            and process.returncode < 0
        )

    def _ensure_process(self) -> subprocess.Popen[str]:
        if self._process is None or self._process.poll() is not None:
            self._process = subprocess.Popen(
                [sys.executable, "-m", _WORKER_MODULE],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Inherit stderr so an abort message still reaches the server log.
                stderr=None,
                text=True,
                bufsize=1,
            )
        return self._process

    def search(
        self,
        zim_path: str,
        query: str,
        count: int,
        version: object = None,
    ) -> list[str] | None:
        """Return result paths, or None when the worker cannot answer."""
        results = self.search_results(zim_path, query, count, version)
        return None if results is None else [result["path"] for result in results]

    def search_results(
        self,
        zim_path: str,
        query: str,
        count: int,
        version: object = None,
    ) -> list[dict[str, str]] | None:
        """Return result paths and titles, or None when the worker cannot answer."""
        with self._lock:
            self._archive_failure = False
            process: subprocess.Popen[str] | None = None
            try:
                process = self._ensure_process()
                assert process.stdin is not None
                assert process.stdout is not None
                process.stdin.write(
                    json.dumps(
                        {
                            "path": zim_path,
                            "query": query,
                            "count": int(count),
                            "version": version,
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
                response: queue.Queue[str] = queue.Queue(maxsize=1)

                def read_response() -> None:
                    try:
                        line = process.stdout.readline()
                    except (OSError, ValueError):
                        line = ""
                    try:
                        response.put_nowait(line)
                    except queue.Full:
                        pass

                reader = threading.Thread(target=read_response, daemon=True)
                reader.start()
                try:
                    line = response.get(timeout=FULLTEXT_TIMEOUT_SECONDS)
                except queue.Empty:
                    logger.warning(
                        "zimantic: native full-text search timed out for %s",
                        _archive_name(zim_path),
                    )
                    logger.debug("full-text timeout for %s", zim_path)
                    self.close()
                    reader.join(timeout=1)
                    return None
            except (BrokenPipeError, OSError, ValueError):
                self._archive_failure = self._process_was_aborted(process)
                line = ""
            if not line:
                self._archive_failure |= self._process_was_aborted(process)
                logger.warning(
                    "zimantic: native full-text worker exited without a response "
                    "for %s",
                    _archive_name(zim_path),
                )
                logger.debug("full-text worker failure for %s", zim_path)
                self.close()
                return None
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "zimantic: native full-text worker returned an invalid "
                    "response for %s",
                    _archive_name(zim_path),
                )
                logger.debug("full-text invalid response for %s", zim_path)
                self.close()
                return None
            if payload.get("error"):
                self._archive_failure = True
                logger.warning(
                    "zimantic: native full-text search failed for %s: %s",
                    _archive_name(zim_path),
                    sanitize_public_text(payload["error"], (str(zim_path),)),
                )
                logger.debug("full-text failure for %s: %s", zim_path, payload["error"])
                return None
            results = payload.get("results") or []
            return [
                result if isinstance(result, dict)
                else {"path": str(result), "title": ""}
                for result in results
            ]

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        else:
            process.wait()
        try:
            if process.stdout is not None:
                process.stdout.close()
        except OSError:
            pass


class FulltextPool:
    """A small pool of helper processes for native full-text searches."""

    def __init__(self, workers: int = DEFAULT_WORKERS) -> None:
        count = max(1, int(workers))
        self._idle: queue.Queue[_Worker] = queue.Queue()
        self._all: list[_Worker] = []
        for _ in range(count):
            worker = _Worker()
            self._all.append(worker)
            self._idle.put(worker)
        self._broken: set[str] = set()
        self._broken_lock = threading.Lock()
        atexit.register(self.close)

    def is_broken(self, zim_path: str | None) -> bool:
        if not zim_path:
            return False
        with self._broken_lock:
            return zim_path in self._broken

    def search(
        self,
        zim_path: str,
        query: str,
        count: int,
        version: object = None,
    ) -> list[str] | None:
        """Search one archive; return None when the helper cannot answer."""
        results = self.search_results(zim_path, query, count, version)
        return None if results is None else [result["path"] for result in results]

    def search_results(
        self,
        zim_path: str,
        query: str,
        count: int,
        version: object = None,
    ) -> list[dict[str, str]] | None:
        """Return result paths and titles from one helper process."""
        if self.is_broken(zim_path):
            return None
        try:
            worker = self._idle.get(timeout=FULLTEXT_TIMEOUT_SECONDS)
        except queue.Empty:
            logger.warning(
                "zimantic: no native full-text worker became available for %s",
                _archive_name(zim_path),
            )
            logger.debug("full-text worker unavailable for %s", zim_path)
            return None
        try:
            results = worker.search_results(zim_path, query, count, version)
        except Exception as error:  # never let helper plumbing fail a search
            logger.warning(
                "zimantic: full-text helper failed for %s: %s",
                _archive_name(zim_path),
                type(error).__name__,
            )
            logger.debug("full-text helper failure for %s: %s", zim_path, error)
            results = None
        finally:
            self._idle.put(worker)
        if results is None and worker.archive_failure:
            with self._broken_lock:
                first_failure = zim_path not in self._broken
                self._broken.add(zim_path)
            if first_failure:
                logger.warning(
                    "zimantic: native full-text search unavailable for %s after "
                    "a helper failure; falling back to title and semantic search "
                    "for this archive",
                    _archive_name(zim_path),
                )
                logger.debug("full-text archive marked broken: %s", zim_path)
        return results

    def clear_broken(self) -> None:
        """Retry archives after a reload; an in-place repair may have fixed one."""
        with self._broken_lock:
            self._broken.clear()

    def close(self) -> None:
        for worker in self._all:
            worker.close()
        with self._broken_lock:
            self._broken.clear()
