"""Kolibri collection/source discovery tests."""

from __future__ import annotations

import sqlite3

from zimantic.providers.kolibri.discovery import (
    kolibri_collapsed_key,
    kolibri_collection_key,
    kolibri_node_source_key,
    kolibri_root_key,
    kolibri_ungrouped_key,
)
from zimantic.providers.kolibri.provider import KolibriProvider

from .helpers import (
    context,
    kolibri_config,
    make_two_channel_fixture,
    open_runtime,
    runtime_config,
)


def test_channel_is_collection_and_topics_are_sources(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    index_dir = tmp_path / "indexes"
    config = kolibri_config(fixture)
    batch = KolibriProvider().discover(
        config, context(index_dir, runtime_config(index_dir, config))
    )
    try:
        collections = list(batch.collections)
        assert {item.key for item in collections} == {
            kolibri_collection_key("kolibri", "channel-a"),
            kolibri_collection_key("kolibri", "channel-b"),
        }
        runtime_a = batch.open(
            next(
                item
                for item in collections
                if item.key == kolibri_collection_key("kolibri", "channel-a")
            )
        )
        runtime_b = batch.open(
            next(
                item
                for item in collections
                if item.key == kolibri_collection_key("kolibri", "channel-b")
            )
        )
        try:
            keys_a = {source.key for source in runtime_a.library_sources()}
            assert keys_a == {
                kolibri_node_source_key("kolibri", "channel-a", "topic-1"),
                kolibri_node_source_key("kolibri", "channel-a", "topic-2"),
                kolibri_ungrouped_key("kolibri", "channel-a"),
            }
            by_key = {source.key: source for source in runtime_a.library_sources()}
            topic = by_key[kolibri_node_source_key("kolibri", "channel-a", "topic-1")]
            assert topic.title
            assert topic.item_count == 1
            assert "text" in topic.media_types
            assert topic.homepage == "node/doc-1"
            video = by_key[kolibri_node_source_key("kolibri", "channel-a", "topic-2")]
            assert "video" in video.media_types

            keys_b = {source.key for source in runtime_b.library_sources()}
            assert keys_b == {kolibri_root_key("kolibri", "channel-b")}
            root = runtime_b.library_sources()[0]
            assert root.item_count == 2
            assert set(root.media_types) == {"text", "audio"}
        finally:
            runtime_a.close()
            runtime_b.close()
    finally:
        batch.close()


def test_collection_granularity_yields_one_source_per_channel(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    index_dir = tmp_path / "indexes"
    config = kolibri_config(fixture, granularity="collection")
    runtime, batch, _ = open_runtime(index_dir, config)
    try:
        assert [source.key for source in runtime.library_sources()] == [
            kolibri_root_key("kolibri", "channel-a")
        ]
    finally:
        runtime.close()
        batch.close()


def test_max_sources_collapses_and_root_override_does_not_apply(tmp_path, caplog):
    fixture = make_two_channel_fixture(tmp_path)
    index_dir = tmp_path / "indexes"
    config = kolibri_config(fixture, max_sources=1)
    runtime, batch, _runtime_config_value = open_runtime(index_dir, config)
    try:
        assert [source.key for source in runtime.library_sources()] == [
            kolibri_collapsed_key("kolibri", "channel-a")
        ]
        assert sum("collapsing" in record.message for record in caplog.records) == 1
        runtime._cfg["derived_source_operations"] = {
            kolibri_root_key("kolibri", "channel-a"): object()
        }
        producer = runtime.entry_producer(
            runtime.library_sources()[0], fast=False
        )
        assert producer.captions_enabled is False
    finally:
        runtime.close()
        batch.close()


def test_missing_database_disables_entry_without_crashing(tmp_path):
    index_dir = tmp_path / "indexes"
    config = kolibri_config(
        type("F", (), {"home": tmp_path / "missing"})(),
        path=str(tmp_path / "missing"),
    )
    batch = KolibriProvider().discover(
        config, context(index_dir, runtime_config(index_dir, config))
    )
    try:
        assert list(batch.collections) == []
        assert batch.error
    finally:
        batch.close()


def test_unsupported_schema_is_reported(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    fixture.db.close()
    db = sqlite3.connect(fixture.db_path)
    db.execute("DROP TABLE content_file")
    db.commit()
    db.close()
    index_dir = tmp_path / "indexes"
    config = kolibri_config(fixture)
    batch = KolibriProvider().discover(
        config, context(index_dir, runtime_config(index_dir, config))
    )
    try:
        assert list(batch.collections) == []
        assert "unsupported" in (batch.error or "").lower()
    finally:
        batch.close()


def test_export_database_is_ignored(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    export = fixture.home / "content" / "databases"
    export.mkdir(parents=True)
    export_db = export / "channel-a.sqlite3"
    conn = sqlite3.connect(export_db)
    conn.execute("CREATE TABLE nonsense (id TEXT)")
    conn.commit()
    conn.close()
    index_dir = tmp_path / "indexes"
    config = kolibri_config(fixture)
    runtime, batch, _ = open_runtime(index_dir, config)
    try:
        assert runtime.library_sources()
    finally:
        runtime.close()
        batch.close()
