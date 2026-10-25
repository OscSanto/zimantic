import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from zimantic import embed
from zimantic.wordpiece import WordPieceTokenizer


VOCAB = [
    "[PAD]",
    "[UNK]",
    "[CLS]",
    "[SEP]",
    "[MASK]",
    "query",
    ":",
    "heart",
    "attack",
    "passage",
    "the",
    "pumps",
    "blood",
    ".",
    "dia",
    "##bet",
    "##is",
    "cafe",
    "naive",
    "co",
    "##vid",
    "-",
    "19",
    "don",
    "'",
    "t",
    "hello",
    "world",
    "世",
]


def _write_vocab(directory):
    path = Path(directory) / "vocab.txt"
    path.write_text("\n".join(VOCAB) + "\n", encoding="utf-8")
    return path


def _ids(*tokens):
    return [VOCAB.index(token) for token in tokens]


class WordPieceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tokenizer = WordPieceTokenizer(_write_vocab(self.tmp.name))

    def test_lowercases_and_splits_punctuation(self):
        self.assertEqual(
            self.tokenizer.encode("Query: Heart Attack"),
            _ids("query", ":", "heart", "attack"),
        )

    def test_strips_accents(self):
        self.assertEqual(
            self.tokenizer.encode("café naïve"),
            _ids("cafe", "naive"),
        )

    def test_uses_continuation_pieces(self):
        self.assertEqual(
            self.tokenizer.encode("diabetis"),
            _ids("dia", "##bet", "##is"),
        )

    def test_unknown_words_become_unk(self):
        self.assertEqual(
            self.tokenizer.encode("zzz"),
            _ids("[UNK]"),
        )

    def test_mixed_punctuation_and_numbers(self):
        self.assertEqual(
            self.tokenizer.encode("COVID-19"),
            _ids("co", "##vid", "-", "19"),
        )

    def test_chinese_characters_are_separate_words(self):
        self.assertEqual(
            self.tokenizer.encode("你好世界"),
            _ids("[UNK]", "[UNK]", "世", "[UNK]"),
        )

    def test_overlong_word_becomes_unk(self):
        self.assertEqual(self.tokenizer.encode("a" * 101), _ids("[UNK]"))

    def test_empty_text_has_no_tokens(self):
        self.assertEqual(self.tokenizer.encode("   \n"), [])

    def test_standard_special_token_ids(self):
        self.assertEqual(self.tokenizer.cls_id, VOCAB.index("[CLS]"))
        self.assertEqual(self.tokenizer.sep_id, VOCAB.index("[SEP]"))
        self.assertEqual(self.tokenizer.unk_id, VOCAB.index("[UNK]"))
        self.assertEqual(self.tokenizer.pad_id, VOCAB.index("[PAD]"))


class WordPieceEmbedderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _write_vocab(self.tmp.name)
        self.session = MagicMock()
        self.session.get_inputs.return_value = []
        self.session.run.return_value = [
            np.ones((1, 4, 4), dtype=np.float32)
        ]

    def _embedder(self, **kwargs):
        with patch.object(embed.ort, "InferenceSession", return_value=self.session):
            return embed.Embedder(self.tmp.name, **kwargs)

    def test_wordpiece_artifact_selects_wordpiece_mode(self):
        self.assertEqual(self._embedder().tokenizer_kind, "wordpiece")

    def test_ids_include_bert_special_tokens(self):
        embedder = self._embedder()
        self.assertEqual(
            embedder._ids("query: heart attack"),
            _ids("[CLS]", "query", ":", "heart", "attack", "[SEP]"),
        )

    def test_token_count_includes_special_tokens(self):
        embedder = self._embedder()
        self.assertEqual(embedder.token_count("query: heart attack"), 6)

    def test_truncate_respects_budget(self):
        embedder = self._embedder(embedding_tokens=5)
        truncated = embedder.truncate("heart attack pumps blood", prefix="query: ")
        self.assertEqual(" ".join(truncated.split()), "heart")
        self.assertLessEqual(embedder.token_count(truncated, prefix="query: "), 5)

    def test_truncate_falls_back_to_characters_without_spaces(self):
        embedder = self._embedder(embedding_tokens=6)
        truncated = embedder.truncate("你" * 100, prefix="query: ")
        self.assertTrue(truncated)
        self.assertLessEqual(embedder.token_count(truncated, prefix="query: "), 6)

    def test_padding_uses_wordpiece_pad_id(self):
        self.session.run.return_value = [
            np.ones((2, 4, 4), dtype=np.float32)
        ]
        embedder = self._embedder()
        embedder.embed(["hello", "hello world"])
        inputs = self.session.run.call_args.args[1]
        # BERT's [PAD] is 0. The shorter row must not be padded with XLM-R's 1.
        self.assertEqual(inputs["input_ids"][0][-1], 0)
        self.assertEqual(inputs["attention_mask"][0][-1], 0)

    def test_unknown_token_is_not_masked_as_padding(self):
        self.session.run.return_value = [
            np.ones((2, 3, 3), dtype=np.float32)
        ]
        embedder = self._embedder()
        # In this vocabulary [UNK] is id 1, which the old hardcoded XLM-R mask
        # would have treated as padding.
        embedder.embed(["hello", "zzz"])
        inputs = self.session.run.call_args.args[1]
        self.assertEqual(inputs["input_ids"][1][1], 1)
        self.assertEqual(inputs["attention_mask"][1][1], 1)


if __name__ == "__main__":
    unittest.main()
