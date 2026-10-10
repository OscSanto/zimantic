import os
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
