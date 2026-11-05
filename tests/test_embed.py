import json
import os
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from zimantic import embed


def _sentencepiece_dir(directory):
    """The Embedder needs a tokenizer artifact to select the XLM-R path."""
    (Path(directory) / "sentencepiece.bpe.model").write_bytes(b"")
    return directory


class ModelChecksumTests(unittest.TestCase):
    def test_mismatch_warns_but_does_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "model.onnx").write_bytes(b"not the documented model")
            warnings = []
            embed.verify_model_files(tmp, warn=warnings.append)
            self.assertTrue(warnings)
            self.assertIn("model.onnx", warnings[0])

    def test_missing_files_do_not_warn(self):
        # A missing model fails later with a clearer error from onnxruntime.
        with tempfile.TemporaryDirectory() as tmp:
            warnings = []
            embed.verify_model_files(tmp, warn=warnings.append)
            self.assertEqual(warnings, [])


class ModelFingerprintTests(unittest.TestCase):
    def test_fingerprint_changes_with_model_or_tokenizer_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.onnx"
            tokenizer = Path(tmp) / "sentencepiece.bpe.model"
            model.write_bytes(b"model-v1")
            tokenizer.write_bytes(b"vocab-v1")
            first = embed.model_fingerprint(tmp)

            tokenizer.write_bytes(b"vocab-v2")
            second = embed.model_fingerprint(tmp)
            model.write_bytes(b"model-v2")
            third = embed.model_fingerprint(tmp)

        self.assertTrue(first)
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)

    def test_fingerprint_changes_with_external_onnx_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.onnx"
            data = Path(tmp) / "model.onnx_data"
            tokenizer = Path(tmp) / "sentencepiece.bpe.model"
            model.write_bytes(b"model")
            data.write_bytes(b"weights-v1")
            tokenizer.write_bytes(b"vocab")
            first = embed.model_fingerprint(tmp)

            data.write_bytes(b"weights-v2")
            second = embed.model_fingerprint(tmp)

        self.assertNotEqual(first, second)

    def test_missing_model_has_no_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            _sentencepiece_dir(tmp)
            self.assertEqual(embed.model_fingerprint(tmp), "")

    def test_verify_and_fingerprint_hash_unchanged_files_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "model.onnx").write_bytes(b"model-bytes")
            (Path(tmp) / "sentencepiece.bpe.model").write_bytes(b"vocab-bytes")
            with patch.object(embed, "_sha256", wraps=embed._sha256) as sha256:
                embed.verify_model_files(tmp, warn=lambda _msg: None)
                first = embed.model_fingerprint(tmp)
                second = embed.model_fingerprint(tmp)
            self.assertTrue(first)
            self.assertEqual(first, second)
            self.assertEqual(sha256.call_count, 2)


class ModelProbeTests(unittest.TestCase):
    def test_serialize_round_trip(self):
        texts = ["first probe", "second probe"]
        vectors = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

        meta = embed.serialize_model_probes(texts, vectors)
        loaded = embed.load_model_probes(meta)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[0], texts)
        np.testing.assert_allclose(loaded[1], vectors)

    def test_missing_or_corrupt_probes_are_ignored(self):
        self.assertIsNone(embed.load_model_probes({}))
        meta = embed.serialize_model_probes(["one"], np.ones((1, 2), dtype=np.float32))
        meta["model_probe_shape"] = "2x2"
        self.assertIsNone(embed.load_model_probes(meta))

    def test_match_accepts_close_models_and_rejects_distant_ones(self):
        stored = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

        class _Embedder:
            def __init__(self, vectors):
                self.vectors = vectors

            def embed(self, _texts):
                return self.vectors

        close = np.array([[1.0, 0.01], [0.01, 1.0]], dtype=np.float32)
        compatible, mean, minimum = embed.model_probes_match(
            ["one", "two"], stored, _Embedder(close)
        )
        self.assertTrue(compatible)
        self.assertGreater(mean, 0.99)
        self.assertGreater(minimum, 0.98)

        distant = np.array([[1.0, 0.0], [-1.0, 0.0]], dtype=np.float32)
        compatible, _mean, _minimum = embed.model_probes_match(
            ["one", "two"], stored, _Embedder(distant)
        )
        self.assertFalse(compatible)

    def test_match_rejects_a_different_dimension(self):
        class _Embedder:
            def embed(self, _texts):
                return np.ones((2, 3), dtype=np.float32)

        compatible, _mean, _minimum = embed.model_probes_match(
            ["one", "two"], np.ones((2, 2), dtype=np.float32), _Embedder()
        )
        self.assertFalse(compatible)


class ThreadConfigTests(unittest.TestCase):
    def test_default_thread_budget_leaves_at_least_one_thread(self):
        self.assertEqual(embed.DEFAULT_EMBED_THREADS, max(1, (os.cpu_count() or 1) - 1))


class LowMemoryArenaTests(unittest.TestCase):
    """The CPU arena is disabled (and heap reclaimed) only on small hosts."""

    def _build(self, memory_gb, low_memory=None, tokenizer=None, session=None):
        captured = {}

        def capture_load(path, sess_opts, providers=None):
            captured["arena"] = sess_opts.enable_cpu_mem_arena
            return session if session is not None else MagicMock()

        with (
            patch.object(embed.ort, "InferenceSession", side_effect=capture_load),
            patch.object(
                embed.sentencepiece,
                "SentencePieceProcessor",
                return_value=tokenizer or MagicMock(),
            ),
            patch.object(embed, "_system_memory_gb", return_value=memory_gb),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                _sentencepiece_dir(tmp)
                embed.Embedder(tmp, low_memory=low_memory)
        return captured["arena"]

    def test_low_ram_host_disables_cpu_arena(self):
        self.assertFalse(self._build(memory_gb=0.5))  # Pi Zero 2 W class

    def test_low_ram_boundary_disables_cpu_arena(self):
        self.assertFalse(self._build(memory_gb=embed.LOW_MEMORY_GB))

    def test_normal_host_keeps_cpu_arena(self):
        self.assertTrue(self._build(memory_gb=8.0))

    def test_unknown_memory_keeps_cpu_arena(self):
        self.assertTrue(self._build(memory_gb=0.0))

    def test_explicit_low_memory_override(self):
        self.assertFalse(self._build(memory_gb=8.0, low_memory=True))
        self.assertTrue(self._build(memory_gb=0.5, low_memory=False))

    def test_reclaim_happens_only_for_small_hosts_and_batches(self):
        tokenizer = MagicMock()
        tokenizer.encode.return_value = [1, 2]
        session = MagicMock()
        session.run.return_value = [np.ones((2, 4, 4), dtype=np.float32)]

        with (
            patch.object(embed.ort, "InferenceSession", return_value=session),
            patch.object(
                embed.sentencepiece, "SentencePieceProcessor", return_value=tokenizer
            ),tempfile.TemporaryDirectory() as tmp
        ):
            _sentencepiece_dir(tmp)
            small = embed.Embedder(tmp, low_memory=True)
            normal = embed.Embedder(tmp, low_memory=False)

        with patch.object(embed, "_reclaim_native_memory") as reclaim:
            small.embed(["passage: a", "passage: b"])
            reclaim.assert_called_once()
        with patch.object(embed, "_reclaim_native_memory") as reclaim:
            small.embed(["query: one"])
            reclaim.assert_not_called()
        with patch.object(embed, "_reclaim_native_memory") as reclaim:
            normal.embed(["passage: a", "passage: b"])
            reclaim.assert_not_called()


class DeviceSelectionTests(unittest.TestCase):
    """embed_device maps to ONNX Runtime providers with a CPU fallback."""

    def test_auto_tries_every_available_gpu_provider_in_priority_order(self):
        providers, warning = embed.resolve_providers(
            "auto",
            ["CPUExecutionProvider", "ROCMExecutionProvider", "CUDAExecutionProvider"],
        )
        self.assertEqual(
            providers,
            [
                "CUDAExecutionProvider",
                "ROCMExecutionProvider",
                "CPUExecutionProvider",
            ],
        )
        self.assertIsNone(warning)

    def test_auto_does_not_select_coreml(self):
        providers, warning = embed.resolve_providers(
            "auto", ["CPUExecutionProvider", "CoreMLExecutionProvider"]
        )
        self.assertEqual(providers, ["CPUExecutionProvider"])
        self.assertIsNone(warning)

    def test_explicit_coreml_is_still_available(self):
        providers, warning = embed.resolve_providers(
            "coreml", ["CPUExecutionProvider", "CoreMLExecutionProvider"]
        )
        self.assertEqual(
            providers, ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        )
        self.assertIsNone(warning)

    def test_every_documented_alias_resolves(self):
        from zimantic import settings

        self.assertEqual(
            set(settings.EMBED_DEVICES),
            (embed.AUTO_DEVICES - {""}) | set(embed.DEVICE_PROVIDERS),
        )
        available = [
            "CPUExecutionProvider",
            "CUDAExecutionProvider",
            "ROCMExecutionProvider",
            "DmlExecutionProvider",
            "OpenVINOExecutionProvider",
            "CoreMLExecutionProvider",
        ]
        for device in settings.EMBED_DEVICES:
            with self.subTest(device=device):
                providers, warning = embed.resolve_providers(device, available)
                self.assertIsNone(warning)

    def test_auto_falls_back_to_cpu_quietly(self):
        providers, warning = embed.resolve_providers(
            "auto", ["CPUExecutionProvider"]
        )
        self.assertEqual(providers, ["CPUExecutionProvider"])
        self.assertIsNone(warning)

    def test_gpu_without_a_gpu_provider_warns_and_uses_cpu(self):
        providers, warning = embed.resolve_providers("gpu", ["CPUExecutionProvider"])
        self.assertEqual(providers, ["CPUExecutionProvider"])
        self.assertIn("no accelerated", warning)

    def test_explicit_provider_keeps_the_cpu_fallback(self):
        providers, warning = embed.resolve_providers(
            "cuda", ["CPUExecutionProvider", "CUDAExecutionProvider"]
        )
        self.assertEqual(
            providers, ["CUDAExecutionProvider", "CPUExecutionProvider"]
        )
        self.assertIsNone(warning)

    def test_unavailable_provider_warns_and_uses_cpu(self):
        providers, warning = embed.resolve_providers(
            "cuda", ["CPUExecutionProvider"]
        )
        self.assertEqual(providers, ["CPUExecutionProvider"])
        self.assertIn("not available", warning)
        self.assertIn("onnxruntime-gpu", warning)

    def test_unknown_device_warns_and_uses_cpu(self):
        providers, warning = embed.resolve_providers(
            "tensor-core", ["CPUExecutionProvider"]
        )
        self.assertEqual(providers, ["CPUExecutionProvider"])
        self.assertIn("unknown embedding device", warning)

    def test_cpu_is_forced(self):
        providers, warning = embed.resolve_providers(
            "cpu", ["CPUExecutionProvider", "CUDAExecutionProvider"]
        )
        self.assertEqual(providers, ["CPUExecutionProvider"])
        self.assertIsNone(warning)

    def test_openvino_requests_automatic_device_selection(self):
        self.assertEqual(
            embed.provider_entries(
                ["OpenVINOExecutionProvider", "CPUExecutionProvider"]
            ),
            [
                ("OpenVINOExecutionProvider", {"device_type": "AUTO"}),
                "CPUExecutionProvider",
            ],
        )


class SessionProviderTests(unittest.TestCase):
    def test_requests_the_resolved_providers(self):
        captured = {}

        def capture_load(path, sess_opts, providers=None):
            captured["providers"] = providers
            return MagicMock()

        with (
            patch.object(embed.ort, "get_available_providers", return_value=[
                "CPUExecutionProvider",
                "CUDAExecutionProvider",
            ]),
            patch.object(embed.ort, "InferenceSession", side_effect=capture_load),
            patch.object(
                embed.sentencepiece, "SentencePieceProcessor", return_value=MagicMock()
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            _sentencepiece_dir(tmp)
            embedder = embed.Embedder(tmp)
        self.assertEqual(
            captured["providers"], ["CUDAExecutionProvider", "CPUExecutionProvider"]
        )
        self.assertEqual(embedder.provider, "CUDAExecutionProvider")

    def test_directml_disables_memory_pattern_and_parallel_execution(self):
        captured = {}

        def capture_load(path, sess_opts, providers=None):
            captured["providers"] = providers
            captured["mem_pattern"] = sess_opts.enable_mem_pattern
            captured["execution_mode"] = sess_opts.execution_mode
            session = MagicMock()
            session.get_providers.return_value = [
                "DmlExecutionProvider",
                "CPUExecutionProvider",
            ]
            return session

        with (
            patch.object(embed.ort, "get_available_providers", return_value=[
                "CPUExecutionProvider",
                "DmlExecutionProvider",
            ]),
            patch.object(embed.ort, "InferenceSession", side_effect=capture_load),
            patch.object(
                embed.sentencepiece, "SentencePieceProcessor", return_value=MagicMock()
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            _sentencepiece_dir(tmp)
            embedder = embed.Embedder(tmp, device="directml")
        self.assertEqual(
            captured["providers"],
            ["DmlExecutionProvider", "CPUExecutionProvider"],
        )
        self.assertFalse(captured["mem_pattern"])
        self.assertEqual(
            captured["execution_mode"], embed.ort.ExecutionMode.ORT_SEQUENTIAL
        )
        self.assertIsNotNone(embedder._run_lock)

    def test_runtime_failure_falls_back_to_cpu(self):
        calls = []

        class FailingSession:
            def get_inputs(self):
                return [types.SimpleNamespace(name="input_ids")]

            def get_providers(self):
                return ["CUDAExecutionProvider", "CPUExecutionProvider"]

            def run(self, _outputs, _inputs):
                raise RuntimeError("CUDA out of memory")

        class CpuSession:
            def get_inputs(self):
                return [types.SimpleNamespace(name="input_ids")]

            def get_providers(self):
                return ["CPUExecutionProvider"]

            def run(self, _outputs, _inputs):
                return [np.ones((1, 4, 4), dtype=np.float32)]

        def load(path, sess_opts, providers=None):
            calls.append(providers)
            return FailingSession() if len(calls) == 1 else CpuSession()

        tokenizer = MagicMock()
        tokenizer.encode.return_value = [1, 2]
        with (
            patch.object(embed.ort, "get_available_providers", return_value=[
                "CPUExecutionProvider",
                "CUDAExecutionProvider",
            ]),
            patch.object(embed.ort, "InferenceSession", side_effect=load),
            patch.object(
                embed.sentencepiece,
                "SentencePieceProcessor",
                return_value=tokenizer,
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            _sentencepiece_dir(tmp)
            embedder = embed.Embedder(tmp)
            with self.assertLogs(embed.logger, level="WARNING"):
                result = embedder.embed(["query: test"])
        self.assertEqual(result.shape, (1, 4))
        self.assertEqual(calls[-1], ["CPUExecutionProvider"])
        self.assertEqual(embedder.provider, "CPUExecutionProvider")
        self.assertEqual(embedder.providers, ["CPUExecutionProvider"])

    def test_cpu_runtime_errors_are_not_swallowed(self):
        class FailingSession:
            def get_inputs(self):
                return [types.SimpleNamespace(name="input_ids")]

            def get_providers(self):
                return ["CPUExecutionProvider"]

            def run(self, _outputs, _inputs):
                raise RuntimeError("bad input shape")

        tokenizer = MagicMock()
        tokenizer.encode.return_value = [1, 2]
        with (
            patch.object(embed.ort, "get_available_providers", return_value=[
                "CPUExecutionProvider",
            ]),
            patch.object(embed.ort, "InferenceSession", return_value=FailingSession()),
            patch.object(
                embed.sentencepiece,
                "SentencePieceProcessor",
                return_value=tokenizer,
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            _sentencepiece_dir(tmp)
            embedder = embed.Embedder(tmp)
            with self.assertRaisesRegex(RuntimeError, "bad input shape"):
                embedder.embed(["query: test"])

    def test_failed_gpu_session_falls_back_to_cpu(self):
        captured = []

        def load(path, sess_opts, providers=None):
            captured.append(providers)
            if len(captured) == 1:
                raise RuntimeError("CUDA driver missing")
            return MagicMock()

        with (
            patch.object(embed.ort, "get_available_providers", return_value=[
                "CPUExecutionProvider",
                "CUDAExecutionProvider",
            ]),
            patch.object(embed.ort, "InferenceSession", side_effect=load),
            patch.object(
                embed.sentencepiece, "SentencePieceProcessor", return_value=MagicMock()
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            _sentencepiece_dir(tmp)
            with self.assertLogs(embed.logger, level="WARNING"):
                embedder = embed.Embedder(tmp)
        self.assertEqual(captured[-1], ["CPUExecutionProvider"])
        self.assertEqual(embedder.provider, "CPUExecutionProvider")

    def test_cpu_session_errors_are_not_swallowed(self):
        with (
            patch.object(embed.ort, "get_available_providers", return_value=[
                "CPUExecutionProvider",
            ]),
            patch.object(
                embed.ort,
                "InferenceSession",
                side_effect=RuntimeError("bad model"),
            ),
            patch.object(
                embed.sentencepiece, "SentencePieceProcessor", return_value=MagicMock()
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            _sentencepiece_dir(tmp)
            with self.assertRaisesRegex(RuntimeError, "bad model"):
                embed.Embedder(tmp)

    def test_internal_provider_fallback_is_reported(self):
        session = MagicMock()
        session.get_providers.return_value = ["CPUExecutionProvider"]
        with (
            patch.object(embed.ort, "get_available_providers", return_value=[
                "CPUExecutionProvider",
                "CUDAExecutionProvider",
            ]),
            patch.object(embed.ort, "InferenceSession", return_value=session),
            patch.object(
                embed.sentencepiece, "SentencePieceProcessor", return_value=MagicMock()
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            _sentencepiece_dir(tmp)
            with self.assertLogs(embed.logger, level="WARNING") as logs:
                embedder = embed.Embedder(tmp)
        self.assertEqual(embedder.provider, "CPUExecutionProvider")
        self.assertEqual(embedder.providers, ["CPUExecutionProvider"])
        self.assertIn("selected CPUExecutionProvider instead of CUDAExecutionProvider", logs.output[0])


class DirectMLConcurrencyTests(unittest.TestCase):
    def test_directml_serializes_session_runs(self):
        state_lock = threading.Lock()
        state = {"active": 0, "max_active": 0}
        tokenizer = MagicMock()
        tokenizer.encode.return_value = [1, 2]

        class Session:
            def get_inputs(self):
                return [types.SimpleNamespace(name="input_ids")]

            def get_providers(self):
                return ["DmlExecutionProvider", "CPUExecutionProvider"]

            def run(self, _outputs, _inputs):
                with state_lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                time.sleep(0.02)
                with state_lock:
                    state["active"] -= 1
                return [np.ones((1, 4, 4), dtype=np.float32)]

        with (
            patch.object(embed.ort, "get_available_providers", return_value=[
                "CPUExecutionProvider",
                "DmlExecutionProvider",
            ]),
            patch.object(embed.ort, "InferenceSession", return_value=Session()),
            patch.object(
                embed.sentencepiece,
                "SentencePieceProcessor",
                return_value=tokenizer,
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            _sentencepiece_dir(tmp)
            embedder = embed.Embedder(tmp)

        errors = []

        def run(text):
            try:
                embedder.embed([text])
            except BaseException as error:  # noqa: BLE001 - re-raised below
                errors.append(error)

        threads = [
            threading.Thread(target=run, args=(f"query: {index}",))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(state["max_active"], 1)


class TokenizerSelectionTests(unittest.TestCase):
    def test_sentencepiece_artifact_selects_xlm_roberta(self):
        with tempfile.TemporaryDirectory() as tmp:
            _sentencepiece_dir(tmp)
            kind, path = embed._resolve_tokenizer(tmp)
        self.assertEqual(kind, "sentencepiece")
        self.assertEqual(path.name, "sentencepiece.bpe.model")

    def test_vocab_artifact_selects_wordpiece(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "vocab.txt").write_text("[PAD]\n", encoding="utf-8")
            kind, path = embed._resolve_tokenizer(tmp)
        self.assertEqual(kind, "wordpiece")
        self.assertEqual(path.name, "vocab.txt")

    def test_tokenizer_config_disambiguates_when_both_exist(self):
        cases = (
            ("BertTokenizer", "wordpiece"),
            ("XLMRobertaTokenizer", "sentencepiece"),
        )
        for tokenizer_class, expected in cases:
            with self.subTest(tokenizer_class=tokenizer_class):
                with tempfile.TemporaryDirectory() as tmp:
                    _sentencepiece_dir(tmp)
                    (Path(tmp) / "vocab.txt").write_text("[PAD]\n", encoding="utf-8")
                    (Path(tmp) / "tokenizer_config.json").write_text(
                        json.dumps({"tokenizer_class": tokenizer_class}),
                        encoding="utf-8",
                    )
                    kind, _ = embed._resolve_tokenizer(tmp)
                self.assertEqual(kind, expected)

    def test_both_artifacts_without_config_keep_sentencepiece(self):
        with tempfile.TemporaryDirectory() as tmp:
            _sentencepiece_dir(tmp)
            (Path(tmp) / "vocab.txt").write_text("[PAD]\n", encoding="utf-8")
            kind, _ = embed._resolve_tokenizer(tmp)
        self.assertEqual(kind, "sentencepiece")

    def test_malformed_tokenizer_config_keeps_sentencepiece(self):
        with tempfile.TemporaryDirectory() as tmp:
            _sentencepiece_dir(tmp)
            (Path(tmp) / "vocab.txt").write_text("[PAD]\n", encoding="utf-8")
            (Path(tmp) / "tokenizer_config.json").write_text("[]", encoding="utf-8")
            kind, _ = embed._resolve_tokenizer(tmp)
        self.assertEqual(kind, "sentencepiece")

    def test_missing_tokenizer_is_reported(self):
        with patch.object(embed.ort, "InferenceSession", return_value=MagicMock()):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(FileNotFoundError, "vocab.txt"):
                    embed.Embedder(tmp)


class InputSignatureTests(unittest.TestCase):
    """Only feed ONNX inputs the exported graph actually declares."""

    def _run(self, input_names):
        tokenizer = MagicMock()
        tokenizer.encode.return_value = [1, 2]
        session = MagicMock()
        session.get_inputs.return_value = [
            types.SimpleNamespace(name=name) for name in input_names
        ]
        session.run.return_value = [np.ones((1, 4, 4), dtype=np.float32)]

        with (
            patch.object(embed.ort, "InferenceSession", return_value=session),
            patch.object(
                embed.sentencepiece, "SentencePieceProcessor", return_value=tokenizer
            ),tempfile.TemporaryDirectory() as tmp
        ):
            _sentencepiece_dir(tmp)
            embedder = embed.Embedder(tmp)
        embedder.embed(["query: a"])
        return session.run.call_args.args[1]

    def test_omits_token_type_ids_when_graph_does_not_declare_it(self):
        inputs = self._run(["input_ids", "attention_mask"])
        self.assertEqual(set(inputs), {"input_ids", "attention_mask"})

    def test_includes_token_type_ids_when_graph_declares_it(self):
        inputs = self._run(["input_ids", "attention_mask", "token_type_ids"])
        self.assertEqual(
            set(inputs), {"input_ids", "attention_mask", "token_type_ids"}
        )


if __name__ == "__main__":
    unittest.main()
