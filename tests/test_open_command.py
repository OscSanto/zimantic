"""CLI, scope, ordering, and toolbar tests for ``zimantic open``."""

from __future__ import annotations

import hashlib
import io
import os
import types
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from libzim.reader import Archive
from libzim.writer import Creator, Hint, Item, StringProvider

from zimantic import __main__ as main_module
from zimantic.catalog import SourceCatalog, catalog_path
from zimantic.open_scope import (
    OPEN_COLLECTION_KEY,
    OpenScopeError,
    build_open_scope,
    expand_open_paths,
)
from zimantic.providers.config import RuntimeConfig, ZimConfig
from zimantic.providers.zim.discovery import scan_collection, zim_source_key
from zimantic.search import Search
from zimantic.web.app import create_app


class _Page(Item):
    def __init__(self, path: str, title: str, html: str):
        super().__init__()
        self._path = path
        self._title = title
        self._html = html.encode("utf-8")

    def get_path(self):
        return self._path

    def get_title(self):
        return self._title

    def get_mimetype(self):
        return "text/html"

    def get_contentprovider(self):
        return StringProvider(self._html)

    def get_hints(self):
        return {Hint.FRONT_ARTICLE: True}


def _create_zim(path: Path, *, title: str, indexing: bool = True) -> Path:
    creator = Creator(path)
    creator.config_indexing(indexing, "eng")
    creator.set_mainpath("Alpha")
    with creator:
        creator.add_metadata("Title", title)
        creator.add_item(
            _Page(
                "Alpha",
                title,
                f"<html><body><p>{title} is a sufficiently long article about "
                "tires and wheels for full-text indexing tests.</p></body></html>",
            )
        )
    return path


def _config(root: Path) -> dict:
    cfg = {
        "index_dir": str(root / "indexes"),
        "model_dir": str(root / "model"),
        "collections": (),
        "page_size": 10,
    }
    cfg["runtime_config"] = RuntimeConfig(settings=cfg)
    return cfg


def _scan(root: Path, name: str, *, archive_factory=Archive) -> SourceCatalog:
    catalog = SourceCatalog(catalog_path(root / "indexes"))
    scan_collection(
        catalog,
        ZimConfig(name=name, path=str(root / "zims")),
        collection_key=f"zim:{name}",
        archive_factory=archive_factory,
    )
    return catalog


class TestOpenParser:
    def test_requires_at_least_one_path(self):
        parser = main_module._make_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["open"])

    def test_options_and_defaults(self):
        parser = main_module._make_parser()
        args = parser.parse_args(
            [
                "open",
                "one.zim",
                "two.zim",
                "--fast",
                "--debug",
                "--no-browser",
                "--port",
                "9000",
                "--device",
                " CUDA ",
                "--max-sources",
                "7",
            ]
        )
        assert args.paths == [Path("one.zim"), Path("two.zim")]
        assert args.fast and args.debug and args.no_browser
        assert args.port == 9000
        assert args.device == "cuda"
        assert args.max_sources == 7

        defaults = parser.parse_args(["open", "one.zim"])
        assert defaults.port == 0
        assert defaults.max_sources == 64
        assert defaults.fast is False
        assert defaults.debug is False
        assert defaults.no_browser is False


class TestOpenExpansion:
    def test_file_order_folders_recursion_and_duplicate_resolution(self, tmp_path):
        files = tmp_path / "files"
        files.mkdir()
        first = files / "b.zim"
        second = files / "a.zim"
        first.write_bytes(b"x")
        second.write_bytes(b"x")
        nested = files / "nested"
        nested.mkdir()
        third = nested / "c.zim"
        third.write_bytes(b"x")
        (files / "notes.txt").write_text("ignored", encoding="utf-8")

        expanded = expand_open_paths([first, files, first])
        assert expanded == [
            first.resolve(),
            second.resolve(),
            third.resolve(),
        ]

    def test_distinct_same_stem_files_are_kept(self, tmp_path):
        one = tmp_path / "one" / "same.zim"
        two = tmp_path / "two" / "same.zim"
        one.parent.mkdir()
        two.parent.mkdir()
        one.write_bytes(b"one")
        two.write_bytes(b"two")

        assert expand_open_paths([one, two]) == [one.resolve(), two.resolve()]

    def test_missing_and_non_zim_paths_fail_before_startup(self, tmp_path):
        with pytest.raises(OpenScopeError, match="path not found"):
            expand_open_paths([tmp_path / "missing.zim"])
        notes = tmp_path / "notes.txt"
        notes.write_text("x", encoding="utf-8")
        with pytest.raises(OpenScopeError, match="not a ZIM file"):
            expand_open_paths([notes])

    def test_source_cap_suggests_serve(self, tmp_path):
        for position in range(3):
            (tmp_path / f"{position}.zim").write_bytes(b"x")
        with pytest.raises(OpenScopeError, match="zimantic serve"):
            expand_open_paths([tmp_path], max_sources=2)

    def test_visited_entry_budget_counts_non_zim_files(self, tmp_path):
        for position in range(3):
            (tmp_path / f"{position}.txt").write_text("x", encoding="utf-8")
        with pytest.raises(OpenScopeError, match="entry limit"):
            expand_open_paths([tmp_path], max_visited_entries=2)

    def test_expansion_deadline_fails_explicitly(self, tmp_path, monkeypatch):
        (tmp_path / "a.zim").write_bytes(b"x")
        (tmp_path / "b.zim").write_bytes(b"x")
        ticks = iter([0.0, 0.0, 100.0, 100.0])
        monkeypatch.setattr(
            "zimantic.open_scope.time.monotonic",
            lambda: next(ticks, 100.0),
        )
        with pytest.raises(OpenScopeError, match="second limit"):
            expand_open_paths([tmp_path], timeout_seconds=1)


class TestOpenScopeIdentity:
    def test_unique_catalog_row_keeps_the_canonical_key(self, tmp_path):
        zims = tmp_path / "zims"
        zims.mkdir()
        archive = _create_zim(zims / "manual.zim", title="Manual")
        catalog = _scan(tmp_path, "library")
        catalog.close()

        scope = build_open_scope([archive], _config(tmp_path))
        assert scope.keys == (zim_source_key("library", "manual.zim"),)

    def test_ambiguous_rows_use_a_fallback_key(self, tmp_path):
        zims = tmp_path / "zims"
        zims.mkdir()
        archive = _create_zim(zims / "manual.zim", title="Manual")
        first = _scan(tmp_path, "one")
        second = _scan(tmp_path, "two")
        first.close()
        second.close()

        keys = build_open_scope([archive], _config(tmp_path)).keys
        assert keys == (f"{OPEN_COLLECTION_KEY}:manual-1",)

    def test_missing_or_mismatched_identity_uses_a_fallback_key(self, tmp_path):
        zims = tmp_path / "zims"
        zims.mkdir()
        archive = _create_zim(zims / "manual.zim", title="Manual")
        catalog = _scan(tmp_path, "library")
        catalog.db.execute("UPDATE sources SET provider_id = ''")
        catalog.db.commit()
        catalog.close()
        assert build_open_scope([archive], _config(tmp_path)).keys == (
            f"{OPEN_COLLECTION_KEY}:manual-1",
        )

        catalog = _scan(tmp_path, "library")
        catalog.db.execute("UPDATE sources SET size = size + 1")
        catalog.db.commit()
        catalog.close()
        assert build_open_scope([archive], _config(tmp_path)).keys == (
            f"{OPEN_COLLECTION_KEY}:manual-1",
        )

    def test_lookup_never_changes_the_catalog_or_creates_sidecars(self, tmp_path):
        zims = tmp_path / "zims"
        zims.mkdir()
        archive = _create_zim(zims / "manual.zim", title="Manual")
        catalog = _scan(tmp_path, "library")
        catalog.close()
        database = catalog_path(tmp_path / "indexes")
        lease = database.parent / ".catalog.lock"
        before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in database.parent.iterdir()
            if path.is_file()
        }
        lease_before = lease.stat().st_mtime_ns
        scope = build_open_scope([archive], _config(tmp_path))
        lease_after = lease.stat().st_mtime_ns
        after = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in database.parent.iterdir()
            if path.is_file()
        }
        assert scope.keys == (zim_source_key("library", "manual.zim"),)
        assert before == after
        assert lease_before == lease_after
        assert not Path(f"{database}-wal").exists()
        assert not Path(f"{database}-shm").exists()

    @pytest.mark.skipif(
        os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
        reason="requires a non-root POSIX read-only lease",
    )
    def test_read_only_lease_still_keeps_the_canonical_key(self, tmp_path):
        zims = tmp_path / "zims"
        zims.mkdir()
        archive = _create_zim(zims / "manual.zim", title="Manual")
        catalog = _scan(tmp_path, "library")
        catalog.close()
        lease = catalog_path(tmp_path / "indexes").parent / ".catalog.lock"
        lease.chmod(0o444)
        try:
            scope = build_open_scope([archive], _config(tmp_path))
        finally:
            lease.chmod(0o644)

        assert scope.keys == (zim_source_key("library", "manual.zim"),)

    def test_absent_lease_uses_fallback_keys(self, tmp_path):
        zims = tmp_path / "zims"
        zims.mkdir()
        archive = _create_zim(zims / "manual.zim", title="Manual")
        catalog = _scan(tmp_path, "library")
        catalog.close()
        catalog_path(tmp_path / "indexes").parent.joinpath(
            ".catalog.lock"
        ).unlink()
        assert build_open_scope([archive], _config(tmp_path)).keys == (
            f"{OPEN_COLLECTION_KEY}:manual-1",
        )


class TestScopedRuntime:
    def _search(self, tmp_path, archives, *, semantic=False):
        paths = expand_open_paths(archives)
        cfg = _config(tmp_path)
        scope = build_open_scope(paths, cfg)
        return Search(cfg["runtime_config"], semantic=semantic, scope=scope), scope

    def test_scope_exposes_only_selected_archives_in_cli_order(self, tmp_path):
        zims = tmp_path / "zims"
        zims.mkdir()
        # The titles sort opposite to the command-line order.
        z_first = _create_zim(zims / "z.zim", title="Alpha")
        a_second = _create_zim(zims / "a.zim", title="Zeta")
        unrelated = _create_zim(zims / "other.zim", title="Other")

        search, scope = self._search(tmp_path, [z_first, a_second])
        try:
            assert [record["key"] for record in search.source_dicts()] == list(
                scope.keys
            )
            assert [record["key"] for record in search.library_dicts()] == list(
                scope.keys
            )
            assert [record["key"] for record in search.collection_dicts()] == [
                "zim:open"
            ]
            assert unrelated.resolve() not in scope.paths

            client = TestClient(create_app(search))
            assert [item["key"] for item in client.get("/sources").json()] == list(
                scope.keys
            )
            assert [
                item["key"] for item in client.get("/library.json").json()
            ] == list(scope.keys)
            assert [
                item["key"] for item in client.get("/collections.json").json()
            ] == ["zim:open"]
            # Filters and paging preserve the same order.
            assert [
                item["key"]
                for item in client.get("/sources", params={"limit": 1}).json()
            ] == [scope.keys[0]]
            pages = client.get(
                "/sources", params={"limit": 1, "offset": 1}
            ).json()
            assert [item["key"] for item in pages] == [scope.keys[1]]
        finally:
            search.close()

    def test_source_neighbors_wrap_and_include_unsearchable_sources(
        self, tmp_path
    ):
        zims = tmp_path / "zims"
        zims.mkdir()
        first = _create_zim(zims / "first.zim", title="First")
        second = _create_zim(zims / "second.zim", title="Second", indexing=False)
        third = _create_zim(zims / "third.zim", title="Third")
        search, scope = self._search(tmp_path, [first, second, third])
        try:
            previous, following = search.source_neighbors(scope.keys[0])
            assert previous.key == scope.keys[2]
            assert following.key == scope.keys[1]
            assert following.title == "Second"
            previous, following = search.source_neighbors(scope.keys[2])
            assert previous.key == scope.keys[1]
            assert following.key == scope.keys[0]
            assert search.source_neighbors("unknown") is None
        finally:
            search.close()

    def test_single_source_has_no_switcher(self, tmp_path):
        zims = tmp_path / "zims"
        zims.mkdir()
        archive = _create_zim(zims / "only.zim", title="Only")
        search, scope = self._search(tmp_path, [archive])
        try:
            client = TestClient(create_app(search))
            response = client.get(f"/content/{scope.keys[0]}/")
            assert response.status_code == 200
            assert "data-zimantic-source-previous" not in response.text
            assert "data-zimantic-source-next" not in response.text
        finally:
            search.close()

    def test_toolbar_switcher_names_targets_and_uses_homepages(self, tmp_path):
        zims = tmp_path / "zims"
        zims.mkdir()
        first = _create_zim(zims / "first.zim", title="First")
        second = _create_zim(zims / "second.zim", title="Second")
        search, scope = self._search(tmp_path, [first, second])
        try:
            client = TestClient(create_app(search))
            response = client.get(f"/content/{scope.keys[0]}/")
            assert response.status_code == 200
            assert 'aria-label="Selected ZIMs"' in response.text
            assert "data-zimantic-source-previous" in response.text
            assert "data-zimantic-source-next" in response.text
            assert 'title="Previous ZIM: Second"' in response.text
            assert 'title="Next ZIM: Second"' in response.text
            expected = f"/content/{quote(scope.keys[1], safe='')}/Alpha"
            assert f'data-zimantic-url="{expected}"' in response.text
            # Search-result arrows remain their own control family.
            assert "data-zimantic-context-previous" in response.text
            assert "data-zimantic-parent-previous" in response.text
        finally:
            search.close()

    def test_error_shell_keeps_the_switcher(self, tmp_path):
        zims = tmp_path / "zims"
        zims.mkdir()
        first = _create_zim(zims / "first.zim", title="First")
        second = _create_zim(zims / "second.zim", title="Second")
        search, scope = self._search(tmp_path, [first, second])
        try:
            client = TestClient(create_app(search))
            response = client.get(f"/content/{scope.keys[0]}/missing")
            assert response.status_code == 404
            assert "data-zimantic-source-previous" in response.text
            assert "data-zimantic-source-next" in response.text
        finally:
            search.close()

    def test_normal_search_has_no_switcher(self, tmp_path):
        zims = tmp_path / "zims"
        zims.mkdir()
        _create_zim(zims / "manual.zim", title="Manual")
        cfg = _config(tmp_path)
        search = Search(cfg["runtime_config"], semantic=False)
        try:
            assert search.open_scope is None
            assert search.source_neighbors("anything") is None
        finally:
            search.close()


class TestOpenCommand:
    def _args(self, tmp_path, **overrides):
        defaults = {
            "paths": [tmp_path / "one.zim"],
            "port": 0,
            "no_browser": False,
            "fast": True,
            "debug": False,
            "device": None,
            "max_sources": 64,
        }
        defaults.update(overrides)
        return types.SimpleNamespace(**defaults)

    def test_no_browser_skips_webbrowser_and_uses_loopback_without_pid(
        self, tmp_path
    ):
        archive = tmp_path / "one.zim"
        archive.write_bytes(b"not read by the fake search")
        captured = {}

        class FakeSearch:
            def __init__(self, cfg, semantic=True, scope=None):
                captured["semantic"] = semantic
                captured["scope"] = scope

            def close(self):
                captured["closed"] = True

        class FakeSocket:
            def getsockname(self):
                return ("127.0.0.1", 43210)

        def fake_run_server(search, **kwargs):
            captured["run"] = kwargs
            kwargs["on_listening"](43210)

        output = io.StringIO()
        with (
            patch("zimantic.search.Search", FakeSearch),
            patch("zimantic.web.app.bind_local_socket", return_value=FakeSocket()),
            patch("zimantic.web.app.run_server", side_effect=fake_run_server),
            patch("zimantic.__main__.webbrowser.open") as browser,
            redirect_stdout(output),
        ):
            main_module._run_open(
                self._args(tmp_path, no_browser=True),
                _config(tmp_path),
                main_module._make_parser(),
            )

        assert browser.call_count == 0
        assert captured["semantic"] is False
        assert captured["closed"] is True
        run = captured["run"]
        assert run["host"] == "127.0.0.1"
        assert run["pid_path"] is None
        assert run["install_sighup"] is False
        assert run["prebound_socket"].getsockname()[1] == 43210
        assert "opening 1 ZIM file" in output.getvalue()
        assert "http://127.0.0.1:43210/content/" in output.getvalue()

    def test_browser_receives_the_first_content_url_once(self, tmp_path):
        archive = tmp_path / "one.zim"
        archive.write_bytes(b"not read by the fake search")
        captured = {}

        class FakeSearch:
            def __init__(self, cfg, semantic=True, scope=None):
                captured["scope"] = scope

            def close(self):
                pass

        class FakeSocket:
            def getsockname(self):
                return ("127.0.0.1", 43211)

        def fake_run_server(search, **kwargs):
            kwargs["on_listening"](43211)

        class ImmediateThread:
            def __init__(self, target, args=(), **_kwargs):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        with (
            patch("zimantic.search.Search", FakeSearch),
            patch("zimantic.web.app.bind_local_socket", return_value=FakeSocket()),
            patch("zimantic.web.app.run_server", side_effect=fake_run_server),
            patch("zimantic.__main__.threading.Thread", ImmediateThread),
            patch("zimantic.__main__._launch_browser") as launcher,
        ):
            main_module._run_open(
                self._args(tmp_path),
                _config(tmp_path),
                main_module._make_parser(),
            )
        # The browser thread starts once, after the listening callback.
        launcher.assert_called_once()
        url = launcher.call_args.args[0]
        assert url == (
            "http://127.0.0.1:43211/content/"
            + captured["scope"].keys[0].replace(":", "%3A")
            + "/"
        )

    def test_browser_failure_is_logged_and_not_raised(self):
        with patch(
            "zimantic.__main__.webbrowser.open", side_effect=RuntimeError("no display")
        ):
            main_module._launch_browser("http://127.0.0.1:1/content/x/")

    def test_usage_errors_exit_with_status_two(self, tmp_path):
        parser = main_module._make_parser()
        with pytest.raises(SystemExit) as raised:
            main_module._run_open(
                self._args(tmp_path, max_sources=0),
                _config(tmp_path),
                parser,
            )
        assert raised.value.code == 2
