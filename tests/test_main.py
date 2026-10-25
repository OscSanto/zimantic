import importlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


class LoggingTests(unittest.TestCase):
    def test_configures_faiss_to_log_only_warnings_and_errors(self):
        import zimantic.__main__ as main_module

        faiss_logger = main_module.logging.getLogger("faiss")
        previous_level = faiss_logger.level
        try:
            with patch.object(main_module.logging, "basicConfig") as basic_config:
                main_module._configure_logging(main_module.logging.INFO)

            basic_config.assert_called_once_with(
                level=main_module.logging.INFO,
                format="%(message)s",
            )
            self.assertEqual(faiss_logger.level, main_module.logging.WARNING)
        finally:
            faiss_logger.setLevel(previous_level)

    @patch("zimantic.__main__.sys.stderr")
    def test_serve_defaults_to_info_when_stderr_is_terminal(self, stderr):
        import zimantic.__main__ as main_module

        stderr.isatty.return_value = True

        self.assertEqual(main_module._get_log_level("serve", 0), main_module.logging.INFO)

    @patch("zimantic.__main__.sys.stderr")
    def test_serve_defaults_to_warning_when_stderr_is_not_terminal(self, stderr):
        import zimantic.__main__ as main_module

        stderr.isatty.return_value = False

        self.assertEqual(main_module._get_log_level("serve", 0), main_module.logging.WARNING)

    @patch("zimantic.__main__.sys.stderr")
    def test_explicit_verbosity_is_unchanged_for_serve(self, stderr):
        import zimantic.__main__ as main_module

        stderr.isatty.return_value = True

        self.assertEqual(main_module._get_log_level("serve", 1), main_module.logging.INFO)
        self.assertEqual(main_module._get_log_level("serve", 2), main_module.logging.DEBUG)


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
                main_module._wait_for_stable_sizes({path})

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


class BuildWorkerTests(unittest.TestCase):
    def test_collect_zims_prefers_the_largest_conflicting_stem(self):
        import zimantic.__main__ as main_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "one" / "manual.zim"
            second = root / "two" / "manual.zim"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"small")
            second.write_bytes(b"a much larger copy")

            zims = main_module._collect_zims([first, second], root)
            selected = main_module._select_zim_paths(
                zims,
                {path: path.stat().st_size for path in zims},
            )

            self.assertEqual([path.name for path in selected], ["manual.zim"])
            self.assertEqual(selected[0].read_bytes(), b"a much larger copy")

    def test_build_workers_process_independent_zims_concurrently(self):
        import zimantic.__main__ as main_module
        from zimantic import settings

        build_module = importlib.import_module("zimantic.build")
        barrier = threading.Barrier(2)
        built = []

        def fake_build(zim_path, *_args, **_kwargs):
            barrier.wait(timeout=2)
            built.append(zim_path.name)

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = directory / "first.zim"
            second = directory / "second.zim"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            config = {
                "zim_dir": str(directory),
                "index_dir": str(directory / "indexes"),
                "batch_size": 1,
                "build_workers": 1,
            }
            with (
                patch.object(main_module.sys, "argv", [
                    "zimantic",
                    "build",
                    "--fast",
                    "--workers",
                    "2",
                    str(first),
                    str(second),
                ]),
                patch.object(settings, "load_config", return_value=config),
                patch.object(main_module, "_configure_logging"),
                patch.object(main_module, "_find_downloading_sizes", return_value=set()),
                patch.object(build_module, "build", side_effect=fake_build),
            ):
                main_module.main()

        self.assertEqual(set(built), {"first.zim", "second.zim"})


if __name__ == "__main__":
    unittest.main()
