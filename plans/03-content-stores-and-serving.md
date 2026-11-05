# Phase 03 — Content stores and serving

Status: proposed.

## Goal

Replace archive-specific article serving with a `ContentStore` per source, so
one set of routes serves ZIM archives, filesystem trees, and Kolibri storage.
Existing ZIM URLs and behavior must not change.

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
- Result URLs currently use `source.local_name` for ZIM, which is the archive
  stem, not the date-stripped source `key`. Preserve that.

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
content root. Add to `SourceInfo`:

```python
content_id: str = ""   # ZIM archive stem; source key for files/Kolibri
```

Rules:

- ZIM: `content_id = local_name` (archive stem). Existing links keep working.
- Files/Kolibri: `content_id = source key` (opaque, namespaced, no absolute
  path). URL helpers percent-encode it as one path segment with
  `quote_path(content_id, safe='')`; for example, `files:modules:algebra` is
  `/content/files%3Amodules%3Aalgebra/...`. Keep this encoding consistent in
  generated links, referer parsing, the navigation bridge, and tests.
- `Search.resolve_content_source(identifier)` matches `content_id`, then
  `key`, `name`, `book`, `local_name`. Exact matches must be globally unique;
  ambiguous aliases return a dedicated ambiguity error with a documented
  4xx mapping rather than selecting an arbitrary provider.
- New content IDs are stable and namespaced; URL helpers quote them as one
  path segment. Legacy ZIM IDs remain the archive stem for URL compatibility,
  so the resolver preserves their existing collision and alias precedence.
- Route parameter names change from `zim` to `source` internally, but the URL
  shape stays `/content/{source}/{path}`.
- Path parameters and referers are decoded exactly once at the HTTP boundary;
  never introduce a second decode of `%3A` or content paths.
- `SourceInfo.to_dict()` may expose `content_id` as additive metadata; the web
  UI continues to use `icon_url` and generated links only.

### `ContentStore`

The protocol below is the canonical contract introduced in phase 00 and
extended here with range-capable reads. Provider modules import it; they do not
redeclare it.

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

### ZIM adapter

`ArchiveContentStore` wraps an `Archive` and preserves exactly:

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

If no source matches, keep raising `ArticleSourceNotFoundError` with the same
HTTP status.

### Routes

- Keep every existing route and response status.
- Resolve the path parameter through `resolve_content_source`, not a ZIM-only
  lookup.
- Generate `/content/{content_id}/...` URLs from `source.content_id`.
- Keep `/source-icon/{source}` and `/article/{source}/...`.
- Preserve the referer-based ZIM extraction in
  `_referer_article_zim` by treating any source content ID as valid; rename it
  to `_referer_content_id` as part of this migration.
- Update `zim_icon_url` and `zim_search_available` to resolve by `content_id`
  (with the existing ZIM aliases as fallbacks), not only `local_name`.
- Update `random_local_source` to select any available local source with a
  `content_id` and homepage. `/random` must redirect using that `content_id`,
  including for Files and Kolibri sources whose `local_name` is `None`.
- Keep the legacy `/random?zim=...` query name for compatibility, but pass the
  percent-encoded `content_id` as its value.
- `_decorate_article_bridge` and `_content_shell` remain source-agnostic; only
  the content ID and base path change.

### Byte ranges (required by later media providers)

Add optional `Range` handling to the raw content response:

- Single `bytes=start-end` range only; multipart ranges may return `200`.
- `206 Partial Content`, `Content-Range`, `Accept-Ranges: bytes`.
- Read only the requested slice when the store supports it; otherwise read the
  full item subject to `max_article_bytes`.
- Disable gzip for partial responses.
- Keep existing full-content ETag/cache behavior.

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
- HTML is still rendered through the content shell and the existing secure
  browsing switch. Do not add a second HTML viewer.

## Work items

- [ ] Extend the canonical `zimantic/content/store.py` contract with
      `ContentRange`/`read_range`; provider implementations import it rather
      than define local variants.
- [ ] Create `zimantic/content/path.py`; move `normalize_article_path` and
      related path rules there.
- [ ] Implement `ArchiveContentStore` in `providers/zim/store.py` using the
      existing `content/serving.py` functions.
- [ ] Add `content_id` to `SourceInfo`; populate it in the ZIM provider.
- [ ] Add `Search.resolve_content_source` and replace ZIM-only lookups in
      `read_article`, `read_article_head`, `article_path_exists`,
      `read_source_icon`, `random_article_path`, `random_local_source`.
- [ ] Add `CollectionRuntime.content_store(source)` to `ZimRuntime`; keep a
      compatibility adapter for tests that construct `Search` partially.
- [ ] Update `_result_doc` and the web URL helpers to use `content_id`.
- [ ] Update routes to resolve any provider source; keep URL shapes.
- [ ] Update `zim_icon_url`, `zim_search_available`, `random_local_source`,
      and `/random` to use `content_id` while retaining ZIM alias behavior.
- [ ] Keep colon-containing IDs percent-encoded in the source path segment and
      test generated URLs, route resolution, referers, and the navigation bridge.
- [ ] Add `Range` support to `_article_content_response`.
- [ ] Define `Range` behavior precisely: `206` for one valid range, `416` for
      an unsatisfiable range, `HEAD` returns headers without a body, and
      conditional requests/ETags retain existing full-response semantics.
- [ ] Keep the old `read_archive_*` functions as deprecated shims that the ZIM
      store calls; delete them when nothing else imports them.
- [ ] Add content-store contract tests that every local provider with a
      `ContentStore` must pass; Kiwix remains an external-only provider.

## Tests

- [ ] URL parity: every existing `/content` and `/article` URL still returns
      the same bytes/MIME/status; result URLs are byte-identical for ZIMs.
- [ ] Redirect chains, missing entries, too-large entries, invalid paths, and
      assets with unusual filenames.
- [ ] `/source-icon` still prefers `-/icon`, then homepage links, then
      illustration.
- [ ] Random article works for indexed and unindexed ZIMs.
- [ ] Range: full, prefix, suffix, out-of-range, multiple ranges, HEAD.
- [ ] A source with no content store returns the same 404/error shape as an
      unknown ZIM today.
- [ ] Traversal tests: `..`, encoded separators, backslashes, NUL, absolute
      paths, symlink escape (for the filesystem store in phase 05).
- [ ] Concurrent reads and reload/close do not race; runtime ownership is
      explicit so a store borrowed by an index handle is closed exactly once.

## Acceptance criteria

- `python -m pytest` passes, including the new content-store contract suite.
- No route contains ZIM-specific lookup logic.
- No provider-specific URL prefix exists.
- Existing ZIM article links, redirects, assets, and homepages are unchanged.
- The raw response supports byte ranges without changing full-response
  caching or compression semantics.

## Risks and mitigations

- **URL breakage.** Preserve `content_id = local_name` for ZIM and add tests
  that assert exact result URLs.
- **Range/compression bugs.** Test `Range` against full, partial, and
  conditional requests. Do not gzip partial bodies.
- **Path traversal.** Centralize normalization and add adversarial tests before
  landing the filesystem store.
- **Thread safety.** Document each store's guarantees; ZIM stores already rely
  on libzim entry reads being thread-safe and a courtesy `read_lock`.
