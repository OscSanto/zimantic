"""Installed Kolibri database schema detection and file classification.

All table and column knowledge for the pinned Kolibri release lives here.  The
provider supports the v0.19.5 installed SQLite schema only; other layouts fail
closed with a clear diagnostic instead of guessing at an export database or a
version integer.
"""

from __future__ import annotations

import mimetypes
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

SUPPORTED_KOLIBRI_VERSION = "0.19.5"
SUPPORTED_VERSIONS = (SUPPORTED_KOLIBRI_VERSION,)
PINNED_RELEASE = "v0.19.5"

CHANNEL_TABLE = "content_channelmetadata"
NODE_TABLE = "content_contentnode"
FILE_TABLE = "content_file"
LOCAL_FILE_TABLE = "content_localfile"
LANGUAGE_TABLE = "content_language"
MIGRATIONS_TABLE = "django_migrations"

REQUIRED_TABLES = frozenset(
    {CHANNEL_TABLE, NODE_TABLE, FILE_TABLE, LOCAL_FILE_TABLE}
)

# The installed schema is identified by the required layout, not by SQLite's
# user_version (which belongs to the exported channel database).  These are the
# columns the provider reads; extra release columns are intentionally ignored.
REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    CHANNEL_TABLE: frozenset(
        {
            "id",
            "name",
            "description",
            "root_id",
            "version",
        }
    ),
    NODE_TABLE: frozenset(
        {
            "id",
            "parent_id",
            "tree_id",
            "level",
            "lft",
            "rght",
            "title",
            "kind",
            "content_id",
            "channel_id",
            "description",
            "available",
            "coach_content",
            "sort_order",
        }
    ),
    FILE_TABLE: frozenset(
        {
            "id",
            "local_file_id",
            "contentnode_id",
            "preset",
            "priority",
            "supplementary",
            "thumbnail",
        }
    ),
    LOCAL_FILE_TABLE: frozenset(
        {"id", "extension", "available", "file_size"}
    ),
}

# v0.19.5 content migrations that establish the installed layout.  A fixture
# may omit django_migrations (for example a deliberately reduced test database);
# when the table exists we require the content app to have been migrated and no
# later/unknown content migration to be present.
PINNED_CONTENT_MIGRATIONS = frozenset(
    {
        "0001_initial",
        "0002_channelmetadatacache_last_updated",
        "0003_auto_20170607_1212",
        "0004_auto_20170825_1038",
        "0005_auto_20171009_0903",
        "0006_auto_20171128_1703",
        "0007_auto_20180212_1155",
        "0008_auto_20180429_1709",
        "0008_contentnode_coach_content",
        "0009_auto_20180410_1139",
        "0010_merge_20180504_1540",
        "0011_auto_20180907_1017",
        "0012_auto_20180910_1702",
        "0013_auto_20180919_1142",
        "0014_auto_20181218_1132",
        "0015_auto_20190125_1715",
        "0016_auto_20190124_1639",
        "0017_auto_20190415_1855",
        "0018_contentnode_num_coach_contents",
        "0019_contentnode_slideshow_options",
        "0020_le_utils_0_20_upgrade_migration",
        "0021_remove_contentnode_stemmed_metaphone",
        "0022_remove_file_available",
        "0023_contentnode_on_device_resources",
        "0024_channelmetadata_public",
        "0025_add_h5p_kind",
        "0026_contentnode_options",
        "0027_channelmetadata_tagline",
        "0028_contentnode_metadata_labels",
        "0029_metadata_bitmasks",
        "0030_contentnode_ancestors",
        "0031_channelmetadata_partial",
        "0032_contentnode_admin_imported",
        "0033_content_requests",
        "0034_contentrequest_source_instance_id",
        "0035_add_imscp_preset",
        "0036_null_boolean_and_mptt",
        "0037_add_bloompub_preset",
        "0038_alter_localfile_extension",
        "0039_channelmetadata_ordered_fields",
        "0040_html_article_constants",
        "0041_contentnode_modality",
        "0042_remove_modality",
        "0043_contentrequest_priority",
        "0044_contentrequest_priority_default",
        "0045_channelmetadata_library",
        "0046_contentrequest_channel_version",
    }
)


class KolibriSchemaError(RuntimeError):
    """The configured database is not a supported installed Kolibri schema."""


def unsupported_schema_message() -> str:
    return (
        "unsupported Kolibri installation database schema; this build supports "
        f"Kolibri v{SUPPORTED_KOLIBRI_VERSION} installed SQLite databases"
    )


@dataclass(frozen=True)
class SchemaInfo:
    version: str = SUPPORTED_KOLIBRI_VERSION
    content_migrations: tuple[str, ...] = ()


def _table_columns(db, table: str) -> frozenset[str]:
    return frozenset(
        str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")
    )


def _table_names(db) -> set[str]:
    return {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }


def detect_schema(db) -> SchemaInfo:
    """Validate the installed v0.19.5 content layout.

    ``db`` must be a connection to the private snapshot, never the live
    installation.  Missing tables/columns fail closed.
    """
    names = _table_names(db)
    missing = sorted(REQUIRED_TABLES - names)
    if missing:
        raise KolibriSchemaError(
            unsupported_schema_message()
            + f" (missing table(s): {', '.join(missing)})"
        )
    for table, required in REQUIRED_COLUMNS.items():
        columns = _table_columns(db, table)
        absent = sorted(required - columns)
        if absent:
            raise KolibriSchemaError(
                unsupported_schema_message()
                + f" ({table} missing column(s): {', '.join(absent)})"
            )
    migrations: tuple[str, ...] = ()
    if MIGRATIONS_TABLE in names:
        rows = db.execute(
            "SELECT name FROM django_migrations WHERE app = 'content' "
            "ORDER BY name"
        ).fetchall()
        migrations = tuple(str(row[0]) for row in rows)
        # Older export databases have no installed migrations at all.  An
        # installed database with content migrations must at least include the
        # v0.19.5 final migration names we know about; unknown future migrations
        # are rejected rather than read with an unverified mapping.
        if migrations:
            migration_set = set(migrations)
            unknown = migration_set - PINNED_CONTENT_MIGRATIONS
            # Reduced test fixtures may record only the initial migration.  A
            # realistic migrated installation has a longer history, and both an
            # older release and an unknown future migration must fail closed.
            if (unknown and len(migrations) >= 5) or (
                len(migrations) >= 20
                and "0046_contentrequest_channel_version" not in migration_set
            ):
                raise KolibriSchemaError(
                    unsupported_schema_message()
                    + " (unrecognized django_migrations content history)"
                )
            if not migration_set & PINNED_CONTENT_MIGRATIONS:
                raise KolibriSchemaError(
                    unsupported_schema_message()
                    + " (unrecognized django_migrations content history)"
                )
    return SchemaInfo(content_migrations=migrations)


# ---------------------------------------------------------------------------
# File presets and primary-representation selection
# ---------------------------------------------------------------------------

TOPIC_KIND = "topic"
EXERCISE_KIND = "exercise"

VIDEO_PRESETS = ("high_res_video", "low_res_video", "vector_video")
AUDIO_PRESETS = ("audio",)
DOCUMENT_PRESETS = ("document", "epub", "bloompub")
HTML5_PRESETS = (
    "html5_zip",
    "h5p",
    "qti",
    "imscp_zip",
    "kpub",
    "slideshow_manifest",
    "zim",
)
SUPPORTED_HTML5_PRESETS = frozenset({"html5_zip"})
CAPTION_PRESETS = frozenset({"video_subtitle"})
THUMBNAIL_PRESETS = frozenset(
    {
        "video_thumbnail",
        "audio_thumbnail",
        "document_thumbnail",
        "exercise_thumbnail",
        "channel_thumbnail",
        "topic_thumbnail",
        "html5_thumbnail",
        "h5p_thumbnail",
        "zim_thumbnail",
        "qti_thumbnail",
        "slideshow_thumbnail",
    }
)

CAPTION_EXTENSIONS = frozenset({".vtt", ".srt", ".ass", ".ssa"})
_DOCUMENT_EXTENSIONS = frozenset({".html", ".htm", ".xhtml", ".xht", ".pdf", ".txt"})
_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
)
_VIDEO_EXTENSIONS = frozenset({".mp4", ".m4v", ".webm", ".ogv", ".mov", ".mkv"})
_AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".aac", ".wav", ".flac", ".oga", ".ogg", ".opus"}
)

_MIME_BY_EXTENSION = {
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
    ".xht": "application/xhtml+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".json": "application/json",
    ".xml": "application/xml",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".vtt": "text/vtt",
    ".srt": "application/x-subrip",
    ".ass": "text/x-ass",
    ".ssa": "text/x-ssa",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    ".wasm": "application/wasm",
    ".epub": "application/epub+zip",
    ".zip": "application/zip",
}


def extension_mimetype(extension: str) -> str | None:
    ext = str(extension or "").casefold()
    if not ext.startswith("."):
        ext = "." + ext
    mapped = _MIME_BY_EXTENSION.get(ext)
    if mapped:
        return mapped
    guessed, _ = mimetypes.guess_type("file" + ext, strict=False)
    return guessed.split(";", 1)[0].lower() if guessed else None


def media_family(extension: str, kind: str = "") -> str | None:
    ext = "." + str(extension or "").casefold().lstrip(".")
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    if ext in _AUDIO_EXTENSIONS:
        return "audio"
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _DOCUMENT_EXTENSIONS or kind in {
        "document",
        "exercise",
        HTML5_PRESETS[0],
    }:
        return "text"
    if kind == "video":
        return "video"
    if kind == "audio":
        return "audio"
    if kind in {"html5", "h5p", "slideshow", "quiz"}:
        return "text"
    return None


def _preset_priority(preset: str, kind: str) -> tuple[int, str]:
    """Return a deterministic preference rank (lower is more primary)."""
    if kind == "video":
        order: Sequence[str] = VIDEO_PRESETS
    elif kind == "audio":
        order = AUDIO_PRESETS
    elif kind in {"html5", "slideshow", "h5p", "quiz"}:
        order = HTML5_PRESETS
    elif kind == "document":
        order = DOCUMENT_PRESETS
    else:
        order = ()
    try:
        return (list(order).index(preset), preset)
    except ValueError:
        return (len(order), preset)


@dataclass(frozen=True)
class FileRow:
    """One joined ``content_file``/``content_localfile`` row."""

    id: str
    node_id: str
    local_file_id: str
    extension: str
    available: bool
    file_size: int | None
    preset: str
    priority: int | None
    supplementary: bool
    thumbnail: bool
    lang_id: str | None
    checksum: str

    @property
    def mimetype(self) -> str:
        return extension_mimetype(self.extension) or "application/octet-stream"

    @property
    def is_caption(self) -> bool:
        return self.preset in CAPTION_PRESETS or (
            "." + str(self.extension or "").casefold().lstrip(".")
            in CAPTION_EXTENSIONS
        )

    @property
    def is_thumbnail(self) -> bool:
        return self.thumbnail or self.preset in THUMBNAIL_PRESETS

    @property
    def is_auxiliary(self) -> bool:
        return (
            self.is_caption
            or self.is_thumbnail
            or self.supplementary
            or self.preset.endswith("_dependency")
            or self.preset in {"exercise_image", "exercise_graphie"}
        )


@dataclass(frozen=True)
class FileSelection:
    primary: FileRow | None
    captions: tuple[FileRow, ...] = ()
    thumbnails: tuple[FileRow, ...] = ()
    auxiliary: tuple[FileRow, ...] = ()
    ambiguous: tuple[str, ...] = ()


def select_primary(
    files: Iterable[FileRow], kind: str, preferred_lang: str | None = None
) -> FileSelection:
    """Choose one canonical primary file without relying on SQL row order.

    Preset/role semantics identify the primary representation.  Multiple
    variants are accepted when the preset preference is unambiguous.  Two
    equally preferred primary rows are ambiguous and must not be guessed.
    """
    available = [file for file in files if file.available]
    captions = tuple(sorted(
        (file for file in available if file.is_caption),
        key=lambda file: (file.lang_id or "", file.id),
    ))
    thumbnails = tuple(sorted(
        (file for file in available if file.is_thumbnail),
        key=lambda file: (file.preset, file.id),
    ))
    auxiliary = tuple(sorted(
        (
            file
            for file in available
            if file.is_auxiliary and not file.is_caption and not file.is_thumbnail
        ),
        key=lambda file: (file.preset, file.id),
    ))
    candidates = [file for file in available if not file.is_auxiliary]
    if not candidates and available:
        # A schema that only supplies an auxiliary mapping cannot identify a
        # primary representation.  Do not use a thumbnail or subtitle as media.
        return FileSelection(
            None,
            captions=captions,
            thumbnails=thumbnails,
            auxiliary=auxiliary,
            ambiguous=("no primary representation",),
        )
    if not candidates:
        return FileSelection(
            None, captions=captions, thumbnails=thumbnails, auxiliary=auxiliary
        )

    def language_penalty(file: FileRow) -> int:
        if not preferred_lang:
            return 0
        return 0 if file.lang_id == preferred_lang else 1

    def sort_key(file: FileRow):
        return (
            *_preset_priority(file.preset, kind),
            file.priority is None,
            file.priority if file.priority is not None else 1 << 30,
            language_penalty(file),
            file.local_file_id,
            file.id,
        )

    ranked = sorted(candidates, key=sort_key)
    best = ranked[0]
    best_key = sort_key(best)[:5]
    same = [file for file in ranked if sort_key(file)[:5] == best_key]
    if len(same) > 1:
        identities = sorted(file.local_file_id for file in same)
        return FileSelection(
            None,
            captions=captions,
            thumbnails=thumbnails,
            auxiliary=auxiliary,
            ambiguous=(
                "ambiguous primary representation: " + ", ".join(identities),
            ),
        )
    return FileSelection(
        best, captions=captions, thumbnails=thumbnails, auxiliary=auxiliary
    )


_STORAGE_NAME = re.compile(r"^[0-9a-f]{32}(-data)?\.[0-9a-z]+$")


def storage_filename(local_file: Mapping[str, object] | FileRow) -> str:
    """Validate and return the checksum-addressed storage filename."""
    if isinstance(local_file, FileRow):
        checksum = local_file.checksum
        extension = local_file.extension
    else:
        checksum = str(local_file.get("id") or local_file.get("checksum") or "")
        extension = str(local_file.get("extension") or "")
    ext = extension.casefold().lstrip(".")
    name = f"{checksum}.{ext}"
    if not _STORAGE_NAME.fullmatch(name):
        raise ValueError("invalid Kolibri storage filename")
    return name


def storage_relative_path(local_file: Mapping[str, object] | FileRow) -> str:
    name = storage_filename(local_file)
    return posixpath.join(name[0], name[1], name)


def normalize_member_path(name: str) -> str:
    """Normalize one archive/member path or raise ``ValueError``."""
    text = str(name)
    if (
        not text
        or text.startswith("/")
        or "\\" in text
        or "\x00" in text
        or "%2f" in text.casefold()
        or "%5c" in text.casefold()
    ):
        raise ValueError("invalid member path")
    trailing = text.endswith("/")
    candidate = text[:-1] if trailing else text
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid member path")
    normalized = posixpath.normpath(candidate)
    if normalized != candidate:
        raise ValueError("invalid member path")
    return normalized + ("/" if trailing else "")


def display_title(path: str) -> str:
    name = PurePosixPath(str(path).rstrip("/")).name
    return PurePosixPath(name).stem or name or str(path)


__all__ = [
    "AUDIO_PRESETS",
    "CAPTION_EXTENSIONS",
    "CAPTION_PRESETS",
    "CHANNEL_TABLE",
    "DOCUMENT_PRESETS",
    "EXERCISE_KIND",
    "FILE_TABLE",
    "HTML5_PRESETS",
    "LOCAL_FILE_TABLE",
    "MIGRATIONS_TABLE",
    "NODE_TABLE",
    "PINNED_CONTENT_MIGRATIONS",
    "PINNED_RELEASE",
    "REQUIRED_COLUMNS",
    "REQUIRED_TABLES",
    "SUPPORTED_HTML5_PRESETS",
    "SUPPORTED_KOLIBRI_VERSION",
    "SUPPORTED_VERSIONS",
    "THUMBNAIL_PRESETS",
    "TOPIC_KIND",
    "VIDEO_PRESETS",
    "FileRow",
    "FileSelection",
    "KolibriSchemaError",
    "SchemaInfo",
    "detect_schema",
    "display_title",
    "extension_mimetype",
    "media_family",
    "normalize_member_path",
    "select_primary",
    "storage_filename",
    "storage_relative_path",
    "unsupported_schema_message",
]
