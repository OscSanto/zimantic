import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from zimantic.web import app as web_app

NODE = shutil.which("node")
APP_JS = Path(__file__).resolve().parents[1] / "zimantic" / "web" / "static" / "app.js"


def _section(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin + len(start))
    return source[begin:finish]


def _inline_script(source: str) -> str:
    source = source.strip()
    return re.sub(r"^<script[^>]*>|</script>$", "", source, flags=re.DOTALL)


def _run_node(script: str) -> dict:
    # Background article timers must not keep synchronous navigation checks alive.
    script = """
const originalSetTimeout = setTimeout;
globalThis.setTimeout = (...args) => originalSetTimeout(...args).unref();
""" + script
    result = subprocess.run(
        [NODE, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


@unittest.skipUnless(NODE, "node is unavailable")
class ArticleNavigationScriptTests(unittest.TestCase):
    """Execute the injected bridge and shell routing, not just their source."""

    @classmethod
    def setUpClass(cls):
        cls.bridge = _inline_script(web_app.ARTICLE_NAV_BRIDGE_SCRIPT)
        toolbar = _inline_script(
            web_app.ARTICLE_TOOLBAR_SCRIPT.replace(
                "__APP_BASE_PATH_JSON__", json.dumps("")
            ).replace("__SECURE_BROWSING_JSON__", json.dumps(False))
        )
        cls.local_source = _section(
            toolbar, "const localPath = pathname =>", "const permalinkLabel"
        )
        cls.helper_source = _section(
            toolbar, "const articleRoute = value =>", "const handleArticleLink = data =>"
        )
        cls.handle_source = _section(
            toolbar,
            "const handleArticleLink = data =>",
            '\n    window.addEventListener("message", event => {\n      if (!articleFrame',
        )
        cls.source_action_source = _section(
            toolbar,
            '} else if (action === "source-previous"',
            '} else if (action === "previous"',
        )

    def _bridge_redirect(self, href, base_path="", framed=False):
        script = f"""
const calls = [];
const window = {{}};
window.parent = {"{}" if framed else "window"};
window.addEventListener = () => {{}};
const location = {{
  href: {json.dumps(href)},
  replace(value) {{ calls.push(String(value)); }},
}};
const document = {{readyState: "complete", addEventListener() {{}}}};
{self.bridge.replace("__BASE_PATH_JSON__", json.dumps(base_path))}
console.log(JSON.stringify(calls));
"""
        return _run_node(script)

    def test_top_level_article_redirects_to_the_shell(self):
        self.assertEqual(
            self._bridge_redirect("http://example.test/article/manual/page?x=1#frag"),
            ["http://example.test/content/manual/page?x=1#frag"],
        )
        self.assertEqual(
            self._bridge_redirect(
                "http://example.test/zimantic/article/manual/page",
                base_path="/zimantic",
            ),
            ["http://example.test/zimantic/content/manual/page"],
        )

    def test_framed_article_does_not_redirect(self):
        self.assertEqual(
            self._bridge_redirect("http://example.test/article/manual/page", framed=True),
            [],
        )

    def test_selection_keeps_raw_title_link_separate_from_shell_permalink(self):
        toolbar = _inline_script(web_app.ARTICLE_TOOLBAR_SCRIPT)
        source = _section(
            toolbar, "const shareUrl =", "const scrollArticleTo ="
        )
        for framed in (False, True):
            with self.subTest(framed=framed):
                payload = _run_node(f"""
const location = new URL("http://example.test/zimantic/content/files%3Amodules/page.sh?keep=1");
const permalink = {{href: location.href}};
const articleLink = {{href: "http://example.test/zimantic/article/files%3Amodules/page.sh?keep=1"}};
const messages = [];
const window = {{}};
window.parent = {("{postMessage: data => messages.push(data)}" if framed else "window")};
const history = {{state: null, replaceState() {{}}}};
let articleSelection = "";
const normalizeSelection = value => String(value || "").trim();
{source}
setArticleSelection("Hello world");
const selected = {{raw: articleLink.href, permalink: permalink.href}};
setArticleSelection("");
console.log(JSON.stringify({{selected, cleared: articleLink.href, messages}}));
""")
                self.assertEqual(
                    payload["selected"]["raw"],
                    "http://example.test/zimantic/article/files%3Amodules/page.sh"
                    "?keep=1&highlight=Hello+world",
                )
                self.assertEqual(
                    payload["selected"]["permalink"],
                    "http://example.test/zimantic/content/files%3Amodules/page.sh"
                    "?keep=1&highlight=Hello+world",
                )
                self.assertEqual(
                    payload["cleared"],
                    "http://example.test/zimantic/article/files%3Amodules/page.sh?keep=1",
                )

    def test_toolbar_search_navigates_with_canonical_source_scope(self):
        toolbar = _inline_script(web_app.ARTICLE_TOOLBAR_SCRIPT)
        search_source = _section(
            toolbar, "const openZimSearchPage = () =>", "const requestZimSuggestions"
        )
        key = "zim:library:rus.stackexchange.com_mul_all_2024-11-6984002a4812c270"
        for framed in (False, True):
            with self.subTest(framed=framed):
                script = f"""
const location = new URL("http://example.test/zimantic/content/manual/questions");
const appBasePath = "/zimantic";
const messages = [];
const window = {{}};
window.parent = {(" {postMessage: data => messages.push(data)}" if framed else "window")};
const zimSearchInput = {{value: " tel "}};
const zimSearchForm = {{dataset: {{zimanticZim: {json.dumps(key)}}}}};
const closeZimSuggestions = () => {{}};
{search_source}
const opened = openZimSearchPage();
console.log(JSON.stringify({{opened, url: messages[0]?.url || location.href, messages}}));
"""
                payload = _run_node(script)
                self.assertTrue(payload["opened"])
                self.assertEqual(
                    payload["url"],
                    "http://example.test/zimantic/?q=tel&source="
                    "zim%3Alibrary%3Arus.stackexchange.com_mul_all_2024-11-6984002a4812c270",
                )
                if framed:
                    self.assertEqual(payload["messages"][0]["type"], "zimantic-app-navigate")

    def _bridge_clicks(self):
        script = f"""
const calls = [];
const handlers = {{}};
const window = {{addEventListener(type, fn) {{ handlers[`win:${{type}}`] = fn; }}}};
const parent = {{postMessage(data) {{ calls.push(data); }}}};
window.parent = parent;
const location = {{
  href: "http://example.test/zimantic/article/manual/example.com/page?x=1",
}};
const document = {{
  readyState: "complete",
  addEventListener(type, fn) {{ handlers[`doc:${{type}}`] = fn; }},
  querySelector() {{ return null; }},
}};
{self.bridge.replace("__BASE_PATH_JSON__", json.dumps("/zimantic"))}
const click = options => {{
  const anchor = {{
    target: options.target || "",
    getAttribute(name) {{ return name === "href" ? options.href : null; }},
    hasAttribute(name) {{ return name === "download" && !!options.download; }},
  }};
  const event = Object.assign({{
    button: 0,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    defaultPrevented: false,
    target: {{closest() {{ return anchor; }}}},
    preventDefault() {{ this.defaultPrevented = true; }},
  }}, options.event);
  handlers["doc:click"](event);
}};
click({{href: "next"}});
click({{href: "#frag"}});
click({{href: "next", event: {{ctrlKey: true}}}});
click({{href: "next", download: true}});
click({{href: "next", target: "_blank"}});
click({{href: "next", target: "_self"}});
click({{href: "mailto:someone@example.test"}});
console.log(JSON.stringify(calls));
"""
        return _run_node(script)

    def test_bridge_reports_only_clicks_the_shell_should_handle(self):
        payload = self._bridge_clicks()
        article = "http://example.test/zimantic/article/manual/example.com/page?x=1"
        expected = {
            "type": "zimantic-article-link",
            "href": "next",
            "base": "",
            "url": article,
        }
        self.assertEqual(payload, [expected, expected])

    def _helpers(self, base_path="/zimantic"):
        shell_href = (
            f"http://example.test{base_path}/content/manual/example.com/page?x=1"
        )
        article_href = (
            f"http://example.test{base_path}/article/manual/example.com/page?x=1"
        )
        script = f"""
const location = {{href: {json.dumps(shell_href)}}};
const shellUrl = new URL(location.href);
const articleFrame = {{getAttribute: () => {json.dumps(article_href)}}};
const appBasePath = {json.dumps(base_path)};
{self.local_source}
{self.helper_source}
const helpers = {{articleRoute, contentPathFor, offlineUrlFor, currentContentUrl, articleUrl}};
const route = articleRoute({json.dumps(article_href)});
console.log(JSON.stringify({{
  route: route && {{
    path: route.path,
    zim: route.zim,
    root: route.root,
    href: route.url.href,
  }},
  articleUrl: helpers.articleUrl && helpers.articleUrl.href,
  content: contentPathFor(route.url),
  parent: currentContentUrl(route),
  offline: offlineUrlFor(new URL("https://www.example.com/next?q=2#z"), route),
  otherHost: offlineUrlFor(new URL("https://other.test/next"), route),
  mailto: articleRoute("mailto:someone@example.test"),
  otherOrigin: articleRoute("http://other.test/article/manual/x"),
}}));
"""
        return _run_node(script)

    def test_shell_maps_article_and_live_urls(self):
        payload = self._helpers()
        self.assertEqual(payload["articleUrl"], payload["route"]["href"])
        self.assertEqual(payload["route"]["path"], "/article/manual/example.com/page")
        self.assertEqual(payload["route"]["zim"], "manual")
        self.assertEqual(payload["route"]["root"], "example.com")
        self.assertEqual(
            payload["content"], "/zimantic/content/manual/example.com/page"
        )
        self.assertEqual(
            payload["parent"], "/zimantic/content/manual/example.com/page?x=1"
        )
        self.assertEqual(
            payload["offline"],
            "http://example.test/zimantic/content/manual/example.com/next?q=2#z",
        )
        self.assertIsNone(payload["otherHost"])
        self.assertIsNone(payload["mailto"])
        self.assertIsNone(payload["otherOrigin"])

    def test_colon_source_keys_stay_percent_encoded_in_bridge_urls(self):
        key = "files%3Amodules%3Aroot"
        base_path = "/zimantic"
        shell_href = (
            f"http://example.test{base_path}/content/{key}/example.com/page"
        )
        article_href = (
            f"http://example.test{base_path}/article/{key}/example.com/page"
        )
        script = f"""
const location = {{href: {json.dumps(shell_href)}}};
const shellUrl = new URL(location.href);
const articleFrame = {{getAttribute: () => {json.dumps(article_href)}}};
const appBasePath = {json.dumps(base_path)};
{self.local_source}
{self.helper_source}
const route = articleRoute({json.dumps(article_href)});
console.log(JSON.stringify({{
  zim: route && route.zim,
  content: route && contentPathFor(route.url),
  offline: route && offlineUrlFor(
    new URL("https://www.example.com/next?q=2#z"), route
  ),
}}));
"""
        payload = _run_node(script)
        self.assertEqual(payload["zim"], key)
        self.assertEqual(
            payload["content"], f"{base_path}/content/{key}/example.com/page"
        )
        self.assertIn(f"{base_path}/content/{key}/example.com/next", payload["offline"])
        self.assertNotIn("files:modules:root", payload["offline"])

    def _handle(self, data, *, base_path="/zimantic", embedded=True):
        shell_href = (
            f"http://example.test{base_path}/content/manual/example.com/page?x=1"
        )
        article_href = (
            f"http://example.test{base_path}/article/manual/example.com/page?x=1"
        )
        script = f"""
const location = {{href: {json.dumps(shell_href)}}};
const shellUrl = new URL(location.href);
const articleFrame = {{getAttribute: () => {json.dumps(article_href)}}};
const appBasePath = {json.dumps(base_path)};
{self.local_source}
{self.helper_source}
const helpers = {{articleRoute, contentPathFor, offlineUrlFor, currentContentUrl, articleUrl}};
const calls = [];
const parent = {{postMessage(data) {{ calls.push(["post", data]); }}}};
const window = {{parent: {json.dumps(embedded)} ? parent : null}};
if (!window.parent) window.parent = window;
const build = new Function(
  "articleRoute", "contentPathFor", "offlineUrlFor", "currentContentUrl",
  "articleUrl", "shellUrl", "window", "location", "scrollArticleTo",
  "openOfflineArticle",
  "let offlineRequest = 0;\\n" + {json.dumps(self.handle_source)} +
    "\\nreturn handleArticleLink;"
);
const handle = build(
  helpers.articleRoute, helpers.contentPathFor, helpers.offlineUrlFor,
  helpers.currentContentUrl, helpers.articleUrl, shellUrl, window, location,
  hash => calls.push(["scroll", hash]),
  (target, fallback) => calls.push(["offline", target.href, fallback])
);
handle({json.dumps(data)});
console.log(JSON.stringify({{calls, href: location.href}}));
"""
        return _run_node(script)

    def test_embedded_external_links_use_offline_copy_or_notice(self):
        article = "http://example.test/zimantic/article/manual/example.com/page?x=1"
        payload = self._handle(
            {
                "type": "zimantic-article-link",
                "href": "https://www.example.com/next?q=2#z",
                "base": "",
                "url": article,
            }
        )
        self.assertEqual(payload["calls"], [[
            "post",
            {
                "type": "zimantic-external-link",
                "url": "https://www.example.com/next?q=2#z",
                "offlineUrl": (
                    "http://example.test/zimantic/content/manual/example.com/"
                    "next?q=2#z"
                ),
                "parent": "/zimantic/content/manual/example.com/page?x=1",
            },
        ]])

        payload = self._handle(
            {
                "type": "zimantic-article-link",
                "href": "https://other.test/next",
                "base": "",
                "url": article,
            }
        )
        self.assertEqual(payload["calls"], [[
            "post",
            {
                "type": "zimantic-external-link",
                "url": "https://other.test/next",
                "offlineUrl": None,
                "parent": "/zimantic/content/manual/example.com/page?x=1",
            },
        ]])

    def test_embedded_internal_links_follow_the_shell_or_the_app(self):
        article = "http://example.test/zimantic/article/manual/example.com/page?x=1"
        payload = self._handle(
            {
                "href": "/zimantic/article/manual/example.com/other",
                "base": "",
                "url": article,
            }
        )
        self.assertEqual(payload["calls"], [[
            "post",
            {
                "type": "zimantic-article-follow",
                "url": (
                    "http://example.test/zimantic/content/manual/example.com/other"
                ),
                "parent": "/zimantic/content/manual/example.com/page?x=1",
            },
        ]])

        payload = self._handle(
            {"href": "/zimantic/library", "base": "", "url": article}
        )
        self.assertEqual(payload["calls"], [[
            "post",
            {
                "type": "zimantic-app-navigate",
                "url": "http://example.test/zimantic/library",
            },
        ]])

    def test_direct_shell_navigates_or_probes_offline_itself(self):
        article = "http://example.test/zimantic/article/manual/example.com/page?x=1"
        payload = self._handle(
            {"href": "/zimantic/article/manual/example.com/other", "url": article},
            embedded=False,
        )
        self.assertEqual(payload["calls"], [])
        self.assertEqual(
            payload["href"],
            "http://example.test/zimantic/content/manual/example.com/other",
        )

        payload = self._handle(
            {"href": "https://www.example.com/next", "url": article},
            embedded=False,
        )
        self.assertEqual(payload["calls"], [[
            "offline",
            "http://example.test/zimantic/content/manual/example.com/next",
            "https://www.example.com/next",
        ]])
        self.assertEqual(
            payload["href"],
            "http://example.test/zimantic/content/manual/example.com/page?x=1",
        )

    def test_same_document_links_scroll_the_article(self):
        article = "http://example.test/zimantic/article/manual/example.com/page?x=1"
        payload = self._handle(
            {"href": f"{article}#section", "url": article}
        )
        self.assertEqual(payload["calls"], [["scroll", "#section"]])

    def test_base_tag_resolves_relative_links(self):
        article = "http://example.test/zimantic/article/manual/example.com/page?x=1"
        payload = self._handle(
            {
                "href": "next",
                "base": "https://www.example.com/docs/",
                "url": article,
            }
        )
        self.assertEqual(payload["calls"], [[
            "post",
            {
                "type": "zimantic-external-link",
                "url": "https://www.example.com/docs/next",
                "offlineUrl": (
                    "http://example.test/zimantic/content/manual/example.com/"
                    "docs/next"
                ),
                "parent": "/zimantic/content/manual/example.com/page?x=1",
            },
        ]])

    def test_source_arrows_navigate_directly_or_through_the_modal(self):
        def run(action, embedded):
            script = f"""
const run = () => {{
const location = {{href: "http://example.test/content/one/Alpha"}};
const articleUrl = new URL("http://example.test/article/one/Alpha");
const articleRoute = () => ({{url: articleUrl}});
const currentContentUrl = () => "/content/one/Alpha";
const calls = [];
let offlineRequest = 0;
const event = {{preventDefault() {{ calls.push(["prevent"]); }}}};
const control = {{
  dataset: {{zimanticUrl: "http://example.test/content/two/Alpha"}},
}};
const action = {json.dumps(action)};
const parent = {{postMessage(data) {{ calls.push(["post", data]); }}}};
const window = {json.dumps(embedded)} ? {{parent}} : {{}};
if (!window.parent) window.parent = window;
const execute = () => {{
if (action === "__never__") {{
{self.source_action_source}
}}
}};
execute();
console.log(JSON.stringify({{calls, href: location.href, offlineRequest}}));
}};
run();
"""
            return _run_node(script)

        direct = run("source-next", embedded=False)
        self.assertEqual(direct["href"], "http://example.test/content/two/Alpha")
        self.assertEqual(direct["offlineRequest"], 1)
        self.assertEqual(direct["calls"], [["prevent"]])

        modal = run("source-previous", embedded=True)
        self.assertEqual(modal["href"], "http://example.test/content/one/Alpha")
        self.assertEqual(modal["offlineRequest"], 0)
        self.assertEqual(modal["calls"], [
            ["prevent"],
            [
                "post",
                {
                    "type": "zimantic-article-follow",
                    "url": "http://example.test/content/two/Alpha",
                    "parent": "/content/one/Alpha",
                },
            ],
        ])

    def test_host_navigates_same_origin_app_links_only(self):
        source = APP_JS.read_text(encoding="utf-8")
        block = _section(
            source,
            'if (event.data?.type === "zimantic-app-navigate") {',
            'if (event.data?.type === "zimantic-open-article") {',
        )
        script = f"""
const calls = [];
const location = {{href: "http://example.test/?q=q", origin: "http://example.test"}};
function handle(event) {{
{block}
}}
handle({{data: {{type: "zimantic-app-navigate", url: "/library"}}}});
const sameOrigin = location.href;
location.href = "http://example.test/?q=q";
handle({{data: {{type: "zimantic-app-navigate", url: "https://other.test/library"}}}});
console.log(JSON.stringify({{sameOrigin, crossOrigin: location.href}}));
"""
        payload = _run_node(script)
        self.assertEqual(payload["sameOrigin"], "http://example.test/library")
        self.assertEqual(payload["crossOrigin"], "http://example.test/?q=q")

    def test_close_returns_to_the_article_trail_base_in_one_history_step(self):
        source = APP_JS.read_text(encoding="utf-8")
        payload = _run_node(f"""
const elements = {{articleModal: {{hidden: false}}}};
const $ = (id) => elements[id];
let hideCalls = 0;
const hideArticleModal = () => {{ hideCalls++; elements.articleModal.hidden = true; }};
const location = new URL("http://example.test/?q=topic");
const history = {{
  state: null,
  pushes: [], replaces: [], goes: [],
  pushState(state, _title, url) {{ this.state = state; this.pushes.push([state, String(url)]); }},
  replaceState(state, _title, url) {{ this.state = state; this.replaces.push([state, String(url)]); }},
  go(delta) {{ this.goes.push(delta); }},
}};
const current = null;
const selectedSourceKeys = new Set();
const readLocationState = () => ({{question: "topic", selectedSources: [], page: 0}});
const readModalUrl = (value) => value ? new URL(value, location.href) : null;
const articleUrlValue = (url) => url.pathname + url.search + url.hash;
const articleRouteValue = (url) => url.pathname + url.search;
const makeStateUrl = (_question, _sources, _page, modal) => `/?q=topic&modal=${{encodeURIComponent(modal)}}`;
{_section(source, "function modalStateEntry(", "function navigateArticleModal(")}
{_section(source, "function closeArticleModal(", "bindArticleFrameLoad();")}
const one = new URL("http://example.test/content/manual/One");
const two = new URL("http://example.test/content/manual/Two");
const first = modalStateEntry(one, "search");
history.state = first.state;
const second = modalStateEntry(two, "follow");
// Simulate the deeper article entry being current.
history.state = second.state;
elements.articleModal.hidden = false;
closeArticleModal();
const deepClose = {{
  go: [...history.goes], replaces: history.replaces.length, hidden: elements.articleModal.hidden,
}};
// A direct /?modal=... load has no in-app trail; keep replaceState fallback.
history.goes.length = 0;
history.replaces.length = 0;
hideCalls = 0;
elements.articleModal.hidden = false;
history.state = null;
location.href = "http://example.test/?q=topic&modal=%2Fcontent%2Fmanual%2FTwo";
closeArticleModal();
const directClose = {{
  go: [...history.goes], replaces: history.replaces.map((entry) => entry[1]),
  hidden: elements.articleModal.hidden, hideCalls,
}};
console.log(JSON.stringify({{
  depths: [first.state.zimanticModalDepth, second.state.zimanticModalDepth],
  deepClose, directClose,
}}));
""")
        self.assertEqual(payload["depths"], [0, 1])
        self.assertEqual(payload["deepClose"], {"go": [-2], "replaces": 0, "hidden": True})
        self.assertEqual(payload["directClose"]["go"], [])
        self.assertEqual(
            payload["directClose"]["replaces"], ["/?q=topic"],
        )
        self.assertTrue(payload["directClose"]["hidden"])

    def test_modal_article_changes_discard_child_history_and_stale_frame_events(self):
        source = APP_JS.read_text(encoding="utf-8")
        payload = _run_node(f"""
const attached = [], messages = [];
let navigationPosts = 0;
const classes = {{add() {{}}, remove() {{}}}};
class Frame {{
  constructor(src = "") {{
    this.src = src;
    this.handlers = {{}};
    this.contentWindow = {{postMessage: (data) => messages.push(data)}};
  }}
  cloneNode() {{ return new Frame(this.src); }}
  removeAttribute(name) {{ if (name === "src") this.src = ""; }}
  addEventListener(name, fn) {{ this.handlers[name] = fn; }}
  replaceWith(fresh) {{
    attached.push(fresh.src);
    elements.articleModalFrame = fresh;
  }}
  focus() {{}}
}}
const elements = {{
  articleModal: {{hidden: true}}, articleModalFrame: new Frame(),
}};
const $ = (id) => elements[id];
const wrap = {{inert: false}};
const document = {{
  activeElement: {{}}, querySelector: () => wrap,
  documentElement: {{classList: classes}}, body: {{classList: classes}},
}};
const location = new URL("http://example.test/?q=teaching");
const appPath = (path) => path;
let activeModalUrl = null, activeLibraryArticleUrl = null, modalPreviousFocus = null;
let previousModalArticleUrl = null, pendingModalArticleUrl = null, pendingModalNavigation = null;
let activeParentNavigation = null, parentNavigationSequence = 0;
const hideExternalLinkNotice = () => {{}};
const postArticleModalNavigation = () => {{ navigationPosts++; }};
const window = {{addEventListener(_name, fn) {{ this.message = fn; }}}};
{_section(source, "function articleUrlValue(", "// The modal loads a trusted")}
{_section(source, "function showArticleModal(", "function modalStateEntry(")}
{_section(source, "function bindArticleFrameLoad(", "function hideArticleModal(")}
{_section(source, 'window.addEventListener("message", (event) => {', "function setResultNode(")}
const one = "http://example.test/content/manual/One";
const two = "http://example.test/content/manual/Two";
showArticleModal(one, "search");
const first = elements.articleModalFrame;
first.handlers.load();
showArticleModal(two, "follow");
const second = elements.articleModalFrame;
first.handlers.load();
window.message({{
  source: first.contentWindow,
  data: {{type: "zimantic-article-ready", url: one}},
}});
const afterStale = {{url: activeModalUrl, navigationPosts}};
second.handlers.load();
// Restoring the previous top-level entry must create a new browsing context,
// not traverse the old iframe's independently restored session history.
showArticleModal(one, "search");
const restored = elements.articleModalFrame;
showArticleModal(one, "search");
console.log(JSON.stringify({{
  attached, afterStale, navigationPosts, messages,
  fresh: first !== second && second !== restored && first !== restored,
  unchanged: elements.articleModalFrame === restored,
  modalOpen: !elements.articleModal.hidden, inert: wrap.inert,
}}));
""")
        self.assertEqual(payload["attached"], [
            "http://example.test/content/manual/One",
            "http://example.test/content/manual/Two",
            "http://example.test/content/manual/One",
        ])
        self.assertEqual(payload["afterStale"], {
            "url": "http://example.test/content/manual/Two",
            "navigationPosts": 1,
        })
        self.assertEqual(payload["navigationPosts"], 2)
        self.assertEqual(payload["messages"], [])
        self.assertTrue(payload["fresh"])
        self.assertTrue(payload["unchanged"])
        self.assertTrue(payload["modalOpen"])
        self.assertTrue(payload["inert"])

    def test_blank_modal_frame_after_a_real_article_closes_the_modal(self):
        source = APP_JS.read_text(encoding="utf-8")
        payload = _run_node(f"""
const attached = [], messages = [];
let navigationPosts = 0, closeCalls = 0;
const classes = {{add() {{}}, remove() {{}}}};
class Frame {{
  constructor(src = "") {{
    this.src = src;
    this.handlers = {{}};
    this.contentWindow = {{
      location: {{href: "about:blank"}},
      postMessage: (data) => messages.push(data),
    }};
  }}
  cloneNode() {{ return new Frame(this.src); }}
  removeAttribute(name) {{ if (name === "src") this.src = ""; }}
  addEventListener(name, fn) {{ this.handlers[name] = fn; }}
  replaceWith(fresh) {{
    attached.push(fresh.src);
    elements.articleModalFrame = fresh;
  }}
  focus() {{}}
}}
const elements = {{
  articleModal: {{hidden: true}}, articleModalFrame: new Frame(),
}};
const $ = (id) => elements[id];
const wrap = {{inert: false}};
const document = {{
  activeElement: {{}}, querySelector: () => wrap,
  documentElement: {{classList: classes}}, body: {{classList: classes}},
}};
const location = new URL("http://example.test/?q=teaching");
const appPath = (path) => path;
let activeLibraryArticleUrl = null, modalPreviousFocus = null;
let activeModalUrl = null, previousModalArticleUrl = null;
let pendingModalArticleUrl = null, pendingModalNavigation = null;
let activeParentNavigation = null, parentNavigationSequence = 0;
const hideExternalLinkNotice = () => {{}};
const postArticleModalNavigation = () => {{ navigationPosts++; }};
const closeArticleModal = () => {{
  closeCalls++;
  elements.articleModal.hidden = true;
}};
const window = {{addEventListener() {{}}}};
{_section(source, "function articleUrlValue(", "// The modal loads a trusted")}
{_section(source, "function showArticleModal(", "function modalStateEntry(")}
{_section(source, "function bindArticleFrameLoad(", "function hideArticleModal(")}
const one = "http://example.test/content/manual/One";
const two = "http://example.test/content/manual/Two";
showArticleModal(one, "search");
const first = elements.articleModalFrame;
// The first blank load belongs to a fresh frame; it must not close the modal.
first.handlers.load();
const afterFreshBlank = {{closeCalls, navigationPosts}};
// A real article load arms the recovery for this browsing context.
first.contentWindow.location.href = one;
first.handlers.load();
const afterRealLoad = {{closeCalls, navigationPosts}};
// Walking history out of the iframe discards its document without a popstate.
first.contentWindow.location.href = "about:blank";
first.handlers.load();
const afterBackedOut = {{closeCalls, navigationPosts}};
// Reopening replaces the frame, so a stale blank load is ignored.
showArticleModal(two, "follow");
const second = elements.articleModalFrame;
first.handlers.load();
const afterStale = {{closeCalls, navigationPosts}};
second.contentWindow.location.href = two;
second.handlers.load();
second.contentWindow.location.href = "about:blank";
second.handlers.load();
console.log(JSON.stringify({{
  afterFreshBlank, afterRealLoad, afterBackedOut, afterStale,
  finalCloseCalls: closeCalls, navigationPosts, messages, attached,
  staleIgnored: second !== first,
}}));
""")
        self.assertEqual(payload["afterFreshBlank"], {
            "closeCalls": 0,
            "navigationPosts": 0,
        })
        self.assertEqual(payload["afterRealLoad"], {
            "closeCalls": 0,
            "navigationPosts": 1,
        })
        self.assertEqual(payload["afterBackedOut"], {
            "closeCalls": 1,
            "navigationPosts": 1,
        })
        self.assertEqual(payload["afterStale"], {
            "closeCalls": 1,
            "navigationPosts": 1,
        })
        self.assertEqual(payload["finalCloseCalls"], 2)
        self.assertEqual(payload["navigationPosts"], 2)
        self.assertTrue(payload["staleIgnored"])
        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["attached"], [
            "http://example.test/content/manual/One",
            "http://example.test/content/manual/Two",
        ])


if __name__ == "__main__":
    unittest.main()
