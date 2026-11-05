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
  `[[collections]] type = "kiwix"`, `name = "..."`, `server = "..."`. Collection key
  is `kiwix:<name>`; the label defaults to `"Kiwix"` and may include the host.
- **Source** = one addressable catalog book. The key uses a safe slug plus a
  digest of the exact remote book slug, under `kiwix:<name>:`. Keep that remote
  slug privately for the remote API. Exact duplicate slugs name the same remote
  endpoint and are deduplicated; distinct dated or flavoured slugs are not
  merged. Detect key collisions instead of overwriting. There are no aliases.
- `SourceInfo.provider = "kiwix"`. A book in the last complete catalog is
  `available = True` because it remains addressable, but `searchable` reflects
  current remote-search capability: it is false after a known outage or
  request-health failure and true after a successful probe/search. A refresh
  failure retains catalog visibility without claiming the server is reachable.
  There is no `content_kind` or `mode`.
- `SourceInfo.homepage`/`icon_url` remain absent unless the catalog provides
  them. Do not fetch remote icons during discovery.

### Runtime behavior

- Entry discovery creates the configured server collection without waiting for
  the network. Its runtime exposes the last complete in-memory catalog, if
  any, while a background refresh runs and atomically publishes a complete
  replacement.
- `open()` returns a runtime with the catalog entries and a
  `search(source, request)` implementation that calls `search_kiwix`.
- No local `ContentStore`; return `None`. Result documents keep absolute
  `url` links and are served by `kiwix-serve`, not by `/content/{key}/...`. The
  unified content route applies to local providers; Kiwix keeps external links.
- Validate result URLs and redirects: only HTTP(S) links on the configured
  Kiwix origin are accepted, with no embedded credentials. Resolve relative
  links against that origin; reject `javascript:`, `data:`, scheme-relative
  foreign hosts, and cross-origin redirects rather than exposing or fetching
  them.
- The runtime owns retry/backoff, response-size limits, and XML hardening.
- `refresh()` is non-blocking for the server startup path and keeps the last good
  catalog on failure.
- `catalog_limit` is a per-page request size, not a total-book cutoff. Fetch
  pages within explicit response-byte, total-entry, and time budgets. If the
  server cannot be paged or the refresh cannot finish consistently within
  those budgets, report an incomplete/error state and keep the last complete
  snapshot; never silently publish a truncated catalog as complete.

### Fan-out budget

Remote sources are unbounded in a large Kiwix catalog, so keep the policy in the
provider:

- `CollectionRuntime.max_sources_per_search()` returns
  `max_remote_sources` for Kiwix (default 100; `0` disables unscoped remote
  search).
- The core applies the budget only to unscoped searches and still honors an
  explicit selection that names a source outside the budget.
- `source_timeout` bounds each remote source search.
- The per-provider task cap and fair scheduling apply here first. With more
  than one worker and competing providers, reserve capacity for other providers;
  with one worker, fairness is bounded by the in-flight task's deadline.

Keep the one-time warning text and behavior for truncated remote fan-out. The
cap does not truncate catalog discovery or remove Kiwix books from paged
source/library APIs.

### Config

- `type`, `name`, `label`, `server`, `catalog_path`, `catalog_limit`,
  `max_response_bytes`, and `max_remote_sources` are the Kiwix keys, alongside
  the common bounded-discovery settings.
- There is no `kiwix_server` flat key. A Kiwix collection exists only through an
  explicit `[[collections]]` entry.
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
- [ ] Bound connect/read and total request time, cancel queued work on timeout,
      and detach timed-out results from the response. A running thread retains
      its worker/provider slot until it actually exits; detaching a future does
      not cancel the underlying I/O.
- [ ] Delete the `mode == "kiwix"` branch from `_search_source`.
- [ ] Keep `source_dicts()` ordering (title casefold) and duplicate-book
      handling.

## Tests

- [ ] Catalog parse, retry/backoff, non-retryable 4xx, DTD rejection, and
      response-size limits (existing tests, relocated).
- [ ] Catalog failure keeps the last good source set and logs once.
- [ ] Multiple catalog pages are discovered; a budget-exhausted or unpageable
      catalog is reported as incomplete rather than silently truncated.
- [ ] Remote search returns the expected documents/links/ordering.
- [ ] Unsafe or cross-origin result links and redirects are rejected without
      exposing credentials or interrupting other providers.
- [ ] `max_remote_sources = 0` suppresses unscoped remote search; an explicit
      selection still searches.
- [ ] A slow remote source times out as `status="error"` without failing the
      merged search.
- [ ] Blocked remote work cannot starve local work, including with a small
      worker pool; timed-out work is counted until it really exits.
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
