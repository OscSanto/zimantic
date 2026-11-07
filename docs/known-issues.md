# Known issues

## Kolibri support is pinned to v0.19.5

The installed Kolibri SQLite schema is recognized by its v0.19.5 tables,
columns, and content migration history.  Other releases and export databases
are reported as unsupported instead of guessed.  Zimantic never migrates,
repairs, or writes the installation.  A live WAL is included in private
snapshots, and a database that changes during the bounded copy fails closed.

## Derived text is optional and local-only

`captions`, `ocr`, and `asr` are independent build-time opt-ins and all default
off.  A provider with no enabled operation reads no caption, image, or media
bytes.  Engines are never loaded by `serve` and no network or hosted service is
used.  Missing local engines make only that operation unavailable.

- Embedded caption tracks require a local `ffmpeg`/`ffprobe` pair.
- OCR requires a local engine and model; pages, pixels, and output are bounded.
- ASR requires a local engine and model; media duration and bytes are bounded.
- A partial requested enrichment publishes the available channels, reports the
  partial state, and exits the build non-zero.
- Auto worker resolution is memory- and CPU-derived.  On a host where one
  worker cannot fit, the operations are reported unavailable rather than
  silently run.

## Secure browsing is shell-scoped

`secure_browsing = true` sandboxes the article iframe inside `/content/`.  A raw
`/article/` document opened directly or in a permitted popup is not sandboxed
and can run with the app origin and persistent browser storage.  It is not a
complete security boundary for untrusted content; use a browser sandbox or
separate profile for hostile file trees.

## Pi 4 release benchmark must be recorded

The Raspberry Pi 4 targets (cold startup under 120 s, mixed-library search p95
under 8 s, peak RSS under 1 GiB) are verified only by a recorded run of
`tests/benchmarks/run_mixed.py` on the documented fixture and hardware.  The
repository ships the fixture generator and harness, but a missing hardware
result is an unverified gate rather than a pass.

## Windows filesystem confinement

Files, Kolibri, and HTML5 zip reads use descriptor-relative no-follow opens on
platforms that provide them.  On platforms without those primitives the local
filesystem stores fail closed rather than silently falling back to a racy
check-then-open path.  Windows x86-64 indexed build/search and reader-lease
coverage remains part of the release matrix.

## Filtering and UI scale

Collections are the grouping unit.  Items never become sources, and the library
uses bounded paged source loads.  A collection that exceeds its configured
source cap collapses to one source with a changed key; rebuild and update any
exact `[[collections.sources]]` override after a collapse or recovery.
