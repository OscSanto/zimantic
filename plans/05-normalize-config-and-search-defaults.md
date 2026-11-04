# Plan 05: Normalize Config Once, Set Class Defaults on `Search`, and Deduplicate SHA-256

## Goal
Remove ~35 defensive `getattr(self, ...)` lookups on `Search`, deduplicate `_sha256` between `embed.py` and `model_files.py`, and centralize config integer/float coercion.

## Why `getattr(self, ...)` Exists on `Search` Today
Several unit tests create a bare `Search` instance with `s = object.__new__(Search)` without calling `Search.__init__` and then set only 2–3 attributes (such as `s.cfg = {}` or `s.indexes = {}`). Because those attributes weren't defined on the `Search` class body, methods across `zimantic/search.py` had to write:
- `getattr(self, "_defer_faiss", False)`
- `getattr(self, "_model_fingerprint", "")`
- `getattr(self, "_cache_generation", 0)`
- `getattr(self, "preview_cache", None)`
- `getattr(self, "indexes", {})`
- `getattr(self, "sources", {})`
- `getattr(self, "_catalog", None)`
- `getattr(self, "_probe_cache", None)`
- `getattr(self, "_probe_cache_lock", None)`
- `getattr(self, "max_stream_ranks", ...)`
- `getattr(self, "stream_update_interval", ...)`
- `getattr(self, "preview_chars", ...)`

When attributes have class-level defaults on `Search`, `object.__new__(Search)` automatically resolves `self._defer_faiss`, `self._cache_generation`, `self._model_fingerprint`, etc. via normal attribute lookup (`self._defer_faiss`) with zero `getattr(...)` clutter!

## Step-by-Step Instructions

### Step 1: Deduplicate SHA-256 file hashing (`zimantic/embed.py` and `zimantic/model_files.py`)
1. View `zimantic/embed.py` lines 25–40 (`_sha256(path: Path) -> str`) and `zimantic/model_files.py` lines 48–60 (`sha256_file(path: Path) -> str`).
2. Both read a file in 1 MiB chunks into `hashlib.sha256()` and return `.hexdigest()`.
3. In `zimantic/embed.py`:
   - Import `sha256_file` from `.model_files` (or alias `from .model_files import sha256_file as _sha256` so any caller/test referencing `embed._sha256` still finds it).
   - Delete the duplicate implementation in `zimantic/embed.py`.
4. Run `pytest tests/test_embed.py -q`.

### Step 2: Add Class-Level Attribute Defaults on `Search` (`zimantic/search.py`)
1. View `zimantic/search.py` around `class Search:` (lines 945–1078).
2. Define immutable class-level defaults on `Search` for scalar/optional attributes:
   ```python
   class Search:
       cfg: dict[str, Any] = {}
       semantic: bool = False
       embedder: Embedder | None = None
       _catalog: ZimCatalog | None = None
       _defer_faiss: bool = False
       _model_fingerprint: str = ""
       _cache_generation: int = 0
       preview_cache: QueryCache | None = None
       _probe_cache: dict[tuple[str, ...], Any] | None = None
       _probe_cache_lock: threading.Lock | None = None
       max_stream_ranks: int = DEFAULT_MAX_STREAM_RANKS
       stream_update_interval: float = DEFAULT_STREAM_UPDATE_INTERVAL
       preview_chars: int = DEFAULT_PREVIEW_CHARS
       preview_limit: int = DEFAULT_PREVIEW_RESULTS
       candidate_count: int = DEFAULT_CANDIDATE_COUNT
       max_results: int = DEFAULT_MAX_RESULTS
       source_timeout: float = DEFAULT_SOURCE_TIMEOUT
   ```
   *(Keep mutable container creation like `self.indexes = {}`, `self.sources = {}`, locks, and executors inside `Search.__init__` so instances never share mutable state when `__init__` is called.)*
3. Update `Search.__init__` so `self.preview_chars`, `self.max_stream_ranks`, and `self.stream_update_interval` are computed once from `self.cfg` during `__init__`.
4. Replace `getattr(self, "_defer_faiss", False)`, `getattr(self, "_model_fingerprint", "")`, `getattr(self, "_cache_generation", 0)`, `getattr(self, "_probe_cache", None)`, `getattr(self, "_probe_cache_lock", None)` with direct `self.<attr>` access.
   - Note: For `self.preview_chars`, `self.max_stream_ranks`, and `self.stream_update_interval`, check if any test sets `s.cfg = {"max_preview_chars": ...}` on an `object.__new__(Search)` instance without setting `s.preview_chars`. Check with `grep` in `tests/test_search.py` for `max_preview_chars` and `max_stream_ranks`. If a test sets `s.cfg` after `object.__new__(Search)`, make `preview_chars` / `max_stream_ranks` a `@property` or helper method on `Search` instead of inline `getattr(self, ...)` at each call site.

### Step 3: Consolidate Config Access Helpers (`zimantic/searching/config.py`)
1. View `zimantic/searching/config.py` (`_int_config`, `_float_config`).
2. Add properties or helper methods on `Search` (e.g. `self._max_article_bytes`, `self._max_html_bytes`) or a lightweight config view in `zimantic/searching/config.py` so methods like `_read_archive_article`, `_enrich_pool`, and `_search_kiwix` don't repeat magic default numbers (`33_554_432`, `DEFAULT_MAX_HTML_BYTES`, `DEFAULT_MAX_KIWIX_RESPONSE_BYTES`) in multiple places.
3. Ensure `DEFAULT_MAX_ARTICLE_BYTES = 33_554_432` is defined as a named constant in `zimantic/searching/config.py` instead of hardcoded `33_554_432` literals across `zimantic/search.py` and `zimantic/content/reader.py`.

### Step 4: Verification
1. Run the full test suite: `pytest -q`.
2. Ensure zero test failures and no unused imports.
