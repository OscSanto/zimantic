import sqlite3
import time
import unittest

import faiss
import numpy as np

from zimantic.contracts import SourceInfo
from zimantic.indexing import schema
from zimantic.indexing.browse import rank_browse_results
from zimantic.indexing.search import SearchLimits, search
from zimantic.providers.base import SourceQuery
from zimantic.searching.index import IndexHandle


class BrowseTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(schema.SCHEMA)
        self.db.executemany(
            "INSERT INTO docs(rowid,title,path,target) VALUES (?,?,?,?)",
            [
                (2, "Common common", "common-1", None),
                (12, "Common", "common-2", None),
                (31, "Unusual", "unusual", None),
                (47, "the and", "stopwords", None),
                (100, "Redirect", "unusual", 31),
            ],
        )
        self.index = IndexHandle(self.db, None)
        self.addCleanup(self.index.close)
        self.source = SourceInfo(key="files:test", provider="files", searchable=True)

    def browse(self, mode, *, seed=0, deadline=None, count=10):
        return search(
            self.source,
            self.index,
            SourceQuery("", "", "", None, count, deadline, mode, seed),
            limits=SearchLimits(candidate_count=1, web_base_path="/zimantic"),
        )

    def test_rarity_counts_distinct_words_and_excludes_redirects_and_stopwords(self):
        statements = []
        self.db.set_trace_callback(statements.append)
        result = self.browse("rare")
        self.assertEqual(result.status, "ok", result.error)
        self.assertEqual(
            [doc["path"] for doc in result.items],
            ["unusual", "common-1", "common-2"],
        )
        self.assertEqual(result.items[1]["score"], result.items[2]["score"])
        self.assertTrue(result.items[0]["url"].startswith("/zimantic/content/"))
        self.browse("rare")
        self.assertEqual(
            sum(statement.startswith("WITH weights") for statement in statements), 1
        )
        self.index.close()
        self.assertEqual(self.index.browse_rankings, {})

    def test_rarity_uses_the_index_unicode_tokenizer(self):
        self.db.executemany(
            "INSERT INTO docs(rowid,title,path) VALUES (?,?,?)",
            [(101, "café", "accent"), (102, "cafe", "plain")],
        )
        result = self.browse("rare")
        scores = {doc["path"]: doc["score"] for doc in result.items}
        self.assertEqual(scores["accent"], scores["plain"])
        self.assertLess(scores["accent"], scores["unusual"])

    def test_random_is_repeatable_and_handles_sparse_native_ids(self):
        self.db.execute(
            "INSERT INTO docs(rowid,title,path) VALUES (?,?,?)",
            (10**12, "Sparse", "sparse"),
        )
        first = self.browse("random", seed=7)
        second = self.browse("random", seed=7)
        self.assertEqual(first.items, second.items)
        self.assertEqual(
            {doc["path"] for doc in first.items},
            {"common-1", "common-2", "unusual", "stopwords", "sparse"},
        )
        self.assertEqual(len(first.items), 5)
        self.assertNotEqual(
            [doc["id"] for doc in first.items],
            [doc["id"] for doc in self.browse("random", seed=19).items],
        )

    def test_semantic_modes_share_the_centroid_and_have_opposite_order(self):
        vectors = np.array([[1, 0], [0.8, 0.6], [-0.6, 0.8]], dtype=np.float32)
        index = faiss.IndexIDMap(faiss.IndexFlatIP(2))
        index.add_with_ids(vectors, np.array([2, 12, 31], dtype=np.int64))
        self.index.faiss_index = index
        outliers = self.browse("outliers")
        centroid = self.index.browse_centroid
        representative = self.browse("representative")
        self.assertEqual(outliers.status, "ok", outliers.error)
        self.assertEqual(representative.status, "ok", representative.error)
        self.assertIs(self.index.browse_centroid, centroid)
        self.assertEqual(outliers.items[0]["path"], "unusual")
        self.assertEqual(representative.items[0]["path"], "common-2")
        self.assertEqual(
            [doc["id"] for doc in outliers.items],
            list(reversed([doc["id"] for doc in representative.items])),
        )

    def test_ivf_centroid_supports_sparse_ids_without_creating_a_direct_map(self):
        threads = faiss.omp_get_max_threads()
        faiss.omp_set_num_threads(1)
        self.addCleanup(faiss.omp_set_num_threads, threads)
        rng = np.random.default_rng(42)
        vectors = rng.normal(size=(128, 4)).astype(np.float32)
        vectors[:, 0] += 2
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        index = faiss.index_factory(4, "IVF2,SQ8", faiss.METRIC_INNER_PRODUCT)
        index.train(vectors)
        ids = np.arange(128, dtype=np.int64) * 1000 + 1000
        index.add_with_ids(vectors, ids)
        self.db.executemany(
            "INSERT INTO docs(rowid,title,path) VALUES (?,?,?)",
            [(int(rowid), f"Item {rowid}", str(rowid)) for rowid in ids],
        )
        self.index.faiss_index = index
        result = self.browse("representative", count=4)
        self.assertEqual(result.status, "ok", result.error)
        self.assertEqual(len(result.items), 4)
        self.assertEqual(index.direct_map.type, faiss.DirectMap.NoMap)
        mean = vectors.mean(axis=0)
        mean /= np.linalg.norm(mean)
        np.testing.assert_allclose(self.index.browse_centroid[0], mean, atol=0.01)

    def test_deadlines_cancel_without_caching_an_incomplete_ranking(self):
        result = self.browse("rare", deadline=time.monotonic() - 1)
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(self.index.browse_rankings)
        self.assertEqual(self.db.execute("SELECT 1").fetchone(), (1,))

    def test_sources_are_interleaved_without_changing_their_internal_order(self):
        first = self.browse("rare")
        self.source = SourceInfo(key="files:other", provider="files", searchable=True)
        second = self.browse("rare")
        results = rank_browse_results([first, second], 4)
        self.assertEqual(len({doc["source_key"] for doc in results[:2]}), 2)
        self.assertEqual(
            [doc["source_rank"] for doc in results], [1, 1, 2, 2]
        )
        self.assertTrue(all("_browse_order" not in doc for doc in results))
