# Evaluation: how good is search?

This folder measures **how often a search engine puts the right article first**, using a fixed set of
queries whose correct answers are known. It works with any version of Zimantic, with Kiwix's own
search, and with new ZIM files and languages you add later.

Unit tests (`tests/`) check that the code does what it says. This checks whether search actually got
better. A ranking rule can pass its tests and still make results worse for most users, especially in
languages the author didn't try. Run this before and after any change that affects ranking or indexing.

**All queries are synthetic.** They were written or generated for testing (with AI assistance), not
collected from real users, and the non-English queries are translations of the English ones. The set is
best for **comparing two versions of Zimantic on the same ZIMs and model**: it shows reliably whether a
change made ranking better or worse, and in which languages and query types. It is not a measure of how
satisfied real users are. See [Limitations](#limitations).

## What it measures

For every query: the position of the right article in the first 20 results. From that, per run:

| Metric | Meaning |
|---|---|
| **#1** | share of queries where the right article is the first result |
| **Top 5** | right article within the first 5 results |
| **Top 20** | right article anywhere in the first 20 results (if not here, no reranking can fix it) |
| **MRR** | mean reciprocal rank: 1 for #1, ½ for #2, ⅓ for #3 … 0 when missing; rewards "almost right" |
| **± (95%)** | margin of error on #1 for that many queries |
| **Fixed / broken** | queries a run gets right at #1 that the baseline didn't, and the reverse. An average can hide a change that fixes 40 searches and breaks 69 |
| **Median / p95 ms** | search time over HTTP |

Every number can be broken down by **language**, **query type** (`set`), **ZIM** and **suite**, or any
combination (`--by lang set zim zim+lang`).

## The queries

`queries/<zim name>/<suite>.<lang>.jsonl`, one query per line:

```json
{"id": "readme/question/019", "zim": "wikipedia_en_medicine_maxi_2026-04", "suite": "readme", "set": "question",
 "lang": "en", "query": "what causes lockjaw", "answers": ["Tetanus"], "source": "hand"}
```

`answers` lists the article path(s) that count as correct (several when a ZIM has near-duplicates).
`source` says how the query was made: `hand`, `translated` or `generated`.

Included (6,132 queries, 5 ZIMs, up to 20 languages):

| ZIM | Suites | What they cover |
|---|---|---|
| `wikipedia_en_medicine_maxi_2026-04` (WikiMed) | `readme`, `words`, `random-targets` | the README's sets (titles, questions, misspellings, descriptions without the name, alternate names, first sentences, sentences deep in an article) in 7 languages; 15 common words in 15 languages; random articles in 7 languages |
| `wikipedia_en_astronomy_maxi_2025-11` | `random-targets` | 70 random articles: names, questions, descriptions in 7 languages, plus misspellings and copied sentences |
| `cooking.stackexchange.com_en_all_2026-07` | `original`, `random-targets` | titles, keywords, reworded questions, misspellings, 12 languages, sentences from questions and answers |
| `ted_mul_ocean_2026-09` | `random-targets` | 70 random talks, 7 languages |
| `wikibooks_en_all_nopic_2026-01` | `hand`, `generated` | hand-written questions, keywords, misspellings; generated sets |

Languages: en, es, fr, pt, de, it, zh, ja, ru, ar, hi, sw, ha, am, so, af, yo, ig, zu, rw, plus `mul`
(30 mixed-language queries).

How many queries each language has decides how much you can read from it. The last column is how far
a language's #1 score (on its test queries) could move by chance alone, in percentage points. With 8
queries, each one is worth 12.5 points, so one lucky or unlucky query swings the score a lot. A
difference between two versions smaller than this number can't be told apart from luck.

| Languages | Queries (dev / test) | Where they come from | #1 score could vary by chance (±) |
|---|---|---|---|
| en | 707 / 1,696 | every ZIM and suite | ±2 points |
| es, fr, ar, hi, sw, zh | ~150 / ~430 each | translations of the WikiMed `readme` sets, `random-targets` on WikiMed, cooking and TED, 15 cooking titles, 15 single words | ±5 points |
| pt | 11 / 19 | 15 cooking titles + 15 single words | ±25 points |
| de, it, ja, ru | 4 / 11 each | 15 cooking titles only | ±30 points |
| af, am, ha, ig, rw, so, yo, zu | 7 / 8 each | 15 single medical words only (`words` suite) | ±35 points |
| mul | 12 / 18 | 30 mixed-language WikiMed queries | ±25 points |

Only English and the six large languages (es, fr, ar, hi, sw, zh) support conclusions on their own. The
small sets are **smoke checks**: they show whether a language works at all (e.g. a change that returns
nothing for Yoruba), not whether it got a few points better or worse. To judge a small language, add
queries for it first (see [Adding a language](#adding-a-language)).

Query sources: 3,699 translated, 1,658 generated from the articles, 775 hand-written.

### dev and test

Every query is assigned to **dev** (~30%) or **test** (~70%) by its id; all language versions of a query
share one split. Tune settings while looking at `--split dev`; report results with `--split test`. This
keeps a setting from being fitted to the very queries used to judge it.

## Running it

Start the engines you want to measure, then:

```bash
# a Zimantic server (current versions serve /search; v0.1.x served /api/search)
python evaluation/run.py --engine zimantic --url http://localhost:8090 --label my-branch --commit abc1234

# Kiwix's full-text search; --zim-dir lets redirects in its results be followed
python evaluation/run.py --engine kiwix --url http://localhost:8080 --label kiwix-serve --zim-dir ./zims

# compare (the first file is the baseline)
python evaluation/compare.py evaluation/results/main.json evaluation/results/my-branch.json --split test
```

`run.py` finds the server's API itself. It probes `/api/zims` (v0.1.x), `/zims`, `/library.json` and
`/sources` in that order, so it works with current versions and older ones; it reads `/config` when the
server has it. Only ZIMs the server reports as **local and searchable** are measured: remote Kiwix
catalog books and readable-but-unindexed archives are skipped, and so are queries for ZIMs the server
does not have (listed in the output). Newer servers also report which source each hit came from; hits
from another ZIM are ignored and counted in a warning, because a server that does not recognise the
`zim` filter would otherwise silently search its whole library.

The server caps how many results it keeps per query (`max_results` in `/config`, 100 by default) and how
long a query may be; `run.py` lowers `--k` to the cap and leaves out over-long queries instead of
recording a truncated or failed run. It says so when it does either.

Useful options for `run.py`: `--zims`, `--suites`, `--sets`, `--langs` (run a subset), `--split`,
`--k` (results requested, default 20), `--append` (add ZIMs served by another server to the same
results file), `--results-dir` (write the results somewhere other than `evaluation/results`). Results
are saved as `<results-dir>/<label>.json`.

For a fair comparison, both runs must use the same ZIM files and, when a change affects how indexes are
built, indexes built by each version's own code. Note it in the PR if they differ.

### Reproducing the published results

`results/v0.1.0-lite.json` (Zimantic `a043177`, 2026-10-10) and `results/kiwix-serve.json` are the
reference runs. To compare against them, use exactly:

- **ZIM files** (from [library.kiwix.org](https://library.kiwix.org); names must match exactly):
  `wikipedia_en_medicine_maxi_2026-04.zim`, `wikipedia_en_astronomy_maxi_2025-11.zim`,
  `cooking.stackexchange.com_en_all_2026-07.zim`, `ted_mul_ocean_2026-09.zim`,
  `wikibooks_en_all_nopic_2026-01.zim`

  Keep them in the git-ignored `evaluation.zims/` folder rather than the normal `zims/`, and run a
  separate server for measurements (`zim_dir = "./evaluation.zims"`, `index_dir = "./evaluation.indexes"`)
  so the scored source set is only the reference ZIMs. The astronomy `2025-11` and wikibooks `2026-01`
  files are no longer published on Kiwix's server; the nearest releases on it are
  `wikipedia_en_astronomy_maxi_2026-08` and `wikibooks_en_all_nopic_2026-04`, whose article paths do
  not all match the queries. Ask the query-set author for the exact files, or regenerate queries for
  the replacement releases (see [Adding a ZIM](#adding-a-zim)).
- **Model:** multilingual-e5-small, int8 ONNX from
  [Xenova/multilingual-e5-small](https://huggingface.co/Xenova/multilingual-e5-small)
  (`onnx/model_quantized.onnx` saved as `model.onnx`), 384 dimensions, with the tokenizer from
  [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small):

  | File | SHA-256 |
  |---|---|
  | `model.onnx` | `f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193` |
  | `sentencepiece.bpe.model` | `cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865` |

- **Indexes** built from those ZIMs with that model by the version being tested, with default build
  settings. An index built by one model and searched with another gives meaningless results without an
  error, so rebuild after changing the model.
- **Config:** defaults, except paths and port. Record any other setting you changed.

Write the commit, ZIM names, model hashes and non-default settings in the PR along with the
`compare.py` output. Search times depend on the machine and its load: compare timings only between runs
made on the same machine, close together.

### Rules for comparisons

For anyone, person or coding agent, changing ranking, indexing, query handling, the model or search
settings:

- **Tune on dev, report on test.** Look only at `--split dev` while trying settings, then report
  `--split test` once. If you tune again after seeing test results, say so.
- **Run everything:** every ZIM and every language, with `--by zim lang set`, not only English.
- **Report fixed / broken counts** and results by language and ZIM, not only averages. Call out anything
  that got worse.
- If a change filters results (e.g. a score cutoff), report how often queries return fewer results than
  requested, or none.
- A difference smaller than the ± margin is not a result. Small languages are smoke checks only.
- **Don't edit, delete or regenerate existing query files** to make a change pass, and don't change
  `DEV_SHARE` or the split. Add new queries in new files with new ids.

`run.py` turns `localhost` into `127.0.0.1`: on Windows, `localhost` first tries IPv6, and against a
server listening on IPv4 only every request then waits ~2 seconds, which would distort the timings.

## Adding a ZIM

1. Generate automatic sets (alternate names, first sentences, sentences deep in an article). It samples
   random entries rather than reading the whole file, so it also works on very large ZIMs (e.g. the
   full English Wikipedia, ~100 GB):
   ```bash
   python evaluation/make_queries.py generate --zim zims/<name>.zim --n 200
   ```
   Use `--lang` if the ZIM's text isn't English, and `--seed` for a different sample.
2. Optionally add hand-written queries as `queries/<name>/hand.en.jsonl`. These matter most: real
   questions, descriptions without the name, misspellings. Ids must be unique within the ZIM.
3. Check every answer exists in the ZIM:
   ```bash
   python evaluation/make_queries.py check --zim-dir zims
   ```

**Very large ZIMs:** the evaluation itself is light (it only sends queries over HTTP). The slow part is
building the Zimantic index for the ZIM. Use `--zims`, `--sets` or `--split dev` for quick checks.

**New ZIM versions:** queries are tied to an exact ZIM file name, because article paths can change
between releases. For a new release, regenerate the generated sets and run `check` on the hand-written
ones; copy over those whose answers still exist.

## Adding a language

```bash
python evaluation/make_queries.py translate --zim wikipedia_en_medicine_maxi_2026-04 --suite readme --to pt
```

This writes `queries/<zim>/readme.pt.jsonl` with each English query in `source_query` and an empty
`query` to fill in. Empty lines are skipped (and counted) until they are translated, so the file can be
filled in gradually. Translations keep the English query's id with `@pt` appended, so results can be
compared query by query across languages. Language codes are free-form; reports group by whatever
codes appear.

## Reading the results

- **Small groups are noisy.** #1 is accurate to roughly ±100/√n points: ±20 for 25 queries, ±10 for
  100, ±5 for 400. Don't read much into a 5-point change in a 25-query set.
- **Look at fixed / broken**, not only averages.
- **Look at every language**, not only English. Zimantic's biggest advantage over Kiwix is outside English.
- **Top 20 is the ceiling** for any reranking: a reranker can only reorder what the first stage found.
- **Kiwix can't search some ZIMs.** kiwix-serve refuses multi-language ("mul") ZIMs such as TED; the
  report lists them as unsupported instead of scoring them 0%.

## Limitations

- **Synthetic queries.** The queries were written for testing, with AI assistance; the non-English
  queries are translations, not searches by native speakers. Real users phrase things differently.
  Real user queries would be a valuable addition.
- **Generated queries come from the articles.** They are sentences taken from the articles themselves,
  with the title words removed. They test some things well (matching remembered text) and others not at
  all (questions), and they favour word matching.
- **One right answer per query.** A different article that also answers the query counts as a miss, so
  scores slightly undercount quality.
- **Mostly English content.** Four of the five ZIMs are English. "Multilingual" here means a query in
  another language finding an English article; searching non-English ZIMs is not tested.
- **Small language sets.** See the table under [The queries](#the-queries): many languages have 8–19
  test queries, enough to notice that a language stopped working, not to measure small changes.
- **Built alongside Zimantic's main branch.** The query sets were written while developing main, so they
  may suit its design. Keep this in mind when judging a very different approach.
- **Search quality also depends on the index:** a ZIM indexed with a different reader, build settings or
  model gives different results with the same code.

## Licenses

The generated queries quote text from Wikipedia, Wikibooks and Stack Exchange, which is licensed under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/): attribution to their contributors,
via the Kiwix ZIM files named in each query. The TED queries describe talks in our own words. The code
in this folder is GPL-3.0-or-later, like the rest of Zimantic.
