# Search evaluation

This folder contains a fixed search-quality evaluation. It measures the rank
of the target article in the first 20 results for Zimantic and Kiwix search
on configured ZIM files.

Unit tests (`tests/`) check code behavior. This evaluation measures ranking
quality. A ranking rule can pass its tests and reduce search quality,
especially for languages with few queries. Use this evaluation for ranking
and indexing work.

All queries are synthetic. They were written or generated for testing,
with AI assistance. They do not come from real users. The non-English queries
are translations of English queries. Use the set to **compare two runs with
the same ZIM files and model**. The report shows ranking differences by
language and query type. It does not measure user satisfaction. See
[Limitations](#limitations).

## What it measures

For each query, the harness records the target article's position in the first
20 results and calculates these metrics for each run:

| Metric | Description |
|---|---|
| **#1** | Share of queries where the target article is the first result |
| **Top 5** | Share of queries where the target article is in the first 5 results |
| **Top 20** | Share of queries where the target article is in the first 20 results; reranking cannot find an article outside this set |
| **MRR** | Mean reciprocal rank: 1 for #1, ½ for #2, ⅓ for #3, and 0 when missing |
| **± (95%)** | margin of error on #1 for that many queries |
| **Fixed / broken** | Queries that reach #1 in one run but not the baseline, and vice versa |
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

The query count limits the conclusions for each language. The last column
shows the chance variation in the #1 test score, in percentage points. With
8 queries, each result is worth 12.5 points. A difference smaller than the
listed margin is inconclusive.

| Languages | Queries (dev / test) | Where they come from | #1 score could vary by chance (±) |
|---|---|---|---|
| en | 707 / 1,696 | every ZIM and suite | ±2 points |
| es, fr, ar, hi, sw, zh | ~150 / ~430 each | translations of the WikiMed `readme` sets, `random-targets` on WikiMed, cooking and TED, 15 cooking titles, 15 single words | ±5 points |
| pt | 11 / 19 | 15 cooking titles + 15 single words | ±25 points |
| de, it, ja, ru | 4 / 11 each | 15 cooking titles only | ±30 points |
| af, am, ha, ig, rw, so, yo, zu | 7 / 8 each | 15 single medical words only (`words` suite) | ±35 points |
| mul | 12 / 18 | 30 mixed-language WikiMed queries | ±25 points |

Only English and the six large languages (es, fr, ar, hi, sw, zh) support
conclusions on their own. The small sets are **smoke checks**: they show whether
a language works at all (for example, whether a search returns results for
Yoruba), not whether its score differs by a few points. Build a larger query
set before drawing conclusions about a small language (see
[Building a language query set](#building-a-language-query-set)).

Query sources: 3,699 translated, 1,658 generated from the articles, 775 hand-written.

### dev and test

Every query is assigned to **dev** (~30%) or **test** (~70%) by its id; all language versions of a query
share one split. Tune settings while looking at `--split dev`; report results with `--split test`. This
keeps a setting from being fitted to the very queries used to judge it.

## Running it

Start the engines you want to measure, then:

```bash
# Start a Zimantic server; select one query-file ZIM and its key from /sources
python evaluation/run.py --engine zimantic --url http://localhost:8090 \
  --label my-branch --commit abc1234 --zims archive_name --source-key '<canonical key>'

# Kiwix's full-text search; --zim-dir lets redirects in its results be followed
python evaluation/run.py --engine kiwix --url http://localhost:8080 --label kiwix-serve --zim-dir ./zims

# compare (the first file is the baseline)
python evaluation/compare.py evaluation/results/main.json evaluation/results/my-branch.json --split test
```

`run.py` uses the Zimantic API. It reads `/sources?provider=zim`
and `/config`, and requires the selected source to be **searchable**.
Every result must carry a canonical source key; hits from another source are
ignored and reported in a warning. Deprecated endpoints and source-name aliases
are not supported.

The server caps how many results it keeps per query (`max_results` in `/config`, 100 by default) and how
long a query may be; `run.py` lowers `--k` to the cap and leaves out over-long queries instead of
recording a truncated or failed run. It says so when it does either.

Useful options for `run.py`: `--zims`, `--suites`, `--sets`, `--langs` (run a subset), `--split`,
`--k` (results requested, default 20), `--append` (add ZIMs served by another server to the same
results file), `--results-dir` (write the results somewhere other than `evaluation/results`). Results
are saved as `<results-dir>/<label>.json`.

Servers expose canonical source keys rather than archive filenames.
Every Zimantic run requires exactly one `--zims` name and its `--source-key`.
Map the query file's ZIM name explicitly:

```bash
uv run python evaluation/run.py --engine zimantic --url http://127.0.0.1:8090 \
  --label small-zim --zims communitybuilding.stackexchange.com_en_all_2026-08 \
  --source-key '<key from /sources>'
```

This sends the canonical `source` filter and checks each hit against that key;
display titles are not archive identities. Use the same label with `--append`
to combine evaluations of multiple ZIMs.

For a fair comparison, both runs use the same ZIM files. Each run also uses
indexes built by its own code when the index format or build behavior differs.
Record any index difference with the comparison.

### Diversity and representative browsing

This section records the four-way experiment behind the unified control. Production code exposes one adaptive Diversity order,
which is also the empty-query default: semantic MMR for items with usable
embeddings, lexical diversity otherwise. The four variants below are comparison
controls, not separate production controls.

`diversity.py` compares four variants: Diversity, Representative,
corrected Diversity, and Semantic diversity. Capture the baseline, then reuse
the same output directory:

```bash
# Use the same four archive paths, model and output directory for every phase.
uv run python evaluation/diversity.py --phase baseline \
  --model /path/to/model/int8 --output evaluation/results/diversity-quality \
  --zims /path/to/communitybuilding.stackexchange.com_en_all_2026-08.zim \
         /path/to/communitybuilding.stackexchange.com_en_all_2025-08.zim \
         /path/to/alpinelinux_en_all_nopic_2025-09.zim \
         /path/to/alcohol.stackexchange.com_en_all_2025-06.zim
# For the lexical order, repeat with --phase lexical.
# For the semantic order, repeat with --phase semantic.
```

The baseline phase uses ranking code from commit
`9e40f8de20de605e2553434530ee84953002e90c` with this harness. The harness
rejects a phase whose ranking code does not match, rather than silently
measuring a different algorithm. The separate corrected lexical and semantic
orders are at `bc8c8f2`. To measure the unified order, use `--phase adaptive`
with the same arguments; this writes `adaptive.json` and includes it in the
comparison. It can also create fresh pools when no baseline run exists.

The adaptive order and the four-way run produce identical top-20 lists for all
eight semantic queries. These archives are fully embedded; mixed/partial indexes and
fast-mode lexical fallback are covered separately by regression tests.

The harness builds isolated indexes, freezes the hybrid query pools,
and records model, index, ranking-code and pool hashes. Every phase uses
identical inputs. It writes `baseline.json`, `lexical.json`, `semantic.json`,
`comparison.json`, and `qualitative.json` with top-five article excerpts.
These are ignored local outputs, not changes to the configured library.

#### Measures

There is deliberately no single composite "diversity quality" number:

| Measure | Interpretation |
|---|---|
| Reference coverage, higher better | Mean best cosine match from a fixed sample of up to 256 reference articles per source to the selected top 20; selected articles are excluded from the reference, and sources have equal weight |
| Semantic repetition, lower better | Mean cosine similarity between each selected item and its most similar other selected item; catches a repeated subset that mean pairwise distance can hide |
| Title repetition, lower better | Same nearest-neighbor measure using non-stopword title-word Jaccard overlap |
| Centrality | Mean similarity to each selected item's source centroid; a representativeness guardrail, not an article-quality judgement |
| Source balance | Source counts and normalized source entropy; equal exposure is desirable for unscoped browsing, not necessarily for a focused query |
| Query relevance retention | Fraction of the baseline hybrid top 20 retained, plus mean baseline relevance rank; a regression proxy, not labelled relevance |

Reference coverage across the whole corpus is useful for empty queries.
It must **not** be maximized for a focused query: adding unrelated topics can
raise that number while making search worse. Query comparisons consider
repetition together with relevance retention and the actual articles.

#### Four-way comparison, 2026-11-09

Inputs: the four archives named above, 6,512 embeddings, the local int8 E5 model
(fingerprint `cee53c88b877c344c7ddf0327772421d047718bfcfdda0e9f8aea282841845dc`),
two ONNX/FAISS threads, `candidate_count=16`, and a 100-item pool. The two
Community Building editions are deliberately separate sources to stress
cross-source repetition; a normal library may collapse editions.

Five browsing scopes were measured: all four archives together and each archive
alone. Eight mixed-source queries used identical frozen candidate pools:
`community`, `online community`, `moderation`, `water`, `beer`, `linux`,
`software`, and `installation`. All quality measures use the top 20. Numbers
below are macro averages across scopes or queries. Warm time is the median
across cases of two warm repetitions after one warm-up repetition.

| Empty-query browsing | Coverage ↑ | Semantic repetition ↓ | Title repetition ↓ | Centrality | Warm ms |
|---|---:|---:|---:|---:|---:|
| Diversity | 0.8667 | 0.8640 | 0.0110 | 0.9019 | 24,532.7 |
| Representative | 0.8932 | 0.9618 | 0.4466 | 0.9556 | 1.72 |
| Corrected Diversity | 0.8879 | 0.8670 | **0.0000** | 0.9065 | 39.07 |
| Semantic diversity | 0.8914 | **0.8547** | 0.0880 | 0.9074 | 30.14 |

Every mixed-source empty-query top 20 contained five items from each source.
Representative has slightly higher reference coverage, but much greater
repetition. Corrected Diversity removes title overlap cheaply; Semantic
diversity gets nearly Representative's coverage with less semantic repetition
than either lexical variant.

| Nonempty queries | Semantic repetition ↓ | Title repetition ↓ | Original top 20 retained ↑ | Mean original rank ↓ | Warm ms |
|---|---:|---:|---:|---:|---:|
| Diversity | 0.9156 | 0.3344 | 14.4% | 48.23 | 6.82 |
| Representative | 0.9507 | 0.5029 | 8.8% | 64.99 | 0.19 |
| Corrected Diversity | **0.8717** | **0.0177** | 23.1% | 43.04 | 2.56 |
| Semantic diversity | 0.8942 | 0.3524 | **81.3%** | **13.19** | 2.03 |

Semantic diversity intentionally sacrifices some novelty to keep relevant
matches. Shared query words also increase title overlap even when articles
cover different subtopics. It is not the winner of every metric.

#### Qualitative findings

The following counts use mixed-source top-20 results; excerpts were read for
the top five in browsing and the `community`, `linux`, and `beer` queries.

| Variant | Browse user-profile pages | Extra identical titles for `community` | First result for `linux` |
|---|---:|---:|---|
| Diversity | 0 | 7 | Alpine Install: from a disc to a old computer single only boot |
| Representative | 15 | 5 | User ccleve |
| Corrected Diversity | 1 | 0 | Alpine Linux in a chroot |
| Semantic diversity | 3 | 1 | Alpine Linux in a chroot |

Diversity favors long titles and repeated matching articles across
the two Community Building editions. Corrected Diversity does not privilege
long titles or sources with larger matching pools. It checks overlap globally,
keeps relevance order on novelty ties, and postpones stopword-only titles.
Incremental maximum-overlap updates and stopping at the required browse
prefix avoid the cubic full-sample ranking cost.

A query objective that uses query cosine alone puts an unrelated beer-site
user profile first for `linux`. The semantic diversity objective averages query cosine
with max-normalized hybrid relevance, then uses equal-weight MMR against
maximum similarity to already selected items. This keeps existing title
boosts and profile penalties. Its first Linux results cover chroots, iSCSI,
hardening, speech synthesis, voicemail, and bootstrapping. Corrected lexical
Diversity starts well too, but later includes Community Building navigation
pages because their titles add novel words.

For browsing, Semantic diversity mixes central candidates with a deterministic
random sample, balances source exposure, and considers redundancy across
sources. It selects distinct material in place of most repeated profiles, but can
start with **User Richard** and retain three profile pages. Both semantic
browsing modes select **Setting up the build environment 1.7**, whose excerpt
explicitly calls it obsolete. Content quality and freshness are separate from
centrality or diversity, and this comparison does not add media-type
exclusions. Adaptive Diversity is the production default; Representative is a
comparison variant.

These are English, small-corpus, unsupervised smoke benchmarks, not a release
gate or an independent relevance evaluation. Coverage and semantic repetition
use the same embeddings as the algorithms and can reward boilerplate;
the profile-page examples show why qualitative review matters.
Timings exclude model loading, query retrieval/embedding, previews, and HTTP,
and were measured on a shared host without confidence intervals. Existing
vectors suffice for the adaptive sort; production indexes need no rebuild.

### Small-ZIM body-index comparison

```bash
uv run python scripts/bench_body_quality.py \
  zims/communitybuilding.stackexchange.com_en_all_2026-08.zim \
  --output /tmp/body-quality
```

The harness uses this runner and `compare.py`, the same dev/test split,
and real HTTP search with previews and final ranking. It compares HEAD,
compact contentless FTS5, and native-only body retrieval in both normal hybrid
search and a semantic-disabled diagnostic. It reuses the 20 labelled Community
Building questions from `tests/benchmarks/community_queries.json`, derives
exact-title, keyword and misspelling queries, and uses `make_queries.py` for
20 first-sentence and 20 deep-sentence queries: 120 fixed English queries total.
It writes indexes, query files, result files, method/config metadata,
selected-split comparison reports, and answer-rank changes (`qualitative.json`)
under the output directory, and stops its isolated servers afterwards.
Caches are disabled; all variants use the same model, thread budget and
query set. This intentionally overrides the multi-ZIM/multilingual release
scope below: it is a quick qualitative smoke comparison, not a release gate.

HEAD is the positional control. Production indexes use compact Unicode61 with
`remove_diacritics 2`.

For storage/BM25 selection, then tokenizers on the preferred layout:

```bash
uv run python scripts/bench_body_quality.py \
  zims/communitybuilding.stackexchange.com_en_all_2026-08.zim \
  --output /tmp/body-content --report-split dev \
  --variants compact stored stored-default head-bm25 stored-bm25

uv run python scripts/bench_body_quality.py \
  zims/communitybuilding.stackexchange.com_en_all_2026-08.zim \
  --output /tmp/body-tokenizers --report-split dev \
  --variants compact compact-unicode61 compact-porter compact-ascii trigram
```

The first selected variant is the baseline. `stored` preserves the
Unicode61 diacritic setting while changing storage; `stored-default` uses
literal `fts5(text)`. BM25 controls retain positions and stored token counts.
Trigram retains full detail because its compact form rejects our long query
tokens. `tokenizer-probes.json` records synthetic capability differences, not
labelled accuracy.

Freeze choices using dev, then run `evaluation/compare.py --split test` on the
saved results with the same generated `--queries` directory; no rebuild or
server is needed. For example:

```bash
uv run python evaluation/compare.py \
  /tmp/body-content/results/compact-hybrid.json \
  /tmp/body-content/results/stored-default-hybrid.json \
  /tmp/body-content/results/head-bm25-hybrid.json \
  --queries /tmp/body-content/queries --split test --by set suite
```

Measure build/provider costs separately without repeatedly loading the model:

```bash
uv run python scripts/bench_body_tradeoffs.py \
  zims/communitybuilding.stackexchange.com_en_all_2026-08.zim \
  --output /tmp/body-runtime \
  --variants head compact stored-default compact-porter compact-ascii trigram \
  --build-repeats 3 --full-repeats 0 --query-repeats 3
```

Use `--queries` to reuse a provider-probe JSON file. These timings
exclude query embeddings, previews, and HTTP; relevance comes from the quality
harness, not the probe-overlap counts. See the
[measured storage/ranking/tokenizer comparison](../docs/build-performance.md#default-stored-content-versus-contentless-including-bm25).

### Reference results

`results/v0.1.0-lite.json` (Zimantic `a043177`, 2026-10-10) and
`results/kiwix-serve.json` are reference runs. Reproduce them with these inputs:

- ZIM files (from [library.kiwix.org](https://library.kiwix.org); names must match exactly):
  `wikipedia_en_medicine_maxi_2026-04.zim`, `wikipedia_en_astronomy_maxi_2025-11.zim`,
  `cooking.stackexchange.com_en_all_2026-07.zim`, `ted_mul_ocean_2026-09.zim`,
  `wikibooks_en_all_nopic_2026-01.zim`

  Keep them in the git-ignored `evaluation.zims/` folder rather than the normal `zims/`, and run a
  separate server for measurements (`zim_dir = "./evaluation.zims"`, `index_dir = "./evaluation.indexes"`)
  so the scored source set is only the reference ZIMs. The astronomy `2025-11` and wikibooks `2026-01`
  files are not available from Kiwix's server; the nearest releases are
  `wikipedia_en_astronomy_maxi_2026-08` and `wikibooks_en_all_nopic_2026-04`, whose article paths do
  not all match the queries. Ask the query-set author for the exact files, or regenerate queries for
  other releases (see
  [Building a ZIM query set](#building-a-zim-query-set)).
- Model: multilingual-e5-small, int8 ONNX from
  [Xenova/multilingual-e5-small](https://huggingface.co/Xenova/multilingual-e5-small)
  (`onnx/model_quantized.onnx` saved as `model.onnx`), 384 dimensions, with the tokenizer from
  [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small):

  | File | SHA-256 |
  |---|---|
  | `model.onnx` | `f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193` |
  | `sentencepiece.bpe.model` | `cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865` |

- Indexes built from those ZIMs with that model by the version being tested, with default build
  settings. An index built by one model and searched with another gives meaningless results without an
  error, so rebuild after changing the model.
- Config: use defaults except for paths and port. Record other settings.

Write the commit, ZIM names, model hashes and non-default settings in the PR along with the
`compare.py` output. Search times depend on the machine and its load: compare timings only between runs
made on the same machine, close together.

### Comparison rules

Use these rules to evaluate ranking, indexing, query handling, models, and
search settings:

- Tune on dev, report on test. Look only at `--split dev` while trying settings, then report
  `--split test` once. If you tune again after seeing test results, say so.
- Run everything: every ZIM and every language, with `--by zim lang set`, not only English.
- Report fixed and broken counts and results by language and ZIM, not only averages. Call out anything
  that got worse.
- If a run filters results (for example, with a score cutoff), report how often
  queries return fewer results than requested or no results.
- A difference smaller than the ± margin is not a result. Small languages are smoke checks only.
- Keep existing query files, `DEV_SHARE`, and the split fixed. Put additional
  queries in separate files with unique ids.

`run.py` uses `127.0.0.1` for `localhost` URLs because Windows tries IPv6
first. Each request to an IPv4-only server otherwise waits about 2 seconds
and distorts the timings.

## Building a ZIM query set

1. Generate automatic sets (alternate names, first sentences, sentences deep in an article). It samples
   random entries rather than reading the whole file, so it also works on very large ZIMs (e.g. the
   full English Wikipedia, ~100 GB):
   ```bash
   python evaluation/make_queries.py generate --zim zims/<name>.zim --n 200
   ```
   Use `--lang` if the ZIM text is not English, and `--seed` for a different sample.
2. Optionally add hand-written queries as `queries/<name>/hand.en.jsonl`. These matter most: real
   questions, descriptions without the name, misspellings. Ids must be unique within the ZIM.
3. Check every answer exists in the ZIM:
   ```bash
   python evaluation/make_queries.py check --zim-dir zims
   ```

Very large ZIMs: the evaluation itself is light (it only sends queries over HTTP). The slow part is
building the Zimantic index for the ZIM. Use `--zims`, `--sets` or `--split dev` for quick checks.

ZIM releases: Queries use an exact ZIM file name because article paths can
differ between releases. For another release, regenerate the generated sets
and run `check` on the hand-written sets. Keep answers that exist in the file.

## Building a language query set

```bash
python evaluation/make_queries.py translate --zim wikipedia_en_medicine_maxi_2026-04 --suite readme --to pt
```

This writes `queries/<zim>/readme.pt.jsonl`. Each English query appears in
`source_query`, and `query` is empty for translation. The runner skips and
counts empty queries, so the file supports partial translation. Each
translation keeps the English query ID with `@pt` appended. This lets reports
compare the same query across languages. Language codes are free-form; reports
group by the codes in the files.

## Reading the results

- Small groups are noisy. #1 is accurate to roughly ±100/√n points: ±20 for
  25 queries, ±10 for 100, and ±5 for 400. A 5-point score difference in a
  25-query set is inconclusive.
- **Look at fixed / broken**, not only averages.
- Look at every language, not only English. Zimantic's biggest advantage over Kiwix is outside English.
- Top 20 is the ceiling for any reranking: a reranker can only reorder what the first stage found.
- Kiwix cannot search some ZIMs. kiwix-serve refuses multi-language ("mul") ZIMs such as TED; the
  report lists them as unsupported rather than scoring them 0%.

## Limitations

- Synthetic queries. The queries were written for testing, with AI
  assistance. The non-English queries are translations, not searches by native
  speakers. They do not represent real user queries.
- Generated queries come from the articles. They are sentences taken from the articles, with title
  words removed. They test matching article text, but do not test natural-language questions well.
  They also favor word matching.
- One target article per query. Another article that also answers the query counts as a miss, so
  scores slightly undercount quality.
- Mostly English content. Four of the five ZIMs are English. "Multilingual" here means a query in
  another language finding an English article; searching non-English ZIMs is not tested.
- Small language sets. See the table under [The queries](#the-queries): many languages have 8–19
  test queries, enough to detect when search returns no results, but not small score differences.
- Query-set bias. The query sets reflect Zimantic's design and may favor
  similar approaches. Consider this when evaluating a different design.
- Search quality also depends on the index: a ZIM indexed with a different reader, build settings or
  model gives different results with the same code.

## Licenses

The generated queries quote text from Wikipedia, Wikibooks and Stack Exchange, which is licensed under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/): attribution to their contributors,
via the Kiwix ZIM files named in each query. The TED queries describe talks in our own words. The code
in this folder is GPL-3.0-or-later, like the rest of Zimantic.
