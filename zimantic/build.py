"""Build a resumable SQLite and optional FAISS index for one ZIM."""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import logging
import os
from queue import Queue
import secrets
import sqlite3
from threading import Thread
from pathlib import Path
from typing import TYPE_CHECKING

from tqdm import tqdm
from libzim.reader import Archive
from .zim import (
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_PREVIEW_CHARS,
    DEFAULT_MAX_ENTRY_BYTES,
    EntryRecord,
    read_entry,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import numpy as np


SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    title, path UNINDEXED, target UNINDEXED,
    tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS vecs(id INTEGER PRIMARY KEY, v BLOB);
-- vecs stores float16 vectors and is dropped once .faiss is written.
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value);
-- meta.next is the resume entry; meta.done is 'fast' or 1.
"""

_FAISS_MIN_POINTS_PER_CENTROID = 39
_FAISS_MIN_TRAINING_POINTS = 100_000


@dataclass(slots=True)
class _EmbeddingRequest:
    rows: list
    future: Future


class EmbeddingPipeline:
    """Bounded embedding stage shared by concurrent ZIM builds."""

    def __init__(
        self,
        embedder,
        workers: int = 1,
        queue_size: int = 8,
        max_pending_per_build: int = 2,
    ):
        workers = int(workers)
        if workers < 1:
            raise ValueError("embedding workers must be at least 1")
        self.max_pending_per_build = max(1, int(max_pending_per_build))
        self._embedder = embedder
        self._requests: Queue[_EmbeddingRequest | None] = Queue(maxsize=max(1, int(queue_size)))
        self._threads = [
            Thread(target=self._run, name=f"zimantic-embed-{index}", daemon=True)
            for index in range(workers)
        ]
        for thread in self._threads:
            thread.start()
        self._closed = False

    def submit(self, rows: list) -> Future:
        if self._closed:
            raise RuntimeError("embedding pipeline is closed")
        future = Future()
        self._requests.put(_EmbeddingRequest(rows, future))
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
                request.future.set_result(_embed_rows(self._embedder, request.rows))
            except Exception as error:
                request.future.set_exception(error)


@dataclass
class _BuildState:
    db: sqlite3.Connection | None
    build_db_path: Path
    build_faiss_path: Path
    publish_upgrade: bool
    filesize: int
    meta: dict[str, str]


def _open_database(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def _source_filesize(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _prepare_build_state(
    zim_path: Path,
    db_path: Path,
    faiss_path: Path,
    fast: bool,
    force: bool,
) -> _BuildState | None:
    if force:
        _reset_index(db_path, faiss_path)

    filesize = _source_filesize(zim_path)
    db = _open_database(db_path)
    try:
        meta = dict(db.execute("SELECT key, value FROM meta"))
        done = str(meta.get("done", ""))
        # A ZIM replaced under the same name must not keep its old index: rebuild
        # when the source file's size no longer matches the recorded size.
        recorded_size = meta.get("filesize")
        if (
            done in {"1", "fast"}
            and recorded_size is not None
            and str(filesize) != recorded_size
        ):
            logger.warning(
                "%s: source ZIM changed size (%s -> %s bytes); rebuilding",
                zim_path.stem,
                recorded_size,
                filesize,
            )
            db.close()
            _reset_index(db_path, faiss_path)
            db = _open_database(db_path)
            meta = {}
            done = ""

        if done == "1":
            if faiss_path.exists():
                logger.info("%s: already built (use --force to rebuild)", zim_path.stem)
                db.close()
                return None
            raise RuntimeError(
                f"{zim_path.stem}: index is marked done but {faiss_path} is missing; "
                "use --force to rebuild"
            )

        publish_upgrade = False
        build_db_path = db_path
        build_faiss_path = faiss_path
        if done == "fast":
            if fast:
                logger.info(
                    f"{zim_path.stem}: already indexed for title/full-text search "
                    "(use --force to rebuild)"
                )
                db.close()
                return None
            logger.info(
                "%s: upgrading title-only index to full "
                "(re-reading entries beside current index)",
                zim_path.stem,
            )
            db.close()
            build_db_path, build_faiss_path = _upgrade_paths(db_path, faiss_path)
            db = _open_database(build_db_path)
            meta = dict(db.execute("SELECT key, value FROM meta"))
            staged_done = str(meta.get("done", ""))
            if staged_done == "1":
                db.close()
                _publish_upgrade(build_db_path, build_faiss_path, db_path, faiss_path)
                logger.debug("%s: full index ready", zim_path.stem)
                return None
            if staged_done == "fast":
                raise RuntimeError(
                    f"{zim_path.stem}: upgrade staging file is marked fast; "
                    "delete it and retry"
                )
            publish_upgrade = True

        return _BuildState(
            db=db,
            build_db_path=build_db_path,
            build_faiss_path=build_faiss_path,
            publish_upgrade=publish_upgrade,
            filesize=filesize,
            meta=meta,
        )
    except Exception:
        db.close()
        raise


def _index_entries(
    zim: Archive,
    zim_name: str,
    db: sqlite3.Connection,
    start: int,
    batch_size: int,
    embedder,
    fast: bool,
    max_html_bytes: int,
    max_entry_bytes: int,
    max_preview_chars: int,
    embedding_pipeline: EmbeddingPipeline | None,
) -> None:
    pending_embeddings = []

    def save_batch(rows: list[EntryRecord], next_entry: int) -> None:
        if embedding_pipeline is None:
            _save(db, embedder, rows, next_entry)
            return
        pending_embeddings.append(
            (embedding_pipeline.submit(rows), rows, next_entry)
        )
        if len(pending_embeddings) >= embedding_pipeline.max_pending_per_build:
            future, pending_rows, pending_next = pending_embeddings.pop(0)
            _save_rows(db, pending_rows, pending_next, future.result())

    batch: list[EntryRecord] = []
    for i in tqdm(
        range(start, zim.entry_count),
        initial=start,
        total=zim.entry_count,
        desc=zim_name,
        delay=5,
    ):
        row = read_entry(
            zim,
            i,
            fast=fast,
            max_html_bytes=max_html_bytes,
            max_entry_bytes=max_entry_bytes,
            max_preview_chars=max_preview_chars,
        )
        if row:
            batch.append(row)
        # Keep fast builds bounded too: they have no rows with text to embed,
        # so an article-count threshold would never flush.
        if len(batch) >= batch_size:
            save_batch(batch, i + 1)
            batch = []
    save_batch(batch, zim.entry_count)
    while pending_embeddings:
        future, pending_rows, pending_next = pending_embeddings.pop(0)
        _save_rows(db, pending_rows, pending_next, future.result())


def _finish_build(state: _BuildState, db_path: Path, faiss_path: Path, fast: bool) -> None:
    db = state.db
    if db is None:
        raise RuntimeError("build database is already closed")
    if fast:
        with db:
            db.execute("DROP TABLE IF EXISTS vecs")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('done', 'fast')")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('filesize', ?)", (str(state.filesize),))
        logger.debug(f"{db_path.stem}: title + full-text index ready")
        return

    _write_faiss(db, state.build_faiss_path)
    with db:
        db.execute("DROP TABLE vecs")
        db.execute("DELETE FROM meta WHERE key = 'next'")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('done', 1)")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('filesize', ?)", (str(state.filesize),))
    if state.publish_upgrade:
        db.close()
        state.db = None
        _publish_upgrade(
            state.build_db_path,
            state.build_faiss_path,
            db_path,
            faiss_path,
        )
    else:
        db.execute("VACUUM")


def _entry_records(rows) -> list[EntryRecord]:
    """Normalize legacy tuple rows at the build boundary."""
    return [
        row if isinstance(row, EntryRecord) else EntryRecord(*row)
        for row in rows
    ]


def build(
    zim_path: Path,
    index_dir,
    embedder,
    batch_size: int,
    fast: bool = False,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
    max_preview_chars: int = DEFAULT_PREVIEW_CHARS,
    force: bool = False,
    embedding_pipeline: EmbeddingPipeline | None = None,
) -> None:
    """Index a ZIM, optionally without article bodies or vectors."""
    db_path = Path(index_dir) / f"{zim_path.stem}.sqlite"
    faiss_path = db_path.with_suffix(".faiss")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    state = _prepare_build_state(zim_path, db_path, faiss_path, fast, force)
    if state is None:
        return
    try:
        zim = Archive(str(zim_path))
        if state.db is None:
            raise RuntimeError("build database is unavailable")
        _index_entries(
            zim,
            zim_path.stem,
            state.db,
            int(state.meta.get("next", 0)),
            batch_size,
            embedder,
            fast,
            max_html_bytes,
            max_entry_bytes,
            max_preview_chars,
            embedding_pipeline,
        )
        _finish_build(state, db_path, faiss_path, fast)
    finally:
        if state.db is not None:
            state.db.close()


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
    """Flush a file or directory to permanent storage, best-effort."""
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


def _upgrade_paths(db_path: Path, faiss_path: Path) -> tuple[Path, Path]:
    """Return staging paths used while replacing a valid fast index."""
    return (
        db_path.with_name(f"{db_path.name}.upgrade"),
        faiss_path.with_name(f"{faiss_path.name}.upgrade"),
    )


def _reset_index(db_path: Path, faiss_path: Path) -> None:
    """Remove index state before a forced rebuild."""
    staging_db_path, staging_faiss_path = _upgrade_paths(db_path, faiss_path)
    for path in (db_path, faiss_path, staging_db_path, staging_faiss_path):
        path.unlink(missing_ok=True)


def _publish_upgrade(
    staging_db_path: Path,
    staging_faiss_path: Path,
    db_path: Path,
    faiss_path: Path,
) -> None:
    """Publish a completed full index without exposing a partial replacement."""
    if staging_db_path.exists() and staging_faiss_path.exists():
        # Fast indexes ignore FAISS files, so publish vectors first and the
        # done=1 SQLite file last. Readers see either the old fast index or
        # the new one.
        os.replace(staging_faiss_path, faiss_path)
        os.replace(staging_db_path, db_path)
        _fsync_path(db_path.parent)
        return
    if staging_db_path.exists() and faiss_path.exists():
        # Recover if the process stopped after publishing FAISS but before
        # publishing SQLite.
        os.replace(staging_db_path, db_path)
        _fsync_path(db_path.parent)
        return
    if db_path.exists() and faiss_path.exists() and not staging_db_path.exists():
        return
    if not staging_db_path.exists() or not staging_faiss_path.exists():
        raise RuntimeError("full index upgrade is missing its completed SQLite or FAISS file")


def _embed_rows(embedder, rows: list[EntryRecord]) -> dict[int, np.ndarray]:
    """Embed article rows and return vectors keyed by entry id."""
    if embedder is None:
        return {}

    rows = _entry_records(rows)
    articles = [row for row in rows if row.excerpt]
    if not articles:
        return {}
    passages = []
    for row in articles:
        prefix = f"passage: {row.title}\n"
        passages.append(prefix + embedder.truncate(row.excerpt, prefix=prefix))
    vectors = embedder.embed(passages)
    return {row.id: vector for row, vector in zip(articles, vectors)}


def _save_rows(
    db,
    rows: list[EntryRecord],
    next_entry: int,
    vectors: dict[int, np.ndarray],
) -> None:
    """Store a batch, vectors and the resume point in one transaction.

    Rows are (id, title, excerpt, path, target). The excerpt is transient:
    it is used for embeddings but is not written to new indexes.
    """
    import numpy as np

    rows = _entry_records(rows)

    with db:
        db.executemany(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            [(row.id, row.title, row.path, row.target_id) for row in rows],
        )
        db.executemany(
            "INSERT INTO vecs VALUES (?, ?)",
            [
                (row.id, vectors[row.id].astype(np.float16).tobytes())
                for row in rows
                if row.id in vectors
            ],
        )
        db.execute("INSERT OR REPLACE INTO meta VALUES ('next', ?)", (next_entry,))


def _save(db, embedder, rows: list[EntryRecord], next_entry: int) -> None:
    """Embed and store a batch and the resume point in one transaction."""
    _save_rows(db, rows, next_entry, _embed_rows(embedder, rows))


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


def _write_faiss(db, path: Path) -> None:
    faiss = _load_faiss()

    n = db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
    dim = db.execute("SELECT length(v) / 2 FROM vecs LIMIT 1").fetchone()
    dim = dim[0] if dim else 384

    if n < 10_000: # IDMap, flat. Compares query with every vector. 
        # Small ZIM: exact search. Does not contain nprobe. 
        index = faiss.index_factory(dim, "IDMap,Flat", faiss.METRIC_INNER_PRODUCT)

    else: # ~4*sqrt(n) clusters (capped for small n) ALSO 1 byte per number (int-8)
        # Large ZIM: vectors grouped into lists, 1 byte per number (384 bytes per article). 
        # Squeezing to 48 bytes (PQ48) lost ~25% of top hits in testing; this
        # 8-bit form was nearly exact.
        # The file is memorymapped at search time, so only the lists a query
        # probes are read from disk.
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
        # Flush before rename so a partial file is never published.
        _fsync_path(temp_path)
        os.replace(temp_path, path)
        _fsync_path(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def _faiss_cluster_count(n: int) -> int:
    """Cluster count for the IVF index, capped so FAISS can train each centroid.

    FAISS warns (and under-trains the coarse quantizer) below 39 points per
    centroid, so for small indexes 4*sqrt(n) is more clusters than n can supply.
    """
    return max(1, min(int(4 * n**0.5), n // _FAISS_MIN_POINTS_PER_CENTROID))


def _faiss_training_step(n: int, cluster_centre_count: int) -> int:
    """Choose a sampling stride that gives FAISS enough training vectors."""
    training_points = min(
        n,
        max(_FAISS_MIN_TRAINING_POINTS, _FAISS_MIN_POINTS_PER_CENTROID * cluster_centre_count),
    )
    return max(1, n // training_points)
