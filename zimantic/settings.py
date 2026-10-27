"""Shared defaults, an automatic hardware profile, and config.toml loading."""

import os
import sys
import tomllib
from pathlib import Path

DEFAULT_EMBEDDING_TOKENS = 256

# Fallback settings selected automatically when config.toml is missing (or a key
# is missing from it). Any value present in config.toml always wins; unset keys
# fall back to the profile that matches the machine running Zimantic.
# Keys that do not differ per machine live in _COMMON; each profile only
# overrides the tuning keys that vary (memory, parallelism, cache and
# result-pool sizes). build_workers and embedding_workers are deliberately not
# profile keys: load_config computes them from the host unless the user sets them.
_COMMON = {
    "zim_dir": "./zims",
    "model_dir": "./model",
    "index_dir": "./indexes",
    "port": 8090,
    "web_base_path": "",
    "kiwix_server": "",
    "kiwix_catalog_path": "/kiwix/catalog/v2/entries?count=50000",
    "kiwix_catalog_limit": 50_000,
    "max_kiwix_response_bytes": 64 * 1024 * 1024,
    "max_remote_sources": 100,
    "page_size": 10,
    "max_html_bytes": 4_194_304,
    "max_article_bytes": 33_554_432,
    "max_preview_chars": 1_000,
    "min_cosine_similarity": 0.85,
    "source_timeout": 12,
}

MOBILE = {  # ~512 MB - 2 GB RAM: Raspberry Pi Zero 2 W and similar
    **_COMMON,
    "max_results": 50,
    "batch_size": 8,
    "max_entry_bytes": 256 * 1024 * 1024,
    "embed_threads": 2,
    "search_workers": 2,
    "max_concurrent_searches": 2,
    "candidate_count": 16,
    "max_stream_ranks": 8,
    "cache_size": 64,
    "cache_bytes": 8 * 1024 * 1024,
}

DESKTOP = {  # baseline settings for typical PCs
    **_COMMON,
    "max_results": 100,
    "batch_size": 32,
    "max_entry_bytes": 1 * 1024 * 1024 * 1024,
    "embed_threads": None,  # let the Embedder pick a sensible budget
    "search_workers": 8,
    "max_concurrent_searches": 4,
    "candidate_count": 16,
    "max_stream_ranks": 16,
    "cache_size": 256,
    "cache_bytes": 32 * 1024 * 1024,
}

SUPERCOMPUTER = {  # many cores and/or lots of RAM: scale the parallel settings up
    **_COMMON,
    "max_results": 200,
    "batch_size": 128,
    "max_entry_bytes": 4 * 1024 * 1024 * 1024,
    "embed_threads": 8,
    "search_workers": 16,
    "max_concurrent_searches": 8,
    "candidate_count": 32,
    "max_stream_ranks": 32,
    "cache_size": 1024,
    "cache_bytes": 256 * 1024 * 1024,
}

PROFILES = {"mobile": MOBILE, "desktop": DESKTOP, "supercomputer": SUPERCOMPUTER}

# Every key Zimantic reads, including optional ones not present in the profiles.
# build_workers and embedding_workers are computed from the host by load_config
# but remain user-settable, so they must stay known.
KNOWN_KEYS = (
    frozenset(key for profile in PROFILES.values() for key in profile)
    | {"nprobe", "build_workers", "embedding_workers"}
)
# Optional integer keys that have no value in the hardware profiles.
_OPTIONAL_INT_KEYS = frozenset({"nprobe", "build_workers", "embedding_workers"})


def _coerce_config_value(key: str, value: object, default: object) -> object | None:
    """Return the value converted to the expected type, or None when it is unusable.

    TOML distinguishes integers, floats and strings, but a hand-edited file can
    still put a string where a number belongs. Passing that through silently
    produces a confusing failure much later (for example a string
    ``batch_size`` or ``port``), so reject it at load time and keep the profile
    default.
    """
    if isinstance(default, bool):  # no boolean settings today, but be explicit
        return value if isinstance(value, bool) else None
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value
    if isinstance(default, str):
        return value if isinstance(value, str) else None
    # ``embed_threads`` is None in the desktop profile but is still an integer
    # setting when present.
    if key in _OPTIONAL_INT_KEYS or key == "embed_threads":
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value
    return value


def _typed_user_values(
    user: dict, defaults: dict, path: str | Path, warn
) -> dict:
    values = {}
    for key, value in user.items():
        if key not in KNOWN_KEYS:
            continue
        default = defaults.get(key)
        corrected = _coerce_config_value(key, value, default)
        if corrected is None:
            if key in _OPTIONAL_INT_KEYS or key == "embed_threads":
                expected = "integer"
            elif isinstance(default, float):
                expected = "number"
            elif isinstance(default, int):
                expected = "integer"
            elif isinstance(default, str):
                expected = "string"
            else:
                expected = "valid value"
            warn(
                f"zimantic: warning: config key {key!r} in {path} "
                f"must be a {expected}; using the profile default"
            )
            continue
        values[key] = corrected
    return values


def normalize_web_base_path(value: object) -> str:
    """Return a normalized URL path prefix for reverse-proxy deployments."""
    path = str(value or "").strip().strip("/")
    return f"/{path}" if path else ""


def _system_memory_gb() -> float:
    """Total physical memory in GiB, 0.0 when unknown."""
    if os.name == "nt":
        # os.sysconf does not exist on Windows; ask the Win32 API instead so
        # the mobile profile can still be selected on low-memory tablets.
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / (1024 ** 3)
        except (AttributeError, OSError, ValueError):
            pass
        return 0.0
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return 0.0
    return (pages * page_size) / (1024 ** 3)


def default_build_workers(cores: int | None = None, memory_gb: float | None = None) -> int:
    """Choose a bounded build pool from the host's CPU and memory budget."""
    cores = max(1, cores or os.cpu_count() or 1)
    memory_gb = _system_memory_gb() if memory_gb is None else memory_gb
    cpu_limit = max(1, cores // 4)
    memory_limit = max(1, int(memory_gb // 4)) if memory_gb > 0 else cpu_limit
    return min(8, cpu_limit, memory_limit)


def default_embedding_workers(
    cores: int | None = None,
    memory_gb: float | None = None,
    build_workers: int | None = None,
    embed_threads: int | None = None,
) -> int:
    """Choose embedding concurrency without oversubscribing CPU or memory."""
    cores = max(1, cores or os.cpu_count() or 1)
    memory_gb = _system_memory_gb() if memory_gb is None else memory_gb
    build_workers = build_workers or default_build_workers(cores, memory_gb)
    thread_budget = embed_threads if embed_threads and embed_threads > 0 else max(1, cores - 1)
    cpu_limit = max(1, cores // thread_budget)
    memory_limit = max(1, int(memory_gb // 2)) if memory_gb > 0 else 1
    return min(build_workers, cpu_limit, memory_limit)


def profile_for_hardware() -> str:
    """Pick mobile/desktop/supercomputer from a simple hardware heuristic."""
    cores = os.cpu_count() or 1
    memory = _system_memory_gb()
    if 0 < memory <= 2.0:  # Pi Zero 2 W (512 MB) through 2 GB hosts
        return "mobile"
    if memory >= 128.0 or cores >= 32:
        return "supercomputer"
    return "desktop"


def load_config(path: str | Path = "config.toml", warn=None) -> dict:
    """Read config.toml over hardware-matched defaults.

    A missing or unreadable config.toml is a warning, not an error: Zimantic
    falls back to the profile chosen for the machine it is running on. Keys
    present in the file always override their profile default.
    """
    if warn is None:
        def warn_to_stderr(message):
            print(message, file=sys.stderr)

        warn = warn_to_stderr
    profile = profile_for_hardware()
    cfg = dict(PROFILES[profile])
    config_path = Path(path)
    user: dict = {}
    loaded = False
    try:
        user = tomllib.loads(config_path.read_text(encoding="utf-8"))
        loaded = True
    except FileNotFoundError:
        warn(f"zimantic: warning: {path} not found; using {profile} defaults")
    except (OSError, tomllib.TOMLDecodeError) as error:
        warn(f"zimantic: warning: could not read {path} ({error}); using {profile} defaults")
    # Only a file that parsed successfully can be called empty. A malformed or
    # unreadable file already got its own warning above, and calling it empty
    # afterwards contradicts that message.
    if loaded and not user:
        warn(f"zimantic: warning: {path} is empty; using {profile} defaults")
    for key in sorted(set(user) - KNOWN_KEYS):
        warn(f"zimantic: warning: unknown config key {key!r} in {path} (ignored)")
    cfg.update(_typed_user_values(user, cfg, path, warn))
    # Worker counts are intentionally computed at load time so missing config
    # keys adapt to the machine instead of inheriting fixed profile values.
    # Apply this after user settings so embed_threads participates in the
    # automatic embedding concurrency calculation.
    # Use the validated values in ``cfg``, not the raw user mapping: an
    # invalid worker type is skipped above and must still get a computed
    # default instead of leaving the key missing.
    if "build_workers" not in cfg:
        cfg["build_workers"] = default_build_workers()
    if "embedding_workers" not in cfg:
        build_workers = cfg.get("build_workers")
        embed_threads = cfg.get("embed_threads")
        cfg["embedding_workers"] = default_embedding_workers(
            build_workers=build_workers if isinstance(build_workers, int) else None,
            embed_threads=embed_threads if isinstance(embed_threads, int) else None,
        )
    return cfg