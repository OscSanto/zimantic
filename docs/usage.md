# Usage and operations
## Commands

```bash
zimantic build                        # index every configured local collection
zimantic -v build                     # show info-level logging
zimantic build -vv                    # show debug-level logging
zimantic build zims/x.zim             # index one file
zimantic build zims/a.zim zims/b.zim  # several files
zimantic build --workers 4            # build up to four independent ZIMs at once
zimantic build /media/usb             # configured collections or detected ZIM/files trees
zimantic build zims/a.zim /media/usb  # mix files and folders
zimantic build /library/www/html/modules  # build an ordinary files tree
zimantic build modules/algebra       # cumulatively index one subtree
zimantic build --fast                 # title and ZIM full-text indexes (no vectors)
zimantic build --wait-stable          # wait for changing download sizes, then index
zimantic build --no-wait-lock         # exit 75 if another build holds the lock
zimantic build --lock-timeout 600     # wait at most ten minutes for the lock
zimantic build --force zims/x.zim     # rebuild one already-indexed ZIM
zimantic build --device cuda          # e5 fp16 on CUDA (see GPU acceleration)
zimantic build --provider files       # only files collections
zimantic build --collection modules   # one collection key or entry name
zimantic build --source files:modules:root  # one exact source key
zimantic build --list                 # read-only preview; write nothing
zimantic sources                      # human-readable collections/sources tree
zimantic sources --json               # machine-readable, no absolute paths
zimantic catalog                      # library metadata and absolute backing paths; no server
zimantic catalog "wikipedia" --provider zim --media-type text
zimantic catalog --collection modules --relative --json
zimantic catalog --limit 20 --offset 20  # second page of title-sorted entries
zimantic diagnose zims/               # isolated native-search smoke tests
zimantic diagnose zims/x.zim --checksum --query hawaii --json
zimantic prune --dry-run              # report orphaned Zimantic indexes
zimantic prune --yes                  # delete only those under the build lock
zimantic serve                        # web page; unindexed ZIMs use native full-text search
zimantic serve --fast                 # start without a model or vectors
zimantic serve --device cuda          # e5 fp16 on CUDA for query embedding
zimantic serve --debug                # include ranking scores and explanations; restart on SIGHUP
zimantic serve --dev                  # alias for --debug
zimantic open zims/a.zim              # throwaway read-only server for one named ZIM
zimantic open zims/                   # every .zim in a folder (recursively)
zimantic open a.zim b.zim             # switch between the selected ZIMs in the toolbar
zimantic open a.zim --no-browser      # print the URL without launching a browser
zimantic open a.zim --port 9000       # pin a loopback port; default is OS-assigned
zimantic reload                       # ask a running server to rescan indexes and ZIMs
zimantic model                        # download the int8 export (CPU default)
zimantic model fp16 --model multilingual-e5-small  # GPU: e5 fp16 export
zimantic model fp32 fp16              # download selected precisions
zimantic model --model e5-small-v2    # download another supported model
zimantic model --dir ./models         # write models/<precision>/ instead of model/
zimantic model --list                 # show supported models and precisions
```

`build` accepts local files or folders, independent of storage provider. Paths select matching configured ZIM, files, and Kolibri collections, including collections below a named parent directory. Enclosing roots use the most specific match per provider; equally specific roots of the same provider require an explicit `--collection` selector. With no arguments it builds every configured local collection; Kiwix collections are reported as skipped because they are remote/search-only. Positional paths and `--provider`/`--collection`/`--source` selectors are separate modes and cannot be combined. Selector values are repeatable: values within one selector are ORed, and different selector types are intersected. `--collection` accepts an exact collection key or a configured entry name; a Kolibri entry name selects all of its channels.

Unmatched paths are atomically appended as `[[collections]]` entries so `serve` can discover them. A `.zim` file becomes a ZIM collection; an ordinary file becomes a files collection rooted at its parent, initially indexing only that named file. An unmatched directory becomes a ZIM collection if it contains archives, a files collection if it contains other files (or is empty), or both when mixed. Entries use paths relative to the active config directory (or absolute private locators) and safe unique names. Kolibri installations must already be configured; their home/database paths select installed channels and storage paths select the nodes backed by those files.

Subpaths build only covered content, not unrelated sibling sources. Within a files or Kolibri source, successive scans accumulate source-relative file/directory scopes. The published generation stores those scopes and a partial-coverage flag; APIs expose `index_partial` for sources and collections, and partial source coverage is also marked `truncated`. Covered content is reread when the scope or source fingerprint changes, rebuilding the covered union so updates and deletions are reflected without losing existing results. This avoids per-item coverage tables and keeps metadata proportional to the selected paths, but cumulative rebuilds can repeat extraction/embedding work. Source row IDs, derived-text inputs, and semantic vectors stay aligned, and interrupted builds of the same scope remain resumable. A whole-source build clears the scoped metadata; a later narrow scan never downgrades an already complete source to a subset. Collection completeness is separate from build mode: `fast`/`full` describes available search channels, not coverage.

Restart a running server to load appended configuration; `zimantic reload` rescans startup-configured providers but does not reread configuration. `build --list` previews configured or generated entries and never writes configuration or starts a build. Covered generations with no changes are skipped. Pass `--force` to rebuild selected indexes, or let a source fingerprint mismatch trigger a rebuild.

`zimantic sources` uses the discovery process from `serve`. It does not start the web server, load the model or FAISS, or modify the catalog, indexes, caches, PID/build/lease files, or configuration. Its `--json` document has `collections` and `sources` at the top level and uses the HTTP record schemas. It omits private locators and absolute paths. `zimantic prune --dry-run` provides the same read-only inventory.

`zimantic catalog` uses the same read-only discovery, including unsearchable library entries. It filters source and collection display metadata using the same case-insensitive substring match as `/library.json`; provider, collection, source, and media-type selectors compose before title sorting and paging. Provider, collection, and source selectors are repeatable, with the same OR/intersection rules as `build`. Omit `--limit` to list every match. JSON output contains `collections`, the paged `sources`, `total` matching sources, `offset`, and `limit`; each source retains the HTTP metadata and adds a private `paths` object. Paths are absolute by default; `--relative` resolves them relative to the current working directory, even when configuration lives elsewhere (paths on another Windows drive remain absolute). ZIM entries show the actual selected archive, files entries show their membership directory (the root for loose-file or whole-collection sources), and Kolibri entries show the configured home, original database, and content storage rather than a temporary snapshot. Kiwix entries have empty `paths` and retain their remote `homepage` URL; remote discovery needs the configured Kiwix server. Unlike `sources --json` and the HTTP APIs, catalog output intentionally includes private local paths.

`zimantic prune --yes` holds the build lock and target-based config lock. It rereads configuration and discovery, then deletes only indexes with a valid Zimantic-owned manifest whose collection namespace is not configured. It retains or reports missing or malformed configuration, entries with unknown scope, unavailable discovery, stale `.tmp`/`.upgrade` files, and unrecognized indexes. It never deletes files by filename alone. Manual configuration edits must not race with `prune`. Restart running servers after you edit collection roots and before you build or prune. A stale server can refer to a removed or different entry. Leases protect open generations, but not indexes the server has not opened. An ad-hoc collection append is safe while a server runs because its scoped catalog refresh does not remove that collection.

Source and index discovery runs at startup or on explicit reload, never on a timer or because a browser requests a source/library listing. Restart `serve` or run `zimantic reload` to discover added, removed, or replaced sources and indexes. Kiwix retries failed catalog requests in the background until a request succeeds; it does not periodically refresh a successful catalog.

`zimantic reload` sends SIGHUP and reports that the signal was delivered, not that the background refresh has finished. A normal server rescans every provider configured at startup (ZIM, files, Kolibri, and Kiwix), but does not reread configuration. Restart the server to load edited settings. A second signal during a refresh is ignored. A `serve --debug` process restarts and rereads configuration on SIGHUP. On platforms without SIGHUP, restart the server to load settings.

Logging defaults to warnings, except that `serve`, `open`, and `model` use info when stderr is a terminal. Repeat `--verbose`/`-v` for info and debug output.

## Opening named ZIMs

`zimantic open` is an inspection command for a named set of archives, not a replacement for `zimantic serve`:

```bash
zimantic open zims/phet_en_all_2026-08.zim
zimantic open zims/a.zim zims/b.zim
zimantic open zims/ --max-sources 32
```

It starts a temporary server on `127.0.0.1` and opens the first selected ZIM. The port is assigned by the operating system unless `--port` pins one. Only the ZIM files named on the command line (after folder expansion) are visible to `/sources`, `/library.json`, search, suggestions, random articles, and the browser; configured collections are not scanned and Kiwix catalogs are ignored. Use `--no-browser` on a headless machine or when the desktop browser cannot be launched.

The scope is read-only and process-scoped. `open` never builds, resumes, publishes, or prunes an index; it never writes `catalog.sqlite`, an index, `zimantic.pid`, a build lock, or config. It may read the persistent catalog once to identify a selected archive, then opens any matching finished index read-only. A uniquely matched catalogued archive keeps its normal source key and `/content/<key>/` URL, so links match a `serve` process using the same catalog. An unmatched, ambiguous, or unreadable archive gets an ephemeral `zim:open:<name>-<position>` key that never locates a persistent index. If a finished index is missing, stale, or mismatched, the archive's native full-text index is used; a ZIM with neither remains readable and navigable but is not searchable.

When more than one ZIM is selected, the article toolbar shows separate previous/next ZIM arrows. They open the adjacent archive's homepage in command-line order and wrap at both ends. These arrows are independent of the search-result and parent-page arrows. They also appear in error shells. `--fast` disables the model and FAISS for this process, as with `serve --fast`; without it, title and full-text search work immediately while semantic search can become available when the model is ready.

`zimantic serve` continues to use the persistent catalog, configured collections, PID file, and SIGHUP reload behavior. Stop the `open` process with Ctrl-C or SIGTERM; it closes its indexes and removes nothing from the user's library.

## Diagnosing ZIM archives

`zimantic diagnose` opens each named archive in a disposable helper, optionally
checks its checksum, and iterates native search results:

```bash
zimantic diagnose zims/
zimantic diagnose zims/osm-hawaii-2026-09-26.zim --query hawaii --checksum
zimantic diagnose a.zim b.zim --query water --query history --json
```

Files and recursively expanded folders are deduplicated, with at most
`--max-sources` archives (64 by default). The command does not load configuration,
models or FAISS, start a server, or write catalogs/indexes. It processes archives
one at a time so a crash cannot hide reports for the remaining files.

Default probes are `water`, `history`, and `science`; repeat `--query` to replace
them, especially with the query that failed in the server. Each probe iterates
up to `--limit` results (16 by default). `--checksum` reads the entire archive.
`--timeout` bounds all checks on each archive (30 seconds by default).
An archive without native full-text search is reported explicitly, not treated
as corrupt.

The exit status is zero when all selected archives pass, and one for any
error, crash, timeout, or resource kill. JSON reports contain an `archives`
array with the path, status, checks, elapsed time, and failure diagnostics.
On failure, stderr breadcrumbs identify the archive and last native operation.
SIGKILL is reported as a possible resource limit, not proof of corruption.
Like `catalog`, this CLI output intentionally includes private paths.

These are smoke tests, not a guarantee for every query. A valid checksum
does not imply a logically consistent full-text index: the Hawaii archive
passed its checksum and aborted during result iteration for `hawaii`.
Diagnostics are on demand, not an expensive mandatory startup scan.

## Build behavior and locking

Fast indexes. `build --fast` stores titles and paths without running the model or creating FAISS vectors. It reads a small bounded HTML prefix per entry to detect disambiguation suffixes, meta-refresh shell targets, and app-shell companion deep links. It does not store article excerpts or build the SQLite body index. Title search and ZIM full-text search work; a normal `build` also creates the body and vector indexes.

Stable files. `build --wait-stable` polls source sizes until they stop changing before indexing. The systemd units below pass it so a file caught mid-download is not indexed. An explicit CLI build starts immediately.

Build lock. Every build takes an advisory lock at `<index_dir>/../.build.lock` (beside `catalog.sqlite`), so a manual build, a `systemd.path` trigger, and a timer cannot write the same indexes at once. A second build waits by default. `--no-wait-lock` exits with status 75 (`EX_TEMPFAIL`) immediately, which the IIAB units use to skip a trigger without marking it failed. `--lock-timeout` bounds the wait. The kernel releases the lock when the process exits, so a killed build leaves no stale lock. Run manual builds as the same user as the server: a lock or index owned by another user (for example after `sudo zimantic build`) produces a `cannot open the build lock` error that names the file to fix.

Workers and batching. The desktop profile reads/commits windows of 32 rows (mobile 8, supercomputer 128). Within each window, final capped inputs are encoded once, sorted by token length, and split into calls bounded by `embedding_batch_tokens` (default 1,000 **padded** tokens). A single longer input runs alone. Independent sources can be read concurrently with `--workers N` or `build_workers`. Full CLI builds overlap reading with a bounded shared embedding pipeline, even for one source: at most `onnx_build_workers` model calls run at once and each source has bounded read-ahead scaled to the pool size, so a slow window does not leave the other workers idle. When one source is actively building, its reader, embedding preparation, and ordered SQLite writer also run as separate bounded stages; commits stay in source order and one in-flight-window budget covers all stages. With several concurrent source builds, source-level overlap applies instead because extra per-source stage threads add CPU contention on the tested desktop. Fast builds and vector-reuse builds use their synchronous paths. Automatic embedding concurrency uses physical cores where detected, CPU affinity/cgroup limits, available RAM and `onnx_build_threads` (desktop default **1**, mobile **2**, supercomputer **8**, bounded by usable cores; the Pi 5/500 preset explicitly uses **3**). `onnx_query_threads` controls **serving/query embedding only**. Increase build threads only after benchmarking: extra intra-call synchronization reduces the independent worker budget when several workers fit in RAM. If RAM limits the pool to one worker, per-call parallelism can help. The RAM allowance is conservative, not an OOM guarantee for arbitrary models. Tune `batch_size` and `embedding_batch_tokens` first; worker/thread settings are [advanced overrides](#advanced-resource-overrides). Builds targeting the same `index_dir` are serialized by the build lock.

Query threads. Every profile uses the same automatic budget: at most four usable physical cores, leaving one for other work (minimum one thread). This gives three on an unconstrained four-core Pi 5/500 and one under a one-CPU quota. One thread is not a good blanket serving default: longer queries benefit substantially from parallelism. More than four can help near-limit queries but hurt typical short searches; override `onnx_query_threads` for that workload. These are balanced defaults, not a universal optimum for every model or device.

With python-libzim 4.1 or newer, ZIM builds use its native `iter_efficient()` (cluster-order) iterator. Article work is followed by a cheap redirects/assets phase without an ETA; article progress counts committed work rather than read-ahead entries. Earlier python-libzim versions use entry-ID order, which can be slower. The order and its fingerprint are recorded for safe resume; interrupted builds without that manifest must be rebuilt. No raw ZIM-table parsing is needed. See [build performance and quality measurements](build-performance.md).

Disk space. Keep about **20% of total ZIM file size** free when building indexes.

## Hardware profiles

Tuning keys in `config.toml` are commented out by default. Profile selection uses usable physical cores and effective total RAM, including ancestor cgroup limits, rather than the unrestricted host's CPU/RAM. Mobile applies through 1 GiB; supercomputer applies at 32 usable physical cores or 128 GiB; other hosts use desktop. Profiles choose batching, caches and result pools, while serving thread/process counts are resolved separately. Build concurrency also uses usable logical CPU threads and **available**, not total, RAM.

Copy `config.pi-zero-2w.toml` or `config.pi-5.toml` for memory/batching starting points, or `config.gpu.toml` for a GPU host. They leave serving counts and source-build concurrency automatic. The Pi 5 preset retains its measured three-thread build override; omit `onnx_build_threads` to use the automatic profile value instead.

### Advanced resource overrides

Normally omit these settings. They control different layers and should not be merged into one equal worker count:

| Setting | Scope | Automatic default |
|---|---|---|
| `source_query_threads` | Shared server threads for source searches and previews: SQLite/FAISS, remote I/O and native-helper calls | Up to 8 usable logical CPU threads |
| `onnx_query_threads` | ONNX threads inside one query-embedding call, not source fan-out | Up to 4 usable physical cores, leaving one for other work; minimum 1 |
| `libzim_search_backend` | Native full-text execution backend | `"processes"`; opt-in `"threads"` requires a GIL-releasing python-libzim build |
| `libzim_search_processes` | Shared native full-text worker slots across every ZIM collection, for either backend | Up to 8, bounded by CPU, source threads and available RAM; 256 MiB server/model reserve plus 128 MiB per worker |
| `build_workers` | Concurrent source builds, not model calls | Up to 8; four usable logical CPU threads and four GiB available RAM per worker; minimum 1 |
| `onnx_build_threads` | ONNX threads inside each build model call | Profile value 1/2/8, bounded by usable physical cores |
| `onnx_build_workers` | Concurrent build model calls sharing one loaded model, including single-source builds | CPU, per-call threads, padded-token budget and available RAM, up to 8 |

The source-thread ceiling of eight preserves the desktop baseline and the source-executor size used in the native benchmarks; it is not proof of an optimum for remote-I/O-heavy libraries. The native-process ceiling and per-query ONNX cap have separate measurements. Positive explicit values override automatic bounds; invalid types or nonpositive counts warn and fall back. Memory allowances are estimates, not OOM guarantees. Restart after changing serving overrides.

`max_concurrent_searches` is separate: it admits user requests, not source tasks or helpers. Profile ceilings of 2/4/8 are trimmed by usable CPU threads and one slot per 256 MiB available RAM, with a minimum of one. This is a conservative admission heuristic, not a per-request allocation. Requests share the source/native pools, so their capacities are not multiplied by the admitted-user count. A positive explicit admission limit overrides this estimate.

These deprecated config aliases are accepted: `embed_threads` → `onnx_query_threads`, `search_workers` → `source_query_threads`, `fulltext_workers` → `libzim_search_processes`, `build_embed_threads` → `onnx_build_threads`, and `embedding_workers` → `onnx_build_workers`. A deprecated key logs one warning when loading a config. If both names are present, the canonical name takes precedence regardless of order. If its value is invalid, the automatic or profile fallback applies rather than the deprecated value.

## CPU build tuning

On the tested i5-13600KF desktop with the `multilingual-e5-small` alternative,
smaller model calls beat larger batches. This CPU-only configuration retains the
normal 32-row read window. The findings describe the transformer pipeline; the
static default is limited by tokenization rather than model-call size:

```toml
embed_device = "cpu"
model_dir = "model/int8"
batch_size = 32
embedding_batch_tokens = 256
onnx_build_threads = 1
```

Leave `onnx_build_workers` automatic as a starting point: it selected eight
workers on this desktop. More workers and multithreaded calls were not faster
enough to justify their extra CPU use here. CPU queues admit at least one read
window, so small calls do not prevent source reading from overlapping inference.
GPU queue bounds do not change.

These settings are not universal defaults, especially for GPUs and small ARM
hosts. See [CPU int8 throughput tuning](build-performance.md#cpu-int8-throughput-tuning)
for repeated complete-build results and numerical checks.

## GPU acceleration

Use the `multilingual-e5-small` transformer, not the static default, when you
have a GPU. The static model is a token-lookup graph that is already fast on a
CPU and gains little from a device; the transformer is the slow path a GPU
actually accelerates, and it retrieves better. Copy
[`config.gpu.toml`](../config.gpu.toml) for a complete starting point.

Quick start for an NVIDIA CUDA host:

```bash
uv sync --locked --no-dev
uv pip install onnxruntime-gpu   # or -rocm, -directml, -openvino
zimantic model fp16 --model multilingual-e5-small
```

```toml
embed_device = "gpu"       # or "cuda"
model_dir = "model/multilingual-e5-small/fp16"   # relative to the XDG data directory
```

Install the accelerated wheel after every `uv sync`, because the lockfile
reinstalls the plain `onnxruntime` package. Embedding can run on a GPU through
ONNX Runtime's accelerated execution providers. `embed_device = "auto"` (the default) tries every accelerated provider the installed ONNX Runtime build exposes, in CUDA, ROCm, MIGraphX, DirectML, and OpenVINO order, and otherwise stays on the CPU.

Supported values are `auto`, `gpu` (prefer an accelerated provider), `cpu`, `cuda` (NVIDIA), `rocm` (AMD), `migraphx` (AMD through a MIGraphX-enabled ONNX Runtime build), `directml`/`dml` (Windows), `openvino` (Intel, using OpenVINO `AUTO` device selection), and `coreml` (Apple). `--device` overrides the setting for one `build` or `serve` run. CoreML is never selected by `auto`: stock macOS `onnxruntime` wheels include it, and its dynamic-shape handling can be slower than the CPU for this model.

Use the fp16 export of `multilingual-e5-small`. Download it with `zimantic model fp16 --model multilingual-e5-small` and point `model_dir` at `model/multilingual-e5-small/fp16`. Accelerated providers usually run fp16 faster than the quantized int8 export, which is tuned for CPUs. Keep the static default on CPU-only hosts and small devices, and keep `e5-small-v2` only for English-only libraries.

The provider comes from the ONNX Runtime package, not from Zimantic. Replace the plain `onnxruntime` wheel with `onnxruntime-gpu` (CUDA), `onnxruntime-rocm`, `onnxruntime-directml`, or `onnxruntime-openvino`; MIGraphX requires an ONNX Runtime build that includes `MIGraphXExecutionProvider` (for example, a ROCm build targeting gfx803). If the requested provider is missing or cannot initialize (for example because a CUDA library is absent), Zimantic warns and embeds on the CPU, so the same configuration also runs on machines without a GPU. If a run fails on the device after initialization (for example a GPU out-of-memory error), Zimantic warns and retries on the CPU instead of failing the build.

```bash
pip uninstall -y onnxruntime   # only one onnxruntime distribution provides the module
pip install onnxruntime-gpu    # or onnxruntime-rocm/-directml/-openvino
zimantic build --device cuda   # or embed_device = "cuda" in config.toml
```

When Zimantic is managed with `uv`, `uv sync` reinstalls the plain `onnxruntime` dependency, so install the GPU wheel after syncing (or change the dependency in the project environment).

GPU acceleration helps full index builds the most, because they embed every article; raising `embedding_batch_tokens` (and, if needed, the `batch_size` read window) uses the device more efficiently. Lower the token budget if a call exhausts GPU memory. Query embedding also runs on the device, but a single short query is too small to gain much. Vector search (FAISS) deliberately stays on the CPU: the indexes are memory-mapped, and copying one to GPU memory to search a single query vector would cost more than the search itself.

MIGraphX compiles programs for input shapes. Zimantic serializes MIGraphX runs
to avoid concurrent first-run crashes. Builds use fixed shapes: sequence widths
are rounded up to powers of two, starting at 32 tokens (or the model limit if
smaller), and batch capacity comes from `embedding_batch_tokens` divided by that
width, with a minimum of one input. At a 256-token limit and a 1,024-token
budget, the shapes are `4x256`, `8x128`, `16x64`, and `32x32`. Up to four sessions
stay resident, or one in low-memory mode. Already queued requests with compatible
shapes are coalesced, including across sources, without waiting for more work;
CPU batching does not change.

Set `ORT_MIGRAPHX_CACHE_DIR` to a writable, GPU-specific directory to reuse
compiled build shapes across commands. Zimantic creates subdirectories isolated
by model fingerprint, ONNX Runtime version, and input shape. The first build compiles each shape; a warm build reloads the compiled sessions. Dynamic
query sessions do not use the disk cache because some MIGraphX versions reuse
an incompatible first-shape program. Compilation and session loading can make
small builds slower than CPU int8 even with a warm cache.

For throughput on a desktop gfx803 GPU, the following settings beat CPU int8 on
a two-source build; they are not universal defaults:

```toml
embed_device = "migraphx"
model_dir = "model/multilingual-e5-small/fp16"
batch_size = 256
embedding_batch_tokens = 1024
onnx_build_workers = 4
onnx_build_threads = 1
```

See [the warm CPU/GPU comparison](build-performance.md#warm-migraphx-versus-cpu-builds-gfx803)
for complete-command timings and the small-build limitation.

## Automatic indexing with systemd

Ready-to-copy user units in `deploy/` watch `zims/`, build indexes, and reload the server as ZIMs become available:

- `deploy/zimantic.service` — the server, with `Restart=always`, restart rate limiting, and sandboxing. Copy it to `~/.config/systemd/user/`, then run `systemctl --user enable --now zimantic`.
- `deploy/zimantic-indexing-fast.path` + `deploy/zimantic-indexing-fast.service` — watch `zims/` and run a fast build followed by `reload` as a ZIM becomes available.
- `deploy/zimantic-indexing.service` + `deploy/zimantic-indexing.timer` — run a full build with semantic vectors once a night.

The path-triggered fast build runs for each ZIM in `zims/`. The full build runs from 01:00–06:00 local time by default; set its `ExecCondition` and timer to select a different window. Both units pass `--no-wait-lock`, so a build that finds the index lock held exits 75 and the unit treats it as a skip instead of overlapping the build that is already running.

The server unit retries after 10 seconds but stops after three starts in ten minutes, so a server that cannot fit its archives fails visibly instead of looping through OOM kills. Check `systemctl --user status zimantic` and `journalctl --user -u zimantic`; after fixing the cause, `systemctl --user reset-failed zimantic` clears the rate limit. Its header also shows `MemoryHigh`/`MemoryMax` pairs for a Pi Zero 2 W and a Pi 5; uncomment and adjust them for the device, leaving headroom for the OS and kiwix-serve. `MemoryHigh` throttles first; `MemoryMax` is the hard limit that OOM-kills only Zimantic.

Each unit has install instructions in its header. The units use the default XDG config/data directories, `~/.config/zimantic` and `~/.local/share/zimantic`, and the CLI at `~/.local/bin/zimantic`. If you use custom XDG directories, configure them in the unit or a drop-in; update `ReadWritePaths` and `PathChanged` for a custom data directory.

The units call the installed `~/.local/bin/zimantic` executable. `reload` does not need the config file. It reads the PID from `$XDG_RUNTIME_DIR/zimantic/zimantic.pid`, then from `./zimantic.pid` when present, or from the file passed with `--pid`.

Watch `zims/`, not `indexes/`, because builds write to `indexes/`. If you [copy finished indexes from another machine](collections.md#copying-finished-indexes-to-another-machine), watch `indexes/` and run only `reload`.

A running `serve` discovers indexes with `zimantic reload`; a fast index is searchable while a full index builds.

## Source selection

The source picker discovers available sources and sorts matching sources by result count, grouped by collection. Counts come from the bounded ranked result pool, so a searched source may not appear if none of its results make that pool. To search one source directly, open its details in the library and choose **Search**. To search a whole collection, choose **Search collection** on the collection group. Each group shows its top `source_filter_limit` sources (10 by default) plus an **Other** disclosure for the rest, and source-filter searches show up to 30 matches plus **Show N more**. Selected sources stay visible even when their group is not expanded. Dated and flavour variants of one book collapse to a single source here and in the library view.

- Selecting a collection header searches that whole collection as one unit. Only one collection group can be selected at a time; selecting another replaces it. Selecting an individual source switches to individual-source display mode and clears the group selection, after which sources from different groups can be combined. The other searched sources stay available in the picker, and the next query keeps the ranked scope while narrowing the returned page to the selected sources.
- An explicit source or collection selection always defines the search scope directly. Zimantic does not promote a small selection to a broader search, and there is no `zim` parameter: the canonical API parameter is `source`, and `collection` selects one collection as a unit.
- A source selected outside an unscoped Kiwix fan-out cap is searched explicitly. An explicitly selected Kiwix collection is bounded by `max_explicit_collection_sources` (500 by default) and the request deadline. Zimantic reports partial scope explicitly and does not report it as complete.
- An unknown source or collection key is rejected with HTTP 400 rather than silently becoming an unscoped search. A known but unsearchable source, or an intersection with no eligible sources, searches no sources rather than broadening the scope.
- A scoped search shows **Searching only in: …** or **Searching collection: …** above the results with a **Search all sources** control, so another query while a filter is active does not silently hide the narrower scope.

## Sorting and empty searches

The **Sort by** selector below the search bar offers Relevance, Random,
Rare titles, Semantic outliers, Diversity, and Recency.
With a query, these orders rearrange the bounded matching result
pool without changing its membership. Items without the local statistics needed
by an order follow the scored matches in relevance order.

Submitting an empty query, or opening `/?q=`, searches local indexes without
embedding a query. Its default order is Diversity, including when semantic
search is disabled. `/` opens without running a search.
Use `/?q=&sort=random`, `/?q=&sort=rare`, `/?q=&sort=outliers`,
`/?q=&sort=diverse`, or `/?q=&sort=recency`,
optionally with `source` or `collection` filters.

- Random shuffles matches, or samples indexed items for an empty query.
  Submitting a random search creates a URL seed; that seed keeps
  pagination, bookmarks, and Back/Forward consistent.
- Rare titles ranks titles by the mean inverse document frequency of their
  distinct non-stopword terms, using the existing Unicode61 title index.
- Semantic outliers finds items least similar to their source's average
  embedding.
- Diversity adapts per item. Items with usable embeddings use equal-weight
  maximal marginal relevance (MMR), balancing semantic novelty against query
  similarity blended with normalized hybrid relevance, or source centrality
  for an empty query. Other items use greedy title-word diversity, including
  in fast mode. Word overlap also detects repeated titles across the two
  groups; lexical-only items are not appended after semantic results.
  Mixed pools keep the input's semantic/lexical slot pattern rather than
  comparing unlike raw scores. Ties preserve input order after novelty.
  Empty or stopword-only titles without embeddings come last.
  Empty queries mix central candidates where available with a deterministic
  random sample, and balance source exposure. If query embedding fails,
  hybrid relevance supplies the semantic relevance component.
- Recency orders by modified time: file modification time for files,
  primary-file modification time for Kolibri, and archive-file modification time
  for ZIM entries. Items without a timestamp come last.

Centroid and rarity statistics are source-local. Empty searches
interleave source rankings rather than comparing their raw scores, except
Recency, which compares timestamps across sources. Diversity also
considers already selected items across sources while balancing exposure.
Empty searches use only local SQLite indexes; remote Kiwix
sources and unindexed sources are omitted. Semantic outliers additionally
require loaded FAISS vectors and are unavailable with `serve --fast` or
`open --fast`; Diversity uses title words in that case.
Rare-title pools and centroids are prepared lazily in memory, reused for the
index handle's lifetime, and discarded when it is retired.
First requests can take longer and obey the normal source/search deadlines.
The usual `max_results` pool limit applies. No index rebuild is needed.

The JSON API is `/search?q=topic&sort=random&seed=7`, or `/search?q=` for an
empty search; `/search/stream` streams the same request. Both accept `source`,
`collection`, `limit`, and `offset`. API random requests default to seed `0`;
the browser chooses a fresh seed for an unseeded random-search link. Rarity
and semantic rankings ignore the seed. The deprecated `?browse=...` links and
the `/browse` and `/browse/stream` APIs remain supported.

## Library view

`/library` shows every configured collection as a group with artwork, descriptions, metadata, and direct links to each source's homepage or random article. A one-source collection renders as a single card; a larger collection shows a header with its source count and media summary plus a bounded preview grid. **Load more sources** opens the group and pages through its remaining sources, so the first source page is not mistaken for the whole collection. **Shuffle** reorders the visible groups and their loaded sources. Dated and flavour variants of one book collapse to a single source card.

Local file sources without an `index.html` or `index.htm` homepage open a directory listing, even before indexing. **Browse files** shows folders, file sizes, and modification dates, with filename filtering and sorting. Subdirectories without a homepage use the same view; existing homepages open normally. Listings omit hidden paths, symlinks, and configured exclusions, and links open through the article reader.

Library cards show a short source description. File sources read root-level `README` files first, then `about` files, then `index.html` or `index.htm` (case-insensitive). README/about files can be Markdown, plain text, or HTML; HTML prefers description/Open Graph metadata and otherwise uses opening prose. Summaries are limited to 400 characters, reading at most 64 KiB per candidate, and respect exclusions and symlink confinement. Like ZIM descriptions, file summaries are cached in `catalog.sqlite`, without needing an index. Startup and explicit reload check candidate names, sizes, timestamps, and permissions before reusing a summary; changed, added, removed, or excluded documents refresh it. Library requests never rescan or read the documents. Read-only commands use an in-memory cache.

The provider filter (All providers / ZIM / Files / Kolibri / Kiwix), media-type filter (All media / Text / Video / Audio / Images / Other), and text filter compose on the server before paging, so matches beyond the first fetched page remain discoverable. A specific media type matches only a source whose `media_types` contains that family; All media includes sources with no known family. Collection cards summarize the union of their sources' known types. Prefix text terms with `-` to exclude matches. Snapshot-version changes while a group is loading restart that group rather than mixing records from two discovery revisions.

Each ZIM source with MIME counters is assigned exactly one media type. Classification uses its two strongest MIME signals, counting common page scaffolding (HTML/XHTML, CSS, JavaScript, fonts, and icons) at one tenth their reported count. When combined audio/video counts reach at least half the HTML, XHTML, and PDF count, the source is classified as whichever of audio or video has the higher count. Plain-text and Markdown counts provide the document baseline when there are no HTML, XHTML, or PDF documents. Invalid counters fall back to the available MIME families; other providers use their own discovery metadata.

If no collection is configured, the page says so and points at a `[[collections]]` entry plus a server restart. Collections that exist but discovered no sources show their discovery state or error rather than appearing unconfigured.

Click a source's title or **Details** for an infobox with its thumbnail, homepage, descriptions, counts, license, attribution, relation, tags, provider, collection, media types, and media-type breakdown. The infobox offers **Search** for one source. No filesystem path is shown.

## Caches

Exact queries use small LRU caches controlled by `cache_size` and `cache_bytes` (32 MiB per cache by default). A ranked pool is keyed by the normalized query, the expanded source keys of the resolved scope, and the publication generation. Paging reuses the same pool. When a search reaches its deadline, the partial pool is reused for `partial_cache_seconds` (60 by default) so reloading the same query does not repeat a timed-out fan-out. Replacing an index without changing source keys, changing collection membership, or changing discovery metadata invalidates the affected generation, so stale results and previews are not served.

For large libraries, `source_query_threads` controls source-query fan-out through one shared thread pool. It defaults to at most eight usable logical CPU threads, independently of the memory profile. `max_stream_ranks` limits completion-spaced intermediate ranking passes. Timed snapshots are additionally emitted about every 700 ms so a slow query keeps updating, and the final result ranks every source. A lower `max_stream_ranks` reduces CPU use on small devices. Profiles retain 16 completion-spaced passes on desktops and 8 on low-memory devices.

Native ZIM full-text search uses one reusable helper pool shared across all ZIM collections, rather than running libzim in the server's source threads. `libzim_search_processes` sets its server-wide budget independently of `source_query_threads`; when omitted, CPU affinity/quota, source-thread capacity and available RAM (including cgroup limits) determine the budget. Explicit positive values override that estimate. See [advanced resource overrides](#advanced-resource-overrides).

`libzim_search_backend = "threads"` selects a bounded native thread pool instead.
It requires the patched GIL-releasing reader bindings and gives
each worker independent archive/searcher handles, with a four-archive LRU cache.
Use it with the native libzim database-mutex acquisition fix as well.
Both backends skip snippet generation when the binding supports that option:
Zimantic only needs native paths and titles.
The [controlled reader/thread benchmark](architecture.md#reader-gilthread-comparison-november-9-2026)
documents native-search overrides for this desktop and a four-CPU/2 GiB proxy;
it does not establish a whole-server Pi memory budget.
Thread timeouts are soft: the caller stops waiting, but the worker slot and
its handles stay occupied until the native call returns. Closing the pool
rejects further work and cancels queued jobs without destroying active handles;
running calls can delay interpreter exit. Processes are the default.

With libzim 4.1.0, malformed full-text results can abort the interpreter, and reader calls hold the GIL. Helpers contain those crashes and provide real native-search parallelism: a four-worker benchmark was 3.4–4 times faster than threads, at a higher memory cost. A native crash logs the archive basename and disables that archive's native search until reload; other available channels stay usable. A helper killed by SIGKILL, including a cgroup OOM kill, does not blacklist the archive. Budget memory for the server and its helpers. See [the architecture rationale and benchmarks](architecture.md#native-zim-full-text-search-uses-helper-processes).
