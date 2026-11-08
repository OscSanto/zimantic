import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from zimantic.contracts import SourceInfo
from zimantic.indexing.locator import IndexLocator
from zimantic.indexing import pipeline as build_module
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


def _meta(locator, source):
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


def _docs(locator, source):
    lease = locator.paths(source)
    assert lease is not None
    try:
        db = sqlite3.connect(lease.sqlite_path)
        try:
            return db.execute("SELECT rowid, title, path FROM docs ORDER BY rowid").fetchall()
        finally:
            db.close()
    finally:
        lease.close()


def _body_hits(locator, source, term):
    lease = locator.paths(source)
    assert lease is not None
    try:
        db = sqlite3.connect(lease.sqlite_path)
        try:
            return db.execute(
                "SELECT rowid FROM body WHERE body MATCH ?", (f'"{term}"',)
            ).fetchall()
        finally:
            db.close()
    finally:
        lease.close()


class _Embedder:
    genome = "embedder"

    def truncate(self, text, prefix=""):
        return text

    def embed(self, texts):
        return np.ones((len(texts), 4), dtype=np.float32)


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
            generation = locator.paths(_source()).generation
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
            self.assertEqual(locator.paths(_source()).generation, generation)
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

    def test_resume_requires_order_fingerprint_and_rejects_changed_order(self):
        rows = [EntryRecord(i, f"T{i}", "", f"p{i}") for i in range(4)]

        class OrderedProducer(Producer):
            order_fingerprint = "order-a"

            def prepare_iteration(self, order):
                self.requested_order = order
                return order or "efficient-v1"

        for missing_manifest in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                first = OrderedProducer(rows, interrupt_at=2)
                with self.assertRaises(KeyboardInterrupt):
                    _build(first, tmp, fast=True)
                directory = IndexLocator(tmp).source_directory(
                    "files", "files:test", "files:test:manual-0123"
                )
                if missing_manifest:
                    with sqlite3.connect(directory / "staging-fast.sqlite") as db:
                        db.execute("DELETE FROM meta WHERE key IN ('entry_order', 'entry_order_fingerprint')")
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
            generation = locator.paths(_source()).generation

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
            self.assertEqual(locator.paths(_source()).generation, generation)

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
    def test_fast_build_flushes_rows_while_scanning(self):
        rows = [
            EntryRecord(index, f"Title {index}", "", f"path-{index}", None, "")
            for index in range(5)
        ]
        saved = []

        with tempfile.TemporaryDirectory() as tmp:
            def save(_db, _embedder, batch, next_entry, **kwargs):
                saved.append((len(batch), next_entry))

            with (
                patch.object(build_module, "_save", side_effect=save),
                patch.object(build_module, "_finish_build"),
            ):
                _build(Producer(rows), tmp, {"batch_size": 2}, fast=True)

        self.assertEqual(saved, [(2, 2), (2, 4), (1, 5)])

    def test_excerpt_is_transient_and_truncated_for_embedding(self):
        class _FakeEmbedder:
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
            embedder.truncate_calls,
            [("a stored excerpt", "passage: A title\n")],
        )
        self.assertEqual(
            embedder.embed_calls, [["passage: A title\nword-safe embedding"]]
        )
        db.close()


class EmbeddingPipelineTests(unittest.TestCase):
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
                        with sqlite3.connect(directory / "staging-full.sqlite") as db:
                            self.assertEqual(db.execute("SELECT COUNT(*) FROM docs").fetchone()[0], 0)
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
        with patch.object(build_module, "tqdm", Bar):
            progress = build_module._BuildProgress(producer, 3)
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

    def test_limits_concurrent_embedding_calls(self):
        active = 0
        maximum = 0
        active_lock = threading.Lock()

        class _FakeEmbedder:
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
