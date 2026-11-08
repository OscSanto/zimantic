#!/usr/bin/env python3
"""Compare compact contentless and stored-content body FTS on a synthetic corpus.

This is the phase-04 local benchmark hook.  It deliberately uses only SQLite so
it can run on a build host without libzim, FAISS, or a model.  The phase-09
release gate repeats the comparison on representative ZIM, Files, and Kolibri
fixtures.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import tempfile
import time
from pathlib import Path

WORDS = (
    "library article knowledge search index vector lexical body caption "
    "image audio video archive reader source collection document"
).split()


def _rows(count: int, words_per_doc: int) -> list[str]:
    random.seed(7)
    return [
        " ".join(random.choice(WORDS) for _ in range(words_per_doc))
        for _ in range(count)
    ]


def _database(path: Path, rows: list[str], *, stored: bool) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    if stored:
        db.execute(
            "CREATE VIRTUAL TABLE body USING fts5("
            "text, tokenize='unicode61 remove_diacritics 2')"
        )
    else:
        db.execute(
            "CREATE VIRTUAL TABLE body USING fts5("
            "text, content='', detail=none, columnsize=0, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
    with db:
        db.executemany(
            "INSERT INTO body(rowid, text) VALUES (?, ?)",
            enumerate(rows, 1),
        )
    return db


def _query_time(db: sqlite3.Connection, rows: list[str], limit: int) -> float:
    queries = [
        " ".join(random.choice(WORDS) for _ in range(2)) for _ in range(limit)
    ]
    started = time.perf_counter()
    for query in queries:
        expression = " AND ".join(f'"{word}"' for word in query.split())
        db.execute("SELECT rowid FROM body WHERE body MATCH ? LIMIT 10", (expression,)).fetchall()
    return (time.perf_counter() - started) / max(1, len(queries))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--words-per-doc", type=int, default=200)
    parser.add_argument("--queries", type=int, default=200)
    args = parser.parse_args()
    rows = _rows(args.rows, args.words_per_doc)
    with tempfile.TemporaryDirectory() as tmp:
        for label, stored in (("contentless", False), ("stored-content", True)):
            path = Path(tmp) / f"{label}.sqlite"
            db = _database(path, rows, stored=stored)
            try:
                per_query = _query_time(db, rows, args.queries)
            finally:
                db.close()
            print(
                f"{label:15s} {path.stat().st_size:>10d} bytes  "
                f"{per_query * 1000:.4f} ms/query"
            )


if __name__ == "__main__":
    main()
