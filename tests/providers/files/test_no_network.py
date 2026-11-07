"""Files discovery, indexing, and serving never open a network socket."""

from __future__ import annotations

import socket

from zimantic.indexing.pipeline import build_index
from zimantic.indexing.locator import IndexLocator
from zimantic.providers.files.entries import FilesEntryProducer
from zimantic.providers.files.discovery import discover_sources
from zimantic.providers.files.store import FilesContentStore

from .helpers import build_tree, files_config


def test_files_operations_do_not_use_the_network(tmp_path, monkeypatch):
    root = tmp_path / "modules"
    build_tree(root)
    config = files_config(root, granularity="collection")
    index_dir = tmp_path / "indexes"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", forbidden)
    discovery = discover_sources(config)
    source = discovery.sources[0]
    producer = FilesEntryProducer(
        source.info,
        root,
        membership_root=source.membership_root,
        loose_only=source.loose_only,
        config=config,
        fast=False,
        cfg={"max_entry_bytes": 1 << 20},
    )
    build_index(producer, IndexLocator(index_dir), None, {"batch_size": 1}, fast=True)
    store = FilesContentStore(
        source.info,
        root,
        membership_root=source.membership_root,
        loose_only=source.loose_only,
        config=config,
        max_article_bytes=1 << 20,
    )
    try:
        assert store.resolve("loose.txt").mimetype == "text/plain"
        assert store.read("loose.txt", head_only=False, max_bytes=1 << 20).content
    finally:
        store.close()
