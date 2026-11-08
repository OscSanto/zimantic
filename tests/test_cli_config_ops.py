"""Phase 08: config validation, config writes, CLI selectors, and prune."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zimantic.__main__ import (
    _make_parser,
    _selected_configured_entries,
    main,
)
from zimantic.config_writer import ConfigWriteError, append_collection
from zimantic.indexing.locator import IndexLocator
from zimantic.prune import collect_inventory, delete_candidates
from zimantic.providers.config import (
    ConfigIssue,
    ConfigValidationError,
    parse_collections,
)
from zimantic.settings import load_config


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _source(key="files:gone:root", provider="files", collection_key="files:gone"):
    return SimpleNamespace(
        key=key, provider=provider, collection_key=collection_key
    )


def _publish(tmp_path: Path, source) -> IndexLocator:
    database = tmp_path / "seed.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    locator = IndexLocator(tmp_path / "indexes")
    lease = locator.publish(source, database)
    lease.close()
    return locator


class TestConfigSchema:
    @pytest.mark.parametrize("provider", ["files", "kolibri", "zim", "kiwix"])
    def test_scoped_settings_inherit_without_leaking_to_other_sources(
        self, tmp_path, provider
    ):
        from zimantic.scoped_settings import resolve_settings

        locator = (
            'server = "http://localhost:8085"'
            if provider == "kiwix"
            else 'path = "media"'
        )
        path = _write(
            tmp_path / "config.toml",
            "max_entry_bytes = 128\nmax_body_text_bytes = 64\ncandidate_count = 32\n"
            "cache_size = 10\ncache_bytes = 1024\nsource_timeout = 2.5\n"
            f'[[collections]]\ntype = "{provider}"\nname = "media"\n{locator}\n'
            "max_entry_bytes = 256\nmax_entries = 2\nmax_preview_chars = 9\n"
            "min_cosine_similarity = 0.6\ncache_size = 4\nnprobe = 3\n"
            f'sources = [{{ key = "{provider}:media:custom", max_entry_bytes = 512, '
            "max_body_text_bytes = 16, candidate_count = 4, cache_bytes = 128 }]\n",
        )
        cfg = load_config(path, warn=lambda _: None)
        assert not cfg["config_issues"]
        custom = SimpleNamespace(
            key=f"{provider}:media:custom", collection_key=f"{provider}:media"
        )
        other = SimpleNamespace(
            key=f"{provider}:media:other", collection_key=f"{provider}:media"
        )
        effective = resolve_settings(cfg, custom)
        inherited = resolve_settings(cfg, other)
        assert effective["max_entry_bytes"] == 512
        assert inherited["max_entry_bytes"] == 256
        assert effective["max_entries"] == inherited["max_entries"] == 2
        assert effective["max_body_text_bytes"] == 16
        assert inherited["max_body_text_bytes"] == 64
        assert effective["candidate_count"] == 4
        assert inherited["candidate_count"] == 32
        assert effective["cache_size"] == 4
        assert effective["cache_bytes"] == 128
        assert effective["max_preview_chars"] == 9
        assert effective["min_cosine_similarity"] == 0.6
        assert effective["nprobe"] == 3
        assert effective["source_timeout"] == 2.5
        assert cfg["max_entry_bytes"] == 128

    @pytest.mark.parametrize("scope", ["global", "collection", "source"])
    @pytest.mark.parametrize("option", [
        "max_entry_bytes = 0", "cache_size = -1", "nprobe = true",
        "min_cosine_similarity = 1.5", "source_timeout = inf",
        'index_include = [""]',
    ])
    def test_invalid_scoped_settings_are_not_applied(self, tmp_path, scope, option):
        collection = '[[collections]]\ntype = "files"\nname = "media"\npath = "media"\n'
        if scope == "global":
            text = f"{option}\n{collection}"
        elif scope == "collection":
            text = collection + option + "\n"
        else:
            text = (
                collection + f'sources = [{{ key = "files:media:root", {option} }}]\n'
            )
        path = _write(tmp_path / "config.toml", text)
        warnings = []
        if scope == "source":
            with pytest.raises(ConfigValidationError):
                load_config(path, warn=warnings.append)
        else:
            cfg = load_config(path, warn=warnings.append)
            assert warnings
            if scope == "collection":
                assert cfg["collections"] == ()

    def test_missing_config_is_an_explicit_empty_library(self, tmp_path):
        cfg = load_config(tmp_path / "missing.toml", warn=lambda _message: None)
        assert cfg["collections"] == ()
        assert cfg["config_present"] is False

    def test_all_provider_scopes_and_global_keys(self, tmp_path):
        config = _write(
            tmp_path / "config.toml",
            'port = 8123\n'
            'max_explicit_collection_sources = 12\n'
            'search_timeout_seconds = 1.5\n'
            '[[collections]]\n'
            'type = "zim"\n'
            'name = "library"\n'
            'path = "zims"\n'
            '[[collections]]\n'
            'type = "files"\n'
            'name = "modules"\n'
            'path = "modules"\n'
            '[[collections]]\n'
            'type = "kolibri"\n'
            'name = "kolibri"\n'
            'path = "kolibri"\n'
            '[[collections]]\n'
            'type = "kiwix"\n'
            'name = "remote"\n'
            'server = "http://localhost:8085"\n'
            'max_remote_sources = 0\n'
            '[derived_text]\n'
            'workers = "auto"\n',
        )
        cfg = load_config(config, warn=lambda _message: None)
        assert [entry.kind for entry in cfg["collections"]] == [
            "zim",
            "files",
            "kolibri",
            "kiwix",
        ]
        assert cfg["port"] == 8123
        assert cfg["max_explicit_collection_sources"] == 12
        assert cfg["search_timeout_seconds"] == 1.5
        kiwix = cfg["collections"][-1]
        assert kiwix.max_remote_sources == 0
        assert cfg["derived_text"].workers == "auto"
        assert cfg["derived_text_workers"]["configured"] == "auto"
        assert cfg["derived_text_workers"]["resolved"] >= 0
        assert cfg["collections"][0].path == str(tmp_path / "zims")
        assert cfg["collections"][1].path == str(tmp_path / "modules")

    @pytest.mark.parametrize("provider", ["files", "kolibri"])
    def test_derived_options_inherit_global_collection_and_source(self, tmp_path, provider):
        from zimantic.indexing.derived import source_derived_config, source_operations

        config = _write(
            tmp_path / "config.toml",
            '[derived_text]\n'
            'ocr_languages = ["eng", "ita"]\n'
            'asr_model = "base"\n'
            'asr_threads = 2\n'
            'max_ocr_pages = 7\n'
            'task_timeout_seconds = 50\n'
            '[[collections]]\n'
            f'type = "{provider}"\n'
            'name = "media"\n'
            'path = "./media"\n'
            'derived_text = { ocr = "tesseract", ocr_languages = ["fre"], asr_model = "tiny", cache = false }\n'
            'sources = [\n'
            f'  {{ key = "{provider}:media:custom", ocr_languages = ["fra"], max_ocr_pages = 2 }},\n'
            f'  {{ key = "{provider}:media:disabled", ocr = "off", asr_model = "small" }},\n'
            ']\n'
            '[[collections]]\n'
            f'type = "{provider}"\n'
            'name = "other"\n'
            'path = "./other"\n'
            'sources = [\n'
            f'  {{ key = "{provider}:other:custom", ocr_languages = ["deu"] }},\n'
            ']\n',
        )
        cfg = load_config(config, warn=lambda _: None)
        assert not cfg["config_issues"]

        def source(name, suffix):
            return SimpleNamespace(
                key=f"{provider}:{name}:{suffix}", collection_key=f"{provider}:{name}"
            )

        inherited = source_derived_config(cfg, source("media", "default"))
        assert inherited.ocr_languages == ("fre",)
        assert inherited.asr_model == "tiny"
        assert inherited.asr_threads == 2
        assert inherited.max_ocr_pages == 7
        assert inherited.task_timeout_seconds == 50
        assert inherited.cache is False
        custom = source_derived_config(cfg, source("media", "custom"))
        assert custom.ocr_languages == ("fra",)
        assert custom.max_ocr_pages == 2
        assert custom.asr_model == "tiny"
        assert source_operations(cfg, source("media", "custom")).ocr == "tesseract"
        assert source_operations(cfg, source("media", "disabled")).any_enabled is False
        assert source_derived_config(cfg, source("media", "disabled")).asr_model == "small"
        assert source_operations(cfg, source("other", "custom")).any_enabled is False
        other = source_derived_config(cfg, source("other", "default"))
        assert other.ocr_languages == ("eng", "ita")
        assert other.asr_model == "base"
        assert other.cache is True
        assert cfg["derived_text"].ocr_languages == ("eng", "ita")
        envelope = json.loads(json.dumps(dict(cfg["runtime_config"].envelope)))
        assert envelope["collection_derived_text"]["media"]["ocr_languages"] == ["fre"]
        assert envelope["source_operations"][f"{provider}:media:custom"]["max_ocr_pages"] == 2

    @pytest.mark.parametrize("scope", ["global", "collection", "source"])
    @pytest.mark.parametrize(
        "option",
        [
            'ocr_languages = []',
            'ocr_languages = "fra"',
            'ocr_languages = [" "]',
            'max_ocr_pages = 0',
            'asr_engine = "unknown"',
            'asr_model = 3',
            'cache = "false"',
            'workers = true',
        ],
    )
    def test_invalid_derived_options_are_rejected_at_every_scope(self, tmp_path, scope, option):
        collection = '[[collections]]\ntype = "files"\nname = "media"\npath = "media"\n'
        if scope == "global":
            text = f"[derived_text]\n{option}\n{collection}"
        elif scope == "collection":
            text = collection + f"derived_text = {{ {option} }}\n"
        else:
            text = collection + f'sources = [{{ key = "files:media:root", {option} }}]\n'
        config = _write(tmp_path / "config.toml", text)
        if scope == "collection":
            cfg = load_config(config, warn=lambda _: None)
            assert cfg["collections"] == ()
            assert cfg["config_issues"]
        else:
            with pytest.raises(ConfigValidationError):
                load_config(config, warn=lambda _: None)

    @pytest.mark.parametrize("scope", ["collection", "source"])
    def test_unknown_derived_override_options_are_rejected(self, tmp_path, scope):
        text = '[[collections]]\ntype = "files"\nname = "media"\npath = "media"\n'
        if scope == "collection":
            text += 'derived_text = { ocr_languges = ["fra"] }\n'
        else:
            text += 'sources = [{ key = "files:media:root", ocr_languges = ["fra"] }]\n'
        config = _write(tmp_path / "config.toml", text)
        if scope == "collection":
            cfg = load_config(config, warn=lambda _: None)
            assert any("unknown derived_text" in issue.message for issue in cfg["config_issues"])
        else:
            with pytest.raises(ConfigValidationError, match="unknown source override"):
                load_config(config, warn=lambda _: None)

    def test_duplicate_unknown_and_invalid_entries_do_not_hide_valid_ones(
        self, tmp_path
    ):
        warnings: list[str] = []
        parsed = parse_collections(
            {
                "collections": [
                    {
                        "type": "files",
                        "name": "dup",
                        "path": "one",
                    },
                    {"type": "files", "name": "dup", "path": "two"},
                    {"type": "nope", "name": "bad", "path": "three"},
                    {"type": "files", "name": "broken"},
                    {
                        "type": "files",
                        "name": "good",
                        "path": "good",
                        "typo_key": True,
                    },
                ]
            },
            config_dir=tmp_path,
            warn=warnings.append,
        )
        assert [entry.name for entry in parsed.collections] == ["good"]
        assert any("configured more than once" in message for message in warnings)
        assert any("invalid type" in message for message in warnings)
        assert any("typo_key" in message for message in warnings)
        assert {issue.namespace for issue in parsed.issues if issue.namespace}

    def test_flat_provider_keys_are_fatal_with_a_migration_message(self, tmp_path):
        config = _write(
            tmp_path / "config.toml",
            'zim_dir = "old"\n'
            '[[collections]]\n'
            'type = "files"\n'
            'name = "files"\n'
            'path = "files"\n',
        )
        with pytest.raises(ConfigValidationError, match="zim_dir"):
            load_config(config, warn=lambda _message: None)


class TestConfigWriter:
    def test_append_preserves_content_mode_and_relative_path(self, tmp_path):
        config = _write(tmp_path / "config.toml", "port = 8123")
        os.chmod(config, 0o600)
        appended = append_collection(
            config,
            lambda _data: {
                "type": "zim",
                "name": "book",
                "path": os.path.relpath(
                    tmp_path / "zims" / "book.zim", tmp_path
                ),
            },
        )
        assert appended is not None
        text = config.read_text(encoding="utf-8")
        assert text.startswith("port = 8123\n")
        assert '[[collections]]' in text
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert appended.path == str(tmp_path / "zims" / "book.zim")

    def test_append_escapes_toml_and_validates_the_result(self, tmp_path):
        config = _write(tmp_path / "config.toml", 'port = 1\n')
        appended = append_collection(
            config,
            lambda _data: {
                "type": "zim",
                "name": "quoted",
                "path": os.path.relpath(tmp_path / 'a "quote".zim', tmp_path),
            },
        )
        assert appended is not None
        cfg = load_config(config, warn=lambda _message: None)
        assert cfg["collections"][0].path == str(tmp_path / 'a "quote".zim')

    def test_symlinked_config_updates_the_target_and_keeps_the_link(self, tmp_path):
        target = _write(tmp_path / "real.toml", "port = 1\n")
        link = tmp_path / "config.toml"
        link.symlink_to(target)
        appended = append_collection(
            link,
            lambda _data: {
                "type": "zim",
                "name": "book",
                "path": "zims/book.zim",
            },
        )
        assert appended is not None
        assert link.is_symlink()
        assert "[[collections]]" in target.read_text(encoding="utf-8")

    def test_hard_linked_config_is_not_silently_detached(self, tmp_path):
        target = _write(tmp_path / "config.toml", "port = 1\n")
        other = tmp_path / "other.toml"
        os.link(target, other)
        original = target.read_text(encoding="utf-8")
        with pytest.raises(ConfigWriteError, match="hard link"):
            append_collection(
                target,
                lambda _data: {
                    "type": "zim",
                    "name": "book",
                    "path": "zims/book.zim",
                },
            )
        assert target.read_text(encoding="utf-8") == original
        assert other.read_text(encoding="utf-8") == original

    def test_duplicate_name_is_refused_under_the_lock(self, tmp_path):
        config = _write(
            tmp_path / "config.toml",
            '[[collections]]\ntype = "zim"\nname = "book"\npath = "zims"\n',
        )
        with pytest.raises(ConfigWriteError, match="already configured"):
            append_collection(
                config,
                lambda _data: {
                    "type": "zim",
                    "name": "book",
                    "path": "other",
                },
            )


class TestCliSelectors:
    def _cfg(self, tmp_path):
        config = _write(
            tmp_path / "config.toml",
            '[[collections]]\ntype = "zim"\nname = "library"\npath = "zims"\n'
            '[[collections]]\ntype = "files"\nname = "modules"\npath = "modules"\n'
            '[[collections]]\ntype = "kolibri"\nname = "kolibri"\npath = "k"\n',
        )
        return load_config(config, warn=lambda _message: None)

    def test_repeatable_selectors_or_inside_and_intersect_across(self, tmp_path):
        cfg = self._cfg(tmp_path)
        assert [
            entry.name
            for entry in _selected_configured_entries(
                cfg, provider_selectors=["files", "zim"]
            )
        ] == ["library", "modules"]
        assert [
            entry.name
            for entry in _selected_configured_entries(
                cfg, collection_selectors=["zim:library", "modules"]
            )
        ] == ["library", "modules"]
        assert (
            _selected_configured_entries(
                cfg,
                provider_selectors=["files"],
                collection_selectors=["zim:library"],
            )
            == []
        )
        assert [
            entry.name
            for entry in _selected_configured_entries(
                cfg, collection_selectors=["kolibri:kolibri:channel"]
            )
        ] == ["kolibri"]

    def test_parser_accepts_new_commands_and_selector_forms(self):
        parser = _make_parser()
        args = parser.parse_args(
            [
                "build",
                "--provider",
                "files",
                "--provider",
                "zim",
                "--collection",
                "modules",
                "--source",
                "files:modules:root",
                "--list",
            ]
        )
        assert args.provider == ["files", "zim"]
        assert args.collection == ["modules"]
        assert args.source == ["files:modules:root"]
        assert args.list is True
        assert parser.parse_args(["sources", "--json"]).json is True
        assert parser.parse_args(["prune"]).yes is False

    def test_positional_paths_cannot_mix_with_selectors(self, tmp_path, monkeypatch):
        config = _write(
            tmp_path / "config.toml",
            '[[collections]]\ntype = "files"\nname = "modules"\npath = "modules"\n',
        )
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as raised:
            main(["build", str(config), "--provider", "files"])
        assert raised.value.code == 2


class TestSourcesAndPrune:
    def test_sources_json_is_read_only_and_has_no_absolute_paths(
        self, tmp_path, capsys, monkeypatch
    ):
        (tmp_path / "modules").mkdir()
        (tmp_path / "modules" / "a.html").write_text("hello", encoding="utf-8")
        config = _write(
            tmp_path / "config.toml",
            'index_dir = "indexes"\n'
            '[[collections]]\ntype = "files"\nname = "modules"\n'
            'path = "modules"\n',
        )
        monkeypatch.chdir(tmp_path)
        main(["sources", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {"collections", "sources"}
        serialized = json.dumps(payload)
        assert str(tmp_path) not in serialized
        assert not (tmp_path / "indexes").exists()
        assert not (tmp_path / ".build.lock").exists()
        assert config.exists()

    def test_build_list_does_not_take_the_build_lock(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "modules").mkdir()
        (tmp_path / "modules" / "a.html").write_text("hello", encoding="utf-8")
        _write(
            tmp_path / "config.toml",
            'index_dir = "indexes"\n'
            '[[collections]]\ntype = "files"\nname = "modules"\n'
            'path = "modules"\n',
        )
        monkeypatch.chdir(tmp_path)
        main(["build", "--list"])
        assert not (tmp_path / ".build.lock").exists()
        assert not (tmp_path / "indexes").exists()
        assert "files:modules" in capsys.readouterr().out

    def test_prune_reports_only_owned_orphans_and_respects_config(self, tmp_path):
        source = _source()
        locator = _publish(tmp_path, source)
        unknown = tmp_path / "indexes" / "legacy.sqlite"
        unknown.write_bytes(b"legacy")

        empty = {"collections": (), "config_issues": ()}
        inventory = collect_inventory(tmp_path / "indexes", empty, None)
        assert [candidate.source_key for candidate in inventory.candidates] == [
            source.key
        ]
        assert any("legacy.sqlite" in problem for problem in inventory.problems)

        configured = {
            "collections": [SimpleNamespace(kind="files", name="gone")],
            "config_issues": (),
        }
        retained = collect_inventory(tmp_path / "indexes", configured, None)
        assert retained.candidates == []

        lease = locator.paths(source)
        assert lease is not None
        try:
            pinned = collect_inventory(tmp_path / "indexes", empty, None)
            assert pinned.candidates[0].pinned is True
        finally:
            lease.close()
        removed, reclaimed = delete_candidates(
            collect_inventory(tmp_path / "indexes", empty, None)
        )
        assert reclaimed > 0
        assert len(removed) == 1
        assert not removed[0].directory.exists()
        assert unknown.exists()

    def test_mixed_startup_serves_zim_before_slow_local_providers(
        self, tmp_path
    ):
        from zimantic.search import Search

        (tmp_path / "zims").mkdir()
        (tmp_path / "files").mkdir()
        (tmp_path / "files" / "a.html").write_text("hello", encoding="utf-8")
        config = _write(
            tmp_path / "config.toml",
            'index_dir = "indexes"\n'
            '[[collections]]\ntype = "zim"\nname = "library"\n'
            'path = "zims"\n'
            '[[collections]]\ntype = "files"\nname = "files"\n'
            'path = "files"\n',
        )
        cfg = load_config(config, warn=lambda _message: None)
        search = Search(cfg, semantic=False)
        try:
            assert "zim:library" in search.collections
            deadline = time.monotonic() + 5
            while (
                "files:files" not in search.collections
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert "files:files" in search.collections
        finally:
            search.close()

    def test_unknown_namespace_never_prunes_configured_indexes(self, tmp_path):
        source = _source()
        _publish(tmp_path, source)
        cfg = {
            "collections": (),
            "config_issues": (
                ConfigIssue(
                    index=0,
                    message="invalid entry",
                    kind=None,
                    name=None,
                    namespace=None,
                    provider_wide=True,
                ),
            ),
        }
        inventory = collect_inventory(tmp_path / "indexes", cfg, None)
        assert inventory.candidates == []
