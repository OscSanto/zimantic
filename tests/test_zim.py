import json
import sys
import types
import unittest


libzim = sys.modules.setdefault("libzim", types.ModuleType("libzim"))
libzim.__path__ = []
reader = sys.modules.setdefault("libzim.reader", types.ModuleType("libzim.reader"))
reader.Archive = object
reader.set_cluster_cache_max_size = lambda size: None

from zimantic.zim import (
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_PREVIEW_CHARS,
    disambiguation_members,
    extract_excerpt,
    is_javascript_shell,
    iter_text_blocks,
    is_disambiguation,
    read_entry,
)


HTML = b"""
<html>
  <head><title>Not article text</title><script>ignored()</script></head>
  <body>
    <h1>Heading</h1>
    <p>Short lead.</p>
    <p>A sufficiently long paragraph with useful text that passes the minimum lead length.<sup class="reference">[1]</sup></p>
    <p>Second paragraph with more information.</p>
    <style>.ignored { display: none; }</style>
  </body>
</html>
"""


LIST_DEFINITION_HTML = b"""
<html><body>
  <h2>Noun</h2>
  <table><tr><td><p>Singular tire</p></td></tr></table>
  <ol><li>A tire is the outer part of a car wheel. It is usually made of rubber.</li></ol>
  <div class="zim-footer">This article is issued from Wiktionary. The text is available under a permissive license.</div>
</body></html>
"""

LIST_BEFORE_PARAGRAPH_HTML = b"""
<html><body>
  <ol><li>A fallback definition that should lose to a later paragraph with the preferred article summary.</li></ol>
  <p>The preferred paragraph summary is used whenever the page provides one.</p>
</body></html>
"""

LIST_BEFORE_BLOCK_HTML = b"""
<html><body>
  <ol><li>A fallback definition that should lose to a later block with the article summary.</li></ol>
  <div>The preferred block summary is used when no paragraph is available.</div>
</body></html>
"""


class _Item:
    mimetype = "text/html"
    content = HTML


class _Entry:
    is_redirect = False
    title = "Example"
    path = "example"

    def get_item(self):
        return _Item()


class _Archive:
    def _get_entry_by_id(self, index):
        return _Entry()


class TextExtractionTests(unittest.TestCase):
    def test_extraction_prefers_first_substantial_paragraph(self):
        self.assertEqual(
            read_entry(_Archive(), 0)[2],
            "A sufficiently long paragraph with useful text that passes the minimum lead length.",
        )

    def test_generator_accepts_ordered_list_definitions(self):
        self.assertEqual(
            list(iter_text_blocks(LIST_DEFINITION_HTML)),
            ["A tire is the outer part of a car wheel. It is usually made of rubber."],
        )

    def test_generator_prefers_later_paragraph_to_list_fallback(self):
        self.assertEqual(
            list(iter_text_blocks(LIST_BEFORE_PARAGRAPH_HTML)),
            [
                "The preferred paragraph summary is used whenever the page provides one.",
                "A fallback definition that should lose to a later paragraph with the preferred article summary.",
            ],
        )

    def test_generator_prefers_block_fallback_to_list_fallback(self):
        self.assertEqual(
            list(iter_text_blocks(LIST_BEFORE_BLOCK_HTML)),
            [
                "The preferred block summary is used when no paragraph is available.",
                "A fallback definition that should lose to a later block with the article summary.",
            ],
        )

    def test_html_limit_is_configurable(self):
        self.assertEqual(DEFAULT_MAX_HTML_BYTES, 4 * 1024 * 1024)
        self.assertEqual(read_entry(_Archive(), 0, max_html_bytes=20)[2], "")

    def test_preview_limit_is_configurable(self):
        self.assertEqual(DEFAULT_PREVIEW_CHARS, 1000)
        excerpt = read_entry(_Archive(), 0, max_preview_chars=20)[2]
        self.assertLessEqual(len(excerpt), 20)
        self.assertEqual(excerpt, "A sufficiently long")

    def test_preview_skips_oversized_blocks_and_keeps_searching(self):
        html = (
            b"<p>" + b"x" * 100 + b"</p>"
            b"<p>" + b"y" * 55 + b"</p>"
        )
        excerpt = extract_excerpt(html, max_preview_chars=60)
        self.assertEqual(excerpt, "y" * 55)

    def test_preview_truncates_best_block_when_none_fits(self):
        excerpt = extract_excerpt(
            b"<p>one two three four five six seven eight nine ten eleven twelve</p>",
            max_preview_chars=20,
            preview_overflow="truncate",
        )
        self.assertEqual(excerpt, "one two three four")

    def test_embedding_excerpt_stays_within_token_budget(self):
        def token_count(text, prefix):
            return 2 + len((prefix + text).split())

        def truncate(text, prefix):
            available = 8 - token_count("", prefix)
            return " ".join(text.split()[:max(0, available)])

        excerpt = extract_excerpt(
            b"<p>" + b"one two three four five six seven eight nine ten " * 5 + b"</p>",
            title="Example",
            max_preview_chars=10,
            max_embedding_tokens=8,
            embedding_token_count=token_count,
            embedding_truncate=truncate,
        )
        self.assertLessEqual(token_count(excerpt, "passage: Example\n"), 8)
        self.assertEqual(excerpt, "one two three four")

    def test_embedding_skip_policy_keeps_looking_for_a_fitting_block(self):
        def token_count(text, prefix):
            return 2 + len((prefix + text).split())

        excerpt = extract_excerpt(
            (
                b"<p>" + b"x " * 100 + b"</p>"
                b"<p>" + b"y" * 55 + b"</p>"
            ),
            title="Example",
            max_preview_chars=20,
            max_embedding_tokens=20,
            embedding_overflow="skip",
            embedding_token_count=token_count,
            embedding_truncate=lambda *_args, **_kwargs: self.fail("unexpected truncation"),
        )
        self.assertEqual(excerpt, "y" * 55)


DISAMBIG_HTML = b"""
<html><body>
  <p>Advocacy may refer to:</p>
  <ul>
    <li><a href="Advocacy">Advocacy</a></li>
    <li><a href="Lawyer" class="mw-redirect">Lawyer</a></li>
    <li><a href="../wiki/Category:Law">Category:Law</a></li>
    <li><a href="https://example.org/external">External</a></li>
    <li><a href="#top">Top</a></li>
  </ul>
</body></html>
"""


BOILERPLATE_HTML = b"<html><body><p>Lead.</p><p><i>This disambiguation page lists articles.</i></p></body></html>"
WGCATEGORY_HTML = b'<html><head><script>RLCONF={"wgCategories":["Mainspace disambiguation pages"]}</script></head><body></body></html>'
CATEGORY_LINK_HTML = b'<html><body><a href="../wiki/Category:Disambiguation_pages">cat</a></body></html>'


class DisambiguationTests(unittest.TestCase):
    def test_title_suffix_detects_hubs(self):
        self.assertTrue(is_disambiguation("Air (disambiguation)", "Air is a mixture of gases."))
        self.assertTrue(is_disambiguation("Foo (disambiguation)", ""))

    def test_template_footer_detects_hubs(self):
        self.assertTrue(is_disambiguation("Abigail", "Abigail This disambiguation page."))
        self.assertTrue(is_disambiguation("Abigail", "", BOILERPLATE_HTML))

    def test_category_detects_hubs(self):
        self.assertTrue(is_disambiguation("Collected Poems", "", WGCATEGORY_HTML))
        self.assertTrue(is_disambiguation("Air", "", CATEGORY_LINK_HTML))

    def test_prose_and_plain_pages_are_not_hubs(self):
        self.assertFalse(is_disambiguation("Air", "Air is a mixture of gases."))
        self.assertFalse(is_disambiguation("Absolutism", "The term may refer to stances.", b"<html></html>"))

    def test_members_resolve_relative_links_and_drop_namespaces(self):
        members = disambiguation_members(DISAMBIG_HTML, "Advocacy_(disambiguation)")
        self.assertEqual([member["path"] for member in members], ["Advocacy", "Lawyer"])
        self.assertEqual(members[0]["title"], "Advocacy")


SPA_STUB = (
    b"<html><head><title>Collected Poems</title>"
    b'<meta http-equiv="refresh" content="0;URL=\'../index.html#/Bookshelves/Poetry/Collected_Poems\'" />'
    b"</head><body></body></html>"
)
SPA_BODY = json.dumps({
    "htmlBody": "<p>" + ("A real article about collected poems and their history. " * 4) + "</p>",
}).encode("utf-8")

SHELL_HTML = (
    b'<html><body><div id="app"></div><noscript><p>JavaScript is disabled in '
    b"your browser. Please enable JavaScript to access content inside this ZIM."
    b"</p></noscript></body></html>"
)


class _SpaItem:
    mimetype = "text/html"

    def __init__(self, content):
        self.content = content


class _HtmlEntry:
    is_redirect = False

    def __init__(self, title, path, content):
        self.title = title
        self.path = path
        self._index = 0
        self._content = content

    def get_item(self):
        return _SpaItem(self._content)


class _JsonEntry:
    def __init__(self, path, content):
        self.path = path
        self._content = content

    def get_item(self):
        return _SpaItem(self._content)


class _SpaArchive:
    """Minimal Archive exposing one app-shell article and its content JSON."""

    def _get_entry_by_id(self, _index):
        return _HtmlEntry("Collected Poems", "index/page_42", SPA_STUB)

    def has_entry_by_path(self, path):
        return path in ("index.html", "content/page_content_42.json")

    def get_entry_by_path(self, path):
        if path == "content/page_content_42.json":
            return _JsonEntry(path, SPA_BODY)
        return _HtmlEntry("index.html", "index.html", SHELL_HTML)


class _ShellArchive:
    def _get_entry_by_id(self, _index):
        return _HtmlEntry("index.html", "index.html", SHELL_HTML)


class JavaScriptShellTests(unittest.TestCase):
    def test_spa_stub_uses_companion_json_and_keeps_its_own_path(self):
        row = read_entry(_SpaArchive(), 0)

        self.assertEqual(row[1], "Collected Poems")
        self.assertIn("real article about collected poems", row[2])
        self.assertEqual(row[3], "index/page_42")  # deep link, not the shell
        self.assertIsNone(row[4])                  # not a redirect onto index.html

    def test_javascript_shell_pages_are_not_indexed(self):
        self.assertIsNone(read_entry(_ShellArchive(), 0))

    def test_javascript_shell_detection_ignores_real_articles(self):
        article = (
            b"<html><body><p>JavaScript is disabled by default in many browsers, "
            b"but this article explains how to enable it safely.</p></body></html>"
        )
        self.assertFalse(
            is_javascript_shell(
                article,
                "JavaScript is disabled by default in many browsers, but this article "
                "explains how to enable it safely.",
            )
        )
        self.assertTrue(
            is_javascript_shell(
                SHELL_HTML,
                "JavaScript is disabled in your browser. Please enable JavaScript to "
                "access content inside this ZIM.",
            )
        )


if __name__ == "__main__":
    unittest.main()
