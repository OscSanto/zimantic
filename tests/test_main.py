import argparse
import importlib
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from zimantic.indexing.pipeline import BuildReport


def _build_report(status="built"):
    return BuildReport("zim", "zim:library", "manual", status, 1, 0, False, 0, 0.0)


class LoggingTests(unittest.TestCase):
    def test_build_report_requires_the_report_contract(self):
        from zimantic.__main__ import _log_build_report

        with self.assertRaises(AttributeError):
            _log_build_report(None)

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

        build_module = importlib.import_module("zimantic.indexing.pipeline")
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
                            build_module, "build_index", return_value=_build_report()
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

        build_module = importlib.import_module("zimantic.indexing.pipeline")
        barrier = threading.Barrier(2)
        built = []
        stage_split = []

        def fake_build(producer, _locator, _embedder, cfg, **_kwargs):
            barrier.wait(timeout=2)
            cfg["progress_callback"](5, 10)
            built.append(Path(producer.path).name)
            stage_split.append(cfg["_stage_split"])
            return _build_report()

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
                patch.object(build_module, "_AllFilesProgress") as progress,
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
        self.assertEqual(stage_split, [False, False])
        self.assertEqual(
            {call.args for call in progress.return_value.advance.call_args_list},
            {(0, 5, 10), (1, 5, 10)},
        )
        self.assertEqual(
            {call.args for call in progress.return_value.finish.call_args_list},
            {(0,), (1,)},
        )
        progress.return_value.close.assert_called_once()

    def test_all_files_progress_finishes_skips_and_failures_but_not_interrupts(self):
        import zimantic.__main__ as main_module

        build_module = importlib.import_module("zimantic.indexing.pipeline")
        cfg = {"index_dir": "/unused/indexes"}
        args = types.SimpleNamespace(workers=1, fast=True, force=False)
        for failure in (RuntimeError("failed"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                runtime = types.SimpleNamespace(
                    entry_producer=lambda *_args, **_kwargs: object()
                )
                report = _build_report(status="skipped")
                with (
                    patch.object(main_module, "_build_config", return_value=cfg.copy()),
                    patch.object(main_module, "_log_build_failure"),
                    patch.object(build_module, "build_index", side_effect=[report, failure]),
                    patch.object(build_module, "_AllFilesProgress") as progress,
                    self.assertRaises(
                        KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else SystemExit
                    ),
                ):
                    main_module._build_tasks(
                        [("one", runtime, object()), ("two", runtime, object())],
                        cfg, args, argparse.ArgumentParser(),
                    )
                finished = [call.args[0] for call in progress.return_value.finish.call_args_list]
                self.assertEqual(
                    finished, [0] if isinstance(failure, KeyboardInterrupt) else [0, 1]
                )
                progress.return_value.close.assert_called_once()

    def test_build_interrupt_exits_with_standard_status(self):
        import zimantic.__main__ as main_module
        from zimantic import settings

        with (
            patch.object(main_module, "_configure_logging"),
            patch.object(settings, "load_config", return_value={}),
            patch.object(
                main_module, "_run_build_locked", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(SystemExit) as raised,
        ):
            main_module.main(["build", "--fast"])

        self.assertEqual(raised.exception.code, 130)

    def test_build_interrupt_signals_workers_before_closing_embedding_pipeline(self):
        import zimantic.__main__ as main_module

        build_module = importlib.import_module("zimantic.indexing.pipeline")
        runtime = types.SimpleNamespace(entry_producer=lambda *_args, **_kwargs: object())
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / name for name in ("first.zim", "second.zim")]
            for path in paths:
                path.write_bytes(b"ZIM\x04")
            cfg = {
                "index_dir": str(Path(tmp) / "indexes"),
                "model_dir": "/unused/model",
                "onnx_build_workers": 1,
            }
            for positional in (False, True):
                for workers in (1, 2):
                    with self.subTest(positional=positional, workers=workers):
                        stop_event = threading.Event()
                        started = threading.Event()
                        args = types.SimpleNamespace(
                            workers=workers, fast=False, force=False,
                            paths=paths, list=False, wait_stable=False,
                        )

                        def fake_build(
                            _producer, _locator, _embedder, config,
                            stop_event=stop_event, started=started, workers=workers,
                            **_kwargs,
                        ):
                            self.assertIs(config["interrupt_event"], stop_event)
                            started.set()
                            if workers == 1:
                                raise KeyboardInterrupt
                            return _build_report()

                        def interrupt(_futures, started=started):
                            self.assertTrue(started.wait(timeout=2))
                            raise KeyboardInterrupt

                        def close(*, wait, stop_event=stop_event):
                            self.assertFalse(wait)
                            self.assertTrue(stop_event.is_set())

                        with (
                            patch.object(main_module, "_BUILD_STOP_EVENT", stop_event),
                            patch.object(main_module, "_build_producer_for_zim"),
                            patch.object(main_module, "as_completed", side_effect=interrupt),
                            patch.object(main_module, "_embedding_queue_size", return_value=2),
                            patch.object(build_module, "EmbeddingPipeline") as pipeline,
                            patch.object(build_module, "build_index", side_effect=fake_build),
                            self.assertRaises(KeyboardInterrupt),
                        ):
                            pipeline.return_value.close.side_effect = close
                            if positional:
                                main_module._run_positional_zim_build(
                                    cfg, args, argparse.ArgumentParser()
                                )
                            else:
                                main_module._build_tasks(
                                    [("one", runtime, object()), ("two", runtime, object())],
                                    cfg, args, argparse.ArgumentParser(),
                                )
                        self.assertTrue(stop_event.is_set())

    def test_lazy_embedder_does_not_load_until_used(self):
        import zimantic.__main__ as main_module

        class _Embedder:
            query_prefix = "query: "
            passage_prefix = "passage: "

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
        self.assertEqual(lazy.query_prefix, "query: ")
        self.assertEqual(lazy.passage_prefix, "passage: ")
        self.assertEqual(calls, [1])

    def test_one_source_uses_all_configured_embedding_workers(self):
        import zimantic.__main__ as main_module

        build_module = importlib.import_module("zimantic.indexing.pipeline")
        runtime = types.SimpleNamespace(entry_producer=lambda *_args, **_kwargs: object())
        config = {
            "index_dir": "/unused/indexes",
            "model_dir": "/unused/model",
            "onnx_build_workers": 3,
            "embedding_batch_tokens": 1000,
            "onnx_query_threads": 8,
            "onnx_build_threads": 2,
        }
        args = types.SimpleNamespace(workers=1, fast=False, force=False)
        with (
            patch.object(main_module, "_build_config", return_value=config.copy()),
            patch.object(build_module, "build_index", return_value=_build_report()) as build,
            patch.object(build_module, "EmbeddingPipeline") as pipeline,
            patch("zimantic.embed.Embedder") as embedder,
        ):
            main_module._build_tasks(
                [("one", runtime, object())], config, args, argparse.ArgumentParser()
            )
            embedder.assert_not_called()
            pipeline.call_args.args[0].embed(["text"])
            embedder.assert_called_once_with(
                config["model_dir"],
                threads=2,
                device=None,
                batch_tokens=1000,
                onnx_warnings=False,
            )
        self.assertEqual(pipeline.call_args.kwargs["workers"], 3)
        self.assertEqual(pipeline.call_args.kwargs["queue_size"], 32)
        self.assertEqual(pipeline.call_args.kwargs["token_budget"], 1000)
        self.assertIs(
            build.call_args.args[3]["embedding_pipeline"], pipeline.return_value
        )
        self.assertTrue(build.call_args.args[3]["_stage_split"])
        pipeline.return_value.close.assert_called_once()

    def test_cpu_embedding_queue_fits_a_read_window_without_changing_gpu_bounds(self):
        import zimantic.__main__ as main_module

        cases = [
            ("CPUExecutionProvider", 32, 8, 32),
            ("CPUExecutionProvider", 256, 2, 256),
            ("CPUExecutionProvider", 4, 8, 16),
            ("MIGraphXExecutionProvider", 256, 4, 8),
        ]
        for provider, window, workers, expected in cases:
            with (
                self.subTest(provider=provider, window=window, workers=workers),
                patch("zimantic.embed.resolve_providers", return_value=([provider], None)),
            ):
                self.assertEqual(
                    main_module._embedding_queue_size({"batch_size": window}, workers),
                    expected,
                )

    def test_legacy_build_forwards_the_cpu_batch_budget(self):
        import zimantic.__main__ as main_module

        build_module = importlib.import_module("zimantic.indexing.pipeline")
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / name for name in ("first.zim", "second.zim")]
            for path in paths:
                path.write_bytes(b"ZIM\x04")
            config = {
                "zim_dir": tmp,
                "index_dir": str(Path(tmp) / "indexes"),
                "model_dir": "/unused/model",
                "batch_size": 32,
                "embedding_batch_tokens": 256,
                "onnx_build_workers": 8,
                "embed_device": "cpu",
            }
            parser = main_module._make_parser()
            args = parser.parse_args(["build", "--workers", "2", *map(str, paths)])
            with (
                patch.object(build_module, "EmbeddingPipeline") as pipeline,
                patch.object(build_module, "build_index", return_value=_build_report()),
                patch.object(main_module, "_build_producer_for_zim", return_value=object()),
                patch.object(build_module, "_AllFilesProgress"),
                patch("zimantic.embed.Embedder") as embedder,
            ):
                main_module._run_build(args, config, parser)
        embedder.assert_not_called()
        self.assertEqual(pipeline.call_args.kwargs["token_budget"], 256)
        self.assertEqual(pipeline.call_args.kwargs["queue_size"], 32)


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

        self.assertIn(
            "static-similarity-mrl-multilingual-v1 (default): fp32, fp16, int8",
            output.getvalue(),
        )
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
        self.assertEqual(seen[0][0], "static-similarity-mrl-multilingual-v1")
        self.assertEqual(seen[0][1], ["int8"])
        self.assertEqual(seen[0][2], Path(config["model_dir"]))


if __name__ == "__main__":
    unittest.main()
