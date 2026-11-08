"""Safe, atomic updates to the active ``config.toml``.

Ad-hoc local builds and maintenance commands both need to append a collection
without rewriting unrelated TOML.  The writer intentionally treats the resolved
config target as the lock owner: accessing a symlink and its target therefore
share one lock, and the symlink itself is never replaced.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ._fs import fsync_path
from .locking import BuildLockError, FileLease
from .providers.config import (
    ConfigValidationError,
    ProviderConfig,
    get_provider_config_spec,
)

__all__ = [
    "ConfigWriteError",
    "append_collection",
    "config_lock_path",
    "config_target_path",
    "render_collection_entry",
]


class ConfigWriteError(RuntimeError):
    """The config could not be updated without risking the user's file."""


def config_target_path(config_path: Path | str) -> Path:
    """Return the real file to update, following any symlink."""
    return Path(config_path).expanduser().resolve()


def config_lock_path(config_path: Path | str) -> Path:
    """Return one lock path for a symlink and its direct target."""
    target = config_target_path(config_path)
    return target.with_name(target.name + ".lock")


def _toml_value(value: Any) -> str:
    """Serialize a scalar as a TOML basic value.

    JSON strings are valid TOML basic strings; ``json.dumps`` also handles the
    escaping rules (quotes, backslashes, control characters) without inventing a
    second escaping implementation.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise ConfigWriteError(f"cannot serialize config value {value!r}")


def render_collection_entry(entry: Mapping[str, Any]) -> str:
    """Render one ``[[collections]]`` table in a deterministic key order."""
    if not entry.get("type") or not entry.get("name"):
        raise ConfigWriteError("a collection entry needs a type and name")
    order = [
        "type",
        "name",
        "label",
        "path",
        "server",
        "granularity",
    ]
    keys = [key for key in order if key in entry]
    keys.extend(
        sorted(key for key in entry if key not in keys and key not in {"sources"})
    )
    lines = ["[[collections]]"]
    lines.extend(f"{key} = {_toml_value(entry[key])}" for key in keys)
    return "\n".join(lines) + "\n"


def _copy_metadata(source: Path, target: Path) -> None:
    """Preserve mode and ACL xattrs on an atomic replacement."""
    info = source.stat()
    os.chmod(target, stat.S_IMODE(info.st_mode))
    if not hasattr(os, "listxattr"):
        return
    try:
        names = os.listxattr(source)
    except OSError:
        names = []
    for name in names:
        if name not in {"system.posix_acl_access", "system.posix_acl_default"}:
            continue
        try:
            value = os.getxattr(source, name)
        except OSError:
            continue
        try:
            os.setxattr(target, name, value)
        except OSError as error:
            raise ConfigWriteError(
                f"could not preserve the access ACL {name!r} on {source}: {error}"
            ) from error


def _read_existing(target: Path) -> str:
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as error:
        raise ConfigWriteError(f"could not read {target}: {error}") from error


def _validate(
    text: str, entry: Mapping[str, Any], *, base: Path
) -> ProviderConfig:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigWriteError(f"appending the collection produced invalid TOML: {error}") from error
    spec = get_provider_config_spec(str(entry.get("type")))
    try:
        return spec.parse(entry, base)
    except ConfigValidationError as error:
        raise ConfigWriteError(f"refusing to write an invalid collection: {error}") from error


def append_collection(
    config_path: Path | str,
    entry_factory: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    *,
    warn: Callable[[str], None] | None = None,
) -> ProviderConfig | None:
    """Append one collection under the target-based config write lock.

    ``entry_factory`` runs after the lock is taken and receives the *current*
    parsed TOML document (an empty dict for a missing file).  It must return the
    entry to append or ``None`` when a concurrent change made the append
    unnecessary.  The original file is left untouched on every failure.
    """
    target = config_target_path(config_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLease(
            config_lock_path(config_path), description="config lock"
        )
        lock.acquire(shared=False)
    except (OSError, BuildLockError) as error:
        raise ConfigWriteError(f"could not lock {target}: {error}") from error
    try:
        existing = _read_existing(target)
        try:
            data = tomllib.loads(existing) if existing.strip() else {}
        except tomllib.TOMLDecodeError as error:
            raise ConfigWriteError(
                f"could not update {target}: existing config is malformed: {error}"
            ) from error
        entry = entry_factory(data)
        if entry is None:
            return None
        from .providers.config import parse_collections

        try:
            current = parse_collections(
                data, config_dir=target.parent, warn=lambda _message: None
            )
        except ConfigValidationError as error:
            raise ConfigWriteError(
                f"could not update {target}: {error}"
            ) from error
        name = entry.get("name")
        if any(existing_entry.name == name for existing_entry in current.collections):
            raise ConfigWriteError(
                f"refusing to append collection {name!r}: the name is already "
                "configured"
            )
        rendered = render_collection_entry(entry)
        separator = "\n" if existing and not existing.endswith("\n") else ""
        updated = existing + separator + rendered
        parsed = _validate(updated, entry, base=target.parent)

        source_info = None
        try:
            source_info = target.stat()
        except FileNotFoundError:
            source_info = None
        if source_info is not None and source_info.st_nlink > 1:
            raise ConfigWriteError(
                f"refusing to replace {target}: it has multiple hard links and "
                "an atomic update would silently detach them. Edit the file "
                "directly or break the hard link first."
            )

        temporary: Path | None = None
        try:
            fd, name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            if source_info is not None:
                _copy_metadata(target, temporary)
            else:
                os.chmod(temporary, 0o644)
            fsync_path(temporary)
            if source_info is not None and target.stat().st_nlink > 1:
                raise ConfigWriteError(
                    f"refusing to replace {target}: it gained a hard link "
                    "while the update was being prepared"
                )
            os.replace(temporary, target)
            temporary = None
            fsync_path(target.parent)
        except ConfigWriteError:
            raise
        except OSError as error:
            raise ConfigWriteError(f"could not update {target}: {error}") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        if warn is not None:
            warn(
                f"zimantic: added collection {entry.get('name')!r} to {target}; "
                "restart an already-running server to load the new entry"
            )
        return parsed
    finally:
        lock.release()
