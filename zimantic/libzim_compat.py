"""Adapters for the libzim 3 and 4 entry and search APIs."""

from typing import Any


def get_entry_by_id(archive: Any, entry_id: int) -> Any:
    getter = getattr(archive, "get_entry_by_id", None)
    if getter is None:
        getter = archive._get_entry_by_id
    return getter(entry_id)


def search_results(
    search: Any, start: int, count: int, *, archive: Any = None
) -> list[dict[str, str]]:
    getter = getattr(search, "get_results", None)
    if getter is None:
        getter = search.getResults
    results = []
    for result in getter(start, count):
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
