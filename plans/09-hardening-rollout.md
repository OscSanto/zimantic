# Phase 09 — Hardening and rollout

Status: proposed.

## Goal

Harden the provider seam for a safe, bounded, and observable mixed-library
release. Each earlier phase must already meet its own safety tests and can ship
independently; this phase is the final cross-provider release gate, not permission
to ship unsafe earlier work. Compatibility is explicitly out of scope: release
notes document the required config, catalog, and index rebuilds.

## Threat model

Zimantic already treats ZIMs as untrusted-ish input. The new providers widen
that surface:

| Input | New risk |
|---|---|
| Configured file trees | path traversal, symlink escape, malicious HTML, MIME confusion, huge media, filename tricks |
| Kolibri database | malformed/schema-shifted SQLite, unrelated private user data, duplicate ids, inconsistent file mappings |
| Kolibri HTML5 apps | zip-slip, asset path escape, executable content |
| Kolibri media | range abuse, oversized responses, missing files |
| Derived-text engines | image decompression bombs, oversized/corrupt media, expensive models, untrusted metadata |
| Kiwix server | existing SSRF/oversized-response/XML risks |
| Config | absolute paths leaked in API/logs, duplicate collections, unsafe roots |

The provider boundary must assume every provider can fail, lie about MIME, or
return a path it should not.

## Security requirements

### Path and content confinement

- Every filesystem/Kolibri store uses race-safe root-confined opens and rejects
  symlinks, `..`, encoded separators, NUL bytes, and Windows-style paths.
- Files-provider hidden paths and configured excludes are denied for serving as
  well as discovery/indexing; includes restrict indexing only.
- Zip extraction/reading uses a manifest and validates every member path;
  reject absolute members, `..`, links, oversized decompressed members, and
  archive bombs (zip-slip and bounded-decompression tests).
- Directory requests never generate listings; only a known index file is
  served.
- Do not expose absolute paths in API payloads, errors, redirects, logs above
  debug level, or ETags. This is an exposure rule, not a ban on private local
  locators: `catalog.sqlite` may store canonical absolute roots internally.
- Keep any catalog locator separate from provider/collection/source identity;
  protect the catalog with the same local filesystem permissions as the index
  directory, and never serialize its path column.
- Sanitize exception-derived diagnostics too: normal logs use public keys,
  stable codes, and safe causes, not an `OSError`/SQLite message that embeds a
  private filename. Raw exception details belong at debug level only.

### MIME and response policy

- Prefer extension mapping, then constrained sniffing.
- Never serve an unknown binary as `text/html`.
- HTML/XHTML is rendered through the existing shell; other types are inline or
  attachment per the files plan.
- Always send `X-Content-Type-Options: nosniff` for provider content.
- Byte ranges: single range only, validated, capped; no gzip on partial
  responses.

### HTML and browser isolation

- Keep the existing content-shell sandbox and `secure_browsing` switch.
- Document that file trees and Kolibri HTML5 apps can be untrusted.
  `secure_browsing = true` isolates the shell iframe, but a raw article opened
  directly or in a permitted popup remains unsandboxed and can use the app
  origin and persistent storage. Do not describe this switch as a complete
  boundary against untrusted content.
- Do not inject provider content into the trusted shell without the existing
  bridge/sandbox rules.

### Database and network

- Kolibri's installed main DB is opened without writes, migrations, sidecar
  creation, or schema changes. Live WAL contents must not be silently ignored.
  Private snapshots are owner-only and queries never expose unrelated user or
  activity data.
- All SQL uses parameters; schema/table names are hard-coded per supported
  version.
- Local providers perform no network I/O. Kiwix is the only remote provider and
  rejects DTDs/entities across supported XML encodings and checks redirect
  origins before making the next request.

## Resource limits

Keep the roadmap defaults and make them configurable per provider:

| Limit | Default | Enforcement |
|---|---|---|
| sources per collection | 64 | hierarchical providers collapse; fixed-identity providers use paging/fan-out limits |
| sources overall | 512 | advisory target, not a hard in-memory cap; never silently drop sources |
| entries per source | 500k | indexing truncates only with explicit `truncated`/`indexed_count` metadata |
| bytes per indexed item | existing `max_entry_bytes` | extraction |
| indexed body text per item | 1 MiB (`max_body_text_bytes`) | lexical text; report `body_truncated_count` |
| caption bytes | 1 MiB | caption parser |
| Kolibri HTML5 zip | 10k members / 8 MiB directory / 256 MiB member / 1 GiB total / 10 s | phase-06 per-entry limits; actual decompression and range seeks are bounded |
| OCR pages per item | 20 | OCR stage |
| OCR pixels per page | 50 MP | image decode guard |
| OCR output bytes | 256 KiB | OCR stage |
| ASR media per item | 1 h / configured | ASR stage |
| ASR media bytes | 1 GiB | ASR stage |
| derived-text workers | `"auto"` (CPU/memory-derived, unavailable if none fits) | enrichment scheduler |
| preview bytes | existing limits | preview hydration |
| content response | existing `max_article_bytes` | store read |
| discovery time | 10 s per provider | background publication after deadline |
| entries examined per traversal | 1 million | includes skipped/excluded entries |
| traversal time | 300 s | hard bound on each traversal |
| search tasks per provider | up to `min(4, search_workers)` | fair scheduler reserves capacity for competitors when possible |
| remote sources per search | `max_remote_sources` | Kiwix runtime |
| admitted search deadline | 30 s (`search_timeout_seconds`) | scheduling, ranking, bounded previews; partial scope on expiry |
| listing page size | global `page_size`, maximum 200 | collection/source/library JSON; no unbounded default |

Additional rules:

- Discovery must never recurse without a cap and exclude list.
- A traversal/time budget that prevents a complete discovery or a deterministically
  capped build is an explicit incomplete/error state, not a complete prefix.
- Apply phase 04's canonical derived-text policy: operations are independent
  opt-ins, build-time-only, local-only, bounded, and isolated; disabled media
  is never read. Phase 04 also owns worker resolution, process-pool lifecycle,
  cache/provenance, and partial-build semantics.
- A truncated source must still serve all permitted content; only indexing is
  truncated.
  Search/API/health output must expose `truncated` and `indexed_count` so
  reduced search coverage is never presented as complete.
- Report `body_truncated_count` independently when an indexed item's lexical
  body exceeds its text budget or native extraction is incomplete.
- A provider hitting a limit logs once and degrades, never crashes or loops.

## Failure isolation

- `SourceProvider.discover()` errors produce an entry-level diagnostic and mark
  affected known collections unavailable; other entries/collections continue.
- `CollectionRuntime.search()` errors become `SourceResult(status="error")`;
  the merged search continues.
- `ContentStore` errors map to the existing HTTP error shapes; a broken source
  must not leak bytes from another source.
- A corrupt/missing index disables search for that source but not serving.
- A missing content file disables that item but not its source.
- Kiwix failures keep the last good catalog.
- Keep the existing native full-text helper-process isolation; a provider
  without native search must not change that path.
- A provider must be removable by deleting its config entry; no orphaned
  background thread, open DB, or index handle.
- An engine/worker crash marks the input as failed or skipped and continues the
  build. A broken derived-text executor is recreated with bounded retries, not
  reused indefinitely. Partial requested enrichment is reported and results in
  a non-zero build exit.

## Observability

- Log once per collection for: discovery collapse/truncation, unsupported
  schema, missing root, duplicate names, index failure.
- Per-collection health:
  - provider, source count, available/searchable count;
  - discovery status/error;
  - index count and done/fast state;
  - last refresh time;
  - truncation/collapse flags.
- Build report: provider, collection, source, items indexed, skipped,
  item truncation, `body_truncated_count`, duration, and failure reason.
- `zimantic sources --json` and `/collections.json` must agree with health.

## Performance and memory gates

The release target is a Raspberry Pi 4: cold startup to an HTTP-ready server
with initial local-provider discovery settled in under 120 seconds; mixed-
library search p95 below 8 seconds; and peak process RSS below 1 GiB while
serving and searching. Measure search latency after one warm-up request and
over 100 fixed queries after the model is ready; separately record cold first
query latency. Startup does not wait for optional model loading or a remote
Kiwix server, but the benchmark's Kiwix endpoint is a local controlled fixture.
Record Pi 4 model/RAM, OS, storage, Python version, commit, and fixture revision.

The reproducible mixed fixture contains one ZIM, a Files tree with 1,000
indexable files, one fixture channel imported into a pinned Kolibri v0.19.5
main SQLite database, and a local Kiwix catalog/search endpoint. Store the fixture
generator, pinned inputs, and fixed query set under
`tests/fixtures/mixed_library/` and `tests/benchmarks/`; keep them versioned
with the benchmark tests. Report cold/warm build and startup measurements, RSS,
and p50/p95 search; a missing fixture or hardware result is an unverified gate,
not a pass.

Required:

- The Pi 4 targets above pass on the documented fixture.
- Adding an unconfigured provider costs zero background work. A derived-text
  engine that is not enabled costs zero work and is never loaded by `serve`.
- Adding a files collection with `max_sources` sources does not load file
  contents at startup; discovery is `stat`-only.
- Kolibri discovery uses a bounded read-only view of the installed main DB,
  shared across its channels; no storage walk at startup.
- Search fan-out over a mixed library stays within `search_workers` and the
  per-provider budget.
- FAISS/index memory is still per-open index; do not add per-provider caches
  proportional to item count.
- Measure index migration/rebuild peak disk use and verify that failed rebuilds
  preserve a validated last-good index, warn with the failure cause, and do not
  accumulate duplicate generations. Successful publication switches the
  SQLite and any FAISS artifact atomically; reclaim prior generations after
  reader release.
- Repeat phase 04's contentless-versus-stored body/preview benchmark using the
  actual ZIM, Files, and Kolibri providers, with the measurements specified
  there. The phase-04 synthetic comparison does not replace this release gate.
- A derived-text worker's model memory is counted once per pool worker, not once
  per source; auto resolution uses one worker on a low-memory host when one
  fits, reports unavailable otherwise, and scales by memory, not just core count.
- Preview/cache byte limits remain global and respected for non-ZIM content.

Also measure and record:

- memory after serving the mixed fixture library;
- `/search` p50/p95 for ZIM-only and mixed;
- `/library` payload size and render time;
- cold/warm build time for a 1,000-file tree and a fixture channel.

## Test matrix

### Unit and contract

- [ ] `SourceInfo`/`CollectionSnapshot` serialization and exact source-key
      resolution.
- [ ] Provider contract suite run against ZIM, files, Kolibri (and Kiwix for
      search-only).
- [ ] Discovery batches retain shared resources until all readers retire and
      clean up after failed, empty, and replaced discoveries.
- [ ] Content-store contract suite: read, resolve, redirect, exists, icon,
      random, errors, ranges, source authorization, and ambiguous-path handling.
- [ ] Entry-producer contract: stable ids, fingerprint change, resume,
      truncation, optional buildable-runtime capability, and cleanup on every
      success/failure/cancellation path.
- [ ] Ephemeral-open contract: loopback-only binding, selected-source scope,
      read-only catalog/index access with no SQLite sidecars, and no persistent
      PID, lock, index, or cache writes.

### Integration

- [ ] Mixed library: ZIM + files + Kolibri + Kiwix; `/search`, `/sources`,
      `/collections.json`, `/library.json`, `/health`.
- [ ] Progressive stream with providers finishing at different times.
- [ ] Cache key/source selection across collections.
- [ ] `open` with matched and fallback ZIM identities, stale/missing indexes,
      and an unaffected normal `serve` process.
- [ ] Reload after adding/removing each provider's content.
- [ ] Migration path: old config is rejected, the catalog is rebuilt, and
    only recognized old indexes are imported by the validated phase-04
    importer; unknown indexes are rebuilt without deleting a usable index on
    failure.
- [ ] A freshly rebuilt catalog cannot legitimize a size-only old index whose
      build-time archive identity is unknown.
- [ ] Composed listing filters match beyond first pages, capture one snapshot
      token with records, and restart bounded loads on publication/server restart.

### Security/adversarial

- [ ] Traversal: `..`, URL-encoded, double-encoded, backslash, NUL, absolute.
- [ ] Symlink escape and zip-slip.
- [ ] Race-safe confinement under symlink replacement; hidden/excluded files are
      not directly servable, while allowed assets remain accessible.
- [ ] Files child homepages, results, previews, and shared-asset links all use
      collection-relative paths without ambiguous source-relative fallback.
- [ ] Identical paths in different sources open their respective bytes.
      Redirect aliases collapse to their target; genuinely ambiguous
      same-source paths are reported rather than attributed or served
      arbitrarily.
- [ ] Absolute configured roots and private catalog locators are accepted, but
      absent from APIs, errors, and logs; relocating a provider root preserves
      its logical source ID and index locator when its identity is unchanged.
- [ ] MIME confusion and `nosniff`.
- [ ] Opaque-origin article preflights allow the documented range/conditional
      headers without granting unrelated methods or headers.
- [ ] UTF-16 as well as UTF-8 XML DTD/entity declarations are rejected, and
      cross-origin redirects are blocked before contacting the other origin.
- [ ] Oversized files, PDFs, caption files, ranges, and XML.
- [ ] Derived text off by default: no caption, image, or media bytes are read;
      fake engines verify each operation is independently opt-in.
- [ ] Enabled embedded captions use bounded local-only demuxing, including a
      media container larger than the caption-text cap; no audio/video decode
      or implicit OCR occurs.
- [ ] OCR/ASR adversarial inputs: decompression bomb, huge dimensions, corrupt
      codec, over-duration media, missing model, and cap-skipped output.
- [ ] Auto worker resolution: 1 on a low-memory host, above 4 on a large host
      when memory allows, unavailable when none fits, correct CPU-vs-memory
      binding, one shared pool across concurrent source builds, and no pool
      created when OCR/ASR are off.
- [ ] A crashing fake engine is reported as partial with a non-zero build exit,
      without stopping other sources or leaking a worker. A non-thread-safe
      engine is safely isolated; inputs use bounded spooled files, not
      arbitrary paths.
- [ ] Malicious/unsupported Kolibri DB schema and duplicate ids.
- [ ] Secure browsing sandboxes the shell iframe; direct raw HTML and permitted
      popups retain their documented unsandboxed behavior.
- [ ] No writes to Kolibri's installed DB, SQLite sidecars, or configured
      content roots, including with a live WAL; private snapshots and queries
      never expose unrelated user data. Closed databases without sidecars and
      live WALs remain readable with prompt, bounded writer delays.
- [ ] Provider refresh/close tests verify that background discovery is
      cancelled or joined and stores/database handles are closed exactly once.
- [ ] `open` rejects index reuse when a sidecar-free read-only SQLite handle
      cannot be created, binds only loopback, and its toolbar cannot navigate
      outside the selected source scope.
- [ ] Cross-process reader leases survive concurrent build/prune; no read-only
      reader creates a lease, and uncertain/incomplete discovery never causes
      pruning of a configured entry's indexes.
- [ ] `sources`, `build --list`, and `prune --dry-run` do not modify persistent
      state; `prune --yes` revalidates config/discovery under the documented
      lock order.
- [ ] Cache-disabled/pruned derived previews do not remove search hits or run
      caption/OCR/ASR extraction at request time.
- [ ] Read-only index reuse is verified on every officially supported
      platform/filesystem combination; elsewhere it fails closed to native
      full-text or readable-but-unsearchable behavior.

### Platform

- [ ] Python 3.11–3.14 in CI.
- [ ] Existing Windows x86-64 indexed build/search works with cross-process
      catalog and generation leases; verify publication/recovery on its
      supported local filesystem. The blocking release dependency introduced
      in phase 00 must pass; the existing non-blocking Windows job alone does
      not gate publishing.
- [ ] No libzim/FAISS installed: core/provider contracts and files provider
      still import and pass where expected.
- [ ] Pi Zero 2 W smoke test (or the existing low-memory CI target).

### JavaScript

- [ ] Library grouping, provider filter, collection selection, source filter
      grouping, history/back behavior.
- [ ] Progressive result rendering with collection labels.

## Rollout plan

Each stage is independently revertible. There is no long-lived branch.

1. **Internal seam (phases 00–02).** Ship contracts, ZIM provider, and Kiwix
   provider. Revert = revert the refactor and follow that release's config,
   catalog, and explicit index rebuild instructions.
2. **Generic serving and indexing (phases 03–04).** Ship content stores and the
   generic pipeline for ZIM. Revert = restore the ZIM-only code paths and rebuild.
3. **Files provider and collection config (phase 05).** Opt in by adding
   `[[collections]] type = "files"`. Ship docs and a commented example. No
   configured files collection means no new work at startup.
4. **Kolibri provider (phase 06).** Opt in by config; `captions`, `ocr`, and
   `asr` all default off and are independent.
   Ship a fixture-based integration test and supported-version note.
5. **Collections UI (phase 07).** Enable grouping, collection APIs, and provider
   filters using the source payload introduced in phase 00; there is no legacy
   compatibility view.
6. **Operations and ephemeral open (phases 08 and 10).** Add `sources`/`prune`,
   complete docs, and ship the scoped read-only ZIM workflow.
7. **Hardening (phase 09).** Run the full matrix, including `open`, then cut
   the release.

### Feature flag

The only flag needed is configuration: a provider with no configured
collection is dormant. Do not add a second global "enable providers" switch.

For a cautious rollout, ship with:

- files and Kolibri examples commented out in `config.toml`;
- captions/OCR/ASR documented but off, with no engine dependency installed by
  default;
- collection grouping is the normal UI; there is no fallback flat view.

### Rollback

- Remove the provider entry; sources disappear, indexes remain on disk.
- `zimantic prune` removes orphan indexes explicitly.
- Reverting to an older release requires rebuilding the catalog and indexes;
  there is no downgrade compatibility guarantee.
- The flat `/sources` and `/library.json` payloads use the canonical source
  fields, not a legacy view.

## Migration checklist

- [ ] Verify flat `zim_dir`/`kiwix_server` keys are absent and explicit
      `[[collections]]` entries are required. There is no shorthand fallback.
- [ ] Adopt recognized, validated old `<stem>.sqlite`/`.faiss` pairs once
      through the phase-04 importer, rebuild any unrecognized or incompatible
      indexes under the new locator, and use `zimantic prune` to reclaim
      superseded owned files.
- [ ] Allow the disposable catalog to be rebuilt by a full scan under the
      catalog's coordination rules; do not delete a live SQLite database.
- [ ] Update bookmarks and API clients to canonical source keys, the `source`
      query parameter, and the new payload fields.
- [ ] Verify the new `/content/{key}/...` and `/article/{key}/...` URLs.
- [ ] Local providers use the unified content route; Kiwix remains
      external-only and has no local content-store fallback.

## Release checklist

- [ ] All phase acceptance criteria met and checked off.
- [ ] The Phase 10 acceptance criteria and its read-only persistence guarantees
      are met and checked off.
- [ ] Full test matrix green.
- [ ] `README.md`, `CONTRIBUTING.md`, `config.toml`, `deploy/` updated.
- [ ] Mixed-library smoke test on a low-resource profile.
- [ ] Security review of path/zip/MIME/range handling.
- [ ] Supported Windows x86-64 indexed builds/searches and reader leases pass;
      do not silently replace this existing support with native-only fallback.
- [ ] Major version bump. Release notes list the new providers, the
      `[[collections]]` config requirement, the catalog/index rebuild, and the
      removed compatibility paths.
- [ ] A known-issues note covering the pinned Kolibri v0.19.5 schema version and
      derived-text engine/model limitations.

## Risks register

| Risk | Impact | Mitigation |
|---|---|---|
| Source explosion | unusable filters, slow fan-out | collection model + source caps + collapse |
| Path escape | data disclosure | real-root confinement + adversarial tests |
| Zip-slip | data disclosure/code execution | manifest validation, no extraction outside root |
| MIME confusion | XSS/drive-by | extension-first MIME, nosniff, attachment policy, documented shell sandbox boundary |
| Schema drift | Kolibri breaks | version detection, fail closed, isolated schema module |
| Index resume drift | wrong/missing results | fingerprint-based rebuild |
| Provider starvation | slow searches | per-provider task budgets |
| Startup regression | Pi Zero timeouts | cached-first/background discovery, stat-only walks |
| Contract churn | rework | minimal phase-00 protocol, contract tests |
| Config complexity | user errors | one `[[collections]]` schema, validation warnings, `sources` command |

## Final definition of done

The effort is complete when:

1. A ZIM-only install works after adding a `[[collections]] type = "zim"` entry and
   rebuilding its catalog and indexes.
2. A configured `/library/www/html/modules` tree appears as one collection with
   bounded sources, is searchable, and serves pages/assets safely.
3. A local Kolibri install appears as one collection per channel, with topics
   as sources, searchable documents, servable media/HTML5 apps, and opt-in
   captions/OCR/ASR.
4. The library and source filters remain usable at scale because items never
   become sources.
5. The core has no provider-specific branches; source serializers use the common
   contracts rather than provider aliases.
6. The security, resource, and failure-isolation requirements above are tested
   and documented.
