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
    disambiguation_members,
    first_paragraph,
    full_text,
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
    def test_default_extraction_reads_full_visible_text(self):
        self.assertEqual(
            read_entry(_Archive(), 0)[2],
            "Heading Short lead. A sufficiently long paragraph with useful text "
            "that passes the minimum lead length. Second paragraph with more information.",
        )

    def test_first_paragraph_mode_keeps_first_substantial_paragraph(self):
        self.assertEqual(
            read_entry(_Archive(), 0, first_paragraph=True)[2],
            "A sufficiently long paragraph with useful text that passes the minimum lead length.",
        )

    def test_html_limit_is_configurable(self):
        self.assertEqual(DEFAULT_MAX_HTML_BYTES, 4 * 1024 * 1024)
        self.assertEqual(read_entry(_Archive(), 0, max_html_bytes=20)[2], "")


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
