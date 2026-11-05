# Phase 08 — CLI, config, and operations

Status: proposed.

## Goal

Make multiple providers configurable and operable: one config schema, one build
command that covers every provider, one source/collection inspection command,
and a predictable index layout that keeps existing ZIM installations working.

## Config schema

Today `zimantic/settings.py` accepts only flat `KNOWN_KEYS`; `load_config`
silently drops unknown keys and warns on bad types. `[[sources]]` is an
array-of-tables, so it needs explicit handling.

### Shape

```toml
# Legacy shorthand, still accepted and unchanged.
zim_dir = "./zims"
kiwix_server = ""

# Explicit providers. Multiple entries are allowed.
[[sources]]
type = "zim"
name = "zim"
path = "./zims"
label = "ZIM files"

[[sources]]
type = "files"
name = "modules"
path = "/library/www/html/modules"
label = "Modules"
granularity = "directory"

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
- Phase 00 defines a normalized provider-entry interface so provider phases can
  validate entries without parsing TOML themselves. This phase owns the TOML
  `[[sources]]` parser and conversion into that interface.
- Legacy `zim_dir` creates the default `zim` collection unless an explicit
  `[[sources]] type = "zim"` already uses the same resolved path; in that case
  warn and skip the legacy shorthand to avoid duplicate sources.
- Legacy `kiwix_server` creates the default `kiwix` collection unless an
  explicit Kiwix entry uses the same server.
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
- `enabled_providers` is not needed: a provider is enabled by having at least
  one configured collection. Keep it that way.

## CLI

Current commands: `serve`, `build`, `reload`, `model`.

### `build`

Backward compatibility:

- `zimantic build` with no paths still builds every ZIM under `zim_dir` (and any
  explicit ZIM collections).
- `zimantic build <path>...` still treats paths as ZIM files/folders,
  recursively, exactly as today.

New behavior:

```bash
zimantic build                     # all configured providers
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
- Provider builds run under the existing global `BuildLock`; do not run one
  lock per provider.
- ZIM builds keep `--workers`, `-j`, `--wait-stable`, `--no-wait-lock`,
  `--lock-timeout`, `--fast`, `--force`, and `--device`.
- For non-ZIM providers, `--fast` means title/path-only indexing; `--force`
  rebuilds; `--wait-stable` is a no-op (there is no download handoff).
- The shared `EmbeddingPipeline` remains global; provider workers submit into
  it.
- Report per-source failures at the end and exit non-zero if any failed,
  matching today's ZIM behavior.

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

- `serve` warning changes from "no local ZIM sources" to "no searchable
  sources" while mentioning the configured providers.
- `reload` rescans all providers, not only ZIM.
- Keep SIGHUP and PID-file behavior unchanged.

## Index layout and migration

- Legacy ZIM indexes stay at `<index_dir>/<stem>.sqlite` and `.faiss`.
- New providers use the phase-04 locator's
  `<index_dir>/<provider>/<collection-slug>-<collection-hash>/<source-slug>-<source-hash>.*`
  layout.
- The phase-04 locator is the only code that computes index paths.
- `index_dir` may itself be configured as an absolute path. Do not incorporate
  source-root absolute paths into logical IDs or generated index filenames.
- `catalog.sqlite` is private local metadata beside `index_dir`. Preserve the
  resolved absolute `path` as a private indexed locator, separate from the
  logical primary key `(provider, collection_key, source_key)`. Canonicalize
  paths at discovery/update time, not repeatedly during search.
- Migrate the supported legacy `zims` catalog schema transactionally to the
  generic `sources` schema on first run; retain the catalog filename and
  metadata, but remove old table/index names. Rebuild only if the legacy
  schema is corrupt or unsupported. This intentionally ends on-disk
  compatibility with tools that expect the old IIAB `zims` table.
- On first run after upgrade:
  - discover legacy ZIM indexes exactly as before; do not migrate or rewrite
    them;
  - create provider subdirectories lazily;
  - ignore unknown files and stale `.tmp`/`.upgrade` files except in `prune`.
- Store provider, collection, source key, and content fingerprint in index
  metadata; use them for `sources`, health, and prune, not for changing the
  legacy ZIM layout.
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
  `search_workers`. Keep the global search slot and cache behavior unchanged.
- `max_sources`/`max_entries` caps apply to discovery and indexing; serving a
  file does not require it to be indexed.
- Health reports per-collection counts, errors, and whether discovery is still
  running.

## Documentation

- `README.md`:
  - new "Sources and providers" section;
  - config examples for ZIM, files, Kolibri, and Kiwix;
  - collection/source explanation and the bounded-source rule;
  - index layout and migration note;
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

- [ ] Extend `settings.py` to accept `sources` as a list of tables, validate
      `type`/`name`/duplicates, and delegate provider keys.
- [ ] Implement legacy shorthand merging for `zim_dir` and `kiwix_server`.
- [ ] Build a provider configuration registry with per-provider defaults and
      validation functions.
- [ ] Parse and validate `[derived_text]` plus per-source `captions`, `ocr`,
      and `asr`; all default off, unknown engines are explicit errors, and
      missing engines degrade only that operation.
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
- [ ] Add an upgrade test starting from a pre-change `index_dir` and
      `config.toml`.

## Tests

- [ ] Config: legacy-only config behaves exactly as today; `[[sources]]`
      entries parse; duplicate names, unknown types, bad types, and missing
      `path` produce clear warnings and do not crash.
- [ ] Legacy shorthand + explicit entry for the same path does not duplicate
      sources.
- [ ] `build` no-arg builds all configured providers; `--provider`,
      `--collection`, and `--source` filter correctly; `--list` does no work.
- [ ] `build <path>` remains ZIM-only and recursive.
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
- [ ] Legacy indexes are discovered without a rebuild.
- [ ] A slow provider does not delay ZIM startup beyond the configured budget.

## Acceptance criteria

- One `config.toml` can describe ZIM, files, Kolibri, and Kiwix collections.
- `zimantic build`, `serve`, `reload`, `sources`, and `prune` work with a mixed
  library.
- Existing ZIM-only installs need no config or index changes.
- Documentation contains a copy-pasteable IIAB example for each local provider.
- `python -m pytest` passes.

## Risks and mitigations

- **Config ambiguity.** Keep legacy shorthands and explicit entries additive,
  with duplicate detection; never silently create two collections for one root.
- **CLI behavior change.** Preserve no-arg and path-argument ZIM semantics;
  document provider flags clearly.
- **Startup regressions.** Background discovery for slow providers and a hard
  time budget for synchronous ones.
- **Deleting user data.** `prune` is opt-in, scoped to known index files, and
  defaults to dry-run.
