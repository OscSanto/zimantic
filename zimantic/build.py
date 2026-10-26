"""Build a resumable SQLite and optional FAISS index for one ZIM."""
from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import TYPE_CHECKING

from libzim.reader import Archive
from tqdm import tqdm

from .catalog import ZimCatalog, catalog_path, is_zim_filename, source_index_name
from .content.extract import DEFAULT_MAX_FILE_TEXT_CHARS, ExtractOptions
from .content.files import FileArchive, is_supported_file
from .content.reader import (
    DEFAULT_MAX_ENTRY_BYTES,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_PREVIEW_CHARS,
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

# Ordinary-file indexes have no libzim native full-text index, so the SQLite
# FTS table carries the extracted body as well as the title. ZIM indexes keep
# the compact title-only layout above.
FILE_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    title, body, path UNINDEXED, target UNINDEXED,
    tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS vecs(id INTEGER PRIMARY KEY, v BLOB);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value);
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


def _open_database(path: Path, schema: str = SCHEMA) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(schema)
    return db


def _source_filesize(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _prepare_build_state(
    source_path: Path,
    db_path: Path,
    faiss_path: Path,
    fast: bool,
    force: bool,
    schema: str = SCHEMA,
    kind: str = "zim",
) -> _BuildState | None:
    if force:
        _reset_index(db_path, faiss_path)

    filesize = _source_filesize(source_path)
    db = _open_database(db_path, schema)
    try:
        meta = dict(db.execute("SELECT key, value FROM meta"))
        done = str(meta.get("done", ""))
        # A build interrupted before meta.filesize was written still needs to
        # notice a source that changed underneath it. build_filesize is written
        # as soon as the build starts, so resuming cannot mix rows from two
        # different versions of the source.
        recorded_build_size = meta.get("build_filesize")
        if (
            done not in {"1", "fast"}
            and recorded_build_size is not None
            and str(filesize) != recorded_build_size
        ):
            logger.warning(
                "%s: source changed size during an interrupted build "
                "(%s -> %s bytes); restarting",
                source_index_name(source_path),
                recorded_build_size,
                filesize,
            )
            db.close()
            _reset_index(db_path, faiss_path)
            db = _open_database(db_path, schema)
            meta = {}
            done = ""
        # A source replaced under the same name must not keep its old index:
        # rebuild when the source file's size no longer matches the recorded
        # size. The same applies when a ZIM and an ordinary file collide on an
        # index identity; their docs layouts differ. An interrupted build may
        # not have written meta.kind yet, so inspect the actual columns too.
        recorded_size = meta.get("filesize")
        kind_changed = done in {"1", "fast"} and str(
            meta.get("kind", "zim")
        ) != kind
        docs_columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(docs)")
        }
        schema_changed = (
            bool(docs_columns)
            and (("body" in docs_columns) != (kind == "file"))
        ) or (done in {"1", "fast"} and not docs_columns)
        size_changed = (
            done in {"1", "fast"}
            and recorded_size is not None
            and str(filesize) != recorded_size
        )
        if kind_changed or schema_changed or size_changed:
            if kind_changed:
                logger.warning(
                    "%s: source kind changed (%s -> %s); rebuilding",
                    source_index_name(source_path),
                    meta.get("kind", "zim"),
                    kind,
                )
            elif schema_changed:
                logger.warning(
                    "%s: index layout changed; rebuilding",
                    source_index_name(source_path),
                )
            else:
                logger.warning(
                    "%s: source changed size (%s -> %s bytes); rebuilding",
                    source_index_name(source_path),
                    recorded_size,
                    filesize,
                )
            db.close()
            _reset_index(db_path, faiss_path)
            db = _open_database(db_path, schema)
            meta = {}
            done = ""

        if done == "1":
            if faiss_path.exists():
                logger.info(
                    "%s: already built (use --force to rebuild)",
                    source_index_name(source_path),
                )
                db.close()
                return None
            raise RuntimeError(
                f"{source_index_name(source_path)}: index is marked done but "
                f"{faiss_path} is missing; use --force to rebuild"
            )

        publish_upgrade = False
        build_db_path = db_path
        build_faiss_path = faiss_path
        if done == "fast":
            if fast:
                logger.info(
                    "%s: already indexed for title/full-text search "
                    "(use --force to rebuild)",
                    source_index_name(source_path),
                )
                db.close()
                return None
            logger.info(
                "%s: upgrading title-only index to full "
                "(re-reading entries beside current index)",
                source_index_name(source_path),
            )
            db.close()
            build_db_path, build_faiss_path = _upgrade_paths(db_path, faiss_path)
            db = _open_database(build_db_path, schema)
            meta = dict(db.execute("SELECT key, value FROM meta"))
            staged_done = str(meta.get("done", ""))
            if staged_done == "1":
                db.close()
                _publish_upgrade(build_db_path, build_faiss_path, db_path, faiss_path)
                logger.debug("%s: full index ready", source_index_name(source_path))
                return None
            if staged_done == "fast":
                raise RuntimeError(
                    f"{source_index_name(source_path)}: upgrade staging file is "
                    "marked fast; delete it and retry"
                )
            publish_upgrade = True

        if publish_upgrade:
            staged_build_size = meta.get("build_filesize")
            if (
                staged_build_size is not None
                and str(filesize) != staged_build_size
            ):
                logger.warning(
                    "%s: upgrade staging file was built from a different source "
                    "size; restarting the upgrade",
                    source_index_name(source_path),
                )
                db.close()
                build_db_path.unlink(missing_ok=True)
                build_faiss_path.unlink(missing_ok=True)
                db = _open_database(build_db_path, schema)
                meta = {}

        with db:
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('build_filesize', ?)",
                (str(filesize),),
            )
        meta["build_filesize"] = str(filesize)

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


def _index_file_source(
    source: FileArchive,
    db: sqlite3.Connection,
    start: int,
    batch_size: int,
    embedder,
    fast: bool,
    max_preview_chars: int,
    embedding_pipeline: EmbeddingPipeline | None,
) -> None:
    """Index every section a normal file exposes, including its FTS body.

    A PDF page, EPUB chapter or subtitle chunk is a separate entry, so the
    index stores one row and (for a full build) one vector per section. The
    batch loop resumes at the last committed entry id.
    """
    batch_size = max(1, int(batch_size))
    rows: list[EntryRecord] = []
    bodies: dict[int, str] = {}

    def flush(next_entry: int) -> None:
        nonlocal rows, bodies
        if not rows:
            return
        if embedding_pipeline is not None and not fast:
            _save_rows(
                db,
                rows,
                next_entry,
                embedding_pipeline.submit(rows).result(),
                bodies=bodies,
            )
        elif fast:
            # Fast builds never keep vectors. Skip the embedder entirely so a
            # caller that supplies one (the CLI does not) cannot pay for work
            # that _finish_build immediately throws away.
            _save_rows(db, rows, next_entry, {}, bodies=bodies)
        else:
            _save(db, embedder, rows, next_entry, bodies=bodies)
        rows = []
        bodies = {}

    for i in range(max(0, start), source.entry_count):
        entry = source._get_entry_by_id(i)
        body = entry.body.strip()
        if body:
            rows.append(
                EntryRecord(
                    i,
                    entry.title,
                    entry.body[:max(1, int(max_preview_chars))],
                    entry.path,
                    None,
                )
            )
            bodies[i] = entry.body
        if len(rows) >= batch_size:
            flush(i + 1)
    flush(source.entry_count)


def _finish_build(
    state: _BuildState,
    db_path: Path,
    faiss_path: Path,
    fast: bool,
    kind: str = "zim",
    metadata: dict[str, str] | None = None,
) -> None:
    db = state.db
    if db is None:
        raise RuntimeError("build database is already closed")
    extra = [("kind", kind)] if kind != "zim" else []
    for key, value in (metadata or {}).items():
        if value is not None:
            extra.append((key, str(value)))
    if fast:
        with db:
            db.execute("DROP TABLE IF EXISTS vecs")
            for key, value in extra:
                db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            db.execute("DELETE FROM meta WHERE key = 'build_filesize'")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('done', 'fast')")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('filesize', ?)", (str(state.filesize),))
        logger.debug(f"{db_path.stem}: title + full-text index ready")
        return

    _write_faiss(db, state.build_faiss_path)
    with db:
        db.execute("DROP TABLE vecs")
        db.execute("DELETE FROM meta WHERE key = 'next'")
        for key, value in extra:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        db.execute("DELETE FROM meta WHERE key = 'build_filesize'")
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


def build(
    source_path: Path,
    index_dir,
    embedder,
    batch_size: int,
    fast: bool = False,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
    max_preview_chars: int = DEFAULT_PREVIEW_CHARS,
    force: bool = False,
    embedding_pipeline: EmbeddingPipeline | None = None,
    kind: str | None = None,
    extract_options: ExtractOptions | None = None,
    index_name: str | None = None,
) -> None:
    """Index a ZIM or ordinary file, optionally without bodies or vectors.

    ``index_name`` overrides the flat index identity; the CLI uses it to give
    same-named ordinary files distinct indexes without changing their source
    paths or titles.
    """
    source_path = Path(source_path)
    if kind is None:
        kind = "zim" if is_zim_filename(source_path.name) else "file"
    if kind not in {"zim", "file"}:
        raise ValueError(f"unknown source kind: {kind}")
    if extract_options is None:
        extract_options = ExtractOptions(
            max_text_chars=DEFAULT_MAX_FILE_TEXT_CHARS
        )
    if kind == "file" and not is_supported_file(source_path):
        logger.warning(
            "%s: no reader for %s files; skipping",
            source_path.name,
            source_path.suffix or "(no extension)",
        )
        return

    name = index_name or source_index_name(source_path)
    db_path = Path(index_dir) / f"{name}.sqlite"
    faiss_path = db_path.with_suffix(".faiss")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    state = _prepare_build_state(
        source_path,
        db_path,
        faiss_path,
        fast,
        force,
        schema=FILE_SCHEMA if kind == "file" else SCHEMA,
        kind=kind,
    )
    if state is None:
        return
    try:
        if kind == "file":
            source = FileArchive(source_path, options=extract_options)
            if source.entry_count == 0:
                logger.warning(
                    "%s: no indexable text found; skipping",
                    source_path.name,
                )
                if state.db is not None:
                    state.db.close()
                    state.db = None
                _reset_index(db_path, faiss_path)
                return
            if state.db is None:
                raise RuntimeError("build database is unavailable")
            _index_file_source(
                source,
                state.db,
                int(state.meta.get("next", 0)),
                batch_size,
                embedder,
                fast,
                max_preview_chars,
                embedding_pipeline,
            )
            _finish_build(
                state,
                db_path,
                faiss_path,
                fast,
                kind=kind,
                metadata={
                    "source_path": str(source_path.resolve()),
                    "source_title": source.title,
                    "source_description": source.description,
                    "source_language": source.language,
                    "source_creator": source.creator,
                    "source_article_count": str(source.entry_count),
                },
            )
            return

        zim = Archive(source_path)
        # The archive is already open: store its catalog metadata now, so serve
        # can list sources without reopening every ZIM at startup.
        try:
            with ZimCatalog(catalog_path(index_dir)) as catalog:
                catalog.upsert_from_archive(zim, source_path)
        except Exception as error:
            logger.warning(
                "catalog metadata update failed for %s (%s)", source_path, error
            )
        if state.db is None:
            raise RuntimeError("build database is unavailable")
        _index_entries(
            zim,
            name,
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
        _finish_build(state, db_path, faiss_path, fast, kind=kind)
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
    bodies: dict[int, str] | None = None,
) -> None:
    """Store a batch, vectors and the resume point in one transaction.

    EntryRecord excerpts are transient: they are used for embeddings but are
    not written to new indexes. File indexes additionally store the extracted
    body so SQLite FTS can provide the full-text retrieval that a ZIM gets from
    libzim.
    """
    import numpy as np

    with db:
        if bodies is not None:
            db.executemany(
                "INSERT INTO docs(rowid, title, body, path, target) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (row.id, row.title, bodies.get(row.id, ""), row.path, row.target_id)
                    for row in rows
                ],
            )
        else:
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


def _save(
    db,
    embedder,
    rows: list[EntryRecord],
    next_entry: int,
    bodies: dict[int, str] | None = None,
) -> None:
    """Embed and store a batch and the resume point in one transaction."""
    _save_rows(
        db,
        rows,
        next_entry,
        _embed_rows(embedder, rows),
        bodies=bodies,
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


def _write_faiss(db, path: Path) -> None:
    faiss = _load_faiss()

    n = db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
    dim = db.execute("SELECT length(v) / 2 FROM vecs LIMIT 1").fetchone()
    dim = dim[0] if dim else 384

    if n < 10_000:  # flat: exact comparison against every vector
        # Small ZIM: exact search; no nprobe.
        index = faiss.index_factory(dim, "IDMap,Flat", faiss.METRIC_INNER_PRODUCT)

    else:  # IVF with ~4*sqrt(n) clusters and one byte per dimension
        # Large ZIM: vectors are grouped into lists and quantized to 8 bits
        # (384 bytes per 384-dimension article). PQ48 lost ~25% of top hits in
        # testing; this form was nearly exact. The file is memory-mapped at
        # search time, so a query reads only the lists it probes.
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
