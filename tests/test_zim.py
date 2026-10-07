import sys
import types
import unittest


libzim = sys.modules.setdefault("libzim", types.ModuleType("libzim"))
libzim.__path__ = []
reader = sys.modules.setdefault("libzim.reader", types.ModuleType("libzim.reader"))
reader.Archive = object
reader.set_cluster_cache_max_size = lambda size: None

from zimantic.zim import DEFAULT_MAX_HTML_BYTES, first_paragraph, full_text, read_entry


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


if __name__ == "__main__":
    unittest.main()
