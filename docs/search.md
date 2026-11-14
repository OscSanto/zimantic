# Search, models, and ranking

## Using a different embedding model

The default is `static-similarity-mrl-multilingual-v1`, a static multilingual model: an EmbeddingBag over a 105,879-token WordPiece vocabulary with 1,024-dimensional token vectors. Zimantic keeps the leading **256 dimensions** (Matryoshka truncation) and renormalizes them. It encodes about 12,300 passages per second through the production Rust tokenizer and int8 graph, and it needs no prompt prefixes. It covers 50 languages, a narrower set than the 100+ languages of `multilingual-e5-small`. Its int8 ONNX graph is about 108 MB.

`multilingual-e5-small` is the quality-first alternative: a 384-dimensional transformer with about 118 MB int8, slower embedding, and better retrieval overall. The trade-offs are recorded in [build performance](build-performance.md#static-embedding-models). Use it when retrieval quality matters more than build time or memory. Use the default for a large library or a slow build host.

Zimantic loads the ONNX graph and tokenizer vocabulary in `model_dir`. Two tokenizer formats are supported:

- `sentencepiece.bpe.model` selects the XLM-R SentencePiece tokenizer used by the multilingual-e5 models.
- `vocab.txt` selects the BERT WordPiece tokenizer used by the English e5-v2 models and the static default.

Zimantic selects the tokenizer from the vocabulary file. If the directory contains both files, it reads `tokenizer_config.json` when available. Otherwise it prefers WordPiece when `tokenizer.json` declares a WordPiece model (the static default) or `zimantic_model.json` describes a static model, and keeps SentencePiece as the fallback for older e5 directories. The vocabularies are not interchangeable: SentencePiece ids exceed the static graph's 105,879-row embedding table. For WordPiece models, Zimantic uses the Hugging Face Rust `tokenizers` package when the directory contains `tokenizer.json`, and falls back to a built-in Python tokenizer when that package or file is unavailable (for example a hand-built directory with only `vocab.txt`). The Rust path is about five times faster end to end. The graph does not identify its tokenizer, but it identifies its pooling contract: a 3-D `last_hidden_state` is mean-pooled, while a 2-D `sentence_embedding` is already pooled and is used as-is. A `zimantic_model.json` beside the graph records anything the graph cannot express: `truncate_dim`, `query_prefix`, and `passage_prefix`. Downloaded static models omit both prompts, while e5 models use `query: ` and `passage: `. Without that file, Zimantic assumes an e5-style transformer with those prefixes and no truncation.

`zimantic model` downloads pinned revisions and verifies each file's SHA-256 before it replaces the destination. It removes vocabulary files and a model config that belong to a different model from a reused precision directory, so switching models cannot leave two tokenizers side by side. For the static default, fp32 is about 434 MB, fp16 about 217 MB, and int8 about 108 MB. The default is int8, the smallest download, and it is optimized for CPU inference. On a GPU host, use the `multilingual-e5-small` transformer instead: `zimantic model fp16 --model multilingual-e5-small` (about 235 MB, `model/multilingual-e5-small/fp16`). The static default gains little from a GPU, and the transformer retrieves better. See [GPU acceleration](usage.md#gpu-acceleration) for a ready-to-copy preset. Use fp32 only to compare against a full-precision baseline. The probe check below accepts an index built with one precision when the server uses another precision, without re-embedding.

The static model ships its vocabulary only as Hugging Face `tokenizer.json`. The download command derives `vocab.txt` from it and verifies a pinned checksum, so a hand-built `model_dir` needs either the same derived file or an e5 vocabulary instead.

To use the quality-first multilingual model:

```bash
zimantic model int8 --model multilingual-e5-small --dir model-e5
```

That command writes `model-e5/int8`, because non-default models get their own directory under the model root. Point `model_dir` at it and rebuild:

```toml
model_dir = "./model-e5/int8"
index_dir = "./indexes-e5"
```

To try the larger multilingual models, replace `small` with `base` or `large`:

```bash
zimantic model int8 --model multilingual-e5-base
zimantic model int8 --model multilingual-e5-large
```

For the English-only `e5-small-v2`, the command selects its WordPiece vocabulary directly:

```bash
zimantic model int8 --model e5-small-v2 --dir model-e5-small-v2
```

The same command works for other BERT WordPiece e5-v2 models such as `e5-base-v2` and `e5-large-v2`; run `zimantic model --list` for the full set. Each precision goes into its own directory (`model-e5-small-v2/int8`, for example), so you can compare or switch without another download. Pass `--force` to replace an existing file whose checksum does not match.

Set `model_dir` directly to the directory that contains `model.onnx` and its tokenizer vocabulary (normally the selected precision directory). Zimantic does not search parent directories for model files. Use a separate `index_dir` to keep existing indexes available while you build indexes for the selected model.

Vectors are model-specific. Each full index records a fingerprint of `model.onnx`, the tokenizer vocabulary, and `zimantic_model.json`. A server configured with a different model ignores those vectors and serves title and full-text results until the index is rebuilt. Rebuild the index after a model switch to enable semantic search. A switch between the static default (256 dimensions) and any e5 model (384 dimensions or more) also changes the vector dimension, so it always requires a rebuild.

An installation can use `multilingual-e5-small`. The default model affects only what `zimantic model` downloads and what a fresh installation fetches. To use the default static model in `model/int8`, run `zimantic model int8 --force` and rebuild the indexes.

A full build also stores a small set of fixed probe sentences and their embeddings. When the model fingerprint differs, `serve` re-embeds those stored probes with the configured model and reuses the index if the mean cosine similarity is at least 0.99 and the minimum is at least 0.98. For example, an index built with the FP32 export works with the int8 export of the same model. The check does not accept a different model or tokenizer. An index without stored probes requires the same model file. The check is deliberately conservative: a different fine-tune with the same architecture fails it.

These measurements use 256-token passages, batch size 32, and 4 CPU threads on one x86-64 host. Absolute numbers vary with CPU, but the ratios show the trade-off:

| Model | Int8 ONNX | Vector dim | Extra RSS¹ | Embedding² | Notes |
|---|---:|---:|---:|---:|---|
| `static-similarity-mrl-multilingual-v1` | 108 MB | 256 | ~130 MB | ~12,300/s | Default; 50 languages; static |
| `multilingual-e5-small` | 118 MB | 384 | ~190 MB | 68/s (1×) | Quality-first alternative; 100+ languages |
| `multilingual-e5-base` | 279 MB | 768 | ~340 MB | 26/s (0.38×) | 100+ languages; ~2.7× slower |
| `multilingual-e5-large` | 562 MB | 1,024 | ~730 MB | 9/s (0.13×) | 100+ languages; ~7.8× slower |
| `e5-small-v2` | 34 MB | 384 | ~65 MB | 64/s (0.93×) | English only; WordPiece |

¹ Additional resident memory for the model and vocabulary after loading, excluding ONNX Runtime's batch arena. Add this to the server's baseline footprint (see [Measured memory use](search.md#measured-memory-use)); leave about 0.5 GB of headroom for base and 1 GB for large. On hosts with 2 GB RAM or less the arena is disabled, so builds stay closer to these figures.

² Passages per second while embedding 256 passages in batches of 32. The static model's rate is measured with the production encoder and depends on the ONNX graph. A single query embedding takes about 0.04 ms with the static default, 14 ms with either e5 small model, 39 ms with base, and 110 ms with large.

`e5-small-v2` has the same 12-layer, 384-hidden encoder as `multilingual-e5-small`; the smaller file comes from a 30k-entry rather than a 250k-entry embedding table. It therefore saves roughly 125 MB of resident memory but embeds at about the same speed, and it cannot search non-English text.

Vector storage grows with dimension. An 8-bit IVF index stores one byte per dimension per article: 256 B for the static default, and 384 B, 768 B, and 1 KB for the e5 models. A flat index for under 10,000 articles stores float32 vectors. FAISS flat indexes do not provide float16 storage; the SQLite staging table uses float16 during a build, but vectors are widened to float32 when written to FAISS.

## Measured memory use

As a reference point, a clean benchmark with `wikipedia_en_100_2026-08.zim` (5,056 articles) and one local index measured these peak resident set sizes:

| Operation | Peak RSS |
|---|---:|
| Full index rebuild | 301.7 MiB |
| Semantic server (`serve`) | 323.5 MiB |
| Title and full-text server (`serve --fast`) | 65.6 MiB |

The server benchmark loaded the embedding model, opened the single ZIM index, and handled health, source-discovery, and search requests. `serve --fast` omits the model and FAISS, so it uses much less memory but does not provide semantic search.

Most of the footprint comes from the model. The static default uses about 108 MB of int8 weights plus a 105k-entry WordPiece vocabulary. The `multilingual-e5-small` alternative uses about 118 MB of weights plus the SentencePiece vocabulary (~50 MB of native memory). The English-only `e5-small-v2` replaces both with a ~34 MB model and a small WordPiece vocabulary. The table lists `multilingual-e5-small` measurements; the static default has smaller per-batch activations.

Models are loaded one time and are not duplicated. `gc.collect()` cannot release them because they are C++ allocations, not Python objects. ONNX Runtime reads the file into a temporary buffer while it loads, so there is a short-lived ~2× peak at startup. Its CPU memory arena keeps the working set of the largest embedding batch for the session lifetime. For the e5 transformer at `batch_size = 32`, that arena alone uses ~400 MB and retains it; static batches are much smaller.

On hosts with 2 GB RAM or less, Zimantic therefore disables the arena and returns freed heap memory to the OS between batches (`malloc_trim` on glibc). This limits build memory near the model size at a modest throughput cost. Machines with more RAM keep the arena for faster builds. `serve` embeds one query per active search. Up to `max_concurrent_searches` queries are in flight, but each query is one short sequence, so the arena costs little there either way.

## How it works

### 1. Indexing a ZIM file (`build`, once per ZIM file)

Zimantic visits every entry one time and skips images, stylesheets, and scripts. It stores redirects as title-only entries that point to their article, so a search for "USA" finds the United States page.

Some ZIM files store article bodies in JSON or Markdown behind a JavaScript shell. Zimantic reads that companion and keeps each article's own path as a deep link, so results point to the real article rather than the shared shell. This behavior covers three layouts: Kiwix's `content/page_content_<id>.json`; media and app bundles that keep a `<slug>.json` companion (for example `videos/<slug>.json` or `playlists/<slug>.json`) keyed by the deep link; and docs/curriculum bundles (freeCodeCamp and similar) whose `<route>.md` holds the body under `content/`. Zimantic also indexes plain-text fields such as video descriptions and Markdown prose; it drops front matter and fenced code. It skips shell stubs and "enable JavaScript" notices.

Text extraction ignores stylesheets, scripts, footnotes, common boilerplate, and page chrome (category footers, navboxes, tables of contents, maintenance tags), including page chrome that is nested or hidden (`display:none`, `visibility:hidden`, the `hidden` attribute). When a page repeats the same text under `lang-*` classes (parallel translations, as in TED talks), Zimantic keeps only the `lang-default` copy. It collects visible blocks in priority order, with paragraphs preferred, and block boundaries keep neighboring text runs from running together.

Zimantic extracts each article's excerpt for the embedding and discards it after it builds the vector. A separate lexical body is tokenized into the contentless FTS5 `body` table up to `max_body_text_bytes` (1 MiB per item by default). This limit is independent of `max_preview_chars` (1,000 by default), which limits the embedding excerpt and the preview. Extraction stops at a UTF-8 character boundary. `body_truncated_count` reports how many rows the budget or an incomplete ZIM parse cut off.

At search time, Zimantic extracts only the displayed local results again for previews, limited to `max_preview_chars` at a word boundary. It hydrates only the first `preview_limit` results of a page (10 by default, matching the default page size; 0 disables previews); the rest keep an empty preview field (`lead`). Preview extraction reads at most 32 KiB of HTML per page and falls back to the `max_html_bytes` limit when that prefix has no readable text. A few large pages thus do not hold up the whole result page.

Zimantic skips entries that are assets, JavaScript shells, empty pages, scanned PDFs without text, or other non-article content. Pages that ship only script or link references (for example PhET simulations) stay title-only, so title search finds them. The builder also skips an entry whose decompressed size exceeds `max_entry_bytes` (1 GiB on desktop, 256 MiB on the mobile profile). This high safety limit preserves large, useful articles and prevents pathological entries from exhausting memory before the parser limit applies. Companion JSON and Markdown bodies have the same limit.

Zimantic supports other ZIM files (Stack Exchange, Gutenberg, TED, and others) when they contain readable text: HTML, XHTML, plain text, and PDFs. It reads PDF text from the file's embedded text layer and skips scanned documents without one.

Zimantic embeds titles and temporary excerpts with [static-similarity-mrl-multilingual-v1](https://huggingface.co/sentence-transformers/static-similarity-mrl-multilingual-v1) (static int8 ONNX, about 108 MB, Matryoshka-truncated to 256 dimensions). Inputs use a fixed budget of 256 tokens. The quality-first alternative, [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) (int8 ONNX, about 118 MB, 384 dimensions), is also available. See [Using a different embedding model](search.md#using-a-different-embedding-model) for both models, their tokenizers, and their memory and speed costs. Entries without body text, such as JavaScript loader pages, stay searchable by title and full text but have no semantic vector. Zimantic creates previews from the local ZIM file only for results on the displayed page. Preview extraction gets its own budget of `search_timeout_seconds` after ranking; it does not inherit the already-elapsed search deadline, so a slow source fan-out does not truncate the excerpts. Failed or deadline-truncated previews are not cached.

Each ZIM file gets an immutable generation under `index_dir/<provider>/<collection-slug>-<hash>/<source-slug>-<hash>/generations/<build-generation>/` (`index.sqlite`, optional `index.faiss`, and a `lease`), plus one shared catalog:

- `index.sqlite`: titles, paths, redirect targets, title postings, and a separate contentless positional FTS5 body index (`detail=full`, `columnsize=1`, `unicode61 remove_diacritics 2`) whose matches are BM25-ordered. The `vecs` table exists in every generation but is empty after a full build writes `index.faiss`; a fast build leaves it empty too. Raw excerpts and raw derived text are not stored.
- `index.faiss`: the vectors (the only place finished embeddings are stored), in one of two layouts that depend on article count. A fast or empty-semantic build has no `index.faiss`.
- `staging-fast.sqlite` / `staging-full.sqlite` (when present): resumable private staging files. They are not published generations and are removed after a successful build or a fatal failure.
- `catalog.sqlite` (beside `index_dir`, not inside it): a provider-neutral `sources` table keyed by canonical source key, with the metadata used by `/sources` and `/library.json` (title, description, language, creator, publisher, date, provider ID, category, normalized tags, item count, license, origin, attribution, relation, MIME counts, homepage, ZIM full-text capability, and the icon as a BLOB). It stores the absolute ZIM file path only as a private indexed locator, never as source identity. It contains metadata and icons only, never vectors. It is a disposable cache: the ZIM files are the source of truth, and deletion makes the next scan rebuild it. A warm catalog lets `serve` list sources without opening every ZIM file. The startup scan opens each added, resized, or failing ZIM file to refresh its row, and the first scan after catalog deletion opens every ZIM file. After that, ZIM files and their libzim `Searcher` objects open lazily on first use. A missing or unreadable configured ZIM collection root (for example an unmounted USB drive) is an error that keeps the last catalog; Zimantic does not treat it as a library from which every ZIM file disappeared. While the directory is unavailable, the served `/sources` and `/library.json` lists are empty; the catalog rows stay on disk for the next successful scan after the directory returns. An unreadable subdirectory is logged as an error and its sources are omitted, but the rest of the library stays available.

The logical source key uses the provider, collection, and root-relative ZIM file path, never an absolute filesystem path. Relocating a collection root without a change to its relative layout preserves keys. Two ZIM files can share a stem and size and be distinct sources; each build writes an immutable generation and atomically replaces the `current` manifest. Build fingerprints use the ZIM file UUID, size, and whole-second mtime as practical change detectors, not proof of identical bytes. A same-size write that restores the mtime avoids detection. Keep source content immutable or use an external snapshot when strong consistency matters. Zimantic does not serve indexes with stem-based paths; build them with the active locator for indexed search. To copy a subset of finished indexes to another machine, see [Copying finished indexes to another machine](collections.md#copying-finished-indexes-to-another-machine).

Only the active index schema and canonical metadata are served or resumed. `build` reuses vectors from recognized v0 root-level pairs. Zimantic rebuilds older index schemas, including the compact body layout. Interrupted builds resume only within the active schema. Cross-model embedding probes permit vector reuse between compatible model exports.

| ZIM file size | Vector index | Why |
|---|---|---|
| Under 10,000 embedded articles | Flat: the query is compared with every vector | Exact, and small enough (a few MB) that comparing everything is fast |
| 10,000 embedded articles or more | IVF + 8-bit (SQ8): vectors are grouped into up to 4·√n clusters (limited so FAISS can train at least 39 points per centroid), and each search scans about 6% of the closest clusters by default (64 of 1,062 on WikiMed's 70k articles) | Comparing millions of vectors per search is too slow. On WikiMed (70k articles), scanning 64 of 1,062 clusters is within a few points of scanning every cluster, at less than half the search time (26 ms vs 66 ms). The probe count scales automatically for larger ZIM files; set `nprobe` in `config.toml` to use a fixed value instead. 8-bit numbers are nearly exact. **Heavier compression (for example PQ48) loses ~25% of the top hits.** |

Zimantic selects IVF training samples across the ZIM file and scales them with the number of clusters, with at least 39 samples per centroid as FAISS requires.

The `.faiss` file is memory-mapped, so searches read only the clusters they touch.

Builds resume from the last saved batch. Zimantic publishes FAISS files atomically, and a fast index stays available while a full index builds. A switch between `--fast` and a full build after an interruption restarts that index, because fast rows have no vectors to reuse. `build --force` applies the configured extraction and preview settings while it rebuilds the selected indexes.

Use `--force` to rebuild a completed index when the ZIM file has the same filename stem and byte size. Use it when you:

- select a different embedding model or tokenizer;
- change text extraction or indexing behavior;
- replace a ZIM file with different content of the same size;
- convert a full index to `--fast`;
- repair an index whose `.faiss` file is missing;
- replace a suspect index.

`--force` is not necessary when the ZIM file size differs, when a build resumes, or when a full build follows a fast build. It deletes the existing SQLite, FAISS, and upgrade files before it rebuilds. Use a separate `index_dir` to preserve indexes for another model.

### 2. Starting the server

At startup Zimantic reads the shared `catalog.sqlite` for source metadata, homepages, counts, and icons. A Files collection also caches root-document summaries there; startup and reload reuse them when candidate names, sizes, timestamps, and permissions match. The server resolves the `current` manifest of each finished index generation whose ZIM file is present and matches the build-time source fingerprint. A cold catalog is refreshed synchronously. The scan opens each ZIM file that is not in the catalog, whose fingerprint differs, or whose prior scan failed. The server opens other ZIM files and their libzim `Searcher` objects on the first search or article request. Zimantic also discovers ZIM files in configured ZIM collection roots without a usable SQLite index and uses each ZIM file's ZIM full-text index as a fallback.

The server ignores an index when its ZIM file is missing. If the ZIM file size does not match the index, the server uses the ZIM file without that index. Run `build` to index the ZIM file again. A partial or stale index does not hide an available ZIM file. For each source, a finished SQLite index takes precedence over the filesystem fallback; an unfinished index uses that fallback. FAISS vectors load in the background during startup, so title and full-text search work while semantic search initializes.

The page lists sources from every configured collection. A Kiwix collection discovers catalog sources and searches them with Kiwix full-text search. Catalog discovery is paged and limited per entry to `max_catalog_entries` (default 50,000); remote XML responses are limited to `max_response_bytes` (default 64 MiB). An unscoped search uses at most each entry's `max_remote_sources` (default 100); an explicit selection of a remote source always searches it. An explicit selection of a Kiwix collection schedules sources in catalog order up to the global `max_explicit_collection_sources` (default 500) and the request deadline, and reports any partial scope instead of claiming completion. Catalog refresh runs in the background: a failed refresh keeps the last complete catalog addressable, retries with a backoff that has limits, and reports the failure on the collection's discovery state. A failed source search is reported only in that request's result, so the next request can try again immediately.

Search without an index. `serve` works without a prior `build` and without `catalog.sqlite`. At startup, it scans configured ZIM collections and creates missing catalog metadata. A usable ZIM file is a search source. An indexed ZIM file without FAISS uses title and ZIM full-text search. A full index also provides SQLite body search. A ZIM file without a SQLite index uses its ZIM full-text index, but it has no SQLite title search, body search, or semantic search. ZIM full-text search matches titles, but it does not provide SQLite title ranking or its title bonus. If a ZIM file without an index has no ZIM full-text index, it appears in `/library` but not `/sources` and is not searchable.

| What exists for a local ZIM file | Semantic search (FAISS) | Title search (SQLite FTS5) | Body search (SQLite FTS5) | ZIM full-text search (libzim) |
|---|:---:|:---:|:---:|:---:|
| Full index (`.sqlite` plus matching `.faiss`) | yes | yes | yes | yes |
| Fast index (`.sqlite` only) | no | yes | no | yes |
| No index, ZIM file has a ZIM full-text index | no | no | no | yes |
| No index, no ZIM full-text index | no | no | no | no — library only, not searchable |

`serve --fast` skips the model and all vectors, leaving title and full-text search. A normal `serve` loads the model in the background; if `model_dir` is missing or unreadable it logs a warning and keeps serving lexical results. Zimantic ignores vectors built with a different embedding model in the same way. For local sources, `kiwix-serve` is not involved: Zimantic searches the ZIM file directly, and the ZIM file's ZIM full-text index is not a separate Kiwix index file. A Kiwix `[[collections]]` entry uses kiwix-serve's full-text API for remote sources.

FastAPI serves a lightweight HTML page that is accessible from any browser at `http://<host>:8090` (the `port` in `config.toml`). To serve it under a path such as `/zimantic`, set `web_base_path = "/zimantic"` and point a reverse proxy at the server with that prefix stripped:

```nginx
location /zimantic/ {
    proxy_pass http://127.0.0.1:8090/;
}
```

Generated links, assets, and API requests then stay under `/zimantic/`; open the page with the trailing slash.

Trusted and untrusted content. By default the `/content/` shell is trusted and the article document inside it is not sandboxed. This behavior keeps functionality intact, including persistent Web Storage and IndexedDB used by some offline sites. Set `secure_browsing = true` to sandbox the article document instead; the shell stays trusted, while the article runs with an opaque origin and gets in-memory storage fallbacks. Secure browsing protects the app from untrusted source scripts, but it is not complete isolation from untrusted content. A raw `/article/...` URL opened directly is a top-level document and is not sandboxed. A popup opened by a sandboxed article with `allow-popups-to-escape-sandbox` also runs with the app's origin and persistent storage. Use the `/content/` shell for the confined view, and treat direct `/article/` navigation and such popups as trusted. The setting is off by default.

Zimantic addresses content from every local provider as `/content/{source-key}/{path}` and `/article/{source-key}/{path}`. The source key is one percent-encoded path segment, so keys with colons (for example `files:modules:root`) appear as `/content/files%3Amodules%3Aroot/...`. `/article` supports byte ranges (`Accept-Ranges: bytes`, `206`/`416`), including `bytes=0-` media playback, and advertises wildcard CORS for opaque-origin article frames and their preflights. Raw HTML receives the navigation bridge, so its response bytes can differ from the stored item; Zimantic does not apply ranges to that decorated representation.

Reverse proxies preserve the original request target rather than decoding or collapsing percent escapes. The server rejects encoded path separators (`%2F`/`%5C`) using the ASGI `raw_path` and fails closed for path-bearing content requests when a trustworthy raw target is unavailable. A proxy that normalizes the URI before it forwards the request therefore disables path-bearing content serving even though the library and search pages load.

Reloading indexes without a restart. `zimantic reload` sends `SIGHUP` to a normal server, which rescans `index_dir` and `catalog.sqlite` and reconciles the available indexes. `kill -HUP <pid>` also works. In `serve --debug` mode (or its alias, `serve --dev`), `SIGHUP` restarts the whole server process and reloads all in-memory state.

### 3. Each search

1. Embed the query with the same model that indexed the source.
2. Run three searches on each selected local source. Sources run in parallel, up to `source_query_threads` at a time:

   | Search | Finds | Good at | Time |
   |---|---|---|---|
   | Semantic (FAISS) | Articles whose content-excerpt vector, within a fixed token limit, is closest to the query (cosine similarity) | Questions, descriptions, other languages | ~13 ms |
   | Title search (SQLite FTS5, BM25 ranking) | Titles that contain every word of the query, including redirect titles | Exact titles, alternate names (through redirect titles) | ~4 ms |
   | Full text (SQLite body index and the ZIM full-text index) | Articles that match either full-text source; Zimantic fuses the results by rank | Words in the article body, including words deep in the ZIM file | ~8 ms |

3. Collapse redirects. Every redirect hit resolves to the article it points to, and Zimantic merges duplicates, so each article appears one time.
4. Optionally filter weak semantic matches. When `min_cosine_similarity` is above `0` and the source also returns a title, body, or ZIM full-text match, Zimantic discards semantic candidates below that cosine similarity before ranking. The default is `0`, which disables the filter and lets every semantic candidate compete with lexical matches. Raise the value in `config.toml` to suppress weak semantic candidates in same-language queries.
5. Merge the three lists with Reciprocal Rank Fusion (RRF). The lists use different score scales: cosine similarity, BM25, and ZIM full-text rank. RRF ignores scores and uses rank only: an article earns `1 / (60 + its position)` from each list where it appears. The full-text contribution is doubled for queries of ten or more words. An article that ranks near the top in several lists beats one that ranks first in only one list. Zimantic collapses redirects before fusion, so an article contributes at most one time per list. A small lexical bonus favors titles that contain more query words, with shorter titles preferred when coverage is equal. Exact phrases in titles or readable article text and source-specific intent rules, such as WikiHow queries, provide additional signals. Stack Exchange-style profile pages under `/users/` rank lower unless the query asks for a user or profile.
6. Treat disambiguation pages specially. At build time Zimantic flags a page when its title ends in "(disambiguation)", when it renders the "This disambiguation page" footer, or when it carries a disambiguation category (the rendered `Category:` link or `wgCategories`). The required suffix is stored in the title, so no separate disambiguation metadata is necessary. Fast builds read an HTML prefix of fixed size for these signals; a footer that appears only beyond that prefix needs a full build for detection. A query that names the full title is navigation, whether it uses `air (disambiguation)` or `air disambiguation`; a question that names the base title and ends in `?` is navigation too. Those queries promote the page above the normal score range. Zimantic demotes any other query that merely matches the page, so the real article wins.

#### Queries in another language

A query in Chinese, Hindi, Arabic, or another language against an English ZIM file often shares no words with its titles or article text. Semantic search finds the right article. Cross-language cosine scores are lower than same-language scores, even for correct results. The default `min_cosine_similarity = 0` keeps those semantic candidates even when a spurious lexical match also exists. Raising the threshold can improve precision for same-language queries but may suppress cross-language meaning matches:

```toml
# config.toml
min_cosine_similarity = 0.80
```

This value is a recall/precision trade-off, not a separate language mode. Higher values admit fewer weak semantic matches; they do not affect title, body, or ZIM full-text results.

Lexical search is tuned for mostly English text with space-delimited words. The stopword list includes English and Spanish. SQLite's `unicode61` tokenizer treats consecutive Chinese characters as one title token, and the long-query boost counts space-separated words. A long Chinese or Japanese query without spaces relies more on semantic search.

Search fetches candidates from each source at the configured candidate budget, then ranks a first block and caches it by query, expanded source keys, and publication generation. The default relevance order ranks `max_results * 2` results (at least `candidate_count`); rare, outlier, diversity, and recency sorts rank `max_results * 4` results before cutting to the display page, while random samples the complete retrieved set without ranking it. Rare and outlier sorts select that block from a `max_results * 8` relevance frontier using `sort_relevance_mix` (90 by default), blending hybrid relevance with the sort's goal score. Every query order reports the number of distinct retrieved candidates as its total, not the size of the display block. Pages inside the materialized block reuse the cached ordering; a page beyond it ranks and sorts a larger block on demand, without widening source retrieval or repeating the fan-out just to advance one page. Empty-query browsing keeps a single bounded block. Every explicit source or collection selection defines the whole scope directly. Zimantic never promotes it to a broader search, and an unknown selector is an error rather than an implicit full-library search. A search that reaches `search_timeout_seconds` caches its partial pool for `partial_cache_seconds` (60 by default), so reloading the same query is fast instead of repeating the whole fan-out. Zimantic reports incomplete scopes as partial. Set `partial_cache_seconds = 0` to always retry immediately. An explicitly selected Kiwix collection is limited by `max_explicit_collection_sources` and the `search_timeout_seconds` deadline. It reports partial `scheduled`/`completed` counts instead of claiming completion.

The streaming endpoint sends a provisional page as each source finishes. Pagination uses regular links and refreshes the page through the JSON endpoint without a full document reload.

*WikiMed timing: a complete search is 26 ms (median over 785 benchmark queries). A Raspberry Pi Zero 2 W takes around 150 ms.*

## Compared with Kiwix search

![Ask a question, get the article](images/1-questions.png)
![Ask in another language](images/3-languages.png)
![How often the target article ranks first: Zimantic vs Kiwix](images/0-scorecard.png)

*Measured on WikiMed, the Wikipedia medical encyclopedia ZIM file (`wikipedia_en_medicine_maxi_2026-04`, 70,523 articles), compared with kiwix-serve's full-text search.*

The tables use three measures: **#1** means the target article is the first result, **Top 5** means it appears within the first five, and "not in top 20" means it does not appear in the first 20 results. Each percentage is the share of queries that met the measure.

### Results by type of search

| Type of search | Example query → target article | Kiwix | Zimantic | #1 Kiwix → Zimantic | Top 5 Kiwix → Zimantic | Queries |
|---|---|---|---|---|---|---|
| Common single words | "tuberculosis" → *Tuberculosis* | #3 | **#1** | 33% → **100%** | 93% → **100%** | 15 |
| Exact titles | "Malaria" → *Malaria* | #2 | **#1** | 72% → **100%** | 100% → 100% | 25 |
| Alternate names | "Leber's disease" → *Leber's hereditary optic neuropathy* | #2 | **#1** | 52% → **82%** | 70% → **99%** | 200¹ |
| Misspellings | "diabetis" → *Diabetes* | not in top 20 | **#1** | 0% → **60%** | 7% → **83%** | 30 |
| First-sentence descriptions | "(INN) is a non-steroidal anti-inflammatory drug (NSAID)." → *Ampiroxicam* | #5 | **#1** | 72% → **83%** | 82% → **92%** | 200¹ |
| Describing it without the name | "poor blood flow to part of the brain that kills brain cells" → *Stroke* | #17 | **#1** | 8% → **37%** | 37% → **78%** | 60 |
| Questions | "what causes lockjaw" → *Tetanus* | not in top 20 | **#1** | 12% → **45%** | 28% → **80%** | 40 |
| Questions and phrases in other languages² | "Herzinfarkt" (German: heart attack) → *Myocardial infarction* | not in top 20 | **#1** | 0% → **33%** | 0% → **53%** | 30 |
| Words deep inside an article | "When undergoing lymphadenopathy, these are described as feeling like a 'firm pea'." → *Facial lymph nodes* | **#1** | #2 | **68%** → 28% | **78%** → 76% | 200¹ |

¹ These 200-query sets are generated automatically from the articles, not typed by real users. The other sets use hand-written queries; with 25–60 queries each, their numbers are accurate to roughly ±12–18 percentage points.

² Spanish, French, German, Chinese, Hindi, Arabic, Russian, Japanese, Swahili, and Portuguese, mixed.

### Single words in other languages

These tests search the English articles with one word in another language, using 15 common medical words per language (malaria, diabetes, fever, cough, pregnancy, heart, blood, and so on). The test uses purpose-built translations, so less common languages contain mistakes.

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

### Where Kiwix performs better

- Words buried deep inside an article. Kiwix indexes every word, so it has better results on deeper searches (68% first vs 28%; in the top 5 they are nearly tied, 78% vs 76%). Zimantic indexes only each article's title and a content excerpt of fixed size.
- No setup. Kiwix search works as soon as a ZIM file is available. Zimantic builds an index for each ZIM file first.
- Smaller footprint. Kiwix needs no index files, while Zimantic stores a `.sqlite` and `.faiss` per ZIM file and uses more RAM.
