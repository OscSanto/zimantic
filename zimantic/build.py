"""Index one ZIM into <index_dir>/<name>.sqlite (titles, leads) and <name>.faiss (vectors).

Rerunning an unfinished build resumes where it stopped. To rebuild, delete both files first.

How a build runs (each step is explained where it happens):
    1. read entries in the order the ZIM stores them (zim.storage_order)
    2. collect a "window" of WINDOW articles, sort it by token count, cut it into batches of about
       embedding_batch_tokens tokens each
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
from .zim import read_entry, storage_order

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    title, lead UNINDEXED, path UNINDEXED, target UNINDEXED, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS vecs(id INTEGER PRIMARY KEY, v BLOB);  -- float16, dropped once .faiss is written
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value);
-- meta keys: order = "storage" or "id" (reading order); next = position in that order to resume from; done
"""

# Articles read per window. Big enough that sorting finds texts of similar length and every worker thread
# gets many batches (~60 batches of 1,000 tokens for Wikipedia first paragraphs), small enough to hold in
# memory anywhere (~0.5 MB of text).
WINDOW = 512
# A window also ends after this many rows of any kind. Storage order reads redirects last, and they have no
# text, so without this cap the article count never reaches WINDOW there: every redirect would pile up in
# memory (~650k rows on wikipedia_en_top) and no resume point would be saved until the very end.
MAX_WINDOW_ROWS = 8192


# Peak RAM measured during builds (Windows): ~300 MB before any embedding (model, tokenizer, ZIM open), then
# per worker thread ~25-45 MB with 8 texts per call (2026-11-07), ~15-19 MB with 1,000-token calls (2026-11-28).
BASE_RAM = 300 << 20
RAM_PER_WORKER_PER_TOKEN = 24 << 10  # x embedding_batch_tokens: 24 MB per worker at 1,000, on the cautious side
RAM_RESERVE = 128 << 20             # left for the OS and a search server running on the same machine
MB = 1 << 20


def build_settings(cfg: dict, hw) -> tuple[int, int, int, str]:
    """(embedding_workers, embed_threads, batch_tokens, note) from config.yaml, with "auto" filled in.

    auto workers: one per hardware thread, minus one left for the main thread (it reads the ZIM),
                  and no more than fit in the RAM free right now.
    auto threads: 1 per call when several calls run at once; ONNX Runtime's default (all cores) for one call.
    note: why the worker count is what it is, printed at build start.
    """
    def setting(name, auto):
        value = cfg.get(name, "auto")
        if value in (None, "auto"):
            return auto
        if not isinstance(value, int) or value < 1:
            raise SystemExit(f"zimantic: {name} must be auto or a whole number of at least 1, not {value!r}")
        return value

    batch_tokens = setting("embedding_batch_tokens", 1000)
    per_worker = RAM_PER_WORKER_PER_TOKEN * batch_tokens
    spare = hw.ram_available - BASE_RAM - RAM_RESERVE
    fit = max(1, spare // per_worker)
    by_threads = max(1, hw.threads - 1)
    workers = setting("embedding_workers", min(by_threads, fit))

    if cfg.get("embedding_workers", "auto") not in (None, "auto"):
        note = "set in config.yaml"
    elif fit < by_threads:
        note = f"limited by free RAM ({hw.ram_available // MB} MB free, ~{per_worker // MB} MB per worker)"
    else:
        note = "one per hardware thread, minus one for reading"
    if spare < per_worker:
        note += (f". WARNING: only {hw.ram_available // MB} MB free, about {(BASE_RAM + RAM_RESERVE + per_worker) // MB}"
                 " MB is needed; the build may run out of memory. Close other programs or build on another machine")
    if cfg.get("batch_size") is not None:
        note += ". batch_size in config.yaml is no longer used: embedding_batch_tokens replaced it"
    threads = setting("embed_threads", 1 if workers > 1 else 0)  # 0 = ONNX Runtime's default
    return workers, threads, batch_tokens, note


def build(zim_path: Path, index_dir, embedder, batch_tokens: int, workers: int) -> None:
    db_path = Path(index_dir) / f"{zim_path.stem}.sqlite"
    faiss_path = db_path.with_suffix(".faiss")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Wait up to 60 s for the lock: with sqlite's default 5 s, anything reading the index during a build
    # (a search server, a script) could crash the build with "database is locked".
    db = sqlite3.connect(db_path, timeout=60)
    db.executescript(SCHEMA)
    meta = dict(db.execute("SELECT key, value FROM meta"))
    if meta.get("done"):
        print(f"{zim_path.stem}: already built (delete its .sqlite and .faiss in index_dir to rebuild)")
        return

    zim = Archive(str(zim_path))
    start = int(meta.get("next", 0))
    # An unfinished build keeps the order it started with. Builds from before this setting existed read in id
    # order, and their "next" is an entry id, which is also their position in id order.
    order_name = meta.get("order") or ("id" if start else "storage")
    if order_name not in ("storage", "id"):  # e.g. "cluster": zimantic's own storage order before libzim 4.1
        raise SystemExit(f"zimantic: {zim_path.stem} was started in a reading order this version can't resume. "
                         f"Delete {db_path} and build again")
    order = storage_order(zim) if order_name == "storage" else None
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('order', ?)", (order_name,))

    progress = tqdm(total=zim.entry_count, initial=start, desc=zim_path.stem)
    for rows, vectors, next_position in embed_windows(zim, order, start, zim.entry_count, embedder,
                                                      batch_tokens, workers, progress):
        _save(db, rows, vectors, next_position)
    progress.close()

    print(f"{zim_path.stem}: writing vector index")
    _write_faiss(db, faiss_path)
    with db:
        db.execute("DROP TABLE vecs")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('done', 1)")
    db.execute("VACUUM")
    db.close()
    release_memory()


def release_memory() -> None:
    """Hand memory freed after a build back to the operating system (Linux with glibc only).

    glibc keeps freed memory reserved for this process in case it is needed again, so after a build the
    process can look hundreds of MB bigger than it is. On a Raspberry Pi that is RAM a search server or the
    next ZIM's build can't use. malloc_trim(0) returns it. Windows and macOS give memory back on their own;
    other C libraries (e.g. musl on Alpine Linux) don't have malloc_trim, so this does nothing there.
    """
    import ctypes
    import ctypes.util
    import gc
    gc.collect()
    try:
        trim = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6").malloc_trim
    except (OSError, AttributeError, TypeError):
        return
    trim(0)


def embed_windows(zim, order, start: int, stop: int, embedder, batch_tokens: int, workers: int, progress=None):
    """Read entries order[start:stop] and yield (rows, vectors, next_position) one window at a time, in order.

    order: entry ids in reading order (from storage_order), or None for id order.
    rows: every indexable entry of the window, including redirects and title-only pages (no vector).
    vectors: {entry id: vector} for the rows that have a first paragraph.

    While the worker threads embed window k, this (main) thread already reads window k+1.
    Shared by build() and scripts/bench_embed.py, so the benchmark measures the real build.
    """
    def embed_window(pool, rows):
        # Sorted by token count, so every batch holds texts of about the same length. The model needs
        # equal-length inputs and pads short ones; the padding is computed and thrown away, so similar lengths
        # waste little. Token count, not characters: characters per token differ by language (about 4 in
        # English, 3 in Arabic or Hindi, 1.3 in Chinese), and a character sort still left 14-18% padding.
        # Tokenizing here, on the reading thread, costs little next to embedding the window.
        articles = sorted(((row, embedder.encode(f"passage: {row[1]}\n{row[2]}")) for row in rows if row[2]),
                          key=lambda article: len(article[1]))
        # Each batch gets as many texts as fit in batch_tokens, counting padding: a call's work and working
        # memory grow with texts x longest text, so ~30 short texts or ~4 long ones. A fixed number of texts
        # made long batches several times heavier than short ones: threads finished unevenly, and peak RAM
        # was set by the heaviest batches running at once. A single text longer than the budget runs alone.
        batches, batch = [], []
        for article in articles:  # sorted, so this text is the longest in the batch so far
            if batch and (len(batch) + 1) * len(article[1]) > batch_tokens:
                batches.append(batch)
                batch = []
            batch.append(article)
        if batch:
            batches.append(batch)
        futures = [pool.submit(embedder.embed_encoded, [ids for _, ids in batch]) for batch in batches]
        return rows, [[row for row, _ in batch] for batch in batches], futures

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
            if articles >= WINDOW or len(rows) >= MAX_WINDOW_ROWS:
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
