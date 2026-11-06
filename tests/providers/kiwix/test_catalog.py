"""Kiwix catalog parsing, bounds, retry, and pagination tests."""

from __future__ import annotations

from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from zimantic.providers.kiwix.catalog import (
    _bounded_catalog_url,
    _parse_remote_xml,
    _read_limited_response,
    fetch_catalog,
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


def _entry(book: str, title: str | None = None, updated: str = "") -> str:
    return (
        "<entry>"
        f"<title>{title or book}</title>"
        f"<updated>{updated}</updated>"
        f'<link type="text/html" href="/content/{book}"/>'
        "</entry>"
    )


def _feed(entries: list[str]) -> bytes:
    return ("<feed>" + "".join(entries) + "</feed>").encode()


def test_catalog_limit_replaces_unbounded_count():
    url = _bounded_catalog_url(
        "http://kiwix/entries?count=-1&foo=bar",
        50_000,
    )
    assert url == "http://kiwix/entries?count=50000&foo=bar"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_dtd_and_entity_declarations_are_rejected_before_expansion(encoding):
    payload = (
        '<?xml version="1.0" encoding="%s"?>'
        '<!DOCTYPE feed [<!ENTITY x "boom">]><feed>&x;</feed>' % encoding
    ).encode(encoding)
    with pytest.raises(ValueError):
        _parse_remote_xml(payload)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_ordinary_documents_in_supported_encodings_parse(encoding):
    payload = (
        '<?xml version="1.0" encoding="%s"?>'
        "<feed><entry><title>Manual</title></entry></feed>" % encoding
    ).encode(encoding)
    root = _parse_remote_xml(payload)
    assert root.tag.rsplit("}", 1)[-1] == "feed"


def test_remote_response_reader_enforces_a_hard_limit():
    class _UnboundedResponse:
        headers: dict[str, str] = {}

        def read(self, size=-1):
            return b"x" * 4

    with pytest.raises(ValueError):
        _read_limited_response(_UnboundedResponse(), 3)


def test_catalog_refresh_retries_with_bounded_backoff():
    response = _Response(_feed([_entry("manual", "Remote Manual")]))
    calls: list[str] = []
    delays: list[float] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise OSError("temporary outage")
        query = parse_qs(urlsplit(request.full_url).query)
        if int(query.get("start", ["0"])[0]) > 0:
            return _Response(_feed([]))
        return response

    result = fetch_catalog(
        "http://kiwix",
        urlopen_fn=fake_urlopen,
        sleep_fn=delays.append,
    )

    assert result.complete
    assert result.entries == (
        {"name": "Remote Manual", "book": "manual", "updated": "",
         "homepage": "/content/manual"},
    )
    assert len(calls) == 3
    assert calls[0] == calls[1]
    assert delays == [1.0]


def test_catalog_refresh_does_not_retry_non_retryable_4xx():
    calls: list[str] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 404, "not found", None, None)

    result = fetch_catalog(
        "http://kiwix",
        urlopen_fn=fake_urlopen,
        sleep_fn=lambda _delay: pytest.fail("must not back off"),
    )

    assert result.entries is None
    assert "404" in (result.error or "")
    assert len(calls) == 1


def test_catalog_response_size_limit_is_reported():
    result = fetch_catalog(
        "http://kiwix",
        max_response_bytes=3,
        urlopen_fn=lambda *_args, **_kwargs: _Response(b"<feed>large</feed>"),
    )

    assert result.entries is None
    assert "byte limit" in (result.error or "")


def test_catalog_pages_until_an_empty_page():
    books = [f"book-{index}" for index in range(5)]
    calls: list[int] = []

    def fake_urlopen(request, timeout=None):
        query = parse_qs(urlsplit(request.full_url).query)
        start = int(query.get("start", ["0"])[0])
        count = int(query["count"][0])
        calls.append(start)
        page = books[start : start + count]
        return _Response(_feed([_entry(book) for book in page]))

    result = fetch_catalog(
        "http://kiwix",
        catalog_limit=2,
        urlopen_fn=fake_urlopen,
        sleep_fn=lambda _delay: None,
    )

    assert result.complete
    assert [entry["book"] for entry in result.entries] == books
    assert calls == [0, 2, 4, 5]


def test_unpageable_catalog_is_incomplete():
    page = _feed([_entry("one"), _entry("two")])

    def fake_urlopen(_request, timeout=None):
        # Ignores the start offset: the same full page comes back forever.
        return _Response(page)

    result = fetch_catalog(
        "http://kiwix",
        catalog_limit=2,
        urlopen_fn=fake_urlopen,
        sleep_fn=lambda _delay: None,
    )

    assert result.entries is None
    assert "did not advance" in (result.error or "")


def test_catalog_over_the_entry_cap_never_publishes_a_prefix():
    page = _feed([_entry("one"), _entry("two"), _entry("three")])

    result = fetch_catalog(
        "http://kiwix",
        catalog_limit=10,
        max_entries=2,
        urlopen_fn=lambda *_args, **_kwargs: _Response(page),
        sleep_fn=lambda _delay: None,
    )

    assert result.entries is None
    assert "2 entry limit" in (result.error or "")


def test_exact_duplicate_slugs_are_deduplicated_but_versions_are_not_merged():
    page = _feed(
        [
            _entry("manual_2025-01", "Manual", "2025-01-01"),
            _entry("manual_2025-01", "Manual again", "2025-02-01"),
            _entry("manual_2025-02", "Manual", "2025-03-01"),
        ]
    )

    def fake_urlopen(request, timeout=None):
        query = parse_qs(urlsplit(request.full_url).query)
        if int(query.get("start", ["0"])[0]) > 0:
            return _Response(_feed([]))
        return _Response(page)

    result = fetch_catalog(
        "http://kiwix",
        urlopen_fn=fake_urlopen,
        sleep_fn=lambda _delay: None,
    )

    assert result.complete
    assert sorted(entry["book"] for entry in result.entries) == [
        "manual_2025-01",
        "manual_2025-02",
    ]
    first = next(
        entry for entry in result.entries if entry["book"] == "manual_2025-01"
    )
    assert first["name"] == "Manual again"
