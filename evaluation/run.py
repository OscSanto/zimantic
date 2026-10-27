"""Run the evaluation queries against one search engine over HTTP and save where the right article lands.

    python evaluation/run.py --engine zimantic --url http://localhost:8090 --label v0.1.0-lite
    python evaluation/run.py --engine kiwix --url http://localhost:8080 --label kiwix --zim-dir ./zims

Engines:
  zimantic   GET <url>/api/search?q=...&zim=<zim>&limit=<k> (v0.1.x), or <url>/search for versions that
             moved the API to the root; the runner finds which one the server uses
  kiwix      GET <url>/search?content=<zim>&pattern=...&pageLength=<k>; kiwix-serve's full-text search

Results go to evaluation/results/<label>.json: for every query its rank (1..k, or null when the right
article is not in the first k results), the search time, and the top results. Use --append to add ZIMs
to an existing results file (e.g. when different ZIMs are served by different servers).
Only the standard library is needed; with --zim-dir, libzim is used to follow redirects in results.
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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import queries as Q  # noqa: E402

RESULTS = Path(__file__).parent / "results"


class Unsupported(Exception):
    """The engine refuses to search this ZIM at all (reported, not counted as misses)."""


def zimantic(url, zim, query, k, timeout):
    """url is the API base: <server>/api for v0.1.x, <server> for versions that moved the API to the root."""
    params = urllib.parse.urlencode({"q": query, "zim": zim, "limit": k})
    for attempt in range(10):
        try:
            with urllib.request.urlopen(f"{url}/search?{params}", timeout=timeout) as response:
                # A result may point into an article (e.g. "Malaria#Treatment" when indexing by section);
                # it counts as finding that article.
                return [hit["path"].split("#", 1)[0] for hit in json.load(response)]
        except urllib.error.HTTPError as error:
            if error.code != 503 or attempt == 9:   # 503 = server busy; it asks clients to retry shortly
                raise
            time.sleep(float(error.headers.get("Retry-After") or 1))


def zimantic_api(url, timeout):
    """Find where this Zimantic version serves its API and which ZIMs it has: (api base, ZIM names)."""
    # v0.1.x: <url>/api/zims lists names. Later versions: <url>/zims, then <url>/sources listing objects.
    for base, listing in ((f"{url}/api", "zims"), (url, "zims"), (url, "sources")):
        try:
            with urllib.request.urlopen(f"{base}/{listing}", timeout=timeout) as response:
                names = json.load(response)
        except (urllib.error.HTTPError, ValueError):
            continue
        if isinstance(names, list) and all(isinstance(n, dict) for n in names):
            names = [n.get("name") or n.get("book") for n in names if n.get("available", True)]
        if isinstance(names, list) and all(isinstance(n, str) for n in names):
            return base, set(names)
    sys.exit(f"no Zimantic API found at {url}/api/zims, {url}/zims or {url}/sources")


def kiwix(url, zim, query, k, timeout):
    params = urllib.parse.urlencode({"content": zim, "pattern": query, "pageLength": k})
    try:
        with urllib.request.urlopen(f"{url}/search?{params}", timeout=timeout) as response:
            html = response.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as error:
        if error.code == 400:   # e.g. kiwix-serve refuses multi-language ("mul") ZIMs
            raise Unsupported(f"kiwix-serve answered HTTP 400 for {zim}") from error
        raise
    return [urllib.parse.unquote(p) for p in re.findall(rf'href="[^"]*?/content/{re.escape(zim)}/([^"#?]+)"', html)]


ENGINES = {"zimantic": zimantic, "kiwix": kiwix}


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=ENGINES, required=True)
    ap.add_argument("--url", required=True, help="engine base URL, e.g. http://localhost:8090")
    ap.add_argument("--label", required=True, help="name for this run, e.g. v0.1.0-lite or kiwix")
    ap.add_argument("--commit", default="", help="optional: version or git commit being measured")
    ap.add_argument("--queries", default=str(Q.QUERIES), help="queries folder")
    ap.add_argument("--zims", nargs="*", help="only these ZIMs (default: all with query files)")
    ap.add_argument("--suites", nargs="*"); ap.add_argument("--sets", nargs="*"); ap.add_argument("--langs", nargs="*")
    ap.add_argument("--split", choices=("all", "dev", "test"), default="all")
    ap.add_argument("--k", type=int, default=20, help="results requested per query (default 20)")
    ap.add_argument("--keep", type=int, default=20, help="results stored per query in the output (default 20)")
    ap.add_argument("--zim-dir", help="folder with the .zim files, to follow redirects in results (recommended for kiwix)")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--append", action="store_true", help="add to an existing results file instead of replacing it")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    todo, skipped = Q.load(Path(args.queries), args.zims, args.suites, args.sets, args.langs, args.split)
    if not todo:
        sys.exit("no queries match the filters")
    out_path = RESULTS / f"{args.label}.json"
    previous = json.loads(out_path.read_text(encoding="utf-8")) if args.append and out_path.exists() else {}
    results = previous.get("results", {})
    unsupported = previous.get("meta", {}).get("unsupported", {})
    search, resolve = ENGINES[args.engine], Resolver(args.zim_dir)
    # On Windows "localhost" tries IPv6 (::1) first; a server listening on IPv4 only then costs ~2 s per
    # request before the fallback, which would swamp the timings. 127.0.0.1 is the same machine, IPv4 directly.
    args.url = re.sub(r"//localhost(?=[:/]|$)", "//127.0.0.1", args.url).rstrip("/")
    endpoint = args.url
    if args.engine == "zimantic":
        # Zimantic silently ignores ZIMs it has no index for and returns no results, which would count as
        # misses. Ask which ZIMs the server has and leave the others out of this run.
        try:
            endpoint, served = zimantic_api(args.url, args.timeout)
        except urllib.error.URLError as error:
            sys.exit(f"could not reach {args.url}: {error}")
        missing = sorted({q["zim"] for q in todo} - served)
        if missing:
            print(f"not indexed on this server, left out of this run: {', '.join(missing)}")
            todo = [q for q in todo if q["zim"] in served]
        if not todo:
            sys.exit("the server has none of the ZIMs these queries are for")
    print(f"{args.engine} at {args.url}: {len(todo)} queries ({skipped} untranslated template lines skipped)")

    start, times = time.time(), []
    for n, q in enumerate(todo, 1):
        key = f"{q['zim']}|{q['id']}"
        if q["zim"] in unsupported:
            continue
        t = time.perf_counter()
        try:
            top = search(endpoint, q["zim"], q["query"], args.k, args.timeout)
        except Unsupported as error:
            unsupported[q["zim"]] = str(error)
            print(f"  {q['zim']}: unsupported by this engine ({error}); skipping it")
            continue
        ms = 1000 * (time.perf_counter() - t)
        top = resolve(q["zim"], top)[: args.k]
        rank = next((i + 1 for i, path in enumerate(top) if path in q["answers"]), None)
        results[key] = {"rank": rank, "ms": round(ms, 1), "top": top[: args.keep]}
        times.append(ms)
        if n % 500 == 0:
            print(f"  {n}/{len(todo)} queries, {time.time() - start:.0f} s", flush=True)

    meta = {"label": args.label, "engine": args.engine, "url": args.url, "commit": args.commit, "k": args.k,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "unsupported": unsupported}
    RESULTS.mkdir(exist_ok=True)
    out_path.write_text(json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=0), encoding="utf-8")
    top1 = sum(1 for q in todo if results.get(f"{q['zim']}|{q['id']}", {}).get("rank") == 1)
    print(f"saved {out_path} | {len(results)} results | right article #1 for {top1}/{len(todo)} of this run's queries"
          + (f" | median {statistics.median(times):.0f} ms" if times else ""))


if __name__ == "__main__":
    main()
