from __future__ import annotations

import sqlite3
from contextlib import closing

from zimantic.indexing.locator import IndexLocator


def _query(
    locator: IndexLocator, source, sql: str, parameters: tuple = ()
) -> list[tuple]:
    lease = locator.paths(source)
    assert lease is not None
    try:
        with closing(sqlite3.connect(lease.sqlite_path)) as db:
            return db.execute(sql, parameters).fetchall()
    finally:
        lease.close()


def meta(locator: IndexLocator, source) -> dict[str, str]:
    return dict(_query(locator, source, "SELECT key, value FROM meta"))


def docs(locator: IndexLocator, source) -> list[tuple[int, str, str]]:
    return _query(
        locator, source, "SELECT rowid, title, path FROM docs ORDER BY rowid"
    )


def body_hits(locator: IndexLocator, source, term: str) -> list[tuple[int]]:
    return _query(
        locator,
        source,
        "SELECT rowid FROM body WHERE body MATCH ?",
        (f'"{term}"',),
    )
