"""Bounded, namespace-aware ZIM path resolution.

The resolver keeps one canonical final path, treats redirect aliases as
aliases of that path, and fails closed when several distinct final items claim
the same canonical path.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..redirects import RedirectError, resolve_redirect_entry

MAX_PATH_HOPS = 32
# Old and new ZIM namespaces that may prefix an otherwise bare path.
_NAMESPACE_PREFIXES: tuple[str, ...] = ("", "A/", "C/", "I/", "J/", "-/")


class ZimPathError(ValueError):
    """A requested ZIM path cannot be resolved safely."""


class ZimPathNotFoundError(ZimPathError):
    """The path does not name a readable ZIM entry."""


class AmbiguousZimPathError(ZimPathError):
    """Several distinct final entries share one canonical path."""


def _candidates(path: str) -> Iterable[str]:
    cleaned = str(path).lstrip("/")
    if not cleaned or "\\" in cleaned:
        raise ZimPathNotFoundError("invalid ZIM path")
    trailing = cleaned.endswith("/")
    normalized = cleaned.rstrip("/") if trailing else cleaned
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ZimPathNotFoundError("invalid ZIM path")
    seen: set[str] = set()
    first = cleaned if trailing else normalized
    if first not in seen:
        seen.add(first)
        yield first
    if trailing:
        # An archive may store the entry with or without its trailing slash.
        if normalized not in seen:
            seen.add(normalized)
            yield normalized
    # A bare article path may live under a namespace; try each bounded prefix.
    if "/" not in normalized or normalized.split("/", 1)[0] not in {
        "A", "C", "I", "J", "-"
    }:
        for prefix in _NAMESPACE_PREFIXES:
            candidate = prefix + normalized
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def resolve_zim_path(
    archive: Any,
    requested_path: str | None,
    *,
    max_hops: int = MAX_PATH_HOPS,
) -> Any:
    """Return the final non-redirect entry for ``requested_path``.

    ``None`` selects the archive's main entry.  Redirect aliases collapse to the
    final path; distinct final entries that resolve to the same path fail closed.
    """
    try:
        entry = (
            archive.main_entry
            if requested_path is None
            else None
        )
    except (KeyError, RuntimeError, ValueError) as error:
        raise ZimPathNotFoundError("article not found") from error
    if entry is None:
        lookup = getattr(archive, "get_entry_by_path", None)
        if lookup is None:
            raise ZimPathNotFoundError("article not found")
        last_error: Exception | None = None
        for candidate in _candidates(str(requested_path)):
            try:
                entry = lookup(candidate)
                break
            except (KeyError, RuntimeError, ValueError) as error:
                last_error = error
        if entry is None:
            raise ZimPathNotFoundError("article not found") from last_error
    try:
        final = resolve_redirect_entry(entry)
    except RedirectError as error:
        raise ZimPathError(str(error)) from error
    path = getattr(final, "path", None)
    if not path:
        raise ZimPathNotFoundError("article not found")
    if final is not entry:
        # The final path must not itself be a different redirect target from an
        # independent lookup; a mismatch means the archive is inconsistent.
        try:
            direct = archive.get_entry_by_path(path)
        except (KeyError, RuntimeError, ValueError):
            direct = final
        try:
            direct_final = resolve_redirect_entry(direct)
        except RedirectError as error:
            raise AmbiguousZimPathError(str(error)) from error
        if getattr(direct_final, "path", None) != path:
            raise AmbiguousZimPathError("ambiguous ZIM path")
    return final


__all__ = [
    "MAX_PATH_HOPS",
    "AmbiguousZimPathError",
    "ZimPathError",
    "ZimPathNotFoundError",
    "resolve_zim_path",
]
