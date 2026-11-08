"""Shared fixtures and helpers for files-provider tests."""

from __future__ import annotations

import logging
from pathlib import Path

from media_helpers import copy_sample

from zimantic.indexing.locator import IndexLocator
from zimantic.indexing.pipeline import build_index
from zimantic.providers.base import ProviderContext
from zimantic.providers.config import FilesConfig, RuntimeConfig
from zimantic.providers.files import FilesProvider

INCLUDE = (
    "**/*.html",
    "**/*.htm",
    "**/*.xhtml",
    "**/*.txt",
    "**/*.md",
    "**/*.pdf",
)
EXCLUDE = ("**/node_modules/**", "**/.git/**", "**/__pycache__/**")


def files_config(root: Path, **overrides) -> FilesConfig:
    values = {
        "name": "modules",
        "path": str(root),
        "label": "Modules",
        "granularity": "children",
        "include": INCLUDE,
        "exclude": EXCLUDE,
    }
    values.update(overrides)
    return FilesConfig(**values)


def runtime_config(
    index_dir: Path, config: FilesConfig, **settings
) -> RuntimeConfig:
    values = {
        "index_dir": str(index_dir),
        "max_entry_bytes": 1 << 20,
        "max_body_text_bytes": 1 << 20,
        "max_preview_chars": 1000,
        "max_article_bytes": 1 << 20,
    }
    values.update(settings)
    return RuntimeConfig(
        settings=values,
        collections=(config,),
        config_dir=index_dir.parent,
    )


def context(index_dir: Path, config: RuntimeConfig) -> ProviderContext:
    return ProviderContext(
        config=config,
        index_dir=index_dir,
        web_base_path="",
        logger=logging.getLogger("zimantic.tests.files"),
        persist_catalog=False,
    )


def build_tree(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "algebra").mkdir()
    (root / "algebra" / "index.html").write_text(
        "<html><head><title>Algebra</title></head><body>"
        "<h1>Algebra</h1><p>"
        + ("algebra lesson about numbers " * 20)
        + "</p></body></html>",
        encoding="utf-8",
    )
    (root / "algebra" / "assets").mkdir()
    (root / "algebra" / "assets" / "style.css").write_text(
        "body { color: #123; }", encoding="utf-8"
    )
    (root / "algebra" / "assets" / "app.js").write_text(
        "console.log('algebra');", encoding="utf-8"
    )
    (root / "algebra" / "assets" / "font.woff2").write_bytes(b"wOF2font")
    (root / "algebra" / "assets" / "module.wasm").write_bytes(b"\x00asm\x01\x00")
    (root / "algebra" / "assets" / "data.json").write_text(
        '{"lesson": 1}', encoding="utf-8"
    )
    (root / "biology").mkdir()
    (root / "biology" / "index.html").write_text(
        "<html><head><title>Biology</title></head><body>"
        "<p>biology lesson about cells</p></body></html>",
        encoding="utf-8",
    )
    copy_sample("video/lesson.mp4", root / "biology" / "video.mp4")
    (root / "loose.txt").write_text(
        "loose notes about zebra and other animals", encoding="utf-8"
    )
    (root / ".hidden").mkdir()
    (root / ".hidden" / "secret.html").write_text(
        "<html><body>hidden secret</body></html>", encoding="utf-8"
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "pkg").mkdir()
    (root / "node_modules" / "pkg" / "index.html").write_text(
        "<html><body>node module</body></html>", encoding="utf-8"
    )
    (root / "broken").mkdir()


def create_pdf(path: Path, text: str = "", *, title: str = "") -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    if text:
        page.insert_text((72, 72), text)
    if title:
        document.set_metadata({"title": title})
    document.save(path)
    document.close()


def open_runtime(index_dir: Path, config: FilesConfig):
    runtime_config_value = runtime_config(index_dir, config)
    provider = FilesProvider()
    batch = provider.discover(
        config, context(index_dir, runtime_config_value)
    )
    collection = batch.collections[0]
    runtime = batch.open(collection)
    return runtime, batch, runtime_config_value


def build_sources(
    runtime,
    index_dir: Path,
    *,
    cfg: dict | None = None,
    fast: bool = False,
) -> IndexLocator:
    locator = IndexLocator(index_dir)
    values = {"batch_size": 1, "max_body_text_bytes": 1 << 20}
    values.update(cfg or {})
    for source in runtime.library_sources():
        producer = runtime.entry_producer(source, fast=fast)
        build_index(producer, locator, None, values, fast=fast)
    runtime.refresh(wait=True)
    return locator
