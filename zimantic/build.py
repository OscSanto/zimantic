"""Build a resumable SQLite and optional FAISS index for one ZIM."""
import os
import secrets
import sqlite3
from pathlib import Path

import faiss
import numpy as np
from tqdm import tqdm
from libzim.reader import Archive
from .settings import DEFAULT_MAX_EMBEDDING_TOKENS
from .zim import (
    DEFAULT_EMBEDDING_OVERFLOW,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_PREVIEW_CHARS,
    DEFAULT_PREVIEW_OVERFLOW,
    read_entry,
)

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    title, excerpt UNINDEXED, path UNINDEXED, target UNINDEXED,
    tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS vecs(id INTEGER PRIMARY KEY, v BLOB);  -- float16, dropped once .faiss is written
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value);     -- next: entry to resume from; done: 'fast' | 1
DROP TABLE IF EXISTS disamb;
"""

_FAISS_MIN_POINTS_PER_CENTROID = 39
_FAISS_MIN_TRAINING_POINTS = 100_000


def build(
    zim_path: Path,
    index_dir,
    embedder,
    batch_size: int,
    fast: bool = False,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    max_preview_chars: int = DEFAULT_PREVIEW_CHARS,
    max_embedding_tokens: int = DEFAULT_MAX_EMBEDDING_TOKENS,
    preview_overflow: str = DEFAULT_PREVIEW_OVERFLOW,
    embedding_overflow: str = DEFAULT_EMBEDDING_OVERFLOW,
    force: bool = False,
) -> None:
    """Index a ZIM, optionally without article bodies or vectors."""
    db_path = Path(index_dir) / f"{zim_path.stem}.sqlite"
    faiss_path = db_path.with_suffix(".faiss")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    build_db_path = db_path
    build_faiss_path = faiss_path
    publish_upgrade = False
    db = None
    try:
        if force:
            _reset_index(db_path, faiss_path)
        try:
            filesize = zim_path.stat().st_size
        except OSError:
            filesize = 0
        db = sqlite3.connect(build_db_path)
        db.executescript(SCHEMA)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        done = str(meta.get("done", ""))
        # A ZIM replaced under the same name must not keep its old index: rebuild
        # when the source file's size no longer matches the size recorded when the
        # index was finished. Both fast and full indexes record it.
        recorded_size = meta.get("filesize")
        if (
            done in {"1", "fast"}
            and recorded_size is not None
            and str(filesize) != recorded_size
        ):
            print(
                f"{zim_path.stem}: source ZIM changed size "
                f"({recorded_size} -> {filesize} bytes); rebuilding"
            )
            db.close()
            db = None
            _reset_index(db_path, faiss_path)
            build_db_path = db_path
            build_faiss_path = faiss_path
            publish_upgrade = False
            db = sqlite3.connect(build_db_path)
            db.executescript(SCHEMA)
            meta = {}
            done = ""
        if done == "1":
            if faiss_path.exists():
                print(f"{zim_path.stem}: already built (use --force to rebuild)")
                return
            raise RuntimeError(
                f"{zim_path.stem}: index is marked done but {faiss_path} is missing; "
                "use --force to rebuild"
            )
        if done == "fast":
            if fast:
                print(f"{zim_path.stem}: already indexed for title/full-text search (use --force to rebuild)")
                return
            print(f"{zim_path.stem}: upgrading title-only index to full (re-reading entries beside current index)")
            db.close()
            db = None
            build_db_path, build_faiss_path = _upgrade_paths(db_path, faiss_path)
            db = sqlite3.connect(build_db_path)
            db.executescript(SCHEMA)
            meta = dict(db.execute("SELECT key, value FROM meta"))
            staged_done = str(meta.get("done", ""))
            if staged_done == "1":
                db.close()
                db = None
                _publish_upgrade(build_db_path, build_faiss_path, db_path, faiss_path)
                print(f"{zim_path.stem}: full index ready")
                return
            if staged_done == "fast":
                raise RuntimeError(f"{zim_path.stem}: upgrade staging file is marked fast; delete it and retry")
            publish_upgrade = True

        zim = Archive(str(zim_path))
        start = int(meta.get("next", 0))
        batch = []
        for i in tqdm(range(start, zim.entry_count), initial=start, total=zim.entry_count, desc=zim_path.stem):
            row = read_entry(
                zim,
                i,
                fast=fast,
                max_html_bytes=max_html_bytes,
                max_preview_chars=max_preview_chars,
                max_embedding_tokens=max_embedding_tokens,
                preview_overflow=preview_overflow,
                embedding_overflow=embedding_overflow,
                embedder=embedder,
            )
            if row:
                batch.append(row)
            # Keep fast builds bounded too: they have no rows with text to
            # embed, so an article-count threshold would never flush.
            if len(batch) >= batch_size:
                _save(db, embedder, batch, i + 1)
                batch = []
        _save(db, embedder, batch, zim.entry_count)

        if fast:
            with db:
                db.execute("DROP TABLE IF EXISTS vecs")
                db.execute("INSERT OR REPLACE INTO meta VALUES ('done', 'fast')")
                db.execute("INSERT OR REPLACE INTO meta VALUES ('filesize', ?)", (str(filesize),))
            print(f"{zim_path.stem}: title + full-text index ready")
            return

        _write_faiss(db, build_faiss_path)
        with db:
            db.execute("DROP TABLE vecs")
            db.execute("DELETE FROM meta WHERE key = 'next'")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('done', 1)")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('filesize', ?)", (str(filesize),))
        if publish_upgrade:
            db.close()
            db = None
            _publish_upgrade(build_db_path, build_faiss_path, db_path, faiss_path)
        else:
            db.execute("VACUUM")
    finally:
        if db is not None:
            db.close()


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


def _fsync_file(path: Path) -> None:
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


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
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
        _fsync_dir(db_path.parent)
        return
    if staging_db_path.exists() and faiss_path.exists():
        # Recover if the process stopped after publishing FAISS but before
        # publishing SQLite.
        os.replace(staging_db_path, db_path)
        _fsync_dir(db_path.parent)
        return
    if db_path.exists() and faiss_path.exists() and not staging_db_path.exists():
        return
    if not staging_db_path.exists() or not staging_faiss_path.exists():
        raise RuntimeError("full index upgrade is missing its completed SQLite or FAISS file")


def _save(db, embedder, rows, next_entry: int) -> None:
    """Store a batch and the resume point in one transaction.

    Rows are (id, title, excerpt, path, target).
    """
    article_ids = {row[0] for row in rows if row[2]}
    with db:
        db.executemany(
            "INSERT INTO docs(rowid, title, excerpt, path, target) VALUES (?, ?, ?, ?, ?)",
            [(row[0], row[1], row[2], row[3], row[4]) for row in rows],
        )
        articles = [row for row in rows if row[0] in article_ids]
        if articles and embedder is not None:
            passages = []
            for row in articles:
                prefix = f"passage: {row[1]}\n"
                passages.append(prefix + embedder.truncate(row[2], prefix=prefix))
            vectors = embedder.embed(passages)
            db.executemany("INSERT INTO vecs VALUES (?, ?)",
                           [(row[0], v.astype(np.float16).tobytes()) for row, v in zip(articles, vectors)])
        db.execute("INSERT OR REPLACE INTO meta VALUES ('next', ?)", (next_entry,))


def _load(rows):
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    vectors = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1)
    return ids, vectors.astype(np.float32)


def _write_faiss(db, path: Path) -> None:
    n = db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
    dim = db.execute("SELECT length(v) / 2 FROM vecs LIMIT 1").fetchone()
    dim = dim[0] if dim else 384

    if n < 10_000: # IDMap, flat. Compares query with every vector. 
        # Small ZIM: exact search. Does not contain nprobe. 
        index = faiss.index_factory(dim, "IDMap,Flat", faiss.METRIC_INNER_PRODUCT)

    else: # ~4*sqrt(n) clusters (capped for small n) ALSO 1 byte per number (int-8)
        # Large ZIM: vectors grouped into lists, 1 byte per number (384 bytes per article). 
        # Squeezing to 48 bytes (PQ48) lost ~25% of top hits in testing; this 8-bit form was nearly exact. 
        # The file is memorymapped at search time, so only the lists a query probes are read from disk.
        cluster_centre_count = _faiss_cluster_count(n)
        index = faiss.index_factory(dim, f"IVF{cluster_centre_count},SQ8", faiss.METRIC_INNER_PRODUCT)

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
    while rows := db.execute("SELECT id, v FROM vecs WHERE id > ? ORDER BY id LIMIT 50000", (last,)).fetchall():
        ids, vectors = _load(rows)
        index.add_with_ids(vectors, ids)
        last = int(ids[-1])
    temp_path = _create_temp_path(path)
    try:
        faiss.write_index(index, str(temp_path))
        # Flush before rename so a partial file is never published.
        _fsync_file(temp_path)
        os.replace(temp_path, path)
        _fsync_dir(path.parent)
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
