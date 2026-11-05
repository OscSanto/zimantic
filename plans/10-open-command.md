# Phase 10 — Ephemeral open command

Status: proposed, 2026-11-05.

## Goal

Add a command that opens one or more ZIM files in a throwaway local server and
shows the first one in the browser:

```bash
zimantic open zims/phet_en_all_2026-08.zim
zimantic open zims/phet_en_all_2026-08.zim zims/wikipedia_en_100_2026-08.zim
```

When more than one ZIM is opened, the article toolbar gains previous/next ZIM
arrows so the reader can switch between the selected archives without returning
to the library.

`open` is for a named set of files, not for a configured library. It must not
change what `zimantic serve` does, and it must not leave indexes, catalog rows,
PID files, or locks behind.

## Locked decisions

1. **Ephemeral means process-scoped and read-only.** `open` uses an in-memory
   catalog, never builds or rewrites indexes, and never writes the normal
   `catalog.sqlite`, `zimantic.pid`, or build lock. It may open the persistent
   catalog read-only once, solely to resolve a selected archive's canonical
   source key and collection for identity and index reuse; that catalog is
   never used as runtime state and is never modified.
2. **Explicit scope.** Only the ZIMs named on the command line (after folder
   expansion) are discoverable, searchable, and visible through `/sources`,
   `/library.json`, random-article links, and search. The configured
   collections are never scanned for additional sources, and Kiwix catalogs
   are ignored.
3. **Loopback only.** The server binds `127.0.0.1`. The default port is chosen
   by the OS (`0`); `--port` may pin one. There is no authentication, so `open`
   must not expose the server to a network.
4. **Order is the command line.** Sources are ordered by the expanded CLI
   arguments. Previous/next wrap around and open the adjacent ZIM's homepage.
5. **Dedicated switch controls.** The existing left/right arrows remain the
   search-result and parent-page navigation controls. ZIM switching gets its
   own arrow group so search navigation remains independent.
6. **No build step, one identity.** A selected archive uses its canonical
   phase-01 source key when exactly one read-only catalog row matches its
   path, size, and live nonempty UUID. The same key locates a finished
   phase-04 index under `index_dir`, subject to the provider, size, and
   fingerprint checks; it never becomes a second locator. An archive with no
   unique matching row uses an ephemeral `zim:open:` fallback key.
   If the index is missing/stale/mismatched, the archive's native full-text
   index is used, exactly as `serve` does. A ZIM with neither remains readable
   and navigable but is not searchable; log that once.
7. **Normal `serve` does not grow a ZIM switcher.** The navigation order exists
   only in an `open` process.

## Current state

- `zimantic/__main__.py` has `serve`, `build`, `reload`, and `model`.
  `_collect_zims` already expands files and folders, but defaults to the
  configured ZIM collection and `serve` always scans every configured library.
- `Search.__init__` builds its source set from every finished index under
  `index_dir` plus every ZIM in the configured collections; `ZimCatalog.scan()`
  walks the whole tree.
- `web/app.py:serve()` always writes `zimantic.pid`, binds `0.0.0.0`, starts the
  Kiwix catalog refresh, and installs SIGHUP reload/restart handlers.
- `web/app.py:_article_toolbar()` already has result-context and parent-link
  arrow groups. There is no notion of an ordered source set.
- `catalog_path(index_dir)` always places the catalog beside `index_dir`, so
  even a scoped scan would otherwise touch the user's persistent catalog.

## CLI

### Syntax

```text
zimantic open PATH [PATH ...] [options]
```

`PATH` is a ZIM file or a folder containing ZIMs. Folders are searched
recursively with the same `iter_zim_files` and `is_zim_filename` rules as
`build`. At least one path is required; `zimantic open` with no arguments is a
usage error rather than an implicit whole-library launch.

Examples:

```bash
zimantic open zims/phet_en_all_2026-08.zim
zimantic open zims/
zimantic open zims/a.zim zims/b.zim
zimantic open zims/a.zim --no-browser
zimantic open zims/a.zim --port 9000
zimantic open zims/a.zim --fast
zimantic open zims/a.zim --debug
```

### Options

| Option | Behavior |
|---|---|
| `--port PORT` | Bind a fixed local port. Default `0` (OS-assigned). |
| `--no-browser` | Start the server and print the URL without launching a browser. |
| `--fast` | Like `serve --fast`: title/native full-text only, no model or FAISS. |
| `--debug` | Like `serve --debug`: ranking details in API responses. No restart-on-SIGHUP. |
| `--device DEVICE` | Same accepted values and precedence as `serve`. |
| `--max-sources N` | Safety cap for folder expansion. Default `64`. |

### Expansion and ordering

- Files are validated with `is_zim_filename`; a non-ZIM file is an error.
- A missing path is an error before any server state is created.
- Duplicate resolved paths are removed.
- File arguments keep their command-line order. Folder arguments contribute
  their ZIMs in `iter_zim_files` order (sorted, recursive).
- Keep distinct selected files even if they share a stem, consistent with the
  phase-01 provider. Only duplicate resolved paths are removed.
- More than `--max-sources` archives is a usage error that suggests
  `zimantic serve` for a whole library.
- Folder expansion also has a visited-entry and elapsed-time budget, including
  non-ZIM files. Stop as soon as the source cap is exceeded. If expansion
  cannot complete within its budget, report an error rather than launch a
  silently incomplete scope.
- The first *available* source in that order is the initial browser URL. If
  none is available, exit non-zero.

### Output and lifetime

Before serving, print the expanded source count. After the socket is listening,
print the actual local URL and the initial `/content/<key>/` URL. The browser
opens only after the server accepts requests.

The process runs until Ctrl-C or `SIGTERM`, then calls `Search.close()`. It does
not create `zimantic.pid`, does not take the build lock, and does not respond to
SIGHUP by reloading. A normal stop returns status 0. Usage errors return 2;
startup failures return 1.

## Scoped runtime

### Source scope

Add an explicit scope to the local ZIM runtime instead of faking a temporary
collection with symlinks:

```python
@dataclass(frozen=True)
class ZimOpenScope:
    paths: tuple[Path, ...]       # resolved, ordered
    keys: tuple[str, ...]         # canonical where known, else fallback, same order
```

The scope reuses the phase-01 canonical source key only when exactly one
catalog row matches the resolved path and size and its stored UUID equals the
live archive's nonempty UUID. Overlapping configured roots can produce multiple
rows for one path; do not arbitrarily choose one. An unmatched, ambiguous, or
unreadable archive gets a process-only fallback key
`zim:open:<safe-stem>-<position>` in the reserved `zim:open`
collection, where `position` is its 1-based position in the deduplicated
expanded list. Check for collisions. Fallback keys are scoped to this command
line and never locate a persistent index.

The persistent catalog lookup must not initialize, migrate, repair, or create
the database or its SQLite sidecars. If it is absent, incompatible, corrupt,
or cannot be opened without writes, log the reason and use fallback keys.
Configured collection metadata may be read from config for matched keys, but
configured roots are not scanned. The scope never invents a second locator
for a catalogued archive.

`keys` is populated by the read-only lookup. `Search` accepts an optional scope.
When present:

- `SourceCatalog` is created in memory (`db_path=None`) and populated with only
  the scoped archives under `keys`. `providers/zim/discovery.py` normalizes
  and extracts metadata from those paths and uses the generic catalog upsert;
  it never walks configured collections or deletes persistent rows.
- `providers/zim/discovery.py` considers only `keys` when locating local
  indexes through the phase-04 locator. It applies the finished/size/staleness
  checks, opens the reused `IndexHandle` under that same key, and never opens
  or validates indexes for other sources.
- `ZimRuntime.refresh`, its scoped discovery helpers, library rows,
  `source_dicts`, and random-source selection resolve only scoped archives.
  Collection metadata is emitted only for collections represented in the
  scope; `zim:open` is an ephemeral fallback collection.
- Kiwix catalog refresh is disabled.
- The scope is a constructor/runtime concern, not a new user-facing
  `config.toml` key.

`serve` passes no scope and keeps its current behavior exactly.

### Indexes and search

- A selected ZIM with a finished index at its key's locator in `index_dir`
  uses that SQLite/FAISS pair read-only; the runtime key is the indexed source
  key, not an alias. Validate the pair as in phase 04. Opening it must not
  create or modify SQLite sidecars: use Phase 04's `IndexHandle(read_only=True)`
  connection path and verify it is sidecar-free before exposing the index. If
  that cannot be guaranteed, report the index unavailable and use native
  full-text instead.
- A missing, in-progress, stale, or size-mismatched index falls back to the
  archive's native full-text search, with the same warnings as `serve`.
- Model and FAISS loading stay off the startup path. The browser opens while
  title/native full-text search already works; semantic search becomes
  available when the model is ready.
- Full-text isolation (`start_fulltext_isolation`) is enabled, as in `serve`,
  so a bad native index cannot abort the ephemeral process.
- No index is created, resumed, published, or pruned.
- No model or cache is downloaded or written; an unavailable local model only
  disables semantic search.

### Server lifecycle

Refactor the current `serve()` rather than copying it:

- Accept host, port, an optional PID path (or `None`), and an optional
  "listening" callback.
- Keep the `serve` defaults: `0.0.0.0`, configured port, PID file, SIGHUP
  behavior, and Kiwix refresh.
- For `open`, pre-bind a `127.0.0.1` socket, pass it to `uvicorn.Server`, and
  read the actual port from the bound socket. This avoids a free-port race and
  gives a reliable URL for `webbrowser.open`.
- Launch the browser from a daemon thread after `server.started`; never let a
  missing desktop browser fail the server. Fall back to printing the URL.
- `--no-browser` skips the launch entirely.

## Toolbar ZIM switcher

### Server side

`Search` stores the scoped order and exposes a small navigation helper:

```python
@dataclass(frozen=True)
class SourceNavigation:
    key: str
    title: str

def source_neighbors(
    self, key: str
) -> tuple[SourceNavigation, SourceNavigation] | None:
    """Previous and next available scoped sources, wrapping at both ends."""
```

- Navigation is built from the scoped catalog rows, not from `self.sources`.
  A readable ZIM with no search index still participates; otherwise an
  unsearchable file could be opened but never left.
- Only unavailable archives are skipped.
- If the current source is available, fewer than two available sources returns
  `None`. If it becomes unavailable, seek around its original position for
  available neighbors; one remaining source is sufficient and may be both
  previous and next. With no available target, render no switcher.
- `content_shell_response` passes the previous/next source keys, titles, and
  homepage URLs into `_content_shell` / `_article_toolbar`.
- The switcher is present on normal article shells and on error shells for a
  selected source, so a bad article does not strand the reader.
- URLs use the normal `/content/<key>/` route and `web_base_path` helpers. They
  do not expose filesystem paths.
- If an available archive has no homepage, try a bounded fallback article;
  if none exists, show an error shell with the switcher rather than strand the
  reader or silently skip the archive.

### Markup and behavior

Add a distinct group near the home link:

```html
<span class="zimantic-toolbar-source-nav" role="group" aria-label="Selected ZIMs">
  <button type="button" data-zimantic-action="source-previous"
    data-zimantic-source-previous aria-label="Previous ZIM"
    title="Previous ZIM: …">←</button>
  <button type="button" data-zimantic-action="source-next"
    data-zimantic-source-next aria-label="Next ZIM"
    title="Next ZIM: …">→</button>
</span>
```

- The group is rendered when `source_neighbors` returns targets, including the
  unavailable-current-source case above. It is independent of
  `data-zimantic-context-previous` / `next`.
- Clicking loads the target ZIM's homepage. A directly loaded shell navigates
  with `location.href`; inside the article modal, send the existing
  `zimantic-article-follow` message so the modal and history stay coherent.
- Both arrows wrap, so the first source's previous arrow opens the last source
  and vice versa.
- Titles name the target ZIM. Keep the buttons keyboard-focusable and use the
  toolbar's existing visible-focus and responsive styles.
- On narrow phones, keep the two arrows reachable without crowding out Close,
  home, and the ZIM search field; remove nonessential text labels first.

No switcher is emitted by a normal `serve` process because it has no scoped
order.

## Documentation

- `README.md`: add `zimantic open` to the CLI examples, explain the read-only
  scope, random port, loopback binding, `--no-browser`, and ZIM switching.
- `plans/README.md`: update the phase status as this work lands.
- Mention that `open` is not a service replacement and does not update indexes.
- Explain that uniquely matched catalogued archives keep their canonical source
  key and URL; unmatched or ambiguous archives get an ephemeral `zim:open:` key.
- Add a troubleshooting note for headless machines (`--no-browser`).

## Work items

- [ ] Add `open` to `_make_parser` with the options above and require at least
      one positional path.
- [ ] Extract bounded, ordered path expansion and resolved-path deduplication
      into an `open` helper.
- [ ] Add `ZimOpenScope`, its read-only catalog key lookup (validating path,
      size, and UUID), the canonical/fallback key rule, and the
      in-memory/scoped `SourceCatalog` path.
- [ ] Populate the in-memory catalog through ZIM-specific scoped discovery and
      generic catalog upserts; ensure it never deletes persistent rows.
- [ ] Scope `providers/zim/discovery.py` index discovery, `ZimRuntime.refresh`,
      library payloads, and random-source selection to the open scope.
- [ ] Add `Search.source_neighbors()` and expose the ordered source set to the
      content-shell route.
- [ ] Refactor `web/app.py:serve()` into a shared server runner with optional
      host, port, PID path, and startup callback; keep the `serve` defaults.
- [ ] Implement `_run_open`: scope construction, in-memory catalog, server
      runner, URL printing, browser launch, signal handling, and cleanup.
- [ ] Add the source-switch group to `_article_toolbar`, and wire it through
      `_content_shell` and `content_shell_response`.
- [ ] Handle clicks and wrapping in `article-toolbar.js`; add styles in
      `article-toolbar.css`.
- [ ] Update `README.md` and `plans/README.md`.
- [ ] Add CLI, scope, lifecycle, toolbar, and regression tests.

## Tests

### CLI and expansion

- [ ] `open` with no paths is a usage error; a missing path or non-ZIM file is
      rejected before server startup.
- [ ] Files preserve argument order; folders are recursive and sorted; duplicate
      resolved paths are removed.
- [ ] Distinct same-stem files are both kept with distinct keys, in argument
      order; duplicate resolved paths are removed.
- [ ] More than `--max-sources` archives fails with a message pointing at
      `serve`.
- [ ] A folder with too many non-ZIM entries or an expired traversal deadline
      fails explicitly instead of scanning indefinitely.
- [ ] `--port`, `--no-browser`, `--fast`, `--debug`, and `--device` parse and
      propagate correctly.

### Scope and read-only behavior

- [ ] `/sources`, `/library.json`, `/search`, `/search/stream`, and
      `/random?source=` see only the selected ZIMs even when `index_dir` contains
      indexes for other archives.
- [ ] The configured collections are never scanned; no Kiwix collection is
      contacted.
- [ ] A uniquely matched archive uses its phase-01 canonical key in
      `/sources`, `/library.json`, search
      results, and `/content/<key>/`, and reuses its matching finished index
      through the same locator; the key and URL match a normal `serve` process.
- [ ] An archive with no unique matching row, a missing UUID, or a size/UUID
      mismatch uses a distinct `zim:open:` fallback key and never reuses an
      unrelated index. Cover overlapping collection roots.
- [ ] A missing, stale, or mismatched index falls back to native full-text; a
      ZIM with no index at all still serves articles.
- [ ] No `catalog.sqlite`, index file, `zimantic.pid`, or build lock is created
      or modified, including SQLite sidecars and model caches.
- [ ] A normal `serve` startup with the same config is byte-for-byte unaffected
      in its public payloads and still uses the persistent catalog.

### Server and browser

- [ ] Port `0` produces a real bound loopback port, and `webbrowser.open`
      receives the first available `/content/<key>/` URL exactly once after the
      server is ready (patch the browser call in tests).
- [ ] `--no-browser` never calls `webbrowser.open`.
- [ ] A browser-launch failure leaves the server running and printable.
- [ ] SIGINT/SIGTERM stop the server, close indexes, and leave no PID file.

### Toolbar

- [ ] One selected ZIM renders no source arrows.
- [ ] Two or more render both arrows; previous from the first wraps to the last,
      next from the last wraps to the first.
- [ ] Arrows appear on article shells and error shells; the current ZIM is never
      a target.
- [ ] If the current source becomes unavailable and only one other source
      remains available, both arrows lead to that source.
- [ ] Targets are the adjacent ZIM's homepage and the titles name that ZIM.
- [ ] A source with no homepage uses a bounded fallback or displays an error
      shell that still lets the user switch sources.
- [ ] Search-result context arrows and parent-link arrows keep their current
      behavior and are not confused with the source switcher.
- [ ] Direct shells navigate normally; modal shells update the modal and browser
      history through the existing follow path.
- [ ] Normal `serve` emits no source switcher.
- [ ] Add/extend `tests/test_article_navigation_js.py` and `tests/test_app_js.py`
      for the click and message behavior.

## Acceptance criteria

- `zimantic open zims/phet_en_all_2026-08.zim` starts a loopback server, prints
  its URL, and opens that ZIM's homepage in the browser. If the archive is also
  uniquely matched in the catalog, its source key and content URL match
  `serve`; otherwise it uses an ephemeral `zim:open:` fallback key.
- `zimantic open a.zim b.zim` exposes only those two sources, and the toolbar's
  left/right ZIM arrows switch between their homepages in both directions.
- Every terminal stop leaves the user's catalog, indexes, PID file, and locks
  untouched.
- `zimantic serve` uses its persistent catalog and configured collections;
  `open` touches no persistent state.
- The command works with a prebuilt index, with a native-fulltext-only ZIM, and
  with a ZIM that has neither (readable but not searchable).
- `python -m pytest` and the JavaScript tests pass.

## Risks and mitigations

- **Scope leaks into the library.** Route all discovery through the explicit
  scope and test that unrelated indexes, ZIMs, and Kiwix sources are absent.
- **Ephemeral writes user state.** Use an in-memory catalog, read-only index
  handles, no PID file, and no build lock; assert this in tests.
- **Clobbering a running server.** `open` must not write `zimantic.pid` or send
  SIGHUP; refactor the shared server helper carefully so `serve` keeps its own
  PID/signal behavior.
- **Port race or opening the browser too early.** Pre-bind the socket, derive
  the actual port, and launch the browser only after startup.
- **Network exposure.** Hard-code a loopback bind; do not add a `--host` escape
  hatch in this phase.
- **Confusion with search-result arrows.** Use a separate action/attribute
  family and explicit "Previous ZIM"/"Next ZIM" labels.
- **Large folder expansion.** Cap the scope and direct whole-library use to
  `zimantic serve`.
- **Stale catalog identity.** Reuse a canonical key only when the row's path,
  size, and UUID match the file; otherwise use the fallback key instead of
  serving a replaced archive under another source's identity.
- **Index mismatch.** Reuse the stale-index fallback and warnings; do not
  silently serve a wrong index.
- **Slow model startup.** Keep model and FAISS loading in the background so the
  browser opens promptly.
