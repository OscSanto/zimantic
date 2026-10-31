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
            web_app.ARTICLE_TOOLBAR_SCRIPT % (json.dumps(""), json.dumps(False))
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
{self.bridge % json.dumps(base_path)}
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
{self.bridge % json.dumps("/zimantic")}
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


if __name__ == "__main__":
    unittest.main()
