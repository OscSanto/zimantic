import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from zimantic import embed


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
            ),
        ):
            with tempfile.TemporaryDirectory() as tmp:
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


if __name__ == "__main__":
    unittest.main()
