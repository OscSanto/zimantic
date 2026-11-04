# Plan 01: Make `ZimCatalog` the Single Source of Truth for Local ZIMs

## Goal
Eliminate the duplicate filesystem scanning, TTL path caching, and archive metadata fallback paths in `zimantic/search.py` and `zimantic/__main__.py` by ensuring `ZimCatalog` is always available (falling back to an `:memory:` SQLite database when `index_dir` is unset or read-only).

## Background & Current Duplication
Currently, `Search` has two parallel ways to discover `.zim` files and their metadata:
1. **Via `self._catalog` (`ZimCatalog` in `zimantic/catalog.py`)**: Scans `zim_dir`, caches metadata in `catalog.sqlite`, tracks `(size, mtime_ns)`, and stores favicons.
2. **Via fallback methods in `zimantic/search.py`**:
   - `_scan_zim_paths()` and `_find_zim_path()`
   - `_library_zim_paths()`, `_library_paths_cache`, `_library_paths_at`, `_library_paths_lock`
   - `_inspect_library_archive()` and `_library_metadata_cache`
   - Branching in `_discover_local_indexes()`, `_rebuild_sources()`, `library_dicts()`, `read_article()`, `article_path_exists()`, `read_source_icon()`, and `health()` that checks `if self._catalog:` and otherwise falls back to manual directory walks and `metadata_from_archive()`.

Additionally, `zimantic/__main__.py` (`_select_zim_paths`) reimplements stem/UUID/filename matching over `zim_dir.rglob("*.zim")`.

## Step-by-Step Instructions

### Step 1: Support `:memory:` fallback in `ZimCatalog` (`zimantic/catalog.py`)
1. Open `zimantic/catalog.py` and inspect `ZimCatalog.__init__(self, db_path: Path | str, zim_dir: Path | str | None)`.
2. Allow `db_path: Path | str | None = None`:
   - When `db_path is None`, set `self.db_path = Path(":memory:")` and connect via `sqlite3.connect(":memory:", check_same_thread=False)` without calling `self.db_path.parent.mkdir(...)`.
   - When `db_path` is a filesystem path, keep existing behavior (`mkdir(parents=True, exist_ok=True)` and connecting to `str(self.db_path)`), but if `OSError` or `sqlite3.Error` occurs when opening the on-disk file, log a warning and fall back to `sqlite3.connect(":memory:", check_same_thread=False)` inside `ZimCatalog` instead of failing completely.
3. Add a helper method `ZimCatalog.find_path(self, stem: str, size: int | None = None) -> Path | None`:
   - Calls `record = self.get_stem_size(stem, size)`
   - Returns `Path(record.path)` if `record is not None` else `None`.
4. Ensure `ZimCatalog.scan()` uses the `Archive` constructor via a configurable factory or module-level `Archive` so tests that monkeypatch `zimantic.search.Archive` or `zimantic.catalog.Archive` continue to work. Specifically:
   - Allow `ZimCatalog.scan(self, archive_cls: Any = Archive)` or pass `archive_cls` into `ZimCatalog.__init__(..., archive_cls: Any = Archive)` so `Search` can pass `zimantic.search.Archive`.

### Step 2: Always initialize `self._catalog` in `Search` (`zimantic/search.py`)
1. In `Search._ensure_catalog(self) -> ZimCatalog`:
   - If `getattr(self, "_catalog", None) is not None`, return `self._catalog`.
   - If `index_dir` is configured in `self.cfg`, try `ZimCatalog(Path(index_dir) / CATALOG_FILENAME, self.cfg.get("zim_dir"), archive_cls=Archive)`.
   - If `index_dir` is missing or `ZimCatalog` raises, construct `ZimCatalog(None, self.cfg.get("zim_dir"), archive_cls=Archive)`.
   - Assign `self._catalog = catalog` and return it.
   - Note: Because some tests construct `Search` with `object.__new__(Search)` without calling `__init__`, always access the catalog via `self._ensure_catalog()` (or a `@property def catalog(self) -> ZimCatalog:` that lazily creates an `:memory:` `ZimCatalog(None, self.cfg.get("zim_dir"), archive_cls=Archive)` if `self._catalog is None`).

### Step 3: Route `Search` ZIM lookups through `self._ensure_catalog()` (`zimantic/search.py`)
1. Update `_scan_zim_paths(self)` and `_find_zim_path(self, stem, expected_size=None)`:
   - Keep `_scan_zim_paths(self) -> dict[str, Path]` and `_find_zim_path(self, stem: str, expected_size: int | None = None) -> Path | None` as thin wrappers around `self._ensure_catalog()` (since existing unit tests in `tests/test_search.py` may call or monkeypatch `_scan_zim_paths` or `_find_zim_path` directly).
   - Inside `_scan_zim_paths`: call `catalog = self._ensure_catalog()`, `catalog.scan()`, and return `{r.stem: Path(r.path) for r in catalog.list_all() if r.available}` (or if `catalog.list_all()` is empty and `zim_dir` changed, sync `catalog.zim_dir = Path(zim_dir)` before scanning).
2. Remove `_library_zim_paths`, `_library_paths_cache`, `_library_paths_at`, `_library_paths_lock`, `_inspect_library_archive`, and `_library_metadata_cache` from `Search` **unless** called directly by a test (check with `grep` in `tests/` first!).
   - Run `grep` for `_library_zim_paths`, `_inspect_library_archive`, `_library_metadata_cache`, `_scan_zim_paths`, `_find_zim_path` across `tests/` before deleting any method name.
   - For any method referenced in `tests/`, keep a thin delegating method rather than deleting the symbol.
3. In `_rebuild_sources`, `library_dicts`, `read_article`, `article_path_exists`, `read_source_icon`, and `health`:
   - Replace the duplicated `if self._catalog: ... else: ...` branches with a single path using `catalog = self._ensure_catalog()`.

### Step 4: Verification
1. Run `pytest tests/test_catalog.py tests/test_search.py tests/test_server.py -q`.
2. Verify all catalog, library, icon, and article tests pass with zero regressions.
