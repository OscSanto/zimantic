import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from zimantic.cache import QueryCache
from zimantic.search import SearchQueryError
from zimantic.web.app import create_app


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
            "matched_by": {"semantic": 1, "title": 2, "fulltext": 3},
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
            "matched_by": {"semantic": 1, "title": 2, "fulltext": 3},
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

    def test_page_assets_are_served(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/app.css"', page.text)
        self.assertIn('src="/app.js"', page.text)

        css = self.client.get("/app.css")
        self.assertEqual(css.status_code, 200)
        self.assertTrue(css.headers["content-type"].startswith("text/css"))
        self.assertIn(":root", css.text)

        javascript = self.client.get("/app.js")
        self.assertEqual(javascript.status_code, 200)
        self.assertTrue(javascript.headers["content-type"].startswith("application/javascript"))
        self.assertIn("initialize();", javascript.text)

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
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertEqual(response.content, bytes(content))

    def test_content_route_wraps_articles_and_article_route_serves_raw(self):
        body = b"<html><head></head><body>Article body</body></html>"
        self.search.read_article = lambda zim, path: {
            "path": path,
            "title": "Page",
            "mimetype": "text/html",
            "content": body,
        }
        try:
            shell = self.client.get("/content/manual/page")
            raw = self.client.get("/article/manual/page")
        finally:
            del self.search.read_article

        # /content/ is the toolbar shell: static toolbar plus an iframe that
        # points at the raw article, never the article HTML inline.
        self.assertEqual(shell.status_code, 200)
        self.assertTrue(shell.headers["content-type"].startswith("text/html"))
        self.assertIn("data-zimantic-toolbar", shell.text)
        self.assertIn('src="/article/manual/page"', shell.text)
        self.assertIn("zimantic-external-link", shell.text)
        self.assertNotIn("Article body", shell.text)

        # /article/ serves the raw article with the shell-navigation bridge
        # and no toolbar of its own.
        self.assertEqual(raw.status_code, 200)
        self.assertTrue(raw.headers["content-type"].startswith("text/html"))
        self.assertEqual(raw.headers["access-control-allow-origin"], "*")
        self.assertIn("Article body", raw.text)
        self.assertIn("data-zimantic-bridge", raw.text)
        self.assertIn("zimantic-external-link", raw.text)
        self.assertNotIn("data-zimantic-toolbar", raw.text)

    def _raw_article(self, body: bytes):
        with patch.object(
            self.search,
            "read_article",
            lambda zim, path: {
                "path": path,
                "title": "Page",
                "mimetype": "text/html",
                "content": body,
            },
            create=True,
        ):
            return self.client.get("/article/manual/page")

    def test_raw_article_gains_mobile_fallback_without_viewport(self):
        raw = self._raw_article(b"<html><head></head><body>Desktop</body></html>")
        self.assertIn('name="viewport"', raw.text)
        self.assertIn("data-zimantic-mobile", raw.text)
        self.assertIn("max-width: 800px", raw.text)
        self.assertIn("data-zimantic-bridge", raw.text)

    def test_raw_article_leaves_mobile_aware_pages_alone(self):
        body = (
            b"<html><head>"
            b'<meta name="viewport" content="width=device-width">'
            b"</head><body>Already mobile</body></html>"
        )
        raw = self._raw_article(body)
        self.assertNotIn("data-zimantic-mobile", raw.text)
        self.assertNotIn('content="width=device-width, initial-scale=1"', raw.text)
        # The navigation bridge is still added.
        self.assertIn("data-zimantic-bridge", raw.text)

    def test_raw_article_enables_known_mobile_layout_only_when_narrow(self):
        hint = self._raw_article(
            b'<html class="_theme-default"><head></head>'
            b"<body><devdocs-navbar></devdocs-navbar></body></html>"
        )
        self.assertIn('["_mobile"]', hint.text)
        self.assertIn("(max-width: 800px)", hint.text)

        plain = self._raw_article(b"<html><head></head><body>Other</body></html>")
        self.assertNotIn("_mobile", plain.text)

    def test_content_shell_uses_article_title_and_icon(self):
        body = (
            b"<html><head><title>Deep &amp; Nice</title>"
            b'<link rel="shortcut icon" href="./favicon.ico"></head>'
            b"<body>Article body</body></html>"
        )
        with patch.object(
            self.search,
            "read_article",
            lambda zim, path: {
                "path": path,
                "title": "Entry title",
                "mimetype": "text/html",
                "content": body,
            },
            create=True,
        ):
            shell = self.client.get("/content/manual/docs/page")

        self.assertEqual(shell.status_code, 200)
        self.assertIn("<title>Deep &amp; Nice</title>", shell.text)
        # The article's relative icon resolves through the raw /article/ route.
        self.assertIn(
            '<link rel="icon" data-zimantic-article-icon '
            'href="/article/manual/docs/favicon.ico">',
            shell.text,
        )
        # The shell forwards its title/icon so the modal can mirror them.
        self.assertIn('title: document.title', shell.text)
        self.assertIn('link[data-zimantic-article-icon]', shell.text)

    def test_content_shell_falls_back_to_zim_source_icon(self):
        body = b"<html><head></head><body>No icon declared</body></html>"
        with patch.object(
            self.search,
            "read_article",
            lambda zim, path: {
                "path": path,
                "title": "Page",
                "mimetype": "text/html",
                "content": body,
            },
            create=True,
        ), patch.object(
            self.search,
            "source_dicts",
            lambda: [{"local_name": "manual", "icon_url": "/source-icon/manual"}],
        ):
            shell = self.client.get("/content/manual/page")

        self.assertEqual(shell.status_code, 200)
        self.assertIn("<title>Page</title>", shell.text)
        self.assertIn(
            '<link rel="icon" data-zimantic-article-icon href="/source-icon/manual">',
            shell.text,
        )

    def test_content_shell_hides_its_own_marker_script(self):
        # The toolbar style, script and nav all carry data-zimantic-toolbar.
        # The layout rule must target the nav only, and the script must stay
        # display:none so its source never shows up as visible page text.
        body = b"<html><head></head><body>Article</body></html>"
        with patch.object(
            self.search,
            "read_article",
            lambda zim, path: {
                "path": path,
                "title": "Page",
                "mimetype": "text/html",
                "content": body,
            },
            create=True,
        ):
            shell = self.client.get("/content/manual/page")
        self.assertEqual(shell.status_code, 200)
        self.assertIn("nav[data-zimantic-toolbar] {", shell.text)
        self.assertIn(
            "script[data-zimantic-toolbar] { display: none !important; }",
            shell.text,
        )
        self.assertNotIn("\n[data-zimantic-toolbar] {", shell.text)

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
        self.assertEqual(
            response.headers["x-matched-by"],
            '{"semantic":1,"title":2,"fulltext":3}',
        )

        stream = self.debug_client.get("/search/stream?q=page")
        events = [json.loads(line) for line in stream.text.splitlines()]
        self.assertEqual(events[-1]["results"][0]["score"], 1.0)
        self.assertEqual(
            events[-1]["matched_by"],
            {"semantic": 1, "title": 2, "fulltext": 3},
        )
        self.assertTrue(self.debug_search.stream_calls[-1][-1])

    def test_config_reports_debug_mode(self):
        self.assertFalse(self.client.get("/config").json()["debug"])
        self.assertTrue(self.debug_client.get("/config").json()["debug"])

    @patch("zimantic.web.app.os.execv")
    @patch("zimantic.web.app.sys.argv", ["zimantic", "serve", "--debug"])
    @patch("zimantic.web.app.sys.executable", "/venv/bin/python")
    def test_restart_handler_reexecutes_the_server_command(self, execv):
        from zimantic.web.app import _handle_restart_request

        _handle_restart_request(1, None)

        execv.assert_called_once_with(
            "/venv/bin/python",
            ["/venv/bin/python", "-m", "zimantic", "serve", "--debug"],
        )

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
        self.assertEqual(response.headers["content-encoding"], "identity")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
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