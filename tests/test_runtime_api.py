"""Pre/post-install check of the third-party APIs zimantic depends on.

``zimantic`` uses private and version-dependent libzim calls for entry lookup
and searching. This module builds a tiny real ZIM and checks those adapters.

Run it after installing dependencies:

    python -m pytest tests/test_runtime_api.py
"""
import tempfile
import unittest
from pathlib import Path

from libzim.reader import Archive
from libzim.search import Query, Searcher
from libzim.writer import Creator, Hint, Item, StringProvider

from zimantic.content.reader import read_entry
from zimantic.libzim_compat import get_entry_by_id, search_results

BODY_ALPHA = (
    "<html><body><p>Alpha is a sufficiently long article about tires and "
    "wheels that passes the minimum block length for indexing.</p></body></html>"
)
BODY_BETA = (
    "<html><body><p>Beta is a sufficiently long article about brakes and "
    "rotors that passes the minimum block length for indexing.</p></body></html>"
)


class _Page(Item):
    def __init__(self, path, title, html):
        super().__init__()
        self._path = path
        self._title = title
        self._html = html.encode("utf-8")

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


def _build_sample_zim(path: Path) -> None:
    creator = Creator(path)
    creator.config_indexing(True, "eng")
    with creator:
        creator.add_item(_Page("Alpha", "Alpha", BODY_ALPHA))
        creator.add_item(_Page("Beta", "Beta", BODY_BETA))
        creator.add_redirection("Tires", "Tires", "Alpha", {})


class LibzimRuntimeApiTests(unittest.TestCase):
    """The private surface the indexer and searcher rely on, verified live."""

    archive: Archive

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.zim_path = Path(cls._tmp.name) / "sample.zim"
        _build_sample_zim(cls.zim_path)
        cls.archive = Archive(cls.zim_path)

    @classmethod
    def tearDownClass(cls):
        del cls.archive
        cls._tmp.cleanup()

    def test_archive_exposes_the_entry_lookup(self):
        entry = get_entry_by_id(self.archive, 0)
        self.assertIsInstance(entry._index, int)
        self.assertTrue(entry.path)
        self.assertTrue(entry.title)

    def test_private_entry_index_matches_path_lookup(self):
        entry = get_entry_by_id(self.archive, 0)
        by_path = self.archive.get_entry_by_path(entry.path)
        self.assertEqual(by_path._index, entry._index)

    def test_redirect_entries_resolve_to_their_target(self):
        self.assertTrue(self.archive.has_entry_by_path("Tires"))
        redirect = self.archive.get_entry_by_path("Tires")
        self.assertTrue(redirect.is_redirect)
        target = redirect.get_redirect_entry()
        self.assertEqual(target.path, "Alpha")
        self.assertIsInstance(target._index, int)
        row = read_entry(self.archive, redirect._index)
        self.assertEqual((row.path, row.target_id), ("Alpha", target._index))

    def test_random_article_fallback_reads_entries_by_id(self):
        from types import SimpleNamespace

        from zimantic.content.serving import random_fallback_article

        index = SimpleNamespace(ensure_open=lambda: self.archive, random_article_ids=None)
        self.assertIn(random_fallback_article(index, attempts=0), ("Alpha", "Beta"))

    def test_entry_content_and_mimetype_are_readable(self):
        entry = self.archive.get_entry_by_path("Alpha")
        item = entry.get_item()
        self.assertTrue(item.mimetype.startswith("text/html"))
        self.assertIn(b"tires", bytes(item.content))

    def test_fulltext_searcher_returns_paths(self):
        if not self.archive.has_fulltext_index:
            self.skipTest("libzim was built without a full-text index")
        search = Searcher(self.archive).search(Query().set_query("brakes"))
        self.assertIn("Beta", [result["path"] for result in search_results(search, 0, 10)])

    def test_native_searcher_returns_titles(self):
        if not self.archive.has_fulltext_index:
            self.skipTest("libzim was built without a full-text index")
        from zimantic.providers.zim.native import ZimNativeSearcher

        searcher = ZimNativeSearcher.open(self.archive)
        assert searcher is not None
        self.assertIn(("Beta", "Beta"), [(hit.path, hit.title) for hit in searcher.search("brakes", 10)])

    def test_fulltext_helper_returns_paths(self):
        if not self.archive.has_fulltext_index:
            self.skipTest("libzim was built without a full-text index")
        from zimantic.fulltext import FulltextPool

        pool = FulltextPool(1)
        try:
            results = pool.search_results(str(self.zim_path), "brakes", 10)
        finally:
            pool.close()
        self.assertIn({"path": "Beta", "title": "Beta"}, results)

    def test_read_entry_produces_an_excerpt(self):
        # End-to-end: the real Archive -> private lookup -> text extraction.
        row = next(
            read_entry(self.archive, index)
            for index in range(self.archive.entry_count)
            if not get_entry_by_id(self.archive, index).is_redirect
        )
        assert row is not None
        self.assertEqual(len(row), 7)
        self.assertIn("tires", row[2])


if __name__ == "__main__":
    unittest.main()
