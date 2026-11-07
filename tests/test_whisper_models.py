"""Whisper checkpoint cache lookup, download, and locking."""

from __future__ import annotations

import hashlib
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from zimantic import whisper_models
from zimantic.whisper_models import (
    WhisperModel,
    WhisperModelError,
    cached_whisper_model,
    ensure_whisper_model,
)


class _Response(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _spec(payload: bytes, filename: str = "base.en.pt") -> WhisperModel:
    sha256 = hashlib.sha256(payload).hexdigest()
    return WhisperModel(
        filename=filename,
        url=f"https://example.invalid/{sha256}/{filename}",
        sha256=sha256,
    )


def test_downloads_verifies_and_reuses_a_checkpoint(tmp_path, monkeypatch):
    payload = b"fake whisper checkpoint"
    monkeypatch.setitem(
        whisper_models.WHISPER_MODELS, "base.en", _spec(payload)
    )
    calls = []

    def fake_urlopen(request, **_kwargs):
        calls.append(request.full_url)
        return _Response(payload)

    monkeypatch.setattr(whisper_models.urllib.request, "urlopen", fake_urlopen)

    path = ensure_whisper_model("base.en", cache_root=tmp_path)

    assert path == tmp_path / "base.en.pt"
    assert path.read_bytes() == payload
    assert not path.with_name("base.en.pt.part").exists()
    assert calls == [whisper_models.WHISPER_MODELS["base.en"].url]

    def unexpected(*_args, **_kwargs):
        raise AssertionError("cached model was downloaded again")

    monkeypatch.setattr(whisper_models.urllib.request, "urlopen", unexpected)
    assert ensure_whisper_model("base.en", cache_root=tmp_path) == path


def test_checksum_mismatch_removes_the_partial_file(tmp_path, monkeypatch):
    monkeypatch.setitem(
        whisper_models.WHISPER_MODELS, "base.en", _spec(b"expected")
    )
    monkeypatch.setattr(
        whisper_models.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"corrupt"),
    )

    with pytest.raises(WhisperModelError, match="checksum"):
        ensure_whisper_model("base.en", cache_root=tmp_path)

    assert not (tmp_path / "base.en.pt").exists()
    assert not (tmp_path / "base.en.pt.part").exists()


def test_unknown_model_without_a_cached_file_is_reported(tmp_path):
    with pytest.raises(WhisperModelError, match="no known download URL"):
        ensure_whisper_model("custom-model", cache_root=tmp_path)


def test_manually_cached_unknown_model_is_used(tmp_path):
    checkpoint = tmp_path / "custom-model.pt"
    checkpoint.write_bytes(b"local checkpoint")

    assert cached_whisper_model("custom-model", tmp_path) == checkpoint
    assert ensure_whisper_model("custom-model", cache_root=tmp_path) == checkpoint


def test_model_aliases_use_the_filename_whisper_creates(tmp_path):
    checkpoint = tmp_path / "large-v3.pt"
    checkpoint.write_bytes(b"local checkpoint")

    assert cached_whisper_model("large", tmp_path) == checkpoint


def test_path_like_model_names_are_rejected(tmp_path):
    assert cached_whisper_model("../checkpoint", tmp_path) is None
    with pytest.raises(WhisperModelError, match="invalid whisper model name"):
        ensure_whisper_model("../checkpoint", cache_root=tmp_path)


def test_concurrent_callers_download_the_checkpoint_once(tmp_path, monkeypatch):
    payload = b"fake whisper checkpoint"
    monkeypatch.setitem(
        whisper_models.WHISPER_MODELS, "base.en", _spec(payload)
    )
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake_urlopen(*_args, **_kwargs):
        calls.append(1)
        started.set()
        assert release.wait(5)
        return _Response(payload)

    monkeypatch.setattr(whisper_models.urllib.request, "urlopen", fake_urlopen)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ensure_whisper_model, "base.en", tmp_path)
        assert started.wait(5)
        second = pool.submit(ensure_whisper_model, "base.en", tmp_path)
        time.sleep(0.1)
        release.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert results == [tmp_path / "base.en.pt", tmp_path / "base.en.pt"]
    assert calls == [1]
