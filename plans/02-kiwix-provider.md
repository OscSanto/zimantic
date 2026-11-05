# Phase 02 — Kiwix provider

Status: proposed.

## Goal

Formalize remote Kiwix as a provider. After this phase the core has no
`mode == "kiwix"` branch: it dispatches by source provider like any other
source. Kiwix remains metadata-only and delegates both search and serving to
`kiwix-serve`.

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

- **Collection** = one Kiwix server (`kiwix_server`, later an explicit
  `[[sources]] type = "kiwix" server = "..."`). Key is stable and opaque;
  label defaults to `"Kiwix"` and may include the host.
- **Source** = one catalog book, as today. Preserve `book` slugs, display-name
  disambiguation, `perma_ref` keys, and dated/flavour collapsing.
- `SourceInfo.provider = "kiwix"`, `content_kind = "external"`,
  `mode = "kiwix"`, `searchable = True`, `available = True`.
- `SourceInfo.homepage`/`icon_url` remain absent unless the catalog provides
  them. Do not fetch remote icons during discovery.

### Runtime behavior

- `discover()` returns the cached catalog immediately when present; a refresh
  runs in a daemon thread and swaps the source set atomically when it changes.
- `open()` returns a runtime with the catalog entries and a
  `search(source, request)` implementation that calls `search_kiwix`.
- No local `ContentStore`; return `None`. Result documents keep absolute
  `url` links and continue to be served by `kiwix-serve`, not by
  `/content/{source}/...`. The unified content route applies to local
  providers; Kiwix keeps the existing external-link behavior.
- The runtime owns retry/backoff, response-size limits, and XML hardening.
- `refresh()` is non-blocking for the server startup path and preserves the
  last good catalog on failure.

### Fan-out budget

Remote sources are unbounded in a large Kiwix catalog, so keep the existing
behavior but move the policy to the provider:

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

### Config and compatibility

- `kiwix_server`, `kiwix_catalog_path`, `kiwix_catalog_limit`,
  `max_kiwix_response_bytes`, and `max_remote_sources` keep their meaning.
- `kiwix_server = ""` means no Kiwix provider/collection, exactly as today.
- The search page and JSON API payloads do not change except for additive
  `provider`/`collection` fields.

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
      `Search._search_scope` instead of `mode == "kiwix"`.
- [ ] Make timed-out remote tasks cancellable or explicitly detached from the
      search result so they cannot consume provider slots after
      `source_timeout`.
- [ ] Keep local sources authoritative for identities they share with remote
      entries.
- [ ] Delete the `mode == "kiwix"` branch from `_search_source`.
- [ ] Keep `source_dicts()` ordering (name casefold) and collisions.

## Tests

- [ ] Catalog parse, retry/backoff, non-retryable 4xx, DTD rejection, and
      response-size limits (existing tests, relocated).
- [ ] Catalog failure preserves the last good source set and logs once.
- [ ] Remote search returns the same documents/links/ordering as before.
- [ ] `max_remote_sources = 0` suppresses unscoped remote search; an explicit
      selection still searches.
- [ ] Local ZIM wins over a remote book with the same identity.
- [ ] A slow remote source times out as `status="error"` without failing the
      merged search.
- [ ] No network access when `kiwix_server` is unset.

## Acceptance criteria

- `grep` finds no `mode == "kiwix"` dispatch outside payload/serialization code.
- A Kiwix-configured server still discovers books, searches them, and returns
  the same absolute links.
- A Kiwix outage cannot delay startup or break local search.
- `python -m pytest` passes.

## Risks and mitigations

- **Turning discovery into a startup dependency.** Keep discovery/cache
  semantics identical: cached first, background refresh, last-good fallback.
- **Remote cap semantics drifting.** Preserve the exact "explicit selection
  overrides the cap" rule and its warning.
- **Security.** Keep the existing hardened XML parser and bounded reads; do not
  add HTML scraping of the Kiwix UI.
