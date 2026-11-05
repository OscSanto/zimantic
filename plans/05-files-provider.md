# Phase 05 — Files provider

Status: proposed.

## Goal

Search and serve a configured directory as one collection. This is the provider
for `/library/www/html/modules/*` and similar offline trees. It must not turn a
directory of files into a directory of sources.

## Locked model

- **One configured directory = one collection.**
- **A source is the searchable unit and content root inside that collection.**
- **Items are never sources.** A source may contain any number of files/pages.

Default granularity is `directory`:

- each immediate child directory of the configured root becomes one source;
- loose files directly in the root become one source labelled after the root;
- all files inside a source are items and assets;
- a source's content root is that child directory (or the collection root for
  the loose-files source).

`granularity = "collection"` makes the whole root one source. That is the
recommended setting for large or unpredictable trees.

Per-file sources are deliberately not supported. If someone needs that, they
configure a root per desired source.

## Configuration

Phase 08 owns the general config loader; phase 05 introduces the minimal
structure and validation.

```toml
[[sources]]
type = "files"
name = "modules"                 # unique collection name; used in keys
path = "/library/www/html/modules"
label = "Modules"
granularity = "directory"        # "directory" | "collection"
include = ["**/*.html", "**/*.htm", "**/*.xhtml",
           "**/*.txt", "**/*.md", "**/*.pdf"]
exclude = ["**/node_modules/**", "**/.git/**", "**/__pycache__/**"]
max_sources = 64
max_entries = 100000
max_file_bytes = 268435456
# transcripts = false            # reserved; files provider has no media ASR
```

Rules:

- `path` must exist and be readable; a missing root disables the collection
  with an error, it does not crash startup.
- `name` must be unique across configured collections. Reject duplicates.
- Unknown keys in a `[[sources]]` entry warn once and are ignored.
- The legacy flat `zim_dir` remains a ZIM collection and is unaffected.
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

For `granularity = "directory"`:

1. List immediate children of the root without following symlinked directories.
2. Directories become candidate sources; skip hidden/system names and excludes.
3. Loose files that match `include` form one source named after the collection
   label (for example `Modules (files)`).
4. Sort by display name; source keys are
   `files:<name>:<slug(relative path)>`, with a short hash on collision.
5. Empty/unreadable child directories are omitted with a debug log. An
   unreadable *root* disables the collection with `available=false`.

For `granularity = "collection"`:

- one source whose content root is the configured root.

### Caps

- If candidates exceed `max_sources`, **collapse the whole collection to one
  source** and log a warning with the count. Do not keep a prefix and hide the
  rest.
- Discovery is stat-only and therefore does not estimate the item count. The
  entry producer builds a complete manifest, indexes at most `max_entries`,
  and marks the source metadata with `truncated: true` when the manifest is
  larger. Serve all files regardless of indexing and expose the reduced search
  coverage in health/API metadata.
- Discovery never parses file contents or computes content hashes; it uses
  `stat` only. This keeps startup bounded.

### Source metadata

- `SourceInfo.provider = "files"`, `content_kind = "filesystem"`,
  `mode = "local"`, `collection_key`, `collection_label`.
- `name` = stable source slug; `title` = display label (directory name).
- `content_id` = source key.
- `searchable` = an index exists for this source; files have no native
  full-text fallback.
- `available` = the root/directory is readable.
- `article_count` = discovered/indexable manifest count when known; otherwise
  `None` (do not walk the tree at startup).
- `indexed_count` = documents actually present in the index when known;
  `truncated` is true when the manifest exceeds `max_entries`.
- `size` = optional cheap sum from discovery only if already available; skip
  otherwise.
- `homepage` = relative `index.html`/`index.htm` when present, else `None`.

## Indexing

Create `providers/files/entries.py` with a `FilesEntryProducer`.

### Stable ids and resume

- Scan once at build start; sort relative paths; assign ids by sorted position.
- Compute `fingerprint` from the sorted list of `(relative path, size, mtime)`.
- Persist `source_fingerprint` in index `meta`; phase 04 restarts the build if
  it changes between resume attempts.
- Since the producer holds the manifest for the build, `entry_at(i)` is O(1).

### Extractors

Reuse the phase-04 extraction layer:

- HTML/XHTML: existing HTML parser, title, readable blocks, disambiguation
  handling where meaningful.
- PDF: existing PyMuPDF text layer.
- Plain text and Markdown: normalize whitespace; strip Markdown markup only if
  the existing text parser can do so without adding a dependency.
- Skip binaries, assets, empty files, and files above `max_file_bytes`;
  log at debug level.
- `EntryRecord.path` = relative path within the source root (POSIX
  separators). `target` = `None`.
- Title fallback order: HTML `<title>`, PDF metadata title, first heading,
  filename stem.

### Build entry point

`FilesProvider` exposes a `build(source, ...)` path that phase 08's CLI calls.
Use the phase-04 locator and pipeline; do not write a files-specific index
format.

## Content store

Create `providers/files/store.py` implementing `ContentStore`.

- Resolve the real root once. Reject the request if the resolved target escapes
  it.
- Do not follow symlinks by default. A config option may allow following
  symlinks, but the resolved target must still be under the real root.
- Normalize and reject absolute paths, `..`, backslashes, NUL, and empty path
  segments.
- Directory requests:
  - serve `index.html` or `index.htm` when present;
  - otherwise 404; do not generate directory listings.
- MIME:
  - use extension mapping first;
  - sniff a small prefix for unknown types;
  - never let an unknown binary be served as `text/html`.
- Response policy:
  - inline: HTML, XHTML, PDF, plain text, Markdown as text, images, audio,
    video;
  - attachment: everything else, with `X-Content-Type-Options: nosniff`;
  - byte ranges for media (phase 03);
  - size limits from `max_article_bytes`.
- Icons: `favicon.ico`, `favicon.png`, `apple-touch-icon.png` at the source
  root, else `None`. Do not crawl pages for icons.
- Random: choose from the indexed manifest when present; otherwise sample
  shallowly without walking a huge tree on every request.

HTML pages are rendered through the existing article shell. Relative links and
assets resolve under the same `content_id`. The global `secure_browsing`
setting still applies; document that untrusted file trees should enable it.

## Library and search behavior

- Library: one collection card; sources inside it. A large collection must not
  produce a flat wall of cards.
- Search: source filters may list sources, grouped by collection. If the UI
  cannot show them all, group under the collection rather than truncating
  silently.
- Results: `source` = source display title, `source_key` = source key,
  `path` = relative path, `url` = `/content/<content_id>/<path>`.
- Previews: read through the content store; never open files without the root
  confinement check.

## Work items

- [ ] Add `zimantic/providers/files/` with provider, discovery, entries, and
      store modules.
- [ ] Add provider-level validation for normalized files configuration; the
      shared `[[sources]]` parser lands in phase 08.
- [ ] Implement collection/source discovery with `directory` and `collection`
      granularity, excludes, and the source cap collapse.
- [ ] Implement `FilesEntryProducer` with stable ids and a manifest
      fingerprint.
- [ ] Add Markdown/plain-text extraction support if not already covered by the
      refactored extractors.
- [ ] Implement `FilesContentStore` with confinement, MIME policy, directory
      index handling, icons, and random.
- [ ] Wire build and index discovery into the runtime.
- [ ] Add source metadata: `provider`, `collection`, `content_id`,
      `searchable`, `homepage`, `article_count`, `indexed_count`, `truncated`.
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

- [ ] `granularity = "directory"` yields sources for `algebra`, `biology`, and
      one loose-files source; hidden/excluded directories are absent.
- [ ] `granularity = "collection"` yields exactly one source.
- [ ] Exceeding `max_sources` collapses to one source and logs once.
- [ ] Stable ids and resume: interrupt/restart with unchanged tree resumes;
      changing a file fingerprint restarts.
- [ ] Indexing extracts HTML, PDF, text, and Markdown; skips binaries and
      oversized files.
- [ ] `/content/<source>/` serves `index.html`; `/assets/style.css` resolves;
      nested pages keep working.
- [ ] Range requests for `video.mp4` return 206 and correct bytes.
- [ ] Traversal and symlink-escape requests return 404, never outside bytes.
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
- **Indexing cost.** Bound entries, bytes, and file types; never index assets or
  media bodies. Start with metadata-only media items (servable, not searchable).
- **Resume drift.** Fingerprint the manifest; never resume against a changed
  file list.
- **MIME confusion.** Prefer extension, then constrained sniffing; never serve
  an unknown type as HTML.
