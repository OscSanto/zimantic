# Phase 01 — ZIM provider

Status: proposed.

## Goal

Move the remaining ZIM-specific discovery and index lifecycle behind the
phase-00 provider seam. ZIM becomes the reference implementation. Source
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
    store.py         # ArchiveContentStore; range support is completed in phase 03
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
  `name = "..."`, `path = "..."`. `path` may name one ZIM archive or a
  directory of archives; directory discovery is bounded by the common scan
  limits. Collection key is `zim:<name>`.
- **Source** = one ZIM archive. Its key is
  `zim:<name>:<safe-stem>-<digest>`, where the digest always derives from its
  root-relative archive path, not just when a collision is discovered. Check
  for digest collisions and report them rather than overwrite a source. Adding
  or removing a same-stem archive cannot rename an existing source. There are no `book`,
  `local_name`, dated, or flavour alias keys; the display `title` comes from
  archive metadata.
- ZIM source identity is fixed by the archive. The global source budgets are
  therefore fan-out and pagination budgets for ZIM, not instructions to merge
  multiple archives into a synthetic source.
- `SourceInfo.provider = "zim"`, `collection_key` / `collection_label`
  populated. There is no `content_kind` or `mode`.
- `SourceInfo.intent_phrases` comes from the existing `_intent_phrases`.
- Relocating the collection root preserves keys when relative paths stay the
  same. Moving or renaming an archive within the root changes its key and
  requires a rebuild; content replacement at the same path retains the key
  but invalidates a stale index.

### ZIM catalog paths

- Store ZIM records in the shared `sources` table with `provider = "zim"`,
  `collection_key`, and the canonical `key`.
- `key` is the row identity. Keep the canonical absolute archive path in the
  separate indexed `path` locator column; it is private and is not serialized
  or logged.
- Normalize a path when scanning/upserting, then reuse that normalized value
  for catalog point lookups; avoid repeated filesystem resolution on hot paths.
- A relocated root updates its `path` locators during catalog refresh. Introduce
  the shared generic index locator here for both build and serve; there is no
  stem-based special case. Phase 04 extends its index schema and validation and
  requires another rebuild rather than trying to read phase-01 indexes.

The generic catalog schema replaces the ZIM-specific `zims` table outright.
Bump `SCHEMA_VERSION`. An older or unrecognized catalog is deleted and rebuilt
by a full provider scan; do not inspect, migrate, or alias old tables.

The catalog count columns are nullable source totals, not index statistics:
`document_count` is the total article/document-like content items reported by
the provider, `media_count` is the total media items, and `item_count` is the
total archive items (`documents + media` where the archive can classify them).
`NULL` means the provider cannot know the total without an unbounded or
incomplete scan; zero is a known empty total. `indexed_count` and `truncated`
belong to the published index metadata and `SourceInfo`, and never change the
meaning of these catalog totals.

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
    document_count INTEGER,
    media_count INTEGER,
    item_count INTEGER,
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
    last_error TEXT NOT NULL DEFAULT '',
    scanned_at REAL NOT NULL,
    provider_metadata TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (key),
    CHECK (native_fulltext_available IN (0, 1)),
    CHECK (size IS NULL OR size >= 0),
    CHECK (mtime IS NULL OR mtime >= 0),
    CHECK (document_count IS NULL OR document_count >= 0),
    CHECK (media_count IS NULL OR media_count >= 0),
    CHECK (item_count IS NULL OR item_count >= 0)
) STRICT;
CREATE INDEX sources_path ON sources(path);
CREATE INDEX sources_collection ON sources(collection_key);
CREATE INDEX sources_provider_id ON sources(provider, provider_id);
```

`path` is nullable for records without a local filesystem locator and is never
the logical primary key. `provider_metadata` is JSON for metadata that has no
provider-neutral typed column; its contents are interpreted only by that
provider's adapter and is validated as canonical JSON at the provider boundary.
`mtime` is the filesystem modification time in seconds, stored as a `REAL`.
Derive it from `st_mtime_ns` by rounding to the nearest whole second before
persistence and fingerprinting; subsecond changes are intentionally ignored.
It is a practical change detector, not proof of identical content.
`scanned_at` is Unix epoch seconds with fractional precision, for
display/diagnostics rather than freshness comparisons. For ZIM rows,
`provider_id` stores the archive UUID when available; phase 10 requires
matching path and size and the same nonempty UUID in the row and live archive
before reusing a catalog identity.

The presence of a row in `sources` means the archive was discovered and is
addressable by its canonical key. Do not persist `SourceInfo.available` or use
it as a proxy for row presence; availability is recomputed from the live
archive at runtime. `SourceInfo.media_types` is derived from the persisted MIME
counts when they provide a usable classification and remains `null` when they
do not; it does not encode discovery state. `last_error` is the latest
discovery diagnostic only, is cleared on a successful scan, and never
determines current `available`.

For ZIM, populate `document_count` from the archive article count,
`media_count` from its media count, and `item_count` from its entry count when
those values are reliable; preserve `NULL` when a binding cannot distinguish a
known zero from an unavailable count. These catalog totals are independent of
the bounded generic index manifest.

### Catalog schema rebuild

The catalog is a disposable cache: it can always be recreated from the
archives, so this phase deliberately rebuilds instead of migrating. This
forced catalog rebuild is independent of index reuse; do not throw away a
usable search index just because the catalog schema changed.

- Bump the catalog `SCHEMA_VERSION` and delete any catalog whose version does
  not match. Recreate the `sources` table and let the normal provider scan
  repopulate it. Do not inspect the old schema, copy rows, or write a migration.
- Rebuilding re-opens every archive once, which also refreshes icons,
  availability, and provider metadata.
- On a corrupt or unsupported catalog, delete and recreate it. Do not leave a
  `zims` view, compatibility table, or old index name in the new schema.
- If catalog creation or repopulation fails, log a warning with the database
  path and cause, leave the prior catalog intact until the replacement is
  complete where feasible, and report that source metadata is unavailable.
- Coordinate rebuilds across processes and close existing connections before
  replacing a database and its SQLite sidecars; never unlink a live catalog
  while another process can keep using or writing the old inode. If exclusive
  coordination cannot be obtained, report the failure and retry later.
- This is a breaking change: release notes state that old catalogs are
  discarded and rebuilt. Index files follow phase 04's separate validated
  adoption/rebuild policy.

### Runtime

```python
class ZimRuntime:
    collection: CollectionInfo
    sources: Sequence[SourceInfo]
    catalog: SourceCatalog
    indexes: dict[str, _LocalIndex]

    def search(self, source, request) -> SourceResult: ...
    def library_sources(self) -> Sequence[SourceInfo]: ...
    def content_store(self, source: SourceInfo) -> ContentStore: ...
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
- [ ] Create the shared index-locator module and use it in both ZIM build
      and serve; the phase-04 one-time importer may adopt only catalog-matched,
      validated old stem-based paths.
- [ ] Add the ZIM `EntryProducer` mapping: preserve the archive `_index` and
      redirect target id, including zero, as the canonical non-negative row id.
      Preserve duplicate paths as distinct rows and reject integer
      overflow/collisions.
- [ ] Move the local-source branch of `_rebuild_sources` and
      `_source_from_catalog` into `providers/zim/metadata.py`.
- [ ] Move `refresh_local_sources` / local half of `refresh_sources` into the
      runtime; `Search.refresh_sources` becomes a provider loop.
- [ ] Keep `reload()` behavior: rescan catalog/indexes and preserve open
      indexes whose files did not change. Invalidate affected search/preview
      caches when membership, index generation, content signature, or relevant
      source metadata changes, even when keys stay the same. Relevant metadata
      includes title, description, homepage, MIME/media classification,
      availability, and search capability. Locator-only changes refresh the
      store/index handle; they invalidate result/preview caches only if content,
      identity, or public metadata changed.
- [ ] Keep `start_faiss` and `start_fulltext_isolation` startup sequencing, but
      iterate runtimes; ZIM capability checks stay in the ZIM runtime.
- [ ] Add `ZimRuntime.library_sources()` for unreadable archives; distinct
      readable duplicate-stem archives are normal sources.

### Search

- [ ] Finish moving `_search_local` and `_search_local_locked` into
      `providers/zim/search.py`; phase 00 already moved what its real runtime
      needs for per-source dispatch.
- [ ] Pass explicit dependencies instead of `self`: `candidate_count`,
      `preview_chars`, `max_html_bytes`, `max_entry_bytes`, `nprobe`, preview
      cache hooks, and the request object.
- [ ] Move `_fetch_docs`, `_fetch_archive_docs`, `_extract_previews` (ZIM
      previews), and the ZIM half of `_result_doc` with them.
- [ ] Keep `_hydrate_results` in `Search`; call the ZIM `ContentStore` for
      previews through the shared runtime contract. Move existing serving
      behavior into its final `ArchiveContentStore` now; `read_range` may
      explicitly report unsupported until phase 03 implements range handling.
- [ ] Keep the four `SourceResult` channels (`semantic`, `keyword`, `fulltext`,
      `items`) identical. This is a move, not a ranking change.
- [ ] Keep native full-text isolation and its partial/error statuses.

### Identity and API

- [ ] `Search.resolve_source` matches only the canonical ZIM source key. Remove
      `_book_identity` and the dated/flavoured alias matcher.
- [ ] Replace `local_names()` with `source_keys()`; update health and callers.
- [ ] `Search.health()` reports ZIM collection/index stats from the runtime.
- [ ] Populate nullable `SourceInfo.media_types` for ZIM from the existing MIME
      counter using the phase-00 media-family taxonomy; include observed families even
      when the counter is partial, use `null` only when none are usable, and
      rescan and warn when a complete collection remains empty.

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
- [ ] `/content/<key>/<path>` returns the correct shell and
      `/article/<key>/<path>` returns the expected raw/decorated representation,
      MIME, redirects, and status codes.
- [ ] Unindexed ZIM native full-text fallback still works.
- [ ] A missing/corrupt/archived-elsewhere index produces the documented
      `searchable`/`error` state.
- [ ] `reload()` finds a newly added ZIM and drops a removed one.
- [ ] Duplicate stems in one collection get distinct stable keys; adding or
      removing one does not change the other's key.
- [ ] A configured ZIM path naming one archive and one naming a directory
      both discover only the intended archive set; ad-hoc single-file builds
      persisted by phase 08 remain usable by `serve`.
- [ ] A duplicate-stem collision logs one warning identifying both archives with
      paths relative to the configured root; paths outside that root use a
      basename or `<unknown>`, never an absolute path.
- [ ] Catalog lookup uses canonical absolute paths only internally; serialized
      source/library records contain no filesystem locator.
- [ ] A mismatched catalog is deleted and rebuilt into `sources` by a full scan;
      corrupt/unsupported catalogs rebuild the same way.
- [ ] `media_types` derives from the MIME counter and matches the phase-00
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
