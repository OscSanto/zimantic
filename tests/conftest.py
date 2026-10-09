"""Test bootstrap.

FastAPI's ``TestClient`` imports ``httpx``. Our dev dependency is ``httpx2``
(Pydantic's maintained continuation), which exposes ``import httpx`` as an alias
but only after ``alias_httpx()`` is called. Do that before any test module
imports ``starlette.testclient``, unless a real ``httpx`` is already installed.
"""

try:
    import httpx  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    import httpx2

    httpx2.alias_httpx()
