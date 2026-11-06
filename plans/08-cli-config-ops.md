# Phase 08 — CLI, config, and operations

Status: proposed.

## Goal

Make multiple providers configurable and operable: one config schema, one build
command that covers every buildable provider, one source/collection inspection
command, and one index layout for local indexed providers.

## Config schema

Today `zimantic/settings.py` accepts only flat `KNOWN_KEYS`; `load_config`
silently drops unknown keys and warns on bad types. `[[collections]]` is an
array-of-tables, so it needs explicit handling.

### Shape

```toml
# Global search bound for an explicitly selected large remote collection.
# A request reports partial completion when this count or its deadline is hit.
max_explicit_collection_sources = 500

# Collections. Multiple entries are allowed. There are no flat provider keys.
[[collections]]
type = "zim"
name = "library"
path = "./zims"
label = "ZIM files"

[[collections]]
type = "files"
name = "modules"
path = "/library/www/html/modules"
label = "Modules"
granularity = "collection"

# Optional exact-source opt-in; omitted operations remain off.
# [[collections.sources]]
# key = "files:modules:root"
# captions = "index"
# ocr = "tesseract"
# asr = "whisper"

[[collections]]
type = "kolibri"
name = "kolibri"
path = "/library/kolibri"
label = "Kolibri"

[[collections]]
type = "kiwix"
name = "remote"
server = "http://localhost:8085"
label = "Remote Kiwix"

# Engines run locally at build time. This table configures them; it does not
# enable any operation. Only an exact-source override can do that.
[derived_text]
max_caption_bytes = 1048576
ocr_languages = ["eng"]
max_ocr_pages = 20
max_ocr_pixels = 50000000
max_ocr_output_bytes = 262144
asr_model = "base.en"
max_asr_seconds = 3600
max_asr_bytes = 1073741824
max_asr_output_bytes = 1048576
task_timeout_seconds = 7200
max_spool_bytes = 2147483648
workers = "auto"   # "auto" | positive int; auto is CPU/memory-derived
cache = true
```

All three operations are off unless a source explicitly names one. `captions`
accepts only `"off"` or `"index"`; `ocr` and `asr` accept `"off"` or the engine
name.

### Rules

- TOML keys after a table header belong to that table. Keep global scalar keys
  before the first `[[collections]]` or `[derived_text]` header in shipped and
  documented examples; otherwise they become provider/derived-text keys.
- `type` is required and must be one of the in-tree providers.
- `name` is required, unique across all entries, and stable: it becomes part of
  collection/source keys and index paths. Apply phase 00's safe-name validation
  and reserve `open` for the ephemeral ZIM collection.
- Provider-specific keys are validated by the provider. Generic validation only
  checks `type`, `name`, and duplicate names.
- `max_explicit_collection_sources` is a positive global request-work cap for
  an explicitly selected Kiwix collection. It defaults to `500`; it does not
  affect a single explicit source or local collection discovery. Reaching it is
  reported as a partial search, never silently broadened or treated as complete.
- Phase 00 defines the normalized provider-entry interface, minimal
  `[[collections]]` TOML parser, and public `RuntimeConfig.collections`
  discovery API. Phases 04–06 may use that API plus the phase-04
  derived-text settings envelope to discover and build configured sources;
  they do not depend on CLI selectors or final diagnostics. Phase 05 adds
  files-provider validation; this phase completes the loader for every
  provider and owns selector parsing, diagnostics presentation, and remaining
  config operations.
- Every collection, including ZIM and Kiwix, requires an explicit `[[collections]]`
  entry. There is no `zim_dir` or `kiwix_server` shorthand and no implicit
  default collection.
- An invalid provider entry, including an unknown provider type, logs a single
  clear error and is reported in configured-entry diagnostics. Mark a collection
  unavailable when a valid collection key can be formed; do not invent one for
  an entry missing a valid name/type. Disable all entries with a duplicate name.
  These errors do not prevent valid entries from working. Invalid TOML remains
  fatal. Registry lookup errors remain explicit; there is no fallback provider.
- Unknown keys inside a provider entry warn once and are ignored; do not turn a
  typo into a crash.
- Keep `collections` as a known top-level key and bypass the flat type coercion for
  it in `_typed_user_values`.
- Do not log absolute `path`/`database_dir`/`storage` values above debug level.

### Derived text

Phase 04 owns the canonical `[derived_text]` schema and validation. This phase
loads and exposes that schema through `settings.py`, preserving its global
scope and precedence; it does not introduce a second definition.

Use exact-key `[[collections.sources]]` switches, all off by default, and
expose the phase-04 operation states and build-report counters without loading
engines. Validate engine names and report missing engines as operation-local
unavailability; preserve the phase-04 fingerprint, local-only model, shared
lazy pool, worker-resolution, cap, retry, and non-zero partial-build rules.
This phase owns settings loading, precedence, diagnostics, and CLI exposure,
not a second derived-text policy.

### Profiles and defaults

- Provider entries are independent of the hardware profile.
- Keep `index_dir`, `cache_size`, `build_workers`, `embedding_workers`,
  `batch_size`, search limits, and model settings global.
- Provider-specific limits (`max_sources`, `max_entries`, `max_file_bytes`,
  `granularity`, and traversal budgets) default in the provider and may be
  overridden per entry. Derived-text switches are exact-source overrides.
- `granularity` uses the same values for Files and Kolibri:
  `collection` means one source for the whole collection and `children` means
  immediate child groups.
- `enabled_providers` is not needed: a provider is enabled by having at least
  one configured collection. Keep it that way.

## CLI

Current commands: `serve`, `build`, `reload`, `model`.

### `build`

```bash
zimantic build                     # all configured collections
zimantic build zims/book.zim      # ad-hoc ZIM; matches config or saves a collection
zimantic build zims/              # ad-hoc folder; matches config or saves a collection
zimantic build --provider files    # only files collections
zimantic build --collection modules
zimantic build --source files:modules:root
zimantic build --list              # show what would be built; no work
```

- Positional paths preserve the ad-hoc ZIM build workflow: each argument is a
  ZIM file or a folder expanded with the normal bounded ZIM traversal. With no
  positional paths, build configured collections as before. Positional paths
  and `--provider`/`--collection`/`--source` selectors are separate modes and
  cannot be combined.
- For each resolved ad-hoc path, match configured ZIM collection roots that
  contain it; use the most-specific (longest resolved root) match, and report
  an error for equally specific matches. The matched collection's settings
  apply; explicit CLI build options override those settings. Matching supplies
  defaults only: build exactly the files/folders selected on the command line,
  never widen the build to the rest of the matched collection.
- If an ad-hoc path has no matching configured collection, append a new
  `[[collections]] type = "zim"` entry to the active config before building.
  Use the path argument as that collection's path. Generate a unique safe name
  from its final component plus the first available numeric suffix; do not
  derive the name or source key from an absolute path. Report the entry and
  config file changed. Under a config-write lock, reread and recheck matches,
  append without rewriting existing TOML content, and atomically replace the
  config. Preserve the original file on write failure and do not build if the
  persistent config update fails. Duplicate resolved arguments are built once.
  `--list` previews generated entries but never writes config or starts a build.
- `--provider`, `--collection`, and `--source` are repeatable: values within
  one selector are ORed, and different selector types are intersected.
- `--collection` accepts an exact collection key or a configured entry name.
  When one entry expands to multiple collections (Kolibri channels), the entry
  name selects all of its collections; do not resolve this by an ambiguous
  string-prefix match.
- Provider builds run under the global `BuildLock`; do not run one lock per
  provider.
- Kiwix is remote/search-only and is skipped with a clear report when building
  all collections. An explicit selection containing no buildable sources is
  a non-zero usage error, not a successful no-op.
- `--workers`, `-j`, `--lock-timeout`, `--no-wait-lock`, `--fast`, `--force`,
  and `--device` apply to every provider where meaningful. `--fast` means
  title/path-only indexing and disables model/FAISS loading and semantic search
  for the entire serve process; `--force` rebuilds. These explicit command-line
  options override applicable configured defaults for both configured and
  ad-hoc builds.
- The shared `EmbeddingPipeline` remains global; provider workers submit into
  it.
- Report per-source failures and partial requested enrichment at the end and
  exit non-zero if either occurred. A fast build reports enrichment as not
  attempted by design; it does not treat that choice as a failure.

### `sources`

New read-only command:

```bash
zimantic sources                 # human-readable tree
zimantic sources --json          # collections + sources, no absolute paths
zimantic sources --provider files
```

Uses the same runtime discovery as `serve` but does not start the web server or
load the model. Useful for IIAB/systemd diagnostics.
The JSON command reuses the same `CollectionSnapshot` and `SourceInfo` records
and nullability rules as the HTTP APIs, but its top-level document contains
both `collections` and `sources`; it is not identical to `/collections.json`.

### `prune`

New maintenance command:

```bash
zimantic prune --dry-run         # indexes/staging files for unconfigured sources
zimantic prune --yes             # delete them
```

Scope:

- only files under `index_dir` that Zimantic itself created;
- never touch ZIM files, configured roots, Kolibri storage, or unknown files;
- require ownership evidence from recognized index metadata before deleting an
  orphan and its associated artifacts. A legacy file whose ownership cannot be
  established is reported for manual inspection, never deleted by extension
  or filename alone;
- report reclaimed bytes.

### `serve` and `reload`

- `serve` warns "no searchable sources" while mentioning the configured
  providers.
- `reload` rescans all providers, not only ZIM.
- Keep SIGHUP and PID-file behavior.

## Index layout and catalog rebuild

- Every local indexed provider uses the phase-04 locator's
  source directory with a `current` generation manifest and immutable
  `generations/<uuid>/index.sqlite` / `index.faiss` pair. There is no ZIM-only
  path exception.
- The phase-04 locator is the only code that computes index paths.
- Require SQLite 3.37 or newer for `STRICT` tables; check the runtime SQLite
  version at startup and report an actionable error rather than silently
  creating a non-STRICT schema.
- `index_dir` may itself be configured as an absolute path. Do not incorporate
  source-root absolute paths into logical IDs or generated index filenames.
- `catalog.sqlite` is the private ZIM archive metadata cache beside `index_dir`.
  Files and Kolibri do not write it, and Kiwix has no persistent local catalog.
  Keep the resolved absolute `path` as a private indexed locator, separate from
  the logical primary key `key`. Canonicalize paths at discovery/update time,
  not repeatedly during search.
- Phase 01 bumps the catalog schema version and rebuilds a mismatched catalog
  by a full ZIM scan. Do not detect, migrate, or alias old tables.
- Ignore unknown files and stale `.tmp`/`.upgrade` files except in `prune`.
  The one-time phase-04 importer discovers only catalog-matched old ZIM index
  paths; `prune` removes obsolete owned artifacts after successful adoption or
  rebuild and leaves unknown files alone.
- Store provider, collection, source key, and content fingerprint in index
  metadata; use them for `sources`, health, and prune.
- Never serialize catalog locators into `sources --json`, HTTP responses, or
  user-visible errors.
- Removal/rename of a configured collection does not delete indexes
  automatically. `prune` is the explicit cleanup path.

## Startup, concurrency, and resources

- ZIM discovery is usually cheap; like other providers it must not prevent
  serving if a slow filesystem exceeds the startup budget.
- Files discovery is `stat`-only and bounded by `max_sources`/excludes.
- Kolibri discovery is a bounded read-only query.
- Kiwix discovery stays cached-first with a background refresh.
- If a provider is slow, start it in the background and publish its sources
  when ready; local ZIM search must never wait for it. `serve` may expose a
  `loading` collection, but `build`, `sources`, and `reload` wait for the
  requested discovery snapshot or report a bounded timeout.
- Runtime refreshes publish source/collection snapshots atomically and expose
  `loading`, `ready`, `error`, and `closed` states plus a monotonically
  increasing snapshot version. Close stops accepting work, cancels queued work,
  and waits for active work before releasing its handles. Use bounded I/O or
  killable isolation where cancellation/deadlines cannot otherwise be enforced;
  cancelling a thread future does not stop a running call.
- Apply fair per-provider task budgets. Reserve a worker for competing providers
  when `search_workers > 1`; with one worker, fairness is bounded by the active
  task's deadline. Count work across all collections of a provider kind and all
  concurrent searches, including timed-out calls still running. A bounded,
  fair queue admits work incrementally rather than submitting one future per
  discovered source. Keep the global search slot and cache behavior.
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

- [ ] Complete `settings.py` from the phase-00 raw `[[collections]]` parser and
      phase-05 files validation to a list of tables for every provider;
      validate `type`/`name`/duplicates and delegate provider keys.
- [ ] Build a provider configuration registry with per-provider defaults and
      validation functions.
- [ ] Expose the phase-04 `[derived_text]` schema and validate per-source
      `captions`, `ocr`, and `asr`; all default off, unknown engines are
      explicit errors, and missing engines degrade only that operation.
- [ ] Implement resource-derived `workers = "auto"` resolution (CPU and memory
      limits, unavailable if none fits, no arbitrary low cap) and an explicit
      integer override.
- [ ] Refactor `__main__.py`:
      - `_run_build` into a provider-aware orchestrator;
      - preserve positional ZIM file/folder arguments and ad-hoc indexing;
      - add `--provider`, `--collection`, `--source`, `--list`;
      - add `sources` and `prune` commands;
      - generalize the "no sources" warning.
- [ ] Resolve ad-hoc paths against configured ZIM roots by most-specific
      ancestor; reject tied matches. Append an unmatched path as a new
      configured ZIM collection using a safe unique name independent of the
      absolute path and an atomic, non-rewriting config append; lock and recheck
      config before writing, preserve config contents and report failure.
- [ ] Add `Search` discovery of configured collections at startup, with
      background publication for slow providers.
- [ ] Make build/source inspection/reload commands wait for discovery rather
      than operating on a partial background snapshot.
- [ ] Add per-provider search budgets.
- [ ] Add unit tests for config validation and CLI argument handling.
- [ ] Update `config.toml`, `README.md`, `CONTRIBUTING.md`, and `deploy/`.
- [ ] Add migration tests showing that old config is rejected, an old catalog
      is rebuilt, and only recognized old index layouts are imported; unknown
      indexes are rebuilt through the documented path.

## Tests

- [ ] Config: `[[collections]]` entries parse with global keys in the correct
      TOML scope; duplicate names, unknown types, bad
      types, and missing `path` produce clear warnings and do not crash. Flat
      `zim_dir`/`kiwix_server` keys are unknown and never create collections.
- [ ] `build` no-arg builds all configured providers; positional ZIM paths
      still expand files/folders and index ad hoc; a matching configured root
      supplies defaults, and an unmatched path is appended to config before
      build. Explicit CLI options override matched settings.
- [ ] Ad-hoc path matching chooses the most-specific configured root and
      rejects ties; duplicate arguments build once; appending config is atomic
      and leaves the original unchanged on failure. A matched collection
      supplies settings without widening the selected input scope; `--list`
      previews pending config changes without writing them.
- [ ] Generated collection names are safe, unique, and do not encode absolute
      path data; concurrent appends cannot lose an intervening config update.
- [ ] A generated configuration entry for a single-file ZIM remains a
      single-file collection; a generated entry for a folder remains scoped to
      that folder. Serving either entry does not discover sibling archives.
- [ ] An ad-hoc invocation does not mutate an existing matched collection's
      options; only explicit CLI overrides affect that run. An unmatched path
      is persisted once as a new collection, then used for that invocation.
- [ ] `--provider`,
      `--collection`, and `--source` filter correctly; `--list` does no work.
- [ ] Repeatable values are ORed within a selector and intersected across
      selector types; positional paths cannot be mixed with selectors.
- [ ] Build failures for one provider/source do not block the others; exit
      code is non-zero.
- [ ] Kiwix is reported as skipped in an all-provider build; an explicitly
      selected remote-only scope fails rather than succeeding without a build.
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
- [ ] Two sources in one collection can have different switches; duplicate and
      stale overrides are reported, and collapse never transfers an opt-in.
- [ ] `workers = "auto"` resolves to 1 on a simulated low-memory host and can
      exceed 4 on a simulated large host; CPU and memory binding limits are
      reported. The OCR/ASR pool is lazy and shared, and concurrent source
      builds do not multiply its size.
- [ ] Changing a derived-text operation or engine/model changes the fingerprint
      and forces a rebuild.
- [ ] The one-time index importer adopts only recognized, validated old ZIM
      indexes; `prune` deletes only obsolete files whose Zimantic ownership can
      be established and leaves unknown files alone.
- [ ] A slow provider does not delay ZIM startup beyond the configured budget.

## Acceptance criteria

- One `config.toml` can describe ZIM, files, Kolibri, and Kiwix collections.
- `zimantic build`, `serve`, `reload`, `sources`, and `prune` work with a mixed
  library.
- Every ZIM-only install is migrated by adding a `[[collections]]` entry and
  rebuilding its indexes.
- Documentation contains a copy-pasteable IIAB example for each local provider.
- `python -m pytest` passes.

## Risks and mitigations

- **Config ambiguity.** One schema only; there are no legacy shorthands to
  merge with explicit entries. Duplicate `name` values are rejected.
- **Config mutation from ad-hoc builds.** Preserve positional ZIM builds and
  their path-matched settings; unmatched paths are made reproducible by
  appending a uniquely named collection atomically. `open` remains the
  inspection-only command and never mutates config.
- **Startup regressions.** Background discovery for slow providers and a hard
  time budget for synchronous ones.
- **Deleting user data.** `prune` is opt-in, scoped to known index files, and
  defaults to dry-run.
