import sys
import types
import unittest
import tempfile
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import sqlite3
from unittest.mock import patch

import numpy as np


def _install_optional_dependency_stubs():
    faiss = types.ModuleType("faiss")
    faiss.IO_FLAG_MMAP_IFC = 1
    faiss.IO_FLAG_READ_ONLY = 2
    faiss.METRIC_INNER_PRODUCT = 0

    class _Index:
        def train(self, _vectors):
            pass

        def add_with_ids(self, _vectors, _ids):
            pass

    faiss.index_factory = lambda *_args: _Index()
    faiss.write_index = lambda _index, path: Path(path).write_bytes(b"vectors")

    libzim = types.ModuleType("libzim")
    libzim.__path__ = []
    reader = types.ModuleType("libzim.reader")
    reader.Archive = object
    reader.set_cluster_cache_max_size = lambda size: None
    search = types.ModuleType("libzim.search")
    search.Query = object
    search.Searcher = object
    sys.modules.setdefault("faiss", faiss)
    sys.modules.setdefault("libzim", libzim)
    sys.modules.setdefault("libzim.reader", reader)
    sys.modules.setdefault("libzim.search", search)


_install_optional_dependency_stubs()

from zimantic.cache import QueryCache
from zimantic.contracts import SourceInfo, SourceResult
from zimantic.search import (
    Search,
    SearchBusyError,
    _LocalIndex,
    _bounded_catalog_url,
    _fulltext_query,
    _find_archive_icon_path,
    _nprobe,
    _parse_remote_xml,
    _query_terms,
    _read_limited_response,
    _title_prefix_query,
    _title_query,
)


def _bare_search(cfg):
    """A Search without __init__ (no model, no filesystem index scan)."""
    search = object.__new__(Search)
    search.cfg = cfg
    search.embedder = None
    search.semantic = True
    search.indexes = {}
    search.sources = {}
    search._state_lock = threading.RLock()
    search._reload_lock = threading.Lock()
    search.source_timeout = 12
    search.candidate_count = cfg.get("candidate_count", 16)
    search.page_size = cfg.get("page_size", 10)
    search.max_results = cfg.get("max_results", 100)
    search.source_workers = 2
    search.max_concurrent_searches = 2
    search._search_slots = threading.BoundedSemaphore(2)
    search._executor = ThreadPoolExecutor(max_workers=2)
    search._inflight = {}
    search._inflight_lock = threading.Lock()
    search.cache = QueryCache(16)
    return search


class _IconItem:
    def __init__(self, mimetype, content):
        self.mimetype = mimetype
        self.content = content
        self.size = len(content)


class _IconEntry:
    def __init__(self, path, item):
        self.path = path
        self._item = item

    def get_item(self):
        return self._item


class _IconArchive:
    def __init__(self, entries, main_path):
        self._entries = entries
        self.main_entry = entries[main_path]

    def get_entry_by_path(self, path):
        if path not in self._entries:
            raise KeyError(path)
        return self._entries[path]


def _title_search(rows, query, limit=1):
    """Run a title-only search (no semantic or full-text) over an in-memory FTS.

    Returns the final ranked result list, exercising the local title path in
    isolation so prefix-fallback behaviour can be asserted end to end.
    """
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute(
        "CREATE VIRTUAL TABLE docs USING fts5("
        "title, path UNINDEXED, target UNINDEXED)"
    )
    db.executemany(
        "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
        rows,
    )
    source = SourceInfo("manual", "Manual", "manual", "local", local_name="manual")
    search = _bare_search({"candidate_count": 2, "kiwix_url": "http://example/content"})
    search.semantic = False
    search.sources = {"manual": source}
    search.indexes = {"manual": _LocalIndex(db, None, None, None, threading.Lock())}
    try:
        events = list(search.stream_search(query, limit=limit))
    finally:
        search._executor.shutdown(wait=True)
        db.close()
    return events[-1]["results"]


class SearchContractTests(unittest.TestCase):
    def test_source_icon_prefers_zim_metadata(self):
        homepage = _IconEntry(
            "Main_Page",
            _IconItem(
                "text/html",
                b'<link rel="icon" href="./assets/home.png">',
            ),
        )
        archive = _IconArchive(
            {
                "Main_Page": homepage,
                "assets/home.png": _IconEntry(
                    "assets/home.png",
                    _IconItem("image/png", b"home"),
                ),
                "-/icon": _IconEntry(
                    "-/icon",
                    _IconItem("image/png", b"zim"),
                ),
            },
            "Main_Page",
        )
        self.assertEqual(_find_archive_icon_path(archive, "Main_Page"), "-/icon")

    def test_source_icon_reads_homepage_relative_link(self):
        homepage = _IconEntry(
            "mainPage",
            _IconItem(
                "text/html",
                b'<link rel="icon" type="image/png" href="./_res_/favicon.png">',
            ),
        )
        archive = _IconArchive(
            {
                "mainPage": homepage,
                "_res_/favicon.png": _IconEntry(
                    "_res_/favicon.png",
                    _IconItem("image/png", b"favicon"),
                ),
            },
            "mainPage",
        )
        self.assertEqual(
            _find_archive_icon_path(archive, "mainPage"),
            "_res_/favicon.png",
        )

    def test_nprobe_scales_with_ivf_list_count(self):
        index = types.SimpleNamespace(nlist=10_000)

        self.assertEqual(_nprobe(index, {}), 600)

    def test_nprobe_can_be_overridden(self):
        index = types.SimpleNamespace(nlist=10_000)

        self.assertEqual(_nprobe(index, {"nprobe": 64}), 64)

    def test_nprobe_does_not_exceed_ivf_list_count(self):
        index = types.SimpleNamespace(nlist=10)

        self.assertEqual(_nprobe(index, {}), 1)
        self.assertEqual(_nprobe(index, {"nprobe": 64}), 10)

    def test_keyword_query_removes_filler_words_but_keeps_meaningful_terms(self):
        self.assertEqual(_title_query("how to change tires"), '"change" AND "tires"')
        self.assertEqual(_fulltext_query("how to change tires"), "change tires")
        self.assertEqual(_query_terms("how to change tires"), ["change", "tire"])

    def test_stopword_only_query_is_not_filtered_away(self):
        self.assertEqual(_title_query("how to"), '"how" AND "to"')
        self.assertEqual(_fulltext_query("how to"), "how to")
        self.assertEqual(_title_query("the"), '"the"')

    def test_title_prefix_query_uses_prefix_tokens_except_single_characters(self):
        self.assertEqual(_title_prefix_query("how to change tires"), "change* AND tires*")
        self.assertEqual(_title_prefix_query("praise of fol"), "praise* AND fol*")
        self.assertEqual(_title_prefix_query("c"), '"c"')

    def test_cache_key_normalizes_query_and_source_selection(self):
        self.assertEqual(
            Search._cache_key("How  TO change", ["beta", "alpha"]),
            Search._cache_key("how to change", ["alpha", "beta"]),
        )
        self.assertNotEqual(
            Search._cache_key("q", ["manual"]),
            Search._cache_key("q", ["other"]),
        )

    def test_catalog_limit_replaces_unbounded_count(self):
        url = _bounded_catalog_url(
            "http://kiwix/entries?count=-1&foo=bar",
            50_000,
        )
        self.assertEqual(url, "http://kiwix/entries?count=50000&foo=bar")

    def test_remote_xml_rejects_dtds(self):
        with self.assertRaises(ValueError):
            _parse_remote_xml(b"<!DOCTYPE root [<!ENTITY x 'y'>]><root/>")

    def test_remote_response_reader_enforces_a_hard_limit(self):
        class _Response:
            headers = {}

            def read(self, size=-1):
                return b"x" * 4

        with self.assertRaises(ValueError):
            _read_limited_response(_Response(), 3)

    def test_hybrid_ranking_is_deterministic(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        doc = {
            "id": "manual:z-tire",
            "source_key": "manual",
            "source": "Manual",
            "title": "Tire change",
            "lead": "Change a tire safely.",
            "path": "z-tire",
            "url": "http://example/z-tire",
            "source_rank": 1,
        }
        tie = {
            "id": "manual:a-tire",
            "source_key": "manual",
            "source": "Manual",
            "title": "Tire change",
            "lead": "Change a tire safely.",
            "path": "a-tire",
            "url": "http://example/a-tire",
            "source_rank": 1,
        }
        result = SourceResult(
            source=source,
            items=[doc, tie],
            semantic=[(0.9, doc), (0.9, tie)],
            keyword=[(-1.0, doc), (-1.0, tie)],
            fulltext=[(0, doc), (0, tie)],
        )
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16
        first = search._rank_results([result], "how to change tires", 10)
        second = search._rank_results([result], "how to change tires", 10)
        self.assertEqual(first, second)
        self.assertEqual([item["id"] for item in first], ["manual:a-tire", "manual:z-tire"])
        self.assertIn("title 100%", first[0]["explain"])
        self.assertEqual(first[0]["ranking"]["final_rank"], 1)
        self.assertEqual(first[0]["ranking"]["rrf"]["semantic"]["rank"], 1)
        self.assertEqual(first[0]["ranking"]["rrf"]["title"]["rank"], 1)
        self.assertEqual(first[0]["ranking"]["rrf"]["fulltext"]["rank"], 1)
        self.assertEqual(first[0]["ranking"]["title"]["matches"], 2)

    def test_ranking_deduplicates_repeated_hits_and_preserves_source_rank(self):
        semantic_source = SourceInfo("semantic", "Semantic", "semantic", "local")
        title_source = SourceInfo("titles", "Titles", "titles", "local")
        unrelated = {
            "id": "semantic:index",
            "source_key": "semantic",
            "source": "Semantic",
            "title": "index.html",
            "lead": "",
            "path": "index.html",
            "source_rank": 1,
        }
        exact = {
            "id": "titles:intuition",
            "source_key": "titles",
            "source": "Titles",
            "title": "Intuition",
            "lead": "",
            "path": "intuition",
            "source_rank": 3,
        }
        result = SourceResult(
            source=semantic_source,
            items=[unrelated],
            semantic=[(0.99, unrelated)] * 10,
        )
        result_with_title = SourceResult(
            source=title_source,
            items=[exact],
            keyword=[(-1.0, exact)],
            fulltext=[(0, exact)],
        )
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16

        ranked = search._rank_results([result, result_with_title], "intuition", 10)

        self.assertEqual(ranked[0]["id"], "titles:intuition")
        self.assertEqual(ranked[0]["rank"], 3)
        self.assertEqual(ranked[1]["score"], 0.016666666666666666)

    def test_compact_title_with_all_query_words_beats_longer_title(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        compact = {
            "id": "manual:compact",
            "source_key": "manual",
            "source": "Manual",
            "title": "Change Tire",
            "lead": "",
            "path": "compact",
            "source_rank": 2,
        }
        long = {
            "id": "manual:long",
            "source_key": "manual",
            "source": "Manual",
            "title": "Change Tire Maintenance and Safety Guide",
            "lead": "",
            "path": "long",
            "source_rank": 1,
        }
        result = SourceResult(
            source=source,
            items=[compact, long],
            keyword=[(-2.0, long), (-3.0, compact)],
        )
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16

        ranked = search._rank_results([result], "change tire", 10)

        self.assertEqual([item["id"] for item in ranked], ["manual:compact", "manual:long"])
        self.assertIn("density 100%", ranked[0]["explain"])

    def test_stream_emits_source_snapshots_and_final_results(self):
        sources = [
            SourceInfo("one", "One", "one", "local"),
            SourceInfo("two", "Two", "two", "local"),
        ]
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16
        search.semantic = False
        search.indexes = {}
        search.sources = {source.key: source for source in sources}
        search._state_lock = threading.RLock()
        search._reload_lock = threading.Lock()
        search._search_slots = threading.BoundedSemaphore(1)
        search._executor = ThreadPoolExecutor(max_workers=2)
        search._inflight = {}
        search._inflight_lock = threading.Lock()
        search.cache = QueryCache(16)
        search.embedder = types.SimpleNamespace(embed=lambda _: [[1.0]])

        def fake_source_search(source, query, title_query, fulltext_query, query_vector, limit):
            doc = {
                "id": f"{source.key}:page",
                "source_key": source.key,
                "source": source.name,
                "title": f"{source.name} page",
                "lead": "A useful page.",
                "path": "page",
                "url": f"http://example/{source.key}",
                "source_rank": 1,
            }
            return SourceResult(source=source, items=[doc], fulltext=[(0, doc)])

        search._search_source = fake_source_search
        events = list(search.stream_search("useful page", limit=2))
        search._executor.shutdown(wait=True)
        event_types = [event["type"] for event in events]
        self.assertEqual(event_types[0], "started")
        self.assertEqual(event_types[-1], "done")
        self.assertEqual(event_types.count("source"), 2)
        self.assertEqual(event_types.count("snapshot"), 2)
        self.assertEqual(len(events[-1]["results"]), 2)

    def test_stream_bounds_intermediate_ranking_passes_for_many_sources(self):
        sources = [
            SourceInfo(f"source-{index}", f"Source {index}", f"source-{index}", "local")
            for index in range(20)
        ]
        search = _bare_search(
            {
                "candidate_count": 2,
                "max_stream_ranks": 4,
            }
        )
        search.max_stream_ranks = 4
        search.semantic = False
        search.sources = {source.key: source for source in sources}
        search._state_lock = threading.RLock()
        search._search_slots = threading.BoundedSemaphore(1)
        search.embedder = None
        rank_sizes = []

        def fake_source_search(source, query, title_query, fulltext_query, query_vector, limit):
            doc = {
                "id": f"{source.key}:page",
                "source_key": source.key,
                "source": source.name,
                "title": source.name,
                "lead": "",
                "path": "page",
                "url": None,
                "source_rank": 1,
            }
            return SourceResult(source=source, items=[doc], fulltext=[(0, doc)])

        real_rank_results = search._rank_results

        def rank_results(source_results, query, limit):
            rank_sizes.append(len(source_results))
            return real_rank_results(source_results, query, limit)

        search._search_source = fake_source_search
        search._rank_results = rank_results
        try:
            events = list(search.stream_search("source page", limit=2))
        finally:
            search._executor.shutdown(wait=True)

        self.assertEqual(rank_sizes, [1, 5, 10, 15, 20])
        self.assertEqual(
            sum(event["type"] == "snapshot" for event in events),
            len(rank_sizes),
        )
        self.assertEqual(len(events[-1]["results"]), 2)

    def test_local_source_uses_batched_document_hydration(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, path UNINDEXED, target UNINDEXED)"
        )
        db.execute(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            (1, "Tire Change", "tire-change", None),
        )

        class FakeFaiss:
            def search(self, query_vector, count):
                return np.array([[0.9]]), np.array([[1]])

        source = SourceInfo("manual", "Manual", "manual", "local", local_name="manual")
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 2, "kiwix_url": "http://example/content"}
        search.candidate_count = 2
        search.semantic = True
        search.sources = {"manual": source}
        search.indexes = {
            "manual": _LocalIndex(db, FakeFaiss(), None, None, threading.Lock())
        }
        search._state_lock = threading.RLock()
        search._reload_lock = threading.Lock()
        search._search_slots = threading.BoundedSemaphore(1)
        search._executor = ThreadPoolExecutor(max_workers=1)
        search._inflight = {}
        search._inflight_lock = threading.Lock()
        search.cache = QueryCache(16)
        search.embedder = types.SimpleNamespace(embed=lambda _: np.array([[1.0]]))

        events = list(search.stream_search("how to change tires", limit=1))
        search._executor.shutdown(wait=True)
        db.close()
        result = events[-1]["results"][0]
        self.assertEqual(result["title"], "Tire Change")
        self.assertEqual(result["path"], "tire-change")

    def test_document_hydration_batches_large_rowid_sets(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, path UNINDEXED, target UNINDEXED)"
        )
        db.executemany(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            [
                (rowid, f"Title {rowid}", f"path-{rowid}", None)
                for rowid in range(1, 1802)
            ],
        )
        db.commit()
        statements = []
        db.set_trace_callback(statements.append)

        source = SourceInfo("manual", "Manual", "manual", "local")
        search = object.__new__(Search)
        search.cfg = {"kiwix_url": "http://example/content"}
        search.preview_chars = 100
        try:
            docs = search._fetch_docs(db=db, source=source, rowids=set(range(1, 1802)))
        finally:
            db.close()

        selects = [
            statement
            for statement in statements
            if statement.startswith("SELECT rowid, title, path, target FROM docs")
        ]
        self.assertEqual(len(selects), 3)
        self.assertEqual(len(docs), 1801)
        self.assertEqual(docs[1801]["path"], "path-1801")

    def test_semantic_results_below_cosine_threshold_are_ignored(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, path UNINDEXED, target UNINDEXED)"
        )
        db.executemany(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            [
                (1, "Below threshold", "weak", None),
                (2, "Above threshold", "strong", None),
            ],
        )

        class FakeFaiss:
            def search(self, query_vector, count):
                return (
                    np.array([[0.84, 0.90]]),
                    np.array([[1, 2]]),
                )

        source = SourceInfo("manual", "Manual", "manual", "local", local_name="manual")
        search = object.__new__(Search)
        search.cfg = {
            "candidate_count": 2,
            "kiwix_url": "http://example/content",
            "min_cosine_similarity": 0.85,
        }
        search.candidate_count = 2
        result = search._search_local_locked(
            source,
            "unrelated",
            _title_query("unrelated"),
            _fulltext_query("unrelated"),
            np.array([[1.0]]),
            1,
            _LocalIndex(db, FakeFaiss(), None, None, threading.Lock()),
        )

        db.close()
        self.assertEqual([item["title"] for item in result.items], ["Above threshold"])
        self.assertEqual(result.semantic[0][0], 0.90)

    def test_local_result_preview_is_extracted_for_display(self):
        class _Entry:
            _index = 1

        class _Archive:
            def get_entry_by_path(self, path):
                self.path = path
                return _Entry()

        source = SourceInfo(
            "manual",
            "Manual",
            "manual",
            "local",
            local_name="manual",
        )
        search = object.__new__(Search)
        search.cfg = {
            "max_html_bytes": 4_194_304,
            "max_article_bytes": 33_554_432,
            "max_preview_chars": 24,
        }
        search.preview_chars = 24
        search.sources = {"manual": source}
        search.indexes = {
            "manual": _LocalIndex(
                None,
                None,
                _Archive(),
                None,
                threading.Lock(),
            )
        }
        search._state_lock = threading.RLock()
        search._executor = ThreadPoolExecutor(max_workers=1)
        search.preview_cache = QueryCache(16)
        doc = {
            "id": "manual:long-page",
            "source_key": "manual",
            "title": "Long page",
            "lead": "",
            "path": "long-page",
        }

        try:
            with patch(
                "zimantic.search.read_entry",
                return_value=(1, "Long page", "one " * 400, "long-page", None),
            ):
                search._hydrate_results([doc])
        finally:
            search._executor.shutdown(wait=True)

        self.assertLessEqual(len(doc["lead"]), 24)
        self.assertTrue(doc["lead"].endswith("one"))

    def test_title_search_falls_back_to_prefix_when_exact_matches_nothing(self):
        # Exact token matching misses these because the query word is a truncated
        # or base form of the title word; only the prefix fallback surfaces them.
        cases = [
            ((1, "Tire Changes", "tire-changes", None), "tire change", "Tire Changes"),
            ((1, "Gettysburg Address", "gettysburg-address", None), "gettysburg addres", "Gettysburg Address"),
            ((1, "Collected Poems", "collected-poems", None), "collected poem", "Collected Poems"),
            ((1, "Declaration of Independence", "declaration-independence", None), "declaration independen", "Declaration of Independence"),
            ((1, "The Praise of Folly", "praise-of-folly", None), "praise of fol", "The Praise of Folly"),
        ]
        for row, query, expected in cases:
            with self.subTest(query=query):
                results = _title_search([row], query)
                self.assertTrue(results, f"no results for {query!r}")
                self.assertEqual(results[0]["title"], expected)


class DisambiguationRankingTests(unittest.TestCase):
    def _search(self):
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16
        return search

    def _result(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        hub = {
            "id": "manual:hub",
            "source_key": "manual",
            "source": "Manual",
            "title": "Air (disambiguation)",
            "lead": "Air may refer to:",
            "path": "Air_(disambiguation)",
            "source_rank": 1,
        }
        article = {
            "id": "manual:air",
            "source_key": "manual",
            "source": "Manual",
            "title": "Air",
            "lead": "Air is a mixture of gases.",
            "path": "Air",
            "source_rank": 1,
        }
        return SourceResult(
            source=source,
            items=[hub, article],
            keyword=[(-5.0, article)],
            fulltext=[(0, article)],
        )

    def test_exact_disambiguation_title_query_promotes_page(self):
        ranked = self._search()._rank_results([self._result()], "air (disambiguation)", 10)

        self.assertEqual(ranked[0]["id"], "manual:hub")
        self.assertNotIn("kind", ranked[0])
        self.assertNotIn("members", ranked[0])

    def test_question_and_unparenthesized_disambiguation_queries_promote_page(self):
        for query in ("what is air?", "air disambiguation"):
            with self.subTest(query=query):
                ranked = self._search()._rank_results([self._result()], query, 10)
                self.assertEqual(ranked[0]["id"], "manual:hub")

    def test_base_title_and_partial_queries_demote_hub(self):
        for query in ("air", "air quality"):
            with self.subTest(query=query):
                ranked = self._search()._rank_results([self._result()], query, 10)
                self.assertEqual(ranked[0]["id"], "manual:air")


class UserPageRankingTests(unittest.TestCase):
    def _search(self):
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16
        return search

    def _result(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        user = {
            "id": "manual:user",
            "source_key": "manual",
            "source": "Manual",
            "title": "User Lockjaw",
            "lead": "Member since July 2017.",
            "path": "users/123/lockjaw",
            "source_rank": 1,
        }
        article = {
            "id": "manual:article",
            "source_key": "manual",
            "source": "Manual",
            "title": "Lockjaw",
            "lead": "Lockjaw is another name for trismus.",
            "path": "Lockjaw",
            "source_rank": 2,
        }
        return SourceResult(
            source=source,
            items=[user, article],
            keyword=[(-1.0, user), (-2.0, article)],
            fulltext=[(0, user), (1, article)],
        )

    def test_user_pages_are_demoted_for_normal_queries(self):
        ranked = self._search()._rank_results([self._result()], "lockjaw", 10)

        self.assertEqual(ranked[0]["id"], "manual:article")
        user = next(item for item in ranked if item["id"] == "manual:user")
        self.assertEqual(user["ranking"]["user_page"]["action"], "demote")

    def test_explicit_user_queries_keep_user_pages_eligible(self):
        ranked = self._search()._rank_results([self._result()], "user lockjaw", 10)

        self.assertEqual(ranked[0]["id"], "manual:user")
        self.assertEqual(ranked[0]["ranking"]["user_page"]["action"], "none")


def _write_index(index_dir: Path, name: str, *, done: str = "1", rows=()):
    db = sqlite3.connect(index_dir / f"{name}.sqlite")
    db.executescript(
        "CREATE VIRTUAL TABLE docs USING fts5("
        "title, path UNINDEXED, target UNINDEXED, "
        "tokenize='unicode61 remove_diacritics 2');"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value);"
    )
    db.executemany(
        "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
        rows,
    )
    db.execute("INSERT INTO meta VALUES ('done', ?)", (done,))
    db.commit()
    db.close()
    return index_dir / f"{name}.sqlite"


class GracefulDegradationTests(unittest.TestCase):
    def _cfg(self, tmp):
        return {
            "index_dir": str(tmp),
            "zim_dir": str(tmp),
            "kiwix_url": "http://example/content",
            "candidate_count": 16,
        }

    def test_index_without_vectors_still_serves_title_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            self.assertIn("manual", search.indexes)
            self.assertFalse(search.indexes["manual"].semantic)

            results = search.search("tire change", limit=1)
            self.assertEqual(results[0]["title"], "Tire Change")
            self.assertEqual(results[0]["path"], "tire-change")
            search._executor.shutdown(wait=True)

    def test_deferred_faiss_loads_vectors_after_indexes_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            (tmp / "manual.faiss").write_bytes(b"vectors")
            search = Search(self._cfg(tmp), semantic=True)
            self.addCleanup(search._executor.shutdown, wait=True)

            self.assertFalse(search.indexes["manual"].semantic)

            reloaded = threading.Event()
            original_reload = search.reload

            def reload():
                try:
                    return original_reload()
                finally:
                    reloaded.set()

            with (
                patch.object(sys.modules["faiss"], "read_index", return_value=object(), create=True),
                patch.object(search, "reload", side_effect=reload),
            ):
                search.start_faiss()
                self.assertTrue(reloaded.wait(2))

            self.assertTrue(search.indexes["manual"].semantic)

    def test_archive_lookup_allows_nested_moves_but_rejects_size_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "collection" / "nested"
            nested.mkdir(parents=True)
            moved = nested / "manual.zim"
            moved.write_bytes(b"x" * 10)
            search = _bare_search({"zim_dir": str(root)})

            self.assertEqual(search._find_zim_path("manual", 10), moved)
            (root / "other").mkdir()
            (root / "other" / "manual.zim").write_bytes(b"x" * 11)
            self.assertIsNone(search._find_zim_path("manual", 10))

    def test_finished_stream_is_cached_for_search_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            events = list(search.stream_search("tire change", limit=1))
            self.assertEqual(events[-1]["type"], "done")
            self.assertIsNotNone(search.cache.get(search._cache_key("tire change", None)))

            with patch.object(search, "_search_source", side_effect=AssertionError("cache miss")):
                cached = search.search("tire change", limit=1)
            self.assertEqual(cached[0]["title"], "Tire Change")
            search._executor.shutdown(wait=True)

    def test_cached_stream_replays_source_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(
                tmp,
                "manual",
                rows=[(1, "Tire Change", "tire-change", None)],
            )
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            first_events = list(search.stream_search("tire change", limit=1))
            cached_events = list(search.stream_search("tire change", limit=1))
            search._executor.shutdown(wait=True)

            self.assertEqual([event["type"] for event in cached_events], [
                "started", "source", "snapshot", "done",
            ])
            self.assertEqual(cached_events[1]["source"]["key"], "manual")
            self.assertEqual(cached_events[1]["count"], 1)
            self.assertEqual(cached_events[-1]["results"], first_events[-1]["results"])

    def test_refresh_sources_keeps_cache_when_sources_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            events = list(search.stream_search("tire change", limit=1))
            self.assertEqual(events[-1]["type"], "done")
            key = search._cache_key("tire change", None)
            self.assertIsNotNone(search.cache.get(key))

            # A plain source refresh with no changes must keep the cached answer
            # (source discovery happens on page load without re-running searches).
            search.refresh_sources()
            self.assertIsNotNone(search.cache.get(key))

            # Adding an index changes what is searchable, so stale answers drop.
            _write_index(tmp, "second", rows=[(1, "Second page", "second", None)])
            search.reload()
            self.assertIsNone(search.cache.get(key))
            search._executor.shutdown(wait=True)

    def test_reload_keeps_cache_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()
            list(search.stream_search("tire change", limit=1))
            key = search._cache_key("tire change", None)
            self.assertIsNotNone(search.cache.get(key))

            search.reload()
            self.assertIsNotNone(search.cache.get(key))
            search._executor.shutdown(wait=True)

    def test_reload_picks_up_and_forgets_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            search = _bare_search(self._cfg(tmp))

            self.assertEqual(search.reload()["indexes"], [])

            _write_index(tmp, "manual", done="fast", rows=[(1, "Tire Change", "tire-change", None)])
            summary = search.reload()
            self.assertEqual(summary["added"], ["manual"])
            self.assertIn("manual", search.indexes)
            self.assertFalse(search.indexes["manual"].semantic)

            (tmp / "manual.sqlite").unlink()
            summary = search.reload()
            self.assertEqual(summary["removed"], ["manual"])
            self.assertEqual(search.indexes, {})
            search._executor.shutdown(wait=True)

    def test_reload_reopens_a_replaced_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(
                tmp,
                "manual",
                rows=[(1, "Old title", "old", None)],
            )
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()
            old_index = search.indexes["manual"]

            _write_index(
                tmp,
                "replacement",
                rows=[(1, "New title", "new", None)],
            )
            (tmp / "replacement.sqlite").replace(tmp / "manual.sqlite")

            search.reload()

            self.assertIsNot(search.indexes["manual"], old_index)
            self.assertEqual(search.search("new title", limit=1)[0]["title"], "New title")
            search._close_index(search.indexes["manual"])
            search._executor.shutdown(wait=True)


class CatalogRefreshTests(unittest.TestCase):
    def test_failed_catalog_refresh_preserves_last_successful_entries(self):
        search = _bare_search({"kiwix_server": "http://kiwix"})
        entries = [{"name": "Remote Manual", "book": "manual"}]

        with patch.object(search, "_catalog_entries", side_effect=[entries, None]):
            first = search.refresh_sources()
            second = search.refresh_sources()

        self.assertEqual(first[0]["name"], "Remote Manual")
        self.assertEqual(second[0]["name"], "Remote Manual")

    def test_catalog_refresh_retries_with_bounded_backoff(self):
        class _Response:
            headers = {}

            def __init__(self):
                self._read = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def _payload():
                return (
                    b"<feed><entry><title>Remote Manual</title>"
                    b'<link href="/content/manual" /></entry></feed>'
                )

            def read(self, _size):
                if self._read:
                    return b""
                self._read = True
                return self._payload()

        search = _bare_search({})
        with (
            patch(
                "zimantic.search.urlopen",
                side_effect=[OSError("temporary outage"), _Response()],
            ) as urlopen,
            patch("zimantic.search.time.sleep") as sleep,
        ):
            entries = search._catalog_entries("http://kiwix")

        self.assertEqual(entries[0]["book"], "manual")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)


class PaginationTests(unittest.TestCase):
    def _cfg(self, tmp):
        return {
            "index_dir": str(tmp),
            "zim_dir": str(tmp),
            "kiwix_url": "http://example/content",
            "candidate_count": 16,
            "page_size": 2,
            "max_results": 5,
        }

    def test_pages_are_prefixes_of_one_ranked_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rows = [(i, f"Tire Change {i}", f"tire-{i}", None) for i in range(1, 6)]
            _write_index(tmp, "manual", rows=rows)
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            page1 = search.search("tire change", limit=2, offset=0)
            page2 = search.search("tire change", limit=2, offset=2)
            page3 = search.search("tire change", limit=2, offset=4)
            whole = search.search("tire change", limit=5, offset=0)

            paged = [doc["id"] for doc in page1 + page2 + page3]
            self.assertEqual(paged, [doc["id"] for doc in whole])
            search._executor.shutdown(wait=True)

    def test_second_page_reuses_the_cached_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rows = [(i, f"Tire Change {i}", f"tire-{i}", None) for i in range(1, 6)]
            _write_index(tmp, "manual", rows=rows)
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            first = search.search("tire change", limit=2, offset=0)
            pooled = search.cache.get(search._cache_key("tire change", None))
            self.assertEqual(len(pooled["pool"]), 5)
            # A different page must be answered from the same cached entry.
            second = search.search("tire change", limit=2, offset=2)
            self.assertIs(search.cache.get(search._cache_key("tire change", None)), pooled)
            self.assertNotEqual(first[0]["id"], second[0]["id"])
            search._executor.shutdown(wait=True)

    def test_display_source_filter_keeps_counts_for_all_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "alpha", rows=[(1, "Tire Change Alpha", "a", None)])
            _write_index(tmp, "beta", rows=[(1, "Tire Change Beta", "b", None)])
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            page = search.search_page("tire change", limit=10, source="beta")

            self.assertEqual([doc["source_key"] for doc in page["results"]], ["beta"])
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["counts"], {"alpha": 1, "beta": 1})

            page = search.search_page("tire change", limit=10, source=["alpha", "beta"])

            self.assertEqual(
                [doc["source_key"] for doc in page["results"]],
                ["alpha", "beta"],
            )
            self.assertEqual(page["total"], 2)
            search._executor.shutdown(wait=True)

    def test_concurrent_identical_searches_share_one_computation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire", None)])
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            original = search._search_source
            release = threading.Event()
            calls = 0

            def slow(*args, **kwargs):
                nonlocal calls
                calls += 1
                release.wait(timeout=5)
                return original(*args, **kwargs)

            search._search_source = slow
            first: list = []
            second: list = []

            def run(target):
                target.extend(search.stream_search("tire change", limit=1))

            owner = threading.Thread(target=run, args=(first,))
            owner.start()
            # Wait until the owner has claimed the key, then start a waiter.
            deadline = time.monotonic() + 5
            while not search._inflight and time.monotonic() < deadline:
                time.sleep(0.005)
            waiter = threading.Thread(target=run, args=(second,))
            waiter.start()
            time.sleep(0.05)
            release.set()
            owner.join(timeout=5)
            waiter.join(timeout=5)

            self.assertEqual(calls, 1)
            self.assertEqual(first[-1]["results"], second[-1]["results"])
            search._executor.shutdown(wait=True)

    def test_debug_flag_gates_ranking_explanation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire", None)])
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            plain = search.search_page("tire change", limit=10, debug=False)["results"][0]
            detailed = search.search_page("tire change", limit=10, debug=True)["results"][0]

            self.assertNotIn("explain", plain)
            self.assertNotIn("score", plain)
            self.assertNotIn("ranking", plain)
            self.assertIn("explain", detailed)
            self.assertIn("ranking", detailed)
            self.assertEqual(
                search.search_page("tire change", limit=10, debug=True)["matched_by"],
                {"semantic": 0, "title": 1, "fulltext": 0},
            )
            search._executor.shutdown(wait=True)


class ProductionHardeningTests(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, path UNINDEXED, target UNINDEXED)"
        )
        db.execute(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            (1, "Tire Change", "tire-change", None),
        )
        return db

    def test_local_results_do_not_require_kiwix_url(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        search = object.__new__(Search)
        search.cfg = {}  # no kiwix_url
        search.preview_chars = 100
        db = self._db()
        try:
            doc = search._fetch_docs(source, db, {1})[1]
        finally:
            db.close()
        self.assertEqual(doc["url"], "/content/manual/tire-change")
        self.assertEqual(doc["path"], "tire-change")

    def test_exhausted_search_slots_report_busy_without_blocking(self):
        search = _bare_search({"kiwix_url": "http://example/content"})
        search._search_slots = threading.BoundedSemaphore(1)
        self.assertTrue(search._search_slots.acquire(blocking=False))
        try:
            events = list(search.stream_search("anything", limit=1))
            with self.assertRaises(SearchBusyError):
                search.search_page("anything", limit=1)
        finally:
            search._search_slots.release()
            search._executor.shutdown(wait=True)
        self.assertEqual(events[-1]["type"], "error")
        self.assertTrue(events[-1]["busy"])


class QueryCacheTests(unittest.TestCase):
    def test_lru_evicts_least_recently_used(self):
        cache = QueryCache(2)
        cache.put("a", [1])
        cache.put("b", [2])
        self.assertEqual(cache.get("a"), [1])
        cache.put("c", [3])
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("c"), [3])

    def test_disabled_cache_is_a_noop(self):
        cache = QueryCache(0)
        cache.put("a", [1])
        self.assertIsNone(cache.get("a"))

    def test_byte_budget_evicts_large_entries(self):
        cache = QueryCache(100, max_bytes=2000)
        cache.put("a", "x" * 1000)
        cache.put("b", "y" * 1000)

        self.assertLessEqual(cache.stats()["bytes"], 2000)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), "y" * 1000)

    def test_byte_budget_can_be_disabled(self):
        cache = QueryCache(100, max_bytes=0)
        cache.put("a", "x" * 10_000)
        self.assertEqual(cache.get("a"), "x" * 10_000)


if __name__ == "__main__":
    unittest.main()
