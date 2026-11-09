"""Adapters for the libzim 3 and 4 entry and search APIs."""

import inspect
from functools import cache
from typing import Any


@cache
def supports_threaded_search() -> bool:
    """Identify development builds with the combined reader search changes."""
    from libzim import search

    # The snippet keyword and GIL release land together; switch to a version
    # cutoff once the first release containing both is known.
    try:
        return "include_snippets" in inspect.signature(search.Search.get_results).parameters
    except (AttributeError, TypeError, ValueError):
        return False


def get_entry_by_id(archive: Any, entry_id: int) -> Any:
    getter = getattr(archive, "get_entry_by_id", None)
    if getter is None:
        getter = archive._get_entry_by_id
    return getter(entry_id)


def search_results(
    search: Any, start: int, count: int, *, archive: Any = None,
    include_snippets: bool = True,
) -> list[dict[str, str]]:
    getter = getattr(search, "get_results", None)
    if getter is None:
        getter = search.getResults
    native_results = (
        getter(start, count, include_snippets=False)
        if not include_snippets and supports_threaded_search()
        else getter(start, count)
    )
    results = []
    for result in native_results:
        if isinstance(result, str):
            path, title = result, ""
        else:
            path = getattr(result, "path", result)
            title = getattr(result, "title", "")
        if not title and path and archive is not None:
            try:
                title = archive.get_entry_by_path(path).title
            except (KeyError, RuntimeError, ValueError):
                pass
        results.append({"path": str(path), "title": str(title or "")})
    return results
