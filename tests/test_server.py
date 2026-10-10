import json
import unittest

from fastapi.testclient import TestClient

from zimantic.cache import QueryCache
from zimantic.search import SearchQueryError
from zimantic.server import create_app


class FakeSearch:
    cache = QueryCache(4)

    def local_names(self):
        return ["manual"]

    def source_dicts(self):
        return [{"key": "manual", "name": "Manual", "available": True}]

    @staticmethod
    def _validate_query(query):
        if not query.strip():
            raise SearchQueryError("empty")
        return query

    def search_page(self, query, zim, limit, offset, source, debug):
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
        cls.client = TestClient(create_app(FakeSearch()))
        cls.debug_client = TestClient(create_app(FakeSearch(), debug=True))

    def test_sources_and_json_search(self):
        self.assertEqual(self.client.get("/api/sources").json()[0]["key"], "manual")
        response = self.client.get("/api/search?q=page")
        self.assertEqual(response.json()[0]["title"], "Page")
        # Totals travel as headers so the body stays a plain list.
        self.assertEqual(response.headers["x-total-count"], "1")
        self.assertEqual(response.headers["x-has-more"], "false")

    def test_search_accepts_page_and_filter_parameters(self):
        response = self.client.get("/api/search?q=page&offset=10&source=manual&limit=5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-offset"], "10")
        self.assertNotIn("score", response.json()[0])

    def test_debug_mode_is_server_wide(self):
        response = self.debug_client.get("/api/search?q=page")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["explain"], "debug")

        stream = self.debug_client.get("/api/search/stream?q=page")
        events = [json.loads(line) for line in stream.text.splitlines()]
        self.assertEqual(events[-1]["results"][0]["score"], 1.0)

    def test_config_reports_debug_mode(self):
        self.assertFalse(self.client.get("/api/config").json()["debug"])
        self.assertTrue(self.debug_client.get("/api/config").json()["debug"])

    def test_sources_ignores_client_refresh_parameter(self):
        # Clients cannot force a source refresh: any refresh parameter is ignored.
        response = self.client.get("/api/sources?refresh=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["key"], "manual")

    def test_sources_supports_etag_revalidation(self):
        first = self.client.get("/api/sources")
        etag = first.headers["etag"]
        second = self.client.get("/api/sources", headers={"If-None-Match": etag})
        self.assertEqual(second.status_code, 304)

    def test_reload_is_not_exposed_over_http(self):
        # Reload is an admin action via `zimantic reload` (SIGHUP), never over HTTP.
        response = self.client.post("/api/reload")
        self.assertEqual(response.status_code, 404)

    def test_streaming_search_returns_ndjson(self):
        response = self.client.get("/api/search/stream?q=page")
        self.assertTrue(response.headers["content-type"].startswith("application/x-ndjson"))
        self.assertEqual(
            [json.loads(line)["type"] for line in response.text.splitlines()],
            ["started", "done"],
        )

    def test_streaming_search_validates_query(self):
        self.assertEqual(self.client.get("/api/search/stream?q=%20").status_code, 400)

    def test_health_reports_indexes_and_cache(self):
        body = self.client.get("/api/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["indexes"], ["manual"])
        self.assertIn("size", body["cache"])


if __name__ == "__main__":
    unittest.main()