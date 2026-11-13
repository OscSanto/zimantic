import json
import shutil
import subprocess
import unittest
from pathlib import Path

from zimantic.providers.files.listing import directory_page

APP_JS = Path(__file__).resolve().parents[1] / "zimantic" / "web" / "static" / "app.js"
NODE = shutil.which("node")


def _function_source(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


@unittest.skipUnless(NODE, "node is unavailable")
class AppSourceStateTests(unittest.TestCase):
    def test_resource_results_use_human_breadcrumbs_kinds_and_metadata(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const location = new URL("http://example.test/zimantic/");
const stripBasePath = (path) => path.replace(/^\\/zimantic/, "");
const t = (text) => text;
{_function_source(source, "function resultUrlLabel(", "function modifiedClick(")}
const item = {{
  path: "node/opaque-id", url: "/zimantic/content/source/node/opaque-id",
  breadcrumbs: ["Math", "Unit 1", "Counting", "Count with small numbers"],
  resource: {{
    kind: "exercise", grade_levels: ["Lower Primary"], duration: 125,
    language: "English", author: "<img src=x onerror=alert(1)>",
    assessment_count: 8, topic_position: 2, topic_total: 3,
    tags: ["numeracy", "arithmetic"], learning_activities: ["Practice"],
  }},
}};
const invalid = {{resource: {{duration: -1, assessment_count: -1, topic_position: 99, topic_total: 3}}}};
console.log(JSON.stringify({{
  label: resultUrlLabel(item), details: resourceDetails(item),
  badge: mediaBadge("application/octet-stream", item.resource.kind),
  interactive: mediaBadge("application/zip", "html5"),
  video: mediaBadge("video/mp4"),
  kolibriVideo: mediaBadge("video/mp4", "video"),
  otherMedia: ["audio/mpeg", "image/png", "text/plain", "application/octet-stream"]
    .map((mime) => mediaBadge(mime)),
  fileLabel: resultUrlLabel({{path: "folder/file.txt"}}),
  pdf: mediaBadge("application/pdf"), invalid: resourceDetails(invalid),
  empty: resourceDetails({{}}),
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["label"], "Math › Unit 1 › Counting › Count with small numbers")
        self.assertEqual(payload["badge"], {"icon": "Exercise", "label": "Exercise"})
        self.assertEqual(payload["interactive"], {"icon": "Interactive", "label": "Interactive"})
        self.assertEqual(payload["video"], {"icon": "Video", "label": "Video"})
        self.assertEqual(payload["video"], payload["kolibriVideo"])
        self.assertEqual(payload["otherMedia"], [
            {"icon": "Audio", "label": "Audio"},
            {"icon": "Image", "label": "Image"},
            {"icon": "Text", "label": "Text"},
            {"icon": "File", "label": "File"},
        ])
        self.assertEqual(payload["fileLabel"], "folder › file.txt")
        self.assertEqual(payload["pdf"], {"icon": "PDF", "label": "PDF"})
        self.assertEqual(payload["details"], [
            "Grade level: Lower Primary", "Duration: 2:05", "Language: English",
            "Author: <img src=x onerror=alert(1)>", "8 questions",
            "Position in topic: 2 / 3", "Tags: numeracy, arithmetic",
            "Learning activities: Practice",
        ])
        self.assertEqual(payload["invalid"], [])
        self.assertEqual(payload["empty"], [])

    def test_result_card_uses_description_fallback_and_clears_stale_metadata(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
class Node {{
  constructor(tagName = "") {{
    this.tagName = tagName; this.attributes = {{}};
    this.children = []; this.textContent = ""; this.dataset = {{}};
    this.classes = new Set();
    this.classList = {{toggle: (name, enabled) =>
      enabled ? this.classes.add(name) : this.classes.delete(name)}};
  }}
  replaceChildren(...children) {{ this.children = children; }}
  append(...children) {{ this.children.push(...children); }}
  appendChild(child) {{ this.append(child); }}
  setAttribute(name, value) {{ this.attributes[name] = value; }}
  remove() {{ node.children = node.children.filter((child) => child !== this); }}
}}
const parts = Object.fromEntries(
  [".result-title", ".result-resource", ".result-snippet"].map((key) => [key, new Node()])
);
Object.defineProperty(parts[".result-resource"], "innerHTML", {{
  set() {{ throw new Error("metadata must not be interpreted as HTML"); }},
}});
const node = new Node();
node.querySelector = (selector) => parts[selector] ||
  (selector === ".result-thumb"
    ? node.children.find((child) => child.className === "result-thumb") : null);
const document = {{createElement: (name) => new Node(name)}};
const resultKey = () => "key";
const t = (text) => text;
const setSourceMeta = () => {{}}, setResultUrl = () => {{}}, setResultDebug = () => {{}};
const previewBlocks = (text) => [text], previewEndsWithPunctuation = () => true;
const highlight = (text) => text;
const current = {{streaming: true, terms: []}};
{_function_source(source, "function resourceDetails(", "function isHtmlMimetype(")}
{_function_source(source, "function isHtmlMimetype(", "function mediaBadge(")}
{_function_source(source, "function mediaBadge(", "function modifiedClick(")}
{_function_source(source, "function setResultNode(", "function newResultNode(")}
setResultNode(node, {{
  title: "Count", url: "/content/manual/count", mimetype: "text/html", resource: {{
    kind: "exercise", author: "<script>unsafe()</script>", description: "Public description.",
  }},
}});
const initial = {{
  details: parts[".result-resource"].textContent,
  detailsHidden: parts[".result-resource"].hidden,
  snippet: parts[".result-snippet"].innerHTML,
  snippetHidden: parts[".result-snippet"].hidden,
  linked: node.classes.has("result-linked"),
  link: parts[".result-title"].children[0].children.map((child) => child.textContent),
  thumb: {{
    tag: node.querySelector(".result-thumb").tagName,
    href: node.querySelector(".result-thumb").href,
    label: node.querySelector(".result-thumb").attributes["aria-label"],
    mime: node.querySelector(".result-thumb").dataset.mimetype,
  }},
}};
setResultNode(node, {{title: "Updated", url: "/content/manual/updated", lead: "Updated body."}});
const updatedThumb = {{
  href: node.querySelector(".result-thumb").href,
  label: node.querySelector(".result-thumb").attributes["aria-label"],
  mime: node.querySelector(".result-thumb").dataset.mimetype || null,
}};
setResultNode(node, {{title: "Other result", lead: "Extracted body."}});
console.log(JSON.stringify({{
  initial, updatedThumb, details: parts[".result-resource"].textContent,
  detailsHidden: parts[".result-resource"].hidden,
  snippet: parts[".result-snippet"].innerHTML,
  linked: node.classes.has("result-linked"),
  titleChildren: parts[".result-title"].children.length,
  thumbnailRemoved: !node.querySelector(".result-thumb"),
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["initial"], {
            "details": "Author: <script>unsafe()</script>",
            "detailsHidden": False,
            "snippet": "<p>Public description.</p>",
            "snippetHidden": False,
            "linked": True,
            "link": ["Count"],
            "thumb": {
                "tag": "a",
                "href": "/content/manual/count",
                "label": "Count",
                "mime": "text/html",
            },
        })
        self.assertEqual(payload["updatedThumb"], {
            "href": "/content/manual/updated", "label": "Updated", "mime": None,
        })
        self.assertEqual(payload["details"], "")
        self.assertTrue(payload["detailsHidden"])
        self.assertEqual(payload["snippet"], "<p>Extracted body.</p>")
        self.assertFalse(payload["linked"])
        self.assertEqual(payload["titleChildren"], 0)
        self.assertTrue(payload["thumbnailRemoved"])

    def test_result_card_taps_open_headers_but_not_the_desktop_snippet(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const location = new URL("http://example.test/?q=teaching");
let selection = "", handler, mobile = true;
const window = {{getSelection: () => ({{toString: () => selection}})}};
const matchMedia = () => ({{matches: mobile}});
const $ = () => ({{addEventListener: (_name, fn) => {{ handler = fn; }}}});
let opened = 0, forwarded = 0, prevented = 0;
const openArticleModal = () => {{ opened++; }};
{_function_source(source, "function isHtmlMimetype(", "function mediaBadge(")}
{_function_source(source, "function modifiedClick(", "function articleUrlValue(")}
{_function_source(source, '$("results").addEventListener("click"', '$("libraryGrid").addEventListener("click"')}
class Element {{
  constructor(kind) {{ this.kind = kind; }}
  closest(selector) {{
    if (selector === ".result-title a[href], a.result-thumb[href]") {{
      return this.kind === "title" || this.kind === "thumbnail" ? link : null;
    }}
    if (selector === ".result-snippet") return this.kind === "snippet" ? this : null;
    if (selector === ".result") return card;
    return this.kind === "control" ? this : null;
  }}
}}
const event = (kind, extra = {{}}) => ({{
  target: new Element(kind), button: 0,
  preventDefault() {{ prevented++; }}, ...extra,
}});
const link = {{
  href: "http://example.test/content/manual/One", dataset: {{mimetype: "text/html"}},
  click() {{ forwarded++; handler(event("title")); }},
}};
const card = {{querySelector: () => link}};
const counts = () => [opened, forwarded, prevented];
handler(event("snippet"));
handler(event("padding"));
const cardTaps = counts();
handler(event("control"));
selection = "selected preview text";
handler(event("snippet"));
selection = "";
handler(event("snippet", {{ctrlKey: true}}));
handler(event("title", {{ctrlKey: true}}));
handler(event("thumbnail", {{ctrlKey: true}}));
handler(event("title", {{button: 1}}));
const ignored = counts();
mobile = false;
handler(event("snippet"));
const desktopSnippet = counts();
handler(event("padding"));
const desktopPadding = counts();
mobile = true;
handler(event("title"));
const titleTap = counts();
handler(event("thumbnail"));
const thumbnailTap = counts();
link.dataset.mimetype = "application/pdf";
handler(event("snippet"));
const pdf = counts();
link.dataset.mimetype = "text/html";
link.href = "https://other.example/article";
handler(event("title"));
console.log(JSON.stringify({{cardTaps, ignored, desktopSnippet, desktopPadding, titleTap, thumbnailTap, pdf, external: counts()}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        self.assertEqual(json.loads(result.stdout), {
            "cardTaps": [2, 2, 2],
            "ignored": [2, 2, 2],
            "desktopSnippet": [2, 2, 2],
            "desktopPadding": [3, 3, 3],
            "titleTap": [4, 3, 4],
            "thumbnailTap": [5, 3, 5],
            "pdf": [5, 4, 5],
            "external": [5, 4, 5],
        })

    def test_directory_listing_filters_names_and_sorts_folders_first(self):
        page = directory_page("Files", "", "/article/files/", []).decode()
        listing_script = page.split("<script>", 1)[1].split("</script>", 1)[0]
        script = """
const makeRow = (name, kind, size, modified) => ({
  cells: [{textContent: name}, {textContent: kind}],
  dataset: {size, modified}, hidden: false,
});
const rows = [
  makeRow("notes.txt", "File", 100, 20),
  makeRow("Folder/", "Folder", 0, 10),
  makeRow("big.txt", "File", 10, 30),
];
const body = {
  rows: [...rows],
  appendChild(row) { this.rows = this.rows.filter((item) => item !== row); this.rows.push(row); },
};
const controls = {
  filter: {value: "", addEventListener(event, fn) {this[event] = fn;}},
  sort: {value: "name", addEventListener(event, fn) {this[event] = fn;}},
  count: {}, "no-matches": {},
};
const document = {
  querySelector: () => body,
  getElementById: (id) => controls[id],
};
""" + listing_script + """
const names = () => body.rows.filter((row) => !row.hidden).map((row) => row.cells[0].textContent);
controls.filter.value = "NOTES";
controls.filter.input();
const filtered = {names: names(), count: controls.count.textContent};
controls.filter.value = "missing";
controls.filter.input();
const noMatches = !controls["no-matches"].hidden && names().length === 0;
controls.filter.value = "";
controls.sort.value = "size";
controls.sort.change();
const bySize = names();
controls.sort.value = "name";
controls.sort.change();
console.log(JSON.stringify({filtered, noMatches, bySize, byName: names()}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        self.assertEqual(json.loads(result.stdout), {
            "filtered": {"names": ["notes.txt"], "count": "1 of 3 items"},
            "noMatches": True,
            "bySize": ["Folder/", "notes.txt", "big.txt"],
            "byName": ["Folder/", "big.txt", "notes.txt"],
        })

    def test_unindexed_files_link_to_listing_instead_of_random_or_dead_end(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const appPath = (path) => "/zimantic" + path;
const contentUrl = (key, path = "") =>
    appPath("/content/" + encodeURIComponent(key) + (path ? "/" + path : ""));
{_function_source(source, "function libraryHomepageUrl(", "function libraryLanguage(")}
const cases = [
    {{key: "files:modules:root", provider: "files", searchable: false}},
    {{key: "files:modules:root", provider: "files", searchable: true}},
    {{key: "files:modules:root", provider: "files", homepage: "index.html"}},
    {{key: "zim:books:manual", provider: "zim", searchable: true}},
    {{key: "zim:books:manual", provider: "zim", searchable: false}},
];
console.log(JSON.stringify(cases.map(libraryHomepageUrl)));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        self.assertEqual(json.loads(result.stdout), [
            "/zimantic/content/files%3Amodules%3Aroot",
            "/zimantic/content/files%3Amodules%3Aroot",
            "/zimantic/content/files%3Amodules%3Aroot/index.html",
            "/zimantic/random?source=zim%3Abooks%3Amanual",
            "",
        ])

    def test_all_sources_clears_empty_search_scope_and_url(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
class Node {{
  constructor() {{
    this.children = []; this.dataset = {{}}; this.hidden = true;
    this.handlers = {{}};
    this.classList = {{add() {{}}, remove() {{}}}};
  }}
  replaceChildren(...children) {{ this.children = children; }}
  setAttribute() {{}}
  querySelectorAll() {{ return this.children; }}
  addEventListener(name, handler) {{ this.handlers[name] = handler; }}
  focus() {{}}
}}
const elements = new Map();
const $ = (id) => {{
  if (!elements.has(id)) elements.set(id, new Node());
  return elements.get(id);
}};
const document = {{createElement: () => new Node()}};
const location = new URL("http://example.test/?collection=files%3Amodules");
const history = {{
  state: null,
  replaceState(state, _title, url) {{ location.href = new URL(url, location.href).href; }},
}};
const t = (text) => text;
const selectionValues = (values) => [...values];
let current = null, selectedSourceKeys = new Set(), selectedCollectionKey = null;
let sourceFilterShowAll = true, sourceFilterShowOther = true;
const setSelectedSources = (values) => {{ selectedSourceKeys = new Set(values); }};
const sourceDisplayName = (key) => key;
const sourceStemName = () => "";
const collectionDisplayName = (key) => key;
const renderSortControls = () => {{}};
{_function_source(source, "function renderSourceSelectionSummary()", "function renderFilterLine(")}
{_function_source(source, "function makeStateUrl(", "function makePageUrl(")}
{_function_source(source, '$("scopeLineReset").addEventListener("click"', '$("articleModal").addEventListener("click"')}
const results = [];
for (const control of ["scopeLineReset"]) {{
  for (const scope of ["collection", "source", "both"]) {{
    selectedCollectionKey = scope === "source" ? null : "files:modules";
    selectedSourceKeys = new Set(scope === "collection" ? [] : ["files:a"]);
    sourceFilterShowAll = true;
    sourceFilterShowOther = true;
    location.href = "http://example.test/?collection=files%3Amodules&source=files%3Aa";
    renderSourceSelectionSummary();
    renderScopeLine();
    const visibleBefore = !$("scopeLine").hidden && !$("sourceFilters").hidden;
    $(control).handlers.click();
    results.push({{
      control, scope, visibleBefore,
      selected: [...selectedSourceKeys], collection: selectedCollectionKey,
      url: location.pathname + location.search,
      scopeHidden: $("scopeLine").hidden, scopeText: $("scopeLineText").textContent,
      resetHidden: $("scopeLineReset").hidden,
      selectionHidden: $("sourceFilters").hidden,
      showAll: sourceFilterShowAll, showOther: sourceFilterShowOther,
    }});
  }}
}}
console.log(JSON.stringify(results));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        for payload in json.loads(result.stdout):
            with self.subTest(control=payload["control"], scope=payload["scope"]):
                self.assertTrue(payload["visibleBefore"])
                self.assertEqual(payload["selected"], [])
                self.assertIsNone(payload["collection"])
                self.assertEqual(payload["url"], "/")
                self.assertTrue(payload["scopeHidden"])
                self.assertEqual(payload["scopeText"], "")
                self.assertTrue(payload["resetHidden"])
                self.assertTrue(payload["selectionHidden"])
                self.assertFalse(payload["showAll"])
                self.assertFalse(payload["showOther"])

    def _initialize_search_page(self, mode="empty"):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
class Node {{
  constructor() {{
    this.children = []; this.dataset = {{}}; this.hidden = true; this.value = "";
    this.classList = {{add() {{}}, remove() {{}}}};
  }}
  replaceChildren(...children) {{ this.children = children; }}
  setAttribute() {{}}
  focus() {{}}
}}
const elements = new Map();
const $ = (id) => {{
  if (!elements.has(id)) elements.set(id, new Node());
  return elements.get(id);
}};
const document = {{
  createElement: () => new Node(),
  querySelectorAll: () => [],
  body: {{classList: {{toggle() {{}}}}}},
}};
const location = new URL("http://example.test/?collection=files%3Amodules");
const t = (text) => text;
const appPath = (path) => path;
const mode = {json.dumps(mode)};
const state = {{
  question: ["empty", "empty-query", "failure", "browse"].includes(mode) ? "" : "original",
  browse: mode === "browse" ? "rare" : mode === "empty-query" ? "diverse" : undefined,
  seed: ["browse", "empty-query"].includes(mode) ? 0 : undefined,
  selectedSources: mode === "unknown-source" ? ["missing"] : [],
  scopeSources: mode === "unknown-source" ? ["missing"] : [],
  collection: mode === "unknown-collection" ? "missing"
    : mode === "unknown-source" ? null : "files:modules",
  page: 0, modal: null,
}};
const normalizePageParam = () => {{}};
const normalizeSortParam = () => {{}};
const readLocationState = () => state;
const applyLocationScope = (state) => {{
  selectedCollectionKey = state.collection;
  selectedSourceKeys = new Set(state.selectedSources);
}};
const availableBrowseModes = new Set(["random", "rare", "outliers", "diverse", "recency"]);
const defaultBrowseMode = () => "diverse";
const setSelectedSources = (values) => {{ selectedSourceKeys = new Set(values); }};
const makeStateUrl = (question, selected, page, modal, scope, collection, browse, seed) => {{
  const params = new URLSearchParams();
  params.set("q", question);
  if (collection) params.set("collection", collection);
  for (const source of selected || []) params.append("source", source);
  if (browse) params.set("sort", browse);
  if (page > 0) params.set("page", String(page + 1));
  if (modal) params.set("modal", modal);
  return "/?" + params.toString();
}};
const renderSortControls = () => {{}};
const renderSourceFilters = () => {{}};
const replaceQueryParam = () => {{}};
let replaced = null;
const history = {{state: null, replaceState(_state, _title, url) {{ replaced = String(url); }}}};
const setSettingsOpen = () => {{}};
const syncArticleModal = () => {{}};
const readCollectionCache = () => null;
const readCanonicalCache = () => null;
const writeJsonCache = () => {{}};
const isCanonicalCollectionRecord = () => true;
const SOURCE_CACHE_KEY = "sources";
const COLLECTION_CACHE_KEY = "collections";
let sources = [], collections = [], current = null, selectedCollectionKey = null;
let selectedSourceKeys = new Set(), searchSequence = 0;
const searches = [], requests = [];
const runSearch = (...args) => {{ searches.push(args); searchSequence++; }};
const collectionDisplayName = (key) =>
  collections.find((record) => record.key === key)?.label || key;
const sourceDisplayName = (key) => key;
const sourceStemName = () => "";
const deferred = () => {{
  let resolve, reject;
  const promise = new Promise((yes, no) => {{ resolve = yes; reject = no; }});
  return {{promise, resolve, reject}};
}};
const config = deferred(), discovery = deferred(), metadata = deferred();
const loadConfig = () => {{ requests.push("config"); return config.promise; }};
const fetchCachedJson = (path, key, options) => {{
  requests.push([path, options.limit]); return discovery.promise;
}};
const fetchPagedJson = (path, options) => {{
  requests.push([path, options.limit]); return metadata.promise;
}};
const applyDiscoveryData = (payload) => {{
  sources = payload; renderSourceSelectionSummary();
}};
{_function_source(source, "function renderSourceSelectionSummary()", "function renderFilterLine(")}
{_function_source(source, "async function loadSearchData()", "async function loadConfig()")}
{_function_source(source, "async function initialize()", '$("localePicker")?.addEventListener')}
const snapshot = () => ({{
  scope: $("scopeLineText").textContent, scopeHidden: $("scopeLine").hidden,
  resetHidden: $("scopeLineReset").hidden,
  selectionHidden: $("sourceFilters").hidden,
  labels: $("sourceFilterOptions").children.map((button) => button.textContent),
  selectionStatus: $("sourceFilterStatus").textContent,
  loading: !$("startupStatus").hidden, question: $("question").value,
  notice: $("notice").textContent, status: $("status").textContent,
  replaced,
}});
(async () => {{
  const ready = initialize();
  const early = snapshot();
  const earlyRequests = [...requests];
  metadata.resolve([{{key: "files:modules", provider: "files", label: "Modules"}}]);
  await new Promise((resolve) => setImmediate(resolve));
  const labeled = snapshot();
  if (mode === "edited") $("question").value = "new query";
  if (mode === "searched") runSearch("new query", [], 0, [], "files:modules");
  config.resolve();
  if (mode === "failure") discovery.reject(new Error("offline"));
  else discovery.resolve({{payload: mode === "unknown-source"
    ? [{{key: "manual", provider: "files", label: "Manual"}}] : [], stale: false}});
  await ready;
  console.log(JSON.stringify({{
    early, earlyRequests, labeled, final: snapshot(), searches, status: $("status").textContent,
  }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        return json.loads(result.stdout)

    def test_collection_scope_is_visible_before_parallel_startup_requests_finish(self):
        payload = self._initialize_search_page()
        self.assertEqual(payload["early"]["scope"], "Searching collection: files:modules")
        self.assertFalse(payload["early"]["scopeHidden"])
        self.assertFalse(payload["early"]["selectionHidden"])
        self.assertTrue(payload["early"]["loading"])
        self.assertEqual(
            payload["earlyRequests"],
            ["config", ["/sources", 200], ["/collections.json", 200]],
        )
        self.assertEqual(payload["labeled"]["scope"], "Searching collection: Modules")
        self.assertEqual(payload["labeled"]["labels"], ["Modules"])
        self.assertFalse(payload["labeled"]["resetHidden"])
        self.assertEqual(
            payload["labeled"]["selectionStatus"],
            "1 selected collection. Type a query to search it.",
        )
        self.assertTrue(payload["labeled"]["loading"])
        self.assertFalse(payload["final"]["loading"])

    def test_startup_loading_clears_on_failure_without_losing_collection_scope(self):
        payload = self._initialize_search_page("failure")
        self.assertFalse(payload["final"]["loading"])
        self.assertFalse(payload["final"]["scopeHidden"])
        self.assertEqual(payload["final"]["scope"], "Searching collection: Modules")
        self.assertEqual(payload["status"], "Could not load sources: offline")

    def test_startup_query_is_visible_early_and_not_replayed_over_user_edits(self):
        for mode in ("query", "edited", "searched"):
            with self.subTest(mode=mode):
                payload = self._initialize_search_page(mode)
                self.assertEqual(payload["early"]["question"], "original")
                if mode == "query":
                    self.assertEqual(
                        payload["searches"],
                        [["original", [], 0, [], "files:modules", None, None]],
                    )
                elif mode == "edited":
                    self.assertEqual(payload["final"]["question"], "new query")
                    self.assertEqual(payload["searches"], [])
                else:
                    self.assertEqual(
                        payload["searches"], [["new query", [], 0, [], "files:modules"]]
                    )

    def test_unknown_source_clears_the_phantom_scope_without_searching(self):
        payload = self._initialize_search_page("unknown-source")
        self.assertEqual(payload["searches"], [])
        self.assertEqual(payload["final"]["status"], "Search failed")
        self.assertIn("Unknown source: missing", payload["final"]["notice"])
        self.assertTrue(payload["final"]["scopeHidden"])
        self.assertTrue(payload["final"]["selectionHidden"])
        self.assertEqual(payload["final"]["labels"], [])
        self.assertNotIn("source=", payload["final"]["replaced"])
        self.assertIn("q=original", payload["final"]["replaced"])

    def test_unknown_collection_clears_the_phantom_scope_without_searching(self):
        payload = self._initialize_search_page("unknown-collection")
        self.assertEqual(payload["searches"], [])
        self.assertIn("Unknown collection: missing", payload["final"]["notice"])
        self.assertTrue(payload["final"]["scopeHidden"])
        self.assertEqual(payload["final"]["labels"], [])
        self.assertNotIn("collection=", payload["final"]["replaced"])
        self.assertIn("q=original", payload["final"]["replaced"])

    def test_startup_requires_a_query_parameter_or_explicit_order(self):
        self.assertEqual(self._initialize_search_page("empty")["searches"], [])
        self.assertEqual(
            self._initialize_search_page("empty-query")["searches"],
            [["", [], 0, [], "files:modules", "diverse", 0]],
        )
        self.assertEqual(
            self._initialize_search_page("browse")["searches"],
            [["", [], 0, [], "files:modules", "rare", 0]],
        )

    def test_sort_state_preserves_seed_scope_paging_modal_and_new_queries(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const location = new URL("http://example.test/?q=");
const MAX_QUERY_LENGTH = 4096;
const BROWSE_MODES = new Set(["random", "rare", "outliers", "diverse", "recency"]);
const defaultBrowseMode = () => "diverse";
const selectionValues = (values) => [...(values || [])];
let current = null, selectedSourceKeys = new Set(["manual"]);
const setSelectedSources = (values) => {{ selectedSourceKeys = new Set(values); }};
const searches = [], pages = [];
const runSearch = (...args) => searches.push(args);
const loadPage = (page) => pages.push(page);
{_function_source(source, "function makeStateUrl(", "function makePageUrl(")}
{_function_source(source, "function readLocationState()", "function syncArticleModal(")}
{_function_source(source, "function applySourceSelection(", "function makeStateUrl(")}
const empty = readLocationState();
location.href = "http://example.test/?sort=random&seed=7&source=manual";
const browsing = readLocationState();
current = {{
  question: "", browse: "random", seed: 7, searchAll: false,
  searchKeys: ["manual"], scopeKeys: ["manual"], searchCollection: null,
}};
const paged = makeStateUrl("", selectedSourceKeys, 1);
const modal = makeStateUrl("", selectedSourceKeys, 1, "/content/manual/topic");
const query = makeStateUrl("topic", selectedSourceKeys, 0);
const rare = makeStateUrl("", [], 0, null, [], "files:modules", "rare", 99);
applySourceSelection("", ["other"], 0, ["other"], null);
applySourceSelection("", ["manual"], 1, ["manual"], null);
location.href = "http://example.test/?q=topic&seed=7";
const queryState = readLocationState();
console.log(JSON.stringify({{empty, browsing, paged, modal, query, rare, searches, pages, queryState}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["empty"]["browse"], "diverse")
        self.assertEqual(payload["browsing"]["browse"], "random")
        self.assertEqual(payload["browsing"]["seed"], 7)
        self.assertEqual(
            payload["paged"], "/?q=&sort=random&seed=7&source=manual&page=2"
        )
        self.assertEqual(
            payload["modal"],
            "/?q=&sort=random&seed=7&source=manual&page=2&modal=%2Fcontent%2Fmanual%2Ftopic",
        )
        self.assertEqual(payload["query"], "/?q=topic&sort=random&seed=7&source=manual")
        self.assertEqual(payload["rare"], "/?q=&sort=rare&collection=files%3Amodules")
        self.assertEqual(
            payload["searches"], [["", ["other"], 0, ["other"], None, "random", 7, {}]]
        )
        self.assertEqual(payload["pages"], [1])
        self.assertIsNone(payload["queryState"]["browse"])

    def test_sorted_pagination_uses_the_search_endpoint_and_original_seed(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
let current = {{
  question: "", browse: "random", seed: 7, searchAll: false,
  searchCollection: "files:modules", scopeKeys: [], searchKeys: [],
}};
let pageController = null, requestController = null, searchSequence = 1;
const PAGE_SIZE = 10, debugMode = false, BUSY_RETRY_LIMIT = 0;
const requests = [];
const t = (text) => text;
const $ = () => ({{textContent: ""}});
const appPath = (path) => "/zimantic" + path;
const renderView = () => {{}};
const searchStatusText = () => "";
const announceSearchStatus = () => {{}};
const syncPage = () => {{}};
const window = {{scrollTo() {{}}}};
const fetch = async (url) => {{
  requests.push(url);
  return {{
    ok: true, json: async () => [{{id: "item"}}],
    headers: new Map([["X-Total-Count", "20"], ["X-Page", "1"]]),
  }};
}};
const dedupeParams = () => {{}};
{_function_source(source, "function searchScopeParams(", "function sourceFilterLayout(")}
{_function_source(source, "async function loadPage(", "function applySourceSelection(")}
loadPage(1).then(() => console.log(JSON.stringify({{requests, current}})));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["requests"],
            ["/zimantic/search?q=&sort=random&seed=7&limit=10&offset=10&collection=files%3Amodules"],
        )
        self.assertEqual(payload["current"]["page"], 1)
        self.assertEqual(payload["current"]["pageItems"], [{"id": "item"}])

    def test_sort_controls_visibility_tracks_results_and_relevance_availability(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const elements = {{
  question: {{value: ""}},
  sortControls: {{hidden: true}},
  sortSelect: {{options: ["relevance", "random", "rare"]
    .map((value) => ({{value}}))}},
}};
const $ = (id) => elements[id];
const availableBrowseModes = new Set(["random", "rare"]);
let current = null;
{_function_source(source, "function defaultBrowseMode()", "function setSettingsOpen(")}
const states = [
  {{question: "", current: null}},
  {{question: "topic", current: {{pageItems: [], streaming: true}}}},
  {{question: "topic", current: {{pageItems: [{{}}], streaming: true}}}},
  {{question: "", current: {{pageItems: [{{}}], browse: "rare"}}}},
  {{question: "missing", current: {{pageItems: []}}}},
  {{question: "", current: null}},
];
console.log(JSON.stringify(states.map((state) => {{
  elements.question.value = state.question;
  current = state.current;
  renderSortControls();
  return {{
    hidden: elements.sortControls.hidden,
    relevanceHidden: elements.sortSelect.options[0].hidden,
  }};
}})));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        self.assertEqual(json.loads(result.stdout), [
            {"hidden": True, "relevanceHidden": True},
            {"hidden": True, "relevanceHidden": False},
            {"hidden": False, "relevanceHidden": False},
            {"hidden": False, "relevanceHidden": True},
            {"hidden": True, "relevanceHidden": False},
            {"hidden": True, "relevanceHidden": True},
        ])

    def test_sort_selector_submits_empty_and_text_queries_without_losing_scope(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const location = new URL("http://example.test/?collection=files%3Amodules");
const history = {{
  pushState(_state, _title, url) {{ location.href = new URL(url, location.href).href; }},
}};
const elements = {{
  question: {{value: ""}},
  sortControls: {{hidden: true}},
  sortSelect: {{value: "", options: ["relevance", "random", "rare", "outliers", "diverse", "recency"]
    .map((value) => ({{value}}))}},
}};
const $ = (id) => elements[id];
const availableBrowseModes = new Set(["random", "rare", "outliers", "diverse", "recency"]);
const selectionValues = (values) => [...(values || [])];
const selectedSourceKeys = new Set(["manual"]);
const selectedCollectionKey = "files:modules";
const nextSearchScope = (selected) => selected;
const MAX_QUERY_LENGTH = 4096;
const BROWSE_MODES = new Set(["random", "rare", "outliers", "diverse", "recency"]);
const commitAutoSearchHistory = () => {{}};
const currentUrlValue = () => location.pathname + location.search;
const searches = [];
let current = null;
const runSearch = (...args) => {{
  searches.push(args);
  current = {{question: args[0], browse: args[5], seed: args[6]}};
}};
Math.random = () => 7 / 2 ** 32;
{_function_source(source, "function defaultBrowseMode()", "function setSettingsOpen(")}
{_function_source(source, "function makeStateUrl(", "function makePageUrl(")}
{_function_source(source, "function submitSearch(", '$("question").addEventListener("input"')}
renderSortControls();
const emptyDefault = $("sortSelect").value;
const relevanceDisabled = $("sortSelect").options[0].disabled;
submitSearch(true);
const emptyUrl = currentUrlValue();
$("question").value = "topic";
renderSortControls();
const typedDefault = $("sortSelect").value;
$("sortSelect").value = "random";
submitSearch(true);
renderSortControls();
const textUrl = currentUrlValue(), textSort = $("sortSelect").value;
$("sortSelect").value = "relevance";
submitSearch(true);
const relevanceUrl = currentUrlValue();
current = null;
$("question").value = "";
availableBrowseModes.delete("outliers");
renderSortControls();
console.log(JSON.stringify({{
  emptyDefault, relevanceDisabled, emptyUrl, typedDefault, textUrl, textSort, relevanceUrl, searches,
  fastDefault: $("sortSelect").value,
  semanticHidden: $("sortSelect").options.filter((option) =>
    ["outliers"].includes(option.value)
  ).every((option) => option.hidden && option.disabled),
  additionalModesVisible: $("sortSelect").options.filter((option) =>
    ["diverse", "recency"].includes(option.value)
  ).every((option) => !option.hidden && !option.disabled),
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["emptyDefault"], "diverse")
        self.assertTrue(payload["relevanceDisabled"])
        self.assertEqual(payload["emptyUrl"], "/?q=&sort=diverse&collection=files%3Amodules&source=manual")
        self.assertEqual(payload["typedDefault"], "relevance")
        self.assertEqual(payload["textUrl"], "/?q=topic&sort=random&seed=7&collection=files%3Amodules&source=manual")
        self.assertEqual(payload["textSort"], "random")
        self.assertEqual(payload["relevanceUrl"], "/?q=topic&collection=files%3Amodules&source=manual")
        self.assertEqual(payload["searches"], [
            ["", ["manual"], 0, ["manual"], "files:modules", "diverse", 0],
            ["topic", ["manual"], 0, ["manual"], "files:modules", "random", 7],
            ["topic", ["manual"], 0, ["manual"], "files:modules", None, 0],
        ])
        self.assertEqual(payload["fastDefault"], "diverse")
        self.assertTrue(payload["semanticHidden"])
        self.assertTrue(payload["additionalModesVisible"])

    def test_history_distinguishes_empty_search_from_home_and_restores_sort(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const location = new URL("http://example.test/");
const MAX_QUERY_LENGTH = 4096;
const BROWSE_MODES = new Set(["random", "rare", "outliers", "diverse", "recency"]);
const defaultBrowseMode = () => "diverse";
const appPath = (path) => path;
const elements = {{articleModal: {{hidden: true}}, question: {{value: ""}}}};
const $ = (id) => elements[id];
const history = {{state: null}};
const cancelAutoSearchHistory = () => {{}};
const syncArticleModal = () => {{}};
const syncLibraryInfo = () => {{}};
const searches = [];
let resets = 0, current = null;
const resetResults = () => {{ resets++; }};
const runSearch = (...args) => searches.push(args);
const window = {{addEventListener(_name, fn) {{ this.popstate = fn; }}}};
{_function_source(source, "function readLocationState()", "function syncArticleModal(")}
{_function_source(source, 'window.addEventListener("popstate"', "function applyDiscoveryData(")}
window.popstate();
location.search = "?q=";
window.popstate();
location.search = "?q=topic&sort=random&seed=7&source=manual&page=2";
window.popstate();
console.log(JSON.stringify({{resets, searches}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        self.assertEqual(json.loads(result.stdout), {
            "resets": 1,
            "searches": [
                ["", [], 0, [], None, "diverse", 0],
                ["topic", ["manual"], 1, ["manual"], None, "random", 7],
            ],
        })

    def test_library_filter_matches_word_prefixes_not_interior_substrings(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
{_function_source(source, "function matchesLibraryTerms(", "function libraryCollectionText(")}
console.log(JSON.stringify([
  matchesLibraryTerms("Illustrated guide", "ill"),
  matchesLibraryTerms("Illustrated guide", "ted"),
  matchesLibraryTerms("Guide to illustrated topics", "ted"),
  matchesLibraryTerms("Café guide", "café"),
  matchesLibraryTerms("Café guide", "fé"),
  matchesLibraryTerms("Illustrated guide", "-ted"),
]));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        self.assertEqual(json.loads(result.stdout), [True, False, False, True, False, True])

    def test_library_filter_options_hide_empty_facets_and_name_remote_kiwix(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const elements = {{
  libraryProviderFilter: {{options: [
    {{value: ""}}, {{value: "zim"}}, {{value: "kiwix"}},
  ]}},
  libraryMediaFilter: {{options: [
    {{value: ""}}, {{value: "text"}}, {{value: "video"}},
  ]}},
}};
const $ = (id) => elements[id];
const t = (text) => text;
const libraryLanguageOptions = new Set(["old"]);
let languagesRendered = false;
const renderLibraryLanguageOptions = () => {{ languagesRendered = true; }};
{_function_source(source, "function setAvailableLibraryFilterOptions(", "function libraryLanguageName(")}
{_function_source(source, "function providerLabel(", "function mediaTypeLabels(")}
setAvailableLibraryFilterOptions({{
  providers: ["zim"], media_types: ["text"], languages: ["ENG", ""],
}});
console.log(JSON.stringify({{
  providers: elements.libraryProviderFilter.options.map((option) => option.hidden || false),
  media: elements.libraryMediaFilter.options.map((option) => option.hidden || false),
  languages: [...libraryLanguageOptions],
  languagesRendered,
  kiwix: providerLabel("kiwix"),
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        self.assertEqual(
            json.loads(result.stdout),
            {
                "providers": [False, False, True],
                "media": [False, False, True],
                "languages": ["eng"],
                "languagesRendered": True,
                "kiwix": "Remote Kiwix",
            },
        )

    def test_library_language_options_use_discovered_codes_and_preserve_selection(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
class Node {{
  constructor() {{ this.children = []; this.dataset = {{}}; this.value = ""; }}
  appendChild(child) {{ this.children.push(child); }}
  replaceChildren() {{ this.children = []; }}
}}
const document = {{createElement: () => new Node()}};
const elements = {{
  libraryLanguageFilter: new Node(), localePicker: {{value: "en"}},
}};
const $ = (id) => elements[id];
const t = (text) => text;
const libraryLanguageOptions = new Set();
const libraryLanguageDisplayNames = new Map();
let librarySources = [{{language: "eng,spa"}}, {{language: "fra,deu"}}];
const sortLibrarySources = (records) => records;
{_function_source(source, "function libraryLanguageCodes(", "function infoUrl(")}
{_function_source(source, "function renderLibraryLanguageOptions()", "function renderLibrary()")}
{_function_source(source, "function refreshLibraryLanguageOptions()", "function restoreLibraryCache()")}
setLibraryLanguageFilter("eng");
refreshLibraryLanguageOptions();
const initial = {{
  selected: elements.libraryLanguageFilter.value,
  options: elements.libraryLanguageFilter.children.map((option) => [
    option.value, option.textContent,
  ]),
}};
elements.localePicker.value = "es";
refreshLibraryLanguageOptions();
const spanish = {{
  options: elements.libraryLanguageFilter.children.map((option) => option.textContent),
}};
setLibraryLanguageFilter("fra");
console.log(JSON.stringify({{
  initial, spanish, selected: elements.libraryLanguageFilter.value,
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["initial"]["selected"], "eng")
        self.assertEqual(
            payload["initial"]["options"],
            [
                ["", "All languages"],
                ["eng", "English (ENG)"],
                ["fra", "French (FRA)"],
                ["deu", "German (DEU)"],
                ["spa", "Spanish (SPA)"],
            ],
        )
        self.assertEqual(
            payload["spanish"]["options"],
            ["All languages", "alemán (DEU)", "español (SPA)", "francés (FRA)", "inglés (ENG)"],
        )
        self.assertEqual(payload["selected"], "fra")

    def test_library_language_names_fall_back_when_browser_lookup_is_unavailable(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const elements = {{localePicker: {{value: "en"}}}};
const $ = (id) => elements[id];
const libraryLanguageDisplayNames = new Map();
{_function_source(source, "function libraryLanguageName(", "function setLibraryLanguageFilter(")}
const results = [];
for (const mode of ["code", "uppercase", "missing", "throws", "constructor", "unavailable"]) {{
  Intl.DisplayNames = mode === "unavailable" ? undefined : class {{
    constructor() {{ if (mode === "constructor") throw new RangeError(); }}
    of(code) {{
      if (mode === "throws") throw new RangeError();
      if (mode === "missing") return undefined;
      return mode === "uppercase" ? code.toUpperCase() : code;
    }}
  }};
  const labels = [];
  for (const locale of ["en", "es"]) {{
    elements.localePicker.value = locale;
    libraryLanguageDisplayNames.clear();
    labels.push(["LAO", "ewe", "zzz"].map(libraryLanguageLabel));
  }}
  results.push(labels);
}}
Intl.DisplayNames = class {{ of() {{ return "Browser name"; }} }};
libraryLanguageDisplayNames.clear();
console.log(JSON.stringify({{results, native: libraryLanguageLabel("lao")}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["results"],
            [
                [
                    ["Lao (LAO)", "Ewe (EWE)", "ZZZ"],
                    ["lao (LAO)", "ewé (EWE)", "ZZZ"],
                ]
            ]
            * 6,
        )
        self.assertEqual(payload["native"], "Browser name (LAO)")

    def test_library_renders_all_loaded_sources_and_filters_source_media(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
class Node {{
  constructor() {{ this.children = []; this.dataset = {{}}; this.classList = {{add() {{}}}}; }}
  append(...children) {{ this.children.push(...children); }}
  appendChild(child) {{ this.append(child); }}
  replaceChildren() {{ this.children = []; }}
  addEventListener() {{}}
  setAttribute() {{}}
}}
const document = {{createElement: () => new Node()}};
const elements = {{
  libraryGrid: new Node(), libraryStatus: new Node(),
  libraryScopeLine: new Node(), libraryScopeLineText: new Node(),
  libraryFilter: {{value: ""}}, libraryProviderFilter: {{value: ""}},
  libraryMediaFilter: {{value: ""}}, libraryLanguageFilter: {{value: ""}},
  localePicker: {{value: "en"}},
}};
const $ = (id) => elements[id];
const t = (text) => text;
const libraryLanguageDisplayNames = new Map();
const libraryTitle = (source) => source.key;
const libraryHomepageUrl = () => "";
const sourceStemName = () => "";
const collectionLabelFor = (source) => source.collection?.label || "";
const formatLibrarySize = (bytes) => `${{Math.round(Number(bytes) / 1024 / 1024)}} MB`;
const libraryLanguage = (source) => String(source.language || "").toUpperCase();
const librarySearchText = (source) => source.key;
const collectionDisplayName = (collection) => collection.title;
const libraryLanguageCodes = (source) =>
  String(source?.language || "").split(",").map((part) => part.trim().toLowerCase()).filter(Boolean);
const collections = [
  {{key: "small", title: "Small", provider: "files", source_count: 766}},
  {{key: "large", title: "Large", provider: "files", source_count: 1001, media_types: ["other"]}},
];
const libraryCollections = collections;
const makeSources = (count, media) => Array.from({{length: count}}, (_, i) => ({{
  key: `${{media}}-${{i}}`, provider: "files", media_types: [media],
  collection: {{label: `${{media}} collection`}},
  language: i === 1 ? "fra" : "eng", date: "2025-08-27",
  size: 109 * 1024 * 1024, item_count: 34448,
  description: "Short <summary>", long_description: "Long description",
}}));
const small = makeSources(766, "text"), large = makeSources(1001, "video");
const libraryCollectionPages = new Map([
  ["small", {{sources: small, total: 766, hasMore: false}}],
  ["large", {{sources: large.slice(0, 1000), total: 1001, hasMore: true, loading: false}}],
]);
const librarySources = [...small, ...large.slice(0, 1000)];
const libraryCardCache = new WeakMap();
{_function_source(source, "function providerLabel(", "function collectionFor(")}
{_function_source(source, "function libraryFilterTerms()", "function resultKey(")}
const find = (node, cls) => node.className === cls ? node :
  node.children.map((child) => find(child, cls)).find(Boolean);
const summarize = () => elements.libraryGrid.children.map((section) => ({{
  key: section.dataset.libraryCollection,
  count: find(section, "library-grid library-collection-grid").children.length,
  meta: find(section, "library-collection-meta").textContent,
  collection: find(section, "library-card-collection").textContent,
  details: find(section, "library-card-details").textContent,
  description: find(section, "library-card-description").textContent,
  buttons: find(section, "library-collection-actions").children.map((button) => button.textContent),
  note: find(section, "library-collection-note")?.textContent,
}}));
renderLibrary();
const initial = summarize();
const firstCard = find(elements.libraryGrid.children[0], "library-grid library-collection-grid").children[0];
const initiallyHidden = elements.libraryScopeLine.hidden;
libraryCollectionPages.get("large").loading = true;
renderLibrary();
const reusedCard = find(elements.libraryGrid.children[0], "library-grid library-collection-grid").children[0] === firstCard;
const loadingDisabled = find(elements.libraryGrid.children[1], "library-collection-actions").children[1].disabled;
libraryCollectionPages.set("large", {{sources: large, total: 1001, hasMore: false}});
renderLibrary();
const appended = summarize();
elements.libraryMediaFilter.value = "video";
elements.libraryLanguageFilter.value = "eng";
libraryCollectionPages.set("large", {{sources: large.slice(0, 2), total: 11, hasMore: true}});
renderLibrary();
console.log(JSON.stringify({{
  initial, initiallyHidden, reusedCard, loadingDisabled, appended, filtered: summarize(),
  scopeText: elements.libraryScopeLineText.textContent,
  scopeHidden: elements.libraryScopeLine.hidden,
  status: elements.libraryStatus.textContent,
  placeholder: elements.libraryFilter.placeholder,
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        self.assertEqual([item["count"] for item in payload["initial"]], [766, 1000])
        self.assertEqual(payload["initial"][0]["description"], "Short <summary>")
        self.assertEqual(payload["initial"][0]["buttons"], ["Search collection"])
        self.assertEqual(
            payload["initial"][1]["buttons"], ["Search collection", "Load more sources"]
        )
        self.assertEqual(payload["initial"][1]["meta"], "Files · 1,001 sources")
        self.assertEqual(
            payload["initial"][1]["collection"], "Files · video collection"
        )
        self.assertEqual(
            payload["initial"][1]["details"],
            "English (ENG) · 2025-08-27\n34,448 items · 109 MB",
        )
        self.assertEqual(
            payload["initial"][1]["note"], "Showing 1,000 of 1,001 sources."
        )
        self.assertTrue(payload["loadingDisabled"])
        self.assertTrue(payload["reusedCard"])
        self.assertEqual(payload["appended"][1]["count"], 1001)
        self.assertEqual(payload["appended"][1]["buttons"], ["Search collection"])
        self.assertEqual([item["key"] for item in payload["filtered"]], ["large"])
        self.assertEqual(payload["filtered"][0]["note"], "Showing 1 of 11 sources.")
        self.assertEqual(payload["filtered"][0]["meta"], "Files · 11 sources")
        self.assertTrue(payload["initiallyHidden"])
        self.assertFalse(payload["scopeHidden"])
        self.assertEqual(payload["filtered"][0]["count"], 1)
        self.assertEqual(
            payload["scopeText"],
            "Filtered by: Media type: Video · Language: English (ENG)",
        )
        self.assertEqual(payload["status"], "1 collection · 1 loaded source")
        self.assertEqual(payload["placeholder"], "Filter 1 source")

    def test_library_hides_empty_kolibri_cards_including_cached_collections(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const $ = () => ({{value: ""}});
const providerLabel = (provider) => provider;
const librarySearchText = (source) => source.key;
const collectionByKey = () => null;
const collections = [
  {{key: "khan", provider: "kolibri"}},
  {{key: "files", provider: "files"}},
];
let libraryCollections = collections;
const makeSource = (key, provider, item_count) => ({{
  key, provider, item_count, searchable: false,
}});
const libraryCollectionPages = new Map([
  ["khan", {{sources: [
    makeSource("empty", "kolibri", 0),
    makeSource("math", "kolibri", 2),
    makeSource("unknown", "kolibri", null),
  ]}}],
  ["files", {{sources: [makeSource("file", "files", 0)]}}],
]);
{_function_source(source, "function libraryFilterTerms()", "function libraryCardsFor(")}
{_function_source(source, "function libraryCollectionViews()", "function libraryStateMessage(")}
const summarize = () => libraryCollectionViews().map((view) => ({{
  key: view.key, cards: view.members.map((source) => source.key),
}}));
const configured = summarize();
libraryCollections = [collections[1]];
const cached = summarize();
console.log(JSON.stringify({{configured, cached}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        khan = {"key": "khan", "cards": ["math", "unknown"]}
        files = {"key": "files", "cards": ["file"]}
        self.assertEqual(payload["configured"], [khan, files])
        self.assertEqual(payload["cached"], [files, khan])

    def test_library_scope_reset_clears_all_filters_and_pending_reload(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const location = new URL("http://example.test/library?keep=1&filter=old&provider=zim&media=audio&language=fra");
const elements = {{
  libraryScopeLine: {{hidden: true}}, libraryScopeLineText: {{textContent: ""}},
  libraryFilter: {{value: " Trees "}}, libraryProviderFilter: {{value: "files"}},
  libraryMediaFilter: {{value: "video"}}, libraryLanguageFilter: {{value: "fra"}},
  localePicker: {{value: "en"}},
}};
const $ = (id) => elements[id];
const appPath = (path) => path;
const t = (text) => text;
const libraryLanguageDisplayNames = new Map();
const replaceQueryParam = (key, value) => {{
  if (value) location.searchParams.set(key, value);
  else location.searchParams.delete(key);
}};
let libraryFilterTimer = null, reloads = 0;
const timers = new Map();
const setTimeout = (callback) => {{ timers.set(1, callback); return 1; }};
const clearTimeout = (id) => timers.delete(id);
const loadLibraryData = () => {{ reloads++; }};
{_function_source(source, "function providerLabel(", "function collectionFor(")}
{_function_source(source, "function updateLibraryFilterUrl()", "function sortLibrarySources(")}
{_function_source(source, "function renderLibraryScopeLine()", "function renderLibrary()")}
scheduleLibraryReload();
const active = {{
  hidden: elements.libraryScopeLine.hidden, text: elements.libraryScopeLineText.textContent,
  params: Object.fromEntries(location.searchParams), reloads, timers: timers.size,
}};
resetLibraryFilters();
console.log(JSON.stringify({{
  active, reset: {{
    hidden: elements.libraryScopeLine.hidden, text: elements.libraryScopeLineText.textContent,
    values: [
      "libraryFilter", "libraryProviderFilter", "libraryMediaFilter", "libraryLanguageFilter",
    ].map((id) => elements[id].value),
    params: Object.fromEntries(location.searchParams), reloads, timers: timers.size, libraryFilterTimer,
  }},
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        self.assertFalse(payload["active"]["hidden"])
        self.assertEqual(
            payload["active"]["text"],
            "Filtered by: Provider: Files · Media type: Video · Language: French (FRA) · Filter: Trees",
        )
        self.assertEqual(
            payload["active"]["params"],
            {
                "keep": "1",
                "filter": "Trees",
                "provider": "files",
                "media": "video",
                "language": "fra",
            },
        )
        self.assertEqual(payload["active"]["reloads"], 0)
        self.assertEqual(payload["active"]["timers"], 1)
        self.assertEqual(
            payload["reset"],
            {
                "hidden": True,
                "text": "",
                "values": ["", "", "", ""],
                "params": {"keep": "1"},
                "reloads": 1,
                "timers": 0,
                "libraryFilterTimer": None,
            },
        )

    def test_library_loading_appends_pages_and_restarts_changed_snapshots(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const location = new URL("http://example.test/library");
const PAGE_SIZE = 10, COLLECTION_SOURCE_LIMIT = 1000;
const libraryCollectionPages = new Map();
const collectionByKey = (key) => ({{key}});
const currentLibraryFilters = () => ({{media: "text", language: "eng"}});
const renderLibrary = () => {{}};
const refreshLibrarySourceList = () => {{}};
const isCanonicalSourceRecord = (record) => Boolean(record.key);
const appPath = (path) => path;
const records = Array.from({{length: 1001}}, (_, i) => ({{key: `source-${{i}}`}}));
let version = "v1";
const requests = [];
const fetch = async (url) => {{
  const limit = Number(url.searchParams.get("limit"));
  const offset = Number(url.searchParams.get("offset"));
  requests.push({{limit, offset, media: url.searchParams.get("media_type"), language: url.searchParams.get("language")}});
  const items = records.slice(offset, offset + limit);
  return {{
    ok: true, json: async () => items,
    headers: new Map([
      ["X-Total-Count", String(records.length)], ["X-Has-More", String(offset + items.length < records.length)],
      ["X-Snapshot-Version", version],
    ]),
  }};
}};
{_function_source(source, "function listingParams(", "async function fetchPagedJson(")}
{_function_source(source, "async function loadCollectionSources(", "async function loadLibraryData(")}
(async () => {{
  await loadCollectionSources("library");
  const initial = libraryCollectionPages.get("library").sources.length;
  version = "v2";
  await loadCollectionSources("library", {{append: true}});
  const restarted = libraryCollectionPages.get("library").sources.length;
  await Promise.all([
    loadCollectionSources("library", {{append: true}}),
    loadCollectionSources("library", {{append: true}}),
  ]);
  const state = libraryCollectionPages.get("library");
  console.log(JSON.stringify({{initial, restarted, count: state.sources.length, hasMore: state.hasMore, requests}}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["initial"], 1000)
        self.assertEqual(payload["restarted"], 1000)
        self.assertEqual(payload["count"], 1001)
        self.assertFalse(payload["hasMore"])
        self.assertEqual([item["offset"] for item in payload["requests"]], [0, 1000, 0, 1000])
        self.assertTrue(
            all(
                item["limit"] == 1000
                and item["media"] == "text"
                and item["language"] == "eng"
                for item in payload["requests"]
            )
        )

    def test_library_cache_renders_before_fetch_and_revalidates_without_clearing_cards(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const PAGE_SIZE = 10, COLLECTION_SOURCE_LIMIT = 1000;
const COLLECTION_CACHE_KEY = "collections", LIBRARY_PAGE_CACHE_KEY = "pages";
const libraryCollectionPages = new Map();
let collections = [], libraryCollections = [], librarySources = [], libraryLoadSequence = 0;
let filters = {{provider: "", media: "", language: "", q: ""}};
const libraryLanguageOptions = new Set();
const elements = {{
  libraryProviderFilter: {{value: ""}}, libraryMediaFilter: {{value: ""}},
  libraryLanguageFilter: {{value: ""}},
  libraryStatus: {{textContent: ""}},
}};
const $ = (id) => elements[id];
const t = (text) => text;
const libraryLanguageCodes = (source) =>
  String(source?.language || "").split(",").map((part) => part.trim().toLowerCase()).filter(Boolean);
const renderLibraryLanguageOptions = () => {{}};
const libraryFilterTerms = () => filters.q;
const appPath = (path) => path;
const sortLibrarySources = (records) => records;
const collectionKeyFor = (record) => record.collection.key;
const collectionByKey = (key) => collections.find((record) => record.key === key);
const isCanonicalSourceRecord = (record) => Boolean(record.key && record.provider);
const isCanonicalCollectionRecord = (record) => Boolean(record.key && record.provider);
const cachedCollections = [
  {{key: "keep", provider: "zim", label: "Keep"}},
  {{key: "removed", provider: "zim", label: "Removed"}},
];
const makeSource = (key, collection) => ({{key, provider: "zim", collection: {{key: collection}}}});
const page = (key, sources) => ({{key, sources, total: sources.length, hasMore: false, version: "old"}});
const storage = new Map([
  [COLLECTION_CACHE_KEY, cachedCollections],
  [LIBRARY_PAGE_CACHE_KEY, [
    page("keep", [makeSource("cached", "keep")]),
    page("removed", [makeSource("removed", "removed")]),
    page("unknown", [makeSource("unknown", "unknown")]),
    {{...page("keep", [makeSource("wrong-group", "removed")])}},
    {{...page("keep", [makeSource("invalid", "keep")]), total: -1}},
    null,
  ]],
]);
const readCollectionCache = () => storage.get(COLLECTION_CACHE_KEY);
const readJsonCache = (key) => storage.get(key);
const writeJsonCache = (key, value) => storage.set(key, value);
const renders = [];
const renderLibrary = () => renders.push(librarySources.map((record) => record.key));
let resolveCollections, resolveSources, rejectSources, sourceRequest;
const fetchPagedJson = () => new Promise((resolve) => {{ resolveCollections = resolve; }});
const fetchJsonPage = (path, options) => {{
  sourceRequest = options;
  return new Promise((resolve, reject) => {{
    resolveSources = resolve;
    rejectSources = reject;
  }});
}};
{_function_source(source, "async function mapWithConcurrency(", "async function initialize()")}
(async () => {{
  const loading = loadLibraryData();
  const beforeFetch = [...librarySources.map((record) => record.key)];
  const cachedState = {{...libraryCollectionPages.get("keep")}};
  resolveCollections([cachedCollections[0]]);
  await new Promise(setImmediate);
  const whileRefreshing = {{
    keys: librarySources.map((record) => record.key),
    loading: libraryCollectionPages.get("keep").loading,
    removed: libraryCollectionPages.has("removed"),
  }};
  resolveSources({{items: [makeSource("fresh", "keep")], total: 2, hasMore: true, version: "new"}});
  await loading;
  const saved = storage.get(LIBRARY_PAGE_CACHE_KEY);
  filters.q = "filtered";
  const filtering = loadLibraryData();
  const duringFilter = librarySources.length;
  resolveCollections([cachedCollections[0]]);
  await new Promise(setImmediate);
  resolveSources({{items: [makeSource("filtered", "keep")], total: 1, hasMore: false, version: "new"}});
  await filtering;
  const duringFilteringCacheUnchanged = saved === storage.get(LIBRARY_PAGE_CACHE_KEY);
  const filterSent = sourceRequest.q;
  filters.q = "";
  const offline = loadLibraryData();
  resolveCollections([cachedCollections[0]]);
  await new Promise(setImmediate);
  rejectSources(new Error("offline"));
  await offline;
  console.log(JSON.stringify({{
    beforeFetch, cachedState, whileRefreshing, saved,
    renders, duringFilter, filterSent,
    cacheUnchanged: duringFilteringCacheUnchanged,
    offlineSources: librarySources.map((record) => record.key),
    offlineCache: storage.get(LIBRARY_PAGE_CACHE_KEY),
    offlineError: libraryCollectionPages.get("keep").error,
  }}));
}})().catch((error) => {{console.error(error); process.exitCode = 1;}});
"""
        result = subprocess.run(
            [NODE, "-e", script], check=True, encoding="utf-8", capture_output=True
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["beforeFetch"], ["cached", "removed"])
        self.assertEqual(payload["cachedState"]["version"], "old")
        self.assertEqual(
            payload["whileRefreshing"],
            {"keys": ["cached"], "loading": True, "removed": False},
        )
        self.assertEqual(payload["saved"][0]["sources"][0]["key"], "fresh")
        self.assertEqual(payload["saved"][0]["version"], "new")
        self.assertEqual(payload["saved"][0]["total"], 2)
        self.assertTrue(payload["saved"][0]["hasMore"])
        self.assertEqual(payload["duringFilter"], 0)
        self.assertEqual(payload["filterSent"], "filtered")
        self.assertTrue(payload["cacheUnchanged"])
        self.assertEqual(payload["offlineSources"], ["fresh"])
        self.assertEqual(payload["offlineCache"], payload["saved"])
        self.assertEqual(payload["offlineError"], "offline")
        self.assertEqual(
            payload["renders"],
            [
                ["cached", "removed"], ["cached"], ["fresh"], [], ["filtered"],
                ["fresh"], ["fresh"], ["fresh"],
            ],
        )

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
  libraryLanguageFilter: {{value: ""}},
}};
const $ = (id) => elements[id] || null;
const document = {{createElement: () => ({{}})}};
const t = (value) => value;
const librarySources = [];
let collections = [];
let query = "";
const libraryCollectionViews = () => [];
const libraryFilterTerms = () => query;
{_function_source(source, "function renderLibraryScopeLine()", "function resultKey(")}
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
        self._check_deep_search_discovery("q")

    def test_browse_source_selections_retain_other_sources(self):
        for browse in ("random", "rare", "recency", "diverse", "outliers"):
            with self.subTest(browse=browse):
                self._check_deep_search_discovery(
                    "", browse, 331061536 if browse == "random" else 0
                )

    def _check_deep_search_discovery(self, question, browse=None, seed=0):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const question = {json.dumps(question)}, browse = {json.dumps(browse)}, seed = {seed};
const BROWSE_LABELS = {{[browse]: browse}};
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
const document = {{body: {{classList: {{add() {{}}, toggle() {{}}}}}}}};
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
const renderSortControls = () => {{}};
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
const scopedSearch = (question, selected, page = 0, scope, collection = null) =>
    runSearch(question, selected, page, scope, collection, browse, seed);
const broad = {{
    question, browse, seed, searchAll: true,
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
    await scopedSearch(question, ["a"]);
    const first = summarize();
    await scopedSearch(question, ["a", "b"]);
    const expanded = summarize();
    await scopedSearch(question, ["b"]);
    const narrowed = summarize();
    const changedOrder = sourceDiscovery(
        question, current, browse === "random" ? "rare" : "random", seed,
    );
    const changedSeed = browse === "random"
        ? sourceDiscovery(question, current, browse, seed + 1)
        : null;
    await scopedSearch("other query", ["b"]);
    const changed = summarize();
    await scopedSearch("other query", ["a", "b"]);
    const direct = summarize();
    await scopedSearch("other query", [], 0, [], "zim:library");
    current.searchKeys = ["a", "b", "c"];
    await loadPage(1);
    await scopedSearch("other query", ["b"], 0, ["b"], "zim:library");
    await loadPage(1);
    console.log(JSON.stringify({{
        first, expanded, narrowed, changed, direct, requests,
        changedOrder, changedSeed, broadCounts: broad.counts,
    }}));
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
        self.assertIsNone(payload["changedOrder"])
        self.assertIsNone(payload["changedSeed"])
        self.assertEqual(payload["changed"]["counts"], {"b": 20})
        self.assertFalse(payload["changed"]["discovery"])
        self.assertEqual(payload["direct"]["counts"], {"a": 20, "b": 20})
        self.assertFalse(payload["direct"]["discovery"])
        self.assertEqual(
            payload["requests"],
            [["a"], ["a", "b"], ["b"], ["b"], ["a", "b"], [], [], ["b"], ["b"]],
        )
        self.assertEqual(payload["broadCounts"], {"a": 5, "b": 4, "c": 3})

    def test_selecting_source_keeps_expanded_other_category_open(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const question = "q";
const BROWSE_LABELS = {{}};
let selectedSourceKeys = new Set();
let selectedCollectionKey = null;
let sourceFilterShowAll = false, sourceFilterShowOther = true;
let requestController = null, pageController = null;
let lastStreamRenderAt = 0, searchSequence = 0, activeSearchResult = null;
const thumbnailCache = new Map();
const PAGE_SIZE = 10, SOURCE_FILTER_LIMIT = 10, SOURCE_FILTER_QUERY_LIMIT = 30;
const STREAM_FIRST_BYTE_TIMEOUT = 1000, STREAM_TOTAL_TIMEOUT = 1000;
const STREAM_IDLE_TIMEOUT = 1000, BUSY_RETRY_LIMIT = 0;
const window = {{scrollTo() {{}}}};
const document = {{body: {{classList: {{add() {{}}, toggle() {{}}}}}}}};
class Node {{
  constructor() {{
    this.value = ""; this.hidden = false; this.disabled = false;
    this.textContent = ""; this.innerHTML = ""; this.handlers = {{}}; this.dataset = {{}};
  }}
  addEventListener(name, handler) {{ this.handlers[name] = handler; }}
  querySelectorAll() {{ return []; }}
  focus() {{}}
  setAttribute() {{}}
}}
const elements = new Map();
const $ = (id) => {{
  if (!elements.has(id)) elements.set(id, new Node());
  return elements.get(id);
}};
const escapeHtml = (text) => text;
const appPath = (path) => path;
const setSettingsOpen = () => {{}};
const cancelStreamRender = () => {{}};
const cancelThumbnails = () => {{}};
const queryTerms = (question) => [question];
const announceSearchStatus = () => {{}};
const renderScopeLine = () => {{}};
const renderSourceFilters = () => {{}};
const renderResults = () => {{}};
const renderSortControls = () => {{}};
const renderSourceSelectionSummary = () => {{}};
const syncPage = () => {{}};
const sourceFilterControlKey = () => "";
const defaultBrowseMode = () => "relevance";
const commitAutoSearchHistory = () => {{}};
const makeStateUrl = () => "/";
const activeSources = () => [{{key: "a"}}, {{key: "b"}}, {{key: "c"}}];
const requests = [];
const fetch = async (url) => {{
  requests.push(new URL(url, "http://example.test").searchParams.getAll("source"));
  return {{ok: true, json: async () => [], headers: new Map()}};
}};
const consumeStream = async () => {{
  current.outcomes = new Map([...selectedSourceKeys].map((key) => [key, {{
    source: {{key, provider: "zim"}}, status: "ok",
  }}]));
  current.counts = Object.fromEntries([...selectedSourceKeys].map((key) => [key, 20]));
  current.completed = current.sourcesTotal = selectedSourceKeys.size;
  return false;
}};
const history = {{state: null, pushState() {{}}, replaceState() {{}}}};
{_function_source(source, "function selectionValues(", "function sourceFilterLayout(")}
{_function_source(source, "async function runSearch(", "async function loadPage(")}
{_function_source(source, "function applySourceSelection(", "function makeStateUrl(")}
const outcome = (key) => ({{source: {{key, provider: "zim"}}, status: "ok"}});
let current = {{
  question, browse: null, seed: 0, searchAll: true, searchKeys: [], scopeKeys: [],
  searchCollection: null,
  outcomes: new Map(["a", "b", "c"].map((key) => [key, outcome(key)])),
  counts: {{a: 5, b: 4, c: 3}}, completed: 3, sourcesTotal: 3,
  streaming: false, page: 0, pageItems: [], total: 0, discovery: null,
}};
{_function_source(source, '$("sourceFilterOptions").addEventListener("click"', '$("results").addEventListener("click"')}
const clickSource = (key) => $("sourceFilterOptions").handlers.click({{
  target: {{closest: () => ({{dataset: {{source: key}}, disabled: false}})}},
}});
(async () => {{
  clickSource("a");
  await new Promise((resolve) => setImmediate(resolve));
  const afterSelection = {{
    showOther: sourceFilterShowOther, showAll: sourceFilterShowAll,
    selected: [...selectedSourceKeys], requests: [...requests],
  }};
  await runSearch("q", ["a"], 0, ["a"], null, null, 0);
  const afterSearch = {{showOther: sourceFilterShowOther}};
  console.log(JSON.stringify({{afterSelection, afterSearch}}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
        result = subprocess.run(
            [NODE, "-e", script], encoding="utf-8", capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["afterSelection"]["showOther"])
        self.assertFalse(payload["afterSelection"]["showAll"])
        self.assertEqual(payload["afterSelection"]["selected"], ["a"])
        self.assertEqual(payload["afterSelection"]["requests"], [["a"]])
        self.assertFalse(payload["afterSearch"]["showOther"])

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

    def test_source_filter_status_waits_for_a_finished_search_before_reporting_none(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
class Node {{
  constructor() {{
    this.children = []; this.dataset = {{}}; this.hidden = false;
    this.textContent = ""; this.classList = {{remove() {{}}}};
  }}
  replaceChildren(...children) {{ this.children = children; }}
  contains() {{ return false; }}
  querySelectorAll() {{ return []; }}
}}
const elements = new Map();
const $ = (id) => {{
  if (!elements.has(id)) elements.set(id, new Node());
  return elements.get(id);
}};
const document = {{activeElement: null, createElement: () => new Node()}};
const t = (text) => text;
let sourceFilterShowOther = false;
let current = {{streaming: true, sourcesTotal: 2}};
let layout = {{
  query: "", matching: [], outcomes: [], groups: [], visible: [],
  showOther: false, otherOutcomes: [],
}};
const sourceFilterLayout = () => layout;
const sourceFilterControlKey = () => "";
const sourceFilterButton = () => new Node();
{_function_source(source, "function renderSourceFilters()", "function renderSourceSelectionSummary()")}
const status = () => $("sourceFilterStatus").textContent;
renderSourceFilters();
const streaming = status();
current.streaming = false;
renderSourceFilters();
const finishedEmpty = status();
layout.outcomes = [{{source: {{key: "a"}}}}];
renderSourceFilters();
const reported = status();
layout.outcomes = [];
current.sourcesTotal = 0;
renderSourceFilters();
const noSources = status();
layout.query = "zzz";
renderSourceFilters();
const noMatch = status();
console.log(JSON.stringify({{streaming, finishedEmpty, reported, noSources, noMatch}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["streaming"], "")
        self.assertEqual(payload["finishedEmpty"], "No sources returned results.")
        self.assertEqual(payload["reported"], "")
        self.assertEqual(payload["noSources"], "")
        self.assertEqual(payload["noMatch"], "No matching sources.")

    def test_source_scope_panel_waits_for_started_sources(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
class Node {{
  constructor() {{
    this.children = []; this.dataset = {{}}; this.hidden = false;
    this.textContent = ""; this.classList = {{remove() {{}}, add() {{}}}};
  }}
  replaceChildren(...children) {{ this.children = children; }}
  contains() {{ return false; }}
  querySelectorAll() {{ return []; }}
}}
const elements = new Map();
const $ = (id) => {{
  if (!elements.has(id)) elements.set(id, new Node());
  return elements.get(id);
}};
const document = {{activeElement: null, createElement: () => new Node()}};
const t = (text) => text;
let sourceFilterShowOther = false;
let selectedSourceKeys = new Set(["a"]);
let selectedCollectionKey = null;
let current = {{
  streaming: true, sourcesTotal: 0, searchKeys: ["a"], searchAll: false,
  outcomes: new Map(), counts: {{}}, discovery: null,
}};
const layout = {{
  query: "", matching: [], outcomes: [], groups: [], visible: [],
  showOther: false, otherOutcomes: [],
}};
const sourceFilterLayout = () => layout;
const sourceFilterControlKey = () => "";
const sourceFilterButton = () => new Node();
const sourceDisplayName = (key) => key;
const collectionDisplayName = (key) => key;
{_function_source(source, "function renderSourceFilters()", "function renderSourceSelectionSummary()")}
{_function_source(source, "function renderScopeLine()", "function renderFilterLine(")}
const snapshot = () => ({{
  filtersHidden: $("sourceFilters").hidden,
  toolbarHidden: $("sourceFilterToolbar").hidden,
  scopeHidden: $("scopeLine").hidden,
  resetHidden: $("scopeLineReset").hidden,
  scopeText: $("scopeLineText").textContent,
}});
renderSourceFilters();
renderScopeLine();
const loading = snapshot();
current.sourcesTotal = 2;
renderSourceFilters();
renderScopeLine();
const started = snapshot();
console.log(JSON.stringify({{loading, started}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["loading"],
            {
                "filtersHidden": True,
                "toolbarHidden": True,
                "scopeHidden": True,
                "resetHidden": True,
                "scopeText": "",
            },
        )
        self.assertEqual(
            payload["started"],
            {
                "filtersHidden": False,
                "toolbarHidden": True,
                "scopeHidden": False,
                "resetHidden": False,
                "scopeText": "Searching only in: a",
            },
        )

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

    def test_sort_control_keeps_the_rendered_mode_until_a_new_search_starts(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const select = {{
  options: ["relevance", "diverse", "rare"].map((value) => ({{
    value, hidden: false, disabled: false,
  }})),
  value: "",
}};
const question = {{value: ""}};
const sortControls = {{hidden: true}};
const nodes = {{sortSelect: select, question, sortControls}};
const $ = (id) => nodes[id];
let current = null;
const availableBrowseModes = new Set(["diverse", "rare"]);
{_function_source(source, "function defaultBrowseMode(", "function renderSortControls(")}
{_function_source(source, "function renderSortControls(", "function setSettingsOpen(")}
const snapshot = () => ({{
  value: select.value,
  hidden: Object.fromEntries(select.options.map((option) => [option.value, option.hidden])),
  controlsHidden: sortControls.hidden,
}});
current = {{question: "water", browse: null, pageItems: [{{}}]}};
question.value = "";
renderSortControls();
const clearedQuery = snapshot();
question.value = "new query";
renderSortControls();
const typed = snapshot();
current = {{question: "", browse: "diverse", pageItems: [{{}}]}};
question.value = "";
renderSortControls();
const browsing = snapshot();
current = null;
question.value = "";
renderSortControls();
const idle = snapshot();
console.log(JSON.stringify({{clearedQuery, typed, browsing, idle}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        # Clearing the box while the q=water page is still rendered must not
        # pretend the results are now an empty-query Diversity browse.
        self.assertEqual(payload["clearedQuery"]["value"], "relevance")
        self.assertEqual(payload["clearedQuery"]["hidden"]["relevance"], False)
        self.assertEqual(payload["clearedQuery"]["controlsHidden"], False)
        # Typing a new query is also still a relevance search.
        self.assertEqual(payload["typed"]["value"], "relevance")
        # Once an empty-query browse is the active search, show Diversity.
        self.assertEqual(payload["browsing"]["value"], "diverse")
        self.assertEqual(payload["browsing"]["hidden"]["relevance"], True)
        # With no search at all, keep the default browse mode.
        self.assertEqual(payload["idle"]["value"], "diverse")

    def test_location_state_ignores_unknown_sort_modes(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const BROWSE_LABELS = {{
  random: "Random", rare: "Rare titles", outliers: "Semantic outliers",
  diverse: "Diversity", recency: "Recency",
}};
const BROWSE_MODES = new Set(Object.keys(BROWSE_LABELS));
const availableBrowseModes = BROWSE_MODES;
const MAX_QUERY_LENGTH = 4096;
const location = {{search: ""}};
{_function_source(source, "function defaultBrowseMode(", "function renderSortControls(")}
{_function_source(source, "function readLocationState(", "function syncArticleModal(")}
const read = (search) => {{
  location.search = search;
  const state = readLocationState();
  return {{question: state.question, browse: state.browse, defaultSort: state.defaultSort}};
}};
console.log(JSON.stringify({{
  badQuery: read("?q=diabetes&sort=bogus"),
  badBrowse: read("?q=&sort=bogus"),
  validBrowse: read("?q=diabetes&sort=rare"),
  explicitRelevance: read("?q=diabetes&sort=relevance"),
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
            payload["badQuery"],
            {"question": "diabetes", "browse": None, "defaultSort": False},
        )
        self.assertEqual(
            payload["badBrowse"],
            {"question": "", "browse": "diverse", "defaultSort": True},
        )
        self.assertEqual(payload["validBrowse"]["browse"], "rare")
        self.assertEqual(payload["explicitRelevance"]["browse"], None)

    def test_result_range_uses_translated_result_nouns(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const PAGE_SIZE = 10;
const catalog = {{
  "of": "de", "result": "resultado", "results": "resultados",
  "No results": "Sin resultados",
}};
const t = (text) => catalog[text] || text;
{_function_source(source, "function pageRangeText(", "function syncPage(")}
console.log(JSON.stringify({{
  many: pageRangeText(65, 0),
  one: pageRangeText(1, 0),
  none: pageRangeText(0, 0),
}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["many"], "1–10 de 65 resultados")
        self.assertEqual(payload["one"], "1–1 de 1 resultado")
        self.assertEqual(payload["none"], "Sin resultados")

    def test_modified_heading_click_opens_search_in_new_tab(self):
        source = APP_JS.read_text(encoding="utf-8")
        script = f"""
const appPath = (path) => `/zimantic${{path}}`;
const opened = [];
const window = {{open: (...args) => opened.push(args)}};
let celebrated = 0, toggled = 0;
const celebrateHeadingClick = () => {{ celebrated++; }};
const toggleHeadingSearch = () => {{ toggled++; }};
{_function_source(source, "function modifiedClick(", "function articleUrlValue(")}
{_function_source(source, "function activateHomeHeading(", "function currentUrlValue(")}
activateHomeHeading({{button: 0, ctrlKey: true}});
const ctrl = opened.slice();
activateHomeHeading({{button: 0, metaKey: true}});
const meta = opened.slice();
activateHomeHeading({{button: 0}});
console.log(JSON.stringify({{ctrl, meta, opened, celebrated, toggled}}));
"""
        result = subprocess.run(
            [NODE, "-e", script],
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        newTab = [["/zimantic/", "_blank", "noopener"]]
        self.assertEqual(payload["ctrl"], newTab)
        self.assertEqual(payload["meta"], newTab * 2)
        self.assertEqual(payload["opened"], newTab * 2)
        self.assertEqual(payload["celebrated"], 1)
        self.assertEqual(payload["toggled"], 1)


if __name__ == "__main__":
    unittest.main()
