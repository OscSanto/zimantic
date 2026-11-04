# Plan 03: Extract Inline JS/CSS/HTML from `zimantic/web/app.py`

## Goal
Move ~900 lines of inline JavaScript and CSS string literals out of `zimantic/web/app.py` into static files under `zimantic/web/static/`, passing per-request dynamic configuration via data attributes or JSON instead of Python `%s` string formatting.

## Critical Invariants to Preserve
1. **CORS on `/article`**: Raw `/article` responses and error responses must still include `Access-Control-Allow-Origin: *` (`ARTICLE_CORS_HEADERS` in `zimantic/web/app.py`) because sandboxed iframes have an opaque origin.
2. **Opaque-origin `/article` iframe scripts**:
   - Inside `/article` HTML (`_inject_article_navigation`), the document may execute inside a sandboxed iframe with an opaque origin (`null`) and a strict Content-Security-Policy (`ARTICLE_SANDBOX_CSP` allows `script-src 'unsafe-inline'`, `style-src 'unsafe-inline'`).
   - Do **not** replace inline `<script>`/`<style>` injection inside raw `/article` documents with external `<script src="/static/...">` tags if `ARTICLE_SANDBOX_CSP` or relative base URLs inside the ZIM article would block or misroute them!
   - Instead, load the static `.js`/`.css` files from `zimantic/web/static/` at module import time (using `functools.lru_cache` or module constants read via `Path(__file__).parent / "static" / ...`) and inject their contents, OR serve the trusted `/content/` shell's toolbar script/style as external static files while keeping `/article` shims loaded from disk files into memory.
3. **Test assertions on HTML output**:
   - Several tests in `tests/test_server.py` assert exact substrings in `/content/...` and `/article?...` responses (e.g., `zimantic-toolbar`, `zimantic-nav-bridge`, `ARTICLE_COOKIE_SHIM`, `ARTICLE_STORAGE_SHIM`, CSS rules, or function names).
   - Before changing whether a tag is inline vs `<script src=...>`, check `tests/test_server.py` with `grep`. Loading the `.js`/`.css` templates from `zimantic/web/static/*.js` and `*.css` files at Python import time preserves 100% of the rendered HTML byte-for-byte while still moving 900 lines of JS/CSS out of `app.py` into real `.js`/`.css` files!

## Step-by-Step Instructions

### Step 1: Audit `tests/test_server.py` assertions on `web/app.py` constants
1. Use `grep` in `tests/` for `ARTICLE_`, `zimantic-toolbar`, `_inject_article`, `_content_shell`, and `content_shell`.
2. Note which constants are imported directly by tests. Keep those constant names in `zimantic/web/app.py` bound to the loaded string contents.

### Step 2: Extract CSS and JS templates into `zimantic/web/static/`
Create the following files under `zimantic/web/static/` (or `zimantic/web/templates/`):
1. `zimantic/web/static/article-shims.js` — Contents of `ARTICLE_COOKIE_SHIM` and `ARTICLE_STORAGE_SHIM` (or split into `article-cookie-shim.js` and `article-storage-shim.js`).
2. `zimantic/web/static/article-toolbar.css` — Contents of `ARTICLE_TOOLBAR_STYLE`.
3. `zimantic/web/static/article-toolbar.js` — Contents of `ARTICLE_TOOLBAR_SCRIPT`.
4. `zimantic/web/static/article-mobile.css` — Contents of `ARTICLE_MOBILE_STYLE`.
5. `zimantic/web/static/article-nav-bridge.js` — Contents of `ARTICLE_NAV_BRIDGE_SCRIPT`.
6. `zimantic/web/static/content-shell.css` — The inline `<style>` block inside `_content_shell()` in `zimantic/web/app.py`.

### Step 3: Load asset files in `zimantic/web/app.py`
1. Add a small helper in `zimantic/web/app.py`:
   ```python
   def _read_static_text(name: str) -> str:
       return (STATIC_DIR / name).read_text(encoding="utf-8").strip()
   ```
2. Bind `ARTICLE_COOKIE_SHIM`, `ARTICLE_STORAGE_SHIM`, `ARTICLE_TOOLBAR_STYLE`, `ARTICLE_TOOLBAR_SCRIPT`, `ARTICLE_MOBILE_STYLE`, and `ARTICLE_NAV_BRIDGE_SCRIPT` using `_read_static_text(...)` wrapped with their `<script>`/`<style>` tags just as before.
   - Verify that `%s` placeholders in `article-toolbar.js` and `article-nav-bridge.js` (and `%%` escapes, if any) either remain compatible with `% (...)` interpolation or use explicit token replacement (`.replace("__ZIMANTIC_ZIM__", ...)`). If using `.replace(...)`, convert `%%` in those JS files back to single `%` so the `.js` files are valid standalone JavaScript!
   - **Important**: Converting `%%` to `%` and replacing the 2–5 `%s` placeholders with named tokens like `__ZIM_NAME_JSON__`, `__ARTICLE_PATH_JSON__`, `__TOOLBAR_HEIGHT__`, `__BREADCRUMBS_JSON__`, `__SECURE_JSON__` makes `article-toolbar.js` and `article-nav-bridge.js` 100% valid JavaScript syntax that linters and editors can parse directly.

### Step 4: Verify
1. Validate JS syntax of extracted files using `node --check zimantic/web/static/article-toolbar.js` and `node --check zimantic/web/static/article-nav-bridge.js` (with placeholder tokens that are valid JS identifiers).
2. Run `pytest tests/test_server.py -q`.
