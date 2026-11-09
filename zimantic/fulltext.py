"""Bounded libzim full-text search, using processes or opt-in threads.

python-libzim declares the ``SearchIterator`` getters without ``except +`` (see
openzim/python-libzim#266). When a ZIM's full-text index references an entry
that is no longer in the archive, those getters throw ``zim::EntryNotFound``
through the CPython evaluation loop and abort the whole process. Python cannot
catch that, so native full-text searches run in helper processes: a corrupt
archive can take down a helper, not the server.

The helpers also keep opened archives and searchers cached, so repeated
searches do not pay the archive-open cost again.

The thread backend requires a GIL-releasing binding. Each worker owns separate
native handles; timeouts stop waiting but cannot interrupt a native call.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import signal
import subprocess
import sys
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError
from pathlib import Path

from .diagnostics import sanitize_public_text

logger = logging.getLogger(__name__)


def _archive_name(path: str | None) -> str:
    """A log-safe archive name; the full locator stays at debug level."""
    return Path(str(path or "")).name or "<archive>"

_WORKER_MODULE = "zimantic.fulltext_worker"
FULLTEXT_TIMEOUT_SECONDS = 30.0


class _Worker:
    """One helper process, used by one search thread at a time."""

    def __init__(self, closed: threading.Event) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._closed = closed
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
            and -process.returncode in {
                getattr(signal, name, None)
                for name in ("SIGABRT", "SIGSEGV", "SIGBUS", "SIGILL", "SIGFPE")
            }
        )

    def _ensure_process(self) -> subprocess.Popen[str]:
        with self._process_lock:
            if self._closed.is_set():
                raise OSError("native full-text pool is closed")
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
                if self._closed.is_set():
                    return None
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
        with self._process_lock:
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


class _ThreadWorker:
    def __init__(self, closed: threading.Event) -> None:
        from .fulltext_worker import ArchiveCache

        self._cache = ArchiveCache()
        self._closed = closed
        self._lock = threading.Lock()
        self.archive_failure = False

    def search_results(
        self, zim_path: str, query: str, count: int, version: object = None,
    ) -> list[dict[str, str]] | None:
        with self._lock:
            self.archive_failure = False
            if self._closed.is_set():
                return None
            try:
                return self._cache.search_results(zim_path, query, count, version)
            except Exception as error:
                self.archive_failure = True
                logger.warning(
                    "zimantic: native full-text search failed for %s: %s",
                    _archive_name(zim_path),
                    sanitize_public_text(str(error), (str(zim_path),)),
                )
                logger.debug("full-text failure for %s", zim_path, exc_info=True)
                return None
            finally:
                if self._closed.is_set():
                    self._cache.clear()

    def close(self) -> None:
        if self._lock.acquire(blocking=False):
            try:
                self._cache.clear()
            finally:
                self._lock.release()


class FulltextPool:
    """A bounded native-search pool with worker-private archive caches."""

    def __init__(
        self, workers: int | None = None, *, backend: str = "processes",
    ) -> None:
        if backend not in {"processes", "threads"}:
            raise ValueError("native full-text backend must be 'processes' or 'threads'")
        if backend == "threads":
            from .libzim_compat import supports_threaded_search

            if not supports_threaded_search():
                raise ValueError(
                    "threaded full-text search requires a GIL-releasing libzim binding"
                )
        if workers is None:
            from .settings import default_fulltext_workers

            workers = default_fulltext_workers()
        count = max(1, int(workers))
        self.backend = backend
        self._closed = threading.Event()
        self._executor = (
            ThreadPoolExecutor(count, thread_name_prefix="zimantic-fulltext")
            if backend == "threads" else None
        )
        self._idle: queue.Queue[_Worker | _ThreadWorker] = queue.Queue()
        self._all: list[_Worker | _ThreadWorker] = []
        for _ in range(count):
            worker = (
                _ThreadWorker(self._closed) if backend == "threads"
                else _Worker(self._closed)
            )
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
        """Return result paths and titles from one native worker."""
        if self._closed.is_set() or self.is_broken(zim_path):
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
        archive_failure = False
        release_worker = True
        try:
            if self._closed.is_set():
                results = None
            elif self._executor is not None:
                future = self._executor.submit(
                    self._thread_search, worker, zim_path, query, count, version,
                )
                release_worker = False
                try:
                    results, archive_failure = future.result(
                        timeout=FULLTEXT_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    logger.warning(
                        "zimantic: native full-text thread timed out for %s; "
                        "its slot remains occupied until native work finishes",
                        _archive_name(zim_path),
                    )
                    return None
                except CancelledError:
                    return None
            else:
                results = worker.search_results(zim_path, query, count, version)
                archive_failure = worker.archive_failure
        except Exception as error:  # never let helper plumbing fail a search
            logger.warning(
                "zimantic: full-text helper failed for %s: %s",
                _archive_name(zim_path),
                type(error).__name__,
            )
            logger.debug("full-text helper failure for %s: %s", zim_path, error, exc_info=True)
            results = None
        finally:
            if release_worker:
                self._idle.put(worker)
        if results is None and archive_failure and not self._closed.is_set():
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

    def _thread_search(self, worker, zim_path, query, count, version):
        try:
            return (
                worker.search_results(zim_path, query, count, version),
                worker.archive_failure,
            )
        finally:
            self._idle.put(worker)

    def clear_broken(self) -> None:
        """Retry archives after a reload; an in-place repair may have fixed one."""
        with self._broken_lock:
            self._broken.clear()

    def close(self) -> None:
        self._closed.set()
        atexit.unregister(self.close)
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        for worker in self._all:
            worker.close()
        with self._broken_lock:
            self._broken.clear()
