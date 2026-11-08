# Limitations

## Kolibri schema

Zimantic supports the installed Kolibri SQLite schema from v0.19.5. It
identifies the schema from its tables, columns, and content migration history.
Other releases and export databases are unsupported. Zimantic does not migrate,
repair, or write to the installation. Private snapshots include committed WAL
data. A concurrent write during the bounded copy makes the copy fail closed.

## Derived text is optional and local-only

`captions`, `ocr`, and `asr` are independent build-time options. They are off
by default. A provider with no enabled operation does not read caption, image,
or media bytes. Engines run only during a build, not in `serve`. Transcription
runs locally. Network access is required only to download a missing checkpoint.
A missing local engine makes only its operation unavailable.

- Embedded caption tracks require a local `ffmpeg`/`ffprobe` pair.
- OCR requires a local engine and model; pages, pixels, and output are bounded.
- ASR requires a local engine (`whisper-cli` or `whisper-ctranslate2`); a
  missing checkpoint is downloaded and verified on first use. The first build
  needs network access unless the model is already cached.
  Media duration and bytes are bounded.
- A partial enrichment publishes available channels, reports a partial state,
  and exits the build with a non-zero status.
- Automatic worker selection uses memory and CPU resources. If one worker
  cannot fit, the operation is unavailable.

## Secure browsing

`secure_browsing = true` sandboxes the article iframe inside `/content/`. A raw
`/article/` document opened directly or in a permitted popup is not sandboxed.
It can use the app origin and persistent browser storage. This setting is not a
complete security boundary for untrusted content. Use a browser sandbox or a
separate profile for untrusted file trees.

## Raspberry Pi 4 benchmark

The Raspberry Pi 4 targets are cold startup under 120 seconds, mixed-library
search p95 under 8 seconds, and peak RSS under 1 GiB. A recorded run of
`tests/benchmarks/run_mixed.py` on the documented fixture and hardware verifies
these targets. The repository includes the fixture generator and harness. A
missing hardware result is unverified.

## Windows filesystem confinement

Files, Kolibri, and HTML5 zip reads use descriptor-relative no-follow opens on
platforms that support them. On other platforms, local filesystem stores fail
closed; they do not use a race-prone check-then-open path. Windows x86-64
indexed build, search, and reader-lease tests remain in the release matrix.

## Filtering and UI scale

Collections group sources. Items remain within their source. The library loads
sources in bounded pages. If a collection exceeds its source cap, Zimantic
represents it as one source with a different key. Rebuild the index and update
any exact `sources` override that uses the other key.
