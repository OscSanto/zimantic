"""Files collection/source discovery and index lookup.

One configured directory is one collection.  The default granularity makes the
whole root one source; ``children`` splits immediate child directories into
sources plus one reserved source for loose root files. Discovery uses names,
extensions, and ``stat``, with bounded content probes for unknown MIME types
and root-document summaries. It never hashes file contents.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat as stat_module
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ...catalog import SourceCatalog
from ...content.reader import (
    extract_html_summary,
    extract_markdown_document,
    extract_plain_text_document,
)
from ...contracts import SourceInfo
from ...diagnostics import public_cause
from ...indexing import schema
from ...indexing.locator import IndexLocator, IndexLocatorError
from ...searching.config import _file_signature
from ...searching.index import IndexHandle, _nprobe
from .common import (
    FilesConfinedError,
    confined_stat,
    detect_mimetype,
    extension_mimetype,
    is_hidden_name,
    is_hidden_path,
    iter_confined_directory,
    library_family,
    matches_any,
    normalize_relative_path,
    open_confined,
    ordered_families,
    path_components,
    relative_join,
)
from .config_defaults import (
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_SOURCES,
)

logger = logging.getLogger(__name__)

_SAFE_SLUG = re.compile(r"[^a-z0-9._-]+")
_SYSTEM_NAMES = frozenset({"__pycache__"})
LOOSE_IDENTIFIER = "__loose__"
ROOT_IDENTIFIER = "root"
COLLAPSED_IDENTIFIER = "collapsed"
_SUMMARY_NAMES = (
    "readme.md", "readme.markdown", "readme.txt", "readme",
    "readme.html", "readme.htm",
    "about.md", "about.markdown", "about.txt", "about",
    "about.html", "about.htm",
    "index.html", "index.htm",
)
_SUMMARY_BYTES = 64 << 10
_SUMMARY_CHARS = 400
# Bump when summary extraction or candidate selection changes.
_SUMMARY_VERSION = 1


class FilesTraversalError(RuntimeError):
    """A discovery/build traversal budget was exhausted."""


def _slug(value: str, *, limit: int = 48) -> str:
    text = _SAFE_SLUG.sub("-", str(value).casefold()).strip("-._")
    return (text or "source")[:limit].strip("-._") or "source"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def files_collection_key(name: str) -> str:
    return f"files:{name}"


def files_source_key(collection_name: str, identifier: str) -> str:
    return f"files:{collection_name}:{identifier}"


def files_dir_key(collection_name: str, relative_dir: str) -> str:
    relative = normalize_relative_path(relative_dir)
    return files_source_key(
        collection_name, f"dir-{_slug(relative)}-{_digest(relative)}"
    )


def files_root_key(collection_name: str) -> str:
    return files_source_key(collection_name, ROOT_IDENTIFIER)


def files_collapsed_key(collection_name: str) -> str:
    return files_source_key(collection_name, COLLAPSED_IDENTIFIER)


def files_loose_key(collection_name: str) -> str:
    return files_source_key(collection_name, LOOSE_IDENTIFIER)


@dataclass(frozen=True)
class FilesSource:
    """One discovered source plus its membership root."""

    info: SourceInfo
    membership_root: str = ""
    loose_only: bool = False
    has_icon: bool = False

    @property
    def key(self) -> str:
        return self.info.key


@dataclass
class FilesDiscovery:
    sources: tuple[FilesSource, ...] = ()
    errors: tuple[str, ...] = ()
    collapsed: bool = False


class _Budget:
    def __init__(self, max_entries: int, timeout_seconds: float) -> None:
        self.max_entries = max(1, int(max_entries))
        self.timeout_seconds = max(0.001, float(timeout_seconds))
        self.started = time.monotonic()
        self.visited = 0

    def tick(self) -> None:
        self.visited += 1
        if self.visited > self.max_entries:
            raise FilesTraversalError(
                f"files traversal exceeded the {self.max_entries}-entry limit"
            )
        if time.monotonic() - self.started > self.timeout_seconds:
            raise FilesTraversalError(
                f"files traversal exceeded the {self.timeout_seconds:g}-second limit"
            )


def _traversal_limits(config: Any) -> tuple[int, float]:
    limits = getattr(config, "traversal", None)
    return (
        int(getattr(limits, "max_visited_entries", 1_000_000)),
        float(getattr(limits, "traversal_timeout_seconds", 300)),
    )


def _max_sources(config: Any) -> int:
    value = getattr(config, "max_sources", None)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_MAX_SOURCES


def _max_entries(config: Any) -> int:
    value = getattr(config, "max_entries", None)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_MAX_ENTRIES


def _max_file_bytes(config: Any) -> int:
    value = getattr(config, "max_file_bytes", None)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_MAX_FILE_BYTES


def _display_label(config: Any) -> str:
    return str(getattr(config, "label", None) or getattr(config, "name", "files"))


def _regular_stat(root: Path, relative: str) -> Any | None:
    try:
        info = confined_stat(root, relative)
    except (FilesConfinedError, OSError, ValueError):
        return None
    if not stat_module.S_ISREG(info.st_mode):
        return None
    return info


def _homepage(root: Path, membership_root: str) -> str | None:
    for name in ("index.html", "index.htm"):
        candidate = relative_join(membership_root, name)
        if _regular_stat(root, candidate) is not None:
            return candidate
    return None


def _icon(root: Path, membership_root: str) -> tuple[bool, str | None]:
    for name in ("favicon.ico", "favicon.png", "apple-touch-icon.png"):
        candidate = relative_join(membership_root, name)
        if _regular_stat(root, candidate) is not None:
            return True, candidate
    return False, None


def _icon_url(source_key: str, has_icon: bool, web_base_path: str) -> str | None:
    if not has_icon:
        return None
    return (
        f"{web_base_path}/source-icon/"
        f"{quote(source_key, safe='')}"
    )


def _description(
    root: Path,
    membership_root: str,
    exclude: Sequence[str],
    budget: _Budget,
    *,
    source: SourceInfo,
    catalog: SourceCatalog | None,
) -> str | None:
    candidates: list[tuple[str, os.stat_result]] = []
    try:
        for name, info in iter_confined_directory(root, membership_root):
            budget.tick()
            if name.casefold() not in _SUMMARY_NAMES:
                continue
            candidate = relative_join(membership_root, name)
            if (
                stat_module.S_ISREG(info.st_mode)
                and not is_hidden_path(candidate)
                and not matches_any(exclude, candidate)
            ):
                candidates.append((candidate, info))
    except (FilesConfinedError, OSError, FilesTraversalError):
        return None

    candidates.sort(
        key=lambda item: (
            _SUMMARY_NAMES.index(Path(item[0]).name.casefold()), item[0]
        )
    )
    source_path = str(root / membership_root)
    metadata = {
        "summary_version": _SUMMARY_VERSION,
        "summary_documents": [
            [path, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode]
            for path, info in candidates
        ],
    }
    if catalog is not None:
        try:
            record = catalog.get_key(source.key, include_favicon=False)
        except sqlite3.Error as error:
            logger.warning(
                "%s: summary cache read failed (%s); reading source documents",
                source.key, public_cause(error),
            )
        else:
            if (
                record is not None
                and record.provider == "files"
                and record.collection_key == source.collection_key
                and record.path == source_path
            ):
                try:
                    cached_metadata = json.loads(record.provider_metadata)
                except ValueError:
                    cached_metadata = None
                if cached_metadata == metadata:
                    return record.description or None

    description = None
    complete = True
    for candidate, _info in candidates:
        if time.monotonic() >= budget.started + budget.timeout_seconds:
            complete = False
            break
        try:
            opened = open_confined(root, candidate)
            with opened.handle as handle:
                raw = handle.read(_SUMMARY_BYTES).removeprefix(b"\xef\xbb\xbf")
        except (FilesConfinedError, OSError, ValueError):
            complete = False
            continue
        if not raw or b"\x00" in raw:
            continue
        suffix = Path(candidate).suffix.casefold()
        if suffix in {".html", ".htm"}:
            text = extract_html_summary(
                raw,
                _SUMMARY_CHARS,
                deadline=budget.started + budget.timeout_seconds,
            )
        elif suffix in {".md", ".markdown"}:
            _, text, _, _ = extract_markdown_document(
                raw, _SUMMARY_CHARS, _SUMMARY_BYTES
            )
        else:
            text, _, _ = extract_plain_text_document(
                raw, _SUMMARY_CHARS, _SUMMARY_BYTES
            )
        if text:
            description = text
            break
    if (
        catalog is not None
        and complete
        and time.monotonic() < budget.started + budget.timeout_seconds
    ):
        try:
            catalog.upsert({
                "provider": "files",
                "collection_key": source.collection_key,
                "key": source.key,
                "path": source_path,
                "title": source.title,
                "description": description,
                "homepage": source.homepage,
                "provider_metadata": metadata,
            })
        except sqlite3.Error as error:
            logger.warning(
                "%s: summary cache write failed (%s); using uncached summary",
                source.key, public_cause(error),
            )
    return description


def _classify(
    root: Path,
    *,
    membership_root: str,
    loose_only: bool,
    exclude: Sequence[str],
    budget: _Budget,
    errors: list[str],
) -> tuple[tuple[str, ...], bool]:
    """Return observed content families and whether the scan was partial."""
    families: set[str] = set()
    partial = False

    def classify_file(relative: str, source_path: str) -> None:
        if is_hidden_path(relative):
            return
        if matches_any(exclude, relative):
            return
        mimetype = extension_mimetype(relative)
        prefix = b""
        if mimetype is None:
            # Only unknown or extensionless files need a bounded content probe.
            try:
                with open(source_path, "rb") as handle:
                    prefix = handle.read(4096)
            except OSError:
                prefix = b""
            mimetype = detect_mimetype(
                relative, prefix, allow_archive_probe=True
            )
        families.add(
            library_family(
                relative,
                mimetype,
                prefix,
                allow_archive_probe=True,
            )
        )

    def visit(relative_dir: str) -> None:
        nonlocal partial
        if partial:
            return
        directory = root / Path(*path_components(relative_dir)) if relative_dir else root
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as error:
            errors.append(
                f"cannot read {relative_dir or '.'}: {error.strerror or error}"
            )
            return
        for entry in entries:
            if partial:
                return
            try:
                budget.tick()
            except FilesTraversalError:
                partial = True
                return
            name = entry.name
            if is_hidden_name(name):
                continue
            relative = relative_join(relative_dir, name)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if matches_any(exclude, relative, directory=True):
                        continue
                    if not loose_only:
                        visit(relative)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            classify_file(relative, entry.path)

    if loose_only:
        try:
            with os.scandir(root) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as error:
            errors.append(f"cannot read .: {error.strerror or error}")
            return (), True
        for entry in entries:
            try:
                budget.tick()
            except FilesTraversalError:
                partial = True
                break
            if is_hidden_name(entry.name):
                continue
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            classify_file(entry.name, entry.path)
    else:
        visit(membership_root)
    return ordered_families(families), partial


def _source_info(
    config: Any,
    *,
    root: Path,
    membership_root: str,
    budget: _Budget,
    catalog: SourceCatalog | None,
    key: str,
    title: str,
    collection_key: str,
    collection_label: str,
    homepage: str | None,
    media_types: Sequence[str],
    has_icon: bool,
    web_base_path: str,
) -> SourceInfo:
    info = SourceInfo(
        key=key,
        provider="files",
        title=title,
        collection_key=collection_key,
        collection_label=collection_label,
        searchable=False,
        homepage=homepage,
        icon_url=_icon_url(key, has_icon, web_base_path),
        media_types=tuple(media_types),
    )
    description = _description(
        root, membership_root, tuple(getattr(config, "exclude", ()) or ()), budget,
        source=info, catalog=catalog,
    )
    object.__setattr__(info, "description", description)
    return info


def discover_sources(
    config: Any,
    *,
    web_base_path: str = "",
    catalog: SourceCatalog | None = None,
) -> FilesDiscovery:
    """Discover the sources of one configured files collection."""
    root = Path(os.path.realpath(Path(getattr(config, "path", "")).expanduser()))
    collection_name = str(getattr(config, "name", "files"))
    collection_key = files_collection_key(collection_name)
    label = _display_label(config)
    exclude = tuple(getattr(config, "exclude", ()) or ())
    granularity = str(getattr(config, "granularity", "collection"))
    max_visited, timeout = _traversal_limits(config)
    budget = _Budget(max_visited, timeout)
    errors: list[str] = []

    try:
        root_stat = root.stat()
    except OSError as error:
        return FilesDiscovery(
            errors=(f"files root is unavailable: {error.strerror or error}",)
        )
    if not root_stat.st_mode & 0o040000:
        return FilesDiscovery(errors=("files root is not a directory",))
    try:
        with os.scandir(root):
            pass
    except OSError as error:
        return FilesDiscovery(
            errors=(f"files root is unreadable: {error.strerror or error}",)
        )

    if granularity != "children":
        families, _partial = _classify(
            root,
            membership_root="",
            loose_only=False,
            exclude=exclude,
            budget=budget,
            errors=errors,
        )
        has_icon, _icon_path = _icon(root, "")
        info = _source_info(
            config,
            root=root,
            membership_root="",
            budget=budget,
            catalog=catalog,
            key=files_root_key(collection_name),
            title=label,
            collection_key=collection_key,
            collection_label=label,
            homepage=_homepage(root, ""),
            media_types=families,
            has_icon=has_icon,
            web_base_path=web_base_path,
        )
        return FilesDiscovery(sources=(FilesSource(info, "", False, has_icon),), errors=tuple(errors))

    # children granularity
    try:
        with os.scandir(root) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as error:
        return FilesDiscovery(
            errors=(f"files root is unreadable: {error.strerror or error}",)
        )

    max_sources = _max_sources(config)
    directories: list[str] = []
    loose_present = False
    candidates = 0
    collapsed = False
    for entry in entries:
        try:
            budget.tick()
        except FilesTraversalError:
            errors.append("files discovery traversal budget exhausted")
            break
        name = entry.name
        if is_hidden_name(name):
            continue
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if matches_any(exclude, name, directory=True):
                    continue
                directories.append(name)
                candidates += 1
            elif entry.is_file(follow_symlinks=False):
                if matches_any(exclude, name):
                    continue
                loose_present = True
                candidates += 1
        except OSError:
            continue
        if candidates > max_sources:
            collapsed = True
            break

    if collapsed:
        logger.warning(
            "zimantic: files collection %s has more than %d sources; "
            "collapsing it to one root source (keys change; rebuild required)",
            label,
            max_sources,
        )
        families, _partial = _classify(
            root,
            membership_root="",
            loose_only=False,
            exclude=exclude,
            budget=budget,
            errors=errors,
        )
        has_icon, _icon_path = _icon(root, "")
        info = _source_info(
            config,
            root=root,
            membership_root="",
            budget=budget,
            catalog=catalog,
            key=files_collapsed_key(collection_name),
            title=label,
            collection_key=collection_key,
            collection_label=label,
            homepage=_homepage(root, ""),
            media_types=families,
            has_icon=has_icon,
            web_base_path=web_base_path,
        )
        return FilesDiscovery(
            sources=(FilesSource(info, "", False, has_icon),),
            errors=tuple(errors),
            collapsed=True,
        )

    sources: list[FilesSource] = []
    seen_keys: dict[str, str] = {}
    for relative in directories:
        key = files_dir_key(collection_name, relative)
        other = seen_keys.get(key)
        if other is not None and other != relative:
            errors.append(
                f"source key collision between {other!r} and {relative!r}"
            )
            continue
        seen_keys[key] = relative
        families, _partial = _classify(
            root,
            membership_root=relative,
            loose_only=False,
            exclude=exclude,
            budget=budget,
            errors=errors,
        )
        has_icon, _icon_path = _icon(root, relative)
        info = _source_info(
            config,
            root=root,
            membership_root=relative,
            budget=budget,
            catalog=catalog,
            key=key,
            title=relative,
            collection_key=collection_key,
            collection_label=label,
            homepage=_homepage(root, relative),
            media_types=families,
            has_icon=has_icon,
            web_base_path=web_base_path,
        )
        sources.append(FilesSource(info, relative, False, has_icon))

    if loose_present:
        key = files_loose_key(collection_name)
        families, _partial = _classify(
            root,
            membership_root="",
            loose_only=True,
            exclude=exclude,
            budget=budget,
            errors=errors,
        )
        has_icon, _icon_path = _icon(root, "")
        info = _source_info(
            config,
            root=root,
            membership_root="",
            budget=budget,
            catalog=catalog,
            key=key,
            title=f"{label} (files)",
            collection_key=collection_key,
            collection_label=label,
            homepage=_homepage(root, ""),
            media_types=families,
            has_icon=has_icon,
            web_base_path=web_base_path,
        )
        sources.append(FilesSource(info, "", True, has_icon))

    sources.sort(key=lambda item: ((item.info.title or "").casefold(), item.key))
    return FilesDiscovery(sources=tuple(sources), errors=tuple(errors))


# ---------------------------------------------------------------------------
# Index lookup
# ---------------------------------------------------------------------------


@dataclass
class FilesIndexOptions:
    semantic: bool = False
    defer_faiss: bool = False
    model_fingerprint: str = ""
    cfg: Mapping[str, Any] = field(default_factory=dict)
    probe_compatible: Callable[[sqlite3.Connection, Path], tuple[bool, str]] | None = None


def _index_uri(db_path: Path) -> str:
    return db_path.resolve().as_uri() + "?mode=ro&immutable=1"


def apply_index_metadata(source: SourceInfo, meta: Mapping[str, str]) -> None:
    """Populate fields from already validated published-index metadata."""
    object.__setattr__(
        source, "indexed_count", schema.decode_int(meta[schema.INDEXED_COUNT])
    )
    object.__setattr__(
        source, "truncated", schema.decode_bool(meta[schema.TRUNCATED])
    )
    object.__setattr__(
        source, "index_partial",
        schema.decode_json(meta[schema.BUILD_SCOPE])["partial"]
        if schema.BUILD_SCOPE in meta else False,
    )
    if schema.BODY_TRUNCATED_COUNT in meta:
        object.__setattr__(
            source, "body_truncated_count",
            schema.decode_int(meta[schema.BODY_TRUNCATED_COUNT]),
        )
    if schema.DERIVED_TEXT in meta:
        object.__setattr__(source, "derived_text", schema.decode_json(meta[schema.DERIVED_TEXT]))
    object.__setattr__(source, "index_freshness", "unknown")


def open_files_index(
    source: SourceInfo,
    *,
    index_dir: Path | str,
    options: FilesIndexOptions,
) -> IndexHandle | None:
    """Open one validated, published files index generation."""
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
    manifest = lease.manifest or {}
    if manifest.get("provider") != source.provider or manifest.get(
        "source_key"
    ) != source.key:
        logger.warning("%s: index manifest identity does not match the source", source.key)
        lease.close()
        return None
    db_path = lease.sqlite_path
    try:
        db = sqlite3.connect(_index_uri(db_path), uri=True, check_same_thread=False)
    except sqlite3.Error:
        lease.close()
        return None
    try:
        meta = schema.read_index_meta(db, completed=True)
        if (
            meta[schema.PROVIDER] != source.provider
            or meta[schema.COLLECTION_KEY] != source.collection_key
            or meta[schema.SOURCE_KEY] != source.key
        ):
            raise schema.SchemaError("index metadata identity does not match the source")
        done = meta[schema.DONE]
        faiss_index = None
        faiss_path = lease.faiss_path
        if (
            options.semantic
            and not options.defer_faiss
            and done == "full"
            and faiss_path is not None
        ):
            stored_fingerprint = str(meta.get("model_fingerprint", ""))
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
                        "%s: vectors were built with a different model file, but "
                        "embedding probes match (%s); using them",
                        source.key,
                        detail,
                    )
                    model_changed = False
                else:
                    logger.warning(
                        "%s: vectors were built with a different model; falling "
                        "back to lexical search (%s)",
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
                        faiss_index.nprobe = _nprobe(faiss_index, dict(options.cfg))
                except Exception as error:
                    logger.warning(
                        "%s: vectors unavailable (%s); falling back to lexical search",
                        source.key,
                        public_cause(error),
                    )
                    logger.debug("%s: vector open error: %s", source.key, error)
                    faiss_index = None
        db.close()
        return IndexHandle(
            None,
            faiss_index,
            None,
            None,
            threading.Lock(),
            db_path=_index_uri(db_path),
            db_signature=_file_signature(db_path),
            done=done,
            source_size=None,
            lease=lease,
            generation=lease.generation,
            index_meta=meta,
        )
    except (sqlite3.Error, schema.SchemaError) as error:
        logger.warning(
            "%s: index unavailable (%s); rebuild it",
            source.key, public_cause(error),
        )
        db.close()
        lease.close()
        return None


__all__ = [
    "COLLAPSED_IDENTIFIER",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_SOURCES",
    "LOOSE_IDENTIFIER",
    "ROOT_IDENTIFIER",
    "FilesDiscovery",
    "FilesIndexOptions",
    "FilesSource",
    "FilesTraversalError",
    "apply_index_metadata",
    "discover_sources",
    "files_collapsed_key",
    "files_collection_key",
    "files_dir_key",
    "files_loose_key",
    "files_root_key",
    "files_source_key",
    "open_files_index",
]
