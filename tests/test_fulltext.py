import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from zimantic.fulltext import FulltextPool


class FulltextTimeoutTests(unittest.TestCase):
    def test_closed_pool_cannot_restart_helpers(self):
        pool = FulltextPool(1)
        pool.close()
        with patch.object(pool._all[0], "_ensure_process") as start:
            self.assertIsNone(pool.search("/tmp/example.zim", "query", 1))
        start.assert_not_called()
        with self.assertRaises(OSError):
            pool._all[0]._ensure_process()

    def test_shutdown_terminates_inflight_native_work(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.readline(); sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        pool = FulltextPool(1)
        worker = pool._all[0]
        worker._process = process
        started = threading.Event()
        ensure_process = worker._ensure_process

        def ensure():
            value = ensure_process()
            started.set()
            return value

        try:
            with ThreadPoolExecutor(1) as executor, patch.object(worker, "_ensure_process", side_effect=ensure):
                future = executor.submit(pool.search, "/tmp/example.zim", "query", 1)
                self.assertTrue(started.wait(timeout=1))
                pool.close()
                self.assertIsNone(future.result(timeout=1))
                self.assertIsNone(worker._process)
        finally:
            pool.close()


class FulltextThreadTests(unittest.TestCase):
    def make_pool(self, workers=1):
        with patch("zimantic.libzim_compat.supports_threaded_search", return_value=True):
            pool = FulltextPool(workers, backend="threads")
        self.addCleanup(pool.close)
        return pool

    def test_requires_gil_releasing_binding(self):
        with (
            patch("zimantic.libzim_compat.supports_threaded_search", return_value=False),
            self.assertRaisesRegex(ValueError, "GIL-releasing"),
        ):
            FulltextPool(1, backend="threads")

    def test_timeout_keeps_worker_occupied_and_close_does_not_clear_active_cache(self):
        pool = self.make_pool()
        worker = pool._all[0]
        started = threading.Event()
        finish = threading.Event()
        cleared = threading.Event()

        def blocked(*_):
            started.set()
            finish.wait(timeout=5)
            return [{"path": "article", "title": "Article"}]

        try:
            with (
                patch.object(worker._cache, "search_results", side_effect=blocked),
                patch.object(worker._cache, "clear", side_effect=cleared.set),
                patch("zimantic.fulltext.FULLTEXT_TIMEOUT_SECONDS", 0.02),
            ):
                self.assertIsNone(pool.search_results("/tmp/example.zim", "query", 1))
                self.assertTrue(started.is_set())
                self.assertTrue(pool._idle.empty())
                self.assertFalse(pool.is_broken("/tmp/example.zim"))
                self.assertIsNone(pool.search_results("/tmp/example.zim", "again", 1))
                pool.close()
                self.assertFalse(cleared.is_set())
                finish.set()
                self.assertTrue(cleared.wait(timeout=2))
                self.assertIsNone(pool.search_results("/tmp/example.zim", "closed", 1))
        finally:
            finish.set()

    def test_workers_run_concurrently_with_distinct_caches(self):
        pool = self.make_pool(2)
        ready = threading.Barrier(2)

        def search(*_):
            ready.wait(timeout=2)
            return [{"path": "article", "title": "Article"}]

        self.assertIsNot(pool._all[0]._cache, pool._all[1]._cache)
        with (
            patch.object(pool._all[0]._cache, "search_results", side_effect=search),
            patch.object(pool._all[1]._cache, "search_results", side_effect=search),
            ThreadPoolExecutor(2) as callers,
        ):
            results = list(callers.map(
                lambda _: pool.search("/tmp/example.zim", "query", 1), range(2),
            ))
        self.assertEqual(results, [["article"], ["article"]])

    def test_archive_failure_and_reload(self):
        pool = self.make_pool()
        with patch.object(pool._all[0]._cache, "search_results", side_effect=RuntimeError("bad archive")):
            self.assertIsNone(pool.search("/tmp/faulty.zim", "query", 1))
        self.assertTrue(pool.is_broken("/tmp/faulty.zim"))
        pool.clear_broken()
        with patch.object(
            pool._all[0]._cache, "search_results",
            return_value=[{"path": "fixed", "title": "Fixed"}],
        ):
            self.assertEqual(pool.search("/tmp/faulty.zim", "query", 1), ["fixed"])


class FulltextArchiveCacheTests(unittest.TestCase):
    def test_versioned_handles_are_worker_private_and_lru_bounded(self):
        from types import SimpleNamespace

        from zimantic.fulltext_worker import ArchiveCache

        archive = SimpleNamespace(has_fulltext_index=True)
        with (
            patch("zimantic.fulltext_worker.Archive", return_value=archive) as factory,
            patch("zimantic.fulltext_worker.Searcher"),
            patch("zimantic.fulltext_worker.search_results", return_value=[]),
        ):
            cache = ArchiveCache()
            other = ArchiveCache()
            cache.search_results("a", "query", 1, [1, 2])
            cache.search_results("a", "query", 1, (1, 2))
            self.assertEqual(factory.call_count, 1)
            other.search_results("a", "query", 1, (1, 2))
            self.assertEqual(factory.call_count, 2)
            for path in ("b", "c", "d"):
                cache.search_results(path, "query", 1)
            cache.search_results("a", "query", 1, (1, 2))
            cache.search_results("e", "query", 1)
            self.assertNotIn(("b", None), cache.archives)
            cache.search_results("a", "query", 1, (2, 3))
            self.assertIn(("a", (2, 3)), cache.archives)
            self.assertEqual(len(cache.archives), 4)

class FulltextWorkerFailureTests(unittest.TestCase):
    def test_archive_failure_is_captured_before_worker_is_reused(self):
        pool = FulltextPool(1)
        worker = pool._all[0]
        worker._archive_failure = True
        try:
            with (
                patch.object(worker, "search_results", return_value=None),
                patch.object(
                    pool._idle, "put",
                    side_effect=lambda item: setattr(item, "_archive_failure", False),
                ),
            ):
                self.assertIsNone(pool.search("/tmp/faulty.zim", "query", 1))
            self.assertTrue(pool.is_broken("/tmp/faulty.zim"))
        finally:
            pool.close()

    @unittest.skipIf(sys.platform == "win32", "POSIX signals")
    def test_resource_kill_does_not_blacklist_archive(self):
        process = subprocess.Popen(
            [
                sys.executable, "-c",
                "import os, signal, sys; sys.stdin.readline(); os.kill(os.getpid(), signal.SIGKILL)",
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        pool = FulltextPool(1)
        try:
            pool._all[0]._process = process
            self.assertIsNone(pool.search("/tmp/example.zim", "query", 1))
            self.assertFalse(pool.is_broken("/tmp/example.zim"))
            self.assertIsNone(pool._all[0]._process)
        finally:
            pool.close()

    def test_unresponsive_worker_is_recycled(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdin.readline(); sys.stdin.read()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        pool = FulltextPool(1)
        try:
            worker = pool._all[0]
            worker._process = process
            with patch("zimantic.fulltext.FULLTEXT_TIMEOUT_SECONDS", 0.01):
                self.assertIsNone(pool.search("/tmp/example.zim", "query", 1))
            self.assertFalse(pool.is_broken("/tmp/example.zim"))
            self.assertIsNone(worker._process)
        finally:
            pool.close()

    def test_partial_worker_response_is_recycled(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys, time; sys.stdin.readline(); "
                    "sys.stdout.write('{'); sys.stdout.flush(); time.sleep(10)"
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        pool = FulltextPool(1)
        try:
            worker = pool._all[0]
            worker._process = process
            started = time.monotonic()
            with patch("zimantic.fulltext.FULLTEXT_TIMEOUT_SECONDS", 0.01):
                self.assertIsNone(pool.search("/tmp/example.zim", "query", 1))
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertIsNone(worker._process)
        finally:
            pool.close()

    def test_archive_error_payload_marks_archive_broken(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import json, sys; sys.stdin.readline(); "
                    "print(json.dumps({'error': 'bad archive'}), flush=True); "
                    "sys.stdin.read()"
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        pool = FulltextPool(1)
        try:
            pool._all[0]._process = process
            self.assertIsNone(pool.search("/tmp/example.zim", "query", 1))
            self.assertTrue(pool.is_broken("/tmp/example.zim"))
        finally:
            pool.close()
