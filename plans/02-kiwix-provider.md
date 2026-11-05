# Phase 02 — Kiwix provider

Status: proposed.

## Goal

Formalize remote Kiwix as a provider. After this phase the core dispatches by
`source.provider` like any other source; there is no `mode` branch. Kiwix
remains metadata-only and delegates both search and serving to `kiwix-serve`.

## Current state

- `zimantic/searching/kiwix.py` — `catalog_entries()` and `search_kiwix()`,
  already returning `SourceResult`.
- `zimantic/search.py` — `_search_source` branches on `mode == "kiwix"`;
  `_search_kiwix()` is a thin wrapper; `_bounded_available_sources()` caps
  remote fan-out; `start_catalog_refresh()` fetches the catalog in a daemon
  thread; `refresh_sources()` merges remote entries with local sources.
- `zimantic/searching/sources.py` — bounded URL/response/XML helpers shared by
  Kiwix discovery.
- Remote results carry absolute `url` links from `kiwix-serve`; there is no
  content store.

## Target shape

```
zimantic/providers/kiwix/
    __init__.py
    provider.py      # KiwixProvider, KiwixRuntime
    catalog.py       # catalog fetch, parse, retry, cache
    search.py        # remote search request/parse
```

### Collection and source mapping

- **Collection** = one configured Kiwix server:
  `[[sources]] type = "kiwix"`, `name = "..."`, `server = "..."`. Collection key
  is `kiwix:<name>`; the label defaults to `"Kiwix"` and may include the host.
- **Source** = one catalog book. Source key is `kiwix:<name>:<book>`; duplicate
  slugs get a stable suffix. The book slug is also kept in provider metadata for
  the remote API. There are no alias keys.
- `SourceInfo.provider = "kiwix"`, `searchable = True`, `available = True`.
  There is no `content_kind` or `mode`.
- `SourceInfo.homepage`/`icon_url` remain absent unless the catalog provides
  them. Do not fetch remote icons during discovery.

### Runtime behavior

- `discover()` returns the cached catalog immediately when present; a refresh
  runs in a daemon thread and swaps the source set atomically when it changes.
- `open()` returns a runtime with the catalog entries and a
  `search(source, request)` implementation that calls `search_kiwix`.
- No local `ContentStore`; return `None`. Result documents keep absolute
  `url` links and are served by `kiwix-serve`, not by `/content/{key}/...`. The
  unified content route applies to local providers; Kiwix keeps external links.
- The runtime owns retry/backoff, response-size limits, and XML hardening.
- `refresh()` is non-blocking for the server startup path and keeps the last good
  catalog on failure.

### Fan-out budget

Remote sources are unbounded in a large Kiwix catalog, so keep the policy in the
provider:

- `CollectionRuntime.max_sources_per_search()` returns
  `max_remote_sources` for Kiwix (default 100; `0` disables unscoped remote
  search).
- The core applies the budget only to unscoped searches and still honors an
  explicit selection that names a source outside the budget.
- `source_timeout` bounds each remote source search.
- The per-provider task cap from the roadmap applies here first; a slow server
  cannot occupy every search worker.

Keep the one-time warning text and behavior for truncated remote fan-out. The
cap does not truncate catalog discovery or remove Kiwix books from paged
source/library APIs.

### Config

- `type`, `name`, `server`, `catalog_path`, `catalog_limit`,
  `max_response_bytes`, and `max_remote_sources` are the only Kiwix keys.
- There is no `kiwix_server` flat key. A Kiwix collection exists only through an
  explicit `[[sources]]` entry.
- The search page and JSON API use the canonical source key and provider fields;
  there is no additive compatibility payload.

## Work items

- [ ] Move `searching/kiwix.py` into `providers/kiwix/` and split catalog from
      search.
- [ ] Move Kiwix-only helpers out of `searching/sources.py` into
      `providers/kiwix/catalog.py`:
      `_bounded_catalog_url`, `_read_limited_response`, `_parse_remote_xml`,
      `_child_text`, `_local_name`, `_catalog_book`.
      Leave ZIM/icon helpers in `searching/sources.py` until phase 03.
- [ ] Implement `KiwixProvider.discover/open` and `KiwixRuntime`.
- [ ] Move `_catalog_entries`, `_source_from_catalog` remote branch,
      `_rebuild_sources` remote branch, and `_search_kiwix` out of `Search`.
- [ ] Move `start_catalog_refresh` and the cached-entry fallback into the
      runtime.
- [ ] Use the phase-00 `max_sources_per_search()` contract in
      `Search._search_scope`; do not branch on a mode.
- [ ] Make timed-out remote tasks cancellable or explicitly detached from the
      search result so they cannot consume provider slots after
      `source_timeout`.
- [ ] Delete the `mode == "kiwix"` branch from `_search_source`.
- [ ] Keep `source_dicts()` ordering (title casefold) and duplicate-book
      handling.

## Tests

- [ ] Catalog parse, retry/backoff, non-retryable 4xx, DTD rejection, and
      response-size limits (existing tests, relocated).
- [ ] Catalog failure keeps the last good source set and logs once.
- [ ] Remote search returns the expected documents/links/ordering.
- [ ] `max_remote_sources = 0` suppresses unscoped remote search; an explicit
      selection still searches.
- [ ] A slow remote source times out as `status="error"` without failing the
      merged search.
- [ ] No network access when no Kiwix collection is configured.

## Acceptance criteria

- `grep` finds no `mode` dispatch.
- A configured Kiwix server discovers books, searches them, and returns absolute
  links.
- A Kiwix outage cannot delay startup or break local search.
- `python -m pytest` passes.

## Risks and mitigations

- **Turning discovery into a startup dependency.** Keep discovery/cache
  semantics: cached first, background refresh, last-good fallback.
- **Remote cap semantics drifting.** Keep the "explicit selection overrides the
  cap" rule and its warning.
- **Security.** Keep the hardened XML parser and bounded reads; do not add HTML
  scraping of the Kiwix UI.
