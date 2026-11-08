"""Read-only snapshots, WAL visibility, and failure containment."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from zimantic.providers.kolibri import KolibriProvider, SnapshotError, SnapshotManager
from zimantic.providers.kolibri.discovery import kolibri_node_source_key

from .helpers import (
    KolibriFixture,
    context,
    kolibri_config,
    make_two_channel_fixture,
    open_runtime,
    runtime_config,
)


def _signature(path: Path):
    try:
        info = path.stat()
        return (
            info.st_size,
            info.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    except FileNotFoundError:
        return None


def _installation_signatures(fixture: KolibriFixture) -> dict[str, object]:
    paths = [
        fixture.db_path,
        fixture.db_path.with_name(fixture.db_path.name + "-wal"),
        fixture.db_path.with_name(fixture.db_path.name + "-shm"),
        fixture.db_path.with_name(fixture.db_path.name + "-journal"),
    ]
    return {str(path): _signature(path) for path in paths}


def test_live_wal_is_read_without_modifying_the_installation(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    fixture.db.close()
    writer = sqlite3.connect(fixture.db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute(
            "UPDATE content_contentnode SET description = 'uncommitted-wal-marker' "
            "WHERE id = 'doc-1'"
        )
        writer.commit()
        before = _installation_signatures(fixture)
        index_dir = tmp_path / "indexes"
        runtime, batch, _ = open_runtime(index_dir, kolibri_config(fixture))
        try:
            assert _installation_signatures(fixture) == before
            key = kolibri_node_source_key("kolibri", "channel-a", "topic-1")
            source = next(
                item for item in runtime.library_sources() if item.key == key
            )
            producer = runtime.entry_producer(source, fast=False)
            assert "uncommitted-wal-marker" in producer.entry_at(0).body
            assert _installation_signatures(fixture) == before

            writer.execute(
                "UPDATE content_contentnode SET description = 'second-wal-marker' "
                "WHERE id = 'doc-1'"
            )
            writer.commit()
            assert runtime.refresh(wait=True)
            producer = runtime.entry_producer(source, fast=False)
            assert "second-wal-marker" in producer.entry_at(0).body
        finally:
            runtime.close()
            batch.close()
    finally:
        writer.close()


def test_declared_postgresql_is_reported_unsupported(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    (fixture.home / "options.ini").write_text(
        "[Database]\nDATABASE_ENGINE = postgres\n", encoding="utf-8"
    )
    index_dir = tmp_path / "indexes"
    config = kolibri_config(fixture)
    batch = KolibriProvider().discover(
        config, context(index_dir, runtime_config(index_dir, config))
    )
    try:
        assert list(batch.collections) == []
        assert "postgres" in (batch.error or "").lower()
    finally:
        batch.close()


def test_snapshot_size_cap_fails_closed(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    index_dir = tmp_path / "indexes"
    config = kolibri_config(fixture, max_snapshot_bytes=128)
    batch = KolibriProvider().discover(
        config, context(index_dir, runtime_config(index_dir, config))
    )
    try:
        assert list(batch.collections) == []
        assert "exceed" in (batch.error or "").lower()
    finally:
        batch.close()


def test_disk_budget_prevents_unbounded_retained_snapshots(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    index_dir = tmp_path / "indexes"
    size = fixture.db_path.stat().st_size
    manager = SnapshotManager(
        database_path=fixture.db_path,
        storage_path=fixture.storage,
        home_path=fixture.home,
        snapshot_root=index_dir / "snapshots",
        max_snapshot_bytes=size * 3,
        disk_budget_bytes=size * 3,
    )
    retained = [manager.acquire(), manager.acquire()]
    try:
        with pytest.raises(SnapshotError):
            for _ in range(8):
                retained.append(manager.acquire())
    finally:
        for snapshot in retained:
            snapshot.close()


def test_failed_refresh_retains_the_last_complete_batch(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(index_dir, kolibri_config(fixture))
    try:
        keys = {source.key for source in runtime.library_sources()}
        assert keys
        fixture.db.close()
        db = sqlite3.connect(fixture.db_path)
        db.execute("DROP TABLE content_channelmetadata")
        db.commit()
        db.close()
        assert runtime.refresh(wait=True) is False
        assert {source.key for source in runtime.library_sources()} == keys
        assert runtime.snapshot.error
    finally:
        runtime.close()
        batch.close()
