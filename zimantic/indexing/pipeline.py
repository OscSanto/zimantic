"""Generic, resumable SQLite/FAISS index build pipeline.

Only the entry source is provider-specific.  Everything in this module works
with :class:`zimantic.indexing.types.EntryProducer`; there are no libzim,
provider-discovery, web, or FastAPI imports here (FAISS is imported lazily
inside the writer/loader functions).
"""

from __future__ import annotations

import atexit
import logging
import os
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Thread
from typing import Any

from tqdm import tqdm

from .._fs import fsync_path
from ..diagnostics import public_cause
from ..model_meta import (
    MODEL_FINGERPRINT_KEY,
    MODEL_PROBE_SHAPE_KEY,
    MODEL_PROBE_TEXTS_KEY,
    MODEL_PROBE_VECTORS_KEY,
)
from . import schema as index_schema
from .locator import IndexLocator
from .types import EntryProducer, EntryRecord

SCHEMA = index_schema.SCHEMA

logger = logging.getLogger(__name__)

_FAISS_MIN_POINTS_PER_CENTROID = 39
_FAISS_MIN_TRAINING_POINTS = 100_000

DEFAULT_MAX_BODY_TEXT_BYTES = 1 << 20
DEFAULT_BODY_BUFFER_BYTES = 16 << 20
DEFAULT_STAGING_RESERVE_BYTES = 1 << 20
DEFAULT_EMBEDDING_BATCH_TOKENS = 1000
FAST_BUILD_BATCH_SIZE = 256
_REUSED_VECTOR_SOURCE = "reused_vector_source"


class BuildError(RuntimeError):
    """A build cannot complete safely."""


class InsufficientSpaceError(BuildError):
    """The staging generation cannot be written without risking the source."""


class SnapshotChangedError(BuildError):
    """The producer's source or build inputs changed during the build."""


class PartialEnrichmentError(BuildError):
    """A requested derived operation was incomplete; available channels published."""


class _StageStopped(Exception):
    """Internal signal that one source's staged workers are shutting down."""


@dataclass(frozen=True)
class BuildReport:
    """One source's build outcome, including bounded-index metadata.

    The report is informational: callers that only need the published index
    may ignore the return value of :func:`build_index`.
    """

    provider: str
    collection_key: str
    source_key: str
    status: str
    indexed_count: int
    skipped_count: int
    truncated: bool
    body_truncated_count: int
    duration_seconds: float
    fast: bool = False
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "collection": self.collection_key,
            "source": self.source_key,
            "status": self.status,
            "indexed_count": self.indexed_count,
            "skipped_count": self.skipped_count,
            "truncated": self.truncated,
            "body_truncated_count": self.body_truncated_count,
            "duration_seconds": round(self.duration_seconds, 3),
            "fast": self.fast,
            "failure_reason": self.failure_reason,
        }


@dataclass(slots=True)
class _EmbeddingRequest:
    ids: list[int]
    inputs: list[list[int]]
    future: Future


class EmbeddingPipeline:
    """Bounded embedding stage shared by concurrent builds."""

    def __init__(
        self,
        embedder,
        workers: int = 1,
        queue_size: int = 8,
        max_pending_per_build: int | None = None,
        token_budget: int = DEFAULT_EMBEDDING_BATCH_TOKENS,
        tokenizer_workers: int | None = None,
    ) -> None:
        workers = int(workers)
        if workers < 1:
            raise ValueError("embedding workers must be at least 1")
        # Short windows may contain only one call; feed every worker plus read-ahead.
        self.max_pending_per_build = (
            max(2, workers + 1)
            if max_pending_per_build is None
            else max(1, int(max_pending_per_build))
        )
        self._embedder = embedder
        self.token_budget = max(1, int(token_budget))
        self._requests: Queue[_EmbeddingRequest | None] = Queue(
            maxsize=max(1, int(queue_size))
        )
        self._submit_condition = threading.Condition()
        self._closed = False
        self._cancelled = False
        self._threads = [
            Thread(target=self._run, name=f"zimantic-embed-{index}", daemon=True)
            for index in range(workers)
        ]
        for thread in self._threads:
            thread.start()
        # Tokenization runs on its own bounded pool so one source's preparation
        # thread can keep the inference workers fed.  Keep it smaller than the
        # inference pool: the Rust tokenizer already parallelizes a batch call,
        # and an unbounded second pool only adds memory and CPU contention.
        if tokenizer_workers is None:
            tokenizer_workers = max(1, min(workers, 4))
        self._tokenizers = ThreadPoolExecutor(
            max_workers=max(1, int(tokenizer_workers)),
            thread_name_prefix="zimantic-tokenize",
        )
        self._tokenize_slots = threading.BoundedSemaphore(max(2, workers + 1))
        self._tokenize_tasks: dict[Future, Future] = {}
        self._tokenize_lock = threading.Lock()
        # Join native inference before interpreter teardown destroys ORT state.
        atexit.register(self.close)

    def _check_open(self) -> None:
        if self._closed:
            if self._cancelled:
                raise KeyboardInterrupt
            raise RuntimeError("embedding pipeline is closed")

    def _check_cancelled(self) -> None:
        # close(wait=True) stops new submissions but lets already-tokenizing
        # windows finish, so only a cancelled pipeline abandons this work.
        if self._cancelled:
            raise KeyboardInterrupt

    def submit(
        self,
        rows: list[EntryRecord],
        *,
        cancel_event: threading.Event | None = None,
    ) -> Future:
        with self._submit_condition:
            self._check_open()
        future = Future()
        # Bound queued tokenization as well as queued inference.  The tokenizer
        # task releases this slot once its batches are in the inference queue.
        while not self._tokenize_slots.acquire(timeout=0.05):
            with self._submit_condition:
                self._check_open()
        try:
            with self._submit_condition:
                self._check_open()
            task = self._tokenizers.submit(
                self._tokenize, rows, future, cancel_event
            )
        except BaseException:
            self._tokenize_slots.release()
            raise
        with self._tokenize_lock:
            self._tokenize_tasks[task] = future
        task.add_done_callback(self._tokenize_done)
        return future

    def _tokenize(
        self,
        rows: list[EntryRecord],
        future: Future,
        cancel_event: threading.Event | None,
    ) -> None:
        """Tokenize one window in parallel with other windows and inference."""
        try:
            self._check_cancelled()
            if cancel_event is not None and cancel_event.is_set():
                future.set_exception(_StageStopped())
                return
            batches = _embedding_batches(self._embedder, rows, self.token_budget)
            if not batches:
                future.set_result({})
                return
            remaining = len(batches)
            vectors = {}
            lock = threading.Lock()

            def finished(batch_future):
                nonlocal remaining
                with lock:
                    error = batch_future.exception()
                    if error is None:
                        vectors.update(batch_future.result())
                    elif not future.done():
                        future.set_exception(error)
                    remaining -= 1
                    if remaining == 0 and not future.done():
                        future.set_result(vectors)

            for ids, inputs in batches:
                self._check_cancelled()
                if cancel_event is not None and cancel_event.is_set():
                    if not future.done():
                        future.set_exception(_StageStopped())
                    return
                batch_future = Future()
                batch_future.add_done_callback(finished)
                request = _EmbeddingRequest(ids, inputs, batch_future)
                # Serialize insertion with close so no request can miss cancellation.
                with self._submit_condition:
                    while True:
                        self._check_cancelled()
                        if cancel_event is not None and cancel_event.is_set():
                            if not future.done():
                                future.set_exception(_StageStopped())
                            return
                        try:
                            self._requests.put_nowait(request)
                            break
                        except Full:
                            self._submit_condition.wait(timeout=0.05)
        except BaseException as error:
            logger.debug("tokenization failed", exc_info=True)
            if not future.done():
                future.set_exception(error)

    def _tokenize_done(self, task: Future) -> None:
        with self._tokenize_lock:
            future = self._tokenize_tasks.pop(task, None)
        self._tokenize_slots.release()
        if future is None or future.done():
            return
        if task.cancelled():
            future.set_exception(KeyboardInterrupt())
            return
        error = task.exception()
        if error is not None:
            future.set_exception(error)

    def close(self, *, wait: bool = True) -> None:
        with self._submit_condition:
            already_closed = self._closed
            if not already_closed:
                self._cancelled = not wait
                self._closed = True
                if self._cancelled:
                    while True:
                        try:
                            request = self._requests.get_nowait()
                        except Empty:
                            break
                        if request is not None:
                            request.future.set_exception(KeyboardInterrupt())
                self._submit_condition.notify_all()
        if wait:
            # Running tokenizer tasks must enqueue their batches before the
            # inference workers see the end-of-queue sentinels.
            self._tokenizers.shutdown(wait=True)
            if not already_closed:
                for _ in self._threads:
                    self._requests.put(None)
            for thread in self._threads:
                thread.join()
            atexit.unregister(self.close)
        else:
            with self._tokenize_lock:
                pending = list(self._tokenize_tasks)
            for task in pending:
                task.cancel()
            self._tokenizers.shutdown(wait=False, cancel_futures=True)

    def _run(self) -> None:
        while True:
            try:
                request = self._requests.get(timeout=0.05)
            except Empty:
                if self._cancelled:
                    return
                continue
            if request is None:
                return
            requests = [request]
            stop_after_batch = False
            batch_shape = getattr(self._embedder, "batch_shape", None)
            if (
                batch_shape is not None
                and batch_shape(max(map(len, request.inputs))) is not None
            ):
                for _ in range(self._requests.maxsize):
                    try:
                        queued = self._requests.get_nowait()
                    except Empty:
                        break
                    if queued is None:
                        stop_after_batch = True
                        break
                    requests.append(queued)
            with self._submit_condition:
                self._submit_condition.notify_all()
            groups = {}
            for pending in requests:
                shape = (
                    batch_shape(max(map(len, pending.inputs)))
                    if batch_shape is not None else None
                )
                groups.setdefault(shape, []).append(pending)
            for group in groups.values():
                if self._cancelled:
                    for pending in group:
                        pending.future.set_exception(KeyboardInterrupt())
                else:
                    self._run_group(group)
            if stop_after_batch or self._cancelled:
                return

    def _run_group(self, requests: list[_EmbeddingRequest]) -> None:
        try:
            inputs = [tokens for request in requests for tokens in request.inputs]
            vectors = self._embedder.embed_encoded(inputs)
        except BaseException as error:  # surface it to the build threads
            logger.debug("embedding batch failed", exc_info=True)
            for request in requests:
                request.future.set_exception(error)
        else:
            offset = 0
            for request in requests:
                stop = offset + len(request.ids)
                request.future.set_result(dict(zip(request.ids, vectors[offset:stop])))
                offset = stop


class BodyBudget:
    """A byte budget for lexical body text held by concurrent builds.

    The default shared budget is small enough for a constrained host but admits
    at least one maximum-sized configured body (and warns when it has to grow).
    """

    def __init__(self, capacity: int = DEFAULT_BODY_BUFFER_BYTES) -> None:
        self.capacity = max(1, int(capacity))
        self._reserved = 0
        self._condition = threading.Condition()
        self._warned = False

    @property
    def reserved(self) -> int:
        with self._condition:
            return self._reserved

    def reserve(
        self,
        size: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> int:
        """Block until ``size`` bytes are available.

        A caller that already holds reservations and then waits here can stall
        every other build.  The indexing loop therefore tries
        :meth:`try_reserve` first and flushes/drains before blocking.

        ``cancel_event`` lets a source abandon the wait when one of its own
        stages fails, without waking unrelated builds.
        """
        requested = max(0, int(size))
        with self._condition:
            if requested > self.capacity:
                if not self._warned:
                    logger.warning(
                        "zimantic: a body of %d bytes exceeds the shared "
                        "in-flight budget (%d); admitting one maximum-sized "
                        "configured body",
                        requested,
                        self.capacity,
                    )
                    self._warned = True
                self.capacity = requested
            while not (
                self._reserved == 0 or self._reserved + requested <= self.capacity
            ):
                if cancel_event is not None and cancel_event.is_set():
                    raise _StageStopped()
                self._condition.wait(timeout=0.05)
            self._reserved += requested
            return requested

    def try_reserve(self, size: int) -> int | None:
        """Do not wait while a build itself holds uncommitted body text."""
        requested = max(0, int(size))
        with self._condition:
            if requested > self.capacity or self._reserved + requested > self.capacity:
                return None
            self._reserved += requested
            return requested

    def release(self, size: int) -> None:
        with self._condition:
            self._reserved = max(0, self._reserved - max(0, int(size)))
            self._condition.notify_all()


_DEFAULT_BODY_BUDGET = BodyBudget()


def shared_body_budget(max_body_text_bytes: int) -> BodyBudget:
    """Return the process-wide budget, growing it to admit one max body."""
    if max_body_text_bytes > _DEFAULT_BODY_BUDGET.capacity:
        with _DEFAULT_BODY_BUDGET._condition:
            _DEFAULT_BODY_BUDGET.capacity = max_body_text_bytes
            _DEFAULT_BODY_BUDGET._condition.notify_all()
    return _DEFAULT_BODY_BUDGET


@dataclass
class _BuildState:
    db: sqlite3.Connection | None
    build_db_path: Path
    build_faiss_path: Path
    publish_upgrade: bool
    filesize: int
    meta: dict[str, str]
    source_fingerprint: str
    build_mode: str
    generation: str
    allow_vector_reuse: bool


def _cfg_int(cfg: Mapping[str, Any], key: str, default: int) -> int:
    value = cfg.get(key, default) if isinstance(cfg, Mapping) else default
    if isinstance(value, bool) or not isinstance(value, int):
        return int(default)
    return value


def _producer_attr(producer: Any, name: str, default: Any = None) -> Any:
    return getattr(producer, name, default)


def _directory_bytes(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _check_staging_space(
    source_dir: Path,
    *,
    current_bytes: int,
    reserve: int = DEFAULT_STAGING_RESERVE_BYTES,
) -> None:
    """Fail before staging when the known minimum cannot fit.

    A safe replacement temporarily needs both generations.  This is an estimate,
    not an arbitrary per-source cap: large sources are core use cases.
    """
    required = max(2 * current_bytes, current_bytes + reserve)
    try:
        free = shutil.disk_usage(source_dir).free
    except OSError:
        return
    if free < required:
        raise InsufficientSpaceError(
            f"not enough free space to stage a replacement index in "
            f"{source_dir.name}: need about {required} bytes, have {free}"
        )


def _open_database(path: Path) -> sqlite3.Connection:
    existing = path.is_file() and path.stat().st_size > 0
    db = sqlite3.connect(path)
    try:
        if existing:
            index_schema.read_index_meta(db)
        else:
            index_schema.ensure_schema(db)
            index_schema.validate_schema(db)
    except BaseException:
        db.close()
        raise
    return db


def _open_or_reset(path: Path) -> sqlite3.Connection:
    """Open a staging database, replacing an unreadable or incompatible one.

    The published generation is never touched: only the recognized staging file
    is removed when it cannot be reused.
    """
    try:
        return _open_database(path)
    except (sqlite3.DatabaseError, index_schema.SchemaError):
        path.unlink(missing_ok=True)
        try:
            return _open_database(path)
        except BaseException:
            path.unlink(missing_ok=True)
            raise


def _read_published_state(locator: Any, source: Any) -> tuple[dict[str, str], Any] | None:
    try:
        lease = locator.paths(source)
    except Exception:
        return None
    if lease is None:
        return None
    try:
        db = sqlite3.connect(lease.sqlite_path)
        try:
            meta = index_schema.read_index_meta(db, completed=True)
            if (
                meta[index_schema.PROVIDER],
                meta[index_schema.COLLECTION_KEY],
                meta[index_schema.SOURCE_KEY],
            ) != (source.provider, source.collection_key, source.key):
                raise index_schema.SchemaError("index identity does not match the source")
            return meta, lease
        finally:
            db.close()
    except Exception:
        lease.close()
        return None


def _staging_paths(directory: Path, mode: str) -> tuple[Path, Path]:
    return (
        directory / f"staging-{mode}.sqlite",
        directory / f"staging-{mode}.faiss",
    )


def _clear_staging(db_path: Path, faiss_path: Path) -> None:
    db_path.unlink(missing_ok=True)
    faiss_path.unlink(missing_ok=True)


def _reset_staging(db_path: Path, faiss_path: Path) -> sqlite3.Connection:
    _clear_staging(db_path, faiss_path)
    return _open_or_reset(db_path)


def _prepare_build_state(
    producer: EntryProducer,
    locator: IndexLocator,
    *,
    fast: bool,
    force: bool,
    cfg: Mapping[str, Any] | None = None,
) -> _BuildState | None:
    """Resolve, validate, and resume-or-create one staging generation."""
    cfg = cfg or {}
    source = producer.source
    directory = locator.source_directory(
        source.provider, source.collection_key, source.key
    )
    current: tuple[dict[str, str], Any] | None = None
    if not force:
        current = _read_published_state(locator, source)
    allow_vector_reuse = not (directory / "current").exists()
    if current is not None:
        current_meta, lease = current
        lease.close()
        done = str(current_meta.get(index_schema.DONE, ""))
        allow_vector_reuse = done == index_schema.DONE_FAST
        raw_derived = current_meta.get(index_schema.DERIVED_TEXT)
        partial_enrichment = bool(
            raw_derived and index_schema.decode_json(raw_derived).get("partial")
        )
        current_fingerprint = current_meta[index_schema.SOURCE_FINGERPRINT]
        producer_fingerprint = producer.fingerprint
        fingerprint_matches = current_fingerprint == producer_fingerprint
        if (
            not partial_enrichment
            and fingerprint_matches
            and (done == index_schema.DONE_FULL or fast)
        ):
            # A full index already contains everything a fast build produces,
            # so --fast must not downgrade it. Only a full build may replace a
            # complete fast generation.
            if done == index_schema.DONE_FULL:
                logger.info("%s: already built (use --force to rebuild)", source.key)
            else:
                logger.info(
                    "%s: already indexed for title/full-text search "
                    "(use --force to rebuild)",
                    source.key,
                )
            return None
        if not fingerprint_matches and not force:
            logger.info(
                "%s: source or build inputs changed; rebuilding",
                source.key,
            )

    mode = index_schema.DONE_FAST if fast else index_schema.DONE_FULL
    build_db_path, build_faiss_path = _staging_paths(directory, mode)
    directory.mkdir(parents=True, exist_ok=True)
    current_bytes = _directory_bytes(directory / "generations")
    _check_staging_space(
        directory,
        current_bytes=current_bytes,
        reserve=_cfg_int(cfg, "staging_reserve_bytes", DEFAULT_STAGING_RESERVE_BYTES),
    )

    fingerprint = producer.fingerprint
    if not isinstance(fingerprint, str) or not fingerprint:
        raise BuildError("producer fingerprint must be non-empty text")
    publish_upgrade = force or (current is not None)
    db = _open_or_reset(build_db_path)
    try:
        meta = index_schema.read_meta(db)
        if meta and (
            meta[index_schema.PROVIDER],
            meta[index_schema.COLLECTION_KEY],
            meta[index_schema.SOURCE_KEY],
        ) != (source.provider, source.collection_key, source.key):
            logger.warning("%s: staged index identity changed; restarting", source.key)
            db.close()
            db = _reset_staging(build_db_path, build_faiss_path)
            meta = {}
        done = str(meta.get(index_schema.DONE, ""))
        if done in index_schema.DONE_VALUES:
            if done == mode and not force:
                # An earlier run completed staging but publication did not
                # happen.  Reuse it only if its identity still matches.
                if meta[index_schema.SOURCE_FINGERPRINT] == fingerprint:
                    return _BuildState(
                        db=db,
                        build_db_path=build_db_path,
                        build_faiss_path=build_faiss_path,
                        publish_upgrade=True,
                        filesize=max(0, producer.source_size),
                        meta=meta,
                        source_fingerprint=fingerprint,
                        build_mode=mode,
                        generation=meta[index_schema.BUILD_GENERATION],
                        allow_vector_reuse=allow_vector_reuse,
                    )
            db.close()
            db = _reset_staging(build_db_path, build_faiss_path)
            meta = {}
        recorded_fingerprint = meta.get(index_schema.SOURCE_FINGERPRINT)
        if (
            recorded_fingerprint is not None
            and recorded_fingerprint != fingerprint
        ):
            logger.warning(
                "%s: source or build inputs changed during an interrupted "
                "build; restarting",
                source.key,
            )
            db.close()
            db = _reset_staging(build_db_path, build_faiss_path)
            meta = {}
        recorded_mode = meta.get(index_schema.BUILD_MODE)
        if recorded_mode is not None and recorded_mode != mode:
            logger.warning(
                "%s: build mode changed during an interrupted build (%s -> %s); "
                "restarting",
                source.key,
                recorded_mode,
                mode,
            )
            db.close()
            db = _reset_staging(build_db_path, build_faiss_path)
            meta = {}
        next_ordinal = index_schema.decode_int(meta.get(index_schema.NEXT, "0"))
        if next_ordinal < 0 or next_ordinal > int(
            _producer_attr(producer, "count", 0) or 0
        ):
            logger.warning(
                "%s: stored resume position is outside the current manifest; "
                "restarting",
                source.key,
            )
            db.close()
            db = _reset_staging(build_db_path, build_faiss_path)
            meta = {}
        else:
            try:
                stored_docs = int(
                    db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
                )
            except sqlite3.Error:
                stored_docs = 0
            if stored_docs > next_ordinal:
                # A responsible caller never rewrites an ordinal that already
                # committed contentless FTS postings; restart the staging
                # generation instead of attempting an unsafe replacement.
                logger.warning(
                    "%s: staging contains rows beyond its resume position; "
                    "restarting",
                    source.key,
                )
                db.close()
                db = _reset_staging(build_db_path, build_faiss_path)
                meta = {}

        generation = str(meta.get(index_schema.BUILD_GENERATION, "") or uuid.uuid4())
        with db:
            index_schema.write_meta(
                db,
                {
                    index_schema.BUILD_FILESIZE: index_schema.encode_int(
                        max(0, producer.source_size)
                    ),
                    index_schema.BUILD_MODE: mode,
                    index_schema.SCHEMA_VERSION: index_schema.encode_int(
                        index_schema.CURRENT_SCHEMA_VERSION
                    ),
                    index_schema.BUILD_GENERATION: generation,
                    index_schema.PROVIDER: str(source.provider),
                    index_schema.COLLECTION_KEY: str(
                        source.collection_key
                    ),
                    index_schema.SOURCE_KEY: str(source.key),
                    index_schema.SOURCE_FINGERPRINT: fingerprint,
                },
            )
            provider_id = _producer_attr(producer, "provider_id", None)
            if provider_id:
                index_schema.write_meta(
                    db, {index_schema.PROVIDER_ID: str(provider_id)}
                )
        meta = index_schema.read_meta(db)
        return _BuildState(
            db=db,
            build_db_path=build_db_path,
            build_faiss_path=build_faiss_path,
            publish_upgrade=publish_upgrade,
            filesize=max(0, producer.source_size),
            meta=meta,
            source_fingerprint=fingerprint,
            build_mode=mode,
            generation=generation,
            allow_vector_reuse=allow_vector_reuse,
        )
    except Exception:
        db.close()
        raise


def _read_entry(
    producer: EntryProducer,
    index: int,
    *,
    fast: bool,
    max_body_text_bytes: int,
    derived_stage: Any | None,
    derived_metadata: dict[str, Any] | None,
    derived_refs: dict[int, list[tuple[str, int, str]]] | None,
) -> tuple[EntryRecord | None, int | None]:
    """Read one source entry and apply derived text in the reader thread.

    Providers and derived-text caches may own thread-affine resources, so this
    stays in the thread that created the producer.  Staged workers only receive
    immutable row snapshots and per-window reference snapshots.
    """
    row = producer.entry_at(index)
    modified_at = getattr(producer, "modified_at", None)
    modified_value = modified_at(index) if callable(modified_at) else None
    if modified_value is not None and (
        isinstance(modified_value, bool) or not isinstance(modified_value, int)
    ):
        raise TypeError("producer modified_at() must return an integer or None")
    if row is not None and derived_stage is not None and not fast:
        try:
            from .derived import compose_body

            inputs = ()
            if hasattr(producer, "derived_text_inputs"):
                inputs = producer.derived_text_inputs(index)
            outcome = derived_stage.process(index, row.body, inputs)
            contributed = [
                (contribution.operation, contribution.text)
                for contribution in outcome.contributions
            ]
            if contributed:
                body, body_truncated, _used = compose_body(
                    row.body, contributed, max_body_text_bytes
                )
                row = row._replace(
                    body=body,
                    body_truncated=row.body_truncated or body_truncated,
                )
            if derived_metadata is not None:
                states = derived_metadata.setdefault("operations", {})
                priority = {
                    "off": 0,
                    "not_checked": 1,
                    "ready": 2,
                    "partial": 3,
                    "unavailable": 4,
                }
                for operation, state in outcome.states.items():
                    previous = states.get(operation, "off")
                    if priority.get(str(state), 0) >= priority.get(
                        str(previous), 0
                    ):
                        states[operation] = state
                    if state in {"partial", "unavailable"}:
                        derived_metadata["partial"] = True
                contributors = derived_metadata.setdefault(
                    "contributors", []
                )
                for contribution in outcome.contributions:
                    if contribution.operation not in contributors:
                        contributors.append(contribution.operation)
                    if derived_refs is not None and contribution.cache_key:
                        refs = derived_refs.setdefault(row.id, [])
                        refs.append(
                            (
                                contribution.operation,
                                len(refs),
                                contribution.cache_key,
                            )
                        )
                if outcome.errors:
                    derived_metadata["partial"] = True
                    derived_metadata.setdefault("errors", []).extend(
                        outcome.errors[:20]
                    )
                    for message in outcome.errors[:3]:
                        logger.warning(
                            "%s: derived text row %d: %s",
                            _producer_attr(producer, "key", "source"),
                            index,
                            message,
                        )
        except Exception as error:
            if derived_metadata is not None:
                derived_metadata["partial"] = True
                derived_metadata.setdefault("errors", []).append(
                    f"derived stage failed: {error}"
                )
    return row, modified_value


@dataclass
class _IndexWindow:
    """One ordered read window moving through preparation and writing."""

    rows: list[EntryRecord]
    modified: dict[int, int | None]
    next_entry: int
    reserved: int
    derived_refs: dict[int, list[tuple[str, int, str]]] | None
    has_permit: bool = True
    future: Future | None = None
    released: bool = False


class _StageController:
    """Private per-source reader/preparer/writer overlap.

    The reader owns provider access and derived extraction.  A preparation
    worker submits embedding batches without waiting for inference, and an
    ordered writer owns its own staging SQLite connection.  A single permit
    budget bounds all in-flight windows across every stage.
    """

    def __init__(
        self,
        *,
        embedding_pipeline: EmbeddingPipeline,
        body_budget: BodyBudget,
        db_path: Path,
        fast: bool,
        progress: Any | None,
        interrupt_event: threading.Event | None,
        max_in_flight: int,
    ) -> None:
        self.embedding_pipeline = embedding_pipeline
        self.body_budget = body_budget
        self.db_path = db_path
        self.fast = fast
        self.progress = progress
        self.interrupt_event = interrupt_event
        self.max_in_flight = max(1, int(max_in_flight))
        self.cancel_event = threading.Event()
        # The extra slots admit the single end-of-stream sentinel without
        # blocking; permits still bound the number of real windows.
        self.read_queue: Queue[_IndexWindow | None] = Queue(
            maxsize=self.max_in_flight + 2
        )
        self.prepared_queue: Queue[_IndexWindow | None] = Queue(
            maxsize=self.max_in_flight + 2
        )
        self._permits = threading.Semaphore(self.max_in_flight)
        self._error_lock = threading.Lock()
        self._release_lock = threading.Lock()
        self._error: BaseException | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"zimantic-stage-{id(self):x}",
        )
        self._prep_future: Future | None = None
        self._write_future: Future | None = None

    def start(self) -> None:
        self._write_future = self._executor.submit(self._write)
        self._write_future.add_done_callback(self._stage_finished)
        self._prep_future = self._executor.submit(self._prepare)
        self._prep_future.add_done_callback(self._stage_finished)

    def _stage_finished(self, future: Future) -> None:
        error = future.exception()
        if error is not None:
            self.record_error(error)

    @property
    def error(self) -> BaseException | None:
        with self._error_lock:
            return self._error

    def record_error(self, error: BaseException) -> None:
        if isinstance(error, _StageStopped):
            return
        with self._error_lock:
            if self._error is None:
                self._error = error
        self.cancel_event.set()

    def check_stop(self) -> None:
        if self.cancel_event.is_set():
            raise _StageStopped()
        if self.interrupt_event is not None and self.interrupt_event.is_set():
            error = KeyboardInterrupt()
            self.record_error(error)
            raise error

    def acquire_permit(self) -> None:
        while not self._permits.acquire(timeout=0.05):
            self.check_stop()

    def release_permit(self) -> None:
        self._permits.release()

    def release_window(self, window: _IndexWindow) -> None:
        with self._release_lock:
            if window.released:
                return
            window.released = True
        if window.reserved:
            self.body_budget.release(window.reserved)
        if window.has_permit:
            self._permits.release()

    def submit_read(self, window: _IndexWindow) -> None:
        try:
            self._put(self.read_queue, window)
        except BaseException:
            self.release_window(window)
            raise

    def _put(
        self, queue: Queue[_IndexWindow | None], item: _IndexWindow | None
    ) -> None:
        while True:
            try:
                queue.put(item, timeout=0.05)
                return
            except Full:
                self.check_stop()

    def finish_read(self) -> None:
        self.read_queue.put_nowait(None)

    def join(self) -> None:
        self._executor.shutdown(wait=True)
        for future in (self._prep_future, self._write_future):
            if future is not None:
                error = future.exception()
                if error is not None:
                    self.record_error(error)

    def drain_pending(self) -> None:
        for queue in (self.read_queue, self.prepared_queue):
            while True:
                try:
                    item = queue.get_nowait()
                except Empty:
                    break
                if isinstance(item, _IndexWindow):
                    self.release_window(item)

    def _prepare(self) -> None:
        while True:
            try:
                window = self.read_queue.get(timeout=0.05)
            except Empty:
                self.check_stop()
                continue
            if window is None:
                self.prepared_queue.put_nowait(None)
                return
            if self.cancel_event.is_set():
                self.release_window(window)
                continue
            handed_off = False
            try:
                embedding_rows = [
                    row._replace(body="", body_truncated=False)
                    for row in window.rows
                ]
                window.future = self.embedding_pipeline.submit(
                    embedding_rows, cancel_event=self.cancel_event
                )
                self._put(self.prepared_queue, window)
                handed_off = True
            finally:
                if not handed_off:
                    self.release_window(window)

    def _await_vectors(self, window: _IndexWindow) -> dict[int, Any]:
        if window.future is None:
            return {}
        while True:
            self.check_stop()
            try:
                return window.future.result(timeout=0.05)
            except TimeoutError:
                if window.future.done():
                    return window.future.result()
                continue

    def _write(self) -> None:
        db = _open_database(self.db_path)
        try:
            while True:
                try:
                    window = self.prepared_queue.get(timeout=0.05)
                except Empty:
                    self.check_stop()
                    continue
                if window is None:
                    return
                if self.cancel_event.is_set():
                    self.release_window(window)
                    continue
                try:
                    vectors = self._await_vectors(window)
                    _save_rows(
                        db,
                        window.rows,
                        window.next_entry,
                        vectors,
                        fast=self.fast,
                        derived_refs=window.derived_refs,
                        modified=window.modified,
                    )
                    if self.progress is not None:
                        self.progress.advance(window.next_entry)
                finally:
                    self.release_window(window)
        finally:
            db.close()


def _index_entries_staged(
    producer: EntryProducer,
    db: sqlite3.Connection,
    start: int,
    batch_size: int,
    *,
    embedding_pipeline: EmbeddingPipeline,
    body_budget: BodyBudget,
    max_body_text_bytes: int,
    fast: bool,
    derived_stage: Any | None,
    derived_metadata: dict[str, Any] | None,
    derived_refs: dict[int, list[tuple[str, int, str]]] | None,
    db_path: Path,
    progress=None,
    interrupt_event: threading.Event | None = None,
) -> None:
    """Read, prepare, embed, and commit windows with independent stages."""
    count = int(_producer_attr(producer, "count", 0) or 0)
    # The writer owns writes from here on; leave the caller's connection with
    # no outstanding transaction before another connection starts.
    db.commit()
    controller = _StageController(
        embedding_pipeline=embedding_pipeline,
        body_budget=body_budget,
        db_path=db_path,
        fast=fast,
        progress=progress,
        interrupt_event=interrupt_event,
        max_in_flight=embedding_pipeline.max_pending_per_build,
    )
    controller.start()

    batch: list[EntryRecord] = []
    batch_modified: dict[int, int | None] = {}
    window_refs: dict[int, list[tuple[str, int, str]]] = {}
    reserved = 0
    window_open = False

    def ensure_window() -> None:
        nonlocal window_open
        if not window_open:
            controller.acquire_permit()
            window_open = True

    def flush(next_entry: int) -> None:
        nonlocal batch, batch_modified, window_refs, reserved, window_open
        rows = batch
        modified = batch_modified
        refs = window_refs
        reservation = reserved
        has_permit = window_open
        batch = []
        batch_modified = {}
        window_refs = {}
        reserved = 0
        window_open = False
        controller.submit_read(
            _IndexWindow(
                rows,
                modified,
                next_entry,
                reservation,
                refs or None,
                has_permit=has_permit,
            )
        )

    def abort_reader_window() -> None:
        nonlocal batch, batch_modified, window_refs, reserved, window_open
        if reserved:
            body_budget.release(reserved)
            reserved = 0
        if window_open:
            controller.release_permit()
            window_open = False
        batch = []
        batch_modified = {}
        window_refs = {}

    index = start
    try:
        while index < count:
            if interrupt_event is not None and interrupt_event.is_set():
                raise KeyboardInterrupt
            ensure_window()
            row, modified_value = _read_entry(
                producer,
                index,
                fast=fast,
                max_body_text_bytes=max_body_text_bytes,
                derived_stage=derived_stage,
                derived_metadata=derived_metadata,
                derived_refs=derived_refs,
            )
            if row is not None:
                batch_modified[row.id] = modified_value
                if row.body and not fast:
                    size = len(row.body.encode("utf-8", "ignore"))
                    reservation = body_budget.try_reserve(size)
                    if reservation is None:
                        if batch:
                            flush(index)
                        ensure_window()
                        reservation = body_budget.reserve(
                            size, cancel_event=controller.cancel_event
                        )
                    reserved += reservation
                batch.append(row)
                if derived_refs is not None and row.id in derived_refs:
                    window_refs[row.id] = list(derived_refs[row.id])
            index += 1
            if len(batch) >= batch_size:
                flush(index)
        if batch or window_open:
            flush(count)
    finally:
        reader_error = sys.exc_info()[1]
        if reader_error is not None:
            controller.record_error(reader_error)
        abort_reader_window()
        controller.finish_read()
        controller.join()
        controller.drain_pending()
        stage_error = controller.error or reader_error
        if interrupt_event is not None and interrupt_event.is_set():
            raise KeyboardInterrupt
        if stage_error is not None:
            raise stage_error


def _index_entries(
    producer: EntryProducer,
    db: sqlite3.Connection,
    start: int,
    batch_size: int,
    embedder,
    fast: bool,
    embedding_pipeline: EmbeddingPipeline | None,
    *,
    body_budget: BodyBudget,
    max_body_text_bytes: int,
    derived_stage: Any | None = None,
    derived_metadata: dict[str, Any] | None = None,
    derived_refs: dict[int, list[tuple[str, int, str]]] | None = None,
    progress=None,
    token_budget: int = DEFAULT_EMBEDDING_BATCH_TOKENS,
    reuse_vectors=None,
    db_path: Path | None = None,
    stage_split: bool = True,
    interrupt_event: threading.Event | None = None,
) -> None:
    if stage_split and embedding_pipeline is not None and reuse_vectors is None:
        if db_path is None:
            raise BuildError("staged embedding builds require the staging database path")
        _index_entries_staged(
            producer,
            db,
            start,
            batch_size,
            embedding_pipeline=embedding_pipeline,
            body_budget=body_budget,
            max_body_text_bytes=max_body_text_bytes,
            fast=fast,
            derived_stage=derived_stage,
            derived_metadata=derived_metadata,
            derived_refs=derived_refs,
            db_path=db_path,
            progress=progress,
            interrupt_event=interrupt_event,
        )
        return
    pending_embeddings: list[
        tuple[Future, list[EntryRecord], int, int, dict[int, int | None]]
    ] = []
    count = int(_producer_attr(producer, "count", 0) or 0)
    truncated_rows = 0
    try:
        truncated_rows = index_schema.decode_int(
            index_schema.read_meta(db).get(index_schema.BODY_TRUNCATED_COUNT, "0")
        )
    except (ValueError, index_schema.SchemaError):
        truncated_rows = 0

    def save_batch(
        rows: list[EntryRecord],
        modified: dict[int, int | None],
        next_entry: int,
        reserved: int,
    ) -> None:
        nonlocal truncated_rows
        if embedding_pipeline is None:
            try:
                if reuse_vectors is None:
                    truncated_rows += _save(
                        db,
                        embedder,
                        rows,
                        next_entry,
                        fast=fast,
                        derived_refs=derived_refs,
                        token_budget=token_budget,
                        modified=modified,
                    ) or 0
                else:
                    vectors = _embed_rows(
                        embedder, reuse_vectors.missing_rows(rows), token_budget
                    )
                    truncated_rows += _save_rows(
                        db, rows, next_entry, vectors,
                        fast=fast, derived_refs=derived_refs,
                        modified=modified,
                    )
                if progress is not None:
                    progress.advance(next_entry)
            finally:
                body_budget.release(reserved)
            return
        try:
            embedding_rows = (
                reuse_vectors.missing_rows(rows) if reuse_vectors else rows
            )
            future = embedding_pipeline.submit(
                [
                    row._replace(body="", body_truncated=False)
                    for row in embedding_rows
                ],
                cancel_event=interrupt_event,
            )
        except BaseException:
            body_budget.release(reserved)
            raise
        pending_embeddings.append((future, rows, next_entry, reserved, modified))
        if len(pending_embeddings) >= embedding_pipeline.max_pending_per_build:
            drain_one()

    def drain_one() -> None:
        nonlocal truncated_rows
        if interrupt_event is not None and interrupt_event.is_set():
            raise KeyboardInterrupt
        future, pending_rows, pending_next, pending_reserved, pending_modified = (
            pending_embeddings.pop(0)
        )
        try:
            while True:
                if interrupt_event is not None and interrupt_event.is_set():
                    raise KeyboardInterrupt
                try:
                    vectors = future.result(timeout=0.05)
                    break
                except TimeoutError:
                    if future.done():
                        vectors = future.result()
                        break
                    continue
            truncated_rows += _save_rows(
                db, pending_rows, pending_next, vectors,
                fast=fast, derived_refs=derived_refs,
                modified=pending_modified,
            )
            if progress is not None:
                progress.advance(pending_next)
        finally:
            body_budget.release(pending_reserved)

    batch: list[EntryRecord] = []
    batch_modified: dict[int, int | None] = {}
    reserved = 0

    def flush(next_entry: int) -> None:
        nonlocal batch, batch_modified, reserved
        rows, modified, reservation = batch, batch_modified, reserved
        batch = []
        batch_modified = {}
        reserved = 0
        save_batch(rows, modified, next_entry, reservation)

    for index in range(start, count):
        try:
            if interrupt_event is not None and interrupt_event.is_set():
                raise KeyboardInterrupt
            row, modified_value = _read_entry(
                producer,
                index,
                fast=fast,
                max_body_text_bytes=max_body_text_bytes,
                derived_stage=derived_stage,
                derived_metadata=derived_metadata,
                derived_refs=derived_refs,
            )
            if row is not None:
                batch_modified[row.id] = modified_value
                if row.body and not fast:
                    size = len(row.body.encode("utf-8", "ignore"))
                    reservation = body_budget.try_reserve(size)
                    if reservation is None:
                        if batch:
                            flush(index)
                        while pending_embeddings:
                            drain_one()
                        reservation = body_budget.reserve(
                            size, cancel_event=interrupt_event
                        )
                    reserved += reservation
                batch.append(row)
            if len(batch) >= batch_size:
                flush(index + 1)
        except BaseException:
            body_budget.release(reserved)
            reserved = 0
            for _future, _rows, _next, pending_reserved, _modified in pending_embeddings:
                body_budget.release(pending_reserved)
            pending_embeddings.clear()
            raise
    try:
        if batch or start < count:
            flush(count)
        while pending_embeddings:
            drain_one()
    finally:
        body_budget.release(reserved)
        for _future, _rows, _next, pending_reserved, _modified in pending_embeddings:
            body_budget.release(pending_reserved)


class _AllFilesProgress:
    """Estimate overall work without opening queued sources to count entries."""

    def __init__(self, count: int):
        self._lock = threading.Lock()
        self._fractions = [0.0] * count
        self._closed = False
        self._bar = tqdm(
            total=count,
            desc="All files",
            unit="file",
            delay=5,
            bar_format="{desc}: {percentage:3.0f}%|{bar}| "
            "{n:.2f}/{total_fmt} files [{elapsed}<{remaining}, {rate_fmt}]",
        )

    def advance(self, index: int, position: int, total: int) -> None:
        # Leave completion to the caller, after FAISS and publication finish.
        fraction = min(position / total, 0.99) if total else 0.0
        self._advance(index, fraction)

    def finish(self, index: int) -> None:
        self._advance(index, 1.0)

    def _advance(self, index: int, fraction: float) -> None:
        with self._lock:
            if self._closed:
                return
            previous = self._fractions[index]
            fraction = max(previous, fraction)
            self._fractions[index] = fraction
            self._bar.update(fraction - previous)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._bar.close()


class _BuildProgress:
    """Report committed article work separately from cheap entries."""

    def __init__(self, producer, start: int, callback=None):
        self._stack = ExitStack()
        self._callback = callback
        self._count = producer.count
        self._key = str(_producer_attr(producer, "key", "index"))
        self._work = _producer_attr(producer, "article_work_count")
        self._position = start
        total = self._work if self._work is not None else producer.count
        self._bar = self._stack.enter_context(tqdm(
            total=total,
            initial=min(start, total),
            desc=self._key + (" articles" if self._work is not None else ""),
            delay=5,
        ))
        self._cheap = None
        if self._callback is not None:
            self._callback(start, self._count)

    def advance(self, position: int) -> None:
        if self._work is None:
            self._bar.update(position - self._position)
        else:
            self._bar.update(min(position, self._work) - min(self._position, self._work))
            if position > self._work:
                if self._cheap is None:
                    self._bar.close()
                    self._cheap = self._stack.enter_context(tqdm(
                        desc=self._key + " redirects/assets",
                        initial=max(0, self._position - self._work),
                        total=None,
                        bar_format="{desc}: {n_fmt} entries [{elapsed}, {rate_fmt}]",
                        delay=5,
                    ))
                self._cheap.update(position - max(self._position, self._work))
        self._position = position
        if self._callback is not None:
            self._callback(position, self._count)

    def close(self) -> None:
        self._stack.close()


def _finish_build(
    state: _BuildState,
    *,
    producer: EntryProducer,
    fast: bool,
    model_fingerprint: str = "",
    model_probes: dict[str, str] | None = None,
    derived_text: Mapping[str, Any] | None = None,
    reuse_vectors=None,
) -> None:
    db = state.db
    if db is None:
        raise BuildError("build database is already closed")

    vector_count = int(db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0])
    indexed_count = int(db.execute("SELECT COUNT(*) FROM docs").fetchone()[0])
    body_truncated_count = int(
        index_schema.decode_int(
            index_schema.read_meta(db).get(index_schema.BODY_TRUNCATED_COUNT, "0")
        )
        if not fast
        else 0
    )

    if fast:
        faiss_digest = ""
    elif reuse_vectors is not None:
        vector_count = reuse_vectors.write(state.build_faiss_path)
        fsync_path(state.build_faiss_path)
        faiss_digest = _file_digest(state.build_faiss_path)
    elif vector_count:
        _write_faiss(db, state.build_faiss_path)
        faiss_digest = _file_digest(state.build_faiss_path)
    else:
        # A valid empty semantic channel: no FAISS artifact and no vector count.
        state.build_faiss_path.unlink(missing_ok=True)
        faiss_digest = ""

    with db:
        db.execute("DELETE FROM vecs")
        index_schema.delete_meta(
            db,
            [
                index_schema.NEXT,
                index_schema.BUILD_FILESIZE,
                index_schema.BUILD_MODE,
                MODEL_FINGERPRINT_KEY,
                MODEL_PROBE_TEXTS_KEY,
                MODEL_PROBE_VECTORS_KEY,
                MODEL_PROBE_SHAPE_KEY,
                index_schema.FAISS_DIGEST,
                index_schema.DERIVED_TEXT,
                index_schema.BUILD_SCOPE,
                _REUSED_VECTOR_SOURCE,
            ],
        )
        if fast:
            index_schema.delete_meta(
                db, [index_schema.BODY_TRUNCATED_COUNT, index_schema.SEMANTIC_VECTOR_COUNT]
            )
        else:
            index_schema.write_meta(
                db,
                {
                    index_schema.BODY_TRUNCATED_COUNT: index_schema.encode_int(
                        body_truncated_count
                    ),
                    index_schema.SEMANTIC_VECTOR_COUNT: index_schema.encode_int(
                        vector_count
                    ),
                },
            )
        channels = (
            (index_schema.CHANNEL_TITLE,)
            if fast
            else (
                index_schema.CHANNEL_TITLE,
                index_schema.CHANNEL_BODY,
                index_schema.CHANNEL_SEMANTIC,
            )
        )
        values = {
            index_schema.DONE: index_schema.DONE_FAST if fast else index_schema.DONE_FULL,
            index_schema.FILESIZE: index_schema.encode_int(state.filesize),
            index_schema.INDEXED_COUNT: index_schema.encode_int(indexed_count),
            index_schema.TRUNCATED: index_schema.encode_bool(
                bool(_producer_attr(producer, "truncated", False))
            ),
            index_schema.INDEXED_CHANNELS: index_schema.encode_channels(channels),
            index_schema.SCHEMA_VERSION: index_schema.encode_int(
                index_schema.CURRENT_SCHEMA_VERSION
            ),
            index_schema.BUILD_GENERATION: state.generation,
        }
        if faiss_digest:
            values[index_schema.FAISS_DIGEST] = faiss_digest
        if model_fingerprint:
            values[MODEL_FINGERPRINT_KEY] = model_fingerprint
        if model_probes:
            values.update(model_probes)
        if derived_text:
            values[index_schema.DERIVED_TEXT] = index_schema.encode_json(
                dict(derived_text)
            )
        build_scope = _producer_attr(producer, "build_scope")
        if build_scope is not None:
            values[index_schema.BUILD_SCOPE] = index_schema.encode_json(build_scope)
        index_schema.write_meta(db, values)
    db.execute("VACUUM")
    db.commit()
    index_schema.read_index_meta(db, completed=True)


def _derived_baseline(
    cfg: Mapping[str, Any], producer: Any, *, fast: bool
) -> dict[str, Any]:
    from .derived import source_operations

    operations = source_operations(cfg, producer)
    states: dict[str, str] = {
        "captions": "off",
        "ocr": "off",
        "asr": "off",
    }
    if operations is not None:
        for name in ("captions", "ocr", "asr"):
            enabled = (
                bool(getattr(operations, "captions", False))
                if name == "captions"
                else bool(getattr(operations, name, None))
            )
            states[name] = "not_checked" if enabled else "off"
    return {
        "operations": states,
        "contributors": [],
        "partial": False,
        "build_mode": "fast" if fast else "full",
    }


def _report_from_meta(
    source: Any,
    meta: Mapping[str, str],
    *,
    status: str,
    started: float,
    fast: bool,
    producer: Any,
) -> BuildReport:
    from . import schema as index_schema

    def _decoded(name: str, default: int | bool) -> Any:
        value = meta.get(name)
        if value is None:
            return default
        try:
            if isinstance(default, bool):
                return index_schema.decode_bool(value)
            return index_schema.decode_int(value)
        except (TypeError, ValueError):
            return default

    indexed = _decoded(index_schema.INDEXED_COUNT, 0)
    count = int(_producer_attr(producer, "count", 0) or 0)
    return BuildReport(
        provider=str(getattr(source, "provider", "") or ""),
        collection_key=str(getattr(source, "collection_key", "") or ""),
        source_key=str(getattr(source, "key", "") or ""),
        status=status,
        indexed_count=int(indexed),
        skipped_count=max(0, count - int(indexed)),
        truncated=bool(_decoded(index_schema.TRUNCATED, False)),
        body_truncated_count=int(
            _decoded(index_schema.BODY_TRUNCATED_COUNT, 0)
        ),
        duration_seconds=max(0.0, time.monotonic() - started),
        fast=fast,
    )


def build_index(
    producer: EntryProducer,
    locator: IndexLocator,
    embedder,
    cfg: Mapping[str, Any] | None = None,
    *,
    fast: bool = False,
    force: bool = False,
    _allow_reuse: bool = True,
) -> BuildReport:
    """Build and publish one complete immutable index generation.

    The producer supplies the only provider-specific behaviour.  The core owns
    staging, resume, embedding, vector publication, metadata, and publication.
    The returned :class:`BuildReport` describes what that source contributed.
    """
    started = time.monotonic()
    from ..scoped_settings import resolve_settings

    cfg = resolve_settings(cfg or {}, producer, getattr(producer, "config", None))
    max_body_setting = (
        cfg.get("max_body_text_bytes", DEFAULT_MAX_BODY_TEXT_BYTES)
        if isinstance(cfg, Mapping)
        else DEFAULT_MAX_BODY_TEXT_BYTES
    )
    if (
        isinstance(max_body_setting, bool)
        or not isinstance(max_body_setting, int)
        or max_body_setting < 1
    ):
        raise ValueError("max_body_text_bytes must be a positive integer")
    source = producer.source
    interrupt_event = cfg.get("interrupt_event")
    state: _BuildState | None = None
    progress = None
    completed = False
    try:
        state = _prepare_build_state(producer, locator, fast=fast, force=force, cfg=cfg)
        if state is None:
            current = _read_published_state(locator, source)
            meta: Mapping[str, str] = {}
            if current is not None:
                meta, lease = current
                lease.close()
            return _report_from_meta(
                source,
                meta,
                status="skipped",
                started=started,
                fast=fast,
                producer=producer,
            )
        if state.db is None:
            raise BuildError("build database is unavailable")
        completed_staging = state.meta.get(index_schema.DONE) in index_schema.DONE_VALUES
        reuse_vectors = None
        if not completed_staging:
            start = int(state.meta.get(index_schema.NEXT, "0"))
            previous_reuse = state.meta.get(_REUSED_VECTOR_SOURCE)
            opener = getattr(producer, "reusable_vectors", None)
            if (
                _allow_reuse and state.allow_vector_reuse
                and not fast and not force and callable(opener)
                and (not start or previous_reuse)
            ):
                reuse_vectors = opener(locator.index_dir, state.db, cfg, embedder)
            if previous_reuse and (
                reuse_vectors is None or previous_reuse != reuse_vectors.token
            ):
                logger.warning(
                    "%s: reusable vector source changed during an interrupted "
                    "build; restarting", source.key,
                )
                state.db.close()
                state.db = None
                _clear_staging(state.build_db_path, state.build_faiss_path)
                state = None
                return build_index(
                    producer, locator, embedder, cfg, fast=fast, force=force,
                    _allow_reuse=False,
                )
            if reuse_vectors is not None:
                _check_staging_space(
                    state.build_db_path.parent,
                    current_bytes=(
                        reuse_vectors.storage_bytes
                        + _directory_bytes(state.build_db_path.parent / "generations")
                    ),
                    reserve=_cfg_int(
                        cfg, "staging_reserve_bytes", DEFAULT_STAGING_RESERVE_BYTES
                    ),
                )
                with state.db:
                    index_schema.write_meta(
                        state.db, {_REUSED_VECTOR_SOURCE: reuse_vectors.token}
                    )
        derived_metadata = _derived_baseline(cfg, producer, fast=fast)
        derived_refs: dict[int, list[tuple[str, int, str]]] = {}
        derived_stage = cfg.get("derived_stage")
        if derived_stage is None and not fast and not completed_staging:
            from .derived import source_operations

            operations = source_operations(cfg, producer)
            if operations is not None and getattr(
                operations, "any_enabled", False
            ):
                from .derived import (
                    DerivedTextCache,
                    DerivedTextStage,
                    source_derived_config,
                )

                derived_config = source_derived_config(cfg, producer)
                cache = cfg.get("derived_cache")
                if cache is None and derived_config.cache:
                    source_directory = locator.source_directory(
                        source.provider,
                        getattr(source, "collection_key", ""),
                        source.key,
                    )
                    cache = DerivedTextCache(
                        source_directory / "derived-cache",
                        max_bytes=max(
                            0,
                            _cfg_int(
                                cfg,
                                "max_derived_cache_bytes_per_source",
                                512 << 20,
                            ),
                        ),
                    )
                derived_stage = DerivedTextStage(
                    derived_config,
                    operations=operations,
                    cache=cache,
                    engines=cfg.get("derived_engines") or {},
                    worker_pool=cfg.get("derived_worker_pool"),
                    spool_directory=cfg.get("derived_spool_directory"),
                )
        if not completed_staging:
            start = int(state.meta.get(index_schema.NEXT, "0"))
            prepare = getattr(producer, "prepare_iteration", None)
            if callable(prepare):
                recorded_order = state.meta.get(index_schema.ENTRY_ORDER)
                recorded_fingerprint = state.meta.get(
                    index_schema.ENTRY_ORDER_FINGERPRINT
                )
                if start and (not recorded_order or not recorded_fingerprint):
                    raise SnapshotChangedError(
                        f"{source.key}: staged index has no reading-order "
                        "fingerprint; rebuild it"
                    )
                order = prepare(recorded_order)
                order_fingerprint = getattr(producer, "order_fingerprint", "")
                if recorded_fingerprint and recorded_fingerprint != order_fingerprint:
                    raise SnapshotChangedError(
                        f"{source.key}: entry reading order changed; refusing an unsafe resume"
                    )
                with state.db:
                    index_schema.write_meta(state.db, {
                        index_schema.ENTRY_ORDER: order,
                        index_schema.ENTRY_ORDER_FINGERPRINT: order_fingerprint,
                    })
            batch_size = max(1, _cfg_int(cfg, "batch_size", 32))
            if fast:
                batch_size = max(batch_size, FAST_BUILD_BATCH_SIZE)
            max_body_text_bytes = max(
                1,
                _cfg_int(
                    cfg, "max_body_text_bytes", DEFAULT_MAX_BODY_TEXT_BYTES
                ),
            )
            body_budget = cfg.get("body_budget")
            if not isinstance(body_budget, BodyBudget):
                body_budget = shared_body_budget(max_body_text_bytes)
            progress = _BuildProgress(producer, start, cfg.get("progress_callback"))
            _index_entries(
                producer,
                state.db,
                start,
                batch_size,
                embedder,
                fast,
                cfg.get("embedding_pipeline"),
                body_budget=body_budget,
                max_body_text_bytes=max_body_text_bytes,
                derived_stage=derived_stage,
                derived_metadata=derived_metadata,
                derived_refs=derived_refs,
                progress=progress,
                token_budget=max(1, _cfg_int(
                    cfg, "embedding_batch_tokens", DEFAULT_EMBEDDING_BATCH_TOKENS
                )),
                reuse_vectors=reuse_vectors,
                db_path=state.build_db_path,
                stage_split=bool(cfg.get("_stage_split", True)),
                interrupt_event=cfg.get("interrupt_event"),
            )
            progress.close()
            progress = None
            if reuse_vectors is not None and not reuse_vectors.valid():
                logger.warning(
                    "%s: v0 vector IDs or paths do not match the archive; "
                    "rebuilding without them", source.key,
                )
                state.db.close()
                state.db = None
                _clear_staging(state.build_db_path, state.build_faiss_path)
                state = None
                return build_index(
                    producer, locator, embedder, cfg, fast=fast, force=force,
                    _allow_reuse=False,
                )
            fingerprint = getattr(embedder, "fingerprint", "") if embedder else ""
            model_probes = None
            if reuse_vectors is not None:
                fingerprint = reuse_vectors.fingerprint
                model_probes = reuse_vectors.probes
            elif (
                not fast
                and embedder is not None
                and state.db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
            ):
                # An index with no vectors has nothing to reuse; keep title-only
                # and empty builds from initializing the model just for probes.
                try:
                    from ..embed import embed_model_probes, serialize_model_probes

                    probe_texts, probe_vectors = embed_model_probes(embedder)
                    model_probes = serialize_model_probes(probe_texts, probe_vectors)
                except Exception as error:
                    logger.warning(
                        "could not compute model compatibility probes for %s (%s); "
                        "a later serve with a different model file will not reuse "
                        "these vectors",
                        source.key,
                        public_cause(error),
                    )
                    logger.debug("model probe error for %s: %s", source.key, error)
            _finish_build(
                state,
                producer=producer,
                fast=fast,
                model_fingerprint=fingerprint,
                model_probes=model_probes,
                derived_text={
                    **derived_metadata,
                    "cache_references": sum(
                        len(refs) for refs in derived_refs.values()
                    ),
                },
                reuse_vectors=reuse_vectors,
            )
        if not producer.validate_snapshot():
            raise SnapshotChangedError(
                f"{source.key}: source or build inputs changed during the build; "
                "the previous published generation was left untouched"
            )
        if state.db is not None:
            state.db.close()
            state.db = None
        lease = locator.publish(
            source,
            state.build_db_path,
            None if fast else (
                state.build_faiss_path
                if state.build_faiss_path.is_file()
                else None
            ),
            generation=state.generation,
            metadata={
                "schema_version": index_schema.CURRENT_SCHEMA_VERSION,
                "source_fingerprint": state.source_fingerprint,
                "source_size": producer.source_size,
                "source_mtime": _producer_attr(producer, "source_mtime", None),
                "build_generation": state.generation,
                "provider_id": _producer_attr(producer, "provider_id", None),
            },
        )
        lease.close()
        locator.cleanup(source)
        state.build_db_path.unlink(missing_ok=True)
        state.build_faiss_path.unlink(missing_ok=True)
        completed = True
        current = _read_published_state(locator, source)
        report_meta: Mapping[str, str] = {}
        if current is not None:
            report_meta, report_lease = current
            report_lease.close()
        report = _report_from_meta(
            source,
            report_meta,
            status="published",
            started=started,
            fast=fast,
            producer=producer,
        )
        if derived_metadata.get("partial"):
            raise PartialEnrichmentError(
                f"{source.key}: requested derived-text enrichment was partial; "
                "available channels were published"
            )
        logger.debug("%s: index ready", source.key)
        return report
    except InsufficientSpaceError:
        raise
    except BaseException as error:
        if state is not None:
            if state.db is not None:
                state.db.close()
                state.db = None
            if not completed and (
                isinstance(error, KeyboardInterrupt)
                or (interrupt_event is not None and interrupt_event.is_set())
            ):
                logger.debug("%s: index build interrupted; staging preserved", source.key)
                raise KeyboardInterrupt from None
            if completed:
                logger.warning(
                    "%s: partial enrichment published (%s); the available "
                    "channels remain searchable and a later build will retry",
                    source.key,
                    public_cause(error),
                )
                logger.debug("partial enrichment error for %s: %s", source.key, error)
                raise
            _clear_staging(state.build_db_path, state.build_faiss_path)
            if state.publish_upgrade:
                logger.warning(
                    "%s: index build failed (%s); the previous published "
                    "generation was left untouched",
                    source.key,
                    public_cause(error),
                )
                logger.debug("index build error for %s: %s", source.key, error)
            else:
                logger.warning(
                    "%s: index build failed (%s); no partial generation was "
                    "published",
                    source.key,
                    public_cause(error),
                )
                logger.debug("index build error for %s: %s", source.key, error)
        raise
    finally:
        if progress is not None:
            progress.close()
        if state is not None and state.db is not None:
            state.db.close()
        try:
            producer.close()
        except Exception:
            pass


def _file_digest(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_temp_path(path: Path) -> Path:
    """Reserve a readable sibling temp file."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for _ in range(100):
        candidate = path.with_name(
            f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        )
        try:
            fd = os.open(candidate, flags, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise RuntimeError(f"could not create a temporary file beside {path}")


def _embedding_batches(embedder, rows: list[EntryRecord], token_budget: int):
    """Bound padded tokens per call; sort actual capped inputs, not characters."""
    if embedder is None:
        return []
    articles = [row for row in rows if row.excerpt]
    if not articles:
        return []
    passages = []
    passage_prefix = getattr(embedder, "passage_prefix", "passage: ")
    # Embedder.encode already caps the token ids, so pre-truncating the text
    # only re-tokenizes long excerpts several times.  Keep the text truncation
    # for embedders that do not make that guarantee.
    skip_truncation = getattr(embedder, "caps_tokens_on_encode", False)
    for row in articles:
        prefix = f"{passage_prefix}{row.title}\n"
        if skip_truncation:
            passages.append(prefix + row.excerpt)
        else:
            passages.append(prefix + embedder.truncate(row.excerpt, prefix=prefix))
    encoded = embedder.encode(passages)
    ordered = sorted(zip(articles, encoded), key=lambda pair: len(pair[1]))
    batch_shape = getattr(embedder, "batch_shape", None)
    batches = []
    ids, inputs = [], []
    current_shape = None
    for row, tokens in ordered:
        # Padding, not just useful tokens, occupies the model workspace.
        shape = batch_shape(len(tokens)) if batch_shape is not None else None
        full = (
            shape != current_shape or len(inputs) >= shape[0]
            if shape is not None
            else (len(inputs) + 1) * len(tokens) > token_budget
        )
        if inputs and full:
            batches.append((ids, inputs))
            ids, inputs = [], []
        current_shape = shape
        ids.append(row.id)
        inputs.append(tokens)
    if inputs:
        batches.append((ids, inputs))
    return batches


def _embed_rows(
    embedder, rows: list[EntryRecord],
    token_budget: int = DEFAULT_EMBEDDING_BATCH_TOKENS,
) -> dict[int, Any]:
    """Embed article rows and return vectors keyed by canonical row id."""
    result = {}
    for ids, inputs in _embedding_batches(
        embedder, rows, token_budget
    ):
        vectors = embedder.embed_encoded(inputs)
        result.update(zip(ids, vectors))
    return result


def _save_rows(
    db: sqlite3.Connection,
    rows: list[EntryRecord],
    next_entry: int,
    vectors: dict[int, Any],
    *,
    fast: bool = False,
    derived_refs: dict[int, list[tuple[str, int, str]]] | None = None,
    modified: dict[int, int | None] | None = None,
) -> int:
    """Store one ordered batch and its resume point in one transaction.

    Returns the number of records whose lexical body was truncated.  Contentless
    body FTS is append-only: a resumed committed row is never overwritten.
    """
    import numpy as np

    truncated = 0
    with db:
        db.executemany(
            "INSERT INTO docs(rowid, title, path, target, modified) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    row.id,
                    row.title,
                    row.path,
                    row.target_id,
                    (modified or {}).get(row.id),
                )
                for row in rows
            ],
        )
        if not fast:
            body_rows = [
                (row.id, row.body) for row in rows if getattr(row, "body", "")
            ]
            if body_rows:
                db.executemany(
                    "INSERT INTO body(rowid, text) VALUES (?, ?)", body_rows
                )
            if derived_refs:
                ref_rows = [
                    (row.id, operation, order, cache_key)
                    for row in rows
                    for operation, order, cache_key in derived_refs.get(row.id, ())
                ]
                if ref_rows:
                    db.executemany(
                        "INSERT INTO derived_refs"
                        "(rowid, operation, contribution_order, cache_key) "
                        "VALUES (?, ?, ?, ?)",
                        ref_rows,
                    )
            truncated = sum(
                1 for row in rows if getattr(row, "body_truncated", False)
            )
            if truncated:
                previous = index_schema.decode_int(
                    index_schema.read_meta(db).get(
                        index_schema.BODY_TRUNCATED_COUNT, "0"
                    )
                )
                index_schema.write_meta(
                    db,
                    {
                        index_schema.BODY_TRUNCATED_COUNT: index_schema.encode_int(
                            previous + truncated
                        )
                    },
                )
        db.executemany(
            "INSERT INTO vecs(id, v) VALUES (?, ?)",
            [
                (row.id, vectors[row.id].astype(np.float16).tobytes())
                for row in rows
                if row.id in vectors
            ],
        )
        index_schema.write_meta(
            db, {index_schema.NEXT: index_schema.encode_int(next_entry)}
        )
    return truncated


def _save(
    db: sqlite3.Connection,
    embedder,
    rows: list[EntryRecord],
    next_entry: int,
    *,
    fast: bool = False,
    derived_refs: dict[int, list[tuple[str, int, str]]] | None = None,
    token_budget: int = DEFAULT_EMBEDDING_BATCH_TOKENS,
    modified: dict[int, int | None] | None = None,
) -> int:
    """Embed and store a batch and the resume point in one transaction."""
    return _save_rows(
        db,
        rows,
        next_entry,
        _embed_rows(embedder, rows, token_budget),
        fast=fast,
        derived_refs=derived_refs,
        modified=modified,
    )


def _load(rows):
    import numpy as np

    ids = np.array([row[0] for row in rows], dtype=np.int64)
    vectors = np.frombuffer(
        b"".join(row[1] for row in rows),
        dtype=np.float16,
    ).reshape(len(rows), -1)
    return ids, vectors.astype(np.float32)


def _load_faiss():
    import faiss

    return faiss


def _write_faiss(db: sqlite3.Connection, path: Path) -> None:
    faiss = _load_faiss()

    n = db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
    dim = db.execute("SELECT length(v) / 2 FROM vecs LIMIT 1").fetchone()
    dim = dim[0] if dim else 384

    if n < 10_000:
        index = faiss.index_factory(dim, "IDMap,Flat", faiss.METRIC_INNER_PRODUCT)
    else:
        cluster_centre_count = _faiss_cluster_count(n)
        index = faiss.index_factory(
            dim,
            f"IVF{cluster_centre_count},SQ8",
            faiss.METRIC_INNER_PRODUCT,
        )
        step = _faiss_training_step(n, cluster_centre_count)
        training_rows = db.execute(
            "SELECT id, v FROM ("
            "SELECT id, v, ROW_NUMBER() OVER (ORDER BY id) AS position "
            "FROM vecs"
            ") WHERE (position - 1) % ? = 0",
            (step,),
        ).fetchall()
        index.train(_load(training_rows)[1])
    last = -1
    while rows := db.execute(
        "SELECT id, v FROM vecs WHERE id > ? ORDER BY id LIMIT 50000",
        (last,),
    ).fetchall():
        ids, vectors = _load(rows)
        index.add_with_ids(vectors, ids)
        last = int(ids[-1])
    temp_path = _create_temp_path(path)
    try:
        faiss.write_index(index, str(temp_path))
        fsync_path(temp_path)
        os.replace(temp_path, path)
        fsync_path(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def _faiss_cluster_count(n: int) -> int:
    return max(1, min(int(4 * n**0.5), n // _FAISS_MIN_POINTS_PER_CENTROID))


def _faiss_training_step(n: int, cluster_centre_count: int) -> int:
    training_points = min(
        n,
        max(
            _FAISS_MIN_TRAINING_POINTS,
            _FAISS_MIN_POINTS_PER_CENTROID * cluster_centre_count,
        ),
    )
    return max(1, n // training_points)


__all__ = [
    "SCHEMA",
    "BodyBudget",
    "BuildError",
    "EmbeddingPipeline",
    "InsufficientSpaceError",
    "PartialEnrichmentError",
    "SnapshotChangedError",
    "build_index",
    "shared_body_budget",
]
