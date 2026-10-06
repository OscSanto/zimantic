import sys
import types
import unittest
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
    search = types.ModuleType("libzim.search")
    search.Query = object
    search.Searcher = object
    sys.modules.setdefault("faiss", faiss)
    sys.modules.setdefault("libzim", libzim)
    sys.modules.setdefault("libzim.reader", reader)
    sys.modules.setdefault("libzim.search", search)


_install_optional_dependency_stubs()

from zimantic.contracts import SourceInfo, SourceResult
from zimantic.search import Search, _LocalIndex, _keyword_query, _query_terms


class SearchContractTests(unittest.TestCase):
    def test_keyword_query_removes_filler_words_but_keeps_meaningful_terms(self):
        self.assertEqual(_keyword_query("how to change tires"), '"change" AND "tires"')
        self.assertEqual(_query_terms("how to change tires"), ["change", "tire"])

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

    def test_stream_emits_source_snapshots_and_final_results(self):
        sources = [
            SourceInfo("one", "One", "one", "local"),
            SourceInfo("two", "Two", "two", "local"),
        ]
        search = object.__new__(Search)
        search.cfg = {"long_query": 10, "candidate_count": 16}
        search.candidate_count = 16
        search.sources = {source.key: source for source in sources}
        search._search_slots = threading.BoundedSemaphore(1)
        search._executor = ThreadPoolExecutor(max_workers=2)
        search.embedder = types.SimpleNamespace(embed=lambda _: [[1.0]])

        def fake_source_search(source, query, keyword_query, query_vector, limit):
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
            "title, lead UNINDEXED, path UNINDEXED, target UNINDEXED)"
        )
        db.execute(
            "INSERT INTO docs(rowid, title, lead, path, target) VALUES (?, ?, ?, ?, ?)",
            (1, "Tire Change", "A useful tire change guide.", "tire-change", None),
        )

        class FakeFaiss:
            def search(self, query_vector, count):
                return np.array([[0.9]]), np.array([[1]])

        source = SourceInfo("manual", "Manual", "manual", "local", local_name="manual")
        search = object.__new__(Search)
        search.cfg = {"long_query": 10, "candidate_count": 2, "kiwix_url": "http://example/content"}
        search.candidate_count = 2
        search.sources = {"manual": source}
        search.indexes = {
            "manual": _LocalIndex(db, FakeFaiss(), None, None, threading.Lock())
        }
        search._search_slots = threading.BoundedSemaphore(1)
        search._executor = ThreadPoolExecutor(max_workers=1)
        search.embedder = types.SimpleNamespace(embed=lambda _: np.array([[1.0]]))

        events = list(search.stream_search("how to change tires", limit=1))
        search._executor.shutdown(wait=True)
        db.close()
        result = events[-1]["results"][0]
        self.assertEqual(result["title"], "Tire Change")
        self.assertEqual(result["path"], "tire-change")


if __name__ == "__main__":
    unittest.main()
