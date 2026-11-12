"""Local derived-text engines and their build wiring."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from media_helpers import sample

from zimantic.indexing import engines as engines_module
from zimantic.indexing.engines import (
    available_derived_engines,
    extract_asr,
    extract_embedded_captions,
    extract_ocr,
)


def test_available_engines_requires_local_executables(monkeypatch):
    monkeypatch.setattr(engines_module.shutil, "which", lambda _name: None)
    assert available_derived_engines() == {}

    tools = {"tesseract", "whisper-cli", "ffmpeg", "ffprobe"}
    monkeypatch.setattr(
        engines_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in tools else None,
    )
    assert set(available_derived_engines()) == {"ocr", "asr", "ffmpeg"}

    tools = {"tesseract", "whisper-ctranslate2"}
    monkeypatch.setattr(
        engines_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in tools else None,
    )
    assert set(available_derived_engines()) == {"ocr", "asr"}


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


def test_asr_adapter_reports_a_missing_executable(monkeypatch):
    monkeypatch.setattr(engines_module.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="whisper"):
        extract_asr(b"fake audio")


def test_auto_asr_prefers_whisper_cpp(monkeypatch):
    available = {
        "whisper-cli",
        "whisper-ctranslate2",
        "ffmpeg",
        "ffprobe",
    }
    monkeypatch.setattr(
        engines_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in available else None,
    )
    selected = []

    def fake_whisper_cpp(executable, *_args, **_kwargs):
        selected.append(executable)
        return "whisper.cpp transcript"

    monkeypatch.setattr(engines_module, "_run_whisper_cpp", fake_whisper_cpp)

    assert extract_asr(b"fake audio", asr_model="base.en") == (
        "whisper.cpp transcript"
    )
    assert selected == ["/usr/bin/whisper-cli"]


def test_auto_asr_skips_whisper_cpp_without_ffmpeg(monkeypatch):
    available = {"whisper-cli", "whisper-ctranslate2"}
    monkeypatch.setattr(
        engines_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in available else None,
    )
    selected = []

    def fake_faster_whisper(executable, *_args, **_kwargs):
        selected.append(executable)
        return "faster-whisper transcript"

    monkeypatch.setattr(
        engines_module, "_run_faster_whisper", fake_faster_whisper
    )

    assert extract_asr(b"fake audio", asr_model="base.en") == (
        "faster-whisper transcript"
    )
    assert selected == ["/usr/bin/whisper-ctranslate2"]


def test_faster_whisper_uses_a_local_model_directory(monkeypatch):
    monkeypatch.setattr(
        engines_module.shutil,
        "which",
        lambda name: (
            "/usr/bin/whisper-ctranslate2"
            if name == "whisper-ctranslate2"
            else None
        ),
    )
    model_directory = Path("/models/faster-whisper-base.en")
    monkeypatch.setattr(
        engines_module,
        "ensure_faster_whisper_model",
        lambda _model: model_directory,
    )
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        output = Path(args[args.index("--output_dir") + 1])
        media = Path(args[1])
        (output / f"{media.stem}.txt").write_text(
            "faster whisper transcript\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(engines_module.subprocess, "run", fake_run)

    assert extract_asr(
        b"fake audio",
        asr_engine="faster-whisper",
        asr_model="base.en",
        asr_threads=2,
        asr_batched=True,
    ) == "faster whisper transcript"
    assert calls[0][0].endswith("whisper-ctranslate2")
    assert calls[0][calls[0].index("--model_directory") + 1] == str(
        model_directory
    )
    assert calls[0][calls[0].index("--local_files_only") + 1] == "True"
    assert calls[0][calls[0].index("--threads") + 1] == "2"
    assert calls[0][calls[0].index("--batched") + 1] == "True"


def test_whisper_cpp_converts_to_wav_and_reads_txt(monkeypatch):
    monkeypatch.setattr(
        engines_module.shutil,
        "which",
        lambda name: (
            f"/usr/bin/{name}"
            if name in {"whisper-cli", "ffmpeg"}
            else None
        ),
    )
    monkeypatch.setattr(
        engines_module,
        "ensure_whisper_cpp_model",
        lambda _model: Path("/models/ggml-base.en.bin"),
    )
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[0].endswith("whisper-cli"):
            base = Path(args[args.index("-of") + 1])
            base.with_suffix(".txt").write_text(
                "whisper.cpp transcript\n", encoding="utf-8"
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(engines_module.subprocess, "run", fake_run)

    assert extract_asr(
        b"fake audio", asr_engine="whisper.cpp", asr_model="base.en"
    ) == "whisper.cpp transcript"
    conversion = calls[0]
    assert conversion[0] == "/usr/bin/ffmpeg"
    assert conversion[conversion.index("-ar") + 1] == "16000"
    assert conversion[conversion.index("-ac") + 1] == "1"
    cli = calls[1]
    assert cli[0] == "/usr/bin/whisper-cli"
    assert Path(cli[cli.index("-m") + 1]) == Path("/models/ggml-base.en.bin")
    assert "-otxt" in cli


class _FakeFuture:
    def __init__(self, value: str) -> None:
        self._value = value

    def result(self, timeout: float | None = None) -> str:
        return self._value


class _RecordingPool:
    def __init__(self, value: str = "pool text") -> None:
        self.value = value
        self.calls: list[tuple[str, str, str, dict]] = []

    def submit(self, operation, engine, payload, options):
        self.calls.append((operation, engine, str(payload), dict(options)))
        return _FakeFuture(self.value)


class _ImageInput:
    kind = "image"
    mimetype = "image/png"
    size = 4

    def open(self):
        import io

        return io.BytesIO(b"png!")


def test_derived_stage_routes_ocr_to_the_worker_pool():
    from zimantic.indexing.derived import (
        DerivedTextConfig,
        DerivedTextStage,
        SourceDerivedOperations,
    )

    pool = _RecordingPool("zebra from worker")
    stage = DerivedTextStage(
        DerivedTextConfig(ocr_languages=("eng",)),
        operations=SourceDerivedOperations(ocr="tesseract"),
        engines={"ocr": lambda _data, **_options: "inline"},
        worker_pool=pool,
    )
    result = stage.process(0, "", [_ImageInput()])
    assert [item.text for item in result.contributions] == ["zebra from worker"]
    assert pool.calls[0][0:2] == ("ocr", "tesseract")
    assert pool.calls[0][3]["ocr_languages"] == ["eng"]


def test_derived_cache_identity_ignores_scheduling_options():
    from zimantic.indexing.derived import DerivedTextConfig, DerivedTextStage

    one = DerivedTextStage(DerivedTextConfig(workers=1))
    many = DerivedTextStage(DerivedTextConfig(workers=8))
    assert one._options() == many._options()
    assert "workers" not in one._options()
    assert "cache" not in one._options()


def test_derived_cache_counts_existing_entries_and_evicts(tmp_path):
    from zimantic.indexing.derived import DerivedTextCache

    first = DerivedTextCache(tmp_path, max_bytes=20)
    assert first.put("a", "ocr", "tesseract", {}, "x" * 12) is not None
    assert first.put("b", "ocr", "tesseract", {}, "y" * 12) is not None
    sizes = [path.stat().st_size for path in tmp_path.rglob("*.txt")]
    assert sum(sizes) <= 20

    # A new build must see the bytes already on disk before adding more.
    second = DerivedTextCache(tmp_path, max_bytes=20)
    assert second._bytes == sum(
        path.stat().st_size for path in tmp_path.rglob("*") if path.is_file()
    )
    assert second.put("c", "ocr", "tesseract", {}, "z" * 12) is not None
    assert sum(
        path.stat().st_size for path in tmp_path.rglob("*.txt")
    ) <= 20


def test_derived_cache_prune_recounts_kept_entries(tmp_path):
    from zimantic.indexing.derived import DerivedTextCache

    cache = DerivedTextCache(tmp_path, max_bytes=100)
    kept = cache.put("a", "ocr", "tesseract", {}, "keep")
    assert kept is not None
    cache.put("b", "ocr", "tesseract", {}, "remove")
    assert cache.prune(keep=[kept]) == 1
    assert cache._bytes == kept.stat().st_size


def test_derived_pool_terminates_after_a_runaway_task():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from zimantic.indexing.derived import DerivedWorkerPool

    pool = DerivedWorkerPool(1)
    pool.mark_runaway()
    executor = SimpleNamespace(shutdown=Mock(), _processes={})
    pool._executor = executor
    pool.close()
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)


@pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract is unavailable"
)
def test_worker_pool_runs_real_ocr_in_a_spawned_process():
    from zimantic.indexing.derived import DerivedWorkerPool

    payload = sample("image/scan.png").read_bytes()
    pool = DerivedWorkerPool(1)
    try:
        future = pool.submit(
            "ocr", "tesseract", payload, {"ocr_languages": ["eng"]}
        )
        assert "zebra OCR fixture" in future.result(timeout=60)
    finally:
        pool.close()
