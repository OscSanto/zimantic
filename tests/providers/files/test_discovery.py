"""Files collection/source discovery tests."""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from zimantic.catalog import SourceCatalog, catalog_path
from zimantic.indexing.derived import SourceDerivedOperations
from zimantic.providers.files import FilesProvider
from zimantic.providers.files import discovery as files_discovery
from zimantic.providers.files.discovery import (
    discover_sources,
    files_collapsed_key,
    files_collection_key,
    files_dir_key,
    files_loose_key,
    files_root_key,
)

from .helpers import build_tree, context, files_config, open_runtime, runtime_config


def test_directory_source_key_uses_eight_character_path_digest():
    assert (
        files_dir_key("modules", "en-file_share")
        == "files:modules:dir-en-file_share-e8d62e4a"
    )


def test_children_discovery_finds_directories_and_loose_files(tmp_path):
    root = tmp_path / "modules"
    build_tree(root)
    config = files_config(root)

    result = discover_sources(config)

    keys = {source.key for source in result.sources}
    assert keys == {
        files_dir_key("modules", "algebra"),
        files_dir_key("modules", "biology"),
        files_dir_key("modules", "broken"),
        files_loose_key("modules"),
    }
    assert not result.collapsed
    assert result.errors == ()

    by_key = {source.key: source for source in result.sources}
    algebra = by_key[files_dir_key("modules", "algebra")]
    assert algebra.info.title
    assert algebra.info.provider == "files"
    assert algebra.info.collection_key == files_collection_key("modules")
    assert algebra.info.homepage == "algebra/index.html"
    assert algebra.membership_root == "algebra"
    assert "text" in algebra.info.media_types

    biology = by_key[files_dir_key("modules", "biology")]
    assert "video" in biology.info.media_types
    assert biology.info.homepage == "biology/index.html"

    loose = by_key[files_loose_key("modules")]
    assert loose.info.title
    assert loose.loose_only is True
    assert loose.membership_root == ""

    assert all(".hidden" not in source.key for source in result.sources)
    assert all("node_modules" not in source.membership_root for source in result.sources)


def test_summaries_use_each_source_root_and_refresh_on_discovery(tmp_path):
    root = tmp_path / "modules"
    child = root / "algebra"
    child.mkdir(parents=True)
    (root / "README.txt").write_text("Collection overview.", encoding="utf-8")
    readme = child / "ReadMe.MD"
    readme.write_text(
        "---\ntitle: Algebra\n---\n"
        "# Algebra\n\nLearn [numbers](lesson.html) and **equations**.\n"
        "```python\nhidden_code()\n```",
        encoding="utf-8",
    )
    (child / "about.txt").write_text("About algebra.", encoding="utf-8")
    (child / "index.html").write_text("<p>Homepage prose.</p>", encoding="utf-8")
    config = files_config(root)

    by_key = {source.key: source.info for source in discover_sources(config).sources}
    assert by_key[files_dir_key("modules", "algebra")].description
    assert by_key[files_loose_key("modules")].description
    readme.write_text("Updated algebra summary.", encoding="utf-8")
    refreshed = discover_sources(config)
    assert next(
        source.info.description for source in refreshed.sources
        if source.key == files_dir_key("modules", "algebra")
    )

    readme.unlink()
    assert next(
        source.info.description for source in discover_sources(config).sources
        if source.key == files_dir_key("modules", "algebra")
    )


@pytest.mark.parametrize("granularity,max_sources", [
    ("collection", 64), ("children", 1),
])
def test_whole_collection_summary_does_not_use_nested_documents(
    tmp_path, granularity, max_sources
):
    root = tmp_path / "modules"
    for name in ("one", "two"):
        child = root / name
        child.mkdir(parents=True)
        (child / "README.txt").write_text("Nested description.", encoding="utf-8")
    config = files_config(root, granularity=granularity, max_sources=max_sources)
    assert discover_sources(config).sources[0].info.description is None
    (root / "ABOUT.TXT").write_text("Root description.", encoding="utf-8")
    assert discover_sources(config).sources[0].info.description


def test_summary_skips_excluded_binary_empty_and_symlink_candidates(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "README.md").write_text("Excluded description.", encoding="utf-8")
    (root / "README.txt").write_bytes(b"\x00binary description")
    (root / "about.txt").write_text(" \n\t ", encoding="utf-8")
    (root / "index.htm").write_text("<p>Safe fallback.</p>", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("Outside description.", encoding="utf-8")
    try:
        (root / "README").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    config = files_config(
        root, granularity="collection", exclude=("README.md",)
    )
    result = discover_sources(config)
    assert result.errors == ()
    assert result.sources[0].info.description


def test_summary_bounds_input_and_output(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    readme = root / "README.txt"
    readme.write_text("word " * 20000, encoding="utf-8")
    config = files_config(root, granularity="collection")
    summary = discover_sources(config).sources[0].info.description
    assert summary is not None
    assert len(summary) <= 400
    assert summary.split() == ["word"] * 80

    readme.unlink()
    (root / "index.html").write_text(
        "<script>" + "x" * (64 << 10) + "</script><p>Beyond read limit.</p>",
        encoding="utf-8",
    )
    assert discover_sources(config).sources[0].info.description is None


def test_summary_catalog_survives_restart_without_reading_unchanged_documents(
    tmp_path, monkeypatch
):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "README.txt").write_text("Cached summary.", encoding="utf-8")
    config = files_config(root, granularity="collection")
    db_path = catalog_path(tmp_path / "indexes")
    with SourceCatalog(db_path) as catalog:
        source = discover_sources(config, catalog=catalog).sources[0].info
        record = catalog.get_key(source.key)
        assert record.provider == "files"
        assert record.path == str(root)
        assert record.description

    def unexpected_read(*args, **kwargs):
        raise AssertionError("unchanged summary documents must not be opened")

    with SourceCatalog(db_path) as catalog:
        version = catalog.version()
        monkeypatch.setattr(files_discovery, "open_confined", unexpected_read)
        assert discover_sources(config, catalog=catalog).sources[0].info.description
        assert catalog.version() == version


def test_negative_summary_cache_is_reused_and_rebuilt_after_catalog_deletion(
    tmp_path, monkeypatch
):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "about.txt").write_text(" \n ", encoding="utf-8")
    config = files_config(root, granularity="collection")
    db_path = catalog_path(tmp_path / "indexes")
    with SourceCatalog(db_path) as catalog:
        assert discover_sources(config, catalog=catalog).sources[0].info.description is None
    with SourceCatalog(db_path) as catalog, monkeypatch.context() as patch:
        def unexpected_read(*args, **kwargs):
            raise AssertionError("empty summaries should also be cached")

        patch.setattr(files_discovery, "open_confined", unexpected_read)
        assert discover_sources(config, catalog=catalog).sources[0].info.description is None

    db_path.unlink()
    (root / "about.txt").write_text("Rebuilt summary.", encoding="utf-8")
    with SourceCatalog(db_path) as catalog:
        assert discover_sources(config, catalog=catalog).sources[0].info.description


def test_summary_cache_rechecks_source_location_and_recipe(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    readme = root / "README.txt"
    readme.write_text("Current summary.", encoding="utf-8")
    config = files_config(root, granularity="collection")
    with SourceCatalog(catalog_path(tmp_path / "indexes")) as catalog:
        source = discover_sources(config, catalog=catalog).sources[0].info
        catalog.db.execute(
            "UPDATE sources SET description = 'Obsolete summary.', "
            "provider_metadata = '{}' WHERE key = ?", (source.key,)
        )
        catalog.db.commit()
        assert discover_sources(config, catalog=catalog).sources[0].info.description
        record = catalog.get_key(source.key)
        record.path = str(tmp_path / "different-root")
        record.description = "Wrong source summary."
        catalog.upsert(record)
        assert discover_sources(config, catalog=catalog).sources[0].info.description


@pytest.mark.parametrize("operation", ["get_key", "upsert"])
def test_summary_cache_errors_do_not_hide_source_summaries(
    tmp_path, monkeypatch, operation
):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "about.txt").write_text("Available summary.", encoding="utf-8")
    config = files_config(root, granularity="collection")
    with SourceCatalog(None) as catalog:
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("catalog unavailable")

        monkeypatch.setattr(catalog, operation, unavailable)
        assert discover_sources(config, catalog=catalog).sources[0].info.description


def test_runtime_persists_summaries_and_prunes_only_successfully_scanned_collection(
    tmp_path,
):
    root = tmp_path / "modules"
    for name in ("one", "two"):
        child = root / name
        child.mkdir(parents=True)
        (child / "about.txt").write_text(f"{name} summary.", encoding="utf-8")
    config = files_config(root)
    index_dir = tmp_path / "indexes"
    cfg = runtime_config(index_dir, config)
    ctx = replace(context(index_dir, cfg), persist_catalog=True)
    provider = FilesProvider()
    batch = provider.discover(config, ctx)
    runtime = batch.open(batch.collections[0])
    db_path = catalog_path(index_dir)
    try:
        with SourceCatalog(db_path) as catalog:
            assert len(catalog.rows(collection_key="files:modules")) == 2
            catalog.upsert({
                "provider": "zim", "collection_key": "zim:books",
                "key": "zim:books:manual", "description": "ZIM description.",
            })
            (root / "one" / "about.txt").unlink()
            (root / "one").rmdir()
            assert runtime.refresh()
            assert [row.key for row in catalog.rows(collection_key="files:modules")] == [
                files_dir_key("modules", "two")
            ]
            assert catalog.get_key("zim:books:manual").description

            offline = tmp_path / "offline"
            root.rename(offline)
            assert not runtime.refresh()
            assert len(catalog.rows(collection_key="files:modules")) == 1
            assert runtime.library_sources() == ()
    finally:
        runtime.close()
        runtime.close()
        batch.close()


def test_read_only_runtime_does_not_create_persistent_summary_catalog(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "about.txt").write_text("Read-only summary.", encoding="utf-8")
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(
        index_dir, files_config(root, granularity="collection")
    )
    try:
        assert runtime.library_sources()[0].description
        assert not catalog_path(index_dir).exists()
    finally:
        runtime.close()
        batch.close()


def test_real_loose_directory_is_distinct_from_the_loose_source(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "__loose__").mkdir()
    (root / "__loose__" / "index.html").write_text(
        "<html><body>real child</body></html>", encoding="utf-8"
    )
    (root / "loose.txt").write_text("loose file", encoding="utf-8")
    config = files_config(root)

    before = discover_sources(config)
    keys_before = {source.key for source in before.sources}
    real_child = files_dir_key("modules", "__loose__")
    assert real_child in keys_before
    assert files_loose_key("modules") in keys_before
    assert real_child != files_loose_key("modules")

    (root / "another").mkdir()
    after = discover_sources(config)
    keys_after = {source.key for source in after.sources}
    assert real_child in keys_after
    assert files_loose_key("modules") in keys_after


def test_collection_granularity_yields_exactly_one_root_source(tmp_path):
    root = tmp_path / "modules"
    build_tree(root)
    config = files_config(root, granularity="collection")

    result = discover_sources(config)

    assert [source.key for source in result.sources] == [
        files_root_key("modules")
    ]
    source = result.sources[0]
    assert source.info.title
    assert source.info.homepage is None
    assert source.membership_root == ""


def test_exceeding_max_sources_collapses_the_whole_collection(tmp_path, caplog):
    root = tmp_path / "modules"
    root.mkdir()
    for index in range(6):
        child = root / f"unit-{index}"
        child.mkdir()
        (child / "index.html").write_text(
            "<html><body>unit</body></html>", encoding="utf-8"
        )
    config = files_config(root, max_sources=2)

    with caplog.at_level(logging.WARNING):
        result = discover_sources(config)

    assert result.collapsed is True
    assert [source.key for source in result.sources] == [
        files_collapsed_key("modules")
    ]
    assert sum("collapsing" in record.message for record in caplog.records) == 1


def test_many_children_collapse_with_the_default_source_cap(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    for index in range(70):
        child = root / f"unit-{index:03d}"
        child.mkdir()
        (child / "index.html").write_text(
            "<html><body>unit</body></html>", encoding="utf-8"
        )

    result = discover_sources(files_config(root))

    assert result.collapsed is True
    assert [source.key for source in result.sources] == [
        files_collapsed_key("modules")
    ]


def test_root_override_does_not_apply_to_the_collapsed_source(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    for index in range(4):
        child = root / f"unit-{index}"
        child.mkdir()
        (child / "index.html").write_text(
            "<html><body>unit</body></html>", encoding="utf-8"
        )
    config = files_config(root, max_sources=2)
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(index_dir, config)
    try:
        sources = list(runtime.library_sources())
        assert [source.key for source in sources] == [
            files_collapsed_key("modules")
        ]
        runtime._cfg["derived_source_operations"] = {
            files_root_key("modules"): SourceDerivedOperations(captions=True)
        }
        producer = runtime.entry_producer(sources[0], fast=False)
        assert producer.captions_enabled is False
    finally:
        runtime.close()
        batch.close()


def test_missing_root_disables_the_collection_without_raising(tmp_path):
    config = files_config(tmp_path / "missing")
    index_dir = tmp_path / "indexes"

    runtime, batch, _ = open_runtime(index_dir, config)
    try:
        snapshot = runtime.snapshot
        assert snapshot.discovery_state == "error"
        assert snapshot.error
        assert runtime.library_sources() == ()
        assert runtime.sources == ()
    finally:
        runtime.close()
        batch.close()


def test_unreadable_child_is_reported_and_other_sources_survive(
    tmp_path, monkeypatch
):
    root = tmp_path / "modules"
    (root / "good").mkdir(parents=True)
    (root / "good" / "index.html").write_text("good", encoding="utf-8")
    (root / "bad").mkdir()
    real_scandir = __import__("os").scandir

    def failing_scandir(path, *args, **kwargs):
        if not isinstance(path, int) and Path(path).name == "bad":
            raise PermissionError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr("os.scandir", failing_scandir)
    result = discover_sources(files_config(root))

    assert any("bad" in message for message in result.errors)
    assert files_dir_key("modules", "good") in {
        source.key for source in result.sources
    }


def test_media_types_include_media_omitted_from_the_index_include(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "video.mp4").write_bytes(b"video bytes")
    (root / "notes.txt").write_text("notes", encoding="utf-8")
    config = files_config(
        root,
        granularity="collection",
        include=("**/*.txt",),
    )

    result = discover_sources(config)

    assert "video" in result.sources[0].info.media_types
    assert "text" in result.sources[0].info.media_types


def test_other_assets_and_extensionless_media_are_library_visible(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "asset.css").write_text("body { color: #123; }", encoding="utf-8")
    (root / "app.js").write_text("console.log('asset');", encoding="utf-8")
    (root / "archive.zip").write_bytes(b"PK\x03\x04fixture")
    (root / "unknown.bin").write_bytes(b"\x00\x01\x02\x03")
    (root / "scan").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"png payload without a suffix"
    )
    config = files_config(root, granularity="collection")

    result = discover_sources(config)

    # CSS/JS/archives/unknown binaries are still servable content.  They make
    # the "Other" library filter meaningful instead of disappearing from every
    # specific content-type view.  The extensionless PNG keeps its media family.
    assert result.sources[0].info.media_types == ("image", "other")
