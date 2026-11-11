"""Turn text into vectors with ONNX embedding models (e5 and static)."""

import ctypes
import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
import onnxruntime as ort
import sentencepiece

from . import hardware
from .model_files import (
    DOCUMENTED_CHECKSUMS,
    MODEL_CONFIG_NAME,
    sha256_file as _sha256,
)
from .model_meta import (
    MODEL_PROBE_MEAN_COSINE,
    MODEL_PROBE_MIN_COSINE,
    MODEL_PROBE_SHAPE_KEY,
    MODEL_PROBE_TEXTS,
    MODEL_PROBE_TEXTS_KEY,
    MODEL_PROBE_VECTORS_KEY,
)
from .settings import DEFAULT_EMBEDDING_TOKENS, default_onnx_query_threads
from .wordpiece import WordPieceTokenizer

logger = logging.getLogger(__name__)

SPECIAL_TOKEN_COUNT = 2

CPU_PROVIDER = "CPUExecutionProvider"
DML_PROVIDER = "DmlExecutionProvider"
OPENVINO_PROVIDER = "OpenVINOExecutionProvider"
MIGRAPHX_PROVIDER = "MIGraphXExecutionProvider"
MIGRAPHX_SESSION_CACHE_SIZE = 4
OPENVINO_PROVIDER_OPTIONS = {"device_type": "AUTO"}

# Accelerated ONNX Runtime providers, best first. "auto" tries every provider
# the installed onnxruntime build reports (and otherwise stays on the CPU) so a
# broken first choice does not hide a working second one. The provider names are
# stable across onnxruntime-gpu, -rocm, -directml and -openvino; MIGraphX is
# available in some ROCm builds. A plain onnxruntime wheel only exposes
# CPUExecutionProvider. CoreML is deliberately excluded: stock macOS wheels
# include it, and its dynamic-shape handling can be slower than the CPU for
# this model, so it must be requested explicitly.
AUTO_PROVIDERS = (
    "CUDAExecutionProvider",
    "ROCMExecutionProvider",
    MIGRAPHX_PROVIDER,
    DML_PROVIDER,
    OPENVINO_PROVIDER,
)

# Accepted values for the embed_device setting, mapped to their provider.
DEVICE_PROVIDERS = {
    "cpu": CPU_PROVIDER,
    "cuda": "CUDAExecutionProvider",
    "rocm": "ROCMExecutionProvider",
    "migraphx": MIGRAPHX_PROVIDER,
    "directml": DML_PROVIDER,
    "dml": DML_PROVIDER,
    "openvino": OPENVINO_PROVIDER,
    "coreml": "CoreMLExecutionProvider",
}
AUTO_DEVICES = frozenset({"", "auto", "gpu"})


def resolve_providers(
    device: str = "auto", available: list[str] | None = None
) -> tuple[list[str], str | None]:
    """Map a configured device to the ONNX Runtime provider list to try.

    Returns the provider list and an optional warning for a request that cannot
    be honored. Accelerated providers always keep the CPU as a fallback so that
    graph nodes the device does not implement still run, and so a provider that
    is listed but cannot initialize (for example a missing CUDA library) does
    not make the build fail. "auto" tries every available accelerated provider
    in priority order before the CPU.
    """
    requested = str(device or "auto").strip().casefold()
    if available is None:
        available = list(ort.get_available_providers())
    available = list(available)

    if requested == "cpu":
        return [CPU_PROVIDER], None

    if requested in AUTO_DEVICES:
        providers = [name for name in AUTO_PROVIDERS if name in available]
        warning = None
        if not providers:
            if requested == "gpu":
                warning = (
                    "no accelerated ONNX Runtime provider is available; "
                    "embedding on CPU (install onnxruntime-gpu, onnxruntime-rocm, "
                    "onnxruntime-directml or onnxruntime-openvino, or use an "
                    "ONNX Runtime build with MIGraphX, for GPU support)"
                )
            return [CPU_PROVIDER], warning
        return _with_cpu_fallback(providers, available), warning

    provider = DEVICE_PROVIDERS.get(requested)
    if provider is None:
        supported = ", ".join(sorted({"auto", "gpu", *DEVICE_PROVIDERS}))
        return [CPU_PROVIDER], (
            f"unknown embedding device {device!r} (use {supported}); "
            "embedding on CPU"
        )
    if provider not in available:
        return [CPU_PROVIDER], (
            f"embedding device {device!r} is not available in this ONNX Runtime "
            f"build (providers: {', '.join(available)}); embedding on CPU. "
            "Install the ONNX Runtime build that includes this provider (for "
            "example onnxruntime-gpu for CUDA) to enable it"
        )
    return _with_cpu_fallback([provider], available), None


def _with_cpu_fallback(providers: list[str], available: list[str]) -> list[str]:
    if CPU_PROVIDER in available and CPU_PROVIDER not in providers:
        return [*providers, CPU_PROVIDER]
    return list(providers)


def provider_entries(
    providers: list[str],
    *,
    migraphx_cache: bool = True,
    migraphx_cache_key: str | None = None,
) -> list[str | tuple[str, dict[str, str]]]:
    """Add provider options where a bare provider name is not enough.

    OpenVINO's default target comes from the wheel's build-time configuration;
    passing only the provider name can silently keep running on the CPU. AUTO
    lets OpenVINO choose its best device (GPU, NPU or CPU) at runtime.
    """
    entries = []
    for name in providers:
        if name == OPENVINO_PROVIDER:
            entries.append((name, dict(OPENVINO_PROVIDER_OPTIONS)))
        elif (
            name == MIGRAPHX_PROVIDER
            and migraphx_cache
            and os.environ.get("ORT_MIGRAPHX_CACHE_DIR")
        ):
            cache_dir = Path(os.environ["ORT_MIGRAPHX_CACHE_DIR"]).expanduser()
            if migraphx_cache_key:
                cache_dir /= migraphx_cache_key
            cache_dir.mkdir(parents=True, exist_ok=True)
            entries.append(
                (
                    name,
                    {"migraphx_model_cache_dir": str(cache_dir)},
                )
            )
        else:
            entries.append(name)
    return entries


# Hosts with up to this much RAM get the embedding budget trimmed. ONNX
# Runtime's CPU arena keeps the largest batch's working set for the session's
# lifetime (measured ~700 MB at batch_size 32), which is risky on small devices
# but a fair price for build speed where RAM is plentiful.
LOW_MEMORY_GB = 2.0

# glibc's malloc_trim returns free heap pages to the OS. Probe once at import;
# other allocators (musl, BSD, macOS) simply never reclaim.
try:
    _libc = ctypes.CDLL("libc.so.6") if sys.platform.startswith("linux") else None
    _malloc_trim = getattr(_libc, "malloc_trim", None) if _libc is not None else None
except Exception:  # noqa: BLE001 - any load failure just disables reclaim
    _malloc_trim = None


def _reclaim_native_memory() -> None:
    """Give free heap pages back to the OS where the allocator allows it."""
    if _malloc_trim is not None:
        try:
            _malloc_trim(0)
        except Exception:  # noqa: BLE001 - reclaim is best-effort
            pass


def _low_memory_host() -> bool:
    """True for small hosts, where retained arena memory risks OOM."""
    total_gb = hardware.detect().ram_total / (1 << 30)
    return 0.0 < total_gb <= LOW_MEMORY_GB


# Checksums for the documented model files. Every documented precision and
# model is accepted, so downloading an alternative does not produce a checksum
# warning for a file this project downloaded itself.
EXPECTED_CHECKSUMS = DOCUMENTED_CHECKSUMS


def _model_config(model_dir) -> dict:
    """Read ``zimantic_model.json`` written beside a downloaded graph."""
    try:
        data = json.loads(
            (Path(model_dir) / MODEL_CONFIG_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _configured_tokenizer_class(model_dir) -> str:
    """Read the optional tokenizer_config.json from a Hugging Face export."""
    try:
        data = json.loads(
            (Path(model_dir) / "tokenizer_config.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("tokenizer_class", "")).casefold()


def _resolve_tokenizer(model_dir) -> tuple[str, Path]:
    """Choose SentencePiece or WordPiece from the files in ``model_dir``.

    The ONNX graph itself does not identify its tokenizer: both e5 families
    accept the same input_ids/attention_mask tensors. The vocabulary artifact
    does, reliably: multilingual-e5 ships ``sentencepiece.bpe.model`` and the
    English e5-v2 models ship BERT's ``vocab.txt``. When a directory contains
    both, honor tokenizer_config.json if it is present, otherwise keep the
    historical SentencePiece choice.
    """
    sentencepiece_path = Path(model_dir) / "sentencepiece.bpe.model"
    wordpiece_path = Path(model_dir) / "vocab.txt"
    if sentencepiece_path.is_file() and wordpiece_path.is_file():
        tokenizer_class = _configured_tokenizer_class(model_dir)
        if "xlm" in tokenizer_class or "sentencepiece" in tokenizer_class:
            return "sentencepiece", sentencepiece_path
        if tokenizer_class.startswith("bert") or "wordpiece" in tokenizer_class:
            return "wordpiece", wordpiece_path
        return "sentencepiece", sentencepiece_path
    if sentencepiece_path.is_file():
        return "sentencepiece", sentencepiece_path
    if wordpiece_path.is_file():
        return "wordpiece", wordpiece_path
    raise FileNotFoundError(
        f"{model_dir} must contain model.onnx and either "
        "sentencepiece.bpe.model (multilingual-e5) or vocab.txt (English e5-v2 "
        "and static WordPiece models; generate it from tokenizer.json)"
    )


_FILE_SHA256_CACHE: dict[tuple[str, int, int, int], str] = {}
_FILE_SHA256_LOCK = threading.Lock()
_FILE_SHA256_MIN_AGE_NS = 2_000_000_000


def _cached_sha256(path: Path) -> str:
    """Return the SHA-256 of ``path``, memoized by file stat signature."""
    resolved = path.resolve()
    stat = resolved.stat()
    # Rapid same-size writes can share an mtime on Windows/coarse filesystems.
    if time.time_ns() - stat.st_mtime_ns < _FILE_SHA256_MIN_AGE_NS:
        return _sha256(resolved)
    key = (str(resolved), stat.st_ino, stat.st_mtime_ns, stat.st_size)
    with _FILE_SHA256_LOCK:
        cached = _FILE_SHA256_CACHE.get(key)
    if cached is not None:
        return cached
    digest = _sha256(resolved)
    with _FILE_SHA256_LOCK:
        _FILE_SHA256_CACHE[key] = digest
    return digest


def verify_model_files(model_dir, warn=None) -> dict[str, str]:
    """Warn (never fail) when a model file differs from the documented checksum.

    Returns the checksums that were actually computed so callers can build a
    model fingerprint without hashing the same files twice.
    """
    if warn is None:
        def warn_to_stderr(message):
            print(message, file=sys.stderr)

        warn = warn_to_stderr
    actual_hashes: dict[str, str] = {}
    for name, expected in EXPECTED_CHECKSUMS.items():
        path = Path(model_dir) / name
        try:
            actual = _cached_sha256(path)
        except OSError:
            continue  # a missing file fails later, with a clearer error
        actual_hashes[name] = actual
        if isinstance(expected, str):
            matches = actual == expected
            documented = expected
        else:
            matches = actual in expected
            documented = ", ".join(sorted(expected))
        if not matches:
            warn(
                f"zimantic: warning: {name} does not match a documented checksum "
                f"(got {actual}, expected {documented}); search results may differ "
                "from the documented model"
            )
    for path in sorted(Path(model_dir).glob("*.onnx_data")):
        try:
            actual_hashes[path.name] = _cached_sha256(path)
        except OSError:
            continue
    return actual_hashes


def model_fingerprint(model_dir, hashes: dict[str, str] | None = None) -> str:
    """Return a stable fingerprint of the model graph and its tokenizer.

    Vectors are model-specific. The fingerprint is stored in each index so a
    later server can notice that the configured model changed instead of
    silently searching with vectors from a different encoder. An empty string
    means the files needed for a fingerprint are unavailable.
    """
    model_dir = Path(model_dir).expanduser()
    model_path = Path(model_dir) / "model.onnx"
    try:
        tokenizer_kind, tokenizer_path = _resolve_tokenizer(model_dir)
    except (OSError, FileNotFoundError):
        return ""
    if not model_path.is_file():
        return ""

    cached = hashes or {}
    digest = hashlib.sha256()
    fingerprint_files = [
        ("model", model_path),
        *(
            (f"model-data:{path.name}", path)
            for path in sorted(Path(model_dir).glob("*.onnx_data"))
        ),
        (tokenizer_kind, tokenizer_path),
    ]
    config_path = Path(model_dir) / MODEL_CONFIG_NAME
    if config_path.is_file():
        fingerprint_files.append(("config", config_path))
    tokenizer_json = Path(model_dir) / "tokenizer.json"
    if tokenizer_json.is_file():
        fingerprint_files.append(("tokenizer-json", tokenizer_json))
    for label, path in fingerprint_files:
        value = cached.get(path.name)
        if value is None:
            try:
                value = _cached_sha256(path)
            except OSError:
                return ""
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def embed_probe_texts(embedder, texts):
    """Embed probe texts as passages with the current model."""
    prefix = getattr(embedder, "passage_prefix", "passage: ")
    return np.asarray(
        embedder.embed([prefix + text for text in texts]),
        dtype=np.float32,
    )


def embed_model_probes(embedder, texts=MODEL_PROBE_TEXTS):
    """Return fixed probe texts and their passage embeddings.

    The probes let ``serve`` distinguish a different model from a different
    export of the same model (for example FP32 versus int8). The returned
    vectors are stored in the index; the texts are stored too, so a later
    version of this module can still re-embed an older index's probes.
    """
    probe_texts = list(texts)
    return probe_texts, embed_probe_texts(embedder, probe_texts)


def serialize_model_probes(texts, vectors) -> dict[str, str]:
    """Serialize probe texts and vectors into ``meta`` key/value pairs."""
    from .indexing.schema import encode_bytes, encode_json

    probe_texts = list(texts)
    array = np.asarray(vectors, dtype="<f4")
    if array.ndim != 2 or array.shape[0] != len(probe_texts):
        raise ValueError("model probes must be a 2-D array with one row per text")
    return {
        MODEL_PROBE_TEXTS_KEY: encode_json(probe_texts),
        MODEL_PROBE_VECTORS_KEY: encode_bytes(array.tobytes(order="C")),
        MODEL_PROBE_SHAPE_KEY: f"{array.shape[0]}x{array.shape[1]}",
    }


def load_model_probes(meta) -> tuple[list[str], np.ndarray] | None:
    """Decode probe metadata, returning ``None`` when it is absent or invalid."""
    from .indexing.schema import decode_bytes, decode_json

    texts_raw = meta.get(MODEL_PROBE_TEXTS_KEY)
    vectors_raw = meta.get(MODEL_PROBE_VECTORS_KEY)
    shape_raw = meta.get(MODEL_PROBE_SHAPE_KEY)
    if not texts_raw or not vectors_raw or not shape_raw:
        return None
    if not all(isinstance(value, str) for value in (texts_raw, vectors_raw, shape_raw)):
        return None
    try:
        texts = decode_json(texts_raw)
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            return None
        rows, dim = (int(part) for part in shape_raw.split("x", 1))
        if rows < 1 or dim < 1 or shape_raw != f"{rows}x{dim}":
            return None
        array = np.frombuffer(decode_bytes(vectors_raw), dtype="<f4")
        if rows != len(texts) or array.size != rows * dim:
            return None
        return texts, array.reshape(rows, dim).astype(np.float32, copy=False)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def model_probes_match(
    stored_texts,
    stored_vectors,
    embedder=None,
    *,
    current=None,
    mean_threshold: float = MODEL_PROBE_MEAN_COSINE,
    min_threshold: float = MODEL_PROBE_MIN_COSINE,
) -> tuple[bool, float, float]:
    """Compare stored probes with the current model's embeddings.

    Returns ``(compatible, mean_cosine, min_cosine)``. Pass ``current`` to
    reuse embeddings that were already computed for the same probe texts.
    Both sides are normalized here so the check also works with embedders or
    older vectors that are not unit length.
    """
    if current is None:
        if embedder is None:
            raise ValueError("an embedder or current probe vectors are required")
        current = embed_probe_texts(embedder, stored_texts)
    current = np.asarray(current, dtype=np.float32)
    stored = np.asarray(stored_vectors, dtype=np.float32)
    if current.shape != stored.shape or current.ndim != 2 or current.shape[0] == 0:
        return False, 0.0, 0.0

    def _normalize(values):
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        return values / np.where(norms == 0.0, 1.0, norms)

    scores = np.sum(_normalize(stored) * _normalize(current), axis=1)
    mean_cosine = float(scores.mean())
    min_cosine = float(scores.min())
    return (
        mean_cosine >= mean_threshold and min_cosine >= min_threshold,
        mean_cosine,
        min_cosine,
    )


class _FastWordPiece:
    """Hugging Face ``tokenizers`` adapter for a WordPiece vocabulary.

    The Rust tokenizer tokenizes batches tens of times faster than the
    dependency-free Python implementation. It is optional: when the package or
    ``tokenizer.json`` is unavailable, the embedder falls back to
    :class:`~zimantic.wordpiece.WordPieceTokenizer`.
    """

    def __init__(self, path: Path):
        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_file(str(path))
        self._tokenizer.no_padding()
        self._tokenizer.no_truncation()
        vocabulary = self._tokenizer.get_vocab()
        self.cls_id = vocabulary.get("[CLS]", 101)
        self.sep_id = vocabulary.get("[SEP]", 102)
        self.pad_id = vocabulary.get("[PAD]", 0)
        self.unk_id = vocabulary.get("[UNK]", 100)

    def encode(self, text: str) -> list[int]:
        return self._tokenizer.encode(text, add_special_tokens=False).ids

    def encode_batch(self, texts) -> list[list[int]]:
        return [
            encoding.ids
            for encoding in self._tokenizer.encode_batch(
                list(texts), add_special_tokens=False
            )
        ]


class Embedder:
    def __init__(
        self,
        model_dir,
        embedding_tokens: int = DEFAULT_EMBEDDING_TOKENS,
        threads: int | None = None,
        low_memory: bool | None = None,
        device: str = "auto",
        batch_tokens: int | None = None,
    ):
        """model_dir holds model.onnx and one tokenizer vocabulary.

        ``sentencepiece.bpe.model`` selects the multilingual XLM-R tokenizer;
        ``vocab.txt`` selects a BERT WordPiece tokenizer (English e5-v2 and the
        static default). A 2-D graph output is used as a pooled sentence
        embedding; a 3-D output is mean-pooled. ``zimantic_model.json`` may set
        Matryoshka ``truncate_dim`` and empty prompt prefixes. low_memory
        disables ONNX Runtime's CPU memory arena and hands freed heap back to
        the OS after batched embeddings. By default (None) it is enabled
        automatically on hosts with LOW_MEMORY_GB or less of RAM. device is
        "auto" (try every accelerated provider the installed onnxruntime
        offers), "cpu", "gpu", or a provider alias such as "cuda", "rocm",
        "migraphx", "directml" or "openvino"; an unavailable device falls back
        to the CPU with a warning, as does a run that fails after initialization.
        """
        self.embedding_tokens = int(embedding_tokens)
        if self.embedding_tokens < SPECIAL_TOKEN_COUNT:
            raise ValueError(f"embedding_tokens must be at least {SPECIAL_TOKEN_COUNT}")
        model_dir = Path(model_dir).expanduser()
        self.model_dir = model_dir
        hashes = verify_model_files(model_dir)
        self.threads = (
            int(threads) if threads and int(threads) > 0 else default_onnx_query_threads()
        )
        self.low_memory = _low_memory_host() if low_memory is None else low_memory
        self.providers, device_warning = resolve_providers(device)
        if device_warning:
            logger.warning("zimantic: %s", device_warning)
        self.provider = self.providers[0]
        self.fingerprint = model_fingerprint(model_dir, hashes)
        config = _model_config(model_dir)
        # e5 models expect prompt prefixes; static models are trained on raw
        # text, and a different prefix on queries and documents would shift the
        # two sides of the similarity apart.
        self.query_prefix = str(config.get("query_prefix", "query: "))
        self.passage_prefix = str(config.get("passage_prefix", "passage: "))
        truncate_dim = config.get("truncate_dim")
        self.truncate_dim = int(truncate_dim) if truncate_dim else None
        self._build_batch_tokens = None
        self._migraphx_sessions = OrderedDict()
        if self.provider == MIGRAPHX_PROVIDER and batch_tokens is not None:
            if batch_tokens < 1:
                raise ValueError("batch_tokens must be at least 1")
            self._build_batch_tokens = int(batch_tokens)
        logger.info("zimantic: embedding with %s", self.provider)
        self._model_path = str(Path(model_dir) / "model.onnx")
        self._session_options = self._new_session_options()
        self._fallback_lock = threading.Lock()
        initial_shape = self.batch_shape(self.embedding_tokens)
        self.session = self._create_session(
            self._model_path, self._new_session_options(), shape=initial_shape
        )
        if initial_shape is not None and self.provider == MIGRAPHX_PROVIDER:
            self._migraphx_sessions[initial_shape] = self.session
        # DirectML rejects concurrent Run calls. MIGraphX can segfault if
        # concurrent calls trigger compilation on the first input shape.
        self._run_lock = (
            threading.Lock()
            if self.provider in {DML_PROVIDER, MIGRAPHX_PROVIDER}
            else None
        )
        # Exported graphs differ: some XLM-R models (for example
        # multilingual-e5-base) omit token_type_ids, while multilingual-e5-small
        # requires it. Feed only the inputs the graph declares.
        self._input_names = {item.name for item in self.session.get_inputs()}
        # A 2-D output is already a pooled sentence embedding (an EmbeddingBag
        # export); a 3-D output is a token sequence that needs mean pooling.
        try:
            outputs = self.session.get_outputs()
        except AttributeError:
            # Test doubles and older wrappers expose inputs only; keep the
            # transformer mean-pooling path for them.
            outputs = []
        self.pooled_output = bool(outputs) and len(outputs[0].shape) == 2
        if self.pooled_output and self.truncate_dim is not None:
            output_dim = outputs[0].shape[1]
            if isinstance(output_dim, int) and self.truncate_dim > output_dim:
                raise ValueError(
                    f"truncate_dim {self.truncate_dim} exceeds the model's "
                    f"{output_dim} output dimensions"
                )
        self.tokenizer_kind, tokenizer_path = _resolve_tokenizer(model_dir)
        self._fast_tokenizer = False
        if self.tokenizer_kind == "sentencepiece":
            # sentencepiece loads this vocabulary in ~45 MB.
            self.tokenizer = sentencepiece.SentencePieceProcessor(
                model_file=str(tokenizer_path)
            )
            # XLM-RoBERTa reserves 0 for <s>, 1 for <pad> and 2 for </s>.
            self.pad_id = 1
        else:
            fast_path = Path(model_dir) / "tokenizer.json"
            if fast_path.is_file():
                try:
                    self.tokenizer = _FastWordPiece(fast_path)
                except ImportError:
                    logger.debug(
                        "zimantic: tokenizers is not installed; using the Python "
                        "WordPiece tokenizer"
                    )
                except Exception as error:  # noqa: BLE001 - any failure falls back
                    logger.warning(
                        "zimantic: could not use %s (%s); falling back to the "
                        "Python WordPiece tokenizer",
                        fast_path,
                        error,
                    )
                else:
                    self._fast_tokenizer = True
            if not self._fast_tokenizer:
                self.tokenizer = WordPieceTokenizer(tokenizer_path)
            # BERT uses [PAD] = 0, unlike XLM-R. Do not hard-code 1 here: it is
            # [unused0] in a standard BERT vocabulary.
            self.pad_id = self.tokenizer.pad_id
        self._tokenizer_lock = threading.Lock()

    def _new_session_options(self):
        opts = ort.SessionOptions()  # ty: ignore[possibly-missing-attribute]
        opts.intra_op_num_threads = self.threads
        opts.inter_op_num_threads = 1
        # The CPU arena holds the largest batch's working set for the lifetime
        # of the session and never releases it. Fast for builds on machines with
        # RAM to spare, but on small hosts a batch_size=32 build can retain
        # hundreds of MB; disable it there and malloc per call instead.
        opts.enable_cpu_mem_arena = not self.low_memory
        if DML_PROVIDER in self.providers:
            # DirectML rejects ONNX Runtime's memory-pattern optimisation and
            # parallel execution; without this the session fails to initialize
            # and every DML request silently falls back to the CPU.
            opts.enable_mem_pattern = False
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        # Prevent idle ORT threads from busy-waiting between batches.
        try:
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        except RuntimeError:
            pass
        return opts

    def batch_shape(self, width: int) -> tuple[int, int] | None:
        if self.provider != MIGRAPHX_PROVIDER or self._build_batch_tokens is None:
            return None
        width = min(
            max(32, 1 << (width - 1).bit_length()),
            1 << (self.embedding_tokens - 1).bit_length(),
        )
        return max(1, self._build_batch_tokens // width), width

    def _create_session(self, model_path: str, opts, *, shape=None):
        """Create the ONNX session, falling back to CPU if a GPU cannot start."""
        cache_key = None
        if shape is not None:
            batch_size, width = shape
            opts.add_free_dimension_override_by_name("batch_size", batch_size)
            opts.add_free_dimension_override_by_name("sequence_length", width)
            if self.fingerprint:
                cache_key = (
                    f"{self.fingerprint}/{ort.__version__}/"
                    f"{batch_size}x{width}"
                )
        # Dynamic first-run shapes share a cache key in some MIGraphX versions.
        # Only cache a session whose input geometry is constant across runs.
        try:
            providers = provider_entries(
                self.providers,
                migraphx_cache=cache_key is not None,
                migraphx_cache_key=cache_key,
            )
            session = ort.InferenceSession(model_path, opts, providers=providers)
        except Exception as error:
            if self.provider == CPU_PROVIDER:
                raise
            logger.warning(
                "zimantic: could not create a session with %s (%s); "
                "retrying on CPU",
                self.provider,
                error,
            )
            self.providers = [CPU_PROVIDER]
            self.provider = CPU_PROVIDER
            session = ort.InferenceSession(
                model_path, self._session_options, providers=[CPU_PROVIDER]
            )
        actual_providers = list(session.get_providers())
        if actual_providers:
            if actual_providers[0] != self.provider:
                logger.warning(
                    "zimantic: ONNX Runtime selected %s instead of %s; "
                    "embedding on %s",
                    actual_providers[0],
                    self.provider,
                    actual_providers[0],
                )
                if shape is not None and actual_providers[0] == CPU_PROVIDER:
                    session = ort.InferenceSession(
                        model_path, self._session_options, providers=[CPU_PROVIDER]
                    )
            self.providers = actual_providers
            self.provider = actual_providers[0]
        return session

    def _fall_back_to_cpu(self, error: Exception) -> bool:
        """Rebuild the session on CPU after a device run fails.

        Initialization failures are handled in ``_create_session``. A device can
        also fail later, most commonly when a batch exhausts GPU memory; retrying
        on the CPU turns that into a slow but successful build instead of a
        failed one. The lock keeps concurrent workers from rebuilding repeatedly.
        """
        with self._fallback_lock:
            if self.provider == CPU_PROVIDER:
                return True
            logger.warning(
                "zimantic: embedding failed on %s (%s); retrying on CPU",
                self.provider,
                error,
            )
            try:
                session = ort.InferenceSession(
                    self._model_path,
                    self._session_options,
                    providers=[CPU_PROVIDER],
                )
            except Exception as cpu_error:
                logger.error(
                    "zimantic: CPU fallback also failed (%s)", cpu_error
                )
                return False
            self.session = session
            self.providers = [CPU_PROVIDER]
            self.provider = CPU_PROVIDER
            self._migraphx_sessions.clear()
            self._run_lock = None
            return True

    def _run_once(self, inputs):
        lock = self._run_lock
        if lock is None:
            return self.session.run(None, inputs)[0]
        with lock:
            # Read the session inside the lock: a fallback may replace it while
            # another run is waiting.
            if self.provider == MIGRAPHX_PROVIDER and self._build_batch_tokens is not None:
                shape = inputs["input_ids"].shape
                session = self._migraphx_sessions.pop(shape, None)
                if session is None:
                    session = self._create_session(
                        self._model_path, self._new_session_options(), shape=shape
                    )
                self.session = session
                if self.provider == MIGRAPHX_PROVIDER:
                    self._migraphx_sessions[shape] = session
                    capacity = 1 if self.low_memory else MIGRAPHX_SESSION_CACHE_SIZE
                    while len(self._migraphx_sessions) > capacity:
                        self._migraphx_sessions.popitem(last=False)
                else:
                    self._migraphx_sessions.clear()
                    self._run_lock = None
            return self.session.run(None, inputs)[0]

    def token_count(self, text: str, prefix: str = "") -> int:
        """Count special tokens and tokenizer pieces for the full input."""
        with self._tokenizer_lock:
            pieces = self.tokenizer.encode(prefix + text)
        return SPECIAL_TOKEN_COUNT + len(pieces)

    def truncate(self, text: str, prefix: str = "") -> str:
        """Keep text within the model budget without cutting through a word."""
        if self.token_count(text, prefix) <= self.embedding_tokens:
            return text
        words = text.split()
        low, high = 0, len(words)
        while low < high:
            midpoint = (low + high + 1) // 2
            candidate = " ".join(words[:midpoint])
            if self.token_count(candidate, prefix) <= self.embedding_tokens:
                low = midpoint
            else:
                high = midpoint - 1
        result = " ".join(words[:low])
        if result:
            return result
        # No whole word fits (or the text has no spaces, as with CJK). Keep
        # the longest run of characters that still fits instead of embedding
        # an empty excerpt.
        low, high = 0, len(text)
        while low < high:
            midpoint = (low + high + 1) // 2
            if self.token_count(text[:midpoint], prefix) <= self.embedding_tokens:
                low = midpoint
            else:
                high = midpoint - 1
        return text[:low]

    def _ids(self, text: str) -> list[int]:
        with self._tokenizer_lock:
            pieces = self.tokenizer.encode(text)[
                : self.embedding_tokens - SPECIAL_TOKEN_COUNT
            ]
        if self.tokenizer_kind == "wordpiece":
            # BERT numbering comes straight from vocab.txt ([CLS]=101, [SEP]=102).
            return [self.tokenizer.cls_id] + pieces + [self.tokenizer.sep_id]
        # XLM-RoBERTa numbering: <s>=0 <pad>=1 </s>=2 <unk>=3, other pieces are sentencepiece id + 1.
        return [0] + [p + 1 if p else 3 for p in pieces] + [2]

    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed texts as-is; callers add the model's query/passage prefix."""
        return self.embed_encoded(self.encode(texts))

    def encode(self, texts: list[str]) -> list[list[int]]:
        """Prepare capped model inputs once, before scheduling build batches."""
        if self._fast_tokenizer:
            limit = self.embedding_tokens - SPECIAL_TOKEN_COUNT
            return [
                [self.tokenizer.cls_id, *row[:limit], self.tokenizer.sep_id]
                for row in self.tokenizer.encode_batch(texts)
            ]
        return [self._ids(text) for text in texts]

    def embed_encoded(self, encoded: list[list[int]]) -> np.ndarray:
        """Embed already-tokenized inputs, including their special tokens."""
        if not encoded:
            # No current caller batches zero texts, but refuse to crash on
            # ``max()`` over an empty list; the shape is unknown until the
            # first real batch, so return a degenerate row-less array.
            return np.empty((0, 0), dtype=np.float32)
        item_count = len(encoded)
        width = max(map(len, encoded))
        shape = self.batch_shape(width)
        capacity = shape[0] if shape is not None else (
            max(1, self._build_batch_tokens // width)
            if self._build_batch_tokens is not None else item_count
        )
        if item_count > capacity:
            return np.concatenate([
                self.embed_encoded(encoded[start:start + capacity])
                for start in range(0, len(encoded), capacity)
            ])
        batch_size = item_count
        if shape is not None:
            if width > self.embedding_tokens:
                raise ValueError("encoded input exceeds the fixed embedding width")
            batch_size, width = shape
        elif self.provider == MIGRAPHX_PROVIDER:
            # MIGraphX compiles programs per input shape. Power-of-two buckets
            # bound recompiles as token lengths and build batch sizes vary.
            width = 1 << (width - 1).bit_length()
            batch_size = 1 << (item_count - 1).bit_length()
        rows = [e + [self.pad_id] * (width - len(e)) for e in encoded]
        rows.extend([rows[0]] * (batch_size - item_count))
        ids = np.array(rows, dtype=np.int64)
        mask = (ids != self.pad_id).astype(np.int64)
        inputs = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            inputs["token_type_ids"] = np.zeros_like(ids)
        provider = self.provider
        try:
            hidden = self._run_once(inputs)
            if provider == MIGRAPHX_PROVIDER and hidden.shape[:2] != ids.shape:
                raise RuntimeError(
                    f"MIGraphX returned shape {hidden.shape[:2]} for input {ids.shape}"
                )
        except Exception as error:
            if provider == CPU_PROVIDER or not self._fall_back_to_cpu(error):
                raise
            hidden = self._run_once(inputs)
        hidden = hidden[:item_count]
        mask = mask[:item_count]
        if self.pooled_output:
            # The graph already pooled the tokens (including special tokens and
            # padding via attention_mask); there is no sequence axis to average.
            vectors = hidden
        else:
            weights = mask[..., None].astype(np.float32)
            vectors = (hidden * weights).sum(1) / weights.sum(1)
        if self.truncate_dim is not None:
            # Matryoshka embeddings keep their leading dimensions. Renormalize
            # after truncation, otherwise the reduced vectors are not unit length.
            vectors = vectors[:, : self.truncate_dim]
        result = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        if self.low_memory and len(encoded) > 1:
            # Batched builds churn varied-shape workspaces and leave free heap
            # behind; return it to the OS between batches so RSS stays near the
            # model size. Single-query server embeddings skip this.
            _reclaim_native_memory()
        return result
