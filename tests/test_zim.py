import json
import subprocess
import sys
import tomllib
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from packaging.requirements import Requirement

try:
    import libzim.reader
except ImportError:
    libzim = types.ModuleType("libzim")
    libzim.__path__ = []
    reader = types.ModuleType("libzim.reader")
    reader.Archive = object
    reader.set_cluster_cache_max_size = lambda size: None
    sys.modules["libzim"] = libzim
    sys.modules["libzim.reader"] = reader

from zimantic.content import reader as content_reader
from zimantic.content.reader import (
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_PREVIEW_CHARS,
    EntryRecord,
    _markdown_source,
    _phet_strings,
    _TextExtractor,
    disambiguation_title,
    extract_excerpt,
    extract_pdf_excerpt,
    is_disambiguation,
    is_javascript_shell,
    iter_text_blocks,
    normalize_archive_path,
    read_entry,
    resolve_archive_href,
    truncate_at_word_boundary,
)


def _read_entry(*args, **kwargs) -> EntryRecord:
    """Call read_entry and assert it produced an indexable record."""
    row = read_entry(*args, **kwargs)
    assert row is not None
    return row


class ArchivePathResolutionTests(unittest.TestCase):
    def test_archive_href_resolution_normalizes_and_bounds_paths(self):
        self.assertEqual(
            resolve_archive_href(
                "docs/page.html", "../images/%23icon.svg?size=small#preview"
            ),
            "images/#icon.svg",
        )
        self.assertEqual(
            resolve_archive_href(
                "docs/page.html", "/assets/%23icon.svg", unquote_path=False
            ),
            "assets/%23icon.svg",
        )
        self.assertIsNone(resolve_archive_href("docs/page.html", "../../outside.html"))
        self.assertIsNone(normalize_archive_path("/"))


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
    def get_entry_by_id(self, index):
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
        def get_entry_by_id(self, _index):
            return _PdfEntry()

    return _PdfArchive()


def _phet_archive(html: bytes, script: bytes, *, path: str, title: str):
    """Minimal Archive exposing one PhET shell and its shared JS bundle."""

    class _HtmlItem:
        mimetype = "text/html"
        content = html

    class _ScriptItem:
        mimetype = "application/javascript"

        def __init__(self):
            self.content = script
            self.size = len(script)

    class _HtmlEntry:
        is_redirect = False

        def __init__(self):
            self.title = title
            self.path = path

        def get_item(self):
            return _HtmlItem()

    class _ScriptEntry:
        path = "sim.js"

        def get_item(self):
            return _ScriptItem()

    class _PhetArchive:
        def get_entry_by_id(self, _index):
            return _HtmlEntry()

        def has_entry_by_path(self, requested):
            return requested == "sim.js"

        def get_entry_by_path(self, requested):
            assert requested == "sim.js"
            return _ScriptEntry()

    return _PhetArchive()


class TextExtractionTests(unittest.TestCase):
    @unittest.skipUnless(content_reader.LexborHTMLParser, "requires Lexbor HTML5 recovery")
    def test_omitted_paragraph_end_tags_do_not_duplicate_prose(self):
        first = "The first paragraph contains useful readable prose that should appear only once."
        second = "The second paragraph also contains useful readable prose without duplicated text."
        html = f"<div><p>{first}<p>{second}</div>".encode()
        self.assertEqual(list(iter_text_blocks(html)), [first, second])

    def test_malformed_navigation_does_not_hide_following_article(self):
        text = "Useful article prose after malformed navigation must remain visible and searchable."
        html = f"<nav><a>Home</a><div>Menu</nav><p>{text}</p>".encode()
        self.assertEqual(list(iter_text_blocks(html)), [text])

    @unittest.skipUnless(content_reader.LexborHTMLParser, "requires Lexbor DOM recovery")
    def test_deep_valid_containers_retain_prose_without_duplicates(self):
        text = "Useful article prose inside deeply nested containers should remain readable."
        html = ("<div>" * 260 + f"<p>{text}</p>" + "</div>" * 260).encode()
        self.assertEqual(list(iter_text_blocks(html)), [text])

    def test_expired_deadline_skips_dom_construction(self):
        with (
            patch("zimantic.content.reader.time.monotonic", return_value=2.0),
            patch("zimantic.content.reader.LexborHTMLParser") as parse,
        ):
            self.assertEqual(list(iter_text_blocks(HTML, deadline=1.0)), [])
        parse.assert_not_called()

    def test_traversal_deadline_retains_collected_paragraph(self):
        first = "The first paragraph contains useful readable prose before the deadline expires."
        second = "The second paragraph must not be traversed after the deadline has expired."
        html = f"<p>{first}</p>".encode() + b" " * 8192 + f"<p>{second}</p>".encode()
        now = [0.0]
        original_end = _TextExtractor.handle_endtag

        def finish_block(extractor, tag):
            original_end(extractor, tag)
            if tag == "p":
                now[0] = 2.0

        with (
            patch("zimantic.content.reader.time.monotonic", side_effect=lambda: now[0]),
            patch.object(_TextExtractor, "handle_endtag", finish_block),
        ):
            self.assertEqual(list(iter_text_blocks(html, deadline=1.0)), [first])

    def test_stdlib_fallback_preserves_visibility_language_and_utf8(self):
        text = "Useful article prose with inline formatting and a nonbreaking space & symbol."
        html = (
            '<div hidden><p>Hidden article text must never be included in a preview.</p></div>'
            '<nav><p>Navigation links are not useful article prose for indexing.</p></nav>'
            f'<p class="lang-fr">Translated prose must not displace the default language: {text}</p>'
            '<p class="lang-default">Useful article prose with <b>inline formatting</b>'
            ' and a nonbreaking&nbsp;space &amp; symbol.</p>'
        ).encode()
        boundary_text = "word " * 1636 + "a\U0001f4a1"
        with patch("zimantic.content.reader.LexborHTMLParser", None):
            self.assertEqual(list(iter_text_blocks(html)), [text])
            self.assertEqual(
                list(iter_text_blocks(f"<div><p>{boundary_text}</p></div>".encode())),
                [boundary_text],
            )
            self.assertEqual(extract_excerpt(b""), "")

    def test_stdlib_fallback_stops_before_large_preview_tail(self):
        first = "Useful first paragraph with enough visible prose for the opening preview."
        html = f"<div><p>{first}</p>".encode() + b" " * 8192 + b"<p>" + b"tail " * 3000 + b"</p></div>"
        with patch("zimantic.content.reader.LexborHTMLParser", None):
            self.assertEqual(list(iter_text_blocks(html, max_chars=50)), [first])

    def test_stdlib_fallback_returns_partial_text_on_deadline(self):
        first = "Useful first paragraph with enough visible prose before the deadline expires."
        html = f"<p>{first}</p>".encode() + b" " * 8192 + b"<p>" + b"tail " * 3000 + b"</p>"
        with (
            patch("zimantic.content.reader.LexborHTMLParser", None),
            patch("zimantic.content.reader.time.monotonic", side_effect=[0.0, 0.0, 2.0]),
        ):
            self.assertEqual(list(iter_text_blocks(html, deadline=1.0)), [first])

    def test_reader_imports_without_selectolax(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.modules['selectolax'] = None; "
                    "from zimantic.content.reader import LexborHTMLParser, extract_excerpt; "
                    "assert LexborHTMLParser is None; "
                    "text = 'Useful article prose with enough visible text for the fallback extractor.'; "
                    "assert extract_excerpt(('<p>' + text + '</p>').encode()) == text"
                ),
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_selectolax_dependency_skips_platforms_without_wheels(self):
        config = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
        requirements = [
            Requirement(value) for value in config["project"]["dependencies"]
            if Requirement(value).name == "selectolax"
        ]
        for platform, machine, implementation, expected in (
            ("linux", "armv7l", "cpython", False),
            ("linux", "i686", "cpython", False),
            ("linux", "ppc64le", "cpython", False),
            ("freebsd", "x86_64", "cpython", False),
            ("linux", "x86_64", "pypy", False),
            ("linux", "aarch64", "cpython", True),
            ("linux", "x86_64", "cpython", True),
            ("darwin", "arm64", "cpython", True),
            ("win32", "AMD64", "cpython", True),
        ):
            with self.subTest(platform=platform, machine=machine, implementation=implementation):
                environment = {
                    "sys_platform": platform,
                    "platform_machine": machine,
                    "implementation_name": implementation,
                }
                self.assertEqual(
                    any(req.marker is None or req.marker.evaluate(environment) for req in requirements),
                    expected,
                )

    def test_extraction_prefers_first_substantial_paragraph(self):
        self.assertEqual(
            _read_entry(_Archive(), 0)[2],
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

    def test_bounded_parse_matches_full_parse_when_lead_fills_budget(self):
        # A long lead paragraph fills a small budget, so parsing can stop there;
        # the bounded parse must agree with the full parse (and skip chrome).
        lead = " ".join(f"word{i}" for i in range(120))
        html = (
            b"<html><body>"
            b"<p>" + lead.encode() + b"</p>"
            b'<div class="navbox"><p>Navigation box text that must never appear.</p></div>'
            b"</body></html>"
        )
        bounded = list(iter_text_blocks(html, max_chars=200))
        self.assertEqual(bounded, list(iter_text_blocks(html)))
        self.assertEqual(len(bounded), 1)
        self.assertNotIn("Navigation", bounded[0])

    def test_bounded_extraction_can_stop_inside_an_open_lower_priority_container(self):
        # The closed paragraph fills the budget while a non-paragraph container
        # is still open. That container can only yield lower-priority text, so
        # stopping early must not change the excerpt.
        lead = " ".join(f"word{i}" for i in range(120))
        tail = " ".join(f"tail{i}" for i in range(120))
        html = (
            b"<html><body><div>"
            b"<p>" + lead.encode() + b"</p>"
            b"<span>" + (b"z" * 9000) + b"</span>"
            b"<p>" + tail.encode() + b"</p>"
            b"</div></body></html>"
        )
        excerpt = extract_excerpt(html, max_preview_chars=200)
        self.assertTrue(excerpt.startswith("word0 "))
        self.assertNotIn("tail", excerpt)

    def test_bounded_parse_does_not_stop_before_parallel_translations(self):
        # A lang-* page must still be parsed fully so the lang-default copy wins.
        html = (
            b'<p class="title lang-default">The primary language title of the talk is shown here.</p>'
            b'<p class="title lang-fr">Le titre principal de la conference est affiche ici.</p>'
        )
        self.assertEqual(
            list(iter_text_blocks(html, max_chars=50)),
            list(iter_text_blocks(html)),
        )

    def test_html_limit_is_configurable(self):
        self.assertEqual(DEFAULT_MAX_HTML_BYTES, 4 * 1024 * 1024)
        self.assertEqual(_read_entry(_Archive(), 0, max_html_bytes=20)[2], "")

    def test_entry_limit_is_checked_before_materializing_content(self):
        class _LargeItem:
            mimetype = "text/html"
            size = 100

            @property
            def content(self):
                raise AssertionError("content should not be materialized")

        class _LargeEntry:
            is_redirect = False
            title = "Large"
            path = "large"

            def get_item(self):
                return _LargeItem()

        class _LargeArchive:
            def get_entry_by_id(self, index):
                return _LargeEntry()

        self.assertIsNone(read_entry(_LargeArchive(), 0, max_entry_bytes=99))

    def test_preview_limit_is_configurable(self):
        self.assertEqual(DEFAULT_PREVIEW_CHARS, 1000)
        excerpt = _read_entry(_Archive(), 0, max_preview_chars=20)[2]
        self.assertLessEqual(len(excerpt), 20)
        self.assertEqual(excerpt, "A sufficiently long")

    def test_empty_article_body_has_no_excerpt(self):
        self.assertEqual(extract_excerpt(b"<html><body></body></html>"), "")

    def test_empty_html_entry_is_not_indexed(self):
        class _EmptyItem:
            mimetype = "text/html"
            content = b"<html><body></body></html>"

        class _EmptyEntry:
            is_redirect = False
            title = "Empty"
            path = "empty"

            def get_item(self):
                return _EmptyItem()

        class _EmptyArchive:
            def get_entry_by_id(self, index):
                return _EmptyEntry()

        self.assertIsNone(read_entry(_EmptyArchive(), 0))

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

    def test_nested_elements_inside_chrome_are_skipped(self):
        html = (
            b'<div id="footer"><div class="row"><p>Footer affiliate text that must never '
            b"appear in the excerpt at all.</p></div>"
            b"<p>More footer text that must also stay out of the excerpt.</p></div>"
            b"<p>An article paragraph long enough to be indexed as the lead text.</p>"
        )
        excerpt = extract_excerpt(html, max_preview_chars=1000)
        self.assertTrue(excerpt.startswith("An article paragraph"))
        self.assertNotIn("Footer affiliate", excerpt)
        self.assertNotIn("More footer", excerpt)

    def test_hidden_elements_are_skipped(self):
        html = (
            b"<p>An article paragraph long enough to be indexed as the lead text.</p>"
            b'<span style="display: none;">data-sort-value="furniture" hidden junk here</span>'
            b"<div hidden><p>Hidden attribute text that should never be indexed.</p></div>"
            b'<span style="visibility:hidden">Invisible text that should never be indexed.</span>'
        )
        excerpt = extract_excerpt(html, max_preview_chars=1000)
        self.assertNotIn("data-sort-value", excerpt)
        self.assertNotIn("Hidden attribute", excerpt)
        self.assertNotIn("Invisible text", excerpt)

    def test_hidden_void_elements_do_not_skip_following_content(self):
        html = (
            b"<meta hidden>"
            b'<img style="display: none;">'
            b"<p>An article paragraph after hidden void elements remains visible.</p>"
        )
        self.assertEqual(
            extract_excerpt(html),
            "An article paragraph after hidden void elements remains visible.",
        )

    def test_hidden_non_void_elements_still_skip_content(self):
        html = (
            b"<div hidden><p>Hidden content must remain excluded from the excerpt.</p></div>"
            b"<p>Visible article content follows the hidden container.</p>"
        )
        excerpt = extract_excerpt(html)
        self.assertEqual(excerpt, "Visible article content follows the hidden container.")
        self.assertNotIn("Hidden content", excerpt)

    def test_parallel_language_blocks_keep_only_the_default(self):
        html = (
            b'<p class="title lang-default">The primary language title of the talk is shown here.</p>'
            b'<p class="title lang-fr">Le titre principal de la conference est affiche ici.</p>'
            b'<p class="description lang-default">The primary language description of this talk appears here.</p>'
            b'<p class="description lang-fr">La description principale de cette conference est ici.</p>'
        )
        joined = " ".join(iter_text_blocks(html))
        self.assertIn("primary language title", joined)
        self.assertIn("primary language description", joined)
        self.assertNotIn("Le titre", joined)
        self.assertNotIn("La description", joined)

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
            def get_entry_by_id(self, _index):
                return _PlainEntry()

        row = _read_entry(_PlainArchive(), 0)
        self.assertEqual(
            row[2], "Plain text article. Second paragraph with more useful detail."
        )

    def test_plain_text_markdown_article_is_indexed(self):
        class _MarkdownItem:
            mimetype = "text/plain"
            content = (
                b"# Tire care\n\n"
                b"Check the tread depth and inflate the tires to the pressure "
                b"listed on the driver's door jamb."
            )

        class _MarkdownEntry:
            is_redirect = False
            title = "Tire care"
            path = "docs/tire-care.md"

            def get_item(self):
                return _MarkdownItem()

        class _MarkdownArchive:
            def get_entry_by_id(self, _index):
                return _MarkdownEntry()

        row = _read_entry(_MarkdownArchive(), 0)
        self.assertIsNotNone(row)
        self.assertIn("tread depth", row[2])

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
            def get_entry_by_id(self, _index):
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
            def get_entry_by_id(self, _index):
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
            def get_entry_by_id(self, _index):
                return _ArticleEntry()

        row = _read_entry(_ArticleArchive(), 0)
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
            def get_entry_by_id(self, _index):
                return _WordEntry()

        row = _read_entry(_WordArchive(), 0)
        self.assertIsNotNone(row)
        self.assertIn("permission", row[2])

    def test_mislabeled_html_asset_is_skipped(self):
        # A script served as text/html (zimit crawls do this for <site>.js) is
        # an asset, not an article, and must not become a title-only result.
        class _AssetItem:
            mimetype = "text/html"
            content = b"var app = function () { return 1; };"

        class _AssetEntry:
            is_redirect = False
            title = "site.js"
            path = "gobyexample.com/site.js"

            def get_item(self):
                return _AssetItem()

        class _AssetArchive:
            def get_entry_by_id(self, _index):
                return _AssetEntry()

        self.assertIsNone(read_entry(_AssetArchive(), 0))

    def test_html_page_at_a_js_path_is_kept(self):
        # A real article whose URL happens to end in .js (a cheatsheet about a
        # JavaScript library) contains markup and must not be mistaken for an
        # asset.
        class _CheatsheetItem:
            mimetype = "text/html"
            content = (
                b"<html><body><p>Commander.js is a complete solution for "
                b"node.js command-line programs with options and subcommands."
                b"</p></body></html>"
            )

        class _CheatsheetEntry:
            is_redirect = False
            title = "Commander.js cheatsheet"
            path = "devhints.io/commander.js"

            def get_item(self):
                return _CheatsheetItem()

        class _CheatsheetArchive:
            def get_entry_by_id(self, _index):
                return _CheatsheetEntry()

        row = _read_entry(_CheatsheetArchive(), 0)
        self.assertIsNotNone(row)
        self.assertIn("complete solution", row[2])

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
            def get_entry_by_id(self, _index):
                return _LogoEntry()

        self.assertIsNone(read_entry(_LogoArchive(), 0))

    def test_plain_text_template_and_sourcemap_assets_are_not_indexed(self):
        class _TemplateItem:
            mimetype = "text/plain"
            content = (
                b"{{#each doc.items}} <p><a href=\"{{this.fpath}}\">{{this.fname}}</a>"
                b"{{/each}}</p>"
            )

        class _TemplateEntry:
            is_redirect = False
            title = "media_player.handlebars"
            path = "media_player.handlebars"

            def get_item(self):
                return _TemplateItem()

        class _TemplateArchive:
            def get_entry_by_id(self, _index):
                return _TemplateEntry()

        self.assertIsNone(read_entry(_TemplateArchive(), 0))

    def test_pdf_text_layer_is_indexed(self):
        content = _pdf_bytes(
            "Tetanus is a serious bacterial infection caused by Clostridium "
            "tetani. It affects the nervous system and causes muscle spasms."
        )
        row = _read_entry(_pdf_archive(content), 0)
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
        row = _read_entry(_pdf_archive(b"unused"), 0, fast=True)
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
            def get_entry_by_id(self, _index):
                return _ShellEntry()

        self.assertIsNone(read_entry(_ShellArchive(), 0))

    def test_phet_compact_localization_decoder(self):
        encoded = (
            chr(15)
            + "en"
            + chr(2)
            + "ACID_BASE_SOLUTIONS"
            + chr(3)
            + "acid-base-solutions"
            + chr(1)
            + "title"
            + chr(9)
            + chr(8)
            + "en"
            + chr(12)
            + "Acid-Base Solutions"
        )
        script = (
            b"window.phet.chipper.strings = (y=>{"
            b"let A=f(1);let B=f(2);let C=f(3);let D=f(4);"
            b"let E=f(5);let F=f(6);let G=f(7);let H=f(8);"
            b"let I=f(9);let J=f(10);let K=f(11);let L=f(12);"
            b"let M=f(13);let N=f(14);let O=f(15);"
            b"// Caf\xc3\xa9\n"
            b"return m;})('"
            + encoded.encode("utf-8")
            + b"');"
        )
        strings = _phet_strings(script)
        self.assertIsNotNone(strings)
        self.assertEqual(
            strings["en"]["ACID_BASE_SOLUTIONS/acid-base-solutions.title"],
            "\u202aAcid-Base Solutions\u202c",
        )

    def test_phet_shell_indexes_localized_simulation_strings(self):
        html = (
            b'<html><head><meta name="phet-sim-level" content="production">'
            b"</head><body><script src=\"sim.js\"></script></body></html>"
        )
        strings = {
            "de": {
                "ACID_BASE_SOLUTIONS/screen.introduction": "Einführung",
                "ACID_BASE_SOLUTIONS/concentrationGraph.yAxis": (
                    "Gleichgewichtskonzentration (mol/L)"
                ),
            },
            "en": {
                "ACID_BASE_SOLUTIONS/screen.introduction": "Intro",
                "ACID_BASE_SOLUTIONS/concentrationGraph.yAxis": (
                    "Equilibrium Concentration (mol/L)"
                ),
            },
        }
        script = (
            b"window.phet.chipper.project = 'acid-base-solutions';"
            b"window.phet.chipper.locale = 'de';"
            b"window.phet.chipper.strings = "
            + json.dumps(strings).encode("utf-8")
            + b";"
        )

        class _PhetItem:
            mimetype = "text/html"
            content = html

        class _ScriptItem:
            mimetype = "application/javascript"
            content = script
            size = len(script)

        class _PhetEntry:
            is_redirect = False
            title = "Säure-Base-Lösungen"
            path = "acid-base-solutions_de.html"

            def get_item(self):
                return _PhetItem()

        class _ScriptEntry:
            path = "sim.js"

            def get_item(self):
                return _ScriptItem()

        class _PhetArchive:
            def get_entry_by_id(self, _index):
                return _PhetEntry()

            def has_entry_by_path(self, path):
                return path == "sim.js"

            def get_entry_by_path(self, path):
                assert path == "sim.js"
                return _ScriptEntry()

        row = _read_entry(_PhetArchive(), 0, max_preview_chars=500)
        self.assertIn("Gleichgewichtskonzentration", row.excerpt)
        self.assertNotIn("Equilibrium Concentration", row.excerpt)
        self.assertNotIn("\u202a", row.excerpt)

    def test_phet_mul_page_prefers_path_locale_over_shared_bundle(self):
        html = (
            b'<html><head><meta name="phet-sim-level" content="production">'
            b'</head><body><script src="sim.js"></script></body></html>'
        )
        strings = {
            "en": {"ACID_BASE_SOLUTIONS/screen.introduction": "Intro"},
            "de": {"ACID_BASE_SOLUTIONS/screen.introduction": "Einführung"},
        }
        script = (
            b"window.phet.chipper.project = 'acid-base-solutions';"
            b"window.phet.chipper.locale = 'en';"
            b"window.phet.chipper.strings = "
            + json.dumps(strings).encode("utf-8")
            + b";"
        )

        row = _read_entry(
            _phet_archive(
                html,
                script,
                path="acid-base-solutions_de.html",
                title="Saure & basische Lösungen",
            ),
            0,
            max_preview_chars=500,
        )
        self.assertIn("Einführung", row.excerpt)
        self.assertNotIn("Intro", row.excerpt)

    def test_phet_locale_matching_normalizes_separators_and_case(self):
        html = (
            b'<html lang="pt-BR"><head>'
            b'<meta name="phet-sim-level" content="production">'
            b'</head><body><script src="sim.js"></script></body></html>'
        )
        strings = {
            "en": {"ACID_BASE_SOLUTIONS/screen.introduction": "Introduction"},
            "pt_BR": {"ACID_BASE_SOLUTIONS/screen.introduction": "Introdução"},
        }
        script = (
            b"window.phet.chipper.project = 'acid-base-solutions';"
            b"window.phet.chipper.locale = 'pt-BR';"
            b"window.phet.chipper.strings = "
            + json.dumps(strings).encode("utf-8")
            + b";"
        )

        row = _read_entry(
            _phet_archive(
                html,
                script,
                path="acid-base-solutions_pt-br.html",
                title="Soluções Ácido-Base",
            ),
            0,
            max_preview_chars=500,
        )
        self.assertIn("Introdução", row.excerpt)
        self.assertNotIn("Introduction", row.excerpt)

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
            def get_entry_by_id(self, _index):
                return _MetaEntry()

        self.assertEqual(_read_entry(_MetaArchive(), 0)[2], "A short summary of the topic for search.")

    def test_spa_body_accepts_alternate_json_keys(self):
        alt_body = json.dumps({
            "body": "<p>" + ("Alternate key article text about the topic. " * 6) + "</p>",
        }).encode("utf-8")

        class _AltSpaArchive(_SpaArchive):
            def get_entry_by_path(self, path):
                if path == "content/page_content_42.json":
                    return _JsonEntry(path, alt_body)
                return super().get_entry_by_path(path)

        row = _read_entry(_AltSpaArchive(), 0)
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
    def get_entry_by_id(self, _index):
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
        row = _read_entry(_DisambiguationArchive(), 0)
        self.assertEqual(row[1], "Air (disambiguation)")
        self.assertEqual(len(row), 7)

    def test_fast_read_entry_persists_disambiguation_suffix(self):
        row = _read_entry(_DisambiguationArchive(), 0, fast=True)
        self.assertEqual(row[1], "Air (disambiguation)")
        self.assertEqual(row[2], "")
        self.assertEqual(row[3], "Air")


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

    def get_entry_by_id(self, _index):
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
        def get_entry_by_id(self, _index):
            return _StubEntry()

        def has_entry_by_path(self, path):
            return path in ("index.html", companion_path)

        def get_entry_by_path(self, path):
            if path == companion_path:
                return _JsonEntry(path, companion_body)
            return _HtmlEntry("index.html", "index.html", SHELL_HTML)

    return _MediaArchive()


class _ShellArchive:
    def get_entry_by_id(self, _index):
        return _HtmlEntry("index.html", "index.html", SHELL_HTML)


class JavaScriptShellTests(unittest.TestCase):
    def test_spa_stub_uses_companion_json_and_keeps_its_own_path(self):
        row = _read_entry(_SpaArchive(), 0)

        self.assertEqual(row[1], "Collected Poems")
        self.assertIn("real article about collected poems", row[2])
        self.assertEqual(row[3], "index/page_42")  # deep link, not the shell
        self.assertIsNone(row[4])                  # not a redirect onto index.html

    def test_fast_spa_stub_uses_companion_json_and_keeps_its_own_path(self):
        row = _read_entry(_SpaArchive(), 0, fast=True)

        self.assertEqual(row[1], "Collected Poems")
        self.assertEqual(row[2], "")               # fast indexes store no body text
        self.assertEqual(row[3], "index/page_42")
        self.assertIsNone(row[4])

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

        row = _read_entry(
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

        row = _read_entry(
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

        row = _read_entry(_EmptyArchive(), 0)

        self.assertEqual(row[2], "")            # no markup leaked as text
        self.assertEqual(row[3], "index.html")  # falls back to the shell target

    def test_oversized_json_companion_is_not_materialized(self):
        class _LargeJsonItem(_SpaItem):
            size = 10_000

        class _LargeJsonEntry(_JsonEntry):
            def get_item(self):
                return _LargeJsonItem(SPA_BODY)

        class _LargeArchive(_SpaArchive):
            def get_entry_by_path(self, path):
                if path == "content/page_content_42.json":
                    return _LargeJsonEntry(path, SPA_BODY)
                return super().get_entry_by_path(path)

        row = _read_entry(_LargeArchive(), 0, max_entry_bytes=100)
        self.assertEqual(row[2], "")
        self.assertEqual(row[3], "index.html")

    def test_markdown_companion_body_is_indexed(self):
        stub = (
            b"<html><head><title>Find the Symmetric Difference</title>"
            b"<meta http-equiv=\"refresh\" content=\"0;URL='../../../index.html"
            b"#coding-interview-prep/algorithms/find-the-symmetric-difference'\" />"
            b"</head><body></body></html>"
        )
        markdown = (
            b"---\nid: a3f503de\n"
            b"title: Find the Symmetric Difference\n"
            b"challengeType: 1\n---\n\n"
            b"# --description--\n\n"
            b"The symmetric difference of two sets is the set of elements which are in "
            b"either of the two sets but not in both.\n\n"
            b"# --instructions--\n\n"
            b"Create a function that takes two or more arrays and returns their "
            b"symmetric difference.\n\n"
            b"```js\nassert.sameMembers(sym([1, 2, 3], [5, 2, 1, 4]), [3, 4, 5]);\n```\n"
        )
        md_path = "content/curriculum/coding-interview-prep/algorithms/find-the-symmetric-difference.md"

        class _MarkdownItem:
            mimetype = "text/markdown"

            def __init__(self, content):
                self.content = content

        class _MarkdownEntry(_HtmlEntry):
            def get_item(self):
                return _MarkdownItem(self._content)

        class _StubEntry:
            is_redirect = False
            path = "index/coding-interview-prep/algorithms/find-the-symmetric-difference.html"
            title = "Find the Symmetric Difference"

            def get_item(self):
                return _SpaItem(stub)

        class _MarkdownArchive:
            def get_entry_by_id(self, _index):
                return _StubEntry()

            def has_entry_by_path(self, path):
                return path in ("index.html", md_path)

            def get_entry_by_path(self, path):
                if path == md_path:
                    return _MarkdownEntry("Find the Symmetric Difference", path, markdown)
                return _HtmlEntry("index.html", "index.html", SHELL_HTML)

        row = _read_entry(_MarkdownArchive(), 0)

        self.assertEqual(row[1], "Find the Symmetric Difference")
        self.assertIn("symmetric difference of two sets", row[2])
        self.assertNotIn("--description--", row[2])
        self.assertNotIn("a3f503de", row[2])          # front matter dropped
        self.assertNotIn("assert.sameMembers", row[2])  # fenced code dropped
        self.assertEqual(row[3], _StubEntry.path)     # keeps the renderable route

    def test_markdown_companion_accepts_extension_or_mime_type(self):
        stub = (
            b"<html><head><title>Markdown Article</title>"
            b"<meta http-equiv=\"refresh\" content=\"0;URL='../index.html#article'\" />"
            b"</head><body></body></html>"
        )
        markdown = (
            b"# Article\n\n"
            b"This is a sufficiently substantial Markdown companion body for indexing."
        )
        cases = (
            ("article.md", "application/javascript"),
            ("article.markdown", "application/octet-stream"),
            ("article", "text/markdown"),
        )

        for companion_path, companion_mimetype in cases:
            with self.subTest(companion_path=companion_path, mimetype=companion_mimetype):
                class _MarkdownItem:
                    def __init__(self):
                        self.mimetype = companion_mimetype
                        self.content = markdown

                class _MarkdownEntry:
                    def get_item(self):
                        return _MarkdownItem()

                class _StubEntry:
                    is_redirect = False
                    path = "index/article"
                    title = "Markdown Article"

                    def get_item(self):
                        return _SpaItem(stub)

                class _MarkdownArchive:
                    def get_entry_by_id(self, _index):
                        return _StubEntry()

                    def has_entry_by_path(self, path):
                        return path in ("index.html", companion_path)

                    def get_entry_by_path(self, path):
                        if path == companion_path:
                            return _MarkdownEntry()
                        return _HtmlEntry("index.html", "index.html", SHELL_HTML)

                row = _read_entry(_MarkdownArchive(), 0)
                self.assertIn("sufficiently substantial Markdown companion", row[2])
                self.assertEqual(row[3], _StubEntry.path)

    def test_javascript_shell_pages_are_not_indexed(self):
        self.assertIsNone(read_entry(_ShellArchive(), 0))

    def test_loader_shell_keeps_title_only(self):
        # A PhET-style body of only <script src> references and a <title>: no
        # visible text, no meta-refresh, no recognized shell marker. The shared
        # meta description must be ignored (it would collapse every article onto
        # the shell), so the page is kept as title-only.
        html = (
            b'<html><head><title>Acid-Base Solutions</title>'
            b'<meta name="description" '
            b'content="A shared description given to every simulation page.">'
            b"</head>"
            b'<body style="background-color:black;">'
            b'<script type="text/javascript" src="app.js"></script>'
            b'<script type="text/javascript" src="sim.js"></script>'
            b"</body></html>"
        )

        class _LoaderItem:
            mimetype = "text/html"
            content = html

        class _LoaderEntry:
            is_redirect = False
            title = "Acid-Base Solutions"
            path = "acid-base-solutions_en.html"

            def get_item(self):
                return _LoaderItem()

        class _LoaderArchive:
            def get_entry_by_id(self, _index):
                return _LoaderEntry()

        row = _read_entry(_LoaderArchive(), 0)
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "Acid-Base Solutions")
        self.assertEqual(row[2], "")
        self.assertEqual(row[3], "acid-base-solutions_en.html")

    def test_redirect_chains_store_the_final_target(self):
        class _Redirect:
            def __init__(self, path, title, target=None):
                self.path = path
                self.title = title
                self.is_redirect = target is not None
                self._target = target
                self._index = {"A": 0, "B": 1, "C": 2}[path]

            def get_redirect_entry(self):
                return self._target

        class _Final:
            path = "C"
            title = "Final title"
            is_redirect = False
            _index = 2

        final = _Final()
        second = _Redirect("B", "Intermediate title", final)
        first = _Redirect("A", "Alias title", second)

        class _ChainArchive:
            def get_entry_by_id(self, _index):
                return first

        row = _read_entry(_ChainArchive(), 0)
        self.assertEqual(row[3], "C")
        self.assertEqual(row[4], 2)

    def test_meta_refresh_accepts_absolute_and_query_paths(self):
        stubs = (
            b'<html><head><meta http-equiv="refresh" '
            b'content="0;url=/index.html#/route"></head><body></body></html>',
            b'<html><head><meta http-equiv="refresh" '
            b'content="0;url=../index.html?x=1#/route"></head><body></body></html>',
        )

        class _RefreshArchive:
            def __init__(self, stub):
                self.stub = stub

            def get_entry_by_id(self, _index):
                return _HtmlEntry(
                    "Alias", "content/page_1.html", self.stub
                )

            def has_entry_by_path(self, path):
                return path == "index.html"

            def get_entry_by_path(self, path):
                return _HtmlEntry("index.html", "index.html", SHELL_HTML)

        for stub in stubs:
            with self.subTest(stub=stub):
                row = _read_entry(_RefreshArchive(stub), 0)
                self.assertEqual(row[3], "index.html")

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

    def test_broken_markdown_companion_is_ignored(self):
        class _Item:
            def get_item(self):
                raise RuntimeError("broken companion")

        class _Archive:
            @staticmethod
            def has_entry_by_path(path):
                return path == "page.md"

            @staticmethod
            def get_entry_by_path(path):
                return _Item()

        self.assertIsNone(_markdown_source(_Archive(), "page.md"))


if __name__ == "__main__":
    unittest.main()
