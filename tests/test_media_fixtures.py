"""The committed media sample set covers v1 families and derived-text scenarios."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from zimantic.indexes.locator import IndexLocator
from zimantic.indexing.derived import (
    DerivedTextConfig,
    SourceDerivedOperations,
    parse_caption_bytes,
)
from zimantic.indexing.pipeline import build_index
from zimantic.providers.files.common import content_family, extension_mimetype
from zimantic.providers.files.discovery import discover_sources
from zimantic.providers.files.entries import FilesEntryProducer

from media_helpers import MEDIA_ROOT, copy_sample, manifest, sample
from providers.files.helpers import files_config


def _source(config, key: str):
    for source in discover_sources(config).sources:
        if source.key == key:
            return source
    raise AssertionError(f"missing source {key}")


def _producer(root: Path, config, source, *, cfg: dict | None = None):
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
        fast=False,
        cfg=values,
    )


def _body_hits(locator: IndexLocator, source, term: str) -> list[int]:
    lease = locator.paths(source.info)
    assert lease is not None
    try:
        import sqlite3

        db = sqlite3.connect(lease.sqlite_path)
        try:
            return [
                int(row[0])
                for row in db.execute(
                    "SELECT rowid FROM body WHERE text MATCH ?", (f'"{term}"',)
                )
            ]
        finally:
            db.close()
    finally:
        lease.close()


def _meta(locator: IndexLocator, source) -> dict[str, str]:
    import sqlite3

    lease = locator.paths(source.info)
    assert lease is not None
    try:
        db = sqlite3.connect(lease.sqlite_path)
        try:
            return dict(db.execute("SELECT key, value FROM meta"))
        finally:
            db.close()
    finally:
        lease.close()


def _item_index(producer: FilesEntryProducer, path: str) -> int:
    return next(
        index
        for index in range(producer.count)
        if producer._items[index].path == path
    )


def test_manifest_matches_every_committed_file():
    data = manifest()
    records = data["files"]
    assert isinstance(records, list)
    families = {record["media_family"] for record in records}
    assert {"text", "video", "audio", "image", None} <= families

    for record in records:
        path = record["path"]
        assert isinstance(path, str)
        assert (MEDIA_ROOT / path).is_file(), path
        assert extension_mimetype(path) == record["mimetype"], path
        assert content_family(path) == record["media_family"], path


def test_caption_samples_parse_to_known_text():
    assert parse_caption_bytes(sample("video/lesson.en.srt").read_bytes()) == (
        "sidecar zebra algebra lesson second cue about unicorn geometry"
    )
    assert parse_caption_bytes(sample("video/lesson.en.vtt").read_bytes()) == (
        "vtt unicorn geometry lesson second vtt cue about algebra"
    )
    assert parse_caption_bytes(sample("video/lesson.tricky.srt").read_bytes()) == (
        "tricky hippopotamus & friends youtube-style tag content"
    )


def test_ffprobe_streams_match_the_declared_scenarios():
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("ffprobe is not installed")

    def streams(relative: str) -> list[str]:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(sample(relative)),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()]

    lesson = streams("video/lesson.mp4")
    assert "video" in lesson and "audio" in lesson
    assert "subtitle" not in lesson

    embedded = streams("video/embedded.mp4")
    assert embedded.count("subtitle") == 1

    assert streams("video/silent.webm") == ["video"]
    assert streams("audio/speech.mp3") == ["audio"]


def test_caption_and_asr_inputs_come_from_the_right_committed_files(tmp_path):
    root = tmp_path / "media"
    copy_sample("video/lesson.mp4", root / "lesson.mp4")
    copy_sample("video/lesson.en.srt", root / "lesson.en.srt")
    copy_sample("video/lesson.en.vtt", root / "lesson.en.vtt")
    copy_sample("video/lesson.tricky.srt", root / "lesson.tricky.srt")
    copy_sample("video/embedded.mp4", root / "embedded.mp4")
    copy_sample("video/silent.webm", root / "silent.webm")
    config = files_config(root, granularity="collection", include=("**/*.txt",))
    source = _source(config, "files:modules:root")

    sidecar_only = _producer(
        root,
        config,
        source,
        cfg={
            "derived_source_operations": {
                source.key: SourceDerivedOperations(captions=True)
            },
            "derived_text": DerivedTextConfig(),
        },
    )
    assert [
        item.kind
        for item in sidecar_only.derived_text_inputs(
            _item_index(sidecar_only, "lesson.mp4")
        )
    ] == ["captions", "captions", "captions"]
    # Without an embedded-caption adapter or ASR, media has no derived input.
    assert (
        sidecar_only.derived_text_inputs(_item_index(sidecar_only, "embedded.mp4"))
        == ()
    )
    assert (
        sidecar_only.derived_text_inputs(_item_index(sidecar_only, "silent.webm"))
        == ()
    )

    with_asr = _producer(
        root,
        config,
        source,
        cfg={
            "derived_source_operations": {
                source.key: SourceDerivedOperations(captions=True, asr="whisper")
            },
            "derived_text": DerivedTextConfig(),
            "derived_engines": {"ffmpeg": lambda data, **_options: "embedded"},
        },
    )
    lesson = with_asr.derived_text_inputs(_item_index(with_asr, "lesson.mp4"))
    assert [item.kind for item in lesson] == [
        "captions",
        "captions",
        "captions",
        "video",
    ]
    # Media without a matching sidecar receives its own embedded/ASR input.
    embedded = with_asr.derived_text_inputs(_item_index(with_asr, "embedded.mp4"))
    assert [item.kind for item in embedded] == ["video"]
    silent = with_asr.derived_text_inputs(_item_index(with_asr, "silent.webm"))
    assert [item.kind for item in silent] == ["video"]


def test_committed_sidecar_text_reaches_the_body_index(tmp_path):
    root = tmp_path / "media"
    copy_sample("video/lesson.mp4", root / "lesson.mp4")
    copy_sample("video/lesson.en.srt", root / "lesson.en.srt")
    copy_sample("video/lesson.en.vtt", root / "lesson.en.vtt")
    copy_sample("video/lesson.tricky.srt", root / "lesson.tricky.srt")
    config = files_config(root, granularity="collection", include=("**/*.txt",))
    source = _source(config, "files:modules:root")
    cfg = {
        "derived_source_operations": {
            source.key: SourceDerivedOperations(captions=True)
        },
        "derived_text": DerivedTextConfig(),
    }
    producer = _producer(root, config, source, cfg=cfg)
    locator = IndexLocator(tmp_path / "indexes")
    build_index(
        producer,
        locator,
        None,
        {"batch_size": 1, "max_body_text_bytes": 1 << 20, **cfg},
        fast=False,
    )

    for term in ("sidecar", "unicorn", "hippopotamus", "algebra"):
        assert _body_hits(locator, source, term), term
    meta = _meta(locator, source)
    derived = json.loads(meta["derived_text"])
    assert derived["operations"]["captions"] == "ready"
    assert derived["contributors"] == ["captions"]


def test_image_only_pdf_and_scanned_image_are_ocr_eligible(tmp_path):
    import pymupdf

    text_pdf = pymupdf.open(sample("documents/text.pdf"))
    try:
        assert "native pdf zebra" in text_pdf[0].get_text().casefold()
    finally:
        text_pdf.close()
    scanned_pdf = pymupdf.open(sample("documents/scanned.pdf"))
    try:
        assert not scanned_pdf[0].get_text().strip()
        assert scanned_pdf[0].get_images()
    finally:
        scanned_pdf.close()

    tesseract = shutil.which("tesseract")
    if tesseract is not None:
        completed = subprocess.run(
            [tesseract, str(sample("image/scan.png")), "stdout"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "zebra OCR fixture" in " ".join(completed.stdout.split())

    root = tmp_path / "media"
    copy_sample("image/scan.png", root / "scan.png")
    copy_sample("documents/scanned.pdf", root / "scanned.pdf")
    config = files_config(root, granularity="collection", include=("**/*.pdf",))
    source = _source(config, "files:modules:root")
    cfg = {
        "derived_source_operations": {
            source.key: SourceDerivedOperations(ocr="tesseract")
        },
        "derived_text": DerivedTextConfig(),
        "derived_engines": {"ocr": lambda data, **_options: "ocr zebra"},
    }
    producer = _producer(root, config, source, cfg=cfg)
    kinds = {
        producer._items[index].path: [
            item.kind for item in producer.derived_text_inputs(index)
        ]
        for index in range(producer.count)
    }
    assert kinds["scan.png"] == ["image"]
    assert kinds["scanned.pdf"] == ["image"]


def test_ambiguous_sidecar_from_committed_samples_is_reported(tmp_path):
    root = tmp_path / "media"
    copy_sample("video/shared.mp4", root / "shared.mp4")
    copy_sample("video/shared.webm", root / "shared.webm")
    copy_sample("video/shared.srt", root / "shared.srt")
    config = files_config(root, granularity="collection", include=("**/*.txt",))
    source = _source(config, "files:modules:root")
    producer = _producer(
        root,
        config,
        source,
        cfg={
            "derived_source_operations": {
                source.key: SourceDerivedOperations(captions=True)
            },
            "derived_text": DerivedTextConfig(),
        },
    )
    assert any("ambiguous caption sidecar" in error for error in producer.errors)
    for index in range(producer.count):
        assert producer.derived_text_inputs(index) == ()
