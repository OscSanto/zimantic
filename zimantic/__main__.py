import argparse
import yaml
from pathlib import Path
from .embed import Embedder
import sys

def main() -> None:

    parser = argparse.ArgumentParser(prog="python -m zimantic")
    
    # subcommand: build (path | list of zims) or serve
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the search page and JSON API")

    build = commands.add_parser("build", help="index ZIM files")
    # Single zim file path OR multiple existing ZIMS in zim_dir by name
    build.add_argument("--path", type = Path, help ="path to one ZIM file" )
    build.add_argument("zims", nargs="*", help="names of ZIM files in zim_dir")

    args = parser.parse_args()

    if args.command == "build" and bool(args.path) == bool(args.zims):
        parser.error("build: give only --path FILE or 1 or more ZIM names. NOT BOTH")

    config = Path("config.yaml")
    if not config.exists():
        sys.exit("config.yaml not found")
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    if cfg is None:
        sys.exit("config.yaml is empty")

   
    if args.command == "build":
        from .build import build as build_zim

        if args.path:
            zims = [args.path]
        else:
            zim_dir = Path(cfg["zim_dir"])
            # create ZIM list from args 
            zims = [zim_dir / (z if z.endswith(".zim") else z + ".zim") for z in args.zims]
        
        # check every ZIM before starting, so a typo in the nth name doesn't fail after hours of indexing
        for zim in zims:
            if not zim.exists():
                sys.exit(f"zimantic: ZIM not found: {zim}")
            if (Path(cfg["index_dir"]) / f"{zim.stem}.faiss").exists():
                sys.exit(f"zimantic: {zim.stem} is already indexed")

        embedder = Embedder(cfg["model_dir"])
        for zim in zims:
            build_zim(zim, cfg["index_dir"], embedder, cfg["batch_size"])

    elif args.command == "serve":
        from .search import Search
        from .server import serve

        serve(Search(cfg, embedder=Embedder(cfg["model_dir"])), cfg["port"])

if __name__ == "__main__":
    main()
