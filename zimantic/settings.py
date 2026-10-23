"""Shared defaults, an automatic hardware profile, and config.toml loading."""

import os
import sys
import tomllib
from pathlib import Path

DEFAULT_EMBEDDING_TOKENS = 256

# Fallback settings selected automatically when config.toml is missing (or a key
# is missing from it). Any value present in config.toml always wins; unset keys
# fall back to the profile that matches the machine running zimantic.
# Keys that do not differ per machine live in _COMMON; each profile only
# overrides the tuning keys that vary (memory, parallelism, cache and result
# pool sizes). build_workers and embedding_workers are deliberately not profile
# keys: load_config computes them from the host unless the user sets them.
_COMMON = {
    "zim_dir": "./zims",
    "model_dir": "./model",
    "index_dir": "./indexes",
    "port": 8090,
    "web_base_path": "",
    "kiwix_url": "",
    "kiwix_server": "",
    "kiwix_catalog_path": "/kiwix/catalog/v2/entries?count=50000",
    "kiwix_catalog_limit": 50_000,
    "max_kiwix_response_bytes": 64 * 1024 * 1024,
    "page_size": 10,
    "max_html_bytes": 4_194_304,
    "max_article_bytes": 33_554_432,
    "max_preview_chars": 1_000,
    "min_cosine_similarity": 0.85,
    "source_timeout": 12,
}

MOBILE = {  # ~512 MB - 1 GB RAM: Raspberry Pi Zero 2 W and similar
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

# Every key zimantic reads, including optional ones not present in the profiles.
# build_workers and embedding_workers are computed from the host by load_config
# but remain user-settable, so they must stay known.
KNOWN_KEYS = (
    frozenset(key for profile in PROFILES.values() for key in profile)
    | {"nprobe", "build_workers", "embedding_workers"}
)


def normalize_web_base_path(value: object) -> str:
    """Return a normalized URL path prefix for reverse-proxy deployments."""
    path = str(value or "").strip().strip("/")
    return f"/{path}" if path else ""


def _system_memory_gb() -> float:
    """Total physical memory in GiB, 0.0 when unknown."""
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
    if 0 < memory <= 1.0:  # Pi Zero 2 W (512 MB) and similar
        return "mobile"
    if memory >= 128.0 or cores >= 32:
        return "supercomputer"
    return "desktop"


def load_config(path: str | Path = "config.toml", warn=None) -> dict:
    """Read config.toml over hardware-matched defaults.

    A missing or unreadable config.toml is a warning, not an error: zimantic
    falls back to the profile chosen for the machine it is running on. Keys
    present in the file always override their profile default.
    """
    if warn is None:
        warn = lambda message: print(message, file=sys.stderr)  # noqa: E731
    profile = profile_for_hardware()
    cfg = dict(PROFILES[profile])
    config_path = Path(path)
    user: dict = {}
    try:
        user = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        warn(f"zimantic: warning: {path} not found; using {profile} defaults")
    except (OSError, tomllib.TOMLDecodeError) as error:
        warn(f"zimantic: warning: could not read {path} ({error}); using {profile} defaults")
    if config_path.exists() and not user:
        warn(f"zimantic: warning: {path} is empty; using {profile} defaults")
    for key in sorted(set(user) - KNOWN_KEYS):
        warn(f"zimantic: warning: unknown config key {key!r} in {path} (ignored)")
    cfg.update({key: value for key, value in user.items() if key in KNOWN_KEYS})
    # Worker counts are intentionally computed at load time so missing config
    # keys adapt to the machine instead of inheriting fixed profile values.
    # Apply this after user settings so embed_threads participates in the
    # automatic embedding concurrency calculation.
    if "build_workers" not in user:
        cfg["build_workers"] = default_build_workers()
    if "embedding_workers" not in user:
        cfg["embedding_workers"] = default_embedding_workers(
            build_workers=cfg["build_workers"],
            embed_threads=cfg.get("embed_threads"),
        )
    return cfg