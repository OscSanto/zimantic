"""ZIM-backed :class:`EntryProducer`.

This adapter is the only place where the generic index pipeline touches libzim.
It preserves the archive entry's raw ``_index`` and redirect target index so
existing FTS rowids and FAISS ids do not need renumbering.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from ...content.reader import (
    DEFAULT_MAX_BODY_TEXT_BYTES,
    DEFAULT_MAX_ENTRY_BYTES,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_PREVIEW_CHARS,
    read_entry,
)
from ...diagnostics import relative_display
from ...indexing.types import EntryRecord, MAX_ROW_ID
from .metadata import header_uuid

logger = logging.getLogger(__name__)


def _setting(cfg: Mapping[str, Any] | None, key: str, default: int) -> int:
    value = (cfg or {}).get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return int(default)
    return int(value)


class ArchiveEntryProducer:
    """Bounded, resumable sequence of rows from one ZIM archive."""

    provider = "zim"

    def __init__(
        self,
        source: Any,
        path: Path | str,
        *,
        fast: bool = False,
        cfg: Mapping[str, Any] | None = None,
        archive: Any | None = None,
        max_entries: int | None = None,
        catalog_path: Path | str | None = None,
    ) -> None:
        self.source = source
        self.key = str(getattr(source, "key", ""))
        self.collection_key = str(getattr(source, "collection_key", ""))
        self.provider = str(getattr(source, "provider", self.provider))
        self.provider_id = getattr(source, "provider_id", None)
        self.path = Path(path)
        self.fast = bool(fast)
        self.cfg = dict(cfg or {})
        self.catalog_path = Path(catalog_path) if catalog_path else None
        self.max_html_bytes = _setting(
            self.cfg, "max_html_bytes", DEFAULT_MAX_HTML_BYTES
        )
        self.max_entry_bytes = _setting(
            self.cfg, "max_entry_bytes", DEFAULT_MAX_ENTRY_BYTES
        )
        self.max_preview_chars = _setting(
            self.cfg, "max_preview_chars", DEFAULT_PREVIEW_CHARS
        )
        self.max_body_text_bytes = _setting(
            self.cfg, "max_body_text_bytes", DEFAULT_MAX_BODY_TEXT_BYTES
        )
        self._archive = archive
        self._owns_archive = archive is None
        self._closed = False
        self._stat = self.path.stat()
        self.filesize = max(0, int(self._stat.st_size))
        self.source_size = self.filesize
        self.source_mtime = round(self._stat.st_mtime_ns / 1_000_000_000)
        try:
            self._uuid = header_uuid(self.path)
        except (OSError, ValueError):
            self._uuid = ""
        self._archive_entry_count = self._entry_count()
        configured_cap = max_entries
        if configured_cap is None:
            configured_cap = getattr(source, "max_entries", None)
        if isinstance(configured_cap, int) and not isinstance(configured_cap, bool):
            self.count = max(0, min(self._archive_entry_count, configured_cap))
        else:
            self.count = self._archive_entry_count
        self.truncated = self.count < self._archive_entry_count
        self.fingerprint = self._fingerprint()
        self._update_catalog()

    def _update_catalog(self) -> None:
        if self.catalog_path is None:
            return
        try:
            from ...catalog import SourceCatalog
            from .metadata import metadata_from_archive

            with SourceCatalog(self.catalog_path) as catalog:
                catalog.upsert(
                    metadata_from_archive(
                        self._open(),
                        self.path,
                        key=self.key,
                        collection_key=self.collection_key,
                    )
                )
        except Exception as error:
            logger.warning(
                "catalog metadata update failed for %s (%s)",
                relative_display(getattr(self, "_root", None), self.path),
                type(error).__name__,
            )
            logger.debug("catalog metadata update error for %s: %s", self.path, error)

    # -- producer contract -------------------------------------------------

    def _entry_count(self) -> int:
        archive = self._open()
        try:
            return max(0, int(archive.entry_count))
        except (AttributeError, TypeError, ValueError):
            return 0

    def _open(self):
        if self._archive is None:
            from libzim.reader import Archive

            self._archive = Archive(str(self.path))
        return self._archive

    def entry_at(self, index: int) -> EntryRecord | None:
        if not isinstance(index, int) or index < 0 or index >= self.count:
            return None
        archive = self._open()
        row = read_entry(
            archive,
            index,
            fast=self.fast,
            max_html_bytes=self.max_html_bytes,
            max_entry_bytes=self.max_entry_bytes,
            max_preview_chars=self.max_preview_chars,
            max_body_text_bytes=self.max_body_text_bytes,
        )
        if row is None:
            return None
        if row.id < 0 or row.id > MAX_ROW_ID:
            raise ValueError(
                f"ZIM entry {index} has an unsafe row id {row.id}; refusing to "
                "merge or remap provider identities"
            )
        if row.target_id is not None and (
            row.target_id < 0 or row.target_id > MAX_ROW_ID
        ):
            raise ValueError(
                f"ZIM entry {index} redirects to an unsafe row id "
                f"{row.target_id}; refusing to merge or remap provider identities"
            )
        return row

    def validate_snapshot(self) -> bool:
        """Independently recheck the live archive against the fingerprint."""
        try:
            stat = self.path.stat()
        except OSError as error:
            raise RuntimeError(
                f"cannot validate ZIM source {self.key}: {error}"
            ) from error
        if stat.st_size != self._stat.st_size:
            return False
        if round(stat.st_mtime_ns / 1_000_000_000) != round(
            self._stat.st_mtime_ns / 1_000_000_000
        ):
            return False
        try:
            current_uuid = header_uuid(self.path)
        except (OSError, ValueError) as error:
            raise RuntimeError(
                f"cannot validate ZIM source {self.key}: {error}"
            ) from error
        if self._uuid and current_uuid != self._uuid:
            return False
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._owns_archive:
            return
        archive = self._archive
        self._archive = None
        close = getattr(archive, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    # -- derived text ------------------------------------------------------

    def derived_text_inputs(self, index: int):
        """ZIM embedded-media enrichment is not enabled in this phase."""
        return ()

    # -- identity ----------------------------------------------------------

    def _fingerprint(self) -> str:
        payload = {
            "provider": self.provider,
            "key": self.key,
            "uuid": self._uuid,
            "size": self._stat.st_size,
            "mtime": round(self._stat.st_mtime_ns / 1_000_000_000),
            "count": self.count,
            "entry_count": self._archive_entry_count,
            "fast": self.fast,
            "max_html_bytes": self.max_html_bytes,
            "max_entry_bytes": self.max_entry_bytes,
            "max_preview_chars": self.max_preview_chars,
            "max_body_text_bytes": self.max_body_text_bytes,
            "derived_text": self.cfg.get("derived_text"),
            "derived_operations": (
                self.cfg.get("derived_source_operations", {}).get(self.key)
                if hasattr(self.cfg.get("derived_source_operations", {}), "get")
                else None
            ),
            "derived_engines": sorted(
                str(name)
                for name in (self.cfg.get("derived_engines") or {})
            ),
        }
        encoded = json.dumps(
            payload, sort_keys=True, default=str, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def create_zim_producer(
    source: Any,
    *,
    fast: bool,
    cfg: Mapping[str, Any] | None = None,
    archive_factory: Any | None = None,
    max_entries: int | None = None,
    catalog_path: Path | str | None = None,
) -> ArchiveEntryProducer:
    """Open the archive through an optional test/runtime factory."""
    path = getattr(source, "path", None)
    if not path:
        record = getattr(source, "record", None)
        path = getattr(record, "path", None)
    if not path:
        raise ValueError("ZIM source does not expose a private archive locator")
    archive = None
    if archive_factory is not None and archive_factory is not object:
        archive = archive_factory(str(path))
    return ArchiveEntryProducer(
        source,
        path,
        fast=fast,
        cfg=cfg,
        archive=archive,
        max_entries=max_entries,
        catalog_path=catalog_path,
    )


__all__ = ["ArchiveEntryProducer", "create_zim_producer"]
