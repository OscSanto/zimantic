"""Files entry producer: stable ids, caps, extraction, and derived text."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zimantic.indexing.locator import IndexLocator
from zimantic.indexing.derived import DerivedTextConfig, SourceDerivedOperations
from zimantic.indexing.pipeline import build_index
from zimantic.providers.files.discovery import (
    FilesTraversalError,
    discover_sources,
    files_dir_key,
    files_loose_key,
)
from zimantic.providers.files.entries import FilesEntryProducer

from media_helpers import copy_sample

from index_helpers import body_hits as _body_hits, docs as _docs, meta as _meta

from .helpers import create_pdf, files_config


def _source(config, key: str):
    result = discover_sources(config)
    for source in result.sources:
        if source.key == key:
            return source
    raise AssertionError(f"missing source {key}")


def _producer(
    root: Path,
    config,
    source,
    *,
    cfg: dict | None = None,
    fast: bool = False,
) -> FilesEntryProducer:
    values = {
        "max_entry_bytes": 1 << 20,
        "max_body_text_bytes": 1 << 20,
        "max_preview_chars": 1000,
    }
    values.update(cfg or {})
    return FilesEntryProducer(
        source.info,
        root,
        membership_root=source.membership_root,
        loose_only=source.loose_only,
        config=config,
        fast=fast,
        cfg=values,
    )


class _InterruptingProducer(FilesEntryProducer):
    def __init__(self, *args, interrupt_at: int = 1, **kwargs):
        self._interrupt_at = interrupt_at
        self._interrupted = False
        super().__init__(*args, **kwargs)

    def entry_at(self, index: int):
        if index == self._interrupt_at and not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt
        return super().entry_at(index)


def test_stable_ids_and_resume_fingerprint(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    (root / "b.txt").write_text("beta", encoding="utf-8")
    config = files_config(root, granularity="collection")
    source = _source(config, "files:modules:root")

    first = _producer(root, config, source)
    second = _producer(root, config, source)
    assert first.count == 2
    assert [first.entry_at(i).id for i in range(2)] == [1, 2]
    assert first.fingerprint == second.fingerprint
    assert first.validate_snapshot() is True

    path = root / "b.txt"
    path.write_text("beta changed", encoding="utf-8")
    os.utime(path, (path.stat().st_atime + 5, path.stat().st_mtime + 5))
    changed = _producer(root, config, source)
    assert changed.fingerprint != first.fingerprint
    assert first.validate_snapshot() is False


def test_scoped_index_filters_and_extraction_limits_do_not_change_serving(tmp_path):
    from zimantic.providers.files.store import FilesContentStore

    root = tmp_path / "modules"
    root.mkdir()
    for name in ("kept.txt", "skip.txt", "notes.md", "denied.txt"):
        (root / name).write_text("alpha omega", encoding="utf-8")
    config = files_config(
        root,
        granularity="collection",
        exclude=("denied.txt",),
        settings={"max_body_text_bytes": 32, "index_include": ("**/*.md",)},
    )
    source = _source(config, "files:modules:root")
    overrides = {
        "max_body_text_bytes": 8,
        "max_preview_chars": 6,
        "index_include": ("**/*.txt",),
        "index_exclude": ("skip.txt",),
    }
    cfg = {
        "max_body_text_bytes": 64,
        "collections": (config,),
        "source_settings": {source.info.key: overrides},
    }
    producer = _producer(root, config, source, cfg=cfg)
    assert producer.count == 1
    row = producer.entry_at(0)
    assert row.path == "kept.txt"
    assert "omega" not in row.excerpt
    locator = IndexLocator(tmp_path / "indexes")
    build_index(producer, locator, None, cfg)
    assert _body_hits(locator, source.info, "alpha")
    assert not _body_hits(locator, source.info, "omega")
    assert _meta(locator, source.info)["body_truncated_count"] == "1"
    overrides["candidate_count"] = 99
    assert _producer(root, config, source, cfg=cfg).fingerprint == producer.fingerprint
    overrides["max_body_text_bytes"] = 32
    changed = _producer(root, config, source, cfg=cfg)
    assert changed.fingerprint != producer.fingerprint
    build_index(changed, locator, None, cfg)
    assert _body_hits(locator, source.info, "omega")
    overrides.update(index_include=(), index_exclude=(), max_entries=1)
    capped = _producer(root, config, source, cfg=cfg)
    assert capped.count == 1
    assert capped.truncated
    store = FilesContentStore(
        source.info,
        root,
        membership_root="",
        loose_only=False,
        config=config,
        max_article_bytes=1 << 20,
    )
    try:
        assert store.exists("skip.txt")
        assert (
            store.read("notes.md", head_only=False, max_bytes=1024).content
            == b"alpha omega"
        )
        assert not store.exists("denied.txt")
    finally:
        store.close()


def test_interrupted_build_resumes_with_the_same_manifest(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    for index in range(4):
        (root / f"{index}.txt").write_text(
            f"document number {index}", encoding="utf-8"
        )
    config = files_config(root, granularity="collection")
    source = _source(config, "files:modules:root")
    locator = IndexLocator(tmp_path / "indexes")

    first = _InterruptingProducer(
        source.info,
        root,
        membership_root="",
        loose_only=False,
        config=config,
        fast=False,
        cfg={
            "max_entry_bytes": 1 << 20,
            "max_body_text_bytes": 1 << 20,
            "max_preview_chars": 1000,
        },
        interrupt_at=1,
    )
    with pytest.raises(KeyboardInterrupt):
        build_index(first, locator, None, {"batch_size": 1}, fast=False)

    resumed = _producer(root, config, source)
    build_index(resumed, locator, None, {"batch_size": 1}, fast=False)

    rows = _docs(locator, source.info)
    assert [row[0] for row in rows] == [1, 2, 3, 4]
    assert [row[2] for row in rows] == [
        "0.txt",
        "1.txt",
        "2.txt",
        "3.txt",
    ]


def test_manifest_cap_is_deterministic_across_enumeration_orders(
    tmp_path, monkeypatch
):
    root = tmp_path / "modules"
    root.mkdir()
    for index in range(10):
        (root / f"{index:02d}.txt").write_text(
            f"document {index}", encoding="utf-8"
        )
    config = files_config(root, granularity="collection", max_entries=3)
    source = _source(config, "files:modules:root")

    first = _producer(root, config, source)
    expected = [first.entry_at(i).path for i in range(first.count)]
    assert first.truncated is True
    assert len(expected) == 3

    import zimantic.providers.files.common as common

    real_scandir = common.os.scandir

    class _ReversedScandir:
        def __init__(self, path):
            self._iterator = real_scandir(path)
            self._entries = sorted(
                list(self._iterator), key=lambda entry: entry.name, reverse=True
            )

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, *args):
            self._iterator.close()
            return False

    monkeypatch.setattr(common.os, "scandir", _ReversedScandir)
    second = _producer(root, config, source)
    assert [second.entry_at(i).path for i in range(second.count)] == expected


def test_collapsed_collection_builds_one_searchable_index(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    for index in range(70):
        child = root / f"unit-{index:03d}"
        child.mkdir()
        (child / "index.html").write_text(
            "<html><head><title>Unit</title></head><body>"
            "<p>unit lesson about zebra</p></body></html>",
            encoding="utf-8",
        )
    config = files_config(root)
    discovery = discover_sources(config)
    assert discovery.collapsed is True
    source = discovery.sources[0]
    producer = _producer(root, config, source)
    locator = IndexLocator(tmp_path / "indexes")
    build_index(producer, locator, None, {"batch_size": 8}, fast=False)

    rows = _docs(locator, source.info)
    assert len(rows) == 70
    assert _body_hits(locator, source.info, "zebra")
    meta = _meta(locator, source.info)
    assert meta["truncated"] == "0"


def test_traversal_budget_raises_and_preserves_published_index(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    for index in range(10):
        (root / f"{index:02d}.txt").write_text(
            f"document {index}", encoding="utf-8"
        )
    config = files_config(root, granularity="collection")
    source = _source(config, "files:modules:root")
    index_dir = tmp_path / "indexes"
    locator = IndexLocator(index_dir)

    producer = _producer(root, config, source)
    build_index(producer, locator, None, {"batch_size": 1}, fast=True)
    before = _meta(locator, source.info)
    assert before["done"] == "fast"

    from zimantic.providers.config import TraversalLimits

    tight = files_config(
        root,
        granularity="collection",
        traversal=TraversalLimits(max_visited_entries=3),
    )
    tight_source = _source(tight, "files:modules:root")
    with pytest.raises(FilesTraversalError):
        _producer(root, tight, tight_source)

    after = _meta(locator, source.info)
    assert after["build_generation"] == before["build_generation"]


def test_max_entries_marks_truncated_with_unknown_total(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    for index in range(5):
        (root / f"{index}.txt").write_text(
            f"document {index}", encoding="utf-8"
        )
    config = files_config(root, granularity="collection", max_entries=2)
    source = _source(config, "files:modules:root")
    producer = _producer(root, config, source)
    assert producer.count == 2
    assert producer.truncated is True

    locator = IndexLocator(tmp_path / "indexes")
    build_index(producer, locator, None, {"batch_size": 1}, fast=True)
    meta = _meta(locator, source.info)
    assert meta["truncated"] == "1"
    assert meta["indexed_count"] == "2"


def test_extractors_cover_html_pdf_text_markdown_and_skip_binaries(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "page.html").write_text(
        "<html><head><title>HTML Title</title></head><body><h1>Heading</h1>"
        "<p>html body zebra</p></body></html>",
        encoding="utf-8",
    )
    create_pdf(root / "report.pdf", "pdf body unicorn", title="PDF Title")
    (root / "notes.txt").write_text("plain text giraffe", encoding="utf-8")
    (root / "guide.md").write_text(
        "---\ntitle: Markdown Title\n---\n# Heading\n\nmarkdown body dolphin",
        encoding="utf-8",
    )
    (root / "unknown.bin").write_bytes(b"\x00\x01binary")
    config = files_config(
        root,
        granularity="collection",
        include=("**/*.html", "**/*.pdf", "**/*.txt", "**/*.md", "**/*.bin"),
    )
    source = _source(config, "files:modules:root")
    producer = _producer(root, config, source)

    rows = {}
    for index in range(producer.count):
        row = producer.entry_at(index)
        if row is not None:
            rows[row.path] = row
    assert rows["page.html"].title == "HTML Title"
    assert "zebra" in rows["page.html"].body
    assert rows["report.pdf"].title == "PDF Title"
    assert "unicorn" in rows["report.pdf"].body
    assert rows["notes.txt"].title == "notes"
    assert "giraffe" in rows["notes.txt"].body
    assert rows["guide.md"].title == "Markdown Title"
    assert "dolphin" in rows["guide.md"].body
    assert "unknown.bin" not in rows


def test_oversized_document_is_skipped(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "big.txt").write_text("x" * 100, encoding="utf-8")
    (root / "small.txt").write_text("ok", encoding="utf-8")
    config = files_config(root, granularity="collection", max_file_bytes=10)
    source = _source(config, "files:modules:root")
    producer = _producer(root, config, source)
    rows = {producer._items[i].path: producer.entry_at(i) for i in range(producer.count)}
    assert rows["big.txt"] is None
    assert rows["small.txt"] is not None


def test_body_beyond_excerpt_and_body_truncation_count(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "long.txt").write_text(
        ("padding " * 300) + "needle", encoding="utf-8"
    )
    (root / "huge.txt").write_text("overflow " * 1000, encoding="utf-8")
    (root / "short.txt").write_text("short body", encoding="utf-8")
    config = files_config(root, granularity="collection")
    source = _source(config, "files:modules:root")
    producer = _producer(
        root,
        config,
        source,
        cfg={"max_preview_chars": 50, "max_body_text_bytes": 4000},
    )
    locator = IndexLocator(tmp_path / "indexes")
    build_index(
        producer,
        locator,
        None,
        {"batch_size": 1, "max_body_text_bytes": 4000},
        fast=False,
    )
    meta = _meta(locator, source.info)
    assert meta["truncated"] == "0"
    assert meta["body_truncated_count"] == "1"
    assert _body_hits(locator, source.info, "needle")


def test_media_is_title_only_without_reading_bytes(tmp_path, monkeypatch):
    root = tmp_path / "modules"
    root.mkdir()
    copy_sample("video/lesson.mp4", root / "video.mp4")
    (root / "notes.txt").write_text("notes", encoding="utf-8")
    config = files_config(
        root,
        granularity="collection",
        include=("**/*.txt",),
    )
    source = _source(config, "files:modules:root")

    import zimantic.providers.files.entries as entries_module

    def forbidden(*_args, **_kwargs):
        raise AssertionError("media bytes were opened")

    monkeypatch.setattr(entries_module, "open_confined", forbidden)
    producer = _producer(root, config, source)
    media_index = next(
        index
        for index in range(producer.count)
        if producer._items[index].path == "video.mp4"
    )
    media = producer.entry_at(media_index)
    assert media is not None
    assert media.title == "video"
    assert media.body == ""
    assert producer.derived_text_inputs(media_index) == ()


def test_ocr_and_asr_use_fake_engines(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    copy_sample("image/scan.png", root / "scan.png")
    copy_sample("audio/speech.mp3", root / "talk.mp3")
    (root / "notes.txt").write_text("native text", encoding="utf-8")
    config = files_config(
        root,
        granularity="collection",
        include=("**/*.txt",),
    )
    source = _source(config, "files:modules:root")
    operations = {
        source.key: SourceDerivedOperations(ocr="tesseract", asr="whisper")
    }
    cfg = {
        "max_entry_bytes": 1 << 20,
        "max_body_text_bytes": 1 << 20,
        "max_preview_chars": 1000,
        "derived_source_operations": operations,
        "derived_text": DerivedTextConfig(),
        "derived_engines": {
            "ocr": lambda data, **_options: "ocr zebra",
            "asr": lambda data, **_options: "asr unicorn",
        },
    }
    producer = _producer(root, config, source, cfg=cfg)
    locator = IndexLocator(tmp_path / "indexes")
    build_index(
        producer,
        locator,
        None,
        {
            "batch_size": 1,
            "max_body_text_bytes": 1 << 20,
            **cfg,
        },
        fast=False,
    )
    meta = _meta(locator, source.info)
    assert _body_hits(locator, source.info, "zebra")
    assert _body_hits(locator, source.info, "unicorn")
    import json

    derived = json.loads(meta["derived_text"])
    assert derived["operations"]["ocr"] == "ready"
    assert derived["operations"]["asr"] == "ready"


@pytest.mark.parametrize("scope", ["collection", "source"])
def test_derived_language_overrides_rebuild_and_index_with_effective_options(tmp_path, scope):
    from zimantic.settings import load_config

    root = tmp_path / "media"
    root.mkdir()
    copy_sample("image/scan.png", root / "scan.png")
    path = tmp_path / "config.toml"
    calls = []

    def engine(data, **options):
        calls.append(options["ocr_languages"])
        return "zebra" if options["ocr_languages"] == ["fra"] else "unicorn"

    def settings(language):
        derived = 'ocr = "tesseract"'
        sources = ""
        if scope == "collection":
            derived += f', ocr_languages = ["{language}"]'
        else:
            sources = f'sources = [{{ key = "files:media:root", ocr_languages = ["{language}"] }}]\n'
        path.write_text(
            '[derived_text]\nocr_languages = ["eng"]\n'
            '[[collections]]\ntype = "files"\nname = "media"\npath = "./media"\n'
            f'derived_text = {{ {derived} }}\n{sources}',
            encoding="utf-8",
        )
        cfg = load_config(path, warn=lambda _: None)
        cfg["derived_engines"] = {"ocr": engine}
        cfg["batch_size"] = 1
        return cfg

    locator = IndexLocator(tmp_path / "indexes")
    cfg = settings("fra")
    config = cfg["collections"][0]
    source = _source(config, "files:media:root")
    first = _producer(root, config, source, cfg=cfg)
    build_index(first, locator, None, cfg)
    assert _body_hits(locator, source.info, "zebra")
    assert build_index(_producer(root, config, source, cfg=cfg), locator, None, cfg).status == "skipped"
    cfg = settings("deu")
    changed = _producer(root, cfg["collections"][0], source, cfg=cfg)
    assert changed.fingerprint != first.fingerprint
    build_index(changed, locator, None, cfg)
    assert _body_hits(locator, source.info, "unicorn")
    assert not _body_hits(locator, source.info, "zebra")
    assert calls == [["fra"], ["deu"]]


def test_image_only_pdf_is_ocr_eligible(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    copy_sample("documents/scanned.pdf", root / "scan.pdf")
    config = files_config(
        root,
        granularity="collection",
        include=("**/*.pdf",),
    )
    source = _source(config, "files:modules:root")
    operations = {source.key: SourceDerivedOperations(ocr="tesseract")}
    cfg = {
        "max_entry_bytes": 1 << 20,
        "max_body_text_bytes": 1 << 20,
        "max_preview_chars": 1000,
        "derived_source_operations": operations,
        "derived_text": DerivedTextConfig(),
        "derived_engines": {"ocr": lambda data, **_options: "scanned zebra"},
    }
    producer = _producer(root, config, source, cfg=cfg)
    row = producer.entry_at(0)
    assert row is not None
    assert row.title == "scan"
    assert row.body == ""
    assert producer.derived_text_inputs(0)[0].kind == "image"

    locator = IndexLocator(tmp_path / "indexes")
    build_index(
        producer,
        locator,
        None,
        {"batch_size": 1, "max_body_text_bytes": 1 << 20, **cfg},
        fast=False,
    )
    assert _body_hits(locator, source.info, "zebra")


def test_caption_sidecars_attach_and_change_fingerprint_only_when_enabled(
    tmp_path,
):
    root = tmp_path / "modules"
    root.mkdir()
    copy_sample("video/lesson.mp4", root / "lesson.mp4")
    sidecar = root / "lesson.en.srt"
    copy_sample("video/lesson.en.srt", sidecar)
    config = files_config(
        root,
        granularity="collection",
        include=("**/*.txt",),
    )
    source = _source(config, "files:modules:root")
    off = _producer(root, config, source)
    assert off.derived_text_inputs(0) == ()
    off_fingerprint = off.fingerprint

    sidecar.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\ncaption unicorn\n",
        encoding="utf-8",
    )
    os.utime(sidecar, (sidecar.stat().st_atime + 5, sidecar.stat().st_mtime + 5))
    off_after = _producer(root, config, source)
    assert off_after.fingerprint == off_fingerprint

    operations = {source.key: SourceDerivedOperations(captions=True)}
    cfg = {
        "max_entry_bytes": 1 << 20,
        "max_body_text_bytes": 1 << 20,
        "max_preview_chars": 1000,
        "derived_source_operations": operations,
        "derived_text": DerivedTextConfig(),
    }
    on = _producer(root, config, source, cfg=cfg)
    assert on.fingerprint != off_fingerprint
    inputs = on.derived_text_inputs(0)
    assert [item.kind for item in inputs] == ["captions"]
    on_fingerprint = on.fingerprint

    locator = IndexLocator(tmp_path / "indexes")
    build_index(
        on,
        locator,
        None,
        {"batch_size": 1, "max_body_text_bytes": 1 << 20, **cfg},
        fast=False,
    )
    assert _body_hits(locator, source.info, "unicorn")

    sidecar.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\ncaption hippopotamus\n",
        encoding="utf-8",
    )
    os.utime(sidecar, (sidecar.stat().st_atime + 5, sidecar.stat().st_mtime + 5))
    on_after = _producer(root, config, source, cfg=cfg)
    assert on_after.fingerprint != on_fingerprint


def test_embedded_captions_use_the_ffmpeg_adapter(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    copy_sample("video/embedded.mp4", root / "talk.mp4")
    config = files_config(
        root,
        granularity="collection",
        include=("**/*.txt",),
    )
    source = _source(config, "files:modules:root")
    operations = {source.key: SourceDerivedOperations(captions=True)}
    cfg = {
        "max_entry_bytes": 1 << 20,
        "max_body_text_bytes": 1 << 20,
        "max_preview_chars": 1000,
        "derived_source_operations": operations,
        "derived_text": DerivedTextConfig(),
        "derived_engines": {
            "ffmpeg": lambda data, **_options: "embedded caption zebra"
        },
    }
    producer = _producer(root, config, source, cfg=cfg)
    assert [item.kind for item in producer.derived_text_inputs(0)] == ["video"]

    locator = IndexLocator(tmp_path / "indexes")
    build_index(
        producer,
        locator,
        None,
        {"batch_size": 1, "max_body_text_bytes": 1 << 20, **cfg},
        fast=False,
    )
    assert _body_hits(locator, source.info, "zebra")


def test_ambiguous_sidecar_is_reported_not_attached(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    copy_sample("video/shared.mp4", root / "video.mp4")
    copy_sample("video/shared.webm", root / "video.webm")
    copy_sample("video/shared.srt", root / "video.srt")
    config = files_config(
        root,
        granularity="collection",
        include=("**/*.txt",),
    )
    source = _source(config, "files:modules:root")
    operations = {source.key: SourceDerivedOperations(captions=True)}
    cfg = {
        "max_entry_bytes": 1 << 20,
        "max_body_text_bytes": 1 << 20,
        "max_preview_chars": 1000,
        "derived_source_operations": operations,
        "derived_text": DerivedTextConfig(),
    }
    producer = _producer(root, config, source, cfg=cfg)
    assert any("ambiguous caption sidecar" in error for error in producer.errors)
    for index in range(producer.count):
        assert "captions" not in {
            item.kind for item in producer.derived_text_inputs(index)
        }
