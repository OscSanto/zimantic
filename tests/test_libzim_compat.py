"""Contract checks for the libzim entry and search adapters."""

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from zimantic.libzim_compat import (
    get_entry_by_id,
    search_results,
    supports_threaded_search,
)


def test_libzim_dependency_requires_modern_api():
    with (Path(__file__).parents[1] / "pyproject.toml").open("rb") as file:
        dependencies = tomllib.load(file)["project"]["dependencies"]
    applicable = [
        Requirement(dep)
        for dep in dependencies
        if dep.startswith("libzim")
    ]
    assert len(applicable) == 1
    requirement = applicable[0]
    assert requirement.marker is None
    assert Version("4.1.0") in requirement.specifier
    assert Version("3.6.0") not in requirement.specifier


def test_entry_lookup_uses_the_public_api():
    archive = SimpleNamespace(get_entry_by_id=lambda entry_id: ("entry", entry_id))
    assert get_entry_by_id(archive, 3) == ("entry", 3)


def test_search_results_normalizes_native_results():
    search = SimpleNamespace(
        get_results=lambda start, count: [
            SimpleNamespace(path="alpha", title="Alpha")
        ][start:start + count]
    )
    titles = SimpleNamespace(get_entry_by_path=lambda path: SimpleNamespace(title=path.title()))
    indexed = SimpleNamespace(
        get_entry_by_path=lambda path: SimpleNamespace(title=path.title(), _index=7)
    )
    assert search_results(search, 0, 1) == [{"path": "alpha", "title": "Alpha"}]
    assert search_results(search, 0, 1, archive=titles) == [
        {"path": "alpha", "title": "Alpha"}
    ]
    assert search_results(
        search, 0, 1, archive=indexed, include_index=True
    ) == [{"path": "alpha", "title": "Alpha", "index": 7}]


@pytest.mark.parametrize(
    ("search_class", "supported"),
    [
        (SimpleNamespace(get_results=lambda start, count: []), False),
        (SimpleNamespace(
            get_results=lambda start, count, *, include_snippets=True: [],
        ), True),
    ],
)
def test_development_thread_capability_checks_search_api(monkeypatch, search_class, supported):
    from libzim import search

    monkeypatch.setattr(search, "Search", search_class)
    supports_threaded_search.cache_clear()
    try:
        assert supports_threaded_search() is supported
    finally:
        supports_threaded_search.cache_clear()


@pytest.mark.parametrize("gil_released", [False, True])
def test_snippet_opt_out_is_only_passed_to_capable_bindings(monkeypatch, gil_released):
    from unittest.mock import Mock

    monkeypatch.setattr(
        "zimantic.libzim_compat.supports_threaded_search", lambda: gil_released,
    )
    getter = Mock(return_value=[SimpleNamespace(path="alpha", title="Alpha")])
    assert search_results(
        SimpleNamespace(get_results=getter), 0, 1, include_snippets=False,
    ) == [{"path": "alpha", "title": "Alpha"}]
    if gil_released:
        getter.assert_called_once_with(0, 1, include_snippets=False)
    else:
        getter.assert_called_once_with(0, 1)
