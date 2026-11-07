"""Phase-09 hardening gates: public diagnostics and build observability."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

from zimantic.catalog import SourceCatalog
from zimantic.contracts import SourceInfo
from zimantic.diagnostics import private_locators, public_cause, sanitize_public_text
from zimantic.indexing.locator import IndexLocator
from zimantic.indexing.pipeline import build_index
from zimantic.indexing.types import EntryRecord


class Producer:
    def __init__(self, rows, *, key="files:test:source-abc"):
        self.rows = list(rows)
        self.key = key
        self.provider = "files"
        self.collection_key = "files:test"
        self.fingerprint = "fp"
        self.count = len(self.rows)
        self.truncated = True
        self.closed = False

    def entry_at(self, index):
        if 0 <= index < self.count:
            return self.rows[index]
        return None

    def validate_snapshot(self):
        return True

    def close(self):
        self.closed = True


def test_public_diagnostics_redact_configured_and_windows_roots():
    root = "/srv/private/library"
    locators = private_locators({"index_dir": root, "collections": []})
    text = (
        f"cannot read {root}/missing.html or {root} "
        r"or C:\Users\alice\private\db.sqlite3"
    )
    public = sanitize_public_text(text, locators)
    assert root not in str(public)
    assert "alice" not in str(public)
    assert "<private>" in str(public)


def test_public_cause_never_echoes_an_os_filename(tmp_path):
    secret = tmp_path / "private-root" / "article.html"
    try:
        open(secret, "rb")
    except OSError as error:
        caused = public_cause(error)
    else:  # pragma: no cover - the path must not exist
        raise AssertionError("expected a missing file")
    assert str(tmp_path) not in caused
    assert caused == "not found"


def test_catalog_rebuild_warning_does_not_expose_the_database_path(tmp_path, caplog):
    private = tmp_path / "private-index-directory" / "catalog.sqlite"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"not a sqlite database")
    with caplog.at_level(logging.WARNING, logger="zimantic.catalog"):
        catalog = SourceCatalog(private)
        catalog.close()
    assert "private-index-directory" not in caplog.text
    assert "catalog.sqlite" not in caplog.text


def test_build_report_carries_counts_truncation_and_duration():
    rows = [
        EntryRecord(1, "Alpha", "short", "alpha", None, "alpha body"),
        EntryRecord(2, "Beta", "short", "beta", None, "beta body", True),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        report = build_index(
            Producer(rows),
            IndexLocator(tmp),
            None,
            {"batch_size": 1, "max_body_text_bytes": 1 << 20},
            fast=False,
        )
    payload = report.to_dict()
    assert payload["provider"] == "files"
    assert payload["collection"] == "files:test"
    assert payload["source"] == "files:test:source-abc"
    assert payload["status"] == "published"
    assert payload["indexed_count"] == 2
    assert payload["skipped_count"] == 0
    assert payload["truncated"] is True
    assert payload["body_truncated_count"] == 1
    assert payload["duration_seconds"] >= 0


def test_collection_snapshot_reports_health_fields(tmp_path):
    from providers.files.helpers import files_config, open_runtime, build_tree

    root = tmp_path / "modules"
    build_tree(root)
    runtime, batch, _ = open_runtime(tmp_path / "indexes", files_config(root))
    try:
        assert runtime.snapshot.last_refresh is not None
        assert runtime.snapshot.index_count == 0
        assert runtime.snapshot.collapsed is False

        from providers.files.helpers import build_sources

        build_sources(runtime, tmp_path / "indexes", fast=True)
        snapshot = runtime.snapshot
        assert snapshot.index_count >= 1
        assert "fast" in snapshot.index_states
        assert snapshot.indexed_count is not None
        assert snapshot.last_refresh is not None
        payload = snapshot.to_dict()
        assert payload["index_count"] == snapshot.index_count
        assert payload["index_states"] == list(snapshot.index_states)
    finally:
        runtime.close()
        batch.close()


def test_source_payload_has_no_private_locator():
    source = SourceInfo(
        "files:test:root",
        provider="files",
        title="Root",
        collection_key="files:test",
        collection_label="Test",
    )
    payload = source.to_dict(include_metadata=True)
    for key, value in payload.items():
        assert "/tmp/" not in str(value), key
        assert "private" not in str(value).casefold(), key
