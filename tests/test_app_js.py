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
let librarySources = [{{language: "eng,spa"}}, {{language: "fra"}}];
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
                ["eng", "ENG - English"],
                ["fra", "FRA - French"],
                ["spa", "SPA - Spanish"],
            ],
        )
        self.assertEqual(
            payload["spanish"]["options"],
            ["All languages", "ENG - inglés", "FRA - francés", "SPA - español"],
        )
        self.assertEqual(payload["selected"], "fra")

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
            "ENG - English · 2025-08-27\n34,448 items · 109 MB",
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
            "Filtered by: Content type: Video · Language: ENG - English",
        )
        self.assertEqual(payload["status"], "1 collection · 1 loaded source")
        self.assertEqual(payload["placeholder"], "Filter 1 source")

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
            "Filtered by: Provider: Files · Content type: Video · Language: FRA - French · Filter: Trees",
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
