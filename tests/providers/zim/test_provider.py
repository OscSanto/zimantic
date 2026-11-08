"""Provider-level ZIM discovery, identity, store, and runtime tests."""

from __future__ import annotations

import logging
from pathlib import Path

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
    creator = Creator(path)
    creator.config_indexing(True, "eng")
    creator.set_mainpath(main_path)
    with creator:
        creator.add_item(_Page("Alpha", "Alpha", ALPHA_HTML))
        creator.add_item(_Page("Beta", "Beta", ALPHA_HTML))


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
        assert actual[-1].title == "Alias"
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


def test_source_key_uses_basename_for_a_single_file_collection(tmp_path):
    zims = tmp_path / "zims"
    zims.mkdir()
    archive = zims / "manual.zim"
    _create_zim(archive)
    config, batch, runtime = _open(tmp_path, archive)
    try:
        expected = zim_source_key("library", "manual.zim")
        assert [source.key for source in runtime.library_sources()] == [expected]
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
    config, batch, runtime = _open(tmp_path, zims)
    try:
        keys = {source.key for source in runtime.library_sources()}
        assert keys == {
            zim_source_key("library", "a/manual.zim"),
            zim_source_key("library", "b/manual.zim"),
        }
        before = keys
        second.unlink()
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
    config, batch, runtime = _open(tmp_path, first_root)
    try:
        first_keys = {source.key for source in runtime.library_sources()}
    finally:
        runtime.close()
        batch.close()
    config, batch, runtime = _open(tmp_path, second_root)
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
    config, batch, runtime = _open(tmp_path, zims)
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
    config, batch, runtime = _open(tmp_path, zims)
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

    config, batch, runtime = _open(tmp_path, zims, index_dir=indexes)
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
        def truncate(self, text, prefix=""):
            return text

        def embed(self, texts):
            return np.ones((len(texts), 4), dtype=np.float32)

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

    config, batch, runtime = _open(tmp_path, zims, index_dir=indexes)
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


def test_source_media_types_come_from_the_mime_counter():
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
        mime_counts="text/html=3;image/png=2;video/mp4=1",
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
    assert source.media_types == ("text", "video", "image")
    payload = source.to_dict(include_metadata=True)
    assert payload["media_types"] == ["text", "video", "image"]
    assert "path" not in payload


def test_key_collision_warns_with_relative_paths_only(tmp_path, monkeypatch, caplog):
    import zimantic.providers.zim.discovery as discovery

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
    config, batch, runtime = _open(tmp_path, zims)
    try:
        assert {source.key for source in runtime.library_sources()} == {
            zim_source_key("library", "manual.zim"),
            zim_source_key("library", "nested/other.zim"),
        }
    finally:
        runtime.close()
        batch.close()
