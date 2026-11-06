# Phase 05 — Files provider

Status: proposed.

## Goal

Search and serve a configured directory as one collection. This is the provider
for `/library/www/html/modules/*` and similar offline trees. It must not turn a
directory of files into a directory of sources.

## Locked model

- **One configured directory = one collection.**
- **A source is the searchable and library unit inside that collection.**
- **Items are never sources.** A source may contain any number of files/pages.

Default granularity is `collection`: the whole root is one source. This keeps
the library bounded even before the grouped collection UI (phase 07) lands.

`granularity = "children"` splits the root into sources:

- each immediate child directory of the configured root becomes one source;
- loose files directly in the root become one source labelled after the root;
- all files inside a source are items and assets;
- a source's membership root is that child directory (or loose root files),
  while its content store's virtual namespace is the whole permitted collection.

Use `granularity = "children"` when the tree has a handful of stable top-level
units and the grouped library/source filters from phase 07 are available;
otherwise the default avoids a wall of cards.

Per-file sources are deliberately not supported. If someone needs that, they
configure a root per desired source.

## Configuration

Phase 00 owns the minimal raw `[[collections]]` TOML parser. Phase 05 owns
files-provider validation and configuration semantics; phase 08 completes the
full loader and adds the remaining operations. Do not duplicate the parser in
the provider.

```toml
[[collections]]
type = "files"
name = "modules"                 # unique collection name; used in keys
path = "/library/www/html/modules"
label = "Modules"
granularity = "collection"       # "collection" | "children"
include = ["**/*.html", "**/*.htm", "**/*.xhtml",
           "**/*.txt", "**/*.md", "**/*.pdf"]
exclude = ["**/node_modules/**", "**/.git/**", "**/__pycache__/**"]
max_sources = 64
max_entries = 100000
max_file_bytes = 268435456

# After discovering the exact key via `/sources` (or `sources --json` in phase 08):
# [[collections.sources]]
# key = "files:modules:root"
# captions = "index"             # opt in only for this source
# ocr = "tesseract"
# asr = "whisper"
```

Rules:

- `path` must exist and be readable; a missing root disables the collection
  with an error, it does not crash startup.
- `name` must be unique across configured collections. Reject duplicates.
- Unknown keys in a `[[collections]]` entry warn once and are ignored.
- `[[collections.sources]]` overrides are keyed by exact discovered source
  key as specified in phase 04; absent switches remain off. A stale key is
  reported and never matched by title or path.
- `include` selects native documents for indexing. Permitted image, audio, and
  video files are also title-only candidates by default, using filenames without
  reading media bytes; their bodies contribute text only when an operation is
  enabled. `exclude` and hidden-path rules apply to discovery, indexing, and
  serving. `**/` matches zero or more directory components, so the example
  includes root-level files too. A directory exclusion also excludes that
  directory itself and its descendants. Match both include/exclude patterns
  against collection-relative POSIX paths, regardless of source granularity.
- There is no flat `zim_dir` key; every collection, including ZIM, is declared
  with `[[collections]]`.
- Absolute configured `path` values are allowed. Resolve the real root once
  and retain it only as a private runtime/content-store/catalog locator; do not
  expose it in API payloads, errors, redirects, or logs above debug level.
- Source and collection keys derive from the configured name and
  root-relative identifiers, never the absolute root path.

## Discovery

Create `zimantic/providers/files/provider.py` and `discovery.py`.

### Collection key

`files:<name>` (stable, opaque). If a user renames `name`, it is a new
collection; document that.

### Source discovery

For `granularity = "children"`:

1. List immediate children of the root without following symlinked directories.
2. Directories become candidate sources; skip hidden/system names and excludes.
3. Permitted loose files form one source named after the collection label (for
   example `Modules (files)`), even if none match the index `include` patterns.
4. Sort by display name; directory source keys are
   `files:<name>:dir-<safe-slug>-<digest>` with the digest always derived from
   the root-relative directory name. Detect a digest collision and report it
   instead of overwriting a source.
5. Include empty directories and report unreadable ones as unavailable rather
   than silently dropping them. An unreadable *root* disables the collection.

For `granularity = "collection"`:

- one source whose content root is the configured root and whose source key is
  `files:<name>:root`.

For `granularity = "children"`, the loose-files source uses the reserved
root-relative identifier `__loose__`, so its key is
`files:<name>:__loose__`. The `dir-` namespace keeps an actual child named
`__loose__` distinct. Its producer indexes only loose root files. Every store
uses the collection root and the same collection-relative request namespace.
This overlap is intentional:
source membership scopes discovery, indexing, search, and library grouping,
not serving authorization. Any source in the same files collection may
directly serve a permitted descendant under the collection root, including
shared assets and files belonging to another source. Confinement, hidden-path,
and exclusion rules apply identically to every such request.

For example, the `algebra` child source indexes `algebra/index.html`, publishes
that path as its homepage, and links to
`/content/<algebra-key>/algebra/index.html`. Its `../shared/style.css` link
resolves naturally to `/article/<algebra-key>/shared/style.css`. Never strip or
guess the child prefix, and never retry a missing path in a second namespace.
An empty source URL resolves/redirects to that source's own homepage, not
silently to the collection's root homepage. All source keys may explicitly
request a permitted collection-relative path, but only membership assigns
search results and counts to a source.

### Caps

- If candidates exceed `max_sources`, **collapse the whole collection to one
  root source** with key `files:<name>:collapsed` and log a warning. This key is
  distinct from the explicitly configured collection-granularity `:root` key,
  so a prior root opt-in cannot silently apply during collapse. Detecting
  `max_sources + 1` is enough; do
  not walk the rest just to log an exact count. Do not keep a prefix and hide
  the rest.
- Collapse and recovery change source keys and may require a new build;
  report the change and any stale per-source overrides. Never transfer an
  enrichment opt-in implicitly to the collapsed source.
- Discovery does not estimate the item count. At build time, enumerate a
  deterministic manifest up to `max_entries + 1` index candidates; index at
  most `max_entries` and mark `truncated: true` if there is another candidate.
  When truncated, the exact total count is unknown. Serve all permitted files
  regardless of indexing and expose reduced coverage in health/API metadata.
- The candidate cap includes title-only media. Establish deterministic
  depth-first, collection-relative path order before applying it: fully
  enumerate and sort each visited directory under the visit/time budgets,
  counting excluded entries too. Do not use an arbitrary filesystem
  enumeration prefix as the supposedly stable capped manifest.
- Bound directory entries examined and elapsed scan time separately from the
  indexing cap. If either budget expires before the scan completes or proves
  truncation, fail that build explicitly and retain its last published index;
  do not call the incomplete prefix a complete manifest.
- Discovery never parses file contents or computes content hashes; it uses
  `stat` only. This keeps startup bounded.

### Source metadata

- `SourceInfo.provider = "files"`, `collection_key`, `collection_label`. There
  is no `content_kind` or `mode`.
- `key` is the canonical source identity and URL identity.
- `title` = display label (directory name).
- `searchable` = a usable, not detectably stale index exists for this source;
  files have no native full-text fallback.
- `available` = the root/directory is currently readable; runtime metadata,
  never persisted as a catalog/index field. A persisted source record means it
  was discovered/addressable, regardless of current availability.
- `document_count` = total permitted document-like items only when known;
  otherwise `None` (including a truncated scan; do not walk the tree at
  startup).
- `media_count` = total permitted media items only when known; otherwise
  `None`. `item_count` = total permitted source items only when known; it is
  never the bounded manifest length.
- `indexed_count` = documents actually present in the index when known;
  `truncated` is true when the manifest exceeds `max_entries`.
- `body_truncated_count` follows the phase-04 body-coverage contract; it is
  independent of the item-count cap.
- `size` = optional cheap sum from discovery only if already available; skip
  otherwise.
- `homepage` = collection-relative `index.html`/`index.htm` within that source's
  membership root when present, else `None`; for a child this includes its
  directory prefix.
- `media_types` = known content families (`text`, `video`, `audio`, `image`,
  `other`) from bounded discovery classification. `include` can omit
  permitted media that remains servable, so classification is independent of
  the index manifest and derived-text settings. Use `None` only when no usable
  family was observed; otherwise list every observed family, including from a
  partial or truncated scan. Use `()` only after complete classification finds
  no supported families. A complete empty result is rescanned once and warned
  if it remains empty.

## Indexing

Create `providers/files/entries.py` with a `FilesEntryProducer`.

### Stable ids and resume

- Scan in deterministic relative-path order at build start; assign ids by
  manifest position. Bound visited entries and time as above.
- Compute `fingerprint` from the ordered selected `(relative path, size,
  mtime)` signatures, where `mtime` is rounded to the nearest whole second,
  plus the manifest, build settings, and associated inputs for enabled
  operations. Only when captions are enabled, include the presence and
  signatures of matching caption sidecars, so adding, editing, or removing one
  invalidates the index even if the media file is unchanged. Do not discover or
  track sidecars for fingerprinting while captions are off; enabling captions
  causes the next build to rescan the source. Re-scan and compare before
  publication; subsecond changes and metadata-preserving writes may evade this
  practical change detector.
- Persist `source_fingerprint` in index `meta`; phase 04 restarts the build if
  it changes between resume attempts.
- Since the producer holds the manifest for the build, `entry_at(i)` is O(1).

### Extractors

Reuse the phase-04 extraction layer:

- HTML/XHTML: existing HTML parser, title, readable blocks, disambiguation
  handling where meaningful, and bounded body text beyond the short excerpt.
- PDF: existing PyMuPDF text layer, continuing across pages up to the
  phase-04 body limit rather than stopping at the preview length.
- Plain text and Markdown: normalize whitespace; strip Markdown markup only if
  the existing text parser can do so without adding a dependency.
- Skip unsupported binaries, non-media assets, empty documents, and documents
  above the effective input limit; log at debug level. The effective limit is the
  minimum of `max_file_bytes` and global `max_entry_bytes`; a derived-text
  operation may impose a lower operation-specific cap. Report cap skips as
  partial enrichment only when an enabled derived-text operation needed the
  input; ordinary excluded/unsupported files are not enrichment failures.
  A media title can still be indexed from its filename when the media body
  exceeds an extraction cap.
- Derived text is off by default. A permitted media candidate gets a
  title/path-only row without reading its bytes. When its source enables
  `captions`, `ocr`, or `asr`, feed eligible items through the shared stage,
  subject to exclusions and the global, provider, and operation byte caps.
  An image-only PDF remains eligible for OCR even if native extraction returns
  no excerpt; emit a title/path record so enrichment can run.
- When captions are enabled, associate permitted `.srt`/`.vtt` sidecars in the
  same directory with their media item. Support the full media filename
  followed by the track extension (optionally with a language suffix) and the
  media stem followed by an optional language suffix and track extension. A
  stem-only match is valid only when it identifies one media file; report an
  ambiguous match rather than attaching it to the wrong item. Sort associated
  sidecars by relative path, then process embedded text tracks in stream order.
  Matched sidecars are not separate result rows. Phase 04 owns embedded
  demuxing, deduplication, and operation failure semantics.
- `EntryRecord.id` = one-based position in the deterministic manifest,
  yielding a positive canonical row id. `EntryRecord.path` = relative path
  within the collection root (POSIX separators), including a child directory's
  prefix. `target_id` = `None`.
- Title fallback order: HTML `<title>`, PDF metadata title, first heading,
  filename stem.

### Build entry point

`FilesRuntime` implements phase 04's `BuildableRuntime.entry_producer` and
returns a `FilesEntryProducer`. Phase 05 wires it into the existing generic
`zimantic build` orchestration so configured files collections can be built
without waiting for phase 08. Phase 08 adds the
`--provider`/`--collection`/`--source`/`--list` selection. Use the phase-04
locator and pipeline; do not write a files-specific build dispatcher or format.

## Content store

Create `providers/files/store.py` implementing `ContentStore`.

- Files have unique collection-relative paths and use normal path-based
  serving without requiring an index.
- Resolve the real collection root once and open descendants using the race-safe,
  no-symlink policy in phase 03. Fail closed if confinement cannot be enforced.
- Deny hidden paths and `exclude` matches for direct requests as well as
  discovery/indexing; `include` does not restrict serving assets.
- Normalize and reject absolute paths, `..`, backslashes, NUL, and empty
  interior path segments. Non-empty paths are always collection-relative.
  `main_path()` and an empty request select the source's membership-root
  homepage and return its fully prefixed collection-relative path; when absent,
  use the standard missing-homepage response rather than a different root.
- Directory requests:
  - serve `index.html` or `index.htm` when present;
  - otherwise 404; do not generate directory listings.
- MIME:
  - use extension mapping first;
  - sniff a small prefix for unknown types;
  - never let an unknown binary be served as `text/html`.
- Response policy:
  - inline: HTML, XHTML, PDF, plain text, Markdown as text, images, audio,
    video, and recognized web assets (CSS, JavaScript, JSON, fonts, and WASM)
    with their correct MIME types;
  - active image formats such as SVG require a restrictive sandbox/CSP when
    opened directly;
  - attachment: everything else, with `X-Content-Type-Options: nosniff`;
  - byte ranges for media (phase 03);
  - size limits from `max_article_bytes`.
- Icons: `favicon.ico`, `favicon.png`, `apple-touch-icon.png` at the source
  membership root, else `None`. Use collection-relative paths and do not crawl
  pages for icons.
- Random: choose from the indexed manifest when present; otherwise sample
  shallowly without walking a huge tree on every request.

HTML pages are rendered through the article shell. Relative links and
assets resolve under the same source key. The global `secure_browsing`
setting still applies; document that untrusted file trees should enable it.

## Library and search behavior

- Library: before phase 07, expose the bounded flat source list with collection
  metadata. Once phase 07 lands, show one collection card with sources inside;
  a large collection must not produce a flat wall of cards.
- Search: source filters may list sources, grouped by collection. If the UI
  cannot show them all, group under the collection rather than truncating
  silently.
- Results: `source` = source display title, `source_key` = source key,
  `path` = collection-relative path, `url` = `/content/<key>/<path>`.
- Serving is broader than search membership: a content URL may use any source
  key in the same files collection to resolve a permitted collection-relative
  path, while search results and library/source listings remain attributed to
  the source that discovered or indexed the item.
- Previews: the runtime preview hook reads documents through the content store
  and uses filename/native metadata for media without reading its body; never
  open files without the root confinement check.

## Work items

- [ ] Add `zimantic/providers/files/` with provider, discovery, entries, and
      store modules.
- [ ] Add provider-level validation for normalized files configuration, and
      consume the phase-00 `[[collections]]` parser without adding a provider-local
      parser.
- [ ] Implement collection/source discovery with `children` and `collection`
      granularity, excludes, and the source cap collapse.
- [ ] Implement `FilesEntryProducer` with stable ids and a manifest
      fingerprint.
- [ ] Add Markdown/plain-text extraction support if not already covered by the
      refactored extractors.
- [ ] Wire opt-in `captions`, `ocr`, and `asr` to the shared derived-text stage;
      keep all three off by default.
- [ ] Implement `FilesContentStore` with confinement, MIME policy, directory
      index handling, icons, and random.
- [ ] Wire build and index discovery into the runtime, including the
      `zimantic build` entry point for configured files collections.
- [ ] Add source metadata: `provider`, `collection`, `key`, `searchable`,
      `homepage`, `document_count`, `media_count`, `item_count`,
      `indexed_count`, `truncated`, `body_truncated_count`, and
      nullable `media_types`.
- [ ] Document a recommended `/library/www/html/modules` configuration and the
      `granularity` trade-off in `README.md`.
- [ ] Add a files-provider example to `config.toml` (commented out by default).

## Tests

Use a fixture tree containing:

```
modules/
  algebra/index.html + assets/style.css + lesson.pdf
  biology/index.html + video.mp4 (tiny)
  loose.txt
  .hidden/secret.html
  node_modules/pkg/index.html
  link-to-outside -> /tmp/...
  broken/
```

- [ ] `granularity = "children"` yields sources for `algebra`, `biology`, and
      one loose-files source with the reserved `__loose__` key;
      hidden/excluded directories are absent.
- [ ] A real `__loose__` child is distinct from the loose-files source;
      adding/removing a similarly named directory does not change existing keys.
- [ ] The default (`granularity = "collection"`) yields exactly one source.
- [ ] Exceeding `max_sources` collapses to one source and logs once.
- [ ] A `:root` enrichment override does not enable operations on the separate
      `:collapsed` source.
- [ ] Stable ids and resume: interrupt/restart with unchanged tree resumes;
      changing a file fingerprint restarts.
- [ ] A capped manifest is identical across differing directory-enumeration
      orders; an oversized directory exhausts the traversal budget rather
      than publishing a nondeterministic prefix.
- [ ] Exceeding `max_entries` is marked truncated with unknown total; exhausting
      a traversal/time budget preserves the last published index and reports
      failure.
- [ ] A source with permitted media omitted from the index is not falsely
      excluded by media filters.
- [ ] Indexing extracts HTML, PDF, text, and Markdown; skips unsupported
      binaries and oversized document bodies.
- [ ] Body search finds terms beyond the embedding excerpt and reports a body
      cap separately from the source item cap.
- [ ] Permitted media filenames are searchable by title with all operations off;
      no media bytes are read, and exclusions still apply.
- [ ] Derived text is absent by default and no image/media bytes are decoded;
      enabling `ocr` or `asr` indexes fake-engine output with the default
      document-only `include` and records provenance.
- [ ] A same-directory sidecar and an embedded text track attach to the
      intended media item; ambiguous sidecars are reported. Adding, editing, or
      removing a sidecar invalidates a captions-enabled index, while a disabled
      sidecar is never read. With the default document includes, sidecar
      changes do not affect a captions-disabled fingerprint; enabling captions
      causes the next build to rescan the affected source.
- [ ] `/content/<key>/` wraps `index.html`; raw article assets resolve beneath
      the same key, and nested pages, CSS, JS, fonts, and WASM have usable
      MIME/disposition.
- [ ] Direct SVG responses have the required browser isolation.
- [ ] Range requests for `video.mp4` return 206 and correct bytes.
- [ ] Traversal, symlink-swap, hidden, and excluded-path requests return 404,
      never outside or excluded bytes; allowed CSS and other assets still load.
- [ ] A page may load a permitted shared or cross-source asset through its
      source key; the asset is not thereby added to that source's search or
      library membership.
- [ ] A child source's indexed paths, homepage, redirects, previews, and raw
      asset URLs all retain the same collection-relative prefix. An ordinary
      `../shared/style.css` link works without traversal parameters or fallback
      path guessing, even when both child and collection have `index.html`.
- [ ] Unknown extension is an attachment and cannot be sniffed as HTML.
- [ ] Library/API payloads expose collection + source grouping without absolute
      paths.
- [ ] Search returns a file item with the correct source/path/URL and preview.
- [ ] Unreadable root disables the collection without crashing the server.
- [ ] No network access during discovery, build, search, or serving.

## Acceptance criteria

- A configured `/library/www/html/modules` tree is browsable as one collection,
  searchable, and serves its pages/assets through `/content/...`.
- Adding 300 random child folders does not create 300 sources: the collection
  collapses and remains searchable.
- No file path outside a configured root can be read through any route.
- `python -m pytest` passes.

## Risks and mitigations

- **Source explosion.** The cap-collapse rule is mandatory and tested.
- **Untrusted HTML.** Keep the content-shell sandbox, `nosniff`, and attachment
  policy; recommend `secure_browsing = true` for file trees.
- **Indexing cost.** Bound entries, bytes, and file types; do not index
  non-media assets or decode media by default. Media filenames are
  title-searchable; captions,
  `ocr`, and `asr` are explicit, capped build-time operations.
- **Resume drift.** Fingerprint the manifest; never resume against a changed
  file list.
- **MIME confusion.** Prefer extension, then constrained sniffing; never serve
  an unknown type as HTML.
