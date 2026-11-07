"""The existing ``zimantic build`` orchestration builds files collections."""

from __future__ import annotations

from types import SimpleNamespace

from zimantic.__main__ import _build_files_collections
from zimantic.indexing.locator import IndexLocator
from zimantic.providers.files.discovery import discover_sources

from .helpers import build_tree, files_config, runtime_config


def test_build_files_collections_uses_the_generic_pipeline(tmp_path):
    root = tmp_path / "modules"
    build_tree(root)
    config = files_config(root)
    index_dir = tmp_path / "indexes"
    runtime_config_value = runtime_config(index_dir, config)
    cfg = {
        "index_dir": str(index_dir),
        "collections": (config,),
        "runtime_config": runtime_config_value,
        "web_base_path": "",
    }
    failures: list = []

    _build_files_collections(
        cfg,
        SimpleNamespace(fast=True, force=False),
        None,
        None,
        failures,
    )

    assert failures == []
    locator = IndexLocator(index_dir)
    for source in discover_sources(config).sources:
        lease = locator.paths(source.info)
        assert lease is not None
        lease.close()
