"""Turn text into vectors with multilingual-e5-small (ONNX, int8). This is the one model zimantic ships with."""
from pathlib import Path

import numpy as np
import onnxruntime as ort
import sentencepiece

MAX_TOKENS = 256

# Turns text into vectors with multilingual-e5-small (ONNX, int8) currently.
class Embedder:
    def __init__(self, model_dir):
        """model_dir holds model.onnx and sentencepiece.bpe.model."""
        opts = ort.SessionOptions()
        opts.enable_cpu_mem_arena = False  # the arena kept ~600 MB after indexing; without it memory is freed
        self.session = ort.InferenceSession(str(Path(model_dir) / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        # sentencepiece loads this vocabulary in ~45 MB; Hugging Face `tokenizers` needed ~250 MB.
        self.tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(Path(model_dir) / "sentencepiece.bpe.model"))

    def _ids(self, text: str) -> list[int]:
        # XLM-RoBERTa numbering: <s>=0 <pad>=1 </s>=2 <unk>=3, other pieces are sentencepiece id + 1.
        pieces = self.tokenizer.encode(text)[:MAX_TOKENS - 2]
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
