"""Helper process used by :mod:`zimantic.fulltext`.

Protocol: one JSON request per line on stdin, one JSON response per line on
stdout. A request is ``{"path": ..., "query": ..., "count": ...}``; a response
is ``{"results": [{"path": ..., "title": ...}, ...]}`` or ``{"error": "..."}``.
The parent treats a missing response (the process aborted) as an unsafe archive.
"""

from __future__ import annotations

import json
import sys
from collections import OrderedDict

from libzim.reader import Archive
from libzim.search import Query, Searcher

# Bound the worker's archive cache. The parent process already keeps its own
# handles for previews and article reads; this cache only needs to cover the
# archives touched by recent searches.
MAX_CACHED_ARCHIVES = 4


def main() -> int:
    archives: OrderedDict[object, tuple[Archive, Searcher]] = OrderedDict()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            path = str(request["path"])
            query = str(request["query"])
            count = max(0, int(request.get("count", 0)))
            version = request.get("version")
            if isinstance(version, list):
                version = tuple(version)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            print(json.dumps({"error": f"bad request: {error}"}), flush=True)
            continue

        try:
            cache_key = (path, version)
            cached = archives.get(cache_key)
            if cached is None:
                archive = Archive(path)
                searcher = Searcher(archive)
                cached = (archive, searcher)
                archives[cache_key] = cached
                while len(archives) > MAX_CACHED_ARCHIVES:
                    archives.popitem(last=False)
            archive, searcher = cached
            if not getattr(archive, "has_fulltext_index", False) or not query:
                results: list[dict[str, str]] = []
            else:
                # Ask for only the caller's window. libzim can return fewer
                # results than the estimate; the iterator is the source of truth.
                search = searcher.search(Query().set_query(query))
                results = [
                    {"path": str(result.path), "title": str(result.title)}
                    for result in search.get_results(0, count)
                ]
        except Exception as error:  # report ordinary libzim errors without dying
            print(
                json.dumps({"error": f"{type(error).__name__}: {error}"}),
                flush=True,
            )
            continue
        print(json.dumps({"results": results}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
