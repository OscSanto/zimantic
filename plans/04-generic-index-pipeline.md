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
    fingerprint: str         # changes when the underlying content changes
    count: int
    def entry_at(self, index: int) -> EntryRecord | None: ...
```

- `EntryRecord.id` is the stable row id written to `docs.rowid`. Providers must
  keep it stable for the duration of a source snapshot and across unchanged
  rebuilds. The producer's `count` is the complete snapshot manifest length;
  non-indexable entries return `None` and are reported as skipped rather than
  changing row-id assignment.
- `entry_at` returns `None` for non-indexable items; the producer is
  responsible for harmless skips.
- `fingerprint` must be cheap and must cover everything that can change item
  identity/content: archive size+mtime for ZIM; a manifest hash for files;
  database mtime + node count for Kolibri; remote catalog version for future
  providers.
- The producer owns traversal order. The core never walks a filesystem, opens a
  ZIM, or queries Kolibri.

Resume rule: store `source_fingerprint` in `meta` when a build starts. On
resume, if it differs, discard staging and start over. The fingerprint includes
the enabled derived-text operations and their engine/model identifiers, so
toggling `captions`, `ocr`, or `asr` rebuilds the index. Verify the same
fingerprint again immediately before publication; if the source changed during
the build, discard or retry the build rather than publishing a mixed snapshot.
Never resume a `meta.next` against a changed item list.

### Derived text enrichment

Native text extraction stays in the existing readers. Derived text is a shared,
optional stage in `indexing/derived.py` so every provider gets the same policy,
caching, provenance, and failure handling.

```python
from pathlib import Path

class DerivedTextInput(Protocol):
    kind: Literal["captions", "image", "audio", "video"]
    mimetype: str
    digest: str            # content identity for the cache
    size: int
    path: Path | None      # provider-confined regular file, when available
    def open(self) -> BinaryIO: ...

class DerivedTextProducer(Protocol):
    def derived_text_inputs(self, index: int) -> Iterable[DerivedTextInput]: ...
```

- The producer decides which candidate files exist and yields bounded,
  provider-confined streams. The core decides whether to read them.
- The hook is provider-neutral: ZIM, files, and Kolibri can implement it, while
  a provider with no local content bytes (Kiwix) cannot. A provider without the
  hook is simply not enrichable. ZIM adoption may follow files/Kolibri rather
  than delaying this phase.
- Operations are independent and off by default:
  - `captions = "index"` parses existing subtitle/caption tracks; no model.
  - `ocr = "tesseract"` extracts text from images and image-only pages.
  - `asr = "whisper"` transcribes speech from audio/video.
- The core skips an input kind that the source has not enabled. Prefer native
  text, then captions, then OCR/ASR, so ASR is skipped when a usable caption
  track exists and OCR is skipped when a page already has a text layer.
- Extractors run only during build. Search, previews, and serving never decode
  media or run a model.
- OCR and ASR are optional dependency extras. A configured but missing engine or
  model marks only that operation unavailable for the source; it does not crash
  the server, fail the whole build, or silently substitute another operation.
- No network. Models must be provisioned locally before the build.
- Results are cached by `(operation, engine, model, engine_version, digest)` in
  a private cache directory and reused across rebuilds. Cache entries are not
  source identity and may be removed by `zimantic prune`.
- Provenance: append derived text to `EntryRecord.excerpt` in a deterministic
  order and record the contributing operations in index metadata and the build
  report. Optionally expose the per-result contributors as an optional field.
- Budgets come from the global `[derived_text]` config: caption bytes, OCR
  pages/pixels/output bytes, ASR media seconds/bytes, and `workers`. A skipped
  or truncated input is logged once and reported, never invented.

#### Derived-text configuration ownership

Phase 04 defines and validates the normalized global `[derived_text]` schema
consumed by this stage, including `max_caption_bytes`, OCR/ASR caps, engine
settings, cache behavior, and `workers`. Source entries only select
`captions`, `ocr`, or `asr`; they do not carry operation budgets. Phase 08
exposes this schema through the complete settings loader without redefining
its keys or precedence.

#### Execution model

- Caption parsing is pure Python and cheap. It runs in-process; it does not need
  a worker pool.
- OCR and ASR run in a single shared `ProcessPoolExecutor`, created lazily only
  when an enabled operation needs it. Process isolation contains native engines
  that are not thread-safe and prevents an engine crash from taking down the
  build.
- Worker functions and their arguments must be safe for `spawn` and
  `forkserver`, not just `fork`. Engine and model objects are created inside
  the worker, never pickled across the boundary.
- Prefer `DerivedTextInput.path` when it names a provider-confined regular
  file. When the content only exists inside an archive or database blob, spool
  a bounded copy to a private `0600` temp file and remove it after the task.
  Never send an unbounded byte blob or an arbitrary path to a worker.
- The pool is shared for the whole build run, not created per source, so
  `build_workers` cannot multiply model memory.
- Engine-internal thread counts are set explicitly (normally 1) so process
  workers do not multiply CPU threads. Parallelism comes from the process pool,
  not from every worker starting an N-thread BLAS/OpenMP/whisper runtime.
- `workers = "auto"` is the default and is resource-derived, not
  `os.cpu_count()`:
  - `cpu_task_limit = max(1, usable_cores - headroom)`, leaving room for
    extraction, the embedding pipeline, concurrent source builds, and the OS;
  - `memory_worker_limit = max(1, usable_memory // per_worker_memory)`, where
    `usable_memory` is currently available memory (MemAvailable plus cgroup
    headroom where detectable), not installed RAM, and `per_worker_memory` is
    the sum of the enabled engines' estimated resident sets plus interpreter
    overhead;
  - `workers = max(1, min(cpu_task_limit, memory_worker_limit))`.
- There is no low fixed cap such as 4. A large host may use more workers when
  memory allows; a 512 MB–1 GB host such as a Pi Zero 2 W still resolves to 1.
  An explicit positive integer overrides auto; warn if it exceeds the estimated
  memory budget but do not silently clamp it. Report the resolved value and the
  binding limit (`cpu` or `memory`) in the build report.
- An abrupt engine/worker crash marks that input as failed or skipped and
  continues the build. Recreate a broken executor rather than failing the
  source or leaving a half-written index.

### Index locator

Every provider uses one layout:

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
- A read-only consumer may reuse a finished index through the logical
  `source_key` stored in its metadata even when the consumer exposes a
  temporary source alias. The alias must not produce a second locator or
  change the indexed source identity.

### Generic build

`pipeline.py` keeps the current semantics:

- staging DB/FAISS, upgrade and atomic publish;
- progress, batching, embedding pipeline, fast mode;
- model probes and fingerprint compatibility;
- `meta.done`, `meta.next`, and crash resumability;
- build locking remains global in `zimantic/locking.py`.

Only the entry source changes:

```python
def build_index(producer, locator, embedder, cfg, fast=False, force=False): ...
```

The ZIM provider calls it with `ArchiveEntryProducer`; files and Kolibri call
the same function with their producers.

### Generic local search

Move `_search_local_locked` and its helpers out of `Search` into
`indexing/search.py`:

- Inputs: `IndexHandle`, `SourceInfo`, `SourceQuery`, config limits, and a
  preview hook.
- Channels: semantic (FAISS), keyword/title (FTS5 `docs`), body/excerpt
  lexical (FTS5 where a provider supplies body text), native full-text
  (optional), and the documented fusion/limits.
- One `docs` FTS5 schema and rowid model for every provider. Add an optional
  `body` FTS5 table keyed by the same rowids, populated from
  `EntryRecord.excerpt`. There are no legacy index variants or schema
  fallbacks.
- A normal Files/Kolibri build makes indexed body text searchable through
  `body`; `--fast` intentionally remains title/path-only, with neither body
  FTS nor embeddings, as specified in phase 08.
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

# signatures = (db_size, db_mtime_ns, faiss_size, faiss_mtime_ns); a missing
# artifact uses the corresponding zero pair. IndexLocks contains the
# lifecycle/ref-count lock, the SQLite read lock, and the close lock.
```

ZIM-specific lazy archive opening moves into `ArchiveContentStore`; the handle
keeps lifecycle reference counting, per-thread SQLite connections, and
search/read locks. The runtime owns the content store; an `IndexHandle` only
borrows it and must never close it independently.

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
      `source_fingerprint` meta keys.
- [ ] Split `build.py`: replace the ZIM-only `build()` with
      `build_index(producer, ...)`. Do not keep a compatibility wrapper.
- [ ] Move `_index_entries`, `_save`, `_finish_build`, `_write_faiss`, and
      staging/upgrade helpers into `indexing/pipeline.py` with no libzim
      imports.
- [ ] Add `indexing/locator.py` with one layout for every provider.
- [ ] Create `providers/zim/entries.py` with `ArchiveEntryProducer`, wrapping
      `read_entry` and preserving ids, progress, and fast mode.
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
- [ ] Add a synthetic in-memory producer in tests to prove the pipeline has no
      libzim dependency.

## Tests

- [ ] ZIM build produces the generic `docs`/`body` schema, `meta.done`/`next`,
      FAISS ids, and search results through `build_index`.
- [ ] Resume after interruption works; changing the fingerprint restarts the
      build safely.
- [ ] A source changing during a build is detected before publication and
      cannot produce a mixed or stale published index.
- [ ] Fast build, force rebuild, upgrade-in-place, and a corrupt staging file
      all work.
- [ ] Model fingerprint/probe mismatch falls back to lexical search.
- [ ] Index locator: all providers use the same layout; paths are safe, unique,
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
- [ ] `workers = "auto"` resolves to 1 on a low-memory host, scales above 4 on
      a large host when memory allows, and reports whether CPU or memory was
      binding; an explicit integer overrides it. The OCR/ASR pool is lazy,
      shared across sources, and not created when those operations are off.
- [ ] A fake engine that crashes the worker does not fail the build or another
      source; the task is reported and the executor recovers.
- [ ] Native fulltext absence yields the documented partial status.
- [ ] Concurrent builds and `build_workers` still work under the global lock.

## Acceptance criteria

- `python -m pytest` passes.
- Every provider uses the same index layout and schema; old indexes are ignored
  and rebuilt.
- `build.py` no longer imports provider-specific discovery.
- `indexing/` imports no libzim, FAISS, or web modules at module scope except
  inside the FAISS loaders.
- Files and Kolibri can implement only `EntryProducer` + `ContentStore` and
  get FTS, semantic search, previews, resume, and publication for free.

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
