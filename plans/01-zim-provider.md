# Phase 01 — ZIM provider

Status: proposed.

## Goal

Move all ZIM-specific discovery, index lifecycle, and per-source search behind
the phase-00 provider seam. ZIM becomes the reference implementation. No user-
visible behavior changes.

## Current state

`zimantic/search.py` mixes core search with ZIM behavior:

- Discovery: `_scan_zim_paths`, `_discover_local_indexes`, `_load_indexes`,
  `_rebuild_sources`, `_source_from_catalog`, `_choose_sources`.
- Index lifecycle: `_open_index`, `_open_zim_index`, `_read_index_state`,
  `_model_probes_compatible`, `_stale_zim_archive`, `start_faiss`,
  `start_fulltext_isolation`.
- Search: `_search_source` (`mode == "kiwix"` branch), `_search_local`,
  `_search_local_locked`, `_fetch_docs`, `_fetch_archive_docs`,
  `_extract_previews`.
- Serving: `read_article`, `read_article_head`, `article_path_exists`,
  `read_source_icon`, `random_article_path`.
- Metadata: `ZimCatalog` in `zimantic/catalog.py`.

`zimantic/searching/index.py` already isolates `_LocalIndex` (database, FAISS,
archive, searcher, locks). Keep that object in this phase; phase 04 generalizes
it.

## Target shape

```
zimantic/providers/zim/
    __init__.py
    provider.py      # ZimProvider, ZimRuntime
    discovery.py     # catalog + index discovery, source identity
    search.py        # one-source ZIM search (moved from Search)
    serving.py       # thin adapter over content/serving.py (phase 03 removes it)
    metadata.py      # SourceInfo construction, intent phrases, icons
```

### Collection and source mapping

- **Collection** = the configured ZIM root (`zim_dir`, and later an explicit
  `[[sources]] type = "zim"` root). Key is stable and opaque. Use `"zim"` for
  the legacy `zim_dir` so existing library grouping is predictable.
- **Source** = one ZIM archive, exactly as today. Keys, names, `book`,
  `local_name`, collision suffixes, dated/flavour collapsing, and
  `perma_ref` behavior stay unchanged.
- ZIM source identity is fixed by the archive. The global source budgets are
  therefore fan-out and pagination budgets for ZIM, not instructions to merge
  multiple archives into a synthetic source.
- `SourceInfo.provider = "zim"`, `content_kind = "zim"`, `mode = "local"`,
  `collection_key` / `collection_label` populated.
- `SourceInfo.intent_phrases` comes from the existing `_intent_phrases`.

### Runtime

```python
class ZimRuntime:
    collection: CollectionInfo
    sources: Sequence[SourceInfo]
    catalog: ZimCatalog
    indexes: dict[str, _LocalIndex]

    def search(self, source, request) -> SourceResult: ...
    def library_sources(self) -> Sequence[SourceInfo]: ...
    def read_article(...): ...
    def icon(...): ...
    def random_path(...): ...
    def refresh(self, *, wait: bool = True) -> bool: ...
    def close(self) -> None: ...
```

`library_sources()` is part of the phase-00 runtime contract. The ZIM library
view must include unreadable archives that are not in the searchable source
set. Other providers default it to `sources`.

### Search dispatch

In `Search._search_source`:

```python
runtime = self.runtimes[source.collection_key]
return runtime.search(source, request)
```

Keep a temporary fallback for `mode == "kiwix"` until phase 02. Replace the
current "anything that is not Kiwix" fallback with runtime dispatch in this
phase; ZIM is the only local provider.

`Search` keeps core responsibilities: query validation, source selection,
slot/concurrency control, progressive events, ranking, caching, preview
hydration orchestration, and HTTP-facing metadata. Provider runtimes own
"how to find/open/search one source".

## Work items

### Discovery and lifecycle

- [ ] Move `_ensure_catalog`, `_scan_catalog`, `_catalog_entries`, and catalog
      construction into `providers/zim/provider.py`.
- [ ] Move `_scan_zim_paths`, `_find_zim_path`, `_match_zim_path`, `_scan_catalog`
      calls, and `_discover_local_indexes` into `providers/zim/discovery.py`.
- [ ] Move `_open_index`, `_open_zim_index`, `_read_index_meta`,
      `_read_index_state`, `_stale_zim_archive`, and
      `_model_probes_compatible` into the ZIM runtime.
- [ ] Move the local-source branch of `_rebuild_sources` and
      `_source_from_catalog` into `providers/zim/metadata.py`.
- [ ] Move `refresh_local_sources` / local half of `refresh_sources` into the
      runtime; `Search.refresh_sources` becomes a provider loop.
- [ ] Keep `reload()` behavior: rescan catalog/indexes, preserve open indexes
      whose files did not change, clear caches only when the source set changes.
- [ ] Keep `start_faiss` and `start_fulltext_isolation` startup sequencing, but
      iterate runtimes; ZIM capability checks stay in the ZIM runtime.
- [ ] Add `ZimRuntime.library_sources()` for unreadable archives and duplicates.

### Search

- [ ] Move `_search_local` and `_search_local_locked` into
      `providers/zim/search.py`.
- [ ] Pass explicit dependencies instead of `self`: `candidate_count`,
      `preview_chars`, `max_html_bytes`, `max_entry_bytes`, `nprobe`, preview
      cache hooks, and the request object.
- [ ] Move `_fetch_docs`, `_fetch_archive_docs`, `_extract_previews` (ZIM
      previews), and the ZIM half of `_result_doc` with them.
- [ ] Keep `_hydrate_results` in `Search`; it calls the runtime for previews.
      Phase 03 replaces the runtime-specific call with `ContentStore`.
- [ ] Keep the five `SourceResult` channels (`semantic`, `keyword`, `fulltext`,
      items) identical. This is a move, not a ranking change.
- [ ] Keep native full-text isolation and its partial/error statuses.

### Identity and API

- [ ] `Search.resolve_source` uses the runtime's alias matcher; ZIM-only
      `_book_identity` matching must not accidentally match files/Kolibri
      sources in later phases.
- [ ] `local_names()` keeps returning ZIM stems for compatibility. Add
      `searchable_source_keys()` for new callers.
- [ ] `Search.health()` reports ZIM collection/index stats from the runtime,
      with the same keys as today.

### Cleanup

- [ ] Delete moved functions from `zimantic/search.py`; do not leave duplicate
      implementations.
- [ ] Add module docstrings explaining that `providers/zim` is the reference
      provider and that new providers should not import it.
- [ ] Add `tests/providers/zim/` and move ZIM-only unit tests there only if it
      reduces duplication; keep existing test files working.

## Parity tests

Add tests that compare pre/post payloads for a fixture library:

- [ ] `/sources` and `/library.json`: same entries, keys, names, booleans,
      sizes, metadata, icons; only the additive phase-00 fields differ.
- [ ] `/search` and `/search/stream`: same document ids, order, scores, and
      previews for a fixed query.
- [ ] `/content/<zim>/<path>` and `/article/<zim>/<path>`: same bytes, MIME,
      redirects, and status codes.
- [ ] Unindexed ZIM native full-text fallback still works.
- [ ] A missing/corrupt/archived-elsewhere index still produces the same
      `searchable`/`error` state.
- [ ] `reload()` still finds a newly added ZIM and drops a removed one.
- [ ] Source-key collisions still get the same suffixes.
- [ ] A source-key collision logs one warning identifying both archives with
      paths relative to `zim_dir`; paths outside that root use a basename or
      `<unknown>`, never an absolute path.

## Acceptance criteria

- `python -m pytest` passes.
- `zimantic serve` on the existing `zims/` and `indexes/` works with no config
  change and no reindex.
- `Search` has no fallback that treats every non-Kiwix source as a ZIM.
- The only ZIM imports outside `providers/zim`, `build.py`, `catalog.py`, and
  `content/` are compatibility shims. The temporary helpers in
  `searching/sources.py` may remain until phase 03, where they are removed.
- Search latency and memory for a ZIM-only library are unchanged within noise.

## Risks and mitigations

- **A 3,800-line class is hard to move mechanically.** Move whole functions,
  keep signatures, then narrow dependencies in a second commit. Never rewrite
  search logic in the same change.
- **Hidden `self` coupling.** Use a small `ZimSearchContext` dataclass for
  config/limits/caches instead of passing the whole `Search` object.
- **Test doubles depend on `Search` internals.** Keep attributes such as
  `indexes` and `catalog` as read-only compatibility aliases during this phase;
  remove them in phase 04.
