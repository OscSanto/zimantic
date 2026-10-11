import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class StableSizeTests(unittest.TestCase):
    def test_waits_until_size_is_unchanged_regardless_of_file_size(self):
        import zimantic.__main__ as main_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.zim"
            path.write_bytes(b"x" * (5 * 1024 * 1024 + 1))

            sleep_calls = 0

            def advance_download(_seconds):
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 1:
                    path.write_bytes(b"x" * (5 * 1024 * 1024 + 2))

            with patch.object(
                main_module.time,
                "sleep",
                side_effect=advance_download,
            ) as sleep:
                main_module._wait_for_stable_size(path)

        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(main_module.STABLE_POLL_SECONDS)

    def test_initial_check_waits_once_and_identifies_changed_files(self):
        import zimantic.__main__ as main_module

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            stable = directory / "stable.zim"
            downloading = directory / "downloading.zim"
            stable.write_bytes(b"x")
            downloading.write_bytes(b"x")
            sizes = {stable: 1, downloading: 1}

            def advance_download(_seconds):
                downloading.write_bytes(b"xx")

            with patch.object(main_module.time, "sleep", side_effect=advance_download) as sleep:
                changed = main_module._find_downloading_sizes(sizes)

        self.assertEqual(changed, {downloading})
        self.assertEqual(sleep.call_count, 1)
        sleep.assert_called_with(main_module.STABLE_POLL_SECONDS)

    def test_changed_files_are_checked_together(self):
        import zimantic.__main__ as main_module

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = directory / "first.zim"
            second = directory / "second.zim"
            first.write_bytes(b"x")
            second.write_bytes(b"x")
            sleep_calls = 0

            def advance_download(_seconds):
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 1:
                    first.write_bytes(b"xx")
                    second.write_bytes(b"xx")

            with patch.object(
                main_module.time,
                "sleep",
                side_effect=advance_download,
            ) as sleep:
                main_module._wait_for_stable_sizes({first, second})

        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(main_module.STABLE_POLL_SECONDS)


if __name__ == "__main__":
    unittest.main()
