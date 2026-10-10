import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from libzim.writer import Creator, Hint, Item, StringProvider

from zimantic import build as build_module
from zimantic.search import Search
from zimantic.server import create_app


ALPHA_HTML = (
    "<html><body><p>Alpha is a sufficiently long article about tires and "
    "wheels that passes the minimum block length for indexing.</p></body></html>"
)
BETA_HTML = (
    "<html><body><p>Beta is a sufficiently long article about brakes and "
    "rotors that passes the minimum block length for indexing.</p></body></html>"
)
AIR_HTML = (
    "<html><body><p>Air is a mixture of gases surrounding the planet and "
    "supporting life through breathing and atmospheric processes.</p></body></html>"
)
AIR_DISAMBIGUATION_HTML = (
    "<html><body>"
    "<p>Air may refer to several different things, including the atmosphere "
    "and various works, places, and organizations.</p>"
    "<p>This disambiguation page lists articles associated with the same title.</p>"
    "</body></html>"
)


class _Page(Item):
    def __init__(self, path: str, title: str, html: str):
        super().__init__()
        self._path = path
        self._title = title
        self._html = html.encode("utf-8")

    def get_path(self):
        return self._path

    def get_title(self):
        return self._title

    def get_mimetype(self):
        return "text/html"

    def get_contentprovider(self):
        return StringProvider(self._html)

    def get_hints(self):
        return {Hint.FRONT_ARTICLE: True}


def _create_zim(
    path: Path,
    pages: list[tuple[str, str, str]],
    redirects: list[tuple[str, str, str]] = (),
) -> None:
    creator = Creator(str(path))
    creator.config_indexing(True, "eng")
    with creator:
        for page_path, title, html in pages:
            creator.add_item(_Page(page_path, title, html))
        for title, page_path, target in redirects:
            creator.add_redirection(title, page_path, target, {})


def _config(root: Path, *, max_results: int = 100) -> dict:
    return {
        "index_dir": str(root / "indexes"),
        "zim_dir": str(root / "zims"),
        "kiwix_url": "http://kiwix/content",
        "candidate_count": 16,
        "page_size": 10,
        "max_results": max_results,
        "search_workers": 1,
    }


def _close_search(search: Search) -> None:
    for index in list(search.indexes.values()):
        search._close_index(index)
    search._executor.shutdown(wait=True)


class LocalIntegrationTests(unittest.TestCase):
    def _build_fixture(
        self,
        root: Path,
        *,
        pages: list[tuple[str, str, str]] | None = None,
        redirects: list[tuple[str, str, str]] = (),
        fast: bool = False,
        zim_name: str = "manual",
    ) -> Path:
        zims = root / "zims"
        indexes = root / "indexes"
        zims.mkdir(exist_ok=True)
        indexes.mkdir(exist_ok=True)
        zim_path = zims / f"{zim_name}.zim"
        _create_zim(
            zim_path,
            pages
            or [
                ("Alpha", "Alpha", ALPHA_HTML),
                ("Beta", "Beta", BETA_HTML),
            ],
            redirects,
        )
        build_module.build(
            zim_path,
            indexes,
            embedder=None,
            batch_size=2,
            fast=fast,
        )
        return zim_path

    def _open_search(self, root: Path, *, semantic: bool = False) -> Search:
        search = Search(_config(root), embedder=None, semantic=semantic)
        self.addCleanup(_close_search, search)
        return search

    def test_real_zim_build_search_and_http_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fixture(root)
            search = self._open_search(root)
            client = TestClient(create_app(search))

            response = client.get("/api/search", params={"q": "rotors"})
            self.assertEqual(response.status_code, 200)
            result = response.json()[0]
            self.assertEqual(result["title"], "Beta")
            self.assertEqual(result["path"], "Beta")
            self.assertEqual(result["url"], "http://kiwix/content/manual/Beta")
            self.assertEqual(response.headers["x-total-count"], "1")

            stream = client.get("/api/search/stream", params={"q": "rotors"})
            self.assertEqual(stream.status_code, 200)
            self.assertEqual(
                [json.loads(line)["type"] for line in stream.text.splitlines()],
                ["started", "source", "snapshot", "done"],
            )
            self.assertEqual(json.loads(stream.text.splitlines()[-1])["results"][0]["title"], "Beta")

    def test_interrupted_upgrade_keeps_published_index_until_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_pages = [("Old", "Old article", ALPHA_HTML)]
            new_pages = [
                ("New", "New article", BETA_HTML),
                ("New Two", "New second article", ALPHA_HTML),
            ]
            zim_path = self._build_fixture(root, pages=old_pages, fast=True)
            search = self._open_search(root)
            self.assertEqual(search.search("old article", limit=1)[0]["title"], "Old article")

            replacement = root / "zims" / "manual-replacement.zim"
            _create_zim(replacement, new_pages)
            replacement.replace(zim_path)

            real_read_entry = build_module.read_entry
            calls = 0

            def read_one_then_interrupt(zim, index, **kwargs):
                nonlocal calls
                if calls:
                    raise RuntimeError("interrupted upgrade")
                calls += 1
                return real_read_entry(zim, index, **kwargs)

            with patch.object(build_module, "read_entry", side_effect=read_one_then_interrupt):
                with self.assertRaisesRegex(RuntimeError, "interrupted upgrade"):
                    build_module.build(
                        zim_path,
                        root / "indexes",
                        embedder=None,
                        batch_size=1,
                        fast=False,
                    )

            self.assertEqual(search.search("old article", limit=1)[0]["title"], "Old article")
            self.assertEqual(search.search("new article", limit=1), [])

            build_module.build(
                zim_path,
                root / "indexes",
                embedder=None,
                batch_size=1,
                fast=False,
            )
            summary = search.reload()
            self.assertEqual(summary["upgraded"], [])
            self.assertEqual(search.search("new article", limit=1)[0]["title"], "New article")
            self.assertEqual(search.search("old article", limit=1), [])

    def test_corrupt_vectors_and_missing_zim_degrade_to_title_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zim_path = self._build_fixture(root)
            faiss_path = root / "indexes" / "manual.faiss"
            faiss_path.write_bytes(b"not a faiss index")

            search = self._open_search(root, semantic=True)
            self.assertFalse(search.indexes["manual"].semantic)
            self.assertEqual(search.search("alpha", limit=1)[0]["title"], "Alpha")

            zim_path.unlink()
            missing_zim_search = self._open_search(root)
            self.assertIsNone(missing_zim_search.indexes["manual"].archive)
            self.assertEqual(
                missing_zim_search.search("beta", limit=1)[0]["title"],
                "Beta",
            )

    def test_redirects_collapse_and_disambiguation_ranking_survive_real_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fixture(
                root,
                pages=[
                    ("Air", "Air", AIR_HTML),
                    ("Air_(disambiguation)", "Air", AIR_DISAMBIGUATION_HTML),
                ],
                redirects=[("Breeze", "Breeze", "Air")],
            )
            search = self._open_search(root)

            redirect_results = search.search("breeze", limit=10)
            self.assertEqual(len(redirect_results), 1)
            self.assertEqual(redirect_results[0]["title"], "Air")
            self.assertEqual(redirect_results[0]["path"], "Air")

            ordinary_results = search.search("air", limit=10)
            self.assertEqual(ordinary_results[0]["title"], "Air")

            disambiguation_results = search.search("air disambiguation", limit=10)
            self.assertEqual(
                disambiguation_results[0]["title"],
                "Air (disambiguation)",
            )

    def test_http_boundaries_etags_and_gzip_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            long_text = "A detailed article body for compression testing. " * 30
            long_html = f"<html><body><p>{long_text}</p></body></html>"
            self._build_fixture(
                root,
                pages=[
                    ("Article One", "Article One", long_html),
                    ("Article Two", "Article Two", long_html),
                ],
            )
            search = self._open_search(root)
            client = TestClient(create_app(search))

            self.assertEqual(client.get("/api/search", params={"q": " "}).status_code, 400)
            self.assertEqual(
                client.get("/api/search", params={"q": "x" * 4097}).status_code,
                422,
            )
            self.assertEqual(
                client.get("/api/search", params={"q": "article", "limit": 0}).status_code,
                422,
            )
            self.assertEqual(
                client.get("/api/search", params={"q": "article", "offset": -1}).status_code,
                422,
            )

            sources = client.get("/api/sources")
            old_etag = sources.headers["etag"]
            self.assertEqual(
                client.get("/api/sources", headers={"If-None-Match": old_etag}).status_code,
                304,
            )

            response = client.get("/api/search", params={"q": "article"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("content-encoding"), "gzip")
            self.assertEqual(len(response.json()), 2)

            self._build_fixture(
                root,
                pages=[("Other", "Other", ALPHA_HTML)],
                fast=True,
                zim_name="other",
            )
            search.reload()
            changed_sources = client.get("/api/sources")
            self.assertNotEqual(changed_sources.headers["etag"], old_etag)
            self.assertEqual(
                client.get("/api/sources", headers={"If-None-Match": old_etag}).status_code,
                200,
            )


if __name__ == "__main__":
    unittest.main()
