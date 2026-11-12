# Derived text and optional dependencies

## Optional dependencies

Everything in this section is optional. Zimantic starts and searches without it. A missing executable disables only the operation that needs it, and a build reports that operation as unavailable instead of failing the rest of the index.

| Dependency | Enables | Without it |
|---|---|---|
| `ffmpeg` and `ffprobe` | Embedded text-caption extraction and audio decoding for `whisper.cpp` | Sidecar `.srt`/`.vtt` captions work; embedded tracks and whisper.cpp ASR are unavailable |
| `tesseract` plus trained language data | `ocr = "tesseract"` on images and image-only PDF pages | OCR is unavailable |
| `whisper-cli` from [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | `asr = "whisper"` with `asr_engine = "whisper.cpp"` or `"auto"` | That backend is unavailable; `auto` falls through to another installed engine |
| `whisper-ctranslate2` from [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | `asr = "whisper"` with `asr_engine = "faster-whisper"` | That backend is unavailable; it is the fastest option on CUDA GPUs |
| `lsar` (from `unar`) | Archive-container detection for files with a missing or unknown extension | Built-in signatures and `puremagic` stay available |
| `kiwix-serve` | A remote `type = "kiwix"` collection | Remote Kiwix sources are unavailable; local ZIM, files, and Kolibri sources work |
| GPU ONNX Runtime wheel | Accelerated embedding during `build` and `serve` | Zimantic warns and embeds on the CPU; see [GPU acceleration](usage.md#gpu-acceleration) |

On Debian or Ubuntu:

```bash
sudo apt install ffmpeg tesseract-ocr tesseract-ocr-eng unar
```

The OCR language package names follow the codes accepted by `ocr_languages`. Install every language you list there, for example `tesseract-ocr-ita` for `["eng", "ita"]`; `tesseract-ocr-all` installs every language. `tesseract --list-langs` shows the installed languages.

On macOS with Homebrew:

```bash
brew install ffmpeg tesseract tesseract-lang unar
```

On Windows, install ffmpeg and Tesseract from their official builds and put `ffmpeg.exe`, `ffprobe.exe`, and `tesseract.exe` on `PATH`. `lsar` is optional on every platform.

### Derived-text workers

`workers` under `[derived_text]` accepts `"auto"` (the default) or a positive
integer. OCR, ASR, and embedded-caption extraction run in a build-wide process
pool shared by every source, so one slow page does not block another source's
reader thread. Inputs are spooled to a private file before submission; only the
path and validated options cross the process boundary. The built-in SubRip and
WebVTT parser always runs in-process because it is pure Python.

`"auto"` sizes the pool from usable cores minus one and currently available
memory (512 MiB per worker, with headroom). If no worker fits, the engine-backed
operations are reported unavailable instead of risking memory pressure; the
built-in caption parser still works. A single source still reads and extracts
its entries sequentially; the pool overlaps extraction across concurrently
built sources. Use the benchmark in `tests/benchmarks/run_derived.py` to compare
pool sizes on the target hardware.

### Quick start: remote Kiwix

Install `kiwix-serve` from [kiwix.org](https://kiwix.org/en/applications/) and start it with a library XML file:

```bash
kiwix-serve --port=8085 /path/to/library.xml
```

Then point a `type = "kiwix"` collection at it:

```toml
[[collections]]
type = "kiwix"
name = "remote"
server = "http://127.0.0.1:8085"
catalog_limit = 1000
max_catalog_entries = 50000
```

Local ZIM, files, and Kolibri sources never use `kiwix-serve`.

GPU ONNX Runtime wheels are covered in [GPU acceleration](usage.md#gpu-acceleration).

### Whisper ASR models

`asr = "whisper"` uses a local Whisper engine. Zimantic supports two backends:

| `asr_engine` | Executable | Best for |
|---|---|---|
| `whisper.cpp` | `whisper-cli` | CPU-only and low-memory devices, including a Raspberry Pi |
| `faster-whisper` | `whisper-ctranslate2` | CUDA GPUs; up to 4× faster than the openai-whisper backend, or up to 16× with batched inference |
| `auto` | first available | Prefers whisper.cpp, then faster-whisper |

#### Quick start: whisper.cpp (CPU and edge devices)

```bash
git clone https://github.com/ggml-org/whisper.cpp
cmake -B whisper.cpp/build -S whisper.cpp
cmake --build whisper.cpp/build -j --config Release
sudo install whisper.cpp/build/bin/whisper-cli /usr/local/bin/
sudo apt install ffmpeg   # Debian/Ubuntu; whisper.cpp needs ffmpeg
```

```toml
[derived_text]
asr_engine = "whisper.cpp"
asr_model = "tiny.en-q5_1"   # ~31 MiB; good default for a Pi Zero 2 W
# asr_model = "tiny-q5_1"    # Spanish / multilingual
asr_threads = 2              # use 4-8 on a Pi 5 or desktop CPU
```

Use `base.en-q5_1` (~57 MiB) when accuracy matters more than size. For Spanish or other languages, use the multilingual `base-q5_1` checkpoint.

#### Quick start: faster-whisper (NVIDIA GPU)

```bash
pipx install whisper-ctranslate2
# GPU execution also needs CUDA 12 cuBLAS and cuDNN 9.
```

```toml
[derived_text]
asr_engine = "faster-whisper"
asr_model = "base.en"   # or large-v3 / distil-large-v3 for higher accuracy
# asr_model = "base"    # for Spanish / multilingual
asr_batched = true      # extra 2-4x throughput on GPU
```

faster-whisper has no official CLI, so Zimantic uses `whisper-ctranslate2`, its openai-whisper-compatible client. It is the fastest backend on CUDA GPUs and does not need a system ffmpeg.

Use `pipx` for faster-whisper, or reinstall it with `uv pip` after a `uv sync`, because `uv sync` prunes packages that are not in the lockfile.

Select the backend and model under `[derived_text]`:

```toml
[derived_text]
asr_engine = "auto"   # auto, whisper.cpp, faster-whisper
asr_model = "base.en"
# asr_model = "base"  # Spanish / multilingual
asr_threads = 0       # 0 uses the engine default
asr_batched = false   # faster-whisper: extra 2-4x speed, uses VAD
```

`auto` prefers whisper.cpp because it is the lightest and most portable. On a CUDA host, set `asr_engine = "faster-whisper"` explicitly to get the GPU speedup, and set `asr_batched = true` for its additional throughput. In a mixed install, the same `asr_model` name works across backends: for example `base.en` maps to `ggml-base.en.bin` or the faster-whisper `Systran/faster-whisper-base.en` repository. For Spanish or other languages, use `base`, which maps to `ggml-base.bin` or `Systran/faster-whisper-base`.

For whisper.cpp, Zimantic supports quantized models by name:

```toml
asr_model = "tiny.en-q5_1"   # about 31 MiB; smaller and faster than tiny.en
# asr_model = "tiny-q5_1"    # Spanish / multilingual
```

Approximate non-quantized sizes are 75 MB (`tiny`), 142 MB (`base`), 466 MB (`small`), 1.5 GB (`medium`), and 2.9 GB (`large`). faster-whisper also accepts distilled models such as `distil-large-v3`, `distil-medium.en`, and `distil-small.en`.

Zimantic downloads the selected checkpoint on first use when it is missing. Downloads are atomic, checksum-verified, and serialized by a per-model lock, so concurrent builds fetch one copy and reuse it. Each backend keeps its models in the usual place:

| Backend | Cache |
|---|---|
| whisper.cpp | `$XDG_CACHE_HOME/zimantic/whisper-cpp` or `~/.cache/zimantic/whisper-cpp` |
| faster-whisper | `$XDG_CACHE_HOME/zimantic/faster-whisper` or `~/.cache/zimantic/faster-whisper` |

The host needs network access the first time it fetches a model. If the host is offline or the download fails, only ASR is marked partial and the build exits non-zero; a cached model is reused without a network request. To prepare a machine in advance, copy the backend's cache directory from another host or run a build that enables ASR while online.

### Quick start: Tesseract OCR

```bash
sudo apt install tesseract-ocr tesseract-ocr-fra
# add tesseract-ocr-<lang> for every language you list
```

```toml
[[collections]]
type = "files"
name = "media"
path = "./media"
derived_text = { ocr = "tesseract", ocr_languages = ["fra"] }
```

OCR reads the same trained-data files as the `tesseract` command. List the languages globally or override them for a collection or exact source. Use the installed trained-data names: French is normally `fra`, not `fre`. Zimantic passes codes through without translation and does not install language data. For the example above, install `tesseract-ocr-fra`. Collection defaults are supported for files and Kolibri collections; Zimantic does not implement ZIM embedded-media enrichment.

```toml
[derived_text]
ocr_languages = ["eng"]   # codes from `tesseract --list-langs`
```

PDF OCR uses the bundled PyMuPDF dependency; only the `tesseract` executable and its language data are external. The `max_ocr_pages`, `max_ocr_pixels`, and `max_ocr_output_bytes` limits apply.
