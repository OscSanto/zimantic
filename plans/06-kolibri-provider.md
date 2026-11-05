# Phase 06 — Kolibri provider

Status: proposed.

## Goal

Search and serve a local Kolibri installation, with each **channel as one
collection** and its searchable subtrees as sources. Read Kolibri's local
database and storage directly; do not require Kolibri to be running. Transcripts
are opt-in and are never generated.

## Locked model

- **Channel = collection.**
- **Source = searchable unit and content root inside the channel.** By default,
  a source is a top-level topic subtree. If a channel has no usable topics, it
  gets one source covering the whole channel.
- **Item = content node with local files** (video, audio, document, PDF, HTML5
  app, exercise, etc.). Topic nodes are grouping, not result rows.
- `granularity` is not exposed per file. Per-node sources are not supported.

## Current state

There is no Kolibri code today. This provider builds on phases 00, 03, and 04:
it supplies discovery metadata, a `ContentStore`, and an `EntryProducer`.

## Configuration

```toml
[[sources]]
type = "kolibri"
name = "kolibri"                  # unique collection namespace
path = "/library/kolibri"         # reads content/database/content.db + content/storage
label = "Kolibri"
source_depth = 1                  # top-level topic depth; 0 = whole channel
max_sources = 64
max_entries = 500000
transcripts = false               # index existing captions/subtitles only
max_transcript_bytes = 1048576
```

Optional overrides when the installation is non-standard:

```toml
database = "/library/kolibri/content/database/content.db"
storage = "/library/kolibri/content/storage"
```

Rules:

- The database is opened read-only (`mode=ro`, `PRAGMA query_only=ON`). Never
  write, migrate, vacuum, or checkpoint it.
- `path`/`database`/`storage` must be local. No network access.
- A missing database or storage dir disables the collection with a clear error
  and does not crash startup.
- Multiple Kolibri installations are allowed via multiple `[[sources]]` entries
  with distinct `name` values.
- `path` and absolute database/storage paths never appear in API payloads.

## Discovery

Create `zimantic/providers/kolibri/`:

```
provider.py
discovery.py       # database queries -> collections/sources
schema.py          # version detection and table/column mapping
entries.py         # nodes -> EntryRecord
store.py           # node/files -> content
transcripts.py     # VTT/SRT parsing (opt-in)
```

### Database access

- Detect the schema version before querying. Kolibri schema changes across
  releases; keep every table/column name in `schema.py`, not scattered through
  the provider.
- If the schema is unsupported, expose the collection as unavailable with an
  error such as "unsupported Kolibri content database version"; do not guess.
- Queries must be bounded and indexed by channel/parent. Never `SELECT *` from
  the whole node table into memory.
- Treat duplicate content IDs and orphaned files as normal; skip and log at
  debug level.
- Do not hold a long-lived write transaction or block Kolibri's writers.

The expected core tables are `content_channelmetadata`, `content_contentnode`,
`content_file`, and `content_localfile`. Confirm names and columns against each
supported Kolibri release and record the tested versions in the provider
docstring.

### Collection and source mapping

- Collection key: `kolibri:<name>`.
- One collection per channel in the database.
- Sources:
  - `source_depth = 1` (default): each top-level topic node under the channel
    root becomes a source; leaf nodes directly under the root form one
    "Ungrouped" source.
  - `source_depth = 0`: one source for the whole channel.
  - Values greater than 1 are rejected for now; deeper depth requires a later
    contract change rather than silently changing source identity.
- Source key: `kolibri:<name>:<channel_id>:<node_id>` (opaque, URL-safe).
- Source label: topic title. Source title may include the channel title for
  disambiguation.
- Source `homepage`: the topic node's first browsable child, or `None`.
- `content_id` = source key; paths inside it address only nodes/files belonging
  to that source.
- Caps:
  - if sources exceed `max_sources`, collapse to one source covering the whole
    channel and log the collapse;
  - if nodes exceed `max_entries`, index up to the cap and mark
    `truncated: true`; serving still works for all nodes.
- `available` = database + storage readable; `searchable` = an index exists.
- `article_count` = local content-node count from a cheap `COUNT` when possible.
- `indexed_count` = nodes actually present in the index; `truncated` is true
  when the source exceeds `max_entries`, and the reduced search coverage is
  reported in health/API metadata.

### Channel and source icons

- Use the channel thumbnail from the database when the file exists in storage.
- Return `None` rather than generating or crawling an icon.

## Indexing

`KolibriEntryProducer`:

- Enumerates **leaf content nodes with at least one locally available file**.
  Exclude topic/exercise nodes that have no readable body unless they have a
  description worth indexing; document the final rule.
- Stable id: the Kolibri content node id when it fits the integer rowid; otherwise
  derive a deterministic 64-bit id from `(channel_id, node_id)` and keep a
  mapping in the producer. Ids must be stable across builds.
- `EntryRecord.title` = node title; `path` = synthetic stable path
  `node/<node_id>`; `target = None`.
- Excerpt sources, in order:
  1. node description / coach content that is safe to index;
  2. document text (HTML/PDF/plain) when the file is a readable document;
  3. transcript text when `transcripts = true` and a caption/subtitle file
     exists.
- Do not read video/audio bytes for indexing. Do not generate transcripts.
- HTML5 apps: index their entry HTML/description, not every bundled asset.
- Fingerprint: database file size/mtime + channel/node counts + storage root
  signature (mtime of the storage dir is not enough; use the DB version and a
  bounded manifest of selected files). A changed fingerprint rebuilds.
- `max_transcript_bytes` bounds transcript parsing.

### Transcripts (opt-in)

- `transcripts = false` by default; a collection can enable it.
- Discover existing caption/subtitle files associated with video/audio nodes
  (`*.vtt`, `*.srt`, and Kolibri's stored caption files) through the DB/file
  mapping, not a recursive storage walk.
- Parse to plain text: strip cue numbers, timestamps, styling, and duplicate
  lines; cap by `max_transcript_bytes`.
- Malformed transcripts are skipped with a debug log; they must not fail the
  build.
- Do not call any speech-to-text service or model.

## Content store

`KolibriContentStore` maps a synthetic path to a local file or HTML5 entry.
The source-level `content_id` is the URL root; node identifiers are deliberately
named separately:

- `node/<node_id>` — a document/PDF/plain file, HTML5 entry page, or media
  file.
- `node/<node_id>/<relative asset path>` — assets inside an HTML5 app.
- `channel/<channel_id>/<file>` — channel icon/thumbnail, if needed.
- Paths are opaque and stable. Never expose the storage path.

Handling:

- Documents/PDF/text: serve inline with the correct MIME.
- Video/audio: serve inline with byte ranges (phase 03); do not load the whole
  file for a range request.
- HTML5 apps:
  - support both extracted app directories and zipped apps if the installed
    Kolibri version uses them;
  - resolve the entry file from the DB or the zip manifest;
  - serve relative assets under the same `content_id`;
  - never traverse outside the app's extracted directory or zip.
- Missing local files: `ArticleNotFoundError` for reads; mark the node
  unavailable during discovery. Do not return a different node's bytes.
- External/on-device resources: only local files are served; external URLs are
  not proxied.
- Range and size limits follow phase 03.
- HTML is rendered through the existing content shell and secure browsing
  switch.

### Random

- Prefer the indexed manifest (searchable nodes), then a bounded random sample
  of locally available leaf nodes. Never scan the entire storage tree per
  request.

## Build and reload behavior

- `zimantic build` (phase 08) builds each configured Kolibri collection/source
  with the phase-04 pipeline.
- `reload` re-reads channel/source metadata and picks up new indexes.
- Adding/removing a channel changes the collection set and clears the relevant
  source caches.
- Kolibri is expected to mutate its database outside Zimantic; the provider
  must handle a concurrently changing DB by opening read-only, catching
  "database is locked"/"schema changed" errors, and retrying discovery on the
  next reload rather than serving stale partial data forever.

## Work items

- [ ] Create `zimantic/providers/kolibri/` with the modules above.
- [ ] Add provider-level validation for normalized Kolibri configuration;
      reject `source_depth > 1` until a deeper-source contract exists.
- [ ] Implement read-only schema detection and a minimal query layer.
- [ ] Implement channel discovery, source-depth mapping, cap collapse, and icons.
- [ ] Implement `KolibriEntryProducer` with stable ids and fingerprinting.
- [ ] Implement document/HTML/PDF extraction; verify which Kolibri file kinds
      are readable and skip the rest.
- [ ] Implement opt-in VTT/SRT parsing and transcript indexing.
- [ ] Implement `KolibriContentStore`, including HTML5 asset resolution and
      media ranges.
- [ ] Add source metadata: `provider`, `collection`, `content_id`,
      `searchable`, `available`, `article_count`, `indexed_count`,
      `truncated`.
- [ ] Document supported Kolibri versions and the read-only guarantee.
- [ ] Add a annotated example config for IIAB's `/library/kolibri` layout.

## Tests

Build a fixture Kolibri root:

```
kolibri/content/database/content.db
kolibri/content/storage/<channel>/<node>/...
```

with:

- two channels, one with topics and leaves, one with only leaves;
- video/audio with and without `.vtt`/`.srt`;
- PDF, HTML document, extracted HTML5 app, zipped HTML5 app;
- missing file, duplicate/orphan node, external-only node, unknown schema row;
- a channel with more topics than `max_sources`.

Assertions:

- [ ] Channel = collection; top-level topics = sources; leaves = items.
- [ ] `source_depth = 0` yields one source per channel.
- [ ] `max_sources` collapse produces one source and logs once.
- [ ] Transcripts absent when `transcripts = false`; present and parsed when
      true; malformed files are skipped.
- [ ] Missing local files do not break the build or serve another node's bytes.
- [ ] HTML5 apps serve entry HTML and relative assets; zip path traversal fails.
- [ ] Video/audio range requests return correct bytes.
- [ ] Database is opened read-only: its mtime/content is unchanged after
      discovery, build, search, and serving.
- [ ] Unsupported schema disables the collection with an error, not a crash.
- [ ] Concurrent Kolibri writes do not crash Zimantic; reload recovers.
- [ ] No network access; no absolute paths in payloads.

## Acceptance criteria

- An IIAB-style `/library/kolibri` installation appears as one collection per
  channel, with sources for top-level topics.
- Titles/descriptions and readable documents are searchable; media is servable
  and searchable only through opt-in transcripts.
- The provider never writes to Kolibri and never generates transcripts.
- `python -m pytest` passes, including a fixture DB that needs no Kolibri
  installation.

## Risks and mitigations

- **Schema drift.** Isolate all SQL/schema knowledge in `schema.py`, detect
  versions, and fail closed with a clear message.
- **Zipped HTML5 apps.** Resolve inside the archive/manifest only; zip-slip
  tests are mandatory.
- **Large channels.** Bound source count, node scan, transcript size, and
  preview reads; mark truncation.
- **Protected/private content.** Do not surface coach-only or unavailable
  content; follow the database's availability flags and document the rule.
- **Concurrent writes.** Read-only connections and next-reload recovery; never
  hold locks that block Kolibri.
