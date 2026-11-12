"""Adapters for the released libzim 4 entry and search APIs."""

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
    return archive.get_entry_by_id(entry_id)


def search_results(
    search: Any, start: int, count: int, *, archive: Any = None,
    include_snippets: bool = True, include_index: bool = False,
) -> list[dict[str, Any]]:
    getter = search.get_results
    native_results = (
        getter(start, count, include_snippets=False)
        if not include_snippets and supports_threaded_search()
        else getter(start, count)
    )
    results = []
    for result in native_results:
        path = result.path
        title = result.title
        entry = None
        if path and archive is not None and (not title or include_index):
            try:
                entry = archive.get_entry_by_path(path)
            except (KeyError, RuntimeError, ValueError):
                entry = None
            if not title and entry is not None:
                try:
                    title = entry.title
                except (AttributeError, KeyError, RuntimeError, ValueError):
                    pass
        item: dict[str, Any] = {"path": str(path), "title": str(title or "")}
        if include_index and entry is not None:
            # The entry id lets the server map a native hit back to its
            # immutable index row without scanning the unindexed path column.
            index = getattr(entry, "_index", None)
            if index is None:
                index = getattr(entry, "index", None)
            if index is not None:
                try:
                    item["index"] = int(index)
                except (TypeError, ValueError):
                    pass
        results.append(item)
    return results
