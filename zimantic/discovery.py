"""Reusable provider discovery for commands that do not start the web server.

``serve`` owns the long-lived, background-published :class:`Search` runtime.
``build``, ``sources``, and ``prune`` instead need one bounded snapshot: open
each configured provider entry, wait for the requested discovery state, and use
the same collection/source records as the HTTP APIs.  This module keeps that
read-only path in one place.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from .contracts import SourceInfo
from .providers.base import (
    CollectionInfo,
    CollectionRuntime,
    DiscoveryBatch,
    ProviderContext,
)
from .providers.config import ProviderConfig, ProviderKind, RuntimeConfig
from .providers.registry import get_provider
from .settings import normalize_web_base_path

__all__ = [
    "DiscoveredCollection",
    "DiscoveryResult",
    "discover_collections",
    "entry_collection_prefix",
    "filter_sources",
]

logger = logging.getLogger(__name__)


def entry_collection_prefix(entry: ProviderConfig) -> str:
    """Return the collection-key prefix that belongs to one configured entry."""
    return f"{entry.kind}:{entry.name}"


@dataclass
class DiscoveredCollection:
    entry: ProviderConfig
    collection: CollectionInfo
    runtime: CollectionRuntime
    batch: DiscoveryBatch
    ready: bool = True

    def sources(self) -> Sequence[SourceInfo]:
        return self.runtime.sources


@dataclass
class DiscoveryResult:
    entries: tuple[ProviderConfig, ...] = ()
    collections: tuple[DiscoveredCollection, ...] = ()
    diagnostics: tuple[str, ...] = ()
    unavailable_entries: tuple[str, ...] = ()
    index_dir: Path | None = None
    index_dir_existed: bool = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def sources(self) -> tuple[SourceInfo, ...]:
        """Every searchable source, in collection then source order."""
        return tuple(
            source
            for discovered in self.collections
            for source in discovered.runtime.sources
        )

    def library_sources(self) -> tuple[SourceInfo, ...]:
        """Every document-library source, including unsearchable local files."""
        return tuple(
            source
            for discovered in self.collections
            for source in discovered.runtime.library_sources()
        )

    def close(self) -> None:
        runtimes = [discovered.runtime for discovered in self.collections]
        batches: list[DiscoveryBatch] = []
        seen: set[int] = set()
        for discovered in self.collections:
            if id(discovered.batch) in seen:
                continue
            seen.add(id(discovered.batch))
            batches.append(discovered.batch)
        for runtime in runtimes:
            try:
                runtime.close()
            except Exception:
                pass
        for batch in batches:
            try:
                batch.close()
            except Exception:
                pass
        if self.index_dir is not None:
            # Kolibri snapshots are private temporary state.  Remove the empty
            # containers they needed so read-only commands leave an existing
            # index_dir untouched and do not create a fresh one.
            snapshots = self.index_dir / "kolibri-snapshots"
            if snapshots.is_dir():
                for directory in sorted(
                    (path for path in snapshots.rglob("*") if path.is_dir()),
                    key=lambda path: len(path.parts),
                    reverse=True,
                ):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                try:
                    snapshots.rmdir()
                except OSError:
                    pass
            if not self.index_dir_existed:
                try:
                    self.index_dir.rmdir()
                except OSError:
                    pass
        self.collections = ()
        self.entries = ()


def _runtime_config(cfg: dict[str, Any], entries: Sequence[ProviderConfig]) -> RuntimeConfig:
    runtime_config = cfg.get("runtime_config")
    if isinstance(runtime_config, RuntimeConfig):
        return runtime_config
    config_dir = Path(cfg.get("config_dir") or Path.cwd())
    return RuntimeConfig(
        settings=dict(cfg),
        collections=tuple(entries),
        config_dir=config_dir,
        search_timeout_seconds=float(cfg.get("search_timeout_seconds", 45.0)),
        page_size=int(cfg.get("page_size", 10)),
    )


def discover_collections(
    cfg: dict[str, Any],
    *,
    entries: Iterable[ProviderConfig] | None = None,
    wait: bool = True,
    timeout: float | None = None,
    persist_catalog: bool = False,
    provider_kinds: Iterable[str] | None = None,
    on_sources_changed: Any | None = None,
    metadata_only: bool = False,
) -> DiscoveryResult:
    """Open configured provider entries and return one bounded snapshot.

    Failures are operation-scoped: an invalid, unavailable, or timed-out entry
    is reported in ``diagnostics`` and never prevents another entry from being
    discovered.  There is no fallback provider.
    """
    selected = tuple(entries if entries is not None else cfg.get("collections") or ())
    if provider_kinds is not None:
        allowed = {str(kind) for kind in provider_kinds}
        selected = tuple(entry for entry in selected if entry.kind in allowed)
    runtime_config = _runtime_config(cfg, selected)
    index_dir = Path(cfg.get("index_dir") or ".")
    index_dir_existed = index_dir.exists()
    ctx = ProviderContext(
        config=runtime_config,
        index_dir=index_dir,
        web_base_path=normalize_web_base_path(cfg.get("web_base_path")),
        logger=logger,
        persist_catalog=persist_catalog,
        on_sources_changed=on_sources_changed,
        metadata_only=metadata_only,
    )
    diagnostics: list[str] = []
    unavailable: list[str] = []
    collections: list[DiscoveredCollection] = []

    if timeout is None:
        try:
            timeout = float(cfg.get("search_timeout_seconds", 45.0))
        except (TypeError, ValueError):
            timeout = 45.0

    for entry in selected:
        try:
            provider = get_provider(entry.kind)  # type: ignore[arg-type]
        except Exception as error:
            diagnostics.append(f"{entry.name}: {entry.kind}: {error}")
            unavailable.append(entry.name)
            continue
        try:
            batch = provider.discover(entry, ctx)
        except Exception as error:
            diagnostics.append(
                f"{entry.name}: {entry.kind} discovery failed: {error}"
            )
            unavailable.append(entry.name)
            continue
        batch_error = getattr(batch, "error", None)
        if batch_error:
            diagnostics.append(f"{entry.name}: {entry.kind} discovery: {batch_error}")
            unavailable.append(entry.name)
        opened: list[DiscoveredCollection] = []
        try:
            batch_collections = tuple(batch.collections)
            if not batch_collections:
                if batch_error:
                    unavailable.append(entry.name)
                continue
            for collection in batch_collections:
                try:
                    runtime = batch.open(collection)
                except Exception as error:
                    diagnostics.append(
                        f"{entry.name}: {entry.kind} collection "
                        f"{collection.key} could not be opened: {error}"
                    )
                    unavailable.append(entry.name)
                    continue
                ready = True
                if wait:
                    try:
                        ready = bool(runtime.wait_ready(timeout))
                    except Exception as error:
                        diagnostics.append(
                            f"{entry.name}: {entry.kind} collection "
                            f"{collection.key} discovery failed: {error}"
                        )
                        ready = False
                    if not ready:
                        unavailable.append(entry.name)
                        diagnostics.append(
                            f"{entry.name}: {entry.kind} collection "
                            f"{collection.key} did not finish discovery within "
                            f"{timeout:g}s; keeping its last published state"
                        )
                snapshot = runtime.snapshot
                if snapshot.error:
                    unavailable.append(entry.name)
                    diagnostics.append(
                        f"{entry.name}: {entry.kind} collection "
                        f"{collection.key}: {snapshot.error}"
                    )
                opened.append(
                    DiscoveredCollection(
                        entry=entry,
                        collection=collection,
                        runtime=runtime,
                        batch=batch,
                        ready=ready,
                    )
                )
        except Exception as error:
            diagnostics.append(f"{entry.name}: {entry.kind} discovery failed: {error}")
            unavailable.append(entry.name)
            for discovered in opened:
                try:
                    discovered.runtime.close()
                except Exception:
                    pass
            opened = []
        finally:
            if not opened:
                try:
                    batch.close()
                except Exception:
                    pass
        collections.extend(opened)

    return DiscoveryResult(
        entries=selected,
        collections=tuple(collections),
        diagnostics=tuple(diagnostics),
        unavailable_entries=tuple(dict.fromkeys(unavailable)),
        index_dir=index_dir,
        index_dir_existed=index_dir_existed,
    )


def filter_sources(
    sources: Iterable[SourceInfo],
    *,
    provider: Iterable[ProviderKind] | None = None,
    collection: Iterable[str] | None = None,
    source: Iterable[str] | None = None,
) -> tuple[SourceInfo, ...]:
    """Filter sources with OR within one selector and AND across selectors."""
    providers = {str(item) for item in provider or ()}
    collections = {str(item) for item in collection or ()}
    keys = {str(item) for item in source or ()}
    selected: list[SourceInfo] = []
    for item in sources:
        if providers and item.provider not in providers:
            continue
        if collections and not (
            item.collection_key in collections
            or item.key in collections
            or item.collection_label in collections
        ):
            continue
        if keys and item.key not in keys:
            continue
        selected.append(item)
    return tuple(selected)
