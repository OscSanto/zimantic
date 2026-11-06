import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from zimantic.providers.base import ProviderContext, SourceQuery
from zimantic.providers.config import KiwixConfig, RuntimeConfig
from zimantic.providers.kiwix import KiwixProvider
from zimantic.searching.sources import _parse_remote_xml


class _Response:
    headers = {}

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self._read = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        if self._read:
            return b""
        self._read = True
        return self.payload


def _context() -> ProviderContext:
    return ProviderContext(
        config=RuntimeConfig(),
        index_dir=Path("."),
        web_base_path="",
        logger=logging.getLogger(__name__),
    )


def test_utf16_dtd_is_rejected_before_expansion():
    payload = (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<!DOCTYPE feed [<!ENTITY x "boom">]><feed>&x;</feed>'
    ).encode("utf-16")
    with pytest.raises(ValueError):
        _parse_remote_xml(payload)


def test_kiwix_runtime_discovers_and_searches(monkeypatch):
    catalog = (
        b'<feed><entry><title>Manual</title>'
        b'<link href="http://kiwix/content/manual"/></entry></feed>'
    )
    results = (
        b"<rss><item><title>Page</title>"
        b"<link>http://kiwix/content/manual/Page</link>"
        b"<description>snippet</description></item></rss>"
    )

    def fake_urlopen(request, timeout=None):
        return _Response(catalog if "catalog" in request.full_url else results)

    monkeypatch.setattr("zimantic.searching.kiwix.urlopen", fake_urlopen)
    provider = KiwixProvider()
    batch = provider.discover(
        KiwixConfig(name="remote", server="http://kiwix"), _context()
    )
    runtime = batch.open(batch.collections[0])
    try:
        assert runtime.refresh(wait=True)
        assert [source.title for source in runtime.sources] == ["Manual"]
        result = runtime.search(
            runtime.sources[0],
            SourceQuery(
                query="page",
                title_query="page",
                fulltext_query="page",
                query_vector=None,
                limit=5,
            ),
        )
        assert result.status == "ok"
        assert result.items[0]["url"].startswith("http://kiwix/")
        assert result.fulltext[0][0] == 1
    finally:
        runtime.close()
        batch.close()


def test_result_link_crossing_origin_is_rejected(monkeypatch):
    catalog = (
        b'<feed><entry><title>Manual</title>'
        b'<link href="http://kiwix/content/manual"/></entry></feed>'
    )
    results = (
        b"<rss><item><title>Foreign</title>"
        b"<link>http://evil.test/page</link></item></rss>"
    )

    def fake_urlopen(request, timeout=None):
        return _Response(catalog if "catalog" in request.full_url else results)

    monkeypatch.setattr("zimantic.searching.kiwix.urlopen", fake_urlopen)
    provider = KiwixProvider()
    batch = provider.discover(
        KiwixConfig(name="remote", server="http://kiwix"), _context()
    )
    runtime = batch.open(batch.collections[0])
    try:
        runtime.refresh(wait=True)
        result = runtime.search(
            runtime.sources[0],
            SourceQuery(
                query="page",
                title_query="page",
                fulltext_query="page",
                query_vector=None,
                limit=5,
            ),
        )
        assert result.items == []
    finally:
        runtime.close()
        batch.close()
