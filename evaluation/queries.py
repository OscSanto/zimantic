"""Query files: loading, validation and the dev/test split. Standard library only.

Layout: evaluation/queries/<zim name>/<suite>.<lang>.jsonl, one query per line:

    {"id": "readme/question/019", "zim": "wikipedia_en_medicine_maxi_2026-04", "suite": "readme",
     "set": "question", "lang": "en", "query": "what causes lockjaw", "answers": ["Tetanus"],
     "source": "hand"}

- id       stable, unique within the ZIM. Translations reuse the English id with "@<lang>" appended,
           so all language versions of a query share one base id (and one split).
- answers  ZIM paths of the article(s) that count as a correct result.
- source   hand | translated | generated (how the query was made; reported, never used for scoring).
- query    may be empty in a translation template; empty queries are skipped and counted.

Any new language or ZIM is just more files in this layout: nothing else needs to change.
"""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).parent
QUERIES = HERE / "queries"
DEV_SHARE = 0.3   # queries for tuning settings; the rest are for reporting (see README "dev and test")
FIELDS = ("id", "zim", "suite", "set", "lang", "query", "answers", "source")


def base_id(query_id: str) -> str:
    return query_id.split("@", 1)[0]


def split_of(query_id: str) -> str:
    """dev or test, fixed by the base id so every language version of a query lands in the same split."""
    digest = hashlib.sha256(base_id(query_id).encode("utf-8")).digest()
    return "dev" if digest[0] / 256 < DEV_SHARE else "test"


def load(root: Path = QUERIES, zims=None, suites=None, sets=None, langs=None, split="all"):
    """All non-empty queries matching the filters (each filter: None = everything, else a collection).
    Returns (queries, skipped) where skipped counts empty (untranslated) template lines."""
    queries, skipped, seen = [], 0, set()
    for path in sorted(Path(root).glob("*/*.jsonl")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            q = json.loads(line)
            missing = [f for f in FIELDS if f not in q]
            if missing:
                raise ValueError(f"{path}:{n}: missing {missing}")
            if not q["query"].strip():
                skipped += 1
                continue
            key = (q["zim"], q["id"])
            if key in seen:
                raise ValueError(f"{path}:{n}: duplicate id {q['id']} for {q['zim']}")
            seen.add(key)
            q["split"] = split_of(q["id"])
            if (zims and q["zim"] not in zims) or (suites and q["suite"] not in suites) \
                    or (sets and q["set"] not in sets) or (langs and q["lang"] not in langs) \
                    or (split != "all" and q["split"] != split):
                continue
            queries.append(q)
    return queries, skipped


def write(path: Path, rows):
    """Write rows as JSON Lines (UTF-8, one compact object per line, readable non-ASCII)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps({k: row[k] for k in FIELDS if k in row} | {k: v for k, v in row.items() if k not in FIELDS and k != "split"},
                               ensure_ascii=False) + "\n")
