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
resume, if it differs, discard staging and start over. Verify the same
fingerprint again immediately before publication; if the source changed during
the build, discard or retry the build rather than publishing a mixed snapshot.
Never resume a `meta.next` against a changed item list.

### Index locator

Keep existing ZIM files valid:

- ZIM: `<index_dir>/<archive stem>.sqlite` and `.faiss` — unchanged.
- New providers: `<index_dir>/<provider>/<collection-key>/<source-slug>-<hash>.sqlite`
  and `.faiss`.

The locator must:

- keep filenames filesystem-safe and collision-free;
- never embed an absolute path;
- expose `legacy_paths(source)` for ZIM and `paths(source)` for new providers;
- be shared by `serve`, `build`, `reload`, cleanup, and health.

### Generic build

`pipelines.py` keeps the current semantics:

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
- Channels: semantic (FAISS), keyword/title (FTS5 `docs`), native full-text
  (optional), and the same fusion/limits as today.
- Preview hydration calls `ContentStore.read`, not libzim directly.
- Redirect resolution still follows `docs.target` chains for all providers.
- Providers without a native index simply return no native hits; they still
  get semantic and keyword results.
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
    signatures: tuple[tuple[int, int, int, int], ...]
    locks: ...
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
      `indexing/schema.py`; add `provider`, `source_key`, and
      `source_fingerprint` meta keys.
- [ ] Split `build.py`: keep the public `build()` as a ZIM compatibility
      wrapper, and implement `build_index(producer, ...)`.
- [ ] Move `_index_entries`, `_save`, `_finish_build`, `_write_faiss`, and
      staging/upgrade helpers into `indexing/pipeline.py` with no libzim
      imports.
- [ ] Add `indexing/locator.py`; make ZIM paths legacy-identical.
- [ ] Create `providers/zim/entries.py` with `ArchiveEntryProducer`, wrapping
      `read_entry` and preserving ids, progress, and fast mode.
- [ ] Split `content/reader.py` into pure extractors and a ZIM adapter only if
      it reduces coupling; keep `read_entry` behavior and tests.
- [ ] Move `_search_local_locked` into `indexing/search.py`; make it take an
      `IndexHandle` + `ContentStore`.
- [ ] Move `_fetch_docs` and non-ZIM preview logic into `indexing/search.py`.
- [ ] Replace `_LocalIndex` with `IndexHandle`; move lazy archive opening to
      `ArchiveContentStore`.
- [ ] Add `NativeSearcher`; wrap libzim and keep isolation unchanged.
- [ ] Delete the temporary `Search.indexes`/`catalog` compatibility aliases
      once provider tests pass.
- [ ] Add a synthetic in-memory producer in tests to prove the pipeline has no
      libzim dependency.

## Tests

- [ ] ZIM build parity: same `docs` rows, `meta.done`/`next`, FAISS ids, and
      search results before/after the refactor.
- [ ] Resume after interruption works; changing the fingerprint restarts the
      build safely.
- [ ] A source changing during a build is detected before publication and
      cannot produce a mixed or stale published index.
- [ ] Fast build, force rebuild, upgrade-in-place, and a corrupt staging file
      all keep current behavior.
- [ ] Model fingerprint/probe mismatch still falls back to lexical search.
- [ ] Index locator: legacy ZIM paths unchanged; new paths safe, unique, and
      bounded; no absolute paths.
- [ ] Generic local search over a synthetic producer returns semantic,
      keyword, and preview results without importing libzim.
- [ ] Native fulltext absence yields the same partial status as now.
- [ ] Concurrent builds and `build_workers` still work under the global lock.

## Acceptance criteria

- `python -m pytest` passes.
- Existing ZIM indexes are discovered and served without a rebuild.
- `build.py` no longer imports provider-specific discovery.
- `indexing/` imports no libzim, FAISS, or web modules at module scope except
  inside the FAISS loaders.
- Files and Kolibri can implement only `EntryProducer` + `ContentStore` and
  get FTS, semantic search, previews, resume, and publication for free.

## Risks and mitigations

- **Index compatibility.** Add only optional meta keys; keep the existing
  `docs` schema and rowid semantics. Test with a pre-change index fixture.
- **Resume correctness.** Fingerprint checking is mandatory; a mismatch must
  rebuild rather than produce shifted row ids.
- **Large mechanical move.** Move code before changing behavior; use parity
  tests and avoid rewriting ranking or limits.
- **FAISS memory on small hosts.** Keep the existing batch/queue behavior; the
  generic pipeline must not add per-provider caches.
