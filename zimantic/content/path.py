"""Shared content-path normalization and request-path safety helpers.

Every content route and store uses these rules.  A request path is decoded
exactly once at the HTTP boundary and validated here; no later layer decodes it
again.  Literal percent signs in an entry name therefore remain literal, while
an encoded path separator is rejected before the ASGI server turns it into an
ordinary separator.
"""

from __future__ import annotations

import posixpath
import re

from .store import ArticleNotFoundError

# Percent-encoded "/" and "\".  The raw request target still contains these
# bytes, while the decoded path FastAPI passes to a route no longer can reveal
# whether a separator was encoded.
_ENCODED_SEPARATOR = re.compile(r"%2f|%5c", re.IGNORECASE)


def has_encoded_separator(raw_path: bytes | str | None) -> bool:
    """True when a raw request target encodes a path separator."""
    if raw_path is None:
        return False
    if isinstance(raw_path, bytes):
        raw_path = raw_path.decode("latin-1", "ignore")
    return bool(_ENCODED_SEPARATOR.search(str(raw_path)))


def raw_request_path(request) -> bytes | str | None:
    """Return the server-preserved request target from an ASGI request."""
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return None
    raw_path = scope.get("raw_path")
    if raw_path is not None:
        return raw_path
    # Some test clients preserve only the decoded path.  That cannot prove the
    # absence of encoded separators, so callers that need the guarantee must
    # treat this as untrustworthy rather than silently accepting it.
    return None


def normalize_archive_path(path: str) -> str | None:
    """Normalize an archive/href path, allowing site-absolute inputs.

    This is used while resolving an authored HTML href, so a leading slash is
    meaningful.  It returns ``None`` for an empty or escaping path.
    """
    candidate = str(path).lstrip("/")
    if not candidate or "\x00" in candidate:
        return None
    normalized = posixpath.normpath(candidate)
    if (
        normalized in {"", ".", ".."}
        or normalized.startswith("../")
        or normalized.startswith("/")
    ):
        return None
    return normalized


def normalize_article_path(path: str) -> str:
    """Validate one decoded content path and return its canonical form.

    Request paths are relative.  The ASGI server already percent-decodes the
    request path before it reaches this function, so it must not decode again:
    entries whose names contain literal escapes (for example
    ``mindtouch.page%23thumbnail``) stay addressable.  A trailing slash is
    preserved because some sources store directory-style homepage entries.
    """
    path = str(path)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
    ):
        raise ArticleNotFoundError("invalid article path")
    trailing_slash = path.endswith("/")
    candidate = path[:-1] if trailing_slash else path
    if not candidate or any(part == ".." for part in candidate.split("/")):
        raise ArticleNotFoundError("invalid article path")
    normalized = normalize_archive_path(candidate)
    if normalized is None or normalized != candidate:
        raise ArticleNotFoundError("invalid article path")
    return normalized + "/" if trailing_slash else normalized


__all__ = [
    "has_encoded_separator",
    "normalize_archive_path",
    "normalize_article_path",
    "raw_request_path",
]
