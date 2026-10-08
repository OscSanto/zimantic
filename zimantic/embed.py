"""Turn text into vectors with multilingual-e5-small (ONNX, int8). This is the one model zimantic ships with."""
from pathlib import Path

import numpy as np
import onnxruntime as ort
import sentencepiece

from .settings import DEFAULT_MAX_EMBEDDING_TOKENS
SPECIAL_TOKEN_COUNT = 2

# Turns text into vectors with multilingual-e5-small (ONNX, int8) currently.
class Embedder:
    def __init__(
        self,
        model_dir,
        max_tokens: int = DEFAULT_MAX_EMBEDDING_TOKENS,
    ):
        """model_dir holds model.onnx and sentencepiece.bpe.model."""
        self.max_tokens = int(max_tokens)
        if self.max_tokens < SPECIAL_TOKEN_COUNT:
            raise ValueError(f"max_tokens must be at least {SPECIAL_TOKEN_COUNT}")
        opts = ort.SessionOptions()
        opts.enable_cpu_mem_arena = False  # the arena kept ~600 MB after indexing; without it memory is freed
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
        vectors = (hidden * weights).sum(1) / weights.sum(1)  # mean over real tokens: the pooling e5 was trained with
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
