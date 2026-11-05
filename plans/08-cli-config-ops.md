# Phase 08 — CLI, config, and operations

Status: proposed.

## Goal

Make multiple providers configurable and operable: one config schema, one build
command that covers every provider, one source/collection inspection command,
and one index layout for every provider.

## Config schema

Today `zimantic/settings.py` accepts only flat `KNOWN_KEYS`; `load_config`
silently drops unknown keys and warns on bad types. `[[sources]]` is an
array-of-tables, so it needs explicit handling.

### Shape

```toml
# Collections. Multiple entries are allowed. There are no flat provider keys.
[[sources]]
type = "zim"
name = "library"
path = "./zims"
label = "ZIM files"

[[sources]]
type = "files"
name = "modules"
path = "/library/www/html/modules"
label = "Modules"
granularity = "collection"

[[sources]]
type = "kolibri"
name = "kolibri"
path = "/library/kolibri"
label = "Kolibri"
captions = "off"        # off | index: existing caption/subtitle tracks
ocr = "off"             # off | tesseract: images and image-only pages
asr = "off"             # off | whisper: audio/video speech-to-text

[[sources]]
type = "kiwix"
name = "remote"
server = "http://localhost:8085"
label = "Remote Kiwix"

# Engines run locally at build time. This table configures them; it does not
# enable any operation. Only a source's captions/ocr/asr key can do that.
[derived_text]
max_caption_bytes = 1048576
ocr_languages = ["eng"]
max_ocr_pages = 20
max_ocr_pixels = 50000000
max_ocr_output_bytes = 262144
asr_model = "base.en"
max_asr_seconds = 3600
max_asr_bytes = 1073741824
workers = "auto"   # "auto" | positive int; auto is CPU/memory-derived, floor 1
cache = true
```

All three operations are off unless a source explicitly names one. `captions`
accepts only `"off"` or `"index"`; `ocr` and `asr` accept `"off"` or the engine
name.

### Rules

- `type` is required and must be one of the in-tree providers.
- `name` is required, unique across all entries, and stable: it becomes part of
  collection/source keys and index paths.
- Provider-specific keys are validated by the provider. Generic validation only
  checks `type`, `name`, and duplicate names.
- Phase 00 defines the normalized provider-entry interface and minimal
  `[[sources]]` TOML parser so provider phases can validate entries without
  parsing TOML themselves. Phase 05 adds files-provider validation; this phase
  completes the loader for every provider and owns the remaining config
  operations.
- Every collection, including ZIM and Kiwix, requires an explicit `[[sources]]`
  entry. There is no `zim_dir` or `kiwix_server` shorthand and no implicit
  default collection.
- An invalid provider entry, including an unknown provider type, logs a single
  clear error and marks that collection unavailable. It must not prevent the
  server from starting or other providers from working. Invalid TOML remains
  fatal. Internal registry lookup errors must still be explicit; there is no
  fallback provider.
- Unknown keys inside a provider entry warn once and are ignored; do not turn a
  typo into a crash.
- Keep `sources` as a known top-level key and bypass the flat type coercion for
  it in `_typed_user_values`.
- Do not log absolute `path`/`database`/`storage` values above debug level.

### Derived text

Phase 04 owns the canonical `[derived_text]` schema and validation. This phase
loads and exposes that schema through `settings.py`, preserving its global
scope and precedence; it does not introduce a second definition.

- `captions`, `ocr`, and `asr` are per-source switches, all `off` by default.
  There is no global "enable enrichment" switch, and enabling one operation
  never enables another.
- `captions = "index"` parses existing tracks. `ocr` and `asr` name the engine
  (`"tesseract"`, `"whisper"`); `[derived_text]` holds only model, language,
  cache, concurrency, and cap settings.
- Validate engine names at config load. OCR and ASR are optional dependency
  extras; a configured engine that is not installed marks only that operation
  unavailable for the source with a clear error. It must not crash startup or
  block builds for sources that do not request it.
- Models are local files or preinstalled packages. Never download at build time
  and never contact a hosted OCR/ASR service.
- Include the resolved operations and engine/model/version in the source
  fingerprint so changing them rebuilds the index.
- `workers` accepts `"auto"` or a positive integer. Auto is
  `max(1, min(cpu_task_limit, memory_worker_limit))`, derived from usable cores,
  currently available memory (including cgroup headroom where detectable), the
  enabled engines' estimated working set, and headroom for `build_workers` and
  the embedding pipeline. There is no arbitrary low cap; low-memory hosts
  resolve to 1, while a large host may use more than 4. An explicit integer is
  honored with a warning if it exceeds the estimated memory budget.
- Engine-internal threads are set to 1 by default so process workers do not
  oversubscribe CPU. Report the resolved worker count and the binding limit in
  the build report.
- Caption parsing runs in-process. OCR/ASR use one lazy, shared process pool per
  build run, not one pool per source. This isolates native engines that are not
  thread-safe and keeps model memory bounded under concurrent source builds.
- `zimantic sources` and health report each operation's state
  (`off`/`ready`/`unavailable`); the build report counts extracted, cached, and
  cap-skipped inputs per operation.

### Profiles and defaults

- Provider entries are independent of the hardware profile.
- Keep `index_dir`, `cache_size`, `build_workers`, `embedding_workers`,
  `batch_size`, search limits, and model settings global.
- Provider-specific limits (`max_sources`, `max_entries`, `max_file_bytes`,
  `captions`, `ocr`, `asr`, `source_depth`) default in the provider and may be
  overridden per entry.
- `granularity` (files: `collection`/`directory`) and `source_depth` (Kolibri:
  0/1) both bound source count but are not synonyms: files chooses a root
  granularity, Kolibri chooses a topic depth.
- `enabled_providers` is not needed: a provider is enabled by having at least
  one configured collection. Keep it that way.

## CLI

Current commands: `serve`, `build`, `reload`, `model`.

### `build`

```bash
zimantic build                     # all configured collections
zimantic build --provider files    # only files collections
zimantic build --collection modules
zimantic build --source files:modules:algebra
zimantic build --list              # show what would be built; no work
```

- `--provider`, `--collection`, and `--source` are repeatable and intersect.
- `--collection` accepts an exact collection key or a configured entry name.
  When one entry expands to multiple collections (Kolibri channels), the entry
  name selects all of its collections; do not resolve this by an ambiguous
  string-prefix match.
- There are no positional path arguments. Build only configured collections;
  use `zimantic open` to browse an unconfigured archive without indexing it.
- Provider builds run under the global `BuildLock`; do not run one lock per
  provider.
- `--workers`, `-j`, `--lock-timeout`, `--no-wait-lock`, `--fast`, `--force`,
  and `--device` apply to every provider where meaningful. `--fast` means
  title/path-only indexing; `--force` rebuilds.
- The shared `EmbeddingPipeline` remains global; provider workers submit into
  it.
- Report per-source failures at the end and exit non-zero if any failed.

### `sources`

New read-only command:

```bash
zimantic sources                 # human-readable tree
zimantic sources --json          # collections + sources, no absolute paths
zimantic sources --provider files
```

Uses the same runtime discovery as `serve` but does not start the web server or
load the model. Useful for IIAB/systemd diagnostics.
The JSON command reuses the same `CollectionInfo` and `SourceInfo` records and
nullability rules as the HTTP APIs, but its top-level document contains both
`collections` and `sources`; it is not identical to `/collections.json`.

### `prune`

New maintenance command:

```bash
zimantic prune --dry-run         # indexes/staging files for unconfigured sources
zimantic prune --yes             # delete them
```

Scope:

- only files under `index_dir` that Zimantic itself created;
- never touch ZIM files, configured roots, Kolibri storage, or unknown files;
- report reclaimed bytes.

### `serve` and `reload`

- `serve` warns "no searchable sources" while mentioning the configured
  providers.
- `reload` rescans all providers, not only ZIM.
- Keep SIGHUP and PID-file behavior.

## Index layout and catalog rebuild

- Every provider uses the phase-04 locator's
  `<index_dir>/<provider>/<collection-slug>-<collection-hash>/<source-slug>-<source-hash>.*`
  layout. There is no ZIM-only path exception.
- The phase-04 locator is the only code that computes index paths.
- `index_dir` may itself be configured as an absolute path. Do not incorporate
  source-root absolute paths into logical IDs or generated index filenames.
- `catalog.sqlite` is private local metadata beside `index_dir`. Keep the
  resolved absolute `path` as a private indexed locator, separate from the
  logical primary key `key`. Canonicalize paths at discovery/update time, not
  repeatedly during search.
- Bump the catalog schema version. A catalog with any other version is deleted
  and rebuilt by a full provider scan. Do not detect, migrate, or alias old
  tables.
- Ignore unknown files and stale `.tmp`/`.upgrade` files except in `prune`.
  Old index files are not discovered; they are removed by `prune` after the
  operator rebuilds configured collections.
- Store provider, collection, source key, and content fingerprint in index
  metadata; use them for `sources`, health, and prune.
- Never serialize catalog locators into `sources --json`, HTTP responses, or
  user-visible errors.
- Removal/rename of a configured collection does not delete indexes
  automatically. `prune` is the explicit cleanup path.

## Startup, concurrency, and resources

- ZIM discovery remains synchronous and cheap enough for startup.
- Files discovery is `stat`-only and bounded by `max_sources`/excludes.
- Kolibri discovery is a bounded read-only query.
- Kiwix discovery stays cached-first with a background refresh.
- If a provider is slow, start it in the background and publish its sources
  when ready; local ZIM search must never wait for it. `serve` may expose a
  `loading` collection, but `build`, `sources`, and `reload` wait for the
  requested discovery snapshot or report a bounded timeout.
- Runtime refreshes publish source/collection snapshots atomically and expose
  `loading`, `ready`, `error`, and `closed` states plus a monotonically
  increasing snapshot version. Closing a provider cancels or joins all
  background work before releasing its database/store handles.
- Apply a per-provider search-task budget so one provider cannot occupy all
  `search_workers`. Keep the global search slot and cache behavior.
- `max_sources`/`max_entries` caps apply to discovery and indexing; serving a
  file does not require it to be indexed.
- Health reports per-collection counts, errors, and whether discovery is still
  running.

## Documentation

- `README.md`:
  - new "Sources and providers" section;
  - config examples for ZIM, files, Kolibri, and Kiwix;
  - collection/source explanation and the bounded-source rule;
  - index layout and catalog-rebuild note;
  - security note for untrusted file trees (`secure_browsing`);
  - derived-text section: the three independent opt-ins, local-only engines,
    optional dependencies, and build-time-only execution;
  - IIAB layout examples (`/library/www/html/modules`, `/library/kolibri`).
- `CONTRIBUTING.md`:
  - "Adding an in-tree provider" checklist;
  - contract tests required;
  - caps, path confinement, and failure-isolation requirements;
  - no third-party plugin loading.
- `config.toml`:
  - commented examples for each provider;
  - defaults documented next to the keys.
- `deploy/`:
  - build timer/path examples for non-ZIM collections;
  - service `ReadOnlyPaths`/`ReadWritePaths` examples for files/Kolibri.

## Work items

- [ ] Complete `settings.py` from the phase-00 raw `[[sources]]` parser and
      phase-05 files validation to a list of tables for every provider;
      validate `type`/`name`/duplicates and delegate provider keys.
- [ ] Remove flat `zim_dir`/`kiwix_server` handling; all collections require an
      explicit `[[sources]]` entry.
- [ ] Build a provider configuration registry with per-provider defaults and
      validation functions.
- [ ] Expose the phase-04 `[derived_text]` schema and validate per-source
      `captions`, `ocr`, and `asr`; all default off, unknown engines are
      explicit errors, and missing engines degrade only that operation.
- [ ] Implement resource-derived `workers = "auto"` resolution (CPU and memory
      limits, floor 1, no arbitrary low cap) and an explicit integer override.
- [ ] Refactor `__main__.py`:
      - `_run_build` into a provider-aware orchestrator;
      - add `--provider`, `--collection`, `--source`, `--list`;
      - add `sources` and `prune` commands;
      - generalize the "no sources" warning.
- [ ] Add `Search` discovery of configured collections at startup, with
      background publication for slow providers.
- [ ] Make build/source inspection/reload commands wait for discovery rather
      than operating on a partial background snapshot.
- [ ] Add per-provider search budgets.
- [ ] Add unit tests for config validation and CLI argument handling.
- [ ] Update `config.toml`, `README.md`, `CONTRIBUTING.md`, and `deploy/`.
- [ ] Add a migration test showing that an old config, catalog, or index layout
      is not read and that the documented rebuild path works.

## Tests

- [ ] Config: `[[sources]]` entries parse; duplicate names, unknown types, bad
      types, and missing `path` produce clear warnings and do not crash. Flat
      `zim_dir`/`kiwix_server` keys are unknown and never create collections.
- [ ] `build` no-arg builds all configured providers; `--provider`,
      `--collection`, and `--source` filter correctly; `--list` does no work.
- [ ] Positional build paths are rejected with a clear message pointing at
      configured collections and `zimantic open`.
- [ ] Build failures for one provider/source do not block the others; exit
      code is non-zero.
- [ ] `sources --json` has no absolute paths, uses the same collection/source
      record schemas as the HTTP APIs, and clearly documents its combined
      top-level shape.
- [ ] `prune --dry-run` reports only Zimantic-created orphan indexes; `--yes`
      deletes only those.
- [ ] `reload` picks up a new files directory, a new Kolibri channel, and a new
      ZIM.
- [ ] `captions`, `ocr`, and `asr` are independently off by default; setting
      one does not enable the others; a missing engine marks only that operation
      unavailable and never crashes or blocks other builds.
- [ ] `workers = "auto"` resolves to 1 on a simulated low-memory host and can
      exceed 4 on a simulated large host; CPU and memory binding limits are
      reported. The OCR/ASR pool is lazy and shared, and concurrent source
      builds do not multiply its size.
- [ ] Changing a derived-text operation or engine/model changes the fingerprint
      and forces a rebuild.
- [ ] Old-format indexes are ignored; `prune` can remove them after a rebuild.
- [ ] A slow provider does not delay ZIM startup beyond the configured budget.

## Acceptance criteria

- One `config.toml` can describe ZIM, files, Kolibri, and Kiwix collections.
- `zimantic build`, `serve`, `reload`, `sources`, and `prune` work with a mixed
  library.
- Every ZIM-only install is migrated by adding a `[[sources]]` entry and
  rebuilding its indexes.
- Documentation contains a copy-pasteable IIAB example for each local provider.
- `python -m pytest` passes.

## Risks and mitigations

- **Config ambiguity.** One schema only; there are no legacy shorthands to
  merge with explicit entries. Duplicate `name` values are rejected.
- **CLI behavior change.** There are no positional build paths. Document the
  configured-collection and `open` workflows.
- **Startup regressions.** Background discovery for slow providers and a hard
  time budget for synchronous ones.
- **Deleting user data.** `prune` is opt-in, scoped to known index files, and
  defaults to dry-run.
