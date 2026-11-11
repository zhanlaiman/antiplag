import unittest
from unittest.mock import patch

from plagiarism.matching import _tokens
from plagiarism.web_search import DISTINCTIVE_STRATEGY, QUERY_STRATEGY, plan_queries, run
from scripts.compare_web_queries_offline import conditional_highlight, probe_evidence


class StrategyTests(unittest.TestCase):
    def test_uniform_remains_default_and_alternative_is_reachable_in_normal_runner(self):
        text = "Перед отправлением дежурная проверила тормозную магистраль пассажирского поезда."
        class NoResults:
            def search(self, query, request_id):
                return []
        self.assertEqual(plan_queries(text), plan_queries(text, strategy=QUERY_STRATEGY))
        result = run(text, searcher=NoResults(), fetcher=object(), query_strategy=DISTINCTIVE_STRATEGY)
        self.assertEqual(result["web"]["query_strategy"], DISTINCTIVE_STRATEGY)
        self.assertEqual(result["web"]["urls_attempted"], 0)
        with patch.dict("os.environ", {"ISTOK_WEB_QUERY_STRATEGY": DISTINCTIVE_STRATEGY}):
            result = run(text, searcher=NoResults(), fetcher=object())
        self.assertEqual(result["web"]["query_strategy"], DISTINCTIVE_STRATEGY)
        with self.assertRaises(ValueError):
            plan_queries(text, strategy="unsupported")

    def test_distinct_windows_respect_input_offsets_budget_and_paragraphs(self):
        text = "\n\n".join(" ".join("абзац%dслово%d" % (p, i) for i in range(90))
                           for p in range(8))
        queries = plan_queries(text, strategy=DISTINCTIVE_STRATEGY)
        self.assertEqual(queries, plan_queries(text, strategy=DISTINCTIVE_STRATEGY))
        self.assertEqual(len(queries), 6)
        paragraphs = set()
        for q in queries:
            self.assertEqual(q["query"].strip('"'), text[q["start"]:q["end"]])
            paragraphs.add(text.count("\n\n", 0, q["start"]))
        self.assertEqual(len(paragraphs), len(queries))
        self.assertEqual(len(plan_queries(text, limit=2, strategy=DISTINCTIVE_STRATEGY)), 2)

    def test_repetitions_and_short_lines_still_return_a_real_query(self):
        text = ("Редкая строка содержит шесть намеренно повторяющихся слов.\n") * 100
        queries = plan_queries(text, strategy=DISTINCTIVE_STRATEGY)
        self.assertEqual(len(queries), 1)
        text = "\n".join("слово%d" % i for i in range(12))
        self.assertTrue(plan_queries(text, strategy=DISTINCTIVE_STRATEGY))

    def test_query_position_is_not_the_same_as_literal_agreement(self):
        text = "Контролёр учёл все расходы при проверке старой сметы"
        q = plan_queries(text)
        result = probe_evidence(q, _tokens(text), {"target_span": [0, len(text)]},
                                "смета после проверки отражает все расходы".split())
        self.assertEqual(result["position_words"], 8)
        self.assertEqual(result["shared_consecutive_words"], 2)
        self.assertFalse(result["whole_query_in_both_spans"])

    def test_duplicate_wording_at_wrong_source_location_does_not_get_exact_credit(self):
        excerpt = "Техник проверил настройки микроскопа перед повторным измерением"
        text = excerpt + ". " + excerpt
        gold = {"source_id": "s", "target_span": [0, len(excerpt)],
                "source_span": [len(excerpt) + 2, len(text)]}
        match = {"kind": "exact", "source_id": "s", "start": 0, "end": len(excerpt),
                 "source_start": 0, "source_end": len(excerpt)}
        result = conditional_highlight([match], _tokens(excerpt), _tokens(text), gold)
        self.assertEqual(result["covered_words"], 0)


if __name__ == "__main__":
    unittest.main()
