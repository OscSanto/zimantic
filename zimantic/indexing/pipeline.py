"""Generic, resumable SQLite/FAISS index build pipeline.

Only the entry source is provider-specific.  Everything in this module works
with :class:`zimantic.indexing.types.EntryProducer`; there are no libzim,
provider-discovery, web, or FastAPI imports here (FAISS is imported lazily
inside the writer/loader functions).
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from threading import Thread
from types import SimpleNamespace
from typing import Any, Mapping

from tqdm import tqdm

from ..diagnostics import public_cause
from ..model_meta import (
    MODEL_FINGERPRINT_KEY,
    MODEL_PROBE_SHAPE_KEY,
    MODEL_PROBE_TEXTS_KEY,
    MODEL_PROBE_VECTORS_KEY,
)
from . import schema as index_schema
from .types import EntryProducer, EntryRecord

SCHEMA = index_schema.SCHEMA

logger = logging.getLogger(__name__)

_FAISS_MIN_POINTS_PER_CENTROID = 39
_FAISS_MIN_TRAINING_POINTS = 100_000

DEFAULT_MAX_BODY_TEXT_BYTES = 1 << 20
DEFAULT_BODY_BUFFER_BYTES = 16 << 20
DEFAULT_STAGING_RESERVE_BYTES = 1 << 20
DEFAULT_EMBEDDING_BATCH_TOKENS = 1000


class BuildError(RuntimeError):
    """A build cannot complete safely."""


class InsufficientSpaceError(BuildError):
    """The staging generation cannot be written without risking the source."""


class SnapshotChangedError(BuildError):
    """The producer's source or build inputs changed during the build."""


class PartialEnrichmentError(BuildError):
    """A requested derived operation was incomplete; available channels published."""


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
    inputs: list
    encoded: bool
    future: Future


class EmbeddingPipeline:
    """Bounded embedding stage shared by concurrent builds."""

    def __init__(
        self,
        embedder,
        workers: int = 1,
        queue_size: int = 8,
        max_pending_per_build: int = 2,
        token_budget: int = DEFAULT_EMBEDDING_BATCH_TOKENS,
    ) -> None:
        workers = int(workers)
        if workers < 1:
            raise ValueError("embedding workers must be at least 1")
        self.max_pending_per_build = max(1, int(max_pending_per_build))
        self._embedder = embedder
        self.token_budget = max(1, int(token_budget))
        self._requests: Queue[_EmbeddingRequest | None] = Queue(
            maxsize=max(1, int(queue_size))
        )
        self._threads = [
            Thread(target=self._run, name=f"zimantic-embed-{index}", daemon=True)
            for index in range(workers)
        ]
        for thread in self._threads:
            thread.start()
        self._closed = False

    def submit(self, rows: list[EntryRecord]) -> Future:
        if self._closed:
            raise RuntimeError("embedding pipeline is closed")
        future = Future()
        batches = _embedding_batches(self._embedder, rows, self.token_budget)
        if not batches:
            future.set_result({})
            return future
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

        for ids, inputs, encoded in batches:
            batch_future = Future()
            batch_future.add_done_callback(finished)
            self._requests.put(_EmbeddingRequest(ids, inputs, encoded, batch_future))
        return future

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for _ in self._threads:
            self._requests.put(None)
        for thread in self._threads:
            thread.join()

    def _run(self) -> None:
        while True:
            request = self._requests.get()
            if request is None:
                return
            try:
                vectors = (
                    self._embedder.embed_encoded(request.inputs)
                    if request.encoded
                    else self._embedder.embed(request.inputs)
                )
                request.future.set_result(dict(zip(request.ids, vectors)))
            except BaseException as error:  # surface it to the build thread
                logger.debug("embedding batch failed", exc_info=True)
                request.future.set_exception(error)


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

    def reserve(self, size: int) -> int:
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
            self._condition.wait_for(
                lambda: self._reserved == 0 or self._reserved + requested <= self.capacity
            )
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
    global _DEFAULT_BODY_BUDGET
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


def _cfg_int(cfg: Mapping[str, Any], key: str, default: int) -> int:
    value = cfg.get(key, default) if isinstance(cfg, Mapping) else default
    if isinstance(value, bool) or not isinstance(value, int):
        return int(default)
    return value


def _producer_attr(producer: Any, name: str, default: Any = None) -> Any:
    return getattr(producer, name, default)


def _source_for_locator(producer: Any) -> Any:
    if all(hasattr(producer, name) for name in ("key", "provider")):
        return producer
    source = _producer_attr(producer, "source", None)
    if source is not None:
        return source
    return SimpleNamespace(
        key=str(_producer_attr(producer, "key", "")),
        provider=str(_producer_attr(producer, "provider", "")),
        collection_key=str(_producer_attr(producer, "collection_key", "")),
    )


def _locator_for(locator: Any) -> Any:
    if hasattr(locator, "source_directory") and hasattr(locator, "paths"):
        return locator
    from .locator import IndexLocator

    return IndexLocator(locator)


def _filesize_for(producer: Any) -> int:
    for name in ("filesize", "source_size", "size"):
        value = _producer_attr(producer, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    return 0


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
    db = sqlite3.connect(path)
    index_schema.ensure_schema(db)
    index_schema.validate_schema(db)
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
            index_schema.validate_schema(db)
            return index_schema.read_meta(db), lease
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
    locator: Any,
    *,
    fast: bool,
    force: bool,
    cfg: Mapping[str, Any] | None = None,
) -> _BuildState | None:
    """Resolve, validate, and resume-or-create one staging generation."""
    cfg = cfg or {}
    locator = _locator_for(locator)
    source = _source_for_locator(producer)
    directory = locator.source_directory(
        source.provider, getattr(source, "collection_key", ""), source.key
    )
    current: tuple[dict[str, str], Any] | None = None
    if not force:
        current = _read_published_state(locator, source)
    if current is not None:
        current_meta, lease = current
        lease.close()
        done = str(current_meta.get(index_schema.DONE, ""))
        partial_enrichment = False
        try:
            raw_derived = current_meta.get(index_schema.DERIVED_TEXT)
            if raw_derived:
                decoded = index_schema.decode_json(raw_derived)
                partial_enrichment = bool(
                    isinstance(decoded, dict) and decoded.get("partial")
                )
        except (TypeError, ValueError):
            partial_enrichment = False
        current_fingerprint = str(
            current_meta.get(index_schema.SOURCE_FINGERPRINT, "") or ""
        )
        producer_fingerprint = str(
            _producer_attr(producer, "fingerprint", "") or ""
        )
        fingerprint_matches = (
            not current_fingerprint
            or not producer_fingerprint
            or current_fingerprint == producer_fingerprint
        )
        if (
            done == index_schema.DONE_FULL
            and not partial_enrichment
            and fingerprint_matches
        ):
            logger.info("%s: already built (use --force to rebuild)", source.key)
            return None
        if (
            done == index_schema.DONE_FAST
            and fast
            and not partial_enrichment
            and fingerprint_matches
        ):
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

    fingerprint = str(_producer_attr(producer, "fingerprint", "") or "")
    publish_upgrade = force or (current is not None)
    db = _open_or_reset(build_db_path)
    try:
        meta = index_schema.read_meta(db)
        done = str(meta.get(index_schema.DONE, ""))
        if done in index_schema.DONE_VALUES:
            if done == mode and not force:
                # An earlier run completed staging but publication did not
                # happen.  Reuse it only if its identity still matches.
                if str(meta.get(index_schema.SOURCE_FINGERPRINT, "")) == fingerprint:
                    return _BuildState(
                        db=db,
                        build_db_path=build_db_path,
                        build_faiss_path=build_faiss_path,
                        publish_upgrade=True,
                        filesize=_filesize_for(producer),
                        meta=meta,
                        source_fingerprint=fingerprint,
                        build_mode=mode,
                        generation=str(
                            meta.get(index_schema.BUILD_GENERATION, "")
                            or uuid.uuid4()
                        ),
                    )
            db.close()
            db = _reset_staging(build_db_path, build_faiss_path)
            meta = {}
        recorded_fingerprint = meta.get(index_schema.SOURCE_FINGERPRINT)
        if (
            recorded_fingerprint is not None
            and fingerprint
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
        try:
            next_ordinal = int(meta.get(index_schema.NEXT, "0"))
        except (TypeError, ValueError):
            next_ordinal = 0
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
                        _filesize_for(producer)
                    ),
                    index_schema.BUILD_MODE: mode,
                    index_schema.SCHEMA_VERSION: index_schema.encode_int(
                        index_schema.CURRENT_SCHEMA_VERSION
                    ),
                    index_schema.BUILD_GENERATION: generation,
                    index_schema.PROVIDER: str(source.provider),
                    index_schema.COLLECTION_KEY: str(
                        getattr(source, "collection_key", "") or ""
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
            filesize=_filesize_for(producer),
            meta=meta,
            source_fingerprint=fingerprint,
            build_mode=mode,
            generation=generation,
        )
    except Exception:
        db.close()
        raise


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
) -> None:
    pending_embeddings: list[tuple[Future, list[EntryRecord], int, int]] = []
    count = int(_producer_attr(producer, "count", 0) or 0)
    truncated_rows = 0
    try:
        truncated_rows = index_schema.decode_int(
            index_schema.read_meta(db).get(index_schema.BODY_TRUNCATED_COUNT, "0")
        )
    except (ValueError, index_schema.SchemaError):
        truncated_rows = 0

    def save_batch(rows: list[EntryRecord], next_entry: int, reserved: int) -> None:
        nonlocal truncated_rows
        if embedding_pipeline is None:
            try:
                truncated_rows += _save(
                    db,
                    embedder,
                    rows,
                    next_entry,
                    fast=fast,
                    derived_refs=derived_refs,
                    token_budget=token_budget,
                ) or 0
                if progress is not None:
                    progress.advance(next_entry)
            finally:
                body_budget.release(reserved)
            return
        try:
            future = embedding_pipeline.submit([
                row._replace(body="", body_truncated=False) for row in rows
            ])
        except BaseException:
            body_budget.release(reserved)
            raise
        pending_embeddings.append((future, rows, next_entry, reserved))
        if len(pending_embeddings) >= embedding_pipeline.max_pending_per_build:
            drain_one()

    def drain_one() -> None:
        nonlocal truncated_rows
        future, pending_rows, pending_next, pending_reserved = pending_embeddings.pop(0)
        try:
            truncated_rows += _save_rows(
                db, pending_rows, pending_next, future.result(),
                fast=fast, derived_refs=derived_refs,
            )
            if progress is not None:
                progress.advance(pending_next)
        finally:
            body_budget.release(pending_reserved)

    batch: list[EntryRecord] = []
    reserved = 0

    def flush(next_entry: int) -> None:
        nonlocal batch, reserved
        rows, reservation = batch, reserved
        batch = []
        reserved = 0
        save_batch(rows, next_entry, reservation)

    for index in range(start, count):
        try:
            row = producer.entry_at(index)
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
                        body, body_truncated, used = compose_body(
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
                            if (
                                derived_refs is not None
                                and contribution.cache_key
                            ):
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
            if row is not None:
                if row.body and not fast:
                    size = len(row.body.encode("utf-8", "ignore"))
                    reservation = body_budget.try_reserve(size)
                    if reservation is None:
                        if batch:
                            flush(index)
                        while pending_embeddings:
                            drain_one()
                        reservation = body_budget.reserve(size)
                    reserved += reservation
                batch.append(row)
            if len(batch) >= batch_size:
                flush(index + 1)
        except BaseException:
            body_budget.release(reserved)
            reserved = 0
            for _future, _rows, _next, pending_reserved in pending_embeddings:
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
        for _future, _rows, _next, pending_reserved in pending_embeddings:
            body_budget.release(pending_reserved)


class _BuildProgress:
    """Report committed article work separately from cheap entries."""

    def __init__(self, producer, start: int):
        self._stack = ExitStack()
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

    def close(self) -> None:
        self._stack.close()


def _finish_build(
    state: _BuildState,
    *,
    producer: EntryProducer,
    fast: bool,
    model_fingerprint: str = "",
    model_probes: dict[str, object] | None = None,
    derived_text: Mapping[str, Any] | None = None,
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
            values.update(
                {str(key): str(value) for key, value in model_probes.items()}
            )
        if derived_text:
            values[index_schema.DERIVED_TEXT] = index_schema.encode_json(
                dict(derived_text)
            )
        index_schema.write_meta(db, values)
    db.execute("VACUUM")
    db.commit()


def _derived_baseline(
    cfg: Mapping[str, Any], producer: Any, *, fast: bool
) -> dict[str, Any]:
    operations = (cfg.get("derived_source_operations") or {}).get(
        getattr(producer, "key", ""), None
    )
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
    locator: Any,
    embedder,
    cfg: Mapping[str, Any] | None = None,
    *,
    fast: bool = False,
    force: bool = False,
) -> BuildReport:
    """Build and publish one complete immutable index generation.

    The producer supplies the only provider-specific behaviour.  The core owns
    staging, resume, embedding, vector publication, metadata, and publication.
    The returned :class:`BuildReport` describes what that source contributed
    without changing any existing caller contract.
    """
    started = time.monotonic()
    cfg = cfg or {}
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
    locator = _locator_for(locator)
    source = _source_for_locator(producer)
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
        derived_metadata = _derived_baseline(cfg, producer, fast=fast)
        derived_refs: dict[int, list[tuple[str, int, str]]] = {}
        derived_stage = cfg.get("derived_stage")
        if derived_stage is None and not fast and not completed_staging:
            operations = (cfg.get("derived_source_operations") or {}).get(
                getattr(producer, "key", ""), None
            )
            if operations is not None and getattr(
                operations, "any_enabled", False
            ):
                from .derived import (
                    DerivedTextCache,
                    DerivedTextStage,
                    parse_derived_text,
                )

                configured = cfg.get("derived_text")
                derived_config = (
                    configured
                    if hasattr(configured, "max_caption_bytes")
                    else parse_derived_text(configured)
                )
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
                order = prepare(recorded_order or ("id" if start else None))
                order_fingerprint = getattr(producer, "order_fingerprint", "")
                recorded_fingerprint = state.meta.get(index_schema.ENTRY_ORDER_FINGERPRINT)
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
            max_body_text_bytes = max(
                1,
                _cfg_int(
                    cfg, "max_body_text_bytes", DEFAULT_MAX_BODY_TEXT_BYTES
                ),
            )
            body_budget = cfg.get("body_budget")
            if not isinstance(body_budget, BodyBudget):
                body_budget = shared_body_budget(max_body_text_bytes)
            progress = _BuildProgress(producer, start)
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
            )
            progress.close()
            progress = None
            fingerprint = getattr(embedder, "fingerprint", "") if embedder else ""
            model_probes = None
            if not fast and embedder is not None:
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
                "source_size": _producer_attr(producer, "source_size", state.filesize),
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
            if completed:
                logger.warning(
                    "%s: partial enrichment published (%s); the available "
                    "channels remain searchable and a later build will retry",
                    source.key,
                    public_cause(error),
                )
                logger.debug("partial enrichment error for %s: %s", source.key, error)
                raise
            resumable = isinstance(error, KeyboardInterrupt)
            if not resumable:
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


def _fsync_path(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _embedding_batches(embedder, rows: list[EntryRecord], token_budget: int):
    """Bound padded tokens per call; sort actual capped inputs, not characters."""
    if embedder is None:
        return []
    articles = [row for row in rows if row.excerpt]
    if not articles:
        return []
    passages = []
    for row in articles:
        prefix = f"passage: {row.title}\n"
        passages.append(prefix + embedder.truncate(row.excerpt, prefix=prefix))
    if not callable(getattr(embedder, "encode", None)) or not callable(
        getattr(embedder, "embed_encoded", None)
    ):
        return [([row.id for row in articles], passages, False)]
    encoded = embedder.encode(passages)
    ordered = sorted(zip(articles, encoded), key=lambda pair: len(pair[1]))
    batches = []
    ids, inputs = [], []
    for row, tokens in ordered:
        # Padding, not just useful tokens, occupies the model workspace.
        if inputs and (len(inputs) + 1) * len(tokens) > token_budget:
            batches.append((ids, inputs, True))
            ids, inputs = [], []
        ids.append(row.id)
        inputs.append(tokens)
    if inputs:
        batches.append((ids, inputs, True))
    return batches


def _embed_rows(
    embedder, rows: list[EntryRecord],
    token_budget: int = DEFAULT_EMBEDDING_BATCH_TOKENS,
) -> dict[int, Any]:
    """Embed article rows and return vectors keyed by canonical row id."""
    result = {}
    for ids, inputs, encoded in _embedding_batches(
        embedder, rows, token_budget
    ):
        vectors = embedder.embed_encoded(inputs) if encoded else embedder.embed(inputs)
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
) -> int:
    """Store one ordered batch and its resume point in one transaction.

    Returns the number of records whose lexical body was truncated.  Contentless
    body FTS is append-only: a resumed committed row is never overwritten.
    """
    import numpy as np

    truncated = 0
    with db:
        db.executemany(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            [(row.id, row.title, row.path, row.target_id) for row in rows],
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
) -> int:
    """Embed and store a batch and the resume point in one transaction."""
    return _save_rows(
        db,
        rows,
        next_entry,
        _embed_rows(embedder, rows, token_budget),
        fast=fast,
        derived_refs=derived_refs,
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
        _fsync_path(temp_path)
        os.replace(temp_path, path)
        _fsync_path(path.parent)
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
    "BodyBudget",
    "BuildError",
    "EmbeddingPipeline",
    "InsufficientSpaceError",
    "PartialEnrichmentError",
    "SCHEMA",
    "SnapshotChangedError",
    "build_index",
    "shared_body_budget",
]
