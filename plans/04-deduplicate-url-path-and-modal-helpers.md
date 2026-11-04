# Plan 04: Deduplicate URL/Path Normalization and Frontend Modal Helpers

## Goal
Consolidate repeated archive relative-path resolution in Python and repeated URL/focus-trap/modal boilerplate in `zimantic/web/static/app.js`.

## Part A: Archive Relative Path Resolution (Python)

### Current Duplication
Three places normalize ZIM entry paths and resolve relative targets while guarding against `..` traversal and stripping query/fragment strings:
1. `zimantic/content/reader.py`: `_refresh_target(entry_path: str, content: str) -> str | None` (lines 207–238)
2. `zimantic/searching/sources.py`: `_archive_path_from_href(base_path: str, href: str) -> str | None` (lines 78–112)
3. `zimantic/search.py`: `Search._article_path(path: str) -> str` (lines 2120–2135)

### Step-by-Step Instructions (Part A)
1. View the three implementations carefully to note their exact edge cases:
   - `_article_path` validates an entry path (`lstrip("/")`, rejects empty or `..` segments).
   - `_archive_path_from_href` resolves a relative or root-relative `href` against `base_path` (ignoring `http://`, `https://`, `mailto:`, `javascript:`, `data:`, `#...`), strips `?query` and `#fragment`, percent-unquotes, normalizes via `posixpath.normpath`, and rejects paths escaping the root (`..`).
   - `_refresh_target` extracts the URL from a `<meta http-equiv="refresh" content="...">` attribute and resolves it relative to `entry_path` using the exact same rules.
2. In `zimantic/content/reader.py` (or `zimantic/searching/sources.py`), define a shared helper `resolve_archive_href(base_path: str, href: str, *, unquote_path: bool = True) -> str | None` and `normalize_archive_path(path: str) -> str | None`.
3. Have `_archive_path_from_href` and `_refresh_target` both use this shared helper (verify whether `_refresh_target` unquotes percent-encoded paths or keeps raw characters—check `tests/test_reader.py` and `tests/test_search.py`).
4. Run `pytest tests/test_reader.py tests/test_search.py -q`.

---

## Part B: Frontend Modal Focus-Trap & URL Helpers (`zimantic/web/static/app.js`)

### Current Duplication in `zimantic/web/static/app.js`
1. **Duplicate `Tab` focus-trap handlers**:
   - `libraryInfoModal` keydown listener (lines 3399–3420)
   - `externalLinkNotice` keydown listener (lines 3440–3468)
   Both query the exact same selector list:
   ```js
   'a[href], button:not([disabled]):not([tabindex="-1"]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'
   ```
   and cycle focus between `first` and `last` on `Tab` / `Shift+Tab`.
2. **Duplicate URL query-param mutators**:
   - `updateLibraryFilterUrl()` (lines 2180–2198)
   - `syncModalStateUrl()` / modal query param toggling (lines 3201–3213)
   - `normalizePageParam()` (lines 3215–3238)
   All parse `new URL(location.href)`, set/delete a search param if changed, and call `history.replaceState(history.state, "", url.pathname + url.search + url.hash)`.

### Step-by-Step Instructions (Part B)
1. Open `zimantic/web/static/app.js` around lines 3200–3475.
2. Add a shared helper `trapModalTabFocus(container, event)`:
   ```js
   const FOCUSABLE_SELECTOR =
       'a[href], button:not([disabled]):not([tabindex="-1"]), ' +
       "input:not([disabled]), select:not([disabled]), " +
       "textarea:not([disabled]), summary, " +
       '[tabindex]:not([tabindex="-1"])';

   function trapModalTabFocus(container, event) {
       if (event.key !== "Tab" || !container || container.hidden) return;
       const focusable = [...container.querySelectorAll(FOCUSABLE_SELECTOR)].filter(
           (node) => node.getClientRects().length > 0,
       );
       if (!focusable.length) return;
       const first = focusable[0];
       const last = focusable[focusable.length - 1];
       if (!container.contains(document.activeElement)) {
           event.preventDefault();
           first.focus();
       } else if (
           event.shiftKey &&
           (document.activeElement === first || document.activeElement === container)
       ) {
           event.preventDefault();
           last.focus();
       } else if (!event.shiftKey && document.activeElement === last) {
           event.preventDefault();
           first.focus();
       }
   }
   ```
3. Replace both keydown handlers on `$("libraryInfoModal")` and `$("externalLinkNotice")` with calls to `trapModalTabFocus($("libraryInfoModal"), event)` and `trapModalTabFocus($("externalLinkNotice"), event)`.
4. Add a small helper `replaceQueryParam(name, value)` that updates `location.href` via `history.replaceState` only when the parameter value actually changes, and use it in `updateLibraryFilterUrl` and `syncModalStateUrl`.
5. Verify with `node --check zimantic/web/static/app.js` and `pytest tests/test_server.py -q`.
