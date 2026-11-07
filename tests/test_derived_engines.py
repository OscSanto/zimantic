"""Local derived-text engines and their build wiring."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from zimantic.indexing import engines as engines_module
from zimantic.indexing.engines import (
    available_derived_engines,
    extract_asr,
    extract_embedded_captions,
    extract_ocr,
)

from media_helpers import sample


def test_available_engines_requires_local_executables(monkeypatch):
    monkeypatch.setattr(engines_module.shutil, "which", lambda _name: None)
    assert available_derived_engines() == {}

    tools = {"tesseract", "whisper", "ffmpeg", "ffprobe"}
    monkeypatch.setattr(
        engines_module.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None
    )
    assert set(available_derived_engines()) == {"ocr", "asr", "ffmpeg"}


def test_build_config_attaches_available_engines(monkeypatch):
    from zimantic.__main__ import _build_config

    monkeypatch.setattr(
        engines_module,
        "available_derived_engines",
        lambda: {"ocr": object(), "asr": object()},
    )
    builds = _build_config({"index_dir": "/tmp/example"}, fast=False)
    assert set(builds["derived_engines"]) == {"ocr", "asr"}
    fast = _build_config({"index_dir": "/tmp/example"}, fast=True)
    assert "derived_engines" not in fast


@pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract is unavailable"
)
def test_real_ocr_reads_the_committed_image_and_scanned_pdf():
    image = sample("image/scan.png").read_bytes()
    assert "zebra OCR fixture" in extract_ocr(image, ocr_languages=["eng"])
    pdf = sample("documents/scanned.pdf").read_bytes()
    assert "zebra OCR fixture" in extract_ocr(pdf, ocr_languages=["eng"])


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are unavailable",
)
def test_real_ffmpeg_extracts_only_embedded_caption_tracks():
    embedded = sample("video/embedded.mp4").read_bytes()
    assert "Embedded caption about a zebra crossing." in extract_embedded_captions(
        embedded
    )
    sidecar_only = sample("video/lesson.mp4").read_bytes()
    assert extract_embedded_captions(sidecar_only) == ""


@pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract is unavailable"
)
def test_real_ocr_respects_the_pixel_cap():
    image = sample("image/scan.png").read_bytes()
    with pytest.raises(RuntimeError, match="max_ocr_pixels"):
        extract_ocr(image, ocr_languages=["eng"], max_ocr_pixels=1)


def test_asr_adapter_reads_the_whisper_text_output(monkeypatch):
    monkeypatch.setattr(
        engines_module.shutil,
        "which",
        lambda name: "/usr/bin/whisper" if name == "whisper" else None,
    )
    monkeypatch.setattr(
        engines_module,
        "_whisper_model_path",
        lambda _model: Path("/tmp/base.en.pt"),
    )
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        output = Path(args[args.index("--output_dir") + 1])
        media = Path(args[1])
        (output / f"{media.stem}.txt").write_text(
            "hello from whisper\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(engines_module.subprocess, "run", fake_run)

    assert extract_asr(b"fake audio", asr_model="base.en") == "hello from whisper"
    assert calls and calls[0][0].endswith("whisper")
    assert "--model" in calls[0]


def test_asr_adapter_reports_a_missing_executable(monkeypatch):
    monkeypatch.setattr(engines_module.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="whisper"):
        extract_asr(b"fake audio")


def test_asr_adapter_downloads_a_missing_model(monkeypatch):
    monkeypatch.setattr(
        engines_module.shutil,
        "which",
        lambda name: "/usr/bin/whisper" if name == "whisper" else None,
    )
    monkeypatch.setattr(engines_module, "_whisper_model_path", lambda _model: None)
    downloaded = []
    monkeypatch.setattr(
        engines_module,
        "ensure_whisper_model",
        lambda model: downloaded.append(model) or Path("/tmp/base.en.pt"),
    )

    def fake_run(args, **_kwargs):
        output = Path(args[args.index("--output_dir") + 1])
        media = Path(args[1])
        (output / f"{media.stem}.txt").write_text(
            "downloaded model transcript\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(engines_module.subprocess, "run", fake_run)

    assert extract_asr(b"fake audio", asr_model="base.en") == (
        "downloaded model transcript"
    )
    assert downloaded == ["base.en"]
