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
from zimantic.paths import default_config_path
from zimantic.providers.config import (
    ConfigIssue,
    ConfigValidationError,
    parse_collections,
)
from zimantic.prune import collect_inventory, delete_candidates
from zimantic.settings import load_config


@pytest.fixture(autouse=True)
def _isolated_cli_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


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
            'cache = 0',
            'asr_batched = "false"',
            'asr_batched = 1',
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
                    },
                    {
                        "type": "files",
                        "name": "typo",
                        "path": "typo",
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

    def test_unknown_global_keys_are_fatal(self, tmp_path):
        config = _write(
            tmp_path / "config.toml",
            'zim_dir = "old"\n'
            '[[collections]]\n'
            'type = "files"\n'
            'name = "files"\n'
            'path = "files"\n',
        )
        with pytest.raises(ConfigValidationError, match="unknown config key.*zim_dir"):
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
        if os.name != "nt":
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


class TestPositionalBuild:
    def _config(self, tmp_path, collections=""):
        config = default_config_path()
        config.parent.mkdir(parents=True, exist_ok=True)
        return _write(
            config,
            f"index_dir = {json.dumps(str(tmp_path / 'indexes'))}\n" + collections,
        )

    def _files_collection(self, root, *, name="modules", granularity="collection"):
        return (
            f'[[collections]]\ntype = "files"\nname = "{name}"\n'
            f'path = {json.dumps(str(root))}\ngranularity = "{granularity}"\n'
        )

    def _index(self, tmp_path, key="files:modules:root"):
        from index_helpers import docs, meta

        source = _source(key, "files", ":".join(key.split(":")[:2]))
        locator = IndexLocator(tmp_path / "indexes")
        return docs(locator, source), meta(locator, source)

    def test_configured_directory_builds_files_without_editing_config(self, tmp_path):
        root = tmp_path / "modules"
        root.mkdir()
        _write(root / "alpha.txt", "alpha")
        config = self._config(tmp_path, self._files_collection(root))
        original = config.read_bytes()
        main(["build", str(root), "--fast"])
        rows, meta = self._index(tmp_path)
        assert [row[2] for row in rows] == ["alpha.txt"]
        assert meta["truncated"] == "0"
        assert "build_scope" not in meta
        assert config.read_bytes() == original

    def test_empty_covered_directory_removes_previously_indexed_results(self, tmp_path):
        root = tmp_path / "modules"
        for child in ("first", "second"):
            (root / child).mkdir(parents=True)
            _write(root / child / "page.txt", child)
        self._config(tmp_path, self._files_collection(root))
        main(["build", str(root / "first"), "--fast"])
        (root / "first" / "page.txt").unlink()
        main(["build", str(root / "first"), "--fast"])
        rows, meta = self._index(tmp_path)
        assert rows == []
        assert meta["truncated"] == "1"
        assert json.loads(meta["build_scope"])["partial"] is True

    def test_partial_scans_accumulate_update_and_remove_covered_files(self, tmp_path, monkeypatch):
        from index_helpers import body_hits

        root = tmp_path / "modules"
        for child in ("first", "second", "third"):
            (root / child).mkdir(parents=True)
            _write(root / child / "page.txt", child + " zebra")
        _write(root / "first" / "removed.txt", "obsolete")
        config = self._config(tmp_path, self._files_collection(root))
        original = config.read_bytes()
        # Avoid a model download while exercising real body FTS publication.
        from zimantic.__main__ import _make_parser, _run_build

        cfg = load_config(config, warn=lambda _: None)
        monkeypatch.setattr("zimantic.__main__._LazyEmbedder", lambda _: None)

        def build(paths):
            args = _make_parser().parse_args(["build", *map(str, paths)])
            _run_build(args, cfg, _make_parser())

        build([root / "first"])
        rows, meta = self._index(tmp_path)
        assert {row[2] for row in rows} == {"first/page.txt", "first/removed.txt"}
        assert json.loads(meta["build_scope"])["partial"] is True
        assert meta["truncated"] == "1"
        generation = meta["build_generation"]
        build([root / "first"])
        assert self._index(tmp_path)[1]["build_generation"] == generation

        (root / "first" / "removed.txt").unlink()
        _write(root / "first" / "page.txt", "updated aardvark")
        build([root / "second"])
        rows, meta = self._index(tmp_path)
        assert {row[2] for row in rows} == {"first/page.txt", "second/page.txt"}
        coverage = json.loads(meta["build_scope"])
        assert coverage == {
            "partial": True,
            "paths": [
                {"path": "first", "recursive": True},
                {"path": "second", "recursive": True},
            ],
        }
        locator = IndexLocator(tmp_path / "indexes")
        assert body_hits(locator, _source("files:modules:root", "files", "files:modules"), "aardvark")
        assert not body_hits(locator, _source("files:modules:root", "files", "files:modules"), "obsolete")

        build([root])
        rows, meta = self._index(tmp_path)
        assert len(rows) == 3
        assert "build_scope" not in meta
        assert meta["truncated"] == "0"
        # A later narrow scan must not discard the complete generation's rows.
        build([root / "first"])
        assert self._index(tmp_path)[1]["build_generation"] == meta["build_generation"]
        assert config.read_bytes() == original

    def test_child_path_leaves_sibling_sources_unbuilt(self, tmp_path, capsys):
        from zimantic.providers.files.discovery import files_dir_key

        root = tmp_path / "modules"
        for child in ("first", "second"):
            (root / child).mkdir(parents=True)
            _write(root / child / "page.txt", child)
        self._config(tmp_path, self._files_collection(root, granularity="children"))
        main(["build", str(root / "first"), "--fast"])
        _, meta = self._index(tmp_path, files_dir_key("modules", "first"))
        assert meta["truncated"] == "0"
        assert "build_scope" not in meta
        assert IndexLocator(tmp_path / "indexes").paths(
            _source(files_dir_key("modules", "second"), "files", "files:modules")
        ) is None
        main(["sources", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["collections"][0]["index_count"] == 1
        assert payload["collections"][0]["source_count"] == 2
        assert payload["collections"][0]["index_partial"] is True
        indexed = next(source for source in payload["sources"] if source["searchable"])
        assert indexed["index_partial"] is False

    def test_unmatched_file_registers_parent_but_indexes_only_named_file(self, tmp_path):
        root = tmp_path / "notes"
        root.mkdir()
        _write(root / "alpha.txt", "alpha")
        _write(root / "beta.txt", "beta")
        config = self._config(tmp_path)
        main(["build", str(root / "alpha.txt"), "--fast"])
        cfg = load_config(config, warn=lambda _: None)
        assert [(entry.kind, entry.name, entry.path) for entry in cfg["collections"]] == [
            ("files", "notes", str(root))
        ]
        rows, meta = self._index(tmp_path, "files:notes:root")
        assert [row[2] for row in rows] == ["alpha.txt"]
        assert json.loads(meta["build_scope"])["partial"] is True
        main(["build", str(root / "beta.txt"), "--fast"])
        assert len(self._index(tmp_path, "files:notes:root")[0]) == 2
        assert len(load_config(config, warn=lambda _: None)["collections"]) == 1

    def test_mixed_directory_registers_and_builds_both_providers(self, tmp_path, capsys):
        from providers.zim.test_provider import _create_zim

        root = tmp_path / "mixed"
        root.mkdir()
        _create_zim(root / "book.zim")
        _write(root / "notes.txt", "alpha")
        config = self._config(tmp_path)
        main(["build", str(root), "--fast"])
        cfg = load_config(config, warn=lambda _: None)
        assert [(entry.kind, entry.name) for entry in cfg["collections"]] == [
            ("zim", "mixed"), ("files", "mixed-2")
        ]
        rows, _ = self._index(tmp_path, "files:mixed-2:root")
        assert [row[2] for row in rows] == ["notes.txt"]
        capsys.readouterr()
        main(["sources", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert {source["provider"] for source in payload["sources"]} == {"zim", "files"}
        assert all(source["searchable"] for source in payload["sources"])

    def test_mixed_build_continues_files_after_a_zim_failure(self, tmp_path):
        root = tmp_path / "mixed"
        root.mkdir()
        (root / "broken.zim").write_bytes(b"broken")
        _write(root / "notes.txt", "alpha")
        self._config(tmp_path)
        with pytest.raises(SystemExit) as raised:
            main(["build", str(root), "--fast"])
        assert raised.value.code == 1
        assert [row[2] for row in self._index(tmp_path, "files:mixed-2:root")[0]] == ["notes.txt"]

    @pytest.mark.parametrize("overlapping", [False, True])
    def test_explicit_zim_in_a_files_tree_is_built_as_an_archive(self, tmp_path, capsys, overlapping):
        from providers.zim.test_provider import _create_zim

        root = tmp_path / "modules"
        root.mkdir()
        archive = root / "book.zim"
        _create_zim(archive)
        collections = self._files_collection(root)
        if overlapping:
            collections += self._files_collection(root, name="other")
        config = self._config(tmp_path, collections)
        main(["build", str(archive), "--fast"])
        assert [entry.kind for entry in load_config(config, warn=lambda _: None)["collections"]] == [
            *(["files", "files"] if overlapping else ["files"]), "zim"
        ]
        capsys.readouterr()
        main(["sources", "--json"])
        sources = json.loads(capsys.readouterr().out)["sources"]
        assert any(source["provider"] == "zim" and source["searchable"] for source in sources)

    def test_concurrently_registered_collection_is_used_for_the_build(self, tmp_path, monkeypatch):
        import zimantic.config_writer as writer

        root = tmp_path / "modules"
        root.mkdir()
        _write(root / "page.txt", "alpha")
        config = self._config(tmp_path)
        append = writer.append_collection

        def concurrent(config_path, factory):
            _write(config, config.read_text() + self._files_collection(root))
            return append(config_path, factory)

        monkeypatch.setattr(writer, "append_collection", concurrent)
        main(["build", str(root), "--fast"])
        assert len(self._index(tmp_path)[0]) == 1
        assert len(load_config(config, warn=lambda _: None)["collections"]) == 1

    def test_kolibri_storage_paths_select_nodes_not_raw_files(self, tmp_path, capsys):
        from providers.kolibri.helpers import make_two_channel_fixture

        fixture = make_two_channel_fixture(tmp_path)
        try:
            config = self._config(
                tmp_path,
                '[[collections]]\ntype = "kolibri"\nname = "lessons"\n'
                f'path = {json.dumps(str(fixture.home))}\ngranularity = "collection"\n',
            )
            original = config.read_bytes()
            main(["build", str(fixture.home), "--list"])
            assert "kolibri:lessons:channel-a:root" in capsys.readouterr().out
            assert not (tmp_path / "indexes").exists()

            storage_file = next(fixture.storage.rglob("*.pdf"))
            main(["build", str(storage_file), "--fast"])
            main(["sources", "--json"])
            payload = json.loads(capsys.readouterr().out)
            indexed = [source for source in payload["sources"] if source["searchable"]]
            assert [source["key"] for source in indexed] == ["kolibri:lessons:channel-a:root"]
            assert indexed[0]["index_partial"] is True
            assert indexed[0]["indexed_count"] == 2

            main(["build", str(fixture.db_path), "--fast"])
            main(["sources", "--json"])
            payload = json.loads(capsys.readouterr().out)
            assert all(source["searchable"] and not source["index_partial"] for source in payload["sources"])
            assert all(not collection["index_partial"] for collection in payload["collections"])
            assert config.read_bytes() == original
        finally:
            fixture.dispose()

    @pytest.mark.parametrize("configured", [False, True])
    def test_positional_preview_is_read_only_and_filters_sources(
        self, tmp_path, capsys, configured
    ):
        root = tmp_path / "modules"
        for child in ("first", "second"):
            (root / child).mkdir(parents=True)
            _write(root / child / "page.txt", child)
        config = self._config(
            tmp_path,
            self._files_collection(root, granularity="children") if configured else "",
        )
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        main(["build", str(root / "first"), "--list"])
        output = capsys.readouterr().out
        assert "files:modules" in output if configured else "files:first" in output
        assert "second" not in output
        assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        assert not (tmp_path / "indexes").exists()
        assert not (tmp_path / ".build.lock").exists()
        assert not config.with_name("config.toml.lock").exists()

    def test_ambiguous_roots_fail_without_writing(self, tmp_path):
        root = tmp_path / "modules"
        root.mkdir()
        _write(root / "page.txt", "alpha")
        config = self._config(
            tmp_path,
            self._files_collection(root) + self._files_collection(root, name="other"),
        )
        original = config.read_bytes()
        with pytest.raises(SystemExit) as raised:
            main(["build", str(root), "--list"])
        assert raised.value.code == 2
        assert config.read_bytes() == original

    def test_invalid_paths_fail_before_registering_other_paths(self, tmp_path):
        root = tmp_path / "modules"
        root.mkdir()
        _write(root / "page.txt", "alpha")
        config = self._config(tmp_path)
        original = config.read_bytes()
        with pytest.raises(SystemExit) as raised:
            main(["build", str(root), str(tmp_path / "missing"), "--fast"])
        assert raised.value.code == 2
        assert config.read_bytes() == original


class TestSourcesAndPrune:
    def test_sources_json_is_read_only_and_has_no_absolute_paths(
        self, tmp_path, capsys
    ):
        (tmp_path / "modules").mkdir()
        (tmp_path / "modules" / "a.html").write_text("hello", encoding="utf-8")
        config_path = default_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = _write(
            config_path,
            f"index_dir = {json.dumps(str(tmp_path / 'indexes'))}\n"
            '[[collections]]\ntype = "files"\nname = "modules"\n'
            f"path = {json.dumps(str(tmp_path / 'modules'))}\n",
        )
        main(["sources", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {"collections", "sources"}
        assert [collection["key"] for collection in payload["collections"]] == [
            "files:modules"
        ]
        assert [source["key"] for source in payload["sources"]] == [
            "files:modules:root"
        ]
        serialized = json.dumps(payload)
        assert str(tmp_path) not in serialized
        assert not (tmp_path / "indexes").exists()
        assert not (tmp_path / ".build.lock").exists()
        assert config.exists()

    def _catalog_config(self, tmp_path, monkeypatch, collections):
        cwd = tmp_path / "working"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        config = default_config_path()
        config.parent.mkdir(parents=True, exist_ok=True)
        return _write(
            config,
            f"index_dir = {json.dumps(str(tmp_path / 'indexes'))}\n" + collections,
        )

    def test_catalog_filters_before_paging_and_reports_membership_paths(
        self, tmp_path, monkeypatch, capsys
    ):
        import sys

        root = tmp_path / "modules"
        for name in ("Alpha", "Beta"):
            child = root / name
            child.mkdir(parents=True)
            _write(child / "index.html", f"<p>{name}</p>")
        _write(root / "loose.txt", "Loose text")
        config = self._catalog_config(
            tmp_path, monkeypatch,
            '[[collections]]\ntype = "files"\nname = "modules"\n'
            f'path = {json.dumps(str(root))}\ngranularity = "children"\n'
            'label = "Reference shelf"\n'
            '[[collections]]\ntype = "files"\nname = "other"\n'
            f'path = {json.dumps(str(root))}\n',
        )
        indexes = tmp_path / "indexes"
        indexes.mkdir()
        sentinel = indexes / "untouched.txt"
        _write(sentinel, "existing index state")
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        monkeypatch.setitem(sys.modules, "zimantic.embed", None)
        monkeypatch.setitem(sys.modules, "zimantic.search", None)
        main([
            "catalog", "REFERENCE", "--provider", "files",
            "--collection", "files:modules", "--media-type", "text",
            "--limit", "1", "--offset", "1", "--json",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 3
        assert payload["offset"] == payload["limit"] == 1
        assert len(payload["sources"]) == 1
        source = payload["sources"][0]
        assert source["title"] == "Beta"
        assert source["searchable"] is False
        assert source["paths"] == {"path": str(root / "Beta")}
        assert source["collection"]["key"] == "files:modules"
        assert [item["key"] for item in payload["collections"]] == ["files:modules"]
        assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        assert config.exists()

        main(["catalog", "--collection", "modules", "--relative", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert {item["paths"]["path"] for item in payload["sources"]} == {
            os.path.relpath(root / name, Path.cwd()) for name in ("Alpha", "Beta", "")
        }
        main(["catalog", "--source", source["key"], "--relative"])
        output = capsys.readouterr().out
        assert "Beta" in output
        assert f"path: {os.path.relpath(root / 'Beta', Path.cwd())}" in output
        assert "not searchable" in output
        assert not (indexes / ".build.lock").exists()

        main(["catalog", "bEtA", "--collection", "modules", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert [item["key"] for item in payload["sources"]] == [source["key"]]
        main(["catalog", "--media-type", "video", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 0
        assert payload["sources"] == []

    def test_catalog_zim_reports_nested_and_unreadable_archives(
        self, tmp_path, monkeypatch, capsys
    ):
        from providers.zim.test_provider import _create_zim

        root = tmp_path / "zims"
        nested = root / "nested"
        nested.mkdir(parents=True)
        archive = nested / "manual.zim"
        _create_zim(archive)
        broken = root / "broken.zim"
        broken.write_bytes(b"not an archive")
        self._catalog_config(
            tmp_path, monkeypatch,
            '[[collections]]\ntype = "zim"\nname = "books"\n'
            f'path = {json.dumps(str(root))}\n',
        )
        main(["catalog", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 2
        assert {item["paths"]["path"] for item in payload["sources"]} == {
            str(archive), str(broken)
        }
        assert any(item["searchable"] is False for item in payload["sources"])
        assert not (tmp_path / "indexes").exists()

    def test_catalog_kolibri_reports_original_not_snapshot_paths(
        self, tmp_path, monkeypatch, capsys
    ):
        from providers.kolibri.helpers import make_two_channel_fixture

        fixture = make_two_channel_fixture(tmp_path)
        try:
            self._catalog_config(
                tmp_path, monkeypatch,
                '[[collections]]\ntype = "kolibri"\nname = "lessons"\n'
                f'path = {json.dumps(str(fixture.home))}\n',
            )
            main(["catalog", "--json"])
            payload = json.loads(capsys.readouterr().out)
            assert payload["sources"]
            assert all(item["paths"] == {
                "path": str(fixture.home),
                "database_path": str(fixture.db_path),
                "storage": str(fixture.storage),
            } for item in payload["sources"])
            assert not (tmp_path / "indexes").exists()
        finally:
            fixture.dispose()

    def test_catalog_remote_entries_keep_urls_without_local_paths(
        self, tmp_path, monkeypatch, capsys
    ):
        from providers.kiwix.test_runtime import _FakeKiwix, _feed

        from zimantic.providers.kiwix import catalog

        fake = _FakeKiwix(catalog=_feed(["manual"]))
        monkeypatch.setattr(catalog, "urlopen", fake)
        self._catalog_config(
            tmp_path, monkeypatch,
            '[[collections]]\ntype = "kiwix"\nname = "remote"\n'
            'server = "http://kiwix"\n',
        )
        main(["catalog", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 1
        assert payload["sources"][0]["paths"] == {}
        assert payload["sources"][0]["homepage"] == "http://kiwix/content/manual"
        assert not (tmp_path / "indexes").exists()

    @pytest.mark.parametrize("arguments", [
        ["--limit", "0"], ["--offset", "-1"], ["--provider", "unknown"],
        ["--media-type", "unknown"],
    ])
    def test_catalog_rejects_invalid_filters(self, arguments):
        with pytest.raises(SystemExit) as raised:
            main(["catalog", *arguments])
        assert raised.value.code == 2

    def test_catalog_empty_and_no_matches(self, tmp_path, monkeypatch, capsys):
        self._catalog_config(tmp_path, monkeypatch, "")
        main(["catalog", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["sources"] == payload["collections"] == []
        assert payload["total"] == 0
        main(["catalog", "missing"])
        assert capsys.readouterr().out == "No matching library entries.\n"
        assert not (tmp_path / "indexes").exists()

    def test_build_list_does_not_take_the_build_lock(self, tmp_path, capsys):
        (tmp_path / "modules").mkdir()
        (tmp_path / "modules" / "a.html").write_text("hello", encoding="utf-8")
        config_path = default_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        _write(
            config_path,
            f"index_dir = {json.dumps(str(tmp_path / 'indexes'))}\n"
            '[[collections]]\ntype = "files"\nname = "modules"\n'
            f"path = {json.dumps(str(tmp_path / 'modules'))}\n",
        )
        main(["build", "--list"])
        assert not (tmp_path / ".build.lock").exists()
        assert not (tmp_path / "indexes").exists()
        assert "files:modules" in capsys.readouterr().out

    @pytest.mark.skipif(os.name == "nt", reason="XDG paths are Unix-only")
    def test_prune_yes_reloads_default_config_without_changing_index_dir(
        self, tmp_path, monkeypatch
    ):
        config_home = tmp_path / "config"
        data_home = tmp_path / "data"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
        config_path = default_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        _write(config_path, "")

        source = _source()
        (data_home / "zimantic").mkdir(parents=True, exist_ok=True)
        locator = _publish(data_home / "zimantic", source)
        source_directory = locator.source_directory(
            source.provider, source.collection_key, source.key
        )
        assert source_directory.exists()

        main(["prune", "--yes"])

        assert not source_directory.exists()

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
        search = Search(cfg["runtime_config"], semantic=False)
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
