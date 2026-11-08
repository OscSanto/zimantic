import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

from zimantic.contracts import SourceInfo, encode_document_id
from zimantic.indexing import derived as derived_module
from zimantic.indexing import pipeline as build_module
from zimantic.indexing import schema
from zimantic.indexing.locator import IndexLocator
from zimantic.indexing.pipeline import BodyBudget, build_index
from zimantic.indexing.search import (
    AmbiguousPathError,
    SearchLimits,
    body_search,
    search,
)
from zimantic.indexing.types import EntryRecord
from zimantic.providers.base import SourceQuery
from zimantic.searching.index import IndexHandle


class Producer:
    def __init__(self, rows, *, key="files:test:source-abc", fingerprint="fp"):
        self.rows = list(rows)
        self.key = key
        self.provider = "files"
        self.collection_key = "files:test"
        self.source = _source(key)
        self.source_size = 0
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


def test_scoped_search_tuning_reaches_semantic_filter_and_ivf_probes(tmp_path):
    from zimantic.scoped_settings import search_limits

    class Vectors:
        nprobe = 1
        nlist = 8

        def search(self, query, count):
            assert self.nprobe == 3
            assert count == 4
            return [[0.7]], [[1]]

    source = _source()
    locator = _build(
        tmp_path, [EntryRecord(1, "Alpha", "excerpt", "alpha", None, "body")]
    )
    handle = _handle(locator, source)
    handle.faiss_index = Vectors()
    cfg = {
        "collection_settings": {"test": {"candidate_count": 4, "nprobe": 3}},
        "source_settings": {source.key: {"min_cosine_similarity": 0.6}},
    }
    request = SourceQuery("alpha", '"alpha"', "", [[1]], 2)
    try:
        tuned = search_limits(SearchLimits(), cfg, source, None)
        assert search(source, handle, request, limits=tuned).semantic
        assert handle.faiss_index.nprobe == 1
        tuned.min_cosine_similarity = 0.8
        assert not search(source, handle, request, limits=tuned).semantic
    finally:
        handle.close()
    with pytest.raises(ValueError):
        schema.decode_bool("true")


def test_body_search_matches_compact_index():
    with sqlite3.connect(":memory:") as db:
        schema.ensure_schema(db)
        assert "body_docsize" not in schema.table_names(db)
        db.executemany("INSERT INTO body(rowid, text) VALUES (?, ?)", [
            (9, "CAFÉ \u1ed9 zebra"),
            (2, "cafe o unrelated"),
            (1, "zebra unrelated"),
        ])
        assert body_search(db, "cafe o zebra", 16) == [(9, 1.0)]
        assert body_search(db, "zebra", 16) == [(1, 1.0), (9, 2.0)]
        assert body_search(db, "zebra", 1) == [(1, 1.0)]
        assert body_search(db, "cafe missing", 16) == []
        assert body_search(db, "", 16) == []


@pytest.mark.parametrize("invalid", ["version", "fingerprint", "size", "positional", "identity"])
def test_invalid_published_indexes_are_rejected_and_rebuilt(tmp_path, invalid):
    from zimantic.providers.files.discovery import FilesIndexOptions, open_files_index

    rows = [EntryRecord(1, "Alpha", "", "alpha")]
    locator = _build(tmp_path, rows, fast=True)
    source = _source()
    lease = locator.paths(source)
    original_generation = lease.generation
    replacement = tmp_path / "replacement.sqlite"
    try:
        with (
            closing(sqlite3.connect(lease.sqlite_path)) as old,
            closing(sqlite3.connect(replacement)) as db,
            old,
            db,
        ):
            old.backup(db)
            if invalid == "version":
                schema.write_meta(db, {schema.SCHEMA_VERSION: str(schema.CURRENT_SCHEMA_VERSION - 1)})
            elif invalid == "fingerprint":
                schema.delete_meta(db, [schema.SOURCE_FINGERPRINT])
            elif invalid == "size":
                schema.write_meta(db, {schema.FILESIZE: "unknown"})
            elif invalid == "identity":
                schema.write_meta(db, {schema.SOURCE_KEY: "files:other:source"})
            else:
                db.execute("DROP TABLE body")
                db.execute(
                    "CREATE VIRTUAL TABLE body USING fts5("
                    "text, content='', tokenize='unicode61 remove_diacritics 2')"
                )
    finally:
        lease.close()
    locator.publish(source, replacement).close()

    assert open_files_index(
        source, index_dir=tmp_path, options=FilesIndexOptions()
    ) is None
    report = build_index(Producer(rows), locator, None, {}, fast=True)
    assert report.status == "published"
    handle = open_files_index(
        source, index_dir=tmp_path, options=FilesIndexOptions()
    )
    assert handle is not None
    try:
        assert handle.generation != original_generation
    finally:
        handle.close()


@pytest.mark.parametrize("invalid", ["version", "missing-version", "resume-position", "identity"])
def test_obsolete_or_invalid_staging_restarts_without_resuming(tmp_path, invalid):
    rows = [EntryRecord(i, f"Title {i}", "", f"path-{i}") for i in range(4)]

    class RecordingProducer(Producer):
        def __init__(self, *, interrupt=False):
            super().__init__(rows)
            self.interrupt = interrupt
            self.visited = []

        def entry_at(self, index):
            self.visited.append(index)
            if self.interrupt and index == 2:
                raise KeyboardInterrupt
            return super().entry_at(index)

    locator = IndexLocator(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        build_index(RecordingProducer(interrupt=True), locator, None, {"batch_size": 1}, fast=True)
    source = _source()
    staging = locator.source_directory(
        source.provider, source.collection_key, source.key
    ) / "staging-fast.sqlite"
    with closing(sqlite3.connect(staging)) as db, db:
        if invalid == "version":
            schema.write_meta(db, {schema.SCHEMA_VERSION: str(schema.CURRENT_SCHEMA_VERSION - 1)})
        elif invalid == "missing-version":
            schema.delete_meta(db, [schema.SCHEMA_VERSION])
        elif invalid == "identity":
            schema.write_meta(db, {schema.SOURCE_KEY: "files:other:source"})
        else:
            schema.write_meta(db, {schema.NEXT: "02"})

    producer = RecordingProducer()
    build_index(producer, locator, None, {"batch_size": 1}, fast=True)
    assert producer.visited == [0, 1, 2, 3]


@pytest.mark.parametrize("matching", [False, True])
def test_files_cross_model_probes_reuse_only_matching_vectors(tmp_path, monkeypatch, matching):
    import threading

    import faiss
    import numpy as np

    from zimantic.providers.files.discovery import FilesIndexOptions, open_files_index
    from zimantic.providers.files.provider import FilesRuntime

    class Embedder:
        fingerprint = "old-model"

        def __init__(self, vector):
            self.vector = vector

        def truncate(self, text, prefix=""):
            return text

        def embed(self, texts):
            return np.tile(np.array(self.vector, dtype=np.float32), (len(texts), 1))

        def encode(self, texts):
            return [list(map(ord, text)) for text in texts]

        def embed_encoded(self, inputs):
            return self.embed(inputs)

    monkeypatch.setattr(build_module, "_write_faiss", lambda db, path: path.write_bytes(b"vectors"))
    locator = _build(
        tmp_path, [EntryRecord(1, "Alpha", "excerpt", "alpha", None, "body")],
        embedder=Embedder([1, 0, 0, 0]),
    )
    runtime = object.__new__(FilesRuntime)
    runtime._embedder_ready_getter = None
    runtime._embedder_getter = lambda: Embedder([1, 0, 0, 0] if matching else [0, 1, 0, 0])
    runtime._probe_cache = {}
    runtime._probe_cache_lock = threading.Lock()
    vectors = object()
    monkeypatch.setattr(faiss, "read_index", lambda *args: vectors)
    handle = open_files_index(
        _source(), index_dir=locator.index_dir,
        options=FilesIndexOptions(
            semantic=True, model_fingerprint="new-model",
            probe_compatible=runtime._probe_compatible,
        ),
    )
    assert handle is not None
    try:
        assert (handle.faiss_index is vectors) is matching
    finally:
        handle.close()


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
            assert "body_docsize" not in schema.table_names(handle.db_for_thread())
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
        initial = locator.paths(_source())
        assert initial is not None
        generation = initial.generation
        initial.close()
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
                "SELECT rowid FROM body WHERE body MATCH '\"Caption\"'"
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


def test_derived_overrides_reach_engines_and_separate_cache_entries(tmp_path):
    cache = derived_module.DerivedTextCache(tmp_path)
    calls = []

    def engine(data, **options):
        calls.append(options)
        return options["ocr_languages"][0]

    def stage(language, **options):
        return derived_module.DerivedTextStage(
            derived_module.DerivedTextConfig(task_timeout_seconds=60),
            operations=derived_module.SourceDerivedOperations(
                ocr="tesseract", options={"ocr_languages": (language,), **options}
            ),
            cache=cache,
            engines={"ocr": engine},
        )

    french = stage("fre", task_timeout_seconds=5)
    german = stage("deu")
    for current, expected in [(french, "fre"), (german, "deu"), (french, "fre")]:
        result = current.process(0, "", [_Input("image", "same image")])
        assert result.contributions[0].text == expected
    assert len(calls) == 2
    assert calls[0]["task_timeout_seconds"] == 5
    assert calls[1]["task_timeout_seconds"] == 60
    uncached = stage("fre", task_timeout_seconds=5, cache=False)
    result = uncached.process(0, "", [_Input("image", "same image")])
    assert len(calls) == 3
    assert result.contributions[0].cache_key is None


def test_derived_override_caps_are_enforced_before_and_after_extraction():
    calls = []

    def engine(data, **options):
        calls.append(data)
        return "long output"

    stage = derived_module.DerivedTextStage(
        operations=derived_module.SourceDerivedOperations(
            ocr="tesseract", options={"max_spool_bytes": 4, "max_ocr_output_bytes": 3}
        ),
        engines={"ocr": engine},
    )
    oversized = stage.process(0, "", [_Input("image", "too large")])
    assert oversized.states["ocr"] == "partial"
    assert calls == []
    bounded = stage.process(0, "", [_Input("image", "fits")])
    assert bounded.contributions[0].text == "lon"
    assert calls == [b"fits"]


def test_derived_config_and_workers_are_validated():
    config = derived_module.parse_derived_text(
        {
            "max_caption_bytes": 10,
            "workers": 2,
            "ocr_languages": ["eng"],
            "asr_engine": "faster-whisper",
            "asr_threads": 4,
            "asr_batched": True,
        }
    )
    assert config.workers == 2
    assert config.asr_engine == "faster-whisper"
    assert config.asr_threads == 4
    assert config.asr_batched is True
    with pytest.raises(ValueError):
        derived_module.parse_derived_text({"workers": True})
    with pytest.raises(ValueError):
        derived_module.parse_derived_text({"asr_engine": "not-an-engine"})
    with pytest.raises(ValueError):
        derived_module.parse_derived_text({"asr_threads": -1})
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
