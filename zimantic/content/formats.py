"""File-format families, extension sets and named scan profiles.

The extension sets are deliberately plain data: extractors, folder scans and
the CLI all read the same definitions, so a format cannot be advertised by one
path and silently rejected by another.  Named profiles (``text``, ``ebooks``,
``docs``, ``media`` ...) expand to ordinary glob patterns through
:func:`expand_file_type_token`, which keeps ``--file-types`` usable without
asking people to type twenty extensions.
"""
from __future__ import annotations

from pathlib import Path

TEXT_EXTENSIONS = frozenset({
    ".txt", ".text", ".log", ".rst", ".rest", ".org", ".adoc", ".asciidoc",
    ".tex", ".latex", ".csv", ".tsv", ".tab", ".wsv", ".json", ".jsonl",
    ".ndjson", ".jsonld", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".properties", ".env", ".xml", ".xsl", ".xsd", ".svg", ".opml", ".rss",
    ".atom", ".plist", ".eml", ".torrent",
    # troff man pages: section numbers are the extension.
    ".man", ".1", ".2", ".3", ".4", ".5", ".6", ".7", ".8", ".9",
})
MAN_EXTENSIONS = frozenset({
    ".man", ".1", ".2", ".3", ".4", ".5", ".6", ".7", ".8", ".9",
})
MARKDOWN_EXTENSIONS = frozenset({".md", ".markdown", ".mdown", ".mkd"})
HTML_EXTENSIONS = frozenset({".html", ".htm", ".xhtml", ".mhtml", ".mht"})
CODE_EXTENSIONS = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hxx", ".java", ".kt", ".kts",
    ".scala", ".rb", ".php", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat",
    ".cmd", ".sql", ".graphql", ".gql", ".swift", ".m", ".mm", ".cs", ".lua",
    ".pl", ".pm", ".r", ".jl", ".hs", ".ex", ".exs", ".erl", ".clj", ".lisp",
})
SUBTITLE_EXTENSIONS = frozenset({
    ".srt", ".vtt", ".ass", ".ssa", ".sub", ".lrc", ".smi", ".sami",
    ".mks", ".jss", ".aqt", ".mpl2", ".mpsub", ".pjs", ".rt", ".stl",
    ".psb", ".ssf", ".usf", ".scc",
})
IMAGE_SUBTITLE_CODECS = frozenset({
    "dvbsub", "dvdsub", "pgssub", "xsub", "dvb_subtitle", "dvd_subtitle",
    "hdmv_pgs_subtitle",
})
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".webm", ".mkv", ".avi", ".mov", ".ogv", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".mts", ".3gp", ".3g2", ".flv", ".wmv", ".asf", ".vob",
    ".divx", ".rm", ".rmvb", ".mpe", ".m2v", ".mxf",
})
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".wav", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif", ".ape", ".mka", ".ac3", ".dts", ".caf",
})
IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif",
    ".heic", ".heif", ".jp2", ".jxl", ".ppm", ".pgm", ".pbm", ".tga",
})
PDF_EXTENSIONS = frozenset({".pdf"})
EPUB_EXTENSIONS = frozenset({".epub", ".epub3"})
RTF_EXTENSIONS = frozenset({".rtf"})
PS_EXTENSIONS = frozenset({".ps", ".eps"})
LEGACY_OFFICE_EXTENSIONS = frozenset({".doc", ".xls", ".ppt"})
ZIP_OFFICE_EXTENSIONS = frozenset({
    # Word
    ".docx", ".docm", ".dotx", ".dotm",
    # OpenDocument
    ".odt", ".ods", ".odp", ".odg", ".odf", ".odm", ".ott", ".ots", ".otp",
    # Excel
    ".xlsx", ".xlsm", ".xltx", ".xltm", ".xlsb",
    # PowerPoint
    ".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm",
    # Apple iWork (modern files fall back to calibre)
    ".pages", ".numbers", ".key",
})
EBOOK_EXTENSIONS = frozenset({
    ".mobi", ".azw", ".azw3", ".azw4", ".prc", ".pdb", ".fb2", ".fbz",
    ".lit", ".lrf", ".pml", ".rb", ".snb", ".tcr", ".txtz", ".htmlz",
    ".chm", ".djvu", ".djv", ".cbc",
})
COMIC_EXTENSIONS = frozenset({".cbz", ".cbr", ".cb7", ".cbt", ".cba"})
ARCHIVE_EXTENSIONS = frozenset({
    ".zip", ".zipx", ".jar", ".apk", ".aar", ".ipa",
    ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz",
    ".txz", ".tar.zst", ".tzst", ".tar.lz", ".tar.lzma", ".tar.lz4",
    ".tlz4", ".tar.z", ".taz", ".cbt",
    ".gz", ".bz2", ".xz", ".zst", ".zstd", ".lz", ".lzma", ".lzo", ".lz4",
    ".z", ".br",
    ".7z", ".rar", ".r00", ".r01", ".001", ".cab", ".cpio", ".rpm", ".deb",
    ".arj", ".arc", ".ace", ".alz", ".alzip", ".lha", ".lzh", ".sit",
    ".sitx", ".xar", ".wim", ".iso", ".udf", ".dmg", ".img", ".adf",
    ".pak", ".zoo",
})

# Longest suffixes must be tested first: Path.suffix only returns the last
# component, so a naive lookup would classify ``book.tar.gz`` as ``.gz``.
COMPOUND_SUFFIXES: tuple[str, ...] = tuple(sorted(
    (suffix for suffix in ARCHIVE_EXTENSIONS if suffix.count(".") > 1),
    key=len,
    reverse=True,
))

TEXTUAL_EXTENSIONS = (
    TEXT_EXTENSIONS | MARKDOWN_EXTENSIONS | HTML_EXTENSIONS | CODE_EXTENSIONS
)

SUPPORTED_EXTENSIONS = frozenset(
    TEXTUAL_EXTENSIONS
    | SUBTITLE_EXTENSIONS
    | VIDEO_EXTENSIONS
    | AUDIO_EXTENSIONS
    | IMAGE_EXTENSIONS
    | PDF_EXTENSIONS
    | EPUB_EXTENSIONS
    | RTF_EXTENSIONS
    | PS_EXTENSIONS
    | LEGACY_OFFICE_EXTENSIONS
    | ZIP_OFFICE_EXTENSIONS
    | EBOOK_EXTENSIONS
    | COMIC_EXTENSIONS
    | ARCHIVE_EXTENSIONS
)

# Named profiles are expansions for the folder-scan ``file_types`` setting.
# ``zim`` stays a normal extension so the default and existing configs keep
# working unchanged.
PROFILE_EXTENSIONS: dict[str, frozenset[str]] = {
    "text": TEXTUAL_EXTENSIONS | SUBTITLE_EXTENSIONS,
    "plaintext": TEXT_EXTENSIONS | MARKDOWN_EXTENSIONS,
    "ebook": EPUB_EXTENSIONS | PDF_EXTENSIONS | EBOOK_EXTENSIONS | COMIC_EXTENSIONS,
    "ebooks": EPUB_EXTENSIONS | PDF_EXTENSIONS | EBOOK_EXTENSIONS | COMIC_EXTENSIONS,
    "book": EPUB_EXTENSIONS | PDF_EXTENSIONS | EBOOK_EXTENSIONS | COMIC_EXTENSIONS,
    "books": EPUB_EXTENSIONS | PDF_EXTENSIONS | EBOOK_EXTENSIONS | COMIC_EXTENSIONS,
    "doc": ZIP_OFFICE_EXTENSIONS | LEGACY_OFFICE_EXTENSIONS | RTF_EXTENSIONS | PS_EXTENSIONS | PDF_EXTENSIONS | EPUB_EXTENSIONS,
    "docs": ZIP_OFFICE_EXTENSIONS | LEGACY_OFFICE_EXTENSIONS | RTF_EXTENSIONS | PS_EXTENSIONS | PDF_EXTENSIONS | EPUB_EXTENSIONS,
    "document": ZIP_OFFICE_EXTENSIONS | LEGACY_OFFICE_EXTENSIONS | RTF_EXTENSIONS | PS_EXTENSIONS | PDF_EXTENSIONS | EPUB_EXTENSIONS,
    "documents": ZIP_OFFICE_EXTENSIONS | LEGACY_OFFICE_EXTENSIONS | RTF_EXTENSIONS | PS_EXTENSIONS | PDF_EXTENSIONS | EPUB_EXTENSIONS,
    "office": ZIP_OFFICE_EXTENSIONS | LEGACY_OFFICE_EXTENSIONS | RTF_EXTENSIONS,
    "media": VIDEO_EXTENSIONS | AUDIO_EXTENSIONS,
    "video": VIDEO_EXTENSIONS,
    "videos": VIDEO_EXTENSIONS,
    "audio": AUDIO_EXTENSIONS,
    "image": IMAGE_EXTENSIONS,
    "images": IMAGE_EXTENSIONS,
    "comic": COMIC_EXTENSIONS,
    "comics": COMIC_EXTENSIONS,
    "archive": ARCHIVE_EXTENSIONS,
    "archives": ARCHIVE_EXTENSIONS,
    "all": SUPPORTED_EXTENSIONS | {".zim"},
}


def file_extension(path: Path | str) -> str:
    """Return the longest known lower-case suffix, or the final ``.suffix``.

    ``book.tar.gz`` yields ``.tar.gz``.  Unknown compound names still fall back
    to the ordinary last component, which keeps ``.zim`` and friends unchanged.
    A dotfile such as ``.env`` has no ``Path.suffix`` but is a meaningful text
    extension, so it is returned whole.
    """
    name = Path(path).name.lower()
    for suffix in COMPOUND_SUFFIXES:
        if name.endswith(suffix):
            return suffix
    suffix = Path(name).suffix
    if not suffix and name.startswith(".") and name.count(".") == 1:
        return name
    return suffix


def is_supported_file(path: Path | str) -> bool:
    """True when an extractor family exists for this file type.

    A known extension is authoritative and costs no file read.  For unknown or
    missing extensions (``manual.bin``, an extensionless download) the leading
    bytes are inspected so explicitly passed files still get the right reader.
    A decodable prose sample is accepted as text so ``--file-types
    all+unknown`` can index extensionless notes such as ``README``.
    """
    extension = file_extension(path)
    if extension in SUPPORTED_EXTENSIONS:
        return True
    source = Path(path)
    if not source.is_file():
        return False
    try:
        from .sniff import SAMPLE_BYTES, sniff_extension

        sniffed = sniff_extension(source)
        if sniffed in SUPPORTED_EXTENSIONS:
            return True
        if sniffed is not None:
            # A recognised but unsupported/binary container is authoritative.
            return False
        with source.open("rb") as handle:
            return is_text_sample(handle.read(SAMPLE_BYTES))
    except Exception:  # a detector must never break source discovery
        return False


def is_text_sample(raw: bytes) -> bool:
    """True for a decodable sample that looks like prose, not a bare token.

    Content sniffing can recognise container formats but has no signature for
    plain text.  Requiring a word separator keeps one-word binary payloads from
    being indexed while accepting ordinary notes, man-page-like text and
    extensionless README files.
    """
    from .sniff import looks_like_text

    if not looks_like_text(raw):
        return False
    # A space, tab or line break is present in UTF-16 text as an ASCII byte
    # followed by NUL, so this check also works for BOM-prefixed text.
    return any(marker in raw for marker in (b" ", b"\t", b"\r", b"\n"))


def matches_extension(path: Path | str, extensions: frozenset[str]) -> bool:
    """Case-insensitive extension membership using the longest suffix."""
    return file_extension(path) in extensions


def profile_extensions(name: str) -> frozenset[str] | None:
    """Return the extensions for a named profile, or None when unknown."""
    return PROFILE_EXTENSIONS.get(str(name).strip().casefold())


def expand_file_type_token(token: object) -> list[str]:
    """Expand one ``file_types`` token into extension patterns.

    A profile name expands to ``*.ext`` patterns; anything else is returned
    unchanged so globs, exact filenames and explicit paths keep working.
    ``all+unknown`` matches every file, letting content sniffing decide
    whether a file is readable.
    """
    value = str(token).strip()
    if not value:
        return []
    if value.casefold() == "all+unknown":
        return ["*"]
    extensions = profile_extensions(value)
    if extensions is None:
        return [value]
    return [f"*{extension}" for extension in sorted(extensions)]
