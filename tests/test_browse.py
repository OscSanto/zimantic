import sqlite3
import time
import unittest

import faiss
import numpy as np

from zimantic.contracts import SourceInfo
from zimantic.indexing import schema
from zimantic.indexing.browse import diversity_vectors, rank_browse_results, sort_scores
from zimantic.indexing.search import SearchLimits, search
from zimantic.providers.base import SourceQuery
from zimantic.searching.diversity import diverse_order
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

    def test_matched_title_rarity_is_scored_even_outside_the_browse_sample(self):
        self.browse("rare", count=1)
        statements = []
        self.db.set_trace_callback(statements.append)
        scores = sort_scores(self.index, [
            {"path": "common-1", "_rowid": 2},
            {"path": "common-2", "_rowid": 12},
            {"path": "unusual", "_rowid": 31},
        ], "rare")
        self.assertEqual(set(scores), {"common-1", "common-2", "unusual"})
        self.assertEqual(scores["common-1"], scores["common-2"])
        self.assertGreater(scores["unusual"], scores["common-1"])
        self.assertFalse(any("WHERE path IN" in sql for sql in statements))
        self.assertEqual(sort_scores(self.index, [{"path": "missing"}], "rare"), {})

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

    def test_recency_orders_newest_items_before_undated_items(self):
        self.db.executemany(
            "UPDATE docs SET modified = ? WHERE path = ?",
            [(100, "common-1"), (300, "unusual"), (200, "stopwords")],
        )
        result = self.browse("recency")
        self.assertEqual(
            [doc["path"] for doc in result.items],
            ["unusual", "stopwords", "common-1", "common-2"],
        )

    def test_diversity_greedily_selects_titles_with_less_word_overlap(self):
        result = self.browse("diverse")
        self.assertEqual(result.status, "ok", result.error)
        paths = [doc["path"] for doc in result.items]
        self.assertEqual(set(paths), {"common-1", "common-2", "unusual", "stopwords"})
        self.assertEqual(paths[-1], "stopwords")
        self.assertIn("unusual", paths[:2])

    def test_diversity_keeps_relevance_ties_instead_of_favoring_long_titles(self):
        order = diverse_order([
            "Solar power", "Solar power technology panel installation guide",
            "Wind turbines", "the and", "",
        ])
        self.assertEqual(order, [0, 2, 1, 3, 4])
        self.assertEqual(diverse_order(["solar", "wind"], limit=1), [0])
        with self.assertRaises(TimeoutError):
            diverse_order(["solar"], time.monotonic() - 1)

    def test_diversity_balances_sources_and_checks_overlap_across_them(self):
        order = diverse_order(
            ["Solar power", "Solar power", "Wind turbines", "Battery storage"],
            sources=["a", "b", "b", "a"],
        )
        self.assertEqual(order, [0, 2, 3, 1])

    def test_semantic_diversity_recognizes_synonyms_without_promoting_outliers(self):
        titles = ["Cars", "Automobiles", "Wind turbines", "Unrelated"]
        vectors = np.array([[1, 0], [1, 0], [0.8, 0.6], [-1, 0]], dtype=np.float32)
        order = diverse_order(titles, vectors=vectors, relevance=[1, 1, 0.8, -1])
        self.assertEqual(order, [0, 2, 1, 3])
        self.assertEqual(diverse_order(titles), [0, 1, 2, 3])

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
        paths = [str(ids[0]), str(ids[32]), str(ids[100])]
        docs = [{"path": path} for path in paths]
        scores = sort_scores(self.index, docs, "representative")
        self.assertEqual(set(scores), set(paths))
        opposite = sort_scores(self.index, docs, "outliers")
        for path in paths:
            self.assertAlmostEqual(scores[path], -opposite[path], places=5)
        selected, centrality = diversity_vectors(self.index, docs)
        self.assertEqual(set(selected), set(paths))
        for path, vector in selected.items():
            np.testing.assert_allclose(vector, vectors[list(ids).index(int(path))], atol=0.01)
            self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=5)
            self.assertAlmostEqual(centrality[path], scores[path], delta=0.01)
        semantic = self.browse("semantic-diverse", count=4)
        self.assertEqual(semantic.status, "ok", semantic.error)
        self.assertEqual(len(semantic.items), 4)
        self.assertEqual(index.direct_map.type, faiss.DirectMap.NoMap)

    def test_candidate_vectors_support_idmaps_missing_and_zero_embeddings(self):
        vectors = np.array([[1, 0], [0, 0], [0.8, 0.6]], dtype=np.float32)
        index = faiss.IndexIDMap(faiss.IndexFlatIP(2))
        index.add_with_ids(vectors, np.array([2, 12, 31], dtype=np.int64))
        self.index.faiss_index = index
        selected, centrality = diversity_vectors(self.index, [
            {"path": "common-1", "_rowid": 2}, {"path": "common-2"},
            {"path": "unusual"}, {"path": "missing"}, {"path": "stopwords"},
        ])
        self.assertEqual(set(selected), {"common-1", "unusual"})
        self.assertEqual(set(centrality), set(selected))
        np.testing.assert_allclose(selected["unusual"], vectors[2])
        with self.assertRaises(TimeoutError):
            diversity_vectors(self.index, [{"path": "unusual"}], time.monotonic() - 1)
        self.assertEqual(self.db.execute("SELECT 1").fetchone(), (1,))

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
