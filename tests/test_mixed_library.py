"""End-to-end mixed-library smoke test over the phase-09 fixture."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "mixed_library"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"zimantic_mixed_{name}", FIXTURES / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read()


def test_mixed_library_serves_all_providers(tmp_path):
    generate = _load("generate")
    kiwix_module = _load("kiwix_server")
    with kiwix_module.KiwixFixture() as kiwix:
        root = generate.generate(
            tmp_path / "mixed",
            files=6,
            zim_entries=4,
            kiwix_server=kiwix.url,
            force=True,
        )
        port = _free_port()
        config_path = root / "config.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "port = 8099", f"port = {port}"
            ),
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
        env["XDG_DATA_HOME"] = str(tmp_path / "data")
        env["XDG_RUNTIME_DIR"] = str(tmp_path)
        build = subprocess.run(
            [sys.executable, "-m", "zimantic", "build", "--fast"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert build.returncode == 0, build.stderr

        server = subprocess.Popen(
            [sys.executable, "-m", "zimantic", "serve", "--fast"],
            cwd=root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 30
            collections: list[dict] = []
            while time.monotonic() < deadline:
                try:
                    status, body = _get(base + "/collections.json?limit=200", timeout=2)
                except OSError:
                    time.sleep(0.1)
                    continue
                if status == 200:
                    collections = json.loads(body)
                    if len(collections) == 4 and all(
                        record["source_count"] for record in collections
                    ):
                        break
                time.sleep(0.15)
            assert {record["provider"] for record in collections} == {
                "zim",
                "files",
                "kolibri",
                "kiwix",
            }

            status, body = _get(base + "/search?q=algebra%20biology")
            assert status == 200
            results = json.loads(body)
            assert results

            for endpoint in ("/sources?limit=200", "/library.json?limit=200"):
                status, body = _get(base + endpoint)
                assert status == 200
                payload = json.loads(body)
                assert isinstance(payload, list)
                assert str(tmp_path) not in body.decode("utf-8")

            status, body = _get(base + "/health")
            assert status == 200
            health = json.loads(body)
            assert health["status"] == "ok"
            assert set(health["collections"]) == {
                record["key"] for record in collections
            }
            for record in health["collections"].values():
                assert "last_refresh" in record
                assert "index_count" in record
                assert "truncated" in record
                assert "collapsed" in record

            status, body = _get(base + "/sources?limit=200")
            sources = json.loads(body)
            zim_source = next(
                source for source in sources if source["provider"] == "zim"
            )
            key = urllib.parse.quote(zim_source["key"], safe="")
            status, shell = _get(base + f"/content/{key}")
            assert status == 200
            assert b"zimantic" in shell.lower() or b"<html" in shell.lower()

            listed = _get(base + "/library.json?limit=200")[1].decode("utf-8")
            assert "files" in listed and "kolibri" in listed and "kiwix" in listed
            assert "X-Content-Type-Options" not in listed
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
