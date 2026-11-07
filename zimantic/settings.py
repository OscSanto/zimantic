"""Shared defaults, an automatic hardware profile, and config.toml loading."""

import os
import sys
import tomllib
from pathlib import Path

from .paths import default_config_path, xdg_data_dir
from .searching.config import DEFAULT_MAX_ARTICLE_BYTES

DEFAULT_EMBEDDING_TOKENS = 256

# Accepted embed_device values. Kept here (instead of embed.py) so config values
# are validated at load time, before a model is loaded and even in --fast mode.
# embed.py maps these aliases to ONNX Runtime provider names.
EMBED_DEVICES = frozenset(
    {
        "auto",
        "gpu",
        "cpu",
        "cuda",
        "rocm",
        "directml",
        "dml",
        "openvino",
        "coreml",
    }
)

# Fallback settings selected automatically when config.toml is missing (or a key
# is missing from it). Any value present in config.toml always wins; unset keys
# fall back to the profile that matches the machine running Zimantic.
# Keys that do not differ per machine live in _COMMON; each profile only
# overrides the tuning keys that vary (memory, parallelism, cache and
# result-pool sizes). build_workers and embedding_workers are deliberately not
# profile keys: load_config computes them from the host unless the user sets them.
_COMMON = {
    "zim_dir": "./zims",
    "model_dir": "./model/int8",
    "index_dir": "./indexes",
    "port": 8090,
    "web_base_path": "",
    "page_size": 10,
    # UI display budget for source chips per collection group and the bounded
    # first page of preview sources loaded for one collection card.
    "source_filter_limit": 10,
    "collection_source_limit": 10,
    # Global request-work cap for an explicitly selected Kiwix collection.
    "max_explicit_collection_sources": 500,
    "embed_device": "auto",
    "embedding_batch_tokens": 1000,
    "max_html_bytes": 4_194_304,
    "max_article_bytes": DEFAULT_MAX_ARTICLE_BYTES,
    "max_preview_chars": 1_000,
    # Per-item lexical body cap, independent of the short embedding/preview
    # limit.  Fast builds intentionally have no body channel.
    "max_body_text_bytes": 1_048_576,
    # Optional derived-preview artifacts; reaching it omits new previews and
    # never removes searchable postings.
    "max_derived_cache_bytes_per_source": 536_870_912,
    "preview_limit": 10,
    "min_cosine_similarity": 0.85,
    "source_timeout": 12,
    # Monotonic deadline for one admitted search: source scheduling, ranking,
    # and bounded preview work all share it.
    "search_timeout_seconds": 30.0,
    # Seconds a search waits for one of max_concurrent_searches before the
    # server reports busy. Keep this above a typical cold-search duration on
    # slow hardware so two overlapping requests queue instead of failing.
    "slot_wait_seconds": 10.0,
    # Sandbox article documents (and therefore isolate their Web Storage).
    # Off by default: local ZIMs are trusted input and article functionality
    # (especially persistent browser storage) matters more than isolation.
    "secure_browsing": False,
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

# Every key Zimantic reads, including optional ones not present in the profiles.
# build_workers and embedding_workers are computed from the host by load_config
# but remain user-settable, so they must stay known.
KNOWN_KEYS = (
    frozenset(key for profile in PROFILES.values() for key in profile)
    | {
        "nprobe",
        "build_workers",
        "embedding_workers",
        "collection_source_limit",
    }
)
# Optional integer keys that have no value in the hardware profiles.
_OPTIONAL_INT_KEYS = frozenset(
    {
        "nprobe",
        "build_workers",
        "embedding_workers",
        "collection_source_limit",
    }
)


def _coerce_config_value(key: str, value: object, default: object) -> object | None:
    """Return the value converted to the expected type, or None when it is unusable.

    TOML distinguishes integers, floats and strings, but a hand-edited file can
    still put a string where a number belongs. Passing that through silently
    produces a confusing failure much later (for example a string
    ``batch_size`` or ``port``), so reject it at load time and keep the profile
    default.
    """
    if isinstance(default, bool):
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
        # Array-of-tables and the derived-text envelope are normalized by their
        # parsers; never run flat scalar type coercion over them.
        if key in {"collections", "derived_text"}:
            continue
        if key not in KNOWN_KEYS:
            continue
        default = defaults.get(key)
        corrected = _coerce_config_value(key, value, default)
        if corrected is None:
            if key in _OPTIONAL_INT_KEYS or key == "embed_threads":
                expected = "integer"
            elif isinstance(default, bool):
                expected = "boolean"
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
    token_budget: int = 1000,
) -> int:
    """Bound model concurrency independently of the number of source readers."""
    from .hardware import detect

    hw = detect() if cores is None or memory_gb is None else None
    cores = max(1, cores if cores is not None else hw.cores)
    available = int(memory_gb * (1 << 30)) if memory_gb is not None else hw.ram_available
    thread_budget = embed_threads if embed_threads and embed_threads > 0 else 1
    cpu_limit = max(1, (cores - 1) // thread_budget)
    # Conservative working-set allowance, not a guarantee for arbitrary models.
    per_worker = max(64 << 20, (64 << 20) * token_budget // 1000)
    memory_limit = max(1, (available - (428 << 20)) // per_worker)
    return min(8, cpu_limit, memory_limit)


def profile_for_hardware() -> str:
    """Pick mobile/desktop/supercomputer from a simple hardware heuristic."""
    cores = os.cpu_count() or 1
    memory = _system_memory_gb()
    if 0 < memory <= 1.0:  # Pi Zero 2 W (512 MB) through 1 GB hosts
        return "mobile"
    if memory >= 128.0 or cores >= 32:
        return "supercomputer"
    return "desktop"


def load_config(path: str | Path | None = None, warn=None) -> dict:
    """Read config.toml over hardware-matched defaults.

    XDG config is preferred on Unix; an existing config.toml in the current
    directory remains a compatibility fallback. A missing or unreadable config
    is a warning, not an error.  Removed flat provider keys and malformed
    ``[[collections]]`` values are fatal schema errors.
    """
    if warn is None:
        def warn_to_stderr(message):
            print(message, file=sys.stderr)

        warn = warn_to_stderr
    profile = profile_for_hardware()
    cfg = dict(PROFILES[profile])
    legacy_config = False
    data_dir: Path | None = None
    if path is None:
        config_path, legacy_config = default_config_path()
        if not legacy_config and os.name != "nt":
            data_dir = xdg_data_dir()
            cfg.update(
                zim_dir=str(data_dir / "zims"),
                model_dir=str(data_dir / "model" / "int8"),
                index_dir=str(data_dir / "indexes"),
            )
    else:
        config_path = Path(path)
    user: dict = {}
    loaded = False
    try:
        user = tomllib.loads(config_path.read_text(encoding="utf-8"))
        loaded = True
    except FileNotFoundError:
        warn(f"zimantic: warning: {config_path} not found; using {profile} defaults")
    except tomllib.TOMLDecodeError as error:
        from .providers.config import ConfigValidationError

        raise ConfigValidationError(
            f"invalid TOML in {config_path}: {error}"
        ) from error
    except OSError as error:
        warn(
            f"zimantic: warning: could not read {config_path} ({error}); "
            f"using {profile} defaults"
        )
    # Only a file that parsed successfully can be called empty. A malformed or
    # unreadable file already got its own warning above, and calling it empty
    # afterwards contradicts that message.
    if loaded and not user:
        warn(f"zimantic: warning: {config_path} is empty; using {profile} defaults")
    from .providers.config import RuntimeConfig, parse_collections

    config_dir = Path(config_path).resolve().parent
    parsed = (
        parse_collections(user, config_dir=config_dir, warn=warn)
        if loaded
        else None
    )
    provider_keys = {"collections", "derived_text"}
    for key in sorted(set(user) - KNOWN_KEYS - provider_keys):
        warn(f"zimantic: warning: unknown config key {key!r} in {config_path} (ignored)")
    cfg.update(_typed_user_values(user, cfg, config_path, warn))
    if data_dir is not None:
        for key in ("zim_dir", "model_dir", "index_dir"):
            configured_path = Path(cfg[key]).expanduser()
            if not configured_path.is_absolute():
                configured_path = data_dir / configured_path
            cfg[key] = str(configured_path)
    # Positive global request-work and body limits.  A zero or negative value
    # would either disable a channel unintentionally or make every request fail,
    # so fall back to the profile value with one clear warning.
    for key in (
        "max_explicit_collection_sources",
        "search_timeout_seconds",
        "source_timeout",
        "collection_source_limit",
    ):
        value = cfg.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
        ):
            warn(
                f"zimantic: warning: config key {key!r} in {config_path} "
                "must be a positive number; using the profile default"
            )
            cfg[key] = PROFILES[profile][key]
        elif key != "search_timeout_seconds" and isinstance(value, float):
            # Integers are integral; a float timeout is meaningful.
            cfg[key] = int(value)
    # Validate embed_device here rather than in embed.py: a typo should not stay
    # hidden until the first embedding call (or forever in --fast mode).
    device = cfg.get("embed_device")
    normalized = device.strip().casefold() if isinstance(device, str) else "auto"
    if normalized in ("", "auto"):
        cfg["embed_device"] = "auto"
    elif normalized in EMBED_DEVICES:
        cfg["embed_device"] = normalized
    else:
        warn(
            f"zimantic: warning: config key 'embed_device' in {config_path} "
            f"must be one of {', '.join(sorted(EMBED_DEVICES))} "
            f"(got {device!r}); using 'auto'"
        )
        cfg["embed_device"] = "auto"
    # Worker counts are intentionally computed at load time so missing config
    # keys adapt to the machine instead of inheriting fixed profile values.
    # Apply this after user settings so embed_threads participates in the
    # automatic embedding concurrency calculation.
    # Use the validated values in ``cfg``, not the raw user mapping: an
    # invalid worker type is skipped above and must still get a computed
    # default instead of leaving the key missing.
    for key in ("build_workers", "embedding_workers", "embedding_batch_tokens", "max_body_text_bytes", "max_derived_cache_bytes_per_source"):
        if key in cfg and (not isinstance(cfg[key], int) or isinstance(cfg[key], bool) or cfg[key] < 1):
            if key in {"build_workers", "embedding_workers"}:
                warn(
                    f"zimantic: warning: config key {key!r} in {config_path} "
                    "must be at least 1; using the automatic default"
                )
                del cfg[key]
            else:
                warn(
                    f"zimantic: warning: config key {key!r} in {config_path} "
                    "must be a positive integer; using the profile default"
                )
                cfg[key] = PROFILES[profile][key]
    if "build_workers" not in cfg:
        cfg["build_workers"] = default_build_workers()
    if "embedding_workers" not in cfg:
        build_workers = cfg.get("build_workers")
        embed_threads = cfg.get("embed_threads")
        cfg["embedding_workers"] = default_embedding_workers(
            build_workers=build_workers if isinstance(build_workers, int) else None,
            embed_threads=embed_threads if isinstance(embed_threads, int) else None,
            token_budget=cfg["embedding_batch_tokens"],
        )
    # Every collection is explicit.  A missing or empty config is an explicitly
    # empty library, not an implicit scan of the historical zim_dir default.
    collections = parsed.collections if parsed is not None else ()
    envelope = dict(parsed.envelope) if parsed is not None else {}
    diagnostics = parsed.diagnostics if parsed is not None else ()
    issues = parsed.issues if parsed is not None else ()
    from .indexing.derived import (
        parse_derived_text,
        parse_source_overrides,
        resolve_workers,
    )

    try:
        derived_text = parse_derived_text(envelope.get("derived_text"))
        overrides = parse_source_overrides(
            envelope.get("sources"),
            collection_names=[entry.name for entry in collections],
            warn=warn,
        )
    except ValueError as error:
        from .providers.config import ConfigValidationError

        raise ConfigValidationError(f"derived_text: {error}") from error
    derived_workers, worker_bound = resolve_workers(derived_text.workers)
    envelope["derived_text_workers"] = {
        "configured": derived_text.workers,
        "resolved": derived_workers,
        "bound": worker_bound,
    }
    envelope["derived_text"] = {
        "max_caption_bytes": derived_text.max_caption_bytes,
        "ocr_languages": list(derived_text.ocr_languages),
        "max_ocr_pages": derived_text.max_ocr_pages,
        "max_ocr_pixels": derived_text.max_ocr_pixels,
        "max_ocr_output_bytes": derived_text.max_ocr_output_bytes,
        "asr_engine": derived_text.asr_engine,
        "asr_model": derived_text.asr_model,
        "asr_threads": derived_text.asr_threads,
        "asr_batched": derived_text.asr_batched,
        "max_asr_seconds": derived_text.max_asr_seconds,
        "max_asr_bytes": derived_text.max_asr_bytes,
        "max_asr_output_bytes": derived_text.max_asr_output_bytes,
        "task_timeout_seconds": derived_text.task_timeout_seconds,
        "max_spool_bytes": derived_text.max_spool_bytes,
        "workers": derived_text.workers,
        "cache": derived_text.cache,
    }
    envelope["source_operations"] = {
        key: operations.state() for key, operations in overrides.items()
    }
    cfg["derived_text"] = derived_text
    cfg["derived_text_workers"] = {
        "configured": derived_text.workers,
        "resolved": derived_workers,
        "bound": worker_bound,
    }
    cfg["derived_source_operations"] = overrides
    cfg["collections"] = tuple(collections)
    cfg["config_dir"] = str(config_dir)
    cfg["config_diagnostics"] = diagnostics
    cfg["config_issues"] = issues
    cfg["config_present"] = loaded
    settings = dict(cfg)
    settings["envelope"] = envelope
    cfg["runtime_config"] = RuntimeConfig(
        settings=settings,
        collections=tuple(collections),
        config_path=Path(config_path),
        config_dir=config_dir,
        search_timeout_seconds=float(cfg.get("search_timeout_seconds", 30.0)),
        page_size=int(cfg.get("page_size", 10)),
        envelope=envelope,
        diagnostics=diagnostics,
        issues=issues,
        config_present=loaded,
    )
    return cfg