import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from zimantic.fulltext import FulltextPool


class FulltextTimeoutTests(unittest.TestCase):
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
