"""Provider-level ZIM discovery, identity, store, and runtime tests."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from libzim.reader import Archive
from libzim.writer import Creator, Hint, Item, StringProvider

from zimantic.catalog import SourceCatalog, catalog_path
from zimantic.providers.base import ProviderContext
from zimantic.providers.config import RuntimeConfig, ZimConfig
from zimantic.providers.zim import ZimProvider
from zimantic.providers.zim.discovery import (
    scan_collection,
    zim_collection_key,
    zim_source_key,
)

ALPHA_HTML = (
    "<html><body><p>Alpha is a sufficiently long article about tires and "
    "wheels that passes the minimum block length for indexing.</p></body></html>"
)


class _Page(Item):
    def __init__(self, path, title, html):
        super().__init__()
        self._path = path
        self._title = title
        self._html = html.encode("utf-8") if isinstance(html, str) else html

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


def _create_zim(path: Path, *, main_path: str = "Alpha") -> None:
    # libzim can leave temporary Xapian indexes behind on Windows.
    with TemporaryDirectory(dir=path.parent) as tmp:
        archive = Path(tmp) / path.name
        creator = Creator(archive)
        creator.config_indexing(True, "eng")
        creator.set_mainpath(main_path)
        with creator:
            creator.add_item(_Page("Alpha", "Alpha", ALPHA_HTML))
            creator.add_item(_Page("Beta", "Beta", ALPHA_HTML))
        del creator
        archive.replace(path)


def test_create_zim_cleans_up_writer_artifacts(tmp_path, monkeypatch):
    real_creator = Creator

    class CreatorWithArtifacts:
        def __init__(self, path):
            self.path = path
            self.creator = real_creator(path)

        def __getattr__(self, name):
            return getattr(self.creator, name)

        def __enter__(self):
            return self.creator.__enter__()

        def __exit__(self, *args):
            result = self.creator.__exit__(*args)
            for suffix in ("_fulltext.idx.tmp", "_title.idx.tmp"):
                directory = Path(str(self.path) + suffix)
                directory.mkdir(exist_ok=True)
                (directory / "docdata.glass").write_bytes(b"temporary index")
            return result

    monkeypatch.setattr(__name__ + ".Creator", CreatorWithArtifacts)
    root = tmp_path / "archives"
    root.mkdir()
    path = root / "book.zim"
    _create_zim(path)
    assert list(root.iterdir()) == [path]
    assert Archive(path).has_fulltext_index


@pytest.mark.skipif(not hasattr(Archive, "iter_efficient"), reason="requires libzim 4.1")
def test_native_order_preserves_ids_redirects_and_original_entry_cap(tmp_path):
    from types import SimpleNamespace

    from zimantic.providers.zim.entries import ArchiveEntryProducer

    path = tmp_path / "ordered.zim"
    with Creator(path) as creator:
        creator.add_item(_Page("Zulu", "Zulu", ALPHA_HTML))
        creator.add_item(_Page("Beta", "Beta", ALPHA_HTML))
        creator.add_redirection("Alias", "Alias", "Zulu", {})
    source = SimpleNamespace(key="ordered", provider="zim", collection_key="zim:test")
    producer = ArchiveEntryProducer(source, path)
    expected = [producer.entry_at(i) for i in range(producer.count)]
    try:
        assert producer.prepare_iteration() == "efficient-v1"
        assert producer.article_work_count == 2
        fingerprint = producer.order_fingerprint
        actual = [producer.entry_at(i) for i in range(producer.count)]
        assert sorted(actual, key=lambda row: row.id) == sorted(expected, key=lambda row: row.id)
        assert actual[-1].title
        assert actual[-1].target_id == actual[0].id or actual[-1].target_id == actual[1].id
        assert producer.prepare_iteration("efficient-v1") == "efficient-v1"
        assert producer.order_fingerprint == fingerprint
    finally:
        producer.close()
    capped = ArchiveEntryProducer(source, path, max_entries=2)
    try:
        capped_expected = [capped.entry_at(i) for i in range(capped.count)]
        capped.prepare_iteration()
        assert sorted(
            (capped.entry_at(i) for i in range(capped.count)), key=lambda row: row.id
        ) == capped_expected
    finally:
        capped.close()


def test_new_builds_require_the_efficient_iterator(tmp_path):
    from types import SimpleNamespace

    from zimantic.providers.zim.entries import ArchiveEntryProducer

    class _NoEfficientIterator:
        """A pre-4.1 reader surface, used only to check the error path."""

        def __init__(self, path):
            self._archive = Archive(path)
            self.entry_count = self._archive.entry_count

        def get_entry_by_id(self, entry_id):
            return self._archive.get_entry_by_id(entry_id)

    path = tmp_path / "legacy.zim"
    _create_zim(path)
    source = SimpleNamespace(key="legacy", provider="zim", collection_key="zim:test")
    producer = ArchiveEntryProducer(
        source, path, archive=_NoEfficientIterator(path), fast=True
    )
    try:
        with pytest.raises(RuntimeError, match="python-libzim 4.1"):
            producer.prepare_iteration()
    finally:
        producer.close()


def _context(index_dir: Path, *, archive_factory=Archive) -> ProviderContext:
    return ProviderContext(
        config=RuntimeConfig(),
        index_dir=index_dir,
        web_base_path="",
        logger=logging.getLogger("zimantic.tests"),
        archive_factory=archive_factory,
    )


def _open(tmp_path: Path, root: Path, *, index_dir: Path | None = None):
    config = ZimConfig(name="library", path=str(root))
    provider = ZimProvider()
    batch = provider.discover(
        config, _context(index_dir or tmp_path / "indexes")
    )
    collection = batch.collections[0]
    runtime = batch.open(collection)
    return config, batch, runtime


def test_runtime_borrows_pool_without_closing_or_resetting_other_sources(tmp_path):
    from unittest.mock import Mock

    _create_zim(tmp_path / "manual.zim")
    _config, batch, runtime = _open(tmp_path, tmp_path / "manual.zim")
    pool = Mock()
    runtime.set_fulltext_pool(pool)
    try:
        runtime.refresh(wait=True)
        pool.clear_broken.assert_not_called()
        runtime.close()
        pool.close.assert_not_called()
    finally:
        runtime.close()
        batch.close()


def test_source_key_uses_basename_for_a_single_file_collection(tmp_path):
    zims = tmp_path / "zims"
    zims.mkdir()
    archive = zims / "manual.zim"
    _create_zim(archive)
    _config, batch, runtime = _open(tmp_path, archive)
    try:
        expected = zim_source_key("library", "manual.zim")
        assert [source.key for source in runtime.library_sources()] == [expected]
        assert len(expected.rsplit("-", 1)[1]) == 8
        assert runtime.library_sources()[0].provider == "zim"
        assert runtime.library_sources()[0].collection_key == "zim:library"
    finally:
        runtime.close()
        batch.close()


def test_duplicate_stems_get_distinct_stable_keys(tmp_path):
    zims = tmp_path / "zims"
    (zims / "a").mkdir(parents=True)
    (zims / "b").mkdir(parents=True)
    first = zims / "a" / "manual.zim"
    second = zims / "b" / "manual.zim"
    _create_zim(first)
    _create_zim(second)
    _config, batch, runtime = _open(tmp_path, zims)
    try:
        keys = {source.key for source in runtime.library_sources()}
        assert keys == {
            zim_source_key("library", "a/manual.zim"),
            zim_source_key("library", "b/manual.zim"),
        }
        before = keys
        if os.name == "nt":
            runtime.close()
            batch.close()
        second.unlink()
        if os.name == "nt":
            _config, batch, runtime = _open(tmp_path, zims)
        else:
            assert runtime.refresh(wait=True)
        after = {source.key for source in runtime.library_sources()}
        assert after == {
            zim_source_key("library", "a/manual.zim"),
        }
        assert after <= before
    finally:
        runtime.close()
        batch.close()


def test_relocating_a_root_preserves_keys(tmp_path):
    first_root = tmp_path / "one"
    second_root = tmp_path / "two"
    first_root.mkdir()
    second_root.mkdir()
    _create_zim(first_root / "manual.zim")
    _create_zim(second_root / "manual.zim")
    _config, batch, runtime = _open(tmp_path, first_root)
    try:
        first_keys = {source.key for source in runtime.library_sources()}
    finally:
        runtime.close()
        batch.close()
    _config, batch, runtime = _open(tmp_path, second_root)
    try:
        second_keys = {source.key for source in runtime.library_sources()}
    finally:
        runtime.close()
        batch.close()
    assert first_keys == second_keys


def test_unreadable_archive_is_library_only(tmp_path):
    zims = tmp_path / "zims"
    zims.mkdir()
    _create_zim(zims / "manual.zim")
    (zims / "broken.zim").write_bytes(b"not a zim")
    _config, batch, runtime = _open(tmp_path, zims)
    try:
        assert len(runtime.sources) == 1
        titles = {source.title for source in runtime.library_sources()}
        assert "manual" in titles
        assert any(source.searchable is False for source in runtime.library_sources())
    finally:
        runtime.close()
        batch.close()


def test_store_reads_articles_and_the_homepage(tmp_path):
    zims = tmp_path / "zims"
    zims.mkdir()
    _create_zim(zims / "manual.zim")
    _config, batch, runtime = _open(tmp_path, zims)
    try:
        source = runtime.sources[0]
        store = runtime.content_store(source)
        assert store is not None
        try:
            assert store.main_path() == "Alpha"
            data = store.read("Beta", head_only=False, max_bytes=1 << 20)
            assert data.path == "Beta"
            assert data.mimetype == "text/html"
            assert b"tires" in data.content
            assert data.truncated is False
            head = store.read("Beta", head_only=True, max_bytes=1 << 20)
            # A prefix read that happens to contain the complete small item is
            # not truncated; a genuinely partial prefix is covered by the
            # shared content-store contract suite.
            assert head.content == data.content
            assert head.truncated is False
        finally:
            store.close()
    finally:
        runtime.close()
        batch.close()


def test_built_generation_is_discovered_and_searchable(tmp_path):
    from types import SimpleNamespace

    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index
    from zimantic.providers.zim.discovery import zim_collection_key, zim_source_key
    from zimantic.providers.zim.entries import ArchiveEntryProducer

    zims = tmp_path / "zims"
    zims.mkdir()
    archive = zims / "manual.zim"
    _create_zim(archive)
    indexes = tmp_path / "indexes"
    source = SimpleNamespace(
        key=zim_source_key("library", archive.name),
        provider="zim",
        collection_key=zim_collection_key("library"),
        provider_id=None,
        path=str(archive),
    )
    build_index(
        ArchiveEntryProducer(source, archive, fast=True),
        IndexLocator(indexes),
        None,
        {"batch_size": 2},
        fast=True,
    )

    _config, batch, runtime = _open(tmp_path, zims, index_dir=indexes)
    try:
        source = runtime.sources[0]
        index = runtime.indexes[source.key]
        assert index.generation is not None
        assert index.db_path is not None
    finally:
        runtime.close()
        batch.close()


def test_start_faiss_loads_after_core_binds_defer_false(tmp_path, caplog):
    """The core binds defer_faiss=False before calling runtime.start_faiss().

    start_faiss() used to require _defer_faiss to still be True and therefore
    returned immediately, leaving every semantic index unloaded.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    import faiss
    import numpy as np

    from zimantic.indexing import pipeline
    from zimantic.indexing.locator import IndexLocator
    from zimantic.providers.zim.discovery import zim_collection_key, zim_source_key
    from zimantic.providers.zim.entries import ArchiveEntryProducer

    class _Embedder:
        fingerprint = "test-model"

        def truncate(self, text, prefix=""):
            return text

        def embed(self, texts):
            return np.ones((len(texts), 4), dtype=np.float32)

        def encode(self, texts):
            return [list(map(ord, text)) for text in texts]

        def embed_encoded(self, inputs):
            return self.embed(inputs)

    zims = tmp_path / "zims"
    zims.mkdir()
    archive = zims / "manual.zim"
    _create_zim(archive)
    indexes = tmp_path / "indexes"
    source = SimpleNamespace(
        key=zim_source_key("library", archive.name),
        provider="zim",
        collection_key=zim_collection_key("library"),
        provider_id=None,
        path=str(archive),
    )
    with patch.object(
        pipeline, "_write_faiss", side_effect=lambda db, path: path.write_bytes(b"vectors")
    ):
        pipeline.build_index(
            ArchiveEntryProducer(source, archive),
            IndexLocator(indexes),
            _Embedder(),
            {"batch_size": 2},
        )

    _config, batch, runtime = _open(tmp_path, zims, index_dir=indexes)
    try:
        runtime.bind_search(semantic=True, defer_faiss=False)
        with (
            patch.object(faiss, "read_index", return_value=object()),
            caplog.at_level(logging.WARNING),
        ):
            runtime.start_faiss()
        assert runtime.indexes
        assert all(
            index.faiss_index is not None for index in runtime.indexes.values()
        )
        assert "FAISS indexes unavailable" not in caplog.text
    finally:
        runtime.close()
        batch.close()


def test_scan_only_removes_rows_in_the_scanned_collection(tmp_path):
    zims = tmp_path / "zims"
    zims.mkdir()
    _create_zim(zims / "manual.zim")
    catalog = SourceCatalog(catalog_path(tmp_path / "indexes"))
    try:
        scan_collection(
            catalog,
            ZimConfig(name="library", path=str(zims)),
            collection_key="zim:library",
            archive_factory=Archive,
        )
        catalog.upsert(
            {
                "provider": "zim",
                "collection_key": "zim:other",
                "key": zim_source_key("other", "kept.zim"),
                "path": str(tmp_path / "elsewhere" / "kept.zim"),
                "title": "kept",
            }
        )
        scan_collection(
            catalog,
            ZimConfig(name="library", path=str(zims)),
            collection_key="zim:library",
            archive_factory=Archive,
        )
        keys = {record.key for record in catalog.rows()}
        assert zim_source_key("library", "manual.zim") in keys
        assert zim_source_key("other", "kept.zim") in keys
    finally:
        catalog.close()


def test_incomplete_scan_does_not_delete_rows(tmp_path):
    zims = tmp_path / "zims"
    zims.mkdir()
    _create_zim(zims / "manual.zim")
    catalog = SourceCatalog(catalog_path(tmp_path / "indexes"))
    try:
        scan_collection(
            catalog,
            ZimConfig(name="library", path=str(zims)),
            collection_key="zim:library",
            archive_factory=Archive,
        )
        result = scan_collection(
            catalog,
            ZimConfig(name="library", path=str(zims / "missing")),
            collection_key="zim:library",
            archive_factory=Archive,
        )
        assert not result.complete
        assert len(catalog.rows(collection_key="zim:library")) == 1
    finally:
        catalog.close()


def test_catalog_schema_has_no_zim_specific_tables(tmp_path):
    catalog = SourceCatalog(tmp_path / "catalog.sqlite")
    try:
        names = {
            row["name"]
            for row in catalog.db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row["name"]
            for row in catalog.db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    finally:
        catalog.close()
    assert "sources" in names
    assert "zims" not in names
    assert not any(name.startswith("zims_") for name in indexes)


@pytest.mark.parametrize(
    ("counter", "expected"),
    [
        ("text/html=3;image/png=2;video/mp4=1", ("image",)),
        (
            "text/html=25;video/webm=24;text/vtt=544;image/webp=42",
            ("video",),
        ),
    ],
)
def test_source_media_types_come_from_the_mime_counter(counter, expected):
    from zimantic.catalog import SourceRecord
    from zimantic.providers.zim.metadata import source_info_from_record

    record = SourceRecord(
        provider="zim",
        collection_key="zim:library",
        key=zim_source_key("library", "manual.zim"),
        path=None,
        provider_id="",
        size=10,
        mtime=1.0,
        title="Manual",
        description="",
        long_description="",
        language="",
        creator="",
        publisher="",
        date="",
        tags="",
        public_tags="",
        category="",
        item_count=6,
        license="",
        origin="",
        attribution="",
        relation="",
        mime_counts=counter,
        homepage="Alpha",
        native_fulltext_available=1,
        icon_data=None,
        icon_mime_type="",
        icon_path="",
        scanned_at=1.0,
        provider_metadata="{}",
    )
    source = source_info_from_record(
        record,
        collection_key="zim:library",
        collection_label="Library",
        icon_url=None,
        searchable=True,
    )
    assert source.media_types == expected
    payload = source.to_dict(include_metadata=True)
    assert payload["media_types"] == list(expected)
    assert "path" not in payload


@pytest.mark.parametrize(
    ("counter", "expected"),
    [
        ("text/html=4;video/mp4=1;audio/mpeg=1", ("video",)),
        ("text/html=5;video/mp4=1;audio/mpeg=1", ("video",)),
        ("application/xhtml+xml=2;application/pdf=2;video/mp4=2", ("video",)),
        ("text/plain=100;text/markdown=2;video/mp4=1", ("text",)),
        ("text/vtt=500;text/css=2;video/mp4=5", ("text",)),
        ("text/html=unknown;video/mp4=5", ("video",)),
        ("text/html=1;video/mp4=unknown", ("text",)),
        ("text/html=1;video/mp4=0", ("text",)),
        ("text/html=100;text/css=90;image/webp=50", ("image",)),
        ("text/html=100;application/json=20", ("other",)),
        ("text/html=100;application/json=5", ("text",)),
        ("text/html=0;video/mp4=0", ()),
    ],
)
def test_zim_media_types_use_document_and_audiovisual_counts(counter, expected):
    from zimantic.media_types import classify_mime_counts

    assert classify_mime_counts(counter) == expected


def test_key_collision_warns_with_relative_paths_only(tmp_path, monkeypatch, caplog):
    from zimantic.providers.zim import discovery

    zims = tmp_path / "zims"
    (zims / "a").mkdir(parents=True)
    (zims / "b").mkdir(parents=True)
    _create_zim(zims / "a" / "manual.zim")
    _create_zim(zims / "b" / "manual.zim")
    monkeypatch.setattr(discovery, "zim_source_key", lambda *_args: "zim:library:same")
    catalog = SourceCatalog(catalog_path(tmp_path / "indexes"))
    try:
        with caplog.at_level(logging.WARNING):
            result = discovery.scan_collection(
                catalog,
                ZimConfig(name="library", path=str(zims)),
                collection_key="zim:library",
                archive_factory=Archive,
            )
        assert result.complete
        assert any("collision" in record.message for record in caplog.records)
        messages = " ".join(record.getMessage() for record in caplog.records)
        assert str(tmp_path) not in messages
        assert "a/manual.zim" in messages and "b/manual.zim" in messages
    finally:
        catalog.close()


def test_directory_collection_discovers_nested_zim_files_only(tmp_path):
    zims = tmp_path / "zims"
    (zims / "nested").mkdir(parents=True)
    _create_zim(zims / "manual.zim")
    _create_zim(zims / "nested" / "other.zim")
    (zims / "notes.txt").write_text("not an archive")
    _config, batch, runtime = _open(tmp_path, zims)
    try:
        assert {source.key for source in runtime.library_sources()} == {
            zim_source_key("library", "manual.zim"),
            zim_source_key("library", "nested/other.zim"),
        }
    finally:
        runtime.close()
        batch.close()


class _MigrationEmbedder:
    def __init__(self, fingerprint="a" * 64, vector=(1, 0, 0, 0)):
        self.fingerprint = fingerprint
        self.vector = vector
        self.encoded_count = 0

    def truncate(self, text, prefix=""):
        return text

    def encode(self, texts):
        return [[1, 2] for _ in texts]

    def embed_encoded(self, inputs):
        import numpy as np

        self.encoded_count += len(inputs)
        return np.tile(np.asarray(self.vector, dtype="float32"), (len(inputs), 1))

    def embed(self, texts):
        import numpy as np

        return np.tile(np.asarray(self.vector, dtype="float32"), (len(texts), 1))


def _migration_producer(zim_path, *, cfg=None, fast=False):
    from types import SimpleNamespace

    from zimantic.providers.zim.entries import ArchiveEntryProducer

    source = SimpleNamespace(
        key=zim_source_key("library", zim_path.name),
        provider="zim",
        collection_key=zim_collection_key("library"),
    )
    return ArchiveEntryProducer(source, zim_path, cfg=cfg, fast=fast)


def _write_v0_index(zim_path, index_dir, *, vector_count=2, problem=None, layout="flat"):
    import json
    import sqlite3

    import faiss
    import numpy as np

    index_dir.mkdir(exist_ok=True)
    producer = _migration_producer(zim_path)
    try:
        rows = [producer.entry_at(i) for i in range(producer.count)]
    finally:
        producer.close()
    sqlite_path = index_dir / f"{zim_path.stem}.sqlite"
    faiss_path = index_dir / f"{zim_path.stem}.faiss"
    with sqlite3.connect(sqlite_path) as db:
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, path UNINDEXED, target UNINDEXED"
            + ("" if problem == "schema" else ", tokenize='unicode61 remove_diacritics 2'")
            + ")"
        )
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value)")
        db.executemany(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            [
                (row.id, row.title, "wrong" if problem == "path" and i == 0 else row.path, row.target_id)
                for i, row in enumerate(rows)
            ],
        )
        db.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("done", 1),
            ("filesize", str(zim_path.stat().st_size + (problem == "size"))),
            ("model_fingerprint", "a" * 64),
            ("model_probe_texts", json.dumps(["first", "second"])),
            ("model_probe_vectors", np.tile(
                np.asarray([1, 0, 0, 0], dtype="<f4"), (2, 1)
            ).tobytes()),
            ("model_probe_shape", "2x4"),
        ])
        if problem == "missing_probes":
            db.execute("DELETE FROM meta WHERE key LIKE 'model_probe_%'")
    if layout == "ivf":
        index = faiss.index_factory(4, "IVF1,SQ8", faiss.METRIC_INNER_PRODUCT)
        threads = faiss.omp_get_max_threads()
        try:
            faiss.omp_set_num_threads(1)
            index.train(np.random.default_rng(42).random((64, 4), dtype=np.float32))
        finally:
            faiss.omp_set_num_threads(threads)
    else:
        index = faiss.index_factory(4, "IDMap,Flat", faiss.METRIC_INNER_PRODUCT)
    index.add_with_ids(
        np.tile(np.asarray([1, 0, 0, 0], dtype="float32"), (vector_count, 1)),
        np.asarray([row.id for row in rows[:vector_count]], dtype="int64"),
    )
    faiss.write_index(index, str(faiss_path))
    return sqlite_path, faiss_path


@pytest.mark.parametrize("old_count", [1, 2])
@pytest.mark.parametrize("layout", ["flat", "ivf"])
def test_v0_vectors_are_reused_and_body_index_is_rebuilt(tmp_path, old_count, layout):
    import sqlite3

    import faiss

    from zimantic.embed import load_model_probes
    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index
    from zimantic.indexing.search import body_search
    from zimantic.providers.zim.discovery import ZimIndexOptions, open_generation_index

    zim = tmp_path / "manual.zim"
    _create_zim(zim)
    index_dir = tmp_path / "indexes"
    old_sqlite, old_faiss = _write_v0_index(
        zim, index_dir, vector_count=old_count, layout=layout
    )
    original = old_faiss.read_bytes()
    embedder = _MigrationEmbedder()
    producer = _migration_producer(zim)
    source = producer.source
    locator = IndexLocator(index_dir)
    report = build_index(producer, locator, embedder, {"batch_size": 1})
    assert report.status == "published"
    assert embedder.encoded_count == 2 - old_count
    assert old_sqlite.exists() and old_faiss.read_bytes() == original
    lease = locator.paths(source)
    assert lease is not None
    try:
        if old_count == 2:
            assert lease.faiss_path.read_bytes() == original
        with sqlite3.connect(lease.sqlite_path) as db:
            meta = dict(db.execute("SELECT key, value FROM meta"))
            assert meta["semantic_vector_count"] == "2"
            assert meta["model_fingerprint"] == embedder.fingerprint
            assert load_model_probes(meta) is not None
            assert len(body_search(db, "wheels", 10)) == 2
        index = faiss.read_index(str(lease.faiss_path))
        if layout == "flat":
            ids = faiss.vector_to_array(index.id_map).tolist()
        else:
            pointer = index.invlists.get_ids(0)
            try:
                ids = faiss.rev_swig_ptr(pointer, index.invlists.list_size(0)).tolist()
            finally:
                index.invlists.release_ids(0, pointer)
        assert sorted(ids) == [0, 1]
    finally:
        lease.close()
    handle = open_generation_index(
        source, index_dir=index_dir,
        options=ZimIndexOptions(semantic=True, model_fingerprint=embedder.fingerprint),
        expected_source_size=zim.stat().st_size,
    )
    assert handle is not None and handle.faiss_index is not None
    handle.close()


@pytest.mark.parametrize("problem", ["size", "path", "model", "schema"])
def test_v0_mismatch_rebuilds_without_legacy_vectors(tmp_path, problem):
    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index

    zim = tmp_path / "manual.zim"
    _create_zim(zim)
    index_dir = tmp_path / "indexes"
    _write_v0_index(zim, index_dir, problem=problem)
    embedder = _MigrationEmbedder(
        fingerprint="b" * 64 if problem == "model" else "a" * 64,
        vector=(0, 1, 0, 0) if problem == "model" else (1, 0, 0, 0),
    )
    report = build_index(
        _migration_producer(zim), IndexLocator(index_dir), embedder, {"batch_size": 1}
    )
    assert report.status == "published"
    assert embedder.encoded_count == 2


def test_v0_cross_model_probes_allow_reuse(tmp_path):
    import threading

    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index
    from zimantic.providers.zim.discovery import ZimIndexOptions, open_generation_index
    from zimantic.providers.zim.provider import ZimRuntime

    zim = tmp_path / "manual.zim"
    _create_zim(zim)
    index_dir = tmp_path / "indexes"
    _write_v0_index(zim, index_dir)
    embedder = _MigrationEmbedder(fingerprint="b" * 64)
    producer = _migration_producer(zim)
    source = producer.source
    build_index(producer, IndexLocator(index_dir), embedder, {})
    assert embedder.encoded_count == 0
    runtime = object.__new__(ZimRuntime)
    runtime._embedder_getter = lambda: embedder
    runtime._embedder_ready_getter = None
    runtime._probe_cache = {}
    runtime._probe_cache_lock = threading.Lock()
    handle = open_generation_index(
        source, index_dir=index_dir,
        options=ZimIndexOptions(
            semantic=True,
            model_fingerprint=embedder.fingerprint,
            probe_compatible=runtime._probe_compatible,
        ),
        expected_source_size=zim.stat().st_size,
    )
    assert handle is not None and handle.faiss_index is not None
    handle.close()


def test_v0_missing_probes_are_rebuilt_with_the_same_model(tmp_path):
    import sqlite3

    from zimantic.embed import load_model_probes
    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index

    zim = tmp_path / "manual.zim"
    _create_zim(zim)
    index_dir = tmp_path / "indexes"
    _write_v0_index(zim, index_dir, problem="missing_probes")
    embedder = _MigrationEmbedder()
    producer = _migration_producer(zim)
    source = producer.source
    locator = IndexLocator(index_dir)
    build_index(producer, locator, embedder, {})
    assert embedder.encoded_count == 0
    lease = locator.paths(source)
    try:
        with sqlite3.connect(lease.sqlite_path) as db:
            assert load_model_probes(dict(db.execute("SELECT key, value FROM meta")))
    finally:
        lease.close()


def test_v0_vectors_are_not_reused_to_replace_a_current_full_index(tmp_path, monkeypatch):
    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index
    from zimantic.providers.zim import legacy_index

    zim = tmp_path / "manual.zim"
    _create_zim(zim)
    index_dir = tmp_path / "indexes"
    _write_v0_index(zim, index_dir)
    locator = IndexLocator(index_dir)
    build_index(_migration_producer(zim), locator, _MigrationEmbedder(), {})
    monkeypatch.setattr(
        legacy_index, "open_legacy_vectors",
        lambda *_args: pytest.fail("must not replace a current full index with v0 vectors"),
    )
    embedder = _MigrationEmbedder()
    build_index(
        _migration_producer(zim, cfg={"max_preview_chars": 321}),
        locator, embedder, {},
    )
    assert embedder.encoded_count == 2


@pytest.mark.parametrize("fast", [False, True])
def test_force_and_fast_builds_do_not_reuse_v0_vectors(tmp_path, fast, monkeypatch):
    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index
    from zimantic.providers.zim import legacy_index

    zim = tmp_path / "manual.zim"
    _create_zim(zim)
    index_dir = tmp_path / "indexes"
    _write_v0_index(zim, index_dir)
    monkeypatch.setattr(
        legacy_index, "open_legacy_vectors",
        lambda *_args: pytest.fail("force and fast builds must not load v0 vectors"),
    )
    embedder = None if fast else _MigrationEmbedder()
    build_index(
        _migration_producer(zim), IndexLocator(index_dir), embedder, {},
        fast=fast, force=not fast,
    )
    if embedder is not None:
        assert embedder.encoded_count == 2


def test_full_build_after_fast_build_reuses_v0_vectors(tmp_path):
    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index

    zim = tmp_path / "manual.zim"
    _create_zim(zim)
    index_dir = tmp_path / "indexes"
    _write_v0_index(zim, index_dir)
    locator = IndexLocator(index_dir)
    build_index(_migration_producer(zim, fast=True), locator, None, {}, fast=True)
    embedder = _MigrationEmbedder()
    build_index(_migration_producer(zim), locator, embedder, {})
    assert embedder.encoded_count == 0


def test_v0_index_changed_during_migration_is_not_published(tmp_path):
    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index

    zim = tmp_path / "manual.zim"
    _create_zim(zim)
    index_dir = tmp_path / "indexes"
    _, old_faiss = _write_v0_index(zim, index_dir)
    producer = _migration_producer(zim)
    original_entry_at = producer.entry_at
    changed = False

    def replace_index(index):
        nonlocal changed
        if index == 1 and not changed:
            old_faiss.write_bytes(b"changed")
            changed = True
        return original_entry_at(index)

    producer.entry_at = replace_index
    embedder = _MigrationEmbedder()
    report = build_index(producer, IndexLocator(index_dir), embedder, {"batch_size": 1})
    assert report.status == "published"
    assert changed and embedder.encoded_count == 2


def test_v0_migration_checks_space_for_vector_copies(tmp_path, monkeypatch):
    from collections import namedtuple

    from zimantic.indexing import pipeline
    from zimantic.indexing.locator import IndexLocator

    zim = tmp_path / "manual.zim"
    _create_zim(zim)
    index_dir = tmp_path / "indexes"
    old_sqlite, old_faiss = _write_v0_index(zim, index_dir)
    original = old_faiss.read_bytes()
    disk_usage = namedtuple("disk_usage", "total used free")
    checks = iter([disk_usage(10**9, 0, 10**9), disk_usage(10**9, 0, 1)])
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _path: next(checks))
    with pytest.raises(pipeline.InsufficientSpaceError):
        pipeline.build_index(
            _migration_producer(zim), IndexLocator(index_dir), _MigrationEmbedder(), {}
        )
    assert old_sqlite.exists() and old_faiss.read_bytes() == original


def test_v0_migration_does_not_follow_staging_faiss_symlink(tmp_path):
    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index

    zim = tmp_path / "manual.zim"
    _create_zim(zim)
    index_dir = tmp_path / "indexes"
    _write_v0_index(zim, index_dir)
    producer = _migration_producer(zim)
    locator = IndexLocator(index_dir)
    directory = locator.source_directory(
        producer.source.provider, producer.source.collection_key, producer.source.key
    )
    directory.mkdir(parents=True)
    sentinel = tmp_path / "unrelated"
    sentinel.write_bytes(b"keep")
    try:
        (directory / "staging-full.faiss").symlink_to(sentinel)
    except OSError:
        pytest.skip("symlinks are unavailable")
    embedder = _MigrationEmbedder()
    report = build_index(producer, locator, embedder, {})
    assert report.status == "published"
    assert embedder.encoded_count == 0
    assert sentinel.read_bytes() == b"keep"


@pytest.mark.parametrize("replace_old", [False, True])
def test_interrupted_v0_migration_resumes_or_restarts(tmp_path, replace_old):
    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index

    zim = tmp_path / "manual.zim"
    _create_zim(zim)
    index_dir = tmp_path / "indexes"
    _, old_faiss = _write_v0_index(zim, index_dir)
    embedder = _MigrationEmbedder()
    producer = _migration_producer(zim)
    original_entry_at = producer.entry_at

    def interrupted(index):
        if index == 1:
            raise KeyboardInterrupt
        return original_entry_at(index)

    producer.entry_at = interrupted
    with pytest.raises(KeyboardInterrupt):
        build_index(producer, IndexLocator(index_dir), embedder, {"batch_size": 1})
    if replace_old:
        old_faiss.write_bytes(b"not a FAISS index")
    report = build_index(
        _migration_producer(zim), IndexLocator(index_dir), embedder, {"batch_size": 1}
    )
    assert report.status == "published"
    assert embedder.encoded_count == (2 if replace_old else 0)


@pytest.mark.skipif(not hasattr(Archive, "iter_efficient"), reason="requires libzim 4.1")
def test_parallel_reader_matches_serial_entry_order(tmp_path):
    from types import SimpleNamespace

    from zimantic.providers.zim.entries import ArchiveEntryProducer

    path = tmp_path / "parallel.zim"
    with Creator(path) as creator:
        creator.add_item(_Page("Alpha", "Alpha", ALPHA_HTML))
        creator.add_item(_Page("Beta", "Beta", ALPHA_HTML))
        creator.add_item(_Page("Gamma", "Gamma", ALPHA_HTML))
        creator.add_redirection("Alias", "Alias", "Alpha", {})
    source = SimpleNamespace(key="parallel", provider="zim", collection_key="zim:test")
    producer = ArchiveEntryProducer(source, path)
    producer.prepare_iteration()
    expected = [producer.entry_at(i) for i in range(producer.count)]
    reader = producer.parallel_reader(workers=2, chunk_size=1)
    assert reader is not None
    try:
        actual = [reader.read(i) for i in range(producer.count)]
    finally:
        reader.close()
    assert actual == expected
