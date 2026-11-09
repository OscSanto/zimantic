"""Helper process used by :mod:`zimantic.fulltext`.

Protocol: one JSON request per line on stdin, one JSON response per line on
stdout. A request is ``{"path": ..., "query": ..., "count": ...}``; a response
is ``{"results": [{"path": ..., "title": ...}, ...]}`` or ``{"error": "..."}``.
The parent treats a missing response (the process aborted) as an unsafe archive.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import OrderedDict

from libzim.reader import Archive
from libzim.search import Query, Searcher

from .libzim_compat import search_results

# Bound the worker's archive cache. The parent process already keeps its own
# handles for previews and article reads; this cache only needs to cover the
# archives touched by recent searches.
MAX_CACHED_ARCHIVES = 4


class ArchiveCache:
    """Bounded native handles owned by one sequential search worker."""

    def __init__(self) -> None:
        self.archives: OrderedDict[object, tuple[Archive, Searcher]] = OrderedDict()

    def search_results(
        self, path: str, query: str, count: int, version: object = None,
    ) -> list[dict[str, str]]:
        if isinstance(version, list):
            version = tuple(version)
        cache_key = (path, version)
        cached = self.archives.get(cache_key)
        if cached is None:
            archive = Archive(path)
            cached = (archive, Searcher(archive))
            self.archives[cache_key] = cached
            while len(self.archives) > MAX_CACHED_ARCHIVES:
                self.archives.popitem(last=False)
        self.archives.move_to_end(cache_key)
        archive, searcher = cached
        if not getattr(archive, "has_fulltext_index", False) or not query:
            return []
        search = searcher.search(Query().set_query(query))
        return search_results(
            search, 0, max(0, int(count)), archive=archive,
            include_snippets=False,
        )

    def clear(self) -> None:
        self.archives.clear()


def main() -> int:
    cache = ArchiveCache()
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
            results = cache.search_results(path, query, count, version)
        except Exception as error:  # report ordinary libzim errors without dying
            logging.getLogger(__name__).debug("native full-text failure", exc_info=True)
            print(
                json.dumps({"error": f"{type(error).__name__}: {error}"}),
                flush=True,
            )
            continue
        print(json.dumps({"results": results}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
