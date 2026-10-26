# Zimantic

**Offline search that understands what you mean for Kiwix ZIM files.**

Zimantic provides meaning-based search for ZIM files, the compressed offline copies of Wikipedia and
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
- **Also index your own files** (opt-in): text, Markdown, HTML, PDF, EPUB, Office/OpenDocument, subtitles, media (embedded or sidecar subtitles, optional speech recognition), images/comics with optional OCR, and archive contents become searchable sources alongside your ZIMs.
- **Alternate names and misspellings**: "diabetis" finds *Diabetes*; "Leber's disease" finds *Leber's hereditary optic neuropathy*.
- **Multilingual**: one model covers 100+ languages; a query in one language finds articles written in another. Strongest in widely used languages (see the results below).
- **Several sources at once**: search a small selection of ZIMs directly, or search the full library and filter the ranked results by source.
- **Best of three searches**: meaning, title-word and full-text retrieval are fused into a single ranking.
- **Clean results and previews**: redirects are merged into their article, so each article appears once, with a bounded excerpt as its preview.
- **Progressive results**: sources run in parallel and the page shows a provisional merged list as each source finishes, then reranks it deterministically.
- **Source-aware UI**: discover sources, sort matching sources by count, select multiple source filters, and reset with **All Sources**; the top 10 sources are shown with an **Other** catch-all when needed. Selected sources stay visible, so choosing **Other** expands that group into its individual filters. A selection of at most `min(10, 35%)` of available sources is searched on its own when it starts a search and is not cached; a larger selection is promoted to one full search whose ranked pool is cached, and the filters narrow what is displayed. Source filters chosen after a full search are display-only and reuse that pool.
- **Installed library view**: browse local ZIMs at `/library` in a responsive card grid with archive artwork, descriptions, metadata, and direct links to each homepage or random article. **Shuffle** reorders the cards. An unreadable archive stays in the grid marked **Unavailable** with the catalog's reason. Click a ZIM's title (or **Details**) for an infobox with its thumbnail, homepage, descriptions, counts, license, scraper, source, attribution, relation, tags and content-type breakdown. Prefix terms with `-` to exclude matching sources; multiple exclusions are supported. The filter searches all catalog metadata, including the ZIM stem.
- **Lightweight and offline**: runs on low-resource devices, such as a Raspberry Pi Zero 2 W (512 MB RAM), using roughly 250–325 MB while serving.
- **Degrades gracefully**: title-word and full-text search work as soon as an index exists; vectors are optional. A fast index and `serve --fast` skip the model and FAISS entirely.
- **Multi-user**: multiple searches can run at once (`max_concurrent_searches`); identical queries in flight share one computation, and repeated queries are answered from a small in-memory cache bounded by `cache_size` and `cache_bytes`.
- **Built-in article serving**: local result links open directly from the ZIM archives, including redirects, assets, and each ZIM's homepage, without requiring `kiwix-serve`.
- **Web page and JSON API**: search from any browser on the network or use your own programs.
  Routes use short paths such as `/search`, `/library`, `/content/<zim>/<path>`, `/article/<zim>/<path>` and `/health`.

Zimantic can search and serve articles from its local ZIM archives. An optional
[kiwix-serve](https://kiwix.org/en/applications/) remains useful for sources that are only
available through a Kiwix server, but local indexed results do not require it.

### Measured memory use

As a reference point, a clean benchmark using `wikipedia_en_100_2026-08.zim` (5,056 articles) and
one local index measured these peak resident set sizes:

| Operation | Peak RSS |
|---|---:|
| Full index rebuild | 301.7 MiB |
| Semantic server (`serve`) | 323.5 MiB |
| Title and full-text server (`serve --fast`) | 65.6 MiB |

The server benchmark loaded the embedding model, opened the single ZIM index, and handled health,
source-discovery and search requests. `serve --fast` omits the model and FAISS, so it uses much less
memory but does not provide meaning-based search.

**Where the memory goes.** With the default model, the ~118 MB int8 weights and the SentencePiece
vocabulary (~50 MB of native memory) make up most of the footprint; the English-only `e5-small-v2`
replaces both with a ~34 MB model and a small WordPiece vocabulary. They are loaded once and are not
duplicated, and `gc.collect()` cannot release them because they are C++ allocations, not Python
objects. ONNX Runtime also reads the file into a temporary buffer while loading (so there is a
short-lived ~2× peak at startup) and its **CPU memory arena** keeps the working set of the largest
embedding batch for the lifetime of the session. At `batch_size = 32` that arena alone can consume
~400 MB and retain it. On hosts with **2 GB RAM or less**, Zimantic therefore disables the arena and
returns freed heap memory to the OS between batches (`malloc_trim` on glibc), capping build memory
near the model size at a modest throughput cost. Machines with more RAM keep the arena for faster
builds. `serve` embeds one query at a time, so the arena costs little there either way.

## How it works

### 1. Indexing a ZIM (`build`, once per ZIM)

**What gets read.** Every entry is visited once; images, stylesheets and scripts are skipped.
Redirects are stored as **title-only** entries pointing to their article, so searching "USA" still
finds the United States page.

**App-shell ZIMs.** Some ZIMs store article bodies in JSON or Markdown behind a JavaScript shell.
Zimantic reads that companion and keeps each article's own path as a deep link, so results point to
the real article instead of the shared shell. This covers Kiwix's `content/page_content_<id>.json`,
media/app bundles that keep a `<slug>.json` companion (for example `videos/<slug>.json` or
`playlists/<slug>.json`) keyed by the deep link, and docs/curriculum bundles (freeCodeCamp and
similar) whose `<route>.md` holds the body under `content/`. Plain-text fields such as video
descriptions and Markdown prose are indexed too (front matter and fenced code are dropped). Shell
stubs and "enable JavaScript" notices are skipped.

**Text extraction.** Stylesheets, scripts, footnotes, common boilerplate and page chrome (category
footers, navboxes, tables of contents, maintenance tags) are ignored, including page chrome that is
nested or hidden (`display:none`, `visibility:hidden`, the `hidden` attribute). When a page
repeats the same text under `lang-*` classes (parallel translations, as in TED talks), only the
`lang-default` copy is kept. Visible blocks are collected in priority order, with paragraphs
preferred, and block boundaries keep neighboring text runs from running together.

Each article's **excerpt** is extracted transiently for the embedding and discarded after the
vector is built. At search time, only the displayed local results are extracted again for previews,
bounded to `max_preview_chars` (1,000 by default) at a word boundary. Extraction reads up to 4 MiB
per page by default. Entries are skipped when they are assets, JavaScript shells, empty pages, scanned
PDFs without text, or other non-article content. Pages that ship only script/link references (for
example PhET simulations) remain title-only, so they can still be found by title. The builder also
skips an entry whose decompressed size exceeds `max_entry_bytes` (1 GiB on desktop, 256 MiB on the
mobile profile); this high safety ceiling preserves large, useful articles while preventing
pathological entries from exhausting memory before the parser limit can be applied. Companion JSON
and Markdown bodies are subject to the same limit.

**Other ZIMs** (Stack Exchange, Gutenberg, TED, …) are supported when they contain readable text:
HTML, XHTML, plain text and PDFs. PDF text is read from the file's embedded text layer, so scanned
documents without one are skipped.

**Ordinary files.** `build` can index files outside ZIMs when `file_types` (or
`--file-types`) includes their extension. Extensions may be written as `epub`, `.epub` or
`*.epub`. Explicit file paths are always considered; when the extension is missing or unknown
(for example a downloaded `manual.bin`), the leading bytes are inspected and the file is routed to
the right reader (PDF, EPUB, ZIP/Office, media, and so on). Use `--file-types all+unknown` to make a folder
scan consider every file and let that content detection decide. The extractor is layered: a native
reader is preferred, then a small optional command (`pdftotext`, `unrtf`, `catdoc`, `ps2ascii`,
`tesseract`, `ffmpeg`), then calibre's `ebook-convert`, and a missing helper only skips that format
instead of failing the build. Calibre 7.19+ uses its faster `pdftohtml` PDF engine; older Calibre
builds keep the default engine.

Supported families include:

- text, Markdown, HTML/XML/SVG, source code, JSON/CSV/YAML, EML messages, man pages, torrent
  metadata and subtitles;
- PDF (page text) and EPUB/EPUB3 (spine chapters);
- Office/OpenDocument: DOCX, ODT, PPTX, XLSX and legacy DOC/XLS/PPT;
- RTF, PostScript/EPS, CHM, MOBI/AZW3/FB2/DjVu (via calibre);
- video and audio through embedded subtitle streams, sidecar subtitles
  (`lesson.mp4` + `lesson.en.srt`, `lesson_en.srt`, `lesson - English.srt`, or a `subtitles/`/`subs/`
  folder), or opt-in speech recognition;
- images and comic archives with opt-in OCR. Images in formats tesseract does not read reliably
  (WebP, AVIF, HEIC, JPEG 2000, JPEG XL, ...) are converted to PNG first;
- archives: member names are always indexed; `archive_depth` optionally extracts and indexes
  nested files. ZIP/7z/RAR and many aliases (`.zipx`, `.tar.lz4`, `.zstd`, `.lzh`, `.001`, `.r00`,
  ...) are supported, and subtitle-bearing media inside an archive is indexed like a standalone
  file. Archives are read with `7z` or `unar`.

A PDF page, EPUB chapter, presentation slide, spreadsheet sheet and subtitle chunk become separate
results. Locators are preserved in the result link, for example `/file/manual.pdf#page=12` or
`/file/lesson.mp4#t=91.4`. A plain file with no sections remains one result. Scanned PDFs and images
are skipped unless OCR is enabled; a comic archive still exposes its page filenames when OCR is off.

```bash
# Index every text and ebook file below ./zims (plus the ZIMs).
zimantic build --file-types 'zim,text,ebook'

# OCR scanned PDFs/images and recurse one archive level.
zimantic build --file-types 'zim,docs,archives' --ocr --archive-depth 1

# Consider every file and rely on content detection for unknown extensions.
zimantic build --file-types all+unknown

# Index one explicit file regardless of file_types.
zimantic build ~/Documents/manual.pdf
```

Named profiles are `text`, `ebook`, `docs`, `office`, `media`, `video`, `audio`, `image`, `comic`,
`archive`, `all` (every supported type) and `all+unknown` (every file, using content detection). The
same list belongs in `config.toml` so a later folder build uses it:

```toml
file_types = ["zim", "text", "docs", "ebook"]
# ocr = true
# archive_depth = 1
# max_archive_bytes = 268435456
# file_timeout = 120.0
# max_file_text_chars = 2000000
```

`archive_depth` controls recursive archive extraction: `0` (the default) lists member names without
unpacking files, `1` extracts and indexes supported direct members, and `2` also extracts and
indexes supported members inside nested archives. `max_archive_bytes` is a separate per-archive
limit on the total **uncompressed member data** extracted (256 MiB by default); archive metadata
and member names do not count toward it. Compressed archives may still be streamed to read their
directory, but member contents are only unpacked when the archive depth requires it. Each
extraction is also bounded by `file_timeout`, `max_file_text_chars` and `max_archive_members`.
A malformed file or a failing helper is logged and skipped without aborting the rest of the build.

**Embedding.** Titles and transient excerpts are embedded with
[multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) (int8 ONNX, ~118 MB).
The model receives a fixed budget of 256 tokens. See
[Using a different embedding model](#using-a-different-embedding-model) for other e5 models, their
tokenizers, and their memory and speed trade-offs. Entries with no body text (title-only pages, for
example JavaScript loader shells) remain searchable through title and full-text retrieval; they
simply have no meaning vector. Search previews are generated independently from the local ZIM only
for the final displayed page.

**Storage.** Each ZIM gets two files in `index_dir`, plus one shared catalog:
- `<name>.sqlite`: titles, paths, redirect targets, and a full-text index of the titles (SQLite FTS5).
- `<name>.faiss`: the vectors, in one of two layouts depending on article count.
- `catalog.sqlite` (beside `index_dir`, not inside it): one row per local ZIM with the metadata used
  by `/sources` and `/library.json` (title, description, language, creator, publisher, date, UUID,
  perma-ref, flavour, category, normalized tags, article/media/entry counts, license, scraper, source,
  attribution, relation, content-type counts, homepage, full-text capability, availability, and the
  favicon as a BLOB). It is a disposable cache: the ZIM files are the source of truth, and deleting it
  makes the next scan rebuild it. `serve` reads this catalog instead of opening every archive at
  startup; archives and their libzim `Searcher` objects are opened lazily on first use. A missing or
  unreadable `zim_dir` (for example an unmounted USB drive) is an error that keeps the previous
  catalog, rather than being mistaken for a library from which every ZIM disappeared. While the
  directory is unavailable, the served `/sources` and `/library.json` lists are empty; the catalog
  rows remain on disk for the next successful scan once the directory is back.

The index identity is `(ZIM filename stem, source byte size)`, not the source path. A ZIM can
move between nested folders below `zim_dir` without rebuilding. When files share a stem but have
different sizes, build and serve discovery use the largest copy and log a warning; the catalog keeps
both rows and resolves the stem to its largest file. A truncated download beside a complete archive
therefore does not hide the readable one.

| ZIM size | Vector index | Why |
|---|---|---|
| Under 10,000 embedded articles | **Flat**: the query is compared with every vector | Exact, and small enough (a few MB) that comparing everything is fast |
| 10,000 embedded articles or more | **IVF + 8-bit (SQ8)**: vectors are grouped into up to 4·√n clusters (capped so FAISS can train at least 39 points per centroid), and each search scans about 6% of the closest clusters by default (64 of 1,062 on WikiMed's 70k articles) | Comparing millions of vectors per search is too slow. On WikiMed (70k articles), scanning 64 of 1,062 clusters is within a few points of scanning every cluster, at less than half the search time (26 ms vs 66 ms). The probe count scales automatically for larger ZIMs; set `nprobe` in `config.toml` to use a fixed value instead. 8-bit numbers are nearly exact. **Heavier compression (for example PQ48) loses ~25% of the top hits.** |

IVF training samples are selected across the ZIM and scale with the number of clusters, with at least
39 samples per centroid as required by FAISS.

The `.faiss` file is memory-mapped, so searches read only the clusters they touch.

Builds resume from the last saved batch. FAISS files are published atomically, and a fast index
remains available while a full index builds. `build --force` applies the configured extraction and
preview settings while rebuilding the selected indexes.

### 2. Starting the server

At startup Zimantic reads the shared `catalog.sqlite` for source metadata, homepages, counts and
icons, then loads every **finished** index whose archive is still present and matches the size
recorded at build time. Archives and their libzim `Searcher` objects are opened lazily on the first
search or article request, so a large library no longer opens every ZIM at startup. It also discovers
ZIM files in `zim_dir` that lack a usable SQLite index and uses each archive's native full-text index
as a graceful fallback. An index whose ZIM is missing is ignored. An index whose archive has changed
size is set aside, and the archive is served unindexed instead (re-run `build` to restore the index),
so a partial or stale index never masks the ZIM that is actually available. Each stem appears only
once: a finished SQLite index takes precedence over the filesystem fallback, and an unfinished one
leaves the stem to that fallback. FAISS and vector files load in the background during startup, so
title-word and full-text searches are available while semantic search initializes.
The page automatically lists local ZIM sources. With `kiwix_server`, it also discovers catalog sources
and can search them with Kiwix full-text search. Kiwix catalog discovery is capped at 50,000 entries
and remote XML responses are capped at 64 MiB by default; both limits are configurable.

A finished index remains usable without FAISS: the server falls back to **title-word and ZIM full-text
search**. A ZIM without a SQLite index is also searchable through its native full-text index,
but has no SQLite title-word or semantic search until it is built. `serve --fast` also skips the
model and all vectors.

FastAPI serves a lightweight HTML page that is accessible from any browser at
`http://<host>:8090` (the `port` in `config.toml`). To serve it under a path such
as `/zimantic`, set `web_base_path = "/zimantic"` and point a reverse proxy at the
server with that prefix stripped, for example:

```nginx
location /zimantic/ {
    proxy_pass http://127.0.0.1:8090/;
}
```

Generated links, assets and API requests then stay under `/zimantic/`; open the
page with the trailing slash.

**Reloading indexes without a restart.** `zimantic reload` sends `SIGHUP` to a normal server,
which rescans `index_dir` and `catalog.sqlite` and reconciles the available indexes. `kill -HUP <pid>` also works.
In `serve --debug` mode (also available as the `serve --dev` alias), SIGHUP restarts the
whole server process and reloads all in-memory state.

### 3. Each search

1. **Embed the query** with the same model used to index.
2. **Run three searches on each selected local source.** Sources run in parallel, up to
   `search_workers` at a time:

   | Search | Finds | Good at | Time |
   |---|---|---|---|
   | **Meaning** (FAISS) | Articles whose bounded content-excerpt vector is closest to the query (cosine similarity) | Questions, descriptions, other languages | ~13 ms |
   | **Title words** (SQLite FTS5, BM25 ranking) | Titles containing every word of the query, including redirect titles | Exact titles, alternate names (via redirect titles) | ~4 ms |
   | **Full text** (the ZIM's own Kiwix index) | Articles containing the words anywhere | Words buried deep inside an article | ~8 ms |

3. **Collapse redirects**: every redirect hit resolves to the article it points to, and duplicates are merged, so each article appears only once.
4. **Filter weak meaning matches.** Semantic candidates below `min_cosine_similarity` (0.85 by default) are discarded before ranking. Title-word and full-text matches still work below that floor, so the threshold only controls meaning-only results. Lower it in `config.toml` when a corpus needs broader semantic recall; cross-language queries often need 0.80 (see **Queries in another language** below).
5. **Merge the three lists with Reciprocal Rank Fusion (RRF).** Their scores are not comparable: one is cosine similarity, another is a BM25 score, and the third is a position in Kiwix's list. RRF therefore ignores scores and uses positions only: an article earns `1 / (60 + its position)` from each list it appears in. An article found near the top by several searches beats one that's first in just one. Redirects are collapsed before fusion, so an article can contribute at most once per search list. A bounded lexical bonus favors titles containing more query words, with compact titles preferred when coverage is equal; exact phrases in titles or readable article text and source-specific intent rules (for example WikiHow queries) provide additional deterministic signals. Stack Exchange-style profile pages under `/users/` are demoted unless the query explicitly asks for a user or profile.
6. **Treat disambiguation pages specially.** At build time a page is flagged when its title ends in "(disambiguation)", when it renders the "This disambiguation page" footer, or when it carries a disambiguation category (the rendered `Category:` link or `wgCategories`). The required suffix is stored in the title, so no separate disambiguation metadata is needed. A query that names the full title is treated as navigation, whether it uses `air (disambiguation)` or `air disambiguation`; a question that names the base title and ends in `?` is treated as navigation too. Those queries promote the page above the normal score range. Any other query that merely matches it is demoted so the real article wins.

**Queries in another language.** A query in Chinese, Hindi, Arabic or another language against an
English ZIM usually shares no words with the titles or article text, so the meaning search is usually
the only stage that can retrieve the right article. Cross-language cosine scores run lower than
same-language scores, even when the result is correct: the whole distribution shifts down rather than
the correct article disappearing. The 0.85 floor can therefore trim results that would otherwise rank
well. Lowering the threshold recovered many Chinese, Hindi and Arabic results in testing:

```toml
# config.toml
min_cosine_similarity = 0.80
```

This is a recall/precision trade-off, not a separate language mode: lower values admit weaker
meaning-only matches, while title-word and full-text matches are unaffected.

The lexical stages remain tuned for space-delimited, mostly English text. The stopword list is
English, SQLite's `unicode61` tokenizer treats a run of Chinese characters as one title token, and the
long-query full-text boost counts space-separated words. A long Chinese or Japanese question without
spaces therefore gets little help from title-word matching or that boost; expect it to depend on
semantic search and the similarity floor.

Search ranks one pool of results (up to `max_results`, at least `candidate_count`) and caches it by
query and source scope. A selection of at most `min(10, 35%)` of the available sources searches only
those sources and is not cached, because one-off subsets would fragment the cache. A larger selection
is promoted to one full search over every available source, and the source filters narrow the
displayed page. Paging and display filters reuse the cached full-search pool; a small scope is
recomputed when it is paged or filtered.

The streaming endpoint sends a provisional page as each source finishes. Pagination uses regular links
and refreshes the page through the JSON endpoint without a full document reload.

*WikiMed timing: a complete search is 26 ms (median over 785 benchmark queries).
A Raspberry Pi Zero 2 W takes around 150 ms.*

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

¹ These 200-query sets are generated automatically from the articles, not typed by real users. The
other sets use hand-written queries; with 25–60 queries each, their numbers are accurate to roughly
±12–18 points.
² Spanish, French, German, Chinese, Hindi, Arabic, Russian, Japanese, Swahili and Portuguese, mixed.

### Single words in other languages

These tests search the English articles with one word in another language, using 15 common medical
words per language (malaria, diabetes, fever, cough, pregnancy, heart, blood, …). The test uses
purpose-built translations, so less common languages may contain mistakes.

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
- **No setup.** Kiwix search works as soon as a ZIM is available. Zimantic indexes each ZIM first.
- **Smaller footprint.** Zimantic stores a `.sqlite` and `.faiss` per ZIM and needs more RAM.

## Requirements

- Python 3.11–3.14 with the current native dependency wheels. `libzim` ships
  wheels for specific Python ABIs and platforms, so choose an interpreter
  with a compatible wheel; update this range when the native dependencies add
  support for a newer Python version.
- 32-bit Linux platforms (such as armhf/armv7) have no prebuilt wheels for the
  native dependencies. They work when those dependencies are installed as
  distribution packages instead; Debian 13/trixie or newer ships
  `python3-onnxruntime`, `python3-faiss`, `python3-libzim`, `python3-sentencepiece`
  and `python3-pymupdf` for armhf. Create the environment with
  `--system-site-packages` so Zimantic can use them.
- About 120 MB for the default embedding model, plus your ZIM files (the English-only `e5-small-v2`
  is about 35 MB on disk).
- Wikipedia-style ZIM files (Zimantic relies on their predictable HTML structure to find useful content blocks).
- Local indexed results are served directly by Zimantic, so `kiwix-serve` is not required.
  With `kiwix_server` set, remote-only results open through the links returned by kiwix-serve's own
  search API.

## Where your files go

The installed CLI is separate from runtime data. By default the data directory is
`~/.local/share/zimantic`, so `config.toml` and its relative paths use the defaults:

```
~/.local/share/zimantic/
├── config.toml          ← settings (paths below are its defaults)
├── model/               ← model_dir: model.onnx + tokenizer vocabulary      (you download)
├── zims/                ← zim_dir:   your .zim files                          (you download)
├── indexes/             ← index_dir: <name>.sqlite + <name>.faiss for each ZIM
├── catalog.sqlite       ← shared ZIM metadata cache (beside indexes/, not a ZIM index)
└── zimantic.pid         ← server PID (present while serving)
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

   On 32-bit armhf, where the native dependencies have no wheels, install the
   Debian packages instead and let the environment use them:

   ```bash
   sudo apt install python3-venv python3-numpy python3-tqdm python3-onnxruntime \
       python3-sentencepiece python3-faiss python3-libzim python3-pymupdf \
       python3-chardet python3-fastapi python3-uvicorn
   python3 -m venv --system-site-packages .venv
   .venv/bin/pip install --no-deps .
   ln -sfn "$PWD/.venv/bin/zimantic" ~/.local/bin/zimantic
   ```

3. **Download the default multilingual model into `model/`:**

   ```bash
   mkdir -p model
   curl -L -o model/model.onnx https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/onnx/model_quantized.onnx
   curl -L -o model/sentencepiece.bpe.model https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/sentencepiece.bpe.model
   ```

   English-only and larger multilingual models use the same `model_dir` layout; see
   [Using a different embedding model](#using-a-different-embedding-model). Zimantic warns if a file
   differs from the documented default checksum, but still runs.

4. **Put your ZIM files in `zims/`.** Download them from
   [library.kiwix.org](https://library.kiwix.org) or [download.kiwix.org/zim](https://download.kiwix.org/zim/).

5. **Check `config.toml`.** Point `zim_dir`, `model_dir` and `index_dir` at your folders if needed.
   Set `kiwix_server` only if you also want remote Kiwix sources.

6. **Check the installation:**

   ```bash
   zimantic --help
   ```

   For a development runtime check, activate the `uv` environment and run
   `python -m pytest tests/test_runtime_api.py`; use `python -m pytest` for the full suite.

### Windows

Zimantic supports 64-bit Windows (x86-64) on Python 3.11–3.14. Windows ARM64 and
32-bit Windows are not supported because `libzim`, `faiss-cpu` and `onnxruntime`
do not publish wheels for those targets.

The steps above work with the Windows equivalents:

```powershell
git clone https://github.com/OscSanto/zimantic.git zimantic
cd zimantic
uv sync --locked --no-dev
.venv\Scripts\activate
zimantic --help
```

Download the model with PowerShell instead of `curl`:

```powershell
New-Item -ItemType Directory -Force model | Out-Null
Invoke-WebRequest -Uri https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/onnx/model_quantized.onnx -OutFile model\model.onnx
Invoke-WebRequest -Uri https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/sentencepiece.bpe.model -OutFile model\sentencepiece.bpe.model
```

`zimantic reload` is unavailable on Windows because it relies on `SIGHUP`; restart
`zimantic serve` instead. Stop the server before replacing or deleting a ZIM or
index file, since Windows does not allow an open file to be replaced in place.

### Using a different embedding model

The default is `multilingual-e5-small`, but Zimantic loads whatever ONNX graph and tokenizer
vocabulary are in `model_dir`. Two tokenizer formats are supported:

- `sentencepiece.bpe.model` selects the XLM-R SentencePiece tokenizer used by the multilingual-e5
  models.
- `vocab.txt` selects the BERT WordPiece tokenizer used by the English e5-v2 models.

Zimantic chooses by which vocabulary file is present. If a directory contains both, it reads
`tokenizer_config.json` when available; otherwise it keeps the historical SentencePiece choice. The
ONNX graph itself cannot reliably identify its tokenizer, so this file-based switch is the dependable
signal. Apart from the vocabulary, the model must follow the same e5 contract: `input_ids` and
`attention_mask` inputs (plus `token_type_ids` when the graph declares it), a `last_hidden_state`
output, mean pooling, L2 normalization, and the `query: ` / `passage: ` prefixes.

For example, to try the larger multilingual model:

```bash
mkdir -p model-e5-base
curl -L -o model-e5-base/model.onnx https://huggingface.co/Xenova/multilingual-e5-base/resolve/main/onnx/model_quantized.onnx
curl -L -o model-e5-base/sentencepiece.bpe.model https://huggingface.co/intfloat/multilingual-e5-base/resolve/main/sentencepiece.bpe.model
```

Replace `base` with `large` in both URLs to try `multilingual-e5-large`. For the English-only
`e5-small-v2`, download its WordPiece vocabulary instead:

```bash
mkdir -p model-e5-small-v2
curl -L -o model-e5-small-v2/model.onnx https://huggingface.co/Xenova/e5-small-v2/resolve/main/onnx/model_quantized.onnx
curl -L -o model-e5-small-v2/vocab.txt https://huggingface.co/Xenova/e5-small-v2/resolve/main/vocab.txt
```

The same layout works for other BERT WordPiece e5-v2 models such as `e5-base-v2`.

Point `model_dir` at the new folder, use a separate `index_dir` so the old indexes stay usable, and
rebuild every index:

```toml
model_dir = "./model-e5-small-v2"
index_dir = "./indexes-e5-small-v2"
```

```bash
zimantic build --force
zimantic serve
```

Vectors are model-specific, and Zimantic does not store a model fingerprint in the index, so always
rebuild after switching models. A checksum warning for a non-default model is expected; the check is
informational.

These measurements use 256-token passages, batch size 32 and 4 CPU threads on one x86-64 host.
Absolute numbers vary with CPU, but the ratios show the trade-off:

| Model | Int8 ONNX | Vector dim | Extra RSS¹ | Embedding² | Notes |
|---|---:|---:|---:|---:|---|
| `multilingual-e5-small` | 118 MB | 384 | ~190 MB | 68/s (1×) | Default; 100+ languages |
| `multilingual-e5-base` | 279 MB | 768 | ~340 MB | 26/s (0.38×) | 100+ languages; ~2.7× slower |
| `multilingual-e5-large` | 562 MB | 1,024 | ~730 MB | 9/s (0.13×) | 100+ languages; ~7.8× slower |
| `e5-small-v2` | 34 MB | 384 | ~65 MB | 64/s (0.93×) | English only; WordPiece |

¹ Additional resident memory for the model and vocabulary after loading, excluding ONNX Runtime's
batch arena. Add this to the server's baseline footprint (see **Measured memory use**); leave about
0.5 GB of headroom for base and 1 GB for large. On hosts with 2 GB RAM or less the arena is disabled,
so builds stay closer to these figures.

² Passages per second while embedding 256 passages in batches of 32. A single query embedding takes
about 14 ms with either small model, 39 ms with base and 110 ms with large, so query latency rises in
the same direction.

`e5-small-v2` has the same 12-layer, 384-hidden encoder as `multilingual-e5-small`; the smaller file
comes from a 30k-entry instead of a 250k-entry embedding table. It therefore saves roughly 125 MB of
resident memory but embeds at about the same speed, and it cannot search non-English text. Choose it
only for an English-only library on a RAM-constrained host; otherwise the default gives much broader
coverage for the same compute.

Vector storage also grows with dimension: an 8-bit IVF index stores one byte per dimension per article
(384 B, 768 B and 1 KB), while a flat index for under 10,000 articles stores float16 vectors
(0.75 KB, 1.5 KB and 2 KB). For a mostly English library on a small device, keep the default; base is
a reasonable upgrade when cross-language recall matters; large needs a machine with more RAM.

## Development install

For a development environment based on the checkout:

```bash
uv sync
. .venv/bin/activate              # Windows: .venv\Scripts\activate
```

`uv sync --no-dev` omits test tools. `pip install .` also works in a virtual environment.

## Release and deploy

The Makefile can increment the patch version, update `uv.lock`, commit and publish an
annotated tag:

```bash
make release                 # 0.1.3 -> 0.1.4
make release VERSION=1.0.0  # explicit version override
```

To deploy a published tag to the default test box:

```bash
make deploy VERSION=v0.1.4
make release-deploy           # release, then deploy the new version
```

Set `DEPLOY_HOST` and `DEPLOY_PATH` to override the SSH target and remote checkout.
The deploy target updates the existing checkout, reinstalls the package, restarts
`zimantic.service`, and verifies the HTTP service and installed package version.

For a development deploy, stage the changes to send and apply them to the existing
remote checkout without checking out a tag. Before applying the patch, the deploy
resets tracked files in the remote checkout to `HEAD`, discarding previous
development-deploy changes:

```bash
git add path/to/changed-file
./scripts/deploy.sh --dev
# or: make deploy DEV=1
```

The development deploy leaves the newly applied changes in the remote checkout.

## Use

```bash
zimantic build                        # index every .zim in zim_dir (the default)
zimantic -v build                     # show info-level logging
zimantic build -vv                    # show debug-level logging
zimantic build zims/x.zim             # index one file
zimantic build zims/a.zim zims/b.zim  # several files
zimantic build --workers 4            # build up to four independent ZIMs at once
zimantic build /media/usb             # every .zim in a folder
zimantic build zims/a.zim /media/usb  # mix files and folders
zimantic build --fast                 # quick title-word + full-text index (no vectors)
zimantic build --force zims/x.zim     # rebuild one already-indexed ZIM
zimantic serve                        # web page; unindexed ZIMs use native full-text search
zimantic serve --fast                 # start without a model or vectors
zimantic serve --debug                # include ranking scores and explanations; restart on SIGHUP
zimantic serve --dev                  # alias for --debug
zimantic reload                       # ask a running server to rescan indexes and ZIMs
```

`build` accepts files or folders; a folder means every `*.zim` file under it, and with no arguments
it uses `zim_dir`. Existing ZIM indexes are skipped. Pass `--force` to rebuild selected indexes.
Logging defaults to warnings, except that `serve` uses info when stderr is a terminal;
repeat `--verbose`/`-v` for info and debug output.

**Fast indexes.** `build --fast` stores titles and paths without reading article bodies or running the
model. Title-word and ZIM full-text search work; a normal `build` creates the vector index.

The desktop profile embeds 32 articles at a time (mobile 8, supercomputer 128). Independent ZIMs can
be read and built concurrently with `--workers N` or the `build_workers` setting; when unset, the
default scales with CPU and RAM. Full builds use a bounded shared embedding pipeline: at most
`embedding_workers` model calls run at once, and each ZIM can have only a small number of batches
waiting for embedding. When unset, `embedding_workers` also accounts for `embed_threads` so the model
is not oversubscribed. Tune `batch_size`, `build_workers`, `embedding_workers` and `embed_threads` in
`config.toml` for different hardware. Do not run concurrent builds that target the same ZIM index
identity (filename stem and source byte size).

**Hardware profiles.** Tuning keys in `config.toml` are commented out by default, so Zimantic selects
mobile, desktop or supercomputer defaults from the machine's resources. Copy
`config.pi-zero-2w.toml` or `config.pi-5.toml` to pin settings.

As a rule of thumb, keep enough free disk space when building indexes: about **20% of total ZIM file size**.

**Automatic indexing with systemd.** Ready-to-copy user units in `deploy/` watch `zims/`, build
indexes and reload the server as ZIMs become available:

- `deploy/zimantic.service` — the server, with `Restart=always`, restart rate limiting, and
  sandboxing. Copy it to `~/.config/systemd/user/`, then `systemctl --user enable --now zimantic`.
- `deploy/zimantic-indexing-fast.path` + `deploy/zimantic-indexing-fast.service` — watch `zims/` and run a fast build
  followed by `reload` as a ZIM becomes available.
- `deploy/zimantic-indexing.service` + `deploy/zimantic-indexing.timer` — run the full build
  (with meaning vectors) once a night.

The path-triggered fast build runs for each ZIM in `zims/`. The full build runs from 01:00–06:00
local time by default; set its `ExecCondition` and timer to select a different window. A shared `flock`
prevents overlapping builds.

The server unit retries after 10 seconds but stops after three starts in ten minutes, so a server
that cannot fit its archives fails visibly instead of looping through OOM kills. Check
`systemctl --user status zimantic` and `journalctl --user -u zimantic`; after fixing the cause,
`systemctl --user reset-failed zimantic` clears the rate limit. Its header also shows
`MemoryHigh`/`MemoryMax` pairs for a Pi Zero 2 W and a Pi 5; uncomment and adjust them for the
device, leaving headroom for the OS and kiwix-serve. `MemoryHigh` throttles first; `MemoryMax` is the
hard limit that OOM-kills only Zimantic.

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

A running `serve` discovers indexes with `zimantic reload`; a fast index remains
searchable while a full index builds.

JSON API examples:

- `GET /search?q=...&zim=<name>&limit=10&offset=0&source=<key>` returns one page of results.
  A small `zim` selection searches only those sources. A selection larger than `min(10, 35%)` of
  the available sources is promoted to a full search so its cached pool can be shared; pass
  `source=<key>` to narrow the displayed results. Totals are in `X-Total-Count`, `X-Has-More`,
  `X-Offset`, `X-Page` and `X-Page-Count` headers. An offset past the last page is
  clamped to the last real page, and `X-Offset`/`X-Page`/stream `offset`/`page` report that
  effective position.
- `GET /search/stream?q=...&limit=10&offset=0&source=<key>` returns newline-delimited progress
  events and result snapshots. `snapshot` and `done` events carry the effective `page` and
  `page_count`. While sources are still working, the stream emits a `progress` heartbeat about every
  700 ms; when a source finishes in between, a fresh snapshot is emitted instead. A source that
  finishes between ranking passes reports no count until the next snapshot; the final `done` event
  always carries the complete per-source count map.
- `GET /content/<zim>/<path>` serves a toolbar shell for a local ZIM entry; `GET /content/<zim>`
  serves the resolved ZIM homepage. The shell keeps the navigation bar in the page and loads the
  article itself from an inner iframe pointed at `/article/<zim>/<path>`, so the toolbar stays
  visible even when the article is missing or fails. Unreadable entries render a friendly error
  panel in the shell instead of showing the raw `/article` JSON error body. `GET /article/<zim>/<path>`
  serves the raw entry: redirects are followed, article links and assets resolve under the same
  route, and entries larger than `max_article_bytes` return `413`. `max_html_bytes` limits how much
  HTML is parsed for indexing, previews and phrase checks; `max_article_bytes` limits the uncompressed
  response body because Zimantic reads each requested entry into memory, including binary assets. The
  toolbar shell also uses the viewed article's own `<title>` and declared icon for the browser tab
  title and favicon (falling back to the ZIM's source icon); when the shell is embedded in the modal,
  the outer page mirrors them and restores its own title and favicon when the modal closes. HTML and
  XHTML articles include a small navigation bar with Close, Home, Random, Permalink and a Zimantic
  search control. Alt-clicking Random opens the homepage of a random available ZIM. Modal articles also expose
  icon-only navigation through visible search results and, for nested articles, the adjacent article
  links listed on the article's parent page. When a modal article opens another modal article,
  the preceding article can also provide the navigation context. Back navigation uses browser
  history when available, otherwise the Close control attempts to close the tab. In a directly
  loaded `/content/` view, Close closes the tab or window when the browser allows it; otherwise it
  goes back through history, or opens the library when the article is the first history entry.
  Article views keep the current ZIM and path in the address bar, including from `/library`, so the
  URL can be shared.
  Selecting text adds the passage to that URL and to the toolbar's Copy link action; reopening the
  link scrolls to and highlights the passage. Fragment links scroll inside the article without
  reloading the shell. The modal's sandbox has an opaque origin, so articles that use Web Storage
  get an in-memory fallback and IndexedDB loads an in-memory polyfill, then the modal offers to open
  the same `/content/` view in a new tab, where the browser's persistent storage is available. The
  IndexedDB polyfill bundles `fake-indexeddb` (Apache-2.0; see `indexeddb-shim.LICENSE`).
  `GET /random?zim=<name>`
  redirects to a random indexed article's shell. `GET /random` without `zim` redirects to the
  homepage of a random available local source; the article toolbar's Random control uses the first
  form, and Alt-clicking it uses the second. Links from an offline article to an external
  `http` or `https` site show an in-modal notice identifying the destination as online and offer
  to open it in a new tab, avoiding failures from sites that disallow iframe embedding. A directly
  loaded `/content/` view tries the captured copy for same-site links before leaving for the live
  site, so only links that are missing from the ZIM go online.
- Start the server with `zimantic serve --debug` to include ranking scores and explanations in both
  search endpoints. Debug result objects additionally include a `ranking` breakdown with each
  semantic/title/full-text rank and contribution, title-match signals, intent and
  disambiguation adjustments. Stream `snapshot` and `done` events always carry source progress and
  a per-source result count, and in debug mode add `matched_by` match-method counts; the
  non-streaming endpoint exposes those match counts in the `X-Matched-By` header, and the web UI
  renders them above the results. Debug output is configured for the whole server; it is not a
  URL parameter.
  SIGHUP restarts the whole server in debug mode; `serve --dev` is an alias for this mode.
- `GET /sources`, `/config`, `/library.json` and `/health` return source,
  configuration, local-library and health information. `/sources` describes the searchable
  sources (local and remote) and includes catalog metadata when available. `/library.json`
  describes every local archive, including unreadable archives and archives that are not
  searchable yet. Source records include a `homepage` path when the local ZIM exposes one, plus
  `available`, `searchable` and `error` flags.
  `/library.json` also exposes
  `uuid`, `publisher`, `perma_ref`, `flavour`, `category`, `media_count`, `license`, `scraper`,
  `source`, `attribution`, `relation`, `counter`, and `has_fulltext_index` from the catalog. The
  UUID is the book identity: duplicate copies of the same ZIM are collapsed to one source, preferring
  a readable copy. An unreadable archive stays in `/library.json` with `available: false` and an
  `error` so the library page can explain why it cannot be opened. Health is read-only and reports
  index/archive capabilities, entry counts, homepage resolution, embedding state, and search and
  preview cache statistics.

Reloading indexes is **not** an HTTP API: running servers rescan via `zimantic reload` or
`kill -HUP <pid>`.

Exact queries use small LRU caches controlled by `cache_size` and `cache_bytes` (32 MiB per cache by default).
The ranked pool is shared across pages and display filters for a given search scope, and invalidated
when the set of searchable sources differs. Small source selections get their own scope so each
selected ZIM contributes a larger share of the pool, but that scope is not cached; larger selections
share the cached full-search pool.
For large libraries, `search_workers` controls how many ZIM indexes are searched in
parallel. `max_stream_ranks` limits completion-spaced intermediate ranking passes; timed snapshots
are additionally emitted about every 700 ms so a slow query keeps updating, and the final result
still ranks every source. A lower `max_stream_ranks` reduces CPU use on small devices. The
automatic profiles use 8 workers and 16 completion-spaced passes on desktops, and 2 workers and 8
passes on low-memory devices.
