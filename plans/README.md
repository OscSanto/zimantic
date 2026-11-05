# Source extensibility plans

Status: proposed, 2026-11-05.

This directory turns Zimantic from a ZIM search engine with a Kiwix remote
special case into a search engine whose core is source-agnostic, with in-tree
providers for ZIM, Kiwix, configured file directories, and Kolibri channels.

The plans are ordered. Each phase is intended to ship on its own and leave
`main` green. There are no compatibility promises: the redesign may change
URLs, API payloads, config keys, the catalog schema, and the index layout.
Every breaking change ships with a migration note and a rebuild path. Do not
build all phases on one long-lived branch.

## Locked decisions

These decisions come from the design discussion and are not open for
re-litigation inside the phase plans:

1. **Kolibri channel = collection.** A channel is not a source. Its searchable
   subtrees/content roots are sources.
2. **Derived text is opt-in, operation by operation.** Native text (titles,
   descriptions, document text) is indexed normally. Text derived from other
   media is off by default and enabled explicitly per source: `captions` for
   existing subtitle/caption tracks, `ocr` for images and image-only pages, and
   `asr` for audio/video speech-to-text. Enabling one never enables another, and
   no derived-text operation runs unless it is enabled.
3. **Files: one configured directory = one collection.** Within it, a source is
   the searchable unit and content root. Items (individual files/pages/videos)
   are never sources.
4. **In-tree providers only.** No third-party plugin API, entry-point
   discovery, or dynamic untrusted code in this project. Extensibility means a
   stable internal seam plus providers shipped in this repository.
5. **The core stays tight.** Fan-out, ranking, result merging, progressive
   streaming, caching, and source selection keep operating on `SourceInfo` and
   `SourceResult`. Provider-specific behavior lives behind the seam.
6. **No compatibility layers.** Do not carry legacy fields, aliases, URL
   shapes, config shorthands, catalog detection, or dual index formats. Replace
   the old design outright and document the migration.

## Vocabulary

| Term | Meaning | Public surface |
|---|---|---|
| Provider | In-tree adapter for one kind of content: `zim`, `kiwix`, `files`, `kolibri`. The only source discriminator. | No |
| Collection | A configured root, server, or channel. Groups sources; hierarchical providers bound their source count. | Yes, as grouping metadata |
| Source | The searchable unit and content root. `SourceInfo.key` is its identity and `/content/{key}/{path}` address. | Yes |
| Item | An article, file, content node, video, or other result row. | Yes, as a result |
| Content store | Path-addressed reader for one source (archive, filesystem tree, Kolibri storage tree). | No |
| Derived text | Text produced from non-native media during a build: existing caption tracks (`captions`), images (`ocr`), or audio/video (`asr`). Each operation is an independent opt-in. | No |

There is no `content_kind` or `mode` field. A source's provider determines what
it is and how it is served.

The scale rule that came out of the earlier `stash@{0}` attempt:

> Discovery may find many items, but it must produce a small, bounded number of
> sources. A folder of 300 PDFs is one source with 300 items by default, not
> 300 sources.

## Phase map

| Phase | Plan | Depends on | Primary outcome |
|---|---|---|---|
| 00 | [Contracts and registry](00-contracts-and-registry.md) | — | Freeze provider/collection/source/content contracts |
| 01 | [ZIM provider](01-zim-provider.md) | 00 | Local ZIM discovery/search behind the provider seam |
| 02 | [Kiwix provider](02-kiwix-provider.md) | 00, 01 | Remote Kiwix is a first-class provider, not a `mode` special case |
| 03 | [Content stores and serving](03-content-stores-and-serving.md) | 00, 01 | One `/content/{source}/{path}` route for every local content provider |
| 04 | [Generic index pipeline](04-generic-index-pipeline.md) | 00, 01, 03 | Reusable SQLite/FAISS build and index handles for non-ZIM entries |
| 05 | [Files provider](05-files-provider.md) | 03, 04 | Configured directories as collections with bounded sources |
| 06 | [Kolibri provider](06-kolibri-provider.md) | 03, 04, 05 | Channels as collections; content nodes as items; opt-in captions/OCR/ASR |
| 07 | [Collections, API, and UI](07-collections-api-ui.md) | 01–06 | Grouped library and source filters that do not explode |
| 08 | [CLI, config, and operations](08-cli-config-ops.md) | 01–07 | Build/serve/reload support for multiple providers; catalog rebuild and docs |
| 10 | [Ephemeral open command](10-open-command.md) | 01, 03, 08 | Open named ZIMs in a read-only local server; switch between them in the toolbar |
| 09 | [Hardening and rollout](09-hardening-rollout.md) | 00–08, 10 | Security, limits, contract tests, performance, staged release |

Phase 00 introduces the minimal `[[collections]]` parser and preserves both
existing search backends through real runtimes. It also freezes the common media
family taxonomy; phase 07 only presents that taxonomy in the UI. Phase 01
introduces the shared index locator; phase 04 extends its schema and validation,
requiring a rebuild at that phase as well. Phase 05 adds files-provider
validation; phase 08 completes the full settings loader. There is one parser
and one normalized entry shape, not a temporary parser per provider. Execute
phase 10 after phase 08 and before phase 09, which is the final release gate.

## Design invariants

Every phase must hold these:

- **One discriminator.** `provider` is the only source discriminator. There is
  no `mode`, no `content_kind`, and no derived alias field.
- **One identity.** `SourceInfo.key` is globally unique, stable, URL-safe, and
  the URL identity used by `/content/{key}/{path}` and `/article/{key}/{path}`.
  There are no `name`, `book`, or `local_name` lookup aliases.
- **One content route.** No `/kolibri/...`, `/files/...`, or `/modules/...`
  parallel content APIs. Kiwix remains external-only and returns absolute
  `kiwix-serve` links.
- **One index layout.** Every local indexed provider uses the same locator and
  index schema. Kiwix is remote/search-only. Old index files are not discovered
  or upgraded; they are rebuilt.
- **One config schema.** Collections are declared only with `[[collections]]`.
  There are no flat `zim_dir` or `kiwix_server` shorthands.
- **The catalog is disposable.** `catalog.sqlite` is the ZIM archive metadata
  cache; Files and Kolibri do not write it, and Kiwix has no persistent local
  catalog. An incompatible catalog is rebuilt after checking its schema version;
  no old-table inspection, row migration, or aliases.
- **Items never become sources.** Any provider that discovers many items maps
  them to a bounded source set.
- **Provider failure is isolated.** A broken or slow provider cannot crash the
  server or starve other providers of search slots.
- **Local providers do not use the network.** Discovery and reading are local
  and read-only unless a provider is explicitly remote (`kiwix`).
- **No unbounded walks.** Every directory/provider traversal has a root,
  exclusions where applicable, and explicit visit, entry, byte, and time
  budgets. Reaching a budget is reported, not mistaken for a complete scan.

## Cross-cutting budgets

These are defaults, not hard-coded promises. Each provider may tighten them.

| Budget | Default | Purpose |
|---|---|---|
| Sources per collection | 64 | Hard discovery cap for providers that can safely collapse sources; otherwise a fan-out/UI budget |
| Sources overall | 512 | Advisory target, not a hard in-memory cap; page API responses and bound search fan-out |
| Indexed items per source | 500,000 | Avoid pathological trees |
| Indexed bytes per item | global `max_entry_bytes` | One limit for every provider |
| Discovery time per provider | 10 s startup budget | Do not block serving on discovery |
| Entries examined per traversal | 1,000,000 | Includes excluded and non-indexable entries |
| Traversal time | 300 s | Hard work budget, distinct from the startup budget |
| Search tasks per provider | up to `min(4, search_workers)` | Fair scheduling; reserve capacity for competitors when possible |
| Remote sources per search | `max_remote_sources` | Bound remote fan-out |

When a hierarchical provider exceeds a discovery cap, it must collapse to a
coarser source granularity (for example, the whole collection as one source)
and log the collapse. Providers whose source identity is fixed by the content
(ZIM archives and Kiwix books) must not invent an aggregate source: they keep
the complete source set, expose it through paged APIs, and enforce limits only
on search fan-out. Discovery that cannot complete within its other budgets must
report an incomplete/error state and retain the last complete snapshot; it
must not present a prefix as complete. Indexing caps are separate: they may
reduce search coverage only when the source is explicitly marked truncated;
serving must continue to cover all content permitted by the provider's access
and exclusion rules.

## Definition of done for the whole effort

- A configured `/library/www/html/modules` directory appears as one collection
  with a bounded set of sources, is searchable, and serves its pages/assets
  through the normal content route.
- A Kolibri installation appears as one collection per channel, searchable by
  title/description, serving documents, PDFs, HTML5 apps, and media; derived
  text (captions, OCR, or ASR) is included only for the operations explicitly
  enabled on that source.
- Every source has one key; local providers use one content route and index
  layout, while Kiwix remains external-only. There are no provider-specific
  branches or compatibility shims in the core.
- The full test suite passes on Python 3.11–3.14, including provider contract
  tests, traversal/security tests, and catalog/rebuild tests.
- `config.toml`, `README.md`, `CONTRIBUTING.md`, and deployment examples
  document the `[[collections]]` schema and the uniform index layout.
- A Pi Zero 2 W can serve a mixed library within the documented memory
  envelope.

## Non-goals

- A public plugin marketplace or loading provider code from outside the package.
- Crawling remote websites or performing live web search.
- Network or hosted OCR/ASR services. Derived-text engines run locally during a
  build, against models provisioned ahead of time.
- Implicit enrichment. `captions`, `ocr`, and `asr` are independent opt-ins;
  selecting an engine does not enable an operation.
- Kolibri synchronization, content import, or writes to Kolibri's database.
- Distributed/remote indexing or a shared index server.
- Replacing libzim, FAISS, or the existing ranking design.

## Working agreement for each phase

1. Start from a green `main`; create one branch/worktree per phase.
2. Add the provider contract tests before moving behavior. Test the new
   contract directly; do not add old-payload parity tests.
3. Move behavior directly and delete the replaced implementation in the same
   phase. Do not add a temporary adapter, dual config path, or alias layer.
4. Update `plans/README.md` checkboxes and the phase document as work lands.
5. Finish with `python -m pytest` and manual `/search` and `/library` smoke
   tests. Include a non-ZIM source once phase 05 makes one available locally.
