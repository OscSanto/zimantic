"""A small BERT WordPiece tokenizer for English-only e5 ONNX models.

The multilingual-e5 models use a SentencePiece vocabulary, while the English
e5-v2 models use BERT's WordPiece vocabulary (``vocab.txt``). This module keeps
that tokenizer dependency-free: ``sentencepiece`` cannot read a BERT vocabulary
and dragging in the Hugging Face Rust tokenizer costs far more memory than the
few hundred lines needed here.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path


def _is_whitespace(char: str) -> bool:
    return char in " \t\n\r" or unicodedata.category(char) == "Zs"


def _is_control(char: str) -> bool:
    if char in "\t\n\r":
        return False
    return unicodedata.category(char) in {"Cc", "Cf"}


def _is_punctuation(char: str) -> bool:
    codepoint = ord(char)
    if (
        33 <= codepoint <= 47
        or 58 <= codepoint <= 64
        or 91 <= codepoint <= 96
        or 123 <= codepoint <= 126
    ):
        return True
    return unicodedata.category(char).startswith("P")


def _is_chinese_char(codepoint: int) -> bool:
    return (
        0x4E00 <= codepoint <= 0x9FFF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x2F800 <= codepoint <= 0x2FA1F
    )


def _clean_text(text: str) -> str:
    output = []
    for char in text:
        codepoint = ord(char)
        if codepoint == 0 or codepoint == 0xFFFD or _is_control(char):
            continue
        output.append(" " if _is_whitespace(char) else char)
    return "".join(output)


def _tokenize_chinese_chars(text: str) -> str:
    output = []
    for char in text:
        if _is_chinese_char(ord(char)):
            output.extend((" ", char, " "))
        else:
            output.append(char)
    return "".join(output)


def _strip_accents(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )


def _split_on_punctuation(text: str) -> list[str]:
    output: list[list[str]] = []
    start_new_word = True
    for char in text:
        if _is_punctuation(char):
            output.append([char])
            start_new_word = True
        else:
            if start_new_word:
                output.append([])
            start_new_word = False
            output[-1].append(char)
    return ["".join(chunk) for chunk in output]


class WordPieceTokenizer:
    """Match the ``BertTokenizer`` used by intfloat/e5-small-v2.

    ``encode`` returns vocabulary ids without the ``[CLS]``/``[SEP]`` wrapper;
    callers add those so token counts and the embedding budget include them.
    """

    def __init__(
        self,
        vocab_file: str | Path,
        lower_case: bool = True,
        max_input_chars_per_word: int = 100,
    ):
        self.vocab = {}
        with open(vocab_file, encoding="utf-8") as handle:
            for index, token in enumerate(handle.read().splitlines()):
                # The first occurrence wins, as in BERT's read_vocab.
                self.vocab.setdefault(token, index)
        self.lower_case = bool(lower_case)
        self.strip_accents = self.lower_case
        self.max_input_chars_per_word = int(max_input_chars_per_word)
        self.unk_id = self.vocab.get("[UNK]", 100)
        self.cls_id = self.vocab.get("[CLS]", 101)
        self.sep_id = self.vocab.get("[SEP]", 102)
        self.pad_id = self.vocab.get("[PAD]", 0)

    def _basic_tokens(self, text: str) -> list[str]:
        text = _tokenize_chinese_chars(_clean_text(text))
        output = []
        for token in text.strip().split():
            if self.lower_case:
                token = token.lower()
            if self.strip_accents:
                token = _strip_accents(token)
            output.extend(_split_on_punctuation(token))
        return output

    def _wordpiece_tokens(self, token: str) -> list[str]:
        chars = list(token)
        if len(chars) > self.max_input_chars_per_word:
            return ["[UNK]"]
        output = []
        start = 0
        while start < len(chars):
            end = len(chars)
            piece = None
            while start < end:
                candidate = "".join(chars[start:end])
                if start:
                    candidate = "##" + candidate
                if candidate in self.vocab:
                    piece = candidate
                    break
                end -= 1
            if piece is None:
                return ["[UNK]"]
            output.append(piece)
            start = end
        return output

    def encode(self, text: str) -> list[int]:
        pieces = []
        for token in self._basic_tokens(text):
            pieces.extend(self._wordpiece_tokens(token))
        return [self.vocab.get(piece, self.unk_id) for piece in pieces]

    def token_count(self, text: str) -> int:
        return len(self.encode(text))
