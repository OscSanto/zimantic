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

    build = commands.add_parser("build", help="index ZIM and other files")
    _add_verbosity_argument(build)
    build.add_argument(
        "paths",
        nargs="*",
        type=Path,
        metavar="PATH",
        help=(
            "files or folders to index; a folder means every matching file "
            "under it (recursively). Default: zim_dir"
        ),
    )
    build.add_argument(
        "--file-types",
        action="append",
        metavar="GLOB",
        default=None,
        help=(
            "file types indexed when scanning folders. Extensions may be "
            "written as epub, .epub or *.epub; repeat the option or separate "
            "values with commas, for example 'zim,epub,pdf,txt,md,mp4'. "
            "Named profiles expand to several formats: text, ebook, docs, "
            "office, media, video, audio, image, comic, archive, all "
            "(every supported type) and any (every file, using content "
            "detection). Explicit file paths are always considered even "
            "when their extension is unknown. Default: config file_types "
            "(zim)"
        ),
    )
    build.add_argument(
        "--ocr",
        action="store_true",
        default=None,
        help="OCR scanned PDFs, images and comic pages when tesseract is available",
    )
    build.add_argument(
        "--asr",
        action="store_true",
        default=None,
        help="transcribe media without subtitles when whisper is available",
    )
    build.add_argument(
        "--archive-depth",
        type=int,
        default=None,
        metavar="N",
        help=(
            "recurse into N levels of archives after indexing their member "
            "names (default: config archive_depth, 0 = list names only)"
        ),
    )
    build.add_argument(
        "--fast",
        action="store_true",
        help="title and full-text only: skip the model and FAISS (much faster)",
    )
    build.add_argument(
        "--force",
        action="store_true",
        help="rebuild the selected indexes even if they are already complete",
    )
    build.add_argument(
        "-j",
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="build up to N independent sources concurrently (default: config build_workers)",
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
        default=Path("zimantic.pid"),
        help="file holding the server's PID (default: ./zimantic.pid)",
    )
    return parser


def _collect_sources(
    paths: list[Path],
    default_dir: str | Path,
    patterns: tuple[str, ...],
) -> list[Path]:
    """Expand CLI files and folders into unique source paths.

    Explicit file arguments are honored regardless of the configured patterns;
    ``file_types`` only decides what a recursive folder scan picks up.
    """
    from .catalog import iter_files

    sources: list[Path] = []
    seen: set[Path] = set()
    for source in (paths or [Path(default_dir)]):
        if source.is_dir():
            matches = [item for item in iter_files(source, patterns) if item.is_file()]
        elif source.is_file():
            matches = [source]
        else:
            sys.exit(f"zimantic: path not found: {source}")
        for item in matches:
            resolved = item.resolve()
            if resolved not in seen:
                seen.add(resolved)
                sources.append(item)
    return sources


def _collect_zims(paths: list[Path], default_dir: str | Path) -> list[Path]:
    """Backward-compatible ZIM-only collector used by older callers/tests."""
    from .catalog import DEFAULT_FILE_TYPES

    return _collect_sources(paths, default_dir, DEFAULT_FILE_TYPES)


def _select_zim_paths(zims: list[Path], zim_sizes: dict[Path, int]) -> list[Path]:
    """Choose one source per index identity, preferring the largest copy."""
    from .catalog import source_index_name

    by_stem: dict[str, list[Path]] = {}
    for zim in zims:
        by_stem.setdefault(source_index_name(zim), []).append(zim)
    accepted: list[Path] = []
    for stem, paths in sorted(by_stem.items()):
        sizes = {zim_sizes[path] for path in paths}
        # Build and serve use the largest copy for an ambiguous stem. The
        # catalog keeps every row but resolves the stem to its largest file, so
        # all three agree on which archive wins.
        chosen = max(paths, key=zim_sizes.__getitem__)
        if len(sizes) > 1:
            logger.warning(
                "zimantic: sources share index identity %r with different sizes; "
                "using the largest %s",
                stem,
                chosen,
            )
        elif len(paths) > 1:
            logger.warning(
                "zimantic: ignoring duplicate sources with index identity %r: %s",
                stem,
                ", ".join(str(path) for path in paths if path != chosen),
            )
        accepted.append(chosen)
    return sorted(accepted, key=zim_sizes.__getitem__)


def _run_build(args, cfg: dict, parser: argparse.ArgumentParser) -> None:
    from .build import build as build_source
    from .catalog import parse_file_patterns
    from .content.extract import (
        DEFAULT_ARCHIVE_DEPTH,
        DEFAULT_ASR_MODEL,
        DEFAULT_FILE_TIMEOUT,
        DEFAULT_MAX_ARCHIVE_BYTES,
        DEFAULT_MAX_ARCHIVE_MEMBERS,
        DEFAULT_MAX_FILE_TEXT_CHARS,
        DEFAULT_MAX_OCR_PAGES,
        DEFAULT_OCR_LANGUAGES,
        ExtractOptions,
    )
    from .content.reader import (
        DEFAULT_MAX_ENTRY_BYTES,
        DEFAULT_MAX_HTML_BYTES,
        DEFAULT_PREVIEW_CHARS,
    )
    from .content.tools import log_capabilities
    from tqdm import tqdm

    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is not None:
        signal.signal(sigterm, _graceful_interrupt)

    patterns = parse_file_patterns(
        args.file_types if args.file_types is not None else cfg.get("file_types")
    )
    sources = _collect_sources(args.paths, cfg["zim_dir"], patterns)
    if not sources:
        logger.info("zimantic: no matching files to index (patterns: %s)", ", ".join(patterns))
        return

    workers = args.workers
    if workers is None:
        workers = cfg.get("build_workers", 1)
    if workers < 1:
        parser.error("--workers must be at least 1")

    archive_depth = (
        args.archive_depth
        if args.archive_depth is not None
        else cfg.get("archive_depth", DEFAULT_ARCHIVE_DEPTH)
    )
    if archive_depth < 0:
        parser.error("--archive-depth must be zero or greater")
    extract_options = ExtractOptions(
        max_text_chars=int(
            cfg.get("max_file_text_chars", DEFAULT_MAX_FILE_TEXT_CHARS)
        ),
        timeout=cfg.get("file_timeout", DEFAULT_FILE_TIMEOUT),
        archive_depth=archive_depth,
        max_archive_members=int(
            cfg.get("max_archive_members", DEFAULT_MAX_ARCHIVE_MEMBERS)
        ),
        max_archive_bytes=int(
            cfg.get("max_archive_bytes", DEFAULT_MAX_ARCHIVE_BYTES)
        ),
        max_ocr_pages=int(cfg.get("max_ocr_pages", DEFAULT_MAX_OCR_PAGES)),
        ocr=bool(cfg.get("ocr", False) if args.ocr is None else args.ocr),
        ocr_languages=str(
            cfg.get("ocr_languages", DEFAULT_OCR_LANGUAGES)
        ),
        asr=bool(cfg.get("asr", False) if args.asr is None else args.asr),
        asr_model=str(cfg.get("asr_model", DEFAULT_ASR_MODEL)),
    )
    if any(not source.name.lower().endswith(".zim") for source in sources):
        log_capabilities()

    source_sizes = {source: source.stat().st_size for source in sources}
    downloading = _find_downloading_sizes(source_sizes)
    sources = _select_zim_paths(sources, source_sizes)
    if not sources:
        logger.info("zimantic: no matching files to index")
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
    if len(sources) > 1 and sys.stderr.isatty():
        global_progress = tqdm(
            total=sum(source_sizes[source] for source in sources),
            desc="all sources",
            unit="B",
            unit_scale=True,
            file=sys.stderr,
        )
    failures: list[tuple[Path, BaseException]] = []

    def run_build(source: Path) -> None:
        build_source(
            source,
            cfg["index_dir"],
            embedder,
            cfg["batch_size"],
            fast=args.fast,
            max_html_bytes=cfg.get("max_html_bytes", DEFAULT_MAX_HTML_BYTES),
            max_entry_bytes=cfg.get("max_entry_bytes", DEFAULT_MAX_ENTRY_BYTES),
            max_preview_chars=cfg.get("max_preview_chars", DEFAULT_PREVIEW_CHARS),
            force=args.force,
            embedding_pipeline=embedding_pipeline,
            extract_options=extract_options,
        )

    try:
        if workers == 1 or len(sources) == 1:
            for source in sources:
                try:
                    run_build(source)
                except Exception as error:
                    logger.error("zimantic: failed to index %s: %s", source, error)
                    failures.append((source, error))
                if global_progress is not None:
                    global_progress.update(source_sizes[source])
        else:
            pool = ThreadPoolExecutor(
                max_workers=min(workers, len(sources)),
                thread_name_prefix="zimantic-build",
            )
            try:
                futures = {pool.submit(run_build, source): source for source in sources}
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        future.result()
                    except Exception as error:
                        logger.error("zimantic: failed to index %s: %s", source, error)
                        failures.append((source, error))
                    if global_progress is not None:
                        global_progress.update(source_sizes[source])
            finally:
                # On Ctrl-C/SIGTERM drop builds that have not started yet
                # instead of running every queued source to completion.
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

    search = Search(cfg, semantic=not args.fast)
    if not search.local_names():
        logger.warning(
            "zimantic: no local sources are available; add a .zim to zim_dir, "
            "index other files with `zimantic build --file-types ...`, "
            "or run `zimantic build --fast`"
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
