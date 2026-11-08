# Zimantic

Zimantic is an offline search engine for Kiwix ZIM files and configured directory trees, such as an offline `/library/www/html/modules`. Semantic search finds articles from questions and descriptions, including misspellings and queries in another language. Zimantic runs offline on devices as small as a Raspberry Pi Zero 2 W.

Semantic search performs best in widely spoken languages. Coverage varies by language; see [Compared with Kiwix search](docs/search.md#compared-with-kiwix-search).

| Query | Kiwix search | Zimantic |
|---|---|---|
| "what causes lockjaw" | *Tetanus* not in the top 20 | *Tetanus* at **#1** |
| "diabetis" (misspelled) | *Diabetes* not in the top 20 | *Diabetes* at **#1** |
| "糖尿病" (Chinese for "diabetes") | *Diabetes* not in the top 20 | *Diabetes* at **#1** |

*Searched in WikiMed, the Wikipedia medical encyclopedia ZIM.*

## Features

- Semantic search. Questions and descriptions match articles even when they share no words with the title.
- Alternate names and misspellings. "diabetis" finds *Diabetes*; "Leber's disease" finds *Leber's hereditary optic neuropathy*.
- Multilingual queries. One model covers 100+ languages, and a query in one language can find articles written in another. Results are strongest in widely used languages; see [Compared with Kiwix search](docs/search.md#compared-with-kiwix-search).
- Collections and multiple sources. Browse the library by collection and provider, search one collection as a unit, or combine individual sources. See [Source selection](docs/usage.md#source-selection).
- Files collections. Search and serve a configured directory tree, such as an offline copy of `/library/www/html/modules`, as one bounded collection. See [Files collections](docs/collections.md#files-collections).
- Kolibri installations. Search and serve a local Kolibri v0.19.5 installation directly, with one collection per installed channel and one source per top-level topic. See [Kolibri installations](docs/collections.md#kolibri-installations).
- Combined ranking. Zimantic ranks semantic, title, and full-text results together. The full-text list combines SQLite body search with archive-native search when available.
- Merged results and bounded previews. Redirects are merged into their target article, so each article appears once. Each result carries a bounded excerpt as its preview.
- Progressive results. Sources run in parallel. The page shows a provisional merged list as each source finishes, then reranks it deterministically.
- Library view. Browse ZIM collections, files collections, Kolibri channels, and remote Kiwix books at `/library`, grouped by collection with provider and content-type filters. See [Library view](docs/usage.md#library-view).
- Low resource use. Runs on low-resource devices such as a Raspberry Pi Zero 2 W (512 MB RAM) using roughly 250–325 MB while serving; see [Measured memory use](docs/search.md#measured-memory-use).
- Search without vectors. Title and body search work when their indexes exist. A ZIM without a Zimantic index can use its archive full-text index. If a model or FAISS file is unavailable, or model probes do not match, Zimantic uses the available lexical search. An invalid archive full-text index affects only that archive; the server logs the error and keeps running.
- Concurrent searches. `max_concurrent_searches` bounds simultaneous searches. A request waits up to `slot_wait_seconds` for a free slot before reporting busy, so overlapping searches queue on slow hardware instead of failing immediately; abandoned browser streams skip queued source work and return their slot. Identical in-flight queries share one computation, and repeated queries are answered from an in-memory cache bounded by `cache_size` and `cache_bytes`. Explicit source and collection scopes are cached by their expanded source keys and publication generation; see [Source selection](docs/usage.md#source-selection).
- Built-in article serving. Result links open directly from the ZIM archives, including redirects, assets, and each ZIM's homepage, without requiring `kiwix-serve`.
- Web interface and JSON API. Search from any browser on the network or from custom programs. Routes use short paths such as `/search`, `/suggest`, `/library`, `/collections.json`, `/content/<source-key>/<path>`, `/article/<source-key>/<path>`, and `/health`.

Zimantic searches local ZIM archives directly; it does not need a separate Kiwix index. `zimantic build` creates semantic vectors and a SQLite title index, but `zimantic serve` also searches an unbuilt ZIM through its archive full-text index. Zimantic serves articles, assets, and homepages directly from each archive. Use [kiwix-serve](https://kiwix.org/en/applications/) only for remote Kiwix sources.

## Requirements

- Python 3.11–3.14 with native dependency wheels available for the interpreter. `libzim` publishes wheels for specific Python ABIs and platforms, so choose an interpreter with a compatible wheel. Update this range when the native dependencies add support for a newer Python version.
- 32-bit Linux platforms (armhf/armv7) have no prebuilt wheels for the native dependencies. They work when the dependencies are installed as distribution packages. Debian 13 (trixie) or newer provides `python3-onnxruntime`, `python3-faiss`, `python3-libzim`, `python3-sentencepiece`, and `python3-pymupdf` for armhf. Create the environment with `--system-site-packages` so Zimantic can use them. Known 32-bit machine names accept python-libzim 3 or newer; other machines require 4.1 or newer. Python package markers report the machine name, not interpreter bitness, so a 32-bit interpreter reporting a 64-bit machine name still requires 4.1.
- About 120 MB for the default embedding model, plus the ZIM files. The English-only `e5-small-v2` model is about 35 MB on disk.
- Wikipedia-style ZIM files. Zimantic relies on their predictable HTML structure to locate useful content blocks.
- One provider seam. ZIM, remote Kiwix, files, and Kolibri are in-tree providers. Source identity is a stable, URL-safe `key`; `provider` is the only discriminator.
- Configured collections. Collections are declared with `[[collections]]` entries (ZIM roots, Kiwix servers, files trees, and Kolibri installations).
- No `kiwix-serve` is needed for local sources: Zimantic serves local results and articles directly. A Kiwix `[[collections]]` entry discovers remote books and opens their results through links returned by kiwix-serve's search API.

## Install

1. Clone the repository into the data directory. Configuration lives at `$XDG_CONFIG_HOME/zimantic/config.toml` (normally `~/.config/zimantic/config.toml`); omitted settings use hardware and XDG defaults.

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

2. Install the CLI with uv. This installs the locked runtime dependencies without the development tools and creates the executable at `~/.local/bin/zimantic`.

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
   .venv/bin/pip install 'puremagic>=1.30'
   .venv/bin/pip install --no-deps .
   ln -sfn "$PWD/.venv/bin/zimantic" ~/.local/bin/zimantic
   ```

3. Download the default multilingual model.

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

   English-only and larger multilingual models use the same layout; see [Using a different embedding model](docs/search.md#using-a-different-embedding-model). Zimantic warns when `model.onnx` or `sentencepiece.bpe.model` differs from a documented checksum, but continues to run.

4. Put ZIM files in `$XDG_DATA_HOME/zimantic/zims/`. Download them from [library.kiwix.org](https://library.kiwix.org) or [download.kiwix.org/zim](https://download.kiwix.org/zim/).

5. Add a collection and check the XDG config. There is no implicit library, so uncomment or add the `[[collections]]` ZIM entry from `config.toml` (use an absolute `path`, because provider paths resolve from the config file's directory). Relative `model_dir` and `index_dir` overrides resolve under `$XDG_DATA_HOME/zimantic`; omit them to use the defaults. Add Kiwix, files, or Kolibri entries only for those sources.

6. Verify the installation.

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

`zimantic reload` is unavailable on Windows because it relies on `SIGHUP`; restart `zimantic serve` to reload. Stop the server before you replace or delete a ZIM or index file. Windows does not allow an open file to be replaced in place.

## Optional dependencies

Core search does not require external tools. Local caption extraction, OCR,
ASR, and remote Kiwix support are optional; see the [dependency and derived-text
guide](docs/derived-text.md#optional-dependencies).

## Configuration and collections

Zimantic uses explicit `[[collections]]` entries for ZIM archives, remote Kiwix,
Files trees, and Kolibri installations. The [collections guide](docs/collections.md)
has minimal examples, path behavior, provider limits, and advanced overrides.

## Usage

```bash
zimantic build
zimantic serve
zimantic open zims/example.zim
zimantic reload
```

A build with no arguments indexes configured local collections. For command
options, build behavior, GPU setup, hardware tuning, and systemd examples, see
[Usage and operations](docs/usage.md). The [web interface guide](docs/usage.md#source-selection)
covers source selection and the library; the [API reference](docs/api.md)
documents the HTTP endpoints.

## Security and limitations

See [Limitations](docs/limitations.md) for operational constraints and
[Architecture](docs/architecture.md) for provider boundaries and content safety.

## Search details

The README's search examples are a brief illustration, not a universal
benchmark. See [Search, models, and ranking](docs/search.md) for model selection,
ranking behavior, measured performance, and the detailed Kiwix comparison.
