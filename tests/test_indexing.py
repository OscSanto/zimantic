import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from zimantic.contracts import SourceInfo, encode_document_id
from zimantic.indexing import derived as derived_module
from zimantic.indexing import schema
from zimantic.indexing.migrate import migrate_legacy_index
from zimantic.indexing.pipeline import BodyBudget, build_index
from zimantic.indexing import pipeline as build_module
from zimantic.indexing.search import AmbiguousPathError, SearchLimits, search
from zimantic.indexing.types import EntryRecord
from zimantic.indexes import IndexLocator
from zimantic.providers.base import SourceQuery
from zimantic.searching.index import IndexHandle


class Producer:
    def __init__(self, rows, *, key="files:test:source-abc", fingerprint="fp"):
        self.rows = list(rows)
        self.key = key
        self.provider = "files"
        self.collection_key = "files:test"
        self.fingerprint = fingerprint
        self.count = len(self.rows)
        self.truncated = False
        self.closed = False

    def entry_at(self, index):
        if 0 <= index < self.count:
            return self.rows[index]
        return None

    def validate_snapshot(self):
        return True

    def close(self):
        self.closed = True


def _source(key="files:test:source-abc"):
    return SourceInfo(key, provider="files", title="Files", collection_key="files:test")


def _build(tmp, rows, *, fast=False, embedder=None, extra=None):
    cfg = {"batch_size": 1}
    cfg.update(extra or {})
    build_index(
        Producer(rows),
        IndexLocator(tmp),
        embedder,
        cfg,
        fast=fast,
    )
    return IndexLocator(tmp)


def _handle(locator, source, *, read_only=True):
    lease = locator.paths(source)
    assert lease is not None
    db_path = lease.sqlite_path.resolve().as_uri() + "?mode=ro"
    return IndexHandle(
        None,
        None,
        db_path=db_path,
        generation=lease.generation,
        lease=lease,
        read_only=read_only,
    )


def test_entry_record_has_body_and_target_contract():
    row = EntryRecord(
        0, "Title", "excerpt", "path", 7, "full body", True
    )
    assert row.id == 0
    assert row.body == "full body"
    assert row.body_truncated is True
    assert row[0] == 0


def test_meta_codec_is_type_stable():
    assert schema.encode_int(3) == "3"
    assert schema.decode_int("3") == 3
    assert schema.encode_bool(True) == "1"
    assert schema.decode_bool("0") is False
    assert schema.decode_channels(schema.encode_channels(["semantic", "title"])) == (
        "title",
        "semantic",
    )
    with pytest.raises(ValueError):
        schema.decode_int(3)
    with pytest.raises(ValueError):
        schema.decode_bool("true")


def test_generic_search_finds_title_and_body_without_embeddings_or_native():
    body = ("filler " * 300) + "latebodyterm"
    rows = [
        EntryRecord(1, "Tire Change", "short", "tire", None, "short body"),
        EntryRecord(2, "Long Article", "short", "long", None, body),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        locator = _build(tmp, rows)
        source = _source()
        handle = _handle(locator, source)
        try:
            result = search(
                source,
                handle,
                SourceQuery(
                    query="latebodyterm",
                    title_query="",
                    fulltext_query="latebodyterm",
                    query_vector=None,
                    limit=5,
                ),
                limits=SearchLimits(web_base_path=""),
            )
            assert result.status == "ok"
            assert [doc["title"] for _rank, doc in result.fulltext] == ["Long Article"]
            assert [rank for rank, _doc in result.fulltext] == [1]

            title = search(
                source,
                handle,
                SourceQuery(
                    query="tire change",
                    title_query='"tire" AND "change"',
                    fulltext_query="tire change",
                    query_vector=None,
                    limit=5,
                ),
                limits=SearchLimits(),
            )
            assert any(doc["title"] == "Tire Change" for _score, doc in title.keyword)
        finally:
            handle.close()


def test_ambiguous_same_source_paths_are_omitted_and_reported():
    rows = [
        EntryRecord(1, "One", "e", "same", None, "body"),
        EntryRecord(2, "Two", "e", "same", None, "body"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        locator = _build(tmp, rows)
        source = _source()
        handle = _handle(locator, source)
        try:
            result = search(
                source,
                handle,
                SourceQuery(
                    query="one",
                    title_query='"one" OR "two"',
                    fulltext_query="",
                    query_vector=None,
                    limit=5,
                ),
                limits=SearchLimits(),
            )
            assert result.items == []
            assert result.status == "partial"
            assert "ambiguous" in (result.error or "")
        finally:
            handle.close()


def test_generic_search_fuses_native_and_body_hits_with_integer_ranks():
    rows = [
        EntryRecord(1, "Alpha", "e", "alpha", None, "needle body"),
        EntryRecord(2, "Beta", "e", "beta", None, "other body"),
    ]

    class Native:
        def search(self, query, limit, *, deadline=None):
            return [{"path": "beta", "title": "Beta"}, {"path": "alpha", "title": "Alpha"}]

        def close(self):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        locator = _build(tmp, rows)
        source = _source()
        handle = _handle(locator, source)
        try:
            result = search(
                source,
                handle,
                SourceQuery(
                    query="needle",
                    title_query="",
                    fulltext_query="needle",
                    query_vector=None,
                    limit=5,
                ),
                limits=SearchLimits(),
                native=Native(),
            )
            ranks = [rank for rank, _doc in result.fulltext]
            assert ranks == list(range(1, len(ranks) + 1))
            assert {doc["path"] for _rank, doc in result.fulltext} == {
                "alpha",
                "beta",
            }
        finally:
            handle.close()


def test_ambiguous_native_path_is_omitted_and_source_is_partial():
    rows = [EntryRecord(1, "Alpha", "e", "alpha", None, "body")]
    handle = IndexHandle(None, None, db_path=None)
    try:
        result = search(
            _source(),
            handle,
            SourceQuery(
                query="alpha",
                title_query="",
                fulltext_query="alpha",
                query_vector=None,
                limit=5,
            ),
            native=_AmbiguousNative(),
            limits=SearchLimits(),
            url_builder=lambda _source, _path: (_ for _ in ()).throw(
                AmbiguousPathError("two items share this path")
            ),
        )
        assert result.status == "partial"
        assert result.fulltext == []
        assert "ambiguous" in (result.error or "")
    finally:
        handle.close()


class _AmbiguousNative:
    def search(self, query, limit, *, deadline=None):
        return [{"path": "same", "title": "One"}]

    def close(self):
        pass


def test_redirect_alias_collapses_to_final_target_document_id():
    rows = [
        EntryRecord(1, "Alpha", "e", "alpha", None, "body"),
        EntryRecord(2, "Alias", "", "alpha", 1, ""),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        locator = _build(tmp, rows)
        source = _source()
        handle = _handle(locator, source)
        try:
            result = search(
                source,
                handle,
                SourceQuery(
                    query="alpha",
                    title_query='"alpha"',
                    fulltext_query="",
                    query_vector=None,
                    limit=5,
                ),
                limits=SearchLimits(),
            )
            assert {item["id"] for item in result.items} == {
                encode_document_id(source.key, "alpha")
            }
        finally:
            handle.close()


def test_fast_build_has_no_body_channel():
    rows = [EntryRecord(1, "Alpha", "e", "alpha", None, "hidden body")]
    with tempfile.TemporaryDirectory() as tmp:
        locator = _build(tmp, rows, fast=True)
        source = _source()
        handle = _handle(locator, source)
        try:
            result = search(
                source,
                handle,
                SourceQuery(
                    query="hidden",
                    title_query="",
                    fulltext_query="hidden",
                    query_vector=None,
                    limit=5,
                ),
                limits=SearchLimits(),
            )
            assert result.fulltext == []
        finally:
            handle.close()


def test_partial_enrichment_publishes_then_retries_on_next_build():
    class _DProducer(Producer):
        def derived_text_inputs(self, index):
            return [_Input("image", "ocr output")]

    operations = derived_module.SourceDerivedOperations(ocr="tesseract")
    source_key = "files:test:source-abc"

    class _MissingStage(derived_module.DerivedTextStage):
        def __init__(self):
            super().__init__(operations=operations, engines={})

    with tempfile.TemporaryDirectory() as tmp:
        rows = [EntryRecord(1, "Alpha", "e", "alpha", None, "native")]
        locator = IndexLocator(tmp)
        with pytest.raises(build_module.PartialEnrichmentError):
            build_index(
                _DProducer(rows),
                locator,
                None,
                {
                    "batch_size": 1,
                    "derived_stage": _MissingStage(),
                    "derived_source_operations": {source_key: operations},
                },
            )
        generation = locator.paths(_source()).generation
        lease = locator.paths(_source())
        assert lease is not None
        db = sqlite3.connect(lease.sqlite_path)
        provenance = __import__("json").loads(
            dict(db.execute("SELECT key, value FROM meta"))["derived_text"]
        )
        db.close()
        lease.close()
        assert provenance["partial"] is True

        good_stage = derived_module.DerivedTextStage(
            operations=operations,
            engines={"ocr": lambda data, **options: "ocr output"},
        )
        build_index(
            _DProducer(rows),
            locator,
            None,
            {
                "batch_size": 1,
                "derived_stage": good_stage,
                "derived_source_operations": {source_key: operations},
            },
        )
        second = locator.paths(_source())
        assert second is not None
        assert second.generation != generation
        db = sqlite3.connect(second.sqlite_path)
        provenance = __import__("json").loads(
            dict(db.execute("SELECT key, value FROM meta"))["derived_text"]
        )
        db.close()
        second.close()
        assert provenance["partial"] is False
        assert provenance["operations"]["ocr"] == "ready"


def test_derived_stage_writes_cache_references_and_provenance():
    with tempfile.TemporaryDirectory() as tmp:
        cache = derived_module.DerivedTextCache(Path(tmp) / "cache")
        candidate = _Input("captions", "Caption text")
        stage = derived_module.DerivedTextStage(
            operations=derived_module.SourceDerivedOperations(captions=True),
            cache=cache,
            engines={"captions": lambda data, **options: data.decode()},
        )

        class _DProducer(Producer):
            def derived_text_inputs(self, index):
                return [candidate]

        rows = [EntryRecord(1, "Alpha", "e", "alpha", None, "native body")]
        source_key = "files:test:source-abc"
        build_index(
            _DProducer(rows),
            IndexLocator(tmp),
            None,
            {
                "batch_size": 1,
                "derived_stage": stage,
                "derived_source_operations": {
                    source_key: derived_module.SourceDerivedOperations(
                        captions=True
                    )
                },
            },
        )
        locator = IndexLocator(tmp)
        lease = locator.paths(_source())
        assert lease is not None
        db = sqlite3.connect(lease.sqlite_path)
        try:
            ref = db.execute(
                "SELECT rowid, operation, contribution_order, cache_key "
                "FROM derived_refs"
            ).fetchone()
            assert ref[0] == 1
            assert ref[1] == "captions"
            assert ref[2] == 0
            assert ref[3]
            meta = dict(db.execute("SELECT key, value FROM meta"))
            provenance = __import__("json").loads(meta["derived_text"])
            assert provenance["operations"]["captions"] == "ready"
            body = db.execute(
                "SELECT rowid FROM body WHERE text MATCH '\"Caption\"'"
            ).fetchall()
            assert body == [(1,)]
        finally:
            db.close()
            lease.close()


def test_body_budget_released_by_build():
    budget = BodyBudget(8)
    rows = [EntryRecord(1, "A", "e", "a", None, "12345678")]
    with tempfile.TemporaryDirectory() as tmp:
        _build(tmp, rows, extra={"body_budget": budget})
    assert budget.reserved == 0


# -- migration -------------------------------------------------------------


def _legacy_index(
    path: Path,
    *,
    done=1,
    excerpt=True,
    rows=((1, "Alpha", "alpha", None, "stored excerpt"),),
):
    db = sqlite3.connect(path)
    columns = "title, path UNINDEXED, target UNINDEXED"
    if excerpt:
        columns += ", excerpt UNINDEXED"
    db.executescript(
        f"CREATE VIRTUAL TABLE docs USING fts5({columns});"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value);"
    )
    for row in rows:
        db.execute(
            "INSERT INTO docs(rowid, title, path, target, excerpt) VALUES (?, ?, ?, ?, ?)",
            row,
        )
    db.execute("INSERT INTO meta VALUES ('done', ?)", (done,))
    db.execute("INSERT INTO meta VALUES ('filesize', '123')")
    db.commit()
    db.close()


def test_legacy_integer_done_migrates_and_preserves_rowids():
    with tempfile.TemporaryDirectory() as tmp:
        legacy = Path(tmp) / "old.sqlite"
        _legacy_index(legacy, done=1)
        result = migrate_legacy_index(
            legacy,
            source_size=123,
            source_uuid="abc",
            provenance={"source_size": 123, "source_uuid": "abc"},
        )
        assert result.adopted
        assert result.done == "full"
        db = sqlite3.connect(result.sqlite_path)
        try:
            assert db.execute("SELECT rowid, title FROM docs").fetchall() == [
                (1, "Alpha")
            ]
            assert db.execute(
                "SELECT rowid FROM body WHERE text MATCH '\"stored\"'"
            ).fetchall() == [(1,)]
            meta = dict(db.execute("SELECT key, value FROM meta"))
            assert meta["done"] == "full"
            assert "body_truncated_count" not in meta
            assert meta["indexed_channels"] == '["title","body"]'
        finally:
            db.close()
        # The legacy file is never modified.
        assert legacy.exists()


def test_legacy_size_only_or_incomplete_provenance_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        legacy = Path(tmp) / "old.sqlite"
        _legacy_index(legacy)
        result = migrate_legacy_index(legacy, source_size=123)
        assert not result.adopted
        assert "provenance" in result.reason
        mismatch = migrate_legacy_index(
            legacy,
            source_size=123,
            source_uuid="abc",
            provenance={"source_size": 999, "source_uuid": "abc"},
        )
        assert not mismatch.adopted


def test_legacy_incomplete_done_marker_is_never_adopted():
    with tempfile.TemporaryDirectory() as tmp:
        legacy = Path(tmp) / "old.sqlite"
        _legacy_index(legacy, done=0)
        result = migrate_legacy_index(
            legacy,
            source_size=123,
            source_uuid="abc",
            provenance={"source_size": 123, "source_uuid": "abc"},
        )
        assert not result.adopted
        assert "completion marker" in result.reason


# -- derived text ----------------------------------------------------------


class _Input:
    def __init__(self, kind, text, mimetype="text/plain"):
        self.kind = kind
        self.data = text.encode()
        self.mimetype = mimetype
        self.size = len(self.data)
        self.opened = 0

    def open(self):
        self.opened += 1
        import io

        return io.BytesIO(self.data)


def test_derived_off_by_default_never_reads_inputs():
    candidate = _Input("captions", "hello")
    stage = derived_module.DerivedTextStage()
    result = stage.process(0, "native", [candidate])
    assert result.states == {"captions": "off", "ocr": "off", "asr": "off"}
    assert candidate.opened == 0
    assert result.contributions == []


def test_derived_captions_and_composition_order_without_duplicates():
    candidate = _Input("captions", "Caption text")
    stage = derived_module.DerivedTextStage(
        operations=derived_module.SourceDerivedOperations(captions=True),
        engines={"captions": lambda data, **options: data.decode()},
    )
    result = stage.process(0, "Native text", [candidate])
    assert result.states["captions"] == "ready"
    body, truncated, used = derived_module.compose_body(
        "Native text",
        [("ocr", "Caption text"), ("captions", "Caption text")],
        1024,
    )
    assert body == "Native text\n\nCaption text"
    assert used == ("captions",)
    assert truncated is False


def test_usable_caption_text_suppresses_asr_regardless_of_input_order():
    caption = _Input("captions", "caption words")
    audio = _Input("audio", "media bytes", mimetype="audio/mpeg")
    calls = []

    def captions_engine(data, **options):
        return "caption words" if data == b"caption words" else ""

    def asr_engine(data, **options):
        calls.append(data)
        return "asr words"

    stage = derived_module.DerivedTextStage(
        operations=derived_module.SourceDerivedOperations(
            captions=True, asr="whisper"
        ),
        engines={"captions": captions_engine, "asr": asr_engine},
    )
    result = stage.process(0, "", [audio, caption])
    assert calls == []
    assert [item.operation for item in result.contributions] == ["captions"]


def test_derived_cache_reuses_complete_outputs():
    candidate = _Input("image", "ocr output")
    with tempfile.TemporaryDirectory() as tmp:
        cache = derived_module.DerivedTextCache(Path(tmp))
        calls = []

        def engine(data, **options):
            calls.append(data)
            return "ocr output"

        stage = derived_module.DerivedTextStage(
            operations=derived_module.SourceDerivedOperations(ocr="tesseract"),
            cache=cache,
            engines={"ocr": engine},
        )
        first = stage.process(0, "", [candidate])
        second = stage.process(0, "", [_Input("image", "ocr output")])
        assert first.states["ocr"] == "ready"
        assert second.states["ocr"] == "ready"
        assert len(calls) == 1


def test_full_derived_cache_omits_new_refs_but_keeps_searchable_text():
    with tempfile.TemporaryDirectory() as tmp:
        cache = derived_module.DerivedTextCache(Path(tmp) / "cache", max_bytes=1)
        stage = derived_module.DerivedTextStage(
            operations=derived_module.SourceDerivedOperations(ocr="tesseract"),
            cache=cache,
            engines={"ocr": lambda data, **options: "ocr words"},
        )
        result = stage.process(0, "", [_Input("image", "ocr words")])
        assert result.contributions
        assert result.contributions[0].cache_key is None
        body, _truncated, used = derived_module.compose_body(
            "",
            [(item.operation, item.text) for item in result.contributions],
            1024,
        )
        assert body == "ocr words"
        assert used == ("ocr",)


def test_derived_config_and_workers_are_validated():
    config = derived_module.parse_derived_text(
        {"max_caption_bytes": 10, "workers": 2, "ocr_languages": ["eng"]}
    )
    assert config.workers == 2
    with pytest.raises(ValueError):
        derived_module.parse_derived_text({"workers": True})
    assert derived_module.resolve_workers("auto", usable_cores=1, available_memory=500, per_worker_memory=1_000)[0] == 0
    workers, binding = derived_module.resolve_workers(
        "auto", usable_cores=16, available_memory=10_000, per_worker_memory=1_000
    )
    assert workers == 10
    assert binding == "memory"


def test_spooled_input_is_private_and_bounded():
    import os

    candidate = _Input("captions", "data")
    with tempfile.TemporaryDirectory() as tmp:
        path, digest, size = derived_module.spool_input(
            candidate, max_bytes=100, directory=tmp
        )
        try:
            assert os.name != "posix" or (path.stat().st_mode & 0o077) == 0
            assert size == 4
        finally:
            path.unlink(missing_ok=True)
        with pytest.raises(ValueError):
            derived_module.spool_input(candidate, max_bytes=2, directory=tmp)
