import json
import unittest

from fastapi.testclient import TestClient

from zimantic.cache import QueryCache
from zimantic.search import SearchQueryError
from zimantic.server import create_app


class FakeSearch:
    cfg = {"results": 2}
    cache = QueryCache(4)

    def local_names(self):
        return ["manual"]

    def source_dicts(self):
        return [{"key": "manual", "name": "Manual", "available": True}]

    def refresh_sources(self):
        return self.source_dicts()

    def reload(self):
        return {"indexes": ["manual"], "added": [], "removed": [], "upgraded": []}

    @staticmethod
    def _validate_query(query):
        if not query.strip():
            raise SearchQueryError("empty")
        return query

    def search(self, query, zim, limit):
        return [{"id": "manual:page", "title": "Page"}]

    def stream_search(self, query, zim, limit):
        yield {"type": "started", "total": 1}
        yield {"type": "done", "results": [{"id": "manual:page", "title": "Page"}]}


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(create_app(FakeSearch()))

    def test_sources_and_legacy_json_search(self):
        self.assertEqual(self.client.get("/api/sources").json()[0]["key"], "manual")
        self.assertEqual(self.client.get("/api/search?q=page").json()[0]["title"], "Page")

    def test_streaming_search_returns_ndjson(self):
        response = self.client.get("/api/search/stream?q=page")
        self.assertTrue(response.headers["content-type"].startswith("application/x-ndjson"))
        self.assertEqual(
            [json.loads(line)["type"] for line in response.text.splitlines()],
            ["started", "done"],
        )

    def test_streaming_search_validates_query(self):
        self.assertEqual(self.client.get("/api/search/stream?q=%20").status_code, 400)

    def test_reload_endpoint_rescans(self):
        response = self.client.post("/api/reload")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["indexes"], ["manual"])

    def test_health_reports_indexes_and_cache(self):
        body = self.client.get("/api/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["indexes"], ["manual"])
        self.assertIn("size", body["cache"])


if __name__ == "__main__":
    unittest.main()
