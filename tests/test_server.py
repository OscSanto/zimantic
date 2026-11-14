import json
import unittest
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from zimantic.cache import QueryCache
from zimantic.content.store import (
    ArticleNotFoundError,
    ArticleSourceNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
)
from zimantic.search import Search, SearchQueryError
from zimantic.web.app import _article_http_error, create_app


class FakeSearch:
    cfg: dict
    read_article: Callable[..., dict]
    semantic = True

    def __init__(self):
        self.cache = QueryCache(4)
        self.search_page_calls = []
        self.suggest_calls = []
        self.stream_calls = []
        self.sort_calls = []
        self.refresh_calls = 0

    def local_names(self):
        return ["manual"]

    def source_keys(self):
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
            "provider": "zim",
            "title": "Manual",
            "collection": {"key": "zim:library", "label": "Library", "provider": "zim"},
            "searchable": True,
            "homepage": "Main_Page",
            "description": "Manual description",
            "media_types": ["text"],
            "language": "eng",
            "provider_id": "00000000-0000-0000-0000-000000000001",
        }]

    def collection_dicts(self):
        return [{
            "key": "zim:library",
            "provider": "zim",
            "label": "Library",
            "title": "Library",
            "description": None,
            "source_count": 1,
            "media_types": ["text"],
            "error": None,
            "discovery_state": "ready",
            "snapshot_version": 1,
            "icon_url": None,
        }]

    def collection_version(self):
        return ("test-process", 1)

    def listing_snapshot(self):
        sources = self.source_dicts()
        return {
            "sources": sources,
            "library": sources,
            "collections": self.collection_dicts(),
            "token": self.collection_version(),
        }

    def refresh_sources(self):
        self.refresh_calls += 1
        return self.source_dicts()

    def random_local_source(self):
        from zimantic.contracts import SourceInfo

        return SourceInfo(
            key="manual",
            provider="zim",
            title="Manual",
            searchable=True,
            homepage="Main_Page",
        )

    @staticmethod
    def _validate_query(query, *, allow_empty=False):
        return Search._validate_query(query, allow_empty=allow_empty)

    def _validate_search(self, query, sort=None, seed=0):
        return Search._validate_search(self, query, sort, seed)

    def _validate_browse(self, mode, seed=0):
        Search._validate_browse(self, mode, seed)

    def suggest(self, query, source, limit, collection):
        self.suggest_calls.append((query, source, limit, collection))
        return [{"id": "manual:page", "title": "Page"}]

    def search_page(
        self,
        query,
        source,
        limit,
        offset,
        collection,
        debug,
        *,
        browse=None,
        seed=0,
        sort=None,
    ):
        if browse is not None:
            self._validate_browse(browse, seed)
        self.sort_calls.append((sort, seed))
        self.search_page_calls.append(
            (query, source, limit, offset, collection, debug)
        )
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

    def stream_search(
        self,
        query,
        source=None,
        limit=None,
        offset=0,
        collection=None,
        debug=False,
        *,
        browse=None,
        seed=0,
        sort=None,
    ):
        if browse is not None:
            self._validate_browse(browse, seed)
        self.sort_calls.append((sort, seed))
        self.stream_calls.append(
            (query, source, limit, offset, collection, debug)
        )
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


def test_inline_script_json_cannot_close_the_script_element():
    from zimantic.web.app import _json_for_script

    assert "</script>" not in _json_for_script("</script>")


class ServerTests(unittest.TestCase):
    def test_get_routes_accept_head_requests(self):
        for path in ("/health", "/sources", "/collections.json", "/config"):
            with self.subTest(path=path):
                response = self.client.head(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"")

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
        self.assertEqual(self.client.get("/library.json").json()[0]["title"], "Manual")
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
        self.assertEqual(config["collection_source_limit"], 1000)
        self.assertEqual(config["max_explicit_collection_sources"], 500)
        self.assertEqual(config["providers"], ["zim", "kiwix", "files", "kolibri"])
        self.assertEqual(
            config["library_filters"],
            {
                "providers": ["zim"],
                "media_types": ["text"],
                "languages": ["eng"],
            },
        )
        self.assertIn("the", config["stopwords"])
        self.assertGreater(config["max_query_length"], 0)

    def test_config_library_filters_follow_discovery_changes(self):
        manual = self.search.source_dicts()[0]
        kolibri = {
            **manual,
            "key": "kolibri:channel",
            "provider": "kolibri",
            "title": "Channel",
            "language": "spa",
            "media_types": ["video"],
            "collection": {
                "key": "kolibri:channel",
                "label": "Channel",
                "provider": "kolibri",
            },
        }
        with (
            patch.object(
                self.search, "source_dicts", lambda: [manual, kolibri]
            ),
            patch.object(
                self.search, "collection_version", lambda: ("test-process", 2)
            ),
        ):
            config = self.client.get("/config").json()
        self.assertEqual(
            config["library_filters"]["providers"], ["zim", "kolibri"]
        )
        self.assertEqual(
            config["library_filters"]["media_types"], ["text", "video"]
        )
        self.assertEqual(config["library_filters"]["languages"], ["eng", "spa"])

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

    def test_library_supports_thousand_source_pages_with_filtered_totals(self):
        class LargeLibrarySearch(FakeSearch):
            def source_dicts(self):
                template = super().source_dicts()[0]
                return [
                    {**template, "key": f"source-{index:04d}", "title": f"Source {index:04d}"}
                    for index in range(1001)
                ] + [{**template, "key": "video", "media_types": ["video"]}]

        with TestClient(create_app(LargeLibrarySearch())) as client:
            params = {"limit": 1000, "collection": "zim:library", "media_type": "text"}
            first = client.get("/library.json", params=params)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(len(first.json()), 1000)
            self.assertEqual(first.headers["x-total-count"], "1001")
            self.assertEqual(first.headers["x-has-more"], "true")
            more = client.get("/library.json", params={**params, "offset": 1000})
            self.assertEqual(more.status_code, 200)
            self.assertEqual([record["key"] for record in more.json()], ["source-1000"])
            self.assertEqual(more.headers["x-has-more"], "false")
            self.assertEqual(
                first.headers["x-snapshot-version"], more.headers["x-snapshot-version"]
            )
            self.assertEqual(
                client.get("/library.json", params={"limit": 1001}).status_code, 422
            )

    def test_library_counts_and_paginates_only_nonempty_kolibri_cards(self):
        snapshot = self.search.listing_snapshot()
        collection = {
            "key": "kolibri:khan",
            "provider": "kolibri",
            "label": "Khan Academy",
            "source_count": 8,
        }
        snapshot["collections"].append(collection)
        snapshot["library"] = [
            {**snapshot["library"][0], "item_count": 0},
            *[
                {
                    "key": f"empty-{index}",
                    "title": f"Empty {index}",
                    "provider": "kolibri",
                    "collection": collection,
                    "item_count": 0,
                    "searchable": False,
                }
                for index in range(7)
            ],
            {
                "key": "math",
                "title": "Math",
                "provider": "kolibri",
                "collection": collection,
                "item_count": 2,
                "searchable": False,
            },
        ]
        # Hidden Kolibri topics are absent from the searchable source listing in
        # production; keep only the non-empty source there so the count has to
        # come from the full library snapshot.
        snapshot["sources"] = [snapshot["library"][-1]]
        with patch.object(self.search, "listing_snapshot", return_value=snapshot):
            collections = self.client.get(
                "/collections.json", params={"provider": "kolibri"}
            )
            self.assertEqual(collections.status_code, 200)
            self.assertEqual(collections.json()[0]["source_count"], 1)
            sources = self.client.get(
                "/sources", params={"provider": "kolibri"}
            )
            self.assertEqual(sources.status_code, 200)
            self.assertEqual([source["key"] for source in sources.json()], ["math"])
            self.assertEqual(sources.headers["x-total-count"], "1")
            response = self.client.get(
                "/library.json", params={"provider": "kolibri", "limit": 1}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual([source["key"] for source in response.json()], ["math"])
            self.assertEqual(response.headers["x-total-count"], "1")
            self.assertEqual(response.headers["x-has-more"], "false")
            filtered = self.client.get(
                "/library.json", params={"provider": "kolibri", "q": "Empty"}
            )
            self.assertEqual(filtered.json(), [])
            self.assertEqual(filtered.headers["x-total-count"], "0")
            other = self.client.get("/library.json", params={"provider": "zim"})
            self.assertEqual([source["key"] for source in other.json()], ["manual"])
            snapshot["library"][1]["item_count"] = None
            unknown = self.client.get(
                "/library.json", params={"provider": "kolibri", "limit": 1}
            )
            self.assertEqual([source["key"] for source in unknown.json()], ["empty-0"])
            self.assertEqual(unknown.headers["x-total-count"], "2")
            self.assertEqual(unknown.headers["x-has-more"], "true")
            more = self.client.get(
                "/library.json", params={"provider": "kolibri", "limit": 1, "offset": 1}
            )
            self.assertEqual([source["key"] for source in more.json()], ["math"])
            self.assertEqual(more.headers["x-has-more"], "false")
            collections = self.client.get(
                "/collections.json", params={"provider": "kolibri"}
            )
            self.assertEqual(collections.json()[0]["source_count"], 2)
        self.assertEqual(collection["source_count"], 8)
        self.assertEqual(len(snapshot["library"]), 9)
        self.assertEqual(len(snapshot["sources"]), 1)

    def test_library_filters_zim_sources_by_count_based_media_types(self):
        from types import SimpleNamespace

        from zimantic.providers.zim.metadata import source_info_from_record

        class MediaLibrarySearch(FakeSearch):
            def source_dicts(self):
                sources = []
                for title, counter in (
                    ("Manual", "text/html=322;video/mp4=2"),
                    ("TED", "text/html=25;video/webm=24;text/vtt=544"),
                ):
                    record = SimpleNamespace(
                        key=title,
                        provider_metadata="{}",
                        icon_data=None,
                        icon_path="",
                        to_source_metadata=lambda title=title, counter=counter: {
                            "title": title,
                            "mime_counts": counter,
                        },
                    )
                    source = source_info_from_record(
                        record,
                        collection_key="zim:library",
                        collection_label="Library",
                        icon_url=None,
                        searchable=True,
                    )
                    sources.append(source.to_dict(include_metadata=True))
                return sources

        with TestClient(create_app(MediaLibrarySearch())) as client:
            text = client.get("/library.json", params={"media_type": "text"})
            video = client.get("/library.json", params={"media_type": "video"})
            self.assertEqual([source["title"] for source in text.json()], ["Manual"])
            self.assertEqual([source["title"] for source in video.json()], ["TED"])
            self.assertEqual(text.headers["x-total-count"], "1")
            self.assertEqual(video.headers["x-total-count"], "1")

    def test_collections_endpoint_is_paged_and_reports_snapshot(self):
        response = self.client.get("/collections.json", params={"limit": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-total-count"], "1")
        self.assertEqual(response.headers["x-has-more"], "false")
        self.assertEqual(response.headers["x-snapshot-version"], "test-process:1")
        self.assertEqual(response.json()[0]["key"], "zim:library")
        self.assertEqual(response.json()[0]["media_types"], ["text"])

        cached = self.client.get(
            "/collections.json",
            params={"limit": 1},
            headers={"If-None-Match": response.headers["etag"]},
        )
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.headers["x-total-count"], "1")
        self.assertEqual(cached.headers["x-snapshot-version"], "test-process:1")

    def test_listing_filters_compose_before_paging(self):
        sources = self.client.get(
            "/sources",
            params={
                "provider": "zim",
                "media_type": "text",
                "language": "ENG",
                "collection": "zim:library",
                "q": "manual",
            },
        )
        self.assertEqual(sources.status_code, 200)
        self.assertEqual([item["key"] for item in sources.json()], ["manual"])

        collections = self.client.get(
            "/collections.json",
            params={
                "provider": "zim",
                "media_type": "text",
                "language": "eng",
                "q": "library",
            },
        )
        self.assertEqual(collections.status_code, 200)
        self.assertEqual([item["key"] for item in collections.json()], ["zim:library"])
        unmatched = self.client.get("/library.json", params={"language": "fra"})
        self.assertEqual(unmatched.status_code, 200)
        self.assertEqual(unmatched.json(), [])

        # Exclusion terms compose on the server instead of reaching it as a
        # literal substring that empties the page before the browser can filter.
        excluded = self.client.get("/sources", params={"q": "-manual"})
        self.assertEqual(excluded.status_code, 200)
        self.assertEqual(excluded.json(), [])
        excluded_collections = self.client.get(
            "/collections.json", params={"q": "-library"}
        )
        self.assertEqual(excluded_collections.json(), [])
        # Terms match at a word boundary, matching the browser's filter.
        prefixed = self.client.get("/collections.json", params={"q": "libr"})
        self.assertEqual([item["key"] for item in prefixed.json()], ["zim:library"])
        self.assertEqual(
            self.client.get("/collections.json", params={"q": "ibrary"}).json(),
            [],
        )

    def test_listing_filters_reject_unknown_values(self):
        for path, params in (
            ("/sources", {"provider": "unknown"}),
            ("/sources", {"media_type": "hologram"}),
            ("/sources", {"collection": "files:missing"}),
            ("/library.json", {"provider": "unknown"}),
            ("/collections.json", {"media_type": "hologram"}),
        ):
            with self.subTest(path=path, params=params):
                self.assertEqual(
                    self.client.get(path, params=params).status_code,
                    400,
                )

    def test_page_assets_are_served(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="app.css?v=', page.text)
        self.assertIn('src="app.js?v=', page.text)
        self.assertIn('src="i18n.js?v=', page.text)
        self.assertEqual(page.headers["cache-control"], "no-cache")
        self.assertNotIn('id="libraryRandom"', page.text)
        self.assertIn('id="articleModalFrame"', page.text)
        # The modal shell itself is trusted; the article document inside it is
        # sandboxed only when secure_browsing is enabled.
        self.assertNotIn('sandbox="allow-scripts allow-popups', page.text)

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

        i18n = self.client.get("/i18n.js")
        self.assertEqual(i18n.status_code, 200)
        self.assertTrue(i18n.headers["content-type"].startswith("application/javascript"))
        self.assertIn("data-i18n", i18n.text)

        shim = self.client.get("/indexeddb-shim.js")
        self.assertEqual(shim.status_code, 200)
        self.assertTrue(shim.headers["content-type"].startswith("application/javascript"))
        self.assertIn("fake-indexeddb", shim.text)
        self.assertIn("zimantic-article-storage-blocked", shim.text)

    def test_versioned_static_assets_are_immutable_and_precompressed(self):
        page = self.client.get("/")
        version = page.text.split('src="app.js?v=')[1].split('"', 1)[0]
        response = self.client.get(
            f"/app.js?v={version}",
            headers={"Accept-Encoding": "gzip"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertIn("immutable", response.headers["cache-control"])
        self.assertIn("initialize();", response.text)
        etag = response.headers["etag"]
        conditional = self.client.get(
            f"/app.js?v={version}",
            headers={"If-None-Match": etag},
        )
        self.assertEqual(conditional.status_code, 304)
        self.assertEqual(conditional.headers["etag"], etag)

    def test_unversioned_static_assets_are_revalidated(self):
        response = self.client.get("/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-cache")

    def test_debug_page_assets_are_not_cached(self):
        for path in (
            "/",
            "/library",
            "/app.css",
            "/app.js",
            "/i18n.js",
            "/indexeddb-shim.js",
        ):
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
            {"key": "manual", "provider": "zim", "icon_url": "/zimantic/source-icon/manual"}
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

        response = self.client.get("/favicon.svg")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("image/svg+xml"))
        self.assertIn(b"<svg", response.content)

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

    def test_source_text_is_displayed_inline_without_executing_markup(self):
        body = b'#!/bin/sh\nprintf "<script>alert(1)</script>\\n"\n'
        for mimetype in (
            "application/x-sh",
            "application/x-csh",
            "text/x-shellscript",
            "text/x-python; charset=utf-8",
        ):
            with self.subTest(mimetype=mimetype), patch.object(
                self.search,
                "read_article",
                lambda zim, path, mimetype=mimetype: {
                    "path": path,
                    "title": "finish_install.sh",
                    "mimetype": mimetype,
                    "content": body,
                },
                create=True,
            ):
                shell = self.client.get("/content/manual/en-file_share/finish_install.sh")
                raw = self.client.get("/article/manual/en-file_share/finish_install.sh")
                head = self.client.head("/article/manual/en-file_share/finish_install.sh")

                self.assertEqual(shell.status_code, 200)
                self.assertIn(
                    'src="/article/manual/en-file_share/finish_install.sh"', shell.text
                )
                self.assertNotIn('<div class="zimantic-shell-attachment"', shell.text)
                self.assertEqual(raw.content, body)
                self.assertTrue(raw.headers["content-type"].startswith("text/plain"))
                self.assertEqual(head.headers["content-type"], raw.headers["content-type"])
                self.assertNotIn("content-disposition", raw.headers)
                self.assertEqual(raw.headers["x-content-type-options"], "nosniff")

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
        self.assertIn(
            '<a class="zimantic-toolbar-article-full" data-zimantic-raw-article '
            'href="/article/manual/page">Page</a>',
            shell.text,
        )
        self.assertIn('src="/article/manual/page"', shell.text)
        self.assertIn("zimantic-external-link", shell.text)
        self.assertIn("data-zimantic-zim-search", shell.text)
        self.assertIn('data-zimantic-zim="manual"', shell.text)
        self.assertIn("data-zimantic-zim-suggestions", shell.text)
        self.assertNotIn("Article body", shell.text)

        # /article/ serves the raw article and only reports link clicks; the
        # shell owns all URL classification and no toolbar appears here.
        self.assertEqual(raw.status_code, 200)
        self.assertTrue(raw.headers["content-type"].startswith("text/html"))
        self.assertEqual(raw.headers["access-control-allow-origin"], "*")
        self.assertIn("Article body", raw.text)
        self.assertIn("data-zimantic-bridge", raw.text)
        self.assertIn("zimantic-article-link", raw.text)
        self.assertIn("anchor.getAttribute(\"href\")", raw.text)
        self.assertIn("location.replace(shell.href)", raw.text)
        self.assertNotIn("offlineUrlFor", raw.text)
        self.assertNotIn("contentPathFor", raw.text)
        self.assertNotIn("zimantic-external-link", raw.text)
        self.assertIn("zimantic-article-hash", raw.text)
        self.assertNotIn("data-zimantic-toolbar", raw.text)

        # The shell resolves the authored href, maps captured-host URLs to the
        # offline copy, and sends everything else to the external-link notice.
        self.assertIn("zimantic-article-link", shell.text)
        self.assertIn("const secureBrowsing = false;", shell.text)
        self.assertNotIn('sandbox="allow-scripts allow-popups', shell.text)
        self.assertIn('referrerpolicy="same-origin"', shell.text)
        self.assertIn("const offlineUrlFor = (url, route) =>", shell.text)
        self.assertIn("const contentPathFor = url =>", shell.text)
        self.assertIn("const openOfflineArticle = (target, fallbackUrl) =>", shell.text)
        self.assertIn("target.origin !== shellUrl.origin", shell.text)
        self.assertIn("offlineUrl,", shell.text)
        self.assertIn('new URL(articleFrame.getAttribute("src")', shell.text)
        self.assertIn("zimantic-app-navigate", shell.text)
        self.assertIn("if (location.hash) scrollArticleTo(location.hash)", shell.text)
        self.assertIn("zimantic-article-hash", shell.text)

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

    def test_content_external_url_uses_top_level_redirect_bridge(self):
        entry = SimpleNamespace(
            path="node/topic",
            title="Topic",
            mimetype="text/html",
            size=None,
            redirect_path=None,
            external_url=(
                "http://kolibri.example/learn/#/topics/t/topic/folders"
            ),
        )
        with patch.object(
            self.search,
            "resolve_content",
            lambda source, path=None: entry,
            create=True,
        ):
            response = self.client.get("/content/manual/node/topic")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertNotIn("data-zimantic-toolbar", response.text)
        self.assertIn("zimantic-external-redirect", response.text)
        self.assertIn("window.top === window", response.text)
        self.assertIn(
            "http://kolibri.example/learn/#/topics/t/topic/folders",
            response.text,
        )
        self.assertIn('target="_top"', response.text)

    def test_content_shell_hides_zim_search_when_scope_does_not_resolve(self):
        body = b"<html><head></head><body>Page</body></html>"
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
        ), patch.object(self.search, "source_dicts", list):
            shell = self.client.get("/content/manual/page")

        self.assertEqual(shell.status_code, 200)
        self.assertNotIn('<form class="zimantic-toolbar-zim-search"', shell.text)
        self.assertNotIn('data-zimantic-zim="manual"', shell.text)
        self.assertNotIn("id=\"zimanticZimSuggestions\"", shell.text)
        self.assertIn('<a class="zimantic-toolbar-search"', shell.text)

    def test_content_shell_shows_zim_search_for_resolvable_key(self):
        body = b"<html><head></head><body>Page</body></html>"
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
            lambda: [{
                "key": "manual",
                "provider": "zim",
                "title": "manual_2025-10",
                "searchable": True,
            }],
        ):
            shell = self.client.get("/content/manual/page")

        self.assertEqual(shell.status_code, 200)
        self.assertIn('<form class="zimantic-toolbar-zim-search"', shell.text)
        self.assertIn('data-zimantic-zim="manual"', shell.text)
        self.assertNotIn('<a class="zimantic-toolbar-search"', shell.text)

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
            'title="Open the homepage of based.cooking"',
            shell.text,
        )
        # The captured domain is the home label, not a second breadcrumb.
        self.assertNotIn(f'href="/content/{zim}/based.cooking/"', shell.text)
        self.assertIn(f'href="/content/{zim}/based.cooking/recipe/"', shell.text)

    def test_toolbar_uses_source_title_and_provider_specific_search_label(self):
        article = lambda _zim, path: {
            "path": path,
            "title": "Page",
            "mimetype": "text/html",
            "content": b"<html><head></head><body>Page</body></html>",
        }
        with patch.object(self.search, "read_article", article, create=True):
            zim_shell = self.client.get("/content/manual/page")
            self.assertIn(
                '<span class="zimantic-toolbar-home-full">Manual</span>',
                zim_shell.text,
            )
            self.assertIn(
                'title="Open the homepage of Manual"',
                zim_shell.text,
            )
            self.assertIn('placeholder="Search this ZIM"', zim_shell.text)

            files_sources = [{
                "key": "manual",
                "provider": "files",
                "title": "Manual files",
                "searchable": True,
            }]
            with patch.object(
                self.search, "source_dicts", return_value=files_sources
            ):
                files_shell = self.client.get("/content/manual/page")
        self.assertIn(
            '<span class="zimantic-toolbar-home-full">Manual files</span>',
            files_shell.text,
        )
        self.assertIn(
            'placeholder="Search this source"',
            files_shell.text,
        )

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

    def test_missing_mathjax_font_uses_vendored_copy(self):
        search = FakeSearch()

        def missing(_zim, _path):
            raise ArticleNotFoundError("missing")

        search.read_article = missing
        client = TestClient(create_app(search))

        response = client.get(
            "/article/manual/static/js/fonts/HTML-CSS/TeX/woff/"
            "MathJax_Main-Regular.woff"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("font/woff"))
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertGreater(len(response.content), 1000)

        absent = client.get("/article/manual/missing.png")
        self.assertEqual(absent.status_code, 404)
        self.assertEqual(absent.headers["access-control-allow-origin"], "*")

    def test_root_absolute_capture_asset_uses_referer_zim(self):
        search = FakeSearch()

        def read_article(_zim, path):
            if path == "-/mw/banner.jpg":
                return {
                    "path": path,
                    "title": "Banner",
                    "mimetype": "image/png",
                    "content": b"banner",
                }
            raise ArticleNotFoundError("missing")

        search.read_article = read_article
        client = TestClient(create_app(search))

        response = client.get(
            "/w/images/5/54/banner.jpg",
            headers={"referer": "http://testserver/article/manual/page"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"banner")
        self.assertEqual(response.headers["access-control-allow-origin"], "*")

        no_referer = client.get("/w/images/5/54/banner.jpg")
        self.assertEqual(no_referer.status_code, 404)

        unknown = client.get(
            "/w/images/5/54/other.png",
            headers={"referer": "http://testserver/article/manual/page"},
        )
        self.assertEqual(unknown.status_code, 404)

    def test_secure_browsing_sandboxes_article_documents(self):
        search = FakeSearch()
        search.cfg = {"secure_browsing": True}
        search.read_article = lambda zim, path: {
            "path": path,
            "title": "Page",
            "mimetype": "text/html",
            "content": b"<html><head></head><body>Page</body></html>",
        }
        client = TestClient(create_app(search))

        shell = client.get("/content/manual/page")

        self.assertEqual(shell.status_code, 200)
        self.assertIn(
            'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"',
            shell.text,
        )
        self.assertIn("const secureBrowsing = true;", shell.text)
        self.assertIn('referrerpolicy="same-origin"', shell.text)

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
            lambda: [{"key": "manual", "provider": "zim", "icon_url": "/source-icon/manual"}],
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

    def test_suggest_uses_the_suggestion_api(self):
        search_calls = len(self.search.search_page_calls)
        response = self.client.get("/suggest?q=page&source=manual&limit=5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["title"], "Page")
        self.assertEqual(
            self.search.suggest_calls[-1], ("page", ["manual"], 5, None)
        )
        self.assertEqual(len(self.search.search_page_calls), search_calls)

    def test_search_accepts_page_and_filter_parameters(self):
        response = self.client.get("/search?q=page&offset=10&source=manual&limit=5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-offset"], "10")
        self.assertEqual(response.headers["x-page"], "2")
        self.assertEqual(response.headers["x-page-count"], "1")
        self.assertNotIn("score", response.json()[0])
        self.assertEqual(
            self.search.search_page_calls[-1],
            ("page", "manual", 5, 10, None, False),
        )

    def test_removed_browse_routes_return_not_found(self):
        for path in ("/browse", "/browse/stream"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_search_sorting_and_empty_queries_work_for_json_and_streams(self):
        for path in ("/search", "/search/stream"):
            with self.subTest(path=path):
                response = self.client.get(path + "?q=&source=manual&offset=10")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.search.sort_calls[-1], ("diverse", 0))
                response = self.client.get(path + "?q=topic&sort=random&seed=7")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.search.sort_calls[-1], ("random", 7))
                response = self.client.get(path + "?q=topic&sort=diverse")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.search.sort_calls[-1], ("diverse", 0))
                for removed in ("representative", "semantic-diverse"):
                    self.assertEqual(self.client.get(path + "?q=topic&sort=" + removed).status_code, 422)
                self.assertEqual(self.client.get(path + "?q=topic&sort=unknown").status_code, 422)
                self.assertEqual(self.client.get(path + "?q=&seed=-1").status_code, 422)

    def test_fast_mode_rejects_semantic_browsing_and_hides_its_controls(self):
        search = FakeSearch()
        search.semantic = False
        with TestClient(create_app(search)) as client:
            self.assertEqual(
                client.get("/config").json()["browse_modes"],
                ["random", "rare", "diverse", "recency"],
            )
            for path in ("/search", "/search/stream"):
                self.assertEqual(client.get(path + "?q=").status_code, 200)
                self.assertEqual(search.sort_calls[-1], ("diverse", 0))
                self.assertEqual(
                    client.get(path + "?q=topic&sort=diverse").status_code, 200
                )

    def test_search_accepts_multiple_source_filters(self):
        response = self.client.get("/search?q=page&source=manual&source=other")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search.search_page_calls[-1][1], ["manual", "other"])

    def test_stream_search_accepts_source_and_collection_filters(self):
        stream = self.client.get(
            "/search/stream?q=page&source=manual&source=other&collection=zim:library"
        )
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(
            self.search.stream_calls[-1],
            ("page", ["manual", "other"], None, 0, "zim:library", False),
        )

    def test_search_accepts_a_collection_filter(self):
        response = self.client.get("/search?q=page&collection=zim:library")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.search.search_page_calls[-1],
            ("page", None, None, 0, "zim:library", False),
        )

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
        self.assertTrue(self.debug_search.stream_calls[-1][5])

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
        self.assertEqual(second.headers["x-snapshot-version"], "test-process:1")
        self.assertEqual(second.headers["x-total-count"], "1")

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
        self.assertEqual(self.client.get("/search/stream?q=%20").status_code, 200)
        self.assertEqual(self.client.get("/search/stream", params={"q": "x" * 4097}).status_code, 422)

    def test_streaming_search_rejects_unknown_scope_before_starting(self):
        class _ValidatingSearch(FakeSearch):
            @staticmethod
            def validate_search_scope(source, collection=None):
                if collection == "missing":
                    raise SearchQueryError("unknown collection: missing")

        client = TestClient(create_app(_ValidatingSearch()))
        response = client.get("/search/stream?q=page&collection=missing")
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown collection", response.json()["detail"])

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
        self.assertIn('aria-label="Adjacent search results"', response.text)

    def test_health_reports_indexes_and_cache(self):
        body = self.client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["indexes"], ["manual"])
        self.assertIn("size", body["cache"])

    def test_health_reports_worker_activity_only_in_debug(self):
        class WorkerSearch(FakeSearch):
            def worker_status(self):
                return {
                    "fulltext": {"worker_count": 0, "workers": []},
                    "source_search": {"worker_count": 8, "active": []},
                }

        client = TestClient(create_app(WorkerSearch()))
        debug_client = TestClient(create_app(WorkerSearch(), debug=True))
        self.assertNotIn("workers", client.get("/health").json())
        self.assertEqual(
            debug_client.get("/health").json()["workers"]["source_search"],
            {"worker_count": 8, "active": []},
        )

    def test_health_debug_merges_workers_into_collected_report(self):
        class WorkerHealthSearch(FakeSearch):
            def health(self):
                return {"status": "ok", "cache": self.cache.stats()}

            def health_version(self):
                return 1

            def worker_status(self):
                return {"fulltext": {"worker_count": 1, "workers": []}}

        client = TestClient(create_app(WorkerHealthSearch()))
        debug_client = TestClient(create_app(WorkerHealthSearch(), debug=True))
        self.assertNotIn("workers", client.get("/health").json())
        body = debug_client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(
            body["workers"], {"fulltext": {"worker_count": 1, "workers": []}}
        )


if __name__ == "__main__":
    unittest.main()