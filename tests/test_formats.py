import tempfile
import unittest
import zipfile
from pathlib import Path

from zimantic.build import build
from zimantic.catalog import parse_file_patterns
from zimantic.content.encoding import decode_text
from zimantic.content.extract import ExtractOptions, extract_document
from zimantic.content.files import FileArchive
from zimantic.content.formats import TEXT_EXTENSIONS, file_extension
from zimantic.content.tools import capabilities
from zimantic.search import Search


def _write_zip(path: Path, members: dict[str, str | bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


class EncodingTests(unittest.TestCase):
    def test_legacy_encodings_are_detected(self):
        cases = [
            ("cp1252", "Café déjà vu"),
            ("cp1251", "Привет мир"),
            ("shift_jis", "日本語のテキスト"),
            ("gb18030", "这是一个中文句子"),
            ("utf-16", "hello world"),
        ]
        for encoding, text in cases:
            with self.subTest(encoding=encoding):
                self.assertEqual(decode_text(text.encode(encoding)), text)

    def test_invalid_utf8_is_not_silently_dropped(self):
        raw = "A café about cycling".encode("cp1252")
        self.assertIn("café", decode_text(raw))


class ProfileTests(unittest.TestCase):
    def test_named_profiles_expand_to_globs(self):
        patterns = parse_file_patterns(["zim", "text", "docs"])
        self.assertIn("*.zim", patterns)
        self.assertIn("*.md", patterns)
        self.assertIn("*.docx", patterns)
        self.assertIn("*.pdf", patterns)
        self.assertIn("*.srt", patterns)

    def test_compound_suffixes_are_preserved(self):
        self.assertEqual(file_extension("backup.tar.gz"), ".tar.gz")
        self.assertEqual(parse_file_patterns("*.tar.gz"), ("*.tar.gz",))

    def test_dotfile_extensions_are_recognized(self):
        self.assertEqual(file_extension(".env"), ".env")
        self.assertIn(".env", TEXT_EXTENSIONS)

    def test_capability_cache_returns_booleans(self):
        result = capabilities()
        self.assertIn("ffmpeg", result)
        self.assertTrue(all(isinstance(value, bool) for value in result.values()))


class OfficeExtractionTests(unittest.TestCase):
    def test_docx_preserves_headings_and_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.docx"
            _write_zip(path, {
                "word/document.xml": (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                    'wordprocessingml/2006/main"><w:body>'
                    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                    "<w:r><w:t>Introduction</w:t></w:r></w:p>"
                    "<w:p><w:r><w:t>A paragraph about bicycles and tires.</w:t>"
                    "</w:r></w:p></w:body></w:document>"
                )
            })
            parsed = FileArchive(path)
            self.assertEqual(parsed.entry_count, 1)
            self.assertEqual(parsed.main_entry.title, "Introduction")
            self.assertIn("bicycles", parsed.main_entry.body)

    def test_xlsx_uses_shared_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.xlsx"
            _write_zip(path, {
                "xl/sharedStrings.xml": (
                    '<sst xmlns="http://schemas.openxmlformats.org/'
                    'spreadsheetml/2006/main"><si><t>Name</t></si>'
                    "<si><t>Alice</t></si></sst>"
                ),
                "xl/worksheets/sheet1.xml": (
                    '<worksheet xmlns="http://schemas.openxmlformats.org/'
                    'spreadsheetml/2006/main"><sheetData><row>'
                    '<c t="s"><v>0</v></c><c t="s"><v>1</v></c>'
                    "</row></sheetData></worksheet>"
                ),
            })
            parsed = FileArchive(path)
            self.assertIn("Name", parsed.main_entry.body)
            self.assertIn("Alice", parsed.main_entry.body)
            self.assertNotIn(" 0 ", parsed.main_entry.body)

    def test_malformed_office_file_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.docx"
            path.write_bytes(b"this is not a zip file")
            self.assertEqual(FileArchive(path).entry_count, 0)

    def test_odp_slides_keep_their_names_without_duplicating_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "talk.odp"
            _write_zip(path, {
                "content.xml": (
                    '<office:document-content xmlns:office="o" '
                    'xmlns:draw="d" xmlns:text="t">'
                    "<office:body><office:presentation>"
                    '<draw:page draw:name="Slide 1"><draw:frame>'
                    "<draw:text-box><text:p>Hello</text:p></draw:text-box>"
                    "</draw:frame></draw:page>"
                    "</office:presentation></office:body>"
                    "</office:document-content>"
                )
            })
            parsed = FileArchive(path)
            self.assertEqual(parsed.main_entry.title, "Slide 1")
            self.assertEqual(parsed.main_entry.body.count("Hello"), 1)
            self.assertEqual(parsed.main_entry.locator, "slide=1")

    def test_xml_containers_do_not_duplicate_child_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feed.rss"
            path.write_text(
                "<rss><channel><item><title>Hello</title>"
                "<description>World</description></item></channel></rss>",
                encoding="utf-8",
            )
            parsed = FileArchive(path)
            self.assertEqual(parsed.main_entry.body.count("Hello"), 1)
            self.assertEqual(parsed.main_entry.body.count("World"), 1)


class MultiEntryBuildTests(unittest.TestCase):
    def test_sections_get_unique_paths_and_previews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zim_dir = root / "zims"
            index_dir = root / "indexes"
            zim_dir.mkdir()
            index_dir.mkdir()
            path = zim_dir / "book.docx"
            _write_zip(path, {
                "word/document.xml": (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                    'wordprocessingml/2006/main"><w:body>'
                    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                    "<w:r><w:t>Bicycles</w:t></w:r></w:p>"
                    "<w:p><w:r><w:t>This chapter is about bicycle tires.</w:t>"
                    "</w:r></w:p>"
                    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                    "<w:r><w:t>Sailboats</w:t></w:r></w:p>"
                    "<w:p><w:r><w:t>This chapter is about sailboat harbors.</w:t>"
                    "</w:r></w:p></w:body></w:document>"
                )
            })
            build(path, index_dir, None, 1, fast=True)
            search = Search(
                {"zim_dir": str(zim_dir), "index_dir": str(index_dir)},
                semantic=False,
            )
            try:
                bicycles = search.search("bicycle", limit=2)[0]
                sailboats = search.search("sailboat", limit=2)[0]
                self.assertEqual(bicycles["url"], "/file/book.docx#section=1")
                self.assertEqual(sailboats["url"], "/file/book.docx#section=2")
                self.assertIn("bicycle", bicycles["lead"])
                self.assertIn("sailboat", sailboats["lead"])
            finally:
                search.close()


class PdfAndLocatorTests(unittest.TestCase):
    def test_pdf_pages_become_separate_entries_with_page_locators(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("pymupdf is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manual.pdf"
            document = pymupdf.open()
            for text in ("First page about sailing.", "Second page about harbors."):
                page = document.new_page()
                page.insert_text((72, 72), text)
            document.save(path)
            document.close()

            parsed = FileArchive(path)
            self.assertEqual(parsed.entry_count, 2)
            self.assertEqual(parsed._get_entry_by_id(1).path, "manual.pdf#page=2")
            self.assertEqual(parsed._get_entry_by_id(1).locator, "page=2")

    def test_file_result_url_keeps_the_locator_fragment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zim_dir = root / "zims"
            index_dir = root / "indexes"
            zim_dir.mkdir()
            index_dir.mkdir()
            path = zim_dir / "notes.txt"
            path.write_text(
                "A sufficiently long note about repairing a bicycle tire. " * 4,
                encoding="utf-8",
            )
            build(path, index_dir, None, 1, fast=True)
            search = Search(
                {"zim_dir": str(zim_dir), "index_dir": str(index_dir)},
                semantic=False,
            )
            try:
                source = search.sources[search.local_names()[0]]
                self.assertEqual(
                    search._source_url(source, "notes.txt#page=3"),
                    "/file/notes.txt#page=3",
                )
            finally:
                search.close()

    def test_file_result_url_keeps_locator_when_index_name_has_a_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zim_dir = root / "zims"
            index_dir = root / "indexes"
            zim_dir.mkdir()
            index_dir.mkdir()
            path = zim_dir / "notes.txt"
            path.write_text(
                "A sufficiently long note about repairing a bicycle tire. " * 4,
                encoding="utf-8",
            )
            # Duplicate-named files get a path-derived suffix; entry paths keep
            # the real filename.
            build(
                path,
                index_dir,
                None,
                1,
                fast=True,
                index_name="notes.txt-abc123",
            )
            search = Search(
                {"zim_dir": str(zim_dir), "index_dir": str(index_dir)},
                semantic=False,
            )
            try:
                source = search.sources[search.local_names()[0]]
                self.assertEqual(source.local_name, "notes.txt-abc123")
                self.assertEqual(
                    search._source_url(source, "notes.txt#page=3"),
                    "/file/notes.txt-abc123#page=3",
                )
            finally:
                search.close()


class ArchiveTests(unittest.TestCase):
    def test_archive_names_are_indexed_without_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.zip"
            _write_zip(path, {
                "readme.txt": "hello",
                "nested/guide.md": "# Guide",
            })
            parsed = FileArchive(path)
            self.assertEqual(parsed.entry_count, 1)
            self.assertIn("readme.txt", parsed.main_entry.body)
            self.assertIn("nested/guide.md", parsed.main_entry.body)

    def test_archive_depth_indexes_members_recursively(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inner = root / "inner.zip"
            _write_zip(inner, {"deep.txt": "Deep text about submarines and sonar."})
            outer = root / "outer.zip"
            _write_zip(outer, {
                "readme.txt": "Root archive text about telescopes and stars.",
                "inner.zip": inner.read_bytes(),
            })
            result = extract_document(
                outer, ExtractOptions(timeout=10, archive_depth=2)
            )
            self.assertIsNotNone(result)
            bodies = " ".join(section.body for section in result.sections)
            self.assertIn("telescopes", bodies)
            self.assertIn("submarines", bodies)


class SubtitleTests(unittest.TestCase):
    def test_sidecar_matching_does_not_use_a_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "lesson.mp4"
            video.write_bytes(b"video")
            (root / "lesson.en.srt").write_text(
                "1\n00:00:01,000 --> 00:00:04,000\nSubtitles about rivers.\n",
                encoding="utf-8",
            )
            (root / "lesson2.srt").write_text(
                "1\n00:00:01,000 --> 00:00:04,000\nWrong video subtitles.\n",
                encoding="utf-8",
            )
            parsed = FileArchive(video)
            self.assertIn("rivers", parsed.main_entry.body)
            self.assertNotIn("Wrong", parsed.main_entry.body)

    def test_binary_subtitle_stream_is_not_decoded_as_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "movie.sub"
            path.write_bytes(b"\x00\x00\x01\xba" + b"\x00binary" * 20)
            parsed = FileArchive(path)
            self.assertEqual(parsed.entry_count, 0)

    def test_ass_dialogue_text_is_parsed_without_timings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "movie.ass"
            path.write_text(
                "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,"
                "{\\i1}A line about lighthouses.{\\i0}\n",
                encoding="utf-8",
            )
            parsed = FileArchive(path)
            self.assertIn("lighthouses", parsed.main_entry.body)
            self.assertNotIn("Dialogue", parsed.main_entry.body)
            self.assertNotIn("0:00:01", parsed.main_entry.body)

    def test_short_fractional_seconds_are_decimal_fractions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ass = Path(tmp) / "movie.ass"
            ass.write_text(
                "[Events]\n"
                "Dialogue: 0,0:00:05.50,0:00:06.00,Default,,0,0,0,,"
                "A line about lighthouses.\n",
                encoding="utf-8",
            )
            self.assertEqual(FileArchive(ass).main_entry.locator, "t=5.500")

            srt = Path(tmp) / "movie.srt"
            srt.write_text(
                "1\n00:00:01,5 --> 00:00:02,000\nA short cue.\n",
                encoding="utf-8",
            )
            self.assertEqual(FileArchive(srt).main_entry.locator, "t=1.500")


class OcrTests(unittest.TestCase):
    def test_scanned_pdf_page_is_ocrd_when_enabled(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("pymupdf is unavailable")
        if not capabilities().get("tesseract"):
            self.skipTest("tesseract is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "page.png"
            document = pymupdf.open()
            page = document.new_page(width=400, height=200)
            page.insert_text((40, 100), "OCR finds the word volcano")
            pixmap = page.get_pixmap(dpi=150)
            pixmap.save(image)
            document.close()

            disabled = extract_document(image, ExtractOptions(timeout=20))
            self.assertEqual(disabled.sections, [])
            enabled = extract_document(
                image,
                ExtractOptions(timeout=20, ocr=True, ocr_languages="eng"),
            )
            self.assertIsNotNone(enabled)
            self.assertIn("volcano", " ".join(s.body for s in enabled.sections).lower())


if __name__ == "__main__":
    unittest.main()
