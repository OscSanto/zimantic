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
    extract_excerpts,
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
            ["The preferred paragraph summary is used whenever the page provides one."],
        )

    def test_generator_prefers_block_fallback_to_list_fallback(self):
        self.assertEqual(
            list(iter_text_blocks(LIST_BEFORE_BLOCK_HTML)),
            ["The preferred block summary is used when no paragraph is available."],
        )

    def test_html_limit_is_configurable(self):
        self.assertEqual(DEFAULT_MAX_HTML_BYTES, 4 * 1024 * 1024)
        self.assertEqual(read_entry(_Archive(), 0, max_html_bytes=20)[2], "")

    def test_preview_limit_is_configurable(self):
        self.assertEqual(DEFAULT_PREVIEW_CHARS, 1000)
        self.assertEqual(
            len(read_entry(_Archive(), 0, max_preview_chars=20)[2]),
            20,
        )

    def test_preview_skips_oversized_blocks_and_keeps_searching(self):
        html = (
            b"<p>" + b"x" * 100 + b"</p>"
            b"<p>" + b"y" * 55 + b"</p>"
        )
        preview, _ = extract_excerpts(html, max_preview_chars=60)
        self.assertEqual(preview, "y" * 55)

    def test_preview_truncates_best_block_when_none_fits(self):
        preview, _ = extract_excerpts(
            b"<p>" + b"x" * 80 + b"</p>",
            max_preview_chars=20,
            preview_overflow="skip",
        )
        self.assertEqual(preview, "x" * 20)

    def test_embedding_excerpt_stays_within_token_budget(self):
        def token_count(text, prefix):
            return 2 + len((prefix + text).split())

        def truncate(text, prefix):
            available = 8 - token_count("", prefix)
            return " ".join(text.split()[:max(0, available)])

        _, embedding = extract_excerpts(
            b"<p>" + b"one two three four five six seven eight nine ten " * 5 + b"</p>",
            title="Example",
            max_embedding_tokens=8,
            embedding_token_count=token_count,
            embedding_truncate=truncate,
        )
        self.assertLessEqual(token_count(embedding, "passage: Example\n"), 8)

    def test_embedding_skip_policy_keeps_looking_for_a_fitting_block(self):
        def token_count(text, prefix):
            return 2 + len((prefix + text).split())

        _, embedding = extract_excerpts(
            (
                b"<p>" + b"x " * 100 + b"</p>"
                b"<p>" + b"y " * 10 + b"</p>"
            ),
            title="Example",
            max_embedding_tokens=20,
            embedding_overflow="skip",
            embedding_token_count=token_count,
            embedding_truncate=lambda *_args, **_kwargs: self.fail("unexpected truncation"),
        )
        self.assertEqual(embedding, " ".join(["y"] * 10))


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


class DisambiguationTests(unittest.TestCase):
    def test_title_suffix_and_early_marker_detect_hubs(self):
        self.assertTrue(is_disambiguation("Air (disambiguation)", ""))
        self.assertTrue(is_disambiguation("Advocacy", "Advocacy may refer to:"))
        self.assertFalse(is_disambiguation("Air", "Air is a mixture of gases."))

    def test_late_marker_is_not_a_hub(self):
        # Prose or a navbox that mentions the phrase far from the lead.
        self.assertFalse(is_disambiguation("Absolutism", "Absolutism " + "x" * 300 + " may refer to stances."))

    def test_members_resolve_relative_links_and_drop_namespaces(self):
        members = disambiguation_members(DISAMBIG_HTML, "Advocacy_(disambiguation)")
        self.assertEqual([member["path"] for member in members], ["Advocacy", "Lawyer"])
        self.assertEqual(members[0]["title"], "Advocacy")

    def test_suffix_hub_with_no_links_is_still_flagged(self):
        self.assertTrue(is_disambiguation("Foo (disambiguation)", "Foo is a thing."))


if __name__ == "__main__":
    unittest.main()
