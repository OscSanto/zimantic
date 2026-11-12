"""Bounded :class:`EntryProducer` for one Kolibri source.

Each available topic with locally usable children becomes one result row.
Child items become redirect rows whose title and extracted text point back to
that topic, so searching a child title returns the topic.  Row ids are a
deterministic positive 63-bit mapping of ``(channel_id, node_id)``; collisions
fail the build rather than merging documents.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ...content.reader import (
    DEFAULT_MAX_BODY_TEXT_BYTES,
    DEFAULT_MAX_ENTRY_BYTES,
    DEFAULT_PREVIEW_CHARS,
    extract_body,
    extract_excerpt,
    extract_pdf_excerpt,
    extract_plain_text_document,
    truncate_at_word_boundary,
    truncate_utf8,
)
from ...indexing.types import MAX_ROW_ID, EntryRecord
from ..files.common import FilesConfinedError, is_ocr_candidate, open_confined
from .captions import associated_captions
from .discovery import (
    ChannelRow,
    KolibriSource,
    KolibriTraversalError,
    NodeRow,
    SourceScope,
    candidate_nodes,
    load_files_for_nodes,
    load_topics,
    title_key,
)
from .html5 import Html5Archive, Html5LimitError, Html5ZipError
from .metadata import BATCH_SIZE, MetadataReader
from .schema import (
    EXERCISE_KIND,
    HTML5_PRESETS,
    SUPPORTED_HTML5_PRESETS,
    TOPIC_KIND,
    FileRow,
    FileSelection,
    media_family,
    select_primary,
    storage_relative_path,
)

logger = logging.getLogger(__name__)


def row_id_for(channel_id: str, node_id: str) -> int:
    """Deterministic positive 63-bit canonical row id."""
    digest = hashlib.sha256(
        f"{channel_id}\x00{node_id}".encode("utf-8", "surrogatepass")
    ).digest()
    value = int.from_bytes(digest[:8], "big") & MAX_ROW_ID
    return value or 1


def _selection_paths(selection: FileSelection) -> tuple[str, ...]:
    files = (
        ([selection.primary] if selection.primary is not None else [])
        + list(associated_captions(selection))
    )
    return tuple(storage_relative_path(file) for file in files)


def _int_setting(cfg: Mapping[str, Any] | None, key: str, default: int) -> int:
    value = (cfg or {}).get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return int(default)
    return int(value)


def _operations_state(operations: Any) -> dict[str, str]:
    if operations is None:
        return {"captions": "off", "ocr": "off", "asr": "off"}
    captions = "index" if getattr(operations, "captions", False) else "off"
    return {
        "captions": captions,
        "ocr": str(getattr(operations, "ocr", None) or "off"),
        "asr": str(getattr(operations, "asr", None) or "off"),
    }


def _derived_config_payload(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "__dataclass_fields__"):
        return {name: getattr(value, name) for name in value.__dataclass_fields__}
    if isinstance(value, Mapping):
        return dict(value)
    return str(value)


@dataclass(frozen=True)
class _ManifestItem:
    node: NodeRow
    selection: FileSelection
    family: str | None
    primary_size: int | None
    primary_mtime: int | None
    caption_signatures: tuple[tuple[str, int | None, int | None], ...] = ()
    metadata_text: str = ""
    target_node_id: str | None = None
    path_members: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Snapshot:
    items: tuple[_ManifestItem, ...]
    truncated: bool
    fingerprint: str
    errors: tuple[str, ...]


class _BoundedReader:
    def __init__(self, handle: Any, limit: int) -> None:
        self._handle = handle
        self._limit = max(1, int(limit))
        self._total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._limit + 1 - self._total
        if remaining <= 0:
            raise ValueError("derived input exceeds the provider file limit")
        if size is None or size < 0:
            size = remaining
        else:
            size = min(int(size), remaining)
        data = self._handle.read(size)
        self._total += len(data)
        if self._total > self._limit:
            raise ValueError("derived input exceeds the provider file limit")
        return data

    def close(self) -> None:
        close = getattr(self._handle, "close", None)
        if callable(close):
            close()


class _DerivedInput:
    def __init__(
        self,
        root: Path,
        relative: str,
        kind: str,
        mimetype: str,
        size: int,
        limit: int,
    ) -> None:
        self.root = root
        self.relative = relative
        self.kind = kind
        self.mimetype = mimetype
        self.size = size
        self.limit = limit

    def open(self) -> Any:
        if self.size > self.limit:
            raise ValueError("derived input exceeds the provider file limit")
        try:
            opened = open_confined(self.root, self.relative)
        except FilesConfinedError as error:
            raise OSError("derived input is unavailable") from error
        return _BoundedReader(opened.handle, self.limit)


class KolibriEntryProducer:
    """Bounded sequence of indexable rows from one Kolibri source."""

    provider = "kolibri"

    def __init__(
        self,
        source: KolibriSource | Any,
        snapshot: Any,
        *,
        channel: ChannelRow | None = None,
        scope: SourceScope | None = None,
        storage_root: Path | str,
        config: Any = None,
        fast: bool = False,
        cfg: Mapping[str, Any] | None = None,
        snapshot_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.info = getattr(source, "info", source)
        self.source = self.info
        self.key = str(getattr(self.info, "key", ""))
        self.collection_key = str(getattr(self.info, "collection_key", ""))
        self.provider = str(getattr(self.info, "provider", self.provider))
        self.provider_id = getattr(self.info, "provider_id", None)
        self.channel = channel or getattr(source, "channel", None)
        self.scope = scope or getattr(source, "scope", None)
        if self.channel is None or self.scope is None:
            raise ValueError("Kolibri producer requires channel and source scope")
        self.snapshot = snapshot
        self._snapshot_factory = snapshot_factory
        self.storage_root = Path(os.path.realpath(Path(storage_root)))
        self.config = config
        self.fast = bool(fast)
        from ...scoped_settings import resolve_settings

        self.cfg = resolve_settings(cfg or {}, source, config)
        self.max_entries = _int_setting(self.cfg, "max_entries", 500_000)
        self.global_max_entry_bytes = _int_setting(
            self.cfg, "max_entry_bytes", DEFAULT_MAX_ENTRY_BYTES
        )
        self.max_html_bytes = _int_setting(
            self.cfg, "max_html_bytes", 4 << 20
        )
        self.max_preview_chars = _int_setting(
            self.cfg, "max_preview_chars", DEFAULT_PREVIEW_CHARS
        )
        self.max_body_text_bytes = _int_setting(
            self.cfg, "max_body_text_bytes", DEFAULT_MAX_BODY_TEXT_BYTES
        )
        traversal = getattr(config, "traversal", None)
        self.max_visited = int(
            getattr(traversal, "max_visited_entries", 1_000_000)
        )
        self.traversal_timeout = float(
            getattr(traversal, "traversal_timeout_seconds", 300)
        )
        from ...indexing.derived import source_derived_config, source_operations

        operations = source_operations(self.cfg, self.info)
        self.operations = operations
        self.derived_config = source_derived_config(self.cfg, self.info)
        self.captions_enabled = bool(getattr(operations, "captions", False))
        self.ocr_enabled = bool(getattr(operations, "ocr", None))
        self.asr_enabled = bool(getattr(operations, "asr", None))
        engines = self.cfg.get("derived_engines") or {}
        self.embedded_captions_available = callable(
            engines.get("ffmpeg") if hasattr(engines, "get") else None
        )
        self._closed = False
        snapshot_value = self._build_snapshot(self.snapshot)
        self._items = snapshot_value.items
        self.count = len(self._items)
        self.truncated = snapshot_value.truncated
        self.fingerprint = snapshot_value.fingerprint
        self.errors = snapshot_value.errors
        self._row_ids: list[int] = []
        seen: dict[int, tuple[str, str]] = {}
        for item in self._items:
            row_id = row_id_for(self.channel.id, item.node.id)
            other = seen.get(row_id)
            if other is not None and other != (self.channel.id, item.node.id):
                raise RuntimeError(
                    "Kolibri row-id collision for "
                    f"{other[0]}/{other[1]} and {self.channel.id}/{item.node.id}"
                )
            seen[row_id] = (self.channel.id, item.node.id)
            self._row_ids.append(row_id)
        self.source_size = sum(
            item.primary_size or 0 for item in self._items
        )
        self.source_mtime = max(
            (item.primary_mtime for item in self._items if item.primary_mtime is not None),
            default=None,
        )
        for message in self.errors:
            logger.warning("zimantic: %s: %s", self.key, message)

    # -- manifest ----------------------------------------------------------

    def _storage_signature(self, file: FileRow | None) -> tuple[int | None, int | None]:
        if file is None:
            return None, None
        try:
            relative = storage_relative_path(file)
            opened = open_confined(self.storage_root, relative)
        except (ValueError, FilesConfinedError):
            return file.file_size, None
        try:
            info = opened.stat
            return int(info.st_size), round(info.st_mtime_ns / 1_000_000_000)
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()

    def _build_snapshot(self, snapshot: Any) -> _Snapshot:
        db = snapshot.connect()
        started = time.monotonic()
        try:
            nodes, truncated = candidate_nodes(
                db, self.channel, self.scope, limit=self.max_entries
            )
            if len(nodes) > self.max_visited:
                raise KolibriTraversalError(
                    f"Kolibri traversal exceeded the {self.max_visited}-entry limit"
                )
            if time.monotonic() - started > self.traversal_timeout:
                raise KolibriTraversalError(
                    "Kolibri traversal exceeded its "
                    f"{self.traversal_timeout:g}-second limit"
                )
            files = load_files_for_nodes(db, (node.id for node in nodes))
            grouped: dict[str, list[_ManifestItem]] = {}
            parent_order: list[str] = []
            errors: list[str] = []
            for node in nodes:
                if not node.parent_id:
                    continue
                if node.parent_id not in grouped:
                    grouped[node.parent_id] = []
                    parent_order.append(node.parent_id)
                selection = select_primary(
                    files.get(node.id, ()), node.kind, node.lang_id
                )
                if selection.ambiguous:
                    errors.append(
                        f"content node {node.id!r} has an ambiguous primary "
                        "representation; it is not indexed"
                    )
                    continue
                if selection.primary is None and not (
                    node.kind == EXERCISE_KIND and node.description.strip()
                ):
                    continue
                primary = selection.primary
                if (
                    primary is not None
                    and primary.preset in HTML5_PRESETS
                    and primary.preset not in SUPPORTED_HTML5_PRESETS
                ):
                    errors.append(
                        f"content node {node.id!r} uses unsupported HTML5 preset "
                        f"{primary.preset!r}; metadata is indexed but the bundle "
                        "cannot be served"
                    )
                family = (
                    media_family(primary.extension, node.kind)
                    if primary is not None
                    else "text"
                )
                size, mtime = self._storage_signature(primary)
                captions = associated_captions(selection) if self.captions_enabled else ()
                caption_signatures = tuple(
                    (caption.local_file_id, *self._storage_signature(caption))
                    for caption in captions
                )
                grouped[node.parent_id].append(
                    _ManifestItem(
                        node,
                        selection,
                        family,
                        size,
                        mtime,
                        caption_signatures,
                        path_members=_selection_paths(selection),
                    )
                )

            topics = load_topics(db, self.channel.id, parent_order)
            items: list[_ManifestItem] = []
            row_limit_hit = False
            for parent_id in parent_order:
                topic = topics.get(parent_id)
                if topic is None or not topic.available or topic.coach_content:
                    continue
                children = grouped.get(parent_id, ())
                if len(items) >= self.max_entries:
                    row_limit_hit = True
                    break
                path_members = tuple(
                    dict.fromkeys(
                        path
                        for child in children
                        for path in child.path_members
                    )
                )
                items.append(
                    _ManifestItem(
                        topic,
                        FileSelection(None),
                        "text",
                        None,
                        None,
                        path_members=path_members,
                    )
                )
                # Child rows are searchable aliases, not separate results.
                topic_title = title_key(topic.title)
                for child in children:
                    if topic_title and title_key(child.node.title) == topic_title:
                        continue
                    if len(items) >= self.max_entries:
                        row_limit_hit = True
                        break
                    items.append(
                        replace(child, target_node_id=topic.id)
                    )
                if row_limit_hit:
                    break
            truncated = truncated or row_limit_hit
            if not self.fast:
                reader = MetadataReader(db, self.channel)
                db.set_progress_handler(
                    lambda: int(time.monotonic() - started > self.traversal_timeout),
                    1000,
                )
                for start in range(0, len(items), BATCH_SIZE):
                    if time.monotonic() - started > self.traversal_timeout:
                        raise KolibriTraversalError(
                            "Kolibri traversal exceeded its "
                            f"{self.traversal_timeout:g}-second limit"
                        )
                    chunk = items[start : start + BATCH_SIZE]
                    metadata = reader.load(
                        self.scope,
                        (item.node.id for item in chunk),
                        include_position=False,
                        include_topics=True,
                    )
                    for offset, item in enumerate(chunk, start):
                        details = metadata.get(item.node.id)
                        if details is not None:
                            items[offset] = replace(
                                item, metadata_text=details.indexing_text()
                            )
        except sqlite3.OperationalError as error:
            if time.monotonic() - started > self.traversal_timeout:
                raise KolibriTraversalError(
                    "Kolibri traversal exceeded its "
                    f"{self.traversal_timeout:g}-second limit"
                ) from error
            raise
        finally:
            db.close()
        return _Snapshot(
            items=tuple(items),
            truncated=truncated,
            fingerprint=self._fingerprint(items, truncated),
            errors=tuple(errors),
        )

    def _fingerprint(
        self, items: Sequence[_ManifestItem], truncated: bool
    ) -> str:
        payload = {
            "provider": self.provider,
            "key": self.key,
            "collection_key": self.collection_key,
            "channel": {
                "id": self.channel.id,
                "name": self.channel.name,
                "version": self.channel.version,
                "root_id": self.channel.root_id,
            },
            "scope": {
                "kind": self.scope.kind,
                "node_id": self.scope.node_id,
                "parent_id": self.scope.parent_id,
                "tree_id": self.scope.tree_id,
                "lft": self.scope.lft,
                "rght": self.scope.rght,
            },
            "max_entries": self.max_entries,
            "fast": self.fast,
            "max_entry_bytes": self.global_max_entry_bytes,
            "max_html_bytes": self.max_html_bytes,
            "max_preview_chars": self.max_preview_chars,
            "max_body_text_bytes": self.max_body_text_bytes,
            "truncated": truncated,
            "items": [
                {
                    "id": item.node.id,
                    "parent_id": item.node.parent_id,
                    "tree_id": item.node.tree_id,
                    "lft": item.node.lft,
                    "rght": item.node.rght,
                    "title": item.node.title,
                    "kind": item.node.kind,
                    "description": item.node.description,
                    "metadata_text": item.metadata_text,
                    "available": item.node.available,
                    "coach_content": item.node.coach_content,
                    "target_node_id": item.target_node_id,
                    "family": item.family,
                    "primary": (
                        None
                        if item.selection.primary is None
                        else {
                            "local_file_id": item.selection.primary.local_file_id,
                            "extension": item.selection.primary.extension,
                            "preset": item.selection.primary.preset,
                            "priority": item.selection.primary.priority,
                            "size": item.primary_size,
                            "mtime": item.primary_mtime,
                        }
                    ),
                    "captions": list(item.caption_signatures),
                }
                for item in items
            ],
            "operations": _operations_state(self.operations),
            "derived_text": _derived_config_payload(self.derived_config),
            "engines": sorted(
                str(name) for name in (self.cfg.get("derived_engines") or {})
            ),
        }
        encoded = json.dumps(
            payload, sort_keys=True, default=str, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    # -- native text -------------------------------------------------------

    @property
    def path_scope_root(self) -> Path:
        return self.storage_root

    @property
    def path_scope_membership(self) -> str:
        return ""

    @property
    def path_scope_aliases(self) -> tuple[Path, ...]:
        if self.config is None:
            return ()
        home = Path(self.config.path).expanduser().resolve()
        database = Path(self.config.database_path).expanduser().resolve() if self.config.database_path else home / "db.sqlite3"
        return (home, database)

    def entry_paths(self, index: int) -> tuple[str, ...]:
        item = self._items[index]
        if item.path_members:
            return item.path_members
        return _selection_paths(item.selection)

    def _read_native_document(
        self, primary: FileRow | None
    ) -> tuple[str, str, bool]:
        """Return ``(excerpt, body, truncated)`` for a readable document."""
        if primary is None:
            return "", "", False
        if primary.preset in HTML5_PRESETS:
            if primary.preset not in SUPPORTED_HTML5_PRESETS:
                return "", "", False
            try:
                archive = Html5Archive(
                    self.storage_root,
                    storage_relative_path(primary),
                    max_entries=int(
                        getattr(self.config, "max_html5_zip_entries", 10_000)
                    ),
                    max_directory_bytes=int(
                        getattr(
                            self.config,
                            "max_html5_zip_directory_bytes",
                            8_388_608,
                        )
                    ),
                    max_member_bytes=int(
                        getattr(
                            self.config,
                            "max_html5_zip_member_bytes",
                            268_435_456,
                        )
                    ),
                    max_total_bytes=int(
                        getattr(
                            self.config,
                            "max_html5_zip_total_bytes",
                            1_073_741_824,
                        )
                    ),
                    timeout_seconds=float(
                        getattr(self.config, "html5_zip_timeout_seconds", 10)
                    ),
                )
                content, _total, _truncated, _mime = archive.read(
                    archive.index_path,
                    head_only=False,
                    max_bytes=min(
                        self.max_html_bytes, self.global_max_entry_bytes
                    ),
                )
            except (Html5ZipError, Html5LimitError, OSError, ValueError):
                return "", "", False
            excerpt = extract_excerpt(content, self.max_preview_chars)
            body, body_truncated = extract_body(
                content, self.max_body_text_bytes
            )
            return excerpt, body, body_truncated
        extension = "." + str(primary.extension or "").casefold().lstrip(".")
        if extension not in {".html", ".htm", ".xhtml", ".xht", ".pdf", ".txt"}:
            return "", "", False
        try:
            relative = storage_relative_path(primary)
            opened = open_confined(self.storage_root, relative)
        except (ValueError, FilesConfinedError):
            return "", "", False
        try:
            if extension in {".html", ".htm", ".xhtml", ".xht"}:
                raw = opened.handle.read(self.max_html_bytes)
                input_truncated = int(opened.stat.st_size) > len(raw)
                excerpt = extract_excerpt(raw, self.max_preview_chars)
                body, truncated = extract_body(
                    raw,
                    self.max_body_text_bytes,
                    input_truncated=input_truncated,
                )
                return excerpt, body, truncated
            raw = opened.handle.read(self.global_max_entry_bytes + 1)
        except OSError:
            return "", "", False
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()
        if len(raw) > self.global_max_entry_bytes:
            return "", "", False
        if extension == ".pdf":
            excerpt = extract_pdf_excerpt(raw, self.max_preview_chars)
            body_text = extract_pdf_excerpt(raw, self.max_body_text_bytes)
            body, truncated = truncate_utf8(body_text, self.max_body_text_bytes)
            return excerpt, body, truncated
        excerpt, body, truncated = extract_plain_text_document(
            raw, self.max_preview_chars, self.max_body_text_bytes
        )
        return excerpt, body, truncated

    def _record_for(self, index: int, item: _ManifestItem) -> EntryRecord | None:
        row_id = self._row_ids[index]
        node = item.node
        # Match the content store's reversible component encoder.
        from .discovery import kolibri_node_path

        target_node_id = item.target_node_id
        target_id = (
            row_id_for(self.channel.id, target_node_id)
            if target_node_id is not None
            else None
        )
        path = kolibri_node_path(target_node_id or node.id)
        title = node.title or "Untitled"
        if node.kind == TOPIC_KIND:
            if self.fast:
                return EntryRecord(row_id, title, "", path, None, "", False)
            description = item.metadata_text or str(node.description or "").strip()
            body, truncated = truncate_utf8(
                description, self.max_body_text_bytes
            )
            excerpt = truncate_at_word_boundary(
                description, self.max_preview_chars
            )
            return EntryRecord(
                row_id, title, excerpt, path, None, body, truncated
            )
        if self.fast:
            return EntryRecord(row_id, title, "", path, target_id, "", False)
        description = item.metadata_text or str(node.description or "").strip()
        if item.selection.primary is None:
            if node.kind == EXERCISE_KIND and description:
                body, truncated = truncate_utf8(
                    description, self.max_body_text_bytes
                )
                excerpt = truncate_at_word_boundary(
                    description, self.max_preview_chars
                )
                return EntryRecord(
                    row_id, title, excerpt, path, target_id, body, truncated
                )
            return EntryRecord(row_id, title, "", path, target_id, "", False)
        excerpt, body_text, body_truncated = self._read_native_document(
            item.selection.primary
        )
        pieces_excerpt = [piece for piece in (description, excerpt) if piece]
        pieces_body = [piece for piece in (description, body_text) if piece]
        excerpt_text = truncate_at_word_boundary(
            "\n\n".join(pieces_excerpt), self.max_preview_chars
        )
        combined, truncated = truncate_utf8(
            "\n\n".join(pieces_body), self.max_body_text_bytes
        )
        truncated = truncated or body_truncated
        if not excerpt_text and not combined:
            # Media and HTML5 bundles still need a searchable/title result.
            return EntryRecord(
                row_id, title, "", path, target_id, "", False
            )
        return EntryRecord(
            row_id, title, excerpt_text, path, target_id, combined, truncated
        )

    # -- producer contract -------------------------------------------------

    def entry_at(self, index: int) -> EntryRecord | None:
        if not isinstance(index, int) or index < 0 or index >= self.count:
            return None
        return self._record_for(index, self._items[index])

    def modified_at(self, index: int) -> int | None:
        if not isinstance(index, int) or index < 0 or index >= self.count:
            return None
        return self._items[index].primary_mtime

    def validate_snapshot(self) -> bool:
        snapshot = None
        close_snapshot = False
        try:
            if self._snapshot_factory is not None:
                snapshot = self._snapshot_factory()
                close_snapshot = True
            else:
                snapshot = self.snapshot
            fresh = self._build_snapshot(snapshot)
        except Exception as error:
            raise RuntimeError(
                f"cannot validate Kolibri source {self.key}: {error}"
            ) from error
        finally:
            if close_snapshot and snapshot is not None:
                try:
                    snapshot.close()
                except Exception:
                    pass
        return fresh.fingerprint == self.fingerprint

    def close(self) -> None:
        self._closed = True

    # -- derived text ------------------------------------------------------

    def derived_text_inputs(self, index: int) -> tuple[_DerivedInput, ...]:
        if not isinstance(index, int) or index < 0 or index >= self.count:
            return ()
        if not (self.captions_enabled or self.ocr_enabled or self.asr_enabled):
            return ()
        item = self._items[index]
        primary = item.selection.primary
        inputs: list[_DerivedInput] = []
        if self.captions_enabled:
            for caption in associated_captions(item.selection):
                size = caption.file_size or 0
                inputs.append(
                    _DerivedInput(
                        self.storage_root,
                        storage_relative_path(caption),
                        "captions",
                        caption.mimetype,
                        int(size),
                        self.global_max_entry_bytes,
                    )
                )
        if self.ocr_enabled and primary is not None:
            extension = "." + str(primary.extension or "").casefold().lstrip(".")
            family = media_family(primary.extension, item.node.kind)
            ocr_mimetype = primary.mimetype or (
                "application/pdf" if extension == ".pdf" else ""
            )
            if is_ocr_candidate(ocr_mimetype, family):
                size = primary.file_size or int(item.primary_size or 0)
                inputs.append(
                    _DerivedInput(
                        self.storage_root,
                        storage_relative_path(primary),
                        "image",
                        primary.mimetype,
                        int(size),
                        self.global_max_entry_bytes,
                    )
                )
        if (
            primary is not None
            and item.family in {"audio", "video"}
            and (
                self.asr_enabled
                or (self.captions_enabled and self.embedded_captions_available)
            )
        ):
            size = primary.file_size or int(item.primary_size or 0)
            inputs.append(
                _DerivedInput(
                    self.storage_root,
                    storage_relative_path(primary),
                    str(item.family),
                    primary.mimetype,
                    int(size),
                    self.global_max_entry_bytes,
                )
            )
        return tuple(inputs)


__all__ = ["KolibriEntryProducer", "row_id_for"]
