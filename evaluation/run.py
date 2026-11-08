"""Run the evaluation queries against one search engine over HTTP and save where the right article lands.

    python evaluation/run.py --engine zimantic --url http://localhost:8090 --label my-branch --zims archive_name --source-key zim:library:archive-key
    python evaluation/run.py --engine kiwix --url http://localhost:8080 --label kiwix --zim-dir ./zims

Engines:
  zimantic   the current server's /search, /sources and /config endpoints. Requires one query-file
             ZIM name and its canonical source key; use --append to combine runs.
  kiwix      GET <url>/search?content=<zim>&pattern=...&pageLength=<k>; kiwix-serve's full-text search

Results go to evaluation/results/<label>.json: for every query its rank (1..k, or null when the right
article is not in the first k results), the search time, and the top results. Use --append to add ZIMs
to an existing results file (e.g. when different ZIMs are served by different servers) and
--results-dir to write somewhere else. Only the standard library is needed; with --zim-dir, libzim is
used to follow redirects in results.
"""
import argparse
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import queries as Q

RESULTS = Path(__file__).parent / "results"
class Unsupported(Exception):
    """The engine refuses to search this ZIM at all (reported, not counted as misses)."""


def zimantic(url, source_key, query, k, timeout):
    params = urllib.parse.urlencode({"q": query, "source": source_key, "limit": k})
    for attempt in range(10):
        try:
            with urllib.request.urlopen(f"{url}/search?{params}", timeout=timeout) as response:
                hits = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 503 or attempt == 9:   # 503 = server busy; it asks clients to retry shortly
                raise
            time.sleep(float(error.headers.get("Retry-After") or 1))
            continue
        # A result may point into an article (e.g. "Malaria#Treatment" when indexing by section);
        # it counts as finding that article.
        return [{"path": hit["path"].split("#", 1)[0],
                 "source_key": hit["source_key"]} for hit in hits]


def source_names(payload):
    """Canonical keys of searchable local ZIM sources."""
    return {entry["key"] for entry in payload
            if entry["provider"] == "zim" and entry["searchable"]}


def zimantic_sources(url, timeout):
    with urllib.request.urlopen(f"{url}/sources?provider=zim", timeout=timeout) as response:
        return source_names(json.load(response))


def zimantic_config(url, timeout):
    with urllib.request.urlopen(f"{url}/config", timeout=timeout) as response:
        return json.load(response)


def kiwix(url, zim, query, k, timeout):
    params = urllib.parse.urlencode({"content": zim, "pattern": query, "pageLength": k})
    try:
        with urllib.request.urlopen(f"{url}/search?{params}", timeout=timeout) as response:
            html = response.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as error:
        if error.code == 400:   # e.g. kiwix-serve refuses multi-language ("mul") ZIMs
            raise Unsupported(f"kiwix-serve answered HTTP 400 for {zim}") from error
        raise
    return [{"path": urllib.parse.unquote(p)}
            for p in re.findall(rf'href="[^"]*?/content/{re.escape(zim)}/([^"#?]+)"', html)]


ENGINES = {"zimantic": zimantic, "kiwix": kiwix}


def belongs(hit, source_key):
    """Match source identity, never display titles or archive-name aliases."""
    return hit.get("source_key") == source_key


class Resolver:
    """Follows redirects so a result counts when it is a redirect to the right article (needs libzim)."""

    def __init__(self, zim_dir):
        self.zim_dir, self.archives = Path(zim_dir) if zim_dir else None, {}

    def __call__(self, zim, paths):
        if not self.zim_dir:
            return list(dict.fromkeys(paths))
        if zim not in self.archives:
            from libzim.reader import Archive
            file = self.zim_dir / f"{zim}.zim"
            self.archives[zim] = Archive(str(file)) if file.exists() else None
        archive, out = self.archives[zim], []
        for path in paths:
            if archive is not None and archive.has_entry_by_path(path):
                entry = archive.get_entry_by_path(path)
                for _ in range(10):   # redirect chains are short; avoid loops
                    if not entry.is_redirect:
                        break
                    entry = entry.get_redirect_entry()
                path = entry.path
            if path not in out:
                out.append(path)
        return out


def dump(meta, results):
    """The results file as JSON with one query per line, so it reads and diffs like the query files."""
    line = lambda value: json.dumps(value, ensure_ascii=False)
    rows = ",\n".join(f"{line(key)}: {line(value)}" for key, value in results.items())
    return f'{{"meta": {line(meta)},\n"results": {{\n{rows}\n}}}}\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=ENGINES, required=True)
    ap.add_argument("--url", required=True, help="engine base URL, e.g. http://localhost:8090")
    ap.add_argument("--label", required=True, help="name for this run, e.g. main or kiwix")
    ap.add_argument("--commit", default="", help="optional: version or git commit being measured")
    ap.add_argument("--queries", default=str(Q.QUERIES), help="queries folder")
    ap.add_argument("--results-dir", default=str(RESULTS), help="where to write <label>.json")
    ap.add_argument("--zims", nargs="*", help="only these ZIMs (default: all with query files)")
    ap.add_argument("--source-key", help="required for Zimantic: canonical source key for exactly one --zims name")
    ap.add_argument("--suites", nargs="*"); ap.add_argument("--sets", nargs="*"); ap.add_argument("--langs", nargs="*")
    ap.add_argument("--split", choices=("all", "dev", "test"), default="all")
    ap.add_argument("--k", type=int, default=20, help="results requested per query (default 20)")
    ap.add_argument("--keep", type=int, default=20, help="results stored per query in the output (default 20)")
    ap.add_argument("--zim-dir", help="folder with the .zim files, to follow redirects in results (recommended for kiwix)")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--append", action="store_true", help="add to an existing results file instead of replacing it")
    args = ap.parse_args(argv)
    if args.engine == "zimantic":
        if not args.source_key or not args.zims or len(args.zims) != 1:
            ap.error("--engine zimantic requires --source-key and exactly one --zims name")
    elif args.source_key:
        ap.error("--source-key requires --engine zimantic")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    todo, skipped = Q.load(Path(args.queries), args.zims, args.suites, args.sets, args.langs, args.split)
    if not todo:
        sys.exit("no queries match the filters")
    out_dir = Path(args.results_dir)
    out_path = out_dir / f"{args.label}.json"
    previous = json.loads(out_path.read_text(encoding="utf-8")) if args.append and out_path.exists() else {}
    results = previous.get("results", {})
    unsupported = previous.get("meta", {}).get("unsupported", {})
    search, resolve = ENGINES[args.engine], Resolver(args.zim_dir)
    # On Windows "localhost" tries IPv6 (::1) first; a server listening on IPv4 only then costs ~2 s per
    # request before the fallback, which would swamp the timings. 127.0.0.1 is the same machine, IPv4 directly.
    args.url = re.sub(r"//localhost(?=[:/]|$)", "//127.0.0.1", args.url).rstrip("/")
    config = {}
    if args.engine == "zimantic":
        try:
            served = zimantic_sources(args.url, args.timeout)
            config = zimantic_config(args.url, args.timeout)
        except urllib.error.URLError as error:
            sys.exit(f"could not reach {args.url}: {error}")
        if args.source_key not in served:
            sys.exit(f"source is not searchable on this server: {args.source_key}")
        # The server keeps a fixed pool of results and caps every page at max_results. Asking for
        # more than that would only hide that the top k cannot be observed.
        max_results = config["max_results"]
        if isinstance(max_results, int) and 0 < max_results < args.k:
            print(f"server keeps at most {max_results} results per query; using --k {max_results} instead of {args.k}")
            args.k = max_results
        max_query = config["max_query_length"]
        if isinstance(max_query, int) and max_query > 0:
            too_long = [q for q in todo if len(q["query"]) > max_query]
            if too_long:
                print(f"{len(too_long)} queries longer than the server's {max_query}-character limit, left out")
                todo = [q for q in todo if len(q["query"]) <= max_query]
    args.keep = max(1, min(args.keep, args.k))
    print(f"{args.engine} at {args.url}: {len(todo)} queries ({skipped} untranslated template lines skipped)")

    start, times, foreign = time.time(), [], Counter()
    for n, q in enumerate(todo, 1):
        key = f"{q['zim']}|{q['id']}"
        if q["zim"] in unsupported:
            continue
        t = time.perf_counter()
        try:
            hits = search(args.url, args.source_key or q["zim"], q["query"], args.k, args.timeout)
        except Unsupported as error:
            unsupported[q["zim"]] = str(error)
            print(f"  {q['zim']}: unsupported by this engine ({error}); skipping it")
            continue
        ms = 1000 * (time.perf_counter() - t)
        wanted = ([hit for hit in hits if belongs(hit, args.source_key)]
                  if args.engine == "zimantic" else hits)
        if len(wanted) < len(hits):
            foreign[q["zim"]] += len(hits) - len(wanted)
        top = resolve(q["zim"], [hit["path"] for hit in wanted])[: args.k]
        rank = next((i + 1 for i, path in enumerate(top) if path in q["answers"]), None)
        results[key] = {"rank": rank, "ms": round(ms, 1), "top": top[: args.keep]}
        times.append(ms)
        if n % 500 == 0:
            print(f"  {n}/{len(todo)} queries, {time.time() - start:.0f} s", flush=True)

    if foreign:
        print(f"warning: left out results from other sources ({', '.join(f'{z}: {c}' for z, c in sorted(foreign.items()))}); "
              "the server did not scope the search to the requested ZIM")
    meta = {"label": args.label, "engine": args.engine, "url": args.url, "commit": args.commit, "k": args.k,
            "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"), "unsupported": unsupported}
    if args.source_key:
        meta["source_key"] = args.source_key
    if isinstance(config.get("max_results"), int):
        meta["max_results"] = config["max_results"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dump(meta, results), encoding="utf-8")
    top1 = sum(1 for q in todo if results.get(f"{q['zim']}|{q['id']}", {}).get("rank") == 1)
    print(f"saved {out_path} | {len(results)} results | right article #1 for {top1}/{len(todo)} of this run's queries"
          + (f" | median {statistics.median(times):.0f} ms" if times else ""))


if __name__ == "__main__":
    main()
