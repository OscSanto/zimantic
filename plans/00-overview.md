# Simplification Plans Overview

These plans break down the five highest-leverage codebase simplifications into self-contained, ordered phases. Every phase preserves existing behavior, public APIs, monkeypatch targets used by `tests/`, and runtime performance.

## Global Rules for Executing Models

1. **Do not use `git` commands** unless explicitly asked by the user, and **never stage files automatically**.
2. **Preserve test monkeypatch targets**: Many tests in `tests/` monkeypatch attributes on `zimantic.search` (e.g., `zimantic.search.Archive`, `zimantic.search.urlopen`, `zimantic.search.Search`) or construct `Search` via `object.__new__(Search)`. When moving functions out of `zimantic/search.py`, re-export them in `zimantic/search.py` or pass dependencies so existing monkeypatches continue to work.
3. **Never add `# noqa` suppressions**: Fix lint issues through clean imports and refactoring.
4. **Run targeted `pytest` commands** after each step before moving to the next step.

## Execution Order

1. [`05-normalize-config-and-search-defaults.md`](./05-normalize-config-and-search-defaults.md) — Low-risk cleanup of class-level defaults on `Search`, config helpers, and duplicate SHA-256 helpers. Doing this first makes later extractions cleaner.
2. [`01-unify-zim-catalog-discovery.md`](./01-unify-zim-catalog-discovery.md) — Make `ZimCatalog` (with an `:memory:` SQLite fallback) the single source of truth for local `.zim` discovery and metadata, removing parallel filesystem scans in `Search`.
3. [`02-decompose-search-monolith.md`](./02-decompose-search-monolith.md) — Extract ranking (`zimantic/searching/ranking.py`), Kiwix remote OPDS/search (`zimantic/searching/kiwix.py`), and ZIM article/icon/random readers (`zimantic/content/serving.py`) from `zimantic/search.py`.
4. [`04-deduplicate-url-path-and-modal-helpers.md`](./04-deduplicate-url-path-and-modal-helpers.md) — Unify archive path resolution in Python and modal/URL helpers in `zimantic/web/static/app.js`.
5. [`03-extract-embedded-web-assets.md`](./03-extract-embedded-web-assets.md) — Move ~900 lines of inline CSS/JS/HTML out of `zimantic/web/app.py` into static assets while keeping `/content/` shell and `/article` injection behavior identical.
