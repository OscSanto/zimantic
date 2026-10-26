import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from zimantic.build import build
from zimantic.catalog import (
    iter_files,
    matches_file_type,
    parse_file_patterns,
    source_index_name,
)
from zimantic.content.files import FileArchive, is_supported_file
from zimantic.search import Search
from zimantic.settings import load_config
from zimantic.web.app import create_app


class FilePatternTests(unittest.TestCase):
    def test_patterns_are_case_insensitive_and_support_lists_and_strings(self):
        self.assertEqual(parse_file_patterns("*.zim, *.EPUB"), ("*.zim", "*.EPUB"))
        self.assertEqual(
            parse_file_patterns(["*.txt", "*.md,*.srt"]),
            ("*.txt", "*.md", "*.srt"),
        )
        self.assertEqual(parse_file_patterns(None), ("*.zim",))
        self.assertEqual(parse_file_patterns(""), ("*.zim",))

    def test_bare_and_leading_dot_extension_notations_become_globs(self):
        self.assertEqual(parse_file_patterns("zim,epub,pdf"), ("*.zim", "*.epub", "*.pdf"))
        self.assertEqual(parse_file_patterns(".zim,.epub"), ("*.zim", "*.epub"))
        self.assertEqual(
            parse_file_patterns("zim,.epub,*.pdf,book.txt"),
            ("*.zim", "*.epub", "*.pdf", "book.txt"),
        )
        # The same values are equivalent, so the result is deduplicated.
        self.assertEqual(parse_file_patterns("zim,.zim,*.zim"), ("*.zim",))

    def test_direct_matching_accepts_all_extension_notations(self):
        for patterns in (("zim",), (".zim",), ("*.zim",)):
            self.assertTrue(matches_file_type("manual.ZIM", patterns))
        self.assertFalse(matches_file_type("manual.txt", ("zim",)))
        self.assertTrue(matches_file_type("notes.txt", ("txt",)))
        self.assertTrue(matches_file_type("book.epub", ("book.epub",)))

    def test_iter_files_matches_configured_patterns_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nested").mkdir()
            (root / "one.zim").write_bytes(b"z")
            (root / "other").mkdir()
            (root / "nested" / "book.EPUB").write_bytes(b"e")
            (root / "nested" / "notes.txt").write_text("text")

            found = list(iter_files(root, ("zim", "epub")))
            self.assertEqual([path.name for path in found], ["one.zim", "book.EPUB"])

    def test_index_names_keep_the_extension_for_non_zim_files(self):
        self.assertEqual(source_index_name(Path("manual.zim")), "manual")
        self.assertEqual(source_index_name(Path("manual.ZIM")), "manual")
        self.assertEqual(source_index_name(Path("book.epub")), "book.epub")

    def test_file_types_accept_a_comma_separated_config_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('file_types = "zim, epub"\n', encoding="utf-8")
            cfg = load_config(config, warn=lambda _message: None)
            self.assertEqual(cfg["file_types"], ["zim", "epub"])
            self.assertEqual(parse_file_patterns(cfg["file_types"]), ("*.zim", "*.epub"))


class FileReaderTests(unittest.TestCase):
    def test_plain_text_and_markdown_are_indexable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = root / "notes.txt"
            text.write_text("A long enough passage about bicycles and tires. " * 4)
            archive = FileArchive(text)
            self.assertEqual(archive.title, "notes")
            self.assertEqual(archive.entry_count, 1)
            self.assertIn("bicycles", archive.main_entry.body)

            markdown = root / "guide.md"
            markdown.write_text(
                "# Guide\n\nA sufficiently long paragraph about changing a "
                "bicycle tire quickly and safely.\n\n```\ncode ignored\n```\n"
            )
            archive = FileArchive(markdown)
            self.assertIn("bicycle tire", archive.main_entry.body)
            self.assertNotIn("code ignored", archive.main_entry.body)

    def test_video_subtitle_sidecar_supplies_the_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "lesson.mp4"
            video.write_bytes(b"not a real video")
            (root / "lesson.en.srt").write_text(
                "1\n00:00:01,000 --> 00:00:04,000\n"
                "Photosynthesis converts light into chemical energy.\n\n"
                "2\n00:00:05,000 --> 00:00:08,000\n"
                "Plants use chlorophyll to absorb sunlight.\n",
                encoding="utf-8",
            )
            archive = FileArchive(video)
            self.assertEqual(archive.entry_count, 1)
            self.assertIn("Photosynthesis", archive.main_entry.body)
            self.assertNotIn("00:00:01", archive.main_entry.body)

    def test_transport_stream_with_ts_extension_uses_its_subtitles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "lesson.ts"
            video.write_bytes((b"\x47" + b"\x00" * 187) * 4)
            (root / "lesson.en.srt").write_text(
                "1\n00:00:01,000 --> 00:00:04,000\n"
                "A transport stream about airships.\n",
                encoding="utf-8",
            )
            archive = FileArchive(video)
            self.assertEqual(archive.entry_count, 1)
            self.assertIn("airships", archive.main_entry.body)
            self.assertNotIn("\\x00", archive.main_entry.body)

    def test_epub_spine_text_is_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = root / "book.epub"
            with zipfile.ZipFile(book, "w") as archive:
                archive.writestr("mimetype", "application/epub+zip")
                archive.writestr(
                    "META-INF/container.xml",
                    '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/>'
                    "</rootfiles></container>",
                )
                archive.writestr(
                    "OEBPS/content.opf",
                    '<package><metadata><title>The Book</title>'
                    "<creator>A Writer</creator></metadata><manifest>"
                    '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
                    'properties="nav"/>'
                    '<item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
                    '</manifest><spine><itemref idref="c1"/></spine></package>',
                )
                archive.writestr(
                    "OEBPS/nav.xhtml",
                    '<html><body><nav><a href="chapter1.xhtml">Chapter One</a>'
                    "</nav></body></html>",
                )
                archive.writestr(
                    "OEBPS/chapter1.xhtml",
                    "<html><body><p>A sufficiently long chapter about lighthouse "
                    "keepers and their daily routines.</p></body></html>",
                )
            parsed = FileArchive(book)
            self.assertEqual(parsed.title, "The Book")
            self.assertEqual(parsed.entry_count, 1)
            self.assertIn("lighthouse", parsed.main_entry.body)
            self.assertIn("Chapter One", parsed.main_entry.body)

    def test_unsupported_files_have_no_reader(self):
        self.assertFalse(is_supported_file("video.mp4.bin"))
        self.assertTrue(is_supported_file("video.mp4"))
        self.assertTrue(is_supported_file("book.EPUB"))


class FileBuildSearchTests(unittest.TestCase):
    def _build(self, root: Path, name: str, content: str) -> tuple[Path, Path, Path]:
        zim_dir = root / "zims"
        index_dir = root / "indexes"
        zim_dir.mkdir(exist_ok=True)
        index_dir.mkdir(exist_ok=True)
        path = zim_dir / name
        path.write_text(content, encoding="utf-8")
        build(path, index_dir, None, 1, fast=True)
        return zim_dir, index_dir, path

    def test_file_index_stores_body_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, index_dir, path = self._build(
                root,
                "notes.txt",
                "The quick brown fox jumps over the lazy dog. " * 8,
            )
            db = sqlite3.connect(index_dir / "notes.txt.sqlite")
            try:
                meta = dict(db.execute("SELECT key, value FROM meta"))
                self.assertEqual(meta["kind"], "file")
                self.assertEqual(meta["source_path"], str(path.resolve()))
                self.assertEqual(meta["source_title"], "notes")
                row = db.execute(
                    "SELECT title, path, body FROM docs"
                ).fetchone()
                self.assertEqual(row[0], "notes")
                self.assertEqual(row[1], "notes.txt")
                self.assertIn("quick brown fox", row[2])
                self.assertTrue(
                    db.execute(
                        "SELECT 1 FROM docs WHERE docs MATCH 'brown'"
                    ).fetchone()
                )
            finally:
                db.close()

    def test_search_discovers_a_file_source_and_links_to_the_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zim_dir, index_dir, _ = self._build(
                root,
                "notes.txt",
                "A sufficiently long note about repairing a bicycle tire. " * 4,
            )
            search = Search(
                {
                    "zim_dir": str(zim_dir),
                    "index_dir": str(index_dir),
                    "max_preview_chars": 200,
                },
                semantic=False,
            )
            try:
                self.assertEqual(search.local_names(), ["notes.txt"])
                results = search.search("bicycle tire", limit=3)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["title"], "notes")
                self.assertEqual(results[0]["url"], "/file/notes.txt")
                self.assertIn("bicycle", results[0]["lead"])
                self.assertEqual(search.source_file_path("notes.txt"), zim_dir / "notes.txt")
            finally:
                search.close()

    def test_missing_source_is_ignored_at_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zim_dir, index_dir, path = self._build(
                root,
                "notes.txt",
                "A sufficiently long note about repairing a bicycle tire. " * 4,
            )
            path.unlink()
            search = Search(
                {"zim_dir": str(zim_dir), "index_dir": str(index_dir)},
                semantic=False,
            )
            try:
                self.assertEqual(search.local_names(), [])
            finally:
                search.close()

    def test_video_is_searchable_through_its_subtitles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zim_dir = root / "zims"
            index_dir = root / "indexes"
            zim_dir.mkdir()
            index_dir.mkdir()
            video = zim_dir / "lesson.mp4"
            video.write_bytes(b"video bytes")
            (zim_dir / "lesson.en.vtt").write_text(
                "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n"
                "Photosynthesis converts light into chemical energy.\n",
                encoding="utf-8",
            )
            build(video, index_dir, None, 1, fast=True)

            search = Search(
                {"zim_dir": str(zim_dir), "index_dir": str(index_dir)},
                semantic=False,
            )
            try:
                results = search.search("photosynthesis", limit=3)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["url"], "/file/lesson.mp4#t=1.000")
            finally:
                search.close()

    def test_extensionless_text_is_supported_and_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_dir = root / "indexes"
            index_dir.mkdir()
            path = root / "README"
            path.write_text(
                "A plain text note about zeppelins and airships. " * 4,
                encoding="utf-8",
            )
            self.assertTrue(is_supported_file(path))
            build(path, index_dir, None, 1, fast=True)
            db = sqlite3.connect(index_dir / "README.sqlite")
            try:
                body = db.execute("SELECT body FROM docs").fetchone()[0]
                self.assertIn("zeppelins", body)
            finally:
                db.close()

    def test_file_source_exposes_extracted_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zim_dir = root / "zims"
            index_dir = root / "indexes"
            zim_dir.mkdir()
            index_dir.mkdir()
            book = zim_dir / "book.epub"
            with zipfile.ZipFile(book, "w") as archive:
                archive.writestr("mimetype", "application/epub+zip")
                archive.writestr(
                    "META-INF/container.xml",
                    '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/>'
                    "</rootfiles></container>",
                )
                archive.writestr(
                    "OEBPS/content.opf",
                    '<package><metadata><title>The Book</title>'
                    "<creator>A Writer</creator><language>fr</language></metadata>"
                    '<manifest><item id="c1" href="chapter.xhtml" '
                    'media-type="application/xhtml+xml"/></manifest>'
                    "<spine><itemref idref=\"c1\"/></spine></package>",
                )
                archive.writestr(
                    "OEBPS/chapter.xhtml",
                    "<html><body><p>A chapter about airships.</p></body></html>",
                )
            build(book, index_dir, None, 1, fast=True)

            search = Search(
                {"zim_dir": str(zim_dir), "index_dir": str(index_dir)},
                semantic=False,
            )
            try:
                source = next(
                    item for item in search.source_dicts() if item["key"] == "book.epub"
                )
                self.assertEqual(source["title"], "The Book")
                self.assertEqual(source["creator"], "A Writer")
                self.assertEqual(source["language"], "fr")
                library = next(
                    item for item in search.library_dicts()
                    if item["local_name"] == "book.epub"
                )
                self.assertEqual(library["creator"], "A Writer")
                self.assertEqual(library["language"], "fr")
            finally:
                search.close()

    def test_unsupported_explicit_file_is_skipped_without_an_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_dir = root / "indexes"
            index_dir.mkdir()
            path = root / "archive.bin"
            path.write_bytes(b"binary")
            build(path, index_dir, None, 1, fast=True)
            self.assertEqual(list(index_dir.iterdir()), [])

    def test_original_file_route_serves_the_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zim_dir, index_dir, path = self._build(
                root,
                "notes.txt",
                "A sufficiently long note about repairing a bicycle tire. " * 4,
            )
            search = Search(
                {"zim_dir": str(zim_dir), "index_dir": str(index_dir)},
                semantic=False,
            )
            try:
                client = TestClient(create_app(search))
                response = client.get("/file/notes.txt")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, path.read_bytes())
                self.assertTrue(response.headers["content-type"].startswith("text/plain"))
                random = client.get(
                    "/random?zim=notes.txt", follow_redirects=False
                )
                self.assertEqual(random.status_code, 307)
                self.assertEqual(random.headers["location"], "/file/notes.txt")
            finally:
                search.close()


if __name__ == "__main__":
    unittest.main()
