# Plan 02: Finish Decomposing `zimantic/search.py`

## Goal
Split the 4,312-line `zimantic/search.py` monolith into focused modules under `zimantic/searching/` and `zimantic/content/` while keeping `Search` as the state/locking coordinator and preserving all existing `zimantic.search` imports and monkeypatch targets.

## Critical Constraint: Test Compatibility
Tests in `tests/` import or monkeypatch symbols directly on `zimantic.search` (for example `zimantic.search.Archive`, `zimantic.search.urlopen`, `zimantic.search.get_entry_by_id`, `zimantic.search._rank_results`, etc.).
- Every function or constant moved out of `zimantic/search.py` **must remain imported in `zimantic/search.py`**.
- Any moved function that calls `Archive`, `get_entry_by_id`, or `urlopen` must accept that callable as a default parameter (or look it up via `zimantic.search` / pass it from `Search` methods) so `monkeypatch.setattr("zimantic.search.Archive", ...)` and `monkeypatch.setattr("zimantic.search.urlopen", ...)` in tests still intercept the calls.

## Step-by-Step Instructions

### Step 1: Check all `zimantic.search` references in `tests/`
1. Use `grep` on `tests/` for `zimantic.search` and `from zimantic.search import`.
2. Record every private helper and module-level symbol accessed or monkeypatched by tests.

### Step 2: Extract RRF Ranking & Phrase Scoring to `zimantic/searching/ranking.py`
1. View `zimantic/search.py` lines 536–880 and 3310–3505.
2. Create `zimantic/searching/ranking.py` containing:
   - Constants: `RRF_K`, `SEMANTIC_WEIGHT`, `KEYWORD_WEIGHT`, `FULLTEXT_WEIGHT`, `SHORT_FULLTEXT_WEIGHT`, `EXACT_TITLE_BOOST`, `NORMALIZED_TITLE_BOOST`, `NEAR_EXACT_TITLE_BOOST`, `PREFIX_TITLE_BOOST`, `QUERY_COVERAGE_BOOST`, `ORDERED_TERMS_BOOST`, `PHRASE_BOOST`, `CONTENT_PHRASE_BOOST`, `TITLE_POLLUTION_PENALTY`, `STOPWORD_HEAVY_PENALTY`, `LOW_VALUE_TITLE_PENALTY`, `UNTITLED_PENALTY`, `NUMERIC_PATH_PENALTY`, `MULTI_LIST_BOOST`, `MAX_PHRASE_CANDIDATES`, `_LOW_VALUE_PREFIXES`, `_PHRASE_MIMETYPES`.
   - Dataclass & pure functions:
     - `_RankedAssessment`
     - `_collect_ranked_candidates`
     - `_rrf_scores`
     - `_assess_ranked_document`
     - `_render_ranked_document`
     - `rank_results(source_results, query, limit, candidate_count)`
     - `_matched_by(pool)`
     - `_source_items(semantic, keyword, fulltext)`
3. Re-export these symbols in `zimantic/search.py` and have `Search._rank_results`, `Search._matched_by`, and `Search._source_items` delegate to them.
4. Run `pytest tests/test_search.py -q`.

### Step 3: Extract Kiwix Remote OPDS & XML Search to `zimantic/searching/kiwix.py`
1. View `zimantic/search.py` lines 250–390 (`_parse_remote_xml`, `_read_limited_response`, `_local_name`, `_child_text`, `_opds_SearchDescription_template`, etc.), lines 1530–1635 (`_discover_kiwix_sources`), and lines 3424–3495 (`_search_kiwix`).
2. Create `zimantic/searching/kiwix.py` containing:
   - XML/OPDS helpers: `_parse_remote_xml`, `_read_limited_response`, `_local_name`, `_child_text`.
   - `discover_kiwix_sources(cfg, source_timeout, urlopen_fn=urlopen) -> list[SourceInfo]`
   - `search_kiwix(source, query, limit, cfg, candidate_count, source_timeout, preview_fn, urlopen_fn=urlopen) -> SourceResult`
3. In `zimantic/search.py`:
   - Re-export the XML helpers.
   - In `Search._discover_kiwix_sources` and `Search._search_kiwix`, pass `urlopen_fn=urlopen` (referencing `zimantic.search.urlopen`) so any test monkeypatching `zimantic.search.urlopen` continues to intercept HTTP calls.
4. Run `pytest tests/test_search.py tests/test_server.py -q`.

### Step 4: Extract Archive Article, Icon, and Random Entry Readers to `zimantic/content/serving.py`
1. View `zimantic/search.py` lines 2100–2525 (`_article_path`, `_resolve_article_entry`, `_read_archive_article`, `_read_archive_icon`, `_is_random_article_entry`, `_random_indexed_article`, `_random_fallback_article`).
2. Create `zimantic/content/serving.py` containing the archive-level helper functions:
   - `normalize_article_path(path: str) -> str`
   - `resolve_article_entry(archive: Any, requested_path: str | None = None)`
   - `read_archive_article(archive: Any, zim_name: str, path: str | None, head_only: bool, max_bytes: int) -> dict[str, Any]`
   - `read_archive_icon(archive: Any, zim_name: str, icon_path: str | None, max_article_bytes: int) -> dict[str, Any]`
   - `is_random_article_entry(entry: Any) -> bool`
   - `random_indexed_article(index: Any, db: sqlite3.Connection, attempts: int = RANDOM_ARTICLE_ATTEMPTS) -> str | None`
   - `random_fallback_article(index: Any, attempts: int = RANDOM_ARTICLE_ATTEMPTS, get_entry_fn: Any = get_entry_by_id) -> str | None`
3. Keep `Search.read_article`, `Search.read_article_head`, `Search.article_path_exists`, `Search.read_source_icon`, `Search.random_local_source`, and `Search.random_article_path` on `Search` as thin locking/lifecycle wrappers that call into `zimantic/content/serving.py` (passing `get_entry_fn=get_entry_by_id` from `zimantic.search` so monkeypatches work).
4. Run `pytest -q`.
