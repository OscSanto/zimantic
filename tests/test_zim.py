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
    disambiguation_title,
    extract_excerpt,
    extract_pdf_excerpt,
    is_javascript_shell,
    iter_text_blocks,
    is_disambiguation,
    read_entry,
    truncate_at_word_boundary,
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

LIST_WITHOUT_WHITESPACE_HTML = b"""
<html><body>
  <p><div><ul>
    <li><a>TitlePage</a></li>
    <li><a>InfoPage</a></li>
    <li><a>Table of Contents</a></li>
    <li><a>Licensing</a></li>
    <li><a>About this Book</a></li>
  </ul></div></p>
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


def _pdf_bytes(text: str, *, pages: int = 1) -> bytes:
    """Build a small in-memory PDF with a real text layer."""
    import pymupdf

    document = pymupdf.open()
    for _ in range(pages):
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 520, 760), text, fontsize=11)
    data = document.tobytes()
    document.close()
    return data


def _pdf_archive(content: bytes, *, title: str = "Annual Report", path: str = "files/report.pdf"):
    """Minimal Archive exposing one application/pdf entry."""

    class _PdfItem:
        mimetype = "application/pdf"

        def __init__(self):
            self.content = content

    class _PdfEntry:
        is_redirect = False

        def __init__(self):
            self.title = title
            self.path = path

        def get_item(self):
            return _PdfItem()

    class _PdfArchive:
        def _get_entry_by_id(self, _index):
            return _PdfEntry()

    return _PdfArchive()


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

    def test_generator_separates_adjacent_block_elements(self):
        self.assertEqual(
            list(iter_text_blocks(LIST_WITHOUT_WHITESPACE_HTML)),
            ["TitlePage InfoPage Table of Contents Licensing About this Book"],
        )

    def test_generator_ignores_stub_boilerplate(self):
        html = (
            b"<p>A useful article paragraph with enough text to be indexed.</p>"
            b"<p>This article or its section is a stub.</p>"
            b"<p>You can help by expanding the article.</p>"
            b"<p>Our robots.txt blocks googlebot.</p>"
            b"<p>You're wasting your own time by spamming here.</p>"
        )
        self.assertEqual(
            list(iter_text_blocks(html)),
            ["A useful article paragraph with enough text to be indexed."],
        )

    def test_html_limit_is_configurable(self):
        self.assertEqual(DEFAULT_MAX_HTML_BYTES, 4 * 1024 * 1024)
        self.assertEqual(read_entry(_Archive(), 0, max_html_bytes=20)[2], "")

    def test_preview_limit_is_configurable(self):
        self.assertEqual(DEFAULT_PREVIEW_CHARS, 1000)
        excerpt = read_entry(_Archive(), 0, max_preview_chars=20)[2]
        self.assertLessEqual(len(excerpt), 20)
        self.assertEqual(excerpt, "A sufficiently long")

    def test_empty_article_body_has_no_excerpt(self):
        self.assertEqual(extract_excerpt(b"<html><body></body></html>"), "")

    def test_preview_fills_a_realistic_article_paragraph(self):
        words = " ".join(f"article{i}" for i in range(400))
        excerpt = extract_excerpt(
            f"<p>{words}</p>".encode(),
            max_preview_chars=750,
        )
        self.assertLessEqual(len(excerpt), 750)
        self.assertGreater(len(excerpt), 700)
        self.assertGreaterEqual(len(excerpt.split()), 70)

    def test_unbroken_text_keeps_a_bounded_excerpt(self):
        excerpt = truncate_at_word_boundary("x" * 1000, 750)
        self.assertEqual(len(excerpt), 750)

    def test_preview_truncates_an_oversized_block_instead_of_skipping_it(self):
        # The stored excerpt is the opening prefix: an oversized block is cut,
        # not skipped in favor of a smaller later one.
        html = (
            b"<p>" + b"x" * 100 + b"</p>"
            b"<p>" + b"y" * 55 + b"</p>"
        )
        excerpt = extract_excerpt(html, max_preview_chars=60)
        self.assertEqual(excerpt, "x" * 60)

    def test_preview_truncates_best_block_when_none_fits(self):
        excerpt = extract_excerpt(
            b"<p>one two three four five six seven eight nine ten eleven twelve</p>",
            max_preview_chars=20,
        )
        self.assertEqual(excerpt, "one two three four")

    def test_page_chrome_is_skipped(self):
        html = (
            b"<p>An article paragraph long enough to be indexed as the lead text.</p>"
            b'<div id="catlinks" class="catlinks catlinks-allhidden">'
            b"<p>Hidden categories: Articles with short description.</p></div>"
            b'<div class="navbox"><p>Navigation box linking to related topics everywhere.</p></div>'
            b'<div aria-hidden="true"><p>Hidden accessibility text that should not be indexed.</p></div>'
            b'<div role="navigation"><p>Menu links that are chrome and should be skipped.</p></div>'
        )
        excerpt = extract_excerpt(html, max_preview_chars=1000)
        self.assertTrue(excerpt.startswith("An article paragraph"))
        for gone in ("Hidden categories", "Navigation box", "accessibility text", "Menu links"):
            self.assertNotIn(gone, excerpt)

    def test_block_boundaries_separate_text_runs(self):
        html = (
            b"<div><dt>A heading</dt>"
            b"<dd>The definition is long enough to be captured as a block here.</dd></div>"
        )
        block = list(iter_text_blocks(html))[0]
        self.assertIn("A heading The definition", block)
        self.assertNotIn("A headingThe", block)

    def test_plain_text_entries_are_indexed(self):
        class _PlainItem:
            mimetype = "text/plain"
            content = b"Plain text article.\n\nSecond paragraph with more useful detail."

        class _PlainEntry:
            is_redirect = False
            title = "Plain"
            path = "plain"

            def get_item(self):
                return _PlainItem()

        class _PlainArchive:
            def _get_entry_by_id(self, _index):
                return _PlainEntry()

        row = read_entry(_PlainArchive(), 0)
        self.assertEqual(
            row[2], "Plain text article. Second paragraph with more useful detail."
        )

    def test_tiny_plain_text_assets_are_not_indexed(self):
        class _TinyItem:
            mimetype = "text/plain"
            content = b"*"

        class _TinyEntry:
            is_redirect = False
            title = ".gitignore"
            path = ".gitignore"

            def get_item(self):
                return _TinyItem()

        class _TinyArchive:
            def _get_entry_by_id(self, _index):
                return _TinyEntry()

        self.assertIsNone(read_entry(_TinyArchive(), 0))

    def test_bundled_plain_text_licenses_are_not_indexed(self):
        class _LicenseItem:
            mimetype = "text/plain"
            # Long enough to clear MIN_BLOCK_CHARS; must still be rejected by path.
            content = (
                b"Redistribution and use in source and binary forms, with or "
                b"without modification, are permitted provided that the "
                b"conditions of this license are met."
            )

        class _LicenseEntry:
            is_redirect = False
            title = "COPYING"
            path = "assets/ogvjs/COPYING"

            def get_item(self):
                return _LicenseItem()

        class _LicenseArchive:
            def _get_entry_by_id(self, _index):
                return _LicenseEntry()

        self.assertIsNone(read_entry(_LicenseArchive(), 0))

    def test_plain_text_article_named_like_a_license_is_indexed(self):
        class _ArticleItem:
            mimetype = "text/plain"
            content = (
                b"License to Wed is a 2007 comedy film starring Robin Williams "
                b"and directed by Ken Kwapis, released in theaters that summer."
            )

        class _ArticleEntry:
            is_redirect = False
            title = "License to Wed"
            path = "License_to_Wed"

            def get_item(self):
                return _ArticleItem()

        class _ArticleArchive:
            def _get_entry_by_id(self, _index):
                return _ArticleEntry()

        row = read_entry(_ArticleArchive(), 0)
        self.assertIsNotNone(row)
        self.assertIn("comedy film", row[2])

    def test_lowercase_plain_text_dictionary_entry_is_indexed(self):
        class _WordItem:
            mimetype = "text/plain"
            content = (
                b"license: permission, especially official permission to do "
                b"something or to use something owned by another party."
            )

        class _WordEntry:
            is_redirect = False
            title = "license"
            path = "license"

            def get_item(self):
                return _WordItem()

        class _WordArchive:
            def _get_entry_by_id(self, _index):
                return _WordEntry()

        row = read_entry(_WordArchive(), 0)
        self.assertIsNotNone(row)
        self.assertIn("permission", row[2])

    def test_plain_text_asset_with_query_string_is_not_indexed(self):
        class _LogoItem:
            mimetype = "text/plain"
            content = (
                b"Copyright (C) 2015-2016 The R Foundation. You can distribute "
                b"this logo under the Creative Commons Attribution-ShareAlike "
                b"4.0 International license."
            )

        class _LogoEntry:
            is_redirect = False
            title = "Rlogo.svg"
            path = "content/stats.example.org/@api/deki/files/1/Rlogo.svg?revision=1"

            def get_item(self):
                return _LogoItem()

        class _LogoArchive:
            def _get_entry_by_id(self, _index):
                return _LogoEntry()

        self.assertIsNone(read_entry(_LogoArchive(), 0))

    def test_pdf_text_layer_is_indexed(self):
        content = _pdf_bytes(
            "Tetanus is a serious bacterial infection caused by Clostridium "
            "tetani. It affects the nervous system and causes muscle spasms."
        )
        row = read_entry(_pdf_archive(content), 0)
        self.assertEqual(row[1], "Annual Report")
        self.assertIn("bacterial infection", row[2])
        self.assertEqual(row[3], "files/report.pdf")
        self.assertIsNone(row[4])

    def test_pdf_excerpt_is_bounded_to_preview_chars(self):
        content = _pdf_bytes(" ".join(f"word{i}" for i in range(400)))
        excerpt = extract_pdf_excerpt(content, max_preview_chars=120)
        self.assertLessEqual(len(excerpt), 120)
        self.assertGreater(len(excerpt), 100)

    def test_scanned_pdf_without_text_layer_is_skipped(self):
        self.assertIsNone(read_entry(_pdf_archive(_pdf_bytes("")), 0))

    def test_malformed_pdf_is_skipped(self):
        self.assertIsNone(read_entry(_pdf_archive(b"%PDF-1.4 not really a pdf"), 0))
        self.assertEqual(extract_pdf_excerpt(b"not a pdf at all"), "")

    def test_fast_build_stores_pdf_title_without_body(self):
        row = read_entry(_pdf_archive(b"unused"), 0, fast=True)
        self.assertEqual(row[1], "Annual Report")
        self.assertEqual(row[2], "")
        self.assertEqual(row[3], "files/report.pdf")

    def test_pdf_text_spans_multiple_pages(self):
        content = _pdf_bytes(
            "First page text about introductory material and background.",
            pages=2,
        )
        excerpt = extract_pdf_excerpt(content, max_preview_chars=1000)
        self.assertEqual(excerpt.count("introductory material"), 2)

    def test_app_shell_with_description_is_not_indexed(self):
        html = (
            b'<html><head><meta name="description" content="My App">'
            b'</head><body><div id="app"></div></body></html>'
        )

        class _ShellItem:
            mimetype = "text/html"
            content = html

        class _ShellEntry:
            is_redirect = False
            title = "index.html"
            path = "index.html"

            def get_item(self):
                return _ShellItem()

        class _ShellArchive:
            def _get_entry_by_id(self, _index):
                return _ShellEntry()

        self.assertIsNone(read_entry(_ShellArchive(), 0))

    def test_meta_description_is_a_fallback_excerpt(self):
        html = (
            b'<html><head><meta name="description" '
            b'content="A short summary of the topic for search.">'
            b"</head><body></body></html>"
        )

        class _MetaItem:
            mimetype = "text/html"
            content = html

        class _MetaEntry:
            is_redirect = False
            title = "Meta"
            path = "meta"

            def get_item(self):
                return _MetaItem()

        class _MetaArchive:
            def _get_entry_by_id(self, _index):
                return _MetaEntry()

        self.assertEqual(read_entry(_MetaArchive(), 0)[2], "A short summary of the topic for search.")

    def test_spa_body_accepts_alternate_json_keys(self):
        alt_body = json.dumps({
            "body": "<p>" + ("Alternate key article text about the topic. " * 6) + "</p>",
        }).encode("utf-8")

        class _AltSpaArchive(_SpaArchive):
            def get_entry_by_path(self, path):
                if path == "content/page_content_42.json":
                    return _JsonEntry(path, alt_body)
                return super().get_entry_by_path(path)

        row = read_entry(_AltSpaArchive(), 0)
        self.assertIn("Alternate key article text", row[2])
        self.assertEqual(row[3], "index/page_42")


BOILERPLATE_HTML = b"<html><body><p>Lead.</p><p><i>This disambiguation page lists articles.</i></p></body></html>"
WGCATEGORY_HTML = b'<html><head><script>RLCONF={"wgCategories":["Mainspace disambiguation pages"]}</script></head><body></body></html>'
CATEGORY_LINK_HTML = b'<html><body><a href="../wiki/Category:Disambiguation_pages">cat</a></body></html>'


class _DisambiguationItem:
    mimetype = "text/html"
    content = CATEGORY_LINK_HTML


class _DisambiguationEntry:
    is_redirect = False
    title = "Air"
    path = "Air"

    def get_item(self):
        return _DisambiguationItem()


class _DisambiguationArchive:
    def _get_entry_by_id(self, _index):
        return _DisambiguationEntry()


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

    def test_disambiguation_title_gets_required_suffix_without_duplicates(self):
        self.assertEqual(disambiguation_title("Air"), "Air (disambiguation)")
        self.assertEqual(disambiguation_title("Air (disambiguation)"), "Air (disambiguation)")

    def test_read_entry_persists_disambiguation_suffix(self):
        row = read_entry(_DisambiguationArchive(), 0)
        self.assertEqual(row[1], "Air (disambiguation)")
        self.assertEqual(len(row), 5)


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


def _media_archive(stub, title, stub_path, companion_path, companion_body):
    """Archive whose shell stub resolves a ``<slug>.json`` companion (youtube2zim)."""

    class _StubEntry:
        is_redirect = False
        path = stub_path

        def __init__(self):
            self.title = title

        def get_item(self):
            return _SpaItem(stub)

    class _MediaArchive:
        def _get_entry_by_id(self, _index):
            return _StubEntry()

        def has_entry_by_path(self, path):
            return path in ("index.html", companion_path)

        def get_entry_by_path(self, path):
            if path == companion_path:
                return _JsonEntry(path, companion_body)
            return _HtmlEntry("index.html", "index.html", SHELL_HTML)

    return _MediaArchive()


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

    def test_media_bundle_video_description_is_indexed(self):
        stub = (
            b"<html><head><title>Getting Started</title>"
            b"<meta http-equiv=\"refresh\" content=\"0;URL='../index.html"
            b"#/watch/getting-started-abc?list=sql-basics'\" /></head><body></body></html>"
        )
        body = json.dumps({
            "id": "abc123",
            "title": "Getting Started",
            "description": "In this video we begin learning the basics. " * 5,
        }).encode("utf-8")

        row = read_entry(
            _media_archive(
                stub, "Getting Started", "index/getting-started-abc",
                "videos/getting-started-abc.json", body,
            ),
            0,
        )

        self.assertEqual(row[1], "Getting Started")
        self.assertIn("begin learning the basics", row[2])
        self.assertEqual(row[3], "index/getting-started-abc")
        self.assertIsNone(row[4])

    def test_media_bundle_playlist_description_is_indexed(self):
        stub = (
            b"<html><head><title>SQL Tutorials</title>"
            b"<meta http-equiv=\"refresh\" content=\"0;URL='../index.html"
            b"#/playlist/sql-basics'\" /></head><body></body></html>"
        )
        body = json.dumps({
            "title": "SQL Tutorials",
            "description": "An in-depth look at SQL and relational databases. " * 5,
        }).encode("utf-8")

        row = read_entry(
            _media_archive(
                stub, "SQL Tutorials", "index/sql-basics",
                "playlists/sql-basics.json", body,
            ),
            0,
        )

        self.assertEqual(row[1], "SQL Tutorials")
        self.assertIn("in-depth look at SQL", row[2])
        self.assertEqual(row[3], "index/sql-basics")

    def test_html_companion_without_visible_text_does_not_leak_markup(self):
        empty = json.dumps({
            "htmlBody": '<p id="indexLetterList"> </p> <div id="indexTable"> </div>',
        }).encode("utf-8")

        class _EmptyArchive(_SpaArchive):
            def get_entry_by_path(self, path):
                if path == "content/page_content_42.json":
                    return _JsonEntry(path, empty)
                return super().get_entry_by_path(path)

        row = read_entry(_EmptyArchive(), 0)

        self.assertEqual(row[2], "")            # no markup leaked as text
        self.assertEqual(row[3], "index.html")  # falls back to the shell target

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
