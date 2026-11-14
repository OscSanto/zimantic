"""Bounded, resumable :class:`EntryProducer` for one files source.

The manifest is a deterministic depth-first walk of the source's membership
root.  Candidate paths are assigned one-based ids; media and documents that
cannot be indexed still consume their manifest position so a resume never shifts
row ids.  Discovery uses ``stat`` only.  Media bytes are opened only when an
enabled derived-text operation asks for them.
"""

from __future__ import annotations

import hashlib
import html as html_module
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ...content.reader import (
    DEFAULT_MAX_BODY_TEXT_BYTES,
    DEFAULT_MAX_ENTRY_BYTES,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_PREVIEW_CHARS,
    disambiguation_title,
    extract_body,
    extract_excerpt,
    extract_markdown_document,
    extract_pdf_excerpt,
    extract_plain_text_document,
    is_disambiguation,
    truncate_at_word_boundary,
    truncate_utf8,
)
from ...content.serving import is_inline_mimetype
from ...indexing.types import EntryRecord
from .common import (
    FilesConfinedError,
    extension_mimetype,
    is_hidden_name,
    is_native_document_path,
    is_ocr_candidate,
    is_track_sidecar,
    list_directory,
    matches_any,
    media_family_for_mimetype,
    media_family_for_path,
    mimetype_for,
    normalize_relative_path,
    open_confined,
    relative_join,
)
from .discovery import (
    FilesTraversalError,
    _Budget,
    _max_entries,
    _max_file_bytes,
    _traversal_limits,
)

logger = logging.getLogger(__name__)

_TITLE_RE = re.compile(rb"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(
    rb"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(rb"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    rb"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_HEAD_RE = re.compile(rb"<head\b[^>]*>.*?</head>", re.IGNORECASE | re.DOTALL)
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*(.+?)[ \t]*$")


@dataclass(frozen=True)
class _Sidecar:
    path: str
    name: str
    size: int
    mtime: int
    mimetype: str


@dataclass(frozen=True)
class _ManifestItem:
    path: str
    name: str
    size: int
    mtime: int
    family: str | None
    mimetype: str
    sidecars: tuple[_Sidecar, ...] = ()


@dataclass(frozen=True)
class _Snapshot:
    items: tuple[_ManifestItem, ...]
    truncated: bool
    fingerprint: str


_PREFIX_PROBE_BYTES = 4096


def _item_mimetype(root: Path, relative: str, size: int | None = None) -> str:
    """Serving MIME for one manifest item, probing unknown suffixes."""

    mapped = extension_mimetype(relative)
    if mapped and is_inline_mimetype(mapped):
        return mapped
    prefix = b""
    try:
        opened = open_confined(root, relative)
    except (FilesConfinedError, OSError):
        opened = None
    if opened is not None:
        try:
            prefix = opened.handle.read(_PREFIX_PROBE_BYTES)
        except OSError:
            prefix = b""
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()
    return mimetype_for(relative, prefix, size)


def _int_setting(cfg: Mapping[str, Any] | None, key: str, default: int) -> int:
    value = (cfg or {}).get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return int(default)
    return int(value)


def _operations_state(operations: Any) -> dict[str, str]:
    if operations is None:
        return {"captions": "off", "ocr": "off", "asr": "off"}
    captions = "index" if getattr(operations, "captions", False) else "off"
    ocr = str(getattr(operations, "ocr", None) or "off")
    asr = str(getattr(operations, "asr", None) or "off")
    return {"captions": captions, "ocr": ocr, "asr": asr}


def _derived_config_payload(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: getattr(value, name)
            for name in value.__dataclass_fields__
        }
    if isinstance(value, Mapping):
        return dict(value)
    return str(value)


def _strip_markup(fragment: bytes) -> str:
    text = _TAG_RE.sub(b" ", fragment).decode("utf-8", "ignore")
    return " ".join(html_module.unescape(text).split())


def _html_title(raw: bytes) -> str:
    match = _TITLE_RE.search(raw)
    if not match:
        return ""
    return _strip_markup(match.group(1))


def _html_first_heading(raw: bytes) -> str:
    match = _HEADING_RE.search(raw)
    if not match:
        return ""
    return _strip_markup(match.group(1))


def _html_plain_text(raw: bytes) -> str:
    """Fallback for short pages that the block extractor deliberately ignores."""
    text = _SCRIPT_STYLE_RE.sub(b" ", raw)
    text = _HEAD_RE.sub(b" ", text)
    text = _TAG_RE.sub(b" ", text)
    return " ".join(html_module.unescape(text.decode("utf-8", "ignore")).split())


def _markdown_first_heading(raw: bytes) -> str:
    text = raw.decode("utf-8", "ignore")
    match = _MARKDOWN_HEADING_RE.search(text)
    return " ".join(match.group(1).split()) if match else ""


def _clean_title(value: str) -> str:
    """Collapse whitespace and drop layout control characters from a title."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return " ".join(text.split())


MAX_PDF_TITLE_CHARS = 160


def _pdf_metadata_title(raw: bytes) -> str:
    try:
        import pymupdf

        document = pymupdf.open(stream=raw, filetype="pdf")
        try:
            metadata = document.metadata or {}
            title = _clean_title(metadata.get("title") or "")
            return title
        finally:
            document.close()
    except Exception:
        return ""


def _first_text_line(raw: bytes) -> str:
    text = raw.decode("utf-8", "ignore")
    for line in text.splitlines():
        normalized = _clean_title(line)
        if normalized:
            return normalized[:200]
    return ""


def _filename_title(name: str) -> str:
    stem = Path(name).stem.strip()
    return stem or name


class _BoundedReader:
    """A file-like wrapper that enforces the provider's effective byte cap."""

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
    """One provider-confined derived-text input."""

    def __init__(
        self,
        root: Path,
        path: str,
        kind: str,
        mimetype: str,
        size: int,
        limit: int,
    ) -> None:
        self.root = root
        self.path = path
        self.kind = kind
        self.mimetype = mimetype
        self.size = size
        self.limit = limit

    def open(self) -> Any:
        if self.size > self.limit:
            raise ValueError("derived input exceeds the provider file limit")
        try:
            opened = open_confined(self.root, self.path)
        except FilesConfinedError as error:
            raise OSError("derived input is unavailable") from error
        return _BoundedReader(opened.handle, self.limit)


class FilesEntryProducer:
    """Bounded sequence of indexable rows from one files source."""

    provider = "files"

    def __init__(
        self,
        source: Any,
        root: Path | str,
        *,
        membership_root: str = "",
        loose_only: bool = False,
        config: Any,
        fast: bool = False,
        cfg: Mapping[str, Any] | None = None,
    ) -> None:
        self.source = source
        self.key = str(getattr(source, "key", ""))
        self.collection_key = str(getattr(source, "collection_key", ""))
        self.provider = str(getattr(source, "provider", self.provider))
        self.provider_id = getattr(source, "provider_id", None)
        self.root = Path(root)
        self.membership_root = (
            normalize_relative_path(membership_root) if membership_root else ""
        )
        self.loose_only = bool(loose_only)
        self.config = config
        self.fast = bool(fast)
        from ...scoped_settings import resolve_settings

        self.cfg = resolve_settings(cfg or {}, source, config)
        self.include = tuple(getattr(config, "include", ()) or ())
        self.index_include = tuple(self.cfg.get("index_include", ()) or ())
        if self.index_include:
            self.include = self.index_include
        self.exclude = (*tuple(getattr(config, "exclude", ()) or ()), *self.cfg.get("index_exclude", ()))
        self.max_entries = _int_setting(self.cfg, "max_entries", _max_entries(config))
        self.max_file_bytes = _max_file_bytes(config)
        self.global_max_entry_bytes = _int_setting(
            self.cfg, "max_entry_bytes", DEFAULT_MAX_ENTRY_BYTES
        )
        self.effective_input_limit = min(
            self.max_file_bytes, self.global_max_entry_bytes
        )
        self.max_html_bytes = _int_setting(
            self.cfg, "max_html_bytes", DEFAULT_MAX_HTML_BYTES
        )
        self.max_preview_chars = _int_setting(
            self.cfg, "max_preview_chars", DEFAULT_PREVIEW_CHARS
        )
        self.max_body_text_bytes = _int_setting(
            self.cfg, "max_body_text_bytes", DEFAULT_MAX_BODY_TEXT_BYTES
        )
        self.max_visited, self.traversal_timeout = _traversal_limits(config)
        from ...indexing.derived import source_derived_config, source_operations

        operations = source_operations(self.cfg, self.source)
        self.operations = operations
        self.derived_config = source_derived_config(self.cfg, self.source)
        self.captions_enabled = bool(getattr(operations, "captions", False))
        self.ocr_enabled = bool(getattr(operations, "ocr", None))
        self.asr_enabled = bool(getattr(operations, "asr", None))
        engines = self.cfg.get("derived_engines") or {}
        self.embedded_captions_available = callable(
            engines.get("ffmpeg") if hasattr(engines, "get") else None
        )
        self._closed = False
        self._sidecar_errors: list[str] = []
        snapshot = self._build_snapshot(errors=self._sidecar_errors)
        self._items = snapshot.items
        self.count = len(self._items)
        self.truncated = snapshot.truncated
        self.fingerprint = snapshot.fingerprint
        self.source_size = sum(item.size for item in self._items)
        self.source_mtime = max((item.mtime for item in self._items), default=None)
        self.errors = tuple(self._sidecar_errors)
        for message in self._sidecar_errors:
            logger.warning("zimantic: %s: %s", self.key, message)

    # -- manifest ----------------------------------------------------------

    def _indexable_path(self, path: str, family: str | None) -> bool:
        if self.index_include and not matches_any(self.index_include, path):
            return False
        if family is not None:
            return True
        return (
            matches_any(self.include, path)
            if self.include else is_native_document_path(path)
        )

    def _build_snapshot(self, *, errors: list[str] | None = None) -> _Snapshot:
        sidecar_errors = errors if errors is not None else []
        items: list[_ManifestItem] = []
        sidecars_by_dir: dict[str, list[_Sidecar]] = {}
        budget = _Budget(self.max_visited, self.traversal_timeout)
        truncated = False

        def visit(relative_dir: str) -> bool:
            nonlocal truncated
            try:
                entries = list_directory(self.root, relative_dir)
            except OSError as error:
                raise FilesTraversalError(
                    f"cannot read {relative_dir or '.'}: "
                    f"{error.strerror or error}"
                ) from error
            for entry in entries:
                budget.tick()
                name = entry.name
                if is_hidden_name(name):
                    continue
                relative = relative_join(relative_dir, name)
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if matches_any(self.exclude, relative, directory=True):
                            continue
                        if not self.loose_only and visit(relative):
                            return True
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if matches_any(self.exclude, relative):
                    continue
                if is_track_sidecar(name):
                    if self.captions_enabled:
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        sidecars_by_dir.setdefault(relative_dir, []).append(
                            _Sidecar(
                                relative,
                                name,
                                int(info.st_size),
                                round(info.st_mtime_ns / 1_000_000_000),
                                extension_mimetype(relative)
                                or "application/octet-stream",
                            )
                        )
                    continue
                mimetype = _item_mimetype(self.root, relative)
                family = media_family_for_path(relative) or media_family_for_mimetype(
                    mimetype
                )
                if not self._indexable_path(relative, family):
                    continue
                if len(items) >= self.max_entries:
                    truncated = True
                    return True
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                items.append(
                    _ManifestItem(
                        path=relative,
                        name=name,
                        size=int(info.st_size),
                        mtime=round(info.st_mtime_ns / 1_000_000_000),
                        family=family,
                        mimetype=mimetype,
                    )
                )
            return False

        if self.loose_only:
            try:
                entries = list_directory(self.root, "")
            except OSError as error:
                raise FilesTraversalError(
                    f"cannot read .: {error.strerror or error}"
                ) from error
            for entry in entries:
                budget.tick()
                name = entry.name
                if is_hidden_name(name):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(
                        follow_symlinks=False
                    ):
                        continue
                except OSError:
                    continue
                if matches_any(self.exclude, name):
                    continue
                if is_track_sidecar(name):
                    if self.captions_enabled:
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        sidecars_by_dir.setdefault("", []).append(
                            _Sidecar(
                                name,
                                name,
                                int(info.st_size),
                                round(info.st_mtime_ns / 1_000_000_000),
                                extension_mimetype(name)
                                or "application/octet-stream",
                            )
                        )
                    continue
                mimetype = _item_mimetype(self.root, name)
                family = media_family_for_path(name) or media_family_for_mimetype(
                    mimetype
                )
                if not self._indexable_path(name, family):
                    continue
                if len(items) >= self.max_entries:
                    truncated = True
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                items.append(
                    _ManifestItem(
                        path=name,
                        name=name,
                        size=int(info.st_size),
                        mtime=round(info.st_mtime_ns / 1_000_000_000),
                        family=family,
                        mimetype=mimetype,
                    )
                )
        else:
            visit(self.membership_root)

        if self.captions_enabled and sidecars_by_dir:
            items = self._attach_sidecars(items, sidecars_by_dir, sidecar_errors)
        return _Snapshot(
            items=tuple(items),
            truncated=truncated,
            fingerprint=self._fingerprint(items, truncated),
        )

    @staticmethod
    def _attach_sidecars(
        items: list[_ManifestItem],
        sidecars_by_dir: Mapping[str, Sequence[_Sidecar]],
        errors: list[str],
    ) -> list[_ManifestItem]:
        by_dir: dict[str, list[int]] = {}
        for index, item in enumerate(items):
            if item.family not in {"audio", "video"}:
                continue
            directory = item.path.rsplit("/", 1)[0] if "/" in item.path else ""
            by_dir.setdefault(directory, []).append(index)
        attached: dict[int, list[_Sidecar]] = {index: [] for index in range(len(items))}
        for directory, sidecars in sidecars_by_dir.items():
            media_indexes = by_dir.get(directory)
            if not media_indexes:
                continue
            by_name: dict[str, int] = {}
            by_stem: dict[str, list[int]] = {}
            for index in media_indexes:
                item = items[index]
                by_name[item.name] = index
                by_stem.setdefault(Path(item.name).stem, []).append(index)
            for sidecar in sidecars:
                base = sidecar.name[: -len(Path(sidecar.name).suffix)]
                variants = [base]
                if "." in base:
                    variants.append(base.rsplit(".", 1)[0])
                full_matches: set[int] = set()
                stem_matches: set[int] = set()
                for variant in variants:
                    if variant in by_name:
                        full_matches.add(by_name[variant])
                    stem_matches.update(by_stem.get(variant, ()))
                full = sorted(full_matches)
                stems = sorted(stem_matches - full_matches)
                if len(full) == 1:
                    target = full[0]
                elif len(full) > 1:
                    errors.append(
                        f"ambiguous caption sidecar {sidecar.path!r} matches "
                        "multiple media files"
                    )
                    continue
                elif len(stems) == 1:
                    target = stems[0]
                elif len(stems) > 1:
                    errors.append(
                        f"ambiguous caption sidecar {sidecar.path!r} matches "
                        "multiple media files with the same stem"
                    )
                    continue
                else:
                    continue
                attached[target].append(sidecar)
        return [
            replace(
                item,
                sidecars=tuple(sorted(attached[index], key=lambda value: value.path)),
            )
            for index, item in enumerate(items)
        ]

    def _fingerprint(
        self, items: Sequence[_ManifestItem], truncated: bool
    ) -> str:
        payload = {
            "provider": self.provider,
            "key": self.key,
            "collection_key": self.collection_key,
            "membership_root": self.membership_root,
            "loose_only": self.loose_only,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "max_entries": self.max_entries,
            "max_file_bytes": self.max_file_bytes,
            "effective_input_limit": self.effective_input_limit,
            "fast": self.fast,
            "max_html_bytes": self.max_html_bytes,
            "max_preview_chars": self.max_preview_chars,
            "max_body_text_bytes": self.max_body_text_bytes,
            "truncated": truncated,
            "items": [
                (
                    item.path,
                    item.size,
                    item.mtime,
                    item.family,
                    [
                        (sidecar.path, sidecar.size, sidecar.mtime)
                        for sidecar in item.sidecars
                    ],
                )
                for item in items
            ],
            "operations": _operations_state(self.operations),
            "derived_text": _derived_config_payload(
                self.derived_config
            ),
            "engines": sorted(
                str(name) for name in (self.cfg.get("derived_engines") or {})
            ),
        }
        encoded = json.dumps(
            payload, sort_keys=True, default=str, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    # -- producer contract -------------------------------------------------

    @property
    def path_scope_root(self) -> Path:
        return self.root

    @property
    def path_scope_membership(self) -> str:
        return self.membership_root

    def entry_paths(self, index: int) -> tuple[str, ...]:
        item = self._items[index]
        return (item.path, *(sidecar.path for sidecar in item.sidecars))

    def entry_at(self, index: int) -> EntryRecord | None:
        if not isinstance(index, int) or index < 0 or index >= self.count:
            return None
        item = self._items[index]
        row_id = index + 1
        if item.family is not None:
            return EntryRecord(
                row_id,
                _filename_title(item.name),
                "",
                item.path,
                None,
                "",
                False,
            )
        if self.fast:
            row = self._fast_document_record(item, row_id)
        else:
            row = self._document_record(item, row_id)
        return row

    def modified_at(self, index: int) -> int | None:
        if not isinstance(index, int) or index < 0 or index >= self.count:
            return None
        return self._items[index].mtime

    def _fast_document_record(
        self, item: _ManifestItem, row_id: int
    ) -> EntryRecord | None:
        """Title/path-only fast-build row; never parse a document body."""
        if item.size > self.effective_input_limit:
            logger.debug(
                "files: skipping %s (%d bytes exceeds the %d-byte input limit)",
                item.path,
                item.size,
                self.effective_input_limit,
            )
            return None
        title = _filename_title(item.name)
        if item.mimetype in {"text/html", "application/xhtml+xml"}:
            try:
                opened = open_confined(self.root, item.path)
            except FilesConfinedError:
                opened = None
            if opened is not None:
                try:
                    prefix = opened.handle.read(
                        min(item.size, 64 << 10, self.max_html_bytes)
                    )
                except OSError:
                    prefix = b""
                finally:
                    close = getattr(opened.handle, "close", None)
                    if callable(close):
                        close()
                title = (
                    _html_title(prefix)
                    or _html_first_heading(prefix)
                    or title
                )
            return EntryRecord(row_id, title, "", item.path, None, "", False)
        if item.mimetype in {
            "application/pdf",
            "text/plain",
            "text/markdown",
        }:
            return EntryRecord(row_id, title, "", item.path, None, "", False)
        logger.debug(
            "files: skipping unsupported document type %s for %s",
            item.mimetype,
            item.path,
        )
        return None

    def _document_record(
        self, item: _ManifestItem, row_id: int
    ) -> EntryRecord | None:
        title_fallback = _filename_title(item.name)
        if item.size > self.effective_input_limit:
            logger.debug(
                "files: skipping %s (%d bytes exceeds the %d-byte input limit)",
                item.path,
                item.size,
                self.effective_input_limit,
            )
            if self.ocr_enabled and item.mimetype == "application/pdf":
                return EntryRecord(
                    row_id, title_fallback, "", item.path, None, "", False
                )
            return None
        try:
            opened = open_confined(self.root, item.path)
        except FilesConfinedError as error:
            logger.debug("files: cannot open %s (%s)", item.path, error)
            return None
        try:
            if item.mimetype in {"text/html", "application/xhtml+xml"}:
                raw = opened.handle.read(self.max_html_bytes)
                input_truncated = item.size > len(raw)
                explicit_title = _html_title(raw) or _html_first_heading(raw)
                title = explicit_title or title_fallback
                excerpt = extract_excerpt(raw, self.max_preview_chars)
                body, body_truncated = extract_body(
                    raw,
                    self.max_body_text_bytes,
                    input_truncated=input_truncated,
                )
                if not excerpt and not body:
                    fallback_text = _html_plain_text(raw)
                    if fallback_text:
                        excerpt = truncate_at_word_boundary(
                            fallback_text, self.max_preview_chars
                        )
                        body, body_truncated = truncate_utf8(
                            fallback_text, self.max_body_text_bytes
                        )
                        body_truncated = body_truncated or input_truncated
                    elif explicit_title:
                        return EntryRecord(
                            row_id, title, "", item.path, None, "", False
                        )
                    else:
                        return None
                if is_disambiguation(title, excerpt, raw):
                    title = disambiguation_title(title)
                return EntryRecord(
                    row_id,
                    title,
                    excerpt,
                    item.path,
                    None,
                    body,
                    body_truncated,
                )
            raw = opened.handle.read(self.effective_input_limit + 1)
        except OSError as error:
            logger.debug("files: cannot read %s (%s)", item.path, error)
            return None
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()

        if len(raw) > self.effective_input_limit:
            logger.debug(
                "files: skipping %s (changed size beyond the input limit)",
                item.path,
            )
            if self.ocr_enabled and item.mimetype == "application/pdf":
                return EntryRecord(
                    row_id, title_fallback, "", item.path, None, "", False
                )
            return None

        if item.mimetype == "application/pdf":
            excerpt = extract_pdf_excerpt(raw, self.max_preview_chars)
            body_text = extract_pdf_excerpt(raw, self.max_body_text_bytes)
            body, truncated = truncate_utf8(body_text, self.max_body_text_bytes)
            body_truncated = truncated or len(
                body_text.encode("utf-8", "ignore")
            ) >= self.max_body_text_bytes
            metadata_title = _pdf_metadata_title(raw)
            first_line = _first_text_line(excerpt.encode("utf-8", "ignore"))
            title = (
                metadata_title
                if metadata_title and len(metadata_title) <= MAX_PDF_TITLE_CHARS
                else first_line
                if first_line and len(first_line) <= MAX_PDF_TITLE_CHARS
                else _clean_title(title_fallback)[:MAX_PDF_TITLE_CHARS]
            )
            title = title or _clean_title(title_fallback)
            if not excerpt and not body_text:
                if self.ocr_enabled:
                    return EntryRecord(
                        row_id, title, "", item.path, None, "", False
                    )
                return None
            return EntryRecord(
                row_id, title, excerpt, item.path, None, body, body_truncated
            )

        if item.mimetype == "text/markdown":
            title, excerpt, body, body_truncated = extract_markdown_document(
                raw, self.max_preview_chars, self.max_body_text_bytes
            )
            title = title or _markdown_first_heading(raw) or title_fallback
            if not excerpt and not body:
                if title:
                    return EntryRecord(
                        row_id, title, "", item.path, None, "", False
                    )
                return None
            return EntryRecord(
                row_id,
                title,
                excerpt,
                item.path,
                None,
                body,
                body_truncated,
            )

        if item.mimetype == "text/plain":
            excerpt, body, body_truncated = extract_plain_text_document(
                raw, self.max_preview_chars, self.max_body_text_bytes
            )
            if not excerpt and not body:
                return None
            title = title_fallback
            if is_disambiguation(title, excerpt, raw):
                title = disambiguation_title(title)
            return EntryRecord(
                row_id,
                title,
                excerpt,
                item.path,
                None,
                body,
                body_truncated,
            )

        logger.debug(
            "files: skipping unsupported document type %s for %s",
            item.mimetype,
            item.path,
        )
        return None

    def validate_snapshot(self) -> bool:
        try:
            snapshot = self._build_snapshot()
        except FilesTraversalError as error:
            raise RuntimeError(
                f"cannot validate files source {self.key}: {error}"
            ) from error
        return snapshot.fingerprint == self.fingerprint

    def close(self) -> None:
        self._closed = True

    # -- derived text ------------------------------------------------------

    def derived_text_inputs(self, index: int) -> tuple[_DerivedInput, ...]:
        if not isinstance(index, int) or index < 0 or index >= self.count:
            return ()
        if not (self.captions_enabled or self.ocr_enabled or self.asr_enabled):
            return ()
        item = self._items[index]
        inputs: list[_DerivedInput] = []
        if self.captions_enabled:
            for sidecar in item.sidecars:
                inputs.append(
                    _DerivedInput(
                        self.root,
                        sidecar.path,
                        "captions",
                        sidecar.mimetype,
                        sidecar.size,
                        self.effective_input_limit,
                    )
                )
        if self.ocr_enabled and is_ocr_candidate(
            item.mimetype, item.family
        ):
            inputs.append(
                _DerivedInput(
                    self.root,
                    item.path,
                    "image",
                    item.mimetype,
                    item.size,
                    self.effective_input_limit,
                )
            )
        if item.family in {"audio", "video"} and (
            self.asr_enabled
            or (self.captions_enabled and self.embedded_captions_available)
        ):
            inputs.append(
                _DerivedInput(
                    self.root,
                    item.path,
                    str(item.family),
                    item.mimetype,
                    item.size,
                    self.effective_input_limit,
                )
            )
        return tuple(inputs)


__all__ = ["FilesEntryProducer"]
