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
   `catalog.sqlite`, `zimantic.pid`, or build lock.
2. **Explicit scope.** Only the ZIMs named on the command line (after folder
   expansion) are discoverable, searchable, and visible through `/sources`,
   `/library.json`, random-article links, and search. The configured
   collections and Kiwix catalogs are ignored.
3. **Loopback only.** The server binds `127.0.0.1`. The default port is chosen
   by the OS (`0`); `--port` may pin one. There is no authentication, so `open`
   must not expose the server to a network.
4. **Order is the command line.** Sources are ordered by the expanded CLI
   arguments. Previous/next wrap around and open the adjacent ZIM's homepage.
5. **Dedicated switch controls.** The existing left/right arrows remain the
   search-result and parent-page navigation controls. ZIM switching gets its
   own arrow group so search navigation remains independent.
6. **No build step.** A finished source index at the phase-04 locator under
   `index_dir` is reused when it matches the selected archive. Otherwise the
   archive's native full-text index is used, exactly as `serve` does. A ZIM
   with neither remains readable and navigable but is not searchable; log that
   once.
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
- If two selected files share a stem, reuse the `build` policy: keep the
  largest copy and log a warning. The surviving file occupies the position of
  the first occurrence in the expanded list.
- More than `--max-sources` archives is a usage error that suggests
  `zimantic serve` for a whole library.
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
    order: tuple[str, ...]        # source keys, same order
```

The scope uses collection key `zim:open` and source keys `zim:open:<stem>`.
`Search` accepts an optional scope. When present:

- `ZimCatalog` is created in memory (`db_path=None`) and populated with only
  the scoped archives. Add `ZimCatalog.scan_paths(paths)` that upserts those
  files without walking configured collections and without deleting unrelated
  rows.
- `_discover_local_indexes` considers only the indexes for scoped source keys
  via the phase-04 locator. It applies the finished/size/staleness checks and
  never opens or validates indexes for other sources.
- `_scan_zim_paths`, library rows, `source_dicts`, and random-source selection
  resolve only scoped archives.
- Kiwix catalog refresh is disabled.
- The scope is a constructor/runtime concern, not a new user-facing
  `config.toml` key.

`serve` passes no scope and keeps its current behavior exactly.

### Indexes and search

- A selected ZIM with a finished index in `index_dir` uses that SQLite/FAISS
  pair read-only.
- A missing, in-progress, stale, or size-mismatched index falls back to the
  archive's native full-text search, with the same warnings as `serve`.
- Model and FAISS loading stay off the startup path. The browser opens while
  title/native full-text search already works; semantic search becomes
  available when the model is ready.
- Full-text isolation (`start_fulltext_isolation`) is enabled, as in `serve`,
  so a bad native index cannot abort the ephemeral process.
- No index is created, resumed, published, or pruned.

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
- Fewer than two available sources returns `None`; the toolbar renders no
  switcher.
- `content_shell_response` passes the previous/next source keys, titles, and
  homepage URLs into `_content_shell` / `_article_toolbar`.
- The switcher is present on normal article shells and on error shells for a
  selected source, so a bad article does not strand the reader.
- URLs use the normal `/content/<key>/` route and `web_base_path` helpers. They
  do not expose filesystem paths.

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

- The group is rendered only when a navigation order has at least two available
  sources. It is independent of `data-zimantic-context-previous` / `next`.
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
- `plans/README.md`: add phase 10 to the phase map and link this document.
- Mention that `open` is not a service replacement and does not update indexes.
- Add a troubleshooting note for headless machines (`--no-browser`).

## Work items

- [ ] Add `open` to `_make_parser` with the options above and require at least
      one positional path.
- [ ] Extract ordered path expansion and the stem-dedup rule into an `open`
      helper.
- [ ] Add `ZimOpenScope` and the in-memory/scoped catalog path.
- [ ] Implement `ZimCatalog.scan_paths(paths)` and ensure it never deletes
      persistent rows.
- [ ] Scope `_discover_local_indexes`, `_scan_zim_paths`, source rebuild,
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
- [ ] Same-stem files keep the largest copy at the first occurrence and log one
      warning.
- [ ] More than `--max-sources` archives fails with a message pointing at
      `serve`.
- [ ] `--port`, `--no-browser`, `--fast`, `--debug`, and `--device` parse and
      propagate correctly.

### Scope and read-only behavior

- [ ] `/sources`, `/library.json`, `/search`, `/search/stream`, and
      `/random?zim=` see only the selected ZIMs even when `index_dir` contains
      indexes for other archives.
- [ ] The configured collections are never scanned; no Kiwix collection is
      contacted.
- [ ] A matching finished index is reused; a stale or missing index falls back
      to native full-text; a ZIM with no index at all still serves articles.
- [ ] No `catalog.sqlite`, index file, `zimantic.pid`, or build lock is created
      or modified.
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
- [ ] Targets are the adjacent ZIM's homepage and the titles name that ZIM.
- [ ] Search-result context arrows and parent-link arrows keep their current
      behavior and are not confused with the source switcher.
- [ ] Direct shells navigate normally; modal shells update the modal and browser
      history through the existing follow path.
- [ ] Normal `serve` emits no source switcher.
- [ ] Add/extend `tests/test_article_navigation_js.py` and `tests/test_app_js.py`
      for the click and message behavior.

## Acceptance criteria

- `zimantic open zims/phet_en_all_2026-08.zim` starts a loopback server, prints
  its URL, and opens that ZIM's homepage in the browser.
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
- **Index mismatch.** Reuse the stale-index fallback and warnings; do not
  silently serve a wrong index.
- **Slow model startup.** Keep model and FAISS loading in the background so the
  browser opens promptly.
