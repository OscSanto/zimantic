"""Synthetic v0.19.5-shaped Kolibri installations for provider tests."""

from __future__ import annotations

import hashlib
import io
import logging
import sqlite3
import zipfile
from pathlib import Path

from zimantic.providers.base import ProviderContext
from zimantic.providers.config import KolibriConfig, RuntimeConfig
from zimantic.providers.kolibri import KolibriProvider
from zimantic.providers.kolibri.discovery import ChannelRow, SourceScope

from media_helpers import sample

SCHEMA = """
CREATE TABLE django_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app TEXT NOT NULL,
    name TEXT NOT NULL,
    applied TEXT NOT NULL
);
CREATE TABLE content_language (
    id TEXT PRIMARY KEY,
    lang_code TEXT,
    lang_subcode TEXT,
    lang_name TEXT,
    lang_direction TEXT
);
CREATE TABLE content_channelmetadata (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    tagline TEXT,
    author TEXT,
    version INTEGER DEFAULT 0,
    thumbnail TEXT DEFAULT '',
    last_updated TEXT,
    min_schema_version TEXT DEFAULT '5',
    root_id TEXT,
    published_size INTEGER DEFAULT 0,
    total_resource_count INTEGER DEFAULT 0,
    included_categories TEXT,
    included_grade_levels TEXT,
    "order" INTEGER DEFAULT 0,
    public INTEGER,
    library TEXT,
    partial INTEGER DEFAULT 0
);
CREATE TABLE content_contentnode (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    tree_id INTEGER,
    level INTEGER,
    lft INTEGER,
    rght INTEGER,
    title TEXT,
    coach_content INTEGER DEFAULT 0,
    content_id TEXT,
    channel_id TEXT,
    description TEXT,
    sort_order REAL,
    license_owner TEXT,
    author TEXT,
    kind TEXT,
    available INTEGER DEFAULT 1,
    lang_id TEXT,
    options TEXT,
    grade_levels TEXT,
    resource_types TEXT,
    learning_activities TEXT,
    accessibility_labels TEXT,
    categories TEXT,
    learner_needs TEXT,
    duration INTEGER,
    num_coach_contents INTEGER DEFAULT 0,
    on_device_resources INTEGER DEFAULT 0,
    ancestors TEXT,
    admin_imported INTEGER,
    modality TEXT
);
CREATE TABLE content_file (
    id TEXT PRIMARY KEY,
    local_file_id TEXT NOT NULL,
    contentnode_id TEXT NOT NULL,
    preset TEXT,
    lang_id TEXT,
    supplementary INTEGER DEFAULT 0,
    thumbnail INTEGER DEFAULT 0,
    priority INTEGER
);
CREATE TABLE content_localfile (
    id TEXT PRIMARY KEY,
    extension TEXT,
    available INTEGER DEFAULT 1,
    file_size INTEGER
);
"""


def id_for(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()[:32]


class KolibriFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "kolibri"
        self.home.mkdir(parents=True)
        self.storage = self.home / "content" / "storage"
        self.storage.mkdir(parents=True)
        self.db_path = self.home / "db.sqlite3"
        self.db = sqlite3.connect(self.db_path)
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT INTO django_migrations (app, name, applied) VALUES (?, ?, ?)",
            ("content", "0001_initial", "2020-01-01"),
        )
        self._counter = 0

    def close(self) -> None:
        # Flush the fixture without preventing later test edits.
        self.db.commit()

    def dispose(self) -> None:
        self.db.close()

    def add_channel(
        self,
        channel_id: str,
        name: str,
        root_id: str,
        *,
        description: str = "",
        version: int = 1,
    ) -> None:
        self.db.execute(
            "INSERT INTO content_channelmetadata "
            "(id, name, description, author, version, root_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, name, description, "Author", version, root_id),
        )

    def add_node(
        self,
        node_id: str,
        channel_id: str,
        title: str,
        *,
        kind: str = "document",
        parent_id: str | None = None,
        tree_id: int = 1,
        level: int = 1,
        lft: int = 1,
        rght: int = 2,
        description: str = "",
        available: bool = True,
        coach_content: bool = False,
        content_id: str | None = None,
        sort_order: float | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO content_contentnode "
            "(id, parent_id, tree_id, level, lft, rght, title, coach_content, "
            "content_id, channel_id, description, sort_order, kind, available) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id,
                parent_id,
                tree_id,
                level,
                lft,
                rght,
                title,
                int(coach_content),
                content_id or node_id,
                channel_id,
                description,
                sort_order,
                kind,
                int(available),
            ),
        )

    def add_file(
        self,
        node_id: str,
        data: bytes,
        extension: str,
        *,
        preset: str,
        priority: int | None = None,
        supplementary: bool = False,
        thumbnail: bool = False,
        available: bool = True,
        lang_id: str | None = None,
    ) -> str:
        checksum = hashlib.md5(data + extension.encode()).hexdigest()
        filename = f"{checksum}.{extension}"
        target = self.storage / filename[0] / filename[1]
        target.mkdir(parents=True, exist_ok=True)
        (target / filename).write_bytes(data)
        self.db.execute(
            "INSERT OR REPLACE INTO content_localfile "
            "(id, extension, available, file_size) VALUES (?, ?, ?, ?)",
            (checksum, extension, int(available), len(data)),
        )
        self._counter += 1
        file_id = id_for(f"file-{self._counter}-{node_id}")
        self.db.execute(
            "INSERT INTO content_file "
            "(id, local_file_id, contentnode_id, preset, lang_id, "
            "supplementary, thumbnail, priority) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_id,
                checksum,
                node_id,
                preset,
                lang_id,
                int(supplementary),
                int(thumbnail),
                priority,
            ),
        )
        return file_id

    def add_document(self, node_id: str, text: str = "document text", *, extension: str = "pdf") -> None:
        if extension == "pdf":
            import pymupdf

            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), text)
            buffer = io.BytesIO()
            document.save(buffer)
            document.close()
            data = buffer.getvalue()
        elif extension in {"html", "htm"}:
            data = (
                f"<html><head><title>{text}</title></head>"
                f"<body><h1>{text}</h1><p>{text} body</p></body></html>"
            ).encode()
        else:
            data = text.encode()
        self.add_file(node_id, data, extension, preset="document")

    def add_video(
        self,
        node_id: str,
        data: bytes | None = None,
        *,
        captions: bytes | None = None,
        preset: str = "high_res_video",
        variant: bool = False,
    ) -> None:
        if data is None:
            data = sample("video/lesson.mp4").read_bytes()
        self.add_file(node_id, data, "mp4", preset=preset, priority=1)
        if variant:
            self.add_file(
                node_id,
                sample("video/shared.mp4").read_bytes(),
                "mp4",
                preset="low_res_video",
                priority=2,
            )
        if captions is not None:
            self.add_file(
                node_id,
                captions,
                "vtt",
                preset="video_subtitle",
                lang_id="en",
            )

    def make_zip(self, files: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return buffer.getvalue()

    def add_html5(self, node_id: str, files: dict[str, bytes] | None = None) -> None:
        files = files or {
            "index.html": b"<html><body><p>HTML5 zebra lesson</p>"
            b"<link rel='stylesheet' href='style.css'></body></html>",
            "style.css": b"body { color: #123; }",
            "nested/app.js": b"console.log('html5');",
        }
        self.add_file(
            node_id,
            self.make_zip(files),
            "zip",
            preset="html5_zip",
        )


def kolibri_config(fixture: KolibriFixture, **overrides) -> KolibriConfig:
    values = {
        "name": "kolibri",
        "path": str(fixture.home),
        "label": "Kolibri",
        "granularity": "children",
        "max_sources": 64,
        "max_entries": 500_000,
    }
    values.update(overrides)
    return KolibriConfig(**values)


def runtime_config(index_dir: Path, config: KolibriConfig, **settings) -> RuntimeConfig:
    values = {
        "index_dir": str(index_dir),
        "max_entry_bytes": 1 << 20,
        "max_body_text_bytes": 1 << 20,
        "max_preview_chars": 1000,
        "max_article_bytes": 1 << 20,
    }
    values.update(settings)
    return RuntimeConfig(
        settings=values,
        collections=(config,),
        config_dir=index_dir.parent,
    )


def context(index_dir: Path, config: RuntimeConfig) -> ProviderContext:
    return ProviderContext(
        config=config,
        index_dir=index_dir,
        web_base_path="",
        logger=logging.getLogger("zimantic.tests.kolibri"),
        persist_catalog=False,
    )


def open_runtime(index_dir: Path, config: KolibriConfig):
    runtime_config_value = runtime_config(index_dir, config)
    batch = KolibriProvider().discover(
        config, context(index_dir, runtime_config_value)
    )
    collections = list(batch.collections)
    if not collections:
        raise AssertionError("no Kolibri collection discovered")
    runtime = batch.open(collections[0])
    return runtime, batch, runtime_config_value


def build_sources(runtime, index_dir: Path, *, cfg: dict | None = None, fast: bool = False):
    from zimantic.indexing.locator import IndexLocator
    from zimantic.indexing.pipeline import build_index

    locator = IndexLocator(index_dir)
    values = {"batch_size": 1, "max_body_text_bytes": 1 << 20}
    values.update(cfg or {})
    for source in runtime.library_sources():
        producer = runtime.entry_producer(source, fast=fast)
        build_index(producer, locator, None, values, fast=fast)
    runtime.refresh(wait=True)
    return locator


def make_two_channel_fixture(root: Path) -> KolibriFixture:
    fixture = KolibriFixture(root)
    # Channel A: root(1) -> topic(2)->doc(3), topic(4)->leaf(5), direct leaf(6).
    fixture.add_channel("channel-a", "Channel A", "root-a", description="First channel")
    fixture.add_node("root-a", "channel-a", "Channel A", kind="topic", lft=1, rght=12, level=0)
    fixture.add_node("topic-1", "channel-a", "Topic One", kind="topic", parent_id="root-a", lft=2, rght=5, level=1)
    fixture.add_node("doc-1", "channel-a", "PDF One", kind="document", parent_id="topic-1", lft=3, rght=4, level=2)
    fixture.add_document("doc-1", "zebra pdf content")
    fixture.add_node("topic-2", "channel-a", "Topic Two", kind="topic", parent_id="root-a", lft=6, rght=9, level=1)
    fixture.add_node("video-1", "channel-a", "Video One", kind="video", parent_id="topic-2", lft=7, rght=8, level=2)
    fixture.add_video("video-1")
    fixture.add_node("direct-doc", "channel-a", "Direct Document", kind="document", parent_id="root-a", lft=10, rght=11, level=1)
    fixture.add_document("direct-doc", "giraffe direct content", extension="txt")

    # Channel B: root(1) -> leaves only, so it gets one whole-channel source.
    fixture.add_channel("channel-b", "Channel B", "root-b")
    fixture.add_node("root-b", "channel-b", "Channel B", kind="topic", lft=1, rght=6, level=0, tree_id=2)
    fixture.add_node("audio-1", "channel-b", "Audio One", kind="audio", parent_id="root-b", lft=2, rght=3, level=1, tree_id=2)
    fixture.add_file("audio-1", sample("audio/speech.mp3").read_bytes(), "mp3", preset="audio")
    fixture.add_node("exercise-1", "channel-b", "Exercise One", kind="exercise", parent_id="root-b", lft=4, rght=5, level=1, tree_id=2, description="public exercise description")
    fixture.close()
    return fixture


def channel_and_scope(runtime, key: str) -> tuple[ChannelRow, SourceScope]:
    with runtime._lock:
        source = runtime._by_key[key]
    return source.channel, source.scope
