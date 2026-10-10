# Zimantic

**Offline search that understands what you mean for Kiwix ZIM files.**

Zimantic adds meaning-based search to ZIM files, the compressed offline copies of Wikipedia and
other sites published by Kiwix. Ask a question, describe something without knowing its name, misspell
it, or search in one of 100+ languages, and Zimantic finds the article closest to what you mean. It
works best in widely spoken languages. Everything runs offline, on hardware as small as a Raspberry Pi
Zero 2 W.

| You type | Kiwix search | Zimantic |
|---|---|---|
| "what causes lockjaw" | *Tetanus* not in the top 20 | *Tetanus* at **#1** |
| "diabetis" (misspelled) | *Diabetes* not in the top 20 | *Diabetes* at **#1** |
| "糖尿病" (Chinese for "diabetes") | *Diabetes* not in the top 20 | *Diabetes* at **#1** |

*Searched in WikiMed, the Wikipedia medical encyclopedia ZIM. Results below.*

## Features

- **Search by meaning**: questions and descriptions find articles even when they share no words with the title.
- **Alternate names and misspellings**: "diabetis" finds *Diabetes*; "Leber's disease" finds *Leber's hereditary optic neuropathy*.
- **Multilingual**: one model covers 100+ languages; a query in one language finds articles written in another. Strongest in widely used languages (see the results below).
- **Several sources at once**: search any combination of your ZIMs and rank the results together in one list.
- **Best of three searches**: meaning, title-word and full-text retrieval are fused into a single ranking.
- **Clean results and previews**: redirects are merged into their article, so each article appears once, with a bounded excerpt as its preview.
- **Progressive results**: sources run in parallel and the page shows a provisional merged list as each source finishes, then reranks it deterministically.
- **Source-aware UI**: discover sources, filter results without re-searching, tolerate individual source failures, and optionally load thumbnails after text results appear. Every search covers **all** available sources; source filters change what is displayed, never what is searched.
- **Lightweight and offline**: runs on low-resource devices, such as a Raspberry Pi Zero 2 W (512 MB RAM), using roughly 250–325 MB while serving.
- **Degrades gracefully**: title-word and full-text search work as soon as an index exists; vectors are optional. A fast index and `serve --fast` skip the model and FAISS entirely.
- **Multi-user**: multiple searches can run at once (`max_concurrent_searches`); identical queries in flight share one computation, and repeated queries are answered from a small in-memory cache bounded by `cache_size` and `cache_bytes`.
- **Web page and JSON API**: search from any browser on the network or use your own programs.

Zimantic finds articles, while [kiwix-serve](https://kiwix.org/en/applications/) displays them.
Searching does not require Kiwix, but displaying the article pages does. Zimantic works alongside
Kiwix rather than replacing it.

### Measured memory use

As a reference point, a clean benchmark using the `wikipedia_en_100_2026-08.zim` ZIM (5,056
articles) and one local index measured these peak resident set sizes:

| Operation | Peak RSS |
|---|---:|
| Full index rebuild | 301.7 MiB |
| Semantic server (`serve`) | 323.5 MiB |
| Title and full-text server (`serve --fast`) | 65.6 MiB |

The server benchmark loaded the embedding model, opened the single ZIM index, and handled health,
source-discovery and search requests. `serve --fast` omits the model and FAISS, so it uses much less
memory but does not provide meaning-based search.

## How it works

### 1. Indexing a ZIM (`build`, once per ZIM)

**What gets read.** Every entry is visited once; images, stylesheets and scripts are skipped.
Redirects are stored as **title-only** entries pointing to their article, so searching "USA" still
finds the United States page.

**App-shell ZIMs.** Some ZIMs store article bodies in JSON behind a JavaScript shell. Zimantic reads
that JSON and keeps each article's own path as a deep link, so results point to the real article
instead of the shared shell. Shell stubs and "enable JavaScript" notices are skipped.

**Text extraction.** Stylesheets, scripts, footnotes, page chrome and common boilerplate are
ignored. Visible blocks are collected in priority order, with paragraphs preferred.

Each article stores one **excerpt** for both previews and embeddings. It is capped by
`max_preview_chars` (1,000 by default) and `max_embedding_tokens` (256); overflow behavior is
controlled by `preview_overflow` and `embedding_overflow`. Extraction reads up to 4 MiB per page by
default.

**Other ZIMs** (Stack Exchange, Gutenberg, TED, …) are supported when they contain readable HTML.
PDFs inside a ZIM are skipped.

**Embedding.** Titles and excerpts are embedded with
[multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) (int8 ONNX, ~118 MB).
Input is capped by `max_embedding_tokens`; results use the same excerpt and expose at most
`max_preview_chars` characters.

**Storage.** Each ZIM gets two files in `index_dir`:
- `<name>.sqlite`: titles, the shared excerpt, paths, redirect targets, and a full-text index of the titles (SQLite FTS5).
- `<name>.faiss`: the vectors, in one of two layouts depending on article count:

| ZIM size | Vector index | Why |
|---|---|---|
| Under 10,000 articles | **Flat**: the query is compared with every vector | Exact, and small enough (a few MB) that comparing everything is fast |
| 10,000 articles or more | **IVF + 8-bit (SQ8)**: vectors are grouped into 4·√n clusters, and each search scans about 6% of the closest clusters by default (64 of 1,062 on WikiMed's 70k articles) | Comparing millions of vectors per search is too slow. On WikiMed (70k articles), scanning 64 of 1,062 clusters was within a few points of scanning every cluster, at less than half the search time (26 ms vs 66 ms). The probe count scales automatically for larger ZIMs; set `nprobe` in `config.toml` to use a fixed value instead. 8-bit numbers were nearly exact. **Heavier compression (e.g. PQ48) lost ~25% of the top hits in earlier testing.** |

IVF training samples are selected across the ZIM and scale with the number of clusters, with at least
39 samples per cluster as required by FAISS.

The `.faiss` file is memory-mapped, so searches read only the clusters they touch.

Builds resume from the last saved batch. FAISS files are published atomically, and a fast index
remains available while a full replacement is built. Rebuild with `build --force` after changing
extraction or excerpt settings.

### 2. Starting the server

At startup Zimantic opens every **finished** index and keeps it open; nothing is reloaded per search.
The page automatically lists local indexes. With `kiwix_server`, it also discovers catalog sources and
can search them with Kiwix full-text search.

An index remains usable without FAISS: the server falls back to **title-word and ZIM full-text
search**. `serve --fast` also skips the model and all vectors.

FastAPI serves a lightweight HTML page that is accessible from any browser at
`http://<host>:8090` (the `port` in `config.toml`).

**Picking up indexes without a restart.** `zimantic reload` sends `SIGHUP` to the server,
which rescans `index_dir` for added, removed or upgraded indexes. `kill -HUP <pid>` also works.

### 3. Each search

1. **Embed the query** with the same model used to index.
2. **Run three searches on each selected local source.** Sources run in parallel, up to
   `search_workers` at a time:

   | Search | Finds | Good at | Time |
   |---|---|---|---|
   | **Meaning** (FAISS) | Articles whose bounded content-excerpt vector is closest to the query (cosine similarity) | Questions, descriptions, other languages | ~13 ms |
   | **Title words** (SQLite FTS5, BM25 ranking) | Titles containing every word of the query, including redirect titles | Exact titles, alternate names (via redirect titles) | ~4 ms |
   | **Full text** (the ZIM's own Kiwix index) | Articles containing the words anywhere | Words buried deep inside an article | ~8 ms |

3. **Collapse redirects**: every hit on a redirect is replaced by the article it points to, and duplicates are merged, so each article appears only once.
4. **Filter weak meaning matches.** Semantic candidates below `min_cosine_similarity` (0.85 by default) are discarded before ranking. Title-word and full-text matches still work below that floor, so the threshold only controls meaning-only results. Lower it in `config.toml` when a corpus needs broader semantic recall.
5. **Merge the three lists with Reciprocal Rank Fusion (RRF).** Their scores are not comparable: one is cosine similarity, another is a BM25 score, and the third is a position in Kiwix's list. RRF therefore ignores scores and uses positions only: an article earns `1 / (60 + its position)` from each list it appears in. An article found near the top by several searches beats one that's first in just one. Redirects are collapsed before fusion, so an article can contribute at most once per search list. A bounded lexical bonus favors titles containing more query words, with compact titles preferred when coverage is equal; phrase matches, snippet coverage, and configured source intent provide additional deterministic signals.
6. **Treat disambiguation pages specially.** At build time a page is flagged when its title ends in "(disambiguation)", it renders the "This disambiguation page" footer, or it carries a disambiguation category (the rendered `Category:` link or `wgCategories`). The required suffix is stored in the title, so no separate disambiguation metadata is needed. A query that names the full title is treated as navigation, whether it writes `air (disambiguation)` or `air disambiguation`; a question that names the base title and ends in `?` is treated as navigation too. Those queries promote the page above the normal score range. Any other query that merely matches it is demoted so the real article wins.

Search ranks one pool of results (up to `max_results`, at least `candidate_count`) and caches it by
query and source set. Paging and display filters reuse that pool.

The streaming endpoint sends a provisional page as each source finishes. Pagination uses regular links
and updates the page through the JSON endpoint without a full document reload.

*Timings measured on WikiMed; a complete search took 26 ms (median over 785 benchmark queries).
A Raspberry Pi Zero 2 W is much slower (around 150 ms).*

## Compared with Kiwix search

![Ask a question, get the article](docs/images/1-questions.png)
![Ask in another language](docs/images/3-languages.png)
![How often the right article comes first: Zimantic vs Kiwix](docs/images/0-scorecard.png)

*Measured on WikiMed, the Wikipedia medical encyclopedia ZIM (`wikipedia_en_medicine_maxi_2026-04`,
70,523 articles), compared with kiwix-serve's full-text search.*

### Results by type of search

**#1** = the right article is the first result. **Top 5** = it's somewhere in the first five.
"Not in top 20" = it didn't appear in the first 20 results.

| Type of search | Example query → article wanted | Kiwix | Zimantic | #1 Kiwix → Zimantic | Top 5 Kiwix → Zimantic | Queries |
|---|---|---|---|---|---|---|
| Common single words | "tuberculosis" → *Tuberculosis* | #3 | **#1** | 33% → **100%** | 93% → **100%** | 15 |
| Exact titles | "Malaria" → *Malaria* | #2 | **#1** | 72% → **100%** | 100% → 100% | 25 |
| Alternate names | "Leber's disease" → *Leber's hereditary optic neuropathy* | #2 | **#1** | 52% → **82%** | 70% → **99%** | 200¹ |
| Misspellings | "diabetis" → *Diabetes* | not in top 20 | **#1** | 0% → **60%** | 7% → **83%** | 30 |
| First-sentence descriptions | "(INN) is a non-steroidal anti-inflammatory drug (NSAID)." → *Ampiroxicam* | #5 | **#1** | 72% → **83%** | 82% → **92%** | 200¹ |
| Describing it without the name | "poor blood flow to part of the brain that kills brain cells" → *Stroke* | #17 | **#1** | 8% → **37%** | 37% → **78%** | 60 |
| Questions | "what causes lockjaw" → *Tetanus* | not in top 20 | **#1** | 12% → **45%** | 28% → **80%** | 40 |
| Questions & phrases in other languages² | "Herzinfarkt" (German: heart attack) → *Myocardial infarction* | not in top 20 | **#1** | 0% → **33%** | 0% → **53%** | 30 |
| Words deep inside an article | "When undergoing lymphadenopathy, these are described as feeling like a 'firm pea'." → *Facial lymph nodes* | **#1** | #2 | **68%** → 28% | **78%** → 76% | 200¹ |

¹ Generated automatically from the articles, not typed by real users. The other sets were written by
hand; with 25–60 queries each, treat their numbers as accurate to roughly ±12–18 points.
² Spanish, French, German, Chinese, Hindi, Arabic, Russian, Japanese, Swahili and Portuguese, mixed.

### Single words in other languages

These tests search the English articles with one word in another language, using 15 common medical words per language
(malaria, diabetes, fever, cough, pregnancy, heart, blood, …). Translations were written for this test,
so less common languages may contain mistakes.

| Language | Example query → article wanted | Kiwix | Zimantic | #1 Kiwix → Zimantic | Top 5 Kiwix → Zimantic |
|---|---|---|---|---|---|
| English | "tuberculosis" → *Tuberculosis* | #3 | **#1** | 33% → **100%** | 93% → **100%** |
| Spanish | "dolor de cabeza" → *Headache* | not in top 20 | **#1** | 13% → **60%** | 20% → **73%** |
| French | "tuberculose" → *Tuberculosis* | #15 | **#1** | 13% → **53%** | 27% → **60%** |
| Portuguese | "coração" → *Heart* | #4 | **#1** | 13% → **53%** | 40% → **80%** |
| Afrikaans | "bloed" → *Blood* | not in top 20 | **#1** | 7% → **33%** | 20% → **33%** |
| Arabic | "سعال" → *Cough* | not in top 20 | **#1** | 0% → **20%** | 0% → **33%** |
| Hindi | "मलेरिया" → *Malaria* | not in top 20 | **#1** | 0% → **20%** | 0% → **33%** |
| Amharic | "ልብ" → *Heart* | not in top 20 | **#1** | 0% → **13%** | 0% → **13%** |
| Somali | "madax xanuun" → *Headache* | not in top 20 | **#1** | 0% → **13%** | 0% → **13%** |
| Swahili, Igbo, Zulu, Kinyarwanda | e.g. "ikholera" (Zulu) → *Cholera* | not in top 20 | **#1** | 0% → **7%** | 0–7% → 7% |
| Hausa, Yoruba | | | | 0% → 0% | 0% → 0% |

**Where Kiwix is still better or cheaper:**
- **Words buried deep inside an article.** Kiwix indexes every word, so it performs better on deeper
  searches (68% first vs 28%; in the top 5 they're nearly tied, 78% vs 76%). Zimantic only indexes
  each article's title and bounded content excerpt.
- **No setup.** Kiwix search works the moment a ZIM is added. Zimantic must index each ZIM first.
- **Smaller footprint.** Zimantic adds a `.sqlite` and `.faiss` per ZIM and needs more RAM.

## Requirements

- Python 3.14 (libzim ships per-version wheels and the pinned release currently provides 3.14 only)
- 32-bit Linux platforms (such as armhf/armv7) are not supported because required native dependencies do not publish compatible wheels
- About 120 MB for the embedding model, plus your ZIM files
- Wikipedia-style ZIM files (Zimantic relies on their predictable HTML structure to find useful content blocks)
- *Searching does not require Kiwix.* To open the articles from the result links, run
  [kiwix-serve](https://kiwix.org/en/applications/) with the same ZIMs (set its address as `kiwix_url` in `config.toml`).

## Where your files go

The installed CLI is separate from runtime data. By default the data directory is
`~/.local/share/zimantic`, so `config.toml` and its relative paths work without changes:

```
~/.local/share/zimantic/
├── config.toml          ← settings (paths below are its defaults)
├── model/               ← model_dir: model.onnx + sentencepiece.bpe.model   (you download)
├── zims/                ← zim_dir:   your .zim files                          (you download)
├── indexes/             ← index_dir: <name>.sqlite + <name>.faiss            (created by build)
└── zimantic.pid         ← server PID (created while serving)
```

If your files are somewhere else (on a USB drive or another disk), point `zim_dir`, `model_dir` or
`index_dir` in `config.toml` at them instead.

## Install

1. **Get the code and enter the data directory.** `config.toml` is optional; missing values use
   hardware-based defaults.

   ```bash
   mkdir -p ~/.local/share
   git clone https://github.com/OscSanto/zimantic.git ~/.local/share/zimantic
   cd ~/.local/share/zimantic
   ```

2. **Install the CLI with uv.** This installs the locked runtime dependencies without the
   development tools, then exposes the executable at `~/.local/bin/zimantic`:

   ```bash
   uv sync --locked --no-dev --no-editable
   ln -sfn "$PWD/.venv/bin/zimantic" ~/.local/bin/zimantic
   ```

   For development, use `uv sync` and `. .venv/bin/activate` instead.

3. **Download the model into `model/`** using these file names:

   ```bash
   mkdir -p model
   curl -L -o model/model.onnx https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/onnx/model_quantized.onnx
   curl -L -o model/sentencepiece.bpe.model https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/sentencepiece.bpe.model
   ```

   Zimantic warns if either file differs from the documented checksum, but still runs.

4. **Put your ZIM files in `zims/`.** Download them from
   [library.kiwix.org](https://library.kiwix.org) or [download.kiwix.org/zim](https://download.kiwix.org/zim/).

5. **Check `config.toml`.** Point `zim_dir`, `model_dir` and `index_dir` at your folders if needed.
   Set `kiwix_url` to the address where kiwix-serve runs.

6. **Check the installation:**

   ```bash
   zimantic --help
   ```

   For a development runtime check, activate the `uv` environment and run
   `python -m pytest tests/test_runtime_api.py`; use `python -m pytest` for the full suite.

## Development install

For a development environment based on the checkout:

```bash
uv sync
. .venv/bin/activate              # Windows: .venv\Scripts\activate
```

`uv sync --no-dev` omits test tools. `pip install .` also works in a virtual environment.

## Use

```bash
zimantic build                        # index every .zim in zim_dir (the default)
zimantic build zims/x.zim             # index one file
zimantic build zims/a.zim zims/b.zim  # several files
zimantic build /media/usb             # every .zim in a folder
zimantic build zims/a.zim /media/usb  # mix files and folders
zimantic build --fast                 # quick title-word + full-text index (no vectors)
zimantic build --force zims/x.zim     # rebuild one already-indexed ZIM
zimantic serve                        # web page on http://<host>:8090 after a build
zimantic serve --fast                 # start now: no model, no vectors
zimantic reload                       # ask a running server to rescan index_dir
```

`build` accepts files or folders; a folder means all its `*.zim` files, and no arguments uses `zim_dir`.
Already-built ZIMs are skipped. Pass `--force` to rebuild selected indexes.

**Fast indexes.** `build --fast` stores titles and paths without reading article bodies or running the
model. Title-word and ZIM full-text search still work; run a normal `build` later to add vectors.

Normal builds embed 32 articles at a time by default. Tune `batch_size` and `embed_threads` in
`config.toml` for different hardware.

**Hardware profiles.** Without `config.toml`, Zimantic selects mobile, desktop or supercomputer
defaults from the machine's resources. Copy `config.pi-zero-2w.toml` or `config.pi-5.toml` to pin
settings.

As a rule of thumb, keep enough free disk space when building indexes: at least **10% of each ZIM file's size**.

**Automatic pickup with systemd.** Ready-to-copy user units in `deploy/` can watch `zims/`, build and
reload when a ZIM is added:

- `deploy/zimantic.service` — the server, with `Restart=always` and sandboxing. Copy it to
  `~/.config/systemd/user/`, then `systemctl --user enable --now zimantic`.
- `deploy/zimantic-indexing-fast.path` + `deploy/zimantic-indexing-fast.service` — watch `zims/` and run a fast build
  followed by `reload` when a ZIM appears.
- `deploy/zimantic-indexing.service` + `deploy/zimantic-indexing.timer` — run the full build
  (with meaning vectors) once a night.

The path-triggered fast build runs whenever a ZIM is added. The full build runs from 01:00–06:00
local time by default; adjust its `ExecCondition` and timer to change the window. A shared `flock`
prevents overlapping builds.

Each unit has install instructions in its header. The units assume runtime data at the default XDG
data directory, `~/.local/share/zimantic`, and the CLI installed at `~/.local/bin/zimantic`.
Adjust `WorkingDirectory` and `ExecStart` if you use a custom `XDG_DATA_HOME` or install the CLI
elsewhere.

The units call the installed `~/.local/bin/zimantic` executable, while `WorkingDirectory` keeps
configuration, model files, ZIMs, indexes and `zimantic.pid` under the data directory. `reload`
does not need `config.toml`; it reads the server PID from `zimantic.pid` next to it by default, or
from the file passed with `--pid`. A stale PID file (left over after a crash or a
signal-triggered shutdown) is reported and ignored.

Watch `zims/`, not `indexes/`, because builds write to `indexes/`. If you copy finished indexes from
another machine, watch `indexes/` and run only `reload`.

To open articles from the results, run kiwix-serve with the same ZIMs, in a second terminal:

```bash
sudo apt install kiwix-tools             # Debian/Ubuntu/Raspberry Pi OS; other systems: kiwix.org/en/applications
kiwix-serve --port 8085 zims/*.zim       # matches the default kiwix_url in config.toml
```

A running `serve` picks up new indexes with `zimantic reload`; a fast index remains
searchable while a full replacement is built.

JSON API examples:

- `GET /api/search?q=...&zim=<name>&limit=10&offset=0&source=<key>` returns one page of results.
  Totals are in `X-Total-Count`, `X-Has-More`, `X-Offset` and `X-Page-Size` headers.
- `GET /api/search/stream?q=...&limit=10&offset=0&source=<key>` returns newline-delimited progress
  events and result snapshots.
- `GET /api/sources`, `/api/config`, `/api/zims` and `/api/health` return source, configuration,
  local-index and health information.

Reloading indexes is **not** an HTTP API: running servers rescan via `zimantic reload` or
`kill -HUP <pid>`.

Exact queries use a small LRU cache controlled by `cache_size` and `cache_bytes` (32 MiB by default).
The ranked pool is shared across pages and display filters, and invalidated when searchable sources
change.
