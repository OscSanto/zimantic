# Build performance and quality

This ports the ideas from [OscSanto/zimantic#3](https://github.com/OscSanto/zimantic/pull/3),
credited to OscSanto, into the current provider-neutral index pipeline rather
than cherry-picking the older ZIM-only builder.

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
continues. Equal token budgets make workspace sizes and scheduling more even,
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
0–3 for every run. These are **not Raspberry Pi results**. Local filesystem
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
FAISS retrieval, not hybrid ranking or UI latency.

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
