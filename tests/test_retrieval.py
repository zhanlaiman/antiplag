from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plagiarism import retrieval


class RetrievalTests(unittest.TestCase):
    def test_chunks_preserve_emoji_offsets_and_cover_entire_text(self):
        text = "🧪 " + " ".join(f"слово{index}" for index in range(350))
        windows = retrieval.chunks(text)
        self.assertEqual(windows[0]["start"], 0)
        self.assertEqual(windows[-1]["end"], len(text))
        for window in windows:
            self.assertEqual(text[window["start"]:window["end"]], window["text"])
        for left, right in zip(windows, windows[1:]):
            self.assertLess(right["start"], left["end"])

    def test_lexical_ranking_finds_known_source_among_distractors(self):
        sources = [
            {"id": "unrelated", "text": "Музыканты выступили в большом концертном зале."},
            {"id": "target", "title": "Вода", "text": "Эксперимент показал, что фильтрация воды через песок снизила содержание примесей."},
            {"id": "topic", "text": "Исследователи изучают влияние температуры воды на рыб."},
        ]
        result = retrieval.retrieve("Фильтрация воды через песок снизила содержание примесей.", sources)
        self.assertEqual(result["candidates"][0]["source_id"], "target")
        self.assertNotIn("plagiarism", result)
        candidate = result["candidates"][0]
        self.assertEqual(sources[1]["text"][candidate["source_start"]:candidate["source_end"]], candidate["source_excerpt"])

    def test_single_oversized_token_is_split_without_losing_text(self):
        text = "🧪" + "длинноеслово" * 1500
        windows = retrieval.chunks(text)
        self.assertGreater(len(windows), 1)
        self.assertEqual(windows[0]["start"], 0)
        self.assertEqual(windows[-1]["end"], len(text))
        for window in windows:
            self.assertLessEqual(len(window["text"]), retrieval.MAX_CHUNK_CHARACTERS)
            self.assertEqual(text[window["start"]:window["end"]], window["text"])
        for previous, current in zip(windows, windows[1:]):
            self.assertLessEqual(current["start"], previous["end"])

    def test_hybrid_can_retrieve_passage_without_shared_words(self):
        sources = [
            {"id": "wrong", "text": "Обсуждается погода."},
            {"id": "target", "text": "The night train arrived after midnight."},
        ]
        with tempfile.TemporaryDirectory() as directory:
            cache = retrieval.EmbeddingCache(Path(directory) / "cache.sqlite3")
            with patch.object(retrieval, "embedding_identity", return_value="model-digest"), \
                 patch.object(retrieval.llm, "embed", return_value=[[0.0, 1.0], [1.0, 0.0], [1.0, 0.0]]):
                result = retrieval.retrieve("Ночной поезд прибыл после полуночи.", sources, mode="hybrid", top_k=1, cache=cache)
        self.assertEqual(result["candidates"][0]["source_id"], "target")
        self.assertEqual(result["candidates"][0]["bm25_score"], 0)
        self.assertNotIn("relation", result["candidates"][0])

    def test_embedding_cache_reuses_content_and_invalidates_model_change(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = retrieval.EmbeddingCache(Path(directory) / "cache.sqlite3")
            with patch.object(retrieval.llm, "embed", return_value=[[3.0, 4.0]]) as embed:
                vectors, missing = cache.vectors(["Текст", "Текст"], "model", "digest-one")
                self.assertEqual(missing, 1)
                self.assertEqual(vectors, [[0.6, 0.8], [0.6, 0.8]])
                self.assertEqual(cache.vectors(["Текст"], "model", "digest-one")[1], 0)
                self.assertEqual(embed.call_count, 1)
                cache.vectors(["Текст"], "model", "digest-two")
                self.assertEqual(embed.call_count, 2)

    def test_weak_word_matches_cannot_exclude_best_cross_language_candidate(self):
        sources = [{"id": f"distractor-{index}", "text": f"Музыка в зале пример{index}."} for index in range(6)]
        sources.append({"id": "translation", "text": "The acoustic ceiling reduced the echo."})
        # Weak lexical overlap boosts six distractors under ordinary RRF. The
        # strongest dense match must still reach the five-item shortlist.
        vectors = [[0.0, 1.0]] * 6 + [[1.0, 0.0], [1.0, 0.0]]
        with tempfile.TemporaryDirectory() as directory:
            cache = retrieval.EmbeddingCache(Path(directory) / "cache.sqlite3")
            with patch.object(retrieval, "embedding_identity", return_value="digest"), \
                 patch.object(retrieval.llm, "embed", return_value=vectors):
                result = retrieval.retrieve("Музыка стала разборчивее после отделки потолка.", sources,
                                            mode="hybrid", top_k=5, cache=cache)
        selected = [item["source_id"] for item in result["candidates"]]
        self.assertIn("translation", selected)

    def test_empty_sources_and_invalid_parameters_fail_explicitly(self):
        with self.assertRaises(ValueError):
            retrieval.retrieve("Текст", [])
        with self.assertRaises(ValueError):
            retrieval.retrieve("Текст", [{"id": "1", "text": "Источник"}], top_k=True)


if __name__ == "__main__":
    unittest.main()
