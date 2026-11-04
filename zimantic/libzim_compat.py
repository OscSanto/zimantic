"""Small adapters for libzim APIs that changed between major versions.

libzim 4 made entry lookup public (``Archive.get_entry_by_id``) and renamed
search accessors to snake case (``Search.get_results``). Search results are now
``SearchResult`` objects instead of bare paths. Older distro packages still ship
libzim 3, so keep both surfaces working through these helpers.
"""

from typing import Any


def get_entry_by_id(archive: Any, entry_id: int) -> Any:
    """Return one entry by numeric id on libzim 3 and 4."""
    getter = getattr(archive, "get_entry_by_id", None)
    if getter is None:
        getter = archive._get_entry_by_id
    return getter(entry_id)


def search_paths(search: Any, start: int, count: int) -> list[str]:
    """Return result paths from a libzim ``Search`` object."""
    return [result["path"] for result in search_results(search, start, count)]


def search_results(
    search: Any, start: int, count: int
) -> list[dict[str, str]]:
    """Return result paths and titles from a libzim ``Search`` object."""
    getter = getattr(search, "get_results", None)
    if getter is None:
        getter = search.getResults
    results = []
    for result in getter(start, count):
        path = getattr(result, "path", result)
        title = getattr(result, "title", "")
        results.append({
            "path": str(path),
            "title": str(title or ""),
        })
    return results
