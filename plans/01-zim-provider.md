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
    metadata.py      # ZIM metadata extraction, SourceInfo construction, icons
```

`zimantic/catalog.py` becomes the provider-neutral `SourceCatalog` storage
layer. The ZIM provider supplies normalized metadata records to it; the catalog
schema and `SourceRecord` fields use generic identifiers (including the generic
`provider` discriminator), with no provider-specific table, index, or column
names.

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

### ZIM catalog paths

- Store ZIM records in the shared `sources` table with `provider = "zim"`,
  `collection_key`, and the existing compatible `source_key`.
- The composite `(provider, collection_key, source_key)` is the row identity.
  Keep the canonical absolute archive path in the separate indexed `path`
  locator column; it is private and is not serialized or logged.
- Normalize a path when scanning/upserting, then reuse that normalized value
  for catalog point lookups; avoid repeated filesystem resolution on hot paths.
- A moved archive updates its `path` locator during catalog refresh. Preserve
  the existing stem-based index path and source-key collision behavior.

The generic catalog schema replaces the legacy `zims` table and its
provider-specific indexes. Bump `SCHEMA_VERSION` (5 → 6) and rebuild a stale
catalog with a full provider scan; do not migrate rows in place and do not
retain legacy table/column aliases. Keep the public ZIM metadata payload
unchanged by mapping generic `SourceRecord` fields and provider metadata back to
the existing `SourceInfo` fields.

| Legacy catalog name | Generic catalog name |
|---|---|
| table `zims` | table `sources` |
| `stem` | `source_name` |
| `uuid` | `provider_id` |
| `article_count` | `document_count` |
| `entry_count` | `item_count` |
| `counter` | `mime_counts` |
| `has_fulltext_index` | `native_fulltext_available` |
| `favicon` / `favicon_mime` | `icon_data` / `icon_mime_type` |
| `source` | `origin` |
| `name`, `flavour`, `scraper`, and other provider-only metadata | `provider_metadata` |

Keep genuinely generic fields such as `path`, `size`, `mtime`, `title`,
`description`, `language`, `tags`, `media_count`, `homepage`, `available`, and
`last_error`. Name indexes generically (for example, `sources_name` and
`sources_provider_id`), never `zims_*`.

The target table has generic columns and a composite logical key:

```sql
CREATE TABLE sources (
    provider TEXT NOT NULL,
    collection_key TEXT NOT NULL,
    source_key TEXT NOT NULL,
    path TEXT,
    provider_id TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
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
    PRIMARY KEY (provider, collection_key, source_key)
);
CREATE INDEX sources_path ON sources(path);
CREATE INDEX sources_name ON sources(provider, source_name);
CREATE INDEX sources_provider_id ON sources(provider, provider_id);
```

`path` is nullable for records without a local filesystem locator and is never
the logical primary key. `provider_metadata` is JSON for metadata that has no
provider-neutral typed column; its contents are interpreted only by that
provider's adapter.

### Catalog schema rebuild

The catalog is a disposable cache: it can always be recreated from the archives,
so this phase rebuilds instead of migrating.

- Bump the catalog `SCHEMA_VERSION` (5 → 6) so an existing catalog is recognized
  as stale. On first run, drop the legacy `zims` table and its indexes, create
  the generic `sources` table, and let the normal provider scan repopulate it. Do
  not copy rows and do not write an in-place migration.
- Recognize the legacy layout by its version and complete column set before
  dropping it, exactly as the current disposable-cache check does; do not infer
  from a matching filename alone.
- Rebuilding re-opens every archive once, which also refreshes icons,
  availability, and provider metadata. That one-time cost is acceptable and
  preserves the existing "delete the catalog to rebuild it" guarantee.
- On a corrupt or unsupported catalog, delete and recreate it exactly as today.
  Do not leave a `zims` view or compatibility table in the new schema.
- The generic `sources` layout is Zimantic's own, not IIAB's catalog layout: IIAB
  already reuses the filename and version with a different table shape. Document
  that external tools expecting the old `zims` table must use their own catalog
  file.
- Because this intentionally ends external `zims`-table compatibility, the
  release that carries it is a major version bump.

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
      scanning into the ZIM provider; keep generic SQLite schema/access in
      `SourceCatalog`.
- [ ] Replace the `zims` table with the generic `sources` schema in
      `SourceCatalog`; use the composite logical primary key and a separately
      indexed canonical `path` locator.
- [ ] Bump the catalog `SCHEMA_VERSION` (5 → 6) and rebuild a stale catalog into
      the generic `sources` schema; remove `zims`-named tables/indexes/columns
      without changing public ZIM metadata serialization.
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

- [ ] `Search.resolve_source` uses the runtime's alias matcher; ZIM-only
      `_book_identity` matching must not accidentally match files/Kolibri
      sources in later phases.
- [ ] `local_names()` keeps returning ZIM stems for compatibility. Add
      `searchable_source_keys()` for new callers.
- [ ] `Search.health()` reports ZIM collection/index stats from the runtime,
      with the same keys as today.
- [ ] Populate `SourceInfo.media_types` for ZIM from the existing MIME counter
      (`counter`) using the phase-07 classification, so the content-type filter
      needs no provider call.

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
- [ ] Catalog lookup uses canonical absolute paths only internally; serialized
      source/library records contain no filesystem locator.
- [ ] A pre-change catalog is detected as stale, dropped, and rebuilt into
      `sources` by a full scan; the rescan preserves public ZIM metadata and
      leaves no legacy table or index names. Corrupt/unsupported catalogs
      rebuild the same way.
- [ ] `media_types` derives from the MIME counter and matches the phase-07
      classification (`text`/`video`/`audio`/`image`/`other`).
- [ ] The database schema has no `zims` table or `zims_*` indexes, and its
      provider-neutral column names preserve the existing public ZIM metadata
      through the provider adapter.

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
