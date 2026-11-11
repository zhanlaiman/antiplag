import unittest
from unittest.mock import patch

from plagiarism.matching import analyze
from scripts.probe_document_metadata import document_index, rank_document_metadata
from scripts.probe_search_metadata import rank_by_snippet_overlap


PHRASE = "Астроном проверил положение телескопа перед повторным наблюдением далёкого созвездия"


class DocumentMetadataProbeTests(unittest.TestCase):
    def test_nonquery_document_window_can_be_ranked_without_labels(self):
        other, source = "https://example.test/other", "https://example.test/source"
        words = PHRASE.split()
        records = [{"query": "Автор добавил " + " ".join(words[:6]),
            "results": [{"url": source, "content": " ".join(words[3:])}]}]
        own, _ = rank_by_snippet_overlap([other, source], records)
        whole, audit = rank_document_metadata([other, source], PHRASE, records)
        self.assertEqual(own, [other, source])
        self.assertEqual(whole, [source, other])
        self.assertEqual(audit["reasons"][source]["longest_contiguous_document_words"], 6)
        self.assertFalse(audit["reasons"][source]["may_be_used_as_matching_evidence"])

    def test_titles_and_chunk_boundaries_do_not_invent_adjacency(self):
        base = ["https://example.test/first", "https://example.test/target"]
        words = PHRASE.split()
        for value in ({"title": " ".join(words[:5]), "content": " ".join(words[5:])},
                      {"content": " ".join(words[:5]) + " [...] " + " ".join(words[5:])}):
            order, audit = rank_document_metadata(base, PHRASE, [{"results": [{"url": base[1], **value}]}])
            self.assertEqual(order, base)
            self.assertEqual(audit["reasons"], {})

    def test_misleading_snippet_loss_and_independent_body_negative_remain(self):
        base = [f"https://example{i}.test/a" for i in range(20)]
        order, _ = rank_document_metadata(base, PHRASE, [{"results": [{"url": base[19], "content": PHRASE}]}])
        self.assertIn(base[11], base[:12])
        self.assertNotIn(base[11], order[:12])
        body = {"id": "negative", "title": "Independent astronomy note", "url": base[19],
                "text": "Наблюдатели составили карту звёздного неба и выбрали время следующей встречи возле городской обсерватории."}
        self.assertEqual(analyze(PHRASE, [body])["matches"], [])

    def test_index_is_bounded_and_unordered_same_topic_words_do_not_promote(self):
        base = ["https://example.test/first", "https://example.test/other"]
        order, audit = rank_document_metadata(base, PHRASE,
            [{"results": [{"url": base[1], "content": " ".join(reversed(PHRASE.split()))}]}])
        self.assertEqual(order, base)
        self.assertEqual(audit["reasons"], {})
        with patch("scripts.probe_document_metadata.MAX_DOCUMENT_TOKENS", 5):
            order, audit = rank_document_metadata(base, PHRASE, [])
            self.assertEqual(order, base)
            self.assertEqual(audit["fallback_reason"], "document_token_bound")
        index, reason = document_index(PHRASE)
        self.assertIsNone(reason)
        self.assertLessEqual(index["entries"], 5 * index["tokens"])


if __name__ == "__main__":
    unittest.main()
