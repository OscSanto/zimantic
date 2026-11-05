# Phase 01 — ZIM provider

Status: proposed.

## Goal

Move all ZIM-specific discovery, index lifecycle, and per-source search behind
the phase-00 provider seam. ZIM becomes the reference implementation. Source
keys, catalog rows, index paths, and content IDs move to the provider-neutral
scheme; there is no ZIM compatibility layer.

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
    metadata.py      # ZIM metadata extraction, SourceInfo construction, icons
```

`zimantic/catalog.py` remains the persistent ZIM archive metadata cache, exposed
through a provider-neutral `SourceCatalog` storage layer. The ZIM provider
supplies normalized metadata records to it; the catalog schema and
`SourceRecord` fields use generic identifiers (including the generic `provider`
discriminator), with no provider-specific table, index, or column names. Files
and Kolibri use their own manifests/databases, and Kiwix uses remote/in-memory
metadata rather than writing this catalog.

### Collection and source mapping

- **Collection** = the configured ZIM root: `[[collections]] type = "zim"`,
  `name = "..."`, `path = "..."`. Collection key is `zim:<name>`.
- **Source** = one ZIM archive. Source key is `zim:<name>:<stem>`; duplicate
  stems in one collection get a stable suffix. There are no `book`,
  `local_name`, dated, or flavour alias keys; the display `title` comes from
  archive metadata.
- ZIM source identity is fixed by the archive. The global source budgets are
  therefore fan-out and pagination budgets for ZIM, not instructions to merge
  multiple archives into a synthetic source.
- `SourceInfo.provider = "zim"`, `collection_key` / `collection_label`
  populated. There is no `content_kind` or `mode`.
- `SourceInfo.intent_phrases` comes from the existing `_intent_phrases`.

### ZIM catalog paths

- Store ZIM records in the shared `sources` table with `provider = "zim"`,
  `collection_key`, and the canonical `key`.
- `key` is the row identity. Keep the canonical absolute archive path in the
  separate indexed `path` locator column; it is private and is not serialized
  or logged.
- Normalize a path when scanning/upserting, then reuse that normalized value
  for catalog point lookups; avoid repeated filesystem resolution on hot paths.
- A moved archive updates its `path` locator during catalog refresh. Index paths
  come from the generic locator; there is no stem-based special case.

The generic catalog schema replaces the ZIM-specific `zims` table outright.
Bump `SCHEMA_VERSION`. An older or unrecognized catalog is deleted and rebuilt
by a full provider scan; do not inspect, migrate, or alias old tables.

The target table has generic columns and a single logical key:

```sql
CREATE TABLE sources (
    provider TEXT NOT NULL,
    collection_key TEXT NOT NULL,
    key TEXT NOT NULL,
    path TEXT,
    provider_id TEXT NOT NULL DEFAULT '',
    size INTEGER,
    mtime REAL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    long_description TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    creator TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    public_tags TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    document_count INTEGER NOT NULL DEFAULT 0,
    media_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    license TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    attribution TEXT NOT NULL DEFAULT '',
    relation TEXT NOT NULL DEFAULT '',
    mime_counts TEXT NOT NULL DEFAULT '',
    homepage TEXT NOT NULL DEFAULT '',
    native_fulltext_available INTEGER NOT NULL DEFAULT 0,
    icon_data BLOB,
    icon_mime_type TEXT NOT NULL DEFAULT '',
    icon_path TEXT NOT NULL DEFAULT '',
    available INTEGER NOT NULL DEFAULT 1,
    last_error TEXT NOT NULL DEFAULT '',
    scanned_at REAL NOT NULL,
    provider_metadata TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (key)
);
CREATE INDEX sources_path ON sources(path);
CREATE INDEX sources_collection ON sources(collection_key);
CREATE INDEX sources_provider_id ON sources(provider, provider_id);
```

`path` is nullable for records without a local filesystem locator and is never
the logical primary key. `provider_metadata` is JSON for metadata that has no
provider-neutral typed column; its contents are interpreted only by that
provider's adapter.

### Catalog schema rebuild

The catalog is a disposable cache: it can always be recreated from the
archives, so this phase rebuilds instead of migrating.

- Bump the catalog `SCHEMA_VERSION` and delete any catalog whose version does
  not match. Recreate the `sources` table and let the normal provider scan
  repopulate it. Do not inspect the old schema, copy rows, or write a migration.
- Rebuilding re-opens every archive once, which also refreshes icons,
  availability, and provider metadata.
- On a corrupt or unsupported catalog, delete and recreate it. Do not leave a
  `zims` view, compatibility table, or old index name in the new schema.
- This is a breaking change: release notes state that old catalogs are
  discarded and old index files must be rebuilt.

### Runtime

```python
class ZimRuntime:
    collection: CollectionInfo
    sources: Sequence[SourceInfo]
    catalog: SourceCatalog
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

There is no `mode` fallback and no "anything that is not Kiwix is ZIM" branch.
ZIM is the only local provider in this phase.

`Search` keeps core responsibilities: query validation, source selection,
slot/concurrency control, progressive events, ranking, caching, preview
hydration orchestration, and HTTP-facing metadata. Provider runtimes own
"how to find/open/search one source".

## Work items

### Discovery and lifecycle

- [ ] Move `_ensure_catalog`, `_scan_catalog`, `_catalog_entries`, and catalog
      scanning into the ZIM provider; keep generic SQLite schema/access in
      `SourceCatalog`.
- [ ] Replace the `zims` table with the generic `sources` schema in
      `SourceCatalog`; use `key` as the primary key and a separately indexed
      canonical `path` locator.
- [ ] Bump the catalog `SCHEMA_VERSION`; delete and rebuild a mismatched
      catalog into the generic `sources` schema. Remove `zims`-named
      tables/indexes/columns without keeping aliases.
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
- [ ] Keep the four `SourceResult` channels (`semantic`, `keyword`, `fulltext`,
      `items`) identical. This is a move, not a ranking change.
- [ ] Keep native full-text isolation and its partial/error statuses.

### Identity and API

- [ ] `Search.resolve_source` matches only the canonical ZIM source key. Remove
      `_book_identity` and the dated/flavoured alias matcher.
- [ ] Replace `local_names()` with `source_keys()`; update health and callers.
- [ ] `Search.health()` reports ZIM collection/index stats from the runtime.
- [ ] Populate `SourceInfo.media_types` for ZIM from the existing MIME counter
      (`counter`) using the phase-07 classification, so the content-type filter
      needs no provider call.

### Cleanup

- [ ] Delete moved functions from `zimantic/search.py`; do not leave duplicate
      implementations.
- [ ] Add module docstrings explaining that `providers/zim` is the reference
      provider and that new providers should not import it.
- [ ] Add `tests/providers/zim/` and move ZIM-only unit tests there.

## Contract and behavior tests

- [ ] `/sources` and `/library.json` use canonical keys, titles, booleans,
      sizes, metadata, and icons from the new catalog.
- [ ] `/search` and `/search/stream` return correct document ids, order,
      scores, and previews for a fixed query.
- [ ] `/content/<key>/<path>` and `/article/<key>/<path>` return correct bytes,
      MIME, redirects, and status codes.
- [ ] Unindexed ZIM native full-text fallback still works.
- [ ] A missing/corrupt/archived-elsewhere index produces the documented
      `searchable`/`error` state.
- [ ] `reload()` finds a newly added ZIM and drops a removed one.
- [ ] Duplicate stems in one collection get distinct stable keys.
- [ ] A duplicate-stem collision logs one warning identifying both archives with
      paths relative to the configured root; paths outside that root use a
      basename or `<unknown>`, never an absolute path.
- [ ] Catalog lookup uses canonical absolute paths only internally; serialized
      source/library records contain no filesystem locator.
- [ ] A mismatched catalog is deleted and rebuilt into `sources` by a full scan;
      corrupt/unsupported catalogs rebuild the same way.
- [ ] `media_types` derives from the MIME counter and matches the phase-07
      classification (`text`/`video`/`audio`/`image`/`other`).
- [ ] The database schema has no `zims` table or `zims_*` indexes.

## Acceptance criteria

- `python -m pytest` passes.
- `zimantic serve` with a `[[collections]] type = "zim"` entry works after rebuilding
  indexes and the catalog.
- `Search` has no fallback that treats every non-Kiwix source as a ZIM.
- The only ZIM imports outside `providers/zim`, `build.py`, `catalog.py`, and
  `content/` are the provider entry point. There are no compatibility shims or
  temporary helpers.
- Search latency and memory for a ZIM-only library meet the documented target.

## Risks and mitigations

- **A 3,800-line class is hard to move mechanically.** Move whole functions,
  keep signatures, then narrow dependencies in a second commit. Never rewrite
  search logic in the same change.
- **Hidden `self` coupling.** Use a small `ZimSearchContext` dataclass for
  config/limits/caches instead of passing the whole `Search` object.
- **Test doubles depend on `Search` internals.** Update them to the runtime
  contract in this phase; do not keep `Search.indexes`/`catalog` aliases.
