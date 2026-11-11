import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def benchmark(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "benchmarks"))
    module = importlib.import_module("run_snippets")
    calls = []

    class Search:
        def get_results(self, start, count, *, include_snippets=True):
            calls.append((start, count, include_snippets))
            return [
                SimpleNamespace(
                    path=f"p{i}", title=f"Title {i}",
                    snippet="<b>needle</b> &amp; context" if include_snippets else "",
                )
                for i in range(start, start + count)
            ]

    monkeypatch.setattr(module, "Archive", lambda path: SimpleNamespace(has_fulltext_index=True))
    monkeypatch.setattr(module, "Searcher", lambda archive: SimpleNamespace(search=lambda query: Search()))
    return module, calls


@pytest.mark.parametrize("budget,count,expected", [
    (2, 4, [(0, 2, True), (2, 2, False)]),
    (8, 4, [(0, 4, True)]),
    (2, 0, [(0, 0, False)]),
])
def test_native_prefetch_preserves_remaining_candidates(benchmark, budget, count, expected):
    module, calls = benchmark
    cache = module.SnippetCache({"strategy": f"native:{budget}", "preview_chars": 1000})

    rows = cache.search_results("archive", "needle", count)

    assert calls == expected
    assert [row["path"] for row in rows] == [f"p{i}" for i in range(count)]
    assert [row["lead"] for row in rows] == [
        "needle & context" if i < budget else "" for i in range(count)
    ]
    assert [row["prefetched"] for row in rows] == [i < budget for i in range(count)]


def test_eager_leads_use_only_the_prefetch_budget(benchmark, monkeypatch):
    module, calls = benchmark
    reads = []

    def lead(index, path, chars):
        reads.append((path, chars))
        return "lead " + path

    monkeypatch.setattr(module, "lead", lead)
    cache = module.SnippetCache({"strategy": "lead:2", "preview_chars": 500})

    rows = cache.search_results("archive", "needle", 4)

    assert calls == [(0, 4, False)]
    assert reads == [("p0", 500), ("p1", 500)]
    assert [row["lead"] for row in rows] == ["lead p0", "lead p1", "", ""]


def test_empty_query_does_not_generate_snippets(benchmark):
    module, calls = benchmark
    cache = module.SnippetCache({"strategy": "native:4", "preview_chars": 1000})

    assert cache.search_results("archive", "", 16) == []
    assert calls == []
