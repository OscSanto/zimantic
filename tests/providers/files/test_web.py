"""End-to-end files-provider routing, search, and payload tests."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from zimantic.providers.files.discovery import files_dir_key
from zimantic.search import Search
from zimantic.web.app import create_app

from .helpers import build_sources, build_tree, files_config, open_runtime


@pytest.fixture()
def files_site(tmp_path):
    root = tmp_path / "modules"
    build_tree(root)
    (root / "shared").mkdir()
    (root / "shared" / "style.css").write_text(
        "body { background: white; }", encoding="utf-8"
    )
    (root / "algebra" / "image.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8"
    )
    (root / "algebra" / "mystery").write_bytes(
        b"<html><body>looks like html</body></html>"
    )
    config = files_config(root)
    index_dir = tmp_path / "indexes"
    runtime, batch, runtime_config = open_runtime(index_dir, config)
    try:
        build_sources(runtime, index_dir)
    finally:
        runtime.close()
        batch.close()
    search_cfg = {
        "index_dir": str(index_dir),
        "runtime_config": runtime_config,
        "collections": (config,),
        "candidate_count": 16,
        "page_size": 10,
        "max_results": 100,
        "search_workers": 1,
        "max_article_bytes": 1 << 20,
        "max_html_bytes": 512 << 10,
        "preview_limit": 10,
    }
    search = Search(search_cfg, semantic=False)
    client = TestClient(create_app(search))
    key = files_dir_key("modules", "algebra")
    try:
        yield client, search, key, root
    finally:
        client.close()
        search.close()


def _article(key: str, path: str | None = None) -> str:
    base = f"/article/{quote(key, safe='')}"
    return base if path is None else f"{base}/{path}"


def test_child_homepage_shell_and_raw_article_keep_the_prefix(files_site):
    client, _search, key, _root = files_site
    homepage = client.get(f"/content/{quote(key, safe='')}", follow_redirects=False)
    assert homepage.status_code == 307
    assert homepage.headers["location"] == (
        f"/content/{quote(key, safe='')}/algebra/index.html"
    )

    shell = client.get(f"/content/{quote(key, safe='')}/algebra/index.html")
    assert shell.status_code == 200
    assert b"<iframe" in shell.content
    assert f"/article/{quote(key, safe='')}/algebra/index.html".encode() in shell.content

    raw = client.get(_article(key, "algebra/index.html"))
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/html")
    assert b"data-zimantic-bridge" in raw.content
    assert b"Algebra" in raw.content

    raw_home = client.get(_article(key), follow_redirects=False)
    assert raw_home.status_code == 307
    assert raw_home.headers["location"] == _article(key, "algebra/index.html")


def test_assets_ranges_and_cross_source_serving(files_site):
    client, _search, key, _root = files_site
    css = client.get(_article(key, "algebra/assets/style.css"))
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert b"color" in css.content

    shared = client.get(_article(key, "shared/style.css"))
    assert shared.status_code == 200
    assert shared.headers["content-type"].startswith("text/css")

    video = client.get(
        _article(key, "biology/video.mp4"), headers={"Range": "bytes=10-19"}
    )
    assert video.status_code == 206
    assert video.headers["content-range"].startswith("bytes 10-19/")
    assert video.headers["accept-ranges"] == "bytes"


def test_direct_svg_is_sandboxed_and_unknown_extension_is_attachment(files_site):
    client, _search, key, _root = files_site
    svg = client.get(_article(key, "algebra/image.svg"))
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in svg.headers["content-security-policy"]
    assert svg.headers["x-content-type-options"] == "nosniff"

    mystery = client.get(_article(key, "algebra/mystery"))
    assert mystery.status_code == 200
    assert mystery.headers["content-type"].startswith("application/octet-stream")
    assert mystery.headers["content-disposition"].startswith("attachment")
    assert mystery.headers["x-content-type-options"] == "nosniff"


def test_hidden_and_excluded_requests_are_404(files_site):
    client, _search, key, _root = files_site
    assert client.get(_article(key, ".hidden/secret.html")).status_code == 404
    assert (
        client.get(_article(key, "node_modules/pkg/index.html")).status_code
        == 404
    )
    assert client.get(_article(key, "../outside.txt")).status_code == 404


def test_search_and_library_payloads_use_files_keys_without_absolute_paths(
    files_site,
):
    client, _search, key, root = files_site
    response = client.get("/search", params={"q": "zebra"})
    assert response.status_code == 200
    results = response.json()
    assert results
    result = results[0]
    assert result["source_key"].startswith("files:modules:")
    assert result["url"].startswith("/content/files%3Amodules%3A")
    assert result["path"]
    assert "zebra" in result["lead"].casefold()

    for endpoint in ("/sources", "/library.json"):
        payload = client.get(endpoint).json()
        body = str(payload)
        assert str(root) not in body
        assert any(
            item.get("provider") == "files"
            for item in payload
        )

    library = client.get("/library.json").json()
    algebra = next(item for item in library if item["key"] == key)
    assert algebra["collection"]["key"] == "files:modules"
    assert algebra["homepage"] == "algebra/index.html"
    assert algebra["provider"] == "files"
