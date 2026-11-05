# Phase 00 — Contracts and provider registry

Status: proposed.

## Goal

Introduce the internal seam that every later phase depends on. This phase is
structural: it defines the provider, collection, source, and content contracts
and the registry. ZIM remains the only working provider, but it reaches the core
through the new contracts. The old `mode`/`content_kind` shape is replaced, not
carried forward.

## Non-goals

- Do not move ZIM discovery or search yet. Phase 01 does that.
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
`ContentStore` method set (including `read_range`) even though implementations
return `None` until phase 03, so the contract is genuinely frozen here.

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
    def discover(self, ctx: ProviderContext) -> Iterable[CollectionInfo]: ...
    def open(self, collection: CollectionInfo, ctx: ProviderContext) -> CollectionRuntime: ...
```

Notes:

- `SourceProvider.discover()` must be cheap and bounded. It returns collection
  descriptors, not items. A provider may cache expensive metadata on disk.
- `CollectionRuntime.search()` is one source's work. Providers that natively
  search a whole collection (Kolibri) map the source to the appropriate subtree
  or content-node filter.
- `ContentStore` is defined in `content/store.py` during this phase but its
  provider implementations land in phase 03 and later. Providers can return
  `None` until their serving support lands.
- `library_sources()` defaults conceptually to `sources`; ZIM overrides it to
  include unreadable archives and duplicates.
- `max_sources_per_search()` returns `None` for providers without a special
  fan-out cap. Refreshes publish an immutable source/collection snapshot
  atomically and increment `snapshot_version`; non-blocking refreshes expose
  `loading` until the new snapshot is published.
- `CollectionInfo` is a descriptor and remains frozen. Dynamic source counts,
  availability, errors, and discovery state come from the current runtime
  snapshot; serializers must not mutate a previously published descriptor.
- `SourceResult` and its four ranking channels (`semantic`, `keyword`,
  `fulltext`, `items`) stay unchanged.

### `SourceInfo` shape

`SourceInfo` has one discriminator and one identity:

```python
key: str                       # globally unique, URL-safe source key
title: str | None = None       # display name
provider: str = "zim"
collection_key: str = ""
collection_label: str | None = None
```

- `provider` is the canonical discriminator. There is no `content_kind`, no
  `mode`, and no derived alias field.
- `key` is globally unique, stable, and URL-safe. It is also the URL identity
  used by `/content/{key}/...` and `/article/{key}/...`; there is no separate
  `content_id` field.
- `title` is the display name. Remove `name`, `book`, `local_name`, and their
  alias precedence. Provider-specific identifiers such as a Kiwix book slug or a
  ZIM archive stem live in provider metadata or in the canonical `key`.

`SourceInfo.to_dict()` emits only the canonical fields:

```json
{
  "key": "zim:library:wikipedia_en_all",
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
- Absolute configured roots are valid. Providers may resolve them once and
  retain the canonical absolute path as a private runtime or catalog locator;
  it is not part of `SourceInfo`, `key`, or any serialized payload.
- `catalog.sqlite` uses a provider-neutral `sources` table keyed by `key`.
  Store a canonical absolute path in a separate indexed `path` locator column;
  it is not part of the key.
- Path relocation updates the locator, not a provider's stable logical key.
  Keep filesystem path normalization out of per-query identity resolution.

### Registry

`zimantic/providers/registry.py`:

- `builtin_providers() -> list[SourceProvider]`
- `get_provider(kind: str) -> SourceProvider`
- `register(provider)` for tests only.
- Registry lookup raises a clear config-validation error rather than silently
  selecting a fallback provider. The top-level config loader decides whether
  that invalid entry is fatal; phase 08 uses the failure-isolated behavior
  described there and marks the entry unavailable without blocking other
  collections.

Register `zim` and `kiwix` as placeholders in this phase. They may return empty
discovery results until phases 01 and 02.

### Minimal source configuration

This phase owns the minimal `[[sources]]` TOML parser. It parses the
array-of-tables shape into normalized provider entries, requires `type` and
`name`, rejects duplicate names, and preserves provider-specific keys for the
provider validators that follow. It must not depend on provider modules that
are introduced later. Flat `zim_dir` and `kiwix_server` shorthands are not
accepted.

The parser reports invalid TOML as fatal and defers provider-specific
validation and unavailable-entry handling to the registry/config layer. Phase
05 adds files-provider validation, and phase 08 completes the full settings
loader without introducing a second parser.

### `Search` integration skeleton

In `Search.__init__`:

- `self.providers: dict[str, SourceProvider]`
- `self.runtimes: dict[str, CollectionRuntime]`
- `self.collections: dict[str, CollectionInfo]`

Add:

- `Search.resolve_source(identifier: str) -> SourceInfo | None` — one place
  that matches an exact `key`. Do not add name, book, local-name, dated, or
  flavoured alias matching.
- `Search.refresh_collections(...)` — a thin loop over providers; no behavior
  change yet.
- `Search.collection_dicts()` — serialization helper for later API work.

`_search_source` dispatches by `source.provider` from this phase onward. Do not
keep a `mode` fallback.

## Work items

- [ ] Create `zimantic/providers/base.py` with the dataclasses and protocols.
- [ ] Create the canonical content-contract module at
      `zimantic/content/store.py` and re-export it from `providers/base.py`.
      Define the complete `ContentStore` protocol, including `read_range`; phase
      03 implements it rather than extending the contract.
- [ ] Create `zimantic/providers/registry.py`; register `zim` and `kiwix`
      placeholders.
- [ ] Add the normalized provider-configuration seam and minimal
      `[[sources]]` TOML parser described above; providers consume normalized
      entries and later phases add provider-specific validation. Do not keep
      flat shorthands.
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
- Registry returns the two built-ins and rejects unknown kinds.
- `provider` is the only discriminator; no serializer emits a derived kind or
  mode.
- The frozen `ContentStore` method set is unchanged when phase 03 implements it.
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
