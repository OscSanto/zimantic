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

Each ZIM runtime lazily starts at most `min(4, search_workers)` helpers.
The default desktop profile therefore permits four; the mobile profile
permits two. Each helper caches at most four archive/searcher pairs.
Multiple ZIM collections have separate pools, so their memory costs can
accumulate. Deployment budgets must include helpers, not just the server's
own RSS.

Keep the current caps: this comparison supports using processes and shows
useful scaling from one to four workers, but does not establish an optimum
at two or eight workers, on low-memory hardware, or for cold storage.
Reducing `search_workers` also reduces other source scheduling, not just
native ZIM concurrency. Reconsider the process/thread choice only after
testing bindings that both translate the relevant exceptions and release
the GIL; fixing only one of those leaves the other reason intact.

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
