"""Compatibility of libzim dependency markers and version-specific APIs."""

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


@pytest.mark.parametrize(
    ("machine", "accepts_3"),
    [
        ("armv6l", True),
        ("armv7b", True),
        ("armv7l", True),
        ("armv8l", True),
        ("i686", True),
        ("x86", True),
        ("mipsel", True),
        ("aarch64", False),
        ("arm64", False),
        ("x86_64", False),
        ("AMD64", False),
        ("ppc64le", False),
    ],
)
def test_libzim_dependency_markers(machine, accepts_3):
    with (Path(__file__).parents[1] / "pyproject.toml").open("rb") as file:
        dependencies = tomllib.load(file)["project"]["dependencies"]
    requirements = [Requirement(dep) for dep in dependencies]
    applicable = [
        req for req in requirements
        if req.name == "libzim"
        and (req.marker is None or req.marker.evaluate({"platform_machine": machine}))
    ]
    assert applicable
    assert all(Version("4.1.0") in req.specifier for req in applicable)
    for version in ("3.0.0", "3.6.0", "4.0.0"):
        assert all(Version(version) in req.specifier for req in applicable) == accepts_3
    assert not all(Version("2.0.0") in req.specifier for req in applicable)


def test_versioned_entry_lookup_and_search_results():
    old_archive = SimpleNamespace(_get_entry_by_id=lambda entry_id: ("old", entry_id))
    new_archive = SimpleNamespace(get_entry_by_id=lambda entry_id: ("new", entry_id))
    assert get_entry_by_id(old_archive, 3) == ("old", 3)
    assert get_entry_by_id(new_archive, 3) == ("new", 3)

    old_search = SimpleNamespace(getResults=lambda start, count: ["alpha"][start:start + count])
    new_search = SimpleNamespace(
        get_results=lambda start, count: [
            SimpleNamespace(path="alpha", title="Alpha")
        ][start:start + count]
    )
    titles = SimpleNamespace(get_entry_by_path=lambda path: SimpleNamespace(title=path.title()))
    assert search_results(old_search, 0, 1) == [{"path": "alpha", "title": ""}]
    assert search_results(old_search, 0, 1, archive=titles) == [
        {"path": "alpha", "title": "Alpha"}
    ]
    assert search_results(new_search, 0, 1) == [{"path": "alpha", "title": "Alpha"}]


@pytest.mark.parametrize(
    ("search_class", "supported"),
    [
        (SimpleNamespace(getResults=lambda start, count: []), False),
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
