import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import os
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


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


def _get_log_level(command: str, verbosity: int) -> int:
    if command == "serve" and verbosity == 0 and sys.stderr.isatty():
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


def _wait_for_stable_size(path: Path) -> None:
    """Wait for one ZIM to finish being written before indexing it."""
    _wait_for_stable_sizes({path})


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
        help="include ranking scores and explanations; --dev is an alias and SIGHUP restarts",
    )

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
    build.add_argument(
        "-j",
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="build up to N independent ZIMs concurrently (default: config build_workers)",
    )

    reload_cmd = commands.add_parser(
        "reload",
        help=(
            "ask a running serve process to rescan index_dir (sends SIGHUP; "
            "suitable for systemd.path or cron)"
        ),
    )
    _add_verbosity_argument(reload_cmd)
    reload_cmd.add_argument(
        "--pid",
        type=Path,
        default=Path("zimantic.pid"),
        help="file holding the server's PID (default: ./zimantic.pid)",
    )
    return parser


def _collect_zims(paths: list[Path], default_dir: str | Path) -> list[Path]:
    """Expand CLI files and folders into unique ZIM paths."""
    zims: list[Path] = []
    seen: set[Path] = set()
    for source in (paths or [Path(default_dir)]):
        if source.is_dir():
            matches = sorted(source.rglob("*.zim"))
        elif source.is_file():
            matches = [source]
        else:
            sys.exit(f"zimantic: path not found: {source}")
        for zim in matches:
            resolved = zim.resolve()
            if resolved not in seen:
                seen.add(resolved)
                zims.append(zim)
    return zims


def _run_build(args, cfg: dict, parser: argparse.ArgumentParser) -> None:
    from .build import build as build_zim
    from .content.reader import (
        DEFAULT_MAX_ENTRY_BYTES,
        DEFAULT_MAX_HTML_BYTES,
        DEFAULT_PREVIEW_CHARS,
    )
    from tqdm import tqdm

    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is not None:
        signal.signal(sigterm, _graceful_interrupt)

    zims = _collect_zims(args.paths, cfg["zim_dir"])
    if not zims:
        logger.info("zimantic: no .zim files to index")
        return

    workers = args.workers
    if workers is None:
        workers = cfg.get("build_workers", 1)
    if workers < 1:
        parser.error("--workers must be at least 1")

    zim_sizes = {zim: zim.stat().st_size for zim in zims}
    downloading = _find_downloading_sizes(zim_sizes)

    by_stem: dict[str, list[Path]] = {}
    for zim in zims:
        by_stem.setdefault(zim.stem, []).append(zim)
    accepted: list[Path] = []
    for stem, paths in sorted(by_stem.items()):
        sizes = {zim_sizes[path] for path in paths}
        if len(sizes) > 1:
            logger.warning(
                "zimantic: ignoring ZIM files with conflicting index identity %r: %s",
                stem,
                ", ".join(f"{path} ({zim_sizes[path]} bytes)" for path in paths),
            )
            continue
        if len(paths) > 1:
            logger.warning(
                "zimantic: ignoring duplicate ZIM files with index identity %r: %s",
                stem,
                ", ".join(str(path) for path in paths[1:]),
            )
        accepted.append(paths[0])
    zims = sorted(accepted, key=zim_sizes.__getitem__)
    if not zims:
        logger.info("zimantic: no non-conflicting .zim files to index")
        return

    if downloading:
        _wait_for_stable_sizes(downloading)

    embedder = None
    embedding_pipeline = None
    if not args.fast:
        from .build import EmbeddingPipeline
        from .embed import Embedder

        embedder = Embedder(
            cfg["model_dir"],
            threads=cfg.get("embed_threads"),
        )
        if workers > 1:
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
            total=sum(zim_sizes.values()),
            desc="all ZIMs",
            unit="B",
            unit_scale=True,
            file=sys.stderr,
        )
    failures: list[tuple[Path, BaseException]] = []

    def run_build(zim: Path) -> None:
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
            with ThreadPoolExecutor(
                max_workers=min(workers, len(zims)),
                thread_name_prefix="zimantic-build",
            ) as pool:
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
        if embedding_pipeline is not None:
            embedding_pipeline.close()
        if global_progress is not None:
            global_progress.close()
    if failures:
        sys.exit(1)


def _run_serve(args, cfg: dict) -> None:
    from .search import Search
    from .web.app import serve

    search = Search(cfg, semantic=not args.fast)
    if not search.local_names():
        logger.warning(
            "zimantic: no sources are indexed yet; "
            "run `zimantic build --fast` before serving searches"
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
            )

        search.start_embedder(_make_embedder)

    if args.fast:
        logger.info("zimantic: fast mode: title and ZIM full-text search only")
    serve(search, cfg["port"], debug=args.debug)


def _run_reload(pid_file: Path) -> None:
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


def main() -> None:
    parser = _make_parser()

    args = parser.parse_args()
    log_level = _get_log_level(args.command, args.verbose)
    _configure_logging(log_level)

    # `reload` only needs the PID file.
    if args.command in {"build", "serve"}:
        from .settings import load_config

        cfg = load_config()
    else:
        cfg = {}

    if args.command == "build":
        _run_build(args, cfg, parser)
    elif args.command == "serve":
        _run_serve(args, cfg)
    elif args.command == "reload":
        _run_reload(args.pid)


if __name__ == "__main__":
    main()
