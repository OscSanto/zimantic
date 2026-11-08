"""Kiwix provider runtime, recovery, search, and redirect-boundary tests."""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from zimantic.providers.base import CollectionInfo, ProviderContext, SourceQuery
from zimantic.providers.config import KiwixConfig, RuntimeConfig
from zimantic.providers.kiwix import (
    KiwixProvider,
    KiwixRuntime,
    kiwix_collection_key,
    kiwix_source_key,
    source_from_entry,
)


class _Response:
    headers: dict[str, str] = {}

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self._read = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        if self._read:
            return b""
        self._read = True
        return self.payload


def _feed(books: list[str]) -> bytes:
    entries = "".join(
        f'<entry><title>{book.title()}</title><updated>2025-01-01</updated>'
        f'<link type="text/html" href="/content/{book}"/>'
        f'<link rel="http://opds-spec.org/image/thumbnail" '
        f'type="image/png" href="/icons/{book}.png"/>'
        "</entry>"
        for book in books
    )
    return f"<feed>{entries}</feed>".encode()


def _results(items: list[tuple[str, str, str]]) -> bytes:
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<description>{snippet}</description></item>"
        for title, link, snippet in items
    )
    return f"<rss>{body}</rss>".encode()


class _FakeKiwix:
    def __init__(
        self,
        *,
        catalog: bytes | None = None,
        results: bytes | None = None,
        catalog_error: Exception | None = None,
    ) -> None:
        self.catalog = catalog
        self.results = results
        self.catalog_error = catalog_error
        self.requests: list[str] = []
        self.search_error: Exception | None = None

    def __call__(self, request, timeout=None):
        self.requests.append(request.full_url)
        if "/catalog" in request.full_url:
            if self.catalog_error is not None:
                raise self.catalog_error
            query = parse_qs(urlsplit(request.full_url).query)
            if int(query.get("start", ["0"])[0]) > 0:
                return _Response(_feed([]))
            return _Response(self.catalog or _feed([]))
        if self.search_error is not None:
            raise self.search_error
        return _Response(self.results or _results([]))


def _context(settings: dict | None = None) -> ProviderContext:
    return ProviderContext(
        config=RuntimeConfig(settings=settings or {}),
        index_dir=Path("."),
        web_base_path="",
        logger=logging.getLogger(__name__),
    )


def _config(**kwargs) -> KiwixConfig:
    return KiwixConfig(name="remote", server="http://kiwix", **kwargs)


def _collection(label: str = "Kiwix") -> CollectionInfo:
    return CollectionInfo(
        key=kiwix_collection_key("remote"),
        provider="kiwix",
        label=label,
        title=label,
    )


def _runtime(fake: _FakeKiwix, **kwargs) -> KiwixRuntime:
    options = {
        "source_timeout": 1.0,
        "urlopen_fn": fake,
        "sleep_fn": lambda _delay: None,
        "retry_backoff": 0.01,
    }
    options.update(kwargs)
    return KiwixRuntime(_collection(), _config(), **options)


def _query(text: str = "page", limit: int = 5, deadline=None) -> SourceQuery:
    return SourceQuery(
        query=text,
        title_query=text,
        fulltext_query=text,
        query_vector=None,
        limit=limit,
        deadline=deadline,
    )


def test_provider_discovery_does_not_touch_the_network():
    fake = _FakeKiwix(catalog=_feed(["manual"]))
    provider = KiwixProvider()
    batch = provider.discover(_config(), _context())
    try:
        collection = batch.collections[0]
        assert collection.key == "kiwix:remote"
        assert collection.provider == "kiwix"
        assert collection.label == "Kiwix"
        assert fake.requests == []
    finally:
        batch.close()


def test_runtime_discovers_books_and_searches_them():
    fake = _FakeKiwix(
        catalog=_feed(["manual"]),
        results=_results([("Page", "manual/Page", "snippet")]),
    )
    runtime = _runtime(fake)
    try:
        assert runtime.refresh(wait=True)
        assert [source.title for source in runtime.sources] == ["Manual"]
        source = runtime.sources[0]
        assert source.key == kiwix_source_key("remote", "manual")
        assert source.provider == "kiwix"
        assert source.collection_key == "kiwix:remote"
        assert source.homepage == "http://kiwix/content/manual"
        assert source.icon_url == "http://kiwix/icons/manual.png"

        result = runtime.search(source, _query())
        assert result.status == "ok"
        assert [doc["title"] for doc in result.items] == ["Page"]
        assert result.items[0]["url"] == "http://kiwix/manual/Page"
        assert result.items[0]["path"] == "http://kiwix/manual/Page"
        assert result.fulltext[0][0] == 1
        assert result.items[0]["id"].endswith("url:http://kiwix/manual/Page")
        assert runtime.content_store(source) is None
        assert runtime.suggest(source, "pa", 5) == ()
        assert runtime.max_sources_per_search() == 100
    finally:
        runtime.close()


def test_listing_source_metadata_never_requests_the_network():
    fake = _FakeKiwix(catalog=_feed(["manual"]))
    runtime = _runtime(fake)
    try:
        assert runtime.sources == ()
        snapshot = runtime.snapshot
        assert snapshot.discovery_state == "loading"
        assert runtime.library_sources() == ()
        assert fake.requests == []
    finally:
        runtime.close()


def test_failed_catalog_keeps_last_good_sources_and_logs_once(caplog):
    fake = _FakeKiwix(catalog=_feed(["manual", "cooking"]))
    runtime = _runtime(fake, retry_backoff=60.0)
    try:
        assert runtime.refresh(wait=True)
        assert len(runtime.sources) == 2

        fake.catalog_error = OSError("down")
        with caplog.at_level(logging.WARNING):
            assert not runtime.refresh(wait=True)
            assert not runtime.refresh(wait=True)

        assert len(runtime.sources) == 2
        snapshot = runtime.snapshot
        assert snapshot.discovery_state == "error"
        assert "down" in (snapshot.error or "")
        warnings = [
            record
            for record in caplog.records
            if "Kiwix catalog refresh failed" in record.getMessage()
        ]
        assert len(warnings) == 1
    finally:
        runtime.close()


def test_background_retry_recovers_after_a_failed_refresh():
    fake = _FakeKiwix(catalog=_feed(["manual"]))
    fake.catalog_error = OSError("down")
    runtime = _runtime(fake, retry_backoff=0.01)
    try:
        runtime.refresh(wait=False)
        fake.catalog_error = None
        deadline = time.monotonic() + 3
        while not runtime.sources and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [source.title for source in runtime.sources] == ["Manual"]
        assert runtime.snapshot.discovery_state == "ready"
    finally:
        runtime.close()


def test_catalog_over_the_entry_cap_keeps_the_prior_snapshot():
    fake = _FakeKiwix(catalog=_feed(["manual"]))
    runtime = KiwixRuntime(
        _collection(),
        KiwixConfig(
            name="remote",
            server="http://kiwix",
            max_catalog_entries=1,
        ),
        source_timeout=1.0,
        urlopen_fn=fake,
        sleep_fn=lambda _delay: None,
        retry_backoff=60.0,
    )
    try:
        assert runtime.refresh(wait=True)
        assert [source.title for source in runtime.sources] == ["Manual"]

        fake.catalog = _feed(["manual", "cooking"])
        assert not runtime.refresh(wait=True)
        assert [source.title for source in runtime.sources] == ["Manual"]
        snapshot = runtime.snapshot
        assert snapshot.discovery_state == "error"
        assert "1 entry limit" in (snapshot.error or "")
    finally:
        runtime.close()


def test_failed_source_search_is_operation_scoped_and_recovers():
    fake = _FakeKiwix(
        catalog=_feed(["manual"]),
        results=_results([("Page", "http://kiwix/manual/Page", "snippet")]),
    )
    runtime = _runtime(fake)
    try:
        assert runtime.refresh(wait=True)
        source = runtime.sources[0]
        fake.search_error = URLError("offline")
        failed = runtime.search(source, _query())
        assert failed.status == "error"
        assert "Kiwix search failed" in (failed.error or "")
        assert runtime.sources == (source,)
        assert runtime.snapshot.discovery_state == "ready"

        fake.search_error = None
        recovered = runtime.search(source, _query())
        assert recovered.status == "ok"
        assert recovered.items[0]["url"] == "http://kiwix/manual/Page"
    finally:
        runtime.close()


def test_server_path_prefix_is_preserved_for_catalog_and_search():
    fake = _FakeKiwix(
        catalog=_feed(["manual"]),
        results=_results(
            [
                ("Relative", "manual/page", ""),
                ("Root", "/content/manual/root", ""),
            ]
        ),
    )
    runtime = KiwixRuntime(
        _collection(),
        KiwixConfig(name="remote", server="http://kiwix/prefix"),
        source_timeout=1.0,
        urlopen_fn=fake,
    )
    try:
        assert runtime.refresh(wait=True)
        catalog_url = next(url for url in fake.requests if "/catalog" in url)
        assert catalog_url.startswith(
            "http://kiwix/prefix/kiwix/catalog/v2/entries?"
        )

        result = runtime.search(runtime.sources[0], _query())
        search_url = next(url for url in fake.requests if "/search" in url)
        assert search_url.startswith("http://kiwix/prefix/kiwix/search?")
        assert [doc["url"] for doc in result.items] == [
            "http://kiwix/prefix/manual/page",
            "http://kiwix/content/manual/root",
        ]
    finally:
        runtime.close()


def test_unsafe_and_foreign_result_links_are_rejected():
    fake = _FakeKiwix(
        catalog=_feed(["manual"]),
        results=_results(
            [
                ("Good", "http://kiwix/manual/Good", ""),
                ("Script", "javascript:alert(1)", ""),
                ("Data", "data:text/html,boom", ""),
                ("Foreign", "http://evil.test/page", ""),
                ("Schemeless foreign", "//evil.test/page", ""),
                ("Credentials", "http://user:pass@kiwix/page", ""),
            ]
        ),
    )
    runtime = _runtime(fake)
    try:
        assert runtime.refresh(wait=True)
        result = runtime.search(runtime.sources[0], _query())
        assert [doc["title"] for doc in result.items] == ["Good"]
        assert result.status == "ok"
    finally:
        runtime.close()


class _LocalHttpServer:
    def __init__(self, routes) -> None:
        self.routes = routes
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                path = urlsplit(self.path).path
                route = outer.routes.get(path, (404, {}, b"not found"))
                if len(route) == 4:
                    status, headers, body, delay = route
                else:
                    status, headers, body = route
                    delay = 0.0
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        ).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _source_for(server: str):
    return source_from_entry(
        _collection(), {"book": "manual", "name": "Manual"}, server=server
    )


def test_foreign_redirect_is_rejected_before_the_foreign_server_is_contacted():
    foreign = _LocalHttpServer({"/secret": (200, {}, b"<rss/>")})
    try:
        kiwix = _LocalHttpServer(
            {
                "/kiwix/search": (
                    302,
                    {"Location": foreign.url + "/secret"},
                    b"",
                )
            }
        )
        try:
            runtime = KiwixRuntime(
                _collection(),
                KiwixConfig(name="remote", server=kiwix.url),
                source_timeout=1.0,
            )
            try:
                result = runtime.search(_source_for(kiwix.url), _query())
                assert result.status == "error"
                assert "cross-origin" in (result.error or "") or "redirect" in (
                    result.error or ""
                )
                assert foreign.requests == []
            finally:
                runtime.close()
        finally:
            kiwix.close()
    finally:
        foreign.close()


def test_same_origin_redirect_chains_are_bounded():
    results = _results([("Page", "/content/manual/Page", "")])
    kiwix = _LocalHttpServer(
        {
            "/kiwix/search": (302, {"Location": "/hop1"}, b""),
            "/hop1": (302, {"Location": "/hop2"}, b""),
            "/hop2": (200, {}, results),
            "/loop0": (302, {"Location": "/loop1"}, b""),
            "/loop1": (302, {"Location": "/loop2"}, b""),
            "/loop2": (302, {"Location": "/loop3"}, b""),
            "/loop3": (302, {"Location": "/loop4"}, b""),
            "/loop4": (302, {"Location": "/loop5"}, b""),
            "/loop5": (302, {"Location": "/loop6"}, b""),
            "/loop6": (200, {}, results),
        }
    )
    try:
        runtime = KiwixRuntime(
            _collection(),
            KiwixConfig(name="remote", server=kiwix.url),
            source_timeout=2.0,
        )
        try:
            ok = runtime.search(_source_for(kiwix.url), _query())
            assert ok.status == "ok"
            assert ok.items[0]["url"] == kiwix.url + "/content/manual/Page"

            # Exercise a longer same-origin chain through the shared opener.
            from urllib.request import Request

            from zimantic.providers.kiwix.catalog import _open_url

            request = Request(kiwix.url + "/loop0")
            with pytest.raises(ValueError, match="too many"), _open_url(
                None,
                request,
                timeout=2.0,
                origin=("http", urlsplit(kiwix.url).netloc),
            ):
                pass
        finally:
            runtime.close()
    finally:
        kiwix.close()


def test_slow_remote_search_times_out_as_an_operation_error():
    results = _results([("Page", "/content/manual/Page", "")])
    kiwix = _LocalHttpServer(
        {
            "/kiwix/search": (
                200,
                {"Content-Type": "application/xml"},
                results,
                0.5,
            )
        }
    )
    try:
        runtime = KiwixRuntime(
            _collection(),
            KiwixConfig(name="remote", server=kiwix.url),
            source_timeout=0.1,
        )
        try:
            started = time.monotonic()
            result = runtime.search(_source_for(kiwix.url), _query())
            elapsed = time.monotonic() - started
            assert result.status == "error"
            assert elapsed < 0.45
        finally:
            runtime.close()
    finally:
        kiwix.close()


def test_max_sources_per_search_follows_configuration():
    fake = _FakeKiwix()
    runtime = KiwixRuntime(
        _collection(), _config(max_remote_sources=0), urlopen_fn=fake
    )
    try:
        assert runtime.max_sources_per_search() == 0
    finally:
        runtime.close()
    runtime = KiwixRuntime(
        _collection(), _config(max_remote_sources=5), urlopen_fn=fake
    )
    try:
        assert runtime.max_sources_per_search() == 5
    finally:
        runtime.close()


def test_search_republishes_background_catalog_sources(monkeypatch, tmp_path):
    from zimantic.providers.kiwix import catalog
    from zimantic.search import Search

    fake = _FakeKiwix(
        catalog=_feed(["manual"]),
        results=_results([("Page", "http://kiwix/manual/Page", "")]),
    )
    monkeypatch.setattr(catalog, "urlopen", fake)
    search = Search(
        RuntimeConfig(
            settings={"index_dir": str(tmp_path), "source_timeout": 1},
            collections=(KiwixConfig(name="remote", server="http://kiwix"),),
        ),
        semantic=False,
    )
    try:
        deadline = time.monotonic() + 3
        while not search.source_dicts() and time.monotonic() < deadline:
            time.sleep(0.01)
        sources = search.source_dicts()
        assert [source["title"] for source in sources] == ["Manual"]
        assert sources[0]["provider"] == "kiwix"
        assert search.collections["kiwix:remote"].provider == "kiwix"
        assert search.collection_dicts()[0]["discovery_state"] == "ready"
    finally:
        search.close()


def test_no_network_access_without_a_configured_kiwix_collection(
    monkeypatch, tmp_path
):
    from zimantic.providers.kiwix import catalog
    from zimantic.search import Search

    def fail(*_args, **_kwargs):
        raise AssertionError("a Kiwix request must not be made")

    monkeypatch.setattr(catalog, "urlopen", fail)
    search = Search(
        RuntimeConfig(settings={"index_dir": str(tmp_path)}),
        semantic=False,
    )
    try:
        assert search.source_dicts() == []
        assert search.collections == {}
    finally:
        search.close()
