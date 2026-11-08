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
    def test_library_empty_state_distinguishes_filtered_from_unconfigured(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const elements = {{
  libraryGrid: {{
    children: [],
    replaceChildren() {{ this.children = []; }},
    appendChild(child) {{ this.children.push(child); }},
  }},
  libraryStatus: {{textContent: ""}},
  libraryFilter: {{value: "", placeholder: ""}},
  libraryProviderFilter: {{value: ""}},
  libraryMediaFilter: {{value: ""}},
}};
const $ = (id) => elements[id] || null;
const document = {{createElement: () => ({{}})}};
const t = (value) => value;
const librarySources = [];
let collections = [];
let query = "";
const libraryCollectionViews = () => [];
const libraryFilterTerms = () => query;
{_function_source(source, "function renderLibrary()", "function resultKey(")}
const renderCase = (filter, configuredCollections) => {{
  query = filter;
  collections = configuredCollections;
  elements.libraryFilter.value = filter;
  renderLibrary();
  return {{
    message: elements.libraryGrid.children[0].textContent,
    status: elements.libraryStatus.textContent,
  }};
}};
console.log(JSON.stringify({{
  unconfigured: renderCase("", []),
  noMatches: renderCase("missing", []),
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["unconfigured"]["message"],
            "No collections are configured. Add a [[collections]] entry to the server configuration, then restart the server.",
        )
        self.assertEqual(payload["unconfigured"]["status"], "")
        self.assertEqual(
            payload["noMatches"]["message"],
            "No collection or source matches these filters.",
        )
        self.assertEqual(
            payload["noMatches"]["status"],
            "0 collections · 0 loaded sources",
        )

    def test_search_urls_use_canonical_source_and_collection_selectors(self):
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
current = {{question: "q", searchAll: false, searchKeys: [], searchCollection: "zim:library"}};
const grouped = makeStateUrl("q", [], 0);
location.href = "http://example.test/?q=q&collection=zim:library&source=a";
location.search = "?q=q&collection=zim:library&source=a";
const state = readLocationState();
location.href = "http://example.test/?q=q&source=a";
location.search = "?q=q&source=a";
const explicit = readLocationState();
current = {{question: "q", searchAll: false, searchKeys: ["a", "b"], searchCollection: "zim:library"}};
const expandedCollection = makeStateUrl("q", [], 0);
console.log(JSON.stringify({{
  full,
  selective,
  grouped,
  selected: state.selectedSources,
  scope: state.scopeSources,
  collection: state.collection,
  hasScope: state.hasScope,
  explicitSources: explicit.selectedSources,
  explicitCollection: explicit.collection,
  explicitHasScope: explicit.hasScope,
  expandedCollection,
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["full"], "/?q=q&source=a")
        self.assertEqual(payload["selective"], "/?q=q&source=a")
        self.assertEqual(payload["grouped"], "/?q=q&collection=zim%3Alibrary")
        self.assertEqual(payload["selected"], ["a"])
        self.assertEqual(payload["scope"], ["a"])
        self.assertEqual(payload["collection"], "zim:library")
        self.assertTrue(payload["hasScope"])
        self.assertEqual(payload["explicitSources"], ["a"])
        self.assertIsNone(payload["explicitCollection"])
        self.assertTrue(payload["explicitHasScope"])
        self.assertEqual(payload["expandedCollection"], "/?q=q&collection=zim%3Alibrary")

    def test_deep_search_retains_discovery_only_for_the_same_broadly_searched_query(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
let selectedSourceKeys = new Set();
let selectedCollectionKey = null;
let sourceFilterShowAll = false, sourceFilterShowOther = false;
let requestController = null, pageController = null;
let lastStreamRenderAt = 0, searchSequence = 0, activeSearchResult = null;
const thumbnailCache = new Map();
const PAGE_SIZE = 10, SOURCE_FILTER_LIMIT = 10, SOURCE_FILTER_QUERY_LIMIT = 30;
const STREAM_FIRST_BYTE_TIMEOUT = 1000, STREAM_TOTAL_TIMEOUT = 1000;
const STREAM_IDLE_TIMEOUT = 1000, BUSY_RETRY_LIMIT = 0;
const window = {{scrollTo() {{}}}};
const document = {{body: {{classList: {{add() {{}}}}}}}};
const $ = () => ({{value: "", hidden: false, disabled: false}});
const setSettingsOpen = () => {{}};
const cancelStreamRender = () => {{}};
const cancelThumbnails = () => {{}};
const queryTerms = (question) => [question];
const announceSearchStatus = () => {{}};
const renderScopeLine = () => {{}};
const renderSourceFilters = () => {{}};
const renderResults = () => {{}};
const renderView = () => {{}};
const searchStatusText = () => "";
const debugMode = false;
const syncPage = () => {{}};
const escapeHtml = (text) => text;
const activeSources = () => [{{key: "a"}}, {{key: "b"}}, {{key: "c"}}];
const appPath = (path) => path;
const sourceDisplayName = (source) => source.key;
const outcome = (key) => ({{source: {{key, provider: "zim"}}, status: "ok"}});
const requests = [];
const fetch = async (url) => {{
    requests.push(new URL(url, "http://example.test").searchParams.getAll("source"));
    return {{ok: true, json: async () => [], headers: new Map()}};
}};
const consumeStream = async () => {{
    current.outcomes = new Map([...selectedSourceKeys].map((key) => [key, outcome(key)]));
    current.counts = Object.fromEntries([...selectedSourceKeys].map((key) => [key, 20]));
    current.completed = current.sourcesTotal = selectedSourceKeys.size;
    return false;
}};
{_function_source(source, "function selectionValues(", "function sourceFilterLayout(")}
{_function_source(source, "function sourceFilterLayout(", "function sourceFilterControlKey(")}
{_function_source(source, "async function runSearch(", "async function loadPage(")}
{_function_source(source, "async function loadPage(", "function applySourceSelection(")}
const broad = {{
    question: "q", searchAll: true,
    outcomes: new Map(["a", "b", "c"].map((key) => [key, outcome(key)])),
    counts: {{a: 5, b: 4, c: 3}},
}};
let current = broad;
const summarize = () => {{
    const layout = sourceFilterLayout();
    return {{
        counts: Object.fromEntries(layout.outcomes.map((item) => [item.source.key, layout.outcomeCount(item)])),
        progress: effectiveSourceProgress(),
        discovery: Boolean(current.discovery),
    }};
}};
(async () => {{
    await runSearch("q", ["a"]);
    const first = summarize();
    await runSearch("q", ["a", "b"]);
    const expanded = summarize();
    await runSearch("q", ["b"]);
    const narrowed = summarize();
    await runSearch("other query", ["b"]);
    const changed = summarize();
    await runSearch("other query", ["a", "b"]);
    const direct = summarize();
    await runSearch("other query", [], 0, [], "zim:library");
    current.searchKeys = ["a", "b", "c"];
    await loadPage(1);
    await runSearch("other query", ["b"], 0, ["b"], "zim:library");
    await loadPage(1);
    console.log(JSON.stringify({{first, expanded, narrowed, changed, direct, requests, broadCounts: broad.counts}}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
        result = subprocess.run(
            [NODE, "-e", script], encoding="utf-8", capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["first"]["counts"], {"a": 20, "b": 4, "c": 3})
        self.assertEqual(payload["first"]["progress"], {"completed": 1, "total": 1, "selected": True})
        self.assertEqual(payload["expanded"]["counts"], {"a": 20, "b": 20, "c": 3})
        self.assertEqual(payload["narrowed"]["counts"], {"a": 5, "b": 20, "c": 3})
        self.assertTrue(payload["narrowed"]["discovery"])
        self.assertEqual(payload["changed"]["counts"], {"b": 20})
        self.assertFalse(payload["changed"]["discovery"])
        self.assertEqual(payload["direct"]["counts"], {"a": 20, "b": 20})
        self.assertFalse(payload["direct"]["discovery"])
        self.assertEqual(
            payload["requests"],
            [["a"], ["a", "b"], ["b"], ["b"], ["a", "b"], [], [], ["b"], ["b"]],
        )
        self.assertEqual(payload["broadCounts"], {"a": 5, "b": 4, "c": 3})

    def test_scope_helpers_search_only_selected_sources(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
let current = null;
let selectedSourceKeys = new Set();
let selectedCollectionKey = null;
{_function_source(source, "function selectionValues(", "function dedupeParams(")}
{_function_source(source, "function dedupeParams(", "function setSelectedSources(")}
{_function_source(source, "function setSelectedSources(", "function searchScopeParams(")}
const full = {{question: "q", searchAll: true, searchKeys: [], searchCollection: null, outcomes: new Map([["a", {{source: {{key: "a"}}}}], ["b", {{source: {{key: "b"}}}}]]), completed: 8, sourcesTotal: 8}};
const explicit = {{question: "q", searchAll: false, searchKeys: ["a", "b"], searchCollection: null, outcomes: new Map([["a", {{source: {{key: "a"}}}}], ["b", {{source: {{key: "b"}}}}]]), completed: 2, sourcesTotal: 2}};
applyLocationScope({{selectedSources: ["a"], collection: "files:media"}});
const locationScope = {{selected: [...selectedSourceKeys], collection: selectedCollectionKey}};
const dedupe = new URLSearchParams("source=a&source=a&source=b");
dedupeParams(dedupe, "source");
current = full;
selectedSourceKeys = new Set(["a"]);
const fullNext = nextSearchScope(["a"], null, current);
const fullProgress = effectiveSourceProgress();
current = explicit;
selectedSourceKeys = new Set(["a"]);
const explicitNext = nextSearchScope(["a"], null, current);
selectedSourceKeys = new Set();
const explicitProgress = effectiveSourceProgress();
const freshNext = nextSearchScope(["a"], null, null);
console.log(JSON.stringify({{
  locationScope,
  dedupe: dedupe.getAll("source"),
  fullNext,
  fullProgress,
  explicitNext,
  explicitProgress,
  freshNext,
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["locationScope"],
            {"selected": ["a"], "collection": "files:media"},
        )
        self.assertEqual(payload["dedupe"], ["a", "b"])
        self.assertEqual(payload["fullNext"], ["a"])
        self.assertEqual(
            payload["fullProgress"],
            {"completed": 1, "total": 1, "selected": True},
        )
        self.assertEqual(payload["explicitNext"], ["a"])
        self.assertEqual(
            payload["explicitProgress"],
            {"completed": 2, "total": 2, "selected": False},
        )
        self.assertEqual(payload["freshNext"], ["a"])

    def test_make_state_url_handles_no_current_search_with_explicit_scope(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const location = new URL("http://example.test/");
let current = null;
{_function_source(source, "function selectionValues(", "function setSelectedSources(")}
{_function_source(source, "function makeStateUrl(", "function makePageUrl(")}
const full = makeStateUrl("q", [], 0, null, [], undefined);
const display = makeStateUrl("q", ["a"], 0, null, [], undefined);
const collection = makeStateUrl("q", [], 0, null, [], "files:media");
console.log(JSON.stringify({{full, display, collection}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["full"], "/?q=q")
        self.assertEqual(payload["display"], "/?q=q&source=a")
        self.assertEqual(payload["collection"], "/?q=q&collection=files%3Amedia")

    def test_source_filter_layout_is_bounded_per_collection_group(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const location = new URL("http://example.test/?q=q");
const document = {{ getElementById: () => ({{ value: "" }}) }};
const $ = (id) => document.getElementById(id);
let sources = [];
let librarySources = [];
let collections = [];
let libraryCollections = [];
const SOURCE_FILTER_LIMIT = 2;
const SOURCE_FILTER_QUERY_LIMIT = 30;
let sourceFilterShowAll = false;
let sourceFilterShowOther = false;
let selectedSourceKeys = new Set(["a3"]);
const formatLibrarySize = () => "";
{_function_source(source, "function libraryTitle(", "function sourceRecord(")}
{_function_source(source, "function sourceRecord(", "function sourceDisplayName(")}
{_function_source(source, "function sourceDisplayName(", "function sourceStemName(")}
{_function_source(source, "function sourceStemName(", "function libraryHomepageUrl(")}
{_function_source(source, "function providerLabel(", "function libraryHomepageUrl(")}
const outcome = (key, collection, count) => ({{
  source: {{ key, title: key.toUpperCase(), provider: "zim", collection }},
  status: "ok",
}});
const a = {{ key: "zim:a", label: "A", provider: "zim" }};
const b = {{ key: "zim:b", label: "B", provider: "zim" }};
let current = {{
  outcomes: new Map([
    ["a1", outcome("a1", a, 5)],
    ["a2", outcome("a2", a, 4)],
    ["a3", outcome("a3", a, 0)],
    ["b1", outcome("b1", b, 3)],
    ["b2", outcome("b2", b, 2)],
  ]),
  counts: {{ a1: 5, a2: 4, a3: 0, b1: 3, b2: 2 }},
}};
{_function_source(source, "function sourceFilterLayout(", "function sourceFilterControlKey(")}
const layout = sourceFilterLayout();
console.log(JSON.stringify(layout.groups.map((group) => ({{
  label: group.label,
  visible: group.visible.map((item) => item.source.key),
  other: group.other.map((item) => item.source.key),
}}))));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload,
            [
                {"label": "A", "visible": ["a3", "a1", "a2"], "other": []},
                {"label": "B", "visible": ["b1", "b2"], "other": []},
            ],
        )

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
{_function_source(source, "function replaceQueryParam(", "let PAGE_SIZE =")}
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
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["zero"], "/?q=q")
        self.assertEqual(payload["one"], "/?q=q")
        self.assertEqual(payload["decimal"], "/?q=q&page=2")
        self.assertIsNone(payload["high"])

    def test_source_selections_search_deeply_and_reuse_only_identical_scopes(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
let current = {{question: "q", searchAll: true, searchKeys: ["inside"], outcomes: new Map([["inside", {{source: {{key: "inside"}}}}]])}};
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
applySourceSelection("q", ["inside"], 0);
const inside = calls.at(-1);
applySourceSelection("q", ["outside"], 0, ["outside"]);
const outside = calls.at(-1);
current = {{question: "q", searchAll: false, searchKeys: ["inside", "outside"]}};
applySourceSelection("q", ["inside"], 0);
const narrowed = calls.at(-1);
current = {{question: "q", searchAll: false, searchKeys: ["inside"]}};
applySourceSelection("q", ["inside"], 1);
const same = calls.at(-1);
applySourceSelection("q", [], 0);
const cleared = calls.at(-1);
console.log(JSON.stringify({{inside, outside, narrowed, same, cleared}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["inside"], ["run", "q", ["inside"], 0, ["inside"]])
        self.assertEqual(
            payload["outside"],
            ["run", "q", ["outside"], 0, ["outside"]],
        )
        self.assertEqual(payload["narrowed"], ["run", "q", ["inside"], 0, ["inside"]])
        self.assertEqual(payload["same"], ["load", 1])
        self.assertEqual(payload["cleared"], ["run", "q", [], 0, []])


if __name__ == "__main__":
    unittest.main()
