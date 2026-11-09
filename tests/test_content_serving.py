"""End-to-end content-store serving: keys, redirects, ranges, and CORS."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from unittest.mock import patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from libzim.writer import Creator, Hint, Item, StringProvider

from zimantic.content.store import ArticleNotFoundError
from zimantic.providers.config import FilesConfig, RuntimeConfig, ZimConfig
from zimantic.providers.files.store import FilesContentStore
from zimantic.providers.zim.discovery import zim_source_key
from zimantic.search import Search
from zimantic.web.app import create_app

HOME_HTML = b"<html><head><title>Home</title></head><body>Home body</body></html>"
ALPHA_HTML = b"<html><head><title>Alpha</title></head><body>Alpha body</body></html>"
BETA_HTML = b"<html><head><title>Beta</title></head><body>Beta body</body></html>"
DATA = bytes(range(64))
SHELL_SCRIPT = b'#!/bin/sh\necho "<script>not HTML</script>"\n'


class _Page(Item):
    def __init__(self, path: str, title: str, content: bytes, mimetype: str):
        super().__init__()
        self._path = path
        self._title = title
        self._content = content
        self._mimetype = mimetype

    def get_path(self):
        return self._path

    def get_title(self):
        return self._title

    def get_mimetype(self):
        return self._mimetype

    def get_contentprovider(self):
        return StringProvider(self._content)

    def get_hints(self):
        return {Hint.FRONT_ARTICLE: True}


def _create_zim(
    path: Path,
    *,
    main: str = "Home",
    body: bytes = HOME_HTML,
    alpha: bytes = ALPHA_HTML,
):
    creator = Creator(path)
    creator.config_indexing(True, "eng")
    creator.set_mainpath(main)
    with creator:
        creator.add_item(_Page("Home", "Home", body, "text/html"))
        creator.add_item(_Page("Alpha", "Alpha", alpha, "text/html"))
        creator.add_item(_Page("Beta", "Beta", BETA_HTML, "text/html"))
        creator.add_item(_Page("data.bin", "Data", DATA, "application/octet-stream"))
        creator.add_item(
            _Page("finish_install.sh", "Install", SHELL_SCRIPT, "application/x-sh")
        )
        creator.add_redirection("Alias", "Alias", "Alpha", {})


def _config(root: Path, *, max_article_bytes: int | None = None) -> RuntimeConfig:
    cfg = {
        "index_dir": str(root / "indexes"),
        "candidate_count": 16,
        "page_size": 10,
        "max_results": 100,
        "source_query_threads": 1,
    }
    if max_article_bytes is not None:
        cfg["max_article_bytes"] = max_article_bytes
    return RuntimeConfig(
        settings=cfg,
        collections=(ZimConfig(name="library", path=str(root / "zims")),),
    )


@pytest.fixture()
def site(tmp_path):
    zims = tmp_path / "zims"
    zims.mkdir()
    _create_zim(zims / "manual.zim")
    search = Search(_config(tmp_path))
    client = TestClient(create_app(search))
    key = zim_source_key("library", "manual.zim")
    try:
        yield client, search, key
    finally:
        client.close()
        search.close()


def _article(key: str, path: str) -> str:
    return f"/article/{quote(key, safe='')}/{path}"


def test_canonical_key_serves_shell_and_raw_article(site):
    client, _search, key = site
    assert ":" in key

    shell = client.get(f"/content/{quote(key, safe='')}/Alpha")
    assert shell.status_code == 200
    assert shell.headers["content-type"].startswith("text/html")
    assert f'src="/article/{quote(key, safe="")}/Alpha"'.encode() in shell.content

    raw = client.get(_article(key, "Alpha"))
    assert raw.status_code == 200
    assert raw.headers["access-control-allow-origin"] == "*"
    assert b"data-zimantic-bridge" in raw.content
    assert b"Alpha body" in raw.content

    homepage = client.get(f"/content/{quote(key, safe='')}", follow_redirects=False)
    assert homepage.status_code == 307
    assert homepage.headers["location"].startswith(
        f"/content/{quote(key, safe='')}/"
    )
    assert homepage.headers["access-control-allow-origin"] == "*"


def test_redirect_alias_collapses_to_one_final_item(site):
    client, _search, key = site
    response = client.get(_article(key, "Alias"), follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["location"] == _article(key, "Alpha")

    followed = client.get(_article(key, "Alias"), follow_redirects=True)
    assert followed.status_code == 200
    assert b"Alpha body" in followed.content

    query = client.get(
        _article(key, "Alias"), params={"keep": "1"}, follow_redirects=False
    )
    assert query.headers["location"] == _article(key, "Alpha") + "?keep=1"

    shell = client.get(
        f"/content/{quote(key, safe='')}/Alias", follow_redirects=False
    )
    assert shell.status_code == 307
    assert shell.headers["location"] == (
        f"/content/{quote(key, safe='')}/Alpha"
    )


def test_missing_paths_and_unknown_sources_use_the_error_envelope(site):
    client, _search, key = site
    missing = client.get(_article(key, "missing"))
    assert missing.status_code == 404
    assert missing.json() == {"error": "not_found", "source": key}
    assert missing.headers["access-control-allow-origin"] == "*"

    unknown = client.get("/article/zim:library:absent/missing")
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "source_not_found"
    assert "detail" not in unknown.json()
    assert unknown.headers["access-control-allow-origin"] == "*"

    shell = client.get(f"/content/{quote(key, safe='')}/missing")
    assert shell.status_code == 404
    assert "Article not found" in shell.text


def test_identical_paths_in_different_sources_open_their_own_body(tmp_path):
    zims = tmp_path / "zims"
    zims.mkdir()
    _create_zim(zims / "one.zim")
    _create_zim(
        zims / "two.zim",
        body=b"<html><body>Two home</body></html>",
        alpha=b"<html><body>Two alpha</body></html>",
    )

    search = Search(_config(tmp_path))
    client = TestClient(create_app(search))
    try:
        one = zim_source_key("library", "one.zim")
        two = zim_source_key("library", "two.zim")
        first = client.get(_article(one, "Alpha"))
        second = client.get(_article(two, "Alpha"))
        assert b"Alpha body" in first.content
        assert b"Two alpha" in second.content
    finally:
        client.close()
        search.close()


def test_full_range_requests_and_validators(site):
    client, _search, key = site
    full = client.get(_article(key, "data.bin"))
    assert full.status_code == 200
    assert full.content == DATA
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["access-control-allow-origin"] == "*"
    assert full.headers["content-disposition"].startswith("attachment")

    prefix = client.get(_article(key, "data.bin"), headers={"Range": "bytes=0-3"})
    assert prefix.status_code == 206
    assert prefix.content == DATA[:4]
    assert prefix.headers["content-range"] == f"bytes 0-3/{len(DATA)}"
    assert prefix.headers["content-length"] == "4"
    assert prefix.headers["accept-ranges"] == "bytes"
    assert prefix.headers["access-control-allow-origin"] == "*"
    exposed = prefix.headers["access-control-expose-headers"].lower()
    assert "content-range" in exposed
    assert "accept-ranges" in exposed
    assert prefix.headers.get("content-encoding") == "identity"

    open_ended = client.get(
        _article(key, "data.bin"), headers={"Range": "bytes=60-"}
    )
    assert open_ended.status_code == 206
    assert open_ended.content == DATA[60:]
    assert open_ended.headers["content-range"] == f"bytes 60-63/{len(DATA)}"

    suffix = client.get(
        _article(key, "data.bin"), headers={"Range": "bytes=-4"}
    )
    assert suffix.status_code == 206
    assert suffix.content == DATA[-4:]
    assert suffix.headers["content-range"] == f"bytes 60-63/{len(DATA)}"

    partial = client.get(
        _article(key, "data.bin"), headers={"Range": "bytes=2-5"}
    )
    assert partial.content == DATA[2:6]
    assert partial.headers["content-range"] == f"bytes 2-5/{len(DATA)}"


def test_script_text_serves_inline_for_get_head_and_ranges(site):
    client, _search, key = site
    url = _article(key, "finish_install.sh")
    full = client.get(url)
    head = client.head(url)
    partial = client.get(url, headers={"Range": "bytes=0-8"})

    assert full.status_code == head.status_code == 200
    assert full.content == SHELL_SCRIPT
    assert head.content == b""
    assert head.headers["content-length"] == str(len(SHELL_SCRIPT))
    assert partial.status_code == 206
    assert partial.content == SHELL_SCRIPT[:9]
    assert partial.headers["content-range"] == f"bytes 0-8/{len(SHELL_SCRIPT)}"
    for response in (full, head, partial):
        assert response.headers["content-type"].startswith("text/plain")
        assert "content-disposition" not in response.headers
        assert response.headers["x-content-type-options"] == "nosniff"

    shell = client.get(f"/content/{quote(key, safe='')}/finish_install.sh")
    assert f'href="{url}">Install</a>' in shell.text
    assert f'src="{url}"' in shell.text
    assert '<div class="zimantic-shell-attachment"' not in shell.text


def test_unsatisfiable_and_ignored_ranges(site):
    client, _search, key = site
    out_of_range = client.get(
        _article(key, "data.bin"), headers={"Range": f"bytes={len(DATA)}-"}
    )
    assert out_of_range.status_code == 416
    assert out_of_range.headers["content-range"] == f"bytes */{len(DATA)}"
    assert out_of_range.headers["accept-ranges"] == "bytes"
    assert out_of_range.headers["access-control-allow-origin"] == "*"

    multi = client.get(
        _article(key, "data.bin"), headers={"Range": "bytes=0-1,4-5"}
    )
    assert multi.status_code == 200
    assert multi.content == DATA

    malformed = client.get(
        _article(key, "data.bin"), headers={"Range": "bytes=oops"}
    )
    assert malformed.status_code == 200
    assert malformed.content == DATA

    backwards = client.get(
        _article(key, "data.bin"), headers={"Range": "bytes=9-2"}
    )
    assert backwards.status_code == 200
    assert backwards.content == DATA


def test_if_range_without_validators_serves_the_full_response(site):
    client, _search, key = site
    response = client.get(
        _article(key, "data.bin"),
        headers={"Range": "bytes=0-3", "If-Range": '"some-validator"'},
    )
    assert response.status_code == 200
    assert response.content == DATA


def test_wildcard_if_none_match_returns_a_bodyless_304(site):
    client, _search, key = site
    response = client.get(
        _article(key, "data.bin"), headers={"If-None-Match": "*"}
    )
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["access-control-allow-origin"] == "*"

    non_wildcard = client.get(
        _article(key, "data.bin"), headers={"If-None-Match": '"etag"'}
    )
    assert non_wildcard.status_code == 200
    assert non_wildcard.content == DATA


def test_head_returns_metadata_without_a_body(site):
    client, _search, key = site
    response = client.head(_article(key, "data.bin"))
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(len(DATA))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["access-control-allow-origin"] == "*"

    html = client.head(_article(key, "Alpha"))
    assert html.status_code == 200
    assert html.content == b""
    # Decorated HTML is not range-addressable.
    assert "accept-ranges" not in html.headers
    assert int(html.headers["content-length"]) > len(ALPHA_HTML)


def test_ranges_are_ignored_for_decorated_html(site):
    client, _search, key = site
    response = client.get(
        _article(key, "Alpha"), headers={"Range": "bytes=0-3"}
    )
    assert response.status_code == 200
    assert b"Alpha body" in response.content
    assert b"data-zimantic-bridge" in response.content
    assert "content-range" not in response.headers


def test_oversized_item_returns_too_large_with_cors(tmp_path):
    zims = tmp_path / "zims"
    zims.mkdir()
    _create_zim(zims / "manual.zim")
    search = Search(_config(tmp_path, max_article_bytes=8))
    client = TestClient(create_app(search))
    key = zim_source_key("library", "manual.zim")
    try:
        full = client.get(_article(key, "data.bin"))
        assert full.status_code == 413
        assert full.json()["error"] == "article_too_large"
        assert full.headers["access-control-allow-origin"] == "*"

        ranged = client.get(
            _article(key, "data.bin"), headers={"Range": "bytes=0-1"}
        )
        assert ranged.status_code == 413
        assert ranged.headers["access-control-allow-origin"] == "*"

        # The shell only needs a bounded prefix, so a large item still opens.
        shell = client.get(f"/content/{quote(key, safe='')}/Alpha")
        assert shell.status_code == 200
    finally:
        client.close()
        search.close()


@pytest.mark.parametrize(
    ("filename", "mimetype"),
    [("about_datapost.mp4", "video/mp4"), ("lesson.mp3", "audio/mpeg")],
)
def test_oversized_file_media_streams_inline(tmp_path, filename, mimetype):
    root = tmp_path / "modules"
    root.mkdir()
    (root / filename).write_bytes(DATA)
    (root / "page.html").write_bytes(ALPHA_HTML)
    (root / "data.bin").write_bytes(DATA)
    config = RuntimeConfig(
        settings={
            "index_dir": str(tmp_path / "indexes"),
            "max_article_bytes": 8,
        },
        collections=(FilesConfig(name="modules", path=str(root)),),
    )
    search = Search(config)
    client = TestClient(create_app(search))
    key = "files:modules:root"
    opened_files = []
    open_stream = FilesContentStore.open_stream

    def capture_stream(store, path):
        opened = open_stream(store, path)
        opened_files.append(opened)
        return opened

    try:
        url = _article(key, filename)
        with (
            patch.object(FilesContentStore, "open_stream", capture_stream),
            patch.object(FilesContentStore, "read", side_effect=AssertionError),
            patch("zimantic.web.app.ARTICLE_STREAM_CHUNK", 16),
        ):
            full = client.get(url)
            head = client.head(url)
            conditional = client.get(
                url, headers={"Range": "bytes=0-3", "If-Range": '"stale"'}
            )
        for response in (full, conditional):
            assert response.status_code == 200
            assert response.content == DATA
            assert response.headers["content-type"] == mimetype
            assert response.headers["content-length"] == str(len(DATA))
            assert response.headers["accept-ranges"] == "bytes"
            assert response.headers["access-control-allow-origin"] == "*"
            assert "content-disposition" not in response.headers
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-type"] == mimetype
        assert head.headers["content-length"] == str(len(DATA))
        assert len(opened_files) == 2
        assert all(opened.handle.closed for opened in opened_files)

        partial = client.get(url, headers={"Range": "bytes=10-13"})
        assert partial.status_code == 206
        assert partial.content == DATA[10:14]
        assert partial.headers["content-type"] == mimetype
        assert partial.headers["content-range"] == f"bytes 10-13/{len(DATA)}"
        for path in ("page.html", "data.bin"):
            response = client.get(_article(key, path))
            assert response.status_code == 413
            assert response.json()["error"] == "article_too_large"
    finally:
        client.close()
        search.close()


def test_article_cors_preflight_is_bounded(site):
    client, _search, key = site
    response = client.options(
        _article(key, "data.bin"),
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "range,if-range",
        },
    )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    assert "GET" in response.headers["access-control-allow-methods"]
    assert "POST" not in response.headers["access-control-allow-methods"]
    allow_headers = response.headers["access-control-allow-headers"].lower()
    assert "range" in allow_headers
    assert "if-range" in allow_headers
    assert "x-not-allowed" not in allow_headers
    assert "access-control-allow-credentials" not in response.headers
    expose = response.headers["access-control-expose-headers"].lower()
    assert "content-range" in expose
    assert "accept-ranges" in expose


def test_capture_asset_referer_decodes_a_colon_key_exactly_once():
    from zimantic.content.store import ArticleData

    key = "files:modules:root"

    class _CaptureSearch:
        cfg: ClassVar[dict] = {}

        def __init__(self):
            self.seen: list[tuple[str, str]] = []

        def read_article(self, source, candidate):
            self.seen.append((source, candidate))
            if candidate == "-/mw/banner.jpg":
                return ArticleData(
                    source_key=source,
                    path=candidate,
                    title="Banner",
                    mimetype="image/png",
                    content=b"banner",
                    total=6,
                    truncated=False,
                )
            raise ArticleNotFoundError("missing")

    search = _CaptureSearch()
    client = TestClient(create_app(search))
    try:
        response = client.get(
            "/w/images/5/54/banner.jpg",
            headers={
                "referer": (
                    f"http://testserver/article/{quote(key, safe='')}/page"
                )
            },
        )
    finally:
        client.close()
    assert response.status_code == 200
    assert response.content == b"banner"
    assert search.seen[0] == (key, "w/images/5/54/banner.jpg")
    assert search.seen[1] == (key, "-/mw/banner.jpg")


def test_source_without_a_content_store_returns_404():
    class _NoStore:
        cfg: ClassVar[dict] = {}

        def content_store(self, identifier):
            raise ArticleNotFoundError("no store")

    client = TestClient(create_app(_NoStore()))
    try:
        response = client.get("/article/kiwix:remote:book/page")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
        assert response.headers["access-control-allow-origin"] == "*"
    finally:
        client.close()


def test_ambiguous_store_path_returns_404(site):
    client, _search, key = site
    import zimantic.providers.zim.store as store_module
    from zimantic.content.zim_paths import AmbiguousZimPathError

    with patch.object(
        store_module,
        "resolve_zim_path",
        side_effect=AmbiguousZimPathError("ambiguous"),
    ):
        raw = client.get(_article(key, "Alpha"))
        head = client.head(_article(key, "Alpha"))
        ranged = client.get(
            _article(key, "data.bin"), headers={"Range": "bytes=0-1"}
        )
        shell = client.get(f"/content/{quote(key, safe='')}/Alpha")
    for response in (raw, head, ranged):
        assert response.status_code == 404
        assert response.headers["access-control-allow-origin"] == "*"
    assert shell.status_code == 404
    assert b"data-zimantic-toolbar" in shell.content
    for response in (raw, ranged):
        assert response.json()["error"] == "not_found"
