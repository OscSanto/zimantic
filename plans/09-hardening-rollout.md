# Phase 09 — Hardening and rollout

Status: proposed.

## Goal

Prove the provider seam is safe, bounded, and observable before the mixed-library
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
| Kolibri database | malformed/schema-shifted SQLite, duplicate ids, inconsistent file mappings |
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
- Document that file trees and Kolibri HTML5 apps are untrusted and should use
  `secure_browsing = true` where persistence is less important than isolation.
- Do not inject provider content into the trusted shell without the existing
  bridge/sandbox rules.

### Database and network

- Kolibri channel DBs are opened without writes, migrations, sidecar creation,
  or schema changes. Live WAL contents must not be silently ignored.
- All SQL uses parameters; schema/table names are hard-coded per supported
  version.
- Local providers perform no network I/O. Kiwix is the only remote provider and
  keeps its hardened XML/response handling.

## Resource limits

Keep the roadmap defaults and make them configurable per provider:

| Limit | Default | Enforcement |
|---|---|---|
| sources per collection | 64 | hierarchical providers collapse; fixed-identity providers use paging/fan-out limits |
| sources overall | 512 | advisory target, not a hard in-memory cap; never silently drop sources |
| entries per source | 500k | indexing truncates only with explicit `truncated`/`indexed_count` metadata |
| bytes per indexed item | existing `max_entry_bytes` | extraction |
| caption bytes | 1 MiB | caption parser |
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

Additional rules:

- Discovery must never recurse without a cap and exclude list.
- A traversal/time budget that prevents a complete discovery or a deterministically
  capped build is an explicit incomplete/error state, not a complete prefix.
- Indexing a files tree must not parse assets or media bodies unless the
  matching derived-text operation is explicitly enabled.
- By default, Kolibri must not read video/audio/image bytes for indexing. Only
  an enabled `asr`/`ocr` operation may decode them, under its caps.
- Derived-text engines run only during builds, never on search, preview, or
  serving paths. Bound decoded pixels, audio duration, output text, and worker
  count; reject decode failures and truncated inputs instead of retrying
  unbounded.
- Caption parsing is in-process. OCR/ASR run in one lazy, shared process pool so
  non-thread-safe native engines cannot corrupt each other and one engine crash
  cannot kill the build. Worker inputs are bounded, spawn-safe, and never
  arbitrary provider paths.
- Auto worker resolution is `min(cpu_limit, memory_limit)`, never a fixed
  `#CPU` or a magic cap. It must account for per-worker model memory, headroom
  for `build_workers`/embedding/OS, and engine-internal threads (normally set
  to 1). Concurrent source builds share one pool and cannot multiply model
  memory.
- A truncated source must still serve all permitted content; only indexing is
  truncated.
  Search/API/health output must expose `truncated` and `indexed_count` so
  reduced search coverage is never presented as complete.
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
  truncated, duration, failure reason.
- `zimantic sources --json` and `/collections.json` must agree with health.

## Performance and memory gates

Baseline from the current README: roughly 250–325 MB while serving a ZIM
library on a Pi Zero 2 W.

Required:

- ZIM-only startup time and search latency meet the documented target.
- Adding an unconfigured provider costs zero background work. A derived-text
  engine that is not enabled costs zero work and is never loaded by `serve`.
- Adding a files collection with `max_sources` sources does not load file
  contents at startup; discovery is `stat`-only.
- Kolibri discovery uses bounded reads of the per-channel DBs; no storage walk
  at startup.
- Search fan-out over a mixed library stays within `search_workers` and the
  per-provider budget.
- FAISS/index memory is still per-open index; do not add per-provider caches
  proportional to item count.
- A derived-text worker's model memory is counted once per pool worker, not once
  per source; auto resolution uses one worker on a low-memory host when one
  fits, reports unavailable otherwise, and scales by memory, not just core count.
- Preview/cache byte limits remain global and respected for non-ZIM content.

Measure and record:

- memory after serving a mixed fixture library;
- `/search` p50/p95 for ZIM-only and mixed;
- `/library` payload size and render time;
- cold/warm build time for a 1,000-file tree and a fixture channel.

## Test matrix

### Unit and contract

- [ ] `SourceInfo`/`CollectionSnapshot` serialization and exact source-key
      resolution.
- [ ] Provider contract suite run against ZIM, files, Kolibri (and Kiwix for
      search-only).
- [ ] Content-store contract suite: read, resolve, redirect, exists, icon,
      random, errors, ranges.
- [ ] Entry-producer contract: stable ids, fingerprint change, resume,
      truncation.

### Integration

- [ ] Mixed library: ZIM + files + Kolibri + Kiwix; `/search`, `/sources`,
      `/collections.json`, `/library.json`, `/health`.
- [ ] Progressive stream with providers finishing at different times.
- [ ] Cache key/source selection across collections.
- [ ] Reload after adding/removing each provider's content.
- [ ] Migration path: an old config, catalog, or index layout is rejected or
      ignored, and the documented `[[collections]]` configuration plus rebuild
      produces a working library.

### Security/adversarial

- [ ] Traversal: `..`, URL-encoded, double-encoded, backslash, NUL, absolute.
- [ ] Symlink escape and zip-slip.
- [ ] Race-safe confinement under symlink replacement; hidden/excluded files are
      not directly servable, while allowed assets remain accessible.
- [ ] Absolute configured roots and private catalog locators are accepted, but
      absent from APIs, errors, and logs; relocating a provider root preserves
      its logical source ID and index locator when its identity is unchanged.
- [ ] MIME confusion and `nosniff`.
- [ ] Oversized files, PDFs, caption files, ranges, and XML.
- [ ] Derived text off by default: no caption, image, or media bytes are read;
      fake engines prove each operation is independently opt-in.
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
- [ ] Untrusted HTML in sandbox/secure mode.
- [ ] No writes to Kolibri channel DBs, SQLite sidecars, or configured content
      roots, including with a live WAL.
- [ ] Provider refresh/close tests prove that background discovery is
      cancelled or joined and stores/database handles are closed exactly once.

### Platform

- [ ] Python 3.11–3.14 in CI.
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
6. **Operations (phase 08) and hardening (phase 09).** Add `sources`/`prune`,
   complete docs, run the full matrix, then cut the release.

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
- [ ] Delete or ignore old `<stem>.sqlite`/`.faiss` files and rebuild every
      configured collection with the new locator.
- [ ] Allow the disposable catalog to be rebuilt by a full scan under the
      catalog's coordination rules; do not delete a live SQLite database.
- [ ] Update bookmarks and API clients to canonical source keys, the `source`
      query parameter, and the new payload fields.
- [ ] Verify the new `/content/{key}/...` and `/article/{key}/...` URLs.
- [ ] Local providers use the unified content route; Kiwix remains
      external-only and has no local content-store fallback.

## Release checklist

- [ ] All phase acceptance criteria met and checked off.
- [ ] Full test matrix green.
- [ ] `README.md`, `CONTRIBUTING.md`, `config.toml`, `deploy/` updated.
- [ ] Mixed-library smoke test on a low-resource profile.
- [ ] Security review of path/zip/MIME/range handling.
- [ ] Major version bump. Release notes list the new providers, the
      `[[collections]]` config requirement, the catalog/index rebuild, and the
      removed compatibility paths.
- [ ] A known-issues note covering supported Kolibri schema versions and
      derived-text engine/model limitations.

## Risks register

| Risk | Impact | Mitigation |
|---|---|---|
| Source explosion | unusable filters, slow fan-out | collection model + source caps + collapse |
| Path escape | data disclosure | real-root confinement + adversarial tests |
| Zip-slip | data disclosure/code execution | manifest validation, no extraction outside root |
| MIME confusion | XSS/drive-by | extension-first MIME, nosniff, attachment policy, sandbox |
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
5. The core has no provider-specific branches outside serialization.
6. The security, resource, and failure-isolation requirements above are tested
   and documented.
