"""Shared redirect-chain resolution for libzim entries."""

from __future__ import annotations

from typing import Any

MAX_REDIRECT_HOPS = 32


class RedirectError(ValueError):
    """A redirect chain is cyclic, broken, or longer than the supported limit."""


def resolve_redirect_entry(entry: Any) -> Any:
    """Follow ``entry`` to its final non-redirect target.

    Serving, indexing, and catalog homepage discovery all need the same
    semantics: libzim permits redirect-to-redirect chains, and each caller used
    to carry its own copy of the 32-hop loop. Keeping the rule here prevents the
    copies from drifting apart.
    """
    seen: set[str] = set()
    current = entry
    for _ in range(MAX_REDIRECT_HOPS):
        if not getattr(current, "is_redirect", False):
            return current
        path = getattr(current, "path", "")
        if path in seen:
            raise RedirectError("article redirect cycle")
        seen.add(path)
        try:
            current = current.get_redirect_entry()
        except (KeyError, RuntimeError, ValueError) as error:
            raise RedirectError("article redirect is unavailable") from error
    raise RedirectError("article redirect chain is too long")
