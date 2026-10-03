# Zimantic

**Offline search that understands what you mean, for Kiwix ZIM files.**

Zimantic adds meaning based searching on ZIM files; the compressed offline copies of Wikipedia and other websites which are published by Kiwix.  

Ask a question, describe something without knowing its name, misspell it, or search in one of 100+ languages, and Zimantic looks for the article closest to what you mean. It works best in widely spoken languages. Everything runs offline, on low resource hardware as small
as a Raspberry Pi Zero 2 W.

| You type | Kiwix search | Zimantic |
|---|---|---|
| "comet that comes back every 76 years" | *Halley's Comet* at #4 | *Halley's Comet* at **#1** |
| "moon" | *Moon* at #9 | *Moon* at **#1** |
| "الشمس" (Arabic for "the sun") | *Sun* not in the top 20 | *Sun* at **#1** |

## Features

- **Search by meaning**: questions and descriptions find articles even when they share no words with the title.
- **Alternate names and misspellings**: "andromeda galaxie" finds *Andromeda Galaxy*.
- **Multilingual**: one model covers 100+ languages; a query in one language finds articles written in another.
  Strongest in widely used languages (see the results below).
- **Several collections at once**: search any combination of your ZIMs, ranked together in one list.
- **Best of three searches**: combines meaning, title-word and Kiwix full-text search into a single ranking.
- **Clean results & previews**: redirects are merged into their article, so each article appears once, with its first paragraph as a preview.
- **Lightweight and offline**: runs on low resource devices, such as on a Raspberry Pi Zero 2 W (512 MB RAM), using ~250–300 MB while serving.
- **Web page and JSON API**: search from any browser on the network, or from your own programs.

Zimantic finds articles; [kiwix-serve](https://kiwix.org/en/applications/) displays them. Searching itself does not
require Kiwix, but serving the article pages does, so Zimantic works alongside Kiwix rather than replacing it.

# How it works

### 1. Indexing a ZIM (`build`, once per ZIM)

**What gets read.** Every entry in the ZIM is visited once. Images, stylesheets and scripts are skipped.
Redirects, including the small "forwarding" pages some
ZIMs use instead of real redirects, are stored as **title-only** entries that point to their article.
Thus, searching "USA" still finds the United States page.

**Why the first paragraph.** For each article, Zimantic keeps the title plus the **first paragraph**.
Wikipedia's writer's guidelines require opening paragraph to summarize the whole article, so it's the
most compact description. That makes it the best text to compare with
questions and descriptions.

**Forming a clean paragraph.** Zimantic uses Wikipedia's page structure:
- it takes the first `<p>` with at least 50 characters, so pages that open with a one-liner like
  "Mercury may refer to:" (disambiguation pages, lists) are stored as title-only;
- it skips stylesheets, scripts and footnote markers like `[1]` inside the paragraph;
- it keeps at most 1,000 characters.

**Other ZIMs** (Stack Exchange, Gutenberg, TED, …) aren't refused: `build` runs on any ZIM and applies the same
first-paragraph rule to every HTML page. But nothing has been tuned or tested for them, so results depend on how
each site lays out its pages. PDFs inside a ZIM are skipped.

**Embedding.** The title and first paragraph are turned into a vector (a list of numbers describing their meaning), currently by
[multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) (int8 ONNX, ~118 MB).
Input is capped at 256 tokens.

**Storage.** Each ZIM gets two files in `index_dir`:
- `<name>.sqlite`: titles, first paragraphs, paths, redirect targets, and a full-text index of the titles (SQLite FTS5).
- `<name>.faiss`: the vectors, in one of two layouts depending on aritlce count:

| ZIM size | Vector index | Why |
|---|---|---|
| Under 10,000 articles | **Flat**: the query is compared with every vector | Exact, and small enough (a few MB) that comparing everything is fast |
| 10,000 articles or more | **IVF + 8-bit (SQ8)**: vectors are grouped into 4·√n clusters, and each search only scans the closest `nprobe` clusters (64 by default) | Comparing millions of vectors per search is too slow. On the 28k-article astronomy ZIM, scanning 64 of 672 clusters (~10%) was about as accurate as scanning everything. 8-bit numbers were nearly exact. **heavier compression methods (e.g. PQ48) lost ~25% of the top hits.** |

The `.faiss` file is memory-mapped: the operating system reads only the **clusters** a search touches instead of loading
the whole index into RAM. Clusters are found relative to the distance of query-to-cluster centres in vector space.

If a build stops, running it again resumes from the last saved batch.

### 2. Starting the server (`serve`, once)

At startup Zimantic loads the embedding model and opens every **finished** index; that is, the SQLite file
, the FAISS file, and the ZIM.
These stay open for as long as the server runs; **nothing is reloaded per search**.

A lightweight HTML page is served through FastAPI and is accessible from any browser at `http://<host>:8090`
(the `port` in `config.yaml`).

### 3. Each search

1. **Embed the query** with the same model used to index
2. **Run three searches** on each selected ZIM. They find the best matches in different ways:

   | Search | Finds | Good at | Time |
   |---|---|---|---|
   | **Meaning** (FAISS) | Articles whose first-paragraph vector is closest to the query (cosine similarity) | Questions, descriptions, other languages | ~5 ms |
   | **Title words** (SQLite FTS5, BM25 ranking) | Titles containing every word of the query, including redirect titles | Exact titles, alternate names (via redirect titles) | ~2 ms |
   | **Full text** (the ZIM's own Kiwix index) | Articles containing the words anywhere | Words buried deep inside an article | ~5 ms |

3. **Collapse redirects**: every hit on a redirect is replaced by the article it points to, and duplicates are
   merged, so each article appears only once.
4. **Merge the three lists with Reciprocal Rank Fusion (RRF).** Their scores can't be compared
   (a cosine similarity, a BM25 score, a position in Kiwix's list), so RRF ignores scores and uses positions only:
   an article earns `1 / (60 + its position)` from each list it appears in. An article found near the top
   by several searches beats one that's first in just one.

*Timings may vary.*

## Compared with Kiwix search


![Ask a question, get the article](docs/images/1-questions.png)
![Ask in another language](docs/images/3-languages.png)
![How often the right article comes first: Zimantic vs Kiwix](docs/images/0-scorecard.png)

*Measured on the Wikipedia astronomy ZIM (28k articles), against kiwix-serve's full-text search.*

### Results by type of search

**#1** = the right article is the first result. **Top 5** = it's somewhere in the first five.
"Not in top 20" = it didn't appear in the first 20 results.

| Type of search | Example query → article wanted | Kiwix | Zimantic | #1 Kiwix → Zimantic | Top 5 Kiwix → Zimantic | Queries |
|---|---|---|---|---|---|---|
| Common single words | "moon" → *Moon* | #9 | **#1** | 33% → **93%** | 80% → **93%** | 15 |
| Exact titles | "Jupiter" → *Jupiter* | #2 | **#1** | 58% → **83%** | **100%** → 83% | 12 |
| Alternate names | "Cosmic rays" → *Cosmic ray* | #2 | **#1** | 70% → **90%** | 80% → **98%** | 200¹ |
| Misspellings | "andromeda galaxie" → *Andromeda Galaxy* | #3 | **#1** | 20% → **40%** | 40% → **60%** | 5² |
| First-sentence descriptions | "is a blue supergiant star in the constellation of Major." → *Eta Canis Majoris* | #4 | **#1** | 66% → **80%** | 80% → **89%** | 200¹ |
| Describing it without the name | "device that automatically keeps a telescope locked onto the target it is observing" → *Autoguider* | not in top 20 | **#1** | 21% → **54%** | 29% → **74%** | 100 |
| Questions | "comet that comes back every 76 years" → *Halley's Comet* | #4 | **#1** | 0% → **31%** | 25% → **69%** | 16 |
| Questions & phrases in other languages³ | "¿por qué la luna tiene fases?" (Spanish) → *Lunar phase* | not in top 20 | **#1** | 0% → **7%** | 0% → **20%** | 15 |
| Words deep inside an article | "In 1998, was awarded the Swiss Marcel Benoist Prize…" → *Michel Mayor* | **#1** | #2 | **53%** → 15% | **65%** → 58% | 200¹ |

¹ Generated automatically from the articles, not typed by real users.
² Small sample: treat as a direction, not a precise number.
³ Spanish, French, German, Chinese, Arabic, Hindi, Swahili, Russian, Japanese and Portuguese, mixed.

### Single words in other languages

The English articles searched with one word in another language (for example "moon" in that language), 15 words per language.

| Language | Example query → article wanted | Kiwix | Zimantic | #1 Kiwix → Zimantic | Top 5 Kiwix → Zimantic |
|---|---|---|---|---|---|
| English | "sun" → *Sun* | #2 | **#1** | 33% → **93%** | 80% → **93%** |
| French | "télescope" → *Telescope* | #4 | **#1** | 20% → **33%** | 33% → **47%** |
| Portuguese | "sol" → *Sun* | not in top 20 | **#3** | 7% → **27%** | 27% → **67%** |
| Arabic | "الشمس" → *Sun* | not in top 20 | **#1** | 0% → **20%** | 0% → **33%** |
| Afrikaans | "planeet" → *Planet* | not in top 20 | **#1** | 0% → **13%** | 7% → **33%** |
| Amharic | "ፀሐይ" → *Sun* | not in top 20 | **#1** | 0% → **13%** | 0% → **20%** |
| Swahili | "mwezi" → *Moon* | not in top 20 | **#6** | 0% → **13%** | 7% → 13% |
| Somali | | | | 0% → **7%** | 7% → 7% |
| Hausa, Yoruba, Igbo, Zulu, Kinyarwanda | | | | 0% → 0% | 0% → 0% |

**Where Kiwix is still better, or cheaper:**
- **Words buried deep inside an article.** Kiwix indexes every word thus performs better on deeper searches(53% first vs 15%). While Zimantic only indexes each article's title and first paragraph.
- **No setup.** Kiwix search works the moment a ZIM is added. Zimantic must index each ZIM
  first.
- **Smaller footprint.** Zimantic adds a `.sqlite` and `.faiss` per ZIM and needs more RAM.

## Requirements
- Python 3.12, 3.13 or 3.14 (the pinned numpy needs 3.12+; libzim doesn't support 3.15 yet)
- About 120 MB for the embedding model, plus your ZIM files
- Wikipedia-style ZIM files (Zimantic relies on their predictable HTML structure to find the first paragraph)
- *Searching does not require Kiwix.* To open the articles from the result links, run
  [kiwix-serve](https://kiwix.org/en/applications/) with the same ZIMs (set its address as `kiwix_url` in `config.yaml`).

## Where your files go

By default everything lives inside the project folder, so `config.yaml` works without changes:

```
zimantic/
├── config.yaml          ← settings (paths below are its defaults)
├── model/               ← model_dir: model.onnx + sentencepiece.bpe.model   (you download)
├── zims/                ← zim_dir:   your .zim files                          (you download)
├── indexes/             ← index_dir: <name>.sqlite + <name>.faiss            (created by build)
└── zimantic/           ← the program
```

Your files are somewhere else (a USB drive, another disk)? Point `zim_dir`, `model_dir` or `index_dir` in
`config.yaml` at them instead. These three folders are git-ignored, so ZIMs, models and indexes never get committed.

## Install

1. **Get the code** and enter the folder 
- run the next commands from here 
- `config.yaml` is read from this folder, so configure appropriately

   ```bash
   git clone https://github.com/OscSanto/zimantic.git
   cd zimantic
   ```

2. **Create a virtual environment and install the dependencies:**

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate              # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Download the model into `model/`** (keep these exact file names):

   ```bash
   mkdir -p model
   curl -L -o model/model.onnx https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/onnx/model_quantized.onnx
   curl -L -o model/sentencepiece.bpe.model https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/sentencepiece.bpe.model
   ```

   (`curl` is built into Linux, macOS and Windows 10+. On Windows PowerShell, use `curl.exe` and `mkdir model`.)

4. **Put your ZIM files in `zims/`.** Download them from [library.kiwix.org](https://library.kiwix.org)
   or [download.kiwix.org/zim](https://download.kiwix.org/zim/).

5. **Check `config.yaml`.** If you used the folders above, nothing needs changing. Otherwise point
   `zim_dir` / `model_dir` / `index_dir` at your folders. To open articles from the results, set
   `kiwix_url` to where kiwix-serve runs.

6. **Check that it runs:**

   ```bash
   python -m zimantic --help
   ```

## Use

```bash
python -m zimantic build wikipedia_en_astronomy_maxi_2025-11   # index ZIMs in zim_dir by name (one or more)
python -m zimantic build --path /some/where/x.zim             # or index one ZIM file by its path
python -m zimantic serve                                      # web page on http://<host>:8090 after build is succesful
```

The name is the ZIM's file name without `.zim` (for `zims/wikipedia_en_astronomy_maxi_2025-11.zim`,
use `wikipedia_en_astronomy_maxi_2025-11`).

To open articles from the results, run kiwix-serve with the same ZIMs, in a second terminal:

```bash
sudo apt install kiwix-tools             # Debian/Ubuntu/Raspberry Pi OS; other systems: kiwix.org/en/applications
kiwix-serve --port 8080 zims/*.zim       # matches the default kiwix_url in config.yaml
```

If a build stops, run it again and it will automatically pick up where it left off.

To rebuild a ZIM, delete its `.sqlite` and `.faiss` from `index_dir` first.
Restart `serve` after building a new index so it picks it up.

Each ZIM gets two files in `index_dir`: `<name>.sqlite` (titles, first paragraphs) and
`<name>.faiss` (vectors). If building index is slow, consider building on a more powerful PC and copying over both files.

JSON API Example: `GET /api/search?q=...&zim=<name>&zim=<name2>&limit=20` (no `zim` = all) and `GET /api/zims`.
