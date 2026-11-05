# Phase 00 — Contracts and provider registry

Status: proposed.

## Goal

Introduce the internal seam that every later phase depends on. This phase is
structural: it defines the provider, collection, source, and content contracts
and the registry. Existing ZIM and Kiwix search continue working through real
runtimes. The old `mode`/`content_kind` shape is replaced, not carried forward.

## Non-goals

- Move the per-source dispatch and search implementations needed for real
  runtimes; phase 01 moves the remaining ZIM discovery and lifecycle, and phase
  02 moves the remaining Kiwix catalog lifecycle.
- Do not change `SourceResult`, ranking, fan-out, caching, or streaming.
- Do not publish a third-party plugin API.
- Do not rename the public `{zim}` route parameters yet. Phase 03 owns the
  route migration after the source-key contract is in place.

## Current state

The concepts already exist but are conflated:

- `zimantic/contracts.py` — `SourceInfo` is identity + search unit + content
  root. `SourceResult` is the provider boundary for one source.
- `zimantic/search.py` — `Search` owns discovery, index lifecycle, search,
  previews, article reads, and routing decisions. `_search_source` dispatches
  on `source.mode`.
- `zimantic/searching/sources.py` — ZIM-specific discovery helpers
  (`_book_identity`, `_intent_phrases`, icon discovery).
- `zimantic/searching/kiwix.py` — effectively a proto-provider with
  `catalog_entries` and `search_kiwix`.
- `zimantic/web/routes.py` — content routes are parameterized as `{zim}`.

## Target contracts

Create `zimantic/providers/`:

```
zimantic/providers/
    __init__.py
    base.py          # provider protocols and dataclasses
    registry.py      # in-tree provider registry
    context.py       # ProviderContext helpers (optional; may live in base)
```

`ContentStore`, `ContentEntry`, `ArticleData`, and range-response types have one
canonical definition in `zimantic/content/store.py`. `providers/base.py`
imports and re-exports those content contracts; it must not define a second
copy. The phase-00 contract tests cover both import paths and pin the complete
`ContentStore` method set (including `read_range`) even though a runtime may
return `None` until its store lands, so the contract is genuinely frozen here.

### Dataclasses

Names are indicative; freeze them in this phase and document changes. The
content dataclasses below are shown for the public shape but are implemented
only in `content/store.py`, then re-exported by `providers/base.py`.

```python
@dataclass(frozen=True)
class ProviderContext:
    cfg: Mapping[str, Any]
    index_dir: Path
    web_base_path: str
    logger: logging.Logger

@dataclass(frozen=True)
class CollectionInfo:
    key: str                 # stable, opaque; safe in URLs and payloads
    provider: str            # "zim" | "kiwix" | "files" | "kolibri"
    label: str               # short display name
    title: str | None = None
    description: str | None = None
    icon_url: str | None = None
    source_count: int = 0
    media_types: tuple[str, ...] | None = None
    available: bool = True
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class SourceQuery:
    query: str
    title_query: str
    fulltext_query: str
    query_vector: Any | None
    limit: int

@dataclass(frozen=True)
class ContentEntry:
    path: str
    title: str
    mimetype: str
    size: int | None = None
    redirect_path: str | None = None

@dataclass(frozen=True)
class ArticleData:
    source_key: str
    path: str
    title: str
    mimetype: str
    content: bytes

@dataclass(frozen=True)
class ContentRange:
    source_key: str
    path: str
    title: str
    mimetype: str
    content: bytes
    start: int
    end: int
    total: int
```

### Protocols

```python
# Imported from the canonical content contract:
from zimantic.content.store import (
    ArticleData,
    ContentEntry,
    ContentRange,
    ContentStore,
)

class CollectionRuntime(Protocol):
    collection: CollectionInfo
    @property
    def sources(self) -> Sequence[SourceInfo]: ...
    def library_sources(self) -> Sequence[SourceInfo]: ...
    def search(self, source: SourceInfo, request: SourceQuery) -> SourceResult: ...
    def content_store(self, source: SourceInfo) -> ContentStore | None: ...
    def max_sources_per_search(self) -> int | None: ...
    def refresh(self, *, wait: bool = True) -> bool: ...
    def wait_ready(self, timeout: float | None = None) -> bool: ...
    @property
    def discovery_state(self) -> str: ...  # loading | ready | error | closed
    @property
    def snapshot_version(self) -> int: ...
    def close(self) -> None: ...

class SourceProvider(Protocol):
    kind: str
    def discover(
        self, entry: Mapping[str, Any], ctx: ProviderContext
    ) -> Iterable[CollectionInfo]: ...
    def open(
        self, entry: Mapping[str, Any], collection: CollectionInfo, ctx: ProviderContext
    ) -> CollectionRuntime: ...
```

The canonical `ContentStore` protocol defined in `content/store.py` is:

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

`head_only` is a bounded content-prefix read for internal preview/metadata
extraction, not an HTTP HEAD response. HTTP HEAD returns no body; it uses
`resolve` metadata for unchanged content and bounded representation processing
when HTML decoration changes the response. Phase 03 specifies the remaining
error, range, redirect, and ownership rules.

Notes:

- `discover()` receives one normalized configured entry and returns its
  collection descriptors, not items. One entry may produce several collections
  (Kolibri), or none. It is bounded; slow discovery runs in the background.
  Configured-entry diagnostics exist independently of collection descriptors,
  so a failed multi-collection discovery is still visible.
- `CollectionRuntime.search()` is one source's work. Providers whose native
  query scope is broader than one source map that source to the appropriate
  subtree or content-node filter. Indexed providers still use the generic
  entry/index pipeline.
- `ContentStore` is defined in `content/store.py` during this phase. The ZIM
  store starts in phase 01 and is completed in phase 03; other local providers
  implement it later. Kiwix always returns `None`.
- `library_sources()` defaults conceptually to `sources`; ZIM overrides it to
  include unreadable archives. `sources` includes readable but unsearchable
  sources; search scheduling filters on both `available` and `searchable`.
  Content lookup also resolves unsearchable library sources, and
  `CollectionInfo.source_count` counts `library_sources`, not just searchable
  sources.
- `max_sources_per_search()` returns `None` for providers without a special
  fan-out cap. Refreshes publish an immutable source/collection snapshot
  atomically and increment `snapshot_version`; non-blocking refreshes expose
  `loading` until the new snapshot is published.
- `CollectionInfo` is a descriptor and remains frozen. Dynamic source counts,
  availability, errors, and discovery state come from the current runtime
  snapshot; serializers must not mutate a previously published descriptor.
- Provider-private `metadata` and config must not be serialized wholesale;
  expose only explicitly approved fields and never filesystem locators.
- `media_types` is the union of observed source media families in the
  snapshot. `None` means no usable classification was observed; a non-null
  tuple lists families that were observed, even when the scan was partial.
  An empty tuple is published only after a complete classification finds no
  supported families. A collection with no observed families is rescanned once
  and emits a warning if the rescan is still empty.
- A specific content-type filter is conservative: a source or collection with
  `media_types is None` does not match it. The unfiltered "All" view still
  includes both unknown (`None`) and known-empty (`()`) sources.
- A `sources` sequence is a snapshot, not an item-level manifest. API paging
  limits response size, not necessarily snapshot memory; the roadmap's 512
  sources is an advisory target, not a promise of lazy source storage.
- `SourceResult` and its four ranking channels (`semantic`, `keyword`,
  `fulltext`, `items`) stay unchanged.

### `SourceInfo` shape

`SourceInfo` has one discriminator and one identity:

```python
key: str                       # globally unique, URL-safe source key
provider: str                  # required; never infer a fallback provider
title: str | None = None       # display name
collection_key: str = ""
collection_label: str | None = None
media_types: tuple[str, ...] | None = None
indexed_count: int | None = None
truncated: bool | None = None
index_freshness: str | None = None  # current | stale | unknown; None if no index
```

- `provider` is the canonical discriminator. There is no `content_kind`, no
  `mode`, and no derived alias field.
- `key` is globally unique, stable, and URL-safe. It is also the URL identity
  used by `/content/{key}/...` and `/article/{key}/...`; there is no separate
  `content_id` field.
- `title` is the display name. Remove `name`, `book`, `local_name`, and their
  alias precedence. Provider-specific identifiers such as a Kiwix book slug or a
  ZIM archive stem live in provider metadata or in the canonical `key`.
- `indexed_count` and `truncated` describe the published index; both are
  unknown (`null`) before one exists. Freshness is `unknown` until validated.
  Later phases populate these fields without introducing alternate payloads.
- `media_types is None` means no usable classification was observed for the
  source. A non-null tuple lists observed families and may be partial;
  incidental web assets need not count as separate content items.
- A source matches a specific content-type filter only when `media_types` is
  not `None` and contains the requested family. Unknown classification is
  intentionally excluded from specific filters.

`SourceInfo.to_dict()` emits canonical identity fields alongside the common
display/status fields. This is an excerpt, not the complete payload:

```json
{
  "key": "zim:library:wikipedia_en_all-0123456789abcdef0123456789abcdef",
  "provider": "zim",
  "collection": {"key": "zim:library", "label": "Library", "provider": "zim"}
}
```

`collection` is always present and is `null` when no collection metadata is
available (for example, a hand-built test source). Do not expose local absolute
paths.

### Logical identity and filesystem locators

- Treat `key` as the canonical internal source identity. `key` is globally
  unique and namespaced by provider and collection; never derive it from an
  absolute filesystem path.
- Require configured names to match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`;
  reserve the ZIM name `open` for phase 10. Encode or slug provider identifiers
  into URL-safe components and include a collision-resistant digest of the
  original identifier when needed. Detect collisions instead of overwriting.
  Phase 01 specifies ZIM identity; provider-specific examples are illustrative.
- Absolute configured roots are valid. Providers may resolve them once and
  retain the canonical absolute path as a private runtime or catalog locator;
  it is not part of `SourceInfo`, `key`, or any serialized payload.
- The persistent `catalog.sqlite` is the ZIM archive metadata cache. It uses a
  provider-neutral `sources` table keyed by `key`; other providers are not
  required to write it. Store a canonical absolute path in a separate indexed
  `path` locator column; it is not part of the key.
- Relocating a configured root without changing its root-relative layout updates
  locators, not keys. Renaming or moving an item within that root may change
  its key; providers document that boundary. Keep filesystem path
  normalization out of per-query identity resolution.

### Registry

`zimantic/providers/registry.py`:

- `builtin_providers() -> list[SourceProvider]`
- `get_provider(kind: str) -> SourceProvider`
- `register(provider)` for tests only.
- Registry lookup raises a clear config-validation error rather than silently
  selecting a fallback provider. The config layer reports an invalid entry
  without blocking other entries, as specified below and in phase 08.

Register working `zim` and `kiwix` runtimes in this phase. Move their existing
per-source search code directly behind those runtimes, without a mode fallback;
their remaining discovery/lifecycle code moves in phases 01 and 02.

### Minimal collection configuration

This phase owns the minimal `[[collections]]` TOML parser. It parses the
array-of-tables shape into normalized provider entries, validates `type` and
`name`, rejects duplicate names, and preserves provider-specific keys for the
provider validators that follow. It must not depend on provider modules that
are introduced later. Flat `zim_dir` and `kiwix_server` shorthands are not
accepted.

Invalid TOML is fatal. Other invalid entries produce config diagnostics without
blocking valid entries; disable every entry sharing a duplicate name rather
than choosing one. Entries without a valid name/type have no invented
collection key. Provider-specific validation belongs to the provider. Phase 05
adds files validation, and phase 08 completes the loader without another parser.

### `Search` integration skeleton

In `Search.__init__`:

- `self.providers: dict[str, SourceProvider]`
- `self.runtimes: dict[str, CollectionRuntime]`
- `self.collections: dict[str, CollectionInfo]`

Add:

- `Search.resolve_source(identifier: str) -> SourceInfo | None` — one place
  that matches an exact `key`. Do not add name, book, local-name, dated, or
  flavoured alias matching.
- `Search.refresh_collections(...)` — a thin loop over configured entries and
  their providers.
- `Search.collection_dicts()` — serialization helper for later API work.

`_search_source` dispatches by `source.provider` from this phase onward. Do not
keep a `mode` fallback.

## Work items

- [ ] Create `zimantic/providers/base.py` with the dataclasses and protocols.
- [ ] Create the canonical content-contract module at
      `zimantic/content/store.py` and re-export it from `providers/base.py`.
      Define the complete `ContentStore` protocol, including `read_range`; phase
      03 implements it rather than extending the contract.
- [ ] Create `zimantic/providers/registry.py`; register working `zim` and
      `kiwix` runtimes and move the existing per-source search dispatch into
      them.
- [ ] Add the normalized provider-configuration seam and minimal
      `[[collections]]` TOML parser described above; providers consume normalized
      entries and later phases add provider-specific validation. Do not keep
      flat shorthands.
- [ ] Migrate the shipped config, deployment examples, and documentation that
      use flat provider keys in this phase. Keep global TOML keys before the
      first `[[collections]]` table so they do not become collection fields.
- [ ] Extend `SourceInfo` and `to_dict()` in `zimantic/contracts.py`.
- [ ] Add `CollectionInfo` (in `contracts.py` or `providers/base.py`; keep one
      public import path).
- [ ] Add provider/collection state and `resolve_source` to `Search`.
- [ ] Add `CollectionInfo.to_dict()`, `collection_dicts()`, and make
      `source_dicts()` include the new fields using the frozen nullability/schema
      rules above.
- [ ] Update route resolution internals to use the canonical `key` and exact
      source lookup; leave the public `{zim}` to `{source}` parameter rename
      to phase 03.
- [ ] Add `tests/providers/test_contracts.py` and
      `tests/providers/test_registry.py`.
- [ ] Add a short "Provider contracts" section to `CONTRIBUTING.md`.

## Tests

- `SourceInfo.to_dict()` snapshots for ZIM and non-ZIM values; ensure the
  payload contains `provider` and `collection`, contains no `mode`,
  `content_kind`, `name`, `book`, or `local_name`, and leaks no absolute path.
- Absolute configured roots are accepted without changing logical source
  identity; private catalog locators never appear in source serialization.
- `CollectionInfo.to_dict()` snapshots use the same fields and nullability in
  API, CLI, and SSE serialization.
- `resolve_source` matches exact `key` values only.
- Registry returns the two built-ins and rejects unknown kinds; configured
  ZIM and Kiwix searches both continue working in this phase.
- Constructing a source without an explicit provider fails rather than silently
  treating it as ZIM.
- `provider` is the only discriminator; no serializer emits a derived kind or
  mode.
- The frozen `ContentStore` method set is unchanged when phase 03 implements it.
- One configured entry can discover multiple collections; invalid or duplicate
  entries produce diagnostics without disabling unrelated valid entries.
- Search and API tests are updated to the new payload in the same phase; there
  is no old-payload compatibility suite.

## Acceptance criteria

- `python -m pytest` passes.
- Searching, `/sources`, `/library.json`, `/content/...`, and `/health` work
  through the new source key and contract.
- No provider-specific branching is added to ranking, caching, or streaming.
- The new contracts are importable without importing libzim, FAISS, or FastAPI.

## Risks and mitigations

- **Contract churn.** Keep the first version minimal; do not add hooks for
  features that no plan needs (auth, live crawling, remote indexing).
- **Payload churn.** Serializers have one schema. Remove obsolete fields
  outright and update clients and tests in the same phase.
- **Hidden coupling in tests.** Snapshot tests may assert exact dict keys;
  update them in this phase and document the new payload in `README.md`.
- **Protocol surface.** Keep the seam minimal; `EntrySource` and `EntryRecord`
  remain internal adapters rather than additional public contracts. Add a
  protocol only when a plan needs it.
