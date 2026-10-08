import sys
import types
import unittest
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import sqlite3

import numpy as np


def _install_optional_dependency_stubs():
    faiss = types.ModuleType("faiss")
    faiss.IO_FLAG_MMAP_IFC = 1
    faiss.IO_FLAG_READ_ONLY = 2

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
from zimantic.search import Search, _LocalIndex, _fulltext_query, _nprobe, _query_terms, _title_prefix_query, _title_query


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
    search.source_workers = 2
    search.max_concurrent_searches = 2
    search._search_slots = threading.BoundedSemaphore(2)
    search._executor = ThreadPoolExecutor(max_workers=2)
    search.cache = QueryCache(16)
    return search


def _title_search(rows, query, limit=1):
    """Run a title-only search (no semantic or full-text) over an in-memory FTS.

    Returns the final ranked result list, exercising the local title path in
    isolation so prefix-fallback behaviour can be asserted end to end.
    """
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute(
        "CREATE VIRTUAL TABLE docs USING fts5("
        "title, excerpt UNINDEXED, path UNINDEXED, target UNINDEXED)"
    )
    db.executemany(
        "INSERT INTO docs(rowid, title, excerpt, path, target) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    source = SourceInfo("manual", "Manual", "manual", "local", local_name="manual")
    search = _bare_search({"long_query": 10, "candidate_count": 2, "kiwix_url": "http://example/content"})
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

    def test_hybrid_ranking_is_deterministic(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        doc = {
            "id": "manual:tire",
            "source_key": "manual",
            "source": "Manual",
            "title": "Tire change",
            "lead": "Change a tire safely.",
            "path": "tire",
            "url": "http://example/tire",
            "source_rank": 1,
        }
        result = SourceResult(
            source=source,
            items=[doc],
            semantic=[(0.9, doc)],
            keyword=[(-1.0, doc)],
            fulltext=[(0, doc)],
        )
        search = object.__new__(Search)
        search.cfg = {"long_query": 10, "candidate_count": 16}
        search.candidate_count = 16
        first = search._rank_results([result], "how to change tires", 10)
        second = search._rank_results([result], "how to change tires", 10)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["id"], "manual:tire")
        self.assertIn("title 100%", first[0]["explain"])

    def test_ranking_deduplicates_redirect_hits_and_preserves_source_rank(self):
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
        search.cfg = {"long_query": 10, "candidate_count": 16}
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
        search.cfg = {"long_query": 10, "candidate_count": 16}
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
        search.cfg = {"long_query": 10, "candidate_count": 16}
        search.candidate_count = 16
        search.semantic = False
        search.indexes = {}
        search.sources = {source.key: source for source in sources}
        search._state_lock = threading.RLock()
        search._reload_lock = threading.Lock()
        search._search_slots = threading.BoundedSemaphore(1)
        search._executor = ThreadPoolExecutor(max_workers=2)
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

    def test_local_source_uses_batched_document_hydration(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, excerpt UNINDEXED, path UNINDEXED, target UNINDEXED)"
        )
        db.execute(
            "INSERT INTO docs(rowid, title, excerpt, path, target) VALUES (?, ?, ?, ?, ?)",
            (1, "Tire Change", "A useful tire change guide.", "tire-change", None),
        )

        class FakeFaiss:
            def search(self, query_vector, count):
                return np.array([[0.9]]), np.array([[1]])

        source = SourceInfo("manual", "Manual", "manual", "local", local_name="manual")
        search = object.__new__(Search)
        search.cfg = {"long_query": 10, "candidate_count": 2, "kiwix_url": "http://example/content"}
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
        search.cache = QueryCache(16)
        search.embedder = types.SimpleNamespace(embed=lambda _: np.array([[1.0]]))

        events = list(search.stream_search("how to change tires", limit=1))
        search._executor.shutdown(wait=True)
        db.close()
        result = events[-1]["results"][0]
        self.assertEqual(result["title"], "Tire Change")
        self.assertEqual(result["path"], "tire-change")

    def test_semantic_results_below_cosine_threshold_are_ignored(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, excerpt UNINDEXED, path UNINDEXED, target UNINDEXED)"
        )
        db.executemany(
            "INSERT INTO docs(rowid, title, excerpt, path, target) VALUES (?, ?, ?, ?, ?)",
            [
                (1, "Below threshold", "A weak semantic match.", "weak", None),
                (2, "Above threshold", "A strong semantic match.", "strong", None),
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

    def test_local_result_preview_is_bounded(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, excerpt UNINDEXED, path UNINDEXED, target UNINDEXED)"
        )
        db.execute(
            "INSERT INTO docs(rowid, title, excerpt, path, target) VALUES (?, ?, ?, ?, ?)",
            (1, "Long page", "one " * 400, "long-page", None),
        )

        source = SourceInfo("manual", "Manual", "manual", "local")
        search = object.__new__(Search)
        search.cfg = {
            "kiwix_url": "http://example/content",
            "max_preview_chars": 24,
        }

        try:
            doc = search._fetch_docs(source, db, {1})[1]
        finally:
            db.close()

        self.assertLessEqual(len(doc["lead"]), 24)
        self.assertTrue(doc["lead"].endswith("one"))

    def test_title_search_falls_back_to_prefix_when_exact_matches_nothing(self):
        # Exact token matching misses these because the query word is a truncated
        # or base form of the title word; only the prefix fallback surfaces them.
        cases = [
            ((1, "Tire Changes", "", "tire-changes", None), "tire change", "Tire Changes"),
            ((1, "Gettysburg Address", "", "gettysburg-address", None), "gettysburg addres", "Gettysburg Address"),
            ((1, "Collected Poems", "", "collected-poems", None), "collected poem", "Collected Poems"),
            ((1, "Declaration of Independence", "", "declaration-independence", None), "declaration independen", "Declaration of Independence"),
            ((1, "The Praise of Folly", "", "praise-of-folly", None), "praise of fol", "The Praise of Folly"),
        ]
        for row, query, expected in cases:
            with self.subTest(query=query):
                results = _title_search([row], query)
                self.assertTrue(results, f"no results for {query!r}")
                self.assertEqual(results[0]["title"], expected)


class DisambiguationRankingTests(unittest.TestCase):
    def _search(self):
        search = object.__new__(Search)
        search.cfg = {"long_query": 10, "candidate_count": 16}
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
            "kind": "disambiguation",
            "members": [{"title": "Air", "path": "Air"}],
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

    def test_exact_hub_title_query_promotes_hub_and_keeps_members(self):
        ranked = self._search()._rank_results([self._result()], "air (disambiguation)", 10)

        self.assertEqual(ranked[0]["id"], "manual:hub")
        self.assertEqual(ranked[0]["kind"], "disambiguation")
        self.assertEqual(ranked[0]["members"][0]["title"], "Air")

    def test_base_title_and_partial_queries_demote_hub(self):
        for query in ("air", "air quality"):
            with self.subTest(query=query):
                ranked = self._search()._rank_results([self._result()], query, 10)
                self.assertEqual(ranked[0]["id"], "manual:air")

    def test_member_links_add_urls_and_survive_bad_json(self):
        search = self._search()
        search.cfg["kiwix_url"] = "http://example/content"
        source = SourceInfo("manual", "Manual", "book", "local")

        links = search._member_links(source, '[{"title": "Air", "path": "Air"}]')

        self.assertEqual(links, [{
            "title": "Air",
            "path": "Air",
            "url": "http://example/content/book/Air",
        }])
        self.assertEqual(search._member_links(source, "not json"), [])


def _write_index(index_dir: Path, name: str, *, done: str = "1", rows=()):
    db = sqlite3.connect(index_dir / f"{name}.sqlite")
    db.executescript(
        "CREATE VIRTUAL TABLE docs USING fts5("
        "title, excerpt UNINDEXED, path UNINDEXED, target UNINDEXED, "
        "tokenize='unicode61 remove_diacritics 2');"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value);"
    )
    db.executemany(
        "INSERT INTO docs(rowid, title, excerpt, path, target) VALUES (?, ?, ?, ?, ?)",
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
            "long_query": 10,
            "candidate_count": 16,
        }

    def test_index_without_vectors_still_serves_title_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "A useful tire change guide.", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            self.assertIn("manual", search.indexes)
            self.assertFalse(search.indexes["manual"].semantic)

            results = search.search("tire change", limit=1)
            self.assertEqual(results[0]["title"], "Tire Change")
            self.assertEqual(results[0]["path"], "tire-change")
            search._executor.shutdown(wait=True)

    def test_finished_stream_is_cached_for_json_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "A useful tire change guide.", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            events = list(search.stream_search("tire change", limit=1))
            self.assertEqual(events[-1]["type"], "done")
            self.assertIsNotNone(search.cache.get(search._cache_key("tire change", None, 1)))

            cached = search.search("tire change", limit=1)
            self.assertEqual(cached[0]["title"], "Tire Change")
            search._executor.shutdown(wait=True)

    def test_cached_stream_replays_source_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(
                tmp,
                "manual",
                rows=[(1, "Tire Change", "A useful tire change guide.", "tire-change", None)],
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
            self.assertEqual(len(cached_events[1]["items"]), 1)
            self.assertEqual(cached_events[-1]["results"], first_events[-1]["results"])

    def test_refresh_sources_keeps_cache_when_sources_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "A useful tire change guide.", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()

            events = list(search.stream_search("tire change", limit=1))
            self.assertEqual(events[-1]["type"], "done")
            key = search._cache_key("tire change", None, 1)
            self.assertIsNotNone(search.cache.get(key))

            # A plain source refresh with no changes must keep the cached answer
            # (source discovery happens on page load without re-running searches).
            search.refresh_sources()
            self.assertIsNotNone(search.cache.get(key))

            # Adding an index changes what is searchable, so stale answers drop.
            _write_index(tmp, "second", rows=[(1, "Second page", "", "second", None)])
            search.reload()
            self.assertIsNone(search.cache.get(key))
            search._executor.shutdown(wait=True)

    def test_reload_keeps_cache_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "A useful tire change guide.", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()
            list(search.stream_search("tire change", limit=1))
            key = search._cache_key("tire change", None, 1)
            self.assertIsNotNone(search.cache.get(key))

            search.reload()
            self.assertIsNotNone(search.cache.get(key))
            search._executor.shutdown(wait=True)

    def test_reload_picks_up_and_forgets_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            search = _bare_search(self._cfg(tmp))

            self.assertEqual(search.reload()["indexes"], [])

            _write_index(tmp, "manual", done="fast", rows=[(1, "Tire Change", "", "tire-change", None)])
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
                rows=[(1, "Old title", "", "old", None)],
            )
            search = _bare_search(self._cfg(tmp))
            search._load_indexes()
            search.refresh_sources()
            old_index = search.indexes["manual"]

            _write_index(
                tmp,
                "replacement",
                rows=[(1, "New title", "", "new", None)],
            )
            (tmp / "replacement.sqlite").replace(tmp / "manual.sqlite")

            search.reload()

            self.assertIsNot(search.indexes["manual"], old_index)
            self.assertEqual(search.search("new title", limit=1)[0]["title"], "New title")
            search._close_index(search.indexes["manual"])
            search._executor.shutdown(wait=True)


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


if __name__ == "__main__":
    unittest.main()
