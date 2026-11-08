"""Kolibri search integration, reload, and public source metadata."""

from __future__ import annotations

import json

from zimantic.search import Search

from .helpers import (
    build_sources,
    kolibri_config,
    make_two_channel_fixture,
    open_runtime,
    runtime_config,
)


def _search(tmp_path, config):
    index_dir = tmp_path / "indexes"
    return Search(
        runtime_config(index_dir, config, web_base_path="", page_size=10),
        semantic=False,
    )


def test_source_metadata_and_public_payload_hide_locators(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(index_dir, kolibri_config(fixture))
    try:
        build_sources(runtime, index_dir)
        sources = list(runtime.library_sources())
        assert any(source.searchable for source in sources)
        for source in sources:
            payload = json.dumps(
                source.to_dict(include_metadata=True), sort_keys=True
            )
            assert str(fixture.home) not in payload
            assert str(fixture.storage) not in payload
            assert source.provider == "kolibri"
            assert source.media_types is not None
            assert source.item_count is not None
    finally:
        runtime.close()
        batch.close()


def test_reload_picks_up_a_newly_installed_channel(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    config = kolibri_config(fixture, granularity="collection")
    search = _search(tmp_path, config)
    try:
        assert set(search.collections) == {
            "kolibri:kolibri:channel-a",
            "kolibri:kolibri:channel-b",
        }
        fixture.add_channel("channel-c", "Channel C", "root-c")
        fixture.add_node(
            "root-c",
            "channel-c",
            "Channel C",
            kind="topic",
            lft=1,
            rght=4,
            level=0,
            tree_id=3,
        )
        fixture.add_node(
            "doc-c",
            "channel-c",
            "Doc C",
            kind="document",
            parent_id="root-c",
            lft=2,
            rght=3,
            tree_id=3,
        )
        fixture.add_document("doc-c", "channel c content", extension="txt")
        fixture.db.commit()
        search.reload()
        assert "kolibri:kolibri:channel-c" in search.collections
    finally:
        for runtime in list(search.runtimes.values()):
            runtime.close()
        for batch in list(getattr(search, "_discovery_batches", ())):
            batch.close()
