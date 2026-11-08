import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zimantic import model_files


def _model_file(payload=b"model bytes", name="model.onnx"):
    return model_files.ModelFile(
        remote=f"onnx/{name}",
        name=name,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class DownloadFileTests(unittest.TestCase):
    def test_downloads_verifies_and_renames_without_leaving_a_part_file(self):
        payload = b"small fake onnx payload"
        model_file = _model_file(payload)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "nested" / "model.onnx"
            with patch.object(
                model_files.urllib.request,
                "urlopen",
                return_value=_Response(payload),
            ):
                written = model_files._download_file(
                    model_files.MODEL_SPECS["multilingual-e5-small"],
                    model_file,
                    destination,
                    force=False,
                )

            self.assertTrue(written)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(destination.with_name("model.onnx.part").exists())

    def test_matching_existing_file_is_not_downloaded_again(self):
        payload = b"already here"
        model_file = _model_file(payload)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "model.onnx"
            destination.write_bytes(payload)
            with patch.object(
                model_files.urllib.request,
                "urlopen",
                side_effect=AssertionError("unexpected download"),
            ):
                written = model_files._download_file(
                    model_files.MODEL_SPECS["multilingual-e5-small"],
                    model_file,
                    destination,
                    force=False,
                )

            self.assertFalse(written)
            self.assertEqual(destination.read_bytes(), payload)

    def test_conflicting_existing_file_requires_force(self):
        model_file = _model_file(b"expected")
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "model.onnx"
            destination.write_bytes(b"something else")
            with self.assertRaisesRegex(
                model_files.ModelDownloadError, "pass --force"
            ):
                model_files._download_file(
                    model_files.MODEL_SPECS["multilingual-e5-small"],
                    model_file,
                    destination,
                    force=False,
                )

    def test_checksum_mismatch_removes_the_partial_file(self):
        model_file = _model_file(b"expected")
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "model.onnx"
            with patch.object(
                model_files.urllib.request,
                "urlopen",
                return_value=_Response(b"corrupt"),
            ), self.assertRaisesRegex(
                model_files.ModelDownloadError, "checksum"
            ):
                model_files._download_file(
                    model_files.MODEL_SPECS["multilingual-e5-small"],
                    model_file,
                    destination,
                    force=False,
                )

            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name("model.onnx.part").exists())


class DownloadModelTests(unittest.TestCase):
    def test_writes_the_model_and_tokenizer_into_each_precision_folder(self):
        output = Path("/tmp/models")
        with patch.object(
            model_files, "_download_file", return_value=True
        ) as download:
            downloads = model_files.download_model(
                "e5-small-v2", ["fp16", "int8"], output
            )

        self.assertEqual(
            downloads,
            [("fp16", output / "fp16"), ("int8", output / "int8")],
        )
        destinations = [Path(call.args[2]) for call in download.call_args_list]
        self.assertEqual(
            destinations,
            [
                output / "fp16" / "vocab.txt",
                output / "fp16" / "model.onnx",
                output / "int8" / "vocab.txt",
                output / "int8" / "model.onnx",
            ],
        )

    def test_rejects_an_unknown_model(self):
        with self.assertRaisesRegex(model_files.ModelDownloadError, "unknown model"):
            model_files.download_model("not-a-model", ["int8"], Path("/tmp/models"))

    def test_rejects_an_unknown_precision(self):
        with self.assertRaisesRegex(
            model_files.ModelDownloadError, "unknown precision"
        ):
            model_files.download_model(
                "multilingual-e5-small", ["int4"], Path("/tmp/models")
            )

    def test_default_checksums_cover_every_default_precision(self):
        exports = model_files.MODEL_SPECS["multilingual-e5-small"].exports
        for precision in model_files.PRECISIONS:
            model = next(
                file for file in exports[precision] if file.name == "model.onnx"
            )
            with self.subTest(precision=precision):
                self.assertIn(
                    model.sha256, model_files.DEFAULT_MODEL_CHECKSUMS["model.onnx"]
                )

    def test_model_root_does_not_nest_a_precision_or_model_folder(self):
        self.assertEqual(
            model_files.model_root(Path("./model/int8")), Path("./model")
        )
        self.assertEqual(
            model_files.model_root(Path("./model/multilingual-e5-base/int8")),
            Path("./model"),
        )
        self.assertEqual(
            model_files.model_root(Path("./model")), Path("./model")
        )

    def test_non_default_models_get_their_own_folder(self):
        self.assertEqual(
            model_files.default_output_dir(
                "multilingual-e5-small", Path("./model")
            ),
            Path("./model"),
        )
        self.assertEqual(
            model_files.default_output_dir(
                "multilingual-e5-base", Path("./model")
            ),
            Path("./model/multilingual-e5-base"),
        )
        self.assertEqual(
            model_files.default_output_dir(
                "multilingual-e5-base",
                Path("./model/multilingual-e5-base/int8"),
            ),
            Path("./model/multilingual-e5-base"),
        )


if __name__ == "__main__":
    unittest.main()
