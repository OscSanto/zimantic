import argparse
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)


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

    @property
    def fingerprint(self) -> str:
        """The loaded model's fingerprint, or empty when it was never needed."""
        return getattr(self._value, "fingerprint", "") if self._value is not None else ""


def _graceful_interrupt(_signum: int, _frame) -> None:
    """Turn SIGTERM (a systemd stop) into KeyboardInterrupt.

    Ctrl-C already interrupts the build; systemd stops a unit with SIGTERM, which
    would otherwise kill the process without running its ``finally`` blocks. With
    this handler, a stopped build closes its database and removes temp files
    exactly like an interrupted batch, and stays resumable from the last
    committed batch.
    """
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
        help="increase logging verbosity (-v for info, -vv for debug)",
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
            "directml/dml, openvino or coreml; overrides embed_device in "
            "config.toml"
        ),
    )


def _get_log_level(command: str, verbosity: int) -> int:
    if command in {"serve", "model"} and verbosity == 0 and sys.stderr.isatty():
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


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zimantic")
    _add_verbosity_argument(parser, default=0)

    # subcommands: build (files/folders, default zim_dir), serve, reload
    commands = parser.add_subparsers(dest="command", required=True)

    serve_cmd = commands.add_parser("serve", help="run the search page and JSON API")
    _add_verbosity_argument(serve_cmd)
    serve_cmd.add_argument(
        "--fast",
        action="store_true",
        help=(
            "start immediately without the model or FAISS vectors "
            "(title and ZIM full-text results only)"
        ),
    )
    serve_cmd.add_argument(
        "--debug",
        "--dev",
        dest="debug",
        action="store_true",
        help="include ranking scores in results; in this mode SIGHUP restarts",
    )
    _add_device_argument(serve_cmd)

    build = commands.add_parser("build", help="index ZIM files")
    _add_verbosity_argument(build)
    build.add_argument(
        "paths",
        nargs="*",
        type=Path,
        metavar="PATH",
        help=(
            "ZIM files or folders containing *.zim; a folder means every .zim "
            "under it (recursively). Default: zim_dir"
        ),
    )
    build.add_argument(
        "--fast",
        action="store_true",
        help="title + ZIM full-text only: skip article bodies, the model and FAISS (much faster)",
    )
    build.add_argument(
        "--force",
        action="store_true",
        help="rebuild the selected ZIM indexes even if they are already complete",
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
        help="build up to N independent ZIMs concurrently (default: config build_workers)",
    )
    build.add_argument(
        "--no-wait-lock",
        action="store_true",
        help=(
            "exit with status 75 instead of waiting when another build already "
            "holds the index lock (for systemd.path and timer triggers)"
        ),
    )
    build.add_argument(
        "--lock-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="stop waiting for the index lock after SECONDS (default: wait forever)",
    )

    reload_cmd = commands.add_parser(
        "reload",
        help=(
            "ask a running serve process to rescan indexes and ZIMs (sends SIGHUP; "
            "suitable for systemd.path or cron)"
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
        help="download ONNX model files and tokenizer vocabularies",
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
        default="multilingual-e5-small",
        metavar="NAME",
        help=(
            "model to download: multilingual-e5-small (default), "
            "multilingual-e5-base, multilingual-e5-large, e5-small-v2, "
            "e5-base-v2 or e5-large-v2"
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
        help="re-download and replace existing files, even mismatched ones",
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


def _configured_zim_roots(cfg: dict) -> list[Path]:
    roots = [
        Path(entry.path)
        for entry in cfg.get("collections") or ()
        if getattr(entry, "kind", None) == "zim"
    ]
    if not roots and cfg.get("zim_dir"):
        roots = [Path(cfg["zim_dir"])]
    return roots


def _collect_zims_from_roots(
    paths: list[Path], roots: list[Path]
) -> list[Path]:
    selected: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for zim in _collect_zims([root], root):
            resolved = zim.resolve()
            if resolved not in seen:
                seen.add(resolved)
                selected.append(zim)
    if paths:
        selected = _collect_zims(paths, roots[0] if roots else ".")
    return selected


def _slug_collection_name(path: Path) -> str:
    import re

    stem = path.stem if path.is_file() else path.name
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or "zim"
    if not text[0].isalnum():
        text = "zim-" + text
    text = text[:64]
    return "open-" + text if text == "open" else text


def _append_collection_entry(config_path: Path, name: str, path: Path) -> None:
    """Atomically append one ZIM collection entry, preserving a symlink target."""
    import json
    import os

    from .locking import BuildLockError, FileLease

    target = Path(config_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLease(target.with_name(target.name + ".lock"))
    lock.acquire(shared=False)
    try:
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        entry = (
            ("\n" if existing and not existing.endswith("\n") else "")
            + "[[collections]]\n"
            + 'type = "zim"\n'
            + f"name = {json.dumps(name)}\n"
            + f"path = {json.dumps(str(path))}\n"
        )
        temporary = target.with_name("." + target.name + ".tmp")
        temporary.write_text(existing + entry, encoding="utf-8")
        try:
            fd = os.open(temporary, os.O_RDONLY)
        except OSError:
            fd = None
        if fd is not None:
            try:
                os.fsync(fd)
            except OSError:
                pass
            finally:
                os.close(fd)
        os.replace(temporary, target)
        try:
            directory = os.open(target.parent, os.O_RDONLY)
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            except OSError:
                pass
            finally:
                os.close(directory)
    except (OSError, BuildLockError) as error:
        raise RuntimeError(f"could not update {target}: {error}") from error
    finally:
        lock.release()


def _persist_unmatched_zim_paths(cfg: dict, paths: list[Path]) -> list[Path]:
    """Append unmatched positional roots as discoverable ZIM collections."""
    runtime_config = cfg.get("runtime_config")
    config_path = getattr(runtime_config, "config_path", None)
    if config_path is None or not paths:
        return []
    existing = [Path(entry.path).resolve() for entry in cfg.get("collections") or ()
                if getattr(entry, "kind", None) == "zim"]

    def covered(candidate: Path) -> bool:
        resolved = candidate.resolve()
        for root in existing:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
            if resolved == root:
                return True
        return False

    appended: list[Path] = []
    used_names = {
        entry.name for entry in cfg.get("collections") or ()
        if getattr(entry, "kind", None) == "zim"
    }
    for raw in paths:
        candidate = Path(raw).expanduser()
        if covered(candidate):
            continue
        base = _slug_collection_name(candidate)
        name = base
        suffix = 2
        while name in used_names:
            name = f"{base}-{suffix}"
            suffix += 1
        _append_collection_entry(config_path, name, candidate.resolve())
        used_names.add(name)
        appended.append(candidate)
    return appended


def _run_build(args, cfg: dict, parser: argparse.ArgumentParser) -> None:
    from tqdm import tqdm

    from .build import build as build_zim
    from .content.reader import (
        DEFAULT_MAX_ENTRY_BYTES,
        DEFAULT_MAX_HTML_BYTES,
        DEFAULT_PREVIEW_CHARS,
    )

    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is not None:
        signal.signal(sigterm, _graceful_interrupt)

    if args.fast and getattr(args, "device", None):
        logger.warning(
            "zimantic: --device is ignored with --fast (no embeddings are computed)"
        )

    zims = _collect_zims_from_roots(
        [Path(path) for path in args.paths], _configured_zim_roots(cfg)
    )
    appended = _persist_unmatched_zim_paths(
        cfg, [Path(path) for path in args.paths]
    )
    if appended:
        logger.warning(
            "zimantic: added %d ZIM collection entr%s to %s; restart a running "
            "serve process to load the changed config (SIGHUP reload is not enough)",
            len(appended),
            "y" if len(appended) == 1 else "ies",
            getattr(getattr(cfg.get("runtime_config"), "config_path", None), "__str__", lambda: "config"),
        )
    if not zims:
        logger.info("zimantic: no .zim files to index")
        return

    workers = args.workers
    if workers is None:
        workers = cfg.get("build_workers", 1)
    if workers < 1:
        parser.error("--workers must be at least 1")

    zim_sizes = {zim: zim.stat().st_size for zim in zims}
    # The stability poll exists for downloads and the systemd.path trigger; an
    # explicit CLI build of already-present files should start immediately.
    downloading = (
        _find_downloading_sizes(zim_sizes) if args.wait_stable else set()
    )
    if downloading:
        _wait_for_stable_sizes(downloading)
        # Selection prefers the largest copy of an ambiguous ZIM stem, so it
        # must see the finished sizes rather than the sizes observed before the
        # download completed.
        zim_sizes = {zim: zim.stat().st_size for zim in zims}
    zims = _select_zim_paths(zims, zim_sizes)
    if not zims:
        logger.info("zimantic: no .zim files to index")
        return

    embedder = None
    embedding_pipeline = None
    if not args.fast:
        from .build import EmbeddingPipeline
        from .embed import Embedder

        def _make_embedder():
            return Embedder(
                cfg["model_dir"],
                threads=cfg.get("embed_threads"),
                device=getattr(args, "device", None) or cfg.get("embed_device"),
            )

        embedder = _LazyEmbedder(_make_embedder)
        if workers > 1 and len(zims) > 1:
            embedding_workers = int(cfg.get("embedding_workers", 2))
            if embedding_workers < 1:
                parser.error("embedding_workers must be at least 1")
            embedding_pipeline = EmbeddingPipeline(
                embedder,
                workers=min(embedding_workers, workers),
                queue_size=max(2, workers * 2),
            )
    global_progress = None
    if len(zims) > 1 and sys.stderr.isatty():
        global_progress = tqdm(
            total=sum(zim_sizes[zim] for zim in zims),
            desc="all ZIMs",
            unit="B",
            unit_scale=True,
            file=sys.stderr,
        )
    failures: list[tuple[Path, BaseException]] = []

    def _collection_for(zim: Path):
        for entry in cfg.get("collections") or ():
            if getattr(entry, "kind", None) != "zim":
                continue
            root = Path(entry.path)
            if root.is_file():
                if root.resolve() == zim.resolve():
                    return entry
            else:
                try:
                    zim.resolve().relative_to(root.resolve())
                    return entry
                except ValueError:
                    continue
        return None

    def run_build(zim: Path) -> None:
        entry = _collection_for(zim)
        build_zim(
            zim,
            cfg["index_dir"],
            embedder,
            cfg["batch_size"],
            fast=args.fast,
            max_html_bytes=cfg.get("max_html_bytes", DEFAULT_MAX_HTML_BYTES),
            max_entry_bytes=cfg.get("max_entry_bytes", DEFAULT_MAX_ENTRY_BYTES),
            max_preview_chars=cfg.get("max_preview_chars", DEFAULT_PREVIEW_CHARS),
            force=args.force,
            embedding_pipeline=embedding_pipeline,
            collection_root=getattr(entry, "path", None),
            collection_name=getattr(entry, "name", "library"),
        )

    try:
        if workers == 1 or len(zims) == 1:
            for zim in zims:
                try:
                    run_build(zim)
                except Exception as error:
                    logger.error("zimantic: failed to index %s: %s", zim, error)
                    failures.append((zim, error))
                if global_progress is not None:
                    global_progress.update(zim_sizes[zim])
        else:
            pool = ThreadPoolExecutor(
                max_workers=min(workers, len(zims)),
                thread_name_prefix="zimantic-build",
            )
            try:
                futures = {pool.submit(run_build, zim): zim for zim in zims}
                for future in as_completed(futures):
                    zim = futures[future]
                    try:
                        future.result()
                    except Exception as error:
                        logger.error("zimantic: failed to index %s: %s", zim, error)
                        failures.append((zim, error))
                    if global_progress is not None:
                        global_progress.update(zim_sizes[zim])
            finally:
                # On Ctrl-C/SIGTERM drop builds that have not started yet
                # instead of running every queued ZIM to completion.
                pool.shutdown(wait=True, cancel_futures=True)
    finally:
        if embedding_pipeline is not None:
            embedding_pipeline.close()
        if global_progress is not None:
            global_progress.close()
    if failures:
        sys.exit(1)


def _run_serve(args, cfg: dict) -> None:
    from .search import Search
    from .web.app import serve

    if args.fast and getattr(args, "device", None):
        logger.warning(
            "zimantic: --device is ignored with --fast (no embeddings are computed)"
        )

    search = Search(cfg, semantic=not args.fast)
    if not search.source_keys():
        logger.warning(
            "zimantic: no local ZIM sources are available; "
            "add a .zim to zim_dir or run `zimantic build --fast`"
        )
    if not args.fast:
        # Load the model off the critical path: the server answers title and
        # full-text searches immediately and gains meaning search once the
        # ONNX model and vocabulary finish loading in the background.
        def _make_embedder():
            from .embed import Embedder

            return Embedder(
                cfg["model_dir"],
                threads=cfg.get("embed_threads"),
                device=getattr(args, "device", None) or cfg.get("embed_device"),
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
    configured_dir = Path(cfg["model_dir"])
    output_dir = (
        args.dir
        if args.dir is not None
        else default_output_dir(args.model, configured_dir)
    )
    try:
        downloads = download_model(
            args.model, precisions, output_dir, force=args.force
        )
    except ModelDownloadError as error:
        sys.exit(f"zimantic: {error}")

    for precision, directory in downloads:
        print(f"zimantic: {args.model} {precision} -> {directory}")
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


def main() -> None:
    parser = _make_parser()

    args = parser.parse_args()
    log_level = _get_log_level(args.command, args.verbose)
    _configure_logging(log_level)

    # `reload` only needs the PID file, and `model --list` only needs the registry.
    if args.command in {"build", "serve"} or (
        args.command == "model" and not args.list
    ):
        from .settings import load_config

        cfg = load_config()
    else:
        cfg = {}

    if args.command == "build":
        if args.lock_timeout is not None and args.lock_timeout < 0:
            parser.error("--lock-timeout must not be negative")
        _run_build_locked(args, cfg, parser)
    elif args.command == "serve":
        _run_serve(args, cfg)
    elif args.command == "model":
        _run_model(args, cfg)
    elif args.command == "reload":
        _run_reload(args.pid)


if __name__ == "__main__":
    main()
