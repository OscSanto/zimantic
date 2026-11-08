"""The evaluation runner against the current Zimantic API.

The runner is deliberately outside the package (standard library only), so it is loaded by path.
"""
import importlib.util
import io
import json
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RUN_PATH = Path(__file__).resolve().parents[1] / "evaluation" / "run.py"
_spec = importlib.util.spec_from_file_location("evaluation_run", RUN_PATH)
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)


class _Server:
    """A throwaway HTTP server: path -> callable(query dict) returning (status, JSON payload)."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                outer.requests.append(parsed.path + ("?" + parsed.query if parsed.query else ""))
                route = outer.routes.get(parsed.path)
                if route is None:
                    self.send_response(404)
                    body = b'{"detail":"Not Found"}'
                else:
                    status, payload = route(urllib.parse.parse_qs(parsed.query))
                    self.send_response(status)
                    if isinstance(payload, str):
                        body = payload.encode()
                    else:
                        body = json.dumps(payload).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@contextmanager
def stub(routes):
    server = _Server(routes)
    try:
        yield server
    finally:
        server.close()


def local(name, **extra):
    source = {"key": f"zim:library:{name}", "provider": "zim", "searchable": True}
    source.update(extra)
    return source


class SourceDiscoveryTests(unittest.TestCase):
    def test_only_current_sources_endpoint_and_searchable_zims_are_used(self):
        routes = {
            "/sources": lambda query: (200, [
                local("zim_a_2026-01"),
                local("zim_b_2026-01", searchable=False),
                {"key": "kiwix:library:wiki", "provider": "kiwix", "searchable": True},
            ]),
            "/api/zims": lambda query: (200, ["old-zim"]),
        }
        with stub(routes) as server:
            self.assertEqual(run.zimantic_sources(server.url, 5), {"zim:library:zim_a_2026-01"})
            self.assertEqual(server.requests, ["/sources?provider=zim"])

    def test_historical_api_is_not_used(self):
        routes = {"/api/zims": lambda query: (200, [{"name": "zim_a_2026-01"}])}
        with stub(routes) as server:
            with self.assertRaises(urllib.error.HTTPError):
                run.zimantic_sources(server.url, 5)
            self.assertEqual(server.requests, ["/sources?provider=zim"])

    def test_empty_source_listing_is_not_replaced(self):
        with stub({
            "/sources": lambda query: (200, []),
            "/library.json": lambda query: (200, [local("zim_a_2026-01")]),
        }) as server:
            self.assertEqual(run.zimantic_sources(server.url, 5), set())
            self.assertEqual(server.requests, ["/sources?provider=zim"])

    def test_config_is_required(self):
        with stub({"/config": lambda query: (200, {"max_results": 100, "max_query_length": 4096})}) as server:
            self.assertEqual(run.zimantic_config(server.url, 5)["max_results"], 100)
        with stub({}) as server:
            with self.assertRaises(urllib.error.HTTPError):
                run.zimantic_config(server.url, 5)

    def test_canonical_sources_exclude_other_providers_and_unindexed_zims(self):
        self.assertEqual(run.source_names([
            {"key": "zim:library:one", "provider": "zim", "searchable": True},
            {"key": "zim:library:two", "provider": "zim", "searchable": False},
            {"key": "files:library:one", "provider": "files", "searchable": True},
            {"key": "kiwix:library:one", "provider": "kiwix", "searchable": True},
        ]), {"zim:library:one"})


class ResultSourceTests(unittest.TestCase):
    def test_hits_require_the_exact_source_key(self):
        key = "zim:library:archive-abc123"
        self.assertTrue(run.belongs({"path": "A", "source_key": key}, key))
        self.assertFalse(run.belongs({"path": "A", "source": key, "source_key": "other"}, key))
        self.assertFalse(run.belongs({"path": "A", "source": key}, key))
        self.assertFalse(run.belongs({"path": "A", "source_key": None}, key))


class RunnerTests(unittest.TestCase):
    def test_zimantic_requires_one_zim_and_an_explicit_key(self):
        base = ["--engine", "zimantic", "--url", "http://localhost:8090", "--label", "test"]
        for extra in ([], ["--zims", "one"], ["--source-key", "zim:key"],
                      ["--source-key", "zim:key", "--zims", "one", "two"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as error:
                run.main(base + extra)
            self.assertEqual(error.exception.code, 2)

    def _queries(self, root: Path):
        folder = root / "queries" / "zim_a_2026-01"
        folder.mkdir(parents=True)
        rows = [
            {"id": "suite/title/000", "zim": "zim_a_2026-01", "suite": "suite", "set": "exact title",
             "lang": "en", "query": "alpha", "answers": ["Alpha"], "source": "hand"},
            {"id": "suite/foreign/000", "zim": "zim_a_2026-01", "suite": "suite", "set": "question",
             "lang": "en", "query": "beta", "answers": ["Beta"], "source": "hand"},
            {"id": "suite/long/000", "zim": "zim_a_2026-01", "suite": "suite", "set": "question",
             "lang": "en", "query": "x" * 30, "answers": ["Alpha"], "source": "hand"},
        ]
        (folder / "suite.en.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        gone = root / "queries" / "gone_2025-01"
        gone.mkdir(parents=True)
        (gone / "suite.en.jsonl").write_text(json.dumps(
            {"id": "suite/title/000", "zim": "gone_2025-01", "suite": "suite", "set": "exact title",
             "lang": "en", "query": "ghost", "answers": ["Ghost"], "source": "hand"}) + "\n")
        return root / "queries"

    def test_run_filters_sources_lengths_and_clamps_k(self):
        key = "zim:library:zim_a_2026-01"

        def search(query):
            pattern = (query.get("q") or [""])[0]
            self.assertEqual(query["source"], [key])
            self.assertNotIn("zim", query)
            if pattern == "alpha":
                return 200, [
                    {"path": "Alpha#Section", "source_key": key},
                    {"path": "Other", "source_key": "zim:library:other"},
                ]
            # The server fell back to every source: this hit is not from the requested ZIM.
            return 200, [{"path": "Beta", "source_key": "zim:library:other"}]

        routes = {
            "/sources": lambda query: (200, [local("zim_a_2026-01")]),
            "/config": lambda query: (200, {"max_results": 5, "max_query_length": 10}),
            "/search": search,
        }
        with tempfile.TemporaryDirectory() as tmp, stub(routes) as server:
            root = Path(tmp)
            queries = self._queries(root)
            output = io.StringIO()
            with redirect_stdout(output):
                run.main(["--engine", "zimantic", "--url", server.url, "--label", "stub",
                          "--queries", str(queries), "--results-dir", str(root / "results"), "--k", "20",
                          "--zims", "zim_a_2026-01", "--source-key", key])
            data = json.loads((root / "results" / "stub.json").read_text())
            self.assertEqual(data["meta"]["k"], 5)
            # A hit from another source is ignored and the query counts as a miss.
            self.assertEqual(data["results"]["zim_a_2026-01|suite/title/000"]["rank"], 1)
            self.assertEqual(data["results"]["zim_a_2026-01|suite/title/000"]["top"], ["Alpha"])
            self.assertIsNone(data["results"]["zim_a_2026-01|suite/foreign/000"]["rank"])
            self.assertEqual(data["results"]["zim_a_2026-01|suite/foreign/000"]["top"], [])
            # Queries for ZIMs the server cannot search, and queries over the length limit, are left out.
            self.assertNotIn("zim_a_2026-01|suite/long/000", data["results"])
            self.assertNotIn("gone_2025-01|suite/title/000", data["results"])
            text = output.getvalue()
            self.assertIn("1 queries longer than the server's 10-character limit", text)
            self.assertIn("left out results from other sources", text)
            self.assertFalse(any("x" * 30 in request for request in server.requests))

    def test_append_keeps_earlier_zims(self):
        key = "zim:library:zim_a_2026-01"
        routes = {
            "/sources": lambda query: (200, [local("zim_a_2026-01")]),
            "/config": lambda query: (200, {"max_results": 100, "max_query_length": 4096}),
            "/search": lambda query: (200, [{"path": "Alpha", "source_key": key}]),
        }
        with tempfile.TemporaryDirectory() as tmp, stub(routes) as server:
            root = Path(tmp)
            queries = self._queries(root)
            out = root / "results"
            out.mkdir()
            (out / "combined.json").write_text(json.dumps(
                {"meta": {"label": "combined", "k": 20, "unsupported": {}},
                 "results": {"other_2025-01|suite/title/000": {"rank": 1, "ms": 1.0, "top": ["Other"]}}}))
            with redirect_stdout(io.StringIO()):
                run.main(["--engine", "zimantic", "--url", server.url, "--label", "combined",
                          "--queries", str(queries), "--results-dir", str(out), "--append",
                          "--zims", "zim_a_2026-01", "--source-key", key])
            data = json.loads((out / "combined.json").read_text())
            self.assertEqual(data["results"]["other_2025-01|suite/title/000"]["rank"], 1)
            self.assertEqual(data["results"]["zim_a_2026-01|suite/title/000"]["rank"], 1)

    def test_canonical_source_key_scopes_requests_and_filters_foreign_hits(self):
        key = "zim:library:archive-abc123"

        def search(query):
            self.assertEqual(query["source"], [key])
            self.assertNotIn("zim", query)
            return 200, [
                {"path": "Alpha", "source": "Display title", "source_key": key},
                {"path": "Beta", "source": "Other title", "source_key": "zim:other"},
            ]

        with tempfile.TemporaryDirectory() as tmp, stub({
            "/sources": lambda query: (200, [
                {"key": key, "provider": "zim", "searchable": True},
            ]),
            "/config": lambda query: (200, {"max_results": 100, "max_query_length": 4096}),
            "/search": search,
        }) as server:
            root = Path(tmp)
            queries = self._queries(root)
            with redirect_stdout(io.StringIO()):
                run.main(["--engine", "zimantic", "--url", server.url, "--label", "canonical",
                          "--queries", str(queries), "--results-dir", str(root / "results"),
                          "--zims", "zim_a_2026-01", "--source-key", key])
            data = json.loads((root / "results" / "canonical.json").read_text())
            self.assertEqual(data["meta"]["source_key"], key)
            self.assertEqual(data["results"]["zim_a_2026-01|suite/title/000"]["rank"], 1)
            self.assertIsNone(data["results"]["zim_a_2026-01|suite/foreign/000"]["rank"])

    def test_unsearchable_key_aborts_before_search(self):
        with tempfile.TemporaryDirectory() as tmp, stub({
            "/sources": lambda query: (200, [local("zim_a_2026-01", searchable=False)]),
            "/config": lambda query: (200, {"max_results": 100, "max_query_length": 4096}),
        }) as server:
            root = Path(tmp)
            queries = self._queries(root)
            with self.assertRaisesRegex(SystemExit, "source is not searchable"):
                run.main(["--engine", "zimantic", "--url", server.url, "--label", "missing",
                          "--queries", str(queries), "--results-dir", str(root / "results"),
                          "--zims", "zim_a_2026-01", "--source-key", "zim:library:zim_a_2026-01"])
            self.assertFalse((root / "results").exists())
            self.assertFalse(any(request.startswith("/search?") for request in server.requests))


class KiwixTests(unittest.TestCase):
    def test_run_keeps_kiwix_results_without_zimantic_source_keys(self):
        with tempfile.TemporaryDirectory() as tmp, stub({
            "/search": lambda query: (200, '<a href="/content/zim_a_2026-01/Alpha">Alpha</a>'),
        }) as server:
            root = Path(tmp)
            queries = RunnerTests()._queries(root)
            with redirect_stdout(io.StringIO()):
                run.main(["--engine", "kiwix", "--url", server.url, "--label", "kiwix",
                          "--queries", str(queries), "--results-dir", str(root / "results"),
                          "--zims", "zim_a_2026-01"])
            data = json.loads((root / "results" / "kiwix.json").read_text())
            self.assertEqual(data["results"]["zim_a_2026-01|suite/title/000"]["rank"], 1)

    def test_result_links_are_decoded(self):
        html = ('<a href="/content/zim_a_2026-01/Alpha">Alpha</a>'
                '<a href="/content/zim_a_2026-01/Pr%C3%A9cis">Précis</a>'
                '<a href="/content/other_2025-01/Alpha">Alpha</a>')
        with stub({"/search": lambda query: (200, html)}) as server:
            hits = run.kiwix(server.url, "zim_a_2026-01", "alpha", 10, 5)
        self.assertEqual([hit["path"] for hit in hits], ["Alpha", "Précis"])

    def test_refused_zim_is_unsupported(self):
        with stub({"/search": lambda query: (400, {"detail": "no"})}) as server:
            with self.assertRaises(run.Unsupported):
                run.kiwix(server.url, "ted_mul_ocean_2026-09", "ocean", 10, 5)


if __name__ == "__main__":
    unittest.main()
