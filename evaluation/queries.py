"""Query files: loading, validation and the dev/test split. Standard library only.

Layout: evaluation/queries/<zim name>/<suite>.<lang>.jsonl, one query per line:

    {"id": "curated/question/019", "zim": "wikipedia_en_medicine_maxi_2026-04", "suite": "curated",
     "type": "question", "lang": "en", "query": "what causes lockjaw", "answers": ["Tetanus"],
     "source": "hand", "split": "test"}

- id       <suite>/<type>/<number>, unique within the ZIM. Translations reuse the English id with
           "@<lang>" appended, so all language versions of a query share one base id (and one split).
- suite    how the target articles were picked: curated (by hand) | random (at random).
- type     what kind of query it is, in kebab-case (see TYPES and the README).
- answers  ZIM paths of the article(s) that count as a correct result.
- source   hand | translated | generated (how the query was made; reported, never used for scoring).
- split    dev | test. Stored in the row so renaming an id never moves a query between splits;
           new queries without it get one from their base id (split_of).
- query    may be empty in a translation template; empty queries are skipped and counted.

Any new language or ZIM is just more files in this layout: nothing else needs to change.
"""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).parent
QUERIES = HERE / "queries"
DEV_SHARE = 0.3   # queries for tuning settings; the rest are for reporting (see README "dev and test")
FIELDS = ("id", "zim", "suite", "type", "lang", "query", "answers", "source", "split")
REQUIRED = FIELDS[:-1]   # split may be missing in a new row; it is then computed
SUITES = ("curated", "random")
TYPES = {   # the query types, and what each one tests
    "exact-title": "the article's exact title",
    "name": "the article's name, or a short phrase naming it",
    "keywords": "a few words a person might type",
    "misspelled": "a title or keywords with typing mistakes",
    "question": "a plain-language question the article answers",
    "reworded-question": "a question asked with different words than the article's title",
    "description": "a description of the subject without its name",
    "alternate-name": "another name for the subject (often a redirect's title)",
    "mixed-language": "a query in another language than the ZIM, in a mixed-language set",
    "single-word": "one common word, in many languages",
    "first-sentence": "the article's first sentence, with its title words removed",
    "deep-sentence": "a sentence from the middle of the article",
    "copied-sentence": "a sentence copied from the article",
    "question-body-sentence": "a sentence from a Q&A question's body",
    "best-answer-sentence": "a sentence from a Q&A question's best answer",
    "other-answer-sentence": "a sentence from another answer to a Q&A question",
}


def base_id(query_id: str) -> str:
    return query_id.split("@", 1)[0]


def split_of(query_id: str) -> str:
    """dev or test, fixed by the base id so every language version of a query lands in the same split."""
    digest = hashlib.sha256(base_id(query_id).encode("utf-8")).digest()
    return "dev" if digest[0] / 256 < DEV_SHARE else "test"


def load(root: Path = QUERIES, zims=None, suites=None, types=None, langs=None, split="all"):
    """All non-empty queries matching the filters (each filter: None = everything, else a collection).
    Returns (queries, skipped) where skipped counts empty (untranslated) template lines."""
    queries, skipped, seen = [], 0, set()
    for path in sorted(Path(root).glob("*/*.jsonl")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            q = json.loads(line)
            missing = [f for f in REQUIRED if f not in q]
            if missing:
                raise ValueError(f"{path}:{n}: missing {missing}")
            if not q["query"].strip():
                skipped += 1
                continue
            key = (q["zim"], q["id"])
            if key in seen:
                raise ValueError(f"{path}:{n}: duplicate id {q['id']} for {q['zim']}")
            seen.add(key)
            if q["suite"] not in SUITES or q["type"] not in TYPES:
                raise ValueError(f"{path}:{n}: unknown suite {q['suite']!r} or type {q['type']!r}")
            q.setdefault("split", split_of(q["id"]))
            if (zims and q["zim"] not in zims) or (suites and q["suite"] not in suites) \
                    or (types and q["type"] not in types) or (langs and q["lang"] not in langs) \
                    or (split != "all" and q["split"] != split):
                continue
            queries.append(q)
    return queries, skipped


def write(path: Path, rows):
    """Write rows as JSON Lines (UTF-8, one compact object per line, readable non-ASCII)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            row = dict(row, split=row.get("split") or split_of(row["id"]))
            f.write(json.dumps({k: row[k] for k in FIELDS if k in row} | {k: v for k, v in row.items() if k not in FIELDS},
                               ensure_ascii=False) + "\n")
