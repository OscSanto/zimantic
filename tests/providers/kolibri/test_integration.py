"""Kolibri search integration, reload, and public source metadata."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from zimantic.search import Search
from zimantic.web.app import create_app

from .helpers import (
    build_sources,
    kolibri_config,
    make_metadata_fixture,
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


@pytest.mark.parametrize("fast", [False, True])
def test_search_enriches_existing_indexes_and_streams_metadata(tmp_path, fast):
    fixture = make_metadata_fixture(tmp_path)
    config = kolibri_config(fixture)
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(index_dir, config)
    try:
        build_sources(runtime, index_dir, fast=fast)
    finally:
        runtime.close()
        batch.close()
    # Display metadata comes from discovery, not from the published index.
    fixture.db.execute(
        "UPDATE content_contentnode SET author = 'Updated author' WHERE id = 'count'"
    )
    fixture.db.commit()
    search = _search(tmp_path, config)
    try:
        with TestClient(create_app(search)) as client:
            response = client.get("/search", params={"q": "Count with small numbers"})
            assert response.status_code == 200
            result = next(doc for doc in response.json() if doc["path"] == "node/count")
            assert result["breadcrumbs"] == [
                "Math", "Unit 1", "Counting", "Count with small numbers",
            ]
            assert result["resource"] == {
                "kind": "exercise",
                "description": "Practice counting small groups of objects.",
                "author": "Updated author",
                "language": "English",
                "duration": 125,
                "grade_levels": ["Lower Primary"],
                "learning_activities": ["Practice", "Read"],
                "tags": ["arithmetic", "numeracy"],
                "assessment_count": 8,
                "topic_position": 2,
                "topic_total": 2,
            }
            assert result["lead"]
            assert str(fixture.home) not in response.text
            events = list(search.stream_search("small numbers", limit=10))
            pages = [event["results"] for event in events if event.get("results")]
            assert pages
            assert all(
                doc["resource"]["kind"] == "exercise" and doc["breadcrumbs"][2] == "Counting"
                for page in pages for doc in page if doc["path"] == "node/count"
            )
            fixture.db.execute(
                "UPDATE content_contentnode SET description = 'New description' WHERE id = 'count'"
            )
            fixture.db.commit()
            assert search.search_page("Count with small numbers")["results"][0][
                "resource"
            ]["description"]
            search.reload()
            updated = search.search_page("Count with small numbers")["results"][0]
            assert updated["resource"]["description"]
            assert updated["lead"]
    finally:
        search.close()
        fixture.dispose()


def test_metadata_checks_membership_and_missing_or_unreadable_previews(tmp_path):
    fixture = make_metadata_fixture(tmp_path)
    fixture.db.execute(
        "UPDATE content_contentnode SET kind = 'html5', title = 'Interactive counting', "
        "description = 'Interactive description' WHERE id = 'subtopic'"
    )
    fixture.add_html5("subtopic", {"index.html": b"<html><body><script>start()</script></body></html>"})
    fixture.db.commit()
    runtime, batch, _ = open_runtime(tmp_path / "indexes", kolibri_config(fixture))
    try:
        source = next(item for item in runtime.library_sources() if item.key.endswith(":unit"))
        documents = [
            {"path": f"node/{node_id}"}
            for node_id in ("count", "coach", "unavailable", "outside", "missing", "count/asset")
        ]
        runtime._enrich_metadata(source, documents)
        assert "resource" in documents[0]
        assert all("resource" not in doc for doc in documents[1:])
        documents = [{"path": "node/count"}]
        runtime._enrich_metadata(source, documents, deadline=time.monotonic() - 1)
        assert "resource" not in documents[0]
        for path, expected in (
            ("node/count", "Practice counting small groups of objects."),
            ("node/subtopic", "Interactive description"),
        ):
            lead, _mime = runtime.preview(
                source, {"path": path}, max_chars=1000, max_bytes=1 << 20, deadline=None,
            )
            assert lead == expected
        lead, _mime = runtime.preview(
            source, {"path": "node/intro"}, max_chars=1000, max_bytes=1 << 20, deadline=None,
        )
        assert lead
    finally:
        runtime.close()
        batch.close()
        fixture.dispose()
