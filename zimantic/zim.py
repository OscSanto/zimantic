"""Backward-compatible imports for the ZIM content reader.

The implementation lives in :mod:`zimantic.content.reader`; this module keeps
the original import path stable for callers and older indexes.
"""

from .content.reader import *
