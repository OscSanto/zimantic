# Phase 07 — Collections, API, and UI

Status: proposed.

## Goal

Expose collections as a first-class grouping without letting non-ZIM content
explode the source list. Keep the flat source/search APIs working, and make the
library and source filters usable when a files collection has dozens of sources
or a Kolibri channel has many topics.

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

The stash's `source_kind`, `media_type`, and folder-card UI are useful ideas,
but its flat source-per-file identity model is not adopted.

## API design

All changes are additive.

### Source payload

Each source gains:

```json
{
  "provider": "files",
  "content_id": "files:modules:algebra",
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
- `content_id` is the value used in `/content/{content_id}/...`; for ZIM it is
  the archive stem, preserving links.
- Do not expose filesystem paths, database paths, or storage roots.

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
health exposes separate available/searchable counts. Add `collection_version()`
to `Search`, invalidated when the collection set or its metadata changes.

### Collection-scoped search

Add an optional `collection` query parameter to `/search` and
`/search/stream`:

- It expands to all available sources in that collection before the normal
  source-selection rules.
- It composes with an explicit `zim` list by intersecting them.
- An unknown collection is ignored with a warning and falls back to the normal
  scope only when no valid collection/source selector remains. If selectors
  resolve to an empty intersection, search no sources rather than broadening to
  the entire library.
- The cache key uses the expanded source keys, not the collection name, so
  adding/removing a source cannot serve a stale pool.

Keep `zim` as the canonical source-selection parameter. Do not add a parallel
source-selection API.

### `/library.json`

Keep the flat list and existing pagination. Each card carries `collection` and
`provider` so the client can group without a second request. Optionally add a
`group=collection` query later; do not change the default shape now.

### `/health`

Add per-provider/collection counters without removing existing fields:

```json
{
  "collections": {
    "files:modules": {"provider": "files", "sources": 12, "searchable": 12}
  }
}
```

Keep `indexes`, `sources`, and `cache`.

## UI behavior

### Library

- Group cards by collection. A collection with one source renders like today
  (one card, optionally with a small collection label).
- A collection with many sources renders a collection card/header with a count
  and a bounded number of preview cards; opening it shows its sources.
- Add a provider filter (All / ZIM / Files / Kolibri / Kiwix) alongside the
  existing text filter. Do not remove the text filter.
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
- A collection group can be selected/deselected as a unit; the UI sends the
  `collection` parameter rather than hundreds of source keys.
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

- [ ] Reuse `CollectionInfo.to_dict()` and `Search.collection_dicts()` from
      phase 00.
- [ ] Add `Search.collection_version()` and invalidate it with the source set.
- [ ] Add `/collections.json` to `zimantic/web/routes.py` using the existing
      paged response helper.
- [ ] Add `collection` to `SourceInfo.to_dict()` (phase 00) and ensure ZIM
      sources populate it.
- [ ] Add `collection` query parsing to `/search` and `/search/stream`; expand
      in `Search._search_scope` before selection.
- [ ] Keep `_cache_key` based on the expanded source keys.
- [ ] Add optional `collection` filtering to `/sources` and `/library.json`,
      preserving their existing pagination so opening a collection can fetch
      all of its sources without an unbounded payload.
- [ ] Add `collection_source_limit` only if the UI cannot stay bounded with the
      existing limit; default to the global source limit.
- [ ] Update `/config` with the new limit and provider list.
- [ ] Update `app.js`:
      - fetch/cache `/collections.json` alongside `/sources`;
      - group `librarySources` by collection;
      - group `renderSourceFilters` options;
      - add provider filter and collection selection;
      - add "search within collection";
      - keep history/back-button behavior.
- [ ] Update `app.css` for collection headers/cards and grouped filters.
- [ ] Update `README.md` API examples and the library section.
- [ ] Update `tests/test_app_js.py` and the API tests.

## Tests

- [ ] `/sources` and `/library.json` remain valid for old clients; new fields
      are additive and paths are absent.
- [ ] `/collections.json` paging, version invalidation, and empty/error cases.
- [ ] Collection records use the same schema and nullability in APIs, CLI
      output, and SSE snapshots.
- [ ] `collection` search expands to the right sources; intersection with
      `zim`; unknown collection falls back; cache key changes when the
      collection membership changes.
- [ ] Library grouping: one-source collections, multi-source collections,
      empty collections, unavailable collections.
- [ ] Filter grouping: top-N + Other per collection; selected sources stay
      visible; collection select/deselect sends the expected request.
- [ ] Provider filter and text filter compose.
- [ ] "Search within collection" from a library card.
- [ ] Opening a collection fetches its paged sources through the documented
      collection filter.
- [ ] Progressive results show the correct collection label.
- [ ] Keyboard/focus behavior in the source filter panel is preserved.
- [ ] No source explosion when a files collection has `max_sources` sources.

## Acceptance criteria

- A mixed library (ZIM + files + Kolibri) is navigable in `/library` by
  collection and provider.
- Source filters remain bounded and usable with hundreds of underlying sources.
- Existing API consumers and bookmarked search URLs keep working.
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
