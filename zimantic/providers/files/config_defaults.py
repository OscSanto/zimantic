"""Defaults owned by the files provider.

They mirror the recommended values in the phase-05 plan and the commented
``config.toml`` example.  A configured value always wins; these bounds keep an
unbounded tree from producing an unbounded source or item list.
"""

DEFAULT_MAX_SOURCES = 64
DEFAULT_MAX_ENTRIES = 100_000
DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024

__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_SOURCES",
]
