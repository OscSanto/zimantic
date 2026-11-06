# Phase 03 — Content stores and serving

Status: proposed.

## Goal

Replace archive-specific article serving with a `ContentStore` per source, so
one set of routes serves ZIM archives, filesystem trees, and Kolibri storage.
URLs use the canonical source key; there is no ZIM-specific URL identity.

## Current state

- `zimantic/content/reader.py` — ZIM entry parsing, redirects, previews, PDF and
  HTML extraction. It already has a narrow `EntrySource` protocol for
  `get_entry_by_id`, but the rest of the module assumes libzim entry shapes.
- `zimantic/content/serving.py` — `read_archive_article`,
  `read_archive_icon`, `random_indexed_article`, `random_fallback_article`.
  Errors: `ArticleNotFoundError`, `ArticleSourceNotFoundError`,
  `ArticleUnavailableError`, `ArticleTooLargeError`.
- `zimantic/search.py` — `read_article`, `read_article_head`,
  `article_path_exists`, `read_source_icon`, `random_article_path`,
  `random_local_source`.
- `zimantic/web/routes.py` — `/content/{zim}`, `/content/{zim}/{path}`,
  `/article/{zim}`, `/article/{zim}/{path}`, `/source-icon/{zim}`.
- `zimantic/web/app.py` — `_content_url`, `_article_url`, `_content_shell`,
  `_decorate_article_bridge`, `_article_content_response`.
- Result URLs use `source.key`. Remove `local_name` and the archive-stem
  special case.

## Target shape

```
zimantic/content/
    store.py         # canonical ContentStore protocol, data types, and errors
    path.py          # shared normalization/traversal helpers
    serving.py       # generic response helpers, random selection policy
    reader.py        # parsers/extractors; no provider-specific discovery

zimantic/providers/zim/
    store.py         # ArchiveContentStore over libzim
```

### Content identity

A source needs one identifier that is stable in URLs and resolves to its
content root. `SourceInfo.key` is that identifier; do not add a separate
`content_id`.

Rules:

- `key` is globally unique and namespaced by provider and collection. ZIM keys
  include a safe stem and root-relative digest; files and Kolibri keys use
  their provider prefixes. There is no archive-stem or alias lookup special case.
- URL helpers percent-encode the key as one path segment with
  `quote_path(key, safe='')`; for example, `files:modules:root` becomes
  `/content/files%3Amodules%3Aroot/...`. Keep this encoding consistent in
  generated links, referer parsing, the navigation bridge, and tests.
- `Search.resolve_content_source(identifier)` matches the exact `key` only.
  There is no `content_id`, alias, or collision-precedence fallback.
- Route parameter names change from `zim` to `source`; the URL shape stays
  `/content/{source}/{path}`.
- Path parameters and referers are decoded exactly once at the HTTP boundary;
  never introduce a second decode of `%3A` or content paths.
- `SourceInfo.to_dict()` exposes `key`; the web UI continues to use `icon_url`
  and generated links only.
- A store defines one unambiguous virtual path namespace. Files uses
  collection-relative paths for every source in that collection, including
  child-directory homepages and indexed paths. Source membership and serving
  authorization are deliberately different there; no route guesses whether
  a Files path is source-relative or collection-relative.

### `ContentStore`

The [phase-00 `ContentStore` contract](00-contracts-and-registry.md#protocols)
is canonical. This phase completes the phase-01 ZIM store with range-capable
reads and migrates routes to it; provider modules import the contract rather
than redeclare it.

Behavior:

- `resolve` follows redirects and returns the final path; the HTTP layer
  redirects if the requested path differs.
- `read` enforces `max_article_bytes` for a complete representation.
  `head_only` is a bounded prefix read for internal extraction, not the
  implementation of an HTTP HEAD response. Every `ArticleData` reports
  `total` when known and sets `truncated` whenever the returned bytes are not
  the complete representation; callers never infer completeness from
  `len(content)`.
- `read_range` uses inclusive `start`/`end` offsets and reports the full
  representation length in `ContentRange.total`. It reads only the requested
  range when the backing store supports seeking. A provider that cannot seek
  may fall back to a bounded full read, but must never exceed `max_bytes`.
- `icon` may return an archive illustration, a discovered favicon, or `None`.
- `random_path` is best-effort and bounded; do not scan an entire tree.
- Implementations must be read-only and thread-safe or documented otherwise.
- Configured store roots may be absolute and are retained privately by the
  runtime/store. Request paths remain normalized relative paths; rejecting an
  absolute request path does not prohibit an absolute configured root.

### ZIM adapter

`ArchiveContentStore` wraps an `Archive` and implements:

- `read_archive_article`, `read_archive_icon`, redirect resolution, MIME
  normalization, size limits, and error types.
- `random_indexed_article` / `random_fallback_article` behavior.
- Archive inspection paths used for `article_path_exists`.

`Search.read_article` etc. become thin delegates:

```python
source = self.resolve_content_source(identifier)
store = self.runtimes[source.collection_key].content_store(source)
return store.read(path, head_only=..., max_bytes=...)
```

If no source matches, raise `ArticleSourceNotFoundError` with the documented
HTTP status.

The route error contract is fixed:

| Condition | `/content` and `/article` response |
|---|---|
| Unknown source key | `404` with the standard not-found error envelope |
| Known source with no `ContentStore` (for example Kiwix) | `404` with the same envelope; do not redirect or proxy |
| Known but unavailable local source | `503` with a retryable source-unavailable error |
| Missing path in an available store | `404` with the standard not-found envelope |

`/article` includes its normal CORS headers on every row above. Error payloads
contain a stable error code and public source key only; they never include a
private locator or exception text.

### Routes

- Use `/content/{source}`, `/article/{source}`, `/source-icon/{source}`, and
  `/random`.
- Resolve the path parameter through `resolve_content_source`.
- Generate `/content/{key}/...` URLs from `source.key`.
- Preserve the route distinction: `/content` returns the viewer shell;
  `/article` returns the item representation and serves relative assets.
  Raw HTML is decorated with the existing bridge, so its response bytes may
  differ from the stored item.
- Rename `_referer_article_zim` to `_referer_source_key` and resolve any source
  key, not only ZIM. Decode the extracted path segment once with `unquote` so
  percent-encoded colons in `/content/<key>/...` and `/article/<key>/...`
  referrers match the canonical source key, and route `capture_asset_fallback`
  through `read_article(source_key, candidate)`.
- Rename `zim_icon_url` to `source_icon_url` and `zim_search_available` to
  `source_search_available`; both resolve by exact `key`.
- `random_local_source` selects any available local source with a homepage.
  `/random` redirects using that source key, including Files and Kolibri
  sources.
- `/random` and `/suggest` accept a `source` query parameter instead of `zim`.
  There is no `zim` parameter on `/random` or `/suggest`.
- `_decorate_article_bridge` and `_content_shell` remain source-agnostic; only
  the source key and base path change.
- A shell for large seekable media uses `resolve` metadata and must not reject
  the item merely because a full-body read would exceed `max_article_bytes`.

### Byte ranges (required by later media providers)

Add optional `Range` handling to the raw content response:

- Accept one `bytes=start-end`, `bytes=start-`, or `bytes=-suffix` range for an
  unchanged item representation.
  Ignore malformed or multiple ranges and serve the full response, subject to
  the normal full-response size limit.
- A satisfiable range returns `206` with `Content-Range`, `Content-Length`, and
  `Accept-Ranges: bytes`; an unsatisfiable range returns `416` with
  `Content-Range: bytes */<total>`.
- Apply `max_article_bytes` to the returned slice, not the total file size.
  If a requested range exceeds that cap, return a bounded subset as `206` with
  its actual `Content-Range` (for suffix ranges, keep the end of the item).
  This supports media clients that initially request `bytes=0-`. A non-seekable
  store may read the full item only if it fits the limit; otherwise return the
  existing too-large error.
- Evaluate ordinary conditional requests before ranges. Honor `If-Range` against
  the full representation when a suitable validator exists; otherwise treat it
  as a mismatch and send the full response subject to its limit. Ignore `Range`
  on HEAD, which returns appropriate full-representation headers and no body.
  Disable gzip for partial responses; any validators must describe the actual
  identity representation and not be confused with a compressed response's.
- Raw article responses currently use `Cache-Control: no-cache` and do not
  generate ETags. Preserve that policy; a newly added strong ETag must identify
  the actual bytes, not just file size/mtime or an absolute path. Do not read an
  entire large media item merely to produce a validator for a range request.
- Ignore ranges on bridge-decorated HTML and return the normal bounded `200`
  representation; byte offsets and validators for stored HTML do not describe
  the decorated response. HEAD metadata for that representation must likewise
  reflect the decorated response.
- Preserve `Access-Control-Allow-Origin: *` on `/article` success, redirects,
  and error responses, including `206`, `416`, and too-large errors. Expose
  range/validator headers needed by scripts in opaque-origin sandbox frames.

This can ship unused in this phase; files and Kolibri media need it.

### Security

- Reject encoded separators using the raw request path before the ASGI
  server's decoding; validate the decoded path once and never decode it again.
  Read the ASGI `raw_path` (or an equivalent server-preserved request target)
  for this check. If the deployment server does not preserve the raw path,
  reject ambiguous encoded-separator requests rather than claiming the check
  succeeded; document and test the supported server configuration.
  The shared normalizer rejects absolute paths, `..`, backslashes, NUL bytes,
  and empty paths where required. Literal percent-containing filenames such as
  `%23thumbnail` must still work when properly URL-encoded; double-encoded
  traversal must never become traversal through a later decode.
- Filesystem stores additionally:
  - resolve the real root once;
  - do not follow symlinks; use descriptor-relative, no-follow opens or an
    equivalent race-safe mechanism, not a check-then-open realpath check;
  - never expose paths outside the configured root in errors, payloads, or
    redirects.
- HTML is rendered through the content shell and the secure browsing switch.
  Do not add a second HTML viewer.

## Work items

- [ ] Implement `ContentRange`/`read_range` for ZIM on `ArchiveContentStore`;
      provider implementations import the phase-00 contract rather than define
      local variants.
- [ ] Create `zimantic/content/path.py`; move `normalize_article_path` and
      related path rules there.
- [ ] Complete the phase-01 `ArchiveContentStore` in
      `providers/zim/store.py` and migrate remaining serving logic to it.
- [ ] Use `SourceInfo.key` as the URL identity; do not add a `content_id` field.
- [ ] Add `Search.resolve_content_source` and replace ZIM-only lookups in
      `read_article`, `read_article_head`, `article_path_exists`,
      `read_source_icon`, `random_article_path`, `random_local_source`.
- [ ] Use the phase-01 `ZimRuntime.content_store(source)` for all local routes.
- [ ] Update `_result_doc` and the web URL helpers to use `key`.
- [ ] Update routes to resolve any provider source by key.
- [ ] Update `source_icon_url`, `source_search_available`, `random_local_source`,
      and `/random` to use `key`.
- [ ] Keep colon-containing keys percent-encoded in the source path segment and
      test generated URLs, route resolution, referers, and the navigation bridge.
- [ ] Add `Range` support to `_article_content_response`.
- [ ] Implement the range, HEAD, conditional-request, and size-limit semantics
      above, including large seekable media.
- [ ] Delete any remaining old `read_archive_*` functions after moving their
      logic into the ZIM store; do not leave deprecated shims.
- [ ] Add content-store contract tests that every local provider with a
      `ContentStore` must pass; Kiwix returns `None`.

## Tests

- [ ] Canonical-key URLs: `/content` returns the expected shell and `/article`
      returns the correct raw or bridge-decorated representation, MIME, and
      status; result URLs use the source key.
- [ ] Redirect chains, missing entries, too-large entries, invalid paths, and
      assets with unusual filenames.
- [ ] `/source-icon` prefers `-/icon`, then homepage links, then illustration.
- [ ] Random article works for indexed and unindexed ZIMs.
- [ ] Range: full, prefix, open-ended, suffix, out-of-range, multiple ranges,
      HEAD, `If-Range`, decorated HTML, and a file larger than the full limit.
      Exercise `bytes=0-` and playback across bounded chunks in a browser.
- [ ] Wildcard CORS survives normal, redirected, partial, unsatisfiable, and
      error `/article` responses for sandboxed in-article fetches.
- [ ] A source with no content store returns the documented 404/error shape.
- [ ] Traversal tests: `..`, encoded separators, double encoding, backslashes,
      NUL, absolute paths, literal percent filenames, and symlink replacement
      races (for the filesystem store in phase 05).
- [ ] Concurrent reads and reload/close do not race; runtime ownership is
      explicit so a store borrowed by an index handle is closed exactly once.

## Acceptance criteria

- `python -m pytest` passes, including the new content-store contract suite.
- No route contains ZIM-specific lookup logic.
- No provider-specific URL prefix exists.
- Every local provider's articles, redirects, assets, and homepages are served
  through the canonical source key.
- The raw response supports byte ranges without changing full-response
  caching or compression semantics.

## Risks and mitigations

- **URL churn.** Canonical keys replace archive stems. Update generated links,
  bookmarks documentation, and tests; do not keep a stem fallback.
- **Range/compression bugs.** Test `Range` against full, partial, and
  conditional requests. Do not gzip partial bodies.
- **Path traversal.** Centralize normalization and add adversarial tests before
  landing the filesystem store.
- **Thread safety.** Document each store's guarantees; ZIM stores rely on libzim
  entry reads being thread-safe and a courtesy `read_lock`.
