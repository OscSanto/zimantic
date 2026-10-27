"""Run the evaluation queries against one search engine over HTTP and save where the right article lands.

    python evaluation/run.py --engine zimantic --url http://localhost:8090 --label my-branch
    python evaluation/run.py --engine zimantic --url http://localhost:8090 --label my-branch --commit abc1234
    python evaluation/run.py --engine kiwix --url http://localhost:8080 --label kiwix --zim-dir ./zims

Engines:
  zimantic   the current server, which serves /search, or v0.1.x, which served /api/search. The runner
             probes /api/zims, /zims, /library.json and /sources to find the API and the indexed ZIMs,
             and reads /config for the server's result and query-length limits.
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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import queries as Q  # noqa: E402

RESULTS = Path(__file__).parent / "results"
# A release suffix on a ZIM name ("_2026-04" or "_2026-04-15"). Newer servers expose a date-stripped
# source key next to the archive name, so a result can be linked back to the ZIM it came from.
ZIM_DATE = re.compile(r"_\d{4}-\d{2}(?:-\d{2})?$")


class Unsupported(Exception):
    """The engine refuses to search this ZIM at all (reported, not counted as misses)."""


def perma_ref(name: str) -> str:
    """A ZIM name without its release date, the key newer servers use for local sources."""
    return ZIM_DATE.sub("", name)


def zimantic(url, zim, query, k, timeout):
    """url is the API base: <server>/api for v0.1.x, <server> for newer versions."""
    params = urllib.parse.urlencode({"q": query, "zim": zim, "limit": k})
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
        # it counts as finding that article. Newer servers also say which source it came from, so
        # results from another ZIM can be recognised (and left out) if the server ignored the filter.
        return [{"path": hit["path"].split("#", 1)[0],
                 "source": hit.get("source"), "source_key": hit.get("source_key")} for hit in hits]


def source_names(payload):
    """ZIM names a /sources or /library.json payload says are local and searchable.

    Returns None when the payload is not a source listing (an error object, for example).
    """
    names = set()
    for entry in payload:
        if isinstance(entry, str):
            names.add(entry)   # v0.1.x /api/zims returned bare names
            continue
        if not isinstance(entry, dict):
            return None
        # /sources mixes local ZIMs with remote Kiwix catalog books; only local indexes can answer
        # the queries, whose answers are paths inside a local archive. Readable-but-unindexed local
        # archives (library.json) are not searchable either.
        if entry.get("mode") not in (None, "local"):
            continue
        if not entry.get("available", True) or not entry.get("searchable", True):
            continue
        for field in ("local_name", "name", "book"):
            value = entry.get(field)
            if isinstance(value, str) and value:
                names.add(value)
    return names


def zimantic_api(url, timeout):
    """Find where this Zimantic version serves its API and which ZIMs it can search: (api base, names)."""
    # v0.1.x: <url>/api/zims. Middle versions: <url>/zims. Current versions: <url>/library.json
    # (local archives only, so a large remote catalog does not have to be downloaded) or
    # <url>/sources (every source). Prefer the first non-empty listing; keep an empty one as fallback.
    empty = None
    for base, listing in ((f"{url}/api", "zims"), (url, "zims"), (url, "library.json"), (url, "sources")):
        try:
            with urllib.request.urlopen(f"{base}/{listing}", timeout=timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError:
            continue   # this version does not have that endpoint
        except ValueError:
            continue   # not JSON: some other app, or a different API shape
        if not isinstance(payload, list):
            continue
        names = source_names(payload)
        if names is None:
            continue
        if names:
            return base, names
        if empty is None:
            empty = (base, names)
    if empty is not None:
        return empty
    sys.exit(f"no Zimantic API found at {url}/api/zims, {url}/zims, {url}/library.json or {url}/sources")


def zimantic_config(url, timeout):
    """Server limits from /config (newer versions); an empty dict on older ones."""
    try:
        with urllib.request.urlopen(f"{url}/config", timeout=timeout) as response:
            config = json.load(response)
    except (urllib.error.URLError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def kiwix(url, zim, query, k, timeout):
    params = urllib.parse.urlencode({"content": zim, "pattern": query, "pageLength": k})
    try:
        with urllib.request.urlopen(f"{url}/search?{params}", timeout=timeout) as response:
            html = response.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as error:
        if error.code == 400:   # e.g. kiwix-serve refuses multi-language ("mul") ZIMs
            raise Unsupported(f"kiwix-serve answered HTTP 400 for {zim}") from error
        raise
    return [{"path": urllib.parse.unquote(p), "source": None, "source_key": None}
            for p in re.findall(rf'href="[^"]*?/content/{re.escape(zim)}/([^"#?]+)"', html)]


ENGINES = {"zimantic": zimantic, "kiwix": kiwix}


def belongs(hit, zim):
    """Whether a hit came from the requested ZIM.

    Older servers do not report the source, so every hit is trusted there. Newer ones do, and the
    request must not silently score articles from other sources: the server falls back to a search
    over every source when the `zim` filter matches nothing.
    """
    names = {hit.get("source"), hit.get("source_key")} - {None}
    return not names or zim in names or perma_ref(zim) in names


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
    ap.add_argument("--label", required=True, help="name for this run, e.g. v0.1.0-lite or kiwix")
    ap.add_argument("--commit", default="", help="optional: version or git commit being measured")
    ap.add_argument("--queries", default=str(Q.QUERIES), help="queries folder")
    ap.add_argument("--results-dir", default=str(RESULTS), help="where to write <label>.json")
    ap.add_argument("--zims", nargs="*", help="only these ZIMs (default: all with query files)")
    ap.add_argument("--suites", nargs="*"); ap.add_argument("--sets", nargs="*"); ap.add_argument("--langs", nargs="*")
    ap.add_argument("--split", choices=("all", "dev", "test"), default="all")
    ap.add_argument("--k", type=int, default=20, help="results requested per query (default 20)")
    ap.add_argument("--keep", type=int, default=20, help="results stored per query in the output (default 20)")
    ap.add_argument("--zim-dir", help="folder with the .zim files, to follow redirects in results (recommended for kiwix)")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--append", action="store_true", help="add to an existing results file instead of replacing it")
    args = ap.parse_args(argv)
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
    endpoint, config = args.url, {}
    if args.engine == "zimantic":
        # Zimantic silently ignores ZIMs it has no index for and would search every source instead,
        # which would count as misses (or worse, as hits from the wrong article). Ask which ZIMs the
        # server can search and leave the others out of this run.
        try:
            endpoint, served = zimantic_api(args.url, args.timeout)
        except urllib.error.URLError as error:
            sys.exit(f"could not reach {args.url}: {error}")
        config = zimantic_config(args.url, args.timeout)
        missing = sorted({q["zim"] for q in todo} - served)
        if missing:
            print(f"not searchable on this server, left out of this run: {', '.join(missing)}")
            todo = [q for q in todo if q["zim"] in served]
        if not todo:
            sys.exit("the server has none of the ZIMs these queries are for")
        # Newer servers keep a fixed pool of results and cap every page at max_results. Asking for
        # more than that would only hide that the top k cannot be observed.
        max_results = config.get("max_results")
        if isinstance(max_results, int) and 0 < max_results < args.k:
            print(f"server keeps at most {max_results} results per query; using --k {max_results} instead of {args.k}")
            args.k = max_results
        max_query = config.get("max_query_length")
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
            hits = search(endpoint, q["zim"], q["query"], args.k, args.timeout)
        except Unsupported as error:
            unsupported[q["zim"]] = str(error)
            print(f"  {q['zim']}: unsupported by this engine ({error}); skipping it")
            continue
        ms = 1000 * (time.perf_counter() - t)
        wanted = [hit for hit in hits if belongs(hit, q["zim"])]
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
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "unsupported": unsupported}
    if isinstance(config.get("max_results"), int):
        meta["max_results"] = config["max_results"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dump(meta, results), encoding="utf-8")
    top1 = sum(1 for q in todo if results.get(f"{q['zim']}|{q['id']}", {}).get("rank") == 1)
    print(f"saved {out_path} | {len(results)} results | right article #1 for {top1}/{len(todo)} of this run's queries"
          + (f" | median {statistics.median(times):.0f} ms" if times else ""))


if __name__ == "__main__":
    main()
