"""Single-file catalog of the local ZIM archives.

Zimantic stores one SQLite search index per ZIM. This module adds a separate,
single ``catalog.sqlite`` beside ``index_dir`` that holds the metadata shared by all
local archives. It is a disposable cache: the ZIM files under ``zim_dir`` are
the source of truth, and deleting the catalog simply makes the next scan
rebuild it.

Why a separate catalog:

* ``serve`` can list sources, homepages, icons and counts without opening every
  archive (and without creating a ``Searcher`` per ZIM) at startup;
* favicons are stored as BLOBs instead of being rediscovered from the archive
  on each start;
* metadata extraction lives in one place and can be shared with other tools
  (IIAB's ``iiab_zim_catalog.py`` uses the same field names and units; its
  database layout differs, so each tool keeps its own file).
"""

from __future__ import annotations

import logging
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from libzim.reader import Archive
except ImportError:
    Archive = None

from .redirects import resolve_redirect_entry

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "catalog.sqlite"


def catalog_path(index_dir: Path | str) -> Path:
    """Default location of the shared catalog, beside an ``indexes/`` directory.

    Keeping the catalog outside ``index_dir`` avoids colliding with a ZIM whose
    filename stem is ``catalog`` (whose index is ``catalog.sqlite``) and makes
    the index directory contain only per-ZIM files.
    """
    return Path(index_dir).parent / CATALOG_FILENAME


# Bump when the table layout or the metadata derivation changes. The catalog is
# disposable, so a mismatch triggers a full staged rebuild instead of a migration.
SCHEMA_VERSION = 6
MIN_SQLITE_VERSION = (3, 37, 0)
MAX_FAVICON_BYTES = 1 << 20
# Library thumbnail size. libzim's default is also 48, but passing it keeps
# the on-disk catalog identical to IIAB's iiab_zim_catalog.py.
ILLUSTRATION_SIZE = 48
_CATALOG_CORRUPTION_MESSAGES = (
    "malformed",
    "not a database",
    "file is encrypted",
)


def _absolute_path(path: Path | str) -> Path:
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


def is_zim_filename(name: str) -> bool:
    """True for a ZIM file name, ignoring extension case.

    USB/FAT media commonly stores ``.ZIM`` in upper case, so every scanner must
    use the same predicate; otherwise the catalog could index files that source
    discovery and ``build`` cannot see.
    """
    return name.lower().endswith(".zim")


def iter_zim_files(root: Path | str, on_error=None) -> Iterator[Path]:
    """Yield ``*.zim`` files below ``root`` in a stable order.

    Build, catalog, and serve discovery all need the same traversal rules
    (recursive, no symlinked-directory following). Keeping one enumerator here
    prevents their directory filters from drifting apart. ``on_error`` receives
    the ``OSError`` for unreadable directories, like ``os.walk``.
    """
    for directory, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames.sort()
        for name in sorted(filenames):
            if is_zim_filename(name):
                yield Path(directory, name)


def largest_zims_by_stem(zim_sizes: Mapping[Path, int]) -> dict[str, Path]:
    """Choose one largest archive per stem, preserving input order on ties."""
    selected: dict[str, tuple[int, Path]] = {}
    for path, size in zim_sizes.items():
        current = selected.get(path.stem)
        if current is None or size > current[0]:
            selected[path.stem] = (size, path)
    return {stem: path for stem, (_size, path) in selected.items()}


# Serializes schema creation between threads in one process. Cross-process
# callers are serialized by BEGIN IMMEDIATE below.
_SCHEMA_LOCK = threading.Lock()

# Columns of the generic sources table. Verified before trusting
# ``user_version``, so an old ZIM-specific catalog or one written by a
# different tool is rebuilt rather than queried with incompatible columns.
_SOURCE_COLUMNS = (
    "provider", "collection_key", "key", "path", "provider_id", "size",
    "mtime", "title", "description", "long_description", "language",
    "creator", "publisher", "date", "tags", "public_tags", "category",
    "item_count", "license", "origin", "attribution", "relation",
    "mime_counts", "homepage", "native_fulltext_available", "icon_data",
    "icon_mime_type", "icon_path", "scanned_at", "provider_metadata",
)
# Backwards-compatible private alias used by code that still speaks in terms of
# the old ZIM cache while the public schema is generic.
_SCHEMA_COLUMNS = _SOURCE_COLUMNS
# Read queries replace the raw icon BLOB with a one-byte marker so listing does
# not allocate and copy megabytes of icon bytes on every request.
_METADATA_SELECT = ", ".join(
    "CASE WHEN icon_data IS NOT NULL AND length(icon_data) > 0 "
    "THEN X'01' ELSE NULL END AS icon_data"
    if col == "icon_data"
    else col
    for col in _SOURCE_COLUMNS
)

META_KEYS = (
    "Title",
    "Description",
    "LongDescription",
    "Language",
    "Creator",
    "Publisher",
    "Date",
    "Tags",
    "Name",
    "Flavour",
    "License",
    "Scraper",
    "Source",
    "Attribution",
    "Relation",
    "Counter",
)


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value).strip()


def normalize_creator(value: Any) -> str:
    """Treat placeholder creator metadata as missing."""
    value = _text(value)
    return "" if value == "-" else value


def _norm_date(value: Any) -> str:
    value = _text(value)
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
    return match.group(1) if match else ""


def normalize_counter(value: Any) -> str:
    """Sort a ZIM Counter (mime=count;...) from highest to lowest count."""
    entries: list[tuple[str, int]] = []
    invalid: list[str] = []
    for entry in _text(value).split(";"):
        entry = entry.strip()
        if not entry:
            continue
        mime, sep, count = entry.partition("=")
        mime = mime.strip()
        count = count.strip()
        if sep and mime and count.isdigit():
            entries.append((mime, int(count)))
        else:
            invalid.append(entry)
    entries.sort(key=lambda item: (-item[1], item[0]))
    return ";".join(
        [f"{mime}={count}" for mime, count in entries] + invalid
    )


def resolve_homepage(archive: Any) -> str:
    """Return the resolved main-entry path (following ``mainPage`` redirects)."""
    try:
        if not _call_maybe(getattr(archive, "has_main_entry", False)):
            return ""
        return resolve_redirect_entry(archive.main_entry).path or ""
    except Exception:
        return ""


def parse_tags(tags: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for tag in _text(tags).split(";"):
        tag = tag.strip()
        if not tag:
            continue
        key, sep, value = tag.partition(":")
        result[key] = value if sep else ""
    return result


def normalize_tags(tags: str) -> str:
    """Normalize tags exactly like libkiwix's ``convertTags()``.

    Old bare tags (nopic/novid/nodet) become ``_pictures:no`` etc., and missing
    defaults are appended. This keeps Zimantic's view of a ZIM consistent with
    Kiwix/OPDS consumers.
    """
    parts = [tag.strip() for tag in _text(tags).split(";") if tag.strip()]
    result: list[str] = []
    pic_seen = vid_seen = det_seen = index_seen = False
    for tag in parts:
        pic_seen = pic_seen or tag == "nopic" or tag.startswith("_pictures:")
        vid_seen = vid_seen or tag == "novid" or tag.startswith("_videos:")
        det_seen = det_seen or tag == "nodet" or tag.startswith("_details:")
        index_seen = index_seen or tag.startswith("_ftindex")
        if tag == "nopic":
            result.append("_pictures:no")
        elif tag == "novid":
            result.append("_videos:no")
        elif tag == "nodet":
            result.append("_details:no")
        elif tag == "_ftindex":
            result.append("_ftindex:yes")
        else:
            result.append(tag)
    if not index_seen:
        result.append("_ftindex:no")
    if not pic_seen:
        result.append("_pictures:yes")
    if not vid_seen:
        result.append("_videos:yes")
    if not det_seen:
        result.append("_details:yes")
    return ";".join(result)


def public_tags(tags: str) -> str:
    """Tags suitable for display (private ``_`` tags removed)."""
    return ";".join(
        tag for tag in _text(tags).split(";") if tag and not tag.startswith("_")
    )


def category_from_tags(tags: str) -> str:
    return parse_tags(tags).get("_category", "")


def perma_ref(stem: str) -> str:
    match = re.search(r"_\d{4}-\d{2}(?:-\d{2})?$", stem)
    return stem[: match.start()] if match else stem


def header_uuid(zim_path: Path | str) -> str:
    """Read the ZIM UUID from the archive header (works without libzim)."""
    with open(zim_path, "rb") as fp:
        header = fp.read(24)
    if len(header) < 24 or header[:4] != b"ZIM\x04":
        raise ValueError("not a ZIM file")
    return str(uuid.UUID(bytes=header[8:24]))


def _call_maybe(value: Any) -> Any:
    return value() if callable(value) else value


def metadata_from_archive(archive: Any, zim_path: Path | str) -> dict[str, Any]:
    """Extract the catalog record for one open libzim ``Archive``."""
    path = Path(zim_path)
    stat = path.stat()
    try:
        available = {
            key.decode("utf-8", "replace") if isinstance(key, bytes) else str(key)
            for key in archive.metadata_keys
        }
    except Exception:
        available = set()

    metadata: dict[str, str] = {}
    for key in META_KEYS:
        if available and key not in available:
            continue
        try:
            metadata[key] = _text(archive.get_metadata(key))
        except Exception:
            continue

    tags = normalize_tags(metadata.get("Tags", ""))
    stem = path.stem

    try:
        article_count = int(_call_maybe(archive.article_count) or 0)
    except Exception:
        article_count = 0
    try:
        media_count = int(_call_maybe(archive.media_count) or 0)
    except Exception:
        media_count = 0
    try:
        entry_count = int(_call_maybe(archive.entry_count) or 0)
    except Exception:
        entry_count = 0

    try:
        has_fulltext_index = 1 if _call_maybe(archive.has_fulltext_index) else 0
    except Exception:
        has_fulltext_index = 0

    homepage = resolve_homepage(archive)
    creator = normalize_creator(metadata.get("Creator", ""))

    favicon = None
    favicon_mime = ""
    try:
        item = archive.get_illustration_item(ILLUSTRATION_SIZE)
        data = bytes(item.content)
        if len(data) <= MAX_FAVICON_BYTES:
            favicon = data
            favicon_mime = item.mimetype or ""
        else:
            logger.warning(
                "ignoring %s illustration of %d bytes (limit %d)",
                path.name,
                len(data),
                MAX_FAVICON_BYTES,
            )
    except Exception:
        pass

    icon_path = ""
    if favicon is None:
        # No embedded illustration: fall back to Zimantic's icon discovery once,
        # at build/scan time, rather than rediscovering it at every startup.
        try:
            from .searching.sources import _find_archive_icon_path

            icon_path = _find_archive_icon_path(archive, homepage or None) or ""
        except Exception:
            icon_path = ""

    try:
        zim_uuid = str(archive.uuid)
    except Exception:
        try:
            zim_uuid = header_uuid(path)
        except Exception:
            zim_uuid = ""

    name = metadata.get("Name", "") or perma_ref(stem)

    return {
        "path": str(_absolute_path(path)),
        "stem": stem,
        "uuid": zim_uuid,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "title": metadata.get("Title", ""),
        "description": metadata.get("Description", ""),
        "long_description": metadata.get("LongDescription", ""),
        "language": metadata.get("Language", ""),
        "creator": creator,
        "publisher": metadata.get("Publisher", ""),
        "date": _norm_date(metadata.get("Date", "")),
        "tags": tags,
        "public_tags": public_tags(tags),
        "name": name,
        "flavour": metadata.get("Flavour", ""),
        "category": category_from_tags(tags),
        "article_count": article_count,
        "media_count": media_count,
        "entry_count": entry_count,
        "license": metadata.get("License", ""),
        "scraper": metadata.get("Scraper", ""),
        "source": metadata.get("Source", ""),
        "attribution": metadata.get("Attribution", ""),
        "relation": metadata.get("Relation", ""),
        "counter": normalize_counter(metadata.get("Counter", "")),
        "homepage": homepage,
        "has_fulltext_index": has_fulltext_index,
        "favicon": favicon,
        "favicon_mime": favicon_mime,
        "icon_path": icon_path,
        "available": 1,
        "last_error": "",
        "scanned_at": time.time(),
    }


@dataclass(slots=True)
class CatalogRecord:
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
    def from_row(cls, row: sqlite3.Row) -> CatalogRecord:
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})

    # -- compatibility views used by the phase-00 ZIM runtime.  These derive
    # from generic columns; they are never persisted as separate fields.
    @property
    def stem(self) -> str:
        return Path(self.path).stem if self.path else ""

    @property
    def uuid(self) -> str:
        return self.provider_id or ""

    @property
    def available(self) -> int:
        return 1

    @property
    def last_error(self) -> str:
        return ""

    @property
    def article_count(self) -> int:
        return int(self.item_count or 0)

    @property
    def media_count(self) -> int:
        return 0

    @property
    def entry_count(self) -> int:
        return int(self.item_count or 0)

    @property
    def favicon(self) -> bytes | None:
        return self.icon_data

    @property
    def favicon_mime(self) -> str:
        return self.icon_mime_type

    @property
    def has_fulltext_index(self) -> int:
        return int(bool(self.native_fulltext_available))

    @property
    def counter(self) -> str:
        return self.mime_counts

    @property
    def name(self) -> str:
        return self.title

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


class ZimCatalog:
    """Metadata cache for every ``*.zim`` below ``zim_dir``."""

    def __init__(
        self,
        db_path: Path | str | None,
        zim_dir: Path | str | None = None,
        archive_factory: Any | None = None,
        *,
        collection_name: str = "library",
        collection_root: Path | str | None = None,
    ):
        self.db_path = Path(db_path) if db_path is not None else Path(":memory:")
        self.zim_dir = _absolute_path(zim_dir) if zim_dir else None
        self.collection_name = collection_name
        self.collection_root = (
            _absolute_path(collection_root) if collection_root else None
        )
        self.archive_factory = archive_factory
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
            logger.warning(
                "zimantic: catalog database %s unavailable (%s); "
                "deleting and recreating it",
                db_path,
                error,
            )
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
        current = version == SCHEMA_VERSION and columns == set(_SCHEMA_COLUMNS)
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
                "%s has %s, rebuilding (wanted version %s)",
                self.db_path,
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

    def __enter__(self) -> ZimCatalog:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ writes

    def _source_row(self, record: dict[str, Any]) -> dict[str, Any]:
        """Map legacy metadata extraction into the generic sources row."""
        path = Path(record["path"])
        root = self.collection_root or self.zim_dir
        try:
            if root is not None and Path(root).is_file():
                relative = path.name
            elif root is not None:
                relative = path.resolve().relative_to(Path(root).resolve()).as_posix()
            else:
                relative = path.name
        except (OSError, ValueError):
            relative = path.name
        from .providers.zim import zim_source_key

        key = record.get("key") or zim_source_key(
            self.collection_name, relative or path.name
        )
        metadata = {
            name: record.get(name, "")
            for name in (
                "name",
                "flavour",
                "scraper",
                "source",
                "counter",
            )
        }
        return {
            "provider": "zim",
            "collection_key": f"zim:{self.collection_name}",
            "key": key,
            "path": record.get("path"),
            "provider_id": record.get("uuid") or "",
            "size": record.get("size"),
            "mtime": (
                round(float(record["mtime"]))
                if record.get("mtime") is not None
                else None
            ),
            "title": record.get("title") or record.get("name", ""),
            "description": record.get("description", ""),
            "long_description": record.get("long_description", ""),
            "language": record.get("language", ""),
            "creator": record.get("creator", ""),
            "publisher": record.get("publisher", ""),
            "date": record.get("date", ""),
            "tags": record.get("tags", ""),
            "public_tags": record.get("public_tags", ""),
            "category": record.get("category", ""),
            "item_count": record.get("item_count", record.get("entry_count")),
            "license": record.get("license", ""),
            "origin": record.get("origin", record.get("source", "")),
            "attribution": record.get("attribution", ""),
            "relation": record.get("relation", ""),
            "mime_counts": record.get(
                "mime_counts", record.get("counter", "")
            ),
            "homepage": record.get("homepage", ""),
            "native_fulltext_available": (
                record.get(
                    "native_fulltext_available",
                    record.get("has_fulltext_index"),
                )
            ),
            "icon_data": record.get("icon_data", record.get("favicon")),
            "icon_mime_type": record.get(
                "icon_mime_type", record.get("favicon_mime", "")
            ),
            "icon_path": record.get("icon_path", ""),
            "scanned_at": record.get("scanned_at", time.time()),
            "provider_metadata": json.dumps(metadata, sort_keys=True),
        }

    def _upsert(self, record: dict[str, Any]) -> None:
        values = self._source_row(record)
        columns = list(_SOURCE_COLUMNS)
        if values.get("icon_data") is not None:
            values["icon_data"] = sqlite3.Binary(values["icon_data"])
        if values.get("native_fulltext_available") is not None:
            values["native_fulltext_available"] = int(
                bool(values["native_fulltext_available"])
            )
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{key}=excluded.{key}" for key in columns[1:])
        self.db.execute(
            f"INSERT INTO sources ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(key) DO UPDATE SET {assignments}",
            [values.get(key) for key in columns],
        )
        self._revision += 1

    def upsert_from_archive(self, archive: Any, zim_path: Path | str) -> CatalogRecord:
        """Store metadata extracted from an already-open archive."""
        record = metadata_from_archive(archive, zim_path)
        with self._lock:
            self._upsert(record)
            self.db.commit()
            row = self._get_path_locked(record["path"])
            return CatalogRecord.from_row(row)

    def mark_error(
        self,
        zim_path: Path | str,
        error: Exception | str,
        *,
        size: int | None = None,
        mtime: float | None = None,
    ) -> None:
        """Record an unreadable archive minimally.

        Availability and error state are deliberately not persisted; the row
        establishes discovery/addressability only. The failure is reported by
        the operation that encounters it.
        """
        path = str(_absolute_path(zim_path))
        try:
            stat = Path(zim_path).stat()
            size = stat.st_size if size is None else size
            mtime = stat.st_mtime if mtime is None else mtime
        except OSError:
            pass
        with self._lock:
            self._upsert(
                {
                    "path": path,
                    "size": size or 0,
                    "mtime": mtime or 0.0,
                    "scanned_at": time.time(),
                }
            )
            self.db.commit()

    def scan(self) -> dict[str, int]:
        """Reconcile the catalog with ``zim_dir``.

        Only new, resized, retimed or previously failing archives are opened. A
        ZIM that cannot be read keeps its old metadata (if any) but is marked
        unavailable; it is retried on every scan. A missing or unreadable
        ``zim_dir`` raises instead of treating every row as removed, so an
        unmounted drive keeps the previous cache and library. A subdirectory
        that cannot be listed keeps its rows but does not block updates to the
        rest of the tree.
        """
        if self.zim_dir is None:
            raise ValueError("scan() needs zim_dir")
        if not self.zim_dir.is_dir():
            # An absent directory (unmounted drive, broken symlink, typo) must
            # not be mistaken for "every ZIM was removed": that would delete the
            # cache and publish an empty library.
            raise FileNotFoundError(
                "ZIM directory does not exist: %s" % self.zim_dir
            )
        files: dict[str, os.stat_result] = {}
        unreadable_roots: set[str] = set()
        stat_failed: set[str] = set()
        top_error: OSError | None = None

        def remember_error(error: OSError) -> None:
            nonlocal top_error
            target = (
                _absolute_path(error.filename)
                if error.filename
                else self.zim_dir
            )
            if target == self.zim_dir:
                top_error = error
            else:
                # Keep rows below this directory instead of deleting them.
                unreadable_roots.add(str(target))
            logger.warning("zimantic: cannot list %s (%s)", target, error)

        for path in iter_zim_files(self.zim_dir, on_error=remember_error):
            try:
                files[str(_absolute_path(path))] = path.stat()
            except OSError as error:
                # A transient stat failure must not look like a deletion.
                stat_failed.add(str(_absolute_path(path)))
                logger.warning(
                    "zimantic: cannot stat %s (%s); keeping its row",
                    path,
                    error,
                )
        if top_error is not None:
            # The tree itself could not be listed; a scan would look like every
            # ZIM was removed, so fail and keep the previous catalog instead.
            raise top_error

        stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0, "invalid": 0}
        with self._lock:
            existing = {
                row["path"]: row
                for row in self.db.execute(
                    "SELECT path, size, mtime FROM sources WHERE path IS NOT NULL"
                )
            }

        def unreadable(path_str: str) -> bool:
            if path_str in stat_failed:
                return True
            path = Path(path_str)
            for root in unreadable_roots:
                try:
                    path.relative_to(root)
                except ValueError:
                    continue
                return True
            return False

        missing = set(existing) - set(files)
        stale = {path_str for path_str in missing if not unreadable(path_str)}
        kept = missing - stale
        if kept:
            logger.warning(
                "zimantic: keeping %d catalog row(s) under unreadable paths",
                len(kept),
            )

        # Unit tests may pass ``object`` to prevent accidental archive access.
        _Archive = self.archive_factory
        if _Archive is None:
            _Archive = Archive
        if _Archive is None or _Archive is object:
            # Keep path and size identity available to SQLite indexes even when
            # archive inspection is intentionally disabled (for example, in
            # tests that pass ``object`` as the archive factory).
            for path_str, stat in files.items():
                if path_str in stale:
                    continue
                row = existing.get(path_str)
                if (
                    row is not None
                    and row["size"] == stat.st_size
                    and abs(float(row["mtime"]) - round(stat.st_mtime)) < 1e-6
                ):
                    stats["skipped"] += 1
                    continue
                metadata = {column: "" for column in _SCHEMA_COLUMNS}
                metadata.update(
                    path=path_str,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    item_count=0,
                    native_fulltext_available=0,
                    icon_data=None,
                    scanned_at=time.time(),
                )
                with self._lock:
                    self._upsert(metadata)
                stats["updated" if path_str in existing else "added"] += 1
            with self._lock:
                for path_str in stale:
                    self.db.execute("DELETE FROM sources WHERE path = ?", (path_str,))
                    stats["removed"] += 1
                    self._revision += 1
                self.db.commit()
            logger.debug("zimantic: archive inspection is disabled; catalogued paths only")
            return stats

        for path_str, stat in files.items():
            row = existing.get(path_str)
            if (
                row is not None
                and row["size"] == stat.st_size
                and abs(float(row["mtime"]) - round(stat.st_mtime)) < 1e-6
            ):
                stats["skipped"] += 1
                continue
            try:
                archive = _Archive(path_str)
                with self._lock:
                    self._upsert(metadata_from_archive(archive, path_str))
                stats["updated" if row is not None else "added"] += 1
            except Exception as error:
                logger.warning(
                    "zimantic: skipping invalid ZIM %s (%s); re-download the "
                    "archive or run zimcheck to see what is wrong",
                    path_str,
                    str(error),
                )
                self.mark_error(path_str, error, size=stat.st_size, mtime=stat.st_mtime)
                stats["invalid"] += 1

        with self._lock:
            for path_str in stale:
                self.db.execute("DELETE FROM sources WHERE path = ?", (path_str,))
                stats["removed"] += 1
                self._revision += 1
            self.db.commit()
        logger.info("zimantic: catalog scan %s", stats)
        return stats

    # ------------------------------------------------------------------- reads

    def _get_path_locked(self, path: Path | str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM sources WHERE path = ?", (str(_absolute_path(path)),)
        ).fetchone()

    def get_path(self, path: Path | str) -> CatalogRecord | None:
        """Return the record for one exact private path locator."""
        with self._lock:
            row = self._get_path_locked(path)
        return CatalogRecord.from_row(row) if row else None

    def rows(self, *, include_favicon: bool = False) -> list[CatalogRecord]:
        cols = "*" if include_favicon else _METADATA_SELECT
        with self._lock:
            return [
                CatalogRecord.from_row(row)
                for row in self.db.execute(
                    f"SELECT {cols} FROM sources ORDER BY path ASC"
                )
            ]

    @staticmethod
    def _stem_clause(stem: str) -> tuple[str, tuple[str, ...]]:
        return (
            "(path = ? OR lower(path) LIKE lower(?))",
            (stem + ".zim", "%/" + stem + ".zim"),
        )

    def get_stem(self, stem: str, *, include_favicon: bool = False) -> CatalogRecord | None:
        cols = "*" if include_favicon else _METADATA_SELECT
        clause, params = self._stem_clause(stem)
        with self._lock:
            row = self.db.execute(
                f"SELECT {cols} FROM sources WHERE {clause} "
                "ORDER BY size DESC, path ASC LIMIT 1",
                params,
            ).fetchone()
        return CatalogRecord.from_row(row) if row else None

    def get_stem_size(
        self, stem: str, size: int | None, *, include_favicon: bool = False
    ) -> CatalogRecord | None:
        """Prefer the row whose source size matches an index's recorded size."""
        if size is None:
            return self.get_stem(stem, include_favicon=include_favicon)
        cols = "*" if include_favicon else _METADATA_SELECT
        clause, params = self._stem_clause(stem)
        with self._lock:
            row = self.db.execute(
                f"SELECT {cols} FROM sources WHERE {clause} AND size = ? "
                "ORDER BY path ASC LIMIT 1",
                (*params, size),
            ).fetchone()
        if row:
            return CatalogRecord.from_row(row)
        return self.get_stem(stem, include_favicon=include_favicon)

    def find_path(self, stem: str, size: int | None = None) -> Path | None:
        """Return the matching-size archive path, or the largest same-stem copy."""
        record = self.get_stem_size(stem, size)
        return Path(record.path) if record is not None and record.path else None

    def favicon(self, stem: str) -> tuple[bytes | None, str]:
        clause, params = self._stem_clause(stem)
        with self._lock:
            row = self.db.execute(
                f"SELECT icon_data, icon_mime_type FROM sources WHERE {clause} "
                "ORDER BY size DESC LIMIT 1",
                params,
            ).fetchone()
        if not row:
            return None, ""
        return row["icon_data"], row["icon_mime_type"] or ""


# Provider-neutral names for the storage layer. Phase 01 moves ZIM scan
# ownership behind a provider; the storage contract is already generic.
SourceCatalog = ZimCatalog
SourceRecord = CatalogRecord
