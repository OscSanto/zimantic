"""Create and check evaluation queries. Needs libzim (already a Zimantic dependency).

    # automatic query sets for a new ZIM (works on very large ZIMs: it samples, never scans everything)
    python evaluation/make_queries.py generate --zim zims/wikipedia_en_all_maxi_2026-09.zim --n 200

    # a translation template for a new language: fill in each "query", empty lines are skipped until then
    python evaluation/make_queries.py translate --zim wikipedia_en_medicine_maxi_2026-04 --suite readme --to pt

    # validate every query file; with --zim-dir also check that every answer still exists in its ZIM
    python evaluation/make_queries.py check --zim-dir zims

Generated sets (English sentences taken from the articles themselves, the article's title words removed):
  alias           a redirect's title, which should find the article it points to (alternate names)
  first sentence  the article's first sentence
  deep in article a sentence from the middle of the article (tests full-text style matching)
"""
import argparse
import html as htmllib
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import queries as Q  # noqa: E402

PARAGRAPH = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
JUNK = re.compile(r"[{}]|mw-parser-output|display\s*:|function\s*\(|https?://")
SETS = ("alias", "first sentence", "deep in article")


def paragraphs(html: str):
    out = []
    for p in PARAGRAPH.findall(html):
        text = " ".join(htmllib.unescape(re.sub(r"<[^>]+>|\[\d+\]", "", p)).split())
        if len(text) >= 80 and not JUNK.search(text):
            out.append(text)
    return out


def first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s+", text)[0]


def without_title(sentence: str, title: str) -> str:
    words = set(re.findall(r"\w+", title.casefold()))
    return " ".join(w for w in sentence.split() if re.sub(r"\W", "", w.casefold()) not in words)


def follow(entry, hops=10):
    for _ in range(hops):
        if not entry.is_redirect:
            return entry
        entry = entry.get_redirect_entry()
    return None


def is_article(entry) -> bool:
    return not entry.is_redirect and entry.get_item().mimetype.startswith("text/html")


def generate(args):
    from libzim.reader import Archive
    zim_path = Path(args.zim)
    zim, name, rng = Archive(str(zim_path)), zim_path.stem, random.Random(args.seed)
    rows, counts, budget = [], Counter(), args.n * args.attempts
    wanted = [s for s in SETS if s in args.sets]
    while budget and any(counts[s] < args.n for s in wanted):
        budget -= 1
        entry = zim._get_entry_by_id(rng.randrange(zim.entry_count))
        if entry.is_redirect:
            if "alias" not in wanted or counts["alias"] >= args.n:
                continue
            title = entry.title.strip()
            target = follow(entry)
            if (not target or not is_article(target) or "/" in title or title.isdigit()
                    or title.casefold() == target.title.casefold() or title == target.path):
                continue
            rows.append({"set": "alias", "query": title, "answers": [target.path]}); counts["alias"] += 1
            continue
        if not is_article(entry) or not ({"first sentence", "deep in article"} & set(wanted)):
            continue
        paras = paragraphs(bytes(entry.get_item().content).decode("utf-8", "ignore"))
        if "first sentence" in wanted and counts["first sentence"] < args.n and paras:
            q = without_title(first_sentence(paras[0]), entry.title)
            if len(q) >= 30:
                rows.append({"set": "first sentence", "query": q[:200], "answers": [entry.path]}); counts["first sentence"] += 1
        deep = paras[3:]
        if "deep in article" in wanted and counts["deep in article"] < args.n and deep:
            q = without_title(first_sentence(deep[len(deep) // 2]), entry.title)
            if len(q) >= 40:
                rows.append({"set": "deep in article", "query": q[:200], "answers": [entry.path]}); counts["deep in article"] += 1
    per_set = Counter()
    for row in rows:
        row.update({"id": f"{args.suite}/{row['set']}/{per_set[row['set']]:03d}", "zim": name, "suite": args.suite,
                    "lang": args.lang, "source": "generated"})
        per_set[row["set"]] += 1
    out = Path(args.queries) / name / f"{args.suite}.{args.lang}.jsonl"
    Q.write(out, rows)
    print(f"wrote {out}: " + ", ".join(f"{s} {counts[s]}" for s in wanted)
          + ("" if all(counts[s] >= args.n for s in wanted) else f"  (budget ran out; raise --attempts)"))


def translate(args):
    folder = Path(args.queries) / args.zim
    source = [json.loads(l) for l in (folder / f"{args.suite}.{args.source_lang}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.sets:
        source = [r for r in source if r["set"] in args.sets]
    out = folder / f"{args.suite}.{args.to}.jsonl"
    existing = {}
    if out.exists():
        existing = {json.loads(l)["id"]: json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()}
    rows = []
    for r in source:
        rid = f"{Q.base_id(r['id'])}@{args.to}"
        done = existing.get(rid, {}).get("query", "")
        rows.append({"id": rid, "zim": r["zim"], "suite": r["suite"], "set": r["set"], "lang": args.to,
                     "query": done, "answers": r["answers"], "source": "translated", "source_query": r["query"]})
    Q.write(out, rows)
    todo = sum(1 for r in rows if not r["query"])
    print(f"wrote {out}: {len(rows)} lines, {todo} still to translate (fill in \"query\"; empty lines are skipped)")


def check(args):
    qs, skipped = Q.load(Path(args.queries))
    print(f"{len(qs)} queries, {skipped} untranslated template lines")
    by = Counter((q["zim"], q["suite"], q["lang"]) for q in qs)
    for (zim, suite, lang), n in sorted(by.items()):
        print(f"  {zim:45} {suite:16} {lang:4} {n:5}")
    splits = Counter(q["split"] for q in qs)
    print(f"split: dev {splits['dev']}, test {splits['test']}")
    if args.zim_dir:
        from libzim.reader import Archive
        problems = 0
        for zim in sorted({q["zim"] for q in qs}):
            file = Path(args.zim_dir) / f"{zim}.zim"
            if not file.exists():
                print(f"  {zim}: ZIM not found in {args.zim_dir}, answers not checked"); continue
            archive = Archive(str(file))
            bad = sorted({a for q in qs if q["zim"] == zim for a in q["answers"]
                          if not archive.has_entry_by_path(a) or not is_article(archive.get_entry_by_path(a))})
            problems += len(bad)
            print(f"  {zim}: {'all answers found' if not bad else f'{len(bad)} answers missing or not articles, e.g. {bad[:3]}'}")
        sys.exit(1 if problems else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queries", default=str(Q.QUERIES))
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="automatic query sets from a ZIM file")
    g.add_argument("--zim", required=True, help="path to the .zim file")
    g.add_argument("--n", type=int, default=200, help="queries per set (default 200)")
    g.add_argument("--sets", nargs="*", default=list(SETS), choices=SETS)
    g.add_argument("--suite", default="generated")
    g.add_argument("--lang", default="en", help="language of the ZIM's text (default en)")
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--attempts", type=int, default=400, help="random entries tried per wanted query before giving up")
    t = sub.add_parser("translate", help="translation template for a new language")
    t.add_argument("--zim", required=True, help="ZIM name (folder under queries/)")
    t.add_argument("--suite", required=True)
    t.add_argument("--to", required=True, help="language code of the new language, e.g. pt")
    t.add_argument("--source-lang", default="en")
    t.add_argument("--sets", nargs="*", help="only these sets")
    c = sub.add_parser("check", help="validate query files")
    c.add_argument("--zim-dir", help="folder with the .zim files, to check that answers exist")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    {"generate": generate, "translate": translate, "check": check}[args.cmd](args)


if __name__ == "__main__":
    main()
