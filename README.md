# Zimantic

Zimantic is an offline search engine for Kiwix ZIM files, the compressed offline copies of Wikipedia and other sites published by Kiwix, and for configured directory trees such as an offline `/library/www/html/modules`. It retrieves articles by meaning rather than by exact keyword overlap, so it can answer questions, match a description written without the article's name, tolerate misspellings, and accept a query in a different language from the article. It runs entirely offline, including on hardware as small as a Raspberry Pi Zero 2 W.

Meaning search performs best in widely spoken languages. Coverage varies by language; see [Compared with Kiwix search](#compared-with-kiwix-search).

| Query | Kiwix search | Zimantic |
|---|---|---|
| "what causes lockjaw" | *Tetanus* not in the top 20 | *Tetanus* at **#1** |
| "diabetis" (misspelled) | *Diabetes* not in the top 20 | *Diabetes* at **#1** |
| "糖尿病" (Chinese for "diabetes") | *Diabetes* not in the top 20 | *Diabetes* at **#1** |

*Searched in WikiMed, the Wikipedia medical encyclopedia ZIM.*

## Features

- **Meaning search (semantic search).** Questions and descriptions match articles even when they share no words with the title.
- **Alternate names and misspellings.** "diabetis" finds *Diabetes*; "Leber's disease" finds *Leber's hereditary optic neuropathy*.
- **Multilingual queries.** One model covers 100+ languages, and a query in one language can find articles written in another. Results are strongest in widely used languages; see [Compared with Kiwix search](#compared-with-kiwix-search).
- **Multiple sources.** Search a small selection of ZIMs directly, or search the full library and filter the ranked results by source. See [Source selection](#source-selection).
- **Files collections.** Search and serve a configured directory tree, such as an offline copy of `/library/www/html/modules`, as one bounded collection. See [Files collections](#files-collections).
- **Hybrid ranking.** Meaning, title-word, and full-text retrieval are fused into a single ranking.
- **Merged results and bounded previews.** Redirects are merged into their target article, so each article appears once. Each result carries a bounded excerpt as its preview.
- **Progressive results.** Sources run in parallel. The page shows a provisional merged list as each source finishes, then reranks it deterministically.
- **Library view.** Browse installed ZIMs and files sources at `/library` with artwork, descriptions, metadata, and links to each homepage or a random article. See [Library view](#library-view).
- **Low resource use.** Runs on low-resource devices such as a Raspberry Pi Zero 2 W (512 MB RAM) using roughly 250–325 MB while serving; see [Measured memory use](#measured-memory-use).
- **Graceful degradation.** Title-word and full-text search work as soon as an index exists. Vectors are optional, and a ZIM with no index remains searchable through its native full-text index. A missing model, a missing FAISS file, or a different model whose probe embeddings do not match falls back to lexical search instead of failing the server. Native full-text searches run in helper processes, so an archive with an inconsistent full-text index cannot take the server down; only that archive loses native full-text results, and the server logs why.
- **Concurrent searches.** `max_concurrent_searches` bounds simultaneous searches. A request waits up to `slot_wait_seconds` for a free slot before reporting busy, so overlapping searches queue on slow hardware instead of failing immediately; abandoned browser streams skip queued source work and return their slot. Identical in-flight queries share one computation, and repeated queries are answered from an in-memory cache bounded by `cache_size` and `cache_bytes`. Small one-off source selections are intentionally not cached; see [Source selection](#source-selection).
- **Built-in article serving.** Result links open directly from the ZIM archives, including redirects, assets, and each ZIM's homepage, without requiring `kiwix-serve`.
- **Web interface and JSON API.** Search from any browser on the network or from custom programs. Routes use short paths such as `/search`, `/suggest`, `/library`, `/content/<source-key>/<path>`, `/article/<source-key>/<path>`, and `/health`.

Zimantic queries the local ZIM archives themselves, not a separate Kiwix index. `zimantic build` adds meaning vectors and a SQLite title-word index, but it is not a prerequisite for `zimantic serve`: an unbuilt ZIM is still searched through the archive's own full-text index, and articles, assets, and homepages are served directly from the archive. An optional [kiwix-serve](https://kiwix.org/en/applications/) instance is useful only for sources available through a Kiwix server, which Zimantic can search with kiwix-serve's full-text API.

## Requirements

- Python 3.11–3.14 with native dependency wheels available for the interpreter. `libzim` publishes wheels for specific Python ABIs and platforms, so choose an interpreter with a compatible wheel. Update this range when the native dependencies add support for a newer Python version.
- 32-bit Linux platforms (armhf/armv7) have no prebuilt wheels for the native dependencies. They work when the dependencies are installed as distribution packages. Debian 13 (trixie) or newer provides `python3-onnxruntime`, `python3-faiss`, `python3-libzim`, `python3-sentencepiece`, and `python3-pymupdf` for armhf. Create the environment with `--system-site-packages` so Zimantic can use them.
- About 120 MB for the default embedding model, plus the ZIM files. The English-only `e5-small-v2` model is about 35 MB on disk.
- Wikipedia-style ZIM files. Zimantic relies on their predictable HTML structure to locate useful content blocks.
- **One provider seam.** ZIM and remote Kiwix are working in-tree providers. Source identity is a stable, URL-safe `key`; `provider` is the only discriminator. There is no `mode`, `content_kind`, `book`, or `local_name` field.
- **Configured collections.** Collections are declared with `[[collections]]` entries (ZIM roots and Kiwix servers). Removed flat keys such as `zim_dir` and `kiwix_server` are fatal configuration errors with a migration message.
- No `kiwix-serve` for local sources: Zimantic serves local results and articles directly. A Kiwix `[[collections]]` entry discovers remote books and opens their results through links returned by kiwix-serve's search API.

## Install

1. **Clone the repository into the data directory.** Configuration lives at `$XDG_CONFIG_HOME/zimantic/config.toml` (normally `~/.config/zimantic/config.toml`); omitted settings use hardware and XDG defaults.

   ```bash
   data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
   config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
   mkdir -p "$data_home" "$config_home/zimantic"
   git clone https://github.com/OscSanto/zimantic.git "$data_home/zimantic"
   cd "$data_home/zimantic"
   if [ ! -e "$config_home/zimantic/config.toml" ]; then
       cp config.toml "$config_home/zimantic/config.toml"
   fi
   ```

2. **Install the CLI with uv.** This installs the locked runtime dependencies without the development tools and creates the executable at `~/.local/bin/zimantic`.

   ```bash
   uv sync --locked --no-dev --no-editable
   ln -sfn "$PWD/.venv/bin/zimantic" ~/.local/bin/zimantic
   ```

   For development, run `uv sync` and activate the environment with `. .venv/bin/activate`.

   On 32-bit armhf, where the native dependencies have no wheels, install the distribution packages and let the environment use them:

   ```bash
   sudo apt install python3-venv python3-numpy python3-tqdm python3-onnxruntime \
       python3-sentencepiece python3-faiss python3-libzim python3-pymupdf \
       python3-fastapi python3-uvicorn
   python3 -m venv --system-site-packages .venv
   .venv/bin/pip install --no-deps .
   ln -sfn "$PWD/.venv/bin/zimantic" ~/.local/bin/zimantic
   ```

3. **Download the default multilingual model.**

   ```bash
   zimantic model
   ```

   This downloads the int8 export to `$XDG_DATA_HOME/zimantic/model/int8/model.onnx` and its tokenizer vocabulary. On a GPU host, download fp16 instead with `zimantic model fp16`; fp16 is faster on CUDA, ROCm, DirectML, and OpenVINO, while int8 is the smallest CPU-oriented default. In that case set `model_dir = "model/fp16"` in the XDG config. `zimantic model --list` shows the other supported e5 models, and `zimantic model --help` shows the options. The equivalent manual download is:

   ```bash
   data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
   mkdir -p "$data_home/zimantic/model/int8"
   curl -L -o "$data_home/zimantic/model/int8/model.onnx" https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/onnx/model_quantized.onnx
   curl -L -o "$data_home/zimantic/model/int8/sentencepiece.bpe.model" https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/sentencepiece.bpe.model
   ```

   English-only and larger multilingual models use the same layout; see [Using a different embedding model](#using-a-different-embedding-model). Zimantic warns when `model.onnx` or `sentencepiece.bpe.model` differs from a documented checksum, but continues to run.

4. **Put ZIM files in `$XDG_DATA_HOME/zimantic/zims/`.** Download them from [library.kiwix.org](https://library.kiwix.org) or [download.kiwix.org/zim](https://download.kiwix.org/zim/).

5. **Check the XDG config.** Relative `model_dir` and `index_dir` overrides resolve under `$XDG_DATA_HOME/zimantic`. A ZIM collection's `path` resolves from the config file's directory. Omit them to use the defaults. Add a Kiwix `[[collections]]` entry only when remote Kiwix sources are also needed.

6. **Verify the installation.**

   ```bash
   zimantic --help
   ```

   For a development runtime check, follow [Development install](CONTRIBUTING.md#development-install) and run `python -m pytest tests/test_runtime_api.py`. Use `python -m pytest` for the full suite.

### Windows

Zimantic supports 64-bit Windows (x86-64) on Python 3.11–3.14. Windows ARM64 and 32-bit Windows are not supported because `libzim`, `faiss-cpu`, and `onnxruntime` do not publish wheels for those targets.

The installation steps work with the Windows equivalents:

```powershell
git clone https://github.com/OscSanto/zimantic.git zimantic
cd zimantic
uv sync --locked --no-dev
.venv\Scripts\activate
zimantic --help
```

Download the default model with the CLI:

```powershell
zimantic model
```

On Windows, the default `model_dir` is `./model/int8`. On a GPU host use `zimantic model fp16` and set `model_dir = "./model/fp16"` in `config.toml`. The PowerShell equivalent of the manual int8 download is:

```powershell
New-Item -ItemType Directory -Force model\int8 | Out-Null
Invoke-WebRequest -Uri https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/onnx/model_quantized.onnx -OutFile model\int8\model.onnx
Invoke-WebRequest -Uri https://huggingface.co/intfloat/multilingual-e5-small/resolve/main/sentencepiece.bpe.model -OutFile model\int8\sentencepiece.bpe.model
```

`zimantic reload` is unavailable on Windows because it relies on `SIGHUP`; restart `zimantic serve` instead. Stop the server before replacing or deleting a ZIM or index file, because Windows does not allow an open file to be replaced in place.

## Files and directories

The installed CLI is separate from configuration and runtime data. On XDG platforms, configuration is read from `$XDG_CONFIG_HOME/zimantic/config.toml` (normally `~/.config/zimantic/config.toml`); ZIMs, models, and indexes default under `$XDG_DATA_HOME/zimantic` (normally `~/.local/share/zimantic`). Relative path overrides in the XDG config resolve under that data directory. For compatibility, if no XDG config exists, an existing `config.toml` in the current directory is still loaded and its relative paths retain their historical current-directory behavior.

```
~/.local/share/zimantic/
├── model/               ← downloaded models; model/int8/ is model_dir         (you download)
├── zims/                ← ZIM collection path: your .zim files                (you download)
├── indexes/             ← index_dir: immutable generations under provider/collection/source
└── catalog.sqlite       ← shared ZIM metadata cache (metadata only, no vectors)
```

```
~/.config/zimantic/config.toml          ← optional settings
$XDG_RUNTIME_DIR/zimantic/zimantic.pid ← PID file while serving
```

If the files are on a USB drive or another disk, point a ZIM collection's `path`, `model_dir`, or `index_dir` in the XDG config at them.

### Files collections

A `type = "files"` collection searches and serves an ordinary directory tree, such as an offline copy of `/library/www/html/modules`. One configured directory is one collection. The default granularity keeps the whole tree as a single source, so a directory of thousands of pages does not become a wall of library cards:

```toml
[[collections]]
type = "files"
name = "modules"
path = "/library/www/html/modules"
label = "Modules"
granularity = "collection"       # "collection" or "children"
include = ["**/*.html", "**/*.htm", "**/*.xhtml",
           "**/*.txt", "**/*.md", "**/*.pdf"]
exclude = ["**/node_modules/**", "**/.git/**", "**/__pycache__/**"]
max_sources = 64
max_entries = 100000
max_file_bytes = 268435456
```

`granularity = "children"` makes each immediate child directory its own source, plus one source for loose files directly in the root. Use it when the tree has a handful of stable top-level units; otherwise keep `collection` so the library stays bounded. If the number of child sources exceeds `max_sources`, the whole collection collapses to one source with a distinct key and a warning. Collapse and recovery change source keys, so a later build is required and any `[[collections.sources]]` override for an old key is reported as stale.

`max_entries` bounds the indexed manifest and marks the source truncated when more candidates exist; all permitted files remain servable. `max_file_bytes` is combined with the global `max_entry_bytes` to bound document extraction, and `max_sources` bounds the child-source list.

Source keys are derived from the configured `name` and root-relative identifiers, never the absolute path. Renaming `name` creates a new collection and requires a rebuild. The collection key is `files:<name>`; collection granularity uses `files:<name>:root`, a collapsed collection uses `files:<name>:collapsed`, a child uses `files:<name>:dir-<slug>-<digest>`, and loose root files use the reserved `files:<name>:__loose__`.

All sources in one files collection share the collection-relative serving namespace. A child page can link to `../shared/style.css` and it resolves under the same source key; search results and library membership still belong to the source that indexed the item. Hidden paths, configured excludes, symlinks, and paths outside the configured root are never served. Unknown extensions are served as attachments with `X-Content-Type-Options: nosniff`; SVG opened directly is sandboxed with a restrictive CSP.

`include` selects native documents for indexing. Permitted image, audio, and video files are title-searchable by filename without reading their bytes. Captions, OCR, and ASR stay off unless an exact source override enables them:

```toml
# Discover the exact key first (for example from `/sources`).
[[collections.sources]]
key = "files:modules:root"
captions = "index"     # .srt/.vtt sidecars (embedded tracks use the shared
                       # ffmpeg adapter when one is available)
ocr = "tesseract"      # images and image-only PDF pages
asr = "whisper"        # audio/video
```

`zimantic build` with no positional paths builds configured files collections as well as ZIM collections, using the same index generations, embeddings, and lock as ZIM builds. Serving does not require an index: permitted files remain browsable even when indexing was capped or skipped.

With `secure_browsing = true`, the `/content/` shell sandboxes the article iframe. A raw `/article/` page opened directly or in a permitted popup is not sandboxed; treat untrusted file trees as untrusted input regardless of that setting.

### Copying finished indexes to another machine

Embeddings live only in `index.faiss`. Its row IDs refer to `docs.rowid` in the matching `index.sqlite`, which holds titles, paths, the title and body full-text indexes, derived-text cache references, and build metadata. Copy the pair plus the source's `current` manifest; a `.faiss` copied without its `.sqlite` is unusable, and a `.sqlite` copied without its `.faiss` provides title and body lexical search only. A full index whose `.faiss` is missing still serves lexical results, but a later full build requires `--force` to replace it.

Copying a subset is safe: each source generation is independent. Keep the same canonical source key and the same ZIM bytes on the target, and copy the generation plus its `current` manifest. Moving a ZIM between subfolders of its collection changes its key and requires a rebuild; replacing content at the same relative path keeps the key but invalidates a stale index. The target must use the same embedding model, or an export whose probe embeddings match it (for example FP32 and int8 exports of the same encoder); otherwise `serve` ignores those vectors and falls back to title and full-text search. The target does not need the GPU used to build them: vector search runs on the CPU.

To copy two completed source generations:

```bash
for source in \
  indexes/zim/library-*/*/current \
  indexes/zim/library-*/*/generations/*; do
    printf '%s\n' "${source#indexes/}"
done > /tmp/index-generations.txt

rsync -a --files-from=/tmp/index-generations.txt indexes/ pi@rpi:~/.local/share/zimantic/indexes/
```

`rsync -a` copies only files whose size or mtime changed. Do not copy `staging-*.sqlite`, `*.tmp`, `*.sqlite-wal`, or `*.sqlite-shm`; staging files are unfinished builds, not published generations. Copy a complete generation directory and then its `current` manifest so a reload never opens a half-written pair. Run `zimantic reload` afterwards.

`catalog.sqlite` is optional for the copy. It contains metadata and icons, never vectors, and a missing catalog is rebuilt by scanning the ZIMs. Its `path` column stores absolute locators, so copying it only avoids the first scan when the target resolves a collection to the same absolute paths; otherwise those rows are treated as new and rescanned. The scan opens each ZIM once for metadata, illustration, and counts, not article bodies. A catalog written by a different schema version is discarded and rebuilt under the cross-process catalog lease. Both `build` and `serve` can create it.

## Usage

### Commands

```bash
zimantic build                        # index every configured ZIM collection
zimantic -v build                     # show info-level logging
zimantic build -vv                    # show debug-level logging
zimantic build zims/x.zim             # index one file
zimantic build zims/a.zim zims/b.zim  # several files
zimantic build --workers 4            # build up to four independent ZIMs at once
zimantic build /media/usb             # every .zim in a folder
zimantic build zims/a.zim /media/usb  # mix files and folders
zimantic build --fast                 # quick title-word + full-text index (no vectors)
zimantic build --wait-stable          # wait for changing download sizes, then index
zimantic build --no-wait-lock         # exit 75 if another build holds the lock
zimantic build --lock-timeout 600     # wait at most ten minutes for the lock
zimantic build --force zims/x.zim     # rebuild one already-indexed ZIM
zimantic build --device cuda          # embed with a CUDA GPU (see below)
zimantic serve                        # web page; unindexed ZIMs use native full-text search
zimantic serve --fast                 # start without a model or vectors
zimantic serve --device cuda          # keep a CUDA GPU for query embedding
zimantic serve --debug                # include ranking scores and explanations; restart on SIGHUP
zimantic serve --dev                  # alias for --debug
zimantic reload                       # ask a running server to rescan indexes and ZIMs
zimantic model                        # download the int8 export (CPU default)
zimantic model fp16                   # download fp16; recommended with a GPU
zimantic model fp32 fp16              # download selected precisions
zimantic model --model e5-small-v2    # download another supported e5 model
zimantic model --dir ./models         # write models/<precision>/ instead of model/
zimantic model --list                 # show supported models and precisions
```

`build` accepts files or folders. A folder means every `*.zim` file under it, and with no arguments it builds every configured ZIM collection plus every configured files collection. An unmatched positional path is atomically appended as a new ZIM `[[collections]]` entry so `serve` can discover it; restart a running server to load that config change. Existing complete generations are skipped; pass `--force` to rebuild selected indexes. Logging defaults to warnings, except that `serve` and `model` use info when stderr is a terminal. Repeat `--verbose`/`-v` for info and debug output.

### Build behavior and locking

**Fast indexes.** `build --fast` stores titles and paths without running the model or creating FAISS vectors. It reads a small bounded HTML prefix per entry so disambiguation suffixes, meta-refresh shell targets, and app-shell companion deep links are still recovered. It does not store article excerpts. Title-word and ZIM full-text search work; a normal `build` creates the vector index.

**Stable files.** `build --wait-stable` polls source sizes until they stop changing before indexing. The systemd units below pass it so a file caught mid-download is not indexed. An explicit CLI build starts immediately.

**Build lock.** Every build takes an advisory lock at `<index_dir>/../.build.lock` (beside `catalog.sqlite`), so a manual build, a `systemd.path` trigger, and a timer cannot write the same indexes at once. A second build waits by default. `--no-wait-lock` exits with status 75 (`EX_TEMPFAIL`) immediately, which the IIAB units use to skip a trigger without marking it failed. `--lock-timeout` bounds the wait. The kernel releases the lock when the process exits, so a killed build leaves no stale lock. Run manual builds as the same user as the server: a lock or index owned by another user (for example after `sudo zimantic build`) produces a `cannot open the build lock` error that names the file to fix.

**Workers and batching.** The desktop profile embeds 32 articles at a time (mobile 8, supercomputer 128). Independent ZIMs can be read and built concurrently with `--workers N` or the `build_workers` setting; when unset, the default scales with CPU and RAM. Full builds use a bounded shared embedding pipeline: at most `embedding_workers` model calls run at once, and each ZIM can have only a small number of batches waiting for embedding. When unset, `embedding_workers` also accounts for `embed_threads` so the model is not oversubscribed. The pipeline is used only when multiple sources are built concurrently (`build_workers` or `--workers` greater than 1). Tune `batch_size`, `build_workers`, `embedding_workers`, and `embed_threads` in `config.toml` for different hardware. Builds that target the same `index_dir` are serialized by the build lock; builds with different `index_dir` values still compete for CPU and memory, so size `--workers` for the host.

**Disk space.** Keep about **20% of total ZIM file size** free when building indexes.

### Hardware profiles

Tuning keys in `config.toml` are commented out by default, so Zimantic selects mobile, desktop, or supercomputer defaults from the machine's resources. Copy `config.pi-zero-2w.toml` or `config.pi-5.toml` to pin settings.

### GPU acceleration

Embedding can run on a GPU through ONNX Runtime's accelerated execution providers. `embed_device = "auto"` (the default) tries every accelerated provider the installed ONNX Runtime build exposes, in CUDA, ROCm, DirectML, and OpenVINO order, and otherwise stays on the CPU.

Supported values are `auto`, `gpu` (require an accelerated provider), `cpu`, `cuda` (NVIDIA), `rocm` (AMD), `directml`/`dml` (Windows), `openvino` (Intel, using OpenVINO `AUTO` device selection), and `coreml` (Apple). `--device` overrides the setting for one `build` or `serve` run. CoreML is never selected by `auto`: stock macOS `onnxruntime` wheels include it, and its dynamic-shape handling can be slower than the CPU for this model.

**Use the fp16 model with a GPU.** Download it with `zimantic model fp16` and point `model_dir` at that folder. Accelerated providers usually run fp16 faster than the default quantized int8 export, which is tuned for CPUs. Keep int8 for CPU-only hosts and small devices.

The provider comes from the ONNX Runtime package, not from Zimantic. Replace the plain `onnxruntime` wheel with `onnxruntime-gpu` (CUDA), `onnxruntime-rocm`, `onnxruntime-directml`, or `onnxruntime-openvino`. If the requested provider is missing or cannot initialize (for example because a CUDA library is absent), Zimantic warns and embeds on the CPU, so the same configuration also runs on machines without a GPU. If a run fails on the device after initialization (for example a GPU out-of-memory error), Zimantic warns and retries on the CPU instead of failing the build.

```bash
pip uninstall -y onnxruntime   # only one onnxruntime distribution provides the module
pip install onnxruntime-gpu    # or onnxruntime-rocm/-directml/-openvino
zimantic build --device cuda   # or embed_device = "cuda" in config.toml
```

When Zimantic is managed with `uv`, `uv sync` reinstalls the plain `onnxruntime` dependency, so install the GPU wheel after syncing (or change the dependency in the project environment).

GPU acceleration helps full index builds the most, because they embed every article; raising `batch_size` uses the device more efficiently. Lower it if a batch exhausts GPU memory. Query embedding also runs on the device, but a single short query is too small to gain much. Vector search (FAISS) deliberately stays on the CPU: the indexes are memory-mapped, and copying one to GPU memory to search a single query vector would cost more than the search itself.

### Automatic indexing with systemd

Ready-to-copy user units in `deploy/` watch `zims/`, build indexes, and reload the server as ZIMs become available:

- `deploy/zimantic.service` — the server, with `Restart=always`, restart rate limiting, and sandboxing. Copy it to `~/.config/systemd/user/`, then run `systemctl --user enable --now zimantic`.
- `deploy/zimantic-indexing-fast.path` + `deploy/zimantic-indexing-fast.service` — watch `zims/` and run a fast build followed by `reload` as a ZIM becomes available.
- `deploy/zimantic-indexing.service` + `deploy/zimantic-indexing.timer` — run the full build (with meaning vectors) once a night.

The path-triggered fast build runs for each ZIM in `zims/`. The full build runs from 01:00–06:00 local time by default; set its `ExecCondition` and timer to select a different window. Both units pass `--no-wait-lock`, so a build that finds the index lock held exits 75 and the unit treats it as a skip instead of overlapping the build that is already running.

The server unit retries after 10 seconds but stops after three starts in ten minutes, so a server that cannot fit its archives fails visibly instead of looping through OOM kills. Check `systemctl --user status zimantic` and `journalctl --user -u zimantic`; after fixing the cause, `systemctl --user reset-failed zimantic` clears the rate limit. Its header also shows `MemoryHigh`/`MemoryMax` pairs for a Pi Zero 2 W and a Pi 5; uncomment and adjust them for the device, leaving headroom for the OS and kiwix-serve. `MemoryHigh` throttles first; `MemoryMax` is the hard limit that OOM-kills only Zimantic.

Each unit has install instructions in its header. The units assume runtime data at the default XDG data directory, `~/.local/share/zimantic`, and the CLI installed at `~/.local/bin/zimantic`. Their working directory preserves legacy `config.toml` fallback, while an XDG config takes precedence. If you use a custom `XDG_DATA_HOME`, adjust `WorkingDirectory`, `ReadWritePaths`, and `PathChanged`; if your systemd user manager does not inherit custom XDG variables, add them to the units or a drop-in.

The units call the installed `~/.local/bin/zimantic` executable. `reload` does not need the config file; it reads the PID from `$XDG_RUNTIME_DIR/zimantic/zimantic.pid`, falling back to the legacy `./zimantic.pid` when present, or from the file passed with `--pid`.

Watch `zims/`, not `indexes/`, because builds write to `indexes/`. If you [copy finished indexes from another machine](#copying-finished-indexes-to-another-machine), watch `indexes/` and run only `reload`.

A running `serve` discovers indexes with `zimantic reload`; a fast index remains searchable while a full index builds.

### Source selection

The source picker discovers available sources and sorts matching sources by result count. Multiple source filters can be selected, and **All Sources** resets the selection. The top 10 sources are shown, with an **Other** catch-all when needed. Searching within sources shows up to 30 matches plus a **Show N more** control for the rest. Selected sources stay visible, so choosing **Other** expands that group into its individual filters. Dated and flavour variants of one book collapse to a single source here and in the library view.

How a selection is searched depends on its size:

- A selection of at most `min(10, 35%)` of the available sources is searched on its own when it starts a search, and the result is not cached.
- A larger selection is promoted to one full search whose ranked pool is cached. Source filters then narrow what is displayed.
- Source filters chosen after a full search are display-only and reuse that pool when the source was part of it.
- Selecting a source that the full search did not include (for example one beyond `max_remote_sources`) starts a new scoped search instead of filtering an empty page.
- A scoped search shows **Searching only in: …** above the results with a **Search all sources** control, so submitting a new query while a source is selected never silently hides the narrower scope.

### Library view

`/library` shows the installed ZIM and files sources in a responsive card grid with archive artwork, descriptions, metadata, and direct links to each homepage or random article. Each card carries its collection key and label; a files collection remains one bounded source list rather than one card per file. Dated and flavour variants of the same local book collapse to one card, keeping the largest readable and searchable copy; a larger archive is usually the more complete download and may contain larger artwork or media. **Shuffle** reorders the cards.

An unreadable archive stays in the grid marked **Unavailable**, with the catalog's reason. Click a ZIM's title or **Details** for an infobox with its thumbnail, homepage, descriptions, counts, license, scraper, source, attribution, relation, tags, and content-type breakdown. Prefix filter terms with `-` to exclude matching sources; multiple exclusions are supported. The filter searches all catalog metadata, including the ZIM stem.

### JSON API

The JSON API provides the following endpoints.

- `GET /search?q=...&zim=<name>&limit=10&offset=0&source=<key>` returns one page of results.
  - A small `zim` selection searches only those sources. A selection larger than `min(10, 35%)` of the available sources is promoted to a full search so its cached pool can be shared. Pass `source=<key>` to narrow the displayed results.
  - An explicit `zim` selection that includes a remote source outside `max_remote_sources` is searched as its own uncached scope.
  - Totals are returned in the `X-Total-Count`, `X-Has-More`, `X-Offset`, `X-Page`, and `X-Page-Count` headers. An offset past the last page is clamped to the last real page, and `X-Offset`, `X-Page`, and the stream's `offset` and `page` report that effective position.
- `GET /search/stream?q=...&limit=10&offset=0&source=<key>` returns newline-delimited progress events and result snapshots.
  - `snapshot` and `done` events carry the effective `page` and `page_count`.
  - While sources are still working, the stream emits a `progress` heartbeat about every 700 ms. When a source finishes in between, it emits a fresh snapshot instead.
  - A source that finishes between ranking passes reports no count until the next snapshot. The final `done` event always carries the complete per-source count map.
  - A cached search replays `started`, the cached source events, and a single `done` event with its final page, without a duplicate `snapshot`.
- `GET /suggest?q=...&source=<key>&limit=8` returns title-completion candidates for one source.
  - Suggestions are title-only: no embedding, no full-text search, and no article reads, so a typing pause is cheap to answer.
  - The response is a plain list of result objects. Sources without a Zimantic title index contribute nothing. Submitting the query without selecting a suggestion opens `/?q=...&zim=...` for a full search.
- `GET /content/<source-key>/<path>` serves a toolbar shell for a local source entry; `GET /content/<source-key>` serves the resolved source homepage.
  - The shell keeps the navigation bar in the page and loads the article itself from an inner iframe pointed at `/article/<source-key>/<path>`, so the toolbar stays visible even when the article is missing or fails.
  - Unreadable entries render an error panel in the shell instead of the raw `/article` JSON error body.
  - `GET /article/<source-key>/<path>` serves the raw entry: redirects are followed, its assets resolve under the same route, and entries larger than `max_article_bytes` return `413`.
  - `/article` supports one inclusive byte range per request (`bytes=start-end`, `bytes=start-`, or `bytes=-suffix`) and returns `206` with `Content-Range`, or `416` when the range starts past the representation. Unknown or malformed ranges, multiple ranges, and `If-Range` without a validator fall back to the bounded full response. Raw HTML is bridge-decorated and ignores ranges.
  - A missing or ambiguous entry, an unknown source key, or a source with no content store returns a JSON error envelope containing only a stable `error` code and the public `source` key. A store that cannot be opened returns `503`. `/article` includes `Access-Control-Allow-Origin: *` on success, redirects, partial and unsatisfiable responses, and errors; its bounded `OPTIONS` preflight allows `GET`, `HEAD`, `Range`, `If-Range`, `If-None-Match`, and `If-Modified-Since` without credentials.
  - With scripting enabled, opening `/article/` as a top-level page hands off to the matching `/content/` shell. Without scripting, or when the entry is fetched, it stays the raw entry. This keeps `/article/` as the entry source for the iframe, assets, and offline fetches rather than a second browsing surface.
  - The raw article's bridge filters out clicks the browser should keep (in-page fragments, modified clicks, downloads, non-HTTP(S) schemes, and links with a non-self target such as `_blank`, which open in a new tab). It reports the authored `href` plus any `<base>` to the shell.
  - The shell resolves that `href` against the article's local `/article/` URL and decides whether to handle it offline, in the app, or externally. It hands same-origin app links to the outer page when the shell is embedded in the modal. This keeps replay shims such as zimit's `wombat.js` from turning relative links in the capture into live-site URLs.
  - `max_html_bytes` limits how much HTML is parsed for indexing and phrase checks, and caps preview scans. Previews use a smaller fixed prefix (32 KiB) so one unusually large page cannot dominate latency. `max_article_bytes` limits the uncompressed response body because Zimantic reads each requested entry into memory, including binary assets.
  - The toolbar shell uses the viewed article's `<title>` and declared icon for the browser tab title and favicon, falling back to the ZIM's source icon. When the shell is embedded in the modal, the outer page mirrors them and restores its own title and favicon when the modal closes.
  - HTML and XHTML articles include a small navigation bar with Close, Home, Random, Permalink, a Zimantic library search control, and a ZIM-scoped search box. The scoped box is hidden on phones and when the ZIM does not resolve to an available local source. Alt-clicking Random opens the homepage of a random available ZIM.
  - Modal articles also expose icon-only navigation through the visible search results and, for nested articles, the adjacent article links listed on the article's parent page. When a modal article opens another modal article, the preceding article can also provide the navigation context.
  - Back navigation uses browser history when available; otherwise the Close control attempts to close the tab. In a directly loaded `/content/` view, Close closes the tab or window when the browser allows it; otherwise it goes back through history, or opens the library when the article is the first history entry.
  - Article views keep the current ZIM and path in the address bar, including from `/library`, so the URL can be shared.
  - Selecting text adds the passage to that URL and to the toolbar's Copy link action; reopening the link scrolls to and highlights the passage. Fragment links scroll inside the article without reloading the shell.
  - With `secure_browsing = true`, the article iframe has an opaque origin. Articles that use Web Storage get an in-memory fallback, and IndexedDB loads an in-memory polyfill. The shell then offers the standalone `/content/` view only when secure browsing is off, because a sandboxed standalone view cannot provide persistent storage either.
  - The IndexedDB polyfill bundles `fake-indexeddb` (Apache-2.0; see `indexeddb-shim.LICENSE`).
  - Stack Exchange captures that omit their MathJax font files are served the vendored MathJax 2.7.5 WOFF set. Root-absolute capture assets such as `/w/images/...` are resolved through the article referrer against the common archived locations (`-/mw/...` and similar) when no app route matches.
  - `GET /random?source=<key>` redirects to a random article's shell for that source. `GET /random` without `source` redirects to the homepage of a random available local source, including Files and Kolibri sources. The article toolbar's Random control uses the first form; Alt-clicking it uses the second.
  - Links from an offline article to an external `http` or `https` site show an in-modal notice identifying the destination as online and offer to open it in a new tab, avoiding failures from sites that disallow iframe embedding. A directly loaded `/content/` view tries the captured copy for same-site links before leaving for the live site, so only links that are missing from the ZIM go online.
- `GET /sources`, `GET /config`, `GET /library.json`, and `GET /health` return source, configuration, local-library, and health information.
  - `/sources` describes searchable sources and carries the canonical `key`, `provider`, `title`, `collection`, `searchable`, `homepage`, `icon_url`, `size`, `intent_phrases`, `media_types`, `item_count`, `indexed_count`, `truncated`, `body_truncated_count`, `index_freshness`, and `derived_text` fields. It never exposes a filesystem locator or an `available`/`error` status.
  - `/library.json` describes library sources from every provider, including unsearchable archives. Its records have the same canonical shape; metadata includes `provider_id`, `mime_counts`, and `native_fulltext_available`.
  - Source and library responses are paged. Omitted `limit` uses the global `page_size`; explicit limits are positive and at most 200. `X-Total-Count`, `X-Offset`, `X-Has-More`, and `X-Snapshot-Version` describe the page. A first page is never presented as the whole library.
  - Failures are operation-scoped: a search result carries its source error, and a content request reports its own failure. Listing does not probe sources and does not persist availability or last-error state.
  - `/health` is read-only and reports index and archive capabilities, entry counts, homepage resolution, embedding state, and search and preview cache statistics.

To include ranking scores and explanations in both search endpoints, start the server with `zimantic serve --debug` (or its alias, `serve --dev`).

- Debug result objects include a `ranking` breakdown with each semantic, title, and full-text rank and contribution, title-match signals, intent, and disambiguation adjustments.
- Stream `snapshot` and `done` events always carry source progress and a per-source result count. In debug mode they also add `matched_by` match-method counts.
- The non-streaming endpoint exposes match counts in the `X-Matched-By` header, and the web UI renders them above the results.
- Debug output is configured for the whole server, not per request. `SIGHUP` restarts the whole server in debug mode.

Reloading indexes is **not** an HTTP API: running servers rescan via `zimantic reload` or `kill -HUP <pid>`.

### Caches

Exact queries use small LRU caches controlled by `cache_size` and `cache_bytes` (32 MiB per cache by default). The ranked pool is shared across pages and display filters for a given search scope, and is invalidated when the set of searchable sources differs. Small source selections get their own scope so each selected ZIM contributes a larger share of the pool, but that scope is not cached. Larger selections share the cached full-search pool, except a selection that names a remote source outside `max_remote_sources`, which is searched as its own uncached scope.

For large libraries, `search_workers` controls how many ZIM indexes are searched in parallel. `max_stream_ranks` limits completion-spaced intermediate ranking passes. Timed snapshots are additionally emitted about every 700 ms so a slow query keeps updating, and the final result still ranks every source. A lower `max_stream_ranks` reduces CPU use on small devices. The automatic profiles use 8 workers and 16 completion-spaced passes on desktops, and 2 workers and 8 passes on low-memory devices.

## Using a different embedding model

The default is `multilingual-e5-small`, but Zimantic loads whatever ONNX graph and tokenizer vocabulary are in `model_dir`. Two tokenizer formats are supported:

- `sentencepiece.bpe.model` selects the XLM-R SentencePiece tokenizer used by the multilingual-e5 models.
- `vocab.txt` selects the BERT WordPiece tokenizer used by the English e5-v2 models.

Zimantic chooses by which vocabulary file is present. If a directory contains both, it reads `tokenizer_config.json` when available; otherwise it keeps the historical SentencePiece choice. The ONNX graph cannot reliably identify its tokenizer, so this file-based switch is the dependable signal. Apart from the vocabulary, the model must follow the same e5 contract: `input_ids` and `attention_mask` inputs (plus `token_type_ids` when the graph declares it), a `last_hidden_state` output, mean pooling, L2 normalization, and the `query: ` / `passage: ` prefixes.

`zimantic model` downloads the Xenova ONNX exports from a pinned revision and verifies each file's SHA-256 before replacing the destination. For the default model, fp32 is about 470 MB, fp16 about 235 MB, and int8 about 118 MB (the int8 export is Xenova's `model_quantized.onnx`). The default is int8, which is the smallest download and is optimized for CPU inference. **If you run embeddings on a GPU, prefer `zimantic model fp16`**: accelerated providers generally execute fp16 faster than quantized int8. Use fp32 only when comparing against a full-precision baseline. The probe check described below lets indexes built with one precision be served with another without re-embedding.

To try the larger multilingual model:

```bash
zimantic model int8 --model multilingual-e5-base
```

Replace `base` with `large` in the model name to try `multilingual-e5-large`. Non-default models get their own folder under the model root, so the base example writes `model/multilingual-e5-base/int8` (and does not replace the default model). For the English-only `e5-small-v2`, the command selects its WordPiece vocabulary automatically:

```bash
zimantic model int8 --model e5-small-v2 --dir model-e5-small-v2
```

The same command works for other BERT WordPiece e5-v2 models such as `e5-base-v2` and `e5-large-v2`; run `zimantic model --list` for the full set. Each precision goes into its own folder (`model-e5-small-v2/int8`, for example), so you can compare or switch without redownloading. Pass `--force` to replace an existing file whose checksum does not match.

Point `model_dir` at one precision folder from the new model, use a separate `index_dir` so the old indexes stay usable, and rebuild every index:

```toml
model_dir = "./model-e5-small-v2/int8"
index_dir = "./indexes-e5-small-v2"
```

```bash
zimantic build --force
zimantic serve
```

Vectors are model-specific. Each full index records a fingerprint of `model.onnx` and the tokenizer vocabulary. A server configured with a different model ignores those vectors and serves title and full-text results until the index is rebuilt. Rebuild after switching models, because otherwise meaning search is unavailable. A checksum warning for a non-default model is expected; the check is informational.

A full build also stores a small set of fixed probe sentences and their embeddings. When the model fingerprint differs, `serve` re-embeds those stored probes with the configured model and reuses the index if the mean cosine similarity is at least 0.99 (and the minimum is at least 0.98). This lets an index built with the FP32 export be served with the int8 export of the same model, for example. It does not accept a different model or tokenizer, and indexes built before this feature have no probes, so they still require the same model file. The check is deliberately conservative: a different fine-tune with the same architecture should fail it.

These measurements use 256-token passages, batch size 32, and 4 CPU threads on one x86-64 host. Absolute numbers vary with CPU, but the ratios show the trade-off:

| Model | Int8 ONNX | Vector dim | Extra RSS¹ | Embedding² | Notes |
|---|---:|---:|---:|---:|---|
| `multilingual-e5-small` | 118 MB | 384 | ~190 MB | 68/s (1×) | Default; 100+ languages |
| `multilingual-e5-base` | 279 MB | 768 | ~340 MB | 26/s (0.38×) | 100+ languages; ~2.7× slower |
| `multilingual-e5-large` | 562 MB | 1,024 | ~730 MB | 9/s (0.13×) | 100+ languages; ~7.8× slower |
| `e5-small-v2` | 34 MB | 384 | ~65 MB | 64/s (0.93×) | English only; WordPiece |

¹ Additional resident memory for the model and vocabulary after loading, excluding ONNX Runtime's batch arena. Add this to the server's baseline footprint (see [Measured memory use](#measured-memory-use)); leave about 0.5 GB of headroom for base and 1 GB for large. On hosts with 2 GB RAM or less the arena is disabled, so builds stay closer to these figures.

² Passages per second while embedding 256 passages in batches of 32. A single query embedding takes about 14 ms with either small model, 39 ms with base, and 110 ms with large, so query latency rises in the same direction.

`e5-small-v2` has the same 12-layer, 384-hidden encoder as `multilingual-e5-small`; the smaller file comes from a 30k-entry instead of a 250k-entry embedding table. It therefore saves roughly 125 MB of resident memory but embeds at about the same speed, and it cannot search non-English text. Choose it only for an English-only library on a RAM-constrained host; otherwise the default gives much broader coverage for the same compute.

Vector storage also grows with dimension: an 8-bit IVF index stores one byte per dimension per article (384 B, 768 B, and 1 KB), while a flat index for under 10,000 articles stores float32 vectors (1.5 KB, 3 KB, and 4 KB). FAISS flat indexes do not provide float16 storage; the SQLite staging table uses float16 while a build runs, but vectors are widened to float32 when written to FAISS. For a mostly English library on a small device, keep the default; base is a reasonable upgrade when cross-language recall matters; large needs a machine with more RAM.

## Measured memory use

As a reference point, a clean benchmark using `wikipedia_en_100_2026-08.zim` (5,056 articles) and one local index measured these peak resident set sizes:

| Operation | Peak RSS |
|---|---:|
| Full index rebuild | 301.7 MiB |
| Semantic server (`serve`) | 323.5 MiB |
| Title and full-text server (`serve --fast`) | 65.6 MiB |

The server benchmark loaded the embedding model, opened the single ZIM index, and handled health, source-discovery, and search requests. `serve --fast` omits the model and FAISS, so it uses much less memory but does not provide meaning-based search.

**Where the memory goes.** With the default model, the ~118 MB int8 weights and the SentencePiece vocabulary (~50 MB of native memory) make up most of the footprint; the English-only `e5-small-v2` replaces both with a ~34 MB model and a small WordPiece vocabulary. They are loaded once and are not duplicated, and `gc.collect()` cannot release them because they are C++ allocations, not Python objects. ONNX Runtime also reads the file into a temporary buffer while loading (so there is a short-lived ~2× peak at startup), and its **CPU memory arena** keeps the working set of the largest embedding batch for the lifetime of the session. At `batch_size = 32` that arena alone can consume ~400 MB and retain it. On hosts with **2 GB RAM or less**, Zimantic therefore disables the arena and returns freed heap memory to the OS between batches (`malloc_trim` on glibc), capping build memory near the model size at a modest throughput cost. Machines with more RAM keep the arena for faster builds. `serve` embeds one query per active search; up to `max_concurrent_searches` queries can be in flight, but each query is a single short sequence, so the arena costs little there either way.

## How it works

### 1. Indexing a ZIM (`build`, once per ZIM)

**What gets read.** Every entry is visited once; images, stylesheets, and scripts are skipped. Redirects are stored as **title-only** entries pointing to their article, so searching "USA" still finds the United States page.

**App-shell ZIMs.** Some ZIMs store article bodies in JSON or Markdown behind a JavaScript shell. Zimantic reads that companion and keeps each article's own path as a deep link, so results point to the real article instead of the shared shell. This covers Kiwix's `content/page_content_<id>.json`, media and app bundles that keep a `<slug>.json` companion (for example `videos/<slug>.json` or `playlists/<slug>.json`) keyed by the deep link, and docs/curriculum bundles (freeCodeCamp and similar) whose `<route>.md` holds the body under `content/`. Plain-text fields such as video descriptions and Markdown prose are indexed too (front matter and fenced code are dropped). Shell stubs and "enable JavaScript" notices are skipped.

**Text extraction.** Stylesheets, scripts, footnotes, common boilerplate, and page chrome (category footers, navboxes, tables of contents, maintenance tags) are ignored, including page chrome that is nested or hidden (`display:none`, `visibility:hidden`, the `hidden` attribute). When a page repeats the same text under `lang-*` classes (parallel translations, as in TED talks), only the `lang-default` copy is kept. Visible blocks are collected in priority order, with paragraphs preferred, and block boundaries keep neighboring text runs from running together.

Each article's **excerpt** is extracted transiently for the embedding and discarded after the vector is built. A separate lexical **body** is tokenized into the contentless FTS5 `body` table up to `max_body_text_bytes` (1 MiB per item by default), independently of the short `max_preview_chars` (1,000 by default) embedding/preview limit. Extraction stops at a UTF-8 character boundary; `body_truncated_count` reports how many rows were cut off by the budget or an incomplete native parse. At search time, only the displayed local results are extracted again for previews, bounded to `max_preview_chars` at a word boundary. Only the first `preview_limit` results of a page are hydrated (10 by default, matching the default page size; 0 disables previews); the rest keep an empty `lead`. Preview extraction reads at most 32 KiB of HTML per page, falling back to the `max_html_bytes` limit when that prefix has no readable text, so a few large pages do not hold up the whole result page. Entries are skipped when they are assets, JavaScript shells, empty pages, scanned PDFs without text, or other non-article content. Pages that ship only script or link references (for example PhET simulations) remain title-only, so they can still be found by title. The builder also skips an entry whose decompressed size exceeds `max_entry_bytes` (1 GiB on desktop, 256 MiB on the mobile profile); this high safety ceiling preserves large, useful articles while preventing pathological entries from exhausting memory before the parser limit can be applied. Companion JSON and Markdown bodies are subject to the same limit.

**Other ZIMs** (Stack Exchange, Gutenberg, TED, and others) are supported when they contain readable text: HTML, XHTML, plain text, and PDFs. PDF text is read from the file's embedded text layer, so scanned documents without one are skipped.

**Embedding.** Titles and transient excerpts are embedded with [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) (int8 ONNX, ~118 MB). The model receives a fixed budget of 256 tokens. See [Using a different embedding model](#using-a-different-embedding-model) for other e5 models, their tokenizers, and their memory and speed trade-offs. Entries with no body text (title-only pages, for example JavaScript loader shells) remain searchable through title and full-text retrieval; they simply have no meaning vector. Search previews are generated independently from the local ZIM only for the final displayed page.

**Storage.** Each ZIM gets an immutable generation under `index_dir/<provider>/<collection-slug>-<hash>/<source-slug>-<hash>/generations/<build-generation>/` (`index.sqlite`, optional `index.faiss`, and a `lease`), plus one shared catalog:

- `index.sqlite`: titles, paths, redirect targets, title postings, and a separate contentless FTS5 body index. The `vecs` table exists in every generation but is empty once a full build writes `index.faiss`; a fast build leaves it empty too. Raw excerpts and raw derived text are not stored in the new schema.
- `index.faiss`: the vectors (the only place finished embeddings are stored), in one of two layouts depending on article count. A fast or empty-semantic build has no `index.faiss`.
- `staging-fast.sqlite` / `staging-full.sqlite` (when present): resumable private staging files. They are not published generations and are removed after a successful build or a fatal failure.
- `catalog.sqlite` (beside `index_dir`, not inside it): a provider-neutral `sources` table keyed by canonical source key, with the metadata used by `/sources` and `/library.json` (title, description, language, creator, publisher, date, provider ID, category, normalized tags, item count, license, origin, attribution, relation, content-type counts, homepage, native full-text capability, and the icon as a BLOB). It stores the absolute archive path only as a private indexed locator, never as source identity. It contains metadata and icons only, never vectors. It is a disposable cache: the ZIM files are the source of truth, and deleting it makes the next scan rebuild it. A warm catalog lets `serve` list sources without opening every archive; the startup scan still opens each new, resized, or previously failing ZIM to refresh its row, and the first scan after deleting the catalog opens every archive. After that, archives and their libzim `Searcher` objects are opened lazily on first use. A missing or unreadable `zim_dir` (for example an unmounted USB drive) is an error that keeps the previous catalog, rather than being mistaken for a library from which every ZIM disappeared. While the directory is unavailable, the served `/sources` and `/library.json` lists are empty; the catalog rows remain on disk for the next successful scan once the directory is back. An unreadable subdirectory is logged as an error and its sources are omitted, but the rest of the library stays available.

The logical source key is derived from the provider, collection, and root-relative archive path, never from an absolute filesystem path. Relocating a collection root without changing its relative layout preserves keys. Two archives can share a stem and size and still be distinct sources; every build writes a fresh immutable generation and atomically replaces the `current` manifest. Build fingerprints use the archive UUID, size, and whole-second mtime as a practical change detector, not proof of identical bytes: a same-size write that also restores the mtime can evade detection. Keep source content immutable or use an external snapshot when strong consistency matters. Pre-redesign stem-based indexes are left untouched but are not served; rebuild under the new locator to restore indexed search. To copy a subset of finished indexes to another machine, see [Copying finished indexes to another machine](#copying-finished-indexes-to-another-machine).

| ZIM size | Vector index | Why |
|---|---|---|
| Under 10,000 embedded articles | **Flat**: the query is compared with every vector | Exact, and small enough (a few MB) that comparing everything is fast |
| 10,000 embedded articles or more | **IVF + 8-bit (SQ8)**: vectors are grouped into up to 4·√n clusters (capped so FAISS can train at least 39 points per centroid), and each search scans about 6% of the closest clusters by default (64 of 1,062 on WikiMed's 70k articles) | Comparing millions of vectors per search is too slow. On WikiMed (70k articles), scanning 64 of 1,062 clusters is within a few points of scanning every cluster, at less than half the search time (26 ms vs 66 ms). The probe count scales automatically for larger ZIMs; set `nprobe` in `config.toml` to use a fixed value instead. 8-bit numbers are nearly exact. **Heavier compression (for example PQ48) loses ~25% of the top hits.** |

IVF training samples are selected across the ZIM and scale with the number of clusters, with at least 39 samples per centroid as required by FAISS.

The `.faiss` file is memory-mapped, so searches read only the clusters they touch.

Builds resume from the last saved batch. FAISS files are published atomically, and a fast index remains available while a full index builds. Changing between `--fast` and a full build after an interruption restarts that index, because fast rows have no vectors to reuse. `build --force` applies the configured extraction and preview settings while rebuilding the selected indexes.

Use `--force` when a completed index must be regenerated even though its ZIM still has the same filename stem and byte size. This includes changing the embedding model or tokenizer, changing text-extraction or indexing behavior, replacing a ZIM with different content but the same size, converting a full index back to `--fast`, repairing a completed index whose `.faiss` file is missing, or intentionally replacing a suspect index. It is not normally needed when a ZIM's size changes (that automatically triggers a rebuild), when resuming an interrupted build, or when upgrading a fast index to a full index (which is automatic). `--force` deletes the existing SQLite, FAISS, and upgrade files before rebuilding, so use a separate `index_dir` for model migrations when the old indexes should be preserved.

### 2. Starting the server

At startup Zimantic reads the shared `catalog.sqlite` for source metadata, homepages, counts, and icons, then resolves the `current` manifest of each **finished** index generation whose archive is still present and matches the build-time source fingerprint. A cold catalog (or one deleted to force a rebuild) is refreshed synchronously and opens each new or changed ZIM once; after that, archives and their libzim `Searcher` objects are opened lazily on the first search or article request. Zimantic also discovers ZIM files in `zim_dir` that lack a usable SQLite index and uses each archive's native full-text index as a graceful fallback.

An index whose ZIM is missing is ignored. An index whose archive has changed size is set aside, and the archive is served unindexed instead (re-run `build` to restore the index), so a partial or stale index never masks the ZIM that is actually available. Each stem appears only once: a finished SQLite index takes precedence over the filesystem fallback, and an unfinished one leaves the stem to that fallback. FAISS and vector files load in the background during startup, so title-word and full-text searches are available while meaning search initializes.

The page automatically lists sources from every configured collection. A Kiwix collection discovers catalog sources and searches them with Kiwix full-text search. Catalog discovery is paged and capped per entry at `max_catalog_entries` (default 50,000); remote XML responses are capped at `max_response_bytes` (default 64 MiB). An unscoped search uses at most each entry's `max_remote_sources` (default 100); selecting a remote source explicitly always searches it. Catalog refresh runs in the background: a failed refresh keeps the last complete catalog addressable, retries with bounded backoff, and reports the failure on the collection's discovery state. A failed source search is reported only in that request's result, so the next request can try again immediately.

**Graceful degradation.** `serve` works without running `build` first and without `catalog.sqlite`: startup scans the configured ZIM collections, rebuilds missing catalog metadata, and treats every usable archive as a search source. A finished index remains usable without FAISS: the server falls back to **title-word and ZIM full-text search**. A ZIM with no SQLite index is still searched through its native full-text index, but has no SQLite title-word ranking and no meaning search until it is built. Native full-text search usually matches titles too, but without the dedicated title-word ranking and title bonus. An unindexed ZIM that has no native full-text index has nothing to search, so it is skipped as a search source rather than advertised as an available but empty one; it still appears in `/library` with its catalog metadata, but `/sources` omits it and it cannot be selected for a search.

| What exists for a local ZIM | Meaning (FAISS) | Title words (SQLite FTS5) | Native full-text (libzim) |
|---|:---:|:---:|:---:|
| Full index (`.sqlite` plus matching `.faiss`) | yes | yes | yes |
| Fast index (`.sqlite` only) | no | yes | yes |
| No index, archive has a native full-text index | no | no | yes |
| No index, no native full-text index | no | no | no — library only, not searchable |

`serve --fast` skips the model and all vectors, leaving title-word and full-text search. A normal `serve` loads the model in the background; if `model_dir` is missing or unreadable it logs a warning and keeps serving lexical results. Vectors built with a different embedding model are ignored in the same way. `kiwix-serve` is not involved in any local row of the table: the ZIM archive is searched directly, and its embedded full-text index is not a separate Kiwix index file. With a Kiwix `[[collections]]` entry configured, remote catalog sources are searched through kiwix-serve's full-text API instead.

FastAPI serves a lightweight HTML page that is accessible from any browser at `http://<host>:8090` (the `port` in `config.toml`). To serve it under a path such as `/zimantic`, set `web_base_path = "/zimantic"` and point a reverse proxy at the server with that prefix stripped:

```nginx
location /zimantic/ {
    proxy_pass http://127.0.0.1:8090/;
}
```

Generated links, assets, and API requests then stay under `/zimantic/`; open the page with the trailing slash.

**Trusted vs. untrusted archives.** By default the `/content/` shell is trusted and the article document inside it is **not** sandboxed. This keeps functionality intact, including persistent Web Storage and IndexedDB used by some offline sites. Set `secure_browsing = true` to sandbox the article document instead; the shell stays trusted, while the article runs with an opaque origin and gets the in-memory storage fallbacks described above. Secure browsing protects the app from untrusted source scripts, but it is not complete isolation from untrusted content: a raw `/article/...` URL opened directly is a top-level document and is still not sandboxed, and a popup opened by a sandboxed article with `allow-popups-to-escape-sandbox` likewise runs with the app's origin and persistent storage. Use the `/content/` shell for the bounded view, and treat direct `/article/` navigation and such popups as trusted. The setting remains off by default.

Content from every local provider is addressed as `/content/{source-key}/{path}` and `/article/{source-key}/{path}`. The source key is one percent-encoded path segment, so keys containing colons (for example `files:modules:root`) appear as `/content/files%3Amodules%3Aroot/...`. `/article` supports byte ranges (`Accept-Ranges: bytes`, `206`/`416`), including `bytes=0-` media playback, and advertises wildcard CORS for opaque-origin article frames and their preflights. Raw HTML receives the navigation bridge, so its response bytes can differ from the stored item; ranges are intentionally not applied to that decorated representation.

Reverse proxies must preserve the original request target rather than decoding or collapsing percent escapes. The server rejects encoded path separators (`%2F`/`%5C`) using the ASGI `raw_path` and fails closed for path-bearing content requests when a trustworthy raw target is unavailable. A proxy that normalizes the URI before forwarding it therefore disables path-bearing content serving even though the library and search pages still load.

**Reloading indexes without a restart.** `zimantic reload` sends `SIGHUP` to a normal server, which rescans `index_dir` and `catalog.sqlite` and reconciles the available indexes. `kill -HUP <pid>` also works. In `serve --debug` mode (also available as the `serve --dev` alias), `SIGHUP` restarts the whole server process and reloads all in-memory state.

### 3. Each search

1. **Embed the query** with the same model used to index.
2. **Run three searches on each selected local source.** Sources run in parallel, up to `search_workers` at a time:

   | Search | Finds | Good at | Time |
   |---|---|---|---|
   | **Meaning** (FAISS) | Articles whose bounded content-excerpt vector is closest to the query (cosine similarity) | Questions, descriptions, other languages | ~13 ms |
   | **Title words** (SQLite FTS5, BM25 ranking) | Titles containing every word of the query, including redirect titles | Exact titles, alternate names (via redirect titles) | ~4 ms |
   | **Full text** (the ZIM's own Kiwix index) | Articles containing the words anywhere | Words buried deep inside an article | ~8 ms |

3. **Collapse redirects.** Every redirect hit resolves to the article it points to, and duplicates are merged, so each article appears only once.
4. **Filter weak meaning matches when a lexical alternative exists.** Semantic candidates below `min_cosine_similarity` (0.85 by default) are discarded before ranking, but only when that source also returned a title-word or full-text match. If a query has no lexical match at all, its best meaning candidates are kept, so a meaning-only query never becomes an empty result list. Lower the value in `config.toml` when a lexical match should not suppress broader meaning matches; cross-language queries often need 0.80 (see [Queries in another language](#queries-in-another-language)).
5. **Merge the three lists with Reciprocal Rank Fusion (RRF).** Their scores are not comparable: one is cosine similarity, another is a BM25 score, and the third is a position in Kiwix's list. RRF therefore ignores scores and uses positions only: an article earns `1 / (60 + its position)` from each list it appears in; the full-text contribution is doubled for queries of ten or more words. An article found near the top by several searches beats one that is first in just one. Redirects are collapsed before fusion, so an article can contribute at most once per search list. A bounded lexical bonus favors titles containing more query words, with compact titles preferred when coverage is equal. Exact phrases in titles or readable article text and source-specific intent rules (for example WikiHow queries) provide additional deterministic signals. Stack Exchange-style profile pages under `/users/` are demoted unless the query explicitly asks for a user or profile.
6. **Treat disambiguation pages specially.** At build time a page is flagged when its title ends in "(disambiguation)", when it renders the "This disambiguation page" footer, or when it carries a disambiguation category (the rendered `Category:` link or `wgCategories`). The required suffix is stored in the title, so no separate disambiguation metadata is needed. Fast builds read a bounded HTML prefix for these signals; a footer that appears only beyond that prefix may need a full build to be detected. A query that names the full title is treated as navigation, whether it uses `air (disambiguation)` or `air disambiguation`; a question that names the base title and ends in `?` is treated as navigation too. Those queries promote the page above the normal score range. Any other query that merely matches it is demoted so the real article wins.

#### Queries in another language

A query in Chinese, Hindi, Arabic, or another language against an English ZIM usually shares no words with the titles or article text, so meaning search is usually the only stage that can retrieve the right article. Cross-language cosine scores run lower than same-language scores, even when the result is correct: the whole distribution shifts down rather than the correct article disappearing. The floor only applies when a source also has a lexical match, so a pure cross-language query keeps its best meaning candidates even at 0.85. A spurious title or full-text match can still suppress them; lowering the threshold recovers those cases:

```toml
# config.toml
min_cosine_similarity = 0.80
```

This is a recall/precision trade-off, not a separate language mode: lower values admit weaker meaning-only matches, while title-word and full-text matches are unaffected.

The lexical stages remain tuned for space-delimited, mostly English text. The stopword list is English, SQLite's `unicode61` tokenizer treats a run of Chinese characters as one title token, and the long-query full-text boost counts space-separated words. A long Chinese or Japanese question without spaces therefore gets little help from title-word matching or that boost; expect it to depend on meaning search and the similarity floor.

Search ranks one pool of results (the larger of `max_results` and `candidate_count`) and caches it by query and source scope. A selection of at most `min(10, 35%)` of the available sources searches only those sources and is not cached, because one-off subsets would fragment the cache. A larger selection is promoted to one full search over every available source, and the source filters narrow the displayed page. The exception is a selection that names a remote source outside `max_remote_sources`: that explicit choice is searched as a scoped, uncached request instead of silently dropping the source. Paging and display filters reuse the cached full-search pool; a small scope is recomputed when it is paged or filtered.

The streaming endpoint sends a provisional page as each source finishes. Pagination uses regular links and refreshes the page through the JSON endpoint without a full document reload.

*WikiMed timing: a complete search is 26 ms (median over 785 benchmark queries). A Raspberry Pi Zero 2 W takes around 150 ms.*

## Compared with Kiwix search

![Ask a question, get the article](docs/images/1-questions.png)
![Ask in another language](docs/images/3-languages.png)
![How often the right article comes first: Zimantic vs Kiwix](docs/images/0-scorecard.png)

*Measured on WikiMed, the Wikipedia medical encyclopedia ZIM (`wikipedia_en_medicine_maxi_2026-04`, 70,523 articles), compared with kiwix-serve's full-text search.*

The tables use three measures: **#1** means the wanted article is the first result, **Top 5** means it appears within the first five, and "not in top 20" means it does not appear in the first 20 results. Each percentage is the share of queries that met the measure.

### Results by type of search

| Type of search | Example query → article wanted | Kiwix | Zimantic | #1 Kiwix → Zimantic | Top 5 Kiwix → Zimantic | Queries |
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

These tests search the English articles with one word in another language, using 15 common medical words per language (malaria, diabetes, fever, cough, pregnancy, heart, blood, and so on). The test uses purpose-built translations, so less common languages may contain mistakes.

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

- **Words buried deep inside an article.** Kiwix indexes every word, so it performs better on deeper searches (68% first vs 28%; in the top 5 they are nearly tied, 78% vs 76%). Zimantic indexes only each article's title and bounded content excerpt.
- **No setup.** Kiwix search works as soon as a ZIM is available. Zimantic indexes each ZIM first.
- **Smaller footprint.** Zimantic stores a `.sqlite` and `.faiss` per ZIM and needs more RAM.
