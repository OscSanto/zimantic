import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from zimantic.build import build
from zimantic.catalog import parse_file_patterns
from zimantic.content.extract import (
    _Deadline,
    _calibre_command,
    _png_for_ocr,
    extract_document,
)
from zimantic.content.files import FileArchive, is_supported_file
from zimantic.content.formats import (
    ARCHIVE_EXTENSIONS,
    EBOOK_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
)
from zimantic.content.sniff import sniff_extension


def _write_zip(path: Path, members: dict[str, str | bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _minimal_epub(path: Path) -> None:
    _write_zip(path, {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": (
            '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/>'
            "</rootfiles></container>"
        ),
        "OEBPS/content.opf": (
            '<package><metadata><title>Hidden Book</title></metadata>'
            '<manifest><item id="c1" href="chapter.xhtml" '
            'media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>'
        ),
        "OEBPS/chapter.xhtml": (
            "<html><body><p>A chapter about airships and zeppelins.</p>"
            "</body></html>"
        ),
    })


def _write_pdf(path: Path) -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "A PDF about airships hidden behind a binary name.")
    document.save(path)
    document.close()


def _bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(
            _bencode(key) + _bencode(item) for key, item in sorted(value.items())
        ) + b"e"
    raise TypeError(type(value))


class SniffingTests(unittest.TestCase):
    def test_any_profile_matches_every_file(self):
        self.assertEqual(parse_file_patterns("any"), ("*",))

    def test_missing_extension_pdf_is_sniffed_and_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manual.bin"
            _write_pdf(path)
            self.assertEqual(sniff_extension(path), ".pdf")
            self.assertTrue(is_supported_file(path))
            parsed = FileArchive(path)
            self.assertEqual(parsed.entry_count, 1)
            self.assertIn("airships", parsed.main_entry.body)
            self.assertNotIn("%PDF-", parsed.main_entry.body)

    def test_missing_extension_epub_is_sniffed_and_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.bin"
            _minimal_epub(path)
            self.assertEqual(sniff_extension(path), ".epub")
            parsed = FileArchive(path)
            self.assertEqual(parsed.title, "Hidden Book")
            self.assertIn("airships", parsed.main_entry.body)

    def test_calibre_ebook_containers_are_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = {
                "book.lit": b"ITOLITLS" + b"\x00" * 64,
                "book.pdb": b"header".ljust(60, b" ") + b"TEXtREAd" + b"\x00" * 32,
                "book.lrf": b"L\x00R\x00F\x00" + b"\x00" * 64,
            }
            for name, content in cases.items():
                with self.subTest(name=name):
                    path = root / name
                    path.write_bytes(content)
                    self.assertEqual(
                        sniff_extension(path), Path(name).suffix
                    )

    def test_calibre_zip_containers_are_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fbz = root / "book.fbz"
            _write_zip(fbz, {"book.fb2": "<FictionBook/>"})
            self.assertEqual(sniff_extension(fbz), ".fbz")

            htmlz = root / "book.htmlz"
            _write_zip(htmlz, {
                "metadata.opf": "<package/>",
                "index.html": "<html><body>Text</body></html>",
            })
            self.assertEqual(sniff_extension(htmlz), ".htmlz")

            txtz = root / "book.txtz"
            _write_zip(txtz, {"book.txt": "Text"})
            self.assertEqual(sniff_extension(txtz), ".txtz")

    def test_comic_metadata_does_not_hide_the_image_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comic.bin"
            _write_zip(path, {
                "ComicInfo.xml": "<ComicInfo/>",
                "pages/001.png": b"\x89PNG\r\n\x1a\n",
                "pages/002.jpg": b"\xff\xd8\xff",
            })
            self.assertEqual(sniff_extension(path), ".cbz")

    def test_unknown_binary_is_not_indexed_as_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "program.bin"
            path.write_bytes(b"\x7fELF" + b"\x00" * 128)
            self.assertEqual(sniff_extension(path), ".bin")
            self.assertIsNone(extract_document(path))

    def test_unknown_utf16_text_is_still_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.bin"
            path.write_bytes("A UTF-16 note about zeppelins.".encode("utf-16"))
            parsed = FileArchive(path)
            self.assertEqual(parsed.entry_count, 1)
            self.assertIn("zeppelins", parsed.main_entry.body)

    def test_build_accepts_explicit_unknown_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_dir = root / "indexes"
            index_dir.mkdir()
            path = root / "manual.bin"
            _write_pdf(path)
            build(path, index_dir, None, 1, fast=True)
            db = sqlite3.connect(index_dir / "manual.bin.sqlite")
            try:
                body = db.execute("SELECT body FROM docs").fetchone()[0]
                self.assertIn("airships", body)
            finally:
                db.close()


class CalibreTests(unittest.TestCase):
    def test_pdf_uses_pdftohtml_engine_on_newer_calibre(self):
        with patch(
            "zimantic.content.extract._calibre_version",
            return_value=(7, 19, 0),
        ):
            command = _calibre_command(Path("manual.pdf"), Path("/tmp/out"))
        self.assertIn("--pdf-engine", command)
        self.assertIn("pdftohtml", command)

    def test_older_calibre_keeps_default_pdf_engine(self):
        with patch(
            "zimantic.content.extract._calibre_version",
            return_value=(7, 18, 0),
        ):
            command = _calibre_command(Path("manual.pdf"), Path("/tmp/out"))
        self.assertNotIn("--pdf-engine", command)

    def test_calibre_ebook_alias_is_supported(self):
        self.assertIn(".cbc", EBOOK_EXTENSIONS)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collection.cbc"
            path.write_bytes(b"not a real cbc")
            self.assertTrue(is_supported_file(path))


class OcrNormalizationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
    def test_webp_is_converted_to_png_before_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "page.webp"
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=white:s=120x60", "-frames:v", "1",
                    "-y", str(source),
                ],
                check=True,
            )
            converted = _png_for_ocr(source.read_bytes(), _Deadline(10))
            self.assertTrue(converted.startswith(b"\x89PNG\r\n\x1a\n"))


class SubtitleTests(unittest.TestCase):
    def test_subtitles_folder_is_searched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "lesson.mp4"
            video.write_bytes(b"not a real video")
            nested = root / "subtitles"
            nested.mkdir()
            (nested / "lesson.srt").write_text(
                "1\n00:00:01,000 --> 00:00:04,000\n"
                "A line about airships and lighthouses.\n",
                encoding="utf-8",
            )
            parsed = FileArchive(video)
            self.assertEqual(parsed.entry_count, 1)
            self.assertIn("airships", parsed.main_entry.body)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
    def test_extra_subtitle_dialects_are_converted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "movie.mpl2"
            path.write_text("[1][2]A line about zeppelins.\n", encoding="utf-8")
            parsed = FileArchive(path)
            self.assertEqual(parsed.entry_count, 1)
            self.assertIn("zeppelins", parsed.main_entry.body)
            self.assertNotIn("[1][2]", parsed.main_entry.body)

    def test_subtitle_extension_families_are_broadened(self):
        for extension in (".mks", ".mpl2", ".pjs", ".stl", ".usf"):
            self.assertIn(extension, SUBTITLE_EXTENSIONS)


class ArchiveTests(unittest.TestCase):
    def test_nested_media_sidecar_is_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.zip"
            _write_zip(path, {
                "lesson.mp4": b"not a real video",
                "lesson.en.srt": (
                    "1\n00:00:01,000 --> 00:00:04,000\n"
                    "Nested subtitle about submarines.\n"
                ),
            })
            result = extract_document(
                path, _options(archive_depth=1)
            )
            self.assertIsNotNone(result)
            body = " ".join(section.body for section in result.sections)
            self.assertIn("submarines", body)

    def test_archive_aliases_are_supported(self):
        for extension in (".zipx", ".zstd", ".br", ".lzh", ".r00", ".001"):
            self.assertIn(extension, ARCHIVE_EXTENSIONS)

    def test_extensionless_text_member_is_indexed_when_depth_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.zip"
            _write_zip(path, {
                "README": "A note about zeppelins and airships.",
            })
            result = extract_document(path, _options(archive_depth=1))
            self.assertIsNotNone(result)
            body = " ".join(section.body for section in result.sections)
            self.assertIn("zeppelins", body)


class SmallFormatTests(unittest.TestCase):
    def test_man_page_troff_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tool.1"
            path.write_text(
                ".TH TOOL 1\n.SH NAME\ntool \\- a small program\n"
                "\\fBtool\\fR reads files and prints information.\n",
                encoding="utf-8",
            )
            parsed = FileArchive(path)
            self.assertIn("small program", parsed.main_entry.body)
            self.assertIn("reads files", parsed.main_entry.body)
            self.assertNotIn(".TH", parsed.main_entry.body)
            self.assertNotIn("\\fB", parsed.main_entry.body)

    def test_svg_text_is_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "diagram.svg"
            path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg">'
                "<title>Diagram</title>"
                "<text>Diagram of a lighthouse</text></svg>",
                encoding="utf-8",
            )
            parsed = FileArchive(path)
            self.assertIn("lighthouse", parsed.main_entry.body)
            self.assertNotIn("<svg", parsed.main_entry.body)

    def test_eml_extracts_headers_and_body(self):
        from email.message import EmailMessage

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "message.eml"
            message = EmailMessage()
            message["Subject"] = "Lighthouse report"
            message["From"] = "keeper@example.com"
            message.set_content("The lamp was cleaned this morning.")
            path.write_bytes(message.as_bytes())
            parsed = FileArchive(path)
            self.assertEqual(parsed.title, "Lighthouse report")
            self.assertIn("cleaned", parsed.main_entry.body)
            self.assertIn("keeper@example.com", parsed.creator)

    def test_epub3_alias_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.epub3"
            _minimal_epub(path)
            parsed = FileArchive(path)
            self.assertEqual(parsed.title, "Hidden Book")

    def test_torrent_metadata_is_searchable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.torrent"
            payload = {
                "announce": "tracker.example",
                "comment": "A collection",
                "info": {
                    "name": "Book Bundle",
                    "files": [
                        {"length": 10, "path": ["docs", "guide.pdf"]},
                        {"length": 20, "path": ["docs", "notes.txt"]},
                    ],
                },
            }
            path.write_bytes(_bencode(payload))
            parsed = FileArchive(path)
            self.assertEqual(parsed.title, "Book Bundle")
            self.assertIn("docs/guide.pdf", parsed.main_entry.body)
            self.assertIn("tracker.example", parsed.main_entry.body)


def _options(**kwargs):
    from zimantic.content.extract import ExtractOptions

    return ExtractOptions(timeout=10, **kwargs)
