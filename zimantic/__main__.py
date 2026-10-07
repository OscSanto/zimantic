import argparse
import json
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import sys

def main() -> None:

    parser = argparse.ArgumentParser(prog="python -m zimantic")
    
    # subcommands: build (files/folders, default zim_dir), serve, reload
    commands = parser.add_subparsers(dest="command", required=True)

    serve_cmd = commands.add_parser("serve", help="run the search page and JSON API")
    serve_cmd.add_argument(
        "--fast",
        action="store_true",
        help="start immediately without the model or FAISS vectors (title and ZIM full-text results only)",
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

    reload_cmd = commands.add_parser(
        "reload",
        help="ask a running serve process to rescan index_dir (fast; suitable for systemd.path)",
    )
    reload_cmd.add_argument("--url", help="base URL of the running server (default http://127.0.0.1:<port>)")
    reload_cmd.add_argument("--timeout", type=float, default=15.0, help="request timeout in seconds")

    args = parser.parse_args()

    config = Path("config.toml")
    cfg = {}
    if config.exists():
        cfg = tomllib.loads(config.read_text(encoding="utf-8"))
    if args.command in {"build", "serve"} and not cfg:
        sys.exit("config.toml not found or empty")

   
    if args.command == "build":
        from .build import build as build_zim
        from .zim import DEFAULT_MAX_HTML_BYTES

        # Expand the positional paths: files are used directly, folders mean
        # their *.zim, and no argument at all means zim_dir from config.toml.
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

        # fast builds never embed: skip loading the model so they start instantly.
        embedder = None
        if not args.fast:
            from .embed import Embedder
            embedder = Embedder(cfg["model_dir"])
        for zim in zims:
            build_zim(
                zim,
                cfg["index_dir"],
                embedder,
                cfg["batch_size"],
                fast=args.fast,
                first_paragraph=bool(cfg.get("first_paragraph", False)),
                max_html_bytes=cfg.get("max_html_bytes", DEFAULT_MAX_HTML_BYTES),
            )

    elif args.command == "serve":
        from .search import Search
        from .server import serve

        search = Search(cfg, semantic=not args.fast)
        if not search.local_names():
            print(
                "zimantic: warning: no sources are indexed yet; "
                "run `zimantic build --fast` before serving searches",
                file=sys.stderr,
            )
        if not args.fast:
            from .embed import Embedder
            search.embedder = Embedder(cfg["model_dir"])
        if args.fast:
            print("zimantic: fast mode: title and ZIM full-text search only")
        serve(search, cfg["port"])

    elif args.command == "reload":
        if not args.url and "port" not in cfg:
            sys.exit("zimantic: reload needs --url (or config.toml with a port)")
        base = args.url or f"http://127.0.0.1:{cfg['port']}"
        url = base.rstrip("/") + "/api/reload"
        try:
            request = Request(url, method="POST", headers={"Accept": "application/json"})
            with urlopen(request, timeout=args.timeout) as response:
                summary = json.loads(response.read() or b"{}")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            sys.exit(f"zimantic: reload failed ({error}); is `zimantic serve` running at {base}?")
        print(
            "zimantic: reloaded "
            f"{len(summary.get('indexes', []))} index(es) "
            f"(+{len(summary.get('added', []))} new, "
            f"~{len(summary.get('upgraded', []))} upgraded, "
            f"-{len(summary.get('removed', []))} removed)"
        )

if __name__ == "__main__":
    main()
