"""Pre/post-install check of the third-party APIs zimantic depends on.

``zimantic`` reaches into libzim through a few private and semi-private calls
(``Archive._get_entry_by_id``, ``Entry._index``, ``Entry.get_item``, the
``Searcher``/``Query`` pair). A libzim upgrade can change them silently, so this
module builds a tiny real ZIM and asserts the calls still exist and still
produce the outputs the indexer and searcher expect.

Run it after installing dependencies:

    python -m pytest tests/test_runtime_api.py
"""
import tempfile
import unittest
from pathlib import Path

from libzim.reader import Archive
from libzim.search import Query, Searcher
from libzim.writer import Creator, Hint, Item, StringProvider

from zimantic.zim import read_entry

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
    creator = Creator(str(path))
    creator.config_indexing(True, "eng")
    with creator:
        creator.add_item(_Page("Alpha", "Alpha", BODY_ALPHA))
        creator.add_item(_Page("Beta", "Beta", BODY_BETA))
        creator.add_redirection("Tires", "Tires", "Alpha", {})


class LibzimRuntimeApiTests(unittest.TestCase):
    """The private surface the indexer and searcher rely on, verified live."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.zim_path = Path(cls._tmp.name) / "sample.zim"
        _build_sample_zim(cls.zim_path)
        cls.archive = Archive(str(cls.zim_path))

    @classmethod
    def tearDownClass(cls):
        cls.archive = None
        cls._tmp.cleanup()

    def test_archive_exposes_the_private_entry_lookup(self):
        entry = self.archive._get_entry_by_id(0)
        self.assertIsInstance(entry._index, int)
        self.assertTrue(entry.path)
        self.assertTrue(entry.title)

    def test_private_entry_index_matches_path_lookup(self):
        entry = self.archive._get_entry_by_id(0)
        by_path = self.archive.get_entry_by_path(entry.path)
        self.assertEqual(by_path._index, entry._index)

    def test_redirect_entries_resolve_to_their_target(self):
        self.assertTrue(self.archive.has_entry_by_path("Tires"))
        redirect = self.archive.get_entry_by_path("Tires")
        self.assertTrue(redirect.is_redirect)
        target = redirect.get_redirect_entry()
        self.assertEqual(target.path, "Alpha")
        self.assertIsInstance(target._index, int)

    def test_entry_content_and_mimetype_are_readable(self):
        entry = self.archive.get_entry_by_path("Alpha")
        item = entry.get_item()
        self.assertTrue(item.mimetype.startswith("text/html"))
        self.assertIn(b"tires", bytes(item.content))

    def test_fulltext_searcher_returns_paths(self):
        if not self.archive.has_fulltext_index:
            self.skipTest("libzim was built without a full-text index")
        paths = list(
            Searcher(self.archive).search(Query().set_query("brakes")).getResults(0, 10)
        )
        self.assertIn("Beta", paths)

    def test_read_entry_produces_an_excerpt(self):
        # End-to-end: the real Archive -> private lookup -> text extraction.
        row = next(
            read_entry(self.archive, index)
            for index in range(self.archive.entry_count)
            if not self.archive._get_entry_by_id(index).is_redirect
        )
        self.assertEqual(len(row), 5)
        self.assertIn("tires", row[2])


if __name__ == "__main__":
    unittest.main()
