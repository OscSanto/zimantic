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
