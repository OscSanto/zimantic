"""Files collection/source discovery tests."""

from __future__ import annotations

import logging
from pathlib import Path

from zimantic.indexing.derived import SourceDerivedOperations
from zimantic.providers.files.discovery import (
    discover_sources,
    files_collapsed_key,
    files_collection_key,
    files_dir_key,
    files_loose_key,
    files_root_key,
)

from .helpers import build_tree, files_config, open_runtime


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
    assert algebra.info.title == "algebra"
    assert algebra.info.provider == "files"
    assert algebra.info.collection_key == files_collection_key("modules")
    assert algebra.info.homepage == "algebra/index.html"
    assert algebra.membership_root == "algebra"
    assert "text" in algebra.info.media_types

    biology = by_key[files_dir_key("modules", "biology")]
    assert "video" in biology.info.media_types
    assert biology.info.homepage == "biology/index.html"

    loose = by_key[files_loose_key("modules")]
    assert loose.info.title == "Modules (files)"
    assert loose.loose_only is True
    assert loose.membership_root == ""

    assert all(".hidden" not in source.key for source in result.sources)
    assert all("node_modules" not in source.membership_root for source in result.sources)


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
    assert source.info.title == "Modules"
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
        if Path(path).name == "bad":
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
