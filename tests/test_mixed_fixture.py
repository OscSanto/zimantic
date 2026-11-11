"""The mixed-library fixture generates every provider and its local Kiwix API."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import urllib.request
import zipfile
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "mixed_library"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"zimantic_fixture_{name}", FIXTURES / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mixed_fixture_generates_all_providers(tmp_path):
    generate = _load("generate")
    root = generate.generate(
        tmp_path / "mixed", files=6, zim_entries=4, force=True
    )
    assert (root / "config.toml").is_file()
    assert (root / "zims" / generate.ZIM_NAME).is_file()
    assert (root / "files" / "shared" / "style.css").is_file()
    assert (root / "files" / ".private" / "secret.html").is_file()
    assert (root / "files" / "node_modules" / "pkg" / "index.html").is_file()
    assert (root / "queries.txt").read_text().count("\n") == 100
    metadata = json.loads((root / "INPUTS.json").read_text())
    assert metadata["kolibri"]["pinned_release"] == "v0.19.5"
    assert metadata["files"]["indexable_pages"] == 6

    db = sqlite3.connect(root / "kolibri" / "db.sqlite3")
    try:
        channel = db.execute(
            "SELECT name FROM content_channelmetadata"
        ).fetchone()[0]
        assert channel == generate.CHANNEL_NAME
        html5 = db.execute(
            "SELECT local_file_id FROM content_file WHERE preset = 'html5_zip'"
        ).fetchone()[0]
        caption = db.execute(
            "SELECT local_file_id FROM content_file WHERE preset = 'video_subtitle'"
        ).fetchone()[0]
        video = db.execute(
            "SELECT local_file_id FROM content_file WHERE preset = 'high_res_video'"
        ).fetchone()[0]
        for checksum in (html5, caption, video):
            storage = root / "kolibri" / "content" / "storage"
            matches = list(storage.glob(f"{checksum[0]}/{checksum[1]}/{checksum}.*"))
            assert matches, checksum
            if checksum == html5:
                with zipfile.ZipFile(matches[0]) as bundle:
                    assert "index.html" in bundle.namelist()
            elif checksum == video:
                # The committed sample is a real MP4, not placeholder bytes.
                assert matches[0].read_bytes()[4:8] == b"ftyp"
            elif checksum == caption:
                assert b"WEBVTT" in matches[0].read_bytes()
    finally:
        db.close()


def test_local_kiwix_fixture_serves_catalog_and_search():
    kiwix = _load("kiwix_server")
    with kiwix.KiwixFixture() as fixture:
        with urllib.request.urlopen(
            fixture.url + "/kiwix/catalog/v2/entries?count=10", timeout=5
        ) as response:
            catalog = response.read()
        with urllib.request.urlopen(
            fixture.url
            + "/kiwix/search?pattern=algebra&books.name=fixture-algebra",
            timeout=5,
        ) as response:
            search = response.read()
    assert b"Fixture Algebra" in catalog
    assert b"result-1" in search
