"""Index one ZIM into <index_dir>/<name>.sqlite (titles, leads) and <name>.faiss (vectors).

Rerunning an unfinished build resumes where it stopped. To rebuild, delete both files first.

How a build runs (each step is explained where it happens):
    1. read entries in the order the ZIM stores them (zim.cluster_order)
    2. collect a "window" of WINDOW articles, sort it by length, cut it into batches of batch_size
    3. worker threads embed the batches at the same time, sharing one model
    4. meanwhile the main thread already reads the next window
    5. write each window, with the point to resume from, in one transaction
"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import faiss
import numpy as np
from tqdm import tqdm
from libzim.reader import Archive
from .zim import cluster_order, read_entry

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    title, lead UNINDEXED, path UNINDEXED, target UNINDEXED, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS vecs(id INTEGER PRIMARY KEY, v BLOB);  -- float16, dropped once .faiss is written
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value);
-- meta keys: order = "cluster" or "id" (reading order); next = position in that order to resume from; done
"""

# Articles read per window. Big enough that sorting finds texts of similar length and every worker thread
# gets many batches (512 / 8 = 64), small enough to hold in memory anywhere (~0.5 MB of text).
WINDOW = 512


def build_settings(cfg: dict, hw) -> tuple[int, int, int]:
    """(embedding_workers, embed_threads, batch_size) from config.yaml, with "auto" filled in from the hardware.

    auto workers: one per hardware thread, minus one left for the main thread, which reads the ZIM.
    auto threads: 1 per call when several calls run at once; ONNX Runtime's default (all cores) for one call.
    """
    def setting(name, auto):
        value = cfg.get(name, "auto")
        if value in (None, "auto"):
            return auto
        if not isinstance(value, int) or value < 1:
            raise SystemExit(f"zimantic: {name} must be auto or a whole number of at least 1, not {value!r}")
        return value

    workers = setting("embedding_workers", max(1, hw.threads - 1))
    threads = setting("embed_threads", 1 if workers > 1 else 0)  # 0 = ONNX Runtime's default
    return workers, threads, setting("batch_size", 8)


def build(zim_path: Path, index_dir, embedder, batch_size: int, workers: int) -> None:
    db_path = Path(index_dir) / f"{zim_path.stem}.sqlite"
    faiss_path = db_path.with_suffix(".faiss")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    meta = dict(db.execute("SELECT key, value FROM meta"))
    if meta.get("done"):
        print(f"{zim_path.stem}: already built (delete its .sqlite and .faiss in index_dir to rebuild)")
        return

    zim = Archive(str(zim_path))
    start = int(meta.get("next", 0))
    # An unfinished build keeps the order it started with. Builds from before this setting existed read in id
    # order, and their "next" is an entry id, which is also their position in id order.
    order_name = meta.get("order") or ("id" if start else "cluster")
    order = cluster_order(zim_path, zim.entry_count) if order_name == "cluster" else None
    if order is None:
        order_name = "id"
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('order', ?)", (order_name,))

    progress = tqdm(total=zim.entry_count, initial=start, desc=zim_path.stem)
    for rows, vectors, next_position in embed_windows(zim, order, start, zim.entry_count, embedder,
                                                      batch_size, workers, progress):
        _save(db, rows, vectors, next_position)
    progress.close()

    print(f"{zim_path.stem}: writing vector index")
    _write_faiss(db, faiss_path)
    with db:
        db.execute("DROP TABLE vecs")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('done', 1)")
    db.execute("VACUUM")
    db.close()


def embed_windows(zim, order, start: int, stop: int, embedder, batch_size: int, workers: int, progress=None):
    """Read entries order[start:stop] and yield (rows, vectors, next_position) one window at a time, in order.

    order: entry ids in reading order (from cluster_order), or None for id order.
    rows: every indexable entry of the window, including redirects and title-only pages (no vector).
    vectors: {entry id: vector} for the rows that have a first paragraph.

    While the worker threads embed window k, this (main) thread already reads window k+1.
    Shared by build() and scripts/bench_embed.py, so the benchmark measures the real build.
    """
    def embed_window(pool, rows):
        articles = sorted((row for row in rows if row[2]), key=lambda row: len(row[2]))
        # Sorted by length, so every batch holds texts of about the same length. The model needs equal-length
        # inputs and pads short ones; the padding is computed and thrown away, so similar lengths waste little.
        batches = [articles[k:k + batch_size] for k in range(0, len(articles), batch_size)]
        futures = [pool.submit(embedder.embed, [f"passage: {title}\n{lead}" for _, title, lead, _, _ in batch])
                   for batch in batches]
        return rows, batches, futures

    def finished(window, next_position):
        rows, batches, futures = window
        vectors = {}
        for batch, future in zip(batches, futures):  # waits until the worker threads are done with this window
            vectors.update((row[0], v) for row, v in zip(batch, future.result()))
        return rows, vectors, next_position

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="embed") as pool:
        pending = None      # (window being embedded, its resume position)
        rows, articles = [], 0
        for position in range(start, stop):
            i = int(order[position]) if order is not None else position
            try:
                row = read_entry(zim, i)
            except Exception as e:  # one corrupt entry shouldn't stop a days-long build
                tqdm.write(f"skipping entry {i}: {e}")
                row = None
            if row:
                rows.append(row)
                articles += bool(row[2])
            if progress is not None:
                progress.update()
            if articles >= WINDOW:
                if pending:
                    yield finished(*pending)  # wait for window k-1 and hand it over to be written...
                pending = embed_window(pool, rows), position + 1  # ...start embedding window k, read on
                rows, articles = [], 0
        if pending:
            yield finished(*pending)
        yield finished(embed_window(pool, rows), stop)


def _save(db, rows, vectors: dict, next_position: int) -> None:
    """Store a window, its vectors and the resume point in one transaction."""
    with db:
        db.executemany("INSERT INTO docs(rowid, title, lead, path, target) VALUES (?, ?, ?, ?, ?)", rows)
        db.executemany("INSERT INTO vecs VALUES (?, ?)",
                       [(i, v.astype(np.float16).tobytes()) for i, v in vectors.items()])
        db.execute("INSERT OR REPLACE INTO meta VALUES ('next', ?)", (next_position,))


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
    faiss.write_index(index, str(path))
