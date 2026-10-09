# Zimantic

**Offline search that understands what you mean, for Kiwix ZIM files.**

Zimantic adds meaning-based search to ZIM files: the compressed offline copies of Wikipedia and
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
- **Several sources at once**: search any combination of your ZIMs, ranked together in one list.
- **Best of three searches**: meaning, title-word and full-text retrieval are fused into a single ranking.
- **Clean results and previews**: redirects are merged into their article, so each article appears once, with a bounded excerpt as its preview.
- **Progressive results**: sources run in parallel and the page shows a provisional merged list as each source finishes, then reranks it deterministically.
- **Source-aware UI**: discover sources, filter results without re-searching, tolerate individual source failures, and optionally load thumbnails after text results appear. Every search always covers **all** available sources; source filters change what is displayed, never what is searched.
- **Lightweight and offline**: runs on low-resource devices, such as a Raspberry Pi Zero 2 W (512 MB RAM), using ~250–300 MB while serving.
- **Degrades gracefully**: title-word and full-text search work as soon as an index exists; vectors are optional. A fast index and `serve --fast` skip the model and FAISS entirely.
- **Multi-user**: several searches run at once (`max_concurrent_searches`); identical queries in flight share one computation, and repeated queries are answered from a small in-memory cache bounded by `cache_size` and `cache_bytes`.
- **Web page and JSON API**: search from any browser on the network, or from your own programs.

Zimantic finds articles; [kiwix-serve](https://kiwix.org/en/applications/) displays them. Searching
itself does not require Kiwix, but serving the article pages does, so Zimantic works alongside Kiwix
rather than replacing it.

## How it works

### 1. Indexing a ZIM (`build`, once per ZIM)

**What gets read.** Every entry in the ZIM is visited once. Images, stylesheets and scripts are
skipped. Redirects, including the small "forwarding" pages some ZIMs use instead of real redirects,
are stored as **title-only** entries that point to their article (app-shell stubs are the exception,
below). Thus, searching "USA" still finds the United States page.

**App-shell ZIMs.** Some ZIMs render every article through a JavaScript app: the HTML entry is a tiny
stub that forwards to a route such as `index.html#/Bookshelves/…`, and the real body is stored as JSON
(`content/page_content_<id>.json`, key `htmlBody`). Zimantic reads that JSON, indexes its text, and
keeps the article's own ZIM path (for example `index/page_3941`) as a deep link into the route, so
title-word, full-text and meaning search all agree on the real article instead of collapsing every
result onto the app shell. Stubs whose only visible text is an "enable JavaScript" notice, and the
shared app shell itself, are skipped so that one boilerplate vector cannot surface as a result.

**Text extraction.** Zimantic skips stylesheets, scripts and footnote markers like `[1]`, then
collects substantial visible blocks. Paragraphs are preferred; when a page has no suitable paragraph,
`blockquote`, `pre`, `div`, `section`, `article` and `main` are considered before ordered-list blocks
used by dictionary-style pages. Page chrome such as navigation, headers, footers and asides is
ignored, as are common boilerplate notices such as stub prompts and anti-bot warnings.

Extraction produces a single stored **excerpt** per article that serves two budgets, so the SQLite
index does not duplicate article text: it computes the preview- and embedding-sized candidates and
stores the longer one. The preview is capped by `max_preview_chars` (1,000 by default); the embedding
is capped by `max_embedding_tokens` (256 by default, including the model's two special tokens).
`preview_overflow = "skip"` keeps looking for another block when one does not fit;
`embedding_overflow = "truncate"` fills the remaining model budget. Truncation stops at word
boundaries. If no block fits, the best available block is truncated as a last resort. Both modes
inspect at most the first 4 MiB of each HTML page by default; change `max_html_bytes` to tune that
limit.

**Other ZIMs** (Stack Exchange, Gutenberg, TED, …) aren't refused: `build` runs on any ZIM and
extracts their visible HTML text using the same block hierarchy. PDFs inside a ZIM are skipped.

**Embedding.** The title and extracted text are turned into a vector — a list of numbers describing
their meaning — currently by
[multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) (int8 ONNX, ~118 MB).
Input is capped at `max_embedding_tokens` (256 by default, including the two special tokens). This is
an explicit application budget; the ONNX input shape is dynamic. The stored excerpt is truncated at
word boundaries to the available token budget before embedding. Search results use the same stored
excerpt and expose at most `max_preview_chars` characters.

**Storage.** Each ZIM gets two files in `index_dir`:
- `<name>.sqlite`: titles, the shared excerpt, paths, redirect targets, and a full-text index of the titles (SQLite FTS5).
- `<name>.faiss`: the vectors, in one of two layouts depending on article count:

| ZIM size | Vector index | Why |
|---|---|---|
| Under 10,000 articles | **Flat**: the query is compared with every vector | Exact, and small enough (a few MB) that comparing everything is fast |
| 10,000 articles or more | **IVF + 8-bit (SQ8)**: vectors are grouped into 4·√n clusters, and each search scans about 6% of the closest clusters by default (64 of 1,062 on WikiMed's 70k articles) | Comparing millions of vectors per search is too slow. On WikiMed (70k articles), scanning 64 of 1,062 clusters was within a few points of scanning every cluster, at less than half the search time (26 ms vs 66 ms). The probe count scales automatically for larger ZIMs; set `nprobe` in `config.toml` to use a fixed value instead. 8-bit numbers were nearly exact. **Heavier compression (e.g. PQ48) lost ~25% of the top hits in earlier testing.** |

IVF training samples are selected across the ZIM and scale with the number of clusters, with at least
39 samples per cluster as required by FAISS. This avoids under-training warnings on larger ZIMs
without loading every vector into the training set.

The `.faiss` file is memory-mapped: the operating system reads only the **clusters** a search touches
instead of loading the whole index into RAM. Clusters are found relative to the distance of
query-to-cluster centres in vector space.

If a build stops, running it again resumes from the last saved batch. Stopping one is safe at any
point: Ctrl-C and a systemd `stop` (SIGTERM is handled like Ctrl-C) land between batches, and an
interrupted run resumes where it left off. The FAISS file is written atomically and the SQLite `done`
marker is written only after it is complete, so an interrupted finalization can be resumed safely.
When a normal build upgrades a fast index, the existing title-word/full-text index remains available
until the replacement is complete. Extracted excerpts
are stored in the SQLite index, so changing `max_preview_chars`, `max_embedding_tokens`, overflow
policies, `max_html_bytes`, or extraction behavior requires rebuilding that ZIM with `build --force`.

### 2. Starting the server (`serve`, once)

At startup Zimantic loads the embedding model and opens every **finished** index; that is, the SQLite
file, the FAISS file, and the ZIM. These stay open for as long as the server runs; **nothing is
reloaded per search**. The page lists local indexes automatically. If `kiwix_server` is configured, it
also refreshes the Kiwix catalog and can search catalog sources without a local semantic index using
Kiwix full-text search.

An index does not need its FAISS file to be usable. If the vectors are missing or unreadable, the
server keeps serving that index with **title-word and ZIM full-text search** instead of refusing to
start. `serve --fast` takes this further and starts without the embedding model or any vectors at
all, which is much quicker on a Pi.

A lightweight HTML page is served through FastAPI and is accessible from any browser at
`http://<host>:8090` (the `port` in `config.toml`).

**Picking up indexes without a restart.** `python -m zimantic reload` sends `SIGHUP` to the running
server (it records its PID in `zimantic.pid` next to `config.toml` at startup) and the server rescans
`index_dir`: it opens new finished indexes, forgets deleted ones, and upgrades a fast index once its
vectors appear. A plain `kill -HUP <pid>` works too; the server also tolerates a second signal during
an in-flight reload. It is cheap enough to trigger from `systemd.path` or `cron`, so the server never
has to poll the directory. Clients cannot trigger a refresh or reload from the web page or the HTTP
API; source discovery is read-only.

### 3. Each search

1. **Embed the query** with the same model used to index. This happens once per search, not once per source.
2. **Run three searches on each selected local source.** Sources run in parallel, up to
   `search_workers` at a time; the three retrieval backends find the best matches in different ways:

   | Search | Finds | Good at | Time |
   |---|---|---|---|
   | **Meaning** (FAISS) | Articles whose bounded content-excerpt vector is closest to the query (cosine similarity) | Questions, descriptions, other languages | ~13 ms |
   | **Title words** (SQLite FTS5, BM25 ranking) | Titles containing every word of the query, including redirect titles | Exact titles, alternate names (via redirect titles) | ~4 ms |
   | **Full text** (the ZIM's own Kiwix index) | Articles containing the words anywhere | Words buried deep inside an article | ~8 ms |

3. **Collapse redirects**: every hit on a redirect is replaced by the article it points to, and duplicates are merged, so each article appears only once.
4. **Filter weak meaning matches.** Semantic candidates below `min_cosine_similarity` (0.85 by default) are discarded before ranking. Title-word and full-text matches still work below that floor, so the threshold only controls meaning-only results. Lower it in `config.toml` when a corpus needs broader semantic recall.
5. **Merge the three lists with Reciprocal Rank Fusion (RRF).** Their scores can't be compared (a cosine similarity, a BM25 score, a position in Kiwix's list), so RRF ignores scores and uses positions only: an article earns `1 / (60 + its position)` from each list it appears in. An article found near the top by several searches beats one that's first in just one. Redirects are collapsed before fusion, so an article can contribute at most once per search list. A bounded lexical bonus favors titles containing more query words, with compact titles preferred when coverage is equal; phrase matches, snippet coverage, and configured source intent provide additional deterministic signals.
6. **Treat disambiguation pages specially.** At build time a page is flagged when its title ends in "(disambiguation)", it renders the "This disambiguation page" footer, or it carries a disambiguation category (the rendered `Category:` link or `wgCategories`). The required suffix is stored in the title, so no separate disambiguation metadata is needed. A query that names the full title is navigation: the page is promoted above the normal score range. Any other query that merely matches it is demoted so the real article wins.

Search ranks **one pool** of results (up to `max_results`, at least `candidate_count`) and caches it
by query and source set, independent of page size, offset and display filter. The response carries a
single page (`page_size`, 10 by default); paging and source filtering reuse the same cached pool, so
the ranking work happens once no matter how far the user browses.

The streaming endpoint sends a source completion and a provisional snapshot of the current page as
each source finishes. The browser preserves result identities while reranking, so moved results
animate into their new positions and new results enter without rebuilding the whole list. Users who
prefer no animation are covered by `prefers-reduced-motion`. Previous/Next pagination uses regular
links carrying the query, source and page, so pages remain shareable and open-in-new-tab works, but a
click is intercepted: the page is fetched from the JSON endpoint and rendered in place instead of
reloading the document, and Back/Forward move through normal browser history.

*Timings measured on WikiMed; a whole search took 26 ms (median over 785 benchmark queries).
A Raspberry Pi Zero 2 W is much slower (around 150 ms).*

## Compared with Kiwix search

![Ask a question, get the article](docs/images/1-questions.png)
![Ask in another language](docs/images/3-languages.png)
![How often the right article comes first: Zimantic vs Kiwix](docs/images/0-scorecard.png)

*Measured on WikiMed, the Wikipedia medical encyclopedia ZIM (`wikipedia_en_medicine_maxi_2026-04`,
70,523 articles), against kiwix-serve's full-text search.*

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

The English articles searched with one word in another language, 15 common medical words per language
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

**Where Kiwix is still better, or cheaper:**
- **Words buried deep inside an article.** Kiwix indexes every word, so it performs better on deeper
  searches (68% first vs 28%; in the top 5 they're nearly tied, 78% vs 76%). Zimantic only indexes
  each article's title and bounded content excerpt.
- **No setup.** Kiwix search works the moment a ZIM is added. Zimantic must index each ZIM first.
- **Smaller footprint.** Zimantic adds a `.sqlite` and `.faiss` per ZIM and needs more RAM.

## Requirements

- Python 3.14 (libzim ships per-version wheels and the pinned release currently provides 3.14 only)
- About 120 MB for the embedding model, plus your ZIM files
- Wikipedia-style ZIM files (Zimantic relies on their predictable HTML structure to find useful content blocks)
- *Searching does not require Kiwix.* To open the articles from the result links, run
  [kiwix-serve](https://kiwix.org/en/applications/) with the same ZIMs (set its address as `kiwix_url` in `config.toml`).

## Where your files go

By default everything lives inside the project folder, so `config.toml` works without changes:

```
zimantic/
├── config.toml          ← settings (paths below are its defaults)
├── model/               ← model_dir: model.onnx + sentencepiece.bpe.model   (you download)
├── zims/                ← zim_dir:   your .zim files                          (you download)
├── indexes/             ← index_dir: <name>.sqlite + <name>.faiss            (created by build)
└── zimantic/           ← the program
```

Your files are somewhere else (a USB drive, another disk)? Point `zim_dir`, `model_dir` or `index_dir`
in `config.toml` at them instead. These three folders are git-ignored, so ZIMs, models and indexes
never get committed.

## Install

1. **Get the code and enter the folder.** Run the next commands from here. `config.toml` is optional —
   when it is missing, zimantic warns and uses defaults auto-detected from the machine
   (mobile / desktop / supercomputer), with the project folders as paths.

   ```bash
   git clone https://github.com/OscSanto/zimantic.git
   cd zimantic
   ```

2. **Create the environment and install the dependencies.** With
   [uv](https://docs.astral.sh/uv/) this is a single step — it creates `.venv` and installs
   Zimantic plus, by default, the dev-only tools (`pytest` and `httpx2`, which back the test
   suite; the program itself does not need them):

   ```bash
   uv sync
   . .venv/bin/activate              # Windows: .venv\Scripts\activate
   ```

   Add `--no-dev` to `uv sync` to skip the test tools. (`pip` works too: create a venv, then
   `pip install .` for Zimantic and `pip install pytest httpx2` to run the tests.)

3. **Download the model into `model/`** (keep these exact file names):

   ```bash
   mkdir -p model
   curl -L -o model/model.onnx https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/onnx/model_quantized.onnx
   curl -L -o model/sentencepiece.bpe.model https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/sentencepiece.bpe.model
   ```

   (`curl` is built into Linux, macOS and Windows 10+. On Windows PowerShell, use `curl.exe` and `mkdir model`.)
   Zimantic prints a warning at startup if either file does not match the documented checksum, but still
   runs: a different conversion works, it may just rank differently.

4. **Put your ZIM files in `zims/`.** Download them from
   [library.kiwix.org](https://library.kiwix.org) or [download.kiwix.org/zim](https://download.kiwix.org/zim/).

5. **Check `config.toml`.** If you used the folders above, nothing needs changing (you can even delete
   the file — auto-detected defaults take over, with a warning). Otherwise point `zim_dir` /
   `model_dir` / `index_dir` at your folders. To open articles from the results, set `kiwix_url` to
   where kiwix-serve runs. Keys you omit fall back to the auto-detected defaults.

6. **Check that it runs, and that the installed dependencies expose the APIs Zimantic uses:**

   ```bash
   python -m zimantic --help
   python -m pytest tests/test_runtime_api.py   # builds a tiny ZIM and reads it back
   ```

   The runtime check exercises the private libzim calls the indexer and searcher depend on
   (`_get_entry_by_id`, `_index`, the full-text `Searcher`), so a dependency upgrade that breaks them
   fails loudly here instead of at first search. Running the whole suite (`python -m pytest`) covers
   the rest.

## Use

```bash
python -m zimantic build                        # index every .zim in zim_dir (the default)
python -m zimantic build zims/x.zim             # index one file
python -m zimantic build zims/a.zim zims/b.zim  # several files
python -m zimantic build /media/usb             # every .zim in a folder
python -m zimantic build zims/a.zim /media/usb  # mix files and folders
python -m zimantic build --fast                 # quick title-word + full-text index (no vectors)
python -m zimantic build --force zims/x.zim     # rebuild one already-indexed ZIM
python -m zimantic serve                        # web page on http://<host>:8090 after a build
python -m zimantic serve --fast                 # start now: no model, no vectors
python -m zimantic reload                       # ask a running server to rescan index_dir
```

`build` takes zero or more files or folders. A folder means its `*.zim`; with no arguments it uses
`zim_dir` from `config.toml`. When multiple ZIMs are selected, they are processed from smallest to
largest file size. In an interactive terminal, `build` also shows a size-weighted overall progress bar
with a global ETA. Already-built ZIMs are skipped, so rerunning it is cheap. Pass `--force` to rebuild
the selected ZIMs even when their indexes are complete; a forced rebuild replaces both the SQLite and
FAISS files.

**Fast indexes.** `build --fast` stores titles and paths but never reads article bodies or runs the
model, so it finishes much sooner and needs no vectors. The result is still searched by
title-word (SQLite FTS) and the ZIM's own full-text index — both live in the ZIM/SQLite, not FAISS —
so only *meaning* search is missing. Run a normal `build` later and it re-reads the entries and adds
vectors in place.

Normal builds embed 32 articles at a time by default. This is intentionally a moderate CPU batch: the
model pads each batch to its longest passage, so larger batches can use more memory and take longer.
Tune `batch_size` in `config.toml` on faster hardware, and benchmark it against your ZIM. `embed_threads`
bounds the ONNX Runtime threads (it defaults to leaving a core free). When all search slots are occupied,
new searches fail fast as busy so they do not tie up web-server request threads; the web UI retries those
responses with exponential backoff.

**Hardware profiles.** When `config.toml` is missing, zimantic picks one of three default profiles
(mobile, desktop, supercomputer) from a simple heuristic: roughly 1 GB of RAM or less is mobile;
many cores (32+) or lots of RAM (128 GB+) is supercomputer; anything else is desktop. To pin explicit
settings instead, copy the ready-made templates at the repo root over `config.toml`:
`config.pi-zero-2w.toml` and `config.pi-5.toml`. Keys you omit from your `config.toml` still fall back
to the auto-detected profile.

Keep free disk space roughly equal to **another copy of the index** while `build` runs: a normal build
renames the replacement into place beside the old index (for a fast→full upgrade) and writes the FAISS
file through a same-directory temporary file before publishing it atomically.

**Automatic pickup with systemd.** Instead of the server polling directories, let systemd watch
`zims/` and build + reload when a ZIM is added. Ready-to-copy user units live in `deploy/`:

- `deploy/zimantic.service` — the server, with `Restart=always` and sandboxing. Copy it to
  `~/.config/systemd/user/`, then `systemctl --user enable --now zimantic`.
- `deploy/zimantic-zims.path` + `deploy/zimantic-index.service` — watch `zims/` and run a fast build
  plus `reload` when a ZIM appears.
- `deploy/zimantic-index-full.service` + `deploy/zimantic-index-full.timer` — run the full build
  (with meaning vectors) once a night.

**Indexing only runs during a nightly window**, 01:00–06:00 in the machine's own local time by
default. Both build services enforce the window with an `ExecCondition` clock check that runs before
every start, and the timer triggers the full build at 01:00 (`OnCalendar=*-*-* 01:00:00`). Because
the window follows local time, the same units work unchanged in any timezone — move the device or
change its timezone and the hours still mean 01:00–06:00 where the machine is. To use different
hours, change the two numbers in each `ExecCondition` and keep the timer's `OnCalendar` start inside
the window. ZIMs added during the day are not ignored: the nightly full build indexes them, and
`build` skips anything already indexed, so the nightly run is near-instant once the library is
complete. A shared `flock` (in util-linux) in both units guarantees the path-triggered fast build and
the nightly full build never write the same index at the same time; a build that arrives while the
lock is held is skipped and picked up on the next run.

Each file has install instructions in its header. Adjust `WorkingDirectory`/`ExecStart` if the project
is not at `~/zimantic`, and uncomment the `MemoryMax`/`CPUQuota` lines to cap resource use on a small
device.

`reload` does not need `config.toml`; it reads the server PID from `zimantic.pid` next to it by
default, or from the file passed with `--pid`. A stale PID file (left over after a crash or a signal
shutdown) is reported and ignored.

Watch `zims/`, not `indexes/`: `build` writes into `indexes/`, so a path unit there would fire on its
own output. `build` skips already-indexed ZIMs, so this is cheap once the library is indexed. If you
instead copy finished indexes in from another machine, point `PathChanged` at `indexes/` and run only
`reload`. Building on a more powerful PC and copying the files over is also a good plan when a ZIM is
slow to index locally.

To open articles from the results, run kiwix-serve with the same ZIMs, in a second terminal:

```bash
sudo apt install kiwix-tools             # Debian/Ubuntu/Raspberry Pi OS; other systems: kiwix.org/en/applications
kiwix-serve --port 8085 zims/*.zim       # matches the default kiwix_url in config.toml
```

A running `serve` picks up new indexes when you run `python -m zimantic reload` (no restart needed);
a fast index remains searchable while the normal build creates and publishes its replacement.

JSON API examples:

- `GET /api/search?q=...&zim=<name>&zim=<name2>&limit=10&offset=0&source=<key>&debug=1` returns one
  page of the JSON result list (`zim`, `source`, and `debug` may be omitted). Totals travel as
  `X-Total-Count`, `X-Has-More`, `X-Offset` and `X-Page-Size` headers, so the body stays a plain list.
  `source` is a display-only filter over the ranked pool: it never changes what is searched.
- `GET /api/search/stream?q=...&limit=10&offset=0&source=<key>` returns newline-delimited JSON events:
  `started`, `source`, `snapshot`, `done`, or a `busy` `error`. Each event carries `total_sources`,
  and snapshots/done carry the page, `total`, `has_more` and per-source `counts`.
- `GET /api/sources` returns source metadata and readiness; `GET /api/config` returns `page_size` and
  `max_results`; `GET /api/zims` remains as the local-index compatibility endpoint.
- `GET /api/health` reports served indexes, source count, and cache statistics.

Reloading indexes is **not** an HTTP API: running servers rescan via `python -m zimantic reload` or
`kill -HUP <pid>`.

Exact queries (same text, same selected sources) are answered from a small LRU cache controlled by
`cache_size` and bounded by `cache_bytes` (32 MiB by default). A streamed search caches its ranked pool
too, so the regular JSON endpoint gets it for free and every page of a query reuses the same
computation. Because the pool is cached independently of page size, offset and display filter, moving
between pages never re-ranks. The cache is only invalidated when the set of searchable sources actually
changes (an index is added, removed, or rebuilt), so repeated queries — including full page reloads —
are served from the cache as long as nothing changed.
