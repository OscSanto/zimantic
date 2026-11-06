"""Contract tests every local ContentStore implementation must pass.

The suite is deliberately provider-agnostic: a provider supplies one open
store; the assertions cover identity, redirects, bounded reads, ranges, icons,
random selection, and lifecycle.  Kiwix has no local store and must return
``None`` instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from libzim.reader import Archive
from libzim.writer import Creator, Hint, Item, StringProvider

from zimantic.content.store import (
    ArticleNotFoundError,
    ArticleTooLargeError,
    ContentStore,
)
from zimantic.contracts import SourceInfo
from zimantic.providers.base import CollectionInfo
from zimantic.providers.config import KiwixConfig
from zimantic.providers.kiwix.provider import KiwixRuntime
from zimantic.providers.zim.store import ArchiveContentStore

HOME_HTML = b"<html><body><p>Home page with enough prose for a preview.</p></body></html>"
DATA = bytes(range(200))
LARGE_HTML = (
    b"<html><body><p>"
    + b"large article body " * 20_000
    + b"</p></body></html>"
)


class _Page(Item):
    def __init__(self, path: str, title: str, content: bytes, mimetype: str):
        super().__init__()
        self._path = path
        self._title = title
        self._content = content
        self._mimetype = mimetype

    def get_path(self):
        return self._path

    def get_title(self):
        return self._title

    def get_mimetype(self):
        return self._mimetype

    def get_contentprovider(self):
        return StringProvider(self._content)

    def get_hints(self):
        return {Hint.FRONT_ARTICLE: True}


def _create_archive(path: Path) -> None:
    creator = Creator(path)
    creator.config_indexing(True, "eng")
    creator.set_mainpath("Home")
    with creator:
        creator.add_item(_Page("Home", "Home", HOME_HTML, "text/html"))
        creator.add_item(_Page("Large", "Large", LARGE_HTML, "text/html"))
        creator.add_item(
            _Page("data.bin", "Data", DATA, "application/octet-stream")
        )
        creator.add_redirection("Alias", "Alias", "Home", {})


def _store(tmp_path: Path, *, max_article_bytes: int = 1 << 20):
    archive_path = tmp_path / "manual.zim"
    _create_archive(archive_path)
    source = SourceInfo(
        key="zim:library:manual-0123456789abcdef",
        provider="zim",
        title="Manual",
        collection_key="zim:library",
        collection_label="Library",
        homepage="Home",
        searchable=True,
    )
    return ArchiveContentStore(
        source,
        Archive(str(archive_path)),
        max_article_bytes=max_article_bytes,
    )


def assert_content_store_contract(store: ContentStore) -> None:
    assert store.main_path() == "Home"
    assert store.exists("data.bin")
    assert not store.exists("missing")

    entry = store.resolve("Alias")
    assert entry.path == "Home"
    assert entry.redirect_path == "Alias"

    full = store.read("data.bin", head_only=False, max_bytes=1 << 20)
    assert full.path == "data.bin"
    assert full.mimetype == "application/octet-stream"
    assert full.content == DATA
    assert full.total == len(DATA)
    assert full.truncated is False

    head = store.read("Large", head_only=True, max_bytes=1024)
    assert head.truncated is True
    assert head.total == len(LARGE_HTML)
    assert len(head.content) <= 1024

    with pytest.raises(ArticleTooLargeError):
        store.read("Large", head_only=False, max_bytes=32)

    with pytest.raises(ArticleNotFoundError):
        store.resolve("missing")

    partial = store.read_range(
        "data.bin", start=2, end=5, max_bytes=1 << 20
    )
    assert partial.content == DATA[2:6]
    assert (partial.start, partial.end, partial.total) == (2, 5, len(DATA))
    assert partial.mimetype == "application/octet-stream"

    with pytest.raises(ArticleTooLargeError):
        store.read_range("Large", start=0, end=1, max_bytes=32)

    icon = store.icon(1 << 20)
    if icon is not None:
        assert icon.total == len(icon.content)
        assert icon.truncated is False

    assert store.random_path() in {"Home", "Large", "Alias"}
    store.close()
    store.close()


def test_zim_archive_store_satisfies_the_contract(tmp_path):
    store = _store(tmp_path)
    try:
        assert_content_store_contract(store)
    finally:
        store.close()


def test_kiwix_has_no_local_content_store():
    collection = CollectionInfo(
        key="kiwix:remote", provider="kiwix", label="Remote"
    )
    runtime = KiwixRuntime(
        collection,
        KiwixConfig(name="remote", server="http://127.0.0.1:8085"),
    )
    try:
        assert runtime.content_store(
            SourceInfo(
                key="kiwix:remote:book-0123456789abcdef",
                provider="kiwix",
                title="Remote book",
                collection_key="kiwix:remote",
            )
        ) is None
    finally:
        runtime.close()


def test_ambiguous_path_fails_closed(tmp_path, monkeypatch):
    from zimantic.content.zim_paths import AmbiguousZimPathError
    import zimantic.providers.zim.store as store_module

    store = _store(tmp_path)
    monkeypatch.setattr(
        store_module,
        "resolve_zim_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AmbiguousZimPathError("ambiguous")
        ),
    )
    try:
        with pytest.raises(ArticleNotFoundError):
            store.resolve("Home")
        with pytest.raises(ArticleNotFoundError):
            store.read("Home", head_only=False, max_bytes=1 << 20)
    finally:
        store.close()


class _Item:
    def __init__(self, content: bytes, mimetype: str):
        self.content = content
        self.mimetype = mimetype
        self.size = len(content)


class _Entry:
    def __init__(self, path: str, content: bytes, mimetype: str):
        self.path = path
        self.title = path
        self._item = _Item(content, mimetype)

    def get_item(self):
        return self._item


class _IconArchive:
    main_entry = _Entry("Home", HOME_HTML, "text/html")

    def get_entry_by_path(self, path):
        if path == "-/icon":
            return _Entry("-/icon", b"metadata-icon", "image/png")
        if path == "Home":
            return self.main_entry
        raise KeyError(path)

    def get_illustration_item(self, _size):
        return _Item(b"illustration", "image/png")


def test_source_icon_prefers_metadata_icon_over_illustration(tmp_path):
    source = SourceInfo(
        key="zim:library:manual-0123456789abcdef",
        provider="zim",
        title="Manual",
        homepage="Home",
    )
    store = ArchiveContentStore(
        source,
        _IconArchive(),
        max_article_bytes=1 << 20,
    )
    try:
        icon = store.icon(1 << 20)
        assert icon is not None
        assert icon.content == b"metadata-icon"
    finally:
        store.close()
