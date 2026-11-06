"""Kolibri entry producer, text extraction, captions, and fingerprints."""

from __future__ import annotations

import sqlite3

import pytest

from zimantic.indexes.locator import IndexLocator
from zimantic.indexing.derived import DerivedTextConfig, SourceDerivedOperations
from zimantic.indexing.pipeline import build_index
from zimantic.providers.kolibri.discovery import (
    kolibri_node_source_key,
    kolibri_root_key,
)
from zimantic.providers.kolibri.entries import row_id_for

from .helpers import (
    KolibriFixture,
    build_sources,
    kolibri_config,
    make_two_channel_fixture,
    open_runtime,
)


def _docs(locator: IndexLocator, source) -> list[tuple[int, str, str]]:
    lease = locator.paths(source)
    assert lease is not None
    try:
        db = sqlite3.connect(lease.sqlite_path)
        try:
            return db.execute(
                "SELECT rowid, title, path FROM docs ORDER BY rowid"
            ).fetchall()
        finally:
            db.close()
    finally:
        lease.close()


def _meta(locator: IndexLocator, source) -> dict[str, str]:
    lease = locator.paths(source)
    assert lease is not None
    try:
        db = sqlite3.connect(lease.sqlite_path)
        try:
            return dict(db.execute("SELECT key, value FROM meta"))
        finally:
            db.close()
    finally:
        lease.close()


def _body_hits(locator: IndexLocator, source, term: str) -> list[int]:
    lease = locator.paths(source)
    assert lease is not None
    try:
        db = sqlite3.connect(lease.sqlite_path)
        try:
            return [
                int(row[0])
                for row in db.execute(
                    "SELECT rowid FROM body WHERE text MATCH ?",
                    (f'"{term}"',),
                )
            ]
        finally:
            db.close()
    finally:
        lease.close()


def test_topics_are_not_rows_and_extracted_text_is_searchable(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(index_dir, kolibri_config(fixture))
    try:
        topic_key = kolibri_node_source_key("kolibri", "channel-a", "topic-1")
        source = next(
            item for item in runtime.library_sources() if item.key == topic_key
        )
        producer = runtime.entry_producer(source, fast=False)
        assert producer.count == 1
        row = producer.entry_at(0)
        assert row is not None
        assert row.title == "PDF One"
        assert row.path == "node/doc-1"
        assert row.id == row_id_for("channel-a", "doc-1")
        assert "zebra" in row.body

        locator = build_sources(runtime, index_dir)
        meta = _meta(locator, source)
        assert meta["truncated"] == "0"
        assert _body_hits(locator, source, "zebra")
        docs = _docs(locator, source)
        assert [doc[1] for doc in docs] == ["PDF One"]
    finally:
        runtime.close()
        batch.close()


def test_exercise_description_is_indexed_and_coach_content_is_not(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    fixture.db.execute(
        "UPDATE content_contentnode SET coach_content = 1 WHERE id = 'exercise-1'"
    )
    coach = "private coach only text"
    fixture.db.execute(
        "INSERT INTO content_contentnode "
        "(id, parent_id, tree_id, level, lft, rght, title, coach_content, "
        "content_id, channel_id, description, kind, available) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "coach-node",
            "root-b",
            2,
            1,
            6,
            7,
            "Coach Node",
            1,
            "coach-node",
            "channel-b",
            coach,
            "exercise",
            1,
        ),
    )
    fixture.db.commit()
    index_dir = tmp_path / "indexes"
    config = kolibri_config(fixture)
    from .helpers import context, runtime_config
    from zimantic.providers.kolibri import KolibriProvider
    from zimantic.providers.kolibri.discovery import kolibri_collection_key

    provider_batch = KolibriProvider().discover(
        config, context(index_dir, runtime_config(index_dir, config))
    )
    collection_b = next(
        item
        for item in provider_batch.collections
        if item.key == kolibri_collection_key("kolibri", "channel-b")
    )
    runtime = provider_batch.open(collection_b)
    batch = provider_batch
    try:
        root_key = kolibri_root_key("kolibri", "channel-b")
        source = next(
            item for item in runtime.library_sources() if item.key == root_key
        )
        producer = runtime.entry_producer(source, fast=False)
        rows = {
            producer.entry_at(index).path: producer.entry_at(index)
            for index in range(producer.count)
        }
        assert "node/exercise-1" not in rows
        assert "node/coach-node" not in rows
        assert all("private coach" not in row.body for row in rows.values())
    finally:
        runtime.close()
        batch.close()


def test_exercise_without_file_gets_metadata_description(tmp_path):
    fixture = KolibriFixture(tmp_path)
    fixture.add_channel("channel-a", "Channel A", "root-a")
    fixture.add_node("root-a", "channel-a", "Channel A", kind="topic", lft=1, rght=4, level=0)
    fixture.add_node(
        "exercise-1",
        "channel-a",
        "Exercise One",
        kind="exercise",
        parent_id="root-a",
        lft=2,
        rght=3,
        description="public exercise description",
    )
    fixture.close()
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(index_dir, kolibri_config(fixture))
    try:
        source = runtime.library_sources()[0]
        producer = runtime.entry_producer(source, fast=False)
        row = producer.entry_at(0)
        assert row is not None
        assert "public exercise description" in row.body
        store = runtime.content_store(source)
        try:
            data = store.read(row.path, head_only=False, max_bytes=1 << 20)
            assert data.mimetype == "text/plain"
            assert b"public exercise description" in data.content
        finally:
            store.close()
    finally:
        runtime.close()
        batch.close()


def test_file_selection_is_deterministic_and_ignores_sql_order(tmp_path):
    fixture = KolibriFixture(tmp_path)
    fixture.add_channel("channel-a", "Channel A", "root-a")
    fixture.add_node("root-a", "channel-a", "Channel A", kind="topic", lft=1, rght=4, level=0)
    fixture.add_node("video", "channel-a", "Video", kind="video", parent_id="root-a", lft=2, rght=3)
    fixture.add_video("video", preset="high_res_video", variant=True)
    fixture.close()
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(index_dir, kolibri_config(fixture))
    try:
        source = runtime.library_sources()[0]
        first = runtime.entry_producer(source, fast=False)
        first_path = first.entry_at(0).path
        # Reorder the physical file rows without changing their meaning.
        fixture.db.execute(
            "UPDATE content_file SET priority = CASE WHEN preset = 'high_res_video' "
            "THEN 1 ELSE 2 END"
        )
        fixture.db.commit()
        second = runtime.entry_producer(source, fast=False)
        assert second.entry_at(0).path == first_path
        assert second.fingerprint == first.fingerprint
    finally:
        fixture.db.close()
        runtime.close()
        batch.close()


def test_ambiguous_primary_mapping_is_skipped_with_an_error(tmp_path):
    fixture = KolibriFixture(tmp_path)
    fixture.add_channel("channel-a", "Channel A", "root-a")
    fixture.add_node("root-a", "channel-a", "Channel A", kind="topic", lft=1, rght=4, level=0)
    fixture.add_node("doc", "channel-a", "Ambiguous", kind="document", parent_id="root-a", lft=2, rght=3)
    fixture.add_file("doc", b"one", "pdf", preset="document")
    fixture.add_file("doc", b"two", "pdf", preset="document")
    fixture.close()
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(index_dir, kolibri_config(fixture))
    try:
        source = runtime.library_sources()[0]
        producer = runtime.entry_producer(source, fast=False)
        assert producer.count == 0
        assert any("ambiguous" in error for error in producer.errors)
        store = runtime.content_store(source)
        try:
            from zimantic.content.store import ArticleUnavailableError

            with pytest.raises(ArticleUnavailableError):
                store.read("node/doc", head_only=False, max_bytes=1 << 20)
        finally:
            store.close()
    finally:
        runtime.close()
        batch.close()


def test_captions_are_absent_by_default_and_indexed_when_enabled(tmp_path):
    fixture = KolibriFixture(tmp_path)
    fixture.add_channel("channel-a", "Channel A", "root-a")
    fixture.add_node("root-a", "channel-a", "Channel A", kind="topic", lft=1, rght=4, level=0)
    fixture.add_node("video", "channel-a", "Lesson Video", kind="video", parent_id="root-a", lft=2, rght=3)
    fixture.add_video(
        "video",
        captions=(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n"
            "caption zebra\n\n00:00:01.000 --> 00:00:02.000\n"
            "caption zebra\n"
        ).encode(),
    )
    fixture.close()
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(index_dir, kolibri_config(fixture))
    try:
        source = runtime.library_sources()[0]
        off = runtime.entry_producer(source, fast=False)
        assert off.derived_text_inputs(0) == ()
        locator = IndexLocator(index_dir)
        build_index(off, locator, None, {"batch_size": 1}, fast=False)
        assert _body_hits(locator, source, "caption") == []

        operations = {source.key: SourceDerivedOperations(captions=True)}
        cfg = {
            "max_entry_bytes": 1 << 20,
            "max_body_text_bytes": 1 << 20,
            "max_preview_chars": 1000,
            "derived_source_operations": operations,
            "derived_text": DerivedTextConfig(),
        }
        on = runtime.entry_producer(source, fast=False)
        on.cfg.update(cfg)
        on.captions_enabled = True
        # Rebuild the manifest/fingerprint with captions included.
        on.__init__(
            source,
            runtime._snapshot,
            channel=runtime.channel,
            scope=runtime._by_key[source.key].scope,
            storage_root=runtime.storage_root,
            config=runtime.config,
            fast=False,
            cfg=cfg,
            snapshot_factory=runtime._manager.acquire,
        )
        assert [item.kind for item in on.derived_text_inputs(0)] == ["captions"]
        build_index(
            on,
            locator,
            None,
            {"batch_size": 1, "max_body_text_bytes": 1 << 20, **cfg},
            fast=False,
        )
        hits = _body_hits(locator, source, "zebra")
        assert hits
        docs = _docs(locator, source)
        assert len(docs) == 1
    finally:
        runtime.close()
        batch.close()


def test_fingerprint_tracks_relevant_changes_and_validation_detects_writes(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(index_dir, kolibri_config(fixture))
    try:
        topic_key = kolibri_node_source_key("kolibri", "channel-a", "topic-1")
        source = next(
            item for item in runtime.library_sources() if item.key == topic_key
        )
        producer = runtime.entry_producer(source, fast=False)
        fingerprint = producer.fingerprint
        assert producer.validate_snapshot() is True

        # An unrelated channel change does not affect this source.
        fixture.db.execute(
            "UPDATE content_contentnode SET title = 'Other' WHERE id = 'audio-1'"
        )
        fixture.db.commit()
        unrelated = runtime.entry_producer(source, fast=False)
        assert unrelated.fingerprint == fingerprint

        # A relevant description, availability, or primary file change does.
        fixture.db.execute(
            "UPDATE content_contentnode SET description = 'changed' WHERE id = 'doc-1'"
        )
        fixture.db.commit()
        changed = runtime.entry_producer(source, fast=False)
        assert changed.fingerprint != fingerprint
        assert producer.validate_snapshot() is False
    finally:
        fixture.db.close()
        runtime.close()
        batch.close()
