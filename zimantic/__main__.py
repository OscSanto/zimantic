import argparse
import logging
import os
import signal
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote as quote_path

logger = logging.getLogger(__name__)

_BUILD_STOP_EVENT: threading.Event | None = None


class _LazyEmbedder:
    """Create the embedding model only when the first real batch needs it.

    A full build of already-complete indexes should not fail (or spend time
    loading a model) merely because ``model_dir`` is unavailable.
    """

    def __init__(self, factory):
        self._factory = factory
        self._lock = threading.Lock()
        self._value = None

    def _get(self):
        if self._value is None:
            with self._lock:
                if self._value is None:
                    self._value = self._factory()
        return self._value

    def truncate(self, *args, **kwargs):
        return self._get().truncate(*args, **kwargs)

    def embed(self, *args, **kwargs):
        return self._get().embed(*args, **kwargs)

    def encode(self, *args, **kwargs):
        return self._get().encode(*args, **kwargs)

    def embed_encoded(self, *args, **kwargs):
        return self._get().embed_encoded(*args, **kwargs)

    def batch_shape(self, width):
        method = getattr(self._get(), "batch_shape", None)
        return method(width) if method is not None else None

    @property
    def query_prefix(self) -> str:
        """The configured model's query prompt (empty for static models)."""
        return self._get().query_prefix

    @property
    def passage_prefix(self) -> str:
        """The configured model's passage prompt (empty for static models)."""
        return self._get().passage_prefix

    @property
    def fingerprint(self) -> str:
        """The loaded model's fingerprint, or empty when it was never needed."""
        return getattr(self._value, "fingerprint", "") if self._value is not None else ""

    @property
    def caps_tokens_on_encode(self) -> bool:
        return bool(getattr(self._get(), "caps_tokens_on_encode", False))


def _embedding_queue_size(cfg: dict, workers: int, device: str | None = None) -> int:
    """Keep a CPU read window from blocking behind its own small model calls."""
    from .embed import CPU_PROVIDER, resolve_providers

    providers, _warning = resolve_providers(device or cfg.get("embed_device"))
    size = max(2, workers * 2)
    if providers[0] == CPU_PROVIDER:
        size = max(size, int(cfg.get("batch_size", 32)))
    return size


def _graceful_interrupt(_signum: int, _frame) -> None:
    """Turn SIGTERM (a systemd stop) into KeyboardInterrupt.

    Mark an active build so worker threads stop at their next safe boundary.
    Systemd stops a unit with SIGTERM, which would otherwise kill the process
    without running its ``finally`` blocks. The raised ``KeyboardInterrupt``
    follows the same cleanup path as Ctrl-C.
    """
    if _BUILD_STOP_EVENT is not None:
        _BUILD_STOP_EVENT.set()
    raise KeyboardInterrupt


# The systemd.path unit can fire the moment a file is created. Poll until its
# size stops changing so a truncated download is not indexed.
STABLE_POLL_SECONDS = 5


def _add_verbosity_argument(parser: argparse.ArgumentParser, default=argparse.SUPPRESS) -> None:
    parser.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=default,
        help="show more log messages (-v: info, -vv: debug)",
    )


def _device_argument(value: str) -> str:
    from .settings import EMBED_DEVICES

    normalized = value.strip().casefold()
    if normalized not in EMBED_DEVICES:
        supported = ", ".join(sorted(EMBED_DEVICES))
        raise argparse.ArgumentTypeError(
            f"unknown device {value!r} (use {supported})"
        )
    return normalized


def _precision_argument(value: str) -> str:
    from .model_files import PRECISIONS

    if value not in PRECISIONS:
        supported = ", ".join(repr(precision) for precision in PRECISIONS)
        raise argparse.ArgumentTypeError(
            f"invalid precision {value!r} (choose from {supported})"
        )
    return value


def _add_device_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--device",
        metavar="DEVICE",
        type=_device_argument,
        help=(
            "embedding device: auto (default), gpu, cpu, cuda, rocm, "
            "migraphx, directml/dml, openvino or coreml; overrides "
            "embed_device in config.toml"
        ),
    )


def _get_log_level(command: str, verbosity: int) -> int:
    if command in {"serve", "open", "model"} and verbosity == 0 and sys.stderr.isatty():
        return logging.INFO
    return (logging.WARNING, logging.INFO, logging.DEBUG)[min(verbosity, 2)]


def _configure_logging(log_level: int) -> None:
    logging.basicConfig(level=log_level, format="%(message)s")
    logging.getLogger("faiss").setLevel(logging.WARNING)


def _wait_for_stable_sizes(paths: set[Path]) -> None:
    """Wait for a batch of ZIMs to finish being written before indexing them."""
    sizes: dict[Path, int] = {}
    for path in paths:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        sizes[path] = size

    while sizes:
        time.sleep(STABLE_POLL_SECONDS)
        downloading: dict[Path, int] = {}
        for path, size in sizes.items():
            try:
                current = path.stat().st_size
            except OSError:
                continue
            if current != size:
                downloading[path] = current
                logger.info(
                    "zimantic: %s is now %s bytes; waiting for it to finish downloading",
                    path.name,
                    current,
                )
        sizes = downloading


def _find_downloading_sizes(zim_sizes: dict[Path, int]) -> set[Path]:
    """Find ZIMs whose sizes changed during the initial polling interval."""
    time.sleep(STABLE_POLL_SECONDS)

    downloading: set[Path] = set()
    for path, size in zim_sizes.items():
        try:
            current = path.stat().st_size
        except OSError:
            continue
        if current != size:
            downloading.add(path)
    return downloading


def _wait_for_configured_zim_downloads(entries: list) -> None:
    """Apply ``--wait-stable`` to configured ZIM collections.

    The positional build already polls the ZIM files named on the command
    line. Configured builds reached the same code path without ever reading
    the flag, so the deploy units passed it with no effect.
    """
    paths: set[Path] = set()
    for entry in entries:
        if getattr(entry, "kind", None) != "zim":
            continue
        path = Path(getattr(entry, "path", "") or "")
        try:
            if path.is_file():
                paths.add(path)
            elif path.is_dir():
                paths.update(_collect_zims([path], path))
        except OSError:
            continue
    sizes: dict[Path, int] = {}
    for path in paths:
        try:
            sizes[path] = path.stat().st_size
        except OSError:
            continue
    if not sizes:
        return
    downloading = _find_downloading_sizes(sizes)
    if downloading:
        _wait_for_stable_sizes(downloading)


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zimantic")
    _add_verbosity_argument(parser, default=0)

    # subcommands: build (files/folders, default zim_dir), serve, reload
    commands = parser.add_subparsers(dest="command", required=True)

    serve_cmd = commands.add_parser("serve", help="start the search page and JSON API")
    _add_verbosity_argument(serve_cmd)
    serve_cmd.add_argument(
        "--fast",
        action="store_true",
        help=(
            "start without the model or FAISS vectors; search uses available "
            "title and full-text indexes"
        ),
    )
    serve_cmd.add_argument(
        "--debug",
        "--dev",
        dest="debug",
        action="store_true",
        help="show ranking scores; SIGHUP restarts the server in this mode",
    )
    _add_device_argument(serve_cmd)

    open_cmd = commands.add_parser(
        "open",
        help=(
            "serve named ZIM files locally without writing to the catalog, "
            "indexes, PID file, or config"
        ),
    )
    _add_verbosity_argument(open_cmd)
    open_cmd.add_argument(
        "paths",
        nargs="+",
        type=Path,
        metavar="PATH",
        help="ZIM file or folder; folders are searched recursively for *.zim files",
    )
    open_cmd.add_argument(
        "--port",
        type=int,
        default=0,
        metavar="PORT",
        help="use this loopback port (default: choose a port)",
    )
    open_cmd.add_argument(
        "--no-browser",
        action="store_true",
        help="print the URL without launching a browser",
    )
    open_cmd.add_argument(
        "--fast",
        action="store_true",
        help=(
            "disable the model and FAISS; search uses available title and full-text indexes"
        ),
    )
    open_cmd.add_argument(
        "--debug",
        dest="debug",
        action="store_true",
        help="include ranking scores in results",
    )
    _add_device_argument(open_cmd)
    open_cmd.add_argument(
        "--max-sources",
        type=int,
        default=64,
        metavar="N",
        help="reject folders with more than N archives (default: 64)",
    )

    diagnose_cmd = commands.add_parser(
        "diagnose", help="check named ZIM files in disposable crash-isolated helpers"
    )
    _add_verbosity_argument(diagnose_cmd)
    diagnose_cmd.add_argument(
        "paths", nargs="+", type=Path, metavar="PATH",
        help="ZIM file or folder; does not load config or write indexes/catalogs",
    )
    diagnose_cmd.add_argument(
        "--query", action="append", metavar="TEXT",
        help="native-search probe; repeatable (default: water, history, science)",
    )
    diagnose_cmd.add_argument(
        "--checksum", action="store_true",
        help="also verify the archive checksum (reads the entire archive)",
    )
    diagnose_cmd.add_argument(
        "--timeout", type=float, default=30.0, metavar="SECONDS",
        help="time limit for all checks on each archive (default: 30)",
    )
    diagnose_cmd.add_argument(
        "--limit", type=int, default=16, metavar="N",
        help="iterate up to N native hits per probe (default: 16)",
    )
    diagnose_cmd.add_argument("--json", action="store_true", help="write reports as JSON")
    diagnose_cmd.add_argument(
        "--max-sources", type=int, default=64, metavar="N",
        help="reject selections with more than N archives (default: 64)",
    )

    build = commands.add_parser(
        "build", help="build indexes for configured collections or selected ZIM files"
    )
    _add_verbosity_argument(build)
    build.add_argument(
        "paths",
        nargs="*",
        type=Path,
        metavar="PATH",
        help=(
            "local files or folders; match configured collections or register "
            "new ZIM/files collections. Subpaths build cumulative partial indexes. "
            "With no paths, build every configured collection. "
            "Do not combine paths with selectors."
        ),
    )
    build.add_argument(
        "--provider",
        action="append",
        default=[],
        metavar="KIND",
        help="select collections by provider type (zim, files, kolibri, kiwix); repeatable",
    )
    build.add_argument(
        "--collection",
        action="append",
        default=[],
        metavar="KEY|NAME",
        help="select this exact collection key or configured entry name; repeatable",
    )
    build.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="KEY",
        help="select this exact source key; repeatable",
    )
    build.add_argument(
        "--list",
        action="store_true",
        help="list selected collections without writing config or building indexes",
    )
    build.add_argument(
        "--fast",
        action="store_true",
        help="skip article bodies, the model, and FAISS; build title and ZIM full-text indexes",
    )
    build.add_argument(
        "--force",
        action="store_true",
        help="rebuild selected indexes, even when they are complete",
    )
    _add_device_argument(build)
    build.add_argument(
        "--wait-stable",
        action="store_true",
        help=(
            "wait until every ZIM's size stops changing before indexing "
            "(for downloaded files and systemd.path triggers)"
        ),
    )
    build.add_argument(
        "-j",
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="build up to N ZIM indexes at once (default: config build_workers)",
    )
    build.add_argument(
        "--no-wait-lock",
        action="store_true",
        help=(
            "exit with status 75 if another build holds the index lock "
            "(for systemd.path and timer triggers)"
        ),
    )
    build.add_argument(
        "--lock-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="maximum seconds to wait for the index lock (default: no limit)",
    )

    sources_cmd = commands.add_parser(
        "sources",
        help="list configured collections and their discovered sources (read-only)",
    )
    _add_verbosity_argument(sources_cmd)
    sources_cmd.add_argument(
        "--json",
        action="store_true",
        help="write collections and sources as one JSON document without paths",
    )
    sources_cmd.add_argument(
        "--provider",
        action="append",
        default=[],
        metavar="KIND",
        help="select collections by provider type; repeatable",
    )
    sources_cmd.add_argument(
        "--collection",
        action="append",
        default=[],
        metavar="KEY|NAME",
        help="select this exact collection key or configured entry name; repeatable",
    )
    sources_cmd.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="KEY",
        help="select this exact source key; repeatable",
    )

    catalog_cmd = commands.add_parser(
        "catalog",
        help="browse and filter library metadata and local paths (read-only, no server needed)",
    )
    _add_verbosity_argument(catalog_cmd)
    catalog_cmd.add_argument(
        "query",
        nargs="?",
        help="case-insensitive substring of source or collection display metadata",
    )
    catalog_cmd.add_argument("--json", action="store_true", help="write metadata and paths as JSON")
    catalog_cmd.add_argument(
        "--relative",
        action="store_true",
        help="show paths relative to cwd (default: absolute; other Windows drives stay absolute)",
    )
    catalog_cmd.add_argument(
        "--provider", action="append", default=[], metavar="KIND",
        help="select provider types (zim, files, kolibri, kiwix); repeatable",
    )
    catalog_cmd.add_argument(
        "--collection", action="append", default=[], metavar="KEY|NAME",
        help="select an exact collection key or configured entry name; repeatable",
    )
    catalog_cmd.add_argument(
        "--source", action="append", default=[], metavar="KEY",
        help="select an exact source key; repeatable",
    )
    from .media_types import MEDIA_TYPE_ORDER

    catalog_cmd.add_argument(
        "--media-type", choices=MEDIA_TYPE_ORDER,
        help="select sources with this content family",
    )
    catalog_cmd.add_argument("--limit", type=int, metavar="N", help="maximum entries (default: all)")
    catalog_cmd.add_argument("--offset", type=int, default=0, metavar="N", help="skip N matching entries")

    prune_cmd = commands.add_parser(
        "prune",
        help="report or delete Zimantic indexes for unconfigured sources",
    )
    _add_verbosity_argument(prune_cmd)
    prune_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="list orphaned indexes without deleting them (default)",
    )
    prune_cmd.add_argument(
        "--yes",
        action="store_true",
        help="delete reported indexes under the build and config locks",
    )

    reload_cmd = commands.add_parser(
        "reload",
        help=(
            "ask a running server to rescan indexes and ZIM files (sends SIGHUP)"
        ),
    )
    _add_verbosity_argument(reload_cmd)
    reload_cmd.add_argument(
        "--pid",
        type=Path,
        default=None,
        help=(
            "file holding the server's PID (default: "
            "$XDG_RUNTIME_DIR/zimantic/zimantic.pid, or ./zimantic.pid)"
        ),
    )

    model_cmd = commands.add_parser(
        "model",
        help="download ONNX model and tokenizer files",
    )
    _add_verbosity_argument(model_cmd)
    model_cmd.add_argument(
        "precision",
        nargs="*",
        type=_precision_argument,
        metavar="PRECISION",
        help=(
            "fp32, fp16 or int8; repeat for several. Default: int8 for CPU; "
            "use fp16 with a GPU. The default model writes <dir>/<precision>; "
            "other models write <dir>/<model>/<precision>"
        ),
    )
    model_cmd.add_argument(
        "--model",
        default=None,
        metavar="NAME",
        help=(
            "model to download: static-similarity-mrl-multilingual-v1 "
            "(default; 256-dimensional static embeddings), "
            "multilingual-e5-small (the previous default; 384-dimensional "
            "transformer), multilingual-e5-base, multilingual-e5-large, "
            "e5-small-v2, e5-base-v2 or e5-large-v2"
        ),
    )
    model_cmd.add_argument(
        "--dir",
        type=Path,
        metavar="DIR",
        help="destination model root (default: model_dir from config.toml)",
    )
    model_cmd.add_argument(
        "--force",
        action="store_true",
        help=        "download files again and replace existing files, including files with a checksum mismatch",
    )
    model_cmd.add_argument(
        "--list",
        action="store_true",
        help="list supported models and precisions, then exit",
    )
    return parser


def _collect_zims(paths: list[Path], default_dir: str | Path) -> list[Path]:
    """Expand CLI files and folders into unique ZIM paths."""
    from .providers.zim.discovery import is_zim_filename, iter_zim_files

    zims: list[Path] = []
    seen: set[Path] = set()
    for source in (paths or [Path(default_dir)]):
        if source.is_dir():
            matches = [zim for zim in iter_zim_files(source) if zim.is_file()]
        elif source.is_file():
            if not is_zim_filename(source.name):
                sys.exit(f"zimantic: not a ZIM file: {source}")
            matches = [source]
        else:
            sys.exit(f"zimantic: path not found: {source}")
        for zim in matches:
            resolved = zim.resolve()
            if resolved not in seen:
                seen.add(resolved)
                zims.append(zim)
    return zims


def _select_zim_paths(zims: list[Path], zim_sizes: dict[Path, int]) -> list[Path]:
    """Choose one archive per index identity, preferring the largest copy."""
    from .providers.zim.discovery import largest_zims_by_stem

    by_stem: dict[str, list[Path]] = {}
    for zim in zims:
        by_stem.setdefault(zim.stem, []).append(zim)
    selected = largest_zims_by_stem({path: zim_sizes[path] for path in zims})
    accepted: list[Path] = []
    for stem, paths in sorted(by_stem.items()):
        sizes = {zim_sizes[path] for path in paths}
        # Build and serve use the largest copy for an ambiguous stem. The
        # catalog keeps every row but resolves the stem to its largest file, so
        # all three agree on which archive wins.
        chosen = selected[stem]
        if len(sizes) > 1:
            logger.warning(
                "zimantic: ZIM files share index identity %r with different sizes; "
                "using the largest %s",
                stem,
                chosen,
            )
        elif len(paths) > 1:
            logger.warning(
                "zimantic: ignoring duplicate ZIM files with index identity %r: %s",
                stem,
                ", ".join(str(path) for path in paths if path != chosen),
            )
        accepted.append(chosen)
    return sorted(accepted, key=zim_sizes.__getitem__)




def _build_producer_for_zim(zim: Path, cfg: dict, *, fast: bool):
    """Build the ZIM producer through the configured collection entry."""
    from types import SimpleNamespace

    from .providers.zim.discovery import (
        relative_archive,
        zim_collection_key,
        zim_source_key,
    )
    from .providers.zim.entries import create_zim_producer

    entry = _most_specific_zim_entry(zim, cfg.get("collections") or ())
    collection_name = getattr(entry, "name", "library")
    root = Path(getattr(entry, "path", None) or zim)
    relative = zim.name if root.is_file() else relative_archive(root, zim)
    source = SimpleNamespace(
        key=zim_source_key(collection_name, relative),
        provider="zim",
        collection_key=zim_collection_key(collection_name),
        provider_id=None,
        path=str(zim),
        max_entries=getattr(entry, "max_entries", None),
    )
    from .catalog import catalog_path

    return create_zim_producer(
        source,
        fast=fast,
        cfg=cfg,
        max_entries=getattr(entry, "max_entries", None),
        catalog_path=catalog_path(cfg["index_dir"]),
    )


def _build_config(cfg: dict, fast: bool) -> dict:
    """Copy build settings and attach locally available derived-text engines."""

    values = dict(cfg)
    if not fast and "derived_engines" not in values:
        from .indexing.engines import available_derived_engines

        values["derived_engines"] = available_derived_engines()
    return values


def _make_build_embedding_context(cfg: dict, args, parser: argparse.ArgumentParser):
    """Create the lazy embedder and shared pipeline for any build path.

    Positional and configured builds must agree on ``onnx_build_threads``,
    ``onnx_build_workers``, queue size, and token budget. ``--workers`` only
    controls concurrent source builds; it must not cap model concurrency.
    """
    if getattr(args, "fast", False):
        return None, None

    from .embed import Embedder
    from .hardware import detect
    from .indexing.pipeline import EmbeddingPipeline
    from .settings import default_embedding_workers

    build_embed_threads = cfg.get("onnx_build_threads") or 1
    token_budget = int(cfg.get("embedding_batch_tokens", 1000))

    def _make_embedder():
        return Embedder(
            cfg["model_dir"],
            threads=build_embed_threads,
            device=getattr(args, "device", None) or cfg.get("embed_device"),
            batch_tokens=token_budget,
            onnx_warnings=bool(getattr(args, "verbose", 0)),
        )

    embedder = _LazyEmbedder(_make_embedder)
    hw = detect()
    embedding_workers = int(
        cfg.get("onnx_build_workers")
        or default_embedding_workers(
            build_embed_threads=build_embed_threads,
            token_budget=token_budget,
        )
    )
    if embedding_workers < 1 or token_budget < 1:
        parser.error(
            "onnx_build_workers and embedding_batch_tokens must be at least 1"
        )
    logger.info(
        "zimantic: build hardware: %s; %d embedding workers x %d threads, "
        "%d padded tokens per call",
        hw.describe(),
        embedding_workers,
        build_embed_threads,
        token_budget,
    )
    pipeline = EmbeddingPipeline(
        embedder,
        workers=embedding_workers,
        queue_size=_embedding_queue_size(
            cfg, embedding_workers, getattr(args, "device", None)
        ),
        token_budget=token_budget,
    )
    return embedder, pipeline


def _provider_kinds() -> tuple[str, ...]:
    from .providers.config import BUILTIN_PROVIDERS

    return tuple(BUILTIN_PROVIDERS)


def _emitted_provider_selectors(values: list[str] | None) -> set[str]:
    from .providers.config import ConfigValidationError

    result = set()
    for value in values or ():
        kind = str(value).strip().casefold()
        if kind not in BUILTIN_PROVIDER_NAMES:
            raise ConfigValidationError(
                f"unknown provider {value!r}; expected one of "
                f"{', '.join(_provider_kinds())}"
            )
        result.add(kind)
    return result


BUILTIN_PROVIDER_NAMES = frozenset(_provider_kinds())


def _entry_collection_key(entry) -> str:
    return f"{entry.kind}:{entry.name}"


def _entry_matches_selector(entry, collection_selectors: set[str]) -> bool:
    if not collection_selectors:
        return True
    if entry.name in collection_selectors:
        return True
    if _entry_collection_key(entry) in collection_selectors:
        return True
    if entry.kind == "kolibri":
        prefix = f"kolibri:{entry.name}:"
        return any(value.startswith(prefix) for value in collection_selectors)
    return False


def _collection_matches_selector(
    entry, collection_key: str, collection_selectors: set[str]
) -> bool:
    if not collection_selectors:
        return True
    return (
        collection_key in collection_selectors
        or entry.name in collection_selectors
        or _entry_collection_key(entry) in collection_selectors
    )


def _selected_configured_entries(
    cfg: dict,
    *,
    provider_selectors: list[str] | None = None,
    collection_selectors: list[str] | None = None,
    source_selectors: list[str] | None = None,
) -> list:
    entries = list(cfg.get("collections") or ())
    providers = _emitted_provider_selectors(provider_selectors)
    collections = {str(value) for value in collection_selectors or ()}
    source_keys = {str(value) for value in source_selectors or ()}
    selected = []
    for entry in entries:
        if providers and entry.kind not in providers:
            continue
        if not _entry_matches_selector(entry, collections):
            continue
        if source_keys:
            prefix = f"{entry.kind}:{entry.name}:"
            if not any(key.startswith(prefix) for key in source_keys):
                continue
        selected.append(entry)
    return selected


def _config_relative_path(path: Path, base: Path) -> str:
    import os

    try:
        return os.path.relpath(path, base)
    except ValueError:
        # Different drives on Windows: an absolute locator is safer than an
        # unresolvable relative path.
        return str(path)


def _unique_collection_name(path: Path, used: set[str], *, kind: str = "zim") -> str:
    import re

    from .providers.config import RESERVED_ZIM_NAMES, SAFE_NAME

    stem = path.stem if path.is_file() else path.name
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or kind
    if not base[0].isalnum():
        base = kind + "-" + base
    if base in RESERVED_ZIM_NAMES:
        base = f"{kind}-{base}"
    base = base[:64].strip("-._") or kind
    if not SAFE_NAME.fullmatch(base):
        base = kind
    name = base
    suffix = 2
    while name in used or name in RESERVED_ZIM_NAMES:
        tail = f"-{suffix}"
        name = base[: max(1, 64 - len(tail))].strip("-._") + tail
        suffix += 1
    return name


def _zim_matches(path: Path, entries) -> list[tuple[int, object]]:
    target = path.expanduser().resolve()
    matches: list[tuple[int, object]] = []
    for entry in entries:
        if getattr(entry, "kind", None) != "zim":
            continue
        root = Path(entry.path).expanduser()
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if resolved_root.is_file():
            if resolved_root == target:
                matches.append((len(str(resolved_root)), entry))
            continue
        try:
            target.relative_to(resolved_root)
        except ValueError:
            continue
        matches.append((len(str(resolved_root)), entry))
    return matches


def _most_specific_zim_entry(path: Path, entries):
    from .providers.config import ConfigValidationError

    matches = _zim_matches(path, entries)
    if not matches:
        return None
    longest = max(length for length, _entry in matches)
    winners = [entry for length, entry in matches if length == longest]
    if len(winners) > 1:
        names = ", ".join(sorted(_entry_collection_key(entry) for entry in winners))
        raise ConfigValidationError(
            f"{path} matches multiple configured ZIM collections equally "
            f"specifically ({names}); remove or rename one of the overlapping roots"
        )
    return winners[0]


def _local_entry_roots(entry) -> list[Path]:
    if entry.kind == "kiwix":
        return []
    return [
        Path(value).expanduser().resolve()
        for field in ("path", "database_path", "storage")
        if (value := getattr(entry, field, None))
    ]


def _matching_path_entries(path: Path, entries) -> list:
    from .providers.config import ConfigValidationError

    target = path.expanduser().resolve()
    covering: dict[str, list[tuple[int, object]]] = {}
    descendants = []
    for entry in entries:
        if entry.kind == "zim" and target.is_file() and target.suffix.casefold() != ".zim":
            continue
        roots = _local_entry_roots(entry)
        matches = [root for root in roots if target == root or target.is_relative_to(root)]
        if matches:
            covering.setdefault(entry.kind, []).append(
                (max(len(root.parts) for root in matches), entry)
            )
        elif target.is_dir() and any(root.is_relative_to(target) for root in roots):
            descendants.append(entry)
    result = []
    for kind, matches in covering.items():
        longest = max(length for length, _entry in matches)
        winners = [entry for length, entry in matches if length == longest]
        if len(winners) > 1:
            names = ", ".join(sorted(_entry_collection_key(entry) for entry in winners))
            raise ConfigValidationError(
                f"{path} matches multiple configured {kind} collections equally "
                f"specifically ({names}); use --collection to select one explicitly"
            )
        result.extend(winners)
    return result + descendants


def _infer_path_kinds(path: Path) -> list[str]:
    from .providers.files.common import is_hidden_name, list_directory
    from .providers.files.discovery import _Budget
    from .providers.zim.discovery import is_zim_filename

    if path.is_file():
        return ["zim"] if is_zim_filename(path.name) else ["files"]
    kinds = set()
    budget = _Budget(1_000_000, 300)

    def visit(relative: str) -> None:
        for item in list_directory(path, relative):
            budget.tick()
            if is_hidden_name(item.name) or item.is_symlink():
                continue
            child = f"{relative}/{item.name}" if relative else item.name
            if item.is_dir(follow_symlinks=False):
                visit(child)
            elif item.is_file(follow_symlinks=False):
                kinds.add("zim" if is_zim_filename(item.name) else "files")

    visit("")
    return [kind for kind in ("zim", "files") if kind in kinds] or ["files"]


def _persist_unmatched_paths(
    cfg: dict,
    paths: list[tuple[str, Path]],
    *,
    preview: bool = False,
) -> list:
    """Append each unmatched positional root as an explicit local collection.

    With ``preview`` the names and paths are computed but nothing is written.
    The real update takes the target-based config lock, rereads the config, and
    rechecks containment and name uniqueness immediately before replacement.
    """
    from dataclasses import replace
    from types import SimpleNamespace

    from .config_writer import append_collection, config_target_path
    from .providers.config import get_provider_config_spec, parse_collections

    runtime_config = cfg.get("runtime_config")
    config_path = getattr(runtime_config, "config_path", None)
    if config_path is None or not paths:
        return []
    target = config_target_path(config_path)
    entries = list(cfg.get("collections") or ())
    used_names = {entry.name for entry in entries}
    appended: list = []
    seen: set[tuple[str, Path]] = set()
    for kind, raw in paths:
        candidate = Path(raw).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        identity = (kind, resolved)
        if identity in seen:
            continue
        seen.add(identity)
        if _matching_path_entries(resolved, [entry for entry in entries if entry.kind == kind]):
            continue
        if preview:
            name = _unique_collection_name(resolved, used_names, kind=kind)
            used_names.add(name)
            parsed_entry = get_provider_config_spec(kind).parse(
                {"type": kind, "name": name, "path": str(resolved)}, target.parent
            )
        else:
            current_entries = []

            def entry_factory(
                data: dict,
                _resolved: Path = resolved,
                _kind: str = kind,
            ):
                nonlocal current_entries
                parsed = parse_collections(
                    data, config_dir=target.parent, warn=lambda _message: None
                )
                current = list(parsed.collections)
                current_entries = current
                if _matching_path_entries(
                    _resolved, [entry for entry in current if entry.kind == _kind]
                ):
                    return None
                used = {entry.name for entry in current}
                name = _unique_collection_name(_resolved, used, kind=_kind)
                return {
                    "type": _kind,
                    "name": name,
                    "path": _config_relative_path(_resolved, target.parent),
                }

            try:
                parsed_entry = append_collection(config_path, entry_factory)
            except Exception as error:
                raise RuntimeError(str(error)) from error
        if parsed_entry is None:
            cfg["collections"] = tuple(current_entries)
            cfg["runtime_config"] = replace(
                cfg["runtime_config"],
                settings={**cfg["runtime_config"].settings, "collections": cfg["collections"]},
                collections=cfg["collections"],
            )
            entries = current_entries
            used_names = {entry.name for entry in entries}
            continue
        cfg["collections"] = tuple(cfg.get("collections") or ()) + (parsed_entry,)
        cfg["runtime_config"] = replace(
            cfg["runtime_config"],
            settings={**cfg["runtime_config"].settings, "collections": cfg["collections"]},
            collections=cfg["collections"],
        )
        entries.append(parsed_entry)
        used_names.add(parsed_entry.name)
        appended.append(
            SimpleNamespace(
                kind=parsed_entry.kind,
                name=parsed_entry.name,
                path=str(resolved),
                saved=_config_relative_path(resolved, target.parent),
                entry=parsed_entry,
            )
        )
    return appended


def _warn_about_appended_collections(appended: list, cfg: dict) -> None:
    if not appended:
        return
    config_path = getattr(cfg.get("runtime_config"), "config_path", None)
    logger.warning(
        "zimantic: added %d collection entr%s to %s; restart a running "
        "serve process to load the changed config (SIGHUP reload is not enough):",
        len(appended),
        "y" if len(appended) == 1 else "ies",
        config_path or "the active config",
    )
    for item in appended:
        logger.warning("zimantic:   %s:%s -> %s", item.kind, item.name, item.saved)


def _log_build_report(report) -> None:
    """Log one provider-neutral, path-free build report."""
    logger.info(
        "zimantic: build report: provider=%s collection=%s source=%s "
        "status=%s indexed=%d skipped=%d truncated=%s "
        "body_truncated=%d duration=%.2fs",
        report.provider,
        report.collection_key or "-",
        report.source_key,
        report.status,
        report.indexed_count,
        report.skipped_count,
        "true" if report.truncated else "false",
        report.body_truncated_count,
        report.duration_seconds,
    )


def _log_build_failure(source: object, error: BaseException, cfg: dict) -> None:
    """Log a path-free failure report for one source."""
    logger.error(
        "zimantic: build report: provider=%s collection=%s source=%s "
        "status=failed failure=%s",
        getattr(source, "provider", "-"),
        getattr(source, "collection_key", "-") or "-",
        getattr(source, "key", "-"),
        _sanitize_public_text(str(error), cfg),
    )


def _build_tasks(
    tasks: list[tuple[str, object, object]],
    cfg: dict,
    args,
    parser: argparse.ArgumentParser,
    *,
    paths: list[Path] | None = None,
) -> None:
    """Build one or more already-discovered sources through the generic pipeline."""
    from .indexing.locator import IndexLocator
    from .indexing.pipeline import _AllFilesProgress, build_index

    workers = args.workers
    if workers is None:
        workers = cfg.get("build_workers", 1)
    if workers < 1:
        parser.error("--workers must be at least 1")

    embedder, embedding_pipeline = _make_build_embedding_context(cfg, args, parser)

    build_cfg = _build_config(cfg, args.fast)
    derived_pool = None
    if not args.fast:
        from .indexing.derived import DerivedWorkerPool, resolve_workers

        derived_engines = build_cfg.get("derived_engines") or {}
        resolved_workers = None
        worker_info = cfg.get("derived_text_workers")
        if isinstance(worker_info, dict):
            resolved_workers = worker_info.get("resolved")
        if resolved_workers is None:
            from .hardware import detect

            hw = detect()
            resolved_workers, _bound = resolve_workers(
                getattr(cfg.get("derived_text"), "workers", "auto"),
                usable_cores=hw.threads,
                available_memory=hw.ram_available,
            )
        operations_enabled = any(
            ops.any_enabled
            for key in ("derived_collection_operations", "derived_source_operations")
            for ops in (cfg.get(key) or {}).values()
        )
        if derived_engines and resolved_workers and operations_enabled:
            derived_pool = DerivedWorkerPool(int(resolved_workers))
            build_cfg["derived_worker_pool"] = derived_pool
            logger.info(
                "zimantic: using %d derived-text worker(s)",
                int(resolved_workers),
            )
        elif derived_engines and operations_enabled:
            # No worker fits the current CPU/memory budget. Disable only the
            # engine-backed operations; the built-in caption parser still runs.
            build_cfg["derived_engines"] = {}
            logger.warning(
                "zimantic: no derived-text worker fits in available memory; "
                "OCR, ASR, and embedded caption extraction are unavailable"
            )
    build_cfg["embedding_pipeline"] = embedding_pipeline
    build_cfg["interrupt_event"] = _BUILD_STOP_EVENT or threading.Event()
    # When several source builds already run concurrently, source-level overlap
    # fills the shared inference pool; per-source stage threads only add CPU
    # contention.  A single active source uses the reader/preparer/writer split.
    concurrent_sources = (
        1 if workers == 1 or len(tasks) == 1 else min(workers, len(tasks))
    )
    build_cfg["_stage_split"] = concurrent_sources == 1
    build_cfg["_concurrent_sources"] = concurrent_sources
    locator = IndexLocator(cfg["index_dir"])
    overall = _AllFilesProgress(len(tasks))

    def run_task(index: int, runtime: object, source: object):
        task_interrupted = False
        try:
            return build_task(index, runtime, source)
        except KeyboardInterrupt:
            task_interrupted = True
            raise
        finally:
            stop_event = build_cfg["interrupt_event"]
            if not task_interrupted and (stop_event is None or not stop_event.is_set()):
                overall.finish(index)

    def build_task(index: int, runtime: object, source: object):
        producer = runtime.entry_producer(source, fast=args.fast)
        if paths is not None:
            from .indexing.scope import restrict_producer

            try:
                producer = restrict_producer(producer, paths, locator)
            except BaseException:
                producer.close()
                raise
            if producer is None:
                return None
        return build_index(
            producer,
            locator,
            embedder,
            {
                **build_cfg,
                "progress_callback": lambda position, total: overall.advance(
                    index, position, total
                ),
            },
            fast=args.fast,
            force=args.force,
        )

    failures: list[tuple[str, BaseException]] = []
    interrupted = False
    try:
        if workers == 1 or len(tasks) == 1:
            for index, (label, runtime, source) in enumerate(tasks):
                try:
                    report = run_task(index, runtime, source)
                    if report is not None:
                        _log_build_report(report)
                except Exception as error:
                    _log_build_failure(source, error, cfg)
                    logger.debug("build error for %s: %s", label, error)
                    failures.append((label, error))
        else:
            pool = ThreadPoolExecutor(
                max_workers=min(workers, len(tasks)),
                thread_name_prefix="zimantic-build",
            )
            try:
                futures = {
                    pool.submit(run_task, index, runtime, source): (label, source)
                    for index, (label, runtime, source) in enumerate(tasks)
                }
                for future in as_completed(futures):
                    label, source = futures[future]
                    try:
                        report = future.result()
                        if report is not None:
                            _log_build_report(report)
                    except Exception as error:
                        _log_build_failure(source, error, cfg)
                        logger.debug("build error for %s: %s", label, error)
                        failures.append((label, error))
            except KeyboardInterrupt:
                interrupted = True
                build_cfg["interrupt_event"].set()
                logger.warning("zimantic: cancelling pending builds")
                raise
            finally:
                pool.shutdown(wait=not interrupted, cancel_futures=True)
    except KeyboardInterrupt:
        interrupted = True
        build_cfg["interrupt_event"].set()
        raise
    finally:
        overall.close()
        if derived_pool is not None:
            if interrupted:
                derived_pool.terminate()
            else:
                derived_pool.close()
        if embedding_pipeline is not None:
            embedding_pipeline.close(wait=not interrupted)
    if failures:
        logger.error(
            "zimantic: %d source build(s) failed; see the errors above",
            len(failures),
        )
        sys.exit(1)


def _log_discovery_diagnostics(result, cfg: dict | None = None) -> None:
    for message in result.diagnostics:
        if cfg is not None:
            message = _sanitize_public_text(message, cfg)
        logger.warning("zimantic: collection discovery: %s", message)


def _run_configured_build(
    cfg: dict,
    args,
    parser: argparse.ArgumentParser,
    *,
    entries: list | None = None,
    paths: list[Path] | None = None,
) -> None:
    from .discovery import discover_collections

    explicit_scope = bool(args.provider or args.collection or args.source)
    if entries is None:
        entries = _selected_configured_entries(
            cfg,
            provider_selectors=args.provider,
            collection_selectors=args.collection,
            source_selectors=args.source,
        )
    if not entries:
        if explicit_scope:
            parser.error("no configured collection matches the selected scope")
        logger.info("zimantic: no sources to index")
        return
    buildable = [entry for entry in entries if entry.kind != "kiwix"]
    remote = [entry for entry in entries if entry.kind == "kiwix"]
    for entry in remote:
        logger.info(
            "zimantic: skipping kiwix collection %r: remote collections are "
            "search-only and cannot be indexed",
            entry.name,
        )
    if not buildable:
        if explicit_scope or (args.source and remote):
            parser.error(
                "the selected scope contains no buildable sources; Kiwix is "
                "remote/search-only"
            )
        logger.info("zimantic: no local sources to index")
        return

    if args.wait_stable and not args.list and paths is None:
        _wait_for_configured_zim_downloads(buildable)

    if args.list:
        with discover_collections(
            cfg, entries=buildable, wait=True, persist_catalog=False
        ) as result:
            _log_discovery_diagnostics(result, cfg)
            source_selectors = set(args.source)
            if paths is not None:
                from .indexing.locator import IndexLocator
                from .indexing.scope import restrict_producer

                locator = IndexLocator(cfg["index_dir"])
                for discovered in result.collections:
                    for source in discovered.runtime.library_sources():
                        producer = discovered.runtime.entry_producer(source, fast=True)
                        try:
                            scoped = restrict_producer(producer, paths, locator)
                            if scoped is not None:
                                source_selectors.add(source.key)
                        finally:
                            producer.close()
                if not source_selectors:
                    print("No matching sources.")
                    return
            _print_discovery_tree(
                result,
                collection_selectors=set(args.collection),
                source_selectors=source_selectors,
            )
        return

    with discover_collections(
        cfg,
        entries=buildable,
        wait=True,
        persist_catalog=bool(cfg.get("index_dir")),
    ) as result:
        _log_discovery_diagnostics(result, cfg)
        discovery_failed = bool(result.unavailable_entries)
        tasks: list[tuple[str, object, object]] = []
        for discovered in result.collections:
            if not _collection_matches_selector(
                discovered.entry,
                discovered.collection.key,
                set(args.collection),
            ):
                continue
            for source in discovered.runtime.library_sources():
                if args.source and source.key not in set(args.source):
                    continue
                tasks.append((source.key, discovered.runtime, source))
        if not tasks:
            if explicit_scope:
                parser.error("the selected scope contains no buildable sources")
            logger.info("zimantic: no sources to index")
            if discovery_failed:
                sys.exit(1)
            return
        _build_tasks(tasks, cfg, args, parser, paths=paths)
        if discovery_failed:
            sys.exit(1)


def _run_positional_zim_build(
    cfg: dict,
    args,
    parser: argparse.ArgumentParser,
) -> None:
    from .indexing.locator import IndexLocator
    from .indexing.pipeline import _AllFilesProgress, build_index

    zims = _collect_zims([Path(path) for path in args.paths], ".")
    if args.list:
        from .providers.config import ConfigValidationError

        try:
            for zim in zims:
                _most_specific_zim_entry(zim, cfg.get("collections") or ())
        except ConfigValidationError as error:
            sys.exit(f"zimantic: {error}")
        print(f"would index {len(zims)} ZIM file(s)")
        for zim in zims:
            print(f"  {zim}")
        return
    if not zims:
        logger.info("zimantic: no sources to index")
        return

    workers = args.workers
    if workers is None:
        workers = cfg.get("build_workers", 1)
    if workers < 1:
        parser.error("--workers must be at least 1")
    zim_sizes = {zim: zim.stat().st_size for zim in zims}
    downloading = _find_downloading_sizes(zim_sizes) if args.wait_stable else set()
    if downloading:
        _wait_for_stable_sizes(downloading)
        zim_sizes = {zim: zim.stat().st_size for zim in zims}
    zims = _select_zim_paths(zims, zim_sizes)

    embedder, embedding_pipeline = _make_build_embedding_context(cfg, args, parser)
    build_cfg = _build_config(cfg, args.fast)
    build_cfg["embedding_pipeline"] = embedding_pipeline
    build_cfg["interrupt_event"] = _BUILD_STOP_EVENT or threading.Event()
    concurrent_sources = (
        1 if workers == 1 or len(zims) == 1 else min(workers, len(zims))
    )
    build_cfg["_stage_split"] = concurrent_sources == 1
    build_cfg["_concurrent_sources"] = concurrent_sources
    locator = IndexLocator(cfg["index_dir"])
    failures: list[tuple[Path, BaseException]] = []
    interrupted = False
    overall = _AllFilesProgress(len(zims))

    def run_build(index: int, zim: Path):
        task_interrupted = False
        try:
            producer = _build_producer_for_zim(zim, cfg, fast=args.fast)
            return build_index(
                producer,
                locator,
                embedder,
                {
                    **build_cfg,
                    "progress_callback": lambda position, total: overall.advance(
                        index, position, total
                    ),
                },
                fast=args.fast,
                force=args.force,
            )
        except KeyboardInterrupt:
            task_interrupted = True
            raise
        finally:
            stop_event = build_cfg["interrupt_event"]
            if not task_interrupted and (stop_event is None or not stop_event.is_set()):
                overall.finish(index)

    try:
        if workers == 1 or len(zims) == 1:
            for index, zim in enumerate(zims):
                try:
                    _log_build_report(run_build(index, zim))
                except Exception as error:
                    logger.error(
                        "zimantic: failed to index %s: %s",
                        zim.name,
                        _sanitize_public_text(str(error), cfg),
                    )
                    logger.debug("build error for %s: %s", zim, error)
                    failures.append((zim, error))
        else:
            pool = ThreadPoolExecutor(
                max_workers=min(workers, len(zims)),
                thread_name_prefix="zimantic-build",
            )
            try:
                futures = {
                    pool.submit(run_build, index, zim): zim
                    for index, zim in enumerate(zims)
                }
                for future in as_completed(futures):
                    zim = futures[future]
                    try:
                        _log_build_report(future.result())
                    except Exception as error:
                        logger.error(
                            "zimantic: failed to index %s: %s",
                            zim.name,
                            _sanitize_public_text(str(error), cfg),
                        )
                        logger.debug("build error for %s: %s", zim, error)
                        failures.append((zim, error))
            except KeyboardInterrupt:
                interrupted = True
                build_cfg["interrupt_event"].set()
                logger.warning("zimantic: cancelling pending builds")
                raise
            finally:
                pool.shutdown(wait=not interrupted, cancel_futures=True)
    except KeyboardInterrupt:
        interrupted = True
        build_cfg["interrupt_event"].set()
        raise
    finally:
        overall.close()
        if embedding_pipeline is not None:
            embedding_pipeline.close(wait=not interrupted)
    if failures:
        logger.error(
            "zimantic: %d source build(s) failed; see the errors above",
            len(failures),
        )
        sys.exit(1)


def _run_positional_build(cfg: dict, args, parser: argparse.ArgumentParser) -> None:
    from copy import copy

    paths = list(dict.fromkeys(Path(path).expanduser().resolve() for path in args.paths))
    for path in paths:
        if not path.is_file() and not path.is_dir():
            parser.error(f"path not found: {path}")
    requests: list[tuple[str, Path]] = []
    for path in paths:
        if path.is_file() and path.suffix.casefold() == ".zim":
            kinds = ["zim"]
        else:
            matches = _matching_path_entries(path, cfg.get("collections") or ())
            kinds = list(dict.fromkeys(entry.kind for entry in matches))
            if not kinds:
                try:
                    kinds = _infer_path_kinds(path)
                except (OSError, RuntimeError) as error:
                    parser.error(f"cannot inspect {path}: {error}")
        requests.extend((kind, path) for kind in kinds)
    roots = [
        (kind, path.parent if kind == "files" and path.is_file() else path)
        for kind, path in requests
    ]
    try:
        appended = _persist_unmatched_paths(cfg, roots, preview=args.list)
    except (RuntimeError, OSError, ValueError) as error:
        sys.exit(f"zimantic: could not persist the ad-hoc collection: {error}")
    if args.list and appended:
        print("would add these collections:")
        for item in appended:
            print(f"  {item.kind}:{item.name} -> {item.saved}")
    elif not args.list:
        _warn_about_appended_collections(appended, cfg)
    zim_paths = [path for kind, path in requests if kind == "zim"]
    failed = False
    if zim_paths:
        zim_args = copy(args)
        zim_args.paths = zim_paths
        try:
            _run_positional_zim_build(cfg, zim_args, parser)
        except SystemExit as error:
            if error.code != 1:
                raise
            failed = True
    entries = []
    for kind, path in requests:
        if kind == "zim":
            continue
        for entry in _matching_path_entries(path, cfg.get("collections") or ()):
            if entry.kind == kind and entry not in entries:
                entries.append(entry)
    if entries:
        try:
            _run_configured_build(cfg, args, parser, entries=entries, paths=paths)
        except SystemExit as error:
            if error.code != 1:
                raise
            failed = True
    if failed:
        sys.exit(1)


def _run_build(args, cfg: dict, parser: argparse.ArgumentParser) -> None:
    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is not None:
        signal.signal(sigterm, _graceful_interrupt)

    if args.fast and getattr(args, "device", None):
        logger.warning(
            "zimantic: --device is ignored with --fast (no embeddings are computed)"
        )
    if args.paths and (args.provider or args.collection or args.source):
        parser.error(
            "positional paths cannot be combined with --provider, "
            "--collection, or --source"
        )
    global _BUILD_STOP_EVENT
    previous_stop_event = _BUILD_STOP_EVENT
    _BUILD_STOP_EVENT = threading.Event()
    try:
        try:
            if args.paths:
                _run_positional_build(cfg, args, parser)
            else:
                _run_configured_build(cfg, args, parser)
        except Exception as error:
            from .providers.config import ConfigValidationError

            if isinstance(error, ConfigValidationError):
                parser.error(str(error))
            raise
    finally:
        _BUILD_STOP_EVENT = previous_stop_event


def _sanitize_public_text(text: Any, cfg: dict) -> Any:
    """Remove configured private locators from user-visible diagnostics."""
    from .diagnostics import private_locators, sanitize_public_text

    return sanitize_public_text(
        text,
        private_locators(cfg["runtime_config"]),
    )


def _public_collection_record(discovered, cfg: dict) -> dict[str, Any]:
    record = discovered.runtime.snapshot.to_dict()
    record["error"] = _sanitize_public_text(record.get("error"), cfg)
    return record


def _run_sources(args, cfg: dict) -> None:
    import json

    from .discovery import discover_collections
    from .providers.config import ConfigValidationError

    providers = getattr(args, "provider", [])
    collection_selectors = getattr(args, "collection", [])
    source_selectors = getattr(args, "source", [])
    try:
        entries = _selected_configured_entries(
            cfg,
            provider_selectors=providers,
            collection_selectors=collection_selectors,
            source_selectors=source_selectors,
        )
    except ConfigValidationError as error:
        sys.exit(f"zimantic: {error}")
    result = discover_collections(
        cfg, entries=entries, wait=True, persist_catalog=False
    )
    try:
        _log_discovery_diagnostics(result, cfg)
        collections: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        for discovered in result.collections:
            if not _collection_matches_selector(
                discovered.entry,
                discovered.collection.key,
                set(collection_selectors),
            ):
                continue
            collections.append(_public_collection_record(discovered, cfg))
            for source in discovered.runtime.library_sources():
                if source_selectors and source.key not in set(source_selectors):
                    continue
                sources.append(source.to_dict(include_metadata=True))
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {"collections": collections, "sources": sources},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return
        for record in collections:
            state = record.get("discovery_state") or "unknown"
            error = record.get("error")
            suffix = f"; error: {error}" if error else ""
            print(
                f"{record['key']}  {record.get('label') or record.get('title') or ''} "
                f"[{record.get('provider')}, {state}, "
                f"{record.get('source_count', 0)} source(s)]{suffix}"
            )
            members = [
                source
                for source in sources
                if (source.get("collection") or {}).get("key") == record["key"]
            ]
            for source in members:
                marker = "" if source.get("searchable") else " (not searchable)"
                print(f"  {source['key']}  {source.get('title') or ''}{marker}")
        workers = cfg.get("derived_text_workers")
        if isinstance(workers, dict):
            bound = workers.get("bound")
            detail = f" ({bound} bound)" if bound else ""
            print(
                f"derived text workers: {workers.get('configured')} -> "
                f"{workers.get('resolved')}{detail}"
            )
    finally:
        result.close()


def _run_diagnose(args, parser: argparse.ArgumentParser) -> None:
    import json
    import math

    from .diagnose import DEFAULT_QUERIES, diagnose_archive
    from .open_scope import OpenScopeError, expand_open_paths

    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")
    if args.limit < 1:
        parser.error("--limit must be positive")
    queries = args.query if args.query is not None else DEFAULT_QUERIES
    if any(not query.strip() for query in queries):
        parser.error("--query must not be empty")
    try:
        paths = expand_open_paths(args.paths, max_sources=args.max_sources)
    except OpenScopeError as error:
        parser.error(str(error))
    reports = []
    for path in paths:
        logger.info("zimantic: checking %s", path.name)
        report = diagnose_archive(
            path, queries=queries, checksum=args.checksum,
            timeout=args.timeout, count=args.limit,
        )
        reports.append(report)
        if not args.json:
            if report["status"] == "ok":
                probes = report.get("searches", [])
                search = (
                    f"{len(probes)} search probe(s), {sum(item['hits'] for item in probes)} hit(s)"
                    if report.get("has_fulltext_index") else "no native full-text index"
                )
                checksum = "checksum verified" if report["checksum_checked"] else "checksum not checked"
                print(f"OK {path.name}: {search}; {checksum}", flush=True)
            else:
                print(f"{report['status'].upper()} {path.name}: {report['error']}", flush=True)
                if report.get("diagnostics"):
                    print(report["diagnostics"].rstrip(), file=sys.stderr)
    if args.json:
        print(json.dumps({"archives": reports}, indent=2, ensure_ascii=False))
    if any(report["status"] != "ok" for report in reports):
        raise SystemExit(1)


def _run_catalog(args, cfg: dict, parser: argparse.ArgumentParser) -> None:
    import json

    from .discovery import discover_collections
    from .providers.base import IndexedRuntime
    from .providers.config import ConfigValidationError
    from .web.listing import filter_collections, filter_sources

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.offset < 0:
        parser.error("--offset must not be negative")
    try:
        entries = _selected_configured_entries(
            cfg,
            provider_selectors=args.provider,
            collection_selectors=args.collection,
            source_selectors=args.source,
        )
    except ConfigValidationError as error:
        parser.error(str(error))
    with discover_collections(
        cfg, entries=entries, wait=True, persist_catalog=False
    ) as result:
        _log_discovery_diagnostics(result, cfg)
        collections: list[dict[str, Any]] = []
        sources: dict[str, dict[str, Any]] = {}
        owners: dict[str, tuple[Any, Any]] = {}
        collection_selectors = set(args.collection)
        source_selectors = set(args.source)
        for discovered in result.collections:
            if not _collection_matches_selector(
                discovered.entry, discovered.collection.key, collection_selectors
            ):
                continue
            collections.append(_public_collection_record(discovered, cfg))
            for source in discovered.runtime.library_sources():
                if source_selectors and source.key not in source_selectors:
                    continue
                sources[source.key] = source.to_dict(include_metadata=True)
                owners[source.key] = (discovered.runtime, source)
        matching = filter_sources(
            sources.values(), media_type=args.media_type, query=args.query,
            collections=collections,
        )
        collections = filter_collections(
            collections, sources.values(), media_type=args.media_type, query=args.query,
        )
        end = None if args.limit is None else args.offset + args.limit
        page = matching[args.offset:end]
        cwd = Path.cwd()
        for record in page:
            runtime, source = owners[record["key"]]
            paths = runtime.source_paths(source) if isinstance(runtime, IndexedRuntime) else {}
            record["paths"] = {
                name: (
                    _config_relative_path(path.absolute(), cwd)
                    if args.relative else str(path.absolute())
                )
                for name, path in paths.items()
            }
        if args.json:
            print(json.dumps(
                {
                    "collections": collections,
                    "sources": page,
                    "total": len(matching),
                    "offset": args.offset,
                    "limit": args.limit,
                },
                indent=2, ensure_ascii=False,
            ))
            return
        if not page:
            print("No matching library entries.")
        if args.limit is not None or args.offset:
            print(f"{len(page)} of {len(matching)} matching entries (offset {args.offset})")
        for record in page:
            marker = "" if record["searchable"] else ", not searchable"
            print(f"{record['key']}  {record.get('title') or ''} [{record['provider']}{marker}]")
            for field in (
                "description", "long_description", "creator", "publisher",
                "language", "date", "tags", "category", "license", "size",
                "media_types", "item_count", "indexed_count", "index_freshness",
                "homepage",
            ):
                value = record.get(field)
                if value is None or value == "" or value == []:
                    continue
                if isinstance(value, list):
                    value = ", ".join(value)
                print(f"  {field}: {value}")
            for name, path in record["paths"].items():
                print(f"  {name}: {path}")
        for collection in collections:
            if collection.get("error"):
                print(f"{collection['key']}: {collection['error']}", file=sys.stderr)


def _print_discovery_tree(
    result,
    *,
    collection_selectors: set[str] | None = None,
    source_selectors: set[str] | None = None,
) -> None:
    collections = 0
    source_count = 0
    for discovered in result.collections:
        if not _collection_matches_selector(
            discovered.entry,
            discovered.collection.key,
            collection_selectors or set(),
        ):
            continue
        collections += 1
        snapshot = discovered.runtime.snapshot
        print(
            f"{discovered.collection.key}  "
            f"{discovered.collection.label} "
            f"[{discovered.collection.provider}, {snapshot.discovery_state}, "
            f"{snapshot.source_count} source(s)]"
        )
        for source in discovered.runtime.library_sources():
            if source_selectors and source.key not in source_selectors:
                continue
            source_count += 1
            marker = "" if source.searchable else " (not searchable)"
            print(f"  {source.key}  {source.title or ''}{marker}")
    if not collections:
        print("no collections matched")
    else:
        print(f"{collections} collection(s), {source_count} source(s)")


def _prune_snapshot(cfg: dict):
    """Discover only providers that can own persistent local indexes."""
    from .discovery import discover_collections

    entries = [
        entry
        for entry in cfg.get("collections") or ()
        if entry.kind in {"zim", "files", "kolibri"}
    ]
    return discover_collections(
        cfg, entries=entries, wait=True, persist_catalog=False
    )


def _print_prune_inventory(
    inventory, *, index_dir: Path, deleted: bool, reclaimed: int | None = None
) -> None:
    from .prune import PruneInventory

    assert isinstance(inventory, PruneInventory)
    if inventory.problems:
        for problem in inventory.problems:
            logger.warning("zimantic: prune: %s", problem)
    if not inventory.candidates:
        print("no orphaned Zimantic indexes found")
    else:
        verb = "deleted" if deleted else "would delete"
        for candidate in inventory.candidates:
            record = candidate.to_dict(index_dir=index_dir)
            suffix = " (pinned; skipped)" if candidate.pinned else ""
            print(
                f"{verb} {record['provider']}/{record['collection']}/"
                f"{record['source']}  {candidate.bytes} bytes{suffix}"
            )
    total = inventory.reclaimed_bytes if reclaimed is None else reclaimed
    print(f"{'reclaimed' if deleted else 'reclaimable'} bytes: {total}")


def _run_prune(args, cfg: dict, parser: argparse.ArgumentParser) -> None:
    from .config_writer import config_lock_path
    from .locking import BuildLock, BuildLockError, FileLease, build_lock_path
    from .prune import collect_inventory, delete_candidates

    if getattr(args, "dry_run", False) and getattr(args, "yes", False):
        parser.error("--dry-run and --yes are mutually exclusive")
    delete = bool(getattr(args, "yes", False))
    index_dir = Path(cfg["index_dir"])

    runtime_config = cfg.get("runtime_config")
    config_path = getattr(runtime_config, "config_path", None)
    if not cfg.get("config_present", False) or config_path is None:
        sys.exit(
            "zimantic: prune: the active config is missing or unreadable. "
            "Refusing to treat that as an empty configuration; no indexes were "
            "deleted."
        )

    if not delete:
        discovery = _prune_snapshot(cfg)
        try:
            _log_discovery_diagnostics(discovery, cfg)
            inventory = collect_inventory(index_dir, cfg, discovery)
        finally:
            discovery.close()
        _print_prune_inventory(inventory, index_dir=index_dir, deleted=False)
        return

    build_lock = BuildLock(build_lock_path(index_dir))
    try:
        try:
            build_lock.acquire()
        except BuildLockError as error:
            sys.exit(str(error))
        config_lock = FileLease(
            config_lock_path(config_path), description="config lock"
        )
        try:
            config_lock.acquire(shared=False)
        except BuildLockError as error:
            sys.exit(str(error))
        discovery = None
        try:
            # Re-read the config and revalidate discovery while both locks are
            # held; a dry-run inventory is never reused for deletion.
            from .providers.config import ConfigValidationError
            from .settings import load_config

            try:
                current = load_config(
                    config_path,
                    warn=lambda message: logger.warning("%s", message),
                )
            except ConfigValidationError as error:
                sys.exit(f"zimantic: prune: {error}; no indexes were deleted")
            if not current.get("config_present", False):
                sys.exit(
                    "zimantic: prune: the active config disappeared or became "
                    "unreadable; no indexes were deleted."
                )
            if Path(current["index_dir"]) != index_dir:
                sys.exit(
                    "zimantic: prune: index_dir changed while waiting for the "
                    "locks; no indexes were deleted. Re-run prune."
                )
            discovery = _prune_snapshot(current)
            _log_discovery_diagnostics(discovery, current)
            inventory = collect_inventory(index_dir, current, discovery)
            removed, reclaimed = delete_candidates(inventory)
            _print_prune_inventory(
                inventory, index_dir=index_dir, deleted=True, reclaimed=reclaimed
            )
            if removed:
                logger.info(
                    "zimantic: prune removed %d source index director%s",
                    len(removed),
                    "y" if len(removed) == 1 else "ies",
                )
        finally:
            if discovery is not None:
                discovery.close()
            config_lock.release()
    finally:
        build_lock.release()


def _launch_browser(url: str) -> None:
    """Open ``url`` on a daemon thread; a missing desktop browser is harmless."""
    from .diagnostics import public_cause

    try:
        opened = webbrowser.open(url)
        if opened is False:
            logger.warning(
                "zimantic: no browser was available; open %s manually", url
            )
    except Exception as error:
        logger.warning(
            "zimantic: could not open a browser (%s); open %s manually",
            public_cause(error),
            url,
        )


def _run_open(args, cfg: dict, parser: argparse.ArgumentParser) -> None:
    """Serve only the ZIM files named on the command line, in memory."""
    from dataclasses import replace

    from .open_scope import OpenScopeError, build_open_scope, expand_open_paths
    from .search import Search
    from .web.app import bind_local_socket, run_server

    if args.max_sources < 1:
        parser.error("--max-sources must be a positive integer")
    if not (0 <= args.port <= 65535):
        parser.error("--port must be between 0 and 65535")
    if args.fast and getattr(args, "device", None):
        logger.warning(
            "zimantic: --device is ignored with --fast (no embeddings are computed)"
        )

    try:
        paths = expand_open_paths(args.paths, max_sources=args.max_sources)
        # An open process is a direct loopback server, not a reverse-proxied
        # deployment. Generated links therefore start at the server root.
        open_cfg = dict(cfg["runtime_config"].settings)
        open_cfg["web_base_path"] = ""
        open_config = replace(cfg["runtime_config"], settings=open_cfg)
        scope = build_open_scope(paths, open_cfg)
    except OpenScopeError as error:
        parser.error(str(error))

    print(
        f"zimantic: opening {len(scope.paths)} ZIM file"
        f"{'' if len(scope.paths) == 1 else 's'}",
        flush=True,
    )

    # Model loading must not contact the network, write caches, or emit
    # telemetry from an ephemeral process.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

    search = Search(open_config, semantic=not args.fast, scope=scope)
    try:
        if not args.fast:
            def _make_embedder():
                from .embed import Embedder

                return Embedder(
                    open_cfg["model_dir"],
                    threads=open_cfg.get("onnx_query_threads"),
                    device=getattr(args, "device", None) or open_cfg.get("embed_device"),
                    onnx_warnings=bool(getattr(args, "verbose", 0)),
                )

            search.start_embedder(_make_embedder)
        if args.fast:
            logger.info(
                "zimantic: fast mode: semantic search disabled; using title and "
                "ZIM full-text (lexical) search"
            )

        sock = bind_local_socket("127.0.0.1", args.port)

        def _listening(port: int) -> None:
            base_url = f"http://127.0.0.1:{port}"
            initial_url = (
                f"{base_url}/content/{quote_path(scope.keys[0], safe='')}/"
            )
            print(f"zimantic: server listening at {base_url}/", flush=True)
            print(f"zimantic: opening {initial_url}", flush=True)
            if args.no_browser:
                return
            threading.Thread(
                target=_launch_browser,
                args=(initial_url,),
                name="zimantic-browser",
                daemon=True,
            ).start()

        run_server(
            search,
            host="127.0.0.1",
            port=args.port,
            debug=args.debug,
            pid_path=None,
            prebound_socket=sock,
            on_listening=_listening,
            install_sighup=False,
            announce=False,
        )
    finally:
        try:
            search.close()
        except Exception:
            pass


def _run_serve(args, cfg: dict) -> None:
    from .search import Search
    from .web.app import serve

    if args.fast and getattr(args, "device", None):
        logger.warning(
            "zimantic: --device is ignored with --fast (no embeddings are computed)"
        )

    search = Search(cfg["runtime_config"], semantic=not args.fast)
    if not search.source_keys():
        configured = ", ".join(
            f"{kind} ({sum(1 for entry in cfg.get('collections') or () if entry.kind == kind)})"
            for kind in _provider_kinds()
            if any(entry.kind == kind for entry in cfg.get("collections") or ())
        ) or "none"
        logger.warning(
            "zimantic: no searchable sources are available; configured "
            "providers: %s. Add or fix a [[collections]] entry and run "
            "`zimantic build --fast`",
            configured,
        )
    if not args.fast:
        # Load the model off the critical path: the server answers title and
        # full-text searches immediately and gains meaning search once the
        # ONNX model and vocabulary finish loading in the background.
        def _make_embedder():
            from .embed import Embedder

            return Embedder(
                cfg["model_dir"],
                threads=cfg.get("onnx_query_threads"),
                device=getattr(args, "device", None) or cfg.get("embed_device"),
                onnx_warnings=bool(getattr(args, "verbose", 0)),
            )

        search.start_embedder(_make_embedder)

    if args.fast:
        logger.info(
            "zimantic: fast mode: semantic search disabled; using title and "
            "ZIM full-text (lexical) search"
        )
    serve(search, cfg["port"], debug=args.debug)


def _run_model(args, cfg: dict) -> None:
    from .model_files import (
        DEFAULT_MODEL,
        DEFAULT_PRECISION,
        MODEL_SPECS,
        ModelDownloadError,
        default_output_dir,
        download_model,
    )

    if args.list:
        for name, spec in sorted(MODEL_SPECS.items()):
            marker = " (default)" if name == DEFAULT_MODEL else ""
            print(f"{name}{marker}: {', '.join(spec.precisions())}")
        print(f"default precision: {DEFAULT_PRECISION} (CPU); use fp16 with a GPU")
        return

    precisions = args.precision or [DEFAULT_PRECISION]
    model = args.model or DEFAULT_MODEL
    configured_dir = Path(cfg["model_dir"])
    output_dir = (
        args.dir
        if args.dir is not None
        else default_output_dir(model, configured_dir)
    )
    try:
        downloads = download_model(
            model, precisions, output_dir, force=args.force
        )
    except ModelDownloadError as error:
        sys.exit(f"zimantic: {error}")

    for precision, directory in downloads:
        print(f"zimantic: {model} {precision} -> {directory}")
    folders = [directory for _precision, directory in downloads]
    if len(folders) == 1 and folders[0] != configured_dir:
        print(f'zimantic: set model_dir = "{folders[0]}" to use this export')
    elif len(folders) > 1:
        print(
            "zimantic: point model_dir at one of these folders; "
            "use a separate index_dir when switching models and rebuild"
        )


def _run_reload(pid_file: Path | None) -> None:
    from .paths import reload_pid_path

    pid_file = reload_pid_path(pid_file)
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        sys.exit(
            f"zimantic: no server PID in {pid_file}; "
            f"Is `zimantic serve` running from this folder (or pass --pid)?"
        )
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:
        sys.exit("zimantic: reload via signals is not supported on this platform")
    try:
        os.kill(pid, sighup)
    except ProcessLookupError:
        sys.exit(
            f"zimantic: no running process with PID {pid}; "
            f"is the PID file {pid_file} stale?"
        )
    except PermissionError:
        sys.exit(
            f"zimantic: permission denied signalling PID {pid}; "
            "run the reload command as the same user as the server"
        )
    logger.info("zimantic: reload signal (SIGHUP) sent to PID %s", pid)


def _run_build_locked(args, cfg: dict, parser: argparse.ArgumentParser) -> None:
    """Run a build under the index lock shared by every entry point."""
    from .locking import (
        BUILD_BUSY_EXIT,
        BuildLock,
        BuildLockError,
        build_lock_path,
    )

    lock = BuildLock(build_lock_path(cfg["index_dir"]))
    try:
        try:
            acquired = lock.try_acquire()
            if not acquired:
                if args.no_wait_lock:
                    logger.warning(
                        "zimantic: another build is already running (lock %s); "
                        "skipping this build",
                        lock.path,
                    )
                    sys.exit(BUILD_BUSY_EXIT)
                logger.warning(
                    "zimantic: another build is already running; waiting for %s",
                    lock.path,
                )
                acquired = lock.acquire(timeout=args.lock_timeout)
                if not acquired:
                    logger.warning(
                        "zimantic: gave up waiting for the build lock after %s "
                        "seconds; another build is still running (lock %s)",
                        args.lock_timeout,
                        lock.path,
                    )
                    sys.exit(BUILD_BUSY_EXIT)
        except BuildLockError as error:
            sys.exit(str(error))
        _run_build(args, cfg, parser)
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> None:
    parser = _make_parser()

    args = parser.parse_args(argv)
    log_level = _get_log_level(args.command, args.verbose)
    _configure_logging(log_level)

    # `reload` only needs the PID file, and `model --list` only needs the registry.
    if args.command in {"build", "serve", "open", "sources", "catalog", "prune"} or (
        args.command == "model" and not args.list
    ):
        from .providers.config import ConfigValidationError
        from .settings import load_config

        try:
            cfg = load_config()
        except ConfigValidationError as error:
            sys.exit(f"zimantic: {error}")
    else:
        cfg = {}

    try:
        if args.command == "build":
            if args.lock_timeout is not None and args.lock_timeout < 0:
                parser.error("--lock-timeout must not be negative")
            if args.list:
                # --list is a read-only preview: no build lock or index write.
                _run_build(args, cfg, parser)
            else:
                _run_build_locked(args, cfg, parser)
        elif args.command == "serve":
            _run_serve(args, cfg)
        elif args.command == "open":
            _run_open(args, cfg, parser)
        elif args.command == "sources":
            _run_sources(args, cfg)
        elif args.command == "catalog":
            _run_catalog(args, cfg, parser)
        elif args.command == "diagnose":
            _run_diagnose(args, parser)
        elif args.command == "prune":
            _run_prune(args, cfg, parser)
        elif args.command == "model":
            _run_model(args, cfg)
        elif args.command == "reload":
            _run_reload(args.pid)
    except KeyboardInterrupt:
        if args.command == "build":
            logger.warning(
                "zimantic: build interrupted; incomplete work is resumable"
            )
            raise SystemExit(130)
        raise


if __name__ == "__main__":
    main()
