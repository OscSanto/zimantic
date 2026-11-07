import argparse
import importlib
import tempfile
import threading
import types
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


class DeviceArgumentTests(unittest.TestCase):
    def test_build_and_serve_accept_device(self):
        import zimantic.__main__ as main_module

        parser = main_module._make_parser()
        for command in ("build", "serve"):
            with self.subTest(command=command):
                self.assertEqual(
                    parser.parse_args([command, "--device", " CUDA "]).device,
                    "cuda",
                )
                self.assertIsNone(parser.parse_args([command]).device)

    def test_unknown_device_is_rejected(self):
        import zimantic.__main__ as main_module

        parser = main_module._make_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["build", "--device", "tensor-core"])


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

    def test_collect_zims_rejects_explicit_non_zim_files(self):
        import zimantic.__main__ as main_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("not a ZIM", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "not a ZIM file"):
                main_module._collect_zims([path], Path(tmp))

    def test_build_stability_wait_is_opt_in(self):
        import zimantic.__main__ as main_module
        from zimantic import settings

        build_module = importlib.import_module("zimantic.build")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "manual.zim"
            source.write_bytes(b"ZIM\x04")
            config = {
                "zim_dir": str(directory),
                "index_dir": str(directory / "indexes"),
                "batch_size": 1,
                "build_workers": 1,
            }
            for extra, expected in (([], 0), (["--wait-stable"], 1)):
                with self.subTest(extra=extra):
                    with (
                        patch.object(
                            main_module.sys,
                            "argv",
                            ["zimantic", "build", "--fast", *extra, str(source)],
                        ),
                        patch.object(settings, "load_config", return_value=config),
                        patch.object(main_module, "_configure_logging"),
                        patch.object(
                            main_module,
                            "_find_downloading_sizes",
                            return_value=set(),
                        ) as find_sizes,
                        patch.object(
                            build_module, "build_index"
                        ),
                        patch.object(
                            main_module,
                            "_build_producer_for_zim",
                            return_value=object(),
                        ),
                    ):
                        main_module.main()
                    self.assertEqual(find_sizes.call_count, expected)

    def test_build_workers_process_independent_zims_concurrently(self):
        import zimantic.__main__ as main_module
        from zimantic import settings

        build_module = importlib.import_module("zimantic.build")
        barrier = threading.Barrier(2)
        built = []

        def fake_build(producer, *_args, **_kwargs):
            barrier.wait(timeout=2)
            built.append(Path(producer.path).name)

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
                patch.object(build_module, "build_index", side_effect=fake_build),
                patch.object(
                    main_module,
                    "_build_producer_for_zim",
                    side_effect=lambda zim, cfg, fast: types.SimpleNamespace(
                        path=str(zim)
                    ),
                ),
            ):
                main_module.main()

        self.assertEqual(set(built), {"first.zim", "second.zim"})

    def test_lazy_embedder_does_not_load_until_used(self):
        import zimantic.__main__ as main_module

        class _Embedder:
            @staticmethod
            def truncate(text, **_kwargs):
                return text

            @staticmethod
            def embed(texts):
                return texts

        calls = []
        lazy = main_module._LazyEmbedder(lambda: calls.append(1) or _Embedder())
        self.assertEqual(calls, [])
        self.assertEqual(lazy.truncate("text"), "text")
        self.assertEqual(calls, [1])
        self.assertEqual(lazy.embed(["a"]), ["a"])
        self.assertEqual(calls, [1])

    def test_one_source_uses_all_configured_embedding_workers(self):
        import zimantic.__main__ as main_module

        build_module = importlib.import_module("zimantic.build")
        runtime = types.SimpleNamespace(entry_producer=lambda *_args, **_kwargs: object())
        config = {
            "index_dir": "/unused/indexes",
            "model_dir": "/unused/model",
            "embedding_workers": 3,
            "embedding_batch_tokens": 1000,
        }
        args = types.SimpleNamespace(workers=1, fast=False, force=False)
        with (
            patch.object(main_module, "_build_config", return_value=config.copy()),
            patch.object(build_module, "build_index") as build,
            patch.object(build_module, "EmbeddingPipeline") as pipeline,
        ):
            main_module._build_tasks(
                [("one", runtime, object())], config, args, argparse.ArgumentParser()
            )
        self.assertEqual(pipeline.call_args.kwargs["workers"], 3)
        self.assertEqual(pipeline.call_args.kwargs["token_budget"], 1000)
        self.assertIs(
            build.call_args.args[3]["embedding_pipeline"], pipeline.return_value
        )
        pipeline.return_value.close.assert_called_once()


class ModelCommandTests(unittest.TestCase):
    def test_accepts_multiple_precisions_and_model_options(self):
        import zimantic.__main__ as main_module

        parser = main_module._make_parser()
        args = parser.parse_args(
            ["model", "fp32", "int8", "--model", "e5-small-v2", "--dir", "models"]
        )

        self.assertEqual(args.precision, ["fp32", "int8"])
        self.assertEqual(args.model, "e5-small-v2")
        self.assertEqual(args.dir, Path("models"))
        self.assertFalse(args.force)

    def test_unknown_precision_is_rejected(self):
        import zimantic.__main__ as main_module

        parser = main_module._make_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["model", "int4"])

    def test_list_does_not_load_config(self):
        import contextlib
        import io

        import zimantic.__main__ as main_module
        from zimantic import settings

        output = io.StringIO()
        with (
            patch.object(main_module.sys, "argv", ["zimantic", "model", "--list"]),
            patch.object(
                settings,
                "load_config",
                side_effect=AssertionError("config loaded for --list"),
            ),
            contextlib.redirect_stdout(output),
        ):
            main_module.main()

        self.assertIn("multilingual-e5-small (default): fp32, fp16, int8", output.getvalue())
        self.assertIn("default precision: int8 (CPU)", output.getvalue())

    def test_omitted_precisions_download_int8_only(self):
        import zimantic.__main__ as main_module
        from zimantic import model_files, settings

        seen = []

        def fake_download(model, precisions, output_dir, force=False):
            seen.append((model, precisions, output_dir, force))
            return [(precision, output_dir / precision) for precision in precisions]

        with tempfile.TemporaryDirectory() as tmp:
            config = {"model_dir": str(Path(tmp) / "model")}
            with (
                patch.object(
                    main_module.sys, "argv", ["zimantic", "model"]
                ),
                patch.object(settings, "load_config", return_value=config),
                patch.object(main_module, "_configure_logging"),
                patch.object(model_files, "download_model", side_effect=fake_download),
            ):
                main_module.main()

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "multilingual-e5-small")
        self.assertEqual(seen[0][1], ["int8"])
        self.assertEqual(seen[0][2], Path(config["model_dir"]))


if __name__ == "__main__":
    unittest.main()
