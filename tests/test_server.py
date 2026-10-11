import json
import unittest

from fastapi.testclient import TestClient

from zimantic.cache import QueryCache
from zimantic.search import SearchQueryError
from zimantic.server import create_app


class FakeSearch:
    def __init__(self):
        self.cache = QueryCache(4)
        self.search_page_calls = []
        self.stream_calls = []
        self.refresh_calls = 0

    def local_names(self):
        return ["manual"]

    @staticmethod
    def read_source_icon(zim):
        return {
            "content": b"icon",
            "mimetype": "image/png",
        }

    def source_dicts(self):
        return [{"key": "manual", "name": "Manual", "available": True}]

    def refresh_sources(self):
        self.refresh_calls += 1
        return self.source_dicts()

    @staticmethod
    def _validate_query(query):
        if not query.strip():
            raise SearchQueryError("empty")
        return query

    def search_page(self, query, zim, limit, offset, source, debug):
        self.search_page_calls.append((query, zim, limit, offset, source, debug))
        result = {"id": "manual:page", "title": "Page"}
        if debug:
            result.update({"score": 1.0, "explain": "debug"})
        return {
            "results": [result],
            "total": 1,
            "has_more": False,
            "offset": offset,
            "counts": {"manual": 1},
        }

    def search(self, query, zim, limit=None, offset=0, source=None, debug=False):
        return self.search_page(query, zim, limit, offset, source, debug)["results"]

    def stream_search(self, query, zim, limit=None, offset=0, source=None, debug=False):
        self.stream_calls.append((query, zim, limit, offset, source, debug))
        result = {"id": "manual:page", "title": "Page"}
        if debug:
            result.update({"score": 1.0, "explain": "debug"})
        yield {"type": "started", "total_sources": 1, "offset": offset, "limit": limit or 10}
        yield {
            "type": "done",
            "results": [result],
            "completed": 1,
            "total_sources": 1,
            "total": 1,
            "has_more": False,
            "offset": offset,
            "limit": limit or 10,
            "counts": {"manual": 1},
        }


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.search = FakeSearch()
        cls.debug_search = FakeSearch()
        cls.client = TestClient(create_app(cls.search))
        cls.debug_client = TestClient(create_app(cls.debug_search, debug=True))

    def test_sources_and_json_search(self):
        self.assertEqual(self.client.get("/sources").json()[0]["key"], "manual")
        response = self.client.get("/search?q=page")
        self.assertEqual(response.json()[0]["title"], "Page")
        # Totals travel as headers so the body stays a plain list.
        self.assertEqual(response.headers["x-total-count"], "1")
        self.assertEqual(response.headers["x-has-more"], "false")

    def test_favicon_is_served(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("image/x-icon"))
        self.assertTrue(response.content.startswith(b"\x00\x00\x01\x00"))

        for path in (
            "/favicon-16x16.png",
            "/favicon-32x32.png",
            "/apple-touch-icon.png",
            "/android-chrome-192x192.png",
            "/android-chrome-512x512.png",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("image/png"))
            self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n"))

        manifest = self.client.get("/site.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertTrue(
            manifest.headers["content-type"].startswith("application/manifest+json")
        )
        self.assertEqual(manifest.json()["icons"][0]["type"], "image/png")

    def test_source_icon_is_served(self):
        response = self.client.get("/source-icon/manual")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("image/png"))
        self.assertEqual(response.content, b"icon")

    def test_large_articles_stream_in_chunks(self):
        content = memoryview(b"x" * (512 * 1024 + 1))
        self.search.read_article = lambda zim, path: {
            "path": path,
            "title": "Large",
            "mimetype": "application/octet-stream",
            "content": content,
        }
        try:
            response = self.client.get("/article/manual/large")
        finally:
            del self.search.read_article
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, bytes(content))

    def test_search_accepts_page_and_filter_parameters(self):
        response = self.client.get("/search?q=page&offset=10&source=manual&limit=5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-offset"], "10")
        self.assertNotIn("score", response.json()[0])
        self.assertEqual(
            self.search.search_page_calls[-1],
            ("page", None, 5, 10, "manual", False),
        )

    def test_search_accepts_multiple_source_filters(self):
        response = self.client.get("/search?q=page&source=manual&source=other")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search.search_page_calls[-1][4], ["manual", "other"])

    def test_debug_mode_is_server_wide(self):
        response = self.debug_client.get("/search?q=page")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["explain"], "debug")

        stream = self.debug_client.get("/search/stream?q=page")
        events = [json.loads(line) for line in stream.text.splitlines()]
        self.assertEqual(events[-1]["results"][0]["score"], 1.0)
        self.assertTrue(self.debug_search.stream_calls[-1][-1])

    def test_config_reports_debug_mode(self):
        self.assertFalse(self.client.get("/config").json()["debug"])
        self.assertTrue(self.debug_client.get("/config").json()["debug"])

    def test_sources_ignores_client_refresh_parameter(self):
        # Clients cannot force a source refresh: any refresh parameter is ignored.
        response = self.client.get("/sources?refresh=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["key"], "manual")
        self.assertEqual(self.search.refresh_calls, 0)

    def test_sources_supports_etag_revalidation(self):
        first = self.client.get("/sources")
        etag = first.headers["etag"]
        second = self.client.get("/sources", headers={"If-None-Match": etag})
        self.assertEqual(second.status_code, 304)

    def test_reload_is_not_exposed_over_http(self):
        # Reload is an admin action via `zimantic reload` (SIGHUP), never over HTTP.
        response = self.client.post("/reload")
        self.assertEqual(response.status_code, 404)

    def test_streaming_search_returns_ndjson(self):
        response = self.client.get("/search/stream?q=page")
        self.assertTrue(response.headers["content-type"].startswith("application/x-ndjson"))
        self.assertEqual(
            [json.loads(line)["type"] for line in response.text.splitlines()],
            ["started", "done"],
        )

    def test_streaming_search_validates_query(self):
        self.assertEqual(self.client.get("/search/stream?q=%20").status_code, 400)

    def test_health_reports_indexes_and_cache(self):
        body = self.client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["indexes"], ["manual"])
        self.assertIn("size", body["cache"])


if __name__ == "__main__":
    unittest.main()