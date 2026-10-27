"""Compare one or more evaluation runs. The first results file is the baseline.

    python evaluation/compare.py results/v0.1.0-lite.json results/kiwix.json
    python evaluation/compare.py results/main.json results/my-branch.json --by lang set --split test

For each run: share of queries with the right article at #1, in the top 5 and in the top 20, mean
reciprocal rank (MRR: 1 for #1, 1/2 for #2, ... 0 when not in the results), a 95% margin of error on #1,
search time (median, p95), and how many queries a run fixed and broke at #1 compared with the baseline.
Prints Markdown (paste-able into a pull request); --out writes it to a file.
"""
import argparse
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import queries as Q  # noqa: E402


def metrics(ranks):
    """ranks: list of int|None (1-based). -> dict of shares and MRR."""
    n = len(ranks)
    if not n:
        return None
    at = lambda k: sum(1 for r in ranks if r is not None and r <= k) / n
    p = at(1)
    return {"n": n, "top1": p, "top5": at(5), "top20": at(20), "mrr": sum(1 / r for r in ranks if r) / n,
            "margin": 1.96 * math.sqrt(p * (1 - p) / n)}


def pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", help="results JSON files; the first is the baseline")
    ap.add_argument("--queries", default=str(Q.QUERIES))
    ap.add_argument("--zims", nargs="*"); ap.add_argument("--suites", nargs="*"); ap.add_argument("--sets", nargs="*"); ap.add_argument("--langs", nargs="*")
    ap.add_argument("--split", choices=("all", "dev", "test"), default="all")
    ap.add_argument("--by", nargs="*", default=["lang", "set", "zim"],
                    help="breakdowns, each a field or fields joined by '+', e.g. lang set zim zim+lang suite+set+lang")
    ap.add_argument("--out", help="also write the report to this file")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    qs, _ = Q.load(Path(args.queries), args.zims, args.suites, args.sets, args.langs, args.split)
    runs = []
    for path in args.results:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        runs.append((data["meta"]["label"], data["meta"], data["results"]))
    names = [label for label, _, _ in runs]

    # Score every run on the same queries. A run that covered fewer ZIMs (e.g. a ZIM no longer
    # downloadable) would otherwise be compared on a different, easier or harder, query set.
    # ZIMs an engine refuses (meta "unsupported") don't count against the other runs.
    def answered(run, q):
        return q["zim"] in run[1].get("unsupported", {}) or f"{q['zim']}|{q['id']}" in run[2]
    shared = [q for q in qs if all(answered(run, q) for run in runs)]
    left_out = sorted({q["zim"] for q in qs} - {q["zim"] for q in shared})
    note = (f"{len(qs) - len(shared)} queries left out because not every run has them"
            + (f" (ZIMs: {', '.join(left_out)})" if left_out else "") + ".") if len(shared) < len(qs) else ""
    qs = shared
    if not qs:
        sys.exit("no queries in common between these runs")

    def ranks(run, subset):
        _, meta, res = run
        out = []
        for q in subset:
            key = f"{q['zim']}|{q['id']}"
            if q["zim"] in meta.get("unsupported", {}) or key not in res:
                continue
            out.append(res[key]["rank"])
        return out

    lines = [f"## Evaluation: {', '.join(names)}", "",
             f"{len(qs)} queries ({args.split} split). Baseline: **{names[0]}**. "
             "#1 / top 5 / top 20 = share of queries with the right article at that position or better.", ""]
    if note:
        lines += [f"**Note:** {note}", ""]

    # overall
    lines += ["### Overall", "", "| Run | Queries | #1 | Top 5 | Top 20 | MRR | ± (#1, 95%) | Median ms | p95 ms | Fixed / broken vs baseline |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    base_rank = {f"{q['zim']}|{q['id']}": runs[0][2].get(f"{q['zim']}|{q['id']}", {}).get("rank") for q in qs}
    for i, run in enumerate(runs):
        m = metrics(ranks(run, qs))
        times = [run[2][f"{q['zim']}|{q['id']}"]["ms"] for q in qs if f"{q['zim']}|{q['id']}" in run[2]]
        if i == 0:
            flips = "–"
        else:
            fixed = broke = 0
            for q in qs:
                key = f"{q['zim']}|{q['id']}"
                if key not in run[2] or key not in runs[0][2]:
                    continue
                now, before = run[2][key]["rank"] == 1, base_rank[key] == 1
                fixed += now and not before; broke += before and not now
            flips = f"{fixed} / {broke}"
        unsupported = ", ".join(run[1].get("unsupported", {}))
        name = run[0] + (f" (unsupported: {unsupported})" if unsupported else "")
        if m:
            scores = f"{m['n']} | {pct(m['top1'])} | {pct(m['top5'])} | {pct(m['top20'])} | {m['mrr']:.3f} | ±{100 * m['margin']:.1f}"
        else:
            scores = "0 | n/a | n/a | n/a | n/a | n/a"
        timing = f"{statistics.median(times):.0f} | {sorted(times)[int(0.95 * (len(times) - 1))]:.0f}" if times else "n/a | n/a"
        lines.append(f"| {name} | {scores} | {timing} | {flips} |")
    lines.append("")

    # breakdowns
    for by in args.by:
        fields = by.split("+")
        groups = {}
        for q in qs:
            groups.setdefault(tuple(q[f] for f in fields), []).append(q)
        lines += [f"### By {' × '.join(fields)} (#1 / top 5 / top 20)", "",
                  "| " + " | ".join(fields) + " | n | " + " | ".join(names) + " |",
                  "|" + "---|" * (len(fields) + 1 + len(names))]
        for key in sorted(groups):
            cells = []
            for run in runs:
                m = metrics(ranks(run, groups[key]))
                cells.append("n/a" if not m else f"{100 * m['top1']:.0f} / {100 * m['top5']:.0f} / {100 * m['top20']:.0f}")
            lines.append("| " + " | ".join(key) + f" | {len(groups[key])} | " + " | ".join(cells) + " |")
        lines.append("")

    lines += ["Small groups are noisy: with n queries, #1 is accurate to roughly ±100/√n points (n=25: ±20, n=100: ±10, n=400: ±5)."]
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
