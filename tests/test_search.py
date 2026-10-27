import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import numpy as np


from zimantic.cache import QueryCache
from zimantic.contracts import SourceInfo, SourceResult
from zimantic.search import (
    ArticleUnavailableError,
    Search,
    SearchBusyError,
    _bounded_catalog_url,
    _find_archive_icon_path,
    _fulltext_query,
    _LocalIndex,
    _nprobe,
    _parse_remote_xml,
    _phrase_in_word_stream,
    _phrase_words,
    _query_terms,
    _read_limited_response,
    _source_icon_url,
    _source_info_from_metadata,
    _title_prefix_query,
    _title_query,
)
from zimantic.searching.config import _file_signature
from zimantic.searching.sources import _book_identity, _html_attribute


def _search_results(search, *args, **kwargs):
    """Return one page of results from the public ``search_page`` API."""
    return search.search_page(*args, **kwargs)["results"]


def _bare_search(cfg):
    """A Search without __init__ (no model, no filesystem index scan)."""
    search = object.__new__(Search)
    search.cfg = cfg
    search.embedder = None
    search.semantic = True
    search.indexes = {}
    search.sources = {}
    search._state_lock = threading.RLock()
    search._reload_lock = threading.Lock()
    search.source_timeout = 12
    search.candidate_count = cfg.get("candidate_count", 16)
    search.page_size = cfg.get("page_size", 10)
    search.max_results = cfg.get("max_results", 100)
    search.source_workers = 2
    search.max_concurrent_searches = 2
    search._search_slots = threading.BoundedSemaphore(2)
    search._executor = ThreadPoolExecutor(max_workers=2)
    search._inflight = {}
    search._inflight_lock = threading.Lock()
    search.cache = QueryCache(16)
    return search


@contextmanager
def _temp_dir(test_case):
    """Yield a temporary directory and register its cleanup early.

    unittest runs cleanups in LIFO order, so a Search created later is closed
    before Windows tries to delete the files it still has open.
    """
    directory = tempfile.TemporaryDirectory()
    test_case.addCleanup(directory.cleanup)
    yield Path(directory.name)


class _IconItem:
    def __init__(self, mimetype, content):
        self.mimetype = mimetype
        self.content = content
        self.size = len(content)


class _IconEntry:
    def __init__(self, path, item):
        self.path = path
        self._item = item

    def get_item(self):
        return self._item


class _IconArchive:
    def __init__(self, entries, main_path):
        self._entries = entries
        self.main_entry = entries[main_path]

    def get_entry_by_path(self, path):
        if path not in self._entries:
            raise KeyError(path)
        return self._entries[path]


class _HealthArchive:
    """Minimal archive stand-in for ``Search.health`` diagnostics."""

    entry_count = 0


def _title_search(rows, query, limit=1):
    """Run a title-only search (no semantic or full-text) over an in-memory FTS.

    Returns the final ranked result list, exercising the local title path in
    isolation so prefix-fallback behaviour can be asserted end to end.
    """
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute(
        "CREATE VIRTUAL TABLE docs USING fts5("
        "title, path UNINDEXED, target UNINDEXED)"
    )
    db.executemany(
        "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
        rows,
    )
    source = SourceInfo("manual", "Manual", "manual", "local", local_name="manual")
    search = _bare_search({"candidate_count": 2})
    search.semantic = False
    search.sources = {"manual": source}
    search.indexes = {"manual": _LocalIndex(db, None, None, None, threading.Lock())}
    try:
        events = list(search.stream_search(query, limit=limit))
    finally:
        search.close()
    return events[-1]["results"]


class SearchContractTests(unittest.TestCase):
    def test_source_info_metadata_fallback_uses_name_and_bool_capability(self):
        source = _source_info_from_metadata(
            {
                "name": "Display Name",
                "has_fulltext_index": 1,
                "size": 10,
                "article_count": 2,
                "media_count": 3,
            },
            key="manual",
            local_name="manual",
            available=True,
            searchable=True,
            icon_url=None,
        )
        self.assertEqual(source.title, "Display Name")
        self.assertIs(source.has_fulltext_index, True)
        self.assertEqual(source.tags, ())

    def test_content_phrase_matching_keeps_stopwords_and_ignores_markup(self):
        text = (
            "<p>Ampiroxicam (INN) is a non-steroidal "
            "anti-inflammatory drug (NSAID).</p>"
        )
        query = "INN is a non-steroidal anti-inflammatory drug (NSAID"
        self.assertTrue(_phrase_in_word_stream(text.casefold(), _phrase_words(query)))

    def test_content_phrase_matching_does_not_backtrack_on_long_separators(self):
        text = "<p>tokyo " + (" " * 128) + "x</p>"
        self.assertFalse(
            _phrase_in_word_stream(text.casefold(), _phrase_words("tokyo banana"))
        )

    def test_content_phrase_matching_handles_repeated_words(self):
        # A naive rolling window drops the match when the phrase repeats a word.
        text = "<p>very very very good results</p>"
        self.assertTrue(
            _phrase_in_word_stream(text.casefold(), _phrase_words("very very good"))
        )
        self.assertFalse(
            _phrase_in_word_stream(text.casefold(), _phrase_words("very good very"))
        )

    def test_phrase_read_supplies_mimetype_and_phrase_flag(self):
        class _Item:
            mimetype = "text/html"
            content = (
                b"<p>Ampiroxicam (INN) is a non-steroidal "
                b"anti-inflammatory drug (NSAID).</p>"
            )

        class _Entry:
            def get_item(self):
                return _Item()

        class _UnrelatedItem(_Item):
            content = b"<p>Something else entirely.</p>"

        class _UnrelatedEntry:
            def get_item(self):
                return _UnrelatedItem()

        class _Archive:
            def __init__(self):
                self.calls = 0

            def get_entry_by_path(self, path):
                self.calls += 1
                return _Entry() if path == "Ampiroxicam" else _UnrelatedEntry()

        source = SourceInfo(
            "manual",
            "Manual",
            "manual",
            "local",
            local_name="manual",
        )
        unrelated = {
            "id": "manual:unrelated",
            "source_key": "manual",
            "source": "Manual",
            "title": "Nonsteroidal anti-inflammatory drug",
            "lead": "",
            "path": "unrelated",
            "source_rank": 1,
            "mimetype": "text/html",
        }
        exact = {
            "id": "manual:exact",
            "source_key": "manual",
            "source": "Manual",
            "title": "Ampiroxicam",
            "lead": "",
            "path": "Ampiroxicam",
            "source_rank": 2,
        }
        result = SourceResult(
            source=source,
            items=[unrelated, exact],
            semantic=[(0.99, unrelated)],
            fulltext=[(0, exact)],
        )
        archive = _Archive()
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16, "max_preview_chars": 1000}
        search.candidate_count = 16
        search.preview_chars = 1000
        search.sources = {"manual": source}
        search.indexes = {
            "manual": _LocalIndex(None, None, archive, None, threading.Lock())
        }
        search._state_lock = threading.RLock()
        query = "INN is a non-steroidal anti-inflammatory drug (NSAID"

        pool = search._rank_results([result], query, 10)
        pool = search._enrich_pool([result], query, pool, 10)

        self.assertEqual(pool[0]["id"], "manual:exact")
        self.assertTrue(pool[0]["ranking"]["content_phrase"])
        self.assertEqual(pool[0]["mimetype"], "text/html")
        # Previews are hydrated only for the final displayed page, not while
        # enriching the whole candidate pool.
        self.assertFalse(pool[0]["lead"])
        self.assertEqual(archive.calls, 2)

    def test_html_attribute_does_not_match_data_prefixed_names(self):
        self.assertEqual(
            _html_attribute('<link rel="icon" data-href="/x.png">', "href"), ""
        )
        self.assertEqual(
            _html_attribute('<link data-rel="icon" href="/x.png">', "rel"), ""
        )
        self.assertEqual(
            _html_attribute('<link rel="icon" href="/x.png">', "href"), "/x.png"
        )

    def test_source_icon_prefers_zim_metadata(self):
        homepage = _IconEntry(
            "Main_Page",
            _IconItem(
                "text/html",
                b'<link rel="icon" href="./assets/home.png">',
            ),
        )
        archive = _IconArchive(
            {
                "Main_Page": homepage,
                "assets/home.png": _IconEntry(
                    "assets/home.png",
                    _IconItem("image/png", b"home"),
                ),
                "-/icon": _IconEntry(
                    "-/icon",
                    _IconItem("image/png", b"zim"),
                ),
            },
            "Main_Page",
        )
        self.assertEqual(_find_archive_icon_path(archive, "Main_Page"), "-/icon")

    def test_source_icon_reads_homepage_relative_link(self):
        homepage = _IconEntry(
            "mainPage",
            _IconItem(
                "text/html",
                b'<link rel="icon" type="image/png" href="./_res_/favicon.png">',
            ),
        )
        archive = _IconArchive(
            {
                "mainPage": homepage,
                "_res_/favicon.png": _IconEntry(
                    "_res_/favicon.png",
                    _IconItem("image/png", b"favicon"),
                ),
            },
            "mainPage",
        )
        self.assertEqual(
            _find_archive_icon_path(archive, "mainPage"),
            "_res_/favicon.png",
        )

    def test_nprobe_scales_with_ivf_list_count(self):
        index = types.SimpleNamespace(nlist=10_000)

        self.assertEqual(_nprobe(index, {}), 600)

    def test_nprobe_can_be_overridden(self):
        index = types.SimpleNamespace(nlist=10_000)

        self.assertEqual(_nprobe(index, {"nprobe": 64}), 64)

    def test_nprobe_does_not_exceed_ivf_list_count(self):
        index = types.SimpleNamespace(nlist=10)

        self.assertEqual(_nprobe(index, {}), 1)
        self.assertEqual(_nprobe(index, {"nprobe": 64}), 10)

    def test_keyword_query_removes_filler_words_but_keeps_meaningful_terms(self):
        self.assertEqual(_title_query("how to change tires"), '"change" AND "tires"')
        self.assertEqual(_fulltext_query("how to change tires"), "change tires")
        self.assertEqual(_query_terms("how to change tires"), ["change", "tire"])

    def test_stopword_only_query_is_not_filtered_away(self):
        self.assertEqual(_title_query("how to"), '"how" AND "to"')
        self.assertEqual(_fulltext_query("how to"), "how to")
        self.assertEqual(_title_query("the"), '"the"')

    def test_title_prefix_query_uses_prefix_tokens_except_single_characters(self):
        self.assertEqual(_title_prefix_query("how to change tires"), "change* AND tires*")
        self.assertEqual(_title_prefix_query("praise of fol"), "praise* AND fol*")
        self.assertEqual(_title_prefix_query("c"), '"c"')

    def test_cache_key_normalizes_query_and_source_selection(self):
        self.assertEqual(
            Search._cache_key("How  TO change", ["beta", "alpha"]),
            Search._cache_key("how to change", ["alpha", "beta"]),
        )
        self.assertNotEqual(
            Search._cache_key("q", ["manual"]),
            Search._cache_key("q", ["other"]),
        )

    def test_catalog_limit_replaces_unbounded_count(self):
        url = _bounded_catalog_url(
            "http://kiwix/entries?count=-1&foo=bar",
            50_000,
        )
        self.assertEqual(url, "http://kiwix/entries?count=50000&foo=bar")

    def test_remote_xml_rejects_dtds(self):
        with self.assertRaises(ValueError):
            _parse_remote_xml(b"<!DOCTYPE root [<!ENTITY x 'y'>]><root/>")

    def test_remote_response_reader_enforces_a_hard_limit(self):
        class _Response:
            headers = {}

            def read(self, size=-1):
                return b"x" * 4

        with self.assertRaises(ValueError):
            _read_limited_response(_Response(), 3)

    def test_hybrid_ranking_is_deterministic(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        doc = {
            "id": "manual:z-tire",
            "source_key": "manual",
            "source": "Manual",
            "title": "Tire change",
            "lead": "Change a tire safely.",
            "path": "z-tire",
            "url": "http://example/z-tire",
            "source_rank": 1,
        }
        tie = {
            "id": "manual:a-tire",
            "source_key": "manual",
            "source": "Manual",
            "title": "Tire change",
            "lead": "Change a tire safely.",
            "path": "a-tire",
            "url": "http://example/a-tire",
            "source_rank": 1,
        }
        result = SourceResult(
            source=source,
            items=[doc, tie],
            semantic=[(0.9, doc), (0.9, tie)],
            keyword=[(-1.0, doc), (-1.0, tie)],
            fulltext=[(0, doc), (0, tie)],
        )
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16
        first = search._rank_results([result], "how to change tires", 10)
        second = search._rank_results([result], "how to change tires", 10)
        self.assertEqual(first, second)
        self.assertEqual([item["id"] for item in first], ["manual:a-tire", "manual:z-tire"])
        self.assertIn("title 100%", first[0]["explain"])
        self.assertEqual(first[0]["ranking"]["final_rank"], 1)
        self.assertEqual(first[0]["ranking"]["rrf"]["semantic"]["rank"], 1)
        self.assertEqual(first[0]["ranking"]["rrf"]["title"]["rank"], 1)
        self.assertEqual(first[0]["ranking"]["rrf"]["fulltext"]["rank"], 1)
        self.assertEqual(first[0]["ranking"]["title"]["matches"], 2)

    def test_ranking_deduplicates_repeated_hits_and_preserves_source_rank(self):
        semantic_source = SourceInfo("semantic", "Semantic", "semantic", "local")
        title_source = SourceInfo("titles", "Titles", "titles", "local")
        unrelated = {
            "id": "semantic:index",
            "source_key": "semantic",
            "source": "Semantic",
            "title": "index.html",
            "lead": "",
            "path": "index.html",
            "source_rank": 1,
        }
        exact = {
            "id": "titles:intuition",
            "source_key": "titles",
            "source": "Titles",
            "title": "Intuition",
            "lead": "",
            "path": "intuition",
            "source_rank": 3,
        }
        result = SourceResult(
            source=semantic_source,
            items=[unrelated],
            semantic=[(0.99, unrelated)] * 10,
        )
        result_with_title = SourceResult(
            source=title_source,
            items=[exact],
            keyword=[(-1.0, exact)],
            fulltext=[(0, exact)],
        )
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16

        ranked = search._rank_results([result, result_with_title], "intuition", 10)

        self.assertEqual(ranked[0]["id"], "titles:intuition")
        self.assertEqual(ranked[0]["rank"], 3)
        self.assertEqual(ranked[1]["score"], 1 / 61)

    def test_semantic_hit_wins_an_exact_ranking_tie(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        semantic_doc = {
            "id": "manual:semantic",
            "source_key": "manual",
            "source": "Manual",
            "title": "Same title",
            "path": "semantic",
            "source_rank": 1,
        }
        title_doc = {
            "id": "manual:title-only",
            "source_key": "manual",
            "source": "Manual",
            "title": "Same title",
            "path": "title-only",
            "source_rank": 1,
        }
        search = object.__new__(Search)
        search.candidate_count = 16
        ranked = search._rank_results(
            [
                SourceResult(
                    source=source,
                    items=[semantic_doc],
                    semantic=[(0.9, semantic_doc)],
                ),
                SourceResult(
                    source=source,
                    items=[title_doc],
                    keyword=[(-1.0, title_doc)],
                ),
            ],
            "same title",
            10,
        )

        self.assertEqual(ranked[0]["id"], "manual:semantic")
        self.assertTrue(ranked[0]["ranking"]["semantic_hit"])
        self.assertFalse(ranked[1]["ranking"]["semantic_hit"])

    def test_compact_title_with_all_query_words_beats_longer_title(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        compact = {
            "id": "manual:compact",
            "source_key": "manual",
            "source": "Manual",
            "title": "Change Tire",
            "lead": "",
            "path": "compact",
            "source_rank": 2,
        }
        long = {
            "id": "manual:long",
            "source_key": "manual",
            "source": "Manual",
            "title": "Change Tire Maintenance and Safety Guide",
            "lead": "",
            "path": "long",
            "source_rank": 1,
        }
        result = SourceResult(
            source=source,
            items=[compact, long],
            keyword=[(-2.0, long), (-3.0, compact)],
        )
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16

        ranked = search._rank_results([result], "change tire", 10)

        self.assertEqual([item["id"] for item in ranked], ["manual:compact", "manual:long"])
        self.assertIn("density 100%", ranked[0]["explain"])

    def test_exact_content_phrase_beats_an_earlier_unrelated_fulltext_hit(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        unrelated = {
            "id": "manual:unrelated",
            "source_key": "manual",
            "source": "Manual",
            "title": "Unrelated result",
            "lead": "",
            "path": "unrelated",
            "source_rank": 1,
        }
        exact = {
            "id": "manual:exact",
            "source_key": "manual",
            "source": "Manual",
            "title": "Product announcement",
            "lead": "",
            "path": "exact",
            "source_rank": 2,
            "_content_phrase": True,
        }
        result = SourceResult(
            source=source,
            items=[unrelated, exact],
            semantic=[(0.99, unrelated)],
            fulltext=[(0, exact)],
        )
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16

        ranked = search._rank_results([result], "Tokyo banana", 10)

        self.assertEqual([item["id"] for item in ranked], ["manual:exact", "manual:unrelated"])
        self.assertTrue(ranked[0]["ranking"]["content_phrase"])
        self.assertNotIn("_content_phrase", ranked[0])

    def test_stream_emits_source_snapshots_and_final_results(self):
        sources = [
            SourceInfo("one", "One", "one", "local"),
            SourceInfo("two", "Two", "two", "local"),
        ]
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16
        search.semantic = False
        search.indexes = {}
        search.sources = {source.key: source for source in sources}
        search._state_lock = threading.RLock()
        search._reload_lock = threading.Lock()
        search._search_slots = threading.BoundedSemaphore(1)
        search._executor = ThreadPoolExecutor(max_workers=2)
        search._inflight = {}
        search._inflight_lock = threading.Lock()
        search.cache = QueryCache(16)
        search.embedder = types.SimpleNamespace(embed=lambda _: [[1.0]])

        def fake_source_search(source, query, title_query, fulltext_query, query_vector, limit):
            doc = {
                "id": f"{source.key}:page",
                "source_key": source.key,
                "source": source.name,
                "title": f"{source.name} page",
                "lead": "A useful page.",
                "path": "page",
                "url": f"http://example/{source.key}",
                "source_rank": 1,
            }
            return SourceResult(source=source, items=[doc], fulltext=[(0, doc)])

        search._search_source = fake_source_search
        events = list(search.stream_search("useful page", limit=2))
        search._executor.shutdown(wait=True)
        event_types = [event["type"] for event in events]
        self.assertEqual(event_types[0], "started")
        self.assertEqual(event_types[-1], "done")
        self.assertEqual(event_types.count("source"), 2)
        self.assertEqual(event_types.count("snapshot"), 2)
        self.assertEqual(len(events[-1]["results"]), 2)
        for event in events:
            if event["type"] in {"snapshot", "done"}:
                self.assertEqual(event["page"], 0)
                self.assertEqual(event["page_count"], 1)

    def test_stream_bounds_intermediate_ranking_passes_for_many_sources(self):
        sources = [
            SourceInfo(f"source-{index}", f"Source {index}", f"source-{index}", "local")
            for index in range(20)
        ]
        search = _bare_search(
            {
                "candidate_count": 2,
                "max_stream_ranks": 4,
            }
        )
        self.addCleanup(search.close)
        search.max_stream_ranks = 4
        search.semantic = False
        search.sources = {source.key: source for source in sources}
        search._state_lock = threading.RLock()
        search._search_slots = threading.BoundedSemaphore(1)
        search.embedder = None
        rank_sizes = []

        def fake_source_search(source, query, title_query, fulltext_query, query_vector, limit):
            doc = {
                "id": f"{source.key}:page",
                "source_key": source.key,
                "source": source.name,
                "title": source.name,
                "lead": "",
                "path": "page",
                "url": None,
                "source_rank": 1,
            }
            return SourceResult(source=source, items=[doc], fulltext=[(0, doc)])

        real_rank_results = search._rank_results

        def rank_results(source_results, query, limit):
            rank_sizes.append(len(source_results))
            return real_rank_results(source_results, query, limit)

        search._search_source = fake_source_search
        search._rank_results = rank_results
        try:
            events = list(search.stream_search("source page", limit=2))
        finally:
            search._executor.shutdown(wait=True)

        self.assertEqual(rank_sizes, [1, 5, 10, 15, 20])
        self.assertEqual(
            sum(event["type"] == "snapshot" for event in events),
            len(rank_sizes),
        )
        self.assertEqual(len(events[-1]["results"]), 2)

    def test_stream_emits_timed_snapshot_and_progress_heartbeat(self):
        # Two sources are processed in one batch: only the first is due, so
        # the second waits for the interval. A following empty interval gets a
        # heartbeat instead of leaving the stream silent.
        from concurrent.futures import Future

        import zimantic.search as search_module

        sources = [
            SourceInfo(f"source-{index}", f"Source {index}", f"source-{index}", "local")
            for index in range(3)
        ]
        search = _bare_search({"candidate_count": 2, "max_stream_ranks": 1})
        self.addCleanup(search.close)
        search.max_stream_ranks = 1  # rank_interval = ceil(3 / 1) = 3
        search.stream_update_interval = 0.05
        search.semantic = False
        search.sources = {source.key: source for source in sources}
        search._state_lock = threading.RLock()
        search._search_slots = threading.BoundedSemaphore(1)
        search.embedder = None

        def fake_source_search(source, query, title_query, fulltext_query, query_vector, limit):
            doc = {
                "id": f"{source.key}:page",
                "source_key": source.key,
                "source": source.name,
                "title": source.name,
                "lead": "",
                "path": "page",
                "url": None,
                "source_rank": 1,
            }
            return SourceResult(source=source, items=[doc], fulltext=[(0, doc)])

        order = []
        by_source = {}
        for source in sources:
            future = Future()
            future.set_result(fake_source_search(source, "query", "", "", None, 2))
            by_source[source.key] = future
            order.append(future)

        calls = {"count": 0}

        def fake_as_completed(futures, timeout=None):
            calls["count"] += 1
            if calls["count"] == 1:
                # Both finish inside one interval but only the first is due:
                # the second stays unranked until the interval elapses.
                yield order[0]
                yield order[1]
                raise TimeoutError
            if calls["count"] == 2:
                # No new completion during this interval: heartbeat.
                raise TimeoutError
            yield from order[2:]

        def submit(_fn, source, *_args, **_kwargs):
            return by_source[source.key]

        try:
            with (
                patch.object(search, "_executor") as executor,
                patch.object(
                    search,
                    "_enrich_pool",
                    lambda results, query, pool, limit: pool,
                ),
                patch.object(search, "_hydrate_results", lambda docs, *_args: None),
                patch.object(search_module, "as_completed", new=fake_as_completed),
            ):
                executor.submit.side_effect = submit
                events = list(search.stream_search("query", limit=2))
        finally:
            search._executor.shutdown(wait=True)

        types = [event["type"] for event in events]
        self.assertEqual(types.count("snapshot"), 3)
        progress = [event for event in events if event["type"] == "progress"]
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]["completed"], 2)
        self.assertEqual(progress[0]["total_sources"], 3)
        self.assertGreaterEqual(progress[0]["elapsed"], 0)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["page"], 0)
        self.assertEqual(events[-1]["page_count"], 2)

    def test_snapshot_omits_unchanged_results(self):
        # Two rank passes can leave the displayed page on the same docs. The
        # second snapshot then omits `results` so the client keeps the list it
        # already has, while the final `done` always carries the page.
        from concurrent.futures import Future

        import zimantic.search as search_module

        sources = [
            SourceInfo(f"source-{index}", f"Source {index}", f"source-{index}", "local")
            for index in range(2)
        ]
        search = _bare_search({"candidate_count": 2})
        self.addCleanup(search.close)
        search.semantic = False
        search.sources = {source.key: source for source in sources}
        search._search_slots = threading.BoundedSemaphore(1)
        search.embedder = None

        stable_doc = {
            "id": "source-0:page",
            "source_key": "source-0",
            "source": "Source 0",
            "title": "Stable",
            "lead": "",
            "path": "page",
            "url": None,
            "source_rank": 1,
        }

        def result_for(source):
            doc = dict(stable_doc)
            return SourceResult(source=source, items=[doc], fulltext=[(0, doc)])

        order = []
        by_source = {}
        for source in sources:
            future = Future()
            future.set_result(result_for(source))
            by_source[source.key] = future
            order.append(future)

        def submit(_fn, source, *_args, **_kwargs):
            return by_source[source.key]

        try:
            with (
                patch.object(search, "_executor") as executor,
                patch.object(search_module, "as_completed", return_value=iter(order)),
                # Every pass ranks to the same single page.
                patch.object(search, "_rank_results", return_value=[dict(stable_doc)]),
                patch.object(
                    search, "_enrich_pool", lambda results, query, pool, limit: pool
                ),
                patch.object(search, "_hydrate_results", lambda docs, *_args: None),
            ):
                executor.submit.side_effect = submit
                events = list(search.stream_search("query", limit=10))
        finally:
            search._executor.shutdown(wait=True)

        snapshots = [event for event in events if event["type"] == "snapshot"]
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(len(snapshots[0]["results"]), 1)
        # The page didn't move, so only the first snapshot carries the list.
        self.assertIsNone(snapshots[1]["results"])
        self.assertEqual(snapshots[1]["total"], 1)
        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(len(done["results"]), 1)

    def test_local_source_uses_batched_document_hydration(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, path UNINDEXED, target UNINDEXED)"
        )
        db.execute(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            (1, "Tire Change", "tire-change", None),
        )

        class FakeFaiss:
            def search(self, query_vector, count):
                return np.array([[0.9]]), np.array([[1]])

        source = SourceInfo("manual", "Manual", "manual", "local", local_name="manual")
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 2}
        search.candidate_count = 2
        search.semantic = True
        search.sources = {"manual": source}
        search.indexes = {
            "manual": _LocalIndex(db, FakeFaiss(), None, None, threading.Lock())
        }
        search._state_lock = threading.RLock()
        search._reload_lock = threading.Lock()
        search._search_slots = threading.BoundedSemaphore(1)
        search._executor = ThreadPoolExecutor(max_workers=1)
        search._inflight = {}
        search._inflight_lock = threading.Lock()
        search.cache = QueryCache(16)
        search.embedder = types.SimpleNamespace(embed=lambda _: np.array([[1.0]]))

        events = list(search.stream_search("how to change tires", limit=1))
        search._executor.shutdown(wait=True)
        db.close()
        result = events[-1]["results"][0]
        self.assertEqual(result["title"], "Tire Change")
        self.assertEqual(result["path"], "tire-change")

    def test_document_hydration_follows_redirect_chains(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, path UNINDEXED, target UNINDEXED)"
        )
        db.executemany(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            [
                (0, "Alias", "B", 1),
                (1, "Intermediate", "C", 2),
                (2, "Final title", "C", None),
            ],
        )
        source = SourceInfo("manual", "Manual", "manual", "local", local_name="manual")
        search = object.__new__(Search)
        search.cfg = {}
        try:
            docs = search._fetch_docs(db=db, source=source, rowids={0})
        finally:
            db.close()

        self.assertEqual(docs[0]["title"], "Final title")
        self.assertEqual(docs[0]["path"], "C")

    def test_document_hydration_batches_large_rowid_sets(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, path UNINDEXED, target UNINDEXED)"
        )
        db.executemany(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            [
                (rowid, f"Title {rowid}", f"path-{rowid}", None)
                for rowid in range(1, 1802)
            ],
        )
        db.commit()
        statements = []
        db.set_trace_callback(statements.append)

        source = SourceInfo("manual", "Manual", "manual", "local")
        search = object.__new__(Search)
        search.cfg = {}
        search.preview_chars = 100
        try:
            docs = search._fetch_docs(db=db, source=source, rowids=set(range(1, 1802)))
        finally:
            db.close()

        selects = [
            statement
            for statement in statements
            if statement.startswith("SELECT rowid, title, path, target FROM docs")
        ]
        self.assertEqual(len(selects), 3)
        self.assertEqual(len(docs), 1801)
        self.assertEqual(docs[1801]["path"], "path-1801")

    def test_semantic_results_below_cosine_threshold_are_ignored(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, path UNINDEXED, target UNINDEXED)"
        )
        db.executemany(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            [
                (1, "Below threshold", "weak", None),
                (2, "Above threshold", "strong", None),
            ],
        )

        class FakeFaiss:
            def search(self, query_vector, count):
                return (
                    np.array([[0.84, 0.90]]),
                    np.array([[1, 2]]),
                )

        source = SourceInfo("manual", "Manual", "manual", "local", local_name="manual")
        search = object.__new__(Search)
        search.cfg = {
            "candidate_count": 2,
            "min_cosine_similarity": 0.85,
        }
        search.candidate_count = 2
        result = search._search_local_locked(
            source,
            "unrelated",
            _title_query("unrelated"),
            _fulltext_query("unrelated"),
            np.array([[1.0]]),
            1,
            _LocalIndex(db, FakeFaiss(), None, None, threading.Lock()),
        )

        db.close()
        self.assertEqual([item["title"] for item in result.items], ["Above threshold"])
        self.assertEqual(result.semantic[0][0], 0.90)

    def test_local_result_preview_is_extracted_for_display(self):
        class _Entry:
            _index = 1

        class _Archive:
            def get_entry_by_path(self, path):
                self.path = path
                return _Entry()

        source = SourceInfo(
            "manual",
            "Manual",
            "manual",
            "local",
            local_name="manual",
        )
        search = object.__new__(Search)
        search.cfg = {
            "max_html_bytes": 4_194_304,
            "max_article_bytes": 33_554_432,
            "max_preview_chars": 24,
        }
        search.preview_chars = 24
        search.sources = {"manual": source}
        search.indexes = {
            "manual": _LocalIndex(
                None,
                None,
                _Archive(),
                None,
                threading.Lock(),
            )
        }
        search._state_lock = threading.RLock()
        search._executor = ThreadPoolExecutor(max_workers=1)
        search.preview_cache = QueryCache(16)
        doc = {
            "id": "manual:long-page",
            "source_key": "manual",
            "title": "Long page",
            "lead": "",
            "path": "long-page",
        }

        try:
            with patch(
                "zimantic.search.read_entry",
                return_value=(1, "Long page", "one " * 400, "long-page", None),
            ):
                search._hydrate_results([doc])
        finally:
            search._executor.shutdown(wait=True)

        self.assertLessEqual(len(doc["lead"]), 24)
        self.assertTrue(doc["lead"].endswith("one"))

    def test_title_search_falls_back_to_prefix_when_exact_matches_nothing(self):
        # Exact token matching misses these because the query word is a truncated
        # or base form of the title word; only the prefix fallback surfaces them.
        cases = [
            ((1, "Tire Changes", "tire-changes", None), "tire change", "Tire Changes"),
            ((1, "Gettysburg Address", "gettysburg-address", None), "gettysburg addres", "Gettysburg Address"),
            ((1, "Collected Poems", "collected-poems", None), "collected poem", "Collected Poems"),
            ((1, "Declaration of Independence", "declaration-independence", None), "declaration independen", "Declaration of Independence"),
            ((1, "The Praise of Folly", "praise-of-folly", None), "praise of fol", "The Praise of Folly"),
        ]
        for row, query, expected in cases:
            with self.subTest(query=query):
                results = _title_search([row], query)
                self.assertTrue(results, f"no results for {query!r}")
                self.assertEqual(results[0]["title"], expected)


class DisambiguationRankingTests(unittest.TestCase):
    def _search(self):
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16
        return search

    def _result(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        hub = {
            "id": "manual:hub",
            "source_key": "manual",
            "source": "Manual",
            "title": "Air (disambiguation)",
            "lead": "Air may refer to:",
            "path": "Air_(disambiguation)",
            "source_rank": 1,
        }
        article = {
            "id": "manual:air",
            "source_key": "manual",
            "source": "Manual",
            "title": "Air",
            "lead": "Air is a mixture of gases.",
            "path": "Air",
            "source_rank": 1,
        }
        return SourceResult(
            source=source,
            items=[hub, article],
            keyword=[(-5.0, article)],
            fulltext=[(0, article)],
        )

    def test_exact_disambiguation_title_query_promotes_page(self):
        ranked = self._search()._rank_results([self._result()], "air (disambiguation)", 10)

        self.assertEqual(ranked[0]["id"], "manual:hub")
        self.assertNotIn("kind", ranked[0])
        self.assertNotIn("members", ranked[0])

    def test_question_and_unparenthesized_disambiguation_queries_promote_page(self):
        for query in ("what is air?", "air disambiguation"):
            with self.subTest(query=query):
                ranked = self._search()._rank_results([self._result()], query, 10)
                self.assertEqual(ranked[0]["id"], "manual:hub")

    def test_base_title_and_partial_queries_demote_hub(self):
        for query in ("air", "air quality"):
            with self.subTest(query=query):
                ranked = self._search()._rank_results([self._result()], query, 10)
                self.assertEqual(ranked[0]["id"], "manual:air")


class UserPageRankingTests(unittest.TestCase):
    def _search(self):
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16
        return search

    def _result(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        user = {
            "id": "manual:user",
            "source_key": "manual",
            "source": "Manual",
            "title": "User Lockjaw",
            "lead": "Member since July 2017.",
            "path": "users/123/lockjaw",
            "source_rank": 1,
        }
        article = {
            "id": "manual:article",
            "source_key": "manual",
            "source": "Manual",
            "title": "Lockjaw",
            "lead": "Lockjaw is another name for trismus.",
            "path": "Lockjaw",
            "source_rank": 2,
        }
        return SourceResult(
            source=source,
            items=[user, article],
            keyword=[(-1.0, user), (-2.0, article)],
            fulltext=[(0, user), (1, article)],
        )

    def test_user_pages_are_demoted_for_normal_queries(self):
        ranked = self._search()._rank_results([self._result()], "lockjaw", 10)

        self.assertEqual(ranked[0]["id"], "manual:article")
        user = next(item for item in ranked if item["id"] == "manual:user")
        self.assertEqual(user["ranking"]["user_page"]["action"], "demote")

    def test_explicit_user_queries_keep_user_pages_eligible(self):
        ranked = self._search()._rank_results([self._result()], "user lockjaw", 10)

        self.assertEqual(ranked[0]["id"], "manual:user")
        self.assertEqual(ranked[0]["ranking"]["user_page"]["action"], "none")


def _write_index(index_dir: Path, name: str, *, done: str = "1", rows=()):
    db = sqlite3.connect(index_dir / f"{name}.sqlite")
    db.executescript(
        "CREATE VIRTUAL TABLE docs USING fts5("
        "title, path UNINDEXED, target UNINDEXED, "
        "tokenize='unicode61 remove_diacritics 2');"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value);"
    )
    db.executemany(
        "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
        rows,
    )
    db.execute("INSERT INTO meta VALUES ('done', ?)", (done,))
    # A matching archive so a finished index is not treated as orphaned, plus
    # the source size a real build would record.
    zim = index_dir / f"{name}.zim"
    zim.write_bytes(b"zim")
    db.execute(
        "INSERT OR REPLACE INTO meta VALUES ('filesize', ?)",
        (str(zim.stat().st_size),),
    )
    db.commit()
    db.close()
    return index_dir / f"{name}.sqlite"


class GracefulDegradationTests(unittest.TestCase):
    def _cfg(self, tmp):
        return {
            "index_dir": str(tmp),
            "zim_dir": str(tmp),
            "candidate_count": 16,
        }

    def test_index_without_vectors_still_serves_title_results(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            self.assertIn("manual", search.indexes)
            self.assertFalse(search.indexes["manual"].semantic)

            results = _search_results(search, "tire change", limit=1)
            self.assertEqual(results[0]["title"], "Tire Change")
            self.assertEqual(results[0]["path"], "tire-change")
            search._executor.shutdown(wait=True)

    def test_index_vectors_built_with_a_different_model_are_ignored(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            db_path = _write_index(
                tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)]
            )
            db = sqlite3.connect(db_path)
            db.execute(
                "INSERT INTO meta VALUES ('model_fingerprint', 'old-model')"
            )
            db.commit()
            db.close()
            db_path.with_suffix(".faiss").write_bytes(b"vectors")

            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._defer_faiss = False
            search._model_fingerprint = "new-model"
            with self.assertLogs("zimantic.search", level="WARNING") as logs:
                index = search._open_index(db_path)

            self.assertIsNotNone(index)
            self.assertIsNone(index.faiss_index)
            self.assertTrue(any("different model" in line for line in logs.output))
            search._executor.shutdown(wait=True)

    def test_health_reports_ok_when_homepage_is_unavailable(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            zim = tmp / "manual.zim"
            zim.write_bytes(b"zim")
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search.semantic = False
            search.sources = {
                "manual": SourceInfo(
                    "manual", "Manual", "manual", "local", local_name="manual"
                )
            }
            search.indexes = {
                "manual": _LocalIndex(
                    None,
                    None,
                    _HealthArchive(),
                    None,
                    threading.Lock(),
                    zim_path=str(zim),
                )
            }
            # A redirect cycle raises ArticleUnavailableError; /health must
            # degrade the field, not bubble up as a 500.
            with patch.object(
                search,
                "_resolve_article_entry",
                side_effect=ArticleUnavailableError("article redirect cycle"),
            ):
                report = search.health()

            self.assertEqual(report["status"], "ok")
            self.assertIsNone(report["indexes"][0]["homepage"])
            search._executor.shutdown(wait=True)

    def test_library_includes_sources_without_a_homepage(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search.sources = {
            "readable": SourceInfo(
                "readable",
                "Readable",
                "readable",
                "local",
                local_name="readable",
                homepage="Main_Page",
            ),
            "unreadable": SourceInfo(
                "unreadable",
                "Unreadable",
                "unreadable",
                "local",
                local_name="unreadable",
                searchable=False,
            ),
        }
        with patch.object(
            search,
            "_scan_zim_paths",
            return_value={
                "readable": Path("/zims/readable.zim"),
                "unreadable": Path("/zims/unreadable.zim"),
            },
        ):
            library = search.library_dicts()

        self.assertEqual(
            [source["name"] for source in library], ["Readable", "Unreadable"]
        )
        self.assertTrue(library[0]["searchable"])
        self.assertFalse(library[1]["searchable"])
        search._executor.shutdown(wait=True)

    def test_library_lists_an_unreadable_source(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search.sources = {
            "broken": SourceInfo(
                "broken",
                "Broken",
                "broken",
                "local",
                local_name="broken",
                available=False,
                error="Invalid magic number",
                title="A broken book",
            ),
        }
        with patch.object(
            search,
            "_scan_zim_paths",
            return_value={"broken": Path("/zims/broken.zim")},
        ):
            library = search.library_dicts()

        self.assertEqual([source["name"] for source in library], ["Broken"])
        self.assertFalse(library[0]["available"])
        self.assertEqual(library[0]["error"], "Invalid magic number")
        self.assertEqual(library[0]["title"], "A broken book")
        search._executor.shutdown(wait=True)

    def test_library_prefers_the_larger_version_of_one_book(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search.sources = {
            "book_2025-01": SourceInfo(
                "book_2025-01",
                "book_2025-01",
                "book_2025-01",
                "local",
                local_name="book_2025-01",
                title="A book",
                date="2025-01-01",
                uuid="older-uuid",
                size=100,
            ),
            "book_2026-01": SourceInfo(
                "book_2026-01",
                "book_2026-01",
                "book_2026-01",
                "local",
                local_name="book_2026-01",
                title="A book",
                date="2026-01-01",
                uuid="newer-uuid",
                size=90,
            ),
        }
        with patch.object(
            search,
            "_scan_zim_paths",
            return_value={
                "book_2025-01": Path("/zims/book_2025-01.zim"),
                "book_2026-01": Path("/zims/book_2026-01.zim"),
            },
        ):
            library = search.library_dicts()

        # A larger copy is usually the more complete download, even when an
        # older date would otherwise win.
        self.assertEqual([source["name"] for source in library], ["book_2025-01"])
        self.assertEqual(library[0]["date"], "2025-01-01")
        search._executor.shutdown(wait=True)

    def test_library_prefers_a_readable_duplicate(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search.sources = {
            "broken": SourceInfo(
                "broken",
                "Broken",
                "broken",
                "local",
                local_name="broken",
                available=False,
                uuid="same-uuid",
                error="Invalid magic number",
            ),
            "copy": SourceInfo(
                "copy",
                "Copy",
                "copy",
                "local",
                local_name="copy",
                available=True,
                uuid="same-uuid",
                homepage="Main_Page",
                title="Working copy",
            ),
        }
        with patch.object(
            search,
            "_scan_zim_paths",
            return_value={
                "broken": Path("/zims/broken.zim"),
                "copy": Path("/zims/copy.zim"),
            },
        ):
            library = search.library_dicts()

        self.assertEqual([source["name"] for source in library], ["Copy"])
        self.assertTrue(library[0]["available"])
        search._executor.shutdown(wait=True)

    def test_failed_archive_open_is_not_retried(self):
        with _temp_dir(self) as tmp:
            index = _LocalIndex(
                None,
                None,
                None,
                None,
                threading.Lock(),
                zim_path=str(Path(tmp) / "missing.zim"),
            )
            with patch(
                "zimantic.searching.index.Archive",
                side_effect=RuntimeError("cannot open"),
            ) as archive:
                self.assertIsNone(index.ensure_open())
                self.assertIsNone(index.ensure_open())
                self.assertEqual(archive.call_count, 1)
            self.assertEqual(index.error, "cannot open")

    def test_reload_retries_a_latched_archive_open(self):
        with _temp_dir(self) as tmp:
            zim = Path(tmp) / "manual.zim"
            zim.write_bytes(b"not a zim")
            search = _bare_search({"zim_dir": tmp})
            self.addCleanup(search.close)
            search._defer_faiss = False
            index = _LocalIndex(
                None,
                None,
                None,
                None,
                threading.Lock(),
                zim_path=str(zim),
                zim_signature=_file_signature(zim),
            )
            search.indexes["manual"] = index
            with patch.object(
                search,
                "_discover_local_indexes",
                return_value={"manual": ("zim", zim)},
            ), patch(
                "zimantic.searching.index.Archive",
                side_effect=RuntimeError("cannot open"),
            ) as archive:
                self.assertIsNone(index.ensure_open())
                self.assertTrue(index.open_failed)
                # Nothing about the index changed, but an explicit reload must
                # still retry (e.g. permissions or a mount were fixed).
                search.reload(scan_catalog=False, refresh=False)
                self.assertIs(search.indexes["manual"], index)
                self.assertFalse(index.open_failed)
                self.assertIsNone(index.ensure_open())
                self.assertEqual(archive.call_count, 2)
            search._executor.shutdown(wait=True)

    def test_deferred_faiss_loads_vectors_after_indexes_open(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            (tmp / "manual.faiss").write_bytes(b"vectors")
            search = Search(self._cfg(tmp), semantic=True, archive_factory=object)
            self.addCleanup(search._executor.shutdown, wait=True)

            self.assertFalse(search.indexes["manual"].semantic)

            reloaded = threading.Event()
            original_reload = search.reload

            def reload(**kwargs):
                try:
                    return original_reload(**kwargs)
                finally:
                    reloaded.set()

            with (
                patch.object(sys.modules["faiss"], "read_index", return_value=object(), create=True),
                patch.object(search, "reload", side_effect=reload),
            ):
                search.start_faiss()
                self.assertTrue(reloaded.wait(2))

            self.assertTrue(search.indexes["manual"].semantic)

    def test_deferred_faiss_reload_skips_catalog_and_source_refresh(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            (tmp / "manual.faiss").write_bytes(b"vectors")
            search = Search(self._cfg(tmp), semantic=True, archive_factory=object)
            self.addCleanup(search._executor.shutdown, wait=True)
            self.assertIsNotNone(search.catalog)

            reloaded = threading.Event()
            original_reload = search.reload

            def reload(**kwargs):
                try:
                    return original_reload(**kwargs)
                finally:
                    reloaded.set()

            with (
                patch.object(sys.modules["faiss"], "read_index", return_value=object(), create=True),
                patch.object(search, "reload", side_effect=reload),
                patch.object(search.catalog, "scan") as catalog_scan,
                patch.object(search, "refresh_sources") as refresh_sources,
            ):
                search.start_faiss()
                self.assertTrue(reloaded.wait(2))

            # __init__ already scanned the catalog and built the source set, so
            # the startup FAISS reload must not repeat either.
            catalog_scan.assert_not_called()
            refresh_sources.assert_not_called()
            self.assertTrue(search.indexes["manual"].semantic)

    def test_archive_lookup_prefers_the_matching_size_then_the_largest(self):
        with _temp_dir(self) as tmp:
            root = Path(tmp)
            nested = root / "collection" / "nested"
            nested.mkdir(parents=True)
            moved = nested / "manual.zim"
            moved.write_bytes(b"x" * 10)
            search = _bare_search({"zim_dir": str(root)})
            self.addCleanup(search.close)

            self.assertEqual(search._find_zim_path("manual", 10), moved)
            (root / "other").mkdir()
            larger = root / "other" / "manual.zim"
            larger.write_bytes(b"x" * 11)
            # An exact-size archive is still the index's own identity.
            self.assertEqual(search._find_zim_path("manual", 10), moved)
            # With no size match, the largest same-stem file is used rather than
            # leaving the index with no archive at all.
            self.assertEqual(search._find_zim_path("manual", 12), larger)

    def test_scan_zim_paths_accepts_uppercase_extension(self):
        with _temp_dir(self) as tmp:
            root = Path(tmp)
            upper = root / "manual.ZIM"
            upper.write_bytes(b"x")
            search = _bare_search({"zim_dir": str(root)})
            self.addCleanup(search.close)
            self.assertEqual(search._scan_zim_paths(), {"manual": upper})
            self.assertEqual(search._find_zim_path("manual", 1), upper)

    def test_scan_zim_paths_keeps_the_largest_of_a_repeated_stem(self):
        with _temp_dir(self) as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "a" / "solo.zim").write_bytes(b"x" * 10)
            # Same-size duplicates keep the first path.
            (root / "a" / "twin.zim").write_bytes(b"y" * 4)
            (root / "b").mkdir()
            (root / "b" / "twin.zim").write_bytes(b"z" * 4)
            # Different sizes: the larger file wins instead of dropping the stem.
            (root / "a" / "clash.zim").write_bytes(b"x" * 5)
            larger = root / "b" / "clash.zim"
            larger.write_bytes(b"x" * 6)

            search = _bare_search({"zim_dir": str(root)})
            self.addCleanup(search.close)
            found = search._scan_zim_paths()

            self.assertEqual(found["solo"], root / "a" / "solo.zim")
            self.assertEqual(found["twin"], root / "a" / "twin.zim")
            self.assertEqual(found["clash"], larger)

    def test_finished_stream_is_cached_for_search_method(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            events = list(search.stream_search("tire change", limit=1))
            self.assertEqual(events[-1]["type"], "done")
            self.assertIsNotNone(search.cache.get(search._cache_key("tire change", None)))

            with patch.object(search, "_search_source", side_effect=AssertionError("cache miss")):
                cached = _search_results(search, "tire change", limit=1)
            self.assertEqual(cached[0]["title"], "Tire Change")
            search._executor.shutdown(wait=True)

    def test_cached_stream_replays_source_outcomes(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(
                tmp,
                "manual",
                rows=[(1, "Tire Change", "tire-change", None)],
            )
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            first_events = list(search.stream_search("tire change", limit=1))
            cached_events = list(search.stream_search("tire change", limit=1))
            search._executor.shutdown(wait=True)

            self.assertEqual([event["type"] for event in cached_events], [
                "started", "source", "done",
            ])
            self.assertEqual(cached_events[1]["source"]["key"], "manual")
            self.assertEqual(cached_events[1]["count"], 1)
            self.assertEqual(cached_events[-1]["results"], first_events[-1]["results"])

    def test_cached_source_counts_match_the_stream(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(
                tmp,
                "manual",
                rows=[(1, "Tire Change", "tire-change", None)],
            )
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            # Enrichment can reorder or drop docs. Simulate it emptying the pool
            # so a count recomputed *after* enrichment would be 0; a cache
            # replay must still match the counts the live stream reported.
            with patch.object(search, "_enrich_pool", return_value=[]):
                live = list(search.stream_search("tire change", limit=1))
                cached = list(search.stream_search("tire change", limit=1))
            search._executor.shutdown(wait=True)

            live_counts = [event.get("count") for event in live if event["type"] == "source"]
            cached_counts = [
                event.get("count") for event in cached if event["type"] == "source"
            ]
            self.assertEqual(live_counts, [1])
            self.assertEqual(cached_counts, live_counts)

    def test_source_event_omits_count_until_its_source_is_ranked(self):
        # With more sources than max_stream_ranks a source can finish between
        # rank passes. It must not report a misleading 0; the following snapshot
        # carries the full counts map instead, and a cache replay matches.
        from concurrent.futures import Future

        import zimantic.search as search_module

        sources = [
            SourceInfo(key=key, name=key.upper(), book=key, mode="local", local_name=key)
            for key in ("a", "b", "c")
        ]
        cfg = self._cfg(Path("/tmp/does-not-exist"))
        cfg["max_stream_ranks"] = 1  # rank_interval = ceil(3 / 1) = 3

        search = _bare_search(cfg)
        self.addCleanup(search.close)
        search.sources = {source.key: source for source in sources}
        search._enrich_pool = lambda results, query, pool, limit: pool
        search._hydrate_results = lambda docs, *_args: None

        def result_for(source):
            doc = {
                "id": f"{source.key}:{source.key}-path",
                "source_key": source.key,
                "source": source.name,
                "source_icon": None,
                "title": source.name,
                "lead": "",
                "path": f"{source.key}-path",
                "mimetype": None,
                "url": None,
            }
            return SourceResult(
                source=source,
                items=[doc],
                semantic=[],
                keyword=[(1.0, doc)],
                fulltext=[],
            )

        order = []
        by_source = {}
        for source in sources:
            future = Future()
            future.set_result(result_for(source))
            by_source[source.key] = future
            order.append(future)

        def submit(_fn, source, *_args, **_kwargs):
            return by_source[source.key]

        try:
            with (
                patch.object(search, "_executor") as executor,
                patch.object(search_module, "as_completed", return_value=iter(order)),
            ):
                executor.submit.side_effect = submit
                events = list(search.stream_search("hello", limit=10))
            cached = list(search.stream_search("hello", limit=10))
        finally:
            search._executor.shutdown(wait=True)

        def counts_of(stream):
            return {
                event["source"]["key"]: event.get("count")
                for event in stream
                if event["type"] == "source"
            }

        live = counts_of(events)
        self.assertEqual(live["a"], 1)
        self.assertEqual(live["c"], 1)
        middle = next(
            event
            for event in events
            if event["type"] == "source" and event["source"]["key"] == "b"
        )
        self.assertNotIn("count", middle)
        # The final snapshot still accounts for every source, b included.
        done = [event for event in events if event["type"] == "done"][-1]
        self.assertEqual(done["counts"].get("b"), 1)
        # A cache replay reproduces exactly what the live stream reported.
        self.assertEqual(counts_of(cached), live)

    def test_refresh_sources_keeps_cache_when_sources_unchanged(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            events = list(search.stream_search("tire change", limit=1))
            self.assertEqual(events[-1]["type"], "done")
            key = search._cache_key("tire change", None)
            self.assertIsNotNone(search.cache.get(key))

            # A plain source refresh with no changes must keep the cached answer
            # (source discovery happens on page load without re-running searches).
            search.refresh_sources()
            self.assertIsNotNone(search.cache.get(key))

            # Adding an index changes what is searchable, so stale answers drop.
            _write_index(tmp, "second", rows=[(1, "Second page", "second", None)])
            search.reload()
            self.assertIsNone(search.cache.get(key))
            search._executor.shutdown(wait=True)

    def test_reload_keeps_cache_when_nothing_changed(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()
            list(search.stream_search("tire change", limit=1))
            key = search._cache_key("tire change", None)
            self.assertIsNotNone(search.cache.get(key))

            search.reload()
            self.assertIsNotNone(search.cache.get(key))
            search._executor.shutdown(wait=True)

    def test_reload_picks_up_and_forgets_indexes(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)

            self.assertEqual(search.reload()["indexes"], [])

            _write_index(tmp, "manual", done="fast", rows=[(1, "Tire Change", "tire-change", None)])
            summary = search.reload()
            self.assertEqual(summary["added"], ["manual"])
            self.assertIn("manual", search.indexes)
            self.assertFalse(search.indexes["manual"].semantic)

            (tmp / "manual.sqlite").unlink()
            (tmp / "manual.zim").unlink()
            summary = search.reload()
            self.assertEqual(summary["removed"], ["manual"])
            self.assertEqual(search.indexes, {})
            search._executor.shutdown(wait=True)

    def test_reload_reopens_a_replaced_index(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(
                tmp,
                "manual",
                rows=[(1, "Old title", "old", None)],
            )
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()
            old_index = search.indexes["manual"]

            _write_index(
                tmp,
                "replacement",
                rows=[(1, "New title", "new", None)],
            )
            (tmp / "replacement.sqlite").replace(tmp / "manual.sqlite")

            search.reload()

            self.assertIsNot(search.indexes["manual"], old_index)
            self.assertEqual(_search_results(search, "new title", limit=1)[0]["title"], "New title")
            search._close_index(search.indexes["manual"])
            search._executor.shutdown(wait=True)

    def test_incomplete_index_keeps_the_zim_fulltext_fallback(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            # A filesystem fallback is discoverable even while a build is in
            # progress (the schema exists but no 'done' marker is written yet).
            (tmp / "manual.zim").write_bytes(b"zim")
            db = sqlite3.connect(tmp / "manual.sqlite")
            db.executescript(
                "CREATE VIRTUAL TABLE docs USING fts5("
                "title, path UNINDEXED, target UNINDEXED, "
                "tokenize='unicode61 remove_diacritics 2');"
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value);"
            )
            db.close()

            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            self.assertEqual(search._discover_local_indexes()["manual"][0], "zim")

            # Once the build finishes, the SQLite index becomes authoritative.
            db = sqlite3.connect(tmp / "manual.sqlite")
            db.execute("INSERT INTO meta VALUES ('done', '1')")
            db.commit()
            db.close()
            self.assertEqual(search._discover_local_indexes()["manual"][0], "sqlite")
            search._executor.shutdown(wait=True)

    def test_index_whose_zim_changed_size_falls_back_to_the_unindexed_zim(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", done="1", rows=[(1, "Old", "old", None)])
            db = sqlite3.connect(tmp / "manual.sqlite")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('filesize', '999')")
            db.commit()
            db.close()
            zim = tmp / "manual.zim"
            zim.write_bytes(b"zim")  # 3 bytes, not the recorded 999

            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            found = search._discover_local_indexes()
            # The stale index is ignored and the archive serves unindexed.
            self.assertEqual(found["manual"], ("zim", zim))
            search._executor.shutdown(wait=True)

    def test_index_matching_its_zim_size_stays_authoritative(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", done="1", rows=[(1, "Old", "old", None)])
            zim = tmp / "manual.zim"
            zim.write_bytes(b"zim")
            db = sqlite3.connect(tmp / "manual.sqlite")
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('filesize', ?)",
                (str(zim.stat().st_size),),
            )
            db.commit()
            db.close()

            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            self.assertEqual(search._discover_local_indexes()["manual"][0], "sqlite")
            search._executor.shutdown(wait=True)

    def test_finished_index_with_no_local_zim_is_ignored(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", done="1", rows=[(1, "Old", "old", None)])
            (tmp / "manual.zim").unlink()

            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            # With no archive to serve, the orphaned index is not loaded.
            self.assertNotIn("manual", search._discover_local_indexes())
            search._executor.shutdown(wait=True)


class CatalogRefreshTests(unittest.TestCase):
    def test_failed_catalog_refresh_preserves_last_successful_entries(self):
        search = _bare_search({"kiwix_server": "http://kiwix"})
        self.addCleanup(search.close)
        entries = [{"name": "Remote Manual", "book": "manual"}]

        with patch.object(search, "_catalog_entries", side_effect=[entries, None]):
            first = search.refresh_sources()
            second = search.refresh_sources()

        self.assertEqual(first[0]["name"], "Remote Manual")
        self.assertEqual(second[0]["name"], "Remote Manual")

    def test_catalog_refresh_retries_with_bounded_backoff(self):
        class _Response:
            headers = {}

            def __init__(self):
                self._read = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def _payload():
                return (
                    b"<feed><entry><title>Remote Manual</title>"
                    b'<link href="/content/manual" /></entry></feed>'
                )

            def read(self, _size):
                if self._read:
                    return b""
                self._read = True
                return self._payload()

        search = _bare_search({})
        self.addCleanup(search.close)
        with (
            patch(
                "zimantic.search.urlopen",
                side_effect=[OSError("temporary outage"), _Response()],
            ) as urlopen,
            patch("zimantic.search.time.sleep") as sleep,
        ):
            entries = search._catalog_entries("http://kiwix")

        self.assertEqual(entries[0]["book"], "manual")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_catalog_refresh_does_not_retry_invalid_responses(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        with (
            patch("zimantic.search.urlopen", side_effect=ValueError("bad XML")) as urlopen,
            patch("zimantic.search.time.sleep") as sleep,
        ):
            entries = search._catalog_entries("http://kiwix")

        self.assertIsNone(entries)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


    def test_catalog_limit_is_enforced_after_parsing(self):
        class _Response:
            headers = {}

            def __init__(self):
                self._read = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                if self._read:
                    return b""
                self._read = True
                return (
                    b"<feed>"
                    b'<entry><title>One</title><link href="/content/one" /></entry>'
                    b'<entry><title>Two</title><link href="/content/two" /></entry>'
                    b'<entry><title>Three</title><link href="/content/three" /></entry>'
                    b"</feed>"
                )

        search = _bare_search({"kiwix_catalog_limit": 2})
        self.addCleanup(search.close)
        with patch("zimantic.search.urlopen", return_value=_Response()):
            entries = search._catalog_entries("http://kiwix")

        self.assertEqual([entry["book"] for entry in entries], ["one", "three"])

    def test_catalog_book_uses_the_content_segment_for_article_links(self):
        class _Response:
            headers = {}

            def __init__(self):
                self._read = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                if self._read:
                    return b""
                self._read = True
                return (
                    b"<feed><entry><title>Manual</title>"
                    b'<link type="text/html" '
                    b'href="https://example.test/content/manual/A/Main_Page" />'
                    b"</entry></feed>"
                )

        search = _bare_search({})
        self.addCleanup(search.close)
        with patch("zimantic.search.urlopen", return_value=_Response()):
            entries = search._catalog_entries("http://kiwix")

        self.assertEqual(entries[0]["book"], "manual")


class KiwixSearchTests(unittest.TestCase):
    def test_relative_result_links_are_resolved_against_the_server(self):
        class _Response:
            headers = {}

            def __init__(self):
                self.payload = (
                    b"<rss><item><title>Relative</title>"
                    b"<link>manual/page</link><description>snippet</description>"
                    b"</item></rss>"
                )
                self._read = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                if self._read:
                    return b""
                self._read = True
                return self.payload

        search = _bare_search({"candidate_count": 4, "kiwix_server": "http://kiwix"})
        self.addCleanup(search.close)
        source = SourceInfo("remote", "Remote", "manual", "kiwix")
        with patch("zimantic.search.urlopen", return_value=_Response()):
            result = search._search_kiwix(source, "query", 4)

        self.assertEqual(result.items[0]["url"], "http://kiwix/manual/page")


class PaginationTests(unittest.TestCase):
    def _cfg(self, tmp):
        return {
            "index_dir": str(tmp),
            "zim_dir": str(tmp),
            "candidate_count": 16,
            "page_size": 2,
            "max_results": 5,
        }

    def test_pages_are_prefixes_of_one_ranked_pool(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            rows = [(i, f"Tire Change {i}", f"tire-{i}", None) for i in range(1, 6)]
            _write_index(tmp, "manual", rows=rows)
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            page1 = _search_results(search, "tire change", limit=2, offset=0)
            page2 = _search_results(search, "tire change", limit=2, offset=2)
            page3 = _search_results(search, "tire change", limit=2, offset=4)
            whole = _search_results(search, "tire change", limit=5, offset=0)

            paged = [doc["id"] for doc in page1 + page2 + page3]
            self.assertEqual(paged, [doc["id"] for doc in whole])
            search._executor.shutdown(wait=True)

    def test_out_of_range_offset_clamps_to_last_page(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            rows = [(i, f"Tire Change {i}", f"tire-{i}", None) for i in range(1, 6)]
            _write_index(tmp, "manual", rows=rows)
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            page = search.search_page("tire change", limit=2, offset=100)
            self.assertEqual(page["offset"], 4)
            self.assertEqual(page["page"], 2)
            self.assertEqual(page["page_count"], 3)
            self.assertEqual(len(page["results"]), 1)
            self.assertFalse(page["has_more"])
            self.assertEqual(page["total"], 5)
            search._executor.shutdown(wait=True)

    def test_second_page_reuses_the_cached_pool(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            rows = [(i, f"Tire Change {i}", f"tire-{i}", None) for i in range(1, 6)]
            _write_index(tmp, "manual", rows=rows)
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            first = _search_results(search, "tire change", limit=2, offset=0)
            pooled = search.cache.get(search._cache_key("tire change", None))
            self.assertEqual(len(pooled["pool"]), 5)
            # A different page must be answered from the same cached entry.
            second = _search_results(search, "tire change", limit=2, offset=2)
            self.assertIs(search.cache.get(search._cache_key("tire change", None)), pooled)
            self.assertNotEqual(first[0]["id"], second[0]["id"])
            search._executor.shutdown(wait=True)

    def test_display_source_filter_keeps_counts_for_all_sources(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "alpha", rows=[(1, "Tire Change Alpha", "a", None)])
            _write_index(tmp, "beta", rows=[(1, "Tire Change Beta", "b", None)])
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            page = search.search_page("tire change", limit=10, source="beta")

            self.assertEqual([doc["source_key"] for doc in page["results"]], ["beta"])
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["counts"], {"alpha": 1, "beta": 1})

            page = search.search_page("tire change", limit=10, source=["alpha", "beta"])

            self.assertEqual(
                [doc["source_key"] for doc in page["results"]],
                ["alpha", "beta"],
            )
            self.assertEqual(page["total"], 2)
            search._executor.shutdown(wait=True)

    def test_concurrent_identical_searches_share_one_computation(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire", None)])
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            original = search._search_source
            release = threading.Event()
            calls = 0

            def slow(*args, **kwargs):
                nonlocal calls
                calls += 1
                release.wait(timeout=5)
                return original(*args, **kwargs)

            search._search_source = slow
            first: list = []
            second: list = []

            def run(target):
                target.extend(search.stream_search("tire change", limit=1))

            owner = threading.Thread(target=run, args=(first,))
            owner.start()
            # Wait until the owner has claimed the key, then start a waiter.
            deadline = time.monotonic() + 5
            while not search._inflight and time.monotonic() < deadline:
                time.sleep(0.005)
            waiter = threading.Thread(target=run, args=(second,))
            waiter.start()
            time.sleep(0.05)
            release.set()
            owner.join(timeout=5)
            waiter.join(timeout=5)

            self.assertEqual(calls, 1)
            self.assertEqual(first[-1]["results"], second[-1]["results"])
            search._executor.shutdown(wait=True)

    def test_disconnected_stream_finishes_shared_search(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        quick = SourceInfo("quick", "Quick", "quick", "local", local_name="quick")
        slow = SourceInfo("slow", "Slow", "slow", "local", local_name="slow")
        search.sources = {source.key: source for source in (quick, slow)}
        release = threading.Event()
        slow_started = threading.Event()

        def search_source(source, *_args):
            if source.key == "slow":
                slow_started.set()
                release.wait(timeout=5)
            return SourceResult(source=source)

        search._search_source = search_source
        stream = search.stream_search("anything", limit=1)
        try:
            self.assertEqual(next(stream)["type"], "started")
            self.assertEqual(next(stream)["type"], "source")
            self.assertTrue(slow_started.wait(timeout=5))
            stream.close()

            self.assertTrue(search._inflight)
            release.set()
            deadline = time.monotonic() + 5
            while search._inflight and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertFalse(search._inflight)
            cached = list(search.stream_search("anything", limit=1))
            self.assertEqual(cached[-1]["type"], "done")
        finally:
            release.set()
            search._executor.shutdown(wait=True)

    def test_debug_flag_gates_ranking_explanation(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire", None)])
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            plain = search.search_page("tire change", limit=10, debug=False)["results"][0]
            detailed = search.search_page("tire change", limit=10, debug=True)["results"][0]

            self.assertNotIn("explain", plain)
            self.assertNotIn("score", plain)
            self.assertNotIn("ranking", plain)
            self.assertIn("explain", detailed)
            self.assertIn("ranking", detailed)
            self.assertEqual(
                search.search_page("tire change", limit=10, debug=True)["matched_by"],
                {"semantic": 0, "title": 1, "fulltext": 0},
            )
            search._executor.shutdown(wait=True)


class SourceScopeTests(unittest.TestCase):
    def _cfg(self, tmp):
        return {
            "index_dir": str(tmp),
            "zim_dir": str(tmp),
            "candidate_count": 16,
            "page_size": 10,
            "max_results": 100,
        }

    def _search(self, tmp):
        for index in range(1, 7):
            _write_index(
                tmp,
                f"s{index}",
                rows=[(1, f"Tire Change {index}", f"tire-{index}", None)],
            )
        search = _bare_search(self._cfg(tmp))
        self.addCleanup(search.close)
        search._load_indexes()
        search.refresh_sources()
        return search

    def test_scan_zim_paths_logs_unreadable_directories(self):
        import zimantic.search as search_module

        search = _bare_search({"zim_dir": "/zims"})
        self.addCleanup(search.close)

        def fake_iter_zim_files(_root, on_error=None):
            assert on_error is not None
            on_error(OSError(13, "Permission denied", "/zims/private"))
            return iter(())

        with (
            patch.object(search_module, "iter_zim_files", side_effect=fake_iter_zim_files),
            self.assertLogs("zimantic.search", level="ERROR") as logs,
        ):
            self.assertEqual(search._scan_zim_paths(), {})

        self.assertTrue(any("/zims/private" in line for line in logs.output))

    def test_selective_limit_is_min_ten_or_thirty_five_percent(self):
        self.assertEqual(Search._selective_source_limit(1), 0)
        self.assertEqual(Search._selective_source_limit(3), 1)
        self.assertEqual(Search._selective_source_limit(20), 7)
        self.assertEqual(Search._selective_source_limit(100), 10)
        self.assertEqual(Search._selective_source_limit(1_000), 10)

    def test_unknown_source_selection_falls_back_to_all_sources(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            search = self._search(tmp)
            try:
                sources, selective, scope = search._search_scope(["removed-source"])
                self.assertEqual(len(sources), 6)
                self.assertFalse(selective)
                self.assertIsNone(scope)
            finally:
                search._executor.shutdown(wait=True)

    def test_explicit_empty_source_list_is_an_empty_scope(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            search = self._search(tmp)
            try:
                sources, selective, scope = search._search_scope([])
                self.assertEqual(sources, [])
                self.assertTrue(selective)
                self.assertEqual(scope, ())
            finally:
                search._executor.shutdown(wait=True)

    def test_remote_dated_book_slug_selects_the_local_base_source(self):
        search = _bare_search({})
        search.sources = {
            "foo": SourceInfo(
                "foo", "foo", "foo", "local", local_name="foo"
            )
        }
        try:
            sources, _selective, _scope = search._search_scope(
                ["foo_2024-01"]
            )
        finally:
            search._executor.shutdown(wait=True)
        self.assertEqual([source.key for source in sources], ["foo"])

    def test_hidden_flavour_variant_selects_the_chosen_book(self):
        search = _bare_search({})
        search.sources = {
            "medicine": SourceInfo(
                "medicine",
                "medicine_nopic_2025-10",
                "medicine_nopic_2025-10",
                "local",
                local_name="medicine_nopic_2025-10",
            )
        }
        for index in range(3):
            key = f"other-{index}"
            search.sources[key] = SourceInfo(
                key, key, key, "local", local_name=key
            )
        try:
            sources, selective, _scope = search._search_scope(
                ["medicine_mini_2025-10"]
            )
        finally:
            search._executor.shutdown(wait=True)
        self.assertEqual([source.key for source in sources], ["medicine"])
        self.assertTrue(selective)

    def test_unscoped_search_caps_remote_sources_but_honors_explicit_ones(self):
        search = _bare_search({"max_remote_sources": 1})
        search.sources = {
            "local": SourceInfo("local", "Local", "local", "local"),
            "remote-one": SourceInfo("remote-one", "One", "one", "kiwix"),
            "remote-two": SourceInfo("remote-two", "Two", "two", "kiwix"),
        }
        try:
            sources, _selective, _scope = search._search_scope(None)
            self.assertEqual([source.key for source in sources], ["local", "remote-one"])
            sources, _selective, _scope = search._search_scope(["remote-two"])
            self.assertEqual([source.key for source in sources], ["remote-two"])
            search.max_remote_sources = 0
            sources, _selective, _scope = search._search_scope(["remote-two"])
            self.assertEqual([source.key for source in sources], ["remote-two"])
            sources, _selective, _scope = search._search_scope(None)
            self.assertEqual([source.key for source in sources], ["local"])
        finally:
            search._executor.shutdown(wait=True)

    def test_remote_catalog_does_not_duplicate_local_or_remote_variants(self):
        search = _bare_search({})
        index = types.SimpleNamespace(source_size=None, ensure_open=lambda: None)
        search.indexes = {"medicine_maxi_2025-10": index}
        sources = search._rebuild_sources(
            [
                {"name": "Remote mini", "book": "medicine_mini_2025-10"},
                {"name": "Remote nopic", "book": "medicine_nopic_2025-10"},
                {"name": "Other mini", "book": "other_mini_2025-10"},
                {"name": "Other nopic", "book": "other_nopic_2025-10"},
            ]
        )
        self.assertEqual(
            sorted(source["key"] for source in sources),
            ["medicine_maxi", "other_mini"],
        )
        self.assertEqual(
            [source["name"] for source in sources if source["key"].startswith("other")],
            ["Other mini"],
        )
        search._executor.shutdown(wait=True)

    def test_remote_catalog_does_not_overwrite_a_local_source_identity(self):
        search = _bare_search({})
        search.indexes = {
            "manual": types.SimpleNamespace(
                source_size=None, ensure_open=lambda: None
            )
        }
        sources = search._rebuild_sources(
            [{"name": "Remote Manual", "book": "manual_2024-01"}]
        )
        self.assertEqual([source["name"] for source in sources], ["manual"])
        self.assertEqual(sources[0]["book"], "manual")
        search._executor.shutdown(wait=True)

    def test_versioned_local_sources_collapse_to_one_book(self):
        search = _bare_search({})
        index = types.SimpleNamespace(
            source_size=None, ensure_open=lambda: None
        )
        # Dated and undated copies of one book are not two sources.
        search.indexes = {"foo_2024-01": index, "foo": index}
        sources = search._rebuild_sources([])
        self.assertEqual([source["key"] for source in sources], ["foo"])
        self.assertEqual([source["name"] for source in sources], ["foo"])
        search._executor.shutdown(wait=True)

    def test_language_variants_of_local_sources_stay_separate(self):
        search = _bare_search({})
        index = types.SimpleNamespace(
            source_size=None, ensure_open=lambda: None
        )
        search.indexes = {"foo_en_2024-01": index, "foo_mul_2024-01": index}
        sources = search._rebuild_sources([])
        self.assertEqual(
            sorted(source["key"] for source in sources), ["foo_en", "foo_mul"]
        )
        search._executor.shutdown(wait=True)

    def test_book_identity_strips_dates_and_flavours(self):
        self.assertEqual(
            _book_identity("wikipedia_en_medicine_mini_2025-10"),
            "wikipedia_en_medicine",
        )
        self.assertEqual(
            _book_identity("wikipedia_en_medicine_nopic_2025-10"),
            "wikipedia_en_medicine",
        )
        self.assertEqual(
            _book_identity("wikipedia_en_top_mini_2026-09"),
            "wikipedia_en_top",
        )
        self.assertNotEqual(
            _book_identity("phet_en_all_2025-03"),
            _book_identity("phet_mul_all_2025-06"),
        )

    def test_unknown_display_filter_falls_back_to_the_full_page(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            search = self._search(tmp)
            try:
                events = list(
                    search.stream_search(
                        "tire change",
                        zim=["removed-source"],
                        source=["removed-source"],
                        limit=2,
                    )
                )
                self.assertTrue(events[-1]["results"])
                self.assertEqual(events[-1]["total"], 6)
            finally:
                search._executor.shutdown(wait=True)

    def test_small_selection_searches_only_those_sources_and_is_not_cached(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            search = self._search(tmp)
            try:
                events = list(
                    search.stream_search("tire change", zim=["s1", "s2"], limit=2)
                )
                started = events[0]
                self.assertTrue(started["selective"])
                self.assertFalse(started["all_sources"])
                self.assertEqual(started["total_sources"], 2)
                self.assertEqual(
                    {source["key"] for source in started["sources"]},
                    {"s1", "s2"},
                )
                self.assertIsNone(
                    search.cache.get(search._cache_key("tire change", ("s1", "s2")))
                )
            finally:
                search._executor.shutdown(wait=True)

    def test_large_selection_promotes_to_full_cached_scope(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            search = self._search(tmp)
            try:
                events = list(
                    search.stream_search(
                        "tire change",
                        zim=["s1", "s2", "s3"],
                        limit=2,
                    )
                )
                started = events[0]
                self.assertFalse(started["selective"])
                self.assertTrue(started["all_sources"])
                self.assertEqual(started["total_sources"], 6)
                self.assertIsNotNone(
                    search.cache.get(search._cache_key("tire change", None))
                )

                # Every promoted selection shares that full-search pool.
                with patch.object(
                    search,
                    "_search_source",
                    side_effect=AssertionError("cache miss"),
                ):
                    cached = list(
                        search.stream_search(
                            "tire change",
                            zim=["s4", "s5", "s6"],
                            limit=2,
                        )
                    )
                self.assertFalse(cached[0]["selective"])
                self.assertTrue(cached[0]["all_sources"])
            finally:
                search._executor.shutdown(wait=True)


class ProductionHardeningTests(unittest.TestCase):
    def test_article_path_preserves_literal_percent_escapes(self):
        # The ASGI layer decodes the request path once. A second decode here
        # would turn "%23" into "#" and make real ZIM entries unreachable.
        path = (
            "content/biz.libretexts.org/@api/deki/files/667/"
            "mindtouch.page%23thumbnail?revision=2"
        )
        self.assertEqual(Search._article_path(path), path)

    def _db(self):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "title, path UNINDEXED, target UNINDEXED)"
        )
        db.execute(
            "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
            (1, "Tire Change", "tire-change", None),
        )
        return db

    def test_local_results_use_content_route(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        search = object.__new__(Search)
        search.cfg = {}
        search.preview_chars = 100
        db = self._db()
        try:
            doc = search._fetch_docs(source, db, {1})[1]
        finally:
            db.close()
        self.assertEqual(doc["url"], "/content/manual/tire-change")
        self.assertEqual(doc["path"], "tire-change")

    def test_local_results_use_web_base_path(self):
        source = SourceInfo("manual", "Manual", "manual", "local")
        search = object.__new__(Search)
        search.cfg = {"web_base_path": "zimantic"}
        search.preview_chars = 100
        db = self._db()
        try:
            doc = search._fetch_docs(source, db, {1})[1]
        finally:
            db.close()
        self.assertEqual(doc["url"], "/zimantic/content/manual/tire-change")

    def test_source_icon_urls_use_web_base_path(self):
        self.assertEqual(
            _source_icon_url("manual", True, "/zimantic"),
            "/zimantic/source-icon/manual",
        )
        self.assertIsNone(_source_icon_url("manual", False, "/zimantic"))

    def test_exhausted_search_slots_report_busy_without_blocking(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search._search_slots = threading.BoundedSemaphore(1)
        self.assertTrue(search._search_slots.acquire(blocking=False))
        try:
            events = list(search.stream_search("anything", limit=1))
            with self.assertRaises(SearchBusyError):
                search.search_page("anything", limit=1)
        finally:
            search._search_slots.release()
            search._executor.shutdown(wait=True)
        self.assertEqual(events[-1]["type"], "error")
        self.assertTrue(events[-1]["busy"])

    def test_public_results_hide_internal_rowids(self):
        doc = {"id": "a:b", "path": "b", "_rowid": 7, "score": 1.0}
        self.assertNotIn("_rowid", Search._public(doc, False))
        self.assertNotIn("_rowid", Search._public(doc, True))
        self.assertIn("_rowid", doc)

    def test_slot_released_before_waiting_is_acquired_without_delay(self):
        class _Slots:
            def __init__(self):
                self.calls = 0

            def acquire(self, blocking=False):
                self.calls += 1
                return self.calls > 1

        search = _bare_search({})
        search._search_slots = _Slots()
        search._slot_condition = threading.Condition()
        self.assertTrue(search._acquire_search_slot())
        self.assertEqual(search._search_slots.calls, 2)
        search._executor.shutdown(wait=True)

    def test_claim_search_reports_busy_when_the_owner_never_finishes(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        stuck = Future()
        search._inflight[("gen", "key")] = stuck
        with patch("zimantic.search.SEARCH_CLAIM_WAIT_SECONDS", 0.01):
            cached, pending = search._claim_search("key", ("gen", "key"), "gen")
        self.assertIsNone(cached)
        self.assertIsNone(pending)
        search._executor.shutdown(wait=True)

    def test_missing_zim_path_is_not_reported_as_exact(self):
        with _temp_dir(self) as tmp:
            search = _bare_search({"zim_dir": str(Path(tmp) / "missing")})
            try:
                self.assertEqual(
                    search._match_zim_path("manual", None), (None, False)
                )
            finally:
                search._executor.shutdown(wait=True)

    def test_phrase_matching_ignores_script_and_style_bodies(self):
        html = (
            '<html><script>var x = "very hidden phrase";</script>'
            "<style>.very.hidden.phrase{color:red}</style>"
            "<p>Visible text mentions none of it.</p></html>"
        )
        self.assertFalse(
            _phrase_in_word_stream(
                html.casefold(), ["very", "hidden", "phrase"]
            )
        )

    def test_health_does_not_call_deferred_vectors_degraded(self):
        with _temp_dir(self) as tmp:
            archive = Path(tmp) / "manual.zim"
            archive.write_bytes(b"zim")
            search = _bare_search({})
            search._defer_faiss = True
            search.indexes = {
                "manual": _LocalIndex(
                    None,
                    None,
                    None,
                    None,
                    threading.Lock(),
                    done="1",
                    has_fulltext_index=False,
                    zim_path=str(archive),
                )
            }
            fake_archive = types.SimpleNamespace(entry_count=1)
            try:
                with (
                    patch.object(
                        _LocalIndex, "ensure_open", return_value=fake_archive
                    ),
                    patch.object(
                        Search,
                        "_resolve_article_entry",
                        return_value=types.SimpleNamespace(path="index"),
                    ),
                ):
                    report = search.health()
                    self.assertEqual(report["indexes"][0]["status"], "ok")
                    search._defer_faiss = False
                    report = search.health()
                    self.assertEqual(report["indexes"][0]["status"], "degraded")
                    self.assertIn(
                        "semantic vectors unavailable",
                        report["indexes"][0]["issues"],
                    )
            finally:
                search._executor.shutdown(wait=True)

    def test_unavailable_native_index_reports_an_error_not_an_empty_ok(self):
        source = SourceInfo(
            "manual", "Manual", "manual", "local", local_name="manual"
        )
        index = _LocalIndex(
            None,
            None,
            None,
            None,
            threading.Lock(),
            open_failed=True,
            error="cannot open archive",
            zim_path="/zims/manual.zim",
        )
        search = _bare_search({})
        search.indexes = {"manual": index}
        try:
            result = search._search_local(source, "query", "", "query", None, 10)
        finally:
            search._executor.shutdown(wait=True)
        self.assertEqual(result.status, "error")
        self.assertIn("cannot open archive", result.error)
        self.assertEqual(result.items, [])


class CatalogLocationTests(unittest.TestCase):
    def test_page_size_is_clamped_to_the_result_pool(self):
        with _temp_dir(self) as tmp:
            root = Path(tmp)
            index_dir = root / "indexes"
            index_dir.mkdir()
            cfg = {
                "index_dir": str(index_dir),
                "zim_dir": str(root / "zims"),
                "candidate_count": 16,
                "page_size": 50,
                "max_results": 10,
            }
            search = Search(cfg, semantic=False, archive_factory=object)
            self.addCleanup(search.close)
            self.assertEqual(search.page_size, 10)

    def test_search_puts_catalog_beside_the_index_directory(self):
        with _temp_dir(self) as tmp:
            root = Path(tmp)
            index_dir = root / "indexes"
            index_dir.mkdir()
            _write_index(
                index_dir,
                "manual",
                rows=[(1, "Tire Change", "tire-change", None)],
            )
            cfg = {
                "index_dir": str(index_dir),
                "zim_dir": str(index_dir),
                "candidate_count": 16,
                "page_size": 10,
                "max_results": 100,
            }
            search = Search(cfg, semantic=False, archive_factory=object)
            self.addCleanup(search.close)
            self.assertTrue((root / "catalog.sqlite").exists())
            self.assertFalse((index_dir / "catalog.sqlite").exists())


class QueryCacheTests(unittest.TestCase):
    def test_lru_evicts_least_recently_used(self):
        cache = QueryCache(2)
        cache.put("a", [1])
        cache.put("b", [2])
        self.assertEqual(cache.get("a"), [1])
        cache.put("c", [3])
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("c"), [3])

    def test_disabled_cache_is_a_noop(self):
        cache = QueryCache(0)
        cache.put("a", [1])
        self.assertIsNone(cache.get("a"))

    def test_byte_budget_evicts_large_entries(self):
        cache = QueryCache(100, max_bytes=2000)
        cache.put("a", "x" * 1000)
        cache.put("b", "y" * 1000)

        self.assertLessEqual(cache.stats()["bytes"], 2000)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), "y" * 1000)

    def test_byte_budget_can_be_disabled(self):
        cache = QueryCache(100, max_bytes=0)
        cache.put("a", "x" * 10_000)
        self.assertEqual(cache.get("a"), "x" * 10_000)

    def test_oversized_entry_is_dropped_without_emptying_the_cache(self):
        cache = QueryCache(10, max_bytes=1000)
        cache.put("small", "x" * 100)
        cache.put("huge", "y" * 5000)

        # The oversized entry can never fit, so it is dropped; the rest survives.
        self.assertEqual(cache.stats()["size"], 1)
        self.assertIsNone(cache.get("huge"))
        self.assertEqual(cache.get("small"), "x" * 100)
        self.assertLessEqual(cache.stats()["bytes"], 1000)

        # Later inserts still work (the cache was not left permanently empty).
        cache.put("small2", "z" * 50)
        self.assertIsNotNone(cache.get("small"))
        self.assertIsNotNone(cache.get("small2"))


if __name__ == "__main__":
    unittest.main()
