# Phase 01 — ZIM provider

Status: implemented.

## Goal

Move the remaining ZIM-specific discovery and index lifecycle behind the
phase-00 provider seam. ZIM becomes the reference implementation. Retain the
provider-neutral source keys, catalog rows, index paths, and content IDs
introduced in phase 00; there is no ZIM compatibility layer.

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
  root-relative archive path, not just when a collision is discovered. For a
  single-file collection, use the archive's basename as that relative path;
  for a directory collection, use its path relative to the configured root. Check
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

- Phase 00 already stores ZIM records in the shared `sources` table with `provider = "zim"`,
  `collection_key`, and the canonical `key`.
- `key` is the row identity. Keep the canonical absolute archive path in the
  separate indexed `path` locator column; it is private and is not serialized
  or logged.
- Normalize a path when scanning/upserting, then reuse that normalized value
  for catalog point lookups; avoid repeated filesystem resolution on hot paths.
- Incremental scans update or remove rows only within the completely scanned
  collection; they never delete namespaces absent from this process's loaded
  config. This matters when an ad-hoc build adds an entry while an older
  server is still running. Incomplete scans do not delete unseen rows.
- Filter catalog-backed runtime sources to the process's loaded collections
  and their discovered paths. If a row's locator conflicts with that
  runtime's resolved root, do not open it as that source; treat its metadata
  as stale and revalidate the path discovered by that runtime. Processes
  temporarily using different configs must not serve each other's archive.
- A relocated root updates its `path` locators during catalog refresh. Retain
  phase 00's shared generic index locator (including its `current` manifest
  and `generations/<build-generation>/` layout) for build and serve; there is
  no stem-based special case. Phase 04 extends the index SQLite schema and
  validation and includes a one-time importer for recognized pre-redesign and
  phase-00/01 ZIM indexes when compatible, falling back to a clean rebuild.

Phase 00 replaces the ZIM-specific `zims` table with the generic catalog schema
outright and bumps `SCHEMA_VERSION`. An older or unrecognized catalog is
replaced by a complete staged provider scan under the coordination rules below;
do not inspect, migrate, or alias old tables. Phase 01 moves the existing scan
and catalog ownership without repeating the schema rebuild.

Phase 00 requires SQLite 3.37 or newer before creating the first `STRICT`
catalog and reports an actionable startup/build error on an older runtime.
Phase 08 documents this requirement; it does not introduce it after earlier
schemas already depend on it.

The shared locator introduced in phase 00 already seals each published SQLite
database: checkpoint and close the staging database, leave no WAL/journal/SHM
dependency, durably flush artifacts and the replaced manifest's parent
directory using the platform's supported mechanism, and never modify a
published generation. Include a schema revision, provider,
collection/source identity, source fingerprint, size/mtime signature, build
generation, and archive UUID when available in its recognized metadata.
Phase 04's importer needs build-time identity evidence, not just freshly
rescanned catalog metadata. Retain the read-only generation lease introduced
with the locator; safe replacement cannot wait until phase 04 or `open`.

`item_count` is the nullable total archive-item count, not an index statistic;
`NULL` means the provider cannot know it without an unbounded or incomplete
scan, and zero means known empty. `indexed_count` and `truncated` belong to
published index metadata and `SourceInfo`, and do not change this total.

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
    item_count INTEGER,
    license TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    attribution TEXT NOT NULL DEFAULT '',
    relation TEXT NOT NULL DEFAULT '',
    mime_counts TEXT NOT NULL DEFAULT '',
    homepage TEXT NOT NULL DEFAULT '',
    native_fulltext_available INTEGER,
    icon_data BLOB,
    icon_mime_type TEXT NOT NULL DEFAULT '',
    icon_path TEXT NOT NULL DEFAULT '',
    scanned_at REAL NOT NULL,
    provider_metadata TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (key),
    CHECK (native_fulltext_available IS NULL OR native_fulltext_available IN (0, 1)),
    CHECK (size IS NULL OR size >= 0),
    CHECK (mtime IS NULL OR mtime >= 0),
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
before reusing a catalog identity. `native_fulltext_available` is `NULL` when
the provider cannot determine it, rather than incorrectly recording false.

The presence of a row in `sources` means the archive was discovered and is
addressable by its canonical key, not that it will succeed on the next
operation. Do not persist source availability or per-source `last_error`.
Refresh stats paths to detect identity/content changes and reopens only changed
archives; operations report their own failures. The new schema omits the
legacy availability and error columns.
`SourceInfo.media_types` is derived from the persisted MIME counts when
available; it is an empty array when no family is known. It does not encode
discovery state. Discovery diagnostics are logged and exposed through
collection-level discovery errors, not stored per source.

For ZIM, populate `item_count` from the archive entry count when reliable;
preserve `NULL` when a binding cannot distinguish a known zero from an unknown
count. This total is independent of the bounded generic index manifest.

### Catalog schema rebuild

The catalog is a disposable cache: it can always be recreated from the
archives, so phase 00 implements the following rebuild instead of migrating.
Phase 01 retains it unchanged while moving provider ownership. This forced
catalog rebuild is independent of index reuse; do not throw away a usable
search index just because the catalog schema changed.

- Bump the catalog `SCHEMA_VERSION` and stage a replacement for any catalog
  whose version does not match. Recreate `sources` and complete a bounded
  provider scan before coordinated replacement. Do not inspect the old tables,
  copy rows, or write a migration. ZIM is the only persistent provider in this
  redesign; any future provider that stores records in this catalog must also
  participate in its complete staged rebuild.
- A one-time schema rebuild reopens every archive to repopulate metadata and
  icons. Routine refreshes do not: stat discovered paths, retain the last
  complete snapshot on missing/unreadable roots or incomplete scans, and reopen
  only changed archives. Report per-archive open/read failures as operation
  diagnostics, not persistent source metadata.
- On a corrupt or unsupported catalog, use the same staged rebuild. Do not leave a
  `zims` view, compatibility table, or old index name in the new schema.
- If catalog creation or repopulation fails, log a warning with a public
  diagnostic code and sanitized cause, leave the prior catalog intact, and
  report that persistent catalog metadata is unavailable. Discover each
  independently complete valid entry into an in-memory catalog so it can
  still serve/search; a failed or incomplete entry never publishes its prefix
  as complete.
  Private paths and raw exception text are debug-only. An incomplete scan
  must not delete unseen rows or publish a prefix as the replacement.
- Do not replace an existing catalog from an implicitly empty or incomplete
  scan when the active config is missing, malformed, or has invalid entries
  that could affect a catalog-backed provider. Keep it intact and report
  uncertainty; an existing, valid config with intentionally no catalog-backed
  entries is distinct. An ad-hoc build may first create valid config.
- Coordinate rebuilds across processes and close existing connections before
  replacing catalog contents; never unlink a live catalog while another
  process can keep using or writing the old inode. If exclusive coordination
  cannot be obtained, report the failure and retry later.
- Capture the active config snapshot used for a staged full scan. Before
  replacement, close this process's catalog connections, take the config-write
  lock, verify the config has not changed, then take the exclusive catalog
  lease and hold both through replacement. If it changed, discard the staged
  rebuild, preserve the prior catalog, and report that the server needs a
  restart; do not apply new settings during normal reload. Cooperating callers
  acquire the build lock first when needed, then config lock, then catalog
  lease; never wait for the config lock while holding a catalog lease.
- Use a stable, separately owned catalog lease file beside the catalog.
  Writable processes create it before catalog access and never replace or
  unlink it. Every participating process holds a shared OS lock from before
  opening a catalog connection until after closing it; replacement takes the
  exclusive lock after closing its own connections. Phase 10 may acquire an
  existing lease read-only but must never create it.
- Preserve existing Windows x86-64 support as well as POSIX. Implement and
  test equivalent shared/exclusive cross-process catalog leases on a read-only
  reader handle there; Windows `LockFileEx` is a candidate. The existing
  exclusive-only `msvcrt.locking` build lock is not by itself a shared reader
  lease. Unsupported filesystems fail safely, but the normal supported Windows
  filesystem must not silently lose indexed search.
- Pre-redesign processes do not know this lease. The initial upgrade therefore
  requires the operator to stop all older Zimantic servers/builders and other
  catalog users before starting the rebuild; the new lock alone cannot prove
  that an uncooperative legacy process has closed its SQLite connection.
  Document this maintenance precondition and never claim the lease protects
  processes that do not participate.
- For an existing readable SQLite catalog, keep its inode and use SQLite's
  transactional backup API to copy the validated staged database into it
  under the exclusive lease. Preserve the existing journal/WAL safety and
  match its page size where required; an interrupted or failed backup must
  leave the old logical catalog recoverable. Never rename a new main database
  over an old live-WAL basename while old sidecars remain.
- If the destination is corrupt and cannot accept a SQLite backup, or the main
  database is absent but old sidecars remain, use a durable recovery marker
  and an owned quarantine beside the catalog under the exclusive lease. Move
  the old main file and recognized sidecars out of the way before atomically
  installing a validated, sidecar-free staged main file; durably flush the
  files and directory using the platform's supported mechanism. A crash at
  any step is recovered under the exclusive lease before catalog access
  resumes. Do not follow symlinks or delete unknown files; if recovery cannot
  be completed safely, leave the catalog unavailable.
  Readers, including phase 10, must not open it while a recovery marker exists.
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
- [ ] Retain phase 00's generic `sources` schema, coordinated rebuild, and
      catalog lease while moving ZIM-specific scanning into the provider.
- [ ] Move `_scan_zim_paths`, `_find_zim_path`, `_match_zim_path`, `_scan_catalog`
      calls, and `_discover_local_indexes` into `providers/zim/discovery.py`.
- [ ] Move `_open_index`, `_open_zim_index`, `_read_index_meta`,
      `_read_index_state`, `_stale_zim_archive`, and
      `_model_probes_compatible` into the ZIM runtime.
- [ ] Move and retain the phase-00 shared index locator in both ZIM build and
      serve; the phase-04 one-time importer may adopt only uniquely
      catalog-matched, validated old stem-based paths.
- [ ] Pin the ZIM row-id mapping for phase 04: preserve the archive `_index` and
      redirect target id, including zero, as the canonical non-negative row id.
      Preserve redirect rows and reject integer overflow/collisions.
- [ ] Keep source-key/path URLs. Respect libzim's old/new namespace rules:
      public content must not be conflated with metadata or search indexes, and
      old namespace prefixes must not be flattened. Redirect aliases may share
      a final path. If distinct permitted final entries share that path, or
      their identity cannot be verified, omit the affected hits with a
      partial/error status and fail ambiguous content requests closed.
- [ ] Move and retain phase 00's bounded, namespace-aware resolver and
      isolated native helper path/title protocol. Unverifiable hits remain
      partial/error; native-only archives still work without a local index,
      and a capped index alone cannot prove uniqueness.
- [ ] Move the local-source branch of `_rebuild_sources` and
      `_source_from_catalog` into `providers/zim/metadata.py`.
- [ ] Move `refresh_local_sources` / local half of `refresh_sources` into the
      runtime; `Search.refresh_sources` becomes a provider loop.
- [ ] Keep `reload()` behavior: reconcile catalog paths with cheap stats and
      preserve open indexes whose files did not change. Reopen archives only
      when their signature changes. Invalidate affected search/preview caches
      when membership, index generation, content signature, or relevant source
      metadata changes, even when keys stay the same. Relevant metadata
      includes title, description, homepage, MIME/media classification, and
      search capability. Locator-only changes refresh the
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
- [ ] Keep `_hydrate_results` in `Search`; delegate to the runtime's phase-00
      `preview` hook, preserving document identity and existing ZIM behavior.
      The hook may read through `ArchiveContentStore`. Move existing serving
      behavior into the final store now; `read_range` may report unsupported
      until phase 03 implements ranges.
- [ ] Move all remaining core index/catalog access in the same phase:
      `Search.suggest` delegates title-prefix queries to `runtime.suggest`, and
      phrase/MIME/preview enrichment uses the runtime/store. Do not keep
      `Search.indexes`/`Search.catalog` aliases until phase 04 to support these
      callers; phase 04 generalizes the already-moved helpers.
- [ ] Keep the four `SourceResult` channels (`semantic`, `keyword`, `fulltext`,
      `items`) identical. This is a move, not a ranking change.
- [ ] Keep native full-text isolation and its partial/error statuses.

### Identity and API

- [ ] `Search.resolve_source` matches only the canonical ZIM source key. Remove
      `_book_identity` and the dated/flavoured alias matcher.
- [ ] Replace `local_names()` with `source_keys()`; update health and callers.
- [ ] `Search.health()` reports ZIM collection/index stats from the runtime.
- [ ] Populate `SourceInfo.media_types` for ZIM from the existing MIME
      counter using the phase-00 media-family taxonomy; include observed
      families even when the counter is partial, and use an empty array when
      none are known.

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
      persisted since phase 00 remain usable by `serve`.
- [ ] A duplicate-stem collision logs one warning identifying both archives with
      paths relative to the configured root; paths outside that root use a
      basename or `<unknown>`, never an absolute path.
- [ ] Catalog lookup uses canonical absolute paths only internally; serialized
      source/library records contain no filesystem locator.
- [ ] A mismatched catalog is replaced with `sources` only after a complete
      scan; corrupt/unsupported catalogs rebuild the same way. An incomplete
      or failed replacement leaves the original artifact untouched.
- [ ] A missing/invalid config cannot replace an existing catalog with an
      accidental empty scan; independently valid collections remain usable via
      bounded in-memory discovery when persistent catalog recovery fails.
- [ ] Published index generations are sidecar-free, immutable, and protected
      by read-only cross-process leases before an old generation is reclaimed.
- [ ] Redirect rows, including a target with row id zero, resolve to one final
      result. Distinct permitted entries with an ambiguous path are reported
      instead of guessed; old/new namespace handling does not confuse public
      content with metadata. Ordinary native-only ZIM search still works on
      supported libzim versions.
- [ ] A catalog reader in another participating process blocks replacement
      until its connection closes; a timed-out rebuild leaves the old catalog
      and sidecars untouched. Migration instructions require older processes to
      be stopped before the first upgrade.
- [ ] An older running server's scoped refresh cannot delete a new ad-hoc
      collection's catalog rows. A config change during full catalog staging
      aborts replacement, and mismatched root locators are never served.
- [ ] The catalog lease and generation lease work across processes on supported
      Windows x86-64 as well as POSIX; a reader can pin without writing the
      lease file.
- [ ] An interrupted SQLite backup preserves the old logical catalog, including
      with a live WAL. Crash at each corrupt-catalog quarantine/install step;
      the next process recovers or fails closed without pairing a new main DB
      with an old WAL.
- [ ] `media_types` derives from the MIME counter and matches the phase-00
      classification (`text`/`video`/`audio`/`image`/`other`).
- [ ] The database schema has no `zims` table or `zims_*` indexes.

## Acceptance criteria

- `python -m pytest` passes.
- `zimantic serve` with a `[[collections]] type = "zim"` entry works after rebuilding
  indexes and the catalog.
- `Search` has no fallback that treats every non-Kiwix source as a ZIM.
- Direct libzim use outside `providers/zim` remains only in the build/content
  adapters not yet replaced by phase 04 and the crash-isolated native full-text
  worker. Shared catalog storage and core search do not import libzim. There
  are no compatibility shims or temporary aliases.
- Search latency and memory for a ZIM-only library meet the documented target.

## Risks and mitigations

- **A 3,800-line class is hard to move mechanically.** Move whole functions,
  keep signatures, then narrow dependencies in a second commit. Never rewrite
  search logic in the same change.
- **Hidden `self` coupling.** Use a small `ZimSearchContext` dataclass for
  config/limits/caches instead of passing the whole `Search` object.
- **Test doubles depend on `Search` internals.** Update them to the runtime
  contract in this phase; do not keep `Search.indexes`/`catalog` aliases.
