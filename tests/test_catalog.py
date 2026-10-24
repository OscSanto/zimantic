"""Tests for the single-file ZIM catalog."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from libzim.reader import Archive
from libzim.writer import Creator, Hint, Item, StringProvider

from zimantic.catalog import (
    ZimCatalog,
    normalize_tags,
    perma_ref,
    read_catalog,
)


ALPHA_HTML = (
    "<html><body><p>Alpha is a sufficiently long article about tires and "
    "wheels that passes the minimum block length for indexing.</p></body></html>"
)


class _Page(Item):
    def __init__(self, path, title, html):
        super().__init__()
        self._path = path
        self._title = title
        self._html = html.encode("utf-8") if isinstance(html, str) else html

    def get_path(self):
        return self._path

    def get_title(self):
        return self._title

    def get_mimetype(self):
        return "text/html"

    def get_contentprovider(self):
        return StringProvider(self._html)

    def get_hints(self):
        return {Hint.FRONT_ARTICLE: True}


def _create_zim(path):
    creator = Creator(path)
    creator.config_indexing(True, "eng")
    creator.set_mainpath("Alpha")
    with creator:
        creator.add_item(_Page("Alpha", "Alpha", ALPHA_HTML))


class CatalogTests(unittest.TestCase):
    def test_scan_populates_and_skips_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            _create_zim(zims / "manual.zim")
            catalog = ZimCatalog(root / "indexes" / "catalog.sqlite", zims, archive_factory=Archive)

            first = catalog.scan()
            self.assertEqual(first["added"], 1)
            record = catalog.get_stem("manual")
            self.assertIsNotNone(record)
            self.assertEqual(record.available, 1)
            self.assertEqual(record.article_count, 1)
            self.assertGreaterEqual(record.entry_count, 1)
            self.assertIn(record.homepage, {"Alpha", "mainPage"})
            self.assertEqual(record.has_fulltext_index, 1)
            self.assertEqual(record.uuid, str(Archive(zims / "manual.zim").uuid))
            self.assertIn("_ftindex:no", record.tags)
            self.assertIn("_pictures:yes", record.tags)
            self.assertEqual(record.public_tags, "")
            self.assertIn("text/html", record.counter)

            second = catalog.scan()
            self.assertEqual(second["skipped"], 1)
            self.assertEqual(second["added"], 0)
            catalog.close()

    def test_invalid_zim_is_marked_and_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            _create_zim(zims / "manual.zim")
            (zims / "broken.zim").write_bytes(b"not a zim")
            catalog = ZimCatalog(root / "indexes" / "catalog.sqlite", zims, archive_factory=Archive)

            stats = catalog.scan()
            self.assertEqual(stats["added"], 1)
            self.assertEqual(stats["invalid"], 1)
            broken = catalog.get_stem("broken")
            self.assertIsNotNone(broken)
            self.assertEqual(broken.available, 0)
            self.assertTrue(broken.last_error)

            # Failing archives are retried on every scan, not skipped forever.
            again = catalog.scan()
            self.assertEqual(again["invalid"], 1)
            catalog.close()

    def test_upsert_from_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            zim_path = zims / "manual.zim"
            _create_zim(zim_path)
            with ZimCatalog(root / "indexes" / "catalog.sqlite") as catalog:
                record = catalog.upsert_from_archive(Archive(zim_path), zim_path)
                self.assertEqual(record.stem, "manual")
                self.assertEqual(record.article_count, 1)
                self.assertEqual(record.available, 1)
                self.assertEqual(record.last_error, "")
                self.assertEqual(len(read_catalog(root / "indexes" / "catalog.sqlite")), 1)

    def test_tag_normalization_and_perma_ref(self):
        self.assertEqual(
            normalize_tags("nopic;wikipedia;_ftindex"),
            "_pictures:no;wikipedia;_ftindex:yes;_videos:yes;_details:yes",
        )
        self.assertEqual(
            normalize_tags("_videos:no"),
            "_videos:no;_ftindex:no;_pictures:yes;_details:yes",
        )
        self.assertEqual(perma_ref("wikipedia_en_100_2026-08"), "wikipedia_en_100")
        self.assertEqual(perma_ref("custom_zim"), "custom_zim")

    def test_schema_version_rebuilds_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            _create_zim(zims / "manual.zim")
            db_path = root / "indexes" / "catalog.sqlite"
            catalog = ZimCatalog(db_path, zims, archive_factory=Archive)
            catalog.scan()
            catalog.close()

            db = sqlite3.connect(db_path)
            db.execute("PRAGMA user_version = 0")
            db.commit()
            db.close()

            catalog = ZimCatalog(db_path, zims, archive_factory=Archive)
            self.assertEqual(catalog.rows(), [])
            catalog.close()


if __name__ == "__main__":
    unittest.main()
