"""Tests for the provider-neutral catalog and the ZIM scanner."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from libzim.reader import Archive
from libzim.writer import Creator, Hint, Item, StringProvider

from zimantic.catalog import (
    SCHEMA_VERSION,
    SOURCE_COLUMNS,
    SourceCatalog,
    catalog_path,
)
from zimantic.providers.config import ZimConfig
from zimantic.providers.zim.discovery import scan_collection, zim_source_key
from zimantic.providers.zim.metadata import (
    metadata_from_archive,
    normalize_counter,
    normalize_tags,
    perma_ref,
    resolve_homepage,
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


def _create_zim(path, *, creator_metadata=None):
    zim_creator = Creator(path)
    zim_creator.config_indexing(True, "eng")
    zim_creator.set_mainpath("Alpha")
    with zim_creator:
        if creator_metadata is not None:
            zim_creator.add_metadata("Creator", creator_metadata)
        zim_creator.add_item(_Page("Alpha", "Alpha", ALPHA_HTML))


def _scan(catalog, zims, *, name="library", force=False):
    return scan_collection(
        catalog,
        ZimConfig(name=name, path=str(zims)),
        collection_key=f"zim:{name}",
        archive_factory=Archive,
        force=force,
    )


class CatalogTests(unittest.TestCase):
    def test_scan_populates_and_skips_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            _create_zim(zims / "manual.zim")
            catalog = SourceCatalog(root / "indexes" / "catalog.sqlite")

            first = _scan(catalog, zims)
            self.assertEqual(first.stats["added"], 1)
            record = catalog.get_path(zims / "manual.zim")
            self.assertIsNotNone(record)
            self.assertEqual(
                record.key, zim_source_key("library", "manual.zim")
            )
            self.assertEqual(record.provider, "zim")
            self.assertEqual(record.item_count, 1)
            self.assertIn(record.homepage, {"Alpha", "mainPage"})
            self.assertIs(record.native_fulltext_available, 1)
            self.assertEqual(
                record.provider_id, str(Archive(zims / "manual.zim").uuid)
            )
            self.assertIn("_ftindex:no", record.tags)
            self.assertIn("_pictures:yes", record.tags)
            self.assertEqual(record.public_tags, "")
            self.assertIn("text/html", record.mime_counts)
            self.assertIsNone(record.icon_data)

            catalog.db.execute(
                "UPDATE sources SET icon_data = ?, icon_mime_type = 'image/png' "
                "WHERE key = ?",
                (b"x" * 2048, record.key),
            )
            catalog.db.commit()
            light = catalog.rows()[0]
            full = catalog.get_key(record.key)
            self.assertEqual(light.icon_data, b"\x01")
            self.assertEqual(full.icon_data, b"x" * 2048)

            second = _scan(catalog, zims)
            self.assertEqual(second.stats["skipped"], 1)
            self.assertEqual(second.stats["added"], 0)
            catalog.close()

    def test_database_open_failure_deletes_and_recreates_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "catalog.sqlite"
            db = sqlite3.connect(db_path)
            db.execute("CREATE TABLE stale_data (value TEXT)")
            db.execute("INSERT INTO stale_data VALUES ('old')")
            db.commit()
            db.close()
            Path(f"{db_path}-wal").write_bytes(b"stale WAL")
            Path(f"{db_path}-shm").write_bytes(b"stale SHM")

            real_connect = sqlite3.connect
            failed = False

            def connect(database, *args, **kwargs):
                nonlocal failed
                if str(database) == str(db_path) and not failed:
                    failed = True
                    raise sqlite3.DatabaseError("database is malformed")
                return real_connect(database, *args, **kwargs)

            with mock.patch("zimantic.catalog.sqlite3.connect", side_effect=connect):
                catalog = SourceCatalog(db_path)

            try:
                self.assertTrue(failed)
                self.assertEqual(catalog.db_path, db_path)
                self.assertEqual(catalog.rows(), [])
                tables = {
                    row["name"]
                    for row in catalog.db.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("sources", tables)
                self.assertNotIn("zims", tables)
            finally:
                catalog.close()

    def test_database_lock_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "catalog.sqlite"
            db = sqlite3.connect(db_path)
            db.execute("CREATE TABLE stale_data (value TEXT)")
            db.execute("INSERT INTO stale_data VALUES ('keep')")
            db.commit()
            db.close()

            with mock.patch(
                "zimantic.catalog.sqlite3.connect",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    SourceCatalog(db_path)

            db = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    db.execute("SELECT value FROM stale_data").fetchone()[0],
                    "keep",
                )
            finally:
                db.close()

    def test_invalid_zim_is_marked_and_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            _create_zim(zims / "manual.zim")
            (zims / "broken.zim").write_bytes(b"not a zim")
            catalog = SourceCatalog(root / "indexes" / "catalog.sqlite")

            stats = _scan(catalog, zims)
            self.assertEqual(stats.stats["added"], 1)
            self.assertEqual(stats.stats["invalid"], 1)
            broken = catalog.get_path(zims / "broken.zim")
            self.assertIsNotNone(broken)
            self.assertIsNone(broken.item_count)
            self.assertIsNone(broken.native_fulltext_available)

            # Routine refreshes stat paths and do not reopen an unchanged
            # archive; the failure is reported by the operation that hits it.
            again = _scan(catalog, zims)
            self.assertEqual(again.stats["skipped"], 2)
            catalog.close()

    def test_upsert_from_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            zim_path = zims / "manual.zim"
            _create_zim(zim_path)
            key = zim_source_key("library", "manual.zim")
            with SourceCatalog(root / "indexes" / "catalog.sqlite") as catalog:
                record = catalog.upsert(
                    metadata_from_archive(
                        Archive(zim_path),
                        zim_path,
                        key=key,
                        collection_key="zim:library",
                    )
                )
                self.assertEqual(record.key, key)
                self.assertEqual(record.item_count, 1)
                self.assertIsNotNone(record)
                self.assertEqual(len(catalog.rows()), 1)

    def test_placeholder_creator_is_stored_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            zim_path = zims / "manual.zim"
            _create_zim(zim_path, creator_metadata="-")

            with SourceCatalog(root / "indexes" / "catalog.sqlite") as catalog:
                _scan(catalog, zims)
                record = catalog.get_path(zim_path)

            self.assertIsNotNone(record)
            self.assertEqual(record.creator, "")
            self.assertIsNone(record.to_source_metadata()["creator"])

    def test_catalog_path_is_beside_the_index_directory(self):
        self.assertEqual(
            catalog_path("/data/zimantic/indexes"),
            Path("/data/zimantic/catalog.sqlite"),
        )
        self.assertEqual(
            catalog_path("indexes"),
            Path("catalog.sqlite"),
        )

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
        self.assertEqual(perma_ref("wikipedia_en_100_2026-08-15"), "wikipedia_en_100")
        self.assertEqual(perma_ref("custom_zim"), "custom_zim")

    def test_schema_version_rebuilds_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            _create_zim(zims / "manual.zim")
            db_path = root / "indexes" / "catalog.sqlite"
            catalog = SourceCatalog(db_path)
            _scan(catalog, zims)
            catalog.close()

            db = sqlite3.connect(db_path)
            db.execute("PRAGMA user_version = 0")
            db.commit()
            db.close()

            catalog = SourceCatalog(db_path)
            self.assertEqual(catalog.rows(), [])
            catalog.close()

    def test_foreign_schema_is_rebuilt(self):
        # A database that claims the current version but uses another layout
        # must be rebuilt, not queried.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "catalog.sqlite"
            db = sqlite3.connect(db_path)
            db.execute("CREATE TABLE sources (path TEXT PRIMARY KEY, size INTEGER)")
            db.execute("INSERT INTO sources VALUES ('stale', 1)")
            db.execute("CREATE TABLE zims (path TEXT PRIMARY KEY)")
            db.execute("INSERT INTO zims VALUES ('unrelated')")
            db.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            db.commit()
            db.close()

            with SourceCatalog(db_path) as catalog:
                self.assertEqual(catalog.rows(), [])
                columns = {
                    row["name"]
                    for row in catalog.db.execute("PRAGMA table_info(sources)")
                }
                self.assertEqual(
                    catalog.db.execute("SELECT path FROM zims").fetchone()["path"],
                    "unrelated",
                )
            self.assertEqual(columns, set(SOURCE_COLUMNS))

    def test_missing_directory_keeps_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            _create_zim(zims / "manual.zim")
            db_path = root / "indexes" / "catalog.sqlite"
            with SourceCatalog(db_path) as catalog:
                _scan(catalog, zims)
                self.assertEqual(len(catalog.rows()), 1)

            # The drive is not mounted: the scan must fail without deleting the
            # cache that the mounted directory will populate again.
            with SourceCatalog(db_path) as missing:
                result = _scan(missing, root / "unmounted")
                self.assertFalse(result.complete)
                self.assertEqual(len(missing.rows()), 1)

            with SourceCatalog(db_path) as catalog:
                self.assertEqual(len(catalog.rows()), 1)
                self.assertEqual(_scan(catalog, zims).stats["skipped"], 1)

    def test_version_token_tracks_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            _create_zim(zims / "manual.zim")
            with SourceCatalog(root / "indexes" / "catalog.sqlite") as catalog:
                before = catalog.version()
                _scan(catalog, zims)
                self.assertNotEqual(catalog.version(), before)
                after_scan = catalog.version()
                _scan(catalog, zims)
                self.assertEqual(catalog.version(), after_scan)

    def test_uppercase_extension_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            _create_zim(zims / "MANUAL.ZIM")
            with SourceCatalog(root / "indexes" / "catalog.sqlite") as catalog:
                stats = _scan(catalog, zims)
                self.assertEqual(stats.stats["added"], 1)
                self.assertIsNotNone(catalog.get_path(zims / "MANUAL.ZIM"))

    def test_unreadable_subdirectory_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            unreadable = zims / "private"
            unreadable.mkdir(parents=True)
            with SourceCatalog(root / "indexes" / "catalog.sqlite") as catalog:
                catalog.upsert(
                    {
                        "provider": "zim",
                        "collection_key": "zim:library",
                        "key": zim_source_key("library", "private/hidden.zim"),
                        "path": str(unreadable / "hidden.zim"),
                        "size": 10,
                        "mtime": 1,
                        "title": "hidden",
                    }
                )

                def fake_walk(_top, onerror=None):
                    onerror(PermissionError(13, "denied", str(unreadable)))
                    return iter([(str(zims), [], [])])

                with mock.patch(
                    "zimantic.providers.zim.discovery.os.walk", fake_walk
                ):
                    stats = _scan(catalog, zims)
                self.assertEqual(stats.stats["removed"], 0)
                self.assertIsNotNone(catalog.get_path(unreadable / "hidden.zim"))

    def test_unreadable_root_aborts_the_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zims = root / "zims"
            zims.mkdir()
            with SourceCatalog(root / "indexes" / "catalog.sqlite") as catalog:

                def fake_walk(top, onerror=None):
                    onerror(PermissionError(13, "denied", str(top)))
                    return iter(())

                with mock.patch(
                    "zimantic.providers.zim.discovery.os.walk", fake_walk
                ):
                    result = _scan(catalog, zims)
                    self.assertFalse(result.complete)
                    self.assertTrue(result.errors)

    def test_normalize_counter_sorts_by_count(self):
        self.assertEqual(
            normalize_counter("text/html=2;image/png=10;broken;video/mp4=2"),
            "image/png=10;text/html=2;video/mp4=2;broken",
        )
        self.assertEqual(normalize_counter(""), "")
        self.assertEqual(normalize_counter("nonsense"), "nonsense")

    def test_resolve_homepage_follows_redirects(self):
        class _Entry:
            def __init__(self, path, target=None):
                self.path = path
                self.is_redirect = target is not None
                self._target = target

            def get_redirect_entry(self):
                return self._target

        class _Archive:
            has_main_entry = True

            def __init__(self, entry):
                self.main_entry = entry

        target = _Entry("Alpha")
        self.assertEqual(
            resolve_homepage(_Archive(_Entry("mainPage", target))), "Alpha"
        )
        self.assertEqual(resolve_homepage(_Archive(target)), "Alpha")

        loop = _Entry("a")
        loop._target = loop
        loop.is_redirect = True
        self.assertEqual(resolve_homepage(_Archive(loop)), "")


if __name__ == "__main__":
    unittest.main()
