"""Test bootstrap.

FastAPI's ``TestClient`` imports ``httpx``. Our dev dependency is ``httpx2``
(Pydantic's maintained continuation), which exposes ``import httpx`` as an alias
but only after ``alias_httpx()`` is called. Do that before any test module
imports ``starlette.testclient``, unless a real ``httpx`` is already installed.
"""

import sys
import types
from pathlib import Path


try:
    import httpx  # noqa: F401  # ty: ignore[unresolved-import]
except ImportError:  # pragma: no cover - depends on the environment
    import httpx2

    httpx2.alias_httpx()


try:
    import faiss
except ImportError:
    faiss = types.ModuleType("faiss")
    faiss.IO_FLAG_MMAP_IFC = 1
    faiss.IO_FLAG_READ_ONLY = 2
    faiss.METRIC_INNER_PRODUCT = 0

    class _Index:
        def train(self, _vectors):
            pass

        def add_with_ids(self, _vectors, _ids):
            pass

    faiss.index_factory = lambda *_args: _Index()
    faiss.write_index = lambda _index, path: Path(path).write_bytes(b"vectors")
    sys.modules["faiss"] = faiss

try:
    import libzim.reader
    import libzim.search
except ImportError:
    libzim = types.ModuleType("libzim")
    libzim.__path__ = []
    reader = types.ModuleType("libzim.reader")
    reader.Archive = object
    reader.set_cluster_cache_max_size = lambda size: None
    search = types.ModuleType("libzim.search")
    search.Query = object
    search.Searcher = object
    sys.modules["libzim"] = libzim
    sys.modules["libzim.reader"] = reader
    sys.modules["libzim.search"] = search
