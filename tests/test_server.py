import json
import unittest
from collections.abc import Callable
from unittest.mock import patch

from fastapi.testclient import TestClient

from zimantic.cache import QueryCache
from zimantic.search import (
    ArticleNotFoundError,
    ArticleSourceNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
    SearchQueryError,
)
from zimantic.web.app import _article_http_error, create_app


class FakeSearch:
    cfg: dict
    read_article: Callable[..., dict]

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
        return [{
            "key": "manual",
            "name": "Manual",
            "mode": "local",
            "local_name": "manual",
            "available": True,
            "searchable": True,
            "homepage": "Main_Page",
            "description": "Manual description",
            "uuid": "00000000-0000-0000-0000-000000000001",
        }]

    def refresh_sources(self):
        self.refresh_calls += 1
        return self.source_dicts()

    def random_local_source(self):
        from zimantic.contracts import SourceInfo

        return SourceInfo(
            "manual",
            "Manual",
            "manual",
            "local",
            local_name="manual",
            homepage="Main_Page",
        )

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
            "page": offset // max(1, limit or 10),
            "page_count": 1,
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
            "page": offset // max(1, limit or 10),
            "page_count": 1,
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
        sources = self.client.get("/sources").json()
        self.assertEqual(sources[0]["key"], "manual")
        self.assertEqual(sources[0]["description"], "Manual description")
        self.assertEqual(self.client.get("/library.json").json()[0]["name"], "Manual")
        response = self.client.get("/search?q=page")
        self.assertEqual(response.json()[0]["title"], "Page")
        # Totals travel as headers so the body stays a plain list.
        self.assertEqual(response.headers["x-total-count"], "1")
        self.assertEqual(response.headers["x-has-more"], "false")
        self.assertNotIn("x-page-size", response.headers)

    def test_zims_endpoint_is_gone(self):
        self.assertEqual(self.client.get("/zims").status_code, 404)

    def test_config_reports_page_and_pool_sizes(self):
        config = self.client.get("/config").json()
        self.assertEqual(config["page_size"], 10)
        self.assertEqual(config["max_results"], 100)
        self.assertEqual(config["source_filter_limit"], 10)
        self.assertIn("the", config["stopwords"])
        self.assertGreater(config["max_query_length"], 0)

    def test_random_without_zim_opens_a_random_local_source(self):
        response = self.client.get("/random", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/content/manual")

    def test_sources_supports_optional_pagination(self):
        response = self.client.get("/sources", params={"limit": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["key"], "manual")
        self.assertEqual(response.headers["x-total-count"], "1")
        self.assertEqual(response.headers["x-offset"], "0")
        self.assertEqual(response.headers["x-has-more"], "false")

    def test_pagination_clamps_an_out_of_range_offset(self):
        response = self.client.get("/sources", params={"limit": 1, "offset": 99})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-offset"], "0")
        self.assertEqual(response.json()[0]["key"], "manual")

    def test_page_assets_are_served(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="app.css"', page.text)
        self.assertIn('src="app.js"', page.text)
        self.assertNotIn('id="libraryRandom"', page.text)
        self.assertIn('id="articleModalFrame"', page.text)
        # Same-origin target="_blank" links (for example PDFs) must be able to
        # open outside the article modal's sandbox.
        self.assertIn(
            'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"',
            page.text,
        )

        css = self.client.get("/app.css")
        self.assertEqual(css.status_code, 200)
        self.assertTrue(css.headers["content-type"].startswith("text/css"))
        self.assertIn(":root", css.text)
        self.assertIn(".article-modal-frame", css.text)

        javascript = self.client.get("/app.js")
        self.assertEqual(javascript.status_code, 200)
        self.assertTrue(javascript.headers["content-type"].startswith("application/javascript"))
        self.assertIn("initialize();", javascript.text)
        self.assertIn("handleExternalLink", javascript.text)
        self.assertIn('credentials: "same-origin"', javascript.text)
        self.assertIn("zimantic-open-article", javascript.text)

        shim = self.client.get("/indexeddb-shim.js")
        self.assertEqual(shim.status_code, 200)
        self.assertTrue(shim.headers["content-type"].startswith("application/javascript"))
        self.assertIn("fake-indexeddb", shim.text)
        self.assertIn("zimantic-article-storage-blocked", shim.text)

    def test_debug_page_assets_are_not_cached(self):
        for path in ("/", "/library", "/app.css", "/app.js", "/indexeddb-shim.js"):
            with self.subTest(path=path):
                response = self.debug_client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_library_view_is_served(self):
        page = self.client.get("/library")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="library"', page.text)
        self.assertIn('id="libraryView"', page.text)

    def test_base_path_is_used_for_generated_article_urls(self):
        search = FakeSearch()
        search.cfg = {"web_base_path": "/zimantic"}
        search.read_article = lambda _zim, path: {
            "path": path,
            "title": "Page",
            "mimetype": "text/html",
            "content": b"<html><head><title>Page</title></head><body></body></html>",
        }
        search.source_dicts = lambda: [
            {"local_name": "manual", "icon_url": "/zimantic/source-icon/manual"}
        ]
        client = TestClient(create_app(search))
        response = client.get("/content/manual/page")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/zimantic/article/manual/page", response.text)
        self.assertIn("/zimantic/content/manual/page", response.text)
        self.assertIn('const appBasePath = "/zimantic";', response.text)
        self.assertIn('href="/zimantic/source-icon/manual"', response.text)

        raw = client.get("/article/manual/page")
        self.assertEqual(raw.status_code, 200)
        self.assertIn('const prefix = "/zimantic";', raw.text)
        self.assertIn('const url = "/zimantic/indexeddb-shim.js";', raw.text)

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
        self.assertIn("const href = hrefFor(anchor)", raw.text)
        self.assertIn("anchor.getAttribute(\"href\")", raw.text)
        self.assertIn("const contentPathFor = url =>", raw.text)
        self.assertIn("const currentContentUrl = () =>", raw.text)
        self.assertIn("offline.pathname === current.pathname", raw.text)
        self.assertIn("zimantic-article-hash", raw.text)
        self.assertIn("offlineUrl: offlineUrl ? offlineUrl.href : null", shell.text)
        self.assertIn("const offlineArticleUrl = value =>", shell.text)
        self.assertIn("const openOfflineArticle = (target, fallbackUrl) =>", shell.text)
        self.assertIn("if (location.hash) scrollArticleTo(location.hash)", shell.text)
        self.assertIn("zimantic-article-hash", shell.text)
        self.assertNotIn("data-zimantic-toolbar", raw.text)

        # The storage notice is part of the shell chrome, directly below the
        # toolbar and above the article iframe.
        self.assertIn("data-zimantic-storage-notice", shell.text)
        self.assertIn("zimantic-open-article", shell.text)
        self.assertLess(
            shell.text.index('data-zimantic-action="close"'),
            shell.text.index("data-zimantic-storage-notice-dismiss"),
        )
        self.assertLess(
            shell.text.index("data-zimantic-storage-notice-dismiss"),
            shell.text.index("<iframe data-zimantic-article"),
        )

    def test_content_shell_preserves_trailing_article_paths(self):
        self.search.read_article = lambda zim, path: {
            "path": "docs/home/",
            "title": "Home",
            "mimetype": "text/html",
            "content": b"<html><head></head><body>Home</body></html>",
        }
        try:
            response = self.client.get("/content/manual/docs/home/")
        finally:
            del self.search.read_article

        self.assertEqual(response.status_code, 200)
        self.assertIn('src="/article/manual/docs/home/"', response.text)
        self.assertIn('href="/content/manual/docs/home/"', response.text)

    def test_toolbar_home_shows_site_name_and_stem_on_hover(self):
        zim = "based.cooking_en_all_2025-10"
        with patch.object(
            self.search,
            "read_article",
            lambda _zim, path: {
                "path": path,
                "title": "Recipe",
                "mimetype": "text/html",
                "content": b"<html><head></head><body>Recipe</body></html>",
            },
            create=True,
        ):
            shell = self.client.get(f"/content/{zim}/based.cooking/recipe/")

        self.assertEqual(shell.status_code, 200)
        self.assertIn(
            '<span class="zimantic-toolbar-home-full">based.cooking</span>',
            shell.text,
        )
        self.assertIn(
            f'title="Open this ZIM\'s homepage ({zim})"',
            shell.text,
        )
        # The captured domain is the home label, not a second breadcrumb.
        self.assertNotIn(f'href="/content/{zim}/based.cooking/"', shell.text)
        self.assertIn(f'href="/content/{zim}/based.cooking/recipe/"', shell.text)

    def test_toolbar_drops_breadcrumb_for_missing_container(self):
        zim = "dandwiki_en_all_nopic_2025-04"

        def read_article(_zim, path):
            if path == "A":
                raise ArticleNotFoundError("article not found")
            return {
                "path": path,
                "title": "5e Hazards",
                "mimetype": "text/html",
                "content": b"<html><head></head><body>Hazards</body></html>",
            }

        with patch.object(self.search, "read_article", read_article, create=True):
            shell = self.client.get(f"/content/{zim}/A/5e_Hazards")

        self.assertEqual(shell.status_code, 200)
        self.assertIn(
            '<span class="zimantic-toolbar-home-full">dandwiki</span>',
            shell.text,
        )
        # "A" is a namespace bucket, not an article, so no broken link to it.
        self.assertNotIn(f'href="/content/{zim}/A"', shell.text)
        self.assertNotIn('title="Open A"', shell.text)

    def test_content_shell_forwards_shared_selection(self):
        body = b"<html><head></head><body>Hello world</body></html>"
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
            shell = self.client.get(
                "/content/manual/page",
                params={"highlight": "Hello world"},
            )

        self.assertEqual(shell.status_code, 200)
        self.assertIn(
            'src="/article/manual/page?highlight=Hello%20world"',
            shell.text,
        )
        self.assertIn(
            'href="/content/manual/page?highlight=Hello%20world"',
            shell.text,
        )
        self.assertIn("zimantic-article-selection", shell.text)

    def test_shared_selection_survives_article_redirects(self):
        self.search.read_article = lambda zim, path: {
            "path": "page",
            "title": "Page",
            "mimetype": "text/html",
            "content": b"<html><head></head><body>Hello</body></html>",
        }
        try:
            response = self.client.get(
                "/article/manual/page/",
                params={"highlight": "Hello world"},
                follow_redirects=False,
            )
        finally:
            del self.search.read_article

        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"],
            "/article/manual/page?highlight=Hello%20world",
        )

    def test_raw_article_assets_allow_sandboxed_fetches(self):
        search = FakeSearch()
        search.read_article = lambda zim, path: {
            "path": path,
            "title": "Shared data",
            "mimetype": "application/json",
            "content": b'{"pages":[]}',
        }
        client = TestClient(create_app(search))

        response = client.get("/article/manual/content/shared.json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"pages": []})
        self.assertEqual(response.headers["access-control-allow-origin"], "*")

    def test_raw_html_shims_cookie_access_in_opaque_sandboxes(self):
        raw = self._raw_article(b"<html><head></head><body>Article</body></html>")
        self.assertIn("data-zimantic-cookie-shim", raw.text)

    def test_raw_html_keeps_web_storage_usable_in_opaque_sandboxes(self):
        raw = self._raw_article(b"<html><head></head><body>Article</body></html>")
        self.assertIn("data-zimantic-storage-shim", raw.text)
        self.assertIn("makeMemoryStorage", raw.text)
        self.assertIn("zimantic-article-storage-blocked", raw.text)
        self.assertIn("data-zimantic-indexeddb-loader", raw.text)
        self.assertIn('const url = "/indexeddb-shim.js";', raw.text)

    def test_raw_html_installs_storage_shim_before_page_scripts(self):
        raw = self._raw_article(
            b'<html><head><script>localStorage.getItem("x")</script>'
            b"</head><body>Article</body></html>"
        )
        self.assertLess(
            raw.text.index("data-zimantic-storage-shim"),
            raw.text.index('localStorage.getItem("x")'),
        )
        self.assertLess(
            raw.text.index("data-zimantic-indexeddb-loader"),
            raw.text.index('localStorage.getItem("x")'),
        )

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
        self.assertIn('<main data-zimantic-shell-main>', shell.text)
        self.assertIn('class="zimantic-shell-heading">Deep &amp; Nice</h1>', shell.text)
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
        self.assertEqual(response.headers["x-page"], "2")
        self.assertEqual(response.headers["x-page-count"], "1")
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

    def test_article_error_types_map_by_subclass(self):
        self.assertEqual(
            _article_http_error(ArticleNotFoundError("x")).status_code, 404
        )
        self.assertEqual(
            _article_http_error(ArticleSourceNotFoundError("x")).status_code, 404
        )
        self.assertEqual(
            _article_http_error(ArticleTooLargeError("x")).status_code, 413
        )
        self.assertEqual(
            _article_http_error(ArticleUnavailableError("x")).status_code, 503
        )

    def test_missing_source_shell_says_source_not_found(self):
        class _MissingSourceSearch(FakeSearch):
            @staticmethod
            def read_article(zim, path=None):
                raise ArticleSourceNotFoundError("ZIM not found")

        client = TestClient(create_app(_MissingSourceSearch()))
        response = client.get("/content/missing")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Source not found", response.text)
        self.assertNotIn("This page is not available", response.text)
        self.assertIn('<main data-zimantic-shell-main>', response.text)
        self.assertIn("data-zimantic-search-home", response.text)
        self.assertIn('role="group" aria-label="Adjacent search results"', response.text)

    def test_health_reports_indexes_and_cache(self):
        body = self.client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["indexes"], ["manual"])
        self.assertIn("size", body["cache"])


if __name__ == "__main__":
    unittest.main()