import importlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


build_module = importlib.import_module("zimantic.build")


class _FakeArchive:
    entry_count = 2

    def __init__(self, path):
        self.path = path


def _create_fast_index(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(build_module.SCHEMA)
    db.execute(
        "INSERT INTO docs(rowid, title, lead, path, target) VALUES (?, ?, ?, ?, ?)",
        (1, "Old title", "", "old", None),
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
                (1, "New title", "A full lead.", "new", None),
                (2, "Another title", "Another full lead.", "another", None),
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


if __name__ == "__main__":
    unittest.main()
