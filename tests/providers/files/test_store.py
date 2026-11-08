"""Files content-store serving, confinement, MIME, ranges, and icons."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zimantic.content.store import (
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
)
from zimantic.providers.files import common as files_common
from zimantic.providers.files.discovery import (
    discover_sources,
    files_dir_key,
    files_loose_key,
)
from zimantic.providers.files.store import FilesContentStore

from .helpers import build_tree, files_config


def _store(root: Path, config, key: str) -> FilesContentStore:
    result = discover_sources(config)
    source = next(item for item in result.sources if item.key == key)
    return FilesContentStore(
        source.info,
        root,
        membership_root=source.membership_root,
        loose_only=source.loose_only,
        config=config,
        max_article_bytes=1 << 20,
    )


def test_homepage_directory_assets_and_mime(tmp_path):
    root = tmp_path / "modules"
    build_tree(root)
    config = files_config(root)
    key = files_dir_key("modules", "algebra")
    store = _store(root, config, key)
    try:
        assert store.main_path() == "algebra/index.html"
        entry = store.resolve(None)
        assert entry.path == "algebra/index.html"
        assert entry.mimetype == "text/html"

        directory = store.resolve("algebra")
        assert directory.path == "algebra/index.html"
        assert directory.redirect_path == "algebra"

        for path, mimetype in (
            ("algebra/assets/style.css", "text/css"),
            ("algebra/assets/app.js", "application/javascript"),
            ("algebra/assets/data.json", "application/json"),
            ("algebra/assets/font.woff2", "font/woff2"),
            ("algebra/assets/module.wasm", "application/wasm"),
        ):
            resolved = store.resolve(path)
            assert resolved.mimetype == mimetype

        assert b"color" in store.read(
            "algebra/assets/style.css", head_only=False, max_bytes=1 << 20
        ).content
    finally:
        store.close()


def test_ranges_and_size_limits(tmp_path):
    root = tmp_path / "modules"
    build_tree(root)
    config = files_config(root)
    key = files_dir_key("modules", "biology")
    store = _store(root, config, key)
    try:
        data = (root / "biology" / "video.mp4").read_bytes()
        partial = store.read_range(
            "biology/video.mp4", start=10, end=19, max_bytes=1 << 20
        )
        assert partial.content == data[10:20]
        assert (partial.start, partial.end, partial.total) == (
            10,
            19,
            len(data),
        )
        assert partial.mimetype == "video/mp4"

        bounded = store.read_range(
            "biology/video.mp4", start=0, end=len(data), max_bytes=8
        )
        assert bounded.content == data[:8]
        assert bounded.end == 7

        with pytest.raises(ValueError):
            store.read_range(
                "biology/video.mp4", start=len(data), end=len(data), max_bytes=8
            )
        with pytest.raises(ArticleTooLargeError):
            store.read(
                "biology/video.mp4", head_only=False, max_bytes=8
            )
        head = store.read("biology/video.mp4", head_only=True, max_bytes=8)
        assert head.content == data[:8]
        assert head.truncated is True
        assert head.total == len(data)
    finally:
        store.close()


def test_hidden_excluded_traversal_symlink_and_invalid_paths(tmp_path):
    root = tmp_path / "modules"
    build_tree(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret", encoding="utf-8")
    link = root / "link-to-outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    config = files_config(root)
    key = files_dir_key("modules", "algebra")
    store = _store(root, config, key)
    try:
        for path in (
            ".hidden/secret.html",
            "node_modules/pkg/index.html",
            "link-to-outside/secret.txt",
            "../outside/secret.txt",
            "/etc/passwd",
            "algebra\\index.html",
            "algebra\x00index.html",
            "algebra//index.html",
        ):
            with pytest.raises(ArticleNotFoundError):
                store.resolve(path)

        assert store.resolve("algebra/assets/style.css").mimetype == "text/css"
    finally:
        store.close()


def test_path_based_confinement_fallback_rejects_symlinks(tmp_path, monkeypatch):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "outside.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(files_common, "_no_follow_supported", lambda: False)

    opened = files_common.open_confined(root, "inside.txt")
    try:
        assert opened.handle.read() == b"inside"
    finally:
        opened.handle.close()
    with pytest.raises(files_common.FilesConfinedError):
        files_common.confined_stat(root, "outside.txt")
    with pytest.raises(files_common.FilesConfinedError):
        files_common.open_confined(root, "outside.txt")


def test_cross_source_asset_and_child_prefix(tmp_path):
    root = tmp_path / "modules"
    build_tree(root)
    (root / "shared").mkdir()
    (root / "shared" / "style.css").write_text(
        "body { background: white; }", encoding="utf-8"
    )
    config = files_config(root)
    algebra_key = files_dir_key("modules", "algebra")
    store = _store(root, config, algebra_key)
    try:
        assert store.main_path() == "algebra/index.html"
        cross = store.resolve("shared/style.css")
        assert cross.path == "shared/style.css"
        assert cross.mimetype == "text/css"
    finally:
        store.close()


def test_extensionless_media_uses_magic_for_inline_serving(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "index.html").write_text("<html><body>home</body></html>", encoding="utf-8")
    (root / "scan").write_bytes(b"\x89PNG\r\n\x1a\n" + b"png payload")
    (root / "bundle").write_bytes(b"PK\x03\x04fixture zip bytes")
    config = files_config(root, granularity="collection")
    store = _store(root, config, "files:modules:root")
    try:
        entry = store.resolve("scan")
        assert entry.mimetype == "image/png"
        # Built-in zip magic wins over puremagic's zip-container guesses
        # (which can mislabel a plain archive as an OOXML document).
        bundle = store.resolve("bundle")
        assert bundle.mimetype == "application/zip"
    finally:
        store.close()


def test_unknown_extension_and_svg_mime(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "index.html").write_text("<html><body>home</body></html>", encoding="utf-8")
    (root / "mystery").write_bytes(b"<html><body>looks like html</body></html>")
    (root / "image.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8"
    )
    config = files_config(root, granularity="collection")
    store = _store(root, config, "files:modules:root")
    try:
        assert store.resolve("mystery").mimetype == "application/octet-stream"
        assert store.resolve("image.svg").mimetype == "image/svg+xml"
    finally:
        store.close()


def test_icons_and_random(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "index.html").write_text("<html><body>home</body></html>", encoding="utf-8")
    (root / "favicon.ico").write_bytes(b"icon-bytes")
    (root / "one.txt").write_text("one", encoding="utf-8")
    config = files_config(root, granularity="collection")
    store = _store(root, config, "files:modules:root")
    try:
        icon = store.icon(1 << 20)
        assert icon is not None
        assert icon.content == b"icon-bytes"
        assert icon.mimetype == "image/x-icon"
        for _ in range(10):
            path = store.random_path()
            assert path in {"index.html", "one.txt", "favicon.ico"}
    finally:
        store.close()


def test_directory_without_index_is_not_listed(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    (root / "no-index").mkdir()
    (root / "no-index" / "page.html").write_text("page", encoding="utf-8")
    config = files_config(root, granularity="collection")
    store = _store(root, config, "files:modules:root")
    try:
        with pytest.raises(ArticleNotFoundError):
            store.resolve("no-index")
        assert store.exists("no-index/page.html") is True
    finally:
        store.close()


def test_missing_root_store_raises_unavailable(tmp_path):
    config = files_config(tmp_path / "missing")
    result = discover_sources(config)
    # Discovery disables the collection; constructing a store directly still
    # fails closed rather than opening an arbitrary parent.
    from zimantic.contracts import SourceInfo

    with pytest.raises(ArticleUnavailableError):
        FilesContentStore(
            SourceInfo(
                key="files:missing:root",
                provider="files",
                title="missing",
                collection_key="files:missing",
            ),
            tmp_path / "missing",
            config=config,
            max_article_bytes=1 << 20,
        )
    assert result.sources == ()
