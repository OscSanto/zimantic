"""Content preview defaults shared by readers and search configuration.

This module deliberately avoids importing libzim so callers that only need the
numeric limits (such as :mod:`zimantic.search`) stay importable without the
native runtime dependency.
"""

DEFAULT_PREVIEW_CHARS = 1000
DEFAULT_MAX_HTML_BYTES = 4 << 20
