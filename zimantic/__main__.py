import argparse
import os
import signal
import sys
import time
from pathlib import Path

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
STABLE_POLL_SECONDS = 10


def _wait_for_stable_size(path: Path) -> None:
    """Wait for a ZIM to finish being written before indexing it."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    print(
        f"zimantic: {path.name} is {size} bytes; waiting for it to finish downloading",
        file=sys.stderr,
        flush=True,
    )
    while True:
        time.sleep(STABLE_POLL_SECONDS)
        try:
            current = path.stat().st_size
        except OSError:
            return
        if current == size:
            return
        size = current
        print(
            f"zimantic: {path.name} is now {size} bytes; still downloading",
            file=sys.stderr,
            flush=True,
        )

def main() -> None:

    parser = argparse.ArgumentParser(prog="zimantic")
    
    # subcommands: build (files/folders, default zim_dir), serve, reload
    commands = parser.add_subparsers(dest="command", required=True)

    serve_cmd = commands.add_parser("serve", help="run the search page and JSON API")
    serve_cmd.add_argument(
        "--fast",
        action="store_true",
        help="start immediately without the model or FAISS vectors (title and ZIM full-text results only)",
    )
    serve_cmd.add_argument(
        "--debug",
        action="store_true",
        help="include ranking scores and explanations in search results",
    )

    build = commands.add_parser("build", help="index ZIM files")
    build.add_argument(
        "paths",
        nargs="*",
        type=Path,
        metavar="PATH",
        help="ZIM files or folders containing *.zim; a folder means every .zim in it. Default: zim_dir",
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

    reload_cmd = commands.add_parser(
        "reload",
        help="ask a running serve process to rescan index_dir (sends SIGHUP; suitable for systemd.path or cron)",
    )
    reload_cmd.add_argument(
        "--pid",
        type=Path,
        default=Path("zimantic.pid"),
        help="file holding the server's PID (default: ./zimantic.pid)",
    )

    args = parser.parse_args()

    # `reload` only needs the PID file.
    if args.command in {"build", "serve"}:
        from .settings import load_config

        cfg = load_config()
    else:
        cfg = {}

    if args.command == "build":
        from .build import build as build_zim
        from .embed import DEFAULT_MAX_EMBEDDING_TOKENS, Embedder
        from .zim import (
            DEFAULT_EMBEDDING_OVERFLOW,
            DEFAULT_MAX_HTML_BYTES,
            DEFAULT_PREVIEW_CHARS,
            DEFAULT_PREVIEW_OVERFLOW,
        )
        from tqdm import tqdm

        # systemd stops units with SIGTERM; treat it like Ctrl-C so a stopped
        # build unwinds cleanly and can be resumed by the next run.
        sigterm = getattr(signal, "SIGTERM", None)
        if sigterm is not None:
            signal.signal(sigterm, _graceful_interrupt)

        # Expand the positional paths: files are used directly, folders mean
        # their *.zim, and no argument at all means the configured zim_dir.
        zims: list[Path] = []
        seen: set[Path] = set()
        for source in (args.paths or [Path(cfg["zim_dir"])]):
            if source.is_dir():
                matches = sorted(source.glob("*.zim"))
            elif source.is_file():
                matches = [source]
            else:
                sys.exit(f"zimantic: path not found: {source}")
            for zim in matches:
                resolved = zim.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    zims.append(zim)

        if not zims:
            print("zimantic: no .zim files to index")
            return

        zim_sizes = {zim: zim.stat().st_size for zim in zims}
        zims.sort(key=zim_sizes.__getitem__)

        by_stem: dict[str, list[Path]] = {}
        for zim in zims:
            by_stem.setdefault(zim.stem, []).append(zim)
        for stem, paths in by_stem.items():
            if len(paths) > 1:
                print(
                    f"zimantic: warning: multiple input files share the index name "
                    f"{stem!r}: {', '.join(map(str, paths))}",
                    file=sys.stderr,
                )

        # fast builds never embed: skip loading the model so they start instantly.
        embedder = None
        if not args.fast:
            embedder = Embedder(
                cfg["model_dir"],
                max_tokens=cfg.get("max_embedding_tokens", DEFAULT_MAX_EMBEDDING_TOKENS),
                threads=cfg.get("embed_threads"),
            )
        global_progress = None
        if len(zims) > 1 and sys.stdout.isatty():
            global_progress = tqdm(
                total=sum(zim_sizes.values()),
                desc="all ZIMs",
                unit="B",
                unit_scale=True,
                file=sys.stdout,
            )
        failures: list[tuple[Path, BaseException]] = []
        try:
            for zim in zims:
                _wait_for_stable_size(zim)
                try:
                    build_zim(
                        zim,
                        cfg["index_dir"],
                        embedder,
                        cfg["batch_size"],
                        fast=args.fast,
                        max_html_bytes=cfg.get("max_html_bytes", DEFAULT_MAX_HTML_BYTES),
                        max_preview_chars=cfg.get("max_preview_chars", DEFAULT_PREVIEW_CHARS),
                        max_embedding_tokens=cfg.get("max_embedding_tokens", DEFAULT_MAX_EMBEDDING_TOKENS),
                        preview_overflow=cfg.get("preview_overflow", DEFAULT_PREVIEW_OVERFLOW),
                        embedding_overflow=cfg.get("embedding_overflow", DEFAULT_EMBEDDING_OVERFLOW),
                        force=args.force,
                    )
                except Exception as error:
                    print(f"zimantic: failed to index {zim}: {error}", file=sys.stderr)
                    failures.append((zim, error))
                if global_progress is not None:
                    global_progress.update(zim_sizes[zim])
        finally:
            if global_progress is not None:
                global_progress.close()
        if failures:
            sys.exit(1)

    elif args.command == "serve":
        from .search import Search

        search = Search(cfg, semantic=not args.fast)
        if not search.local_names():
            print(
                "zimantic: warning: no sources are indexed yet; "
                "run `zimantic build --fast` before serving searches",
                file=sys.stderr,
            )
        if not args.fast:
            # Load the model off the critical path: the server answers title and
            # full-text searches immediately and gains meaning search once the
            # ONNX model and vocabulary finish loading in the background.
            def _make_embedder():
                from .embed import DEFAULT_MAX_EMBEDDING_TOKENS, Embedder

                return Embedder(
                    cfg["model_dir"],
                    max_tokens=cfg.get("max_embedding_tokens", DEFAULT_MAX_EMBEDDING_TOKENS),
                    threads=cfg.get("embed_threads"),
                )

            search.start_embedder(_make_embedder)

        from .server import serve

        if args.fast:
            print("zimantic: fast mode: title and ZIM full-text search only")
        if args.debug:
            print("zimantic: debug mode: ranking scores and explanations enabled")
        serve(search, cfg["port"], debug=args.debug)

    elif args.command == "reload":
        pid_file = args.pid
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
        print(f"zimantic: reload signal (SIGHUP) sent to PID {pid}")

if __name__ == "__main__":
    main()
