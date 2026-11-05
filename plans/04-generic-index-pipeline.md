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
- `zimantic/content/reader.py` — `EntryRecord` and `read_entry`, plus pure HTML,
  plain-text, and PDF extraction. `read_entry` is ZIM-entry-shaped.
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
    types.py          # EntryRecord re-export, EntryProducer protocol
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
```

- `EntryRecord.id` is the canonical positive 63-bit SQLite row id written to
  `docs.rowid`, `vecs.id`, FAISS, and `EntryRecord.target_id`. It is not an
  arbitrary provider-native id. Providers must keep it stable for the duration
  of a source snapshot and across unchanged rebuilds; path is not an identity
  because duplicate paths are permitted.
- Providers own the deterministic mapping from their native identity to this
  row-id space. The ZIM producer maps an archive entry's raw `_index` to
  `_index + 1` (so raw id `0` is valid and sparse ids remain distinct), and
  maps redirect targets with the same rule. Files use one-based manifest
  position; Kolibri uses its deterministic positive 63-bit `(channel_id,
  node_id)` mapping. A provider must reject overflow or mapping collisions
  rather than merge rows. The core never remaps ids after receiving an
  `EntryRecord`.
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
  UUID/size/mtime, a files manifest, and a consistent Kolibri DB snapshot plus
  selected storage-file signatures as specified in the provider plans.
- Size/mtime signatures are a practical change detector, not a proof of
  identical bytes: same-size writes with restored timestamps can evade them.
  Document that limitation and require immutable content or an external
  snapshot for strong consistency. Do not read disabled media solely to hash
  it. Builds and reloads must report detected changes rather than claiming
  arbitrary concurrent writes are impossible.
- The producer owns traversal order. The core never walks a filesystem, opens a
  ZIM, or queries Kolibri.

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
- No network. Models must be provisioned locally before the build.
- Results are cached in a private directory by content digest, operation,
  engine/model and version, and every output-affecting option (including
  language, parser version, and caps). Partial/failed outputs are not cached as
  complete. Cache entries are not source identity and may be removed by
  `zimantic prune`.
- Provenance: append derived text to `EntryRecord.excerpt` in a deterministic
  order and record the contributing operations in index metadata and the build
  report. Optionally expose the per-result contributors as an optional field.
- Source/health output reports the last build's per-operation state:
  `off`, `not_checked`, `ready`, `partial`, or `unavailable`. A changed config
  or a fast build is `not_checked` until a matching full build runs. `serve`
  reads this metadata; it never loads an engine to probe its state.
- Budgets come from the global `[derived_text]` config: caption bytes, OCR
  pages/pixels/output bytes, ASR media seconds/input/output bytes, task time,
  aggregate spool bytes, and `workers`. The global `max_entry_bytes` also
  applies to each input. An input skipped or truncated because of a cap,
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
<index_dir>/<provider>/<collection-slug>-<collection-hash>/<source-slug>-<source-hash>.sqlite
<index_dir>/<provider>/<collection-slug>-<collection-hash>/<source-slug>-<source-hash>.faiss
```

There is no ZIM-specific index path, no discovery of old `<stem>.sqlite` /
`<stem>.faiss` pairs, and no `legacy_paths()` branch. Existing indexes are
ignored and must be rebuilt with the new layout.

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
- expose one `paths(source)` function;
- be shared by `serve`, `build`, `reload`, cleanup, and health.

### Index metadata encoding

New indexes use a type-stable metadata table:

```sql
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

`meta.value` is always UTF-8 text. The schema is a rebuild-only change; old
indexes with SQLite INTEGER or BLOB values are ignored rather than migrated.
Readers and writers use one codec and reject malformed values instead of
silently coercing them:

| Key(s) | Meaning | Canonical encoding |
|---|---|---|
| `done` | publication mode | enum `"fast"` or `"full"` |
| `next`, `filesize`, `build_filesize`, `indexed_count` | non-negative counts/offsets | decimal ASCII integer |
| `truncated` | bounded index coverage | `"0"` or `"1"` |
| `build_mode` | in-progress mode | enum `"fast"` or `"full"` |
| `provider`, `source_key`, `source_fingerprint`, `model_fingerprint` | identity/fingerprint | non-empty UTF-8 string; fingerprints are lower-case hex |
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

- staging DB/FAISS and upgrade; publish each artifact atomically and verify the
  SQLite/FAISS pair as described below;
- progress, batching, embedding pipeline, fast mode;
- model probes and fingerprint compatibility;
- `meta.done`, `meta.next`, and crash resumability;
- build locking remains global in `zimantic/locking.py`.

Even a forced rebuild stages its replacement without deleting the last
published index first. A fatal traversal, extraction, or build failure leaves
that artifact in place; a detectably stale artifact is not searched. Requested
derived-text failures instead publish the available channels as partial, as
specified above.

Only the entry source changes:

```python
def build_index(producer, locator, embedder, cfg, fast=False, force=False): ...
```

The ZIM provider calls it with `ArchiveEntryProducer`; files and Kolibri call
the same function with their producers. Phase 04 changes the index schema and
requires rebuilding phase-01 indexes; the locator itself does not change.
The per-source item cap applies to ZIM too, so very large ZIMs can have
truncated semantic/title coverage after rebuilding; native full-text may still
cover the archive. Report `truncated` and `indexed_count` and call out this
change in the migration notes.

Two file renames are not an atomic pair. Record a digest of the completed FAISS
file and a unique build generation in staged SQLite metadata. Before enabling
semantic search, verify the same opened bytes that FAISS loads, not a pathname
that can be replaced between verification and loading. Pin each index handle to
that generation; verify newly opened per-thread SQLite connections match it, or
reopen/retry against the current generation. If publication is interrupted or a
reader observes mismatched generations, retain safe lexical/native behavior and
report the mismatch; never pair vectors with the wrong document rows. Fast
indexes have no FAISS digest and must not load a leftover FAISS file. Cache
verification on the loaded handle so searches do not repeatedly hash vectors;
verify again when reopening a pair rather than trusting file signatures alone.

### Generic local search

Move `_search_local_locked` and its helpers out of `Search` into
`indexing/search.py`:

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
- One `docs` FTS5 schema and rowid model for every local indexed provider. Add an optional
  `body` FTS5 table keyed by the same rowids, populated from
  `EntryRecord.excerpt`. There are no legacy index variants or schema
  fallbacks.
- A normal Files/Kolibri build makes indexed body text searchable through
  `body`; `--fast` intentionally remains title/path-only, with neither body
  FTS nor embeddings or derived-text processing, as specified in phase 08.
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
    locks: IndexLocks
    read_only: bool = False

# signatures = (db_size, db_mtime_ns, faiss_size, faiss_mtime_ns); a missing
# artifact uses the corresponding zero pair. IndexLocks contains the
# lifecycle/ref-count lock, the SQLite read lock, and the close lock.
```

ZIM-specific lazy archive opening moves into `ArchiveContentStore`; the handle
keeps lifecycle reference counting, per-thread SQLite connections, and
search/read locks. The runtime owns the content store; an `IndexHandle` only
borrows it and must never close it independently.

When `read_only` is true, every SQLite connection uses a tested read-only URI
mode that cannot create journal/WAL/SHM sidecars, and opening a FAISS artifact
must not write beside it. If that guarantee is unavailable for an artifact or
platform, the handle is unavailable rather than falling back to a writable
connection. Phase 10 uses this mode for reused indexes.

### Native full-text isolation

- `NativeSearcher` is a protocol; the ZIM implementation wraps libzim.
- `fulltext.py` and `fulltext_worker.py` continue to isolate ZIM native
  searches in helper processes.
- Non-ZIM providers return no native searcher. Do not add process isolation
  for SQLite FTS, which is already in-process and bounded.
- Preserve the existing partial/error statuses when isolation fails.

## Work items

- [ ] Add `zimantic/indexing/types.py`; re-export `EntryRecord` and define
      `EntryProducer`/`NativeSearcher`.
- [ ] Move the schema and meta-key constants from `build.py` into
      `indexing/schema.py`; add the separate optional `body` FTS5 table for
      provider excerpts, plus `provider`, `source_key`, and
      `source_fingerprint` meta keys. Implement the canonical text codec and
      metadata table above; old mixed-type metadata is rebuild-only.
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
- [ ] Move `_search_local_locked` into `indexing/search.py`; make it take an
      `IndexHandle` + `ContentStore`.
- [ ] Move `_fetch_docs`, `suggest`, and non-ZIM preview logic into
      `indexing/search.py`; `suggest` must use `IndexHandle`, not
      `Search.indexes`.
- [ ] Persist provider excerpts in the `body` FTS5 table and query it as a
      lexical channel; preserve title/path-only `--fast` behavior.
- [ ] Move `_enrich_pool` exact-phrase verification and MIME population behind
      `ContentStore` reads, retaining byte limits and error handling; it must
      not open a ZIM archive directly.
- [ ] Replace `_LocalIndex` with `IndexHandle`; move lazy archive opening to
      `ArchiveContentStore`.
- [ ] Add `NativeSearcher`; wrap libzim and keep isolation.
- [ ] Delete `Search.indexes` and `Search.catalog` in the same change; do not
      leave aliases.
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

## Tests

- [ ] ZIM build produces the generic `docs`/`body` schema, `meta.done`/`next`,
      FAISS ids, and search results through `build_index`.
- [ ] A ZIM exceeding the item cap reports truncated index coverage while its
      available native full-text and content serving continue to work.
- [ ] Resume after interruption works; changing the fingerprint restarts the
      build safely.
- [ ] A detectable manifest/signature change during a build prevents
      publication; a later detectable change marks the index stale on reload.
- [ ] Interrupted publication or concurrent opening cannot pair a SQLite index
      with a different FAISS generation.
- [ ] A failed forced rebuild leaves the previous published index intact.
- [ ] Fast build, force rebuild, upgrade-in-place, and a corrupt staging file
      all work.
- [ ] Model fingerprint/probe mismatch falls back to lexical search.
- [ ] Index locator: all local indexed providers use the same layout; paths are safe, unique,
      and bounded; independent of source-root relocation; no source absolute
      path in generated filenames or public metadata.
- [ ] Generic local search over a synthetic producer returns semantic,
      title/body keyword, and preview results without importing libzim.
- [ ] A non-ZIM body-term query returns a lexical result without embeddings or
      a `NativeSearcher`; a fast build is explicitly title/path-only.
- [ ] Derived text is off by default: caption/image/media inputs are never
      read. Each operation works alone with a fake engine, records provenance,
      respects its caps, and reuses the digest cache across rebuilds.
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
  remains search-only. Old indexes are ignored and rebuilt.
- `build.py` no longer imports provider-specific discovery.
- `indexing/` imports no libzim, FAISS, or web modules at module scope except
  inside the FAISS loaders.
- Files and Kolibri use `EntryProducer` + `ContentStore` for FTS, semantic
  search, previews, resume, and publication, alongside their provider runtimes.

## Risks and mitigations

- **Index churn.** One schema means old indexes are discarded. Ship the rebuild
  path and document it; do not add a legacy reader.
- **Resume correctness.** Fingerprint checking is mandatory; a mismatch must
  rebuild rather than produce shifted row ids.
- **Large mechanical move.** Move code before changing behavior; use contract
  tests and avoid rewriting ranking or limits.
- **FAISS memory on small hosts.** Keep the batch/queue behavior; the generic
  pipeline must not add per-provider caches.
- **Derived-text cost on small hosts.** Engines are opt-in, build-time-only, and
  capped; `workers = "auto"` resolves to 1 on low-memory hosts; `serve` never
  loads them.
