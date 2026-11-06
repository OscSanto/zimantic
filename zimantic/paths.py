"""XDG base directories and legacy path selection."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "zimantic"


def _xdg_home(variable: str, default: str) -> Path:
    value = os.environ.get(variable)
    if value:
        path = Path(value)
        if path.is_absolute():
            return path
    return Path.home() / default


def xdg_config_home() -> Path:
    return _xdg_home("XDG_CONFIG_HOME", ".config")


def xdg_data_home() -> Path:
    return _xdg_home("XDG_DATA_HOME", ".local/share")


def runtime_pid_path() -> Path | None:
    """Return the XDG runtime PID path, or None when no runtime dir is set."""
    if os.name == "nt":
        return None
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value:
        return None
    runtime_dir = Path(value)
    if not runtime_dir.is_absolute():
        return None
    if not runtime_dir.is_dir():
        return None
    return runtime_dir / APP_NAME / "zimantic.pid"


def default_config_path(cwd: Path | None = None) -> tuple[Path, bool]:
    """Choose XDG config first, then an existing cwd config for compatibility.

    The boolean indicates that the selected file is the legacy cwd config.
    """
    cwd = cwd or Path.cwd()
    legacy = cwd / "config.toml"
    if os.name == "nt":
        return legacy, True

    xdg = xdg_config_home() / APP_NAME / "config.toml"
    if xdg.is_file():
        return xdg, False
    if legacy.is_file():
        return legacy, True
    return xdg, False


def reload_pid_path(explicit: Path | None = None) -> Path:
    """Resolve the PID file, checking the old cwd location during migration."""
    if explicit is not None:
        return explicit
    runtime = runtime_pid_path()
    legacy = Path("zimantic.pid")
    if runtime is not None and runtime.is_file():
        return runtime
    if legacy.is_file():
        return legacy
    return runtime or legacy


def server_pid_path() -> Path:
    """Return the PID file location used by a newly started server."""
    return runtime_pid_path() or Path("zimantic.pid")


def xdg_data_dir() -> Path:
    return xdg_data_home() / APP_NAME
