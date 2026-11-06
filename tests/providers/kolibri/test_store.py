"""Kolibri content-store serving, membership, ranges, and HTML5 confinement."""

from __future__ import annotations

import io
import zipfile

import pytest

from zimantic.content.store import (
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
)
from zimantic.providers.kolibri.discovery import (
    kolibri_node_source_key,
    kolibri_root_key,
)
from zimantic.providers.kolibri.html5 import Html5ZipError
from zimantic.providers.kolibri.store import KolibriContentStore

from .helpers import KolibriFixture, kolibri_config, make_two_channel_fixture, open_runtime


def _runtime(tmp_path, fixture):
    index_dir = tmp_path / "indexes"
    runtime, batch, _ = open_runtime(index_dir, kolibri_config(fixture))
    return runtime, batch


def _source(runtime, channel: str, node: str):
    key = kolibri_node_source_key("kolibri", channel, node)
    return next(item for item in runtime.library_sources() if item.key == key)


def test_document_ranges_and_cross_source_membership(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    runtime, batch = _runtime(tmp_path, fixture)
    try:
        topic = _source(runtime, "channel-a", "topic-1")
        store = runtime.content_store(topic)
        try:
            entry = store.resolve("node/doc-1")
            assert entry.mimetype == "application/pdf"
            data = store.read("node/doc-1", head_only=False, max_bytes=1 << 20)
            assert data.content.startswith(b"%PDF")
            partial = store.read_range(
                "node/doc-1", start=2, end=6, max_bytes=1 << 20
            )
            assert partial.content == data.content[2:7]
            with pytest.raises(ArticleNotFoundError):
                store.resolve("node/video-1")
        finally:
            store.close()
    finally:
        runtime.close()
        batch.close()


def test_missing_file_is_item_level_not_source_wide(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    runtime, batch = _runtime(tmp_path, fixture)
    try:
        source = _source(runtime, "channel-a", "topic-1")
        store = runtime.content_store(source)
        try:
            data = store.read("node/doc-1", head_only=False, max_bytes=1 << 20)
            assert data.content
            # Remove only the selected storage file.
            for path in (fixture.storage).rglob("*.pdf"):
                path.unlink()
            with pytest.raises(ArticleNotFoundError):
                store.read("node/doc-1", head_only=False, max_bytes=1 << 20)
            # Other nodes in other sources remain available.
            assert source.item_count == 1
        finally:
            store.close()
    finally:
        runtime.close()
        batch.close()


def test_html5_zip_entry_assets_and_ranges(tmp_path):
    fixture = KolibriFixture(tmp_path)
    fixture.add_channel("channel-a", "Channel A", "root-a")
    fixture.add_node("root-a", "channel-a", "Channel A", kind="topic", lft=1, rght=4, level=0)
    fixture.add_node("app", "channel-a", "HTML5 App", kind="html5", parent_id="root-a", lft=2, rght=3)
    fixture.add_html5("app")
    fixture.close()
    runtime, batch = _runtime(tmp_path, fixture)
    try:
        source = runtime.library_sources()[0]
        store = runtime.content_store(source)
        try:
            root = store.resolve("node/app")
            assert root.path == "node/app/index.html"
            assert root.redirect_path == "node/app"
            index = store.resolve("node/app/index.html")
            assert index.mimetype == "text/html"
            content = store.read(
                "node/app/index.html", head_only=False, max_bytes=1 << 20
            ).content
            assert b"HTML5 zebra" in content
            css = store.read(
                "node/app/style.css", head_only=False, max_bytes=1 << 20
            )
            assert b"color" in css.content
            assert store.resolve("node/app/nested/app.js").mimetype == (
                "application/javascript"
            )
            partial = store.read_range(
                "node/app/index.html", start=0, end=10, max_bytes=1 << 20
            )
            assert partial.content == content[:11]
        finally:
            store.close()
    finally:
        runtime.close()
        batch.close()


def _zip_bytes(files: list[tuple[str, bytes]], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, data in files:
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "files",
    [
        [("index.html", b"ok"), ("../escape.css", b"bad")],
        [("index.html", b"ok"), ("a/../../escape.css", b"bad")],
        [("index.html", b"ok"), ("index.html", b"duplicate")],
        [("index.html", b"ok"), ("/absolute.css", b"bad")],
        [("index.html", b"ok"), ("a%2fescape.css", b"bad")],
    ],
)
def test_html5_zip_unsafe_members_fail_closed(tmp_path, files):
    fixture = KolibriFixture(tmp_path)
    fixture.add_channel("channel-a", "Channel A", "root-a")
    fixture.add_node("root-a", "channel-a", "Channel A", kind="topic", lft=1, rght=4, level=0)
    fixture.add_node("app", "channel-a", "HTML5 App", kind="html5", parent_id="root-a", lft=2, rght=3)
    fixture.add_file("app", _zip_bytes(files), "zip", preset="html5_zip")
    fixture.close()
    runtime, batch = _runtime(tmp_path, fixture)
    try:
        source = runtime.library_sources()[0]
        store = runtime.content_store(source)
        try:
            with pytest.raises((ArticleUnavailableError, ArticleNotFoundError)):
                store.resolve("node/app")
        finally:
            store.close()
    finally:
        runtime.close()
        batch.close()


def test_html5_symlink_member_is_rejected(tmp_path):
    fixture = KolibriFixture(tmp_path)
    fixture.add_channel("channel-a", "Channel A", "root-a")
    fixture.add_node("root-a", "channel-a", "Channel A", kind="topic", lft=1, rght=4, level=0)
    fixture.add_node("app", "channel-a", "HTML5 App", kind="html5", parent_id="root-a", lft=2, rght=3)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("index.html", b"ok")
        link = zipfile.ZipInfo("link")
        link.external_attr = (0o120777 << 16)
        archive.writestr(link, b"target")
    fixture.add_file("app", buffer.getvalue(), "zip", preset="html5_zip")
    fixture.close()
    runtime, batch = _runtime(tmp_path, fixture)
    try:
        source = runtime.library_sources()[0]
        store = runtime.content_store(source)
        try:
            with pytest.raises((ArticleUnavailableError, ArticleNotFoundError)):
                store.resolve("node/app")
        finally:
            store.close()
    finally:
        runtime.close()
        batch.close()


def test_html5_limits_and_stored_range(tmp_path):
    fixture = KolibriFixture(tmp_path)
    fixture.add_channel("channel-a", "Channel A", "root-a")
    fixture.add_node("root-a", "channel-a", "Channel A", kind="topic", lft=1, rght=8, level=0)
    fixture.add_node("stored", "channel-a", "Stored", kind="html5", parent_id="root-a", lft=2, rght=3)
    fixture.add_node("compressed", "channel-a", "Compressed", kind="html5", parent_id="root-a", lft=4, rght=5)
    fixture.add_node("many", "channel-a", "Many", kind="html5", parent_id="root-a", lft=6, rght=7)
    fixture.add_file(
        "stored",
        _zip_bytes(
            [("index.html", b"hello stored world")],
            compression=zipfile.ZIP_STORED,
        ),
        "zip",
        preset="html5_zip",
    )
    fixture.add_file(
        "compressed",
        _zip_bytes([("index.html", b"hello compressed world")]),
        "zip",
        preset="html5_zip",
    )
    fixture.add_file(
        "many",
        _zip_bytes(
            [("index.html", b"ok")]
            + [(f"f{index}.txt", b"x") for index in range(8)]
        ),
        "zip",
        preset="html5_zip",
    )
    fixture.close()
    index_dir = tmp_path / "indexes"
    config = kolibri_config(
        fixture,
        max_html5_zip_entries=4,
        max_html5_zip_member_bytes=100,
        max_html5_zip_total_bytes=1000,
    )
    runtime, batch, _ = open_runtime(index_dir, config)
    try:
        key = kolibri_root_key("kolibri", "channel-a")
        source = next(item for item in runtime.library_sources() if item.key == key)
        store = runtime.content_store(source)
        try:
            data = store.read("node/stored/index.html", head_only=False, max_bytes=1 << 20)
            partial = store.read_range(
                "node/stored/index.html", start=6, end=11, max_bytes=3
            )
            assert partial.content == b"sto"
            assert partial.total == len(data.content)
            with pytest.raises(ArticleTooLargeError):
                store.read_range(
                    "node/compressed/index.html",
                    start=0,
                    end=2,
                    max_bytes=4,
                )
            with pytest.raises((ArticleUnavailableError, Html5ZipError)):
                store.resolve("node/many")
        finally:
            store.close()
    finally:
        runtime.close()
        batch.close()


def test_unsupported_html5_preset_is_explicit(tmp_path):
    fixture = KolibriFixture(tmp_path)
    fixture.add_channel("channel-a", "Channel A", "root-a")
    fixture.add_node("root-a", "channel-a", "Channel A", kind="topic", lft=1, rght=4, level=0)
    fixture.add_node("h5p", "channel-a", "H5P", kind="h5p", parent_id="root-a", lft=2, rght=3)
    fixture.add_file("h5p", b"fake h5p", "h5p", preset="h5p")
    fixture.close()
    runtime, batch = _runtime(tmp_path, fixture)
    try:
        source = runtime.library_sources()[0]
        store = runtime.content_store(source)
        try:
            with pytest.raises(ArticleUnavailableError) as error:
                store.resolve("node/h5p")
            assert "unsupported" in str(error.value).lower()
        finally:
            store.close()
    finally:
        runtime.close()
        batch.close()


def test_channel_and_topic_icons_are_bounded(tmp_path):
    fixture = KolibriFixture(tmp_path)
    fixture.add_channel("channel-a", "Channel A", "root-a")
    fixture.add_node("root-a", "channel-a", "Channel A", kind="topic", lft=1, rght=6, level=0)
    fixture.add_node("topic-1", "channel-a", "Topic One", kind="topic", parent_id="root-a", lft=2, rght=5)
    fixture.add_node("doc", "channel-a", "Doc", kind="document", parent_id="topic-1", lft=3, rght=4)
    fixture.add_document("doc", "text")
    fixture.add_file("root-a", b"channel-icon", "png", preset="channel_thumbnail", thumbnail=True)
    fixture.add_file("topic-1", b"topic-icon", "png", preset="topic_thumbnail", thumbnail=True)
    fixture.close()
    runtime, batch = _runtime(tmp_path, fixture)
    try:
        topic = _source(runtime, "channel-a", "topic-1")
        store = runtime.content_store(topic)
        try:
            icon = store.icon(4)
            assert icon is not None
            assert icon.content == b"topi"
        finally:
            store.close()
    finally:
        runtime.close()
        batch.close()
