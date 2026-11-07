#!/usr/bin/env python3
"""A local, deterministic Kiwix catalog/search endpoint for the mixed fixture.

Run it next to ``generate.py``:

    python tests/fixtures/mixed_library/kiwix_server.py --port 8085

The fixture never contacts the network.  It serves enough of the Kiwix HTTP API
for catalog discovery and remote search: ``/kiwix/catalog/v2/entries`` and
``/kiwix/search``.  The default three books match the mixed fixture's fixed
queries.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

BOOKS = (
    ("fixture-algebra", "Fixture Algebra", "2026-01-01"),
    ("fixture-biology", "Fixture Biology", "2026-01-02"),
    ("fixture-geography", "Fixture Geography", "2026-01-03"),
)


def catalog_xml(start: int = 0, count: int = 50) -> bytes:
    entries = []
    for book, title, updated in BOOKS[start : start + count]:
        entries.append(
            "<entry>"
            f"<title>{title}</title>"
            f"<updated>{updated}T00:00:00Z</updated>"
            f'<link type="text/html" href="/content/{book}"/>'
            "</entry>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + "".join(entries)
        + "</feed>"
    ).encode("utf-8")


def search_xml(pattern: str, book: str) -> bytes:
    title = next(
        (entry[1] for entry in BOOKS if entry[0] == book), "Fixture book"
    )
    items = [
        (
            f"{title}: {pattern}",
            f"/content/{book}/result-{index}",
            f"A fixed mixed-library result for {pattern}.",
        )
        for index in range(1, 6)
    ]
    return (
        "<rss><channel>"
        + "".join(
            f"<item><title>{item_title}</title><link>{link}</link>"
            f"<description>{snippet}</description></item>"
            for item_title, link, snippet in items
        )
        + "</channel></rss>"
    ).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "zimantic-mixed-kiwix/1"

    def _send(self, payload: bytes, content_type: str = "application/xml") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path.rstrip("/") == "/kiwix/catalog/v2/entries":
            start = int(query.get("start", ["0"])[0] or 0)
            count = int(query.get("count", ["50"])[0] or 50)
            self._send(catalog_xml(start=start, count=count))
            return
        if parsed.path.rstrip("/") == "/kiwix/search":
            pattern = (query.get("pattern") or [""])[0]
            book = (query.get("books.name") or [""])[0]
            self._send(search_xml(pattern, book))
            return
        if parsed.path.rstrip("/") == "/health":
            self._send(json.dumps({"status": "ok"}).encode(), "application/json")
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args) -> None:
        return


class KiwixFixture:
    """Context manager running the fixture on an OS-assigned or fixed port."""

    def __init__(self, port: int = 0, host: str = "127.0.0.1") -> None:
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="zimantic-kiwix-fixture",
            daemon=True,
        )

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "KiwixFixture":
        self.thread.start()
        return self

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def __enter__(self) -> "KiwixFixture":
        return self.start()

    def __exit__(self, *_args) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8085)
    args = parser.parse_args(argv)
    with KiwixFixture(port=args.port, host=args.host) as fixture:
        print(fixture.url, flush=True)
        try:
            fixture.thread.join()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
