"""Turn text into vectors with multilingual-e5-small."""
import hashlib
import os
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
import sentencepiece

from .settings import DEFAULT_MAX_EMBEDDING_TOKENS
SPECIAL_TOKEN_COUNT = 2

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
    ):
        """model_dir holds model.onnx and sentencepiece.bpe.model."""
        self.max_tokens = int(max_tokens)
        if self.max_tokens < SPECIAL_TOKEN_COUNT:
            raise ValueError(f"max_tokens must be at least {SPECIAL_TOKEN_COUNT}")
        verify_model_files(model_dir)
        self.threads = int(threads) if threads and int(threads) > 0 else DEFAULT_EMBED_THREADS
        opts = ort.SessionOptions()
        opts.enable_cpu_mem_arena = False
        opts.intra_op_num_threads = self.threads
        opts.inter_op_num_threads = 1
        # Prevent idle ORT threads from busy-waiting between batches.
        try:
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        except Exception:
            pass
        self.session = ort.InferenceSession(str(Path(model_dir) / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        # sentencepiece loads this vocabulary in ~45 MB; Hugging Face `tokenizers` needed ~250 MB.
        self.tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(Path(model_dir) / "sentencepiece.bpe.model"))

    def token_count(self, text: str, prefix: str = "") -> int:
        """Count special tokens and SentencePiece pieces for the full input."""
        return SPECIAL_TOKEN_COUNT + len(self.tokenizer.encode(prefix + text))

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
        pieces = self.tokenizer.encode(text)[:self.max_tokens - SPECIAL_TOKEN_COUNT]
        return [0] + [p + 1 if p else 3 for p in pieces] + [2]

    def embed(self, texts: list[str]) -> np.ndarray:
        """e5 expects each text to start with "query: " or "passage: "."""
        encoded = [self._ids(t) for t in texts]
        width = max(map(len, encoded))
        ids = np.array([e + [1] * (width - len(e)) for e in encoded], dtype=np.int64)
        mask = (ids != 1).astype(np.int64)
        hidden = self.session.run(None, {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)})[0]
        weights = mask[..., None].astype(np.float32)
        vectors = (hidden * weights).sum(1) / weights.sum(1)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
