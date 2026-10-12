"""Backward-compatible imports for the HTTP application.

The implementation and bundled assets live under :mod:`zimantic.web`.
"""

from .web.app import _handle_restart_request, create_app, serve

__all__ = ["create_app", "serve", "_handle_restart_request"]
