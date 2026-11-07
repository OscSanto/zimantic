"""Provider-neutral persistent source metadata catalog.

``catalog.sqlite`` sits beside ``index_dir`` and stores one row per discovered
source in the generic ``sources`` table.  It is a disposable cache: providers
own extraction and validation, while this module owns only the SQLite schema,
coordinated rebuild, and cross-process lease.

ZIM is the only provider that writes this catalog.  Files and Kolibri use their
own manifests/databases, and Kiwix uses remote/in-memory metadata, so nothing
here may contain ZIM-specific columns, tables, or scan behavior.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "catalog.sqlite"

# Bump when the table layout or the metadata derivation changes. The catalog is
# disposable, so a mismatch triggers a full staged rebuild instead of a
# migration.
SCHEMA_VERSION = 6
MIN_SQLITE_VERSION = (3, 37, 0)
_CATALOG_CORRUPTION_MESSAGES = (
    "malformed",
    "not a database",
    "file is encrypted",
)


def catalog_path(index_dir: Path | str) -> Path:
    """Default location of the shared catalog, beside an ``indexes/`` directory."""
    return Path(index_dir).parent / CATALOG_FILENAME


def absolute_path(path: Path | str) -> Path:
    """Make a path absolute without resolving aliases or symlinks."""
    return Path(os.path.abspath(path))


def _is_catalog_corruption(error: BaseException) -> bool:
    """Return whether an initialization error warrants rebuilding the cache."""
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None and code & 0xFF in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
    }:
        return True
    message = str(error).casefold()
    return any(marker in message for marker in _CATALOG_CORRUPTION_MESSAGES)


# Serializes schema creation between threads in one process. Cross-process
# callers are serialized by the catalog lease and BEGIN IMMEDIATE below.
_SCHEMA_LOCK = threading.Lock()

# Columns of the generic sources table. Verified before trusting
# ``user_version``, so an old ZIM-specific catalog or one written by a
# different tool is rebuilt rather than queried with incompatible columns.
SOURCE_COLUMNS = (
    "provider", "collection_key", "key", "path", "provider_id", "size",
    "mtime", "title", "description", "long_description", "language",
    "creator", "publisher", "date", "tags", "public_tags", "category",
    "item_count", "license", "origin", "attribution", "relation",
    "mime_counts", "homepage", "native_fulltext_available", "icon_data",
    "icon_mime_type", "icon_path", "scanned_at", "provider_metadata",
)
# Read queries replace the raw icon BLOB with a one-byte marker so listing does
# not allocate and copy megabytes of icon bytes on every request.
_METADATA_SELECT = ", ".join(
    "CASE WHEN icon_data IS NOT NULL AND length(icon_data) > 0 "
    "THEN X'01' ELSE NULL END AS icon_data"
    if col == "icon_data"
    else col
    for col in SOURCE_COLUMNS
)


@dataclass(slots=True)
class SourceRecord:
    """One generic catalog row.

    The row identity is ``key``.  ``path`` is a private, nullable filesystem
    locator and is never part of a serialized source record.
    """

    provider: str
    collection_key: str
    key: str
    path: str | None
    provider_id: str
    size: int | None
    mtime: float | None
    title: str
    description: str
    long_description: str
    language: str
    creator: str
    publisher: str
    date: str
    tags: str
    public_tags: str
    category: str
    item_count: int | None
    license: str
    origin: str
    attribution: str
    relation: str
    mime_counts: str
    homepage: str
    native_fulltext_available: int | None
    icon_data: bytes | None
    icon_mime_type: str
    icon_path: str
    scanned_at: float
    provider_metadata: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SourceRecord:
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})

    def to_source_metadata(self) -> dict[str, Any]:
        """Provider-neutral fields used to construct ``SourceInfo``."""
        return {
            "provider": self.provider,
            "collection_key": self.collection_key,
            "title": self.title or None,
            "description": self.description or None,
            "long_description": self.long_description or None,
            "creator": self.creator or None,
            "publisher": self.publisher or None,
            "language": self.language or None,
            "date": self.date or None,
            "tags": tuple(tag for tag in self.public_tags.split(";") if tag),
            "size": self.size,
            "item_count": self.item_count,
            "category": self.category or None,
            "license": self.license or None,
            "origin": self.origin or None,
            "attribution": self.attribution or None,
            "relation": self.relation or None,
            "mime_counts": self.mime_counts or None,
            "homepage": self.homepage or None,
            "native_fulltext_available": (
                None
                if self.native_fulltext_available is None
                else bool(self.native_fulltext_available)
            ),
            "icon_path": self.icon_path or None,
            "provider_id": self.provider_id or None,
        }


def _canonical_provider_metadata(value: Any) -> str:
    """Validate and canonicalize a provider's private metadata JSON."""
    if value is None:
        return "{}"
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as error:
            raise ValueError("provider_metadata must be valid JSON") from error
    else:
        parsed = dict(value)
    if not isinstance(parsed, dict):
        raise ValueError("provider_metadata must be a JSON object")
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


class SourceCatalog:
    """SQLite storage for provider-neutral source metadata."""

    def __init__(self, db_path: Path | str | None):
        self.db_path = Path(db_path) if db_path is not None else Path(":memory:")
        self._lock = threading.RLock()
        # Bumped on every local write; combined with SQLite's data_version in
        # version() so HTTP caches notice metadata-only changes.
        self._revision = 0
        self._lease = None
        try:
            if db_path is not None:
                from .locking import FileLease, catalog_lease_path

                self._lease = FileLease(catalog_lease_path(db_path))
                self._lease.acquire(shared=True)
            self._initialize_database(db_path)
        except (OSError, sqlite3.Error) as error:
            if db_path is None or not _is_catalog_corruption(error):
                try:
                    self.db.close()
                except (AttributeError, sqlite3.Error):
                    pass
                raise
            from .diagnostics import public_cause

            logger.warning(
                "zimantic: catalog database unavailable (%s); deleting and "
                "recreating it",
                public_cause(error),
            )
            logger.debug("catalog database %s: %s", db_path, error)
            try:
                self.db.close()
            except (AttributeError, sqlite3.Error):
                pass
            database_path = Path(db_path)
            for suffix in ("", "-wal", "-shm"):
                Path(f"{database_path}{suffix}").unlink(missing_ok=True)
            self.db_path = database_path
            self._initialize_database(database_path)

    def _initialize_database(self, db_path: Path | str | None) -> None:
        self.db = self._open_database(db_path)
        version = tuple(int(part) for part in sqlite3.sqlite_version.split(".")[:3])
        if version < MIN_SQLITE_VERSION:
            self.db.close()
            raise RuntimeError(
                "zimantic requires SQLite 3.37 or newer for its STRICT catalog "
                f"schema (found {sqlite3.sqlite_version})"
            )
        self._ensure_schema()
        self.db.execute("PRAGMA busy_timeout=0")
        try:
            self.db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", None)
            if code is None or code & 0xFF not in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                raise
        else:
            self.db.rollback()
        finally:
            self.db.execute("PRAGMA busy_timeout=30000")

    def _open_database(self, db_path: Path | str | None) -> sqlite3.Connection:
        if db_path is not None:
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                path, timeout=30, check_same_thread=False
            )
        else:
            connection = sqlite3.connect(
                ":memory:", timeout=30, check_same_thread=False
            )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def _schema_current(self) -> tuple[bool, int]:
        """Read the schema state without taking a write lock."""
        version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(sources)")
        }
        current = version == SCHEMA_VERSION and columns == set(SOURCE_COLUMNS)
        return current, version

    def _ensure_schema(self) -> None:
        with _SCHEMA_LOCK:
            # Journal mode is persistent per database; do it under the same lock
            # as the schema check so parallel workers cannot race on first use.
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            # Most opens just read this; only a rebuild needs a write lock.
            current, _version = self._schema_current()
            if current:
                return
            lease = getattr(self, "_lease", None)
            if lease is not None:
                lease.release()
                lease.acquire(shared=False)
            try:
                # Serialize the rebuild with other processes (for example,
                # parallel ``zimantic build`` workers), then re-check: another
                # process may have rebuilt the catalog while we waited.
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    current, version = self._schema_current()
                    if not current:
                        self._rebuild_schema(version)
                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
            finally:
                if lease is not None:
                    lease.release()
                    lease.acquire(shared=True)

    def _rebuild_schema(self, version: int) -> None:
        if version:
            reason = (
                "schema version %s" % version
                if version != SCHEMA_VERSION
                else "a different table layout"
            )
            logger.warning(
                "zimantic: catalog database has %s; rebuilding (wanted version %s)",
                reason,
                SCHEMA_VERSION,
            )
        # The catalog is disposable: drop old provider-specific tables outright
        # rather than migrating rows or leaving a compatibility view.
        self.db.execute("DROP TABLE IF EXISTS zims")
        self.db.execute("DROP TABLE IF EXISTS sources")
        self.db.execute(
            """
            CREATE TABLE sources (
                provider TEXT NOT NULL,
                collection_key TEXT NOT NULL,
                key TEXT NOT NULL,
                path TEXT,
                provider_id TEXT NOT NULL DEFAULT '',
                size INTEGER,
                mtime REAL,
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                long_description TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                creator TEXT NOT NULL DEFAULT '',
                publisher TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                public_tags TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                item_count INTEGER,
                license TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT '',
                attribution TEXT NOT NULL DEFAULT '',
                relation TEXT NOT NULL DEFAULT '',
                mime_counts TEXT NOT NULL DEFAULT '',
                homepage TEXT NOT NULL DEFAULT '',
                native_fulltext_available INTEGER,
                icon_data BLOB,
                icon_mime_type TEXT NOT NULL DEFAULT '',
                icon_path TEXT NOT NULL DEFAULT '',
                scanned_at REAL NOT NULL,
                provider_metadata TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (key),
                CHECK (native_fulltext_available IS NULL
                       OR native_fulltext_available IN (0, 1)),
                CHECK (size IS NULL OR size >= 0),
                CHECK (mtime IS NULL OR mtime >= 0),
                CHECK (item_count IS NULL OR item_count >= 0)
            ) STRICT
            """
        )
        self.db.execute("CREATE INDEX sources_path ON sources(path)")
        self.db.execute(
            "CREATE INDEX sources_collection ON sources(collection_key)"
        )
        self.db.execute(
            "CREATE INDEX sources_provider_id ON sources(provider, provider_id)"
        )
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def version(self) -> tuple[int, int]:
        """Cheap token that changes when the catalog contents change."""
        with self._lock:
            data_version = self.db.execute("PRAGMA data_version").fetchone()[0]
        return (self._revision, int(data_version))

    def close(self) -> None:
        with self._lock:
            self.db.close()
        lease = getattr(self, "_lease", None)
        if lease is not None:
            lease.release()
            self._lease = None

    def __enter__(self) -> SourceCatalog:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ writes

    @staticmethod
    def _row(record: Mapping[str, Any] | SourceRecord) -> dict[str, Any]:
        """Normalize a provider record into a generic sources row."""
        if isinstance(record, SourceRecord):
            values = {
                field: getattr(record, field)
                for field in SourceRecord.__dataclass_fields__
            }
        else:
            values = dict(record)
        missing = {"provider", "collection_key", "key"} - values.keys()
        if missing:
            raise ValueError(
                "catalog record is missing " + ", ".join(sorted(missing))
            )
        row: dict[str, Any] = {}
        for column in SOURCE_COLUMNS:
            value = values.get(column)
            if column in {"provider", "collection_key", "key", "provider_id",
                          "title", "description", "long_description",
                          "language", "creator", "publisher", "date", "tags",
                          "public_tags", "category", "license", "origin",
                          "attribution", "relation", "mime_counts",
                          "homepage", "icon_mime_type", "icon_path"}:
                row[column] = "" if value is None else str(value)
            elif column == "scanned_at":
                row[column] = time.time() if value is None else float(value)
            elif column == "native_fulltext_available":
                row[column] = (
                    None if value is None else int(bool(value))
                )
            elif column == "provider_metadata":
                row[column] = _canonical_provider_metadata(
                    values.get(column)
                )
            elif column == "icon_data":
                data = values.get(column)
                row[column] = (
                    sqlite3.Binary(bytes(data)) if data is not None else None
                )
            else:
                row[column] = value
        if row["mtime"] is not None:
            # Subsecond changes are intentionally ignored; mtime is a cheap
            # change detector, not proof of identical content.
            row["mtime"] = float(round(float(row["mtime"])))
        return row

    def upsert(self, record: Mapping[str, Any] | SourceRecord) -> SourceRecord:
        """Insert or replace one record and return its stored form."""
        row = self._row(record)
        columns = list(SOURCE_COLUMNS)
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{key}=excluded.{key}" for key in columns[1:])
        with self._lock:
            self.db.execute(
                f"INSERT INTO sources ({', '.join(columns)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT(key) DO UPDATE SET {assignments}",
                [row.get(key) for key in columns],
            )
            self.db.commit()
            self._revision += 1
            stored = self._get_key_locked(str(row["key"]))
        if stored is None:  # pragma: no cover - the just-written row exists
            raise sqlite3.DatabaseError("catalog write did not persist")
        return SourceRecord.from_row(stored)

    def delete_paths(
        self, collection_key: str, paths: set[str] | list[str]
    ) -> int:
        """Delete records in one collection whose locator is in ``paths``."""
        values = [str(path) for path in paths]
        removed = 0
        with self._lock:
            for start in range(0, len(values), 900):
                batch = values[start : start + 900]
                placeholders = ",".join("?" for _ in batch)
                cursor = self.db.execute(
                    "DELETE FROM sources WHERE collection_key = ? "
                    f"AND path IN ({placeholders})",
                    [collection_key, *batch],
                )
                removed += max(0, cursor.rowcount)
            if removed:
                self.db.commit()
                self._revision += 1
        return removed

    def delete_keys(self, collection_key: str, keys: set[str] | list[str]) -> int:
        """Delete records in one collection by canonical key."""
        values = [str(key) for key in keys]
        removed = 0
        with self._lock:
            for start in range(0, len(values), 900):
                batch = values[start : start + 900]
                placeholders = ",".join("?" for _ in batch)
                cursor = self.db.execute(
                    "DELETE FROM sources WHERE collection_key = ? "
                    f"AND key IN ({placeholders})",
                    [collection_key, *batch],
                )
                removed += max(0, cursor.rowcount)
            if removed:
                self.db.commit()
                self._revision += 1
        return removed

    # ------------------------------------------------------------------- reads

    def _get_key_locked(self, key: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM sources WHERE key = ?", (key,)
        ).fetchone()

    def get_key(
        self, key: str, *, include_favicon: bool = True
    ) -> SourceRecord | None:
        """Return the record for one canonical key."""
        cols = "*" if include_favicon else _METADATA_SELECT
        with self._lock:
            row = self.db.execute(
                f"SELECT {cols} FROM sources WHERE key = ?", (key,)
            ).fetchone()
        return SourceRecord.from_row(row) if row else None

    def get_path(self, path: Path | str) -> SourceRecord | None:
        """Return the record for one exact private path locator."""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM sources WHERE path = ?",
                (str(absolute_path(path)),),
            ).fetchone()
        return SourceRecord.from_row(row) if row else None

    def rows(
        self,
        *,
        collection_key: str | None = None,
        include_favicon: bool = False,
    ) -> list[SourceRecord]:
        cols = "*" if include_favicon else _METADATA_SELECT
        query = f"SELECT {cols} FROM sources"
        params: tuple[Any, ...] = ()
        if collection_key is not None:
            query += " WHERE collection_key = ?"
            params = (collection_key,)
        query += " ORDER BY path ASC, key ASC"
        with self._lock:
            return [
                SourceRecord.from_row(row)
                for row in self.db.execute(query, params)
            ]

    def favicon(self, key: str) -> tuple[bytes | None, str]:
        with self._lock:
            row = self.db.execute(
                "SELECT icon_data, icon_mime_type FROM sources WHERE key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None, ""
        return row["icon_data"], row["icon_mime_type"] or ""


__all__ = [
    "CATALOG_FILENAME",
    "MIN_SQLITE_VERSION",
    "SCHEMA_VERSION",
    "SOURCE_COLUMNS",
    "SourceCatalog",
    "SourceRecord",
    "absolute_path",
    "catalog_path",
]
