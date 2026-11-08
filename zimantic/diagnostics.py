"""Public-safe diagnostic text helpers.

Absolute provider roots, index directories, and private snapshot/database
locators are implementation details.  They may be used internally (and by
``catalog.sqlite``), but they must not leak into API payloads, redirects,
ETags, or normal logs. Embedding model load warnings are an exception: their
raw errors are logged to expose the model file path for troubleshooting. This
module owns the redaction policy for the other public-facing surfaces.

Raw exception details may still be useful when debugging.  Callers should log
the redacted public cause at warning/error level and the untouched exception at
debug level, except for embedding model load warnings.
"""

from __future__ import annotations

import errno
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .providers.config import RuntimeConfig

PRIVATE_PLACEHOLDER = "<private>"

# Windows drive paths and UNC paths.  POSIX paths are handled by the explicit
# locator list below so a public URL such as ``/search`` is never mistaken for
# a private filesystem path.
_WINDOWS_PATH = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/][^\s\"'<>|]*|\\\\[^\s\"'<>|]+)"
)


def _add_path(values: set[str], value: Any) -> None:
    if not isinstance(value, (str, os.PathLike)):
        return
    text = os.fspath(value)
    if not text or text.startswith(":"):
        return
    values.add(text)
    expanded = os.path.expanduser(text)
    if expanded != text:
        values.add(expanded)
    try:
        absolute = os.path.abspath(expanded)
    except OSError:
        return
    values.add(absolute)
    try:
        values.add(os.path.realpath(absolute))
    except OSError:
        pass


def private_locators(config: RuntimeConfig) -> tuple[str, ...]:
    """Return configured private paths for diagnostic string replacement."""
    values: set[str] = set()
    for key in ("zim_dir", "model_dir", "index_dir"):
        _add_path(values, config.get(key))
    for entry in config.collections:
        for attribute in ("path", "database_path", "storage"):
            _add_path(values, getattr(entry, attribute, None))
    _add_path(values, config.config_path)
    _add_path(values, config.config_dir)
    # Longest first so a nested locator is replaced before its parent.
    return tuple(sorted(values, key=len, reverse=True))


def sanitize_public_text(
    value: Any,
    locators: Iterable[str] = (),
    *,
    replacement: str = PRIVATE_PLACEHOLDER,
) -> Any:
    """Redact configured private locators from one public-facing string."""
    if not isinstance(value, str):
        return value
    result = value
    for locator in sorted(set(locators), key=len, reverse=True):
        if locator:
            result = result.replace(locator, replacement)
    # A normalized variant may appear when an error message was built from a
    # resolved path rather than the configured spelling.
    for locator in sorted(set(locators), key=len, reverse=True):
        if not locator:
            continue
        try:
            normalized = os.path.normpath(locator)
        except (OSError, ValueError):
            continue
        if normalized and normalized != locator:
            result = result.replace(normalized, replacement)
    result = _WINDOWS_PATH.sub(replacement, result)
    return result


def _safe_os_cause(error: OSError) -> str:
    """Describe an OS error without its filename-bearing message."""
    if error.errno == errno.ENOENT:
        return "not found"
    if error.errno in {errno.EACCES, errno.EPERM}:
        return "permission denied"
    if error.errno in {errno.ENOSPC, errno.EDQUOT}:
        return "storage is full"
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return "unsafe or invalid path"
    if error.strerror:
        return str(error.strerror)
    return "I/O error"


def public_cause(
    error: BaseException,
    locators: Iterable[str] = (),
    *,
    fallback: str = "operation failed",
) -> str:
    """Return a stable, path-safe description of an exception.

    The result is suitable for warnings and API payloads.  Callers that need
    the raw exception should log it at debug level.
    """
    if isinstance(error, FileNotFoundError):
        return "not found"
    if isinstance(error, PermissionError):
        return "permission denied"
    if isinstance(error, TimeoutError):
        return "timed out"
    if isinstance(error, sqlite3.Error):
        return "database unavailable"
    if isinstance(error, OSError):
        return _safe_os_cause(error)
    text = sanitize_public_text(str(error), locators).strip()
    if not text:
        return fallback
    return text


def relative_display(root: Path | str | None, path: Path | str | None) -> str:
    """Return a display path relative to ``root`` without exposing an absolute."""
    if path is None:
        return "<unknown>"
    if root is None:
        return Path(path).name or "<unknown>"
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError):
        return Path(path).name or "<unknown>"


__all__ = [
    "PRIVATE_PLACEHOLDER",
    "private_locators",
    "public_cause",
    "relative_display",
    "sanitize_public_text",
]
