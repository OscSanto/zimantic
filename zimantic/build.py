"""Index one ZIM into <index_dir>/<name>.sqlite (titles, leads) and, unless
fast, <name>.faiss (vectors).

Rerunning an unfinished build resumes where it stopped. A fast build marks the
index done='fast' and can later be upgraded in place by a normal build.
To rebuild from scratch, delete both files first.
"""
import os
import sqlite3
import tempfile
from pathlib import Path

import faiss
import numpy as np
from tqdm import tqdm
from libzim.reader import Archive
from .zim import read_entry

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    title, lead UNINDEXED, path UNINDEXED, target UNINDEXED, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS vecs(id INTEGER PRIMARY KEY, v BLOB);  -- float16, dropped once .faiss is written
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value);     -- next: entry to resume from; done: 'fast' | 1
"""


def build(zim_path: Path, index_dir, embedder, batch_size: int, fast: bool = False) -> None:
    """Index a ZIM.

    fast=True builds title + ZIM full-text search only: it never reads article
    bodies or runs the embedding model, so it is much quicker. A later full
    build upgrades the same index in place.
    """
    db_path = Path(index_dir) / f"{zim_path.stem}.sqlite"
    faiss_path = db_path.with_suffix(".faiss")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        done = str(meta.get("done", ""))
        if done == "1":
            if faiss_path.exists():
                print(f"{zim_path.stem}: already built (delete its .sqlite and .faiss in index_dir to rebuild)")
                return
            raise RuntimeError(
                f"{zim_path.stem}: index is marked done but {faiss_path} is missing; "
                "delete both index files and rebuild"
            )
        if done == "fast":
            if fast:
                print(f"{zim_path.stem}: already indexed for title/full-text search")
                return
            print(f"{zim_path.stem}: upgrading title-only index to full (re-reading entries)")
            with db:
                db.execute("DELETE FROM docs")
                db.execute("DELETE FROM vecs")
                db.execute("DELETE FROM meta")
            meta = {}

        zim = Archive(str(zim_path))
        start = int(meta.get("next", 0))
        batch, articles = [], 0  # articles: rows in the batch that have a first paragraph to embed
        for i in tqdm(range(start, zim.entry_count), initial=start, total=zim.entry_count, desc=zim_path.stem):
            row = read_entry(zim, i, fast=fast)
            if row:
                batch.append(row)
                articles += bool(row[2])
            if articles >= batch_size:
                _save(db, embedder, batch, i + 1)
                batch, articles = [], 0
        _save(db, embedder, batch, zim.entry_count)

        if fast:
            with db:
                db.execute("DROP TABLE IF EXISTS vecs")
                db.execute("INSERT OR REPLACE INTO meta VALUES ('done', 'fast')")
            print(f"{zim_path.stem}: title + full-text index ready")
            return

        print(f"{zim_path.stem}: writing vector index")
        _write_faiss(db, faiss_path)
        with db:
            db.execute("DROP TABLE vecs")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('done', 1)")
        db.execute("VACUUM")
    finally:
        db.close()


def _save(db, embedder, rows, next_entry: int) -> None:
    """Store a batch and the resume point in one transaction."""
    with db:
        db.executemany("INSERT INTO docs(rowid, title, lead, path, target) VALUES (?, ?, ?, ?, ?)", rows)
        articles = [row for row in rows if row[2]]  # only pages with a first paragraph get a vector
        if articles and embedder is not None:
            vectors = embedder.embed([f"passage: {title}\n{lead}" for _, title, lead, _, _ in articles])
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

    else: # cluster_centre_count = 4 *sqrt(n) clusters AlSO 1 byte per number (int-8)
        # Large ZIM: vectors grouped into lists, 1 byte per number (384 bytes per article). 
        # Squeezing to 48 bytes (PQ48) lost ~25% of top hits in testing; this 8-bit form was nearly exact. 
        # The file is memorymapped at search time, so only the lists a query probes are read from disk.
        # TODO dynamic nprobe. Currenlty nprobe = 64 (check .yaml). On large 6M zim -> only 0.6% vector comparison due to large cluster count.
        cluster_centre_count = int(4 * n ** 0.5) # TODO: dyanmically size in relation to zim's vec count 
        index = faiss.index_factory(dim, f"IVF{cluster_centre_count},SQ8", faiss.METRIC_INNER_PRODUCT) #IVF{cluster_centre_count}
       
        step = max(1, n // 100_000)  # train on ~100k vectors spread across the ZIM
        index.train(_load(db.execute("SELECT id, v FROM vecs WHERE id % ? = 0", (step,)).fetchall())[1])
    last = -1
    while rows := db.execute("SELECT id, v FROM vecs WHERE id > ? ORDER BY id LIMIT 50000", (last,)).fetchall():
        ids, vectors = _load(rows)
        index.add_with_ids(vectors, ids)
        last = int(ids[-1])
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as temp:
        temp_path = Path(temp.name)
    try:
        faiss.write_index(index, str(temp_path))
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
