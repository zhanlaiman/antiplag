import unittest
from unittest.mock import patch

from plagiarism.matching import _tokens
from plagiarism.web_search import DEFAULT_QUERY_STRATEGY, DISTINCTIVE_STRATEGY, QUERY_STRATEGY, plan_queries, run
from scripts.compare_web_queries_offline import conditional_highlight, probe_evidence


class StrategyTests(unittest.TestCase):
    def test_hybrid_is_default_and_previous_strategies_are_reachable_in_normal_runner(self):
        text = "Перед отправлением дежурная проверила тормозную магистраль пассажирского поезда."
        class NoResults:
            def search(self, query, request_id):
                return []
        self.assertEqual(plan_queries(text), plan_queries(text, strategy=DEFAULT_QUERY_STRATEGY))
        default_result = run(text, searcher=NoResults(), fetcher=object())
        self.assertEqual(default_result["web"]["query_strategy"], DEFAULT_QUERY_STRATEGY)
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


class HybridStrategyTests(unittest.TestCase):
    def test_budget_offsets_and_uniqueness_for_all_supported_request_counts(self):
        from plagiarism.web_search import HYBRID_STRATEGY
        for size in (6, 79, 80, 81, 160, 161, 400, 710, 2000):
            text = " ".join("слово%d" % i for i in range(size))
            for limit in (1, 2, 3, 6):
                queries = plan_queries(text, limit=limit, strategy=HYBRID_STRATEGY)
                self.assertEqual(len(queries), min(limit, (size + 79) // 80))
                self.assertEqual(len({q["query"] for q in queries}), len(queries))
                for q in queries:
                    self.assertEqual(q["query"].strip('"'), text[q["start"]:q["end"]])

    def test_keeps_the_opening_and_a_distinctive_passage_in_two_requests(self):
        from plagiarism.web_search import HYBRID_STRATEGY
        opening = "Дежурный внимательно перепроверил каждое показание перед записью итогового результата"
        text = opening + ". " + ("Повторные замеры подтвердили ранее записанные показания прибора. " * 12)
        text += "\nКриогенная установка неожиданно зарегистрировала сверхпроводимость образца при охлаждении."
        queries = plan_queries(text, limit=2, strategy=HYBRID_STRATEGY)
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0]["start"], 0)
        self.assertTrue(any("криогенная" in q["query"] for q in queries))

    def test_duplicate_phrases_do_not_create_paid_repeat_requests(self):
        from plagiarism.web_search import HYBRID_STRATEGY
        text = "Контролёр проверил каждую строку после окончания измерения.\n" * 100
        queries = plan_queries(text, strategy=HYBRID_STRATEGY)
        self.assertEqual(len(queries), len({q["query"] for q in queries}))
        self.assertLessEqual(len(queries), 6)
        class NoResults:
            def search(self, query, request_id):
                return []
        result = run(text, searcher=NoResults(), fetcher=object(), query_strategy=HYBRID_STRATEGY)
        self.assertEqual(result["web"]["queries_attempted"], len(queries))
        self.assertEqual(result["web"]["query_strategy"], HYBRID_STRATEGY)


if __name__ == "__main__":
    unittest.main()
