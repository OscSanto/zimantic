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
  are `zim:<name>:<stem>`; files and Kolibri keys use their provider prefixes.
  There is no archive-stem or alias special case.
- URL helpers percent-encode the key as one path segment with
  `quote_path(key, safe='')`; for example, `files:modules:algebra` becomes
  `/content/files%3Amodules%3Aalgebra/...`. Keep this encoding consistent in
  generated links, referer parsing, the navigation bridge, and tests.
- `Search.resolve_content_source(identifier)` matches the exact `key` only.
  There is no `content_id`, alias, or collision-precedence fallback.
- Route parameter names change from `zim` to `source`; the URL shape stays
  `/content/{source}/{path}`.
- Path parameters and referers are decoded exactly once at the HTTP boundary;
  never introduce a second decode of `%3A` or content paths.
- `SourceInfo.to_dict()` exposes `key`; the web UI continues to use `icon_url`
  and generated links only.

### `ContentStore`

The protocol below is the canonical contract frozen in phase 00. This phase
implements it for ZIM, including range-capable reads. Provider modules import
it; they do not redeclare it.

```python
class ContentStore(Protocol):
    def main_path(self) -> str | None: ...
    def resolve(self, path: str | None) -> ContentEntry: ...
    def exists(self, path: str) -> bool: ...
    def read(self, path: str | None, *, head_only: bool, max_bytes: int) -> ArticleData: ...
    def read_range(
        self, path: str, *, start: int, end: int, max_bytes: int
    ) -> ContentRange: ...
    def random_path(self) -> str | None: ...
    def icon(self, max_bytes: int) -> ArticleData | None: ...
    def close(self) -> None: ...
```

Behavior:

- `resolve` follows redirects and returns the final path; the HTTP layer
  redirects if the requested path differs.
- `read` enforces `max_article_bytes` and `head_only`.
- `read_range` reads only the requested inclusive byte range when the backing
  store supports seeking. A provider that cannot seek may fall back to a
  bounded full read, but must never exceed `max_bytes`.
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

### Routes

- Use `/content/{source}`, `/article/{source}`, `/source-icon/{source}`, and
  `/random`.
- Resolve the path parameter through `resolve_content_source`.
- Generate `/content/{key}/...` URLs from `source.key`.
- Rename `_referer_article_zim` to `_referer_source_key` and resolve any source
  key, not only ZIM.
- Rename `zim_icon_url` to `source_icon_url` and `zim_search_available` to
  `source_search_available`; both resolve by exact `key`.
- `random_local_source` selects any available local source with a homepage.
  `/random` redirects using that source key, including Files and Kolibri
  sources.
- `/random` accepts a `source` query parameter. There is no `zim` parameter.
- `_decorate_article_bridge` and `_content_shell` remain source-agnostic; only
  the source key and base path change.

### Byte ranges (required by later media providers)

Add optional `Range` handling to the raw content response:

- Single `bytes=start-end` range only; multipart ranges may return `200`.
- `206 Partial Content`, `Content-Range`, `Accept-Ranges: bytes`.
- Read only the requested slice when the store supports it; otherwise read the
  full item subject to `max_article_bytes`.
- Disable gzip for partial responses.
- Keep full-content ETag/cache behavior.

This can ship unused in this phase; files and Kolibri media need it.

### Security

- All provider stores use the shared path normalizer after URL decoding; reject
  absolute paths, `..`, backslashes, NUL bytes, encoded separators, and empty
  paths where required. Double-encoded traversal must not become a valid path
  after a later decode.
- Filesystem stores additionally:
  - resolve the real root once;
  - reject symlink escapes when following symlinks (default: do not follow);
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
- [ ] Implement `ArchiveContentStore` in `providers/zim/store.py` using the
      existing `content/serving.py` functions.
- [ ] Use `SourceInfo.key` as the URL identity; do not add a `content_id` field.
- [ ] Add `Search.resolve_content_source` and replace ZIM-only lookups in
      `read_article`, `read_article_head`, `article_path_exists`,
      `read_source_icon`, `random_article_path`, `random_local_source`.
- [ ] Add `CollectionRuntime.content_store(source)` to `ZimRuntime`.
- [ ] Update `_result_doc` and the web URL helpers to use `key`.
- [ ] Update routes to resolve any provider source by key.
- [ ] Update `source_icon_url`, `source_search_available`, `random_local_source`,
      and `/random` to use `key`.
- [ ] Keep colon-containing keys percent-encoded in the source path segment and
      test generated URLs, route resolution, referers, and the navigation bridge.
- [ ] Add `Range` support to `_article_content_response`.
- [ ] Define `Range` behavior precisely: `206` for one valid range, `416` for
      an unsatisfiable range, `HEAD` returns headers without a body, and
      conditional requests/ETags retain full-response semantics.
- [ ] Delete the old `read_archive_*` functions after moving their logic into
      the ZIM store; do not leave deprecated shims.
- [ ] Add content-store contract tests that every local provider with a
      `ContentStore` must pass; Kiwix returns `None`.

## Tests

- [ ] Canonical-key URLs: every `/content` and `/article` URL returns correct
      bytes/MIME/status; result URLs use the source key.
- [ ] Redirect chains, missing entries, too-large entries, invalid paths, and
      assets with unusual filenames.
- [ ] `/source-icon` prefers `-/icon`, then homepage links, then illustration.
- [ ] Random article works for indexed and unindexed ZIMs.
- [ ] Range: full, prefix, suffix, out-of-range, multiple ranges, HEAD.
- [ ] A source with no content store returns the documented 404/error shape.
- [ ] Traversal tests: `..`, encoded separators, backslashes, NUL, absolute
      paths, symlink escape (for the filesystem store in phase 05).
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
