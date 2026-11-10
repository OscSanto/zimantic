import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from index_helpers import body_hits as _body_hits
from index_helpers import docs as _docs
from index_helpers import meta as _meta

from zimantic.contracts import SourceInfo
from zimantic.indexing import pipeline as build_module
from zimantic.indexing.locator import IndexLocator
from zimantic.indexing.schema import (
    BODY_TRUNCATED_COUNT,
    DONE,
    NEXT,
    SCHEMA,
)
from zimantic.indexing.types import EntryRecord


def _source(key="files:test:manual-0123", provider="files", collection_key="files:test"):
    return SourceInfo(
        key=key, provider=provider, collection_key=collection_key
    )


class Producer:
    def __init__(
        self,
        rows,
        *,
        source=None,
        fingerprint="fingerprint-1",
        truncated=False,
        fail_at=None,
        interrupt_at=None,
        snapshot_valid=True,
    ):
        self.rows = list(rows)
        self.source = source or _source()
        self.key = self.source.key
        self.provider = self.source.provider
        self.collection_key = self.source.collection_key
        self.source_size = 0
        self.fingerprint = fingerprint
        self.count = len(self.rows)
        self.truncated = truncated
        self.fail_at = fail_at
        self.interrupt_at = interrupt_at
        self.snapshot_valid = snapshot_valid
        self.closed = False

    def entry_at(self, index):
        if not isinstance(index, int) or index < 0 or index >= self.count:
            return None
        if index == self.fail_at:
            raise RuntimeError("parse failed")
        if index == self.interrupt_at:
            raise KeyboardInterrupt
        return self.rows[index]

    def validate_snapshot(self):
        return self.snapshot_valid

    def close(self):
        self.closed = True


def _build(producer, directory, cfg=None, **kwargs):
    locator = IndexLocator(directory)
    config = {"batch_size": 1}
    config.update(cfg or {})
    build_module.build_index(producer, locator, kwargs.pop("embedder", None), config, **kwargs)
    return locator


class _Embedder:
    genome = "embedder"
    fingerprint = "test-model"

    def truncate(self, text, prefix=""):
        return text

    def embed(self, texts):
        return np.ones((len(texts), 4), dtype=np.float32)

    def encode(self, texts):
        return [list(map(ord, text)) for text in texts]

    def embed_encoded(self, inputs):
        return self.embed(["".join(map(chr, tokens)) for tokens in inputs])


class _FingerprintEmbedder(_Embedder):
    fingerprint = "model-fingerprint-1"


def _write_fake_faiss(_db, path):
    path.write_bytes(b"vectors")


class BuildPipelineTests(unittest.TestCase):
    def test_fast_build_has_no_body_or_vectors_and_full_build_upgrades(self):
        rows = [
            EntryRecord(0, "Alpha", "Alpha excerpt", "a", None, "Alpha body"),
            EntryRecord(1, "Beta", "Beta excerpt", "b", None, ""),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            locator = _build(Producer(rows), tmp, fast=True)
            source = rows and _source()
            meta = _meta(locator, source)
            self.assertEqual(meta[DONE], "fast")
            self.assertEqual(meta["indexed_channels"], '["title"]')
            self.assertNotIn("body_truncated_count", meta)
            lease = locator.paths(source)
            assert lease is not None
            self.assertIsNone(lease.faiss_path)
            lease.close()

            with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                locator = _build(
                    Producer(rows, fingerprint="fingerprint-2"),
                    tmp,
                    {"batch_size": 2},
                    embedder=_Embedder(),
                )
            meta = _meta(locator, source)
            self.assertEqual(meta[DONE], "full")
            self.assertEqual(meta["indexed_channels"], '["title","body","semantic"]')
            self.assertEqual(meta["semantic_vector_count"], "2")
            self.assertIn("faiss_digest", meta)
            lease = locator.paths(source)
            assert lease is not None
            self.assertIsNotNone(lease.faiss_path)
            lease.close()
            self.assertEqual(_body_hits(locator, source, "Alpha"), [(0,)])

    def test_body_after_the_first_thousand_characters_is_searchable(self):
        body = ("filler " * 300) + "needleword appears late"
        rows = [EntryRecord(1, "Long", "short excerpt", "long", None, body)]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                locator = _build(Producer(rows), tmp, embedder=_Embedder())
            self.assertEqual(_body_hits(locator, _source(), "needleword"), [(1,)])
            meta = _meta(locator, _source())
            self.assertEqual(meta[BODY_TRUNCATED_COUNT], "0")

    def test_truncated_body_count_is_separate_from_item_cap(self):
        rows = [
            EntryRecord(1, "A", "", "a", None, "short", False),
            EntryRecord(2, "B", "", "b", None, "cut body", True),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                locator = _build(
                    Producer(rows, truncated=True), tmp, embedder=_Embedder()
                )
            meta = _meta(locator, _source())
            self.assertEqual(meta[BODY_TRUNCATED_COUNT], "1")
            self.assertEqual(meta["truncated"], "1")
            self.assertEqual(meta["indexed_count"], "2")

    def test_empty_full_build_publishes_without_a_faiss_dimension(self):
        rows = [EntryRecord(1, "Title only", "", "title", None, "", False)]
        with tempfile.TemporaryDirectory() as tmp:
            locator = _build(Producer(rows), tmp, embedder=None)
            meta = _meta(locator, _source())
            self.assertEqual(meta[DONE], "full")
            self.assertEqual(meta["semantic_vector_count"], "0")
            self.assertNotIn("faiss_digest", meta)
            lease = locator.paths(_source())
            assert lease is not None
            self.assertIsNone(lease.faiss_path)
            lease.close()

    def test_force_fast_rebuild_replaces_full_generation_without_faiss(self):
        full = [EntryRecord(1, "Old", "excerpt", "old", None, "old body")]
        fast = [EntryRecord(2, "New", "", "new", None, "")]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                locator = _build(Producer(full), tmp, embedder=_Embedder())
            first = locator.paths(_source())
            assert first is not None
            first_generation = first.generation
            first.close()

            locator = _build(Producer(fast, fingerprint="fast"), tmp, fast=True, force=True)
            meta = _meta(locator, _source())
            self.assertEqual(meta[DONE], "fast")
            self.assertEqual(_docs(locator, _source()), [(2, "New", "new")])
            lease = locator.paths(_source())
            assert lease is not None
            self.assertNotEqual(lease.generation, first_generation)
            self.assertIsNone(lease.faiss_path)
            lease.close()

    def test_failed_force_rebuild_preserves_last_good_generation_and_warns(self):
        good = [EntryRecord(1, "Old", "excerpt", "old", None, "old body")]
        bad = [EntryRecord(2, "New", "excerpt", "new", None, "new body")]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                locator = _build(Producer(good), tmp, embedder=_Embedder())
            lease = locator.paths(_source())
            assert lease is not None
            try:
                generation = lease.generation
            finally:
                lease.close()
            with (
                self.assertLogs("zimantic.indexing.pipeline", level="WARNING") as logs,
                self.assertRaisesRegex(RuntimeError, "parse failed"),
            ):
                _build(
                    Producer(bad, fail_at=0),
                    tmp,
                    embedder=_Embedder(),
                    force=True,
                )
            self.assertTrue(
                any("previous published generation" in message for message in logs.output)
            )
            lease = locator.paths(_source())
            assert lease is not None
            try:
                self.assertEqual(lease.generation, generation)
            finally:
                lease.close()
            self.assertEqual(_docs(locator, _source()), [(1, "Old", "old")])
            directory = locator.source_directory(
                "files", "files:test", "files:test:manual-0123"
            )
            self.assertFalse((directory / "staging-full.sqlite").exists())

    def test_model_fingerprint_and_probes_are_stored_then_removed_by_fast_build(self):
        rows = [EntryRecord(1, "Title", "Excerpt", "path", None, "body")]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                locator = _build(Producer(rows), tmp, embedder=_FingerprintEmbedder())
            meta = _meta(locator, _source())
            self.assertEqual(meta["model_fingerprint"], "model-fingerprint-1")
            self.assertIn("model_probe_texts", meta)
            self.assertIn("model_probe_vectors", meta)
            self.assertIn("model_probe_shape", meta)
            from zimantic.embed import load_model_probes

            probes = load_model_probes(meta)
            self.assertIsNotNone(probes)
            np.testing.assert_array_equal(
                probes[1], np.ones((len(probes[0]), 4), dtype=np.float32)
            )

            locator = _build(
                Producer(rows, fingerprint="fast"), tmp, fast=True, force=True
            )
            meta = _meta(locator, _source())
            self.assertNotIn("model_fingerprint", meta)
            self.assertNotIn("model_probe_texts", meta)
            self.assertNotIn("model_probe_vectors", meta)
            self.assertNotIn("model_probe_shape", meta)

    def test_interrupted_build_resumes_from_the_last_committed_batch(self):
        rows = [
            EntryRecord(i, f"Title {i}", f"Excerpt {i}", f"path-{i}", None, f"body {i}")
            for i in range(1, 9)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KeyboardInterrupt):
                _build(
                    Producer(rows, interrupt_at=4),
                    tmp,
                    {"batch_size": 2},
                    embedder=_Embedder(),
                )
            directory = IndexLocator(tmp).source_directory(
                "files", "files:test", "files:test:manual-0123"
            )
            db = sqlite3.connect(directory / "staging-full.sqlite")
            try:
                meta = dict(db.execute("SELECT key, value FROM meta"))
            finally:
                db.close()
            self.assertEqual(meta[NEXT], "4")
            self.assertNotIn(DONE, meta)

            with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                locator = _build(
                    Producer(rows), tmp, {"batch_size": 2}, embedder=_Embedder()
                )
            self.assertEqual(_meta(locator, _source())[DONE], "full")
            self.assertEqual(len(_docs(locator, _source())), 8)

    def test_interrupt_event_stops_before_reading_entries(self):
        rows = [EntryRecord(i, f"T{i}", "e", f"p{i}", None, "body") for i in range(2)]
        producer = Producer(rows)
        stop_event = threading.Event()
        stop_event.set()

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(KeyboardInterrupt):
            _build(
                producer,
                tmp,
                {"interrupt_event": stop_event},
                fast=True,
            )

        self.assertTrue(producer.closed)

    def test_resume_requires_order_fingerprint_and_rejects_changed_order(self):
        batch_size = build_module.FAST_BUILD_BATCH_SIZE
        rows = [EntryRecord(i, f"T{i}", "", f"p{i}") for i in range(batch_size + 2)]

        class OrderedProducer(Producer):
            order_fingerprint = "order-a"

            def prepare_iteration(self, order):
                self.requested_order = order
                return order or "efficient-v1"

        for missing_manifest in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                first = OrderedProducer(rows, interrupt_at=batch_size + 1)
                with self.assertRaises(KeyboardInterrupt):
                    _build(first, tmp, fast=True)
                directory = IndexLocator(tmp).source_directory(
                    "files", "files:test", "files:test:manual-0123"
                )
                db = sqlite3.connect(directory / "staging-fast.sqlite")
                try:
                    meta = dict(db.execute("SELECT key, value FROM meta"))
                    self.assertEqual(meta[NEXT], str(batch_size))
                    if missing_manifest:
                        db.execute("DELETE FROM meta WHERE key IN ('entry_order', 'entry_order_fingerprint')")
                        db.commit()
                finally:
                    db.close()
                resumed = OrderedProducer(rows)
                if missing_manifest:
                    with self.assertRaisesRegex(
                        build_module.SnapshotChangedError,
                        "no reading-order fingerprint",
                    ):
                        _build(resumed, tmp, fast=True)
                else:
                    resumed.order_fingerprint = "order-b"
                    with self.assertRaisesRegex(build_module.SnapshotChangedError, "reading order changed"):
                        _build(resumed, tmp, fast=True)

    def test_changed_fingerprint_discards_staging(self):
        rows = [EntryRecord(i, f"T{i}", "e", f"p{i}", None, "body") for i in range(4)]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KeyboardInterrupt):
                _build(
                    Producer(rows, interrupt_at=2, fingerprint="first"),
                    tmp,
                    {"batch_size": 1},
                    embedder=_Embedder(),
                )
            directory = IndexLocator(tmp).source_directory(
                "files", "files:test", "files:test:manual-0123"
            )
            staging = directory / "staging-full.sqlite"
            db = sqlite3.connect(staging)
            self.assertIsNotNone(
                db.execute("SELECT 1 FROM docs WHERE rowid = 1").fetchone()
            )
            db.close()

            with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                locator = _build(
                    Producer(rows, fingerprint="second"),
                    tmp,
                    {"batch_size": 1},
                    embedder=_Embedder(),
                )
            self.assertEqual(_meta(locator, _source())["source_fingerprint"], "second")
            self.assertEqual(len(_docs(locator, _source())), 4)

    def test_snapshot_change_during_build_prevents_publication(self):
        rows = [EntryRecord(1, "T", "e", "p", None, "body")]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(build_module.SnapshotChangedError):
                _build(
                    Producer(rows, snapshot_valid=False),
                    tmp,
                    embedder=_Embedder(),
                )
            self.assertIsNone(IndexLocator(tmp).paths(_source()))

    def test_insufficient_space_fails_before_touching_the_published_generation(self):
        rows = [EntryRecord(1, "T", "e", "p", None, "body")]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                locator = _build(Producer(rows), tmp, embedder=_Embedder())
            lease = locator.paths(_source())
            assert lease is not None
            try:
                generation = lease.generation
            finally:
                lease.close()

            fake = type("usage", (), {"free": 1, "total": 1, "used": 0})()
            with (
                patch.object(build_module.shutil, "disk_usage", return_value=fake),
                self.assertRaises(build_module.InsufficientSpaceError),
            ):
                _build(
                    Producer(rows, fingerprint="new"),
                    tmp,
                    embedder=_Embedder(),
                    force=True,
                )
            lease = locator.paths(_source())
            assert lease is not None
            try:
                self.assertEqual(lease.generation, generation)
            finally:
                lease.close()

    def test_corrupt_staging_generation_is_discarded_and_rebuilt(self):
        rows = [EntryRecord(1, "T", "e", "p", None, "body")]
        with tempfile.TemporaryDirectory() as tmp:
            directory = IndexLocator(tmp).source_directory(
                "files", "files:test", "files:test:manual-0123"
            )
            directory.mkdir(parents=True)
            staging = directory / "staging-full.sqlite"
            staging.write_bytes(b"not a sqlite database")
            with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                locator = _build(Producer(rows), tmp, embedder=_Embedder())
            self.assertEqual(_meta(locator, _source())[DONE], "full")
            self.assertFalse(staging.exists())

    def test_producer_is_closed_on_success_and_failure(self):
        rows = [EntryRecord(1, "T", "e", "p", None, "body")]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                producer = Producer(rows)
                _build(producer, tmp, embedder=_Embedder())
            self.assertTrue(producer.closed)

            failing = Producer(rows, fail_at=0)
            with self.assertRaises(RuntimeError):
                _build(failing, tmp, embedder=_Embedder(), force=True)
            self.assertTrue(failing.closed)


class BodyBudgetTests(unittest.TestCase):
    def test_pipeline_drains_before_waiting_for_its_own_body_budget(self):
        rows = [
            EntryRecord(i, f"T{i}", "excerpt", f"p{i}", None, "123456")
            for i in range(4)
        ]
        budget = build_module.BodyBudget(10)
        pipeline = build_module.EmbeddingPipeline(_Embedder(), workers=2)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss):
                    locator = _build(
                        Producer(rows), tmp,
                        {"batch_size": 32, "body_budget": budget, "embedding_pipeline": pipeline},
                        embedder=_Embedder(),
                    )
                self.assertEqual(len(_docs(locator, _source())), 4)
                self.assertEqual(budget.reserved, 0)
        finally:
            pipeline.close()

    def test_concurrent_builds_make_progress_with_a_tiny_shared_budget(self):
        """Four builds sharing 10 bytes must flush/drain instead of all blocking.

        This reproduces the original deadlock: each build held an unflushed
        batch in reserve(), so no thread could release the budget.  The build
        threads are daemon threads so a regression fails the assertion rather
        than hanging the test run.
        """
        rows = [
            EntryRecord(i, f"T{i}", "excerpt", f"p{i}", None, "123456")
            for i in range(4)
        ]
        budget = build_module.BodyBudget(10)
        pipeline = build_module.EmbeddingPipeline(_Embedder(), workers=2)
        failures: list[BaseException] = []

        def run(number: int) -> None:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    _build(
                        Producer(
                            rows,
                            source=_source(f"files:test:source-{number}"),
                        ),
                        tmp,
                        {
                            "batch_size": 32,
                            "body_budget": budget,
                            "embedding_pipeline": pipeline,
                        },
                        embedder=_Embedder(),
                    )
            except BaseException as error:  # noqa: BLE001 - surfaced below
                failures.append(error)

        threads = [
            threading.Thread(target=run, args=(number,), daemon=True)
            for number in range(4)
        ]
        try:
            # Patch once around every thread: concurrent patch.object()
            # enter/exit is not thread-safe and would leak the mock.
            with patch.object(
                build_module, "_write_faiss", side_effect=_write_fake_faiss
            ):
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
            self.assertFalse(
                [thread for thread in threads if thread.is_alive()],
                "concurrent builds deadlocked on the shared body budget",
            )
            self.assertEqual(failures, [])
            self.assertEqual(budget.reserved, 0)
        finally:
            pipeline.close()

    def test_concurrent_reservations_are_bounded_and_released(self):
        budget = build_module.BodyBudget(10)
        budget.reserve(6)

        def second():
            budget.reserve(6)
            budget.release(6)

        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.02)
        self.assertEqual(budget.reserved, 6)
        budget.release(6)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(budget.reserved, 0)

    def test_failed_batches_release_reservations(self):
        class _FailingEmbedder(_Embedder):
            def embed(self, texts):
                raise RuntimeError("embed failed")

        rows = [EntryRecord(1, "T", "e", "p", None, "body")]
        with tempfile.TemporaryDirectory() as tmp:
            budget = build_module.BodyBudget(10)
            with self.assertRaisesRegex(RuntimeError, "embed failed"):
                _build(
                    Producer(rows),
                    tmp,
                    {"body_budget": budget},
                    embedder=_FailingEmbedder(),
                )
            self.assertEqual(budget.reserved, 0)


def _stage_threads():
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("zimantic-stage-")
    ]


class _GatePipeline:
    """Preparation-stage stand-in that blocks before returning a future."""

    def __init__(self, limit):
        self.max_pending_per_build = limit
        self.started = threading.Event()
        self.release = threading.Event()

    def submit(self, _rows, *, cancel_event=None):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("preparation was not released")
        future = Future()
        future.set_result({})
        return future


class StageOverlapTests(unittest.TestCase):
    def test_reading_advances_while_preparation_is_blocked(self):
        limit = 3
        positions = []
        reached = threading.Event()

        class TrackingProducer(Producer):
            def entry_at(self, index):
                positions.append(index)
                if len(positions) >= limit:
                    reached.set()
                return super().entry_at(index)

        rows = [
            EntryRecord(i, f"T{i}", "excerpt", f"p{i}", None, "body")
            for i in range(limit + 3)
        ]
        pipeline = _GatePipeline(limit)
        with tempfile.TemporaryDirectory() as tmp:
            with ThreadPoolExecutor(max_workers=1) as reader:
                build = reader.submit(
                    _build,
                    TrackingProducer(rows),
                    tmp,
                    {"batch_size": 1, "embedding_pipeline": pipeline},
                    embedder=_Embedder(),
                )
                try:
                    self.assertTrue(pipeline.started.wait(timeout=2))
                    self.assertTrue(reached.wait(timeout=2))
                    self.assertEqual(positions, list(range(limit)))
                    directory = IndexLocator(tmp).source_directory(
                        "files", "files:test", "files:test:manual-0123"
                    )
                    db = sqlite3.connect(directory / "staging-full.sqlite")
                    try:
                        self.assertEqual(
                            db.execute("SELECT COUNT(*) FROM docs").fetchone()[0], 0
                        )
                    finally:
                        db.close()
                finally:
                    pipeline.release.set()
                locator = build.result(timeout=5)
            self.assertEqual(
                [row[0] for row in _docs(locator, _source())],
                list(range(limit + 3)),
            )
        self.assertEqual(_stage_threads(), [])

    def test_reading_advances_while_writer_is_blocked(self):
        limit = 3
        positions = []
        reached = threading.Event()
        save_started = threading.Event()
        release_save = threading.Event()

        class TrackingProducer(Producer):
            def entry_at(self, index):
                positions.append(index)
                if len(positions) >= limit:
                    reached.set()
                return super().entry_at(index)

        original_save_rows = build_module._save_rows

        def blocking_save(db, rows, next_entry, vectors, **kwargs):
            save_started.set()
            if not release_save.wait(timeout=5):
                raise TimeoutError("writer was not released")
            return original_save_rows(db, rows, next_entry, vectors, **kwargs)

        rows = [
            EntryRecord(i, f"T{i}", "excerpt", f"p{i}", None, "body")
            for i in range(limit + 3)
        ]
        pipeline = build_module.EmbeddingPipeline(_Embedder(), workers=2)
        try:
            with (
                tempfile.TemporaryDirectory() as tmp,
                ThreadPoolExecutor(max_workers=1) as reader,
                patch.object(build_module, "_save_rows", side_effect=blocking_save),
                patch.object(
                    build_module,
                    "_write_faiss",
                    side_effect=_write_fake_faiss,
                ),
            ):
                build = reader.submit(
                    _build,
                    TrackingProducer(rows),
                    tmp,
                    {"batch_size": 1, "embedding_pipeline": pipeline},
                    embedder=_Embedder(),
                )
                try:
                    self.assertTrue(save_started.wait(timeout=2))
                    self.assertTrue(reached.wait(timeout=2))
                    self.assertEqual(positions, list(range(limit)))
                finally:
                    release_save.set()
                locator = build.result(timeout=5)
                self.assertEqual(
                    [row[0] for row in _docs(locator, _source())],
                    list(range(limit + 3)),
                )
        finally:
            release_save.set()
            pipeline.close()
        self.assertEqual(_stage_threads(), [])

    def test_slow_first_window_commits_every_window_in_order(self):
        save_order = []
        original_save_rows = build_module._save_rows

        def recording_save(db, rows, next_entry, vectors, **kwargs):
            save_order.append(next_entry)
            return original_save_rows(db, rows, next_entry, vectors, **kwargs)

        class SlowFirstEmbedder(_Embedder):
            def __init__(self):
                self.slow_started = threading.Event()
                self.release_slow = threading.Event()
                self.later_finished = threading.Event()

            def embed(self, texts):
                if texts and texts[0].startswith("passage: T0"):
                    self.slow_started.set()
                    if not self.release_slow.wait(timeout=5):
                        raise TimeoutError("slow batch was not released")
                if texts and texts[0].startswith("passage: T4"):
                    self.later_finished.set()
                return super().embed(texts)

        rows = [
            EntryRecord(i, f"T{i}", "excerpt", f"p{i}", None, "body")
            for i in range(5)
        ]
        embedder = SlowFirstEmbedder()
        pipeline = build_module.EmbeddingPipeline(
            embedder, workers=2, max_pending_per_build=8
        )
        try:
            with (
                tempfile.TemporaryDirectory() as tmp,
                ThreadPoolExecutor(max_workers=1) as reader,
                patch.object(build_module, "_save_rows", side_effect=recording_save),
                patch.object(
                    build_module,
                    "_write_faiss",
                    side_effect=_write_fake_faiss,
                ),
            ):
                build = reader.submit(
                    _build,
                    Producer(rows),
                    tmp,
                    {"batch_size": 1, "embedding_pipeline": pipeline},
                    embedder=embedder,
                )
                try:
                    self.assertTrue(embedder.slow_started.wait(timeout=2))
                    self.assertTrue(embedder.later_finished.wait(timeout=2))
                    self.assertEqual(save_order, [])
                finally:
                    embedder.release_slow.set()
                locator = build.result(timeout=5)
                self.assertEqual(save_order, [1, 2, 3, 4, 5])
                self.assertEqual(
                    [row[0] for row in _docs(locator, _source())],
                    list(range(5)),
                )
        finally:
            embedder.release_slow.set()
            pipeline.close()

    def test_interrupt_keeps_only_committed_windows_and_resumes(self):
        first_committed = threading.Event()
        original_save_rows = build_module._save_rows

        def signalling_save(db, rows, next_entry, vectors, **kwargs):
            result = original_save_rows(db, rows, next_entry, vectors, **kwargs)
            if next_entry == 1:
                first_committed.set()
            return result

        class SlowSecondEmbedder(_Embedder):
            def __init__(self):
                self.release_slow = threading.Event()

            def embed(self, texts):
                if (
                    texts
                    and texts[0].startswith("passage: T1")
                    and not self.release_slow.wait(timeout=5)
                ):
                    raise TimeoutError("slow batch was not released")
                return super().embed(texts)

        rows = [
            EntryRecord(i, f"T{i}", "excerpt", f"p{i}", None, "body")
            for i in range(4)
        ]
        embedder = SlowSecondEmbedder()
        pipeline = build_module.EmbeddingPipeline(
            embedder, workers=2, max_pending_per_build=8
        )
        stop_event = threading.Event()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with (
                    ThreadPoolExecutor(max_workers=1) as reader,
                    patch.object(
                        build_module, "_save_rows", side_effect=signalling_save
                    ),
                ):
                    build = reader.submit(
                        _build,
                        Producer(rows),
                        tmp,
                        {
                            "batch_size": 1,
                            "embedding_pipeline": pipeline,
                            "interrupt_event": stop_event,
                        },
                        embedder=embedder,
                    )
                    try:
                        self.assertTrue(first_committed.wait(timeout=2))
                        stop_event.set()
                        with self.assertRaises(KeyboardInterrupt):
                            build.result(timeout=2)
                    finally:
                        embedder.release_slow.set()
                directory = IndexLocator(tmp).source_directory(
                    "files", "files:test", "files:test:manual-0123"
                )
                db = sqlite3.connect(directory / "staging-full.sqlite")
                try:
                    meta = dict(db.execute("SELECT key, value FROM meta"))
                    self.assertEqual(meta[NEXT], "1")
                    self.assertNotIn(DONE, meta)
                    self.assertEqual(
                        [row[0] for row in db.execute("SELECT rowid FROM docs")],
                        [0],
                    )
                finally:
                    db.close()

                resumed_pipeline = build_module.EmbeddingPipeline(
                    _Embedder(), workers=2
                )
                try:
                    with (
                        patch.object(
                            build_module, "_save_rows", side_effect=original_save_rows
                        ),
                        patch.object(
                            build_module,
                            "_write_faiss",
                            side_effect=_write_fake_faiss,
                        ),
                    ):
                        locator = _build(
                            Producer(rows),
                            tmp,
                            {
                                "batch_size": 1,
                                "embedding_pipeline": resumed_pipeline,
                            },
                            embedder=_Embedder(),
                        )
                finally:
                    resumed_pipeline.close()
                self.assertEqual(
                    [row[0] for row in _docs(locator, _source())],
                    list(range(4)),
                )
        finally:
            embedder.release_slow.set()
            pipeline.close()
        self.assertEqual(_stage_threads(), [])

    def test_empty_and_title_only_builds_never_touch_the_model(self):
        class ExplodingEmbedder(_Embedder):
            def truncate(self, *_args, **_kwargs):
                raise AssertionError("model truncate was called")

            def encode(self, *_args, **_kwargs):
                raise AssertionError("model encode was called")

            def embed(self, *_args, **_kwargs):
                raise AssertionError("model embed was called")

            def embed_encoded(self, *_args, **_kwargs):
                raise AssertionError("model embed_encoded was called")

        rows = [
            EntryRecord(1, "Title only", "", "title", None, ""),
            EntryRecord(2, "Another title", "", "another", None, ""),
        ]
        pipeline = build_module.EmbeddingPipeline(ExplodingEmbedder(), workers=2)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                locator = _build(
                    Producer(rows),
                    tmp,
                    {"batch_size": 2, "embedding_pipeline": pipeline},
                    embedder=ExplodingEmbedder(),
                )
                self.assertEqual(_meta(locator, _source())[DONE], "full")
                self.assertEqual(
                    [row[0] for row in _docs(locator, _source())], [1, 2]
                )
        finally:
            pipeline.close()

    def test_stage_errors_propagate_without_publishing_or_leaking_reservations(self):
        rows = [
            EntryRecord(i, f"T{i}", "excerpt", f"p{i}", None, "body")
            for i in range(4)
        ]

        class FailingPipeline:
            max_pending_per_build = 2

            def submit(self, _rows, *, cancel_event=None):
                raise RuntimeError("preparation failed")

        class FailingEmbedder(_Embedder):
            def embed(self, _texts):
                raise RuntimeError("inference failed")

        def writer_failure(_db, _rows, _next, _vectors, **_kwargs):
            raise RuntimeError("writer failed")

        expected = {
            "reader": "parse failed",
            "preparation": "preparation failed",
            "inference": "inference failed",
            "writer": "writer failed",
        }
        for name, message in expected.items():
            with self.subTest(stage=name):
                budget = build_module.BodyBudget(4096)
                producer = Producer(rows, fail_at=0 if name == "reader" else None)
                embedder = FailingEmbedder() if name == "inference" else _Embedder()
                if name == "preparation":
                    pipeline = FailingPipeline()
                    close_pipeline = None
                else:
                    pipeline = build_module.EmbeddingPipeline(embedder, workers=2)
                    close_pipeline = pipeline
                save_patch = (
                    patch.object(build_module, "_save_rows", side_effect=writer_failure)
                    if name == "writer"
                    else patch.object(
                        build_module, "_write_faiss", side_effect=_write_fake_faiss
                    )
                )
                with tempfile.TemporaryDirectory() as tmp:
                    try:
                        with save_patch, self.assertRaisesRegex(RuntimeError, message):
                            _build(
                                producer,
                                tmp,
                                {
                                    "batch_size": 1,
                                    "body_budget": budget,
                                    "embedding_pipeline": pipeline,
                                },
                                embedder=embedder,
                            )
                    finally:
                        if close_pipeline is not None:
                            close_pipeline.close()
                    self.assertEqual(budget.reserved, 0)
                    self.assertIsNone(IndexLocator(tmp).paths(_source()))
                    directory = IndexLocator(tmp).source_directory(
                        "files", "files:test", "files:test:manual-0123"
                    )
                    self.assertFalse((directory / "staging-full.sqlite").exists())
                    self.assertEqual(_stage_threads(), [])


class TempIndexFileTests(unittest.TestCase):
    @unittest.skipIf(
        os.name == "nt", "Windows does not expose POSIX permission bits"
    )
    def test_temp_index_file_uses_readable_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "manual.faiss"
            temp = build_module._create_temp_path(target)
            try:
                self.assertEqual(temp.stat().st_mode & 0o777, 0o644)
                self.assertEqual(temp.parent, target.parent)
            finally:
                temp.unlink(missing_ok=True)


class FaissTrainingTests(unittest.TestCase):
    def test_training_stride_meets_faiss_minimum(self):
        vector_count = 700_000
        cluster_count = int(4 * vector_count**0.5)
        step = build_module._faiss_training_step(vector_count, cluster_count)
        self.assertGreaterEqual(
            vector_count // step,
            build_module._FAISS_MIN_POINTS_PER_CENTROID * cluster_count,
        )

    def test_training_stride_uses_all_vectors_when_fewer_than_target(self):
        vector_count = 10_000
        cluster_count = int(4 * vector_count**0.5)
        self.assertEqual(
            build_module._faiss_training_step(vector_count, cluster_count), 1
        )

    def test_cluster_count_never_exceeds_trainable_centroids(self):
        for vector_count in (10_000, 10_728, 24_000, 24_335, 250_000, 700_000):
            with self.subTest(vector_count=vector_count):
                cluster_count = build_module._faiss_cluster_count(vector_count)
                self.assertGreaterEqual(cluster_count, 1)
                self.assertLessEqual(
                    build_module._FAISS_MIN_POINTS_PER_CENTROID * cluster_count,
                    vector_count,
                )


class BuildBatchTests(unittest.TestCase):
    def test_fast_build_uses_larger_commit_windows(self):
        rows = [
            EntryRecord(index, f"Title {index}", "", f"path-{index}", None, "")
            for index in range(build_module.FAST_BUILD_BATCH_SIZE + 2)
        ]
        saved = []

        with tempfile.TemporaryDirectory() as tmp:
            def save(_db, _embedder, batch, next_entry, **kwargs):
                saved.append((len(batch), next_entry))

            with (
                patch.object(build_module, "_save", side_effect=save),
            ):
                _build(
                    Producer(rows),
                    tmp,
                    {"batch_size": 2},
                    fast=True,
                )

        self.assertEqual(
            saved,
            [
                (build_module.FAST_BUILD_BATCH_SIZE, build_module.FAST_BUILD_BATCH_SIZE),
                (2, build_module.FAST_BUILD_BATCH_SIZE + 2),
            ],
        )

    def test_excerpt_is_transient_and_truncated_for_embedding(self):
        class _FakeEmbedder(_Embedder):
            def __init__(self):
                self.truncate_calls = []
                self.embed_calls = []

            def truncate(self, text, prefix):
                self.truncate_calls.append((text, prefix))
                return "word-safe embedding"

            def embed(self, texts):
                self.embed_calls.append(texts)
                return np.array([[1.0, 0.0]], dtype=np.float32)

        db = sqlite3.connect(":memory:")
        db.executescript(SCHEMA)
        embedder = _FakeEmbedder()
        build_module._save(
            db,
            embedder,
            [EntryRecord(1, "A title", "a stored excerpt", "article", None, "body text")],
            1,
            modified={1: 123},
        )

        self.assertNotIn(
            "excerpt",
            {row[1] for row in db.execute("PRAGMA table_info(docs)")},
        )
        self.assertEqual(
            db.execute("SELECT title, path FROM docs WHERE rowid = 1").fetchone(),
            ("A title", "article"),
        )
        self.assertEqual(
            db.execute("SELECT modified FROM docs WHERE rowid = 1").fetchone(),
            (123,),
        )
        self.assertEqual(
            embedder.truncate_calls,
            [("a stored excerpt", "passage: A title\n")],
        )
        self.assertEqual(
            embedder.embed_calls, [["passage: A title\nword-safe embedding"]]
        )
        db.close()


class EmbeddingPipelineTests(unittest.TestCase):
    def test_build_requires_tokenized_embedder_api(self):
        embedder = SimpleNamespace(truncate=lambda text, prefix: text)
        embedder.embed = lambda texts: np.ones((len(texts), 4), dtype=np.float32)
        rows = [EntryRecord(1, "Title", "Excerpt", "p")]
        with self.assertRaisesRegex(AttributeError, "encode"):
            build_module._embed_rows(embedder, rows)

    def test_short_windows_feed_all_workers_past_a_straggler(self):
        slow_started = threading.Event()
        release_slow = threading.Event()
        fourth_finished = threading.Event()
        fast_workers_started = threading.Barrier(3)
        worker_ids = set()
        worker_lock = threading.Lock()
        read_positions = []

        class TrackingProducer(Producer):
            def entry_at(self, index):
                read_positions.append(index)
                return super().entry_at(index)

        class StragglerEmbedder(_Embedder):
            def embed(self, texts):
                if len(texts) == 1 and texts[0].startswith("passage: T"):
                    entry_id = int(texts[0].split("\n", 1)[0].removeprefix("passage: T"))
                    with worker_lock:
                        worker_ids.add(threading.get_ident())
                    if entry_id == 0:
                        slow_started.set()
                        if not release_slow.wait(timeout=5):
                            raise TimeoutError("slow batch was not released")
                    elif entry_id <= 3:
                        fast_workers_started.wait(timeout=2)
                    elif entry_id == 4:
                        fourth_finished.set()
                return super().embed(texts)

        rows = [EntryRecord(i, f"T{i}", "short excerpt", f"p{i}", None, "body") for i in range(8)]
        producer = TrackingProducer(rows)
        embedder = StragglerEmbedder()
        pipeline = build_module.EmbeddingPipeline(embedder, workers=4)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with (
                    patch.object(build_module, "_write_faiss", side_effect=_write_fake_faiss),
                    ThreadPoolExecutor(max_workers=1) as reader,
                ):
                    build = reader.submit(
                        _build, producer, tmp,
                        {"embedding_pipeline": pipeline},
                        embedder=embedder,
                    )
                    try:
                        self.assertTrue(slow_started.wait(timeout=2))
                        self.assertTrue(fourth_finished.wait(timeout=2))
                        self.assertEqual(len(worker_ids), 4)
                        self.assertLessEqual(len(read_positions), pipeline.max_pending_per_build)
                        directory = IndexLocator(tmp).source_directory(
                            "files", "files:test", "files:test:manual-0123"
                        )
                        db = sqlite3.connect(directory / "staging-full.sqlite")
                        try:
                            self.assertEqual(db.execute("SELECT COUNT(*) FROM docs").fetchone()[0], 0)
                        finally:
                            db.close()
                    finally:
                        release_slow.set()
                        if not fourth_finished.is_set():
                            fast_workers_started.abort()
                    locator = build.result(timeout=5)
                self.assertEqual([row[0] for row in _docs(locator, _source())], list(range(8)))
                self.assertEqual(_meta(locator, _source())["semantic_vector_count"], "8")
        finally:
            release_slow.set()
            fast_workers_started.abort()
            pipeline.close()

    def test_batches_use_capped_padded_tokens_and_keep_vector_ids(self):
        calls = []

        class TokenEmbedder(_Embedder):
            def encode(self, texts):
                return [[int(text.split("\n")[-1])] * int(text.split("\n")[-1]) for text in texts]

            def embed_encoded(self, inputs):
                calls.append([len(tokens) for tokens in inputs])
                return np.array([[tokens[0], 1] for tokens in inputs], dtype=np.float32)

        lengths = [250, 20, 250, 20, 250, 20, 250, 20, 250, 20]
        rows = [
            EntryRecord(i, "T", str(length), str(i), None, "")
            for i, length in enumerate(lengths)
        ]
        rows.append(EntryRecord(99, "Redirect", "", "", 0))
        pipeline = build_module.EmbeddingPipeline(TokenEmbedder(), workers=2, token_budget=1000)
        try:
            result = pipeline.submit(rows).result(timeout=2)
        finally:
            pipeline.close()
        self.assertEqual(sorted(calls), [[20] * 5, [250], [250] * 4])
        self.assertEqual(set(result), set(range(10)))
        for i, length in enumerate(lengths):
            self.assertEqual(result[i][0], length)

    def test_oversized_single_input_is_not_dropped_and_errors_propagate(self):
        class TokenEmbedder(_Embedder):
            def encode(self, texts):
                return [[0] * 256 for _ in texts]

            def embed_encoded(self, inputs):
                raise RuntimeError("model failed")

        rows = [EntryRecord(1, "Title", "Excerpt", "p")]
        pipeline = build_module.EmbeddingPipeline(TokenEmbedder(), token_budget=100)
        try:
            with self.assertRaisesRegex(RuntimeError, "model failed"):
                pipeline.submit(rows).result(timeout=2)
            self.assertEqual(pipeline.submit([rows[0]._replace(excerpt="")]).result(), {})
        finally:
            pipeline.close()

    def test_fixed_shape_batches_fill_the_provider_capacity(self):
        calls = []

        class FixedEmbedder(_Embedder):
            def batch_shape(self, width):
                return 3, 256

            def encode(self, texts):
                return [[int(text.split("\n")[-1])] * int(text.split("\n")[-1]) for text in texts]

            def embed_encoded(self, inputs):
                calls.append([len(tokens) for tokens in inputs])
                return np.array([[tokens[0], 1] for tokens in inputs], dtype=np.float32)

        lengths = [250, 20] * 5
        rows = [EntryRecord(i, "T", str(length), str(i)) for i, length in enumerate(lengths)]
        result = build_module._embed_rows(FixedEmbedder(), rows, token_budget=1000)
        self.assertEqual(sorted(calls), [[20] * 3, [20, 20, 250], [250], [250] * 3])
        self.assertEqual(set(result), set(range(10)))
        for i, length in enumerate(lengths):
            self.assertEqual(result[i][0], length)

    def test_cpu_small_batches_can_enqueue_a_read_window_while_workers_are_busy(self):
        from zimantic.__main__ import _embedding_queue_size

        started = threading.Event()
        release = threading.Event()

        class SmallBatchEmbedder(_Embedder):
            def encode(self, texts):
                return [[int(text.split("\n")[-1])] * 256 for text in texts]

            def embed_encoded(self, inputs):
                started.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test did not release model calls")
                return np.array([[tokens[0], 1] for tokens in inputs], dtype=np.float32)

        rows = [EntryRecord(i, "T", str(i), str(i)) for i in range(32)]
        pipeline = build_module.EmbeddingPipeline(
            SmallBatchEmbedder(), workers=8, token_budget=256,
            queue_size=_embedding_queue_size({"batch_size": 32}, 8, "cpu"),
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as reader:
                submitted = reader.submit(pipeline.submit, rows)
                try:
                    future = submitted.result(timeout=2)
                    self.assertTrue(started.wait(timeout=2))
                    self.assertFalse(future.done())
                finally:
                    release.set()
            result = future.result(timeout=2)
            self.assertEqual(set(result), set(range(32)))
            for entry_id, vector in result.items():
                self.assertEqual(vector[0], entry_id)
        finally:
            release.set()
            pipeline.close()

    def test_fixed_shape_requests_coalesce_without_mixing_source_ids(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        class FixedEmbedder(_Embedder):
            def batch_shape(self, width):
                return 4, 8

            def encode(self, texts):
                return [[int(text.split("\n")[-1]), 0] for text in texts]

            def embed_encoded(self, inputs):
                calls.append(len(inputs))
                if len(calls) == 1:
                    started.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test did not release first batch")
                return np.array([[tokens[0], 1] for tokens in inputs], dtype=np.float32)

        def rows(values):
            return [EntryRecord(i, "T", str(value), str(i)) for i, value in enumerate(values)]

        pipeline = build_module.EmbeddingPipeline(FixedEmbedder(), queue_size=8, token_budget=32)
        try:
            first = pipeline.submit(rows([1, 2, 3]))
            self.assertTrue(started.wait(timeout=2))
            values = [[10], [20, 21, 22], [30, 31], [40, 41]]
            pending = [pipeline.submit(rows(group)) for group in values]
            release.set()
            self.assertEqual(set(first.result(timeout=2)), {0, 1, 2})
            for future, expected in zip(pending, values):
                result = future.result(timeout=2)
                self.assertEqual([result[i][0] for i in range(len(expected))], expected)
        finally:
            release.set()
            pipeline.close()
        self.assertEqual(calls, [3, 8])

    def test_coalesced_failure_keeps_other_shapes_and_shutdown_working(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        class FixedEmbedder(_Embedder):
            def batch_shape(self, width):
                return 4, 8 if width <= 8 else 16

            def encode(self, texts):
                return [
                    [int(text.split("\n")[-1])] * int(text.split("\n")[-1])
                    for text in texts
                ]

            def embed_encoded(self, inputs):
                lengths = list(map(len, inputs))
                calls.append(lengths)
                if lengths == [1]:
                    started.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test did not release first batch")
                elif lengths == [2, 3]:
                    raise RuntimeError("coalesced model failure")
                return np.array([[tokens[0], 1] for tokens in inputs], dtype=np.float32)

        def rows(value):
            return [EntryRecord(0, "T", str(value), "p")]

        pipeline = build_module.EmbeddingPipeline(FixedEmbedder(), queue_size=8)
        try:
            first = pipeline.submit(rows(1))
            self.assertTrue(started.wait(timeout=2))
            pending = [pipeline.submit(rows(value)) for value in (2, 9, 3)]
            pipeline._requests.put(None)
            release.set()
            self.assertEqual(first.result(timeout=2)[0][0], 1)
            for future in (pending[0], pending[2]):
                with self.assertRaisesRegex(RuntimeError, "coalesced model failure"):
                    future.result(timeout=2)
            self.assertEqual(pending[1].result(timeout=2)[0][0], 9)
            pipeline._threads[0].join(timeout=2)
            self.assertFalse(pipeline._threads[0].is_alive())
        finally:
            release.set()
            pipeline.close()
        self.assertEqual(calls, [[1], [2, 3], [9]])

    def test_progress_counts_committed_articles_and_omits_redirect_eta(self):
        bars = []

        class Bar:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.n = kwargs.get("initial", 0)
                bars.append(self)

            def update(self, amount):
                self.n += amount

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        producer = SimpleNamespace(key="test", count=1000, article_work_count=10)
        updates = []
        with patch.object(build_module, "tqdm", Bar):
            progress = build_module._BuildProgress(
                producer, 3, lambda position, total: updates.append((position, total))
            )
            progress.advance(7)
            self.assertEqual(bars[0].n, 7)
            progress.advance(20)
            progress.advance(1000)
            progress.close()
        self.assertEqual(bars[0].kwargs["total"], 10)
        self.assertEqual(bars[0].n, 10)
        self.assertIsNone(bars[1].kwargs["total"])
        self.assertNotIn("remaining", bars[1].kwargs["bar_format"])
        self.assertEqual(bars[1].n, 990)
        self.assertEqual(updates, [(3, 1000), (7, 1000), (20, 1000), (1000, 1000)])

    def test_all_files_progress_tracks_resume_and_waits_for_publication(self):
        with patch.object(build_module, "tqdm") as bar:
            progress = build_module._AllFilesProgress(3)
            progress.advance(0, 30, 100)
            progress.advance(0, 20, 100)
            progress.advance(1, 50, 100)
            progress.advance(0, 100, 100)
            self.assertAlmostEqual(
                sum(call.args[0] for call in bar.return_value.update.call_args_list),
                1.49,
            )
            progress.finish(0)
            progress.finish(1)
            progress.advance(2, 0, 0)
            progress.finish(2)
            self.assertAlmostEqual(
                sum(call.args[0] for call in bar.return_value.update.call_args_list), 3
            )
            progress.close()
            calls = bar.return_value.update.call_count
            progress.advance(1, 100, 100)
            progress.finish(1)
            self.assertEqual(bar.return_value.update.call_count, calls)
            bar.return_value.close.assert_called_once()

    def test_build_reports_committed_progress_to_overall_callback(self):
        updates = []
        producer = Producer([EntryRecord(i, f"Title {i}") for i in range(300)])
        with tempfile.TemporaryDirectory() as directory:
            _build(
                producer,
                directory,
                {"progress_callback": lambda position, total: updates.append((position, total))},
                fast=True,
            )
        self.assertEqual(updates, [(0, 300), (256, 300), (300, 300)])

    def test_limits_concurrent_embedding_calls(self):
        active = 0
        maximum = 0
        active_lock = threading.Lock()

        class _FakeEmbedder(_Embedder):
            def truncate(self, text, prefix):
                return text

            def embed(self, texts):
                nonlocal active, maximum
                with active_lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.01)
                with active_lock:
                    active -= 1
                return np.ones((len(texts), 2), dtype=np.float32)

        rows = [
            EntryRecord(index, f"Title {index}", f"Excerpt {index}", f"path-{index}", None, "")
            for index in range(4)
        ]
        pipeline = build_module.EmbeddingPipeline(
            _FakeEmbedder(),
            workers=2,
            queue_size=2,
        )
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(pipeline.submit, [row]).result() for row in rows
                ]
                results = [future.result() for future in futures]
        finally:
            pipeline.close()

        self.assertEqual(maximum, 2)
        self.assertEqual(
            [list(result) for result in results], [[row[0]] for row in rows]
        )

    def test_build_commits_pipeline_results_before_writing_faiss(self):
        rows = [
            EntryRecord(index, f"Title {index}", f"Excerpt {index}", f"path-{index}", None, "body")
            for index in range(2)
        ]

        with tempfile.TemporaryDirectory() as tmp:
            pipeline = build_module.EmbeddingPipeline(_Embedder(), workers=1)

            def write_fake_faiss(db, path):
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0], 2
                )
                path.write_bytes(b"vectors")

            try:
                with patch.object(build_module, "_write_faiss", side_effect=write_fake_faiss):
                    locator = _build(
                        Producer(rows),
                        tmp,
                        {"embedding_pipeline": pipeline},
                        embedder=_Embedder(),
                    )
            finally:
                pipeline.close()

            self.assertEqual(_meta(locator, _source())[DONE], "full")


class FaissTrainingSamplingTests(unittest.TestCase):
    def test_training_samples_use_vector_positions_not_sparse_entry_ids(self):
        class _FakeIndex:
            def train(self, vectors):
                self.training_vectors = vectors

            def add_with_ids(self, _vectors, _ids):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "indexes"
            directory.mkdir()
            db = sqlite3.connect(directory / "manual.sqlite")
            db.execute("CREATE TABLE vecs(id INTEGER PRIMARY KEY, v BLOB NOT NULL)")
            vector = np.array([1.0, 2.0], dtype=np.float16).tobytes()
            db.executemany(
                "INSERT INTO vecs VALUES (?, ?)",
                ((2 * index + 1, vector) for index in range(10_000)),
            )
            db.commit()
            index = _FakeIndex()
            fake_faiss = type(
                "_FakeFaiss",
                (),
                {
                    "METRIC_INNER_PRODUCT": 0,
                    "index_factory": staticmethod(lambda *_args: index),
                    "write_index": staticmethod(lambda *_args: None),
                },
            )()
            with (
                patch.object(build_module, "_faiss_training_step", return_value=2),
                patch.object(build_module, "_load_faiss", return_value=fake_faiss),
            ):
                build_module._write_faiss(db, directory / "manual.faiss")
            db.close()

        self.assertEqual(index.training_vectors.shape, (5_000, 2))


class SigtermHandlerTests(unittest.TestCase):
    def test_sigterm_handler_raises_keyboard_interrupt(self):
        import zimantic.__main__ as main_module

        with self.assertRaises(KeyboardInterrupt):
            main_module._graceful_interrupt(0, None)


if __name__ == "__main__":
    unittest.main()
