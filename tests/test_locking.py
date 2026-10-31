import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import zimantic.__main__ as main_module
from zimantic import locking


class BuildLockPathTests(unittest.TestCase):
    def test_lock_lives_beside_the_index_directory(self):
        self.assertEqual(
            locking.build_lock_path("/library/zimantic/indexes"),
            Path("/library/zimantic/.build.lock"),
        )


class BuildLockTests(unittest.TestCase):
    def test_second_build_is_busy_until_the_first_releases(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".build.lock"
            first = locking.BuildLock(path)
            second = locking.BuildLock(path)
            try:
                self.assertTrue(first.try_acquire())
                self.assertFalse(second.try_acquire())
                first.release()
                self.assertTrue(second.try_acquire())
            finally:
                first.release()
                second.release()

    def test_acquire_waits_for_the_running_build(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".build.lock"
            first = locking.BuildLock(path)
            second = locking.BuildLock(path)
            try:
                self.assertTrue(first.try_acquire())
                releaser = threading.Timer(0.05, first.release)
                releaser.start()
                self.assertTrue(second.acquire(timeout=5))
            finally:
                first.release()
                second.release()

    def test_acquire_times_out_while_the_other_build_runs(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".build.lock"
            first = locking.BuildLock(path)
            second = locking.BuildLock(path)
            try:
                self.assertTrue(first.try_acquire())
                started = time.monotonic()
                self.assertFalse(second.acquire(timeout=0.01))
                self.assertGreaterEqual(time.monotonic() - started, 0.2)
            finally:
                first.release()
                second.release()

    def test_unreadable_lock_explains_the_likely_owner(self):
        with TemporaryDirectory() as tmp:
            lock = locking.BuildLock(Path(tmp) / ".build.lock")
            with patch.object(
                locking.os,
                "open",
                side_effect=PermissionError(13, "Permission denied"),
            ):
                with self.assertRaises(locking.BuildLockError) as caught:
                    lock.try_acquire()
        message = str(caught.exception)
        self.assertIn("cannot open the build lock", message)
        self.assertIn("sudo", message)
        self.assertIn("writable", message)

    def test_release_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            lock = locking.BuildLock(Path(tmp) / ".build.lock")
            self.assertTrue(lock.try_acquire())
            lock.release()
            lock.release()


class BuildLockCliTests(unittest.TestCase):
    def test_build_accepts_no_wait_lock_and_lock_timeout(self):
        parser = main_module._make_parser()
        args = parser.parse_args(["build", "--no-wait-lock", "--lock-timeout", "5"])
        self.assertTrue(args.no_wait_lock)
        self.assertEqual(args.lock_timeout, 5.0)

    def test_build_waits_by_default(self):
        parser = main_module._make_parser()
        args = parser.parse_args(["build"])
        self.assertFalse(args.no_wait_lock)
        self.assertIsNone(args.lock_timeout)


if __name__ == "__main__":
    unittest.main()
