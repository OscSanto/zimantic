"""Owned-index inventory and pruning.

Pruning is intentionally conservative.  A directory is eligible only when its
``current`` manifest is valid, its identity fields match the directory layout
that :class:`~zimantic.indexing.locator.IndexLocator` would compute, and its
collection namespace is not configured by a complete, validated discovery
snapshot.  Unknown files and partially recognizable legacy layouts are reported
for manual inspection and never deleted by extension or filename.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .discovery import DiscoveryResult
from ._filelock import lock_file, unlock_file
from .indexing.locator import MANIFEST_VERSION, IndexLocator
from .providers.config import BUILTIN_PROVIDERS, ConfigIssue

__all__ = [
    "PruneCandidate",
    "PruneInventory",
    "collect_inventory",
    "configured_namespaces",
]


@dataclass(frozen=True)
class PruneCandidate:
    provider: str
    collection_key: str
    source_key: str
    directory: Path
    bytes: int
    pinned: bool = False

    def to_dict(self, *, index_dir: Path | None = None) -> dict[str, Any]:
        directory = self.directory
        if index_dir is not None:
            try:
                directory = directory.resolve().relative_to(index_dir.resolve())
            except (OSError, ValueError):
                pass
        return {
            "provider": self.provider,
            "collection": self.collection_key,
            "source": self.source_key,
            "directory": str(directory),
            "bytes": self.bytes,
            "pinned": self.pinned,
        }


@dataclass
class PruneInventory:
    candidates: list[PruneCandidate] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def reclaimed_bytes(self) -> int:
        return sum(
            candidate.bytes
            for candidate in self.candidates
            if not candidate.pinned
        )


def _lock_is_free(lease_path: Path) -> bool:
    try:
        fd = os.open(lease_path, os.O_RDWR)
    except OSError:
        return True
    locked = False
    try:
        locked = lock_file(fd)
        return locked
    finally:
        if locked:
            try:
                unlock_file(fd)
            except OSError:
                pass
        os.close(fd)


def _is_pinned(source_directory: Path) -> bool:
    generations = source_directory / "generations"
    if not generations.is_dir():
        return False
    for generation in generations.iterdir():
        if not generation.is_dir():
            continue
        lease = generation / "lease"
        if lease.exists() and not _lock_is_free(lease):
            return True
    return False


def _owned_children(directory: Path) -> tuple[list[Path], list[Path]]:
    """Split one source directory into Zimantic-owned and unknown entries."""
    owned: list[Path] = []
    unknown: list[Path] = []
    try:
        children = list(directory.iterdir())
    except OSError:
        return owned, unknown
    for child in children:
        name = child.name
        if name in {"current", "generations", "derived-cache"}:
            owned.append(child)
        elif name.startswith("staging-") or name.endswith(
            (".tmp", ".upgrade")
        ):
            # Interrupted staging files and manifests written by Zimantic.
            owned.append(child)
        else:
            unknown.append(child)
    return owned, unknown


def _path_bytes(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _directory_bytes(directory: Path) -> int:
    owned, _unknown = _owned_children(directory)
    return sum(_path_bytes(path) for path in owned)


def configured_namespaces(
    cfg: Mapping[str, Any],
    discovery: DiscoveryResult | None,
) -> tuple[set[str], set[str], bool]:
    """Return ``(exact, prefixes, retain_all)`` for configured namespaces."""
    exact: set[str] = set()
    prefixes: set[str] = set()
    retain_all = False
    if discovery is not None:
        for discovered in discovery.collections:
            exact.add(discovered.collection.key)
        unavailable = set(discovery.unavailable_entries)
        discovered_entries = {
            (getattr(entry, "kind", None), getattr(entry, "name", None))
            for entry in discovery.entries
        }
    else:
        unavailable = set()
        discovered_entries = set()
    for entry in cfg.get("collections") or ():
        kind = getattr(entry, "kind", None)
        name = getattr(entry, "name", None)
        if kind in {"zim", "files", "kiwix"} and name:
            # These collection keys are deterministic from the entry itself, so
            # even an unavailable discovery still identifies the namespace.
            exact.add(f"{kind}:{name}")
        elif kind == "kolibri" and name:
            # A Kolibri entry expands to one collection per channel.  Only a
            # complete, available discovery can prove which channels are gone.
            if discovery is None or (kind, name) not in discovered_entries or name in unavailable:
                prefixes.add(f"kolibri:{name}:")
    for issue in cfg.get("config_issues") or ():
        if not isinstance(issue, ConfigIssue):
            continue
        if issue.namespace:
            exact.add(issue.namespace)
        if issue.provider_wide:
            if issue.kind in BUILTIN_PROVIDERS:
                prefixes.add(f"{issue.kind}:")
            else:
                retain_all = True
    return exact, prefixes, retain_all


def _namespace_retained(
    collection_key: str,
    *,
    exact: set[str],
    prefixes: set[str],
    retain_all: bool,
) -> bool:
    if retain_all:
        return True
    if collection_key in exact:
        return True
    return any(collection_key.startswith(prefix) for prefix in prefixes)


def collect_inventory(
    index_dir: Path | str,
    cfg: Mapping[str, Any],
    discovery: DiscoveryResult | None,
) -> PruneInventory:
    """Inspect owned source directories without creating or modifying anything."""
    root = Path(index_dir)
    inventory = PruneInventory()
    if not root.exists():
        return inventory
    exact, prefixes, retain_all = configured_namespaces(cfg, discovery)
    for issue in cfg.get("config_issues") or ():
        if isinstance(issue, ConfigIssue):
            inventory.problems.append(
                f"configuration: {issue.message}; retaining any affected index "
                "namespaces for manual review"
            )
    if discovery is not None:
        for name in discovery.unavailable_entries:
            inventory.problems.append(
                f"entry {name!r}: discovery is unavailable or incomplete; "
                "retaining its index namespaces"
            )
    if retain_all:
        inventory.problems.append(
            "an invalid collection entry has unknown scope; retaining every "
            "index namespace"
        )
    locator = IndexLocator(root)
    try:
        top_level = sorted(root.iterdir())
    except OSError as error:
        inventory.problems.append(
            f"{root}: index directory is unreadable ({error}); nothing was "
            "deleted"
        )
        return inventory
    for path in top_level:
        if path.is_file():
            inventory.problems.append(
                f"{path}: unrecognized index file; leaving it for manual "
                "inspection"
            )
    manifests = sorted(root.glob("*/*/*/current"))
    for manifest_path in manifests:
        source_directory = manifest_path.parent
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            inventory.problems.append(
                f"{manifest_path}: unreadable index manifest ({error}); "
                "leaving it for manual inspection"
            )
            continue
        if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
            inventory.problems.append(
                f"{manifest_path}: unsupported index manifest; leaving it for "
                "manual inspection"
            )
            continue
        provider = data.get("provider")
        collection_key = data.get("collection_key")
        source_key = data.get("source_key")
        if not all(
            isinstance(value, str) and value
            for value in (provider, collection_key, source_key)
        ):
            inventory.problems.append(
                f"{manifest_path}: index metadata is missing its owner; leaving "
                "it for manual inspection"
            )
            continue
        if provider not in BUILTIN_PROVIDERS:
            inventory.problems.append(
                f"{manifest_path}: unknown provider {provider!r}; leaving it for "
                "manual inspection"
            )
            continue
        expected = locator.source_directory(provider, collection_key, source_key)
        try:
            matches_layout = expected.resolve() == source_directory.resolve()
        except OSError:
            matches_layout = False
        if not matches_layout:
            inventory.problems.append(
                f"{manifest_path}: metadata does not match the index layout; "
                "leaving it for manual inspection"
            )
            continue
        if _namespace_retained(
            collection_key, exact=exact, prefixes=prefixes, retain_all=retain_all
        ):
            inventory.retained.append(collection_key)
            continue
        _owned, unknown = _owned_children(source_directory)
        if unknown:
            inventory.problems.append(
                f"{source_directory}: contains unrecognized file(s) "
                f"({', '.join(sorted(path.name for path in unknown))}); "
                "only recognized Zimantic artifacts are eligible"
            )
        inventory.candidates.append(
            PruneCandidate(
                provider=provider,
                collection_key=collection_key,
                source_key=source_key,
                directory=source_directory,
                bytes=_directory_bytes(source_directory),
                pinned=_is_pinned(source_directory),
            )
        )

    # Old layouts and interrupted staging files have no trusted manifest.  Do
    # not infer ownership from an extension: report them for manual inspection.
    recognized = {manifest.parent.resolve() for manifest in manifests}
    suffixes = (
        ".sqlite",
        ".sqlite-wal",
        ".sqlite-shm",
        ".faiss",
        ".tmp",
        ".upgrade",
        ".staging",
        ".db",
    )
    try:
        for directory, dirnames, filenames in os.walk(root):
            current = Path(directory)
            try:
                if any(
                    current.resolve() == owned
                    or owned in current.resolve().parents
                    for owned in recognized
                ):
                    dirnames[:] = []
                    continue
            except OSError:
                pass
            if current.resolve() == root.resolve():
                continue
            for name in filenames:
                if not name.endswith(suffixes):
                    continue
                inventory.problems.append(
                    f"{current / name}: unrecognized legacy/index file; "
                    "leaving it for manual inspection"
                )
    except OSError:
        pass
    return inventory


def delete_candidates(inventory: PruneInventory) -> tuple[list[PruneCandidate], int]:
    """Delete eligible owned artifacts, skipping pinned or unknown content."""
    removed: list[PruneCandidate] = []
    reclaimed = 0
    for candidate in inventory.candidates:
        if candidate.pinned or _is_pinned(candidate.directory):
            continue
        owned, unknown = _owned_children(candidate.directory)
        if unknown:
            inventory.problems.append(
                f"{candidate.directory}: contains unrecognized file(s) "
                f"({', '.join(sorted(path.name for path in unknown))}); "
                "deleting only recognized Zimantic artifacts"
            )
        deleted = 0
        try:
            for path in owned:
                deleted += _path_bytes(path)
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            if not unknown:
                candidate.directory.rmdir()
            else:
                try:
                    candidate.directory.rmdir()
                except OSError:
                    pass
        except OSError:
            continue
        removed.append(candidate)
        reclaimed += deleted
    for candidate in removed:
        for parent in (
            candidate.directory.parent,
            candidate.directory.parent.parent,
        ):
            try:
                parent.rmdir()
            except OSError:
                pass
    return removed, reclaimed
