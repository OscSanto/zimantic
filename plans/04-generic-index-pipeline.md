# Phase 04 — Generic index pipeline

Status: proposed.

## Goal

Make the existing SQLite + FTS + FAISS build/search pipeline provider-agnostic.
Providers supply a bounded, resumable sequence of `EntryRecord`s and an optional
native full-text searcher; the core owns indexing, vectors, publication,
resume, and the generic local search over the index.

After this phase, a synthetic producer that does not touch libzim can build and
search an index in tests.

## Current state

- `zimantic/build.py` — `build(zim_path, ...)`, `_prepare_build_state`,
  `_index_entries`, `_save`, `_finish_build`, `_write_faiss`,
  embedding pipeline, staging/upgrade/publish.
  `_index_entries` loops `range(start, zim.entry_count)` and calls
  `read_entry(zim, i, ...)`.
- `zimantic/content/reader.py` — ZIM-shaped entry reading plus pure HTML,
  plain-text, and PDF extraction. `EntryRecord` currently lives here but moves
  to the dependency-free indexing types module in this phase.
- `zimantic/searching/index.py` — `_LocalIndex`, `ensure_open`,
  `ensure_searcher`, `_nprobe`.
- `zimantic/search.py` — `_search_local_locked`, `_fetch_docs`, previews, and
  native full-text fallback.
- `zimantic/fulltext.py` / `fulltext_worker.py` — crash-isolated native ZIM
  full-text search.

## Target shape

```
zimantic/indexing/
    __init__.py
    types.py          # dependency-free EntryRecord and EntryProducer protocol
    schema.py         # docs/meta/vecs schema and meta keys
    pipeline.py       # prepare/resume/embed/publish; no libzim imports
    locator.py        # provider-scoped index paths
    search.py         # generic local semantic/keyword/native search
    native.py         # NativeSearcher protocol and helper
    derived.py        # optional captions/OCR/ASR enrichment stage

zimantic/providers/zim/
    entries.py        # ArchiveEntryProducer
    native.py         # libzim Searcher adapter
```

### Entry producer contract

```python
class EntryProducer(Protocol):
    key: str                 # source key
    provider: str
    fingerprint: str         # initial snapshot and build inputs
    count: int
    def entry_at(self, index: int) -> EntryRecord | None: ...
    def validate_snapshot(self) -> bool: ...
    def close(self) -> None: ...
```

- Move the existing five-field `EntryRecord` unchanged into `indexing/types.py`:
  `id: int`, `title: str`, `excerpt: str`, `path: str`, and
  `target_id: int | None`. `excerpt` is build-time text, not a promise to retain
  raw text in the published index.
- `EntryRecord.id` is the canonical non-negative 63-bit SQLite row id written to
  `docs.rowid`, `vecs.id`, FAISS, and `EntryRecord.target_id`. It is not an
  arbitrary provider-native id. Providers must keep it stable for the duration
  of a source snapshot and across unchanged rebuilds; path is not an identity
  because duplicate paths are permitted.
- Providers own the deterministic mapping from their native identity to this
  row-id space. ZIM preserves the archive entry's raw `_index` and redirect
  target id, including zero, so existing FTS rowids and FAISS ids do not need
  to be renumbered. Files use one-based manifest position; Kolibri uses its
  deterministic positive 63-bit `(channel_id, node_id)` mapping. A
  provider must reject overflow or mapping collisions rather than merge rows.
  The core never remaps ids after receiving an `EntryRecord`.
- `EntryRecord.target_id` is the nullable canonical row id of the final
  redirect target; non-redirect rows use `None`. The field is named
  `target_id` everywhere, including provider plans and the SQLite `docs.target`
  column. `count` is the length of the bounded build manifest, not necessarily
  the total source size. Non-indexable entries return `None` and are reported
  as skipped rather than changing row-id assignment.
- `entry_at` returns `None` for non-indexable items; the producer is
  responsible for harmless skips.
- `entry_at(index)` accepts a zero-based manifest ordinal in
  `0 <= index < count`; a caller outside that range receives `None`.
  `meta.next` is the next zero-based ordinal to visit, so `0` starts a build
  and `count` means enumeration is complete. It is not a SQLite row id.
- `validate_snapshot()` independently checks the live source and build inputs
  against the initial fingerprint under the traversal budgets. It returns
  false on a detected change and raises a reported error if validation cannot
  finish; merely rereading the stored `fingerprint` is not validation.
- `fingerprint` covers the ordered selected manifest, relevant source
  signatures, extraction settings, and enabled derived-text inputs. Use ZIM
  UUID/size/mtime rounded to whole seconds, a files manifest, and a consistent
  Kolibri DB snapshot plus selected storage-file signatures as specified in
  the provider plans.
- Size/mtime signatures are a practical change detector, not a proof of
  identical bytes: same-size writes with restored timestamps can evade them.
  Document that limitation and require immutable content or an external
  snapshot for strong consistency. Do not read disabled media solely to hash
  it. Builds and reloads must report detected changes rather than claiming
  arbitrary concurrent writes are impossible.
- The producer owns traversal order. The core never walks a filesystem, opens a
  ZIM, or queries Kolibri.
- The build orchestrator closes every producer in `finally`, including
  cancellation and publication failure; its factory cleans up partial
  construction. Producers release their own manifests/snapshots/streams, not
  borrowed runtime content stores.

### Buildable runtime capability

Add one optional internal capability alongside the frozen phase-00 runtime:

```python
@runtime_checkable
class BuildableRuntime(CollectionRuntime, Protocol):
    def entry_producer(
        self, source: SourceInfo, *, fast: bool
    ) -> EntryProducer: ...
```

ZIM implements it here; Files and Kolibri implement it in phases 05/06.
Kiwix does not. The CLI discovers sources through the common runtime contract,
checks this capability, obtains the producer, and calls the same `build_index`
with the global build lock and embedding/enrichment executors. Do not add a
provider-specific `build()` dispatcher, let providers publish indexes
themselves, or wait until phase 08 to define the hook needed by phase 05.

Resume rule: store `source_fingerprint` in `meta` when a build starts. On
resume, if it differs, discard staging and start over. The fingerprint includes
enabled derived-text operations, engine version, local model/data identity, and
availability, so changing them or making a previously missing engine available
triggers a rebuild. A model identifier alone is insufficient if its local files
can change in place. Call `validate_snapshot()` immediately before publication;
if the selected manifest or observed signatures changed during the build,
discard or retry rather than publish it. Never resume a `meta.next` against a
changed item list. A change after the final check can still race publication.
On reload, perform a bounded
signature check or report index freshness as unknown until one can finish;
never treat an unchecked index as verified current. A detectably stale index is
not searched until rebuilt; native full-text and content serving remain
available. An unchecked index may still be searched, but its unknown freshness
is exposed.

### Derived text enrichment

This section is the canonical derived-text policy. Files, Kolibri, CLI, and
rollout plans reference it rather than redefining operation, cache, worker, or
failure semantics.

Native text extraction stays in the existing readers. Derived text is a shared,
optional stage in `indexing/derived.py` so every provider gets the same policy,
caching, provenance, and failure handling.

```python
class DerivedTextInput(Protocol):
    kind: Literal["captions", "image", "audio", "video"]
    mimetype: str
    size: int
    def open(self) -> BinaryIO: ...  # race-safe, provider-confined

class DerivedTextProducer(Protocol):
    def derived_text_inputs(self, index: int) -> Iterable[DerivedTextInput]: ...
```

- The producer decides which candidate files exist and yields bounded,
  provider-confined inputs. The core decides whether to read them and computes
  the digest while consuming an enabled input, rather than requiring a separate
  digest read. Enumeration itself must not read bytes for a disabled operation.
- The hook is provider-neutral: ZIM, files, and Kolibri can implement it, while
  a provider with no local content bytes (Kiwix) cannot. A provider without the
  hook is not enrichable; a requested operation is reported unavailable, not
  silently ignored. ZIM adoption may follow files/Kolibri rather than delaying
  this phase.
- Operations are independent and off by default:
  - `captions = "index"` parses existing subtitle/caption tracks; no model.
  - `ocr = "tesseract"` extracts text from images and image-only pages.
  - `asr = "whisper"` transcribes speech from audio/video.
- The core skips an input kind that the source has not enabled. Prefer native
  text, then enabled captions, then OCR/ASR: skip ASR when enabled caption
  parsing produced usable text, and skip OCR for each page with a text layer.
  When captions are off, never read them to decide whether ASR should run.
- Extractors run only during build. Search, previews, and serving never decode
  media or run a model.
- OCR and ASR are optional dependency extras. A configured but missing engine or
  model marks only that operation unavailable for the source; it does not crash
  the server, prevent other sources from building, or silently substitute
  another operation. Publish available channels with an explicit partial
  enrichment status and non-zero build exit, and retry the requested operation
  on a later build when its availability changes.
  A matching fingerprint alone does not make a partial build up to date:
  subsequent normal builds retry failed/pending requested enrichment even when
  availability is unchanged, reusing only complete cached contributions.
- No network. Models must be provisioned locally before the build.
- Results are cached in a private directory by content digest, operation,
  engine/model and version, and every output-affecting option (including
  language, parser version, and caps). Partial/failed outputs are not cached as
  complete. Cache entries are not source identity and may be removed by
  `zimantic prune`.
- Provenance and excerpt composition: retain native text first, then
  dedupe-append usable caption, OCR, and ASR contributions in that fixed order.
  Normalize each non-empty contribution to Unicode NFKC, casefold, and
  collapsed whitespace for equality only; preserve its original text when
  appending and append an equivalent normalized contribution at most once.
  Do not remove repetition within an individual contribution. Keep the
  operation suppression rules above (for example, captions suppress ASR only
  when enabled captions produced usable text). Record contributing operations
  in index metadata and the build report; optionally expose per-result
  contributors.
- Source/health output reports the last build's per-operation state:
  `off`, `not_checked`, `ready`, `partial`, or `unavailable`. A changed config
  or a fast build is `not_checked` until a matching full build runs. `serve`
  reads this metadata; it never loads an engine to probe its state.
- Budgets come from the global `[derived_text]` config: caption bytes, OCR
  pages/pixels/output bytes, ASR media seconds/input/output bytes, task time,
  aggregate spool bytes, and `workers`. The effective source-file input cap is
  the minimum of the provider's `max_file_bytes` (where applicable), global
  `max_entry_bytes`, and the enabled operation's own byte cap. Serving limits
  are independent. An input skipped or truncated because of a cap,
  malformed data, or failure is logged and reported as partial enrichment;
  other items continue and the overall build exits non-zero. Intentionally
  disabled or unnecessary inputs are not failures.

#### Derived-text configuration ownership

Phase 04 defines and validates the normalized global `[derived_text]` schema
consumed by this stage, including `max_caption_bytes`, OCR/ASR caps, engine
settings, cache behavior, and `workers`. Each configured entry may contain
`[[collections.sources]]` tables with an exact source `key` and explicit
`captions`, `ocr`, or `asr` switches. Unspecified operations and sources are
off; there are no collection-wide enable defaults. Reject all conflicting
overrides for a duplicate key and report unknown/stale keys rather than
applying them to a different source. An override may only name a source
belonging to its configured entry. Defer stale-key diagnostics until
discovery has completed; a loading snapshot is not evidence that a key is
unknown. Overrides carry no budgets. Phase 08 exposes the same schema.

#### Execution model

- Caption parsing is pure Python and cheap. It runs in-process; it does not need
  a worker pool.
- OCR and ASR run in a single shared process pool, created lazily only
  when an enabled operation needs it. Process isolation contains native engines
  that are not thread-safe and prevents an engine crash from taking down the
  build. Use a pool with a supported terminate-and-join operation on Python
  3.11–3.14; timing out a `Future` alone does not stop a hung native worker.
- Worker functions and their arguments must be safe for `spawn` and
  `forkserver`, not just `fork`. Engine and model objects are created inside
  the worker, never pickled across the boundary.
- Spool each enabled worker input from its confined stream into a bounded,
  private `0600` temp file; remove it after completion, cancellation, or
  failure. Enforce an aggregate temp-space budget across concurrent workers,
  check storage availability, and report failures. Clean up only recognized
  stale private spools after an uncatchable process exit. Never send an
  unbounded blob or a provider path that could be swapped after validation to a
  worker.
- The pool is shared for the whole build run, not created per source, so
  `build_workers` cannot multiply model memory.
- Engine-internal thread counts are set explicitly (normally 1) so process
  workers do not multiply CPU threads. Parallelism comes from the process pool,
  not from every worker starting an N-thread BLAS/OpenMP/whisper runtime.
- `workers = "auto"` is the default and is resource-derived, not
  `os.cpu_count()`:
  - `cpu_task_limit = max(1, usable_cores - headroom)`, leaving room for
    extraction, the embedding pipeline, concurrent source builds, and the OS;
  - `memory_worker_limit = usable_memory // per_worker_memory`, where
    `usable_memory` is currently available memory after reserves, bounded by
    cgroup headroom where detectable, not installed RAM, and `per_worker_memory`
    is the sum of the enabled engines' estimated resident sets plus interpreter
    overhead;
  - if that limit is positive, `workers = min(cpu_task_limit, memory_worker_limit)`.
- There is no low fixed cap such as 4. A large host may use more workers when
  memory allows; a 512 MB–1 GB host such as a Pi Zero 2 W resolves to 1 when
  one worker fits. If the memory limit is zero, report the operation unavailable
  instead of starting it and risking OOM.
  An explicit positive integer overrides auto; warn if it exceeds the estimated
  memory budget but do not silently clamp it. Report the resolved value and the
  binding limit (`cpu` or `memory`) in the build report.
- Give each engine task a wall-clock budget. On a crash or timeout, terminate
  and join the affected pool, report the input, and recreate it; requeue other
  interrupted work only within a bounded retry limit. Continue building other
  sources and never loop indefinitely on a repeatable failure.

### Index locator

The shared locator module introduced in phase 01 uses one layout for every local
indexed provider:

```
<index_dir>/<provider>/<collection-slug>-<collection-hash>/<source-slug>-<source-hash>/
    current
    generations/<build-generation>/index.sqlite
    generations/<build-generation>/index.faiss
    generations/<build-generation>/lease
```

There is no ZIM-specific runtime index path or dual-format reader. During the
one-time rollout only, the importer may locate the old `<stem>.sqlite` /
`<stem>.faiss` pair for a ZIM already present in the catalog, validate it, and
adopt it into the new layout. It must not search arbitrary filenames or trust
unknown files. Unrecognized, corrupt, stale, or incompatible artifacts are
rebuilt; a successfully adopted index is thereafter read only through the
canonical layout.

Store each source's artifacts as immutable generations. Build and validate a
complete SQLite/FAISS pair in a new generation directory, fsync its files, then
atomically replace a small `current` manifest naming that generation and its
digests. Readers resolve that manifest once and pin the generation, so they
cannot combine files from different builds. Keep the prior generation until
all local and cross-process readers release it, then remove it; if release
cannot be established, warn and defer cleanup rather than deleting a live
generation. A successful replacement must not leave the old and new copies
indefinitely.

The phase-01 locator must already implement this lifecycle:

- The publisher creates the generation's `lease` file before publication.
  Readers open that existing file read-only and hold an OS-backed shared lock
  for the lifetime of every SQLite/FAISS handle and in-flight read. A POSIX
  shared `flock` on a read-only descriptor is the baseline; another platform
  must demonstrate equivalent cross-process behavior. PID files and in-memory
  reference counts alone are not reader leases.
- `paths(source)` returns an owned `GenerationLease` containing the generation,
  validated artifact paths/digests, and an idempotent `close()`, not bare paths
  whose pin can disappear. Acquire the shared lock and recheck `current` before
  opening artifacts; if publication/cleanup won the race, release and retry.
  Transfer that ownership to the phase-01 `_LocalIndex`, then `IndexHandle`
  after generalization, until its last reader closes.
- Cleanup takes a nonblocking exclusive lock on the same lease and rechecks
  that the generation is not current before deleting only its recognized
  artifacts. Unsupported locking defers cleanup and fails read-only reuse
  closed where pinning cannot be guaranteed; never manufacture a lease from
  an `open` process or unlink a lease while it has active readers.
- Retry deferred cleanup during normal build/reload/maintenance and reader
  release, with bounded background retries while a writable server remains
  active. A read-only `open` process releases leases but never performs cleanup.
- Before publication, checkpoint and close SQLite, remove its dependency on
  WAL/SHM/journal files, and seal the generation against subsequent mutation.
  Read it with a sidecar-free immutable connection only after those invariants
  are validated. `immutable=1` is not a shortcut for opening a live database.
  Fsync completed artifacts and the manifest's parent directory after replacement.

The versioned `current` manifest contains provider/collection/source identity,
a validated generation UUID, and SQLite plus optional FAISS digests. Its
artifact names are fixed, not arbitrary supplied paths. Reject malformed or
escaping components and symlinks before opening them. Schema/identity checks
apply to lexical-only reads too; a semantic-channel failure need not disable a
separately validated SQLite channel.

The locator must:

- keep filenames filesystem-safe and collision-free;
- derive provider index paths from stable logical identity, not the source's
  absolute path. The configured `index_dir` itself may be absolute; source
  roots must not appear in generated filenames, source IDs, or public metadata;
- make every generated component a bounded filesystem-safe slug plus a
  collision-resistant digest; never use raw collection names or source paths
  as path components;
- derive the collection digest from `collection_key` and the source digest from
  `key`;
- keep `source_fingerprint` out of the locator; content changes reuse the same
  path and are handled by the fingerprint/resume rules;
- expose one `paths(source)` function that resolves the current manifest once
  per acquisition attempt and returns its owned generation lease;
- be shared by `serve`, `build`, `reload`, cleanup, and health.

### Index metadata encoding

New indexes use a type-stable metadata table:

```sql
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
```

`meta.value` is always UTF-8 text. New ordinary tables use SQLite `STRICT`
under phase 01's SQLite 3.37 minimum; this enforces storage classes,
not faster queries. FTS5 virtual tables cannot be declared `STRICT`. The
one-time importer may convert recognized legacy metadata values through the
canonical codec below; it must reject unknown or malformed values rather than
silently coerce them. Ordinary `vecs` staging tables use
`CREATE TABLE vecs(id INTEGER PRIMARY KEY, v BLOB NOT NULL) STRICT`.

| Key(s) | Meaning | Canonical encoding |
|---|---|---|
| `done` | publication mode | enum `"fast"` or `"full"` |
| `next`, `filesize`, `build_filesize`, `indexed_count` | non-negative counts/offsets | decimal ASCII integer |
| `schema_version` | recognized SQLite schema revision | positive decimal ASCII integer |
| `truncated` | bounded index coverage | `"0"` or `"1"` |
| `build_mode` | in-progress mode | enum `"fast"` or `"full"` |
| `provider`, `collection_key`, `source_key`, `provider_id`, `source_fingerprint`, `model_fingerprint` | identity/fingerprint | non-empty UTF-8 string; fingerprints are lower-case hex; absent `provider_id` when unknown |
| `indexed_channels` | channels actually populated by this build/import | canonical UTF-8 JSON array drawn from `"title"`, `"body"`, `"semantic"` |
| `model_probe_texts` | probe inputs | canonical UTF-8 JSON array |
| `model_probe_shape` | probe matrix shape | canonical UTF-8 JSON two-integer array |
| `model_probe_vectors` | probe matrix bytes | base64 of the documented little-endian float16 bytes |
| `faiss_digest` | published FAISS bytes | lower-case SHA-256 hex |
| `build_generation` | SQLite/FAISS publication generation | lower-case UUID string |
| `derived_text` | operation availability/provenance | canonical UTF-8 JSON object |

Absent keys mean not known/not applicable; no key stores SQL `NULL`. Boolean,
enum, integer, JSON, fingerprint, and binary-probe values must not use another
representation in a new index. `done = "full"` is the only completed full
build marker, and `indexed_count`/`truncated` describe the published rows,
not the provider's source totals.

### Generic build

`pipeline.py` keeps the current semantics:

- staging DB/FAISS and upgrade; atomically publish a complete generation by
  swapping its manifest and verify the SQLite/FAISS pair as described below;
- progress, batching, embedding pipeline, fast mode;
- model probes and fingerprint compatibility;
- `meta.done`, `meta.next`, and crash resumability;
- build locking remains global in `zimantic/locking.py`.

Runtime fast mode is separate from fast build mode. When `serve --fast` or
`open --fast` is active, the process must not load a model or FAISS and must
disable semantic search for its entire lifetime, even when a finished semantic
index is available. SQLite lexical/title/body search and native full-text
search remain usable.

Every generation has the same table schema, including an empty `body` table
after a fast build; `indexed_channels` records built channels, including valid
empty ones. A completed import may retain lexical channels while semantic search is
unavailable, and must report that fact rather than imply `done = "full"`
guarantees usable vectors.

Even a forced rebuild stages its replacement without deleting the last
published index first. Before starting, report current artifact bytes and
available staging space; if the known minimum cannot fit, fail before modifying
the published generation. A fatal traversal, extraction, validation, or build
failure removes only its incomplete staging generation, logs a warning naming
the source and failure, and leaves the prior successful generation published.
Continue using that generation only if its source identity and required
channels still validate. If the source or build inputs changed, keep the old
generation for recovery but mark it stale and do not present its results as
current (notably, do not hydrate old rowids against changed content). Requested
derived-text failures instead publish the available channels as partial, as
specified above, with the partial state visible and a non-zero build result.
An interrupted but resumable build may retain at most one recognized staging
generation per source/build mode. Validate it before reuse; remove abandoned,
invalid, or superseded staging on the next build/prune. A deliberately retained
checkpoint is not a published generation or permission to accumulate retries.

Only the entry source changes:

```python
def build_index(producer, locator, embedder, cfg, fast=False, force=False): ...
```

The ZIM provider calls it with `ArchiveEntryProducer`; files and Kolibri call
the same function with their producers. Phase 04 changes the index SQLite
schema. Migrate recognized pre-redesign `<stem>.sqlite`/`.faiss` pairs and
phase-01 generation databases where possible rather than requiring a full
article re-extraction; the locator identity remains stable, and migrated
artifacts are published as a new generation under the canonical locator.
The per-source item cap applies to ZIM too, so very large ZIMs can have
truncated semantic/title coverage after rebuilding; native full-text may still
cover the archive. Report `truncated` and `indexed_count` and call out this
change in the migration notes.

Record a digest of the completed FAISS file and a unique build generation in
staged SQLite metadata and the `current` manifest. Before enabling semantic
search, verify the same opened bytes that FAISS loads, not a pathname that can
be replaced between verification and loading. Pin each index handle to that
generation; verify newly opened per-thread SQLite connections match it, or
reopen/retry against the current generation. If publication is interrupted or
a reader observes mismatched generations, retain safe lexical/native behavior,
report the mismatch, and never pair vectors with the wrong document rows. Fast
indexes have no FAISS digest and must not load a leftover FAISS file. Cache
verification on the loaded handle so searches do not repeatedly hash vectors;
verify again when reopening a pair rather than trusting file signatures alone.

### Recognized index migration

The schema differences are migration work, not a reason to discard every
successful ZIM index. Current and previously written databases in the field
include `docs` FTS5 layouts both with and without an unindexed `excerpt`
column, while legacy `meta` values can be SQLite INTEGER, TEXT, or BLOB; the
new schema adds provider/source identity, fingerprints, typed publication
metadata, and contentless body FTS. Some completed indexes have no `vecs` table
because vectors live in the separate FAISS file. Migration must inspect the
actual table/column definitions and storage classes rather than infer a format
from a version integer alone.

The importer may preserve the canonical non-negative rowids (including ZIM
rowid zero), doc paths/titles/redirect targets, and compatible lexical index
without reopening every article. Require both a recognized schema and
build-time source provenance. A missing fingerprint is acceptable only when
the index's recognized metadata/provenance records a non-empty archive UUID
and size that match the selected live archive; the catalog resolves the path,
but a UUID obtained only by rebuilding/rescanning that catalog is not evidence
of what archive produced the old index. The current pre-redesign builder
records size but not archive UUID, so those artifacts require a clean rebuild
unless independent recognized build-time provenance exists. Never invent
provenance from a matching filename, size, or the new catalog.
If provenance cannot be established, leave the old artifacts untouched and
report why adoption was refused. Phase-01 generations carry the required
identity evidence. The importer can rebuild the `docs` FTS table from recognized
stored columns and normalize known metadata values in a staged copy.
Preserve semantic search only when FAISS IDs, dimensions, and model identity
are verified against the migrated rowids and current model; otherwise retain
the compatible lexical index and rebuild or disable only the semantic channel.
If source identity/freshness cannot be validated, if rowids/targets collide, or
if any required field is irrecoverable, reject adoption and perform a clean
build while keeping the old artifact untouched. Do not create a permanent
legacy search path.

The new `body` FTS index is contentless by default. A recognized stored excerpt
may be tokenized into its postings during migration, but the raw excerpt is
not retained in the new index. When no excerpt exists, body search can be
added by a normal producer build; it must not force discarding compatible
title/redirect data. Record which channels were migrated, rebuilt, unavailable,
or stale in the report. Measure migration time, peak extra disk/RSS, rows
reused, and fallback channel status on representative existing indexes.

### Excerpt and derived-text storage experiment

Do not add an excerpt or raw derived-text column to the new `docs` table by
default. A preliminary local sizing test used the supplied 22-minute video's
547-cue English SRT plus a 45-second Whisper-tiny ASR preview:

- the subtitle file was 36,049 bytes, normalized caption text was 16,749 bytes,
  and gzip-compressed normalized text was 8,052 bytes;
- the 45-second ASR preview produced 281 bytes of text. The tiny model output
  was used for sizing only, not as a quality benchmark;
- on a synthetic 1,000-row corpus, stored-content body FTS5 was 811,008 bytes
  versus 372,736 bytes for contentless FTS5 (54% smaller), with effectively
  identical term-query time (~0.0118 ms versus ~0.0117 ms per query).

Use contentless body FTS5 as the initial default. It cannot provide snippets or
stored text, so preview hydration must use `ContentStore` for native content
and a private digest-keyed derived-text cache for derived contributions. Keep a
row-to-cache-reference mapping in a `STRICT` side table, not one enormous
metadata value or a raw derived-text column. References identify the canonical
row id, operation, contribution order, and cache key.
Cache files are atomically written, validated private artifacts, never paths
supplied by content. With `cache = false`, do not retain/reuse raw derived
outputs after the build or publish dangling cache references. A cache miss or
explicit prune may remove a derived preview, but never a searchable posting or
result: return bounded native text or an empty lead, without invoking an engine.
Use contentless FTS phrase queries for indexed body phrase evidence; do not
require unavailable raw derived text to verify a hit. For media, use the
provider's bounded native-metadata preview hook rather than reading or decoding
the media body. Revisit this storage choice only if the release benchmark shows
that preview latency or cache misses are unacceptable.

The strong alternative is stored-content body FTS5: it costs substantially more
space but supports direct snippets/highlighting and avoids a separate native
text hydration path. Choose it only if those latency/UX benefits survive the
same release benchmark.

The preliminary result does not replace a representative benchmark. Compare on
identical representative ZIM, Files, and Kolibri fixtures:

- body FTS5 with stored content versus contentless FTS5 postings plus
  `ContentStore` hydration for result previews;
- ordinary preview extraction on result selection versus retaining compact
  per-document preview text;
- derived-text searchable postings with native-only/empty preview fallback
  versus retaining build-produced output in the digest-keyed result cache.

For the selected contentless baseline and the stored-content alternative,
record SQLite/FAISS/cache bytes, peak additional disk during rebuild, build
time, peak RSS, query p50/p95, first-result latency, preview hydration
latency, and derived-engine invocations. Use a fixed query/result set, warm
and cold cache runs, and include long/large excerpts. No query-time caption
parsing, OCR, or ASR is permitted, including on a preview cache miss. Compare
storage/preview alternatives using already-produced output only. The
stored-content alternative is adopted only if it demonstrates a material
preview/UX benefit at the phase-09 gates; alternatives remain benchmark-only
until a documented schema decision, never dual runtime readers.

### Generic local search

Generalize `_search_local_locked` and its helpers, already moved from `Search`
to the ZIM runtime in phase 01, into `indexing/search.py`:

- Inputs: `IndexHandle`, `SourceInfo`, `SourceQuery`, config limits, and a
  preview hook.
- Channels: semantic (FAISS), keyword/title (FTS5 `docs`), body/excerpt
  lexical (FTS5 where a provider supplies body text), native full-text
  (optional), and the documented fusion/limits.
- Merge body lexical and native hits into the existing `fulltext` result
  channel by reciprocal ranks, deduplicating by document ID and breaking ties
  deterministically; their native and BM25 scores are not comparable. Do not
  introduce a fifth `SourceResult` channel. A provider with no native searcher
  is not partial solely because it lacks one.
- Build local result `id` values from `source_key` plus the canonical row id,
  not from the content path; duplicate paths must remain distinct results.
- Key preview/phrase/MIME caches by document identity plus source/index
  generation, never by `(source_key, path)` alone. Pass canonical row ids to
  provider preview hooks so duplicate paths cannot alias different documents.
- One `docs` FTS5 schema and rowid model for every local indexed provider. Add
  a contentless `body` FTS5 table keyed by the same rowids and populated from
  `EntryRecord.excerpt`; retain only derived-text cache references needed for
  preview hydration. There are no legacy index variants or schema fallbacks.
- A normal Files/Kolibri build makes indexed body text searchable through
  `body`; a fast build intentionally remains title/path-only, with neither
  body postings nor embeddings or derived-text processing, as specified in phase 08.
  Report that configured enrichment was not attempted in a fast build.
- Preview hydration calls `ContentStore.read`, not libzim directly.
- Redirect resolution still follows `docs.target` chains for all providers.
- Providers without a native index return no native hits; they still get
  semantic and title/body keyword results when those channels are indexed.
- Providers that are not indexed still use their native searcher if one
  exists (only ZIM today).

### `IndexHandle`

Replace `_LocalIndex` with a provider-neutral handle:

```python
@dataclass
class IndexHandle:
    db_path: Path | None
    faiss_path: Path | None
    store: ContentStore | None
    native: NativeSearcher | None
    signatures: tuple[int, int, int, int] | None
    generation: GenerationLease | None
    locks: IndexLocks
    read_only: bool = False

# signatures = (db_size, db_mtime_seconds, faiss_size, faiss_mtime_seconds); a missing
# artifact uses the corresponding zero pair. IndexLocks contains the
# lifecycle/ref-count lock, the SQLite read lock, and the close lock.
```

ZIM-specific lazy archive opening moves into `ArchiveContentStore`; the handle
keeps lifecycle reference counting, per-thread SQLite connections, and
search/read locks. The runtime owns the content store; an `IndexHandle` only
borrows it and must never close it independently.

Published generations always use sidecar-free read-only SQLite connections
after validating that the generation is sealed. `read_only=True` additionally
forbids repair, lease creation, cleanup, and cache writes by the handle's
read/reuse path; opening a FAISS artifact must not write beside it. If these
guarantees or a pre-existing reader lease are unavailable for an artifact or
platform, the handle is unavailable rather than falling back to writable
access. Phase 10 uses this mode for reused indexes.

### Native full-text isolation

- `NativeSearcher` is a protocol; the ZIM implementation wraps libzim.
- `fulltext.py` and `fulltext_worker.py` continue to isolate ZIM native
  searches in helper processes.
- Non-ZIM providers return no native searcher. Do not add process isolation
  for SQLite FTS, which is already in-process and bounded.
- Preserve the existing partial/error statuses when isolation fails.

## Work items

- [ ] Add `zimantic/indexing/types.py`; move the canonical `EntryRecord` there
      and define `EntryProducer`/`NativeSearcher`. The module must import
      without libzim, FAISS, or web dependencies.
- [ ] Move the schema and meta-key constants from `build.py` into
      `indexing/schema.py`; use `STRICT` ordinary tables where the minimum
      SQLite supports them, and add the default contentless `body` FTS5 table,
      derived-text cache references, identity metadata, and canonical codec.
- [ ] Add a one-time, allowlisted importer for recognized old ZIM index
      variants. Preserve rowids and compatible FTS data; migrate metadata and
      semantic channels only when each can be validated. Reject unknown
      variants without modifying their files.
- [ ] Split `build.py`: replace the ZIM-only `build()` with
      `build_index(producer, ...)`. Do not keep a compatibility wrapper.
- [ ] Move `_index_entries`, `_save`, `_finish_build`, `_write_faiss`, and
      staging/upgrade helpers into `indexing/pipeline.py` with no libzim
      imports.
- [ ] Reuse the phase-01 `indexing/locator.py` for every local indexed provider.
- [ ] Create `providers/zim/entries.py` with `ArchiveEntryProducer`, wrapping
      `read_entry`, applying the canonical row-id/`target_id` mapping, and
      preserving progress and fast mode.
- [ ] Split `content/reader.py` into pure extractors and a ZIM adapter only if
      it reduces coupling; keep `read_entry` behavior and tests.
- [ ] Move the ZIM runtime's `_search_local_locked` into `indexing/search.py`;
      make it take an `IndexHandle` + `ContentStore`.
- [ ] Generalize the runtime's `_fetch_docs`, `suggest`, and preview helpers in
      `indexing/search.py`; `suggest` must use `IndexHandle`, not
      `Search.indexes`.
- [ ] Benchmark the selected contentless body-FTS baseline against stored
      content on fixed fixtures; preserve title/path-only `--fast` behavior
      and retain stored content only if it wins the preview/UX gate.
- [ ] Extend the phase-01 store-based phrase/MIME enrichment with generic body
      FTS evidence and derived-cache preview hooks, retaining byte limits and
      error handling; it must not open a ZIM archive directly.
- [ ] Replace `_LocalIndex` with `IndexHandle`; move lazy archive opening to
      `ArchiveContentStore`.
- [ ] Add `NativeSearcher`; wrap libzim and keep isolation.
- [ ] Keep `Search.indexes`/`Search.catalog` absent after their phase-01 removal;
      generalize provider-owned handles without reintroducing core aliases.
- [ ] Add the shared derived-text stage with independent `captions`, `ocr`, and
      `asr` opt-ins, optional engine extras, a digest cache, provenance, and
      fingerprint inputs.
- [ ] Add the lazy shared process pool for OCR/ASR with spawn-safe workers,
      auto worker resolution, bounded temp-file spooling, and crash recovery.
- [ ] Validate the SQLite/FAISS digest on open, including interrupted
      publication and per-thread connections opened during a rebuild; do not
      serve mismatched vectors and document rows.
- [ ] Add a synthetic in-memory producer in tests to prove the pipeline has no
      libzim dependency.
- [ ] Add `BuildableRuntime.entry_producer` and producer cleanup; exercise the
      same CLI orchestration with ZIM and a synthetic buildable runtime, and
      reject/skip a search-only runtime without provider-specific branches.

## Tests

- [ ] ZIM build produces the generic `docs`/contentless-`body` schema,
      `meta.done`/`next`, FAISS ids, derived-text cache references, and search
      results through `build_index`.
- [ ] A ZIM exceeding the item cap reports truncated index coverage while its
      available native full-text and content serving continue to work.
- [ ] Resume after interruption works; changing the fingerprint restarts the
      build safely.
- [ ] A detectable manifest/signature change during a build prevents
      publication; a later detectable change marks the index stale on reload.
- [ ] Interrupted publication or concurrent opening cannot pair a SQLite index
      with a different FAISS generation.
- [ ] A failed forced rebuild warns with the cause, leaves the previous
      successful generation in place, and continues using it only if its
      source and required channels still validate.
- [ ] Successful publication atomically switches a complete SQLite/FAISS
      generation; after readers release old generations, cleanup removes them.
- [ ] Insufficient staging space is reported before publication is touched;
      failed staging files are cleaned without accumulating duplicate copies.
- [ ] Recognized old index schemas migrate without rowid changes; unknown
      schemas remain untouched and fall back to a clean staged build.
- [ ] A recognized size-only legacy index cannot acquire provenance from a
      freshly rebuilt catalog; an unrelated same-size archive is never adopted.
- [ ] A separate reader process, including read-only `open`, pins an old
      generation across publication; cleanup waits for release/process exit
      and never creates/modifies a reader's lease or SQLite sidecars.
- [ ] Fast build, force rebuild, upgrade-in-place, and a corrupt staging file
      all work.
- [ ] Model fingerprint/probe mismatch falls back to lexical search.
- [ ] Index locator: all local indexed providers use the same layout; paths are safe, unique,
      and bounded; independent of source-root relocation; no source absolute
      path in generated filenames or public metadata.
- [ ] Generic local search over a synthetic producer returns semantic,
      title/body keyword, and preview results without importing libzim; native
      previews and derived-text cache references hydrate correctly.
- [ ] Distinct canonical rows sharing a path cannot overwrite one another's
      preview/phrase/MIME cache entries.
- [ ] The storage benchmark records the contentless baseline and stored-content
      alternative, including storage, build, RSS, query, hydration, and
      engine-invocation measurements.
- [ ] A non-ZIM body-term query returns a lexical result without embeddings or
      a `NativeSearcher`; a fast build is explicitly title/path-only.
- [ ] Derived text is off by default: caption/image/media inputs are never
      read. Each operation works alone with a fake engine, records provenance,
      respects its caps, and reuses the digest cache across rebuilds.
- [ ] Native text remains first; distinct derived contributions are appended
      captions/OCR/ASR in order, compared by normalized Unicode text, and
      duplicate contributions are appended only once without altering their
      original text.
- [ ] Cache-disabled and cache-pruned derived hits remain searchable, including
      body phrase queries; missing previews never run an engine or drop a hit.
- [ ] A partial enrichment build is retried on the next normal build even with
      the same fingerprint; a complete build still skips unchanged work.
- [ ] Changing an operation, engine, or model changes the fingerprint and
      restarts the build.
- [ ] ASR works alone even when a caption file exists; with captions enabled,
      usable caption text suppresses ASR. Cache entries vary with language and
      caps, and unavailable engines are retried when they become available.
- [ ] `workers = "auto"` resolves to 1 on a low-memory host, scales above 4 on
      a large host when memory allows, reports unavailable when none fits, and
      reports whether CPU or memory was binding; an explicit integer overrides
      it. The OCR/ASR pool is lazy,
      shared across sources, and not created when those operations are off.
- [ ] A fake engine that crashes the worker does not prevent other sources from
      building; report partial enrichment and exit non-zero. A hung worker is
      terminated on timeout on Python 3.11 as well as newer versions.
- [ ] An absent optional native searcher is not an error; failure of an expected
      isolated native search retains the documented partial/error status.
- [ ] Concurrent builds and `build_workers` still work under the global lock.

## Acceptance criteria

- `python -m pytest` passes.
- Every local indexed provider uses the same index layout and schema; Kiwix
  remains search-only. Recognized ZIM indexes are migrated once where
  validation permits; unknown or incompatible ones are rebuilt.
- `build.py` no longer imports provider-specific discovery.
- `indexing/` imports no libzim, FAISS, or web modules at module scope except
  inside the FAISS loaders.
- Files and Kolibri use `EntryProducer` + `ContentStore` for FTS, semantic
  search, previews, resume, and publication, alongside their provider runtimes.

## Risks and mitigations

- **Index migration.** Legacy FTS layouts, mixed-type metadata, and absent
  fingerprints require a versioned importer with channel-level validation.
  Unknown data must be left intact while a replacement is staged; never guess
  FAISS/model compatibility or expose a permanent legacy reader.
- **Staging disk use.** A safe rebuild temporarily needs space for old and new
  generations. Estimate and report that headroom, preserve the old generation
  on failure, and reclaim it after successful publication and reader release.
- **Resume correctness.** Fingerprint checking is mandatory; a mismatch must
  rebuild rather than produce shifted row ids.
- **Large mechanical move.** Move code before changing behavior; use contract
  tests and avoid rewriting ranking or limits.
- **FAISS memory on small hosts.** Keep the batch/queue behavior; the generic
  pipeline must not add per-provider caches.
- **Derived-text cost on small hosts.** Engines are opt-in, build-time-only, and
  capped; `workers = "auto"` resolves to 1 on low-memory hosts; `serve` never
  loads them.
