# Limitations

## Kolibri schema

Zimantic supports the installed Kolibri SQLite schema from v0.19.5. It identifies the schema from its tables, columns, and content migration history. Other releases and export databases are unsupported. Zimantic does not migrate, repair, or write to the installation. Private snapshots include committed WAL data. A concurrent write during the snapshot copy makes the copy fail closed.

## Derived text is optional and local-only

`captions`, `ocr`, and `asr` are independent build-time options. They are off by default. A provider with no enabled operation does not read caption, image, or media bytes. Engines run only during a build, not in `serve`. Transcription runs locally. Network access is necessary only to download a missing checkpoint. A missing local engine makes only its operation unavailable.

- Embedded caption tracks require a local `ffmpeg`/`ffprobe` pair.
- OCR requires a local engine and model; pages, pixels, and output have limits.
- ASR requires a local engine (`whisper-cli` or `whisper-ctranslate2`); a missing checkpoint is downloaded and verified on first use. The first build needs network access unless the model is already cached. Media duration and bytes have limits.
- A partial enrichment publishes available channels, reports a partial state, and exits the build with a non-zero status.
- Automatic worker selection uses memory and CPU resources. If one worker cannot fit, the operation is unavailable.

## Secure browsing

`secure_browsing = true` sandboxes the article iframe inside `/content/`. A raw `/article/` document opened directly or in a permitted popup is not sandboxed. It uses the app origin and persistent browser storage. This setting is not a complete security boundary for untrusted content. Use a browser sandbox or a separate profile for untrusted file trees.

## Raspberry Pi 4 benchmark

The Raspberry Pi 4 targets are cold startup under 120 seconds, mixed-library search p95 under 8 seconds, and peak RSS under 1 GiB. A recorded run of `../tests/benchmarks/run_mixed.py` on the documented fixture and hardware verifies these targets. The repository includes the fixture generator and harness. A missing hardware result is unverified.

## Windows filesystem confinement

Local file, Kolibri, and HTML5 zip reads use descriptor-relative no-follow opens on platforms that support them. On other platforms, local filesystem stores fail closed; they do not use a race-prone check-then-open path. Windows x86-64 indexed build, search, and reader-lease tests are in the release matrix.

## Filtering and library scale

Collections group sources. Items stay within their source. The library loads sources in pages of limited size. If a collection exceeds its source limit, Zimantic represents it as one source with a different key. Rebuild the index and update any exact `sources` override that uses the other key.

## Hardening and release gates

Zimantic treats configured file trees and Kolibri installations as untrusted input. The provider boundary assumes that a provider fails, reports an unexpected MIME type, or returns a path it must not return.

- Path confinement. Local stores use race-safe, root-confined, no-follow opens and reject symlinks, `..`, encoded separators, backslashes, NUL bytes, absolute paths, hidden paths, and configured excludes. A directory without an index page serves a bounded generated listing that omits those entries; the listing is not an arbitrary filesystem view.
- Zip bounds. Zimantic validates Kolibri HTML5 zips from their end record and central directory before it reads any member. Absolute, `..`, and link members, duplicate or overlapping entries, oversized output, and zip bombs fail closed. Per-entry member, directory, total, and read-time limits are configurable.
- MIME and responses. Extension mapping comes first, followed by constrained sniffing. Unknown binaries are attachments, never `text/html`. Provider content sends `X-Content-Type-Options: nosniff`; SVG is sandboxed. A request presents one validated, limited byte range, and partial responses are never gzip-compressed.
- Kiwix network boundary. Zimantic rejects XML DTD/entity declarations in UTF-8 and UTF-16 before expansion. It checks redirect origins before the next request, limits remote catalogs, and keeps the last complete catalog when a refresh fails.
- Kolibri isolation. Zimantic never writes the installed database and its WAL/SHM/journal sidecars. Queries use a private owner-only snapshot that includes committed WAL data; unsupported schemas, PostgreSQL installations, changing databases, and malformed mappings fail closed.
- Private diagnostics. Absolute configured roots and private catalog locators do not appear in API payloads, errors, redirects, ETags, or logs above debug level. Raw exception details are debug-only.
- Failure isolation. Discovery, search, and content failures are operation-scoped. A failed refresh keeps the last complete snapshot; one broken source does not hide or crash another provider.
- Observability. `zimantic sources --json`, `/collections.json`, and `/health` agree on collection/source counts, discovery state, index counts and fast/full state, last refresh time, collapse, and truncation metadata. Build logs report provider, collection, source, indexed/skipped counts, truncation, `body_truncated_count`, duration, and failure cause.

Resource limits are documented in [Resource limits](#resource-limits). The mixed-library release fixture and Pi 4 benchmark harness live under `../tests/fixtures/mixed_library/` and `../tests/benchmarks/`. A result recorded on the documented hardware is required; a missing result is unverified, not a pass.

See [Architecture](architecture.md) for the provider model.

### Resource limits

| Limit | Default | Enforcement |
|---|---|---|
| Sources per collection | 64 | hierarchical providers collapse; fixed-identity providers use paging/fan-out limits |
| Sources overall | 512 | advisory target; never silently drop sources |
| Entries per source | files 100,000 / Kolibri 500,000 / ZIM configurable | indexing reports `truncated`/`indexed_count` |
| Bytes per indexed item | `max_entry_bytes` | extraction |
| Indexed body text per item | `max_body_text_bytes` (1 MiB) | lexical text; reports `body_truncated_count` |
| Published index size per source | <10% of original source bytes | advisory optimization goal for SQLite + FAISS; never truncate content or reject a build to meet it |
| Staged index replacement | active + replacement + reserve | abort on storage errors; keep last good generation |
| Derived preview cache per source | `max_derived_cache_bytes_per_source` (512 MiB) | omit previews at the limit; searchable postings stay available |
| Kiwix catalog entries | `max_catalog_entries` (50,000) | oversized/unpageable catalog keeps the last good snapshot |
| Caption bytes | 1 MiB (`max_caption_bytes`) | caption parser |
| Kolibri HTML5 zip | 10k members / 8 MiB directory / 256 MiB member / 1 GiB total / 10 s | manifest validation and reads with limits |
| OCR pages / pixels / output | 20 / 50 MP / 256 KiB | OCR stage and image guard |
| ASR media | 1 h / 1 GiB | ASR stage |
| Derived-text workers | `"auto"` | memory/CPU-derived; unavailable if none fits |
| Discovery time per provider | `search_timeout_seconds` (30 s) | serving starts before optional/remote discovery completes |
| Entries/traversal time | 1,000,000 / 300 s | per-provider walk with limits |
| Search timeout | `search_timeout_seconds` (30 s) | admitted search scheduling and ranking; the partial pool is reused for `partial_cache_seconds` (60 s). Preview extraction gets its own separate budget of the same length after ranking. |
| Listing page size | `page_size`, maximum 200 for `/sources` and `/collections.json`, 1000 for `/library.json` | listing JSON |

A provider that reaches a limit logs one time and degrades; index truncation never truncates serving. A traversal budget that prevents a complete discovery is an explicit partial/error state, not a complete prefix.
