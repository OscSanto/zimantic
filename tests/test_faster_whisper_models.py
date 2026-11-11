"""faster-whisper CTranslate2 model download and cache handling."""

from __future__ import annotations

import hashlib
import io
import json

import pytest

from zimantic import faster_whisper_models
from zimantic.faster_whisper_models import (
    FasterWhisperModelError,
    ensure_faster_whisper_model,
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


def _git_oid(payload: bytes) -> str:
    return hashlib.sha1(
        f"blob {len(payload)}\0".encode("ascii") + payload
    ).hexdigest()


def _file_item(path: str, payload: bytes) -> dict:
    return {
        "type": "file",
        "path": path,
        "size": len(payload),
        "oid": _git_oid(payload),
    }


def _fake_hub(monkeypatch, *, corrupt_model: bool = False):
    payloads = {
        "config.json": b'{"lang_ids": []}',
        "model.bin": b"fake ctranslate2 weights",
        "tokenizer.json": b"fake tokenizer",
    }
    tree = [
        _file_item("config.json", payloads["config.json"]),
        {
            **_file_item("model.bin", payloads["model.bin"]),
            "oid": "unused-git-oid",
            "lfs": {
                "oid": hashlib.sha256(payloads["model.bin"]).hexdigest(),
                "size": len(payloads["model.bin"]),
            },
        },
        _file_item("tokenizer.json", payloads["tokenizer.json"]),
    ]
    calls = []

    def fake_urlopen(request, **_kwargs):
        url = request.full_url
        calls.append(url)
        if url.endswith("/api/models/Systran/faster-whisper-base.en"):
            return _Response(json.dumps({"sha": "revision"}).encode())
        if url.endswith("/tree/revision"):
            return _Response(json.dumps(tree).encode())
        for name, payload in payloads.items():
            if url.endswith(f"/{name}"):
                if name == "model.bin" and corrupt_model:
                    return _Response(b"corrupt weights")
                return _Response(payload)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(
        faster_whisper_models.urllib.request, "urlopen", fake_urlopen
    )
    return calls


def test_downloads_a_pinned_model_and_reuses_the_manifest(
    tmp_path, monkeypatch
):
    calls = _fake_hub(monkeypatch)

    model_dir = ensure_faster_whisper_model("base.en", cache_root=tmp_path)

    assert model_dir == tmp_path / "base.en"
    assert (model_dir / "model.bin").read_bytes() == (
        b"fake ctranslate2 weights"
    )
    manifest = json.loads(
        (model_dir / faster_whisper_models.MANIFEST_NAME).read_text()
    )
    assert manifest["repo"] == "Systran/faster-whisper-base.en"
    assert manifest["revision"] == "revision"
    assert set(manifest["files"]) == {
        "config.json",
        "model.bin",
        "tokenizer.json",
    }
    assert any("/resolve/revision/model.bin" in url for url in calls)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("cached model was downloaded again")

    monkeypatch.setattr(
        faster_whisper_models.urllib.request, "urlopen", unexpected
    )
    assert ensure_faster_whisper_model("base.en", cache_root=tmp_path) == model_dir


def test_checksum_mismatch_removes_the_partial_file(tmp_path, monkeypatch):
    _fake_hub(monkeypatch, corrupt_model=True)

    with pytest.raises(FasterWhisperModelError, match="size or checksum"):
        ensure_faster_whisper_model("base.en", cache_root=tmp_path)

    model_dir = tmp_path / "base.en"
    assert not (model_dir / "model.bin").exists()
    assert not (model_dir / "model.bin.part").exists()
    assert not (model_dir / faster_whisper_models.MANIFEST_NAME).exists()


def test_unknown_model_is_reported(tmp_path):
    with pytest.raises(FasterWhisperModelError, match="no known download"):
        ensure_faster_whisper_model("custom", cache_root=tmp_path)


def test_local_model_directory_is_used_without_downloading(tmp_path):
    local = tmp_path / "fine-tuned"
    local.mkdir()
    (local / "model.bin").write_bytes(b"weights")

    assert ensure_faster_whisper_model(str(local), cache_root=tmp_path) == local


def test_missing_required_file_is_reported(tmp_path, monkeypatch):
    def fake_urlopen(request, **_kwargs):
        url = request.full_url
        if url.endswith("/api/models/Systran/faster-whisper-base.en"):
            return _Response(json.dumps({"sha": "revision"}).encode())
        if url.endswith("/tree/revision"):
            return _Response(
                json.dumps(
                    [_file_item("config.json", b"config")]
                ).encode()
            )
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(
        faster_whisper_models.urllib.request, "urlopen", fake_urlopen
    )

    with pytest.raises(FasterWhisperModelError, match="missing required"):
        ensure_faster_whisper_model("base.en", cache_root=tmp_path)
