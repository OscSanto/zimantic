"""Shared defaults, an automatic hardware profile, and config.toml loading."""

import os
import sys
import tomllib
from pathlib import Path

DEFAULT_MAX_EMBEDDING_TOKENS = 256

# Fallback settings selected automatically when config.toml is missing (or a key
# is missing from it). Any value present in config.toml always wins; unset keys
# fall back to the profile that matches the machine running zimantic.
MOBILE = {  # ~512 MB - 1 GB RAM: Raspberry Pi Zero 2 W and similar
    "zim_dir": "./zims",
    "model_dir": "./model",
    "index_dir": "./indexes",
    "port": 8090,
    "kiwix_url": "http://localhost:8085/content",
    "kiwix_server": "",
    "kiwix_catalog_path": "/kiwix/catalog/v2/entries?count=-1",
    "kiwix_search_path": "/kiwix/search",
    "page_size": 10,
    "max_results": 50,
    "batch_size": 8,
    "max_html_bytes": 4_194_304,
    "max_preview_chars": 1_000,
    "max_embedding_tokens": DEFAULT_MAX_EMBEDDING_TOKENS,
    "preview_overflow": "skip",
    "embedding_overflow": "truncate",
    "embed_threads": 2,
    "min_cosine_similarity": 0.85,
    "long_query": 10,
    "disambiguation_boost": 0.08,
    "disambiguation_penalty": 0.5,
    "search_workers": 2,
    "max_concurrent_searches": 2,
    "candidate_count": 16,
    "source_timeout": 12,
    "cache_size": 64,
    "cache_bytes": 8 * 1024 * 1024,
}

DESKTOP = {  # the shipped config.toml values, for typical PCs
    "zim_dir": "./zims",
    "model_dir": "./model",
    "index_dir": "./indexes",
    "port": 8090,
    "kiwix_url": "http://localhost:8085/content",
    "kiwix_server": "",
    "kiwix_catalog_path": "/kiwix/catalog/v2/entries?count=-1",
    "kiwix_search_path": "/kiwix/search",
    "page_size": 10,
    "max_results": 100,
    "batch_size": 32,
    "max_html_bytes": 4_194_304,
    "max_preview_chars": 1_000,
    "max_embedding_tokens": DEFAULT_MAX_EMBEDDING_TOKENS,
    "preview_overflow": "skip",
    "embedding_overflow": "truncate",
    "embed_threads": None,  # let the Embedder pick a sensible budget
    "min_cosine_similarity": 0.85,
    "long_query": 10,
    "disambiguation_boost": 0.08,
    "disambiguation_penalty": 0.5,
    "search_workers": 4,
    "max_concurrent_searches": 4,
    "candidate_count": 16,
    "source_timeout": 12,
    "cache_size": 256,
    "cache_bytes": 32 * 1024 * 1024,
}

SUPERCOMPUTER = {  # many cores and/or lots of RAM: scale the parallel settings up
    "zim_dir": "./zims",
    "model_dir": "./model",
    "index_dir": "./indexes",
    "port": 8090,
    "kiwix_url": "http://localhost:8085/content",
    "kiwix_server": "",
    "kiwix_catalog_path": "/kiwix/catalog/v2/entries?count=-1",
    "kiwix_search_path": "/kiwix/search",
    "page_size": 10,
    "max_results": 200,
    "batch_size": 128,
    "max_html_bytes": 4_194_304,
    "max_preview_chars": 1_000,
    "max_embedding_tokens": DEFAULT_MAX_EMBEDDING_TOKENS,
    "preview_overflow": "skip",
    "embedding_overflow": "truncate",
    "embed_threads": 8,
    "min_cosine_similarity": 0.85,
    "long_query": 10,
    "disambiguation_boost": 0.08,
    "disambiguation_penalty": 0.5,
    "search_workers": 8,
    "max_concurrent_searches": 8,
    "candidate_count": 32,
    "source_timeout": 12,
    "cache_size": 1024,
    "cache_bytes": 256 * 1024 * 1024,
}

PROFILES = {"mobile": MOBILE, "desktop": DESKTOP, "supercomputer": SUPERCOMPUTER}

# Every key zimantic reads, including optional ones not present in the profiles
KNOWN_KEYS = frozenset(key for profile in PROFILES.values() for key in profile) | {"nprobe"}


def _system_memory_gb() -> float:
    """Total physical memory in GiB, 0.0 when unknown."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return 0.0
    return (pages * page_size) / (1024 ** 3)


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
    cfg.update(user)
    return cfg