# Build performance and quality

This ports the ideas from [OscSanto/zimantic#3](https://github.com/OscSanto/zimantic/pull/3),
credited to OscSanto, into the current provider-neutral index pipeline rather
than cherry-picking the older ZIM-only builder.

## Advisory index size goal

Aim to keep each source's total published index size (`index.sqlite` plus
`index.faiss`, when present) **below 10% of the original source file size**.
This is an optimization goal, not a hard cap: preserve search coverage and
quality rather than silently truncating content, disabling channels, or
rejecting a build to meet it. Models, the shared catalog, optional preview
caches, and transient staging/retained generations are separate storage costs;
budget for those too.

Report the index-to-source byte ratio alongside build time and search quality.
Small, highly compressed text archives can exceed the goal substantially:
the 6,331,350-byte Community Building ZIM measured 79.1% at HEAD `a27dd69`,
56.1% with compact body FTS, and 46.3% with native-only body retrieval.
None meets the goal; removing body FTS alone is insufficient on this fixture.

## Body FTS alternatives and search quality

The benchmark variants change the body channel only. Title FTS and semantic
vectors remain unchanged. New production indexes now use compact Unicode61
with `remove_diacritics 2`. Existing positional indexes remain readable and
resume-compatible; rebuilding is needed to obtain the smaller layout. In this
comparison, HEAD denotes the historical positional control, which the harness
continues to reproduce explicitly rather than using the moving production
default.

| Variant | Conventional description | SQLite options |
|---|---|---|
| HEAD | Positional contentless FTS5 inverted index | [`content=''`](https://www.sqlite.org/fts5.html#contentless_tables), [`detail=full`](https://www.sqlite.org/fts5.html#the_detail_option), [`columnsize=1`](https://www.sqlite.org/fts5.html#the_columnsize_option); the latter two are defaults |
| Compact | Non-positional, document-id-only contentless FTS5 inverted index | `content=''`, [`detail=none`](https://www.sqlite.org/fts5.html#the_detail_option), [`columnsize=0`](https://www.sqlite.org/fts5.html#the_columnsize_option) |
| Native-only | Archive-native full-text retrieval (Xapian, through libzim) | No SQLite body table; [libzim full-text search](https://libzim.readthedocs.io/en/latest/usage.html#full-text-searching-in-entries) |

HEAD and compact use the same [`unicode61`](https://www.sqlite.org/fts5.html#unicode61_tokenizer)
tokenizer with `remove_diacritics 2`. The SQLite body query performs exact-token
Boolean AND matching, not BM25 ordering. Its candidates and native candidates
are fused by reciprocal rank fusion (RRF). Compact drops positions and stored
token counts unused by that query; it must use table-level MATCH, and cannot
support SQLite phrase/NEAR queries, column filters, or normal length-aware BM25
with this contentless configuration.

The earlier 48-query provider-only probe changed 32 top-10 lists under
native-only: 10 were reorderings of the same members, 22 changed membership,
and only 7 changed the first result. This is not a count of relevance failures,
and those probes did not include semantic retrieval or final HTTP ranking.

The follow-up uses the [existing evaluation method](../evaluation/README.md#small-zim-body-index-comparison)
on the same small archive, real HTTP search and a fixed synthetic query set.
There are 120 English queries, 41 dev and 79 test; no settings were tuned.
All variants have identical embedding vectors. Reported results are test-only:

| Mode / variant | #1 | Top 5 | Top 20 | MRR | Fixed / broken at #1 vs HEAD |
|---|---:|---:|---:|---:|---:|
| Hybrid HEAD | 79.7% | 92.4% | 96.2% | 0.854 | baseline |
| Hybrid compact | 79.7% | 92.4% | 96.2% | 0.854 | 0 / 0 |
| Hybrid native-only | 75.9% | 87.3% | 93.7% | 0.812 | 0 / 3 |
| Lexical HEAD | 58.2% | 63.3% | 63.3% | 0.604 | baseline |
| Lexical compact | 58.2% | 63.3% | 63.3% | 0.604 | 0 / 0 |
| Lexical native-only | 51.9% | 57.0% | 58.2% | 0.539 | 0 / 5 |

Compact matches every stored top-20 list in both modes, including dev queries.
For a deep sentence mentioning Zulip and LaTeX, the expected Slack-alternatives
question falls from #1 to #8 under native-only hybrid search; without semantic
retrieval it disappears. The moderator-permissions question likewise drops
from #1 to outside the hybrid top 20. A second lost hybrid target is a user
profile, not a question; these generated labels are not exhaustive relevance
judgements.

The four lost lexical deep-sentence targets expose a tokenizer/query mismatch:
normalization turns contractions into fragments (`didn't` -> `didn`,
`doesn't` -> `doesn`, `We're` -> `we re`, `don't` -> `don`). SQLite matches those
fragments, but the native index does not match them in the target documents.
The native index does find the underlying articles for other terms, including
Zulip and DARPANet. These losses are not proof that native retrieval intrinsically
lacks the article text; a native-only approach also needs compatible query
handling. That follow-up was not tuned on these test queries.

Treat the rates as a small-ZIM smoke comparison: labels are synthetic and
incomplete, generated sentences can be awkward or include profiles, and
aggregate differences are smaller than this sample's reported uncertainty.
Concrete regressions still identify behavior to preserve.

### Default stored content versus contentless, including BM25

[`content=''` is not the default](https://www.sqlite.org/fts5.html#external_content_and_contentless_tables).
Ordinary `fts5(text)` stores a second copy of the indexed text in its content
shadow table. This supports raw text reads, snippets/highlights, and normal
updates/deletes. Classic contentless tables return NULL for text, cannot supply
snippets, and have update/delete restrictions. Our published indexes are rebuilt,
and previews still read the archive: this experiment does **not** implement
preview reuse from stored FTS text.

Raw content, positions, and document lengths are separate choices:

| Body layout | Raw text | Positions | Stored token counts | Useful SQLite BM25 |
|---|---|---|---|---|
| HEAD contentless | No | Yes | Yes | Yes |
| Compact contentless | No | No | No | No; the probe returns tied `-0.0` scores |
| Default stored content | Yes | Yes | Yes | Yes |

HEAD already has the information needed for
[`bm25(body)`](https://www.sqlite.org/fts5.html#the_bm25_function), but its current
body query does not use it. The BM25 experiment changes only candidate ordering:
`ORDER BY bm25(body), rowid` (SQLite's better scores are more negative), followed
by the existing RRF and final ranking. It retains the same Boolean AND query;
this is not a redesign of retrieval or fusion.

First compare compact against `stored`, holding `unicode61 remove_diacritics 2`
constant to isolate storage, plus their positional BM25 controls. Separately,
`stored-default` uses literal `fts5(text)`, including bare default Unicode61.
Both stored variants match **all 120 compact top-20 lists in both modes**.
Stored content itself adds no quality here.

Compact remained preferred on the 41 dev queries: all storage/ranking variants
tied at 85.4% hybrid #1, but BM25 reduced lexical #1 from 68.3% to 63.4%
(0 fixed, 2 broken). With choices frozen, the 79 held-out test queries give:

| Mode / body variant | #1 | Top 5 | Top 20 | MRR | Fixed / broken #1 vs compact |
|---|---:|---:|---:|---:|---:|
| Hybrid compact / either stored variant | 79.7% | 92.4% | 96.2% | 0.854 | 0 / 0 |
| Hybrid contentless or stored BM25 | 79.7% | 91.1% | 96.2% | 0.846 | 0 / 0 |
| Lexical compact / either stored variant | 58.2% | 63.3% | 63.3% | 0.604 | 0 / 0 |
| Lexical contentless or stored BM25 | 57.0% | 62.0% | 63.3% | 0.587 | 0 / 1 |

BM25 improves no labelled test answer rank: four hybrid ranks worsen and five
lexical ranks worsen. For the generated first-sentence query about finding a
common chat venue, BM25 promotes the chat-rooms tag listing above the actual
Slack question in the body channel. After fusion, the intended question moves
from #1 to #3 lexical and #2 to #4 hybrid. The listing contains related material;
single-answer labels do not judge every alternative's relevance.

A dedicated warm SQL-only sweep on the same physical indexes (48 queries,
three trials of 20 repetitions, limit 16) measured median body queries at
**0.0175 ms unranked versus 0.0335 ms BM25** for contentless HEAD; stored content
was effectively identical at 0.0174/0.0336 ms. That small extra cost does not
materially change HTTP latency: test hybrid medians were 112-115 ms across the
storage/BM25 controls. These HTTP timings are single evaluation passes, not
counterbalanced latency trials. There is no measured BM25 quality improvement
on this fixture, not evidence that BM25 is generally worse.

### Tokenizers and repeated build/runtime measurements

The second stage compares the dev-preferred compact layout across
[Unicode61](https://www.sqlite.org/fts5.html#unicode61_tokenizer),
[Porter](https://www.sqlite.org/fts5.html#porter_tokenizer), and
[ASCII](https://www.sqlite.org/fts5.html#ascii_tokenizer). The
[trigram](https://www.sqlite.org/fts5.html#the_trigram_tokenizer) control is
contentless with **full detail**, `columnsize=0`: SQLite rejects query tokens
longer than three characters with trigram `detail=none`. It is substring
retrieval, not the same word-token compact layout, and cannot match terms shorter
than three characters. Query normalization was not changed to accommodate it.

Unicode61 `remove_diacritics 2` was retained before inspecting test results.
Bare Unicode61 and ASCII tied on English dev; Porter and trigram worsened #1.
Held-out results:

| Body tokenizer | Hybrid #1 / MRR | Hybrid fixed / broken #1 | Lexical #1 / MRR | Lexical fixed / broken #1 |
|---|---:|---:|---:|---:|
| Unicode61, `remove_diacritics 2` | 79.7% / 0.854 | baseline | 58.2% / 0.604 | baseline |
| Bare Unicode61 (`remove_diacritics 1`) | 79.7% / 0.854 | 0 / 0 | 58.2% / 0.604 | 0 / 0 |
| Porter over Unicode61 `remove_diacritics 2` | 77.2% / 0.839 | 0 / 2 | 55.7% / 0.589 | 0 / 2 |
| ASCII | 79.7% / 0.854 | 0 / 0 | 58.2% / 0.604 | 0 / 0 |
| Trigram, full detail | 77.2% / 0.825 | 0 / 2 | 53.2% / 0.552 | 0 / 4 |

Bare Unicode61 and ASCII match all 120 stored top-20 lists here, but synthetic
capability probes expose differences that this English archive cannot score:
Unicode61's default diacritic mode misses `ộ` when queried as `o`; mode 2
matches. ASCII misses accent folding and Unicode punctuation/emoji boundaries.
Porter matches `corrected`/`correction`, but also conflates
`university`/`universe`. Trigram matches `keep` inside `bookkeeping`, but misses
`AI`. These probes describe semantics, not an additional relevance score.
Porter also moves the misspelled rules-question target from #1 to #2 hybrid;
one of its two lost hybrid targets is a generated user-profile label.

On the same four-logical-CPU, warm-cache host, rotated fresh-process lexical
builds (three per variant) and 48-query provider trials (three processes, three
repetitions each) measured:

| Body variant | Lexical build median | Full build, single observation | Full SQLite MiB | SQLite + FAISS / ZIM | Warm helper-provider median |
|---|---:|---:|---:|---:|---:|
| HEAD contentless, full detail | 9.52 s | 28.51 s | 2.56 | 79.1% | 3.62 ms |
| Compact Unicode61, mode 2 | 9.48 s | 28.63 s | 1.17 | 56.1% | 3.60 ms |
| Default stored FTS5 / Unicode61 | 9.63 s | 29.59 s | 7.17 | 155.4% | 3.62 ms |
| Compact Porter | 9.45 s | 28.99 s | 1.07 | 54.5% | 3.59 ms |
| Compact ASCII | 9.43 s | 28.78 s | 1.19 | 56.3% | 3.67 ms |
| Contentless trigram, full detail | 9.96 s | 29.25 s | 10.71 | 214.0% | 3.65 ms |

Full-build observations come from the HTTP study; the HEAD-layout observation
used BM25, which does not affect building. Do not infer a full-build speedup
from these single observations. Lexical-build SQLite files omit about 0.10 MiB
of vector metadata; the sizes above include it and the identical 2.21 MiB FAISS.
Provider timings exclude embedding, previews and HTTP. Trigram body SQL was
about 0.194 ms versus compact's 0.067 ms in those provider trials, despite
similar total provider medians.

Default storage costs about **6.1 times compact's SQLite space** for identical
results; small build/runtime differences are not compelling. Porter saves
about 8% of SQLite bytes but loses labelled #1 results. Keep compact Unicode61
mode 2 as the measured candidate, not ASCII or trigram; none reaches the
advisory 10% total-index goal. Compact is now the default for new indexes;
candidate matching and ranking remain unchanged.

## Review and libzim API verification

- The storage-order API claim is true for **python-libzim 4.0.0**, but false for
  **4.1.0**, released November 5, 2026. Its
  [tagged reader interface](https://github.com/openzim/python-libzim/blob/v4.1.0/libzim/reader.pyi#L67-L72)
  exposes `iter_efficient()` and `__iter__()` (its alias). The
  [binding implementation](https://github.com/openzim/python-libzim/blob/v4.1.0/libzim/libzim.pyx#L1362-L1386)
  delegates to libzim's `iterEfficient()`: internally its efficient iterator
  maps positions through `getIndexByClusterOrder`. `iter_by_path()` and
  `iter_by_title()` are not storage-order iterators; `find_by_*()` selects
  prefix ranges. The efficient ordering is intentionally unspecified publicly.
- Use that native iterator, not the PR's raw entry-table parser. Canonical row
  ids and the original entry cap stay unchanged. Record the chosen order and
  its digest; reject a changed native ordering on resume rather than applying
  a saved storage ordinal to id order. Older interrupted builds/bindings keep
  id order. Existing completed indexes are not invalidated just for this change.
- The PR's 512-*article* windows could retain arbitrarily many redirects or
  title-only rows. Retain bounded *row* windows and the existing shared body
  budget instead. Flush/drain before waiting for budget held by the same build.
- The PR's hardware fallback could turn exhausted available RAM into half of
  total RAM, and its root-only cgroup checks missed parent limits. Clamp
  exhaustion to zero, inspect leaf/ancestor limits, and respect CPU affinity.
  Use physical cores where detected (logical-core fallback elsewhere), a
  conservative available-memory allowance, and a bounded shared model pool.
  This is not an OOM guarantee for arbitrary models or a substitute for Pi
  measurements. Existing low-memory arena/reclamation behavior is retained.

## Token batches and progress

Read/commit windows still use `batch_size`. Inside each window, preserve the
existing word-boundary truncation, encode final inputs once, sort by actual capped token
length, and schedule model calls with at most **1,000 padded input tokens** by
default (`embedding_batch_tokens`). Useful-token sums alone would underestimate
the workspace needed by padding. A single input exceeding the batch budget is
admitted alone, still capped by the model's per-input limit.

Workers share one model and can embed one source concurrently while its reader
continues. The default pending-window limit scales to workers + 1 (at least
two), so even one-call short-text windows can feed the whole pool. Ordered
commits still wait for earlier batches; read-ahead and the shared body budget
remain bounded. A deterministic regression holds one worker's first call,
requires all other workers to start, and verifies that a later batch finishes
before the straggler is released without committing out of order.
Equal token budgets make workspace sizes and scheduling more even,
not identical-cost: attention depends on sequence length and call overhead
still matters. Increase the token budget for GPU throughput.

Readable non-redirect entries retain native efficient order and form the
article phase. The cheap entries follow. Advance article progress only after
rows **and vectors** commit, including resumed work. Report redirects/assets
separately without an ETA; they no longer inflate the early article estimate.
The metadata-only order preparation and final FAISS publication are outside
that article ETA.

## Measurements: November 7, 2026

Linux x86-64, Intel i5-13600KF, 31.2 GiB RAM; CPU affinity restricted to CPUs
0–3 (two physical/four logical cores) for every run. These are **not Raspberry Pi results**. Local filesystem
cache was warm; no system caches were dropped on the shared host.

Complete `communitybuilding.stackexchange.com_en_all_2026-08.zim`:
**6,331,350 bytes**, 3,706 entries, 3,508 indexed rows, 1,504 vectors.
Default multilingual-e5-small int8 model, 256-token inputs, 32-row windows.
Before source revision: `3c4349b`. Archive SHA-256:
`33ba5516c9eb1fcb3da26fbd552cf6a3dda66f956469cf9e7f55e36abedcd424`.
Model graph SHA-256:
`f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193`.
python-libzim **4.1.0 was held constant** for the comparison; a preliminary
4.0.0 baseline was excluded. Build times include source reading, extraction,
embedding, SQLite/body postings, FAISS writing, and generation publication;
model loading/warm-up and quality evaluation are excluded.

| Metric | Before | Port |
|---|---:|---:|
| Median build time, two runs | 43.77 s | 23.93 s |
| Articles/s | 34.4 | 62.8 |
| Median peak process RSS | 881.5 MB | 471.0 MB |
| Embedding workers × threads/call | 1 × 4 | 3 × 1 |
| Recall@1, 20 labelled queries | 40% | 40% |
| Recall@5 | 65% | 60% |
| Recall@10 | 70% | 75% |
| MRR@10 | 0.4933 | 0.5113 |

**1.83× faster, 47% lower peak RSS.** Before times: 43.661/43.875 s;
after: 23.792/24.071 s. An additional final-code run took 23.914 s and
confirmed the same quality metrics using the actual published FAISS index.

The figures above describe revision `e0670ea`, with its original two-window
read-ahead limit. After scaling read-ahead with worker count, a sanity run took
23.559 s with 475.9 MB peak RSS; vectors, postings and query rankings were
identical to that revision. This single run is not a new median or evidence of
a general speedup; the straggler regression demonstrates the scheduling fix.

With the same native order, row window, sorting and 3×1 workers, fixed
**eight-text** calls took 26.716/26.396 s (median 26.56 s), with 647.4 MB
peak RSS. Token budgeting is **1.11× faster** and uses **27% less peak RSS**
than that ablation here. This separates its benefit from concurrent calls,
but is not a reproduction of every detail of the older PR.

SQLite title/path/redirect rows and **all lexical body postings are identical**.
Vector ids are identical; mean corresponding-vector cosine is **0.99730**
(minimum 0.99404). Top result agreement is **18/20**, and mean top-10 overlap
is **85%**. These are real int8 batch-neighbour effects, not identical search
results. The one-query decrease at rank 5 and increase at rank 10 do not
establish a general accuracy improvement. Labels are one relevant question per
query, not exhaustive relevance judgements; the measurement covers semantic
FAISS retrieval, not hybrid ranking or UI latency. Queries are embedded in one
20-query batch, held constant across comparisons; int8 results can differ from
single-query serving.

## Separate build and query thread budgets

Do not set one thread globally. `build_embed_threads` now controls build calls
and automatic embedding-worker sizing; `embed_threads` controls query serving.
Existing `embed_threads` overrides no longer affect builds. Both remain
independently configurable.

The following tuning used the same 6.33 MB archive and model. Desktop affinity
was CPUs 0–7: **four physical/eight logical cores**. The Pi was a **Pi 500,
four ARM Cortex-A76 cores, 8 GB RAM**, upgraded to zimantic 1.0.0 and libzim
4.1.0 before benchmarking. Its affinity was CPUs 0–2, leaving one core available
for other work, and benchmarks ran at lower priority. No caches were dropped.
ONNX Runtime was 1.31.0 on desktop and 1.31.1 on Pi; compare configurations
within each machine, not cross-platform accuracy gains.

### Embedding-stage sweep

Two repetitions of 512 evenly sampled, predecoded article excerpts, with
32-row windows and 1,000 padded-token calls. Timing includes token preparation
and the shared queue, but excludes archive reads, body extraction and index
writing. RSS is the peak of the whole benchmark process.

| Workers × threads | Desktop articles/s | Desktop RSS MB | Pi articles/s | Pi RSS MB |
|---|---:|---:|---:|---:|
| 1 × 1 | 39.16 | 341.0 | 7.09 | 340.7 |
| 2 × 1 | — | — | 12.25 | 439.8 |
| 3 × 1 | 103.12 | 469.2 | 13.85 | 468.2 |
| 4 × 1 | 132.12 | 521.3 | — | — |
| 8 × 1 | 137.27 | 654.6 | — | — |
| 4 × 2 | 125.66 | 488.5 | — | — |
| 2 × 3 | 130.70 | 434.8 | — | — |
| 1 × 2 | — | — | 11.81 | 340.8 |
| 1 × 3 | — | — | 14.02 | 340.3 |
| 1 × 8 | 137.46 | 341.7 | — | — |

This alone does **not** establish that independent calls are universally
faster. Single multithreaded calls matched throughput at lower RSS.

### Complete builds decide the tradeoff

Two fresh-process runs per configuration, counterbalanced A/B/B/A, including
reads, extraction, embeddings, SQLite/body postings and FAISS publication:

| Machine | Workers × threads | Median build seconds | Median peak RSS MB |
|---|---:|---:|---:|
| Desktop | 1 × 4 | 20.34 | 368.5 |
| Desktop | 4 × 1 | 16.26 | 503.4 |
| Pi 500 | 1 × 3 | 117.16 | 365.8 |
| Pi 500 | 3 × 1 | 115.28 | 471.0 |

Desktop parallel calls gave **25% higher throughput** (20% less wall time),
justifying the desktop build default of one thread per call. On Pi, the gain
was only **1.6%**, costing **29% more RSS**. The Pi 5/500 preset therefore uses
three build threads and one automatic model worker: approximately 105 MB less
RAM for a two-second difference on this archive. The Pi 5 recommendation is
based on the Pi 500's same CPU class, not a separate Pi 5 measurement.

Within each machine, all four runs had **bit-identical vectors and vector
ids**, identical lexical postings/title/path/redirect rows, and identical
query rankings. Desktop recall@1/@5/@10 was 40%/60%/75%, MRR@10 0.5113;
Pi was 50%/70%/75%, MRR@10 0.5631. The differing platform results are not a
thread-policy accuracy improvement. Pi temperature reached 49.1°C;
`vcgencmd get_throttled` remained `0x0`.

### Query latency

Four warm-ups per length, then 50 isolated queries on desktop or 30 on Pi.
These measure embedding latency, not the HTTP/hybrid-search path. Median
milliseconds with the affinities above:

| Threads | Desktop 12 tokens | Desktop 27 | Desktop 245 | Pi 12 | Pi 27 | Pi 245 |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 1.97 | 3.85 | 37.40 | 8.25 | 17.39 | 208.69 |
| 2 | 1.79 | 3.03 | 21.24 | 6.48 | 11.93 | 125.11 |
| 3 | 1.71 | 2.84 | 15.61 | 6.23 | 10.05 | 100.64 |
| 4 | 1.43 | 2.36 | 12.77 | — | — | — |
| 6 | 1.53 | 2.44 | 13.24 | — | — | — |
| 8 | 1.69 | 2.49 | 12.75 | — | — | — |
| 19 | 2.63 | 3.60 | 14.62 | — | — | — |

An additional desktop control with all 14 physical/20 logical cores available
confirmed the tradeoff: four threads took 1.60/2.39/13.20 ms, eight took
1.66/2.64/11.72 ms, and the former default of 19 took 2.57/3.50/12.30 ms.
Three threads were fastest on the shortest input (1.42 ms), but slower on the
long input (15.56 ms). Choose a desktop automatic cap of **four usable physical
cores, leaving one available** (minimum one thread), favoring typical short
searches; this yields **three on a four-core Pi**. Override to eight for a
near-limit-query workload. A blanket one-thread serving default is not supported.

The mobile and supercomputer profiles retain their previous per-call budgets
of two and eight, now separately named for builds and serving. Pi Zero 2 W and
large-core servers were **not** benchmarked here; these are not newly claimed
optima. A RAM-limited single model worker can still benefit from intra-call
parallelism, as the Pi 1×1 versus 1×2 sweep illustrates.

## Reproduce

The [labelled query set](../tests/benchmarks/community_queries.json) is specific
to that exact archive. Use a fresh output directory for each complete build:

```sh
taskset -c 0-3 uv run python scripts/bench_build.py \
  zims/communitybuilding.stackexchange.com_en_all_2026-08.zim \
  --queries tests/benchmarks/community_queries.json \
  --threads 1 --workers 3 --output /tmp/zimantic-after \
  --reference /tmp/zimantic-before
```

For the before checkout, pass `--source-root /path/to/before-checkout
--threads 4 --order id` to the same harness, omit `--reference`, and choose
`--output /tmp/zimantic-before`. The archive and installed dependencies must be
identical. `--fixed-batch 8` performs the fixed-size ablation on the port.
The Linux harness rejects archives >=80 MB and records timings, peak RSS,
canonical/posting digests, vectors, and retrieval quality in its output folder.

Thread tuning, with a fresh process per configuration:

```sh
taskset -c 0-2 python scripts/bench_threads.py \
  --model /library/zimantic/model --threads 3
taskset -c 0-2 python scripts/bench_threads.py \
  --model /library/zimantic/model --threads 1 --workers 3 \
  --zim communitybuilding.stackexchange.com_en_all_2026-08.zim
```

## HTML parser comparison: November 7, 2026

Text extraction prefers **selectolax's Lexbor HTML5 parser** for both body
indexing and previews, falling back to stdlib `HTMLParser` when Lexbor cannot
be imported. Block priorities, hidden/navigation filtering,
boilerplate removal, primary-language selection and text caps remain unchanged.
The small, marker-gated meta-refresh scan still uses stdlib `HTMLParser`.
Existing indexes are not automatically rewritten.

Automatic selectolax installation is restricted to the published CPython
wheel OS/architecture families: Linux x86-64/AArch64, macOS x86-64/ARM64, and
Windows x86/x64/ARM64. ARM32 and other unlisted targets do not require
selectolax or a native source build. A separately installed selectolax is
still used on any platform; `pip install 'zimantic[lexbor]'` explicitly opts
into installing/building it outside the automatic platform list. Platform
markers cannot distinguish every libc/OS-version/ABI wheel constraint.

The fallback is the previous streaming extractor measured below. It retains
the same visibility/language rules and capped text, and checks deadlines
between input chunks. It is slower and lacks Lexbor's HTML5 malformed-markup
recovery; the recorded legacy-page differences remain its known tradeoffs.

The [recorded results](../tests/benchmarks/results/html-parsers-2026-11-07.json)
compare the previous stdlib parser with native backends using the same
extraction rules: **242 real pages from 18 ZIMs and six module directories**
(10.60 MiB HTML), plus eight stress cases. Up to 12 pages per input mix random
pages with large pages from a sampled pool, not archive-wide maxima. There
are 186 nonempty real pages; all backends return empty text on the same other
56 HTML pages.

Linux x86-64, Python 3.13.12, selectolax 1.0.2, lxml 6.1.3/libxml 2.14.6,
Beautiful Soup 4.15.0. No CPU-affinity restriction or cache flushing on the
shared host. These are **not Raspberry Pi measurements**. Five timed repeats
follow warmup, with randomized backend order. Times are sums of per-page
medians and exclude archive decompression, file I/O, embeddings and index
writing; they are not complete-build or HTTP speedups.

| Backend | Body speedup | Preview speedup | Additional peak RSS | Preferred/tied quality cases |
|---|---:|---:|---:|---:|
| Previous stdlib HTMLParser | 1.00x | 1.00x | 8.0 MiB | 10/14 |
| lxml streaming target | 1.50x | 1.49x | 17.2 MiB | 13/14 |
| lxml DOM | 1.40x | 1.82x | 21.1 MiB | 12/14 |
| **Lexbor** | **1.44x** | **2.06x** | **24.8 MiB** | **14/14** |
| Beautiful Soup + lxml | 0.63x | 0.49x | 61.2 MiB | 13/14 |

The stdlib body/preview totals were 1.097/0.646 seconds; Lexbor took
0.763/0.313 seconds. Memory is a separate single fresh-process probe on the
eight largest corpus inputs, with all backends imported and inputs loaded
before measuring additional post-exec `VmHWM`. It includes native allocations,
but is not whole-application RSS or a concurrency/OOM guarantee.

All native alternatives exactly matched **240/242 real body and preview
outputs**, and none lost a previously nonempty real page. The two differences
were improvements: useful opening paragraphs were recovered from an old
resource-directory page, and repeated copyright/webmaster footer text was
removed from another legacy document.
The production Lexbor extractor was checked against the measured prototype:
body and preview hashes match on all 250 real/stress pages, as do the preserved
stdlib baseline's hashes against the original baseline.

A **blinded, source-based AI review** covered those two disagreements, four
random nonempty real pages and all eight stress cases. Lexbor was preferred or
tied in all 14. lxml-backed variants lost prose after malformed navigation;
lxml DOM also lost prose beyond its default nesting-depth limit. All backends
still produce a noisy dot-leader preview for one Wikisource table of contents:
that requires an extraction-policy fix, not another parser. This small,
disagreement-enriched review is not a human user study, an estimate of defect
prevalence, or proof of universal quality equivalence.

The production traversal retains bounded text accumulation and checks the
deadline before DOM construction and between traversal events. **Native DOM
construction itself cannot be interrupted**, and early preview stopping avoids
later traversal, not DOM allocation. The extra memory is the principal tradeoff
for Lexbor's HTML5 recovery and speed.

Reproduce with [the parser harness](../tests/benchmarks/run_html.py):

```sh
uv run --with lxml --with beautifulsoup4 python tests/benchmarks/run_html.py \
  --zim zims/wikipedia_en_100_2026-08.zim \
  --module modules/en-olpc --pages 12 --repeats 5 --seed 20261107 \
  --output /tmp/zimantic-html
```

Supply each of the recorded corpus sources with another `--zim` or `--module`
to reproduce the complete sample. The harness retains an explicit `stdlib`
baseline and exercises the production Lexbor path. Review `review.json` and
its source HTML before opening `review-key.json`; timings alone do not establish
extraction quality.

Without `--workers`, this measures query latency. In both benchmark scripts,
`--workers` means embedding workers, unlike the CLI's source-reader `--workers`.
