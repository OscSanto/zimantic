# Usage and operations
## Commands

```bash
zimantic build                        # index every configured ZIM collection
zimantic -v build                     # show info-level logging
zimantic build -vv                    # show debug-level logging
zimantic build zims/x.zim             # index one file
zimantic build zims/a.zim zims/b.zim  # several files
zimantic build --workers 4            # build up to four independent ZIMs at once
zimantic build /media/usb             # every .zim in a folder
zimantic build zims/a.zim /media/usb  # mix files and folders
zimantic build --fast                 # title and ZIM full-text indexes (no vectors)
zimantic build --wait-stable          # wait for changing download sizes, then index
zimantic build --no-wait-lock         # exit 75 if another build holds the lock
zimantic build --lock-timeout 600     # wait at most ten minutes for the lock
zimantic build --force zims/x.zim     # rebuild one already-indexed ZIM
zimantic build --device cuda          # embed with a CUDA GPU (see below)
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
zimantic prune --dry-run              # report orphaned Zimantic indexes
zimantic prune --yes                  # delete only those under the build lock
zimantic serve                        # web page; unindexed ZIMs use native full-text search
zimantic serve --fast                 # start without a model or vectors
zimantic serve --device cuda          # keep a CUDA GPU for query embedding
zimantic serve --debug                # include ranking scores and explanations; restart on SIGHUP
zimantic serve --dev                  # alias for --debug
zimantic open zims/a.zim              # throwaway read-only server for one named ZIM
zimantic open zims/                   # every .zim in a folder (recursively)
zimantic open a.zim b.zim             # switch between the selected ZIMs in the toolbar
zimantic open a.zim --no-browser      # print the URL without launching a browser
zimantic open a.zim --port 9000       # pin a loopback port; default is OS-assigned
zimantic reload                       # ask a running server to rescan indexes and ZIMs
zimantic model                        # download the int8 export (CPU default)
zimantic model fp16                   # download fp16; recommended with a GPU
zimantic model fp32 fp16              # download selected precisions
zimantic model --model e5-small-v2    # download another supported e5 model
zimantic model --dir ./models         # write models/<precision>/ instead of model/
zimantic model --list                 # show supported models and precisions
```

`build` accepts files or folders. A folder means every `*.zim` file under it, and with no arguments it builds every configured ZIM, files, and Kolibri collection; Kiwix collections are reported as skipped because they are remote/search-only. Positional paths and `--provider`/`--collection`/`--source` selectors are separate modes and cannot be combined. Selector values are repeatable: values within one selector are ORed, and different selector types are intersected. `--collection` accepts an exact collection key or a configured entry name; a Kolibri entry name selects all of its channels.

An unmatched positional path is atomically appended as a `[[collections]]` ZIM entry so `serve` can discover it. The entry uses a relative path from the active config directory (or an absolute private locator) and a safe unique name. A matched configured ZIM root supplies defaults only: the build still covers exactly the files or folders named on the command line. Restart a running server to load edited configuration; `zimantic reload` rescans startup-configured providers but does not reread configuration. `build --list` previews configured or generated entries and never writes configuration or starts a build. Existing complete generations are skipped. Pass `--force` to rebuild selected indexes, or let a source fingerprint mismatch trigger a rebuild.

`zimantic sources` uses the discovery process from `serve`. It does not start the web server, load the model or FAISS, or modify the catalog, indexes, caches, PID/build/lease files, or configuration. Its `--json` document has `collections` and `sources` at the top level and uses the HTTP record schemas. It omits private locators and absolute paths. `zimantic prune --dry-run` provides the same read-only inventory.

`zimantic catalog` uses the same read-only discovery, including unsearchable library entries. It filters source and collection display metadata using the same case-insensitive substring match as `/library.json`; provider, collection, source, and media-type selectors compose before title sorting and paging. Provider, collection, and source selectors are repeatable, with the same OR/intersection rules as `build`. Omit `--limit` to list every match. JSON output contains `collections`, the paged `sources`, `total` matching sources, `offset`, and `limit`; each source retains the HTTP metadata and adds a private `paths` object. Paths are absolute by default; `--relative` resolves them relative to the current working directory, even when configuration lives elsewhere (paths on another Windows drive remain absolute). ZIM entries show the actual selected archive, files entries show their membership directory (the root for loose-file or whole-collection sources), and Kolibri entries show the configured home, original database, and content storage rather than a temporary snapshot. Kiwix entries have empty `paths` and retain their remote `homepage` URL; remote discovery still needs the configured Kiwix server. Unlike `sources --json` and the HTTP APIs, catalog output intentionally includes private local paths.

`zimantic prune --yes` holds the build lock and target-based config lock. It rereads configuration and discovery, then deletes only indexes with a valid Zimantic-owned manifest whose collection namespace is not configured. It retains or reports missing or malformed configuration, entries with unknown scope, unavailable discovery, stale `.tmp`/`.upgrade` files, and unrecognized indexes. It never deletes files by filename alone. Manual configuration edits must not race with `prune`. Restart running servers after you edit collection roots and before you build or prune. A stale server can still refer to a removed or different entry. Leases protect open generations, but not indexes the server has not opened. An ad-hoc collection append is safe while a server runs because its scoped catalog refresh does not remove that collection.

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

When more than one ZIM is selected, the article toolbar shows separate previous/next ZIM arrows. They open the adjacent archive's homepage in command-line order and wrap at both ends. These arrows are independent of the search-result and parent-page arrows. They also appear in error shells. `--fast` disables the model and FAISS for this process, just like `serve --fast`; without it, title and full-text search work immediately while semantic search may become available when the model is ready.

`zimantic serve` continues to use the persistent catalog, configured collections, PID file, and SIGHUP reload behavior. Stop the `open` process with Ctrl-C or SIGTERM; it closes its indexes and removes nothing from the user's library.

## Build behavior and locking

Fast indexes. `build --fast` stores titles and paths without running the model or creating FAISS vectors. It reads a small bounded HTML prefix per entry to detect disambiguation suffixes, meta-refresh shell targets, and app-shell companion deep links. It does not store article excerpts or build the SQLite body index. Title search and ZIM full-text search work; a normal `build` also creates the body and vector indexes.

Stable files. `build --wait-stable` polls source sizes until they stop changing before indexing. The systemd units below pass it so a file caught mid-download is not indexed. An explicit CLI build starts immediately.

Build lock. Every build takes an advisory lock at `<index_dir>/../.build.lock` (beside `catalog.sqlite`), so a manual build, a `systemd.path` trigger, and a timer cannot write the same indexes at once. A second build waits by default. `--no-wait-lock` exits with status 75 (`EX_TEMPFAIL`) immediately, which the IIAB units use to skip a trigger without marking it failed. `--lock-timeout` bounds the wait. The kernel releases the lock when the process exits, so a killed build leaves no stale lock. Run manual builds as the same user as the server: a lock or index owned by another user (for example after `sudo zimantic build`) produces a `cannot open the build lock` error that names the file to fix.

Workers and batching. The desktop profile reads/commits windows of 32 rows (mobile 8, supercomputer 128). Within each window, final capped inputs are encoded once, sorted by token length, and split into calls bounded by `embedding_batch_tokens` (default 1,000 **padded** tokens). A single longer input runs alone. Independent sources can be read concurrently with `--workers N` or `build_workers`. Full CLI builds overlap reading with a bounded shared embedding pipeline, even for one source: at most `embedding_workers` model calls run at once and each source has bounded read-ahead scaled to the pool size, so a slow earlier window does not leave the other workers idle. Automatic embedding concurrency uses physical cores where detected, CPU affinity/cgroup limits, available RAM and `build_embed_threads` (desktop default **1**; the Pi 5/500 preset uses **3**, mobile retains **2**, supercomputer **8**). `embed_threads` now controls **serving/query embedding only**; existing overrides no longer affect builds. Increase build threads only after benchmarking: extra intra-call synchronization reduces the independent worker budget when several workers fit in RAM. If RAM limits the pool to one worker, per-call parallelism can still help. The RAM allowance is conservative, not an OOM guarantee for arbitrary models. Tune `batch_size`, `embedding_batch_tokens`, `build_workers`, `embedding_workers`, and `build_embed_threads` in `config.toml`. Builds targeting the same `index_dir` are serialized by the build lock.

Query threads. The desktop default uses at most four usable physical cores, leaving one for other work (minimum one thread); this gives three on a four-core Pi 5/500. Mobile and supercomputer profiles retain two and eight. One thread is not a good serving default: longer queries benefited substantially from parallelism. More than four can help near-limit queries but hurt typical short searches; override `embed_threads` for that workload. These are balanced defaults, not a universal optimum for every model or device.

With **python-libzim 4.1+**, ZIM builds use its native `iter_efficient()` (cluster-order) iterator. Article work is followed by a cheap redirects/assets phase without an ETA; article progress counts committed work rather than read-ahead entries. Older bindings use entry-ID order, which can be slower. The order and its fingerprint are recorded for safe resume; interrupted builds without that manifest must be rebuilt. No raw ZIM-table parsing is needed. See [build performance and quality measurements](build-performance.md).

Disk space. Keep about **20% of total ZIM file size** free when building indexes.

## Hardware profiles

Tuning keys in `config.toml` are commented out by default, so Zimantic selects mobile, desktop, or supercomputer defaults from the machine's resources. Copy `config.pi-zero-2w.toml` or `config.pi-5.toml` to pin settings.

## GPU acceleration

Quick start for an NVIDIA CUDA host:

```bash
uv sync --locked --no-dev
uv pip install onnxruntime-gpu   # or -rocm, -directml, -openvino
zimantic model fp16
```

```toml
embed_device = "gpu"       # or "cuda"
model_dir = "model/fp16"   # relative to the XDG data directory
```

Install the accelerated wheel after every `uv sync`, because the lockfile
reinstalls the plain `onnxruntime` package. Embedding can run on a GPU through
ONNX Runtime's accelerated execution providers. `embed_device = "auto"` (the default) tries every accelerated provider the installed ONNX Runtime build exposes, in CUDA, ROCm, DirectML, and OpenVINO order, and otherwise stays on the CPU.

Supported values are `auto`, `gpu` (require an accelerated provider), `cpu`, `cuda` (NVIDIA), `rocm` (AMD), `directml`/`dml` (Windows), `openvino` (Intel, using OpenVINO `AUTO` device selection), and `coreml` (Apple). `--device` overrides the setting for one `build` or `serve` run. CoreML is never selected by `auto`: stock macOS `onnxruntime` wheels include it, and its dynamic-shape handling can be slower than the CPU for this model.

Use the fp16 model with a GPU. Download it with `zimantic model fp16` and point `model_dir` at that folder. Accelerated providers usually run fp16 faster than the default quantized int8 export, which is tuned for CPUs. Keep int8 for CPU-only hosts and small devices.

The provider comes from the ONNX Runtime package, not from Zimantic. Replace the plain `onnxruntime` wheel with `onnxruntime-gpu` (CUDA), `onnxruntime-rocm`, `onnxruntime-directml`, or `onnxruntime-openvino`. If the requested provider is missing or cannot initialize (for example because a CUDA library is absent), Zimantic warns and embeds on the CPU, so the same configuration also runs on machines without a GPU. If a run fails on the device after initialization (for example a GPU out-of-memory error), Zimantic warns and retries on the CPU instead of failing the build.

```bash
pip uninstall -y onnxruntime   # only one onnxruntime distribution provides the module
pip install onnxruntime-gpu    # or onnxruntime-rocm/-directml/-openvino
zimantic build --device cuda   # or embed_device = "cuda" in config.toml
```

When Zimantic is managed with `uv`, `uv sync` reinstalls the plain `onnxruntime` dependency, so install the GPU wheel after syncing (or change the dependency in the project environment).

GPU acceleration helps full index builds the most, because they embed every article; raising `embedding_batch_tokens` (and, if needed, the `batch_size` read window) uses the device more efficiently. Lower the token budget if a call exhausts GPU memory. Query embedding also runs on the device, but a single short query is too small to gain much. Vector search (FAISS) deliberately stays on the CPU: the indexes are memory-mapped, and copying one to GPU memory to search a single query vector would cost more than the search itself.

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

A running `serve` discovers indexes with `zimantic reload`; a fast index remains searchable while a full index builds.

## Source selection

The source picker discovers available sources and sorts matching sources by result count, grouped by collection. Each group shows its top `source_filter_limit` sources (10 by default) plus an **Other** disclosure for the rest, and sourced-filter searches show up to 30 matches plus **Show N more**. Selected sources stay visible even when their group is not expanded. Dated and flavour variants of one book collapse to a single source here and in the library view.

- Selecting a collection header searches that whole collection as one unit. Only one collection group can be selected at a time; selecting another replaces it. Selecting an individual source switches to individual-source display mode and clears the group selection, after which sources from different groups can be combined. The other searched sources stay available in the picker, and the next query keeps the current ranked scope while narrowing the returned page to the selected sources.
- An explicit source or collection selection always defines the search scope directly. There is no small-selection promotion and no `zim` parameter: the canonical API parameter is `source`, and `collection` selects one collection as a unit.
- A source selected outside an unscoped Kiwix fan-out cap is still searched explicitly. An explicitly selected Kiwix collection is bounded by `max_explicit_collection_sources` (500 by default) and the request deadline. Partial scope is reported explicitly rather than claiming that every book completed.
- An unknown source or collection key is rejected with HTTP 400 instead of silently becoming an unscoped search. A known but unsearchable source, or an intersection with no eligible sources, searches no sources rather than broadening the scope.
- A scoped search shows **Searching only in: …** or **Searching collection: …** above the results with a **Search all sources** control, so submitting a new query while a filter is active never silently hides the narrower scope.

## Library view

`/library` shows every configured collection as a group with artwork, descriptions, metadata, and direct links to each source's homepage or random article. A one-source collection renders as a single card; a larger collection shows a header with its source count and media summary plus a bounded preview grid. **Show all sources** opens the group and pages through its remaining sources, so the first source page is never mistaken for the whole collection. **Shuffle** reorders the visible groups and their loaded sources. Dated and flavour variants of one book still collapse to a single source card.

The provider filter (All / ZIM / Files / Kolibri / Kiwix), content-type filter (All / Text / Video / Audio / Images / Other), and text filter compose on the server before paging, so matches beyond the first fetched page remain discoverable. A specific content type matches only a source whose `media_types` contains that family; **All** includes sources with no known family. Collection cards summarize the union of their sources' known types. Prefix text terms with `-` to exclude matches. Snapshot-version changes while a group is loading restart that group instead of mixing records from two discovery revisions.

If no collection is configured, the page says so and points at a `[[collections]]` entry plus a server restart. Collections that exist but discovered no sources show their discovery state or error instead of appearing unconfigured.

Click a source's title or **Details** for an infobox with its thumbnail, homepage, descriptions, counts, license, attribution, relation, tags, provider, collection, media types, and content-type breakdown. The infobox offers **Search** for one source and **Search collection** for its whole collection. No filesystem path is shown.

## Caches

Exact queries use small LRU caches controlled by `cache_size` and `cache_bytes` (32 MiB per cache by default). A ranked pool is keyed by the normalized query, the expanded source keys of the resolved scope, and the publication generation. Paging reuses the same pool. Replacing an index without changing source keys, changing collection membership, or changing discovery metadata invalidates the affected generation, so stale results and previews are not served.

For large libraries, `search_workers` controls how many ZIM indexes are searched in parallel. `max_stream_ranks` limits completion-spaced intermediate ranking passes. Timed snapshots are additionally emitted about every 700 ms so a slow query keeps updating, and the final result still ranks every source. A lower `max_stream_ranks` reduces CPU use on small devices. The automatic profiles use 8 workers and 16 completion-spaced passes on desktops, and 2 workers and 8 passes on low-memory devices.

Native ZIM full-text search uses reusable helper processes, capped at `min(4, search_workers)` per ZIM collection, rather than running libzim in the server's source threads. With libzim 4.1.0, malformed full-text results can abort the interpreter, and reader calls hold the GIL. Helpers both contain those crashes and provide real native-search parallelism: a four-worker benchmark was 3.4–4 times faster than threads, at a higher memory cost. A native crash logs the archive basename and disables that archive's native search until reload; other available channels remain usable. Budget memory for the server and its helpers. See [the architecture rationale and benchmark](architecture.md#native-zim-full-text-search-uses-helper-processes).
