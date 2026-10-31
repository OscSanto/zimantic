import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "zimantic" / "web" / "static" / "app.js"
NODE = shutil.which("node")


def _function_source(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


@unittest.skipUnless(NODE, "node is unavailable")
class AppSourceStateTests(unittest.TestCase):
    def test_full_search_filters_are_url_display_filters(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const location = new URL("http://example.test/?q=q");
const MAX_QUERY_LENGTH = 4096;
let current = {{question: "q", searchAll: true, searchKeys: ["a", "b", "c"]}};
{_function_source(source, "function selectionValues(", "function setSelectedSources(")}
{_function_source(source, "function makeStateUrl(", "function makePageUrl(")}
{_function_source(source, "function readLocationState(", "function syncArticleModal(")}
const full = makeStateUrl("q", ["a"], 0);
current = {{question: "q", searchAll: false, searchKeys: ["a", "b"]}};
const selective = makeStateUrl("q", ["a"], 0);
location.href = "http://example.test/?q=q&zim=a&zim=b&source=a";
location.search = "?q=q&zim=a&zim=b&source=a";
const state = readLocationState();
location.href = "http://example.test/?q=q&source=a";
location.search = "?q=q&source=a";
const displayOnly = readLocationState();
console.log(JSON.stringify({{
  full,
  selective,
  selected: state.selectedSources,
  scope: state.scopeSources,
  hasScope: state.hasScope,
  displayOnly: displayOnly.selectedSources,
  displayOnlyScope: displayOnly.scopeSources,
  displayOnlyHasScope: displayOnly.hasScope,
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            text=True,
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["full"], "/?q=q&source=a")
        self.assertEqual(
            payload["selective"], "/?q=q&zim=a&zim=b&source=a"
        )
        self.assertEqual(payload["selected"], ["a"])
        self.assertEqual(payload["scope"], ["a", "b"])
        self.assertTrue(payload["hasScope"])
        self.assertEqual(payload["displayOnly"], ["a"])
        self.assertEqual(payload["displayOnlyScope"], [])
        self.assertFalse(payload["displayOnlyHasScope"])

    def test_page_parameter_is_canonicalized(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const location = new URL("http://example.test/?q=q&page=0");
let replaced = null;
const history = {{
  state: null,
  replaceState(state, _title, url) {{ replaced = String(url); }},
}};
const MAX_QUERY_LENGTH = 4096;
{_function_source(source, "function normalizePageParam(", "function readLocationState(")}
normalizePageParam();
const zero = replaced;
location.href = "http://example.test/?q=q&page=1";
replaced = null;
normalizePageParam();
const one = replaced;
location.href = "http://example.test/?q=q&page=2.5";
replaced = null;
normalizePageParam();
const decimal = replaced;
location.href = "http://example.test/?q=q&page=999";
replaced = null;
normalizePageParam();
const high = replaced;
console.log(JSON.stringify({{zero, one, decimal, high}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            text=True,
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["zero"], "/?q=q")
        self.assertEqual(payload["one"], "/?q=q")
        self.assertEqual(payload["decimal"], "/?q=q&page=2")
        self.assertIsNone(payload["high"])

    def test_source_outside_a_full_search_pool_is_searched_explicitly(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
let current = {{question: "q", searchAll: true, searchKeys: ["inside"]}};
let selectedSourceKeys = new Set();
const calls = [];
function selectionValues(values) {{
  if (values instanceof Set) return [...values].filter(Boolean);
  return (Array.isArray(values) ? values : values ? [values] : []).filter(Boolean);
}}
function setSelectedSources(values) {{ selectedSourceKeys = new Set(values); }}
function loadPage(page) {{ calls.push(["load", page]); }}
function runSearch(question, sources, page, scope) {{
  calls.push(["run", question, sources, page, scope]);
}}
{_function_source(source, "function applySourceSelection(", "function makeStateUrl(")}
applySourceSelection("q", ["inside"], 0, []);
const inside = calls.at(-1);
applySourceSelection("q", ["outside"], 0, ["outside"]);
const outside = calls.at(-1);
console.log(JSON.stringify({{inside, outside}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            text=True,
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["inside"], ["load", 0])
        self.assertEqual(
            payload["outside"],
            ["run", "q", ["outside"], 0, ["outside"]],
        )


if __name__ == "__main__":
    unittest.main()
