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
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
# disposable, so a mismatch triggers a full rescan instead of a migration.
SCHEMA_VERSION = 5
MAX_FAVICON_BYTES = 1 << 20
# Library thumbnail size. libzim's default is also 48, but passing it keeps
# the on-disk catalog identical to IIAB's iiab_zim_catalog.py.
ILLUSTRATION_SIZE = 48


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


# Serializes schema creation between threads in one process. Cross-process
# callers are serialized by BEGIN IMMEDIATE below.
_SCHEMA_LOCK = threading.Lock()

# Columns of the zims table. Verified before trusting ``user_version``, so a
# catalog written by a different schema (for example IIAB's iiab_zim_catalog.py,
# which reuses the filename and SCHEMA_VERSION but not the layout) is rebuilt
# instead of failing every query with "no such column".
_SCHEMA_COLUMNS = (
    "path", "stem", "uuid", "size", "mtime", "title", "description",
    "long_description", "language", "creator", "publisher", "date", "tags",
    "public_tags", "name", "flavour", "category", "article_count",
    "media_count", "entry_count", "license", "scraper", "source",
    "attribution", "relation", "counter", "homepage", "has_fulltext_index",
    "favicon", "favicon_mime", "icon_path", "available",
    "last_error", "scanned_at",
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
        "path": str(path.resolve()),
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
    path: str
    stem: str
    uuid: str
    size: int
    mtime: float
    title: str
    description: str
    long_description: str
    language: str
    creator: str
    publisher: str
    date: str
    tags: str
    public_tags: str
    name: str
    flavour: str
    category: str
    article_count: int
    media_count: int
    entry_count: int
    license: str
    scraper: str
    source: str
    attribution: str
    relation: str
    counter: str
    homepage: str
    has_fulltext_index: int
    favicon: bytes | None
    favicon_mime: str
    icon_path: str
    available: int
    last_error: str
    scanned_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> CatalogRecord:
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})

    def to_source_metadata(self) -> dict[str, Any]:
        """Fields used by ``SourceInfo`` and the JSON API."""
        return {
            "uuid": self.uuid or None,
            "title": self.title or None,
            "description": self.description or None,
            "long_description": self.long_description or None,
            "creator": self.creator or None,
            "publisher": self.publisher or None,
            "language": self.language or None,
            "date": self.date or None,
            "tags": tuple(tag for tag in self.public_tags.split(";") if tag),
            "size": self.size,
            "article_count": self.article_count,
            "media_count": self.media_count,
            "name": self.name or None,
            "flavour": self.flavour or None,
            "category": self.category or None,
            "license": self.license or None,
            "scraper": self.scraper or None,
            "source": self.source or None,
            "attribution": self.attribution or None,
            "relation": self.relation or None,
            "counter": self.counter or None,
            "homepage": self.homepage or None,
            "has_fulltext_index": bool(self.has_fulltext_index),
            "icon_path": self.icon_path or None,
        }


class ZimCatalog:
    """Metadata cache for every ``*.zim`` below ``zim_dir``."""

    def __init__(
        self,
        db_path: Path | str,
        zim_dir: Path | str | None = None,
        archive_factory: Any | None = None,
    ):
        self.db_path = Path(db_path)
        self.zim_dir = Path(zim_dir).resolve() if zim_dir else None
        self.archive_factory = archive_factory
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(
            self.db_path, timeout=30, check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Bumped on every local write; combined with SQLite's data_version in
        # version() so HTTP caches notice metadata-only changes.
        self._revision = 0
        self.db.execute("PRAGMA busy_timeout=30000")
        self._ensure_schema()

    def _schema_current(self) -> tuple[bool, int]:
        """Read the schema state without taking a write lock."""
        version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(zims)")
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
            # Serialize the rebuild with other processes (for example, parallel
            # ``zimantic build`` workers), then re-check: another process may
            # have rebuilt the catalog while we waited for the lock.
            self.db.execute("BEGIN IMMEDIATE")
            try:
                current, version = self._schema_current()
                if not current:
                    self._rebuild_schema(version)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

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
        self.db.execute("DROP TABLE IF EXISTS zims")
        self.db.execute(
            """
            CREATE TABLE zims (
                path TEXT PRIMARY KEY,
                stem TEXT NOT NULL,
                uuid TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                long_description TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                creator TEXT NOT NULL DEFAULT '',
                publisher TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                public_tags TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                flavour TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                article_count INTEGER NOT NULL DEFAULT 0,
                media_count INTEGER NOT NULL DEFAULT 0,
                entry_count INTEGER NOT NULL DEFAULT 0,
                license TEXT NOT NULL DEFAULT '',
                scraper TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                attribution TEXT NOT NULL DEFAULT '',
                relation TEXT NOT NULL DEFAULT '',
                counter TEXT NOT NULL DEFAULT '',
                homepage TEXT NOT NULL DEFAULT '',
                has_fulltext_index INTEGER NOT NULL DEFAULT 0,
                favicon BLOB,
                favicon_mime TEXT NOT NULL DEFAULT '',
                icon_path TEXT NOT NULL DEFAULT '',
                available INTEGER NOT NULL DEFAULT 1,
                last_error TEXT NOT NULL DEFAULT '',
                scanned_at REAL NOT NULL
            )
            """
        )
        self.db.execute("CREATE INDEX zims_stem ON zims(stem)")
        self.db.execute("CREATE INDEX zims_uuid ON zims(uuid)")
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def version(self) -> tuple[int, int]:
        """Cheap token that changes when the catalog contents change."""
        with self._lock:
            data_version = self.db.execute("PRAGMA data_version").fetchone()[0]
        return (self._revision, int(data_version))

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> ZimCatalog:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ writes

    def _upsert(self, record: dict[str, Any]) -> None:
        columns = [
            "path", "stem", "uuid", "size", "mtime", "title", "description",
            "long_description", "language", "creator", "publisher", "date",
            "tags", "public_tags", "name", "flavour", "category",
            "article_count", "media_count", "entry_count", "license", "scraper",
            "source", "attribution", "relation", "counter",
            "homepage", "has_fulltext_index", "favicon", "favicon_mime",
            "icon_path", "available", "last_error",
            "scanned_at",
        ]
        values = {key: record.get(key) for key in columns}
        if values.get("favicon") is not None:
            values["favicon"] = sqlite3.Binary(values["favicon"])
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{key}=excluded.{key}" for key in columns[1:])
        self.db.execute(
            f"INSERT INTO zims ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(path) DO UPDATE SET {assignments}",
            [values[key] for key in columns],
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
        """Record an unreadable archive without discarding earlier metadata."""
        path = str(Path(zim_path).resolve())
        try:
            stat = Path(zim_path).stat()
            size = stat.st_size if size is None else size
            mtime = stat.st_mtime if mtime is None else mtime
        except OSError:
            pass
        with self._lock:
            self.db.execute(
                """
                INSERT INTO zims (
                    path, stem, size, mtime, available, last_error, scanned_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    available=0,
                    last_error=excluded.last_error,
                    scanned_at=excluded.scanned_at
                """,
                (
                    path,
                    Path(zim_path).stem,
                    size or 0,
                    mtime or 0.0,
                    str(error),
                    time.time(),
                ),
            )
            self._revision += 1
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
                Path(error.filename).resolve()
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
                files[str(path.resolve())] = path.stat()
            except OSError as error:
                # A transient stat failure must not look like a deletion.
                stat_failed.add(str(path.resolve()))
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
                row["path"]: row for row in self.db.execute("SELECT * FROM zims")
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

        # Unit tests replace libzim.reader.Archive with ``object`` to prevent
        # accidental archive access. In that environment the catalog cannot
        # inspect ZIMs; leave it empty so Search's archive fallback still works.
        _Archive = self.archive_factory
        if _Archive is None:
            try:
                from libzim.reader import Archive as _Archive
            except Exception:
                _Archive = None
        if _Archive is None or _Archive is object:
            logger.debug("zimantic: libzim.reader.Archive is patched; skipping catalog scan")
            return stats

        for path_str, stat in files.items():
            row = existing.get(path_str)
            if (
                row is not None
                and row["available"]
                and row["size"] == stat.st_size
                and abs(float(row["mtime"]) - stat.st_mtime) < 1e-6
            ):
                stats["skipped"] += 1
                continue
            try:
                archive = _Archive(path_str)
                with self._lock:
                    self._upsert(metadata_from_archive(archive, path_str))
                stats["updated" if row is not None else "added"] += 1
            except Exception as error:
                logger.warning("zimantic: skipping invalid ZIM %s (%s)", path_str, str(error))
                self.mark_error(path_str, error, size=stat.st_size, mtime=stat.st_mtime)
                stats["invalid"] += 1

        with self._lock:
            for path_str in stale:
                self.db.execute("DELETE FROM zims WHERE path = ?", (path_str,))
                stats["removed"] += 1
                self._revision += 1
            self.db.commit()
        logger.info("zimantic: catalog scan %s", stats)
        return stats

    # ------------------------------------------------------------------- reads

    def _get_path_locked(self, path: Path | str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM zims WHERE path = ?", (str(Path(path).resolve()),)
        ).fetchone()

    def rows(self) -> list[CatalogRecord]:
        query = "SELECT * FROM zims ORDER BY stem"
        with self._lock:
            return [CatalogRecord.from_row(row) for row in self.db.execute(query)]

    def get_stem(self, stem: str) -> CatalogRecord | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM zims WHERE stem = ? ORDER BY size DESC LIMIT 1",
                (stem,),
            ).fetchone()
        return CatalogRecord.from_row(row) if row else None

    def get_stem_size(self, stem: str, size: int | None) -> CatalogRecord | None:
        """Prefer the row whose source size matches an index's recorded size."""
        if size is None:
            return self.get_stem(stem)
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM zims WHERE stem = ? AND size = ? "
                "ORDER BY size DESC LIMIT 1",
                (stem, size),
            ).fetchone()
        if row:
            return CatalogRecord.from_row(row)
        return self.get_stem(stem)

    def favicon(self, stem: str) -> tuple[bytes | None, str]:
        with self._lock:
            row = self.db.execute(
                "SELECT favicon, favicon_mime FROM zims WHERE stem = ? "
                "ORDER BY size DESC LIMIT 1",
                (stem,),
            ).fetchone()
        if not row:
            return None, ""
        return row["favicon"], row["favicon_mime"] or ""
