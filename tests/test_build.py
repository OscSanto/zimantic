import importlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np


build_module = importlib.import_module("zimantic.build")


class _FakeArchive:
    entry_count = 2

    def __init__(self, path):
        self.path = path


def _create_fast_index(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(build_module.SCHEMA)
    db.execute(
        "INSERT INTO docs(rowid, title, preview, embedding, path, target) VALUES (?, ?, ?, ?, ?, ?)",
        (1, "Old title", "", "", "old", None),
    )
    db.execute("INSERT INTO meta VALUES ('done', 'fast')")
    db.commit()
    db.close()


def _meta(path: Path) -> dict[str, str]:
    db = sqlite3.connect(path)
    values = dict(db.execute("SELECT key, value FROM meta"))
    db.close()
    return values


class BuildUpgradeTests(unittest.TestCase):
    def test_fast_index_stays_searchable_until_full_upgrade_is_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            db_path = directory / "manual.sqlite"
            faiss_path = directory / "manual.faiss"
            _create_fast_index(db_path)
            rows = [
                (1, "New title", "A preview.", "A passage.", "new", None, None),
                (2, "Another title", "Another preview.", "Another passage.", "another", None, None),
            ]

            with (
                patch.object(build_module, "Archive", _FakeArchive),
                patch.object(
                    build_module,
                    "read_entry",
                    side_effect=[rows[0], RuntimeError("interrupted")],
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    build_module.build(directory / "manual.zim", directory, None, 1)

            self.assertEqual(_meta(db_path), {"done": "fast"})
            db = sqlite3.connect(db_path)
            self.assertEqual(db.execute("SELECT title FROM docs").fetchone()[0], "Old title")
            db.close()
            staging_db, staging_faiss = build_module._upgrade_paths(db_path, faiss_path)
            self.assertTrue(staging_db.exists())
            self.assertFalse(staging_faiss.exists())

            def write_fake_faiss(_db, path):
                path.write_bytes(b"vectors")

            with (
                patch.object(build_module, "Archive", _FakeArchive),
                patch.object(build_module, "read_entry", return_value=rows[1]),
                patch.object(build_module, "_write_faiss", side_effect=write_fake_faiss),
            ):
                build_module.build(directory / "manual.zim", directory, None, 1)

            self.assertEqual({key: str(value) for key, value in _meta(db_path).items()}, {"done": "1"})
            db = sqlite3.connect(db_path)
            self.assertEqual(
                db.execute("SELECT title FROM docs ORDER BY rowid").fetchall(),
                [("New title",), ("Another title",)],
            )
            db.close()
            self.assertEqual(faiss_path.read_bytes(), b"vectors")
            self.assertFalse(staging_db.exists())
            self.assertFalse(staging_faiss.exists())


class FaissTrainingTests(unittest.TestCase):
    def test_training_stride_meets_faiss_minimum(self):
        vector_count = 700_000
        cluster_count = int(4 * vector_count**0.5)
        step = build_module._faiss_training_step(vector_count, cluster_count)

        self.assertGreaterEqual(
            vector_count // step,
            build_module._FAISS_MIN_POINTS_PER_CENTROID * cluster_count,
        )

    def test_training_stride_uses_all_vectors_when_fewer_than_target(self):
        vector_count = 10_000
        cluster_count = int(4 * vector_count**0.5)

        self.assertEqual(build_module._faiss_training_step(vector_count, cluster_count), 1)


class BuildBatchTests(unittest.TestCase):
    def test_fast_build_flushes_rows_while_scanning(self):
        class _ManyFakeArchive(_FakeArchive):
            entry_count = 5

        rows = [
            (index, f"Title {index}", "", "", f"path-{index}", None, None)
            for index in range(5)
        ]
        saved = []

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)

            def save(_db, _embedder, batch, next_entry):
                saved.append((len(batch), next_entry))

            with (
                patch.object(build_module, "Archive", _ManyFakeArchive),
                patch.object(build_module, "read_entry", side_effect=rows),
                patch.object(build_module, "_save", side_effect=save),
            ):
                build_module.build(directory / "manual.zim", directory, None, 2, fast=True)

        self.assertEqual(saved, [(2, 2), (2, 4), (1, 5)])

    def test_saved_embedding_text_is_persisted_and_embedded(self):
        class _FakeEmbedder:
            def __init__(self):
                self.embed_calls = []

            def embed(self, texts):
                self.embed_calls.append(texts)
                return np.array([[1.0, 0.0]], dtype=np.float32)

        db = sqlite3.connect(":memory:")
        db.executescript(build_module.SCHEMA)
        embedder = _FakeEmbedder()
        build_module._save(
            db,
            embedder,
            [(1, "A title", "a preview", "stored embedding", "article", None, None)],
            1,
        )

        self.assertEqual(
            db.execute("SELECT preview, embedding FROM docs WHERE rowid = 1").fetchone(),
            ("a preview", "stored embedding"),
        )
        self.assertEqual(embedder.embed_calls, [["passage: A title\nstored embedding"]])
        db.close()


class FaissTrainingSamplingTests(unittest.TestCase):
    def test_training_samples_use_vector_positions_not_sparse_entry_ids(self):
        class _FakeIndex:
            def train(self, vectors):
                self.training_vectors = vectors

            def add_with_ids(self, _vectors, _ids):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            db = sqlite3.connect(directory / "manual.sqlite")
            db.execute("CREATE TABLE vecs(id INTEGER PRIMARY KEY, v BLOB)")
            vector = np.array([1.0, 2.0], dtype=np.float16).tobytes()
            db.executemany(
                "INSERT INTO vecs VALUES (?, ?)",
                ((2 * index + 1, vector) for index in range(10_000)),
            )
            db.commit()
            index = _FakeIndex()
            with (
                patch.object(build_module, "_faiss_training_step", return_value=2),
                patch.object(build_module.faiss, "index_factory", return_value=index),
                patch.object(build_module.faiss, "write_index"),
            ):
                build_module._write_faiss(db, directory / "manual.faiss")
            db.close()

        self.assertEqual(index.training_vectors.shape, (5_000, 2))


if __name__ == "__main__":
    unittest.main()
