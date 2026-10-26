"""Best-effort content detection for files with missing or misleading names.

Zimantic's extractor dispatch is extension based.  Most of the time that is
exactly right, but files downloaded from the web or copied off removable media
often have no extension, a generic ``.bin``/``.dat`` name, or the wrong one.
Reading the first few kilobytes is enough to recover the common container
formats, and a little archive introspection distinguishes EPUB, Office and
comic ZIP files from an ordinary ZIP.

The detector is deliberately a *fallback*: callers should trust a known
extension first and only ask this module when the extension is unknown or when
the normal extractor produced nothing.  That keeps the common path free of
extra file reads.  No external command or dependency is required.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .encoding import decode_text

SAMPLE_BYTES = 64 << 10
OLE_SAMPLE_BYTES = 8 << 20

# Internal sentinels: the header is a container whose exact family needs a
# second look.  They are never returned from the public helpers.
_ZIP = ".zip"
_OLE = ".ole"
_BINARY = ".bin"

_ODF_MIMETYPES = {
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.text-template": ".ott",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.spreadsheet-template": ".ots",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/vnd.oasis.opendocument.presentation-template": ".otp",
    "application/vnd.oasis.opendocument.graphics": ".odg",
    "application/vnd.oasis.opendocument.formula": ".odf",
}

_ZIP_IMAGE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".avif", ".heic", ".heif", ".jp2", ".jxl",
})

_EMAIL_HEADER = re.compile(
    rb"(?im)^(?:from|to|cc|bcc|subject|date|mime-version|content-type):"
)


def sniff_extension(path: Path | str) -> str | None:
    """Return a canonical extension inferred from ``path``'s contents.

    Returns ``None`` when the contents are not recognised.  A recognised but
    unsupported type (for example an ELF executable) returns ``.bin`` so
    callers can distinguish "unknown text" from "known binary".
    """
    source = Path(path)
    try:
        with source.open("rb") as handle:
            head = handle.read(SAMPLE_BYTES)
    except OSError:
        return None
    if not head:
        return None

    extension = _sniff_header(head)
    if extension == _ZIP:
        return _refine_zip(source)
    if extension == _OLE:
        return _refine_ole(source)
    if extension is not None:
        return extension
    return _sniff_text_markers(head)


def sniff_bytes(raw: bytes) -> str | None:
    """Return an extension inferred from an in-memory sample.

    This only looks at leading bytes; ZIP/OLE families are reported as
    ``.zip``/``.doc`` because full container introspection needs a real file.
    """
    if not raw:
        return None
    extension = _sniff_header(raw)
    if extension == _OLE:
        return ".doc"
    if extension is not None:
        return extension
    return _sniff_text_markers(raw)


def _sniff_header(raw: bytes) -> str | None:
    """Recognise leading-byte signatures shared by file and memory paths."""
    # Documents and simple containers.
    if raw.startswith(b"%PDF-"):
        return ".pdf"
    if raw.startswith(b"{\\rtf"):
        return ".rtf"
    if raw.startswith(b"%!PS"):
        return ".ps"
    if raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _OLE
    if raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return _ZIP
    if raw.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    if raw.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    if raw.startswith(b"\x1f\x8b"):
        return ".gz"
    if raw.startswith(b"BZh"):
        return ".bz2"
    if raw.startswith(b"\xfd7zXZ\x00"):
        return ".xz"
    if raw.startswith(b"\x28\xb5\x2f\xfd"):
        return ".zst"
    if raw.startswith(b"\x04\x22\x4d\x18"):
        return ".lz4"
    if raw.startswith(b"LZIP"):
        return ".lz"
    if raw.startswith(b"\x1f\x9d"):
        return ".z"
    if len(raw) >= 265 and raw[257:262] == b"ustar":
        return ".tar"
    if raw.startswith((b"070701", b"070702", b"070707")):
        return ".cpio"
    if raw.startswith(b"MSCF"):
        return ".cab"
    if raw.startswith(b"\xed\xab\xee\xdb"):
        return ".rpm"
    if raw.startswith(b"!<arch>\ndebian-binary"):
        return ".deb"
    if len(raw) > 32774 and raw[32769:32774] == b"CD001":
        return ".iso"
    if raw.startswith(b"ITSF"):
        return ".chm"
    if raw.startswith(b"AT&TFORM"):
        return ".djvu"
    if len(raw) >= 68 and raw[60:68] == b"BOOKMOBI":
        return ".mobi"
    if len(raw) >= 68 and raw[60:68] == b"TEXtREAd":
        return ".pdb"
    if raw.startswith(b"ITOLITLS"):
        return ".lit"
    if raw.startswith(b"L\x00R\x00F\x00"):
        return ".lrf"

    # Images.
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if raw.startswith(b"BM"):
        return ".bmp"
    if raw.startswith((b"II*\x00", b"MM\x00*")):
        return ".tif"
    if raw.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n"):
        return ".jp2"
    if raw.startswith((b"\xff\x0a", b"\x00\x00\x00\x0cJXL \r\n\x87\n")):
        return ".jxl"

    # Audio, video and containers with a leading format tag.
    # UTF-16/32 BOMs share the MPEG frame-sync prefix; check them first so a
    # BOM-prefixed text file is not mistaken for an MP3.
    if raw.startswith((
        b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff",
    )):
        return None
    if raw.startswith(b"ID3") or _looks_like_mpeg_audio(raw):
        return ".mp3"
    if raw.startswith(b"fLaC"):
        return ".flac"
    if raw.startswith(b"OggS"):
        return ".ogg"
    if raw.startswith(b"\x1aE\xdf\xa3"):
        return ".mkv"
    if raw.startswith(b"FLV\x01"):
        return ".flv"
    if raw.startswith(b"\x00\x00\x01\xba"):
        return ".mpg"
    if raw.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"):
        return ".wmv"
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        return _ftyp_extension(raw[8:12])
    if raw.startswith(b"RIFF") and len(raw) >= 12:
        kind = raw[8:12]
        if kind == b"WAVE":
            return ".wav"
        if kind == b"AVI ":
            return ".avi"
        if kind == b"WEBP":
            return ".webp"
    if _looks_like_mpeg_ts(raw):
        return ".ts"

    # Executables and other known binaries are not text even when a sample
    # happens to decode cleanly.
    if raw.startswith(b"\x7fELF") or raw.startswith(b"MZ"):
        return _BINARY
    if raw[:4] in {
        b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    }:
        return _BINARY
    return None


def looks_like_text(raw: bytes) -> bool:
    """True when ``raw`` is plausible decodable text rather than a binary.

    The signature check runs first so a well-known container is never
    classified as text merely because its header happens to be printable.
    UTF-16/32 files legitimately contain NUL bytes; the decoder handles those
    through their BOM, so only NUL-bearing samples without a BOM are rejected.
    """
    if not raw:
        return False
    sample = raw[:SAMPLE_BYTES]
    has_unicode_bom = sample.startswith((
        b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff",
    ))
    if b"\x00" in sample and not has_unicode_bom:
        return False
    if _sniff_header(sample) is not None:
        # Any recognised header belongs to a concrete container/binary family.
        # Text markers such as HTML/XML are handled by _sniff_text_markers.
        return False
    try:
        text = decode_text(sample)
    except Exception:  # a decoder failure must not break source discovery
        return False
    if not text.strip():
        return False
    printable = sum(1 for char in text if char.isprintable() or char.isspace())
    return printable / max(1, len(text)) >= 0.9


def _sniff_text_markers(raw: bytes) -> str | None:
    """Recognise text formats that carry a reliable leading marker."""
    stripped = raw.lstrip()
    if not stripped:
        return None
    lower = stripped[:1024].lower()

    if lower.startswith((b"<!doctype html", b"<html")) or b"<html" in lower:
        return ".html"
    if b"<svg" in lower:
        return ".svg"
    if stripped.startswith(b"<?xml"):
        return ".xml"
    if stripped.startswith(b"<"):
        return ".xml"
    if stripped[:1] in {b"{", b"["}:
        return ".json"
    if _EMAIL_HEADER.search(raw[:4096]):
        return ".eml"
    if stripped.startswith(b"d") and (
        b"8:announce" in raw[:4096] or b"4:info" in raw[:4096]
    ):
        return ".torrent"
    return None


def _refine_zip(path: Path) -> str:
    """Distinguish EPUB, OOXML, OpenDocument, iWork and comic ZIP files."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if "mimetype" in names:
                try:
                    info = archive.getinfo("mimetype")
                    mimetype = (
                        archive.read("mimetype")[:256]
                        if info.file_size <= 256
                        else b""
                    ).decode("ascii", "ignore").strip().lower()
                except (KeyError, OSError, RuntimeError):
                    mimetype = ""
                if mimetype == "application/epub+zip":
                    return ".epub"
                if mimetype in _ODF_MIMETYPES:
                    return _ODF_MIMETYPES[mimetype]
                if "comicbook" in mimetype:
                    return ".cbz"
            if "META-INF/container.xml" in names:
                return ".epub"

            word = any(name.startswith("word/") for name in names)
            excel = any(name.startswith("xl/") for name in names)
            powerpoint = any(name.startswith("ppt/") for name in names)
            if word:
                return ".docx"
            if excel:
                return ".xlsx"
            if powerpoint:
                return ".pptx"

            if "content.xml" in names and "META-INF/manifest.xml" in names:
                return ".odt"
            if "index.xml" in names and (
                any(name.startswith("Index/") for name in names)
                or any(name.startswith("Metadata/") for name in names)
            ):
                return ".pages"

            files = [
                name for name in names
                if not name.endswith("/")
                and not name.startswith("__MACOSX/")
                and not name.rsplit("/", 1)[-1].startswith("._")
                and name.rsplit("/", 1)[-1].casefold()
                not in {"comicinfo.xml", "thumbs.db", ".ds_store"}
            ]
            folded = [name.casefold() for name in files]
            if any(name.endswith(".fb2") for name in folded):
                return ".fbz"
            if (
                "metadata.opf" in folded
                and any(name.endswith((".html", ".htm", ".xhtml")) for name in folded)
            ):
                return ".htmlz"
            if folded and all(name.endswith(".txt") for name in folded):
                return ".txtz"

            images = [
                name for name in files
                if Path(name).suffix.lower() in _ZIP_IMAGE_EXTENSIONS
            ]
            if images and len(images) == len(files):
                return ".cbz"
    except (OSError, zipfile.BadZipFile, RuntimeError):
        pass
    return ".zip"


def _refine_ole(path: Path) -> str:
    """Tell old Office documents apart by scanning OLE directory names."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(OLE_SAMPLE_BYTES)
    except OSError:
        return ".doc"
    markers = (
        (b"WordDocument", ".doc", True),
        (b"Workbook", ".xls", True),
        # "Book" is short enough to appear in ordinary document text; the OLE
        # directory entry that identifies BIFF5 is UTF-16, so require that form.
        (b"Book", ".xls", False),
        (b"PowerPoint Document", ".ppt", True),
    )
    for marker, extension, allow_ascii in markers:
        utf16 = marker.decode("ascii").encode("utf-16-le")
        if (allow_ascii and marker in raw) or utf16 in raw:
            return extension
    return ".doc"


def _ftyp_extension(brand: bytes) -> str:
    if brand in {b"avif", b"avis"}:
        return ".avif"
    if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
        return ".heic"
    if brand in {b"M4A ", b"M4B ", b"M4P "}:
        return ".m4a"
    if brand == b"qt  ":
        return ".mov"
    return ".mp4"


def _looks_like_mpeg_audio(raw: bytes) -> bool:
    # MPEG audio frame sync: 11 set bits, a valid version/layer, and a real
    # bitrate/sample-rate index.  The extra checks keep a UTF-16 BOM
    # (``ff fe``) from being mistaken for an MP3 frame.
    if len(raw) < 3 or raw[0] != 0xFF or (raw[1] & 0xE0) != 0xE0:
        return False
    version = (raw[1] >> 3) & 0x03
    layer = (raw[1] >> 1) & 0x03
    if version == 0x01 or layer == 0x00:
        return False
    bitrate = (raw[2] >> 4) & 0x0F
    sample_rate = (raw[2] >> 2) & 0x03
    return bitrate not in {0x00, 0x0F} and sample_rate != 0x03


def _looks_like_mpeg_ts(raw: bytes) -> bool:
    if not raw or raw[0] != 0x47:
        return False
    return any(
        len(raw) > offset and raw[offset] == 0x47
        for offset in (188, 376, 564)
    )
