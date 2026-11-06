"""Fresh-interpreter import check for the pure contract modules."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def test_contract_modules_import_without_runtime_dependencies():
    script = textwrap.dedent(
        """
        import sys
        import types

        class _Blocker:
            def find_module(self, name, path=None):
                return self if name.split('.')[0] in {'libzim', 'faiss', 'fastapi'} else None
            def load_module(self, name):
                raise ImportError(f"{name} is intentionally unavailable")

        sys.meta_path.insert(0, _Blocker())
        import zimantic.content.store
        import zimantic.providers.base
        import zimantic.providers.registry
        import zimantic.catalog
        import zimantic.search
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_content_store_and_provider_exports_are_the_same_types():
    import zimantic.content.store as store
    import zimantic.providers.base as base

    assert base.ContentStore is store.ContentStore
    assert base.ContentEntry is store.ContentEntry
    assert base.ArticleData is store.ArticleData
    assert base.ContentRange is store.ContentRange


def test_content_store_method_set_is_frozen():
    from zimantic.content.store import ContentStore

    expected = {
        "main_path",
        "resolve",
        "exists",
        "read",
        "read_range",
        "random_path",
        "icon",
        "close",
    }
    actual = {
        name
        for name in ContentStore.__dict__
        if not name.startswith("_")
    }
    assert actual == expected


def test_source_info_payload_has_one_discriminator_and_one_identity():
    from zimantic.contracts import SourceInfo

    zim = SourceInfo(
        key="zim:library:wikipedia_en_all-0123456789abcdef",
        provider="zim",
        title="Wikipedia",
        collection_key="zim:library",
        collection_label="Library",
        homepage="Main_Page",
    )
    payload = zim.to_dict(include_metadata=True)
    assert payload["provider"] == "zim"
    assert payload["collection"] == {
        "key": "zim:library",
        "label": "Library",
        "provider": "zim",
    }
    for forbidden in ("mode", "content_kind", "name", "book", "local_name"):
        assert forbidden not in payload
    assert "C:\\" not in str(payload) and "/home/" not in str(payload)

    kiwix = SourceInfo(
        key="kiwix:remote:book-0123456789abcdef",
        provider="kiwix",
        title="Remote",
        collection_key="kiwix:remote",
        collection_label="Remote",
    )
    assert kiwix.to_dict()["provider"] == "kiwix"


def test_source_info_requires_an_explicit_provider():
    from zimantic.contracts import SourceInfo

    with pytest.raises(TypeError):
        SourceInfo(key="hand-built", title="No provider")


def test_media_types_are_serialized_in_taxonomy_order():
    from zimantic.contracts import SourceInfo

    source = SourceInfo(
        key="zim:library:x-1",
        provider="zim",
        title="X",
        media_types=("image", "text", "audio"),
    )
    assert source.to_dict()["media_types"] == ["text", "audio", "image"]


def test_collection_snapshot_combines_descriptor_and_runtime_fields():
    from zimantic.providers.base import CollectionInfo, CollectionSnapshot

    collection = CollectionInfo(
        key="zim:library", provider="zim", label="Library", metadata={"a": 1}
    )
    snapshot = CollectionSnapshot(
        collection=collection,
        source_count=3,
        media_types=("video", "text"),
        error=None,
        discovery_state="ready",
        snapshot_version=7,
    )
    payload = snapshot.to_dict()
    assert payload["key"] == "zim:library"
    assert payload["source_count"] == 3
    assert payload["media_types"] == ["text", "video"]
    assert payload["snapshot_version"] == 7
    assert "source_count" not in collection.to_dict()


def test_result_document_id_encoding_is_unambiguous():
    from zimantic.contracts import encode_document_id

    assert encode_document_id("a:b", "c:d") != encode_document_id("a", "b:c:d")
    # The UTF-8 byte-length prefix makes the pair reconstructible.
    encoded = encode_document_id("é:x", "path")
    length, rest = encoded.split(":", 1)
    assert int(length) == len("é:x".encode("utf-8"))
    assert rest == "é:xpath"


def test_minimal_collections_parser_normalizes_and_resolves_paths(tmp_path):
    from zimantic.providers.config import KiwixConfig, ZimConfig, parse_collections

    parsed = parse_collections(
        {
            "collections": [
                {"type": "zim", "name": "library", "path": "zims"},
                {
                    "type": "kiwix",
                    "name": "remote",
                    "server": "http://localhost:8085",
                    "max_remote_sources": 0,
                },
            ]
        },
        config_dir=tmp_path,
    )
    assert [entry.kind for entry in parsed.collections] == ["zim", "kiwix"]
    zim = parsed.collections[0]
    assert isinstance(zim, ZimConfig)
    assert zim.path == str(tmp_path / "zims")
    kiwix = parsed.collections[1]
    assert isinstance(kiwix, KiwixConfig)
    assert kiwix.max_remote_sources == 0


def test_removed_flat_provider_keys_are_fatal(tmp_path):
    from zimantic.providers.config import ConfigValidationError, parse_collections

    for key, value in (
        ("zim_dir", "zims"),
        ("kiwix_server", "http://localhost:8085"),
        ("max_remote_sources", 10),
    ):
        with pytest.raises(ConfigValidationError):
            parse_collections({key: value}, config_dir=tmp_path)


def test_duplicate_collection_names_disable_all_duplicates(tmp_path):
    from zimantic.providers.config import parse_collections

    parsed = parse_collections(
        {
            "collections": [
                {"type": "zim", "name": "same", "path": "a"},
                {"type": "zim", "name": "same", "path": "b"},
                {"type": "zim", "name": "other", "path": "c"},
            ]
        },
        config_dir=tmp_path,
    )
    assert [entry.name for entry in parsed.collections] == ["other"]
    assert any("same" in message for message in parsed.diagnostics)


def test_files_and_kolibri_are_disabled_with_diagnostics(tmp_path):
    from zimantic.providers.config import parse_collections

    parsed = parse_collections(
        {
            "collections": [
                {"type": "files", "name": "files", "path": "files"},
                {"type": "kolibri", "name": "kolibri", "path": "kolibri"},
            ]
        },
        config_dir=tmp_path,
    )
    assert parsed.collections == ()
    assert len(parsed.diagnostics) == 2
