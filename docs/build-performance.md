# Build performance and search quality

This document records index-size, retrieval-quality, build-time, memory, and
query-latency measurements for the index pipeline.

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
the 6,331,350-byte Community Building ZIM measured 79.1% with contentless
positional body FTS (the current layout), 56.1% with the former compact layout,
and 46.3% with native-only body retrieval. None meets the goal; removing body
FTS alone is insufficient on this fixture.

## Body FTS alternatives and search quality

The benchmark variants differ only in the body-search channel. Title FTS and
semantic vectors are the same in every variant. Production indexes use a
contentless positional FTS5 body (`content=''`, `detail=full`, `columnsize=1`,
Unicode61 with `remove_diacritics 2`) ordered by `ORDER BY bm25(body), rowid`.
It keeps term positions and document lengths so BM25 can order candidates, but
never stores raw derived or native text. Production only reads and resumes the
active index schema and canonical metadata; `build` can reuse compatible
vectors from v0 root-level pairs while rebuilding the active schema.

| Variant | Conventional description | SQLite options |
|---|---|---|
| Contentless positional (production) | Positional contentless FTS5 inverted index with BM25 ordering | [`content=''`](https://www.sqlite.org/fts5.html#contentless_tables), [`detail=full`](https://www.sqlite.org/fts5.html#the_detail_option), [`columnsize=1`](https://www.sqlite.org/fts5.html#the_columnsize_option) |
| Compact (former default) | Non-positional, document-id-only contentless FTS5 inverted index | `content=''`, [`detail=none`](https://www.sqlite.org/fts5.html#the_detail_option), [`columnsize=0`](https://www.sqlite.org/fts5.html#the_columnsize_option) |
| Native-only | Archive-native full-text retrieval (Xapian, through libzim) | No SQLite body table; [libzim full-text search](https://libzim.readthedocs.io/en/latest/usage.html#full-text-searching-in-entries) |

Every SQLite variant uses the same
[`unicode61`](https://www.sqlite.org/fts5.html#unicode61_tokenizer) tokenizer
with `remove_diacritics 2`. The body query performs exact-token Boolean AND
matching, then fuses its candidates with native candidates by reciprocal rank
fusion (RRF). Compact drops positions and stored token counts, so its matches
tie at `-0.0` and come back in rowid order; it also cannot support SQLite
phrase/NEAR queries, column filters, or length-aware BM25. `detail=column`
stores a column bitmap and document lengths but no positions; it ties compact
exactly on the larger fixture below, so it is not a middle ground.

In a 48-query provider-only probe, native-only retrieval differs in 32 top-10
lists: 10 reorder the same results, 22 have different members, and 7 have a
different first result. These are not relevance-failure counts. The probe did not
include semantic retrieval or final HTTP ranking.

The HTTP comparison uses the [evaluation method](../evaluation/README.md#small-zim-body-index-comparison)
on the same small archive, real HTTP search and a fixed synthetic query set.
There are 120 English queries, 41 dev and 79 test; no settings are tuned.
All variants have identical embedding vectors. These rows use unranked body
matching, so they isolate the storage layout from BM25 ordering; the production
combination is measured in the next subsection. Reported results are test-only:

| Mode / variant | #1 | Top 5 | Top 20 | MRR | Fixed / broken at #1 vs positional |
|---|---:|---:|---:|---:|---:|
| Hybrid contentless positional | 79.7% | 92.4% | 96.2% | 0.854 | baseline |
| Hybrid compact | 79.7% | 92.4% | 96.2% | 0.854 | 0 / 0 |
| Hybrid native-only | 75.9% | 87.3% | 93.7% | 0.812 | 0 / 3 |
| Lexical contentless positional | 58.2% | 63.3% | 63.3% | 0.604 | baseline |
| Lexical compact | 58.2% | 63.3% | 63.3% | 0.604 | 0 / 0 |
| Lexical native-only | 51.9% | 57.0% | 58.2% | 0.539 | 0 / 5 |

Compact and positional match every stored top-20 list in both modes, including
dev queries. On this 3,508-document fixture the two layouts are
indistinguishable without BM25; the larger fixture below separates them.
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
handling. This document does not tune that follow-up on these test queries.

Treat the rates as a small-ZIM smoke comparison: labels are synthetic and
incomplete, generated sentences can be awkward or include profiles, and
aggregate differences are smaller than this sample's reported uncertainty.
These results identify cases to consider when evaluating retrieval behavior.

### Default stored content versus contentless, including BM25

[`content=''` is not the default](https://www.sqlite.org/fts5.html#external_content_and_contentless_tables).
Ordinary `fts5(text)` stores a second copy of the indexed text in its content
shadow table. This supports raw text reads, snippets/highlights, and normal
updates/deletes. Classic contentless tables return NULL for text, cannot supply
snippets, and have update/delete restrictions. Our published indexes are rebuilt,
and previews read the archive, not the stored FTS text.

Raw content, positions, and document lengths are separate choices:

| Body layout | Raw text | Positions | Stored token counts | Useful SQLite BM25 |
|---|---|---|---|---|
| Contentless positional (production) | No | Yes | Yes | Yes |
| Compact contentless | No | No | No | No; the probe returns tied `-0.0` scores |
| Default stored content | Yes | Yes | Yes | Yes |

The production body query uses
[`bm25(body)`](https://www.sqlite.org/fts5.html#the_bm25_function) for candidate
ordering: `ORDER BY bm25(body), rowid` (SQLite's better scores are more
negative), followed by the existing RRF and final ranking, with rowid as a
deterministic tiebreak. It keeps the same Boolean AND query; this is not a
redesign of retrieval or fusion.

The BM25 experiment below was run on the small fixture while production still
used the compact layout, so it measures a candidate-ordering change on an
unchanged query. It compares compact against stored content with `unicode61
remove_diacritics 2` held constant, plus positional BM25 controls;
`stored-default` uses literal `fts5(text)`, including bare default Unicode61.
Both stored variants match **all 120 compact top-20 lists in both modes**:
stored content adds no quality here.

All storage/ranking variants tie at 85.4% hybrid #1 on the 41 dev queries.
BM25 lowers lexical #1 from 68.3% to 63.4% (0 fixed, 2 broken). On the
79 held-out test queries:

| Mode / body variant | #1 | Top 5 | Top 20 | MRR | Fixed / broken #1 vs compact |
|---|---:|---:|---:|---:|---:|
| Hybrid compact / either stored variant | 79.7% | 92.4% | 96.2% | 0.854 | 0 / 0 |
| Hybrid contentless positional + BM25 (production), or stored BM25 | 79.7% | 91.1% | 96.2% | 0.846 | 0 / 0 |
| Lexical compact / either stored variant | 58.2% | 63.3% | 63.3% | 0.604 | 0 / 0 |
| Lexical contentless positional + BM25 (production), or stored BM25 | 57.0% | 62.0% | 63.3% | 0.587 | 0 / 1 |

BM25 improves no labelled test answer rank **on this 3,508-document fixture**:
four hybrid ranks worsen and five lexical ranks worsen. For the generated
first-sentence query about finding a common chat venue, BM25 promotes the
chat-rooms tag listing above the actual Slack question in the body channel.
After fusion, the intended question moves from #1 to #3 lexical and #2 to #4
hybrid. The listing contains related material; single-answer labels do not judge
every alternative's relevance. The larger fixture below reverses this
conclusion, which is why production now ranks by BM25.

A dedicated warm SQL-only sweep on the same physical indexes (48 queries,
three trials of 20 repetitions, limit 16) measures median body queries at
**0.0175 ms unranked versus 0.0335 ms BM25** for contentless positional; stored
content is effectively identical at 0.0174/0.0336 ms. That small extra cost does
not materially change HTTP latency: test hybrid medians are 112-115 ms across
the storage/BM25 controls. These HTTP timings are single evaluation passes, not
counterbalanced latency trials.

### Tokenizers and repeated build/runtime measurements

The second stage compares the dev-preferred compact layout across
[Unicode61](https://www.sqlite.org/fts5.html#unicode61_tokenizer),
[Porter](https://www.sqlite.org/fts5.html#porter_tokenizer), and
[ASCII](https://www.sqlite.org/fts5.html#ascii_tokenizer). It was measured
before the switch to contentless positional; the tokenizer produces the same
terms in either layout, and the positional row is included in the build table. The
[trigram](https://www.sqlite.org/fts5.html#the_trigram_tokenizer) control is
contentless with **full detail**, `columnsize=0`: SQLite rejects query tokens
longer than three characters with trigram `detail=none`. It is substring
retrieval, not the same word-token compact layout, and cannot match terms shorter
than three characters. Query normalization is the same in this comparison.

Unicode61 `remove_diacritics 2` is the reference setting, chosen before test-result
inspection. Bare Unicode61 and ASCII tied on English dev; Porter and
trigram worsened #1.
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
repetitions each) measure:

| Body variant | Lexical build median | Full build, single observation | Full SQLite MiB | SQLite + FAISS / ZIM | Warm helper-provider median |
|---|---:|---:|---:|---:|---:|
| Contentless positional, full detail | 9.52 s | 28.51 s | 2.56 | 79.1% | 3.62 ms |
| Compact Unicode61, mode 2 | 9.48 s | 28.63 s | 1.17 | 56.1% | 3.60 ms |
| Default stored FTS5 / Unicode61 | 9.63 s | 29.59 s | 7.17 | 155.4% | 3.62 ms |
| Compact Porter | 9.45 s | 28.99 s | 1.07 | 54.5% | 3.59 ms |
| Compact ASCII | 9.43 s | 28.78 s | 1.19 | 56.3% | 3.67 ms |
| Contentless trigram, full detail | 9.96 s | 29.25 s | 10.71 | 214.0% | 3.65 ms |

Full-build observations come from the HTTP study; the positional-layout observation
used BM25, which does not affect building. Do not infer a full-build speedup
from these single observations. Lexical-build SQLite files omit about 0.10 MiB
of vector metadata; the sizes above include it and the identical 2.21 MiB FAISS.
Provider timings exclude embedding, previews and HTTP. Trigram body SQL was
about 0.194 ms versus compact's 0.067 ms in those provider trials, despite
similar total provider medians.

The tokenizer selection is unchanged: Unicode61 `remove_diacritics 2`, not
ASCII or trigram. Default stored content costs about **6.1 times compact's
SQLite space** for identical results, and contentless positional costs about
**3.5 times compact's** while using **70% less space than stored content** at
the same quality (66.2 MB versus 221.6 MB on the larger fixture below). Porter
saves about 8% of SQLite bytes but loses labelled #1 results. None of these
layouts reaches the advisory 10% total-index goal on these fixtures, and the
body channel is only part of the total: the same cooking index needs about
24 MB of int8 vectors for 63,608 articles.

### Larger fixture: FTS5 layouts, Tantivy, and Bleve

The Community Building archive is too small, and its synthetic labels too
sparse, to separate body layouts or engines. A second measurement uses the
cooking Stack Exchange archive
(`cooking.stackexchange.com_en_all_2026-07.zim`, 237,005,541 bytes, 63,608
article bodies) with 1,450 labelled queries: 865 English plus 585 in eleven
other languages. Each arm is a body-only lexical index built from the same
extracted text, with zimantic's own extractor and query normalization, in a
standalone harness (SQLite 3, tantivy-py 0.27, Bleve 2.6, FAISS) rather than
through the HTTP server. Title and semantic channels are excluded, so absolute
rates are lower than the HTTP numbers above; compare arms within this table
only. The archive is English-only, so the 585 non-English queries cannot match
their own language there.

| Body index | English #1 | Top 20 | Build s | Index MB | % of ZIM |
|---|---:|---:|---:|---:|---:|
| FTS5 `detail=none`, `columnsize=0` (compact) | 53.5% | 71.7% | 1.81 | 18.7 | 7.9% |
| FTS5 `detail=column`, `columnsize=1` | 53.5% | 71.7% | 2.30 | 40.7 | 17.2% |
| FTS5 `detail=full`, `columnsize=1` (production) | 65.3% | 79.8% | 2.49 | 66.2 | 27.9% |
| FTS5 stored content, BM25 | 65.3% | 79.8% | 2.61 | 221.6 | 93.5% |
| Tantivy 0.27, BM25 | 65.5% | 79.5% | 1.79 | 50.7 | 21.4% |
| Bleve 2.6, BM25 | 54.1% | 67.1% | 4.33 | 61.3 | 25.9% |

Positions, not stored text, carry the gain: contentless positional matches
stored BM25 on **every top-20 list** at 30% of its bytes. `detail=column` keeps
document lengths but no positions and ties `detail=none` exactly, so it is not a
middle ground. The +11.8-point English #1 gain is the reason production moved
from compact to contentless positional.

Tantivy ties FTS5-BM25 on quality (65.5% versus 65.3% #1) in a smaller index
with a somewhat faster build. It is not adopted: PyPI publishes `tantivy` wheels
for x86_64 and aarch64 only, which would break the documented 32-bit armhf
install path, and the quality difference is inside this sample's noise. Staying
on SQLite also avoids a second index format, a second query implementation, and
a build-time dependency.

Bleve's default BM25 returns 54.1% #1 and 67.1% top-20, below compact FTS5
despite a larger index. Combining several fuzzy term variants with AND recovers
57.9% / 78.2%, still below exact BM25; combining them with OR collapses quality
to 12.5% / 53.8%. Bleve is lexical-only, so replacing the current stack means a
Go sidecar on every platform in addition to FAISS.

Hybrid rows below fuse each lexical arm with the same multilingual-e5-small semantic IVF-SQ8
arm by reciprocal rank fusion, on the 865 English queries:

| Hybrid lexical arm | #1 | Top 5 | Top 20 | MRR |
|---|---:|---:|---:|---:|
| Compact FTS5 | 49.4% | 77.3% | 86.1% | 0.625 |
| Bleve, BM25 | 48.1% | 74.5% | 83.1% | 0.606 |
| Bleve, fuzzy AND | 47.6% | 79.1% | 88.0% | 0.617 |
| Bleve, fuzzy OR | 21.3% | 41.5% | 62.9% | 0.306 |
| Tantivy, BM25 | 55.7% | 84.9% | 91.7% | 0.695 |
| Contentless positional FTS5 (production) | 55.5% | 84.7% | 91.8% | 0.694 |
| Semantic arm alone | 20.6% | 30.1% | 36.6% | 0.247 |

The semantic arm alone is included for scale. Either BM25 engine roughly
triples its #1 rate and adds about 55 points of top-20 recall, which is what
justifies the extra body-index bytes. Stored content buys nothing over
contentless positional, and Tantivy buys nothing over FTS5 here.

### Static embedding models

Embedding dominates build time. multilingual-e5-small int8 runs at about **84 documents/s**
on the same restricted CPUs, roughly 12.6 minutes for these 63,608 articles,
while the entire lexical index builds in 1.8-2.6 seconds. Static embedding
models replace the transformer with one learned vector per token and mean
pooling, which makes document encoding roughly 400 times cheaper.

Two [Model2Vec](https://github.com/MinishLab/model2vec) models,
sentence-transformers' static multilingual model, and the English-only
`e5-small-v2` were measured on the same corpus, queries, lexical arm, and
IVF-SQ8 layout. `potion-retrieval-32M` is an English retrieval model;
`potion-multilingual-128M` has multilingual vocabulary and tokenization;
`static-similarity-mrl-multilingual-v1` is an EmbeddingBag over 105,879
WordPiece tokens with Matryoshka-truncatable dimensions.

| Embedding model | Dim | Vocabulary | Parameters | fp32 size | English semantic #1 / top 20 | Hybrid + BM25 #1 / top 20 | Misspelled hybrid #1 | Encode docs/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| multilingual-e5-small int8 (production) | 384 | ~250k | ~118M | 118 MB int8 | 20.6% / 36.6% | 55.5% / 91.8% | 58.0% | ~84 |
| e5-small-v2 int8 (English-only) | 384 | ~30.5k | 33M | 34 MB int8 | 21.7% / 38.3% | 56.1% / 92.5% | 58.0% | ~88 |
| potion-retrieval-32M | 512 | 63,091 | 32.3M | 129.2 MB | 18.5% / 39.2% | 52.6% / 89.0% | 28.0% | ~35,600 |
| static-similarity-mrl-multilingual-v1 | 1024 | 105,879 | 108.4M | 433.7 MB | 17.0% / 35.7% | 48.3% / 87.1% | 24.0% | ~17,400 |
| potion-multilingual-128M | 256 | 500,353 | 128.1M | 512.4 MB | 10.2% / 28.3% | 47.3% / 84.6% | 16.0% | ~31,400 |

`e5-small-v2` is the English-only counterpart to the production model: a third
of the parameters and int8 bytes, marginally better English quality (+1.1
points semantic #1, +0.6 hybrid), and identical misspelling behavior. It scores
about zero on every non-English query set (Spanish 2.4%/3.5%, French
3.5%/8.2%, everything else 0%), and its IVF-SQ8 recall is lower (63.4% versus
78.3% top-1 at the same probe count), so it needs a higher `nprobe` or a flat
index. Its measured throughput is only about 5% higher than the multilingual
model's, which shows that model size is not the build bottleneck in this
pipeline.

`potion-retrieval-32M` is close to e5 on English semantic retrieval and only
2.9 points behind on hybrid #1, while encoding about 420 times faster and using
a quarter of the fp32 parameters. It still loses the cases that matter for a
mixed library: misspelled queries (28% versus 58% #1), because averaging fixed
token vectors gives no contextual correction, and every non-English query set
because its vocabulary and training are English.

`static-similarity-mrl-multilingual-v1` is the closest multilingual static
model: it recovers most of e5's English semantic rate (17.0% versus 20.6%, and
35.7% versus 36.6% top-20) and keeps more cross-language signal than
`potion-multilingual-128M`. It is still 7.2 points behind on hybrid #1 and loses
most typo tolerance. Truncating its Matryoshka vectors to 256 dimensions costs
about one point of hybrid #1 (47.4% / 86.8%) at a quarter of the vector bytes;
512 dimensions are within 0.1 point of the full 1,024.

The static `docs/s` figures are not measured identically: the two Model2Vec
models use a vectorized encoder, while the sentence-transformers model was
mean-pooled per document in numpy. Treat the column as an order of magnitude,
not a controlled speed ranking.

Semantic #1 / top-20 by query language on this English-only archive (these rows
test whether the model lands on the right English article from another
language's phrasing, not retrieval within that language):

| Language | multilingual-e5-small | potion-retrieval-32M | static-similarity-mrl-multilingual-v1 | potion-multilingual-128M |
|---|---:|---:|---:|---:|
| English | 20.6% / 36.6% | 18.5% / 39.2% | 17.0% / 35.7% | 10.2% / 28.3% |
| Spanish | 15.3% / 45.9% | 0.0% / 2.4% | 9.4% / 30.6% | 7.1% / 21.2% |
| French | 21.2% / 47.1% | 1.2% / 5.9% | 11.8% / 35.3% | 4.7% / 23.5% |
| Chinese | 4.7% / 36.5% | 0.0% / 0.0% | 8.2% / 41.2% | 3.5% / 14.1% |
| Hindi | 23.5% / 41.2% | 0.0% / 0.0% | 0.0% / 4.7% | 1.2% / 7.1% |
| Arabic | 9.4% / 22.4% | 0.0% / 0.0% | 4.7% / 25.9% | 0.0% / 5.9% |
| German, Italian, Portuguese, Japanese, Russian, Swahili | 1.2-33.3% / 3.5-53.3% | 0.0% / 0.0% | 0.0-13.3% / 1.2-40.0% | 0.0-6.7% / 6.7-33.3% |

Static vectors are unit length and drop into the existing IVF-SQ8 index, but
they separate worse: on `potion-retrieval-32M`, scanning 6% of clusters matches
only 81.4% of exact top-1 results, versus 94.2% for the 256-dimensional
`potion-multilingual-128M` and 91.2% for the 1,024-dimensional
`static-similarity-mrl-multilingual-v1`. That would need a higher `nprobe` or
exact flat search to match e5's ANN recall.

Static embeddings are therefore a plausible opt-in profile for English-only,
build-speed-sensitive libraries, not the default. Even the closest multilingual
static model remains 7.2 points behind e5 on hybrid #1, keeps only part of its
cross-language behavior, and trades away typo tolerance.

## ZIM iteration and resource bounds

ZIM builds use `iter_efficient()` from python-libzim 4.1.0 or later. This
iterator follows storage order; its exact order is not part of the public
contract. Earlier python-libzim versions use entry-ID order, which can be slower.
`iter_by_path()` and `iter_by_title()` do not follow storage order. The build
records the selected order and its digest. Resume preserves ID order after an
upgrade and rejects a different order or an incomplete manifest instead of
applying saved row positions to a different sequence. Completed indexes remain
valid.

Builds use bounded row windows, not article-count windows. This also bounds
redirect and title-only rows. A shared body-text budget limits retained text.
The build flushes queued work before it waits for budget that the same build
holds.

Automatic worker selection respects CPU affinity, physical cores when
available, parent and leaf cgroup memory limits, and available RAM. A bounded
shared model pool limits concurrent embedding work. Low-memory arena
reclamation is enabled. These limits do not guarantee that an arbitrary
model fits in memory and do not replace measurements on Raspberry Pi hardware.

## Token batches and progress

Read/commit windows use `batch_size`. Inside each window, inputs are truncated
at word boundaries, encoded once, sorted by capped token length, and grouped
into model calls with at most **1,000 padded input tokens** by default
(`embedding_batch_tokens`). Useful-token sums underestimate the workspace
needed for padding. One input that exceeds the batch budget runs alone and
stays capped by the model's per-input limit.

Fast builds commit at least 256 rows per window, independent of the selected
profile's `batch_size`; these title/path-only rows need no embedding workspace,
so larger windows reduce transaction overhead. An interruption can therefore
redo up to 255 more rows than the configured full-build window.

Workers share one model. Embedding runs concurrently with source reading. The
default pending-window limit scales to workers + 1, with a minimum of two, so
short-text windows can keep the worker pool busy. Commits preserve batch order;
read-ahead and the shared body budget remain bounded. A concurrency test
verifies that later work finishes while an earlier batch is blocked, without
committing out of order.
Equal token budgets make workspace sizes and scheduling more even,
not identical-cost: attention depends on sequence length and call overhead
also matters. Increase the token budget for GPU throughput.

Readable non-redirect entries form the article phase in native storage order.
Redirects and assets follow as a separate phase. Article progress advances only
after rows **and vectors** commit, including resumed work. The redirect and
asset phase has no ETA. Metadata-only order preparation and final FAISS
publication are outside the article ETA.

## Warm MIGraphX versus CPU builds (gfx803)

Linux x86-64, Intel i5-13600KF (14 physical/20 logical cores, unrestricted
affinity), 31.2 GiB RAM, AMD gfx803 GPU. CPU uses multilingual-e5-small int8
with ONNX Runtime 1.31.1. GPU uses the fp16 export with ONNX Runtime 1.29.0 in
`ghcr.io/schaka/rocm-migraphx-ort-torch-builder:latest-gfx803`.

Each command force-rebuilds both `devdocs_en_cmake_2025-10.zim` and
`devdocs_en_cpp_2025-10.zim`, publishing **2,001 + 4,870 = 6,871 documents**.
Both configurations use `--workers 2`, `batch_size = 256`,
`embedding_batch_tokens = 1024`, and `onnx_build_threads = 1`.
CPU uses eight embedding workers; GPU uses four workers feeding serialized
MIGraphX calls. Source reading, extraction, lexical indexing, vector creation,
FAISS writing, publication, model/session loading, and command startup are all
included. GPU timing also includes container startup.

These are fresh-process, warm-filesystem and warm-compiled-cache builds, not
reused-vector or persistent-process timings. The procedure drops no system caches.
After tuning, repeated alternating runs give:

| Backend | Full-command wall seconds, three runs | Median |
|---|---|---:|
| CPU int8 | 51.680 / 54.823 / 53.235 | 53.235 s |
| GPU fp16, MIGraphX | 47.049 / 46.849 / 48.312 | 47.049 s |

GPU took **11.6% less wall time** (1.13x throughput) on this workload, including
against CPU with the same larger read windows and token budget. No GPU run
compiled additional shapes or fell back to CPU. Per-source report durations overlap;
do not sum them to compare complete commands.
This shared-token-budget comparison is not the fastest CPU configuration:
the CPU-specific tuning below is faster than this GPU result.

The improvements are shape-isolated compiled caches, a bounded resident
session pool, and coalescing already queued compatible requests. With the
256-token model limit, the 1,024-token budget needs four fixed shapes:
`4x256`, `8x128`, `16x64`, and `32x32`. A separate warm CMake profile spent
6.535 seconds loading sessions and 8.740 seconds in GPU calls. Small ZIMs
favor CPU int8 in separate smoke runs: warm disk caches do not eliminate
session-loading overhead. This is not a universal GPU speedup or a search
latency result. See [GPU setup and settings](usage.md#gpu-acceleration).

Published CPU/GPU indexes have identical document rows, vector IDs, and all
596,366 lexical body postings for the two archives, with no body truncation.
Reconstructed vectors are finite and unit-length within the existing float16
storage tolerance. Int8 CPU and fp16 GPU vectors are not bit-identical:
corresponding-vector cosine means are 0.996388 (CMake) and 0.995869 (C++).
A separate same-model fp16 CPU/GPU check on all 85 Go by Example vectors gives
mean cosine 0.999921, minimum 0.999877. These are numerical checks, not labelled
retrieval-quality measurements.

SQLite plus FAISS occupies 141.8% of the original CMake archive and 121.6% of
the C++ archive for both backends. Neither meets the advisory index-size goal;
the throughput improvement does not come from dropping content or vectors.

## CPU int8 throughput tuning

On the same desktop and int8 model, a 256-token model-call
budget with the default 32-row read window is fastest in this measurement.
Eight embedding workers with one thread each are preferable to more workers or
multithreaded calls. This is measured tuning for this host and model, not a
universal default for every host or model.

Profiling shows a pipeline limit with smaller calls: a 32-row window
can require 32 requests, but the eight-worker queue holds only 16. Submitting a
window blocks source reading, then the workers run out of work while reading
catches up. CPU queues hold at least one read window, while retaining the
worker-based minimum. Model loading stays lazy, GPU queue bounds do not change,
and row windows and the shared body-text budget stay bounded. Multi-ZIM
builds also forward the configured token budget to the shared pool.

On the Python Enhancement Proposals archive
(`peps.python_en_all_2025-09.zim`), smaller calls increase the model-call
count from 228 to 715 and slow the build. A queue trace measures **2.000 s
blocked on submission** without the one-window bound and **0.075 s** with it.
The profiled build returns from 9.92 s to 8.54 s, matching the original
large-batch profile at 8.52 s. The regression is lost overlap, not extra model
computation; total process CPU time is lower with smaller calls.

Three fresh-process forced rebuilds of CMake + C++, including command startup
and complete publication, measure:

| CPU int8 configuration | Full-command wall seconds | Median |
|---|---|---:|
| Original defaults: 32-row windows, 1,000 tokens, 16 queued batches | 51.791 / 52.127 / 52.519 | 52.127 s |
| Tuned: 32-row windows, 256 tokens, corrected 32-batch queue | 41.151 / 42.379 / 42.607 | 42.379 s |

That is **18.7% less wall time** (1.23x throughput) for the same 6,871
documents. Increasing the read window to 256 rows was slower than retaining
32 rows. The tuned CPU observations also beat the 47.049 s GPU median,
but GPU is not rerun in these CPU-only trials.

The PEP archive does not regress. With the corrected queue in both
configurations, alternating three-run checks measure:

| PEP build token budget | Full-command wall seconds | Median |
|---|---|---:|
| 1,000 tokens | 9.743 / 9.487 / 9.700 | 9.700 s |
| 256 tokens | 9.197 / 9.234 / 9.361 | 9.234 s |

Both publish 714 documents with no body truncation. These timings include
startup; they measure indexing the PEP archive, not execution of Python code.

CMake/C++ document rows, vector IDs, and all 596,366 lexical body postings
stay identical. Vectors are finite and normalized within float16 storage
tolerance. Against the 1,024-token CPU reference, corresponding-vector
cosine means are 0.997883/0.997890 and minima 0.994248/0.993637 for CMake/C++.
Int8 batch-neighbour effects are therefore not bit-identical results; no
labelled retrieval-quality improvement is claimed. SQLite plus FAISS is
about 141.8%/121.6% of the respective archives, above the advisory goal.

See the [CPU configuration example](usage.md#cpu-build-tuning). Global defaults
stay fixed so this CPU-specific budget does not become a GPU or ARM
recommendation.

## Reader, preparation, and writer stage split

A full build with one active source can overlap three stages without
changing batching geometry:

```text
source reader (provider thread)
    -> bounded read-window queue
    -> preparation/submission worker
    -> bounded prepared-window queue with embedding futures
    -> ordered SQLite writer (its own staging connection)
```

The reader keeps `entry_at()`, `modified_at()`, and derived-text extraction in
the thread that owns the producer and its caches. The preparation worker removes
body text and submits the existing token batches without waiting for inference.
The writer processes windows FIFO, awaits that window's future, calls the
existing `_save_rows()` transaction, advances progress, and only then releases
that window's body reservation and permit. One `max_pending_per_build` permit
budget covers every in-flight window across all three queues, and the existing
shared body-byte budget is the same. Commit order, canonical row/vector IDs,
resume checkpoints, and publication order remain source order.

The staged path is used when one source is actively building. When several
source builds already run concurrently, source-level overlap fills the shared
inference pool and the existing coupled per-source path applies; adding two
more threads per source measures about 1.5% slower on the two-archive workload
below. Fast builds and vector-reuse builds also use their existing synchronous
paths. A vector-reuse build may use the existing staging connection, so it is
not moved across threads.

### Complete-build measurements

Fresh-process forced rebuilds, same host and configuration as the CPU tuning
above (`batch_size = 32`, `embedding_batch_tokens = 256`,
`onnx_build_workers = 8`, `onnx_build_threads = 1`). Warm filesystem and model
cache; no system caches dropped; no tests or other CPU benchmarks run during
timing. Each row is the median of its listed runs; command startup and complete
publication are included. The coupled-path control is the same working tree with
only the staged route disabled.

| Workload | Coupled path | Staged path | Default policy |
|---|---:|---:|---:|
| CMake + C++, 6,871 documents | 42.373 s (6 runs) | 43.030 s (6 runs) | 42.780 s (3 runs) |
| PEP, 714 documents | 9.555 s (6 runs) | 8.632 s (6 runs) | 8.738 s (3 runs) |

The staged path is **9.7% faster** on the single-source PEP build (about 1.11x
throughput) and about **1.5% slower** on the two-archive bulk build. With the
adaptive policy the bulk workload stays in the 42.4–42.8 s range and
PEP stays about 8.5–8.7 s. The tuned medians elsewhere in this document are 42.379 s
and 9.234 s; absolute values move with host state, so compare rows within this
table rather than treating small cross-table differences as gains.

Raw wall seconds, in run order:

```text
CMake + C++ coupled: 42.003 / 42.655 / 42.920 / 42.354 / 41.991 / 42.391
CMake + C++ staged:  42.572 / 42.812 / 43.238 / 42.822 / 44.189 / 44.687
CMake + C++ default: 42.668 / 43.808 / 42.780
PEP coupled:          9.116 /  9.597 /  9.639 /  9.512 /  9.509 /  9.600
PEP staged:           8.560 /  8.682 /  8.568 /  8.641 /  8.691 /  8.622
PEP default:          8.728 /  8.738 /  8.845
```

The bulk profile explains the limit. On a 42.61 s staged run, inference calls
sum **311.7 s of worker time** across eight workers (about 39 s per worker),
while ordered `_save_rows()` calls sum **8.1 s**. The writer spends 46.9 s
waiting on futures, overlapped with reading and preparation; the coupled control
spends about 27.0 s blocked in queue insertion from its reader threads. Moving
that wait to a writer frees the reader, but it cannot reduce the inference work
that dominates this workload. The single-source PEP build has enough
reading/preparation/commit work outside inference to benefit.

### Output integrity and GPU smoke

For both CMake and C++, the coupled and staged builds produce identical
document rows, lexical body postings, vector IDs, and document/body digests,
with zero body truncation and all `vecs` rows emptied after FAISS publication.
Corresponding staged-vs-coupled vectors from `index.faiss` have mean cosine 1.0
and minimum 0.9999998 after the existing float16 round-trip; vectors are finite
and unit length within float16 tolerance. These are numerical equivalence
checks, not labelled retrieval-quality measurements.

One warm MIGraphX smoke build using `run_gpu.sh`, `gpu-throughput` settings, and
the production shape-isolated cache publishes 2,001 + 4,870 documents with no
body truncation in 50.125 s. That single observation includes container startup
and is not a GPU regression or speedup claim; with two concurrent sources the
adaptive policy keeps the coupled path, so it does not exercise the split.

A separate single-source PEP A/B uses the same wrapper, settings, and cache,
with four runs each: coupled 17.845 / 18.011 / 18.046 / 17.367 s (median
17.93 s) versus staged 19.456 / 18.219 / 17.531 / 17.214 s (median 17.88 s).
That is neutral within this small sample, not a GPU speedup. MIGraphX runs stay
serialized, provider selection does not change, and no concurrent MIGraphX
execution is used.

## Measurements: November 7, 2026

Linux x86-64, Intel i5-13600KF, 31.2 GiB RAM; CPU affinity restricted to CPUs
0–3 (two physical/four logical cores) for every run. These are **not Raspberry Pi results**. Local filesystem
cache is warm; the procedure drops no system caches on the shared host.

Complete `communitybuilding.stackexchange.com_en_all_2026-08.zim`:
**6,331,350 bytes**, 3,706 entries, 3,508 indexed rows, 1,504 vectors.
Default multilingual-e5-small int8 model, 256-token inputs, 32-row windows.
Source revision: `3c4349b`. Archive SHA-256:
`33ba5516c9eb1fcb3da26fbd552cf6a3dda66f956469cf9e7f55e36abedcd424`.
Model graph SHA-256:
`f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193`.
python-libzim **4.1.0 is constant** for the comparison; the preliminary
4.0.0 baseline is excluded. Build times include source reading, extraction,
embedding, SQLite/body postings, FAISS writing, and generation publication;
model loading/warm-up and quality evaluation are excluded.

| Metric | Baseline | Measured pipeline |
|---|---:|---:|
| Median build time, two runs | 43.77 s | 23.93 s |
| Articles/s | 34.4 | 62.8 |
| Median peak process RSS | 881.5 MB | 471.0 MB |
| Embedding workers × threads/call | 1 × 4 | 3 × 1 |
| Recall@1, 20 labelled queries | 40% | 40% |
| Recall@5 | 65% | 60% |
| Recall@10 | 70% | 75% |
| MRR@10 | 0.4933 | 0.5113 |

The measured pipeline is **1.83× faster and uses 47% less peak RSS** in this
measurement. Baseline runs: 43.661/43.875 s; measured runs:
23.792/24.071 s. A separate run at 23.914 s confirms the same quality
metrics using the published FAISS index.

Revision `e0670ea` uses a two-window read-ahead limit. A run with read-ahead
scaled to worker count takes 23.559 s with 475.9 MB peak RSS; vectors, postings,
and query rankings match that revision. This single run is not a median and
does not establish a general speedup.

With the same native order, row window, sorting and 3×1 workers, fixed
eight-text calls take 26.716/26.396 s (median 26.56 s), with 647.4 MB
peak RSS. Token budgeting is **1.11× faster** and uses **27% less peak RSS**
than that ablation here. This separates its benefit from concurrent calls,
but does not reproduce every condition in the comparison.

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

`onnx_build_threads` controls build calls and automatic embedding-worker
sizing. `onnx_query_threads` controls query serving. These settings are independent.
Deprecated names are accepted as configuration aliases.

The following tuning uses the same 6.33 MB archive and model. Desktop affinity
is CPUs 0–7: **four physical/eight logical cores**. The Pi is a **Pi 500,
four ARM Cortex-A76 cores, 8 GB RAM**, with zimantic 1.0.0 and libzim
4.1.0. Its affinity is CPUs 0–2, leaving one core available
for other work, and benchmarks run at lower priority. No caches are dropped.
ONNX Runtime is 1.31.0 on desktop and 1.31.1 on Pi; compare configurations
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

Desktop parallel calls give **25% higher throughput** (20% less wall time),
justifying the desktop build default of one thread per call. On Pi, the gain
is only **1.6%**, costing **29% more RSS**. The Pi 5/500 preset therefore uses
three build threads and one automatic model worker: approximately 105 MB less
RAM for a two-second difference on this archive. The Pi 5 recommendation is
based on the Pi 500's same CPU class, not a separate Pi 5 measurement.

Within each machine, all four runs have **bit-identical vectors and vector
ids**, identical lexical postings/title/path/redirect rows, and identical
query rankings. Desktop recall@1/@5/@10 is 40%/60%/75%, MRR@10 0.5113;
Pi is 50%/70%/75%, MRR@10 0.5631. The differing platform results are not a
thread-policy accuracy improvement. Pi temperature reaches 49.1°C;
`vcgencmd get_throttled` stays `0x0`.

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
confirms the tradeoff: four threads take 1.60/2.39/13.20 ms, eight take
1.66/2.64/11.72 ms, and the former default of 19 takes 2.57/3.50/12.30 ms.
Three threads were fastest on the shortest input (1.42 ms), but slower on the
long input (15.56 ms). Choose a desktop automatic cap of **four usable physical
cores, leaving one available** (minimum one thread), favoring typical short
searches; this yields **three on a four-core Pi**. Override to eight for a
near-limit-query workload. A blanket one-thread serving default is not supported.

Every profile uses the same automatic query-thread rule above. Build
profiles keep two threads on mobile and eight on supercomputer, bounded by
usable cores; build and serving settings remain separate. This document does
not benchmark Pi Zero 2 W or large-core servers; the results do not set
optima for those devices. A RAM-limited single model worker can benefit from intra-call
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

For the baseline checkout, pass `--source-root /path/to/before-checkout
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

Text extraction prefers selectolax's Lexbor HTML5 parser for both body
indexing and previews, falling back to stdlib `HTMLParser` when Lexbor cannot
be imported. Block priorities, hidden/navigation filtering,
boilerplate removal, primary-language selection and text caps do not change.
The small, marker-gated meta-refresh scan uses stdlib `HTMLParser`.
Existing indexes are not rewritten automatically.

Automatic selectolax installation excludes known 32-bit machine names, using
the same platform marker as libzim. Other machines require selectolax even
when no compatible wheel is published, which may require a source build.
A separately installed selectolax is used on any platform;
`pip install 'zimantic[lexbor]'` explicitly opts in on excluded machines.
Platform markers cannot distinguish every interpreter bitness or
libc/OS-version/ABI wheel constraint.

The fallback uses stdlib `HTMLParser`. It applies the same visibility and
language rules and text caps, and checks deadlines between input chunks. It is
slower than Lexbor and does not recover malformed HTML5 markup.

The [recorded results](../tests/benchmarks/results/html-parsers-2026-11-07.json)
compare stdlib parsing with native backends using the same
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
| Stdlib HTMLParser | 1.00x | 1.00x | 8.0 MiB | 10/14 |
| lxml streaming target | 1.50x | 1.49x | 17.2 MiB | 13/14 |
| lxml DOM | 1.40x | 1.82x | 21.1 MiB | 12/14 |
| **Lexbor** | **1.44x** | **2.06x** | **24.8 MiB** | **14/14** |
| Beautiful Soup + lxml | 0.63x | 0.49x | 61.2 MiB | 13/14 |

The stdlib body/preview totals were 1.097/0.646 seconds; Lexbor took
0.763/0.313 seconds. Memory is a separate single fresh-process probe on the
eight largest corpus inputs, with all backends imported and inputs loaded
before measuring additional post-exec `VmHWM`. It includes native allocations,
but is not whole-application RSS or a concurrency/OOM guarantee.

All native alternatives match **240/242 real body and preview
outputs**, and none return empty text for a page where stdlib parsing finds
text. The two differences are improvements: Lexbor recovers useful opening
paragraphs from a resource-directory page and removes repeated
copyright/webmaster footer text from another document.
The production Lexbor extractor matches the measured prototype:
body and preview hashes match on all 250 real/stress pages, as do the preserved
stdlib baseline's hashes against the original baseline.

A **blinded, source-based AI review** covers those two disagreements, four
random nonempty real pages and all eight stress cases. Lexbor is preferred or
tied in all 14. lxml-backed variants lost prose after malformed navigation;
lxml DOM also lost prose beyond its default nesting-depth limit. All backends
produce a noisy dot-leader preview for one Wikisource table of contents:
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
