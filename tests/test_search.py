import sqlite3
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
from zimantic.indexing import schema as index_schema
from zimantic.providers.base import (
    EnrichingRuntime,
    IndexedRuntime,
    IsolatedFulltextRuntime,
)
from zimantic.providers.config import RuntimeConfig, ZimConfig
from zimantic.providers.zim.metadata import (
    find_archive_icon_path as _find_archive_icon_path,
)
from zimantic.providers.zim.metadata import (
    source_icon_url as _source_icon_url,
)
from zimantic.providers.zim.metadata import (
    source_info_from_metadata as _source_info_from_metadata,
)
from zimantic.providers.zim.search import (
    fetch_docs as _fetch_zim_docs,
)
from zimantic.providers.zim.search import (
    phrase_in_word_stream as _phrase_in_word_stream,
)
from zimantic.search import (
    PREVIEW_HTML_BYTES,
    SearchBusyError,
    SearchQueryError,
)
from zimantic.search import (
    Search as _SearchBase,
)
from zimantic.searching.index import IndexHandle, _nprobe
from zimantic.searching.query import (
    _fulltext_query,
    _phrase_words,
    _query_terms,
    _title_prefix_query,
    _title_query,
)
from zimantic.searching.sources import _html_attribute

Search = _SearchBase


def _search_results(search, *args, **kwargs):
    """Return one page of results from the public ``search_page`` API."""
    return search.search_page(*args, **kwargs)["results"]


def _runtime_search(
    search, source, query, title_query, fulltext_query, query_vector, limit, index
):
    from zimantic.providers.base import SourceQuery

    search.indexes = {source.key: index}
    return _TestZimRuntime(search).search(
        source,
        SourceQuery(
            query=query,
            title_query=title_query,
            fulltext_query=fulltext_query,
            query_vector=query_vector,
            limit=limit,
        ),
    )


def _probe_compatible(search, db, db_path):
    from zimantic.providers.zim.provider import ZimRuntime

    runtime = object.__new__(ZimRuntime)
    runtime._embedder_getter = lambda: search.embedder
    runtime._probe_cache = search._probe_cache
    runtime._probe_cache_lock = search._probe_cache_lock
    runtime._embedder_ready_getter = None
    return ZimRuntime._probe_compatible(runtime, db, db_path)


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
    search._closed = False
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
    search.runtime_config = RuntimeConfig(settings=cfg)
    search.refresh_collections = lambda **_kwargs: search.source_dicts()
    search.providers = {}
    search._defer_faiss = False
    search._embedder_ready = threading.Event()
    search._embedder_started = False
    search.fulltext_pool = None
    search._probe_cache = {}
    search._probe_cache_lock = threading.Lock()
    return search


def _zim_runtime_config(path, **settings):
    return RuntimeConfig(
        settings=settings,
        collections=(ZimConfig(name="library", path=str(path)),)
    )


def _zim_source(key, title, **kwargs):
    kwargs.setdefault("searchable", True)
    return SourceInfo(key=key, provider="zim", title=title, **kwargs)


class _TestCollection:
    key = ""
    provider = "zim"
    label = "Library"


class _TestZimRuntime(IndexedRuntime, EnrichingRuntime):
    """Minimal runtime that exercises the real ZIM search module in tests."""

    def __init__(self, search):
        self.core = search
        self.collection = _TestCollection()
        self.config = ZimConfig(name="library", path=".")

    def bind_search(self, **bindings):
        self.bindings = bindings

    def start_faiss(self):
        pass

    def random_source(self):
        from zimantic.content.store import ArticleNotFoundError

        for source in self.sources:
            if source.homepage:
                return source
        raise ArticleNotFoundError("no local source has a homepage")

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
            preview_cache=getattr(self.core, "preview_cache", None),
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
        indexes = list(self.core.indexes.values())
        self.core.indexes.clear()
        for index in indexes:
            index.close()


class RuntimeCapabilityTests(unittest.TestCase):
    def test_legacy_resource_keys_still_control_runtime_pools(self):
        search = Search(
            RuntimeConfig(settings={"search_workers": 2, "fulltext_workers": 3}),
            semantic=False,
        )
        with patch("zimantic.fulltext.FulltextPool") as pool_factory:
            try:
                self.assertEqual(search.source_workers, 2)
                search.start_fulltext_isolation()
                pool_factory.assert_called_once_with(3, backend="processes")
            finally:
                search.close()

    def test_late_native_runtime_borrows_pool_when_startup_had_no_sources(self):
        class _IsolatedRuntime(IsolatedFulltextRuntime):
            def set_fulltext_pool(self, pool):
                self.pool = pool

        search = _bare_search({"libzim_search_processes": 1})
        search.runtimes = {}
        with patch("zimantic.fulltext.FulltextPool") as pool_factory:
            try:
                search.start_fulltext_isolation()
                late = _IsolatedRuntime()
                search._bind_runtime(late)
                self.assertIs(late.pool, search.fulltext_pool)
                pool_factory.assert_called_once_with(1, backend="processes")
            finally:
                search.close()

    def test_binding_requires_a_declared_indexed_runtime(self):
        search = _bare_search({})
        try:
            runtime = search.runtimes[""]
            search._bind_runtime(runtime)
            search.embedder = object()
            search._model_fingerprint = "model-fingerprint"
            self.assertIs(runtime.bindings["embedder_getter"](), search.embedder)
            self.assertEqual(runtime.bindings["model_fingerprint_getter"](), "model-fingerprint")
            self.assertEqual(
                runtime.bindings["embedder_ready_getter"](),
                (search._embedder_ready, False),
            )
            with patch.object(runtime, "bind_search") as bind:
                search._bind_runtime(types.SimpleNamespace(bind_search=bind))
                bind.assert_not_called()
        finally:
            search._executor.shutdown(wait=True)

    def test_deferred_faiss_startup_only_reopens_declared_indexed_runtimes(self):
        search = _bare_search({})
        runtime = search.runtimes[""]
        search._defer_faiss = True
        try:
            with patch.object(runtime, "start_faiss") as start:
                search.runtimes["other"] = types.SimpleNamespace(
                    bind_search=runtime.bind_search, start_faiss=start
                )
                with patch("zimantic.search.threading.Thread") as thread:
                    search.start_faiss()
                    thread.call_args.kwargs["target"]()
                start.assert_called_once_with()
                self.assertFalse(runtime.bindings["defer_faiss"])
        finally:
            search._executor.shutdown(wait=True)

    def test_late_discovery_loads_vectors_only_after_faiss_is_ready(self):
        for semantic, deferred in ((True, False), (True, True), (False, False)):
            with self.subTest(semantic=semantic, deferred=deferred):
                search = _bare_search({})
                runtime = search.runtimes.pop("")
                search.semantic = semantic
                search._defer_faiss = deferred
                batch = unittest.mock.Mock()
                discovered = types.SimpleNamespace(
                    collection=runtime.collection, runtime=runtime, batch=batch
                )
                result = types.SimpleNamespace(
                    collections=[discovered], diagnostics=()
                )
                try:
                    with (
                        patch("zimantic.discovery.discover_collections", return_value=result),
                        patch.object(search, "_publish_runtime_sources", return_value=[]),
                        patch.object(runtime, "start_faiss") as start,
                        patch("zimantic.search.threading.Thread") as thread,
                    ):
                        search._start_background_discovery(())
                        thread.call_args.kwargs["target"]()
                        self.assertEqual(start.call_count, int(semantic and not deferred))
                        self.assertIs(search.runtimes[runtime.collection.key], runtime)
                        if semantic and deferred:
                            search.start_faiss()
                            thread.call_args.kwargs["target"]()
                            start.assert_called_once_with()
                finally:
                    search._executor.shutdown(wait=True)

    def test_collection_refresh_loads_new_runtime_vectors_after_startup(self):
        search = _bare_search({})
        runtime = search.runtimes.pop("")
        batch = unittest.mock.Mock(
            collections=[runtime.collection], error=None
        )
        batch.open.return_value = runtime
        search.runtime_config = _zim_runtime_config(".")
        search.providers = {
            "zim": types.SimpleNamespace(discover=lambda *_args: batch)
        }
        try:
            with (
                patch.object(search, "_publish_runtime_sources", return_value=[]),
                patch.object(runtime, "start_faiss") as start,
            ):
                Search.refresh_collections(search, wait=True)
                start.assert_called_once_with()
                self.assertFalse(runtime.bindings["defer_faiss"])
        finally:
            search._executor.shutdown(wait=True)

    def test_open_scope_refresh_loads_new_runtime_vectors_after_startup(self):
        search = _bare_search({})
        search.open_scope = object()
        runtime = search.runtimes.pop("")
        batch = unittest.mock.Mock(collections=[runtime.collection])
        batch.open.return_value = runtime
        try:
            with (
                patch("zimantic.providers.zim.provider.open_zim_scope_batch", return_value=batch),
                patch.object(search, "_publish_runtime_sources", return_value=[]),
                patch.object(runtime, "start_faiss") as start,
            ):
                search._refresh_open_scope()
                start.assert_called_once_with()
                self.assertFalse(runtime.bindings["defer_faiss"])
        finally:
            search._executor.shutdown(wait=True)

    def test_fulltext_startup_shares_one_budget_across_declared_runtimes(self):
        class _IsolatedRuntime(IsolatedFulltextRuntime):
            def set_fulltext_pool(self, pool):
                self.pool = pool

        search = _bare_search({
            "libzim_search_processes": 6, "libzim_search_backend": "threads",
        })
        isolated = _IsolatedRuntime()
        second = _IsolatedRuntime()
        with patch("zimantic.fulltext.FulltextPool") as pool_factory:
            search.runtimes = {
                "isolated": isolated,
                "second": second,
                "other": types.SimpleNamespace(set_fulltext_pool=unittest.mock.Mock()),
            }
            try:
                search.start_fulltext_isolation()
                search.start_fulltext_isolation()
                pool_factory.assert_called_once_with(6, backend="threads")
                self.assertIs(isolated.pool, search.fulltext_pool)
                self.assertIs(second.pool, search.fulltext_pool)
                search.runtimes["other"].set_fulltext_pool.assert_not_called()
                late = _IsolatedRuntime()
                search._bind_runtime(late)
                self.assertIs(late.pool, search.fulltext_pool)
            finally:
                search.fulltext_pool.close()
                search._executor.shutdown(wait=True)


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
        index = IndexHandle(
            None,
            None,
            archive,
            None,
            threading.Lock(),
            zim_path="/tmp/manual.zim",
            has_fulltext_index=True,
        )

        result = _runtime_search(
            search,
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
        index = IndexHandle(
            None,
            None,
            None,
            None,
            threading.Lock(),
            zim_path="/tmp/manual.zim",
            has_fulltext_index=True,
        )

        result = _runtime_search(
            search,
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
    search.indexes = {"manual": IndexHandle(db, None, None, None, threading.Lock())}
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
    search.indexes = {"manual": IndexHandle(db, None, None, None, threading.Lock())}
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
            "manual": IndexHandle(None, None, archive, None, threading.Lock())
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

        class _PreviewRuntime(EnrichingRuntime):
            def preview(self, *args, **kwargs):
                return preview(*args, **kwargs)

            def enrich(self, *args, **kwargs):
                return enrich(*args, **kwargs)

        search.sources = {source.key: source}
        search.runtimes = {
            source.collection_key: _PreviewRuntime()
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

        def rank_results(source_results, query, limit, **kwargs):
            rank_sizes.append(len(source_results))
            return real_rank_results(source_results, query, limit, **kwargs)

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
            "manual": IndexHandle(db, FakeFaiss(), None, None, threading.Lock())
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
            docs = _fetch_zim_docs(
                source, db, {0}, web_base_path=search.web_base_path
            )
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
            docs = _fetch_zim_docs(
                source,
                db,
                set(range(1, 1802)),
                web_base_path=search.web_base_path,
            )
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
        result = _runtime_search(
            search,
            source,
            "above",
            _title_query("above"),
            _fulltext_query("above"),
            np.array([[1.0]]),
            1,
            IndexHandle(db, FakeFaiss(), None, None, threading.Lock()),
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
        result = _runtime_search(
            search,
            source,
            "unrelated",
            _title_query("unrelated"),
            _fulltext_query("unrelated"),
            np.array([[1.0]]),
            1,
            IndexHandle(db, FakeFaiss(), None, None, threading.Lock()),
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
            "manual": IndexHandle(
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
            "manual": IndexHandle(None, None, _Archive(), None, threading.Lock())
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
            "manual": IndexHandle(manual_db, None, None, None, threading.Lock()),
            "other": IndexHandle(other_db, None, None, None, threading.Lock()),
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
    rows = list(rows)
    db = sqlite3.connect(index_dir / f"{name}.sqlite")
    index_schema.ensure_schema(db)
    db.executemany(
        "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
        rows,
    )
    # Keep a matching archive for fixture searches that exercise article routes.
    zim = index_dir / f"{name}.zim"
    zim.write_bytes(b"zim")
    index_schema.write_meta(
        db,
        {
            index_schema.DONE: done,
            index_schema.FILESIZE: index_schema.encode_int(zim.stat().st_size),
            index_schema.SCHEMA_VERSION: str(index_schema.CURRENT_SCHEMA_VERSION),
            index_schema.PROVIDER: "zim",
            index_schema.COLLECTION_KEY: "",
            index_schema.SOURCE_KEY: name,
            index_schema.SOURCE_FINGERPRINT: "fixture-source",
            index_schema.BUILD_GENERATION: "fixture-generation",
            index_schema.INDEXED_COUNT: str(len(rows)),
            index_schema.TRUNCATED: "0",
            index_schema.INDEXED_CHANNELS: index_schema.encode_channels(
                ["title"] if done == "fast" else ["title", "body", "semantic"]
            ),
            **(
                {
                    index_schema.BODY_TRUNCATED_COUNT: "0",
                    index_schema.SEMANTIC_VECTOR_COUNT: "0",
                }
                if done == "full" else {}
            ),
        },
    )
    db.commit()
    db.close()
    return index_dir / f"{name}.sqlite"


def _attach_index_fixtures(search):
    """Attach current-schema SQLite fixtures to the test runtime."""
    for db_path in sorted(Path(search.cfg["index_dir"]).glob("*.sqlite")):
        db = sqlite3.connect(db_path, check_same_thread=False)
        meta = index_schema.read_meta(db)
        done = meta.get(index_schema.DONE, "")
        if done not in index_schema.DONE_VALUES:
            db.close()
            continue
        name = db_path.stem
        source_size = index_schema.decode_int(
            meta.get(index_schema.FILESIZE, "0")
        )
        zim_path = db_path.with_suffix(".zim")
        search.indexes[name] = IndexHandle(
            db,
            None,
            None,
            None,
            threading.Lock(),
            db_path=str(db_path),
            done=done,
            source_size=source_size,
            zim_path=str(zim_path) if zim_path.exists() else None,
            has_fulltext_index=False,
        )
        search.sources[name] = SourceInfo(
            name,
            provider="zim",
            title=name,
            searchable=True,
            size=source_size,
        )


class GracefulDegradationTests(unittest.TestCase):
    def _cfg(self, tmp):
        return {
            "index_dir": str(tmp),
            "zim_dir": str(tmp),
            "candidate_count": 16,
        }

    def test_close_releases_fixture_index_connections(self):
        with _temp_dir(self) as tmp:
            _write_index(tmp, "manual")
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            _attach_index_fixtures(search)
            index = search.indexes["manual"]
            worker_db = search._executor.submit(index.db_for_thread).result(timeout=5)

            search.close()

            for db in (index.db, worker_db):
                with self.assertRaises(sqlite3.ProgrammingError):
                    db.execute("SELECT 1")
            self.assertEqual(search.indexes, {})

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
            _attach_index_fixtures(search)
            search.refresh_sources()

            self.assertIn("manual", search.indexes)
            self.assertFalse(search.indexes["manual"].semantic)

            results = _search_results(search, "tire change", limit=1)
            self.assertEqual(results[0]["title"], "Tire Change")
            self.assertEqual(results[0]["path"], "tire-change")
            search._executor.shutdown(wait=True)

    def test_index_vectors_built_with_a_different_model_are_ignored(self):
        from zimantic.indexing.locator import IndexLocator
        from zimantic.providers.zim.discovery import (
            ZimIndexOptions,
            open_generation_index,
        )

        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            db_path = _write_index(
                tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)]
            )
            db = sqlite3.connect(db_path)
            index_schema.write_meta(
                db, {index_schema.MODEL_FINGERPRINT: "old-model"}
            )
            db.commit()
            db.close()
            db_path.with_suffix(".faiss").write_bytes(b"vectors")
            source = _zim_source("manual", "Manual")
            indexes = tmp / "indexes"
            lease = IndexLocator(indexes).publish(
                source,
                db_path,
                db_path.with_suffix(".faiss"),
                metadata={"source_size": "3"},
            )
            lease.close()
            options = ZimIndexOptions(
                semantic=True,
                model_fingerprint="new-model",
                probe_compatible=lambda db, path: (False, "no compatibility probes"),
            )
            with self.assertLogs("zimantic.providers.zim.discovery", level="WARNING") as logs:
                index = open_generation_index(
                    source,
                    index_dir=indexes,
                    options=options,
                    expected_source_size=3,
                )

            self.assertIsNotNone(index)
            self.assertIsNone(index.faiss_index)
            self.assertTrue(any("different model" in line for line in logs.output))
            index.close()

    def test_matching_probes_allow_vectors_from_another_model_file(self):
        from zimantic.indexing.locator import IndexLocator
        from zimantic.providers.zim.discovery import (
            ZimIndexOptions,
            open_generation_index,
        )

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
            source = _zim_source("manual", "Manual")
            db = sqlite3.connect(db_path)
            index_schema.write_meta(
                db, {index_schema.MODEL_FINGERPRINT: "old-model"}
            )
            from zimantic.embed import serialize_model_probes

            for key, value in serialize_model_probes(
                ["first probe", "second probe"], probes
            ).items():
                db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            db.commit()
            db.close()
            db_path.with_suffix(".faiss").write_bytes(b"vectors")

            search = _bare_search(self._cfg(tmp))
            search.embedder = _ProbeEmbedder(probes)
            try:
                lease = IndexLocator(tmp / "indexes").publish(
                    source,
                    db_path,
                    db_path.with_suffix(".faiss"),
                    metadata={"source_size": "3"},
                )
                lease.close()
                options = ZimIndexOptions(
                    semantic=True,
                    model_fingerprint="new-model",
                    probe_compatible=lambda db, path: _probe_compatible(
                        search, db, path
                    ),
                )
                import faiss

                with (
                    patch.object(
                        faiss, "read_index", return_value=object()
                    ),
                    self.assertLogs(
                        "zimantic.providers.zim.discovery", level="WARNING"
                    ) as logs,
                ):
                    index = open_generation_index(
                        source,
                        index_dir=tmp / "indexes",
                        options=options,
                        expected_source_size=3,
                    )
            finally:
                search._executor.shutdown(wait=True)

            self.assertIsNotNone(index)
            self.assertIsNotNone(index.faiss_index)
            self.assertTrue(any("probes match" in line for line in logs.output))
            index.close()

    def test_concurrent_probe_checks_share_cached_embeddings(self):
        from zimantic.embed import serialize_model_probes

        probes = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32)
        metadata = serialize_model_probes(
            ["first probe", "second probe"], probes
        )
        # Concurrent index opens use separate connections, not one shared DB.
        dbs = [
            sqlite3.connect(":memory:", check_same_thread=False) for _ in range(2)
        ]
        for db in dbs:
            index_schema.ensure_schema(db)
            db.executemany("INSERT INTO meta VALUES (?, ?)", metadata.items())
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
            if not release.wait(10):
                raise RuntimeError("test probe release timed out")
            return probes

        executor = ThreadPoolExecutor(max_workers=2)
        futures = []
        try:
            with patch("zimantic.embed.embed_probe_texts", side_effect=embed_probe):
                futures = [
                    executor.submit(
                        _probe_compatible,
                        search,
                        db,
                        Path("index.sqlite"),
                    )
                    for db in dbs
                ]
                self.assertTrue(started.wait(10))
                self.assertFalse(second_call.wait(2))
        finally:
            release.set()
            executor.shutdown(wait=True)
            for db in dbs:
                db.close()

        results = [future.result() for future in futures]
        self.assertEqual(calls, 1)
        self.assertTrue(all(result[0] for result in results))

    def test_mismatched_probes_keep_vectors_disabled(self):
        from zimantic.indexing.locator import IndexLocator
        from zimantic.providers.zim.discovery import (
            ZimIndexOptions,
            open_generation_index,
        )

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
            source = _zim_source("manual", "Manual")
            db = sqlite3.connect(db_path)
            index_schema.write_meta(
                db, {index_schema.MODEL_FINGERPRINT: "old-model"}
            )
            from zimantic.embed import serialize_model_probes

            for key, value in serialize_model_probes(
                ["first probe", "second probe"], stored
            ).items():
                db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            db.commit()
            db.close()
            db_path.with_suffix(".faiss").write_bytes(b"vectors")

            search = _bare_search(self._cfg(tmp))
            search.embedder = _ProbeEmbedder(current)
            try:
                lease = IndexLocator(tmp / "indexes").publish(
                    source,
                    db_path,
                    db_path.with_suffix(".faiss"),
                    metadata={"source_size": "3"},
                )
                lease.close()
                options = ZimIndexOptions(
                    semantic=True,
                    model_fingerprint="new-model",
                    probe_compatible=lambda db, path: _probe_compatible(
                        search, db, path
                    ),
                )
                with self.assertLogs(
                    "zimantic.providers.zim.discovery", level="WARNING"
                ) as logs:
                    index = open_generation_index(
                        source,
                        index_dir=tmp / "indexes",
                        options=options,
                        expected_source_size=3,
                    )
            finally:
                search._executor.shutdown(wait=True)

            self.assertIsNotNone(index)
            self.assertIsNone(index.faiss_index)
            self.assertTrue(any("different model" in line for line in logs.output))
            index.close()


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
        library = search.library_dicts()

        self.assertEqual([source["key"] for source in library], ["broken"])
        self.assertFalse(library[0]["searchable"])
        self.assertEqual(library[0]["title"], "A broken book")
        search._executor.shutdown(wait=True)

    def test_listing_does_not_refresh_provider_discovery(self):
        search = _bare_search({})
        self.addCleanup(search.close)
        search.sources = {"manual": _zim_source("manual", "Manual")}
        runtime = search.runtimes[""]
        with patch.object(runtime, "refresh") as refresh:
            self.assertEqual(search.library_dicts()[0]["key"], "manual")
            snapshot = search.listing_snapshot()
        refresh.assert_not_called()
        self.assertEqual(snapshot["library"][0]["key"], "manual")
        self.assertEqual(snapshot["sources"][0]["key"], "manual")
        self.assertEqual(snapshot["token"], ("test", 0))


    def test_failed_archive_open_is_not_retried(self):
        with _temp_dir(self) as tmp:
            index = IndexHandle(
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


    def test_finished_stream_is_cached_for_search_method(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire-change", None)])
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            _attach_index_fixtures(search)
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
            _attach_index_fixtures(search)
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
            _attach_index_fixtures(search)
            search.refresh_sources()
            list(search.stream_search("tire change", limit=1))
            key = search._cache_key("tire change", None)
            self.assertIsNotNone(search.cache.get(key))

            search.reload()
            self.assertIsNotNone(search.cache.get(key))
            search._executor.shutdown(wait=True)



class RandomArticleServingTests(unittest.TestCase):
    def test_fallback_uses_public_get_entry_by_id(self):
        class _Archive:
            entry_count = 1

            @staticmethod
            def get_entry_by_id(_index):
                return _Entry()

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

        _bare_search({})
        from zimantic.content.serving import random_fallback_article

        self.assertEqual(random_fallback_article(_Index()), "Article")


class BrowseSearchTests(unittest.TestCase):
    def make_search(self):
        search = _bare_search(
            {"page_size": 2, "max_results": 5, "preview_limit": 0}
        )
        self.addCleanup(search.close)
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.executescript(index_schema.SCHEMA)
        db.executemany(
            "INSERT INTO docs(rowid,title,path) VALUES (?,?,?)",
            [(i, f"Topic {i}", f"item-{i}") for i in range(1, 21)],
        )
        search.sources = {"manual": _zim_source("manual", "Manual")}
        search.indexes = {"manual": IndexHandle(db, None)}
        return search

    def test_browse_pages_reuse_a_seeded_pool_without_query_embedding(self):
        search = self.make_search()
        with patch.object(
            search, "_query_vector", side_effect=AssertionError("must not embed")
        ):
            first = search.search_page("", limit=2, browse="random", seed=7)
            second = search.search_page(
                "", limit=2, offset=2, browse="random", seed=7
            )
            whole = search.search_page("", limit=5, browse="random", seed=7)
            reshuffled = search.search_page("", limit=5, browse="random", seed=19)
        self.assertEqual(
            [doc["id"] for doc in first["results"] + second["results"]],
            [doc["id"] for doc in whole["results"][:4]],
        )
        self.assertNotEqual(whole["results"], reshuffled["results"])
        self.assertGreaterEqual(search.cache.hits, 2)

    def test_empty_search_respects_fast_mode_and_source_scope(self):
        search = self.make_search()
        search.semantic = False
        self.assertTrue(search.search_page("")["results"])
        with self.assertRaises(SearchQueryError):
            search.search_page("", browse="outliers")
        with self.assertRaises(SearchQueryError):
            search.search_page("", source="missing", browse="random")
        with self.assertRaises(SearchQueryError):
            search.search_page("topic", browse="rare")
        search.sources["remote"] = SourceInfo(
            key="remote", provider="kiwix", searchable=True
        )
        self.assertEqual(
            search.search_page("", source="remote", browse="random")["results"], []
        )
        self.assertEqual(
            search.search_page("", source=[], browse="random")["results"], []
        )
        rare = search.search_page("", source="manual", browse="rare")
        self.assertTrue(rare["results"])
        self.assertTrue(all(doc["source_key"] == "manual" for doc in rare["results"]))

    def test_empty_search_defaults_to_adaptive_diversity_without_embedding_a_query(self):
        import faiss

        search = self.make_search()
        vectors = np.array([[1, 0], [0.8, 0.6], [-0.6, 0.8]], dtype=np.float32)
        index = faiss.IndexIDMap(faiss.IndexFlatIP(2))
        index.add_with_ids(vectors, np.array([1, 2, 3], dtype=np.int64))
        search.indexes["manual"].faiss_index = index
        with patch.object(search, "_query_vector", side_effect=AssertionError("must not embed")):
            page = search.search_page("  ")
        self.assertEqual(page["results"][0]["path"], "item-2")

    def test_text_sorts_reorder_matches_and_keep_paging_and_cache_isolated(self):
        import faiss

        search = self.make_search()
        search.candidate_count = 32
        search.max_results = 32
        angles = np.linspace(-1, 1, 20)
        vectors = np.column_stack([np.cos(angles), np.sin(angles)]).astype(np.float32)
        index = faiss.IndexIDMap(faiss.IndexFlatIP(2))
        index.add_with_ids(vectors, np.arange(1, 21, dtype=np.int64))
        search.indexes["manual"].faiss_index = index
        with patch.object(search, "_query_vector", return_value=None):
            relevance = search.search_page("topic", limit=20)["results"]
            outliers = search.search_page("topic", sort="outliers", limit=20)["results"]
            rare = search.search_page("topic", sort="rare", limit=20)["results"]
            diverse = search.search_page("topic", sort="diverse", limit=20)["results"]
            first = search.search_page("topic", sort="random", seed=7, limit=2)
            second = search.search_page("topic", sort="random", seed=7, limit=2, offset=2)
            whole = search.search_page("topic", sort="random", seed=7, limit=20)
            reshuffled = search.search_page("topic", sort="random", seed=19, limit=20)
            cached_relevance = search.search_page("topic", limit=20)["results"]
        expected = {doc["id"] for doc in relevance}
        for items in (outliers, rare, diverse, whole["results"]):
            self.assertEqual({doc["id"] for doc in items}, expected)
        self.assertIn(outliers[0]["path"], ("item-1", "item-20"))
        self.assertEqual(first["results"] + second["results"], whole["results"][:4])
        self.assertNotEqual(whole["results"], reshuffled["results"])
        self.assertEqual(relevance, cached_relevance)

    def test_diversity_scores_the_global_pool_without_source_size_bias(self):
        search = self.make_search()
        pool = [
            {"id": "best", "source_key": "small", "title": "Solar power"},
            {"id": "duplicate", "source_key": "large", "title": "Solar power"},
            {"id": "wind", "source_key": "large", "title": "Wind turbines"},
            {"id": "battery", "source_key": "large", "title": "Battery storage"},
        ]
        ordered = search._sort_results(pool, "diverse", 0, None)
        self.assertEqual(
            [doc["id"] for doc in ordered], ["best", "wind", "battery", "duplicate"],
        )
        self.assertEqual(
            [doc["ranking"]["final_rank"] for doc in ordered], [1, 2, 3, 4],
        )
        self.assertTrue(all("ranking" not in doc for doc in pool))
        self.assertEqual(search._sort_results(pool, "diverse", 0, time.monotonic() - 1), pool)

    def test_empty_diversity_avoids_cross_source_repeats_and_preserves_balance(self):
        search = self.make_search()
        results = []
        for key, titles in (("a", ["Solar power", "Wind turbines"]),
                            ("b", ["Solar power", "Battery storage"])):
            source = _zim_source(key, key)
            results.append(SourceResult(source=source, items=[
                {
                    "id": f"{key}-{rank}", "source_key": key, "title": title,
                    "source_rank": rank, "_browse_order": key, "score": 1.0,
                    "ranking": {"browse": {"mode": "diverse"}},
                }
                for rank, title in enumerate(titles, 1)
            ]))
        ordered = search._rank_results(results, "", 3)
        self.assertEqual([doc["title"] for doc in ordered], [
            "Solar power", "Battery storage", "Wind turbines",
        ])
        self.assertEqual([doc["source_key"] for doc in ordered[:2]], ["a", "b"])

    def test_semantic_diversity_reuses_query_vectors_and_retains_unscored_matches(self):
        import faiss

        search = self.make_search()
        vectors = np.array([[1, 0], [1, 0], [0.8, 0.6], [-1, 0]], dtype=np.float32)
        index = faiss.IndexIDMap(faiss.IndexFlatIP(2))
        index.add_with_ids(vectors, np.array([1, 2, 3, 4], dtype=np.int64))
        search.indexes["manual"].faiss_index = index
        pool = [
            {"id": str(i), "source_key": "manual", "path": f"item-{i}", "title": title}
            for i, title in enumerate(["Cars", "Automobiles", "Wind turbines", "Unrelated", "Missing vector"], 1)
        ]
        ordered = search._sort_results(
            pool, "diverse", 0, None,
            query_vector=np.array([[1, 0]], dtype=np.float32),
        )
        self.assertEqual([doc["id"] for doc in ordered], ["1", "3", "2", "4", "5"])
        with patch.object(search, "_query_vector", return_value=vectors[:1]) as embed:
            first = search.search_page("topic", sort="diverse", limit=2)
            second = search.search_page("topic", sort="diverse", offset=2, limit=2)
            whole = search.search_page("topic", sort="diverse", limit=5)
        self.assertEqual(embed.call_count, 1)
        self.assertEqual(first["results"] + second["results"], whole["results"][:4])

    def test_empty_semantic_diversity_uses_vectors_without_embedding_a_query(self):
        import faiss

        search = self.make_search()
        vectors = np.array([[1, 0], [1, 0], [0.8, 0.6], [-1, 0]], dtype=np.float32)
        index = faiss.IndexIDMap(faiss.IndexFlatIP(2))
        index.add_with_ids(vectors, np.array([1, 2, 3, 4], dtype=np.int64))
        search.indexes["manual"].faiss_index = index
        with patch.object(search, "_query_vector", side_effect=AssertionError("must not embed")):
            whole = search.search_page("", sort="diverse", limit=5)
            first = search.search_page("", sort="diverse", limit=2)
            second = search.search_page("", sort="diverse", limit=2, offset=2)
        self.assertEqual(len(whole["results"]), 5)
        self.assertEqual(first["results"] + second["results"], whole["results"][:4])
        self.assertNotEqual(
            {doc["path"] for doc in whole["results"][:2]}, {"item-1", "item-2"},
        )
        search.semantic = False
        self.assertTrue(search.search_page("", sort="diverse")["results"])

    def test_diversity_interleaves_lexical_fallback_for_missing_and_zero_vectors(self):
        import faiss

        search = self.make_search()
        vectors = np.array([[1, 0], [1, 0], [0.8, 0.6], [0, 0]], dtype=np.float32)
        index = faiss.IndexIDMap(faiss.IndexFlatIP(2))
        index.add_with_ids(vectors, np.array([1, 2, 3, 6], dtype=np.int64))
        search.indexes["manual"].faiss_index = index
        pool = [
            {"id": str(i), "source_key": "manual", "path": f"item-{i}", "title": title, "score": 1.0}
            for i, title in (
                (1, "Cars"), (5, "Solar power"), (2, "Automobiles"),
                (6, "Solar power"), (3, "Wind turbines"), (7, "Battery storage"),
            )
        ]
        ordered = search._sort_results(
            pool, "diverse", 0, None, query_vector=np.array([[1, 0]], dtype=np.float32),
        )
        self.assertEqual([doc["id"] for doc in ordered], ["1", "5", "3", "7", "2", "6"])
        search.semantic = False
        with patch("zimantic.search.diversity_vectors", side_effect=AssertionError("must not use embeddings")):
            lexical = search._sort_results(pool, "diverse", 0, None)
        self.assertEqual(
            [doc["id"] for doc in lexical],
            ["1", "5", "2", "3", "7", "6"],
        )

    def test_semantic_diversity_preserves_hybrid_relevance_penalties(self):
        import faiss

        search = self.make_search()
        vectors = np.array([[0.8, 0.6], [1, 0], [0.6, 0.8]], dtype=np.float32)
        index = faiss.IndexIDMap(faiss.IndexFlatIP(2))
        index.add_with_ids(vectors, np.array([1, 2, 3], dtype=np.int64))
        search.indexes["manual"].faiss_index = index
        pool = [
            {"id": "guide", "path": "item-1", "title": "Linux guide", "score": 0.04},
            {"id": "profile", "path": "item-2", "title": "User Linux", "score": 0.004},
            {"id": "install", "path": "item-3", "title": "Installing Linux", "score": 0.03},
        ]
        pool = [{**doc, "source_key": "manual"} for doc in pool]
        ordered = search._sort_results(
            pool, "diverse", 0, None,
            query_vector=np.array([[1, 0]], dtype=np.float32),
        )
        self.assertEqual(ordered[0]["id"], "guide")
        self.assertIsNone(search.indexes["manual"].browse_centroid)
        self.assertEqual({doc["id"] for doc in ordered}, {"guide", "profile", "install"})

    def test_semantic_diversity_compares_embeddings_across_balanced_sources(self):
        import faiss

        search = self.make_search()
        other_db = sqlite3.connect(":memory:")
        other_db.executescript(index_schema.SCHEMA)
        other_db.executemany(
            "INSERT INTO docs(rowid,title,path) VALUES (?,?,?)",
            [(1, "Automobiles", "item-1"), (2, "Battery storage", "item-2")],
        )
        self.addCleanup(other_db.close)
        search.sources["other"] = _zim_source("other", "Other")
        search.indexes["other"] = IndexHandle(other_db, None)
        results = []
        for key, titles, vectors in (
            ("manual", ["Cars", "Wind turbines"], [[1, 0, 0], [0.8, 0.6, 0]]),
            ("other", ["Automobiles", "Battery storage"], [[1, 0, 0], [0.8, 0, 0.6]]),
        ):
            index = faiss.IndexIDMap(faiss.IndexFlatIP(3))
            index.add_with_ids(np.array(vectors, dtype=np.float32), np.array([1, 2], dtype=np.int64))
            search.indexes[key].faiss_index = index
            results.append(SourceResult(source=search.sources[key], items=[
                {
                    "id": f"{key}-{rank}", "source_key": key, "title": title,
                    "path": f"item-{rank}", "source_rank": rank, "_browse_order": key,
                    "score": 1.0, "ranking": {"browse": {"mode": "diverse"}},
                }
                for rank, title in enumerate(titles, 1)
            ]))
        ordered = search._rank_results(results, "", 4)
        self.assertEqual(len({doc["source_key"] for doc in ordered[:2]}), 2)
        self.assertNotEqual(
            {doc["title"] for doc in ordered[:2]}, {"Cars", "Automobiles"},
        )
        self.assertEqual(
            {doc["id"] for doc in ordered},
            {doc["id"] for result in results for doc in result.items},
        )
        search.indexes["other"].faiss_index = None
        results[1].items[0]["title"] = "Cars"
        mixed = search._rank_results(results, "", 4)
        self.assertEqual([doc["source_key"] for doc in mixed[:2]], ["manual", "other"])
        self.assertEqual([doc["title"] for doc in mixed[:2]], ["Cars", "Battery storage"])
        self.assertEqual({doc["id"] for doc in mixed}, {doc["id"] for doc in ordered})

    def test_text_sorting_preserves_the_relevance_enrichment_candidates(self):
        search = self.make_search()
        with (
            patch.object(search, "_query_vector", return_value=None),
            patch.object(search, "_enrich_pool", side_effect=lambda results, query, pool, limit: pool[:3]),
        ):
            relevance = search.search_page("topic", limit=5)["results"]
            random = search.search_page("topic", sort="random", seed=7, limit=5)["results"]
        self.assertEqual(
            {doc["id"] for doc in random}, {doc["id"] for doc in relevance}
        )

    def test_semantic_browse_recovers_when_deferred_vectors_become_available(self):
        import faiss

        search = self.make_search()
        self.assertFalse(search.search_page("", browse="outliers")["results"])
        vectors = np.array([[1, 0], [0.8, 0.6], [-0.6, 0.8]], dtype=np.float32)
        index = faiss.IndexIDMap(faiss.IndexFlatIP(2))
        index.add_with_ids(vectors, np.array([1, 2, 3], dtype=np.int64))
        search.indexes["manual"].faiss_index = index
        results = search.search_page("", browse="outliers")["results"]
        self.assertTrue(results)
        self.assertEqual(results[0]["path"], "item-3")

    def test_diversity_refreshes_cached_pools_when_deferred_vectors_become_available(self):
        import faiss

        vectors = np.array([[1, 0], [0.8, 0.6], [-0.6, 0.8]], dtype=np.float32)
        for query in ("", "topic"):
            with self.subTest(query=query):
                search = self.make_search()
                with patch.object(search, "_query_vector", return_value=vectors[:1]):
                    search.search_page(query, sort="diverse")
                    search.search_page(query, sort="diverse")
                    hits = search.cache.hits
                    self.assertGreater(hits, 0)
                    index = faiss.IndexIDMap(faiss.IndexFlatIP(2))
                    index.add_with_ids(vectors, np.array([1, 2, 3], dtype=np.int64))
                    search.indexes["manual"].faiss_index = index
                    page = search.search_page(query, sort="diverse")
                self.assertEqual(search.cache.hits, hits)
                self.assertTrue(page["results"])
                if not query:
                    self.assertEqual(page["results"][0]["path"], "item-2")


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
            _attach_index_fixtures(search)
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
            _attach_index_fixtures(search)
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
            _attach_index_fixtures(search)
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
            _attach_index_fixtures(search)
            search.refresh_sources()

            page = search.search_page("tire change", limit=10, source="beta")

            self.assertEqual([doc["source_key"] for doc in page["results"]], ["beta"])
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["counts"], {"beta": 1})

            page = search.search_page("tire change", limit=10, source=["alpha", "beta"])

            self.assertEqual(
                [doc["source_key"] for doc in page["results"]],
                ["alpha", "beta"],
            )
            self.assertEqual(page["total"], 2)
            search._executor.shutdown(wait=True)

    def test_selective_search_retrieves_deeper_and_reuses_its_own_pool(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            rows = [(i, f"Tire Change {i}", f"tire-{i}", None) for i in range(1, 21)]
            for name in ("alpha", "beta", "gamma", "delta"):
                _write_index(tmp, name, rows=rows)
            search = _bare_search({**self._cfg(tmp), "candidate_count": 2, "max_results": 20})
            self.addCleanup(search.close)
            _attach_index_fixtures(search)
            search.refresh_sources()

            broad = search.search_page("tire change")
            with patch.object(search, "_search_source", wraps=search._search_source) as retrieve:
                selected = search.search_page("tire change", source="beta")
            self.assertEqual(retrieve.call_count, 1)
            self.assertGreater(selected["total"], broad["counts"].get("beta", 0))
            self.assertEqual(selected["total"], 20)
            self.assertEqual(selected["scope"]["requested"], 1)
            self.assertEqual(set(selected["counts"]), {"beta"})

            with patch.object(search, "_search_source", wraps=search._search_source) as retrieve:
                cached = list(search.stream_search(
                    "tire change", source="beta", limit=5, offset=5
                ))[-1]
            self.assertEqual(retrieve.call_count, 0)
            self.assertEqual(cached["total"], 20)
            self.assertEqual(cached["offset"], 5)
            self.assertEqual({doc["source_key"] for doc in cached["results"]}, {"beta"})

    def test_concurrent_identical_searches_share_one_computation(self):
        with _temp_dir(self) as tmp:
            tmp = Path(tmp)
            _write_index(tmp, "manual", rows=[(1, "Tire Change", "tire", None)])
            search = _bare_search(self._cfg(tmp))
            self.addCleanup(search.close)
            _attach_index_fixtures(search)
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
            _attach_index_fixtures(search)
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
        _attach_index_fixtures(search)
        search.refresh_sources()
        return search


    def test_search_uses_an_in_memory_catalog_without_index_dir(self):
        with _temp_dir(self) as tmp:
            search = Search(
                _zim_runtime_config(tmp),
                semantic=False,
                archive_factory=object,
            )
            self.addCleanup(search.close)

            runtime = next(iter(search.runtimes.values()))
            self.assertEqual(runtime.catalog.db_path, Path(":memory:"))

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
        from zimantic.content.path import normalize_article_path

        self.assertEqual(normalize_article_path(path), path)

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
            doc = _fetch_zim_docs(
                source, db, {1}, web_base_path=search.web_base_path
            )[1]
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
            doc = _fetch_zim_docs(
                source, db, {1}, web_base_path=search.web_base_path
            )[1]
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
                "manual": IndexHandle(
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
                        IndexHandle, "ensure_open", return_value=fake_archive
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
        index = IndexHandle(
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
            result = _runtime_search(
                search, source, "query", "", "query", None, 10, index
            )
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
                _zim_runtime_config(Path(tmp) / "missing"),
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
                "candidate_count": 16,
                "page_size": 50,
                "max_results": 10,
            }
            search = Search(
                RuntimeConfig(settings=cfg, page_size=50),
                semantic=False, archive_factory=object,
            )
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
                "candidate_count": 16,
                "page_size": 10,
                "max_results": 100,
            }
            search = Search(_zim_runtime_config(index_dir, **cfg), semantic=False)
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


if __name__ == "__main__":
    unittest.main()
