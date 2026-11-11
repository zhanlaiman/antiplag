import json
import unittest

from plagiarism.matching import analyze
from scripts.probe_search_metadata import (MAX_CONTENT_CHARACTERS, MAX_TITLE_CHARACTERS,
                                          rank_by_snippet_overlap, retain_results)


PHRASE = "Астроном проверил положение телескопа перед повторным наблюдением далёкого созвездия"


class SearchMetadataProbeTests(unittest.TestCase):
    def test_optional_metadata_is_bounded_and_secret_free_without_losing_url(self):
        result = retain_results([{"url": "https://example.test/a", "title": "T" * 1000,
            "content": "C" * 9000, "score": 0.8}])[0]
        self.assertEqual(len(result["title"]), MAX_TITLE_CHARACTERS)
        self.assertEqual(len(result["content"]), MAX_CONTENT_CHARACTERS)
        self.assertTrue(result["title_truncated"] and result["content_truncated"])
        self.assertEqual(result["metadata_purpose"], "ranking_only")
        for score in (True, float("nan"), float("inf"), -0.5, 2, "high"):
            clean = retain_results([{"url": "https://example.test/a", "title": "secret-value", "score": score}], "secret-value")
            self.assertNotIn("score", clean[0])
            self.assertNotIn("secret-value", json.dumps(clean))
        with self.assertRaises(ValueError):
            retain_results([{"url": "https://example.test/secret-value"}], "secret-value")

    def test_provider_rank_survives_duplicate_url_and_legacy_metadata_falls_back(self):
        results = retain_results([{"url": "https://example.test/a#one"}, {"url": "https://example.test/a#two"},
                                 {"url": "https://example.test/b"}])
        self.assertEqual([r["provider_rank"] for r in results], [1, 3])
        order = ["https://example.test/b", "https://example.test/a"]
        selected, reasons = rank_by_snippet_overlap(order, [{"query": PHRASE, "results": results}])
        self.assertEqual(selected, order)
        self.assertEqual(reasons, {})
        rich = retain_results([{"url": "https://example.test/a"}, {"url": "https://example.test/a#duplicate"},
                               {"url": "https://example.test/b", "content": PHRASE}])
        _, why = rank_by_snippet_overlap(order, [{"query": PHRASE, "results": rich}], projected=True)
        self.assertEqual(why["https://example.test/b"]["provider_rank"], 3)

    def test_one_phrase_is_not_created_across_title_and_snippet_or_chunk_break(self):
        target, other = "https://example.test/target", "https://example.test/other"
        words = PHRASE.split()
        for metadata in ({"title": " ".join(words[:5]), "content": " ".join(words[5:])},
                         {"content": " ".join(words[:5]) + " [...] " + " ".join(words[5:])}):
            order, reasons = rank_by_snippet_overlap([other, target],
                [{"query": PHRASE, "results": [{"url": target, **metadata}]}])
            self.assertEqual(order, [other, target])
            self.assertEqual(reasons, {})

    def test_rank14_phrase_moves_inside_twelve_but_unordered_vocabulary_does_not(self):
        base = [f"https://example{i}.test/a" for i in range(15)]
        records = [{"query": PHRASE, "results": [
            {"url": base[13], "content": "Преамбула. " + PHRASE + ".", "score": 0.2},
            {"url": base[14], "content": " ".join(reversed(PHRASE.split())), "score": 0.99}]}]
        order, reasons = rank_by_snippet_overlap(base, records)
        self.assertEqual(order[0], base[13])
        self.assertEqual(order[1:], [url for url in base if url != base[13]])
        self.assertEqual(reasons[base[13]]["longest_contiguous_query_words"], 9)
        self.assertFalse(reasons[base[13]]["may_be_used_as_matching_evidence"])

    def test_false_snippet_can_displace_a_real_source_but_never_proves_copying(self):
        base = [f"https://example{i}.test/a" for i in range(20)]
        genuine, misleading = base[11], base[19]
        order, _ = rank_by_snippet_overlap(base, [{"query": PHRASE,
            "results": [{"url": misleading, "content": PHRASE}]}])
        self.assertIn(genuine, base[:12])
        self.assertNotIn(genuine, order[:12])  # Explicit authored loss.
        source = {"id": "authored-misleading-page", "title": "Independent page", "url": misleading,
            "text": "Редактор подготовил независимое описание расписания школьной библиотеки перед открытием выставки."}
        self.assertEqual(analyze(PHRASE, [source])["matches"], [])

    def test_unbounded_groups_and_extra_results_are_rejected_locally(self):
        with self.assertRaises(ValueError):
            retain_results([{"url": "https://example.test/a"}] * 6)
        with self.assertRaises(ValueError):
            rank_by_snippet_overlap([], [{"query": PHRASE, "results": []}] * 7)


if __name__ == "__main__":
    unittest.main()
