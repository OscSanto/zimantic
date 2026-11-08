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
from zimantic.contracts import SourceInfo, SourceResult, encode_document_id
from zimantic.search import (
    PREVIEW_HTML_BYTES,
    Search as _SearchBase,
    SearchBusyError,
    SearchQueryError,
    _fulltext_query,
    _phrase_words,
    _query_terms,
    _title_query,
)
from zimantic.searching.query import _title_prefix_query
from zimantic.providers.zim.metadata import (
    find_archive_icon_path as _find_archive_icon_path,
    source_icon_url as _source_icon_url,
    source_info_from_metadata as _source_info_from_metadata,
)
from zimantic.providers.zim.search import (
    phrase_in_word_stream as _phrase_in_word_stream,
)
from zimantic.searching.index import _LocalIndex, _nprobe
from zimantic.searching.config import _file_signature
from zimantic.searching.sources import _html_attribute


class Search(_SearchBase):
    """Test-local subclass; production ``Search`` stays provider-neutral."""


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
    search.preview_limit = cfg.get("preview_limit", search.page_size)
    search.max_results = cfg.get("max_results", 100)
    search.source_workers = 2
    search.max_concurrent_searches = 2
    search._search_slots = threading.BoundedSemaphore(2)
    search._slot_waiters = threading.BoundedSemaphore(2)
    search._executor = ThreadPoolExecutor(max_workers=2)
    search._inflight = {}
    search._inflight_lock = threading.Lock()
    search.cache = QueryCache(16)
    search.runtimes = {"": _TestZimRuntime(search)}
    search.collections = {}
    search._collection_snapshots = {}
    search._library_source_index = {}
    search._discovery_batches = []
    search._process_token = "test"
    search._aggregate_revision = 0
    search.runtime_config = None
    search.providers = {}
    search._defer_faiss = False
    search.fulltext_pool = None
    return search


def _zim_source(key, title, **kwargs):
    kwargs.setdefault("searchable", True)
    return SourceInfo(key=key, provider="zim", title=title, **kwargs)


class _TestCollection:
    key = ""
    provider = "zim"
    label = "Library"


class _TestZimRuntime:
    """Minimal runtime that exercises the real ZIM search module in tests."""

    def __init__(self, search):
        self.core = search
        self.collection = _TestCollection()

    @property
    def indexes(self):
        return self.core.indexes

    @property
    def sources(self):
        return list(self.core.sources.values())

    def _ctx(self):
        from zimantic.providers.zim.search import ZimSearchContext

        return ZimSearchContext(
            candidate_count=self.core.candidate_count,
            preview_chars=self.core.preview_chars,
            max_html_bytes=self.core.max_html_bytes,
            max_article_bytes=self.core.max_article_bytes,
            min_cosine_similarity=0.85,
            web_base_path=self.core.web_base_path,
            fulltext_pool=self.core.fulltext_pool,
            preview_cache=getattr(self.search, "preview_cache", None),
            cfg=self.core.cfg,
        )

    def search_source(self, source, request):
        from zimantic.providers.zim import search as zim_search

        return zim_search.search(self, source, request, self._ctx())

    def search(self, source, request):
        return self.search_source(source, request)

    def suggest(self, source, query, limit):
        from zimantic.providers.zim.search import suggest as zim_suggest

        return zim_suggest(self, source, query, limit, self._ctx())

    def preview(self, source, document, *, max_chars, max_bytes, deadline):
        from zimantic.providers.zim.search import preview as zim_preview

        return zim_preview(
            self,
            source,
            document,
            max_chars=max_chars,
            max_bytes=max_bytes,
            deadline=deadline,
            ctx=self._ctx(),
        )

    def enrich(
        self,
        source,
        result_docs,
        source_docs,
        *,
        phrase_ids,
        phrase_words,
        max_html_bytes,
        max_article_bytes,
    ):
        from zimantic.providers.zim.search import enrich as zim_enrich

        return zim_enrich(
            self,
            source,
            result_docs,
            source_docs,
            phrase_ids=phrase_ids,
            phrase_words=phrase_words,
            max_html_bytes=max_html_bytes,
            max_article_bytes=max_article_bytes,
        )

    def library_sources(self):
        return list(self.core.sources.values())

    @property
    def snapshot(self):
        from zimantic.providers.base import CollectionSnapshot

        return CollectionSnapshot(
            collection=self.collection,
            source_count=len(self.core.sources),
            media_types=(),
            error=None,
            discovery_state="ready",
            snapshot_version=1,
        )

    @property
    def snapshot_version(self):
        return 1

    @property
    def discovery_state(self):
        return "ready"

    def health(self):
        reports = []
        for key, index in self.core.indexes.items():
            reasons = []
            vectors_expected = (
                self.core.semantic
                and index.done == "full"
                and not self.core._defer_faiss
            )
            vectors_loaded = index.faiss_index is not None
            if vectors_expected and not vectors_loaded:
                reasons.append("semantic vectors unavailable")
            archive_current = bool(index.zim_path)
            if not archive_current:
                reasons.append("ZIM archive unavailable")
            report = {
                "name": key,
                "status": "ok" if not reasons else "degraded",
                "index_file": index.db_path is not None,
                "archive": archive_current,
                "capabilities": {
                    "title": index.db_path is not None,
                    "fulltext": True,
                    "semantic": vectors_loaded,
                    "article": archive_current,
                },
            }
            if reasons:
                report["issues"] = reasons
            reports.append(report)
        return reports

    def max_sources_per_search(self):
        return None

    def refresh(self, *, wait=True):
        return True

    def close(self):
        pass


class FulltextFailureTests(unittest.TestCase):
    def test_native_fulltext_failure_is_reported_as_partial(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        source = _zim_source("manual", "Manual")
        search.fulltext_pool = types.SimpleNamespace(
            search=lambda *_args: None,
            close=lambda: None,
        )
        archive = types.SimpleNamespace(has_fulltext_index=True)
        index = _LocalIndex(
            None,
            None,
            archive,
            None,
            threading.Lock(),
            zim_path="/tmp/manual.zim",
            has_fulltext_index=True,
        )

        result = search._search_local_locked(
            source,
            "tires",
            "tires",
            "tires",
            None,
            10,
            index,
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.error, "full-text search unavailable")
        self.assertEqual(result.error, "full-text search unavailable")

    def test_isolated_fulltext_results_do_not_open_archive_in_parent(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        source = _zim_source("manual", "Manual")
        search.fulltext_pool = types.SimpleNamespace(
            search_results=lambda *_args: [
                {"path": "article", "title": "Article"}
            ],
            close=lambda: None,
        )
        index = _LocalIndex(
            None,
            None,
            None,
            None,
            threading.Lock(),
            zim_path="/tmp/manual.zim",
            has_fulltext_index=True,
        )

        result = search._search_local_locked(
            source,
            "article",
            "",
            "article",
            None,
            10,
            index,
        )

        self.assertEqual(result.fulltext[0][1]["path"], "article")
        self.assertEqual(result.fulltext[0][1]["title"], "Article")
        self.assertIsNone(index.archive)


class _CountingSemaphore(threading.BoundedSemaphore):
    """BoundedSemaphore that records successful releases for assertions."""

    def __init__(self, value):
        super().__init__(value)
        self.releases = 0

    def release(self):
        super().release()
        self.releases += 1


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
    source = _zim_source("manual", "Manual")
    search = _bare_search({"candidate_count": 2})
    search.semantic = False
    search.sources = {"manual": source}
    search.indexes = {"manual": _LocalIndex(db, None, None, None, threading.Lock())}
    try:
        events = list(search.stream_search(query, limit=limit))
    finally:
        search.close()
    return events[-1]["results"]


def _title_suggest(rows, query, limit=2):
    """Run the autocomplete path against an in-memory title index."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute(
        "CREATE VIRTUAL TABLE docs USING fts5("
        "title, path UNINDEXED, target UNINDEXED)"
    )
    db.executemany(
        "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
        rows,
    )
    source = _zim_source("manual", "Manual")
    search = _bare_search({"candidate_count": 2})
    search.semantic = False
    search.sources = {"manual": source}
    search.indexes = {"manual": _LocalIndex(db, None, None, None, threading.Lock())}
    try:
        return search.suggest(query, ["manual"], limit)
    finally:
        search.close()


class SearchContractTests(unittest.TestCase):
    def test_source_info_metadata_fallback_uses_name_and_bool_capability(self):
        source = _source_info_from_metadata(
            {
                "name": "Display Name",
                "native_fulltext_available": 1,
                "size": 10,
                "item_count": 2,
            },
            key="manual",
            provider="zim",
            searchable=True,
            icon_url=None,
        )
        self.assertEqual(source.title, "Display Name")
        self.assertIs(source.native_fulltext_available, True)
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

        source = _zim_source("manual", "Manual")
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
        search.runtimes = {"": _TestZimRuntime(search)}
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

    def test_spanish_question_normalizes_stopwords_and_accents(self):
        query = "¿qué es la química?"
        self.assertEqual(_title_query(query), '"quimica"')
        self.assertEqual(_fulltext_query(query), "quimica")
        self.assertEqual(_query_terms(query), ["quimica"])

    def test_spanish_query_normalizes_stopwords_and_common_inflections(self):
        self.assertEqual(
            _query_terms("impacto de la IA en la era moderna"),
            ["impacto", "ia", "moderna"],
        )
        self.assertEqual(
            _query_terms("tipos de peces beta"),
            ["tipo", "pez", "beta"],
        )

    def test_stopword_only_query_is_not_filtered_away(self):
        self.assertEqual(_title_query("how to"), '"how" AND "to"')
        self.assertEqual(_fulltext_query("how to"), "how to")
        self.assertEqual(_title_query("the"), '"the"')

    def test_one_letter_contraction_fragments_are_dropped(self):
        # "can't" tokenizes to "can" + "t"; the stray "t" is not a Xapian term
        # and used to make the whole full-text query match nothing.
        self.assertEqual(_fulltext_query("can't keep it uncovered"), "keep uncovered")
        self.assertEqual(_fulltext_query("C"), "c")

    def test_title_prefix_query_uses_prefix_tokens_except_single_characters(self):
        self.assertEqual(_title_prefix_query("how to change tires"), "change* AND tires*")
        self.assertEqual(_title_prefix_query("praise of fol"), "praise* AND fol*")
        self.assertEqual(_title_prefix_query("c"), '"c"')

    def test_cache_key_normalizes_query_expanded_keys_and_generation(self):
        search = _bare_search({})
        try:
            search._cache_generation = 1
            self.assertEqual(
                search._cache_key("How  TO change", ("beta", "alpha")),
                search._cache_key("how to change", ("alpha", "beta")),
            )
            self.assertNotEqual(
                search._cache_key("q", ("manual",)),
                search._cache_key("q", ("other",)),
            )
            search._cache_generation = 2
            self.assertNotEqual(
                search._cache_key("q", ("manual",)),
                search._cache_key("q", ("manual",), 1),
            )
        finally:
            search._executor.shutdown(wait=True)

    def test_scoped_search_and_cache_changes_invalidate_only_the_relevant_scope(self):
        from zimantic.contracts import SourceInfo as CanonicalSourceInfo

        source = CanonicalSourceInfo(
            "files:media:root", provider="files", collection_key="files:media"
        )
        other = CanonicalSourceInfo(
            "files:other:root", provider="files", collection_key="files:other"
        )
        search = _bare_search(
            {
                "collection_settings": {"media": {"candidate_count": 2}},
                "source_settings": {source.key: {"cache_size": 1}},
            }
        )
        try:
            search._cache_generation = 1
            search.sources = {source.key: source, other.key: other}
            selected = search._cache_key("alpha", (source.key,))
            unrelated = search._cache_key("alpha", (other.key,))
            search.cfg["source_settings"][source.key]["nprobe"] = 5
            self.assertNotEqual(search._cache_key("alpha", (source.key,)), selected)
            self.assertEqual(search._cache_key("alpha", (other.key,)), unrelated)
            self.assertEqual(search._source_limit(100, source), 2)
            self.assertEqual(search._source_limit(100, other), 16)
            key = search._cache_key("alpha", (source.key,))
            search.cache.put(
                key, "result", size=1, quotas=search._cache_quotas([source])
            )
            search.cfg["source_settings"][source.key]["cache_size"] = 0
            disabled_key = search._cache_key("alpha", (source.key,))
            search.cache.put(
                disabled_key, "result", size=1, quotas=search._cache_quotas([source])
            )
            self.assertIsNone(search.cache.get(disabled_key))
        finally:
            search._executor.shutdown(wait=True)

    def test_scoped_source_deadlines_are_capped_and_late_results_are_cancelled(self):
        from types import SimpleNamespace

        from zimantic.contracts import (
            SourceInfo as CanonicalSourceInfo,
        )
        from zimantic.contracts import (
            SourceResult as CanonicalSourceResult,
        )

        source = CanonicalSourceInfo(
            "files:media:root", provider="files", collection_key="files:media"
        )
        search = _bare_search(
            {"source_settings": {source.key: {"source_timeout": 0.25}}}
        )
        requests = []

        def execute(source, request):
            requests.append(request)
            return CanonicalSourceResult(source)

        search.runtimes = {"files:media": SimpleNamespace(search=execute)}
        try:
            with patch("zimantic.search.time.monotonic", side_effect=[100, 100]):
                result = search._search_source(source, "alpha", "", "", None, 4, 200)
            self.assertEqual(requests[-1].deadline, 100.25)
            self.assertNotEqual(result.status, "cancelled")
            with patch("zimantic.search.time.monotonic", side_effect=[100, 101]):
                result = search._search_source(source, "alpha", "", "", None, 4, 100.1)
            self.assertEqual(requests[-1].deadline, 100.1)
            self.assertEqual(result.status, "cancelled")
        finally:
            search._executor.shutdown(wait=True)

    def test_scoped_preview_limits_and_disabled_quota_apply_to_hydration(self):
        from types import SimpleNamespace

        from zimantic.contracts import (
            SourceInfo as CanonicalSourceInfo,
        )
        from zimantic.contracts import (
            SourceResult as CanonicalSourceResult,
        )

        source = CanonicalSourceInfo(
            "files:media:root", provider="files", collection_key="files:media"
        )
        search = _bare_search(
            {
                "source_settings": {
                    source.key: {
                        "max_preview_chars": 3,
                        "max_html_bytes": 4,
                        "cache_size": 0,
                    }
                }
            }
        )
        calls = []

        def preview(source, doc, *, max_chars, max_bytes, deadline):
            calls.append((max_chars, max_bytes))
            return "alpha"[:max_chars], "text/plain"

        def enrich(*args, **kwargs):
            self.assertEqual(kwargs["max_html_bytes"], 4)
            return False

        search.sources = {source.key: source}
        search.runtimes = {
            source.collection_key: SimpleNamespace(preview=preview, enrich=enrich)
        }
        search.preview_cache = QueryCache(16)
        doc = {
            "id": "item",
            "path": "alpha",
            "title": "Alpha",
            "source_key": source.key,
        }
        try:
            search._hydrate_results([doc])
            self.assertEqual(doc["lead"], "alp")
            doc["lead"] = ""
            search._hydrate_results([doc])
            self.assertEqual(calls, [(3, 4), (3, 4)])
            self.assertEqual(search.preview_cache.stats()["size"], 0)
            search._enrich_pool(
                [CanonicalSourceResult(source, items=[doc])], "alpha", [doc], 1
            )
        finally:
            search._executor.shutdown(wait=True)

    def test_hybrid_ranking_is_deterministic(self):
        source = _zim_source("manual", "Manual")
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
        semantic_source = _zim_source("semantic", "Semantic")
        title_source = _zim_source("titles", "Titles")
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
        source = _zim_source("manual", "Manual")
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
        source = _zim_source("manual", "Manual")
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
        source = _zim_source("manual", "Manual")
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
            _zim_source("one", "One"),
            _zim_source("two", "Two"),
        ]
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16
        search.semantic = False
        search.indexes = {}
        search.sources = {source.key: source for source in sources}
        search._state_lock = threading.RLock()
        search._reload_lock = threading.Lock()
        search.preview_limit = 10
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
                "source": source.title,
                "title": f"{source.title} page",
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
            _zim_source(f"source-{index}", f"Source {index}")
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
                "source": source.title,
                "title": source.title,
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
            _zim_source(f"source-{index}", f"Source {index}")
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
                "source": source.title,
                "title": source.title,
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

        def submit(_fn, _stop, source, *_args, **_kwargs):
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
            _zim_source(f"source-{index}", f"Source {index}")
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

        def submit(_fn, _stop, source, *_args, **_kwargs):
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

        source = _zim_source("manual", "Manual")
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 2}
        search.candidate_count = 2
        search.semantic = True
        search.sources = {"manual": source}
        search.indexes = {
            "manual": _LocalIndex(db, FakeFaiss(), None, None, threading.Lock())
        }
        search.runtimes = {"": _TestZimRuntime(search)}
        search._state_lock = threading.RLock()
        search._reload_lock = threading.Lock()
        search.preview_limit = 10
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
        source = _zim_source("manual", "Manual")
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

        source = _zim_source("manual", "Manual")
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

    def test_semantic_results_below_cosine_threshold_are_ignored_when_lexical_matches_exist(self):
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
                    np.array([[0.90, 0.84]]),
                    np.array([[2, 1]]),
                )

        source = _zim_source("manual", "Manual")
        search = object.__new__(Search)
        search.cfg = {
            "candidate_count": 2,
            "min_cosine_similarity": 0.85,
        }
        search.candidate_count = 2
        result = search._search_local_locked(
            source,
            "above",
            _title_query("above"),
            _fulltext_query("above"),
            np.array([[1.0]]),
            1,
            _LocalIndex(db, FakeFaiss(), None, None, threading.Lock()),
        )

        db.close()
        self.assertEqual([item["title"] for item in result.items], ["Above threshold"])
        self.assertEqual(result.semantic[0][0], 0.90)

    def test_meaning_only_results_keep_candidates_below_the_cosine_floor(self):
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
                    np.array([[0.90, 0.84]]),
                    np.array([[2, 1]]),
                )

        source = _zim_source("manual", "Manual")
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
        # No title or full-text candidate: dropping the weak meaning match
        # would return nothing at all, so it is kept.
        self.assertEqual(
            [item["title"] for item in result.items],
            ["Above threshold", "Below threshold"],
        )
        self.assertEqual([score for score, _ in result.semantic], [0.90, 0.84])

    def test_local_result_preview_is_extracted_for_display(self):
        class _Entry:
            _index = 1

        class _Archive:
            def get_entry_by_path(self, path):
                self.path = path
                return _Entry()

        source = _zim_source("manual", "Manual")
        search = object.__new__(Search)
        search.cfg = {
            "max_html_bytes": 4_194_304,
            "max_article_bytes": 33_554_432,
            "max_preview_chars": 24,
        }
        search.preview_chars = 24
        search.preview_limit = 10
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
        search.runtimes = {"": _TestZimRuntime(search)}
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
                "zimantic.providers.zim.search.read_entry",
                return_value=(1, "Long page", "one " * 400, "long-page", None),
            ) as read:
                search._hydrate_results([doc])
        finally:
            search._executor.shutdown(wait=True)

        self.assertLessEqual(len(doc["lead"]), 24)
        self.assertTrue(doc["lead"].endswith("one"))
        self.assertEqual(
            read.call_args.kwargs["max_html_bytes"], PREVIEW_HTML_BYTES
        )

    def test_preview_hydration_is_bounded(self):
        class _Entry:
            _index = 1

        class _Archive:
            def get_entry_by_path(self, path):
                return _Entry()

        source = _zim_source("manual", "Manual")
        search = object.__new__(Search)
        search.cfg = {
            "max_html_bytes": 4_194_304,
            "max_article_bytes": 33_554_432,
            "max_preview_chars": 24,
        }
        search.preview_chars = 24
        search.preview_limit = 1
        search.sources = {"manual": source}
        search.indexes = {
            "manual": _LocalIndex(None, None, _Archive(), None, threading.Lock())
        }
        search.runtimes = {"": _TestZimRuntime(search)}
        search._state_lock = threading.RLock()
        search._executor = ThreadPoolExecutor(max_workers=1)
        search.preview_cache = QueryCache(16)
        docs = [
            {"id": "manual:first", "source_key": "manual", "lead": "", "path": "first"},
            {"id": "manual:second", "source_key": "manual", "lead": "", "path": "second"},
        ]

        try:
            with patch(
                "zimantic.providers.zim.search.read_entry",
                return_value=(1, "First", "first preview", "first", None),
            ) as read:
                search._hydrate_results(docs)
        finally:
            search._executor.shutdown(wait=True)

        self.assertEqual(read.call_count, 1)
        self.assertEqual(docs[0]["lead"], "first preview")
        self.assertEqual(docs[1]["lead"], "")

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

    def test_suggest_returns_title_prefix_matches_with_article_urls(self):
        rows = [
            (1, "Tire", "Tire", None),
            (2, "Tire Changes", "Tire_Changes", None),
            (3, "Tiredness", "Tiredness", None),
            (4, "Wheel", "Wheel", None),
        ]

        suggestions = _title_suggest(rows, "tire", limit=3)

        self.assertEqual(
            {item["title"] for item in suggestions},
            {"Tire", "Tire Changes", "Tiredness"},
        )
        by_title = {item["title"]: item for item in suggestions}
        self.assertEqual(by_title["Tire Changes"]["path"], "Tire_Changes")
        self.assertEqual(by_title["Tire Changes"]["id"], encode_document_id("manual", "Tire_Changes"))
        self.assertEqual(
            by_title["Tire Changes"]["url"], "/content/manual/Tire_Changes"
        )

    def test_suggest_matches_partial_words_and_respects_limit(self):
        rows = [
            (1, "Tire Changes", "Tire_Changes", None),
            (2, "Tire Repair", "Tire_Repair", None),
        ]

        suggestions = _title_suggest(rows, "tire cha", limit=2)
        self.assertEqual([item["title"] for item in suggestions], ["Tire Changes"])

        limited = _title_suggest(rows, "tire", limit=1)
        self.assertEqual(len(limited), 1)
        self.assertIn(limited[0]["title"], {"Tire Changes", "Tire Repair"})

    def test_suggest_returns_no_matches_for_unrelated_query(self):
        rows = [(1, "Tire Changes", "Tire_Changes", None)]

        self.assertEqual(_title_suggest(rows, "wheel", limit=3), [])

    def test_suggest_deduplicates_redirects_that_resolve_to_one_article(self):
        rows = [
            (1, "Tires", "Tires", 2),
            (2, "Tire", "Tire", None),
        ]

        suggestions = _title_suggest(rows, "tir", limit=8)

        self.assertEqual([item["id"] for item in suggestions], [encode_document_id("manual", "Tire")])

    def test_suggest_fills_limit_when_redirects_collapse(self):
        rows = [
            (1, "Tires", "Tires", 5),
            (2, "Tire", "Tire", 5),
            (3, "Tired", "Tired", 5),
            (4, "Tireless", "Tireless", 5),
            (5, "Tire", "Tire", None),
            (6, "Tire Changes", "Tire_Changes", None),
            (7, "Tire Repair", "Tire_Repair", None),
        ]

        suggestions = _title_suggest(rows, "tir", limit=3)

        self.assertEqual(
            {item["id"] for item in suggestions},
            {
                encode_document_id("manual", "Tire"),
                encode_document_id("manual", "Tire_Changes"),
                encode_document_id("manual", "Tire_Repair"),
            },
        )

    def test_suggest_stays_scoped_and_rejects_unknown_source(self):
        manual_db = sqlite3.connect(":memory:", check_same_thread=False)
        other_db = sqlite3.connect(":memory:", check_same_thread=False)
        for db, row in (
            (manual_db, (1, "Tire Changes", "Tire_Changes", None)),
            (other_db, (1, "Tire Pressure", "Tire_Pressure", None)),
        ):
            db.execute(
                "CREATE VIRTUAL TABLE docs USING fts5("
                "title, path UNINDEXED, target UNINDEXED)"
            )
            db.execute(
                "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
                row,
            )
        search = _bare_search({"candidate_count": 2})
        search.semantic = False
        search.sources = {
            "manual": _zim_source("manual", "Manual"),
            "other": _zim_source("other", "Other"),
        }
        search.indexes = {
            "manual": _LocalIndex(manual_db, None, None, None, threading.Lock()),
            "other": _LocalIndex(other_db, None, None, None, threading.Lock()),
        }
        try:
            scoped = search.suggest("tire", ["manual"], limit=8)
            self.assertEqual([item["id"] for item in scoped], [encode_document_id("manual", "Tire_Changes")])
            with self.assertRaises(SearchQueryError):
                search.suggest("tire", ["missing"], limit=8)
        finally:
            search.close()


class DisambiguationRankingTests(unittest.TestCase):
    def _search(self):
        search = object.__new__(Search)
        search.cfg = {"candidate_count": 16}
        search.candidate_count = 16
        return search

    def _result(self):
        source = _zim_source("manual", "Manual")
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
        source = _zim_source("manual", "Manual")
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


def _write_index(index_dir: Path, name: str, *, done: str = "full", rows=()):
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

    def test_embedding_model_warning_includes_model_file_path(self):
        with _temp_dir(self) as tmp:
            model_path = Path(tmp) / "model" / "model.onnx"
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._embedder_ready = threading.Event()

            def fail_to_load():
                raise RuntimeError(
                    f"Load model from {model_path} failed: File doesn't exist"
                )

            with self.assertLogs("zimantic.search", level="WARNING") as logs:
                search.start_embedder(fail_to_load)
                self.assertTrue(search._embedder_ready.wait(timeout=2))

            self.assertTrue(any(str(model_path) in line for line in logs.output))

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

    def test_matching_probes_allow_vectors_from_another_model_file(self):
        class _ProbeEmbedder:
            def __init__(self, vectors):
                self.vectors = vectors

            def embed(self, _texts):
                return self.vectors

        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            db_path = _write_index(
                tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)]
            )
            probes = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32)
            db = sqlite3.connect(db_path)
            db.execute("INSERT INTO meta VALUES ('model_fingerprint', 'old-model')")
            from zimantic.embed import serialize_model_probes

            for key, value in serialize_model_probes(
                ["first probe", "second probe"], probes
            ).items():
                db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            db.commit()
            db.close()
            db_path.with_suffix(".faiss").write_bytes(b"vectors")

            search = _bare_search(self._cfg(tmp))
            search._defer_faiss = False
            search._model_fingerprint = "new-model"
            search.embedder = _ProbeEmbedder(probes)
            try:
                with (
                    patch.object(
                        sys.modules["faiss"],
                        "read_index",
                        return_value=object(),
                        create=True,
                    ),
                    self.assertLogs("zimantic.search", level="WARNING") as logs,
                ):
                    index = search._open_index(db_path)
            finally:
                search._executor.shutdown(wait=True)

            self.assertIsNotNone(index)
            self.assertIsNotNone(index.faiss_index)
            self.assertTrue(any("probes match" in line for line in logs.output))

    def test_concurrent_probe_checks_share_cached_embeddings(self):
        from zimantic.embed import serialize_model_probes

        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value)")
        probes = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32)
        for key, value in serialize_model_probes(
            ["first probe", "second probe"], probes
        ).items():
            db.execute("INSERT INTO meta VALUES (?, ?)", (key, value))
        db.commit()

        search = _bare_search({})
        search.embedder = object()
        search._probe_cache = {}
        search._probe_cache_lock = threading.Lock()
        started = threading.Event()
        release = threading.Event()
        second_call = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def embed_probe(_embedder, _texts):
            nonlocal calls
            with calls_lock:
                calls += 1
                if calls > 1:
                    second_call.set()
            started.set()
            if not release.wait(2):
                raise RuntimeError("test probe release timed out")
            return probes

        executor = ThreadPoolExecutor(max_workers=2)
        futures = []
        try:
            with patch("zimantic.embed.embed_probe_texts", side_effect=embed_probe):
                futures = [
                    executor.submit(
                        search._model_probes_compatible, db, Path("index.sqlite")
                    )
                    for _ in range(2)
                ]
                self.assertTrue(started.wait(2))
                self.assertFalse(second_call.wait(1))
        finally:
            release.set()
            executor.shutdown(wait=True)
            db.close()

        results = [future.result() for future in futures]
        self.assertEqual(calls, 1)
        self.assertTrue(all(result[0] for result in results))

    def test_mismatched_probes_keep_vectors_disabled(self):
        class _ProbeEmbedder:
            def __init__(self, vectors):
                self.vectors = vectors

            def embed(self, _texts):
                return self.vectors

        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            db_path = _write_index(
                tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)]
            )
            stored = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32)
            current = np.array([[1, 0, 0, 0], [1, 0, 0, 0]], dtype=np.float32)
            db = sqlite3.connect(db_path)
            db.execute("INSERT INTO meta VALUES ('model_fingerprint', 'old-model')")
            from zimantic.embed import serialize_model_probes

            for key, value in serialize_model_probes(
                ["first probe", "second probe"], stored
            ).items():
                db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            db.commit()
            db.close()
            db_path.with_suffix(".faiss").write_bytes(b"vectors")

            search = _bare_search(self._cfg(tmp))
            search._defer_faiss = False
            search._model_fingerprint = "new-model"
            search.embedder = _ProbeEmbedder(current)
            try:
                with self.assertLogs("zimantic.search", level="WARNING") as logs:
                    index = search._open_index(db_path)
            finally:
                search._executor.shutdown(wait=True)

            self.assertIsNotNone(index)
            self.assertIsNone(index.faiss_index)
            self.assertTrue(any("different model" in line for line in logs.output))


    def test_library_includes_sources_without_a_homepage(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search.sources = {
            "readable": _zim_source(
                "readable", "Readable", homepage="Main_Page"
            ),
            "unreadable": _zim_source(
                "unreadable", "Unreadable", searchable=False
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
            [source["title"] for source in library], ["Readable", "Unreadable"]
        )
        self.assertTrue(library[0]["searchable"])
        self.assertFalse(library[1]["searchable"])
        search._executor.shutdown(wait=True)

    def test_library_lists_an_unsearchable_source(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search.sources = {
            "broken": _zim_source(
                "broken", "A broken book", searchable=False
            ),
        }
        with patch.object(
            search,
            "_scan_zim_paths",
            return_value={"broken": Path("/zims/broken.zim")},
        ):
            library = search.library_dicts()

        self.assertEqual([source["key"] for source in library], ["broken"])
        self.assertFalse(library[0]["searchable"])
        self.assertEqual(library[0]["title"], "A broken book")
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
            _zim_source(key, key.upper())
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
                "source": source.title,
                "source_icon": None,
                "title": source.title,
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

        def submit(_fn, _stop, source, *_args, **_kwargs):
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
            db.execute("INSERT INTO meta VALUES ('done', 'full')")
            db.commit()
            db.close()
            self.assertEqual(search._discover_local_indexes()["manual"][0], "sqlite")
            search._executor.shutdown(wait=True)

    def test_index_whose_zim_changed_size_falls_back_to_the_unindexed_zim(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", done="full", rows=[(1, "Old", "old", None)])
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
            _write_index(tmp, "manual", done="full", rows=[(1, "Old", "old", None)])
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
            _write_index(tmp, "manual", done="full", rows=[(1, "Old", "old", None)])
            (tmp / "manual.zim").unlink()

            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            # With no archive to serve, the orphaned index is not loaded.
            self.assertNotIn("manual", search._discover_local_indexes())
            search._executor.shutdown(wait=True)


class RandomArticleServingTests(unittest.TestCase):
    def test_fallback_uses_get_entry_by_id_from_search_module(self):
        class _Archive:
            entry_count = 1

        class _Index:
            random_article_ids = None

            @staticmethod
            def ensure_open():
                return _Archive()

        class _Entry:
            is_redirect = False
            path = "Article"

            @staticmethod
            def get_item():
                return types.SimpleNamespace(mimetype="text/html")

        search = _bare_search({})
        with patch(
            "zimantic.libzim_compat.get_entry_by_id", return_value=_Entry()
        ):
            self.assertEqual(search._random_fallback_article(_Index()), "Article")


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

    def test_explicit_source_scope_searches_only_selected_sources(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "alpha", rows=[(1, "Tire Change Alpha", "a", None)])
            _write_index(tmp, "beta", rows=[(1, "Tire Change Beta", "b", None)])
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            page = search.search_page("tire change", limit=10, source="beta")

            self.assertEqual([doc["source_key"] for doc in page["results"]], [search._zim_source_key("beta")])
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["counts"], {search._zim_source_key("beta"): 1})

            page = search.search_page("tire change", limit=10, source=["alpha", "beta"])

            self.assertEqual(
                [doc["source_key"] for doc in page["results"]],
                [search._zim_source_key("alpha"), search._zim_source_key("beta")],
            )
            self.assertEqual(page["total"], 2)
            search._executor.shutdown(wait=True)

    def test_selective_search_retrieves_deeper_than_a_broad_display_filter(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            rows = [(i, f"Tire Change {i}", f"tire-{i}", None) for i in range(1, 21)]
            for name in ("alpha", "beta", "gamma", "delta"):
                _write_index(tmp, name, rows=rows)
            search = _bare_search({**self._cfg(tmp), "candidate_count": 2, "max_results": 20})
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            broad = search.search_page("tire change", display_source="beta")
            with patch.object(search, "_search_source", wraps=search._search_source) as retrieve:
                selected = search.search_page("tire change", source="beta")
            self.assertEqual(retrieve.call_count, 1)
            self.assertGreater(selected["total"], broad["total"])
            self.assertEqual(selected["total"], 20)
            self.assertEqual(selected["scope"]["requested"], 1)
            self.assertEqual(set(selected["counts"]), {search._zim_source_key("beta")})

    def test_display_source_narrows_the_page_without_changing_scope(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "alpha", rows=[(1, "Tire Change Alpha", "a", None)])
            _write_index(tmp, "beta", rows=[(1, "Tire Change Beta", "b", None)])
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            search._load_indexes()
            search.refresh_sources()

            page = search.search_page(
                "tire change", limit=10, display_source="beta"
            )
            beta = search._zim_source_key("beta")
            alpha = search._zim_source_key("alpha")
            self.assertEqual(
                [doc["source_key"] for doc in page["results"]], [beta]
            )
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["counts"], {alpha: 1, beta: 1})
            self.assertEqual(page["scope"]["requested"], 2)

            page = search.search_page(
                "tire change", limit=10, display_source=["alpha", "beta"]
            )
            self.assertEqual(
                [doc["source_key"] for doc in page["results"]],
                [alpha, beta],
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

    def _begin_abandoned_stream(self, search):
        """Start a two-source stream whose slow worker blocks until released.

        Consumes `started` and the first `source` event, leaving the stream
        suspended while `slow` is still running.
        """
        quick = _zim_source("quick", "Quick")
        slow = _zim_source("slow", "Slow")
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
        self.assertEqual(next(stream)["type"], "started")
        self.assertEqual(next(stream)["type"], "source")
        self.assertTrue(slow_started.wait(timeout=5))
        return stream, release

    def test_disconnected_stream_releases_the_search_slot(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search._search_slots = _CountingSemaphore(1)
        stream, release = self._begin_abandoned_stream(search)
        pending = next(iter(search._inflight.values()))
        try:
            stream.close()

            # A disconnected stream returns its search slot and resolves its
            # inflight claim immediately without spawning a finalizer thread.
            self.assertEqual(search._search_slots.releases, 1)
            self.assertTrue(search._search_slots.acquire(blocking=False))
            search._search_slots.release()
            self.assertFalse(search._inflight)
            self.assertTrue(pending.done())
            self.assertIsNone(pending.result())
            self.assertEqual(search.cache.stats()["size"], 0)

            release.set()
            search._executor.shutdown(wait=True)
            self.assertTrue(search._search_slots.acquire(blocking=False))
            search._search_slots.release()
        finally:
            release.set()
            search._executor.shutdown(wait=True)

    def test_disconnected_stream_skips_queued_sources(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        sources = [
            _zim_source("slow1", "Slow 1"),
            _zim_source("slow2", "Slow 2"),
            _zim_source("queued", "Queued"),
        ]
        search.sources = {source.key: source for source in sources}
        release = threading.Event()
        slow_started = [threading.Event(), threading.Event()]
        calls = []

        def search_source(source, *_args):
            calls.append(source.key)
            if source.key.startswith("slow"):
                slow_started[int(source.key[-1]) - 1].set()
                release.wait(timeout=5)
            return SourceResult(source=source)

        search._search_source = search_source
        search.stream_update_interval = 0.01
        stream = search.stream_search("anything", limit=1)
        try:
            self.assertEqual(next(stream)["type"], "started")
            self.assertEqual(next(stream)["type"], "progress")
            self.assertTrue(all(event.wait(timeout=5) for event in slow_started))
            stream.close()

            self.assertNotIn("queued", calls)
            self.assertTrue(search._search_slots.acquire(blocking=False))
            search._search_slots.release()
            self.assertFalse(search._inflight)

            release.set()
            search._executor.shutdown(wait=True)
            self.assertNotIn("queued", calls)
            self.assertEqual(search.cache.stats()["size"], 0)
        finally:
            release.set()
            search._executor.shutdown(wait=True)

    def test_waiter_retries_after_abandoned_search(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        sources = [
            _zim_source("slow1", "Slow 1"),
            _zim_source("slow2", "Slow 2"),
            _zim_source("queued", "Queued"),
        ]
        search.sources = {source.key: source for source in sources}
        release = threading.Event()
        slow_started = [threading.Event(), threading.Event()]
        waiter_claimed = threading.Event()
        claim_count = 0
        claim_lock = threading.Lock()
        calls = []

        def search_source(source, *_args):
            calls.append(source.key)
            if source.key.startswith("slow"):
                slow_started[int(source.key[-1]) - 1].set()
                release.wait(timeout=5)
            return SourceResult(source=source)

        original_claim = search._claim_search

        def claim(*args, **kwargs):
            nonlocal claim_count
            with claim_lock:
                claim_count += 1
                if claim_count == 2:
                    waiter_claimed.set()
            return original_claim(*args, **kwargs)

        search._search_source = search_source
        search._claim_search = claim
        search.stream_update_interval = 0.01
        owner = search.stream_search("anything", limit=1)
        waiter_events = []

        def wait_for_search():
            waiter_events.extend(search.stream_search("anything", limit=1))

        waiter = threading.Thread(target=wait_for_search)
        try:
            self.assertEqual(next(owner)["type"], "started")
            self.assertEqual(next(owner)["type"], "progress")
            self.assertTrue(all(event.wait(timeout=5) for event in slow_started))
            waiter.start()
            self.assertTrue(waiter_claimed.wait(timeout=5))
            owner.close()
            release.set()
            waiter.join(timeout=5)

            self.assertFalse(waiter.is_alive())
            self.assertEqual(
                {
                    event["source"]["key"]
                    for event in waiter_events
                    if event["type"] == "source"
                },
                {"slow1", "slow2", "queued"},
            )
            self.assertIn("queued", calls)
        finally:
            release.set()
            owner.close()
            waiter.join(timeout=5)
            search._executor.shutdown(wait=True)

    def test_abandoned_queued_search_skips_instead_of_blocking_retries(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search._search_slots = _CountingSemaphore(1)
        search.stream_update_interval = 0.001
        source = _zim_source("queued", "Queued")
        search.sources = {source.key: source}
        release = threading.Event()
        started = [threading.Event(), threading.Event()]
        calls = []

        def occupy_worker(started):
            started.set()
            release.wait(timeout=5)

        def search_source(source, *_args):
            calls.append(source.key)
            return SourceResult(source=source)

        search._search_source = search_source
        for event in started:
            search._executor.submit(occupy_worker, event)
        stream = search.stream_search("queued", limit=1)
        try:
            self.assertTrue(all(event.wait(timeout=5) for event in started))
            self.assertEqual(next(stream)["type"], "started")
            self.assertEqual(next(stream)["type"], "progress")
            pending = next(iter(search._inflight.values()))
            stream.close()

            # The queued source is skipped, so it never runs and the abandoned
            # stream is not cached as a complete answer.
            self.assertTrue(pending.done())
            self.assertIsNone(pending.result())
            self.assertFalse(search._inflight)
            self.assertEqual(calls, [])
            self.assertEqual(search.cache.stats()["size"], 0)

            # Retries are admitted immediately instead of stacking queued work
            # behind the occupied workers.
            for query in ("retry one", "retry two", "retry three"):
                retry = search.stream_search(query, limit=1)
                self.assertEqual(next(retry)["type"], "started")
                retry.close()
                self.assertFalse(search._inflight)
                self.assertEqual(calls, [])
            self.assertTrue(search._search_slots.acquire(blocking=False))
            search._search_slots.release()
        finally:
            release.set()
            search._executor.shutdown(wait=True)

    def test_competing_providers_reserve_a_worker_for_local_work(self):
        search = _bare_search({})
        search.source_workers = 2
        search.sources = {
            "remote-one": SourceInfo(
                key="remote-one", provider="kiwix", title="Remote One",
                collection_key="kiwix:remote", searchable=True,
            ),
            "remote-two": SourceInfo(
                key="remote-two", provider="kiwix", title="Remote Two",
                collection_key="kiwix:remote", searchable=True,
            ),
            "local": SourceInfo(
                key="local", provider="zim", title="Local",
                collection_key="zim:local", searchable=True,
            ),
        }
        remote_started = threading.Event()
        local_started = threading.Event()
        release = threading.Event()

        def search_source(source, *_args):
            if source.provider == "kiwix":
                remote_started.set()
                release.wait(timeout=5)
                return SourceResult(
                    source=source, error="remote is down", status="error"
                )
            local_started.set()
            return SourceResult(source=source)

        search._search_source = search_source
        search.stream_update_interval = 0.01
        events = []

        def run():
            events.extend(search.stream_search("anything", limit=1))

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(remote_started.wait(timeout=5))
            # One remote worker is blocked, but the reserved slot lets local
            # work run instead of queuing behind it.
            self.assertTrue(local_started.wait(timeout=5))
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(events[-1]["type"], "done")
        search._executor.shutdown(wait=True)

    def test_one_worker_keeps_the_slot_until_the_remote_call_exits(self):
        search = _bare_search({})
        old_executor = search._executor
        search.source_workers = 1
        search._executor = ThreadPoolExecutor(max_workers=1)
        old_executor.shutdown(wait=True)
        search.sources = {
            "remote": SourceInfo(
                key="remote", provider="kiwix", title="Remote",
                collection_key="kiwix:remote", searchable=True,
            ),
            "local": SourceInfo(
                key="local", provider="zim", title="Local",
                collection_key="zim:local", searchable=True,
            ),
        }
        remote_started = threading.Event()
        local_started = threading.Event()
        release = threading.Event()

        def search_source(source, *_args):
            if source.provider == "kiwix":
                remote_started.set()
                release.wait(timeout=5)
                return SourceResult(
                    source=source, error="remote is down", status="error"
                )
            local_started.set()
            return SourceResult(source=source)

        search._search_source = search_source
        search.stream_update_interval = 0.01
        events = []

        def run():
            events.extend(search.stream_search("anything", limit=1))

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(remote_started.wait(timeout=5))
            # A running call retains the only worker; it is not detached as if
            # the slot were free.
            self.assertFalse(local_started.wait(timeout=0.15))
        finally:
            release.set()
        try:
            self.assertTrue(local_started.wait(timeout=5))
            thread.join(timeout=5)
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(events[-1]["type"], "done")
        search._executor.shutdown(wait=True)

    def test_slow_remote_source_error_does_not_fail_merged_search(self):
        search = _bare_search({})
        search.sources = {
            "remote": SourceInfo(
                key="remote", provider="kiwix", title="Remote",
                collection_key="kiwix:remote", searchable=True,
            ),
            "local": SourceInfo(
                key="local", provider="zim", title="Local",
                collection_key="zim:local", searchable=True,
            ),
        }
        doc = {
            "id": encode_document_id("local", "page"),
            "source_key": "local",
            "source": "Local",
            "source_icon": None,
            "title": "Local Page",
            "lead": "",
            "path": "page",
            "url": "/content/local/page",
            "mimetype": None,
            "source_rank": 1,
        }

        def search_source(source, *_args):
            if source.provider == "kiwix":
                return SourceResult(
                    source=source, error="Kiwix search failed: timed out",
                    status="error",
                )
            return SourceResult(source=source, items=[doc], fulltext=[(1, doc)])

        search._search_source = search_source
        events = list(search.stream_search("page", limit=5))
        try:
            done = events[-1]
            self.assertEqual(done["type"], "done")
            self.assertEqual([result["source_key"] for result in done["results"]], ["local"])
            remote = next(
                event for event in events
                if event["type"] == "source"
                and event["source"]["key"] == "remote"
            )
            self.assertEqual(remote["status"], "error")
        finally:
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


    def test_search_uses_an_in_memory_catalog_without_index_dir(self):
        with _temp_dir(self) as tmp:
            zim_path = tmp / "manual.zim"
            zim_path.write_bytes(b"not inspected")
            search = Search(
                {"zim_dir": str(tmp)},
                semantic=False,
                archive_factory=object,
            )
            self.addCleanup(search.close)

            runtime = next(iter(search.runtimes.values()))
            self.assertEqual(runtime.catalog.db_path, Path(":memory:"))
            self.assertEqual(search._scan_zim_paths(), {"manual": zim_path})

    def test_unknown_source_selection_is_a_client_error(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            search = self._search(tmp)
            try:
                with self.assertRaises(SearchQueryError):
                    search._search_scope(["removed-source"])
            finally:
                search._executor.shutdown(wait=True)

    def test_explicit_empty_source_list_is_an_empty_scope(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            search = self._search(tmp)
            try:
                scope = search._search_scope([])
                self.assertEqual(scope.sources, [])
                self.assertTrue(scope.selective)
                self.assertEqual(scope.cache_selection, ())
                self.assertEqual(scope.requested, 0)
            finally:
                search._executor.shutdown(wait=True)

    def test_known_unsearchable_source_resolves_to_an_empty_scope(self):
        search = _bare_search({})
        search.sources = {
            "quiet": SourceInfo(
                key="quiet",
                provider="zim",
                title="Quiet",
                searchable=False,
            )
        }
        try:
            scope = search._search_scope(["quiet"])
            self.assertEqual(scope.sources, [])
            self.assertTrue(scope.selective)
            self.assertEqual(scope.cache_selection, ())
        finally:
            search._executor.shutdown(wait=True)

    def test_collection_selector_expands_and_intersects_sources(self):
        from zimantic.providers.base import CollectionInfo

        search = _bare_search({})
        search.collections = {
            "zim:group": CollectionInfo(
                key="zim:group", provider="zim", label="Group"
            )
        }
        search.sources = {
            "one": SourceInfo(
                key="one", provider="zim", title="One",
                collection_key="zim:group", collection_label="Group",
                searchable=True,
            ),
            "two": SourceInfo(
                key="two", provider="zim", title="Two",
                collection_key="zim:group", collection_label="Group",
                searchable=True,
            ),
            "other": SourceInfo(
                key="other", provider="zim", title="Other",
                collection_key="zim:other", collection_label="Other",
                searchable=True,
            ),
        }
        try:
            expanded = search._search_scope(None, "zim:group")
            self.assertEqual(
                [source.key for source in expanded.sources], ["one", "two"]
            )
            self.assertEqual(expanded.requested, 2)
            intersected = search._search_scope(["two"], "zim:group")
            self.assertEqual(
                [source.key for source in intersected.sources], ["two"]
            )
            with self.assertRaises(SearchQueryError):
                search._search_scope(None, "zim:missing")
        finally:
            search._executor.shutdown(wait=True)

    def test_explicit_kiwix_collection_is_capped(self):
        from zimantic.providers.base import CollectionInfo

        search = _bare_search({})
        search.max_explicit_collection_sources = 2
        search.collections = {
            "kiwix:remote": CollectionInfo(
                key="kiwix:remote", provider="kiwix", label="Remote"
            )
        }
        search.sources = {
            f"book-{index}": SourceInfo(
                key=f"book-{index}",
                provider="kiwix",
                title=f"Book {index}",
                collection_key="kiwix:remote",
                collection_label="Remote",
                searchable=True,
            )
            for index in range(3)
        }
        try:
            scope = search._search_scope(None, "kiwix:remote")
            self.assertEqual(len(scope.sources), 2)
            self.assertEqual(scope.requested, 3)
            self.assertTrue(scope.source_cap)
        finally:
            search._executor.shutdown(wait=True)

    def test_remote_dated_book_slug_selects_the_local_base_source(self):
        search = _bare_search({})
        search.sources = {
            "foo": _zim_source("foo", "foo")
        }
        try:
            scope = search._search_scope(["foo"])
        finally:
            search._executor.shutdown(wait=True)
        self.assertEqual([source.key for source in scope.sources], ["foo"])

    def test_hidden_flavour_variant_selects_the_chosen_book(self):
        search = _bare_search({})
        search.sources = {
            "medicine": _zim_source("medicine", "medicine_nopic_2025-10")
        }
        for index in range(3):
            key = f"other-{index}"
            search.sources[key] = _zim_source(key, key)
        try:
            scope = search._search_scope(["medicine"])
        finally:
            search._executor.shutdown(wait=True)
        self.assertEqual([source.key for source in scope.sources], ["medicine"])
        self.assertTrue(scope.selective)

    def test_unscoped_search_caps_remote_sources_but_honors_explicit_ones(self):
        class _RemoteRuntime:
            def __init__(self, limit):
                self.limit = limit

            def max_sources_per_search(self):
                return self.limit

        search = _bare_search({})
        search.runtimes["kiwix:remote"] = _RemoteRuntime(1)
        search.sources = {
            "local": SourceInfo(
                key="local", provider="zim", title="Local",
                collection_key="zim:local", searchable=True,
            ),
            "remote-one": SourceInfo(
                key="remote-one", provider="kiwix", title="One",
                collection_key="kiwix:remote", searchable=True,
            ),
            "remote-two": SourceInfo(
                key="remote-two", provider="kiwix", title="Two",
                collection_key="kiwix:remote", searchable=True,
            ),
        }
        try:
            scope = search._search_scope(None)
            self.assertEqual(
                [source.key for source in scope.sources], ["local", "remote-one"]
            )
            self.assertTrue(scope.source_cap)
            scope = search._search_scope(["remote-two"])
            self.assertEqual([source.key for source in scope.sources], ["remote-two"])
            self.assertFalse(scope.source_cap)
            search.runtimes["kiwix:remote"].limit = 0
            scope = search._search_scope(["remote-two"])
            self.assertEqual([source.key for source in scope.sources], ["remote-two"])
            self.assertFalse(scope.source_cap)
            scope = search._search_scope(None)
            self.assertEqual([source.key for source in scope.sources], ["local"])
        finally:
            search._executor.shutdown(wait=True)





    def test_unknown_source_selection_does_not_fall_back_to_all_sources(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            search = self._search(tmp)
            try:
                with self.assertRaises(SearchQueryError):
                    list(
                        search.stream_search(
                            "tire change",
                            source=["removed-source"],
                            limit=2,
                        )
                    )
            finally:
                search._executor.shutdown(wait=True)

    def test_small_selection_searches_only_those_sources_and_is_cached(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            search = self._search(tmp)
            try:
                events = list(
                    search.stream_search(
                        "tire change", source=["s1", "s2"], limit=2
                    )
                )
                started = events[0]
                self.assertTrue(started["selective"])
                self.assertFalse(started["all_sources"])
                self.assertEqual(started["total_sources"], 2)
                self.assertEqual(
                    {source["title"] for source in started["sources"]},
                    {"s1", "s2"},
                )
                keys = tuple(
                    sorted(source["key"] for source in started["sources"])
                )
                self.assertIsNotNone(
                    search.cache.get(search._cache_key("tire change", keys))
                )
                self.assertIsNone(
                    search.cache.get(search._cache_key("tire change", None))
                )
            finally:
                search._executor.shutdown(wait=True)

    def test_large_selection_is_not_promoted_to_the_full_scope(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            search = self._search(tmp)
            try:
                events = list(
                    search.stream_search(
                        "tire change",
                        source=["s1", "s2", "s3"],
                        limit=2,
                    )
                )
                started = events[0]
                self.assertTrue(started["selective"])
                self.assertFalse(started["all_sources"])
                self.assertEqual(started["total_sources"], 3)
                keys = tuple(
                    sorted(source["key"] for source in started["sources"])
                )
                self.assertIsNotNone(
                    search.cache.get(search._cache_key("tire change", keys))
                )
                self.assertIsNone(
                    search.cache.get(search._cache_key("tire change", None))
                )

                # The explicit scope is reused instead of being promoted.
                with patch.object(
                    search,
                    "_search_source",
                    side_effect=AssertionError("cache miss"),
                ):
                    cached = list(
                        search.stream_search(
                            "tire change",
                            source=["s1", "s2", "s3"],
                            limit=2,
                        )
                    )
                self.assertTrue(cached[0]["selective"])
                self.assertFalse(cached[0]["all_sources"])
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
        source = _zim_source("manual", "Manual")
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
        source = _zim_source("manual", "Manual")
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
        search.slot_wait_seconds = 0
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

    def test_search_slot_wait_returns_false_after_the_budget(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search._search_slots = threading.BoundedSemaphore(1)
        self.assertTrue(search._search_slots.acquire(blocking=False))
        search.slot_wait_seconds = 0.02
        started = time.monotonic()
        try:
            self.assertFalse(search._acquire_search_slot())
        finally:
            search._search_slots.release()
        self.assertLess(time.monotonic() - started, 1.0)

    def test_search_slot_release_wakes_a_waiter(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search._search_slots = threading.BoundedSemaphore(1)
        self.assertTrue(search._search_slots.acquire(blocking=False))
        search.slot_wait_seconds = 5
        timer = threading.Timer(0.02, search._search_slots.release)
        timer.start()
        try:
            acquired = search._acquire_search_slot()
        finally:
            timer.cancel()
        self.assertTrue(acquired)
        search._search_slots.release()

    def test_excess_slot_waiters_report_busy_without_blocking(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search._search_slots = threading.BoundedSemaphore(1)
        self.assertTrue(search._search_slots.acquire(blocking=False))
        search._slot_waiters = threading.BoundedSemaphore(1)
        self.assertTrue(search._slot_waiters.acquire(blocking=False))
        search.slot_wait_seconds = 5
        started = time.monotonic()
        try:
            self.assertFalse(search._acquire_search_slot())
        finally:
            search._slot_waiters.release()
            search._search_slots.release()
        self.assertLess(time.monotonic() - started, 1.0)

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
                    done="full",
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
        source = _zim_source("manual", "Manual")
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
    def test_bare_search_uses_an_in_memory_catalog_with_index_dir(self):
        from zimantic.catalog import SourceCatalog

        with _temp_dir(self) as tmp:
            search = _bare_search(
                {"index_dir": str(tmp), "zim_dir": str(tmp)}
            )
            self.addCleanup(search.close)
            search.catalog = SourceCatalog(None)
            self.assertEqual(search.catalog.db_path, Path(":memory:"))

    def test_search_passes_its_archive_factory_to_the_catalog(self):
        with _temp_dir(self) as tmp:
            search = Search(
                {"zim_dir": str(Path(tmp) / "missing")},
                semantic=False,
                archive_factory=object,
            )
            self.addCleanup(search.close)
            runtime = next(iter(search.runtimes.values()))
            self.assertEqual(runtime.catalog.db_path, Path(":memory:"))

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
            search = Search(cfg, semantic=False)
            self.addCleanup(search.close)
            self.assertTrue((root / "catalog.sqlite").exists())
            self.assertFalse((index_dir / "catalog.sqlite").exists())


class QueryCacheTests(unittest.TestCase):
    def test_scoped_quotas_evict_matching_entries_and_account_for_mixed_pools(self):
        cache = QueryCache(4, max_bytes=100)
        cache.put("other", "other", size=20, quotas={"b": (2, 60)})
        cache.put("mixed", "mixed", size=30, quotas={"a": (1, 60), "b": (2, 60)})
        cache.put("a", "a", size=20, quotas={"a": (1, 60)})
        self.assertIsNone(cache.get("mixed"))
        self.assertEqual(cache.get("other"), "other")
        cache.put("b", "b", size=40, quotas={"b": (2, 60)})
        cache.put("third", "third", size=10, quotas={"b": (2, 60)})
        self.assertIsNone(cache.get("other"))
        self.assertEqual(cache.get("a"), "a")
        self.assertEqual(cache.stats()["bytes"], 70)

    def test_scoped_admission_does_not_evict_for_unusable_entries_or_raise_global_ceilings(
        self,
    ):
        cache = QueryCache(2, max_bytes=100)
        cache.put("kept", "kept", size=20, quotas={"a": (2, 50)})
        cache.put("oversized", "oversized", size=60, quotas={"a": (2, 50)})
        cache.put("disabled", "disabled", size=10, quotas={"a": (0, 0)})
        self.assertEqual(cache.stats()["size"], 1)
        self.assertEqual(cache.get("kept"), "kept")
        cache.put("new", "new", size=60, quotas={"b": (100, 1000)})
        cache.put("last", "last", size=30, quotas={"c": (100, 1000)})
        self.assertEqual(cache.stats()["size"], 2)
        self.assertLessEqual(cache.stats()["bytes"], 100)

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


# ---------------------------------------------------------------------------
# Test-only adapters for the phase-01 provider seam.
#
# Production ``Search`` no longer owns ZIM catalog/index discovery.  These
# helpers let older unit tests keep exercising core search behavior against
# stem-based fixture indexes while the real implementation lives in
# ``zimantic.providers.zim``.


def _test_open_index(self, db_path: Path):
    import logging
    import sqlite3

    from zimantic.searching.config import _file_signature

    try:
        db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = dict(db.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error:
        db.close()
        return None
    done = str(rows.get("done", ""))
    if done not in {"1", "full", "fast"}:
        db.close()
        return None
    try:
        source_size = int(rows["filesize"]) if "filesize" in rows else None
    except (TypeError, ValueError):
        source_size = None
    zim_path = db_path.with_suffix(".zim")
    faiss_path = db_path.with_suffix(".faiss")
    faiss_index = None
    stored_fingerprint = str(rows.get("model_fingerprint", "") or "")
    try:
        if (
            self.semantic
            and not self._defer_faiss
            and done in {"1", "full"}
            and faiss_path.exists()
        ):
            model_changed = bool(
                stored_fingerprint
                and self._model_fingerprint
                and stored_fingerprint != self._model_fingerprint
            )
            if model_changed:
                compatible, detail = self._model_probes_compatible(db, db_path)
                if compatible:
                    logging.getLogger("zimantic.search").warning(
                        "%s: vectors were built with a different model file, "
                        "but embedding probes match (%s); using them",
                        db_path.stem,
                        detail,
                    )
                    model_changed = False
                else:
                    logging.getLogger("zimantic.search").warning(
                        "%s: vectors were built with a different model; "
                        "falling back to title and full-text (%s)",
                        db_path.stem,
                        detail,
                    )
            if not model_changed:
                try:
                    import faiss

                    faiss_index = faiss.read_index(str(faiss_path))
                except Exception:
                    faiss_index = None
    finally:
        db.close()
    return _LocalIndex(
        None,
        faiss_index,
        None,
        None,
        threading.Lock(),
        db_path=db_path.resolve().as_uri() + "?mode=ro",
        db_signature=_file_signature(db_path),
        faiss_signature=(
            _file_signature(faiss_path) if faiss_index is not None else None
        ),
        done=done,
        source_size=source_size,
        zim_path=str(zim_path) if zim_path.exists() else None,
        zim_signature=_file_signature(zim_path) if zim_path.exists() else None,
        has_fulltext_index=False,
    )


def _test_load_indexes(self):
    index_dir = self.cfg.get("index_dir")
    if not index_dir:
        return
    for db_path in sorted(Path(index_dir).glob("*.sqlite")):
        index = _test_open_index(self, db_path)
        if index is None:
            continue
        name = db_path.stem
        self.indexes[name] = index
        self.sources.setdefault(
            name,
            SourceInfo(
                name,
                provider="zim",
                title=name,
                collection_key="",
                searchable=True,
                size=index.source_size,
            ),
        )
    self.runtimes = {"": _TestZimRuntime(self)}


def _test_scan_zim_paths(self, *, refresh=True, records=None):
    from zimantic.providers.zim.discovery import (
        iter_zim_files,
        largest_zims_by_stem,
    )

    zim_dir = self.cfg.get("zim_dir")
    if not zim_dir or not Path(zim_dir).is_dir():
        return {}
    paths = {
        path: path.stat().st_size
        for path in iter_zim_files(zim_dir)
        if path.is_file()
    }
    return largest_zims_by_stem(paths)


def _test_find_zim_path(self, stem, source_size, *, refresh=True):
    from zimantic.providers.zim.discovery import iter_zim_files

    zim_dir = self.cfg.get("zim_dir")
    if not zim_dir or not Path(zim_dir).is_dir():
        return None
    candidates = [path for path in iter_zim_files(zim_dir) if path.stem == stem]
    if not candidates:
        return None
    if source_size is not None:
        for path in candidates:
            try:
                if path.stat().st_size == source_size:
                    return path
            except OSError:
                continue
    return max(candidates, key=lambda path: path.stat().st_size)


def _test_match_zim_path(self, stem, source_size, *, refresh=True):
    path = self._find_zim_path(stem, source_size, refresh=refresh)
    if path is None:
        return None, False
    if source_size is None:
        return path, True
    try:
        return path, path.stat().st_size == source_size
    except OSError:
        return path, False


def _test_discover_local_indexes(self, *, refresh_catalog=True):
    found = {}
    finished = {}
    for db_path in sorted(Path(self.cfg.get("index_dir") or ".").glob("*.sqlite")):
        from zimantic.providers.zim.discovery import read_index_state

        done, source_size = read_index_state(db_path)
        if done:
            finished[db_path.stem] = (db_path, source_size)
    zims = self._scan_zim_paths(refresh=False)
    for stem, (db_path, recorded) in finished.items():
        scanned = zims.get(stem)
        if scanned is None:
            continue
        if recorded is not None and scanned.stat().st_size != recorded:
            found[stem] = ("zim", scanned)
        else:
            found[stem] = ("sqlite", db_path)
    for stem, path in zims.items():
        found.setdefault(stem, ("zim", path))
    return found


def _test_open_zim_index(self, zim_path):
    archive = None
    from libzim.reader import Archive

    try:
        archive = Archive(str(zim_path))
        if not getattr(archive, "has_fulltext_index", False):
            return None
    except Exception:
        if not Path(zim_path).exists():
            return None
    return _LocalIndex(
        None,
        None,
        archive,
        None,
        threading.Lock(),
        done="filesystem",
        source_size=zim_path.stat().st_size,
        zim_path=str(zim_path),
        zim_signature=_file_signature(zim_path),
        has_fulltext_index=True,
    )


def _test_random_indexed_article(self, index, db):
    from zimantic.content.serving import RANDOM_ARTICLE_ATTEMPTS, random_indexed_article

    return random_indexed_article(index, db, attempts=RANDOM_ARTICLE_ATTEMPTS)


def _test_random_fallback_article(self, index):
    from zimantic.content.serving import RANDOM_ARTICLE_ATTEMPTS, random_fallback_article
    from zimantic.libzim_compat import get_entry_by_id

    return random_fallback_article(
        index,
        attempts=RANDOM_ARTICLE_ATTEMPTS,
        get_entry_fn=get_entry_by_id,
    )


def _test_fetch_docs(self, source, db, rowids):
    from zimantic.providers.zim.search import fetch_docs

    return fetch_docs(source, db, rowids, web_base_path=self.web_base_path)


def _test_search_local_locked(
    self, source, query, title_query, fulltext_query, query_vector, limit, index
):
    from zimantic.providers.base import SourceQuery
    from zimantic.providers.zim.search import ZimSearchContext, _search_locked

    return _search_locked(
        source,
        index,
        SourceQuery(
            query=query,
            title_query=title_query,
            fulltext_query=fulltext_query,
            query_vector=query_vector,
            limit=limit,
        ),
        ZimSearchContext(
            candidate_count=self.candidate_count,
            preview_chars=self.preview_chars,
            max_html_bytes=self.max_html_bytes,
            max_article_bytes=self.max_article_bytes,
            web_base_path="",
            fulltext_pool=getattr(self, "fulltext_pool", None),
            cfg=self.cfg,
        ),
    )


_TEST_SEARCH = Search
Search._load_indexes = _test_load_indexes
Search._scan_zim_paths = _test_scan_zim_paths
Search._find_zim_path = _test_find_zim_path
Search._match_zim_path = _test_match_zim_path
Search._discover_local_indexes = _test_discover_local_indexes
Search._open_index = _test_open_index
Search._open_zim_index = _test_open_zim_index
Search._random_indexed_article = _test_random_indexed_article
Search._random_fallback_article = _test_random_fallback_article
Search._fetch_docs = _test_fetch_docs
Search._search_local_locked = _test_search_local_locked
Search._article_path = staticmethod(
    __import__("zimantic.content.path", fromlist=["normalize_article_path"]).normalize_article_path
)


def _test_model_probes_compatible(self, db, db_path):
    from zimantic.providers.zim.provider import ZimRuntime

    if getattr(self, "_embedder_getter", None) is None:
        self._embedder_getter = lambda: self.embedder
    if getattr(self, "_probe_cache", None) is None:
        self._probe_cache = {}
    if getattr(self, "_probe_cache_lock", None) is None:
        self._probe_cache_lock = threading.Lock()
    return ZimRuntime._probe_compatible(self, db, db_path)


Search._model_probes_compatible = _test_model_probes_compatible


def _test_zim_source_key(self, local_name, path=None):
    return str(local_name)


def _test_search_local(
    self, source, query, title_query, fulltext_query, query_vector, limit, deadline=None
):
    from zimantic.providers.base import SourceQuery

    runtime = _TestZimRuntime(self)
    return runtime.search(
        source,
        SourceQuery(
            query=query,
            title_query=title_query,
            fulltext_query=fulltext_query,
            query_vector=query_vector,
            limit=limit,
            deadline=deadline,
        ),
    )


def _test_resolve_article_entry(archive, path=None):
    from zimantic.content.zim_paths import resolve_zim_path

    return resolve_zim_path(archive, path)


Search._zim_source_key = _test_zim_source_key
Search._search_local = _test_search_local
Search._resolve_article_entry = staticmethod(_test_resolve_article_entry)


if __name__ == "__main__":
    unittest.main()
