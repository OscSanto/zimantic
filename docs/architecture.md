# Architecture

Zimantic uses one provider-neutral collection schema, one source identity, and
one local content route.

## Providers

- **ZIM** collections use `[[collections]]` entries with `type = "zim"`.
- **Files** collections index and serve a configured directory tree. Child
  source links stay within the collection. Hidden and excluded paths are not
  served.
- **Kolibri** collections read a supported v0.19.5 SQLite installation through
  private, read-only snapshots. Each channel is a collection; topics are
  sources. Captions, OCR, and ASR are independent build-time options.
- **Kiwix** collections search remote books through the `kiwix-serve` API and
  return external links. They do not serve local content.
- The library and source APIs expose `provider`, `collection`, `media_types`,
  `indexed_count`, `truncated`, and `body_truncated_count`. ZIM, Files, and
  Kolibri content use `/content/{key}/{path}`.

Configuration uses `[[collections]]` entries. Source selection
uses canonical source keys and the `source` query parameter. Local content uses
`/content/{key}/...` or `/article/{key}/...`.

## Content safety and operation

- Local stores confine reads to the configured root and reject traversal,
  encoded separators, backslashes, NUL bytes, symlinks, hidden paths, and
  configured excludes. Directory requests do not create listings.
- HTML5 zip files use a validated manifest, bounded members and output, and a
  read deadline. Unsafe paths, links, duplicate members, overlapping members,
  and oversized output fail closed.
- MIME classification uses file extensions and constrained content
  detection. Unknown binaries are attachments. Provider content sends
  `X-Content-Type-Options: nosniff`. A response supports one validated,
  bounded byte range; partial responses do not use gzip.
- Kiwix XML rejects DTD and entity declarations in UTF-8 and UTF-16. Redirect
  origins are validated before the next request.
- Kolibri reads do not write to the installation, WAL/SHM/journal sidecars, or
  configured storage roots. Private snapshots are owner-only and are removed
  after the last reader.
- API payloads, errors, redirects, ETags, and logs above debug level do not
  expose absolute configured roots or private catalog locators.
- Discovery, search, and content failures are operation-scoped. A failed
  refresh keeps the last complete snapshot. A broken source does not stop
  other sources.

- `secure_browsing = true` sandboxes the `/content/` shell iframe. A raw
  `/article/` document opened directly or in a permitted popup is not
  sandboxed. Treat file trees and Kolibri HTML5 apps as untrusted input.

## Native ZIM full-text search uses helper processes

Source scheduling uses threads, but `serve` and `open` run libzim's native
full-text searches in reusable helper processes. This is specific to native
ZIM search, not a requirement to move every search channel into a process.
The helpers cache opened archives and searchers; they do not load the
embedding model or FAISS indexes.

There are two reasons to retain the process boundary:

- Crash containment. Some python-libzim C++ declarations lack `except +`,
  so an exception during result iteration can abort the interpreter instead
  of becoming a catchable Python exception. See
  [openzim/python-libzim#266](https://github.com/openzim/python-libzim/issues/266).
  On November 8, 2026, querying `hawaii` in
  `osm-hawaii-2026-09-26.zim` reproduced an abort with libzim 4.1.0:
  `zim::EntryNotFound: Cannot find entry`. That native message does not name
  the archive. The parent knows which ZIM the helper was searching, logs its
  basename, and disables native search for that archive until reload.
  Other available search channels and the server remain usable.
- Actual parallelism. The tested python-libzim reader bindings hold the
  GIL across native work. Threads therefore do not provide parallel native
  search, even across different archives. Processes do. Threads also cannot
  forcibly stop a stuck native call; a helper can be terminated when its
  search times out.

### Process/thread comparison, November 8, 2026

The comparison used Python 3.13.12, libzim 4.1.0, and Linux with 20 available
logical CPUs. Each configuration had three fresh-process trials, warmed
caches, 192 searches per measured trial, and a result limit of 16. The
12-query mix included common terms, `"new york"`, and a nonexistent term.
The single-archive workload used `wikipedia_en_top_mini_2026-09.zim`; the
four-archive workload added `wikipedia_en_geography_maxi_2026-07.zim`,
`wikisource_en_all_nopic_2026-09.zim`, and
`libretexts.org_en_stats_2026-01.zim`.

The process variant used the existing helper pool. The thread variant shared
one locked searcher per archive and wrote unbuffered begin/end breadcrumbs
containing the thread ID and archive basename. Plain threads performed
similarly. Results agreed across variants. These are native-search
microbenchmarks, not end-to-end web latency or whole-server memory targets.

| Workload | Workers | Processes: searches/s | Threads with breadcrumbs: searches/s |
|---|---:|---:|---:|
| One ZIM | 1 | 82 | 88 |
| One ZIM | 4 | 282 | 83 |
| Four ZIMs | 1 | 46 | 48 |
| Four ZIMs | 4 | 176 | 45 |

Values are medians across the trials. For four ZIMs and four workers,
processes had a 63 ms p95 search duration versus 265 ms for threads.
Durations include backend waits but exclude time queued in the source
executor. Sampled peak proportional memory (PSS), including the benchmark
process and its helpers, was approximately 488 MiB versus 266 MiB.
First-search latency was approximately 50 ms versus 27 ms, with warm OS
caches; this is not cold server startup.

Breadcrumbs plus `faulthandler` successfully identified the Hawaii archive
and crashing thread, but the threaded process still died with SIGABRT.
Diagnostics are not crash containment. Cheap startup discovery and checksum
validation alone cannot prove that every native search query is safe.

### Worker budget

`Search` owns one helper pool shared by every ZIM collection. Runtimes borrow
it, including newly discovered runtimes after reload; closing a collection
does not close the shared pool. Server shutdown stops its helpers and cancels
queued source work without allowing the closed pool to spawn replacements.
Slots are configured even when startup discovery has no usable ZIM sources;
subprocesses start only when native search first uses those slots.
Each helper still caches at most four archive/searcher pairs. Deployment
budgets must include helpers, not just the server's own RSS.

`fulltext_workers` is an independent global setting, not a multiplier per
collection. Its automatic budget uses usable CPU threads, source-executor
capacity, and available RAM, including ancestor cgroup quotas and
memory limits, with a ceiling of eight helpers. The estimate reserves
256 MiB for the server/model and
128 MiB per helper, always permitting at least one helper. This is a
conservative heuristic, not an OOM guarantee for arbitrary archives or models.
An explicit positive integer overrides the automatic budget, including its
CPU, memory, source-thread and eight-helper bounds. Allocating more native
helpers than source threads usually wastes cache memory without increasing
parallelism.
The estimate retains server/model headroom even in `--fast` mode; a
lexical-only deployment can override it after measuring its full memory use.

Resource kills must not masquerade as archive corruption. Native fatal
signals such as SIGABRT and SIGSEGV disable the offending archive's native
search until reload, but SIGKILL/SIGTERM do not blacklist it. Periodic
collection refresh does not reset the shared broken-archive set.

Reconsider the process/thread choice only after testing bindings that both
translate the relevant exceptions and release the GIL; fixing only one of
those leaves the other reason intact. On-demand
[`zimantic diagnose`](usage.md#diagnosing-zim-archives) uses disposable
helpers for checksum and query probes, without delaying startup.

### Constrained worker/budget comparison, November 8, 2026

[`run_native.py`](../tests/benchmarks/run_native.py) ran 132 full-workload trials
over the same four archives and query mix, with eight source threads, 96
warm-up searches and 96 measured searches per trial. Each configuration had
two fresh systemd user services. CPUQuota simulated 1, 2, 4, or 8 usable
CPUs; MemoryMax ranged from 256 MiB to 2 GiB, with swap disabled.
The services were isolated and collected after each run; host caches were
not dropped. This simulates resource budgets, not Raspberry Pi instruction
speed, storage, or complete web-server performance.

The [native-only matrix](../tests/benchmarks/results/native-workers-2026-11-08.json)
used the initial four-helper automatic ceiling:

| CPU quota | MemoryMax | 1 helper: searches/s | 2 helpers | 4 helpers | 8 helpers |
|---|---:|---:|---:|---:|---:|
| 1 CPU | 256 MiB | 47 | 47 | failed | failed |
| 1 CPU | 512 MiB | 48 | 47 | 45 | failed |
| 2 CPUs | 512 MiB | 48 | 94 | 93 | failed |
| 2 CPUs | 1 GiB | 48 | 92 | 89 | 75 |
| 4 CPUs | 1 GiB | 49 | 92 | 169 | 157 |
| 8 CPUs | 2 GiB | 49 | 93 | 175 | 294 |

Values are medians of successful trials. Extra helpers beyond the CPU quota
provided no benefit or reduced throughput, while increasing memory.
Eight helpers on eight CPUs did improve throughput substantially, so the
automatic ceiling increased from four to eight, bounded by CPU, source-thread
capacity, and memory rather than an unconditional eight-process default.
Counts above eight remain explicit overrides, not a measured recommendation.

The [headroom matrix](../tests/benchmarks/results/native-budgets-2026-11-08.json)
then retained 256 MiB of touched anonymous memory to simulate server/model
residency and checked the revised automatic budget:

| CPU quota | MemoryMax | Automatic helpers | Searches/s | Median peak cgroup memory |
|---|---:|---:|---:|---:|
| 1 CPU | 512 MiB | 1 | 48 | 376 MiB |
| 2 CPUs | 512 MiB | 1 | 48 | 390 MiB |
| 4 CPUs | 1 GiB | 4 | 172 | 656 MiB |
| 8 CPUs | 1 GiB | 5 | 211 | 756 MiB |
| 8 CPUs | 2 GiB | 8 | 289 | 1,020 MiB |

All automatic trials completed. Two helpers also worked at 2 CPUs/512 MiB
(93 searches/s), but their approximately 466 MiB cgroup peak left little
room for variable model work, previews, and indexes. The conservative
automatic budget keeps one there. Four/eight helpers failed at 512 MiB,
and eight failed at 1 GiB with the resident allocation.

Sharing also improved utilization at the same total helper count. On
8 CPUs/2 GiB with the resident allocation, one shared eight-helper pool
managed 295 searches/s versus 197 for four per-collection two-helper pools.
The old per-collection allocation failed under smaller limits. Native-only
OOM counters confirmed killed helpers without blacklisting any archive.
When the service itself was killed before completing, the report preserves
that failure rather than inventing throughput or memory measurements.
Successful result digests agreed across both matrices.

Cgroup peaks include all memory charged to the service, but warm cached
archive pages may remain charged elsewhere. Process-tree PSS is recorded
separately. These measurements are a budget heuristic, not a guarantee that
arbitrary archives, models, or concurrent article reads fit the same limits.

### Other limits containing four

The old native process ceiling was a heuristic, not a requirement of libzim
or Python. It is now centralized in the automatic budget and has no duplicate
standalone-pool ceiling. Other fours measure different work and should not
be changed together:

| Setting or limit | Meaning | Current rationale |
|---|---|---|
| `MAX_CACHED_ARCHIVES = 4` | Archive/searcher pairs cached by each helper | Bounds retained handles and memory; the four-archive workload does not establish an optimum for larger libraries, so this LRU cap is not retuned |
| `max_concurrent_searches = 4` on desktop | Admitted user searches, not native processes | Bounds model work, previews, and request backlog; configurable and separate from this benchmark |
| Desktop query embedding threads capped at four | ONNX threads per query | Short-query synchronization tradeoff; `embed_threads` overrides it, and [embedding measurements](usage.md#build-behavior-and-locking) are separate |
| Automatic build worker CPU/RAM divisors of four | Approximately four CPU threads/four GiB per source build | A build resource heuristic, not a four-worker maximum; unrelated to the native-search pool |

## Resource limits and status

Provider limits are configurable per entry. They include `max_sources`,
`max_entries`, `max_file_bytes`, traversal budgets, Kiwix catalog/response/source
limits, and Kolibri snapshot/HTML5-zip limits. Global limits include body size,
derived-cache size, search deadlines, and listing size.

Index truncation uses `truncated` and `indexed_count`. Body-text truncation uses
`body_truncated_count`. A truncated source still serves all permitted content.
See [limitations](limitations.md) for operational constraints and
[benchmark instructions](../tests/benchmarks/README.md) for the Raspberry Pi 4
measurement procedure.
