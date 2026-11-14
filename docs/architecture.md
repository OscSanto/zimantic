# Architecture

Zimantic uses one provider-neutral collection schema, one source identity, and one local content route.

## Providers

- ZIM collections use `[[collections]]` entries with `type = "zim"`.
- Files collections index and serve a configured directory tree. Child source links stay within the collection. Zimantic does not serve hidden and excluded paths.
- Kolibri collections read a supported v0.19.5 SQLite installation through private, read-only snapshots. Each channel is a collection; topics are sources. Captions, OCR, and ASR are independent build-time options.
- Kiwix collections search remote books through the `kiwix-serve` API and return external links. They do not serve local content.
- The library and source APIs expose `provider`, `collection`, `media_types`, `indexed_count`, `truncated`, and `body_truncated_count`. ZIM, Files, and Kolibri content use `/content/{key}/{path}`.

Configuration uses `[[collections]]` entries. Source selection uses canonical source keys and the `source` query parameter. Local content uses `/content/{key}/...` or `/article/{key}/...`.

## Content safety and operation

- Local stores confine reads to the configured root and reject traversal, encoded separators, backslashes, NUL bytes, symlinks, hidden paths, and configured excludes. A directory without an index page opens a bounded generated listing that omits hidden, excluded, and symlinked entries.
- HTML5 zip files use a validated manifest, member and output limits, and a read deadline. Unsafe paths, links, duplicate members, overlapping members, and oversized output fail closed.
- MIME classification uses file extensions and constrained content detection. Unknown binaries are attachments. Provider content sends `X-Content-Type-Options: nosniff`. A response supports one validated, size-limited byte range; partial responses do not use gzip.
- Kiwix XML rejects DTD and entity declarations in UTF-8 and UTF-16. Zimantic validates redirect origins before the next request.
- Kolibri reads do not write to the installation, WAL/SHM/journal sidecars, or configured storage roots. Private snapshots are owner-only and are removed after the last reader.
- API payloads, errors, redirects, ETags, and logs above debug level do not expose absolute configured roots or private catalog locators.
- Discovery, search, and content failures are operation-scoped. A failed refresh keeps the last complete snapshot. A broken source does not stop other sources.
- `secure_browsing = true` sandboxes the `/content/` shell iframe. A raw `/article/` document opened directly or in a permitted popup is not sandboxed. Treat directory trees and Kolibri HTML5 apps as untrusted input.

## Native ZIM full-text search uses helper processes

Source scheduling uses threads, but `serve` and `open` run libzim's ZIM full-text searches in reusable helper processes by default. This design is specific to ZIM full-text search, not a requirement to move every search channel into a process. The helpers cache opened ZIM files and searchers; they do not load the embedding model or FAISS indexes.

An opt-in `libzim_search_backend = "threads"` uses the same server-wide worker budget with independent ZIM-file/searcher handles per slot. It requires GIL-releasing reader bindings; each worker caches at most four `(path, version)` pairs. Native search objects do not escape their worker. The thread backend never reuses a timed-out slot until its native call returns. Closing it rejects further work and cancels pending jobs, but it cannot interrupt a running native call or promise immediate interpreter exit. Both backends avoid unrequested snippets when the binding supports `include_snippets=False`.

There are two reasons to keep the process boundary:

- Crash containment. Some python-libzim C++ declarations lack `except +`, so an exception during result iteration aborts the interpreter instead of becoming a catchable Python exception. See [openzim/python-libzim#266](https://github.com/openzim/python-libzim/issues/266). A query for `hawaii` in `osm-hawaii-2026-09-26.zim` aborts the interpreter with libzim 4.1.0: `zim::EntryNotFound: Cannot find entry`. That native message does not name the ZIM file. The parent knows which ZIM file the helper searches, logs its basename, and disables ZIM full-text search for that ZIM file until reload. Other available search channels and the server stay usable.
- Actual parallelism with the released python-libzim 4.1.0 binding. Its reader bindings hold the GIL across native work. Threads therefore do not provide parallel ZIM full-text search, even across different ZIM files. Processes do. Threads also cannot forcibly stop a stuck native call; a helper can be terminated when its search times out.

### Process/thread comparison, November 8, 2026

The comparison uses Python 3.13.12, libzim 4.1.0, and Linux with 20 available logical CPUs. Each configuration has three fresh-process trials, warmed caches, 192 searches per measured trial, and a result limit of 16. The 12-query mix includes common terms, `"new york"`, and a nonexistent term. The single-ZIM-file workload uses `wikipedia_en_top_mini_2026-09.zim`; the four-ZIM-file workload includes `wikipedia_en_geography_maxi_2026-07.zim`, `wikisource_en_all_nopic_2026-09.zim`, and `libretexts.org_en_stats_2026-01.zim`.

The process variant uses the helper pool. The thread variant shares one locked searcher per ZIM file and writes unbuffered begin/end breadcrumbs with the thread ID and ZIM file basename. Plain threads perform similarly. Results agree across variants. These are ZIM full-text microbenchmarks, not end-to-end web latency or whole-server memory targets.

| Workload | Workers | Processes: searches/s | Threads with breadcrumbs: searches/s |
|---|---:|---:|---:|
| One ZIM file | 1 | 82 | 88 |
| One ZIM file | 4 | 282 | 83 |
| Four ZIM files | 1 | 46 | 48 |
| Four ZIM files | 4 | 176 | 45 |

Values are medians across the trials. For four ZIM files and four workers, processes have a 63 ms p95 search duration versus 265 ms for threads. Durations include backend waits but exclude time queued in the source executor. Sampled peak proportional memory (PSS), including the benchmark process and its helpers, is approximately 488 MiB versus 266 MiB. First-search latency is approximately 50 ms versus 27 ms, with warm OS caches; this is not cold server startup.

Breadcrumbs plus `faulthandler` identify the Hawaii ZIM file and crashing thread, but the threaded process dies with SIGABRT. Diagnostics are not crash containment. Cheap startup discovery and checksum validation alone cannot prove that every ZIM full-text query is safe.

### Worker budget

`Search` owns one helper pool shared by every ZIM collection. Runtimes borrow it, including runtimes discovered after reload; closing a collection does not close the shared pool. Server shutdown stops its helpers and cancels queued source work without allowing the closed pool to spawn replacements. Slots are configured even when startup discovery has no usable ZIM sources; subprocesses start only when ZIM full-text search first uses those slots. Each helper caches at most four ZIM-file/searcher pairs. Deployment budgets include helpers, not only the server's RSS.

`libzim_search_processes` is an independent global setting, not a multiplier per collection. Its automatic budget uses usable CPU threads, source-executor capacity, and available RAM, including ancestor cgroup quotas and memory limits, with a ceiling of eight helpers. The estimate reserves 256 MiB for the server/model and 128 MiB per helper, and it always permits at least one helper. This is a conservative heuristic, not an OOM guarantee for arbitrary ZIM files or models. An explicit positive integer overrides the automatic budget, including its CPU, memory, source-thread and eight-helper limits. Allocation of more native helpers than source threads usually wastes cache memory without an increase in parallelism. The estimate keeps server/model headroom even in `--fast` mode. A lexical-only deployment overrides it after measuring its full memory use.

All hardware profiles use the same serving defaults. `source_query_threads` budgets one shared source/preview thread pool from usable logical CPUs, limited to eight. `onnx_query_threads` budgets threads inside a query embedding from usable physical cores, leaving one and limited to four. These are separate from native subprocess memory and user-request admission. Profile selection and build budgeting use the same affinity/cgroup-aware hardware detector. Normal configs omit these counts. See [advanced resource overrides](usage.md#advanced-resource-overrides).

Resource kills do not masquerade as ZIM-file corruption. Native fatal signals such as SIGABRT and SIGSEGV disable the offending ZIM file's ZIM full-text search until reload, but SIGKILL/SIGTERM do not blacklist it. Periodic collection refresh does not reset the shared broken-ZIM-file set.

Reconsider the default process/thread choice only after testing bindings that both translate the relevant exceptions and release the GIL; a fix for only one of those leaves the other reason intact. On-demand [`zimantic diagnose`](usage.md#diagnosing-zim-files) uses disposable helpers for checksum and query probes, without delay to startup.

### Constrained worker/budget comparison, November 8, 2026

[`run_native.py`](../tests/benchmarks/run_native.py) runs 132 full-workload trials over the same four ZIM files and query mix, with eight source threads, 96 warm-up searches and 96 measured searches per trial. Each configuration has two fresh systemd user services. CPUQuota simulates 1, 2, 4, or 8 usable CPUs; MemoryMax ranges from 256 MiB to 2 GiB, with swap disabled. The harness isolates and collects the services after each run; it does not drop host caches. This simulates resource budgets, not Raspberry Pi instruction speed, storage, or complete web-server performance.

The native-only matrix (regenerate with [`run_native.py`](../tests/benchmarks/run_native.py)) uses a four-helper automatic ceiling. Result files are local artifacts and are not retained in this repository:

| CPU quota | MemoryMax | 1 helper: searches/s | 2 helpers | 4 helpers | 8 helpers |
|---|---:|---:|---:|---:|---:|
| 1 CPU | 256 MiB | 47 | 47 | failed | failed |
| 1 CPU | 512 MiB | 48 | 47 | 45 | failed |
| 2 CPUs | 512 MiB | 48 | 94 | 93 | failed |
| 2 CPUs | 1 GiB | 48 | 92 | 89 | 75 |
| 4 CPUs | 1 GiB | 49 | 92 | 169 | 157 |
| 8 CPUs | 2 GiB | 49 | 93 | 175 | 294 |

Values are medians of successful trials. Extra helpers beyond the CPU quota provide no benefit or reduce throughput, while they increase memory. Eight helpers on eight CPUs improve throughput substantially. The automatic budget therefore allows up to eight helpers, limited by CPU, source-thread capacity, and memory rather than an unconditional eight-process default. Counts above eight are explicit overrides, not a measured recommendation.

The headroom matrix (also from `run_native.py`) retains 256 MiB of touched anonymous memory to simulate server/model residency and checks the automatic budget:

| CPU quota | MemoryMax | Automatic helpers | Searches/s | Median peak cgroup memory |
|---|---:|---:|---:|---:|
| 1 CPU | 512 MiB | 1 | 48 | 376 MiB |
| 2 CPUs | 512 MiB | 1 | 48 | 390 MiB |
| 4 CPUs | 1 GiB | 4 | 172 | 656 MiB |
| 8 CPUs | 1 GiB | 5 | 211 | 756 MiB |
| 8 CPUs | 2 GiB | 8 | 289 | 1,020 MiB |

All automatic trials complete. Two helpers also work at 2 CPUs/512 MiB (93 searches/s), but their approximately 466 MiB cgroup peak leaves little room for variable model work, previews, and indexes. The conservative automatic budget keeps one there. Four/eight helpers fail at 512 MiB, and eight fail at 1 GiB with the resident allocation.

Sharing also improves utilization at the same total helper count. On 8 CPUs/2 GiB with the resident allocation, one shared eight-helper pool manages 295 searches/s versus 197 for four per-collection two-helper pools. Per-collection allocation fails under smaller limits. Native-only OOM counters confirm killed helpers without blacklisting any ZIM file. When the service itself is killed before completion, the report preserves that failure rather than inventing throughput or memory measurements. Successful result digests agree across both matrices.

Cgroup peaks include all memory charged to the service, but warm cached ZIM-file pages can be charged elsewhere. Process-tree PSS is recorded separately. These measurements are a budget heuristic, not a guarantee that arbitrary ZIM files, models, or concurrent article reads fit the same limits.

### Other limits containing four

The native process ceiling is a heuristic, not a requirement of libzim or Python. It is centralized in the automatic budget and has no duplicate standalone-pool ceiling. Other fours measure different work and do not vary together:

| Setting or limit | Meaning | Rationale |
|---|---|---|
| `MAX_CACHED_ARCHIVES = 4` | ZIM-file/searcher pairs cached by each helper | Limits retained handles and memory; the four-ZIM-file workload does not establish an optimum for larger libraries, so this LRU limit stays fixed |
| `max_concurrent_searches = 4` on desktop | Admitted user searches, not native processes | Limits model work, previews, and request backlog; configurable and separate from this benchmark |
| Query embedding threads limited to four across profiles | ONNX threads per query | Short-query synchronization trade-off; `onnx_query_threads` overrides it, and [embedding measurements](usage.md#build-behavior-and-locking) are separate |
| Automatic build worker CPU/RAM divisors of four | Approximately four CPU threads/four GiB per source build | A build resource heuristic, not a four-worker maximum; unrelated to the ZIM full-text pool |

### Reader GIL/thread comparison, November 9, 2026

The raw results and median summaries from the 81-trial run are not retained in this repository; regenerate them with `tests/benchmarks/run_native.py`. The run contains 81 successful fresh-service trials. Every repeated batch returns the same path/title digest. The workload covers astronomy and medicine Wikipedia ZIM files and Cooking Stack Exchange, twelve fixed queries, and sixteen hits. Each trial warms 144 searches, then repeats the 144-search batch for at least three seconds. Each configuration has three repetitions.

The desktop is an i5-13600KF with 20 logical CPUs and 32 GiB RAM. Its service has `CPUQuota=2000%`, `MemoryMax=8192M`; the Pi-like service has `CPUQuota=400%`, `MemoryMax=2048M`. Both disable swap and retain a touched 512 MiB allocation to represent server/model headroom. This is a CPU-time/RAM budget proxy, not ARM/Pi clock or storage emulation.

The controlled baseline uses python-libzim `57555c2` and zimantic `cb261c5`, built against the same patched native libzim `ad888b4` stack as the measured variants: libzim 9.8.2, Xapian 1.4.31, ICU 78.3, Python 3.13.12, Cython 3.3.1 and GCC 16. The native database-lock correction is present in both controlled variants. Separate installed-binding trials in the report use earlier native dependencies and give similar baseline throughput. Installed distribution metadata alone does not identify a `PYTHONPATH`-overridden extension; the report records source provenance.

Values below are medians, with eight source/native workers for desktop and four for the Pi-like confirmation. CPU includes the driver and all workers; PSS is the sampled peak of the entire process tree, including the 512 MiB reserve. One search is one query against one ZIM file, not one user request.

| Profile | Variant | Searches/s | CPU ms/search | CPU cores used | Peak PSS MiB | p95 ms |
|---|---|---:|---:|---:|---:|---:|
| Desktop | Baseline reader, processes | 315 | 23.898 | 7.48 | 1,484 | 50.42 |
| Desktop | GIL-releasing reader, processes | 7,543 | 0.703 | 5.30 | 644 | 1.83 |
| Desktop | GIL-releasing reader, threads | 9,197 | 0.574 | 5.25 | 562 | 1.40 |
| Four-CPU/2 GiB proxy | Baseline reader, processes | 183 | 21.137 | 3.85 | 1,069 | 46.87 |
| Four-CPU/2 GiB proxy | GIL-releasing reader, processes | 5,373 | 0.665 | 3.58 | 596 | 1.48 |
| Four-CPU/2 GiB proxy | GIL-releasing reader, threads | 7,904 | 0.449 | 3.54 | 556 | 0.99 |

Avoidance of discarded snippets dominates the gain: the GIL-releasing process backend also benefits. Against GIL-releasing processes on the identical stack, threads add 22% desktop throughput and 47% Pi-proxy throughput, while they use less CPU per search and less memory. This is not a 29-43x pure GIL speedup.

The main matrix tests one, two, four and eight native slots with eight source threads. Eight native threads are fastest on desktop; four are fastest under the four-CPU quota. Eight threads under that quota reach only 6,904 searches/s versus 7,601 with four and eight source callers. A reduction of the Pi-proxy source pool to four improves its median to 7,904 searches/s and reduces queue latency. These are the best tested ZIM full-text search settings, not a global optimum:

| Override | Desktop | Pi-like 2 GiB budget |
|---|---|---|
| `libzim_search_backend` | `"threads"` | `"threads"` |
| `libzim_search_processes` | `8` | `4` |
| `source_query_threads` | `8` | `4` |

Threads are opt-in and require the patched binding and native mutex fix. These overrides do not change ONNX, FAISS, previews, request admission or build workers. Full-server model/index residency is not measured. OS caches stay warm, file pages can be charged outside the service cgroup, and PSS is sampled, so neither the 2 GiB limit nor this reserve proves whole-server Pi viability.

### Snippet prefetch comparison, November 9, 2026

The [snippet runner](../tests/benchmarks/run_snippets.py) compares deferred previews with fetching native snippets or identical HTML leads for the first four candidates per source. It also checks fetching all sixteen native snippets. These are benchmark-only alternatives. Production search uses deferred previews.

The three ZIM files and twelve queries are the same as the reader comparison above. Every query searches all three ZIM files, ranks their candidates with the production ranker, and hydrates only the displayed ten. One query means one search across the library, **not one ZIM-file search**. There are 75 successful fresh-service trials, three repetitions per configuration, and 9,408 measured queries. Exact candidate/ranked identities agree across strategies; eager HTML leads and deferred previews return identical text. All trials use the same local GIL-releasing reader/native build: libzim 9.8.2, Xapian 1.4.31, ICU 78.3, and the production Lexbor preview reader.

The single-client report (regenerate with `tests/benchmarks/run_snippets.py`; result files are not retained in this repository) uses four native/source workers, a four-CPU/2 GiB quota, and 512 MiB of simulated residency. Values below are medians of the three process-backend trials. The miss case never reuses previews; the hit case has every displayed path cached, but it bypasses the finalized-query cache.

| Strategy | Miss queries/s | CPU ms/query | Miss p50 ms | Miss p95 ms | Search/rank p50 ms | Cached-preview p50 ms |
|---|---:|---:|---:|---:|---:|---:|
| Deferred previews | 19.1 | 53.3 | 54.6 | 72.8 | 2.5 | 2.5 |
| First four native snippets/source | 23.9 | 63.4 | 34.4 | 85.3 | 29.6 | 30.4 |
| All sixteen native snippets/source | 9.7 | 182.7 | 110.8 | 159.3 | 110.8 | 110.8 |
| First four identical HTML leads/source | 28.2 | 71.6 | 36.1 | 54.5 | 27.5 | 28.3 |

Selective native prefetch improves uncached median latency, but it increases CPU and tail latency and postpones source completion. With cached previews, throughput drops from 383 to 29 queries/s. Thread results have the same native trade-off: 19.2 to 22.8 uncached queries/s, but 475 to 28 with cached previews. Eager HTML leads on threads instead regress from 19.2 to 14.4 uncached queries/s.

The concurrent report (from `run_snippets.py`) uses four clients, eight native/source workers, the desktop's twenty-CPU/8 GiB quota, the same reserve, and preview misses:

| Backend | Strategy | Queries/s | CPU ms/query | p50 ms | p95 ms |
|---|---|---:|---:|---:|---:|
| Processes | Deferred | 18.8 | 54.9 | 199.1 | 382.0 |
| Processes | First four native snippets/source | 65.8 | 73.0 | 49.3 | 112.2 |
| Processes | First four identical HTML leads/source | 63.4 | 84.3 | 60.4 | 98.4 |
| Threads | Deferred | 18.4 | 55.5 | 209.8 | 343.2 |
| Threads | First four native snippets/source | 55.6 | 77.5 | 62.0 | 125.0 |
| Threads | First four identical HTML leads/source | 13.6 | 74.1 | 290.8 | 408.3 |

Here selective prefetch wins substantially: native snippets yield 3.5x process-backend throughput, and identical HTML leads yield 3.4x. This is parallel preview work, not free text that the search query already retrieved. Native snippet generation runs outside the GIL; normal HTML preview traversal executes Python. Moving that traversal into separate helper processes also scales, whereas moving it into native-worker threads does not.

The single-ZIM-file control (from `run_snippets.py`) searches astronomy alone, with one client and four threads under the four-CPU quota. Deferred previews reach 24.1 queries/s versus 21.3 with four prefetched native snippets and 14.4 with sixteen. Four-snippet prefetch roughly preserves median latency (43.7 versus 42.4 ms), but it worsens p95 from 54.8 to 103.5 ms. Thus the multi-source uncached improvement is not universal.

Across the three-source miss cases, four-per-source prefetch generates eleven previews per query on average and reuses 7.42: about 81% of displayed previews, with 33% of prefetches wasted. Fetching sixteen generates 44 and reuses 9.17, which wastes 79%. Every prefetch is wasted in the fully cached-preview case. Native snippets also change the displayed text: this workload averages 583 characters with selective native reuse and 498 with all-native reuse, versus 943 for normal leads. They are query-centered fragments, not equivalent opening-text previews. They do not go in the query-independent preview cache without changing its key.

Recommendation: deferred previews are the default. Production search does not use unconditional native snippets. Selective worker-side work may help uncached concurrent searches. A more targeted alternative sends only displayed, cache-missing paths to preview workers after ranking. It preserves the existing lead text without speculative reads and is not benchmarked here.

These are native-only search/preview pipeline results, not full hybrid-search or HTTP measurements. They omit SQLite title/body channels, semantic models, phrase enrichment, and finalized-query caching; an exact-query cache hit skips ZIM full-text search in every strategy. The two preview-cache extremes do not establish a real hit rate or break-even point. Host caches stay warm, quotas are not Pi emulation, and memory accounting has the caveats above. Eager process PSS additionally includes the larger benchmark-worker bootstrap, so it does not isolate a production snippet implementation's memory overhead. No OOM kills occurred.

## Resource limits and status

Provider limits are configurable per entry. They include `max_sources`, `max_entries`, `max_file_bytes`, traversal budgets, Kiwix catalog/response/source limits, and Kolibri snapshot/HTML5-zip limits. Global limits include body size, derived-cache size, search deadlines, and listing size.

Index truncation uses `truncated` and `indexed_count`. Body-text truncation uses `body_truncated_count`. A truncated source serves all permitted content. See [limitations](limitations.md) for operational constraints and [benchmark instructions](../tests/benchmarks/README.md) for the Raspberry Pi 4 measurement procedure.
