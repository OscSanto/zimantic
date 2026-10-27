"""The evaluation runner against the API shapes current and older Zimantic servers expose.

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
    source = {"mode": "local", "local_name": name, "name": name, "book": name,
              "available": True, "searchable": True}
    source.update(extra)
    return source


class SourceDiscoveryTests(unittest.TestCase):
    def test_library_json_is_preferred_and_only_indexed_local_zims_count(self):
        routes = {
            "/library.json": lambda query: (200, [
                local("zim_a_2026-01"),
                local("zim_b_2026-01", searchable=False),
                local("zim_c_2026-01", available=False),
                {"mode": "kiwix", "name": "Wikipedia", "book": "wikipedia_en_all",
                 "available": True, "searchable": True},
            ]),
            "/sources": lambda query: (200, [local("zim_a_2026-01")]),
        }
        with stub(routes) as server:
            base, names = run.zimantic_api(server.url, 5)
            self.assertEqual(base, server.url)
            self.assertEqual(names, {"zim_a_2026-01"})
            self.assertIn("/library.json", server.requests)
            self.assertNotIn("/sources", server.requests)

    def test_sources_is_used_when_library_json_is_missing(self):
        routes = {"/sources": lambda query: (200, [
            local("zim_a_2026-01"),
            {"mode": "kiwix", "name": "Wikipedia", "book": "wikipedia_en_all"},
        ])}
        with stub(routes) as server:
            self.assertEqual(run.zimantic_api(server.url, 5), (server.url, {"zim_a_2026-01"}))

    def test_empty_library_falls_through_to_sources(self):
        routes = {
            "/library.json": lambda query: (200, []),
            "/sources": lambda query: (200, [local("zim_a_2026-01")]),
        }
        with stub(routes) as server:
            self.assertEqual(run.zimantic_api(server.url, 5), (server.url, {"zim_a_2026-01"}))

    def test_v01_api_is_still_found(self):
        routes = {"/api/zims": lambda query: (200, [{"name": "zim_a_2026-01"}])}
        with stub(routes) as server:
            base, names = run.zimantic_api(server.url, 5)
            self.assertEqual(base, f"{server.url}/api")
            self.assertEqual(names, {"zim_a_2026-01"})

    def test_no_known_api_exits(self):
        with stub({}) as server:
            with self.assertRaises(SystemExit):
                run.zimantic_api(server.url, 5)

    def test_config_is_optional(self):
        with stub({"/config": lambda query: (200, {"max_results": 100, "max_query_length": 4096})}) as server:
            self.assertEqual(run.zimantic_config(server.url, 5)["max_results"], 100)
        with stub({}) as server:
            self.assertEqual(run.zimantic_config(server.url, 5), {})


class ResultSourceTests(unittest.TestCase):
    def test_hits_are_matched_to_the_requested_zim(self):
        self.assertTrue(run.belongs({"path": "A", "source": "zim_a_2026-01", "source_key": "zim_a"}, "zim_a_2026-01"))
        self.assertTrue(run.belongs({"path": "A", "source": None, "source_key": "zim_a"}, "zim_a_2026-01"))
        # Older servers do not report a source at all.
        self.assertTrue(run.belongs({"path": "A", "source": None, "source_key": None}, "zim_a_2026-01"))
        # Another ZIM, even one with the same article path, must not count.
        self.assertFalse(run.belongs({"path": "A", "source": "zim_b_2026-01", "source_key": "zim_b"}, "zim_a_2026-01"))

    def test_perma_ref_strips_release_dates_only(self):
        self.assertEqual(run.perma_ref("wikipedia_en_medicine_maxi_2026-04"), "wikipedia_en_medicine_maxi")
        self.assertEqual(run.perma_ref("wikipedia_en_medicine_maxi_2026-04-15"), "wikipedia_en_medicine_maxi")
        self.assertEqual(run.perma_ref("cooking.stackexchange.com_en_all_2026-07"), "cooking.stackexchange.com_en_all")
        self.assertEqual(run.perma_ref("manual"), "manual")


class RunnerTests(unittest.TestCase):
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
        def search(query):
            pattern = (query.get("q") or [""])[0]
            zim = (query.get("zim") or [""])[0]
            if zim != "zim_a_2026-01":
                return 200, [{"path": "Ghost", "source": "zim_a_2026-01", "source_key": "zim_a"}]
            if pattern == "alpha":
                return 200, [
                    {"path": "Alpha", "source": "zim_a_2026-01", "source_key": "zim_a"},
                    {"path": "Other", "source": "zim_b_2026-01", "source_key": "zim_b"},
                ]
            # The server fell back to every source: this hit is not from the requested ZIM.
            return 200, [{"path": "Beta", "source": "zim_b_2026-01", "source_key": "zim_b"}]

        routes = {
            "/library.json": lambda query: (200, [local("zim_a_2026-01")]),
            "/config": lambda query: (200, {"max_results": 5, "max_query_length": 10}),
            "/search": search,
        }
        with tempfile.TemporaryDirectory() as tmp, stub(routes) as server:
            root = Path(tmp)
            queries = self._queries(root)
            output = io.StringIO()
            with redirect_stdout(output):
                run.main(["--engine", "zimantic", "--url", server.url, "--label", "stub",
                          "--queries", str(queries), "--results-dir", str(root / "results"), "--k", "20"])
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
            self.assertIn("left out of this run: gone_2025-01", text)
            self.assertIn("1 queries longer than the server's 10-character limit", text)
            self.assertIn("left out results from other sources", text)
            self.assertFalse(any("x" * 30 in request for request in server.requests))

    def test_append_keeps_earlier_zims(self):
        routes = {
            "/library.json": lambda query: (200, [local("zim_a_2026-01")]),
            "/search": lambda query: (200, [{"path": "Alpha", "source": "zim_a_2026-01", "source_key": "zim_a"}]),
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
                          "--queries", str(queries), "--results-dir", str(out), "--append"])
            data = json.loads((out / "combined.json").read_text())
            self.assertEqual(data["results"]["other_2025-01|suite/title/000"]["rank"], 1)
            self.assertEqual(data["results"]["zim_a_2026-01|suite/title/000"]["rank"], 1)


class KiwixTests(unittest.TestCase):
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
