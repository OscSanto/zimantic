# Phase 07 — Collections, API, and UI

Status: proposed.

## Goal

Expose collections as a first-class grouping without letting non-ZIM content
explode the source list. The flat source/search APIs and the grouped library
use the same canonical source records, so there is no separate compatibility
view.

The files provider defaults to `granularity = "collection"` until this phase
lands, so the flat fallback stays bounded even without the grouped view. Once
grouping is available, users can opt into directory granularity.

## Current state

- `SourceInfo.to_dict()` returns a flat source record.
- `/sources` returns a paged flat list.
- `/library.json` returns a paged flat list of local sources; the frontend
  stores it in `librarySources` and renders cards in `renderLibrary()`.
- Source filters in `app.js` (`sourceFilterLayout`, `renderSourceFilters`,
  `sourceFilterShowAll`, `sourceFilterShowOther`) show the top sources plus an
  "Other" catch-all.
- `SELECTIVE_SOURCE_LIMIT` / `SELECTIVE_SOURCE_RATIO` decide when a selection
  searches only the selected sources.
- `/config` exposes `source_filter_limit`.
- SSE source snapshots serialize `SourceInfo` through `to_dict()`.

The referenced stash (git diff 20997b04ee00537ab51b91d65c17df33ed6a7723^ 20997b04ee00537ab51b91d65c17df33ed6a7723) adds useful library facets: provider/source kind, coarse
media classification, and grouping of related files. Keep this plan's
collection-based identity instead of reviving its source-per-file model:
`provider` identifies the source kind, and collection cards group sources
without exposing a filesystem folder key or path.

## API design

There is one source payload. Do not add compatibility aliases or duplicate
fields.

### Source payload

Each source uses:

```json
{
  "key": "files:modules:root",
  "provider": "files",
  "media_types": ["text", "video"],
  "collection": {
    "key": "files:modules",
    "label": "Modules",
    "provider": "files"
  }
}
```

- `collection` is always present: it is an object for discovered sources and
  `null` for hand-built test sources. The object always contains `key`, `label`,
  and `provider`.
- `key` is the canonical source identity and the value used in
  `/content/{key}/...` and `/article/{key}/...`.
- Do not expose filesystem paths, database paths, or storage roots.
- `media_types` is an array drawn from `text`, `video`, `audio`, `image`, and
  `other`. It describes known content families and can contain multiple values.
  ZIM classification uses its MIME counter; other local providers use bounded
  provider metadata/classification, independent of indexing and derived-text
  settings. Use `null` only when no usable family was observed; otherwise list
  every observed family, including from partial classification. Use `[]` only
  when complete classification found no supported families. A complete empty
  collection is rescanned once and emits a warning if it remains empty. A source matches a specific content-type
  filter when `media_types` is non-null and contains the requested family; the
  unfiltered "All" view still includes both `null` and empty arrays. Providers
  populate this field; it is not a source identity.

### `/collections.json`

New endpoint, paged like `/sources`:

```json
{
  "items": [
    {
      "key": "files:modules",
      "provider": "files",
      "label": "Modules",
      "title": "Modules",
      "description": null,
      "source_count": 12,
      "media_types": ["text", "video"],
      "available": true,
      "error": null,
      "icon_url": null
    }
  ],
  "version": 7,
  "limit": null,
  "offset": 0,
  "total": 4
}
```

Use the existing paged JSON envelope and cache/version helpers. `source_count`
means all discovered sources, including unavailable or unsearchable sources;
health exposes separate available/searchable counts. `media_types` is the
snapshot-wide union of observed source families, not a summary of one page; it
is `null` only when no usable classification was observed for any member.
Unknown members do not erase known families from the union. `available` is
current runtime state and is never persisted. Serialize families in the
phase-00 taxonomy order. Add `collection_version()` to `Search`, invalidated
when collection membership, discovery state, or metadata changes.

### Collection-scoped search

Add an optional `collection` query parameter to `/search` and
`/search/stream`:

- It expands to all available, searchable sources in that collection before
  the normal source-selection rules.
- It composes with an explicit `source` list by intersecting them.
- An explicit selection always limits the actual search work to its resolved
  scope. Do not promote a large selection to an unscoped cached pool; doing so
  would search outside the collection and violate the intersection.
- An unknown explicit collection or source key returns HTTP 400 before a
  streaming response starts; never turn a stale selection into an unscoped
  search. If valid selectors resolve to an empty intersection, search no
  sources rather than broadening to the entire library.
- The cache key uses the expanded source keys plus the relevant source/index
  generation, not just the collection name. Invalidate affected pools when
  membership, content, or index generation changes even if the keys do not.
- A collection selector is explicit scope and bypasses the *unscoped* Kiwix
  fan-out cap, but not resource bounds. A Kiwix collection request schedules
  sources in canonical catalog order up to
  `max_explicit_collection_sources` (default 500) and the request deadline;
  it returns a `scope` summary when either bound is hit, for example:
  `{"scope":{"status":"partial","requested":700,"scheduled":500,"completed":498,"limit_reason":"source_cap"}}`.
  The `limit_reason` enum is `source_cap`, `deadline`, or `null`. On an
  unbounded successful request, `status` is `complete`. `requested` counts the resolved eligible source
  scope after selector intersection; `scheduled` counts admitted source tasks;
  `completed` counts tasks that reached a terminal result (including
  per-source errors). The regular `/search` response includes this summary;
  `/search/stream` emits the same summary in its final `search_summary` event.
  A per-source provider error is carried by that source's `SourceResult` and
  does not imply the scheduler hit a cap. An explicit individual source
  remains one task. Do not claim every selected book completed when the
  collection cap or deadline wins.

Use `source` as the canonical source-selection query parameter. There is no
`zim` parameter and no parallel source-selection API.

### `/library.json`

Return a flat, paged list of all providers' library sources, including
external-only Kiwix books. Each record carries `collection` and `provider`.
Kiwix links remain external; there is no local content store. Use
`/collections.json` for collection-wide counts and media summaries.

### `/health`

Add per-provider/collection counters:

```json
{
  "collections": {
    "files:modules": {"provider": "files", "sources": 12, "searchable": 12}
  }
}
```

Keep `indexes`, `sources`, and `cache` counters.

## UI behavior

### Library

- Group cards by collection. A collection with one source renders like today
  (one card, optionally with a small collection label).
- A collection with many sources renders a collection card/header with a count
  and a bounded number of preview cards; opening it shows its sources.
- Fetch collections in pages and fetch a bounded first page of sources for each
  visible collection; opening a group pages through its remaining sources.
  Never treat the first source page as the full collection. If page versions
  change while loading, refresh the group rather than silently mixing snapshots.
- Add a provider filter (All / ZIM / Files / Kolibri / Kiwix) alongside the
  existing text filter. Do not remove the text filter.
- Add an independent content-type filter (All / Text / Video / Audio / Images /
  Other). A source matches a selected type only when `media_types` contains
  that type and `media_types` is not `null`. Unknown sources are hidden for
  specific type filters rather than treated as possible matches. "All" does
  not apply this exclusion.
  Collection cards summarize the union of known source types. A collection is
  eligible for a specific type when its union contains that type; an unknown
  member with no observed types does not make the collection eligible by
  itself.
- The existing "search within this source" action remains; add "search within
  this collection" using the new `collection` parameter.
- Details/infobox continues to show source metadata; add provider and
  collection, not absolute paths.
- Preserve `/library` routing/history behavior (`syncLibraryInfo`,
  `showLibraryInfo`, `closeLibraryInfo`) and the shuffle action within the
  visible group.

### Source filters

- Group options by collection. Keep the existing top-N + "Other" logic per
  group so the panel stays bounded.
- At most one collection group is selected as a unit; selecting another
  replaces it. The UI sends the single `collection` parameter rather than
  hundreds of source keys. Selecting a group clears individual selections.
  Choosing an individual source while a group is selected switches to
  individual-selection mode, clearing the group; users can then select sources
  from different groups. API clients may still supply both selectors to
  intersect them without broadening the scope.
- Selected sources stay visible even when the group is collapsed.
- Keep the existing `source_filter_limit` behavior for ungrouped/top-level
  display; add a separate `collection_source_limit` config if needed.
- The filter search matches collection labels, source titles, and provider.

### Results

- Result rows still show the source title; optionally prefix the collection
  label when two sources share a title.
- No change to ranking, paging, previews, or progressive rendering.
- Source snapshots already include collection fields via `to_dict()`.

## Work items

- [ ] Reuse `CollectionSnapshot.to_dict()` and `Search.collection_dicts()`
      from phase 00; nested source payloads use the immutable collection
      descriptor rather than a stale runtime snapshot.
- [ ] Add `Search.collection_version()` and invalidate it with the source set.
- [ ] Add `/collections.json` to `zimantic/web/routes.py` using the existing
      paged response helper.
- [ ] Add `collection` to `SourceInfo.to_dict()` (phase 00) and ensure ZIM
      sources populate it.
- [ ] Add `collection` query parsing to `/search` and `/search/stream`; expand
      in `Search._search_scope` before selection.
- [ ] Key/invalidate cached pools using the expanded source keys and relevant
      source/index generations.
- [ ] Apply the documented Kiwix explicit-collection cap, deadline, canonical
      ordering, and partial-result metadata.
- [ ] Add `collection` filtering to `/sources` and `/library.json` using the
      standard pagination so opening a collection can fetch all its sources
      without an unbounded payload.
- [ ] Add `collection_source_limit` only if the UI cannot stay bounded with the
      existing limit; default to the global source limit.
- [ ] Update `/config` with the new limit and provider list.
- [ ] Update `app.js`:
      - fetch/cache `/collections.json` alongside `/sources`;
      - group `librarySources` by collection;
      - expose the content-type filter using `media_types`;
      - group `renderSourceFilters` options;
      - add provider filter and collection selection;
      - add "search within collection";
      - keep history/back-button behavior.
- [ ] Update `app.css` for collection headers/cards and grouped filters.
- [ ] Update `README.md` API examples and the library section.
- [ ] Update `tests/test_app_js.py` and the API tests.

## Tests

- [ ] `/sources` and `/library.json` use the canonical source payload; no
      `zim`, `content_id`, `mode`, or alias fields remain.
- [ ] `/collections.json` paging, version invalidation, and empty/error cases.
- [ ] Collection records use the same schema and nullability in APIs, CLI
      output, and SSE snapshots.
- [ ] `collection` search expands to the right sources; intersection with
      `source`; unknown selectors return a client error; cache key changes when
      collection membership changes.
- [ ] A large explicit selection never schedules a source outside its scope;
      switching between group and individual selection has predictable UI state.
- [ ] A Kiwix collection selection stops at its explicit collection cap or
      deadline deterministically and reports partial scheduled/completed counts.
- [ ] Collection search response and terminal stream summary use the same
      `scope` status/count fields; provider errors remain per-source results and
      are not confused with scheduler cap/deadline partial status.
- [ ] Library grouping: one-source collections, multi-source collections,
      empty collections, unavailable collections.
- [ ] Content-type classification and filtering for ZIM MIME counters and
      mixed Files/Kolibri sources, including exclusion of a truncated source
      with known types from specific filters; folder paths and absolute paths
      stay hidden.
- [ ] Replacing an index without changing source keys invalidates cached
      results and previews.
- [ ] Filter grouping: top-N + Other per collection; selected sources stay
      visible; collection select/deselect sends the expected request.
- [ ] Provider filter and text filter compose.
- [ ] "Search within collection" from a library card.
- [ ] Opening a collection fetches its paged sources through the documented
      collection filter; collection media facets remain correct when members
      appear on later pages and page versions change.
- [ ] Progressive results show the correct collection label.
- [ ] Keyboard/focus behavior in the source filter panel is preserved.
- [ ] No source explosion when a files collection has `max_sources` sources.

## Acceptance criteria

- A mixed library (ZIM + files + Kolibri) is navigable in `/library` by
  collection and provider.
- Source filters remain bounded and usable with hundreds of underlying sources.
- API clients use the canonical `source` query parameter.
- Selecting a collection searches all of its sources without a huge URL.
- `python -m pytest` and the JS tests pass.

## Risks and mitigations

- **UI complexity.** Reuse the existing panel/card rendering paths; add
  grouping as a transform on the data, not a second renderer.
- **Cache staleness.** Never cache on a collection name alone; use the expanded
  source keys and version tokens.
- **Truncated groups.** Prefer an explicit "Other"/expand control over silently
  hiding sources.
- **Payload size.** Collections and sources are paged; do not duplicate full
  source metadata inside collection records.
