#!/usr/bin/env python3
"""Generate the phase-09 mixed-library release fixture.

The fixture intentionally contains one of every provider:

* one small ZIM archive,
* one Files collection with 1,000 indexable pages plus shared assets,
* one fixture channel imported into a v0.19.5-shaped Kolibri SQLite database,
* a local Kiwix catalog/search HTTP fixture (started separately).

The generator is deterministic.  Pinned inputs and fixture identities are
written to ``INPUTS.json``; the fixed query list is written to ``queries.txt``.
Run it from the repository root:

    python tests/fixtures/mixed_library/generate.py --root /tmp/zimantic-mixed

The benchmark harness (``tests/benchmarks/run_mixed.py``) consumes this tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

PINNED_KOLIBRI = "v0.19.5"
ZIM_NAME = "fixture_en_all_2026-01.zim"
CHANNEL_ID = "fixture-channel"
CHANNEL_NAME = "Fixture Channel"
DEFAULT_FILES = 1_000
DEFAULT_ZIM_ENTRIES = 200
MEDIA_FIXTURES = Path(__file__).resolve().parents[1] / "media"

_TOPICS = ("Mathematics", "Biology", "Geography")
_WORDS = (
    "library article knowledge search index vector lexical body caption "
    "image audio video archive reader source collection document algebra "
    "biology geography climate medicine history mathematics physics"
).split()


def _digest(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _page(title: str, body: str, topic: str) -> str:
    return (
        "<html><head><title>"
        f"{title}</title></head><body><h1>{title}</h1>"
        f"<p>Topic: {topic}. {body}</p>"
        "</body></html>"
    )


def write_files_tree(root: Path, count: int = DEFAULT_FILES) -> None:
    """Write ``count`` indexable HTML pages plus non-indexed shared assets."""
    root.mkdir(parents=True, exist_ok=True)
    _write(root / "shared" / "style.css", "body { color: #123; }\n")
    (root / "shared" / "logo.png").write_bytes(
        b"\x89PNG\r\n\x1a\nfixture-logo"
    )
    per_topic = max(1, (count + len(_TOPICS) - 1) // len(_TOPICS))
    written = 0
    for topic_index, topic in enumerate(_TOPICS):
        slug = topic.casefold()
        directory = root / f"{topic_index:02d}-{slug}"
        for number in range(per_topic):
            if written >= count:
                break
            body = " ".join(
                _WORDS[(number + offset) % len(_WORDS)]
                for offset in range(24)
            )
            _write(
                directory / f"lesson-{number:04d}.html",
                _page(f"{topic} lesson {number:04d}", body, topic),
            )
            written += 1
        _write(
            directory / "index.html",
            _page(f"{topic} home", "Shared collection homepage.", topic),
        )
    # A hidden and an excluded path must never be indexed or served.
    _write(root / ".private" / "secret.html", _page("Secret", "hidden", "none"))
    _write(root / "node_modules" / "pkg" / "index.html", _page("Dep", "x", "none"))


def write_zim(path: Path, count: int = DEFAULT_ZIM_ENTRIES) -> None:
    """Write one deterministic ZIM archive with ``count`` front articles."""
    from libzim.writer import Creator, Hint, Item, StringProvider

    class Page(Item):
        def __init__(self, article_path: str, title: str, html: str) -> None:
            super().__init__()
            self._path = article_path
            self._title = title
            self._html = html.encode("utf-8")

        def get_path(self) -> str:
            return self._path

        def get_title(self) -> str:
            return self._title

        def get_mimetype(self) -> str:
            return "text/html"

        def get_contentprovider(self):
            return StringProvider(self._html)

        def get_hints(self):
            return {Hint.FRONT_ARTICLE: True}

    path.parent.mkdir(parents=True, exist_ok=True)
    creator = Creator(path)
    creator.config_indexing(True, "eng")
    creator.set_mainpath("index.html")
    with creator:
        creator.add_item(
            Page("index.html", "Fixture ZIM", _page("Fixture ZIM", "home", "ZIM"))
        )
        for number in range(count):
            title = f"Fixture article {number:04d}"
            body = " ".join(
                _WORDS[(number + offset) % len(_WORDS)]
                for offset in range(32)
            )
            creator.add_item(
                Page(
                    f"article-{number:04d}.html",
                    title,
                    _page(title, body, "ZIM"),
                )
            )


_KOLIBRI_SCHEMA = """
CREATE TABLE django_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app TEXT NOT NULL,
    name TEXT NOT NULL,
    applied TEXT NOT NULL
);
CREATE TABLE content_language (
    id TEXT PRIMARY KEY,
    lang_code TEXT,
    lang_subcode TEXT,
    lang_name TEXT,
    lang_direction TEXT
);
CREATE TABLE content_channelmetadata (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    tagline TEXT,
    author TEXT,
    version INTEGER DEFAULT 0,
    thumbnail TEXT DEFAULT '',
    last_updated TEXT,
    min_schema_version TEXT DEFAULT '5',
    root_id TEXT,
    published_size INTEGER DEFAULT 0,
    total_resource_count INTEGER DEFAULT 0,
    included_categories TEXT,
    included_grade_levels TEXT,
    "order" INTEGER DEFAULT 0,
    public INTEGER,
    library TEXT,
    partial INTEGER DEFAULT 0
);
CREATE TABLE content_contentnode (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    tree_id INTEGER,
    level INTEGER,
    lft INTEGER,
    rght INTEGER,
    title TEXT,
    coach_content INTEGER DEFAULT 0,
    content_id TEXT,
    channel_id TEXT,
    description TEXT,
    sort_order REAL,
    license_owner TEXT,
    author TEXT,
    kind TEXT,
    available INTEGER DEFAULT 1,
    lang_id TEXT,
    options TEXT,
    grade_levels TEXT,
    resource_types TEXT,
    learning_activities TEXT,
    accessibility_labels TEXT,
    categories TEXT,
    learner_needs TEXT,
    duration INTEGER,
    num_coach_contents INTEGER DEFAULT 0,
    on_device_resources INTEGER DEFAULT 0,
    ancestors TEXT,
    admin_imported INTEGER,
    modality TEXT
);
CREATE TABLE content_file (
    id TEXT PRIMARY KEY,
    local_file_id TEXT NOT NULL,
    contentnode_id TEXT NOT NULL,
    preset TEXT,
    lang_id TEXT,
    supplementary INTEGER DEFAULT 0,
    thumbnail INTEGER DEFAULT 0,
    priority INTEGER
);
CREATE TABLE content_localfile (
    id TEXT PRIMARY KEY,
    extension TEXT,
    available INTEGER DEFAULT 1,
    file_size INTEGER
);
"""


def _storage_path(storage: Path, checksum: str, extension: str) -> Path:
    name = f"{checksum}.{extension.casefold().lstrip('.')}"
    return storage / checksum[0] / checksum[1] / name


def _add_local_file(
    db: sqlite3.Connection,
    storage: Path,
    *,
    checksum_seed: str,
    extension: str,
    content: bytes,
    node_id: str,
    preset: str,
    supplementary: bool = False,
) -> None:
    checksum = _digest(checksum_seed)
    path = _storage_path(storage, checksum, extension)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    db.execute(
        "INSERT INTO content_localfile (id, extension, available, file_size) "
        "VALUES (?, ?, 1, ?)",
        (checksum, extension.casefold().lstrip("."), len(content)),
    )
    db.execute(
        "INSERT INTO content_file "
        "(id, local_file_id, contentnode_id, preset, supplementary, thumbnail, priority) "
        "VALUES (?, ?, ?, ?, ?, 0, 0)",
        (
            _digest(f"file:{checksum_seed}"),
            checksum,
            node_id,
            preset,
            int(supplementary),
        ),
    )


def write_kolibri(home: Path, *, documents_per_topic: int = 5) -> None:
    """Write one v0.19.5-shaped installed Kolibri database and content tree."""
    home.mkdir(parents=True, exist_ok=True)
    storage = home / "content" / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home / "db.sqlite3")
    try:
        db.executescript(_KOLIBRI_SCHEMA)
        db.execute(
            "INSERT INTO django_migrations (app, name, applied) VALUES (?, ?, ?)",
            ("content", "0001_initial", "2020-01-01"),
        )
        db.execute(
            "INSERT INTO content_language "
            "(id, lang_code, lang_name, lang_direction) "
            "VALUES ('lang-en', 'en', 'English', 'ltr')"
        )
        db.execute(
            "INSERT INTO content_channelmetadata "
            "(id, name, description, author, version, root_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                CHANNEL_ID,
                CHANNEL_NAME,
                "Phase-09 mixed-library fixture channel",
                "Zimantic",
                1,
                "topic-root",
            ),
        )
        root_lft = 1
        db.execute(
            "INSERT INTO content_contentnode "
            "(id, parent_id, tree_id, level, lft, rght, title, content_id, "
            "channel_id, description, kind, available, coach_content, lang_id) "
            "VALUES (?, NULL, 1, 0, ?, ?, ?, ?, ?, ?, 'topic', 1, 0, ?)",
            (
                "topic-root",
                root_lft,
                root_lft + len(_TOPICS) * (documents_per_topic * 2 + 2),
                CHANNEL_NAME,
                CHANNEL_ID,
                CHANNEL_ID,
                "Fixture channel root",
                "lang-en",
            ),
        )
        cursor = root_lft + 1
        for topic_index, topic in enumerate(_TOPICS):
            topic_id = _digest(f"topic:{topic}")
            topic_rght = cursor + documents_per_topic * 2 + 1
            db.execute(
                "INSERT INTO content_contentnode "
                "(id, parent_id, tree_id, level, lft, rght, title, content_id, "
                "channel_id, description, kind, available, coach_content, lang_id) "
                "VALUES (?, 'topic-root', 1, 1, ?, ?, ?, ?, ?, ?, 'topic', 1, 0, ?)",
                (
                    topic_id,
                    cursor,
                    topic_rght,
                    topic,
                    topic_id,
                    CHANNEL_ID,
                    f"{topic} topic",
                    "lang-en",
                ),
            )
            cursor += 1
            for number in range(documents_per_topic):
                node_id = _digest(f"doc:{topic}:{number}")
                body = (
                    f"{topic} document {number:02d}. "
                    + " ".join(
                        _WORDS[(number + offset) % len(_WORDS)]
                        for offset in range(28)
                    )
                )
                db.execute(
                    "INSERT INTO content_contentnode "
                    "(id, parent_id, tree_id, level, lft, rght, title, content_id, "
                    "channel_id, description, kind, available, coach_content, lang_id) "
                    "VALUES (?, ?, 1, 2, ?, ?, ?, ?, ?, ?, 'document', 1, 0, ?)",
                    (
                        node_id,
                        topic_id,
                        cursor,
                        cursor + 1,
                        f"{topic} document {number:02d}",
                        node_id,
                        CHANNEL_ID,
                        body,
                        "lang-en",
                    ),
                )
                _add_local_file(
                    db,
                    storage,
                    checksum_seed=f"doc:{topic}:{number}",
                    extension="txt",
                    content=body.encode("utf-8"),
                    node_id=node_id,
                    preset="document",
                )
                cursor += 2
            cursor = topic_rght + 1

        # One HTML5 app, including a shared asset and a caption sidecar track.
        html5_id = _digest("html5:app")
        html5_node_lft = cursor
        db.execute(
            "INSERT INTO content_contentnode "
            "(id, parent_id, tree_id, level, lft, rght, title, content_id, "
            "channel_id, description, kind, available, coach_content, lang_id) "
            "VALUES (?, 'topic-root', 1, 1, ?, ?, ?, ?, ?, ?, 'html5', 1, 0, ?)",
            (
                html5_id,
                html5_node_lft,
                html5_node_lft + 1,
                "HTML5 fixture app",
                html5_id,
                CHANNEL_ID,
                "A bounded HTML5 app used by the serving smoke test.",
                "lang-en",
            ),
        )
        archive = _storage_path(storage, _digest("html5:app"), "zip")
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(
                "index.html",
                _page("HTML5 fixture", "app homepage with offline assets", "HTML5"),
            )
            bundle.writestr("assets/app.js", "console.log('fixture');\n")
            bundle.writestr("assets/style.css", "body { color: #345; }\n")
        db.execute(
            "INSERT INTO content_localfile (id, extension, available, file_size) "
            "VALUES (?, 'zip', 1, ?)",
            (_digest("html5:app"), archive.stat().st_size),
        )
        db.execute(
            "INSERT INTO content_file "
            "(id, local_file_id, contentnode_id, preset, supplementary, thumbnail, priority) "
            "VALUES (?, ?, ?, 'html5_zip', 0, 0, 0)",
            (_digest("file:html5:app"), _digest("html5:app"), html5_id),
        )

        # One video and one caption sidecar; captions stay off unless enabled.
        video_id = _digest("video:fixture")
        video_lft = cursor
        db.execute(
            "INSERT INTO content_contentnode "
            "(id, parent_id, tree_id, level, lft, rght, title, content_id, "
            "channel_id, description, kind, available, coach_content, lang_id) "
            "VALUES (?, 'topic-root', 1, 1, ?, ?, ?, ?, ?, ?, 'video', 1, 0, ?)",
            (
                video_id,
                video_lft,
                video_lft + 1,
                "Fixture video",
                video_id,
                CHANNEL_ID,
                "Video with an optional caption track.",
                "lang-en",
            ),
        )
        _add_local_file(
            db,
            storage,
            checksum_seed="video:fixture",
            extension="mp4",
            content=(MEDIA_FIXTURES / "video" / "lesson.mp4").read_bytes(),
            node_id=video_id,
            preset="high_res_video",
        )
        _add_local_file(
            db,
            storage,
            checksum_seed="caption:fixture",
            extension="vtt",
            content=(MEDIA_FIXTURES / "video" / "lesson.en.vtt").read_bytes(),
            node_id=video_id,
            preset="video_subtitle",
            supplementary=True,
        )
        db.commit()
    finally:
        db.close()


def _queries() -> list[str]:
    words = list(_WORDS)
    queries: list[str] = []
    for index in range(100):
        first = words[index % len(words)]
        second = words[(index * 7 + 3) % len(words)]
        queries.append(f"{first} {second}")
    return queries


def write_config(root: Path, *, kiwix_server: str = "http://127.0.0.1:8085") -> None:
    config = f"""# Generated mixed-library release fixture (phase 09).
port = 8099
page_size = 20
max_results = 100
search_workers = 4
max_concurrent_searches = 2
max_body_text_bytes = 1048576
search_timeout_seconds = 30
index_dir = "./indexes"

[[collections]]
type = "zim"
name = "fixture-zim"
label = "Fixture ZIM"
path = "./zims"

[[collections]]
type = "files"
name = "fixture-files"
label = "Fixture files"
path = "./files"
granularity = "children"
include = ["**/*.html", "**/*.htm", "**/*.txt", "**/*.md", "**/*.pdf"]
exclude = ["**/node_modules/**", "**/.git/**", "**/__pycache__/**"]
max_sources = 64
max_entries = 100000

[[collections]]
type = "kolibri"
name = "fixture-kolibri"
label = "Fixture Kolibri"
path = "./kolibri"
granularity = "children"
max_sources = 64
max_entries = 500000

[[collections]]
type = "kiwix"
name = "fixture-kiwix"
label = "Fixture Kiwix"
server = "{kiwix_server}"
catalog_limit = 50
max_catalog_entries = 50000
max_remote_sources = 20
"""
    _write(root / "config.toml", config)


def write_metadata(root: Path, *, files: int, zim_entries: int) -> None:
    import platform

    payload = {
        "fixture_version": 1,
        "zim": {"name": ZIM_NAME, "front_articles": zim_entries},
        "files": {"indexable_pages": files, "topics": list(_TOPICS)},
        "kolibri": {
            "pinned_release": PINNED_KOLIBRI,
            "channel_id": CHANNEL_ID,
            "channel_name": CHANNEL_NAME,
            "topics": list(_TOPICS),
            "documents_per_topic": 5,
        },
        "kiwix": {"catalog_entries": 3, "fixture": "local HTTP endpoint"},
        "queries": len(_queries()),
        "python": platform.python_version(),
    }
    _write(root / "INPUTS.json", json.dumps(payload, indent=2) + "\n")
    _write(root / "queries.txt", "\n".join(_queries()) + "\n")


def generate(
    root: Path,
    *,
    files: int = DEFAULT_FILES,
    zim_entries: int = DEFAULT_ZIM_ENTRIES,
    kiwix_server: str = "http://127.0.0.1:8085",
    force: bool = False,
) -> Path:
    """Create a complete fixture tree below ``root`` and return it."""
    root = Path(root)
    if root.exists() and force:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    write_zim(root / "zims" / ZIM_NAME, zim_entries)
    write_files_tree(root / "files", files)
    write_kolibri(root / "kolibri")
    write_config(root, kiwix_server=kiwix_server)
    write_metadata(root, files=files, zim_entries=zim_entries)
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("tests/fixtures/mixed_library/generated"),
        help="destination directory (default: tests/fixtures/mixed_library/generated)",
    )
    parser.add_argument("--files", type=int, default=DEFAULT_FILES)
    parser.add_argument("--zim-entries", type=int, default=DEFAULT_ZIM_ENTRIES)
    parser.add_argument(
        "--kiwix-server",
        default="http://127.0.0.1:8085",
        help="local Kiwix fixture endpoint written into config.toml",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing tree")
    args = parser.parse_args(argv)
    if args.files < 1 or args.zim_entries < 1:
        parser.error("--files and --zim-entries must be positive")
    root = generate(
        args.root,
        files=args.files,
        zim_entries=args.zim_entries,
        kiwix_server=args.kiwix_server,
        force=args.force,
    )
    print(root.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
