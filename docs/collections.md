# Collections and providers

## Files and directories

The installed CLI is separate from configuration and runtime data. On XDG platforms, configuration is read from `$XDG_CONFIG_HOME/zimantic/config.toml` (normally `~/.config/zimantic/config.toml`); ZIMs, models, and indexes default under `$XDG_DATA_HOME/zimantic` (normally `~/.local/share/zimantic`). If the XDG config is missing, Zimantic uses profile defaults; it does not search the current directory. Relative global path overrides (`model_dir`, `index_dir`) resolve under the data directory. Relative provider `path`/`database_path`/`storage` values resolve against the directory containing the active config file, even through a symlink. On Windows, the default config is `config.toml` in the current directory.

```
~/.local/share/zimantic/
├── model/               ← downloaded models; model/int8/ is model_dir         (you download)
├── zims/                ← ZIM collection path: your .zim files                (you download)
├── indexes/             ← index_dir: immutable generations under provider/collection/source
└── catalog.sqlite       ← shared metadata cache (metadata only, no vectors)
```

```
~/.config/zimantic/config.toml          ← optional settings
$XDG_RUNTIME_DIR/zimantic/zimantic.pid ← PID file while serving
```

If the files are on a USB drive or another disk, point a ZIM collection's `path`, `model_dir`, or `index_dir` in the XDG config at them.

## Sources and providers

Zimantic has one configuration schema for every in-tree provider. A
`[[collections]]` entry is one collection; the provider discovers one or more
sources inside it (one ZIM archive per source, one files subtree or child group,
one Kolibri channel/topic, one Kiwix book). Every collection is explicit: there
is no `zim_dir`/`kiwix_server` shorthand and no implicit default library.

```toml
[[collections]]
type = "zim"
name = "library"
path = "./zims"
label = "ZIM files"

[[collections]]
type = "files"
name = "modules"
path = "/library/www/html/modules"
label = "Modules"
granularity = "collection"       # or "children"
traversal.max_visited_entries = 1000000
traversal.traversal_timeout_seconds = 300

[[collections]]
type = "kolibri"
name = "kolibri"
path = "/library/kolibri"
label = "Kolibri"
granularity = "children"

[[collections]]
type = "kiwix"
name = "remote"
server = "http://localhost:8085"
label = "Remote Kiwix"
catalog_limit = 1000
max_catalog_entries = 50000
```

Provider `path` values, including `database_path` and `storage`, resolve once
against the directory holding the active config file, even through a symlink.
CLI path operands resolve against the working directory. The resolved choice is
kept when an ad-hoc ZIM build saves a collection entry, so it points at the
same files on the next invocation. Do not put global scalar keys after a table
header; after `[[collections]]` or `[derived_text]` a key belongs to that table.
Unknown global keys are fatal configuration errors.

Provider limits default in the provider and can be overridden per entry
(`max_sources`, `max_entries`, `max_file_bytes`, `traversal.max_visited_entries`,
`traversal.traversal_timeout_seconds`, Kiwix `catalog_limit`/`max_catalog_entries`, and the
Kolibri snapshot/HTML5 zip limits). `max_sources` and `max_entries` bound
discovery and indexing, not serving: a permitted file stays browsable even
when indexing was capped. Exceeding `max_sources` collapses a files/Kolibri
collection to one source with a distinct key and a warning; exceeding
`max_entries` marks the source truncated. Rebuild the index when the source
collapses or returns to its normal state.

Traversal limits belong to the nested `traversal` table; the dotted keys above
configure it without a separate table header. Unknown collection or traversal
keys reject that collection instead of silently using defaults.

Global bounds stay global: `max_explicit_collection_sources` (default 500)
limits work for an explicitly selected Kiwix collection and reports partial
scope instead of claiming completion; `search_timeout_seconds` (default 30) is
the admitted-search deadline across providers and is separate from
`source_timeout` and `slot_wait_seconds`; `max_body_text_bytes` (default 1 MiB)
caps the lexical body per item independently of previews and item-count
truncation; `max_derived_cache_bytes_per_source` caps optional preview artifacts
only, never search postings.

Every local indexed provider uses the same immutable generation layout:

```
index_dir/<provider>/<collection>-<digest>/<source>-<digest>/
├── current                         ← JSON manifest pinning one generation
└── generations/<uuid>/
    ├── index.sqlite                ← docs, title/body FTS, metadata
    ├── index.faiss                 ← only when semantic vectors exist
    └── lease                       ← shared reader pin
```

SQLite must be 3.37 or newer for the STRICT catalog and index schemas, and the
SQLite build must provide FTS5; Zimantic checks the version early rather than
deferring a failure to the first query. A SQLite build at a sufficient version can
be compiled without FTS5. `catalog.sqlite` beside `index_dir` is the
private cache for ZIM archive metadata and Files root-document summaries.
Kolibri does not write it, and Kiwix has no persistent local catalog. A
catalog with an unsupported schema is rebuilt by source discovery. Provider-specific
tables that are not part of the active schema are not removed automatically; to reclaim their space,
stop Zimantic and delete the disposable catalog and any SQLite sidecars.
Use one schema version for a catalog at a time. Index
identity is logical (`provider`, `collection key`, `source key`), never an
absolute source path. Removing or renaming a collection does not delete its
indexes; `zimantic prune` is the explicit cleanup path.

Treat untrusted file trees as untrusted input. Path confinement and traversal
limits apply to serving and indexing, but `secure_browsing` sandboxes only the
`/content/` shell; a raw `/article/` document opened directly or in a permitted
popup is not sandboxed and can run with the app's origin and persistent storage.

### Scoped settings

Extraction limits, search tuning, cache quotas, and Files indexing filters share
one **global → collection → exact-source** resolver. Global defaults stay at the
TOML root; put the same keys directly on a collection or in its `sources`
entries. Only specified fields override inherited values.

| Settings | Scope and effect |
| --- | --- |
| `max_entry_bytes`, `max_html_bytes`, `max_body_text_bytes`, `max_preview_chars`, `max_entries` | Per-source local extraction/indexing budgets; preview settings also apply during search |
| `candidate_count`, `nprobe`, `min_cosine_similarity`, `source_timeout` | Per-source search effort, IVF probes, hybrid semantic threshold, and deadline |
| `cache_size`, `cache_bytes` | Per-source quotas inside the shared query and preview caches; global values also bound each entire cache |
| `max_derived_cache_bytes_per_source` | Per-source derived-preview artifact budget |
| `index_include`, `index_exclude` | Files-only indexing globs; lists replace inherited lists, never change serving permissions |

```toml
max_body_text_bytes = 1_048_576
candidate_count = 16
cache_size = 256
cache_bytes = 33_554_432

[[collections]]
type = "files"
name = "media"
path = "./media"
max_body_text_bytes = 262_144
source_timeout = 3.5
index_exclude = ["**/drafts/**"]
sources = [
  { key = "files:media:root", max_body_text_bytes = 65_536, candidate_count = 8, cache_size = 32 },
]
```

Use exact keys from `/sources`. Extraction/search budgets must be positive;
`min_cosine_similarity` must be between -1 and 1. Cache quotas accept zero:
`cache_size = 0` disables admission for that source, `cache_bytes = 0` means
count-only caching, and `max_derived_cache_bytes_per_source = 0` disables creation of
derived-preview artifacts without removing searchable text. A mixed-source
query-cache entry counts in full against each participating source's quota.
Raising a scoped quota cannot raise the shared cache's global ceiling.

`index_include` selects permitted primary Files items for indexing, including
media title rows. An empty list restores the normal collection `include`
selection for native documents. `index_exclude` adds indexing-only exclusions;
the existing collection `exclude`, hidden-path, symlink, and confinement rules
always remain in force for both indexing and serving.

Build-affecting settings enter source fingerprints; search/preview policy enters
cache keys. Host-wide model selection, worker pools, memory/concurrency controls,
serving limits, and the overall `search_timeout_seconds` stay global. A source
deadline cannot extend the overall deadline; native calls that cannot be
interrupted are checked when they return.

### Derived text

Derived text has three independent opt-ins, all off by default. Files and
Kolibri collections can set defaults inline on the collection:

```toml
[[collections]]
type = "files"
name = "media"
path = "./media"
derived_text = { captions = "index", ocr = "tesseract", asr = "whisper" }
```

Accepted operation values are:

| Setting | Values | Effect |
| --- | --- | --- |
| `captions` | `"off"`, `"index"` | Index sidecar and available embedded captions |
| `ocr` | `"off"`, `"tesseract"` | OCR images and image-only PDF pages |
| `asr` | `"off"`, `"whisper"` | Transcribe audio/video locally |

`asr_engine` separately selects the Whisper backend: `"auto"`,
`"whisper.cpp"`, or `"faster-whisper"`.

All options accepted under global `[derived_text]` (languages, ASR backend/model,
caps, timeout, workers, and cache) can also appear in a collection's
`derived_text` table or an exact-source override. Precedence is built-in defaults
→ global `[derived_text]` → collection `derived_text` → exact source. Only
specified fields override inherited values; language lists replace rather than
append. Engine options alone never enable an operation.

Advanced: per-source overrides. Use `sources` only when a source needs
different operations or engine options from its collection defaults. Each entry
overrides only the fields it specifies:

```toml
[derived_text]
ocr_languages = ["eng"]
asr_model = "base"

[[collections]]
type = "files"
name = "media"
path = "./media"
derived_text = { captions = "index", ocr = "tesseract", ocr_languages = ["fra"], asr = "whisper" }
sources = [
  { key = "files:media:french", asr_model = "small" },
  { key = "files:media:german", ocr_languages = ["deu"], asr = "off" },
]
```

Use keys from `/sources`; the example keys are illustrative. Effective options
participate in source build fingerprints and derived-output cache keys, so
changing them rebuilds affected sources and does not reuse incompatible text.

Setting one does not enable the others. Engines run locally at build time only
and are never loaded by `serve`; a missing engine marks only that operation
unavailable. Zimantic uses local `tesseract`, `whisper-cli`,
`whisper-ctranslate2`, `whisper`, and `ffmpeg`/`ffprobe` executables when they
are on `PATH`; no hosted transcription service is used. ASR uses the backend
selected by `asr_engine` and downloads its checkpoint on first use when it is
absent. See [Optional dependencies](derived-text.md#optional-dependencies) for installing the
backends and their model caches.
Embedded text captions use the optional local `ffmpeg`/`ffprobe`
pair when available, including supported descriptor-only inputs; a missing
pair leaves only embedded-track caption extraction unavailable. The
per-container input and spool caps under
`[derived_text]` are separate from `max_entry_bytes` and may need raising for
large media. See [Files collections](collections.md#files-collections) and
[Kolibri installations](collections.md#kolibri-installations) for copy-pasteable IIAB layouts
(`/library/www/html/modules` and `/library/kolibri`).

### Files collections

A `type = "files"` collection searches and serves an ordinary directory tree, such as an offline copy of `/library/www/html/modules`. One configured directory is one collection. The default granularity keeps the whole tree as a single source, so a directory of thousands of pages does not become a wall of library cards:

```toml
[[collections]]
type = "files"
name = "modules"
path = "/library/www/html/modules"
label = "Modules"
granularity = "collection"       # "collection" or "children"
include = ["**/*.html", "**/*.htm", "**/*.xhtml",
           "**/*.txt", "**/*.md", "**/*.pdf"]
exclude = ["**/node_modules/**", "**/.git/**", "**/__pycache__/**"]
max_sources = 64
max_entries = 100000
max_file_bytes = 268435456
```

`granularity = "children"` makes each immediate child directory its own source, plus one source for loose files directly in the root. Use it when the tree has a handful of stable top-level units; otherwise keep `collection` so the library stays bounded. If the number of child sources exceeds `max_sources`, the whole collection collapses to one source with a distinct key and a warning. The collapsed and normal states use different source keys. Rebuild the index and update any `sources` override that uses the other key.

`max_entries` bounds the indexed manifest and marks the source truncated when more candidates exist; all permitted files remain servable. `max_file_bytes` is combined with the global `max_entry_bytes` to bound document extraction, and `max_sources` bounds the child-source list.

Source keys use the configured `name` and root-relative identifiers, never the absolute path. The collection key is `files:<name>`; collection granularity uses `files:<name>:root`, a collapsed collection uses `files:<name>:collapsed`, a child uses `files:<name>:dir-<slug>-<digest>`, and loose root files use the reserved `files:<name>:__loose__`. The configured name is part of the key, so a different name has a different identity and index.

All sources in one files collection share the collection-relative serving namespace. A child page can link to `../shared/style.css` and it resolves under the same source key; search results and library membership belong to the source that indexed the item. Hidden paths, configured excludes, symlinks, and paths outside the configured root are never served. Unknown extensions are served as attachments with `X-Content-Type-Options: nosniff`; SVG opened directly is sandboxed with a restrictive CSP.

`include` selects native documents for indexing by default. Scoped `index_include` and `index_exclude` can change indexing selection without changing serving permissions. Permitted image, audio, and video files are title-searchable by filename without reading their bytes. Captions, OCR, and ASR stay off unless enabled by the collection's `derived_text` defaults (see [Derived text](collections.md#derived-text)). Files whose extension is missing or unknown get a bounded MIME probe: the built-in signatures and `puremagic` are used for the prefix, and `lsar` identifies archive containers. This keeps extensionless media and servable “other” assets visible in the library media-type filters.

`zimantic build` with no positional paths builds configured files collections as well as ZIM collections, using the same index generations, embeddings, and lock as ZIM builds. `zimantic build /library/www/html/modules` also selects this configured collection; a subdirectory or file builds cumulative partial coverage without indexing unrelated sibling sources. See [positional builds](usage.md#commands) for automatic registration and coverage semantics. Serving does not require an index: permitted files remain browsable even when indexing was capped or skipped.

With `secure_browsing = true`, the `/content/` shell sandboxes the article iframe. A raw `/article/` page opened directly or in a permitted popup is not sandboxed; treat untrusted file trees as untrusted input regardless of that setting.

### Kolibri installations

A `type = "kolibri"` collection reads a local Kolibri installation directly; Kolibri does not need to be running. The supported target is **Kolibri v0.19.5** with its installed SQLite schema. Other releases and layouts are reported as unsupported rather than guessed from a channel export database. An IIAB-style installation looks like this:

```toml
[[collections]]
type = "kolibri"
name = "kolibri"
path = "/library/kolibri"          # home containing db.sqlite3 and content/storage
label = "Kolibri"
granularity = "children"           # "children" (top-level topics) or "collection"
max_sources = 64
max_entries = 500000
# Private consistent snapshots; all limits must be positive. The disk budget
# must be at least the per-snapshot limit.
max_snapshot_bytes = 1073741824
snapshot_disk_budget_bytes = 3221225472
snapshot_timeout_seconds = 10
# Bounded HTML5 zip serving.
max_html5_zip_entries = 10000
max_html5_zip_directory_bytes = 8388608
max_html5_zip_member_bytes = 268435456
max_html5_zip_total_bytes = 1073741824
html5_zip_timeout_seconds = 10
```

Each installed channel is one collection (`kolibri:<name>:<channel_id>`). With `granularity = "children"`, each top-level topic becomes a source, direct leaf items under the channel root form an `Ungrouped` source, and a channel with no topics gets one whole-channel source. `granularity = "collection"` always uses one source per channel. If the topic count exceeds `max_sources`, the whole channel collapses to one source with the distinct `:collapsed` key and a warning; `max_entries` bounds the indexed manifest and marks the source truncated while all permitted items remain servable. Source keys are `kolibri:<name>:<channel_id>:node:<node_id>`, `:ungrouped`, `:root`, or `:collapsed`.

Items are leaf content nodes with locally available files, plus exercises whose public description is non-empty. Topics are grouping only. Titles, public descriptions, HTML/PDF/plain documents, and an HTML5 app's entry page are indexed. Audio and video bodies contribute no text unless enabled by the collection's `derived_text` defaults (see [Derived text](collections.md#derived-text)).

Discover the exact key first (for example from `/sources`). Captions can use
existing `.vtt`/`.srt` tracks and embedded text tracks; ASR transcribes
audio/video when captions did not provide usable text.

The provider **never writes to Kolibri**. It copies the installed database through a private, owner-only, SQLite-consistent snapshot outside the installation and storage roots; committed WAL data is included, and a concurrently changing source causes a bounded retry or a fail-closed diagnostic rather than a torn read. Snapshots are shared across a channel discovery batch, removed after their last reader, and capped by the configured size, disk, and time budgets. A failed refresh keeps the last complete published batch and reports the error. The installation database and its WAL/SHM/journal sidecars are not modified, migrated, vacuumed, or checkpointed.

Search results use human-readable channel/topic breadcrumbs and resource kinds instead of opaque node paths and generic file badges. When populated, cards also show grade level, duration, language, author, tags, learning activities, assessment question count, and position within the topic. A title or fast index reads these display details directly from the snapshot; description fallbacks cover resources with no extractable preview, including interactive bundles. A normal build additionally indexes the descriptive metadata for body and semantic search; fast builds remain title-only. Restart or explicitly reload the server to refresh the snapshot and cached result metadata after editing Kolibri content. No running Kolibri server is needed for metadata, and resource positions are not presented as lesson numbers.

Keep the installation, storage, and private snapshot directory on local filesystems. The snapshot copy is validated before and after reading, but a stalled kernel or filesystem cannot provide a hard real-time bound. Content fingerprints use selected database rows and storage-file size/mtime signatures. These identify many content updates but do not prove that the bytes are identical. A same-size write that restores the timestamp can evade detection. Keep installed content immutable or use an external snapshot when strong consistency is required.

Set `database_path` and `storage` only when the active database or content directory is non-standard. The provider reads SQLite installations only: if `options.ini` declares PostgreSQL, the collection is unsupported. Zimantic does not read a separate SQLite file in that installation. Absolute configured paths stay private runtime locators and never appear in source keys, API payloads, or normal logs. `zimantic build` with no positional paths builds configured Kolibri channels along with ZIM and files collections. HTML5 serving supports the `html5_zip` preset (entry `index.html` at the zip root) with bounded streaming, path confinement, duplicate/symlink rejection, and zip-bomb limits; other viewer-dependent presets such as H5P or IMSCP are reported explicitly as unsupported. Set `server_url` (for example `"http://localhost:8080"`) to a running Kolibri server and the content viewer redirects Perseus exercises and those unsupported presets to Kolibri's learn page (`<server_url>/learn/#/topics/c/<node_id>`); without it, exercises with a description show that text and the rest stay unsupported. As with files collections, untrusted app HTML can ask the browser to contact external URLs.

### Reusing v0 ZIM indexes

First update the configuration to use `[[collections]]`; v0's `zim_dir` and
`kiwix_server` settings are not migrated automatically. Keep `index_dir`
pointing at the v0 indexes and run `zimantic build`, then `zimantic serve`.
For a complete root-level v0 `<name>.sqlite` / `<name>.faiss` pair, a full
build reuses compatible vectors, scans the ZIM to rebuild title and body search,
and embeds any eligible articles without vectors. It publishes a generation and
leaves the v0 pair untouched. If the build needs vectors, the v0 FAISS index
is loaded into memory to append them. A full generation takes precedence
over the v0 pair. `serve` does not migrate indexes; `build --fast` does not
load FAISS or migrate vectors, and `build --force` rebuilds from scratch.

Reuse requires matching source size, vector IDs and document paths, and either
the same model fingerprint or matching model probes. v0 did not record a source
content digest: matching filename, size, IDs, and paths cannot detect every
same-size content replacement or verify the prior extraction settings. Use
`build --force` if the original ZIM, index, or build settings are uncertain.
Missing, incomplete, incompatible, or unrecognized pairs are rebuilt normally.
After you verify the published generation, you can remove the v0 root-level files manually.

### Copying finished indexes to another machine

Embeddings live only in `index.faiss`. Its row IDs refer to `docs.rowid` in the matching `index.sqlite`, which holds titles, paths, the title and body full-text indexes, derived-text cache references, and build metadata. Copy the pair plus the source's `current` manifest; a `.faiss` copied without its `.sqlite` is unusable, and a `.sqlite` copied without its `.faiss` provides title and body lexical search only. A full index whose `.faiss` is missing serves lexical results, but a later full build requires `--force` to replace it.

Copying a subset is safe: each source generation is independent. Keep the same canonical source key and the same ZIM bytes on the target, and copy the generation plus its `current` manifest. A ZIM in another collection subfolder has a different key and needs a rebuild. Different content at the same relative path invalidates the index. The target must use the same embedding model, or an export whose probe embeddings match it (for example FP32 and int8 exports of the same encoder); otherwise `serve` ignores those vectors and falls back to title and full-text search. The target does not need the GPU used to build them: vector search runs on the CPU.

To copy two completed source generations:

```bash
for source in \
  indexes/zim/library-*/*/current \
  indexes/zim/library-*/*/generations/*; do
    printf '%s\n' "${source#indexes/}"
done > /tmp/index-generations.txt

rsync -a --files-from=/tmp/index-generations.txt indexes/ pi@rpi:~/.local/share/zimantic/indexes/
```

`rsync -a` copies only files whose size or mtime differs. Do not copy `staging-*.sqlite`, `*.tmp`, `*.sqlite-wal`, or `*.sqlite-shm`; staging files are unfinished builds, not published generations. Copy a complete generation directory and then its `current` manifest so a reload never opens a half-written pair. Run `zimantic reload` afterwards.

`catalog.sqlite` is optional for the copy. It contains metadata and icons, never vectors, and a missing catalog is rebuilt by source discovery. Its `path` column stores absolute locators, so copying it avoids metadata extraction only when the target resolves a collection to the same absolute paths and matching fingerprints; otherwise those rows are not reused. The ZIM scan opens each archive once for metadata, illustration, and counts, not article bodies. Files summaries read bounded root documents when their cached fingerprints do not match. A catalog with a different schema is rebuilt under the cross-process catalog lease. Both `build` and `serve` can create it.
