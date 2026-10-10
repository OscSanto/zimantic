"""Turn text into vectors with multilingual-e5-small."""

import ctypes
import hashlib
import os
import sys
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
import sentencepiece

from .settings import DEFAULT_MAX_EMBEDDING_TOKENS, _system_memory_gb

SPECIAL_TOKEN_COUNT = 2

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
    total_gb = _system_memory_gb()
    return 0.0 < total_gb <= LOW_MEMORY_GB


# Checksums for the model files documented in the README.
EXPECTED_CHECKSUMS = {
    "model.onnx": "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193",
    "sentencepiece.bpe.model": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
}

# Leave a core for search and the web server.
DEFAULT_EMBED_THREADS = max(1, (os.cpu_count() or 1) - 1)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model_files(model_dir, warn=None) -> None:
    """Warn (never fail) when a model file differs from the documented checksum."""
    if warn is None:
        warn = lambda message: print(message, file=sys.stderr)  # noqa: E731
    for name, expected in EXPECTED_CHECKSUMS.items():
        path = Path(model_dir) / name
        try:
            actual = _sha256(path)
        except OSError:
            continue  # a missing file fails later, with a clearer error
        if actual != expected:
            warn(
                f"zimantic: warning: {name} does not match the expected checksum "
                f"(got {actual}, expected {expected}); search results may differ "
                "from the documented model"
            )


class Embedder:
    def __init__(
        self,
        model_dir,
        max_tokens: int = DEFAULT_MAX_EMBEDDING_TOKENS,
        threads: int | None = None,
        low_memory: bool | None = None,
    ):
        """model_dir holds model.onnx and sentencepiece.bpe.model.

        low_memory disables ONNX Runtime's CPU memory arena and hands freed heap
        back to the OS after batched embeddings. By default (None) it is enabled
        automatically on hosts with LOW_MEMORY_GB or less of RAM.
        """
        self.max_tokens = int(max_tokens)
        if self.max_tokens < SPECIAL_TOKEN_COUNT:
            raise ValueError(f"max_tokens must be at least {SPECIAL_TOKEN_COUNT}")
        verify_model_files(model_dir)
        self.threads = (
            int(threads) if threads and int(threads) > 0 else DEFAULT_EMBED_THREADS
        )
        self.low_memory = _low_memory_host() if low_memory is None else low_memory
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.threads
        opts.inter_op_num_threads = 1
        # The CPU arena holds the largest batch's working set for the lifetime
        # of the session and never releases it. Fast for builds on machines with
        # RAM to spare, but on small hosts a batch_size=32 build can retain
        # hundreds of MB; disable it there and malloc per call instead.
        opts.enable_cpu_mem_arena = not self.low_memory
        # Prevent idle ORT threads from busy-waiting between batches.
        try:
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        except Exception:
            pass
        self.session = ort.InferenceSession(
            str(Path(model_dir) / "model.onnx"),
            opts,
            providers=["CPUExecutionProvider"],
        )
        # sentencepiece loads this vocabulary in ~45 MB; Hugging Face `tokenizers` needed ~250 MB.
        self.tokenizer = sentencepiece.SentencePieceProcessor(
            model_file=str(Path(model_dir) / "sentencepiece.bpe.model")
        )
        # The SentencePiece processor is not safe for concurrent encode calls from
        # several search threads; serialize access to it. ONNX inference itself is
        # thread-safe and needs no lock.
        self._tokenizer_lock = threading.Lock()

    def token_count(self, text: str, prefix: str = "") -> int:
        """Count special tokens and SentencePiece pieces for the full input."""
        with self._tokenizer_lock:
            pieces = self.tokenizer.encode(prefix + text)
        return SPECIAL_TOKEN_COUNT + len(pieces)

    def truncate(self, text: str, prefix: str = "") -> str:
        """Keep text within the model budget without cutting through a word."""
        if self.token_count(text, prefix) <= self.max_tokens:
            return text
        words = text.split()
        low, high = 0, len(words)
        while low < high:
            midpoint = (low + high + 1) // 2
            candidate = " ".join(words[:midpoint])
            if self.token_count(candidate, prefix) <= self.max_tokens:
                low = midpoint
            else:
                high = midpoint - 1
        return " ".join(words[:low])

    def _ids(self, text: str) -> list[int]:
        # XLM-RoBERTa numbering: <s>=0 <pad>=1 </s>=2 <unk>=3, other pieces are sentencepiece id + 1.
        with self._tokenizer_lock:
            pieces = self.tokenizer.encode(text)[
                : self.max_tokens - SPECIAL_TOKEN_COUNT
            ]
        return [0] + [p + 1 if p else 3 for p in pieces] + [2]

    def embed(self, texts: list[str]) -> np.ndarray:
        """e5 expects each text to start with "query: " or "passage: "."""
        encoded = [self._ids(t) for t in texts]
        width = max(map(len, encoded))
        ids = np.array([e + [1] * (width - len(e)) for e in encoded], dtype=np.int64)
        mask = (ids != 1).astype(np.int64)
        hidden = self.session.run(
            None,
            {
                "input_ids": ids,
                "attention_mask": mask,
                "token_type_ids": np.zeros_like(ids),
            },
        )[0]
        weights = mask[..., None].astype(np.float32)
        vectors = (hidden * weights).sum(1) / weights.sum(1)
        result = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        if self.low_memory and len(texts) > 1:
            # Batched builds churn varied-shape workspaces and leave free heap
            # behind; return it to the OS between batches so RSS stays near the
            # model size. Single-query server embeddings skip this.
            _reclaim_native_memory()
        return result
