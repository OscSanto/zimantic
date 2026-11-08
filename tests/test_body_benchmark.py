import sqlite3

import pytest

from scripts.bench_body_tradeoffs import (
    bm25_search, body_query, compact_search, compare, summary, variant_patches,
)
from zimantic.content import reader
from zimantic.indexing import pipeline, schema
from zimantic.indexing.search import body_search
from zimantic.indexing.types import EntryRecord


@pytest.mark.parametrize("variant", ["head", "compact", "stored", "stored-default", "native-only"])
def test_benchmark_variant_preserves_docs_and_restores_schema(variant):
    original_schema = schema.SCHEMA
    original_extract = reader.extract_body
    db = sqlite3.connect(":memory:")
    row = EntryRecord(7, "A title", "excerpt", "article", None, "late café zebra")
    try:
        with variant_patches(variant):
            schema.ensure_schema(db)
            schema.validate_schema(db)
            pipeline._save_rows(db, [row], 1, {})
            assert db.execute(
                "SELECT rowid, title, path, target FROM docs"
            ).fetchall() == [(7, "A title", "article", None)]
            if variant == "native-only":
                assert not any(
                    name.startswith("body") for name in schema.table_names(db)
                )
                assert reader.extract_body(b"ignored") == ("", False)
            else:
                if variant == "compact":
                    assert "body_docsize" not in schema.table_names(db)
                else:
                    assert "body_docsize" in schema.table_names(db)
                search = compact_search if variant == "compact" else body_search
                assert search(db, "cafe zebra", 16) == [(7, 1.0)]
                assert search(db, "missing", 16) == []
                assert search(db, "", 16) == []
        assert schema.SCHEMA == original_schema
        assert reader.extract_body is original_extract
    finally:
        db.close()


def test_benchmark_overlap_handles_empty_results():
    reference = {"helper": {"results": [
        {"query": "missing", "fulltext": [], "items": []},
        {"query": "needle", "fulltext": ["a"], "items": ["a"], "target": "a"},
    ]}}
    candidate = {"helper": {"results": [
        {"query": "missing", "fulltext": [], "items": []},
        {"query": "needle", "fulltext": [], "items": [], "target": "a"},
    ]}}
    comparison = compare(reference, candidate)
    assert compare(reference, reference)["mean_fulltext_top10_jaccard"] == 1.0
    assert comparison["mean_fulltext_top10_jaccard"] == 0.5
    assert comparison["lost_fulltext_queries"] == ["needle"]
    assert comparison["head_target_hits"] == 1
    assert comparison["candidate_target_hits"] == 0


def test_benchmark_p95_for_small_trial_counts():
    assert summary([1.0, 2.0])["p95"] == 2.0


@pytest.mark.parametrize("variant", ["head-bm25", "stored-bm25"])
def test_bm25_ranks_relevance_not_document_id(variant):
    with sqlite3.connect(":memory:") as db, variant_patches(variant):
        schema.ensure_schema(db)
        db.executemany("INSERT INTO body(rowid,text) VALUES (?,?)", [
            (1, "needle " + "filler " * 100),
            (2, "needle needle needle"),
        ])
        assert [rowid for rowid, _ in bm25_search(db, "needle", 16)] == [2, 1]
        assert bm25_search(db, "", 16) == []


@pytest.mark.parametrize("variant", ["compact", "stored", "stored-default"])
def test_only_stored_content_retains_raw_body(variant):
    with sqlite3.connect(":memory:") as db, variant_patches(variant):
        schema.ensure_schema(db)
        db.execute("INSERT INTO body(rowid,text) VALUES (1,'searchable raw text')")
        text = db.execute(
            "SELECT text FROM body WHERE body MATCH 'searchable'"
        ).fetchone()[0]
        assert text == (None if variant == "compact" else "searchable raw text")


@pytest.mark.parametrize("variant,text,query", [
    ("compact-porter", "corrected", "correction"),
    ("trigram", "bookkeeping", "keep"),
])
def test_tokenizer_variant_uses_compatible_index_detail(variant, text, query):
    with sqlite3.connect(":memory:") as db, variant_patches(variant):
        schema.ensure_schema(db)
        db.execute("INSERT INTO body(rowid,text) VALUES (1,?)", (text,))
        assert body_query(variant)(db, query, 16)[0][0] == 1
