"""ZIM source identity, bounded discovery, catalog scanning, and index lookup.

``providers/zim`` is the reference provider.  New providers should implement
the contracts in :mod:`zimantic.providers.base` rather than importing this
module.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from ...catalog import SourceCatalog, SourceRecord, absolute_path
from ...diagnostics import public_cause
from ...indexes.locator import IndexLocator, IndexLocatorError
from ...model_meta import (
    MODEL_PROBE_SHAPE_KEY,
    MODEL_PROBE_TEXTS_KEY,
    MODEL_PROBE_VECTORS_KEY,
)
from ...searching.config import _file_signature
from ...searching.index import _LocalIndex, _nprobe
from .metadata import metadata_from_archive, perma_ref, source_info_from_record

logger = logging.getLogger(__name__)

_SAFE_SLUG = re.compile(r"[^a-z0-9._-]+")
DEFAULT_SCAN_LIMIT = 1_000_000
DEFAULT_SCAN_TIMEOUT = 300


def is_zim_filename(name: str) -> bool:
    """True for a ZIM file name, ignoring extension case."""
    return name.lower().endswith(".zim")


def iter_zim_files(
    root: Path | str,
    on_error: Callable[[OSError], None] | None = None,
    *,
    max_entries: int | None = None,
    timeout_seconds: float | None = None,
) -> Iterator[Path]:
    """Yield ``*.zim`` files below ``root`` in a stable, bounded order."""
    started = time.monotonic()
    visited = 0
    for directory, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames.sort()
        for name in sorted(filenames):
            if not is_zim_filename(name):
                continue
            visited += 1
            if max_entries is not None and visited > max_entries:
                raise TimeoutError(
                    f"ZIM discovery exceeded the {max_entries}-entry limit"
                )
            if (
                timeout_seconds is not None
                and time.monotonic() - started > timeout_seconds
            ):
                raise TimeoutError(
                    f"ZIM discovery exceeded the {timeout_seconds}-second limit"
                )
            yield Path(directory, name)


def largest_zims_by_stem(zim_sizes: Any) -> dict[str, Path]:
    """Choose one largest archive per stem, preserving input order on ties."""
    selected: dict[str, tuple[int, Path]] = {}
    for path, size in zim_sizes.items():
        current = selected.get(path.stem)
        if current is None or size > current[0]:
            selected[path.stem] = (size, path)
    return {stem: path for stem, (_size, path) in selected.items()}


def _slug(value: str, *, limit: int = 48) -> str:
    text = _SAFE_SLUG.sub("-", str(value).casefold()).strip("-._")
    return (text or "source")[:limit].strip("-._") or "source"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def zim_collection_key(name: str) -> str:
    return f"zim:{name}"


def zim_source_key(collection_name: str, relative_path: str) -> str:
    """Stable ZIM source key from the collection-relative archive path."""
    relative = str(relative_path).replace("\\", "/")
    stem = Path(relative).stem
    return f"zim:{collection_name}:{_slug(stem)}-{_digest(relative)}"


def relative_archive(root: Path, path: Path) -> str:
    """Return a root-relative archive identity, never an absolute path.

    A single-file collection uses the archive basename; a directory collection
    uses the archive's path relative to that directory.
    """
    root_path = absolute_path(root)
    if root_path.is_file():
        return path.name
    try:
        return absolute_path(path).relative_to(root_path).as_posix()
    except (OSError, ValueError):
        return path.name


def archive_paths(
    config: Any,
    *,
    max_entries: int | None = None,
    timeout_seconds: float | None = None,
    on_error: Callable[[OSError], None] | None = None,
) -> list[Path]:
    """Return the archives addressed by one configured ZIM root."""
    root = Path(config.path)
    limits = getattr(config, "traversal", None)
    if max_entries is None:
        max_entries = getattr(limits, "max_visited_entries", DEFAULT_SCAN_LIMIT)
    if timeout_seconds is None:
        timeout_seconds = getattr(
            limits, "traversal_timeout_seconds", DEFAULT_SCAN_TIMEOUT
        )
    try:
        if root.is_file():
            return [root] if is_zim_filename(root.name) else []
        if root.is_dir():
            return list(
                iter_zim_files(
                    root,
                    on_error=on_error,
                    max_entries=max_entries,
                    timeout_seconds=timeout_seconds,
                )
            )
    except TimeoutError:
        raise
    except OSError:
        if on_error is not None:
            raise
        return []
    return []


def _minimal_record(
    path: Path,
    stat: os.stat_result,
    *,
    key: str,
    collection_key: str,
) -> dict[str, Any]:
    """A discovered-but-unreadable archive row (no persistent error state)."""
    return {
        "provider": "zim",
        "collection_key": collection_key,
        "key": key,
        "path": str(absolute_path(path)),
        "provider_id": "",
        "size": stat.st_size,
        "mtime": round(stat.st_mtime_ns / 1_000_000_000),
        "title": perma_ref(path.stem),
        "item_count": None,
        "native_fulltext_available": None,
        "scanned_at": time.time(),
        "provider_metadata": {},
    }


@dataclass
class ScanResult:
    """Outcome of one bounded, collection-scoped catalog scan."""

    complete: bool
    records: dict[str, SourceRecord] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "added": 0,
            "updated": 0,
            "removed": 0,
            "skipped": 0,
            "invalid": 0,
        }
    )

    @property
    def discovered_paths(self) -> set[str]:
        return set(self.records)


def _same_signature(record: SourceRecord | None, stat: os.stat_result) -> bool:
    if record is None:
        return False
    if record.size != stat.st_size:
        return False
    if record.mtime is None:
        return False
    return abs(float(record.mtime) - round(stat.st_mtime_ns / 1_000_000_000)) < 1e-6


def _key_for_path(config: Any, collection_name: str, root: Path, path: Path) -> str:
    return zim_source_key(collection_name, relative_archive(root, path))


def scan_collection(
    catalog: SourceCatalog,
    config: Any,
    *,
    collection_key: str,
    archive_factory: Any | None = None,
    force: bool = False,
) -> ScanResult:
    """Reconcile one ZIM collection with its archives.

    Only rows belonging to ``collection_key`` are considered.  Missing or
    unreadable roots and incomplete scans never delete unseen rows.
    """
    from libzim.reader import Archive

    root = Path(config.path)
    archive_type = Archive if archive_factory is None else archive_factory
    if archive_type is object:
        archive_type = None

    stats = ScanResult(complete=True)
    if not root.exists():
        stats.complete = False
        stats.errors.append(f"ZIM collection path does not exist: {root.name}")
        return stats

    unreadable_roots: set[str] = set()
    stat_failed: set[str] = set()
    top_error: OSError | None = None

    def remember_error(error: OSError) -> None:
        nonlocal top_error
        target = (
            absolute_path(error.filename) if error.filename else root
        )
        if target == absolute_path(root):
            top_error = error
        else:
            unreadable_roots.add(str(target))
        logger.warning(
            "zimantic: cannot list %s (%s)",
            _safe_display(root, Path(target)),
            public_cause(error),
        )
        logger.debug("zimantic: cannot list %s: %s", target, error)

    try:
        paths = archive_paths(config, on_error=remember_error)
    except TimeoutError as error:
        stats.complete = False
        stats.errors.append(str(error))
        return stats
    except OSError as error:
        stats.complete = False
        stats.errors.append(
            f"cannot scan ZIM collection: {public_cause(error)}"
        )
        logger.debug("cannot scan ZIM collection: %s", error)
        return stats

    if top_error is not None:
        stats.complete = False
        stats.errors.append(
            f"cannot list ZIM collection: {public_cause(top_error)}"
        )
        logger.debug("cannot list ZIM collection: %s", top_error)
        return stats

    files: dict[str, os.stat_result] = {}
    for path in paths:
        try:
            files[str(absolute_path(path))] = path.stat()
        except OSError as error:
            stat_failed.add(str(absolute_path(path)))
            logger.warning(
                "zimantic: cannot stat %s (%s); keeping its row",
                _safe_display(root, path),
                public_cause(error),
            )
            logger.debug("zimantic: cannot stat %s: %s", path, error)

    existing = {
        record.path: record
        for record in catalog.rows(collection_key=collection_key)
        if record.path is not None
    }

    keys: dict[str, str] = {}
    for path_str in files:
        key = _key_for_path(config, collection_key.split(":", 1)[-1], root, Path(path_str))
        other = keys.get(key)
        if other is not None and other != path_str:
            stats.errors.append(
                "source key collision between "
                f"{_safe_display(root, Path(other))} and "
                f"{_safe_display(root, Path(path_str))}"
            )
            logger.warning(
                "zimantic: ZIM source key collision (%s): %s and %s",
                key,
                _safe_display(root, Path(other)),
                _safe_display(root, Path(path_str)),
            )
            continue
        keys[key] = path_str

    for key, path_str in sorted(keys.items(), key=lambda item: item[1].casefold()):
        path = Path(path_str)
        stat = files[path_str]
        record = existing.get(path_str)
        if not force and _same_signature(record, stat):
            stats.records[path_str] = record  # type: ignore[assignment]
            stats.stats["skipped"] += 1
            continue
        if archive_type is None:
            # Tests may intentionally disable archive inspection.
            values = _minimal_record(
                path, stat, key=key, collection_key=collection_key
            )
            stored = catalog.upsert(values)
            # Keep the scan snapshot light: icon bytes stay in SQLite and are
            # read only if an operation actually serves the icon.
            if stored.icon_data:
                stored.icon_data = b"\x01"
            stats.records[path_str] = stored
            stats.stats["updated" if record is not None else "added"] += 1
            continue
        try:
            archive = archive_type(path_str)
            values = metadata_from_archive(
                archive, path, key=key, collection_key=collection_key
            )
            stored = catalog.upsert(values)
            # Keep the scan snapshot light: icon bytes stay in SQLite and are
            # read only if an operation actually serves the icon.
            if stored.icon_data:
                stored.icon_data = b"\x01"
            stats.records[path_str] = stored
            stats.stats["updated" if record is not None else "added"] += 1
        except Exception as error:
            logger.warning(
                "zimantic: skipping invalid ZIM %s (%s); re-download the "
                "archive or run zimcheck to see what is wrong",
                _safe_display(root, path),
                public_cause(error),
            )
            logger.debug("zimantic: invalid ZIM %s: %s", path, error)
            stored = catalog.upsert(
                _minimal_record(
                    path, stat, key=key, collection_key=collection_key
                )
            )
            stats.records[path_str] = stored
            stats.unreadable[path_str] = str(error)
            stats.stats["invalid"] += 1

    if stats.complete:
        missing = set(existing) - set(files)
        stale: set[str] = set()
        for path_str in missing:
            if path_str in stat_failed:
                continue
            path = Path(path_str)
            if any(_is_below(path, Path(area)) for area in unreadable_roots):
                continue
            stale.add(path_str)
        if stale:
            stats.stats["removed"] += catalog.delete_paths(collection_key, stale)
        kept = missing - stale
        if kept:
            logger.warning(
                "zimantic: keeping %d catalog row(s) under unreadable paths",
                len(kept),
            )
    logger.info("zimantic: catalog scan %s", stats.stats)
    return stats


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_display(root: Path, path: Path) -> str:
    """Log a path relative to its configured root; never leak an absolute path."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name or "<unknown>"


# --------------------------------------------------------------------- indexes


def _index_uri(db_path: Path) -> str:
    return db_path.resolve().as_uri() + "?mode=ro&immutable=1"


def _read_index_meta(
    db: sqlite3.Connection,
) -> tuple[bool, str, int | None, str]:
    rows = dict(
        db.execute(
            "SELECT key, value FROM meta WHERE key IN "
            "('done', 'filesize', 'model_fingerprint')"
        ).fetchall()
    )
    done_raw = rows.get("done")
    done_value = str(done_raw) if done_raw is not None else ""
    finished = done_value in {"full", "fast"}
    size_raw = rows.get("filesize")
    try:
        source_size = int(size_raw) if size_raw is not None else None
    except (TypeError, ValueError):
        source_size = None
    fingerprint_raw = rows.get("model_fingerprint")
    stored_fingerprint = (
        str(fingerprint_raw) if fingerprint_raw is not None else ""
    )
    return finished, done_value, source_size, stored_fingerprint


def read_index_state(db_path: Path) -> tuple[bool, int | None]:
    """Return ``(finished, recorded_source_size)`` for one SQLite index."""
    try:
        db = sqlite3.connect(_index_uri(db_path), uri=True, check_same_thread=False)
    except sqlite3.Error:
        return False, None
    try:
        finished, _done_value, source_size, _fp = _read_index_meta(db)
    except sqlite3.Error:
        return False, None
    finally:
        db.close()
    return finished, source_size


@dataclass
class ZimIndexOptions:
    semantic: bool = False
    defer_faiss: bool = False
    model_fingerprint: str = ""
    cfg: dict[str, Any] = field(default_factory=dict)
    probe_compatible: Callable[[sqlite3.Connection, Path], tuple[bool, str]] | None = None


def open_generation_index(
    source: Any,
    *,
    index_dir: Path | str,
    options: ZimIndexOptions,
    expected_source_size: int | None,
    expected_source_mtime: int | None = None,
) -> _LocalIndex | None:
    """Open the current validated generation for one ZIM source."""
    try:
        lease = IndexLocator(index_dir).paths(
            source,
            verify_faiss=bool(options.semantic and not options.defer_faiss),
        )
    except IndexLocatorError as error:
        logger.warning(
            "%s: index generation unavailable (%s)",
            source.key,
            public_cause(error),
        )
        logger.debug("%s: index generation error: %s", source.key, error)
        return None
    if lease is None:
        return None
    manifest_meta = (lease.manifest.get("metadata") or {}) if lease.manifest else {}
    try:
        recorded_size = manifest_meta.get("source_size")
        if (
            expected_source_size is not None
            and recorded_size is not None
            and int(recorded_size) != int(expected_source_size)
        ):
            logger.warning(
                "%s: index was built from a source of a different size; "
                "using native full-text until it is rebuilt",
                source.key,
            )
            lease.close()
            return None
        recorded_mtime = manifest_meta.get("source_mtime")
        if (
            expected_source_mtime is not None
            and recorded_mtime is not None
            and int(recorded_mtime) != int(expected_source_mtime)
        ):
            logger.warning(
                "%s: source archive changed since this index was built; "
                "using native full-text until it is rebuilt",
                source.key,
            )
            lease.close()
            return None
    except (TypeError, ValueError):
        pass
    db_path = lease.sqlite_path
    try:
        db = sqlite3.connect(
            _index_uri(db_path), uri=True, check_same_thread=False
        )
    except sqlite3.Error:
        lease.close()
        return None
    try:
        finished, done_value, source_size, stored_fingerprint = _read_index_meta(db)
        try:
            index_meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.Error:
            index_meta = {}
        if not finished:
            db.close()
            lease.close()
            return None
        if (
            expected_source_size is not None
            and source_size is not None
            and source_size != expected_source_size
        ):
            logger.warning(
                "%s: index (recorded source %s bytes) does not match the "
                "archive (%s bytes); using native full-text until it is rebuilt",
                source.key,
                source_size,
                expected_source_size,
            )
            db.close()
            lease.close()
            return None

        faiss_index = None
        faiss_path = lease.faiss_path
        if (
            options.semantic
            and not options.defer_faiss
            and done_value == "full"
            and faiss_path is not None
        ):
            current_fingerprint = options.model_fingerprint
            model_changed = bool(
                stored_fingerprint
                and current_fingerprint
                and stored_fingerprint != current_fingerprint
            )
            if model_changed and options.probe_compatible is not None:
                compatible, detail = options.probe_compatible(db, db_path)
                if compatible:
                    logger.warning(
                        "%s: vectors were built with a different model file, "
                        "but embedding probes match (%s); using them",
                        source.key,
                        detail,
                    )
                    model_changed = False
                else:
                    logger.warning(
                        "%s: vectors were built with a different model; "
                        "falling back to title and full-text until it is "
                        "rebuilt (%s)",
                        source.key,
                        detail,
                    )
            if not model_changed:
                try:
                    import faiss

                    faiss_index = faiss.read_index(
                        str(faiss_path),
                        faiss.IO_FLAG_MMAP_IFC | faiss.IO_FLAG_READ_ONLY,
                    )
                    if hasattr(faiss_index, "nprobe"):
                        faiss_index.nprobe = _nprobe(faiss_index, options.cfg)
                except Exception as error:
                    logger.warning(
                        "%s: vectors unavailable (%s); falling back to title "
                        "and full-text",
                        source.key,
                        public_cause(error),
                    )
                    logger.debug("%s: vector open error: %s", source.key, error)
                    faiss_index = None
        db.close()
        return _LocalIndex(
            None,
            faiss_index,
            None,
            None,
            threading.Lock(),
            db_path=_index_uri(db_path),
            db_signature=_file_signature(db_path),
            faiss_signature=(
                _file_signature(faiss_path)
                if options.semantic and not options.defer_faiss and faiss_path
                else None
            ),
            done=done_value,
            source_size=source_size,
            zim_path=None,
            zim_signature=None,
            has_fulltext_index=None,
            lease=lease,
            generation=lease.generation,
            index_meta=index_meta,
        )
    except sqlite3.Error:
        db.close()
        lease.close()
        return None


def open_native_index(
    record: SourceRecord,
    source: Any,
    *,
    archive_factory: Any | None = None,
) -> _LocalIndex | None:
    """Open an unindexed ZIM using only its native full-text index."""
    from libzim.reader import Archive

    archive_type = Archive if archive_factory is None else archive_factory
    if record.native_fulltext_available is False:
        return None
    zim_path = Path(record.path) if record.path else None
    if zim_path is None:
        return None
    archive = None
    if archive_type is not object:
        try:
            archive = archive_type(str(zim_path))
        except Exception as error:
            logger.warning(
                "%s: ZIM fallback unavailable (%s)",
                source.key,
                public_cause(error),
            )
            logger.debug("%s: ZIM fallback error: %s", source.key, error)
            return None
        if not getattr(archive, "has_fulltext_index", False):
            return None
    elif record.native_fulltext_available is not True:
        return None
    return _LocalIndex(
        None,
        None,
        archive,
        None,
        threading.Lock(),
        done="filesystem",
        source_size=record.size,
        zim_path=str(zim_path),
        zim_signature=_file_signature(zim_path),
        has_fulltext_index=True,
    )


def _apply_index_metadata(source: Any, meta: dict[str, str]) -> None:
    """Populate SourceInfo's published-index fields without inventing values."""
    from ...indexing import schema

    try:
        if schema.INDEXED_COUNT in meta:
            object.__setattr__(
                source,
                "indexed_count",
                schema.decode_int(meta[schema.INDEXED_COUNT]),
            )
        if schema.TRUNCATED in meta:
            object.__setattr__(
                source,
                "truncated",
                schema.decode_bool(meta[schema.TRUNCATED]),
            )
        if schema.BODY_TRUNCATED_COUNT in meta:
            object.__setattr__(
                source,
                "body_truncated_count",
                schema.decode_int(meta[schema.BODY_TRUNCATED_COUNT]),
            )
        if schema.DERIVED_TEXT in meta:
            value = schema.decode_json(meta[schema.DERIVED_TEXT])
            if isinstance(value, dict):
                object.__setattr__(source, "derived_text", value)
        if meta:
            object.__setattr__(source, "index_freshness", "unknown")
    except (KeyError, TypeError, ValueError):
        return


def discover_source_indexes(
    records: list[SourceRecord],
    *,
    collection_key: str,
    collection_label: str,
    index_dir: Path | str,
    web_base_path: str,
    options: ZimIndexOptions,
    archive_factory: Any | None = None,
    unreadable: dict[str, str] | None = None,
) -> tuple[list[Any], list[Any], dict[str, _LocalIndex]]:
    """Build searchable/library sources and open their indexes.

    Returns ``(sources, library_sources, indexes)``.  A source is searchable
    only when it has a finished index or a native full-text index.
    """
    unreadable = unreadable or {}
    sources = []
    library_sources = []
    indexes: dict[str, _LocalIndex] = {}
    for record in records:
        path_str = record.path or ""
        readable = path_str not in unreadable
        placeholder = source_info_from_record(
            record,
            collection_key=collection_key,
            collection_label=collection_label,
            icon_url=None,
            searchable=False,
            web_base_path=web_base_path,
        )
        index = None
        if readable:
            index = open_generation_index(
                placeholder,
                index_dir=index_dir,
                options=options,
                expected_source_size=record.size,
                expected_source_mtime=record.mtime,
            )
            if index is None:
                index = open_native_index(
                    record, placeholder, archive_factory=archive_factory
                )
        library_source = placeholder
        if index is not None:
            # A generation index still addresses its archive lazily; native
            # full-text, previews, and content serving use this locator.
            if record.path:
                index.zim_path = record.path
                try:
                    index.zim_signature = _file_signature(Path(record.path))
                except OSError:
                    index.zim_signature = None
            if record.native_fulltext_available is not None:
                index.has_fulltext_index = bool(
                    record.native_fulltext_available
                )
            searchable_source = source_info_from_record(
                record,
                collection_key=collection_key,
                collection_label=collection_label,
                icon_url=None,
                searchable=True,
                web_base_path=web_base_path,
            )
            _apply_index_metadata(searchable_source, index.index_meta)
            sources.append(searchable_source)
            library_sources.append(searchable_source)
            indexes[searchable_source.key] = index
        else:
            library_sources.append(library_source)
    sources.sort(key=lambda source: ((source.title or "").casefold(), source.key))
    library_sources.sort(
        key=lambda source: ((source.title or "").casefold(), source.key)
    )
    return sources, library_sources, indexes


__all__ = [
    "DEFAULT_SCAN_LIMIT",
    "DEFAULT_SCAN_TIMEOUT",
    "ScanResult",
    "ZimIndexOptions",
    "archive_paths",
    "discover_source_indexes",
    "is_zim_filename",
    "iter_zim_files",
    "largest_zims_by_stem",
    "open_generation_index",
    "open_native_index",
    "read_index_state",
    "relative_archive",
    "scan_collection",
    "zim_collection_key",
    "zim_source_key",
]
