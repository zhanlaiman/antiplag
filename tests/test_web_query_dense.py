import unittest

from plagiarism.matching import _tokens
from plagiarism.web_search import CITATION_STRATEGY, DENSE_STRATEGY, plan_queries


class DenseQueryTests(unittest.TestCase):
    def test_additional_phrase_covers_another_sentence_and_retains_initial_request(self):
        # Authored fixture: the short, unusual opening wins the old average
        # score; the ordinary technical sentence must still get a query.
        opening = "Кристаллографическая лаборатория продемонстрировала сверхпроводимость приглашённым преподавателям."
        passage = "Датчик сохраняет изменения напряжения после завершения повторного измерения контрольного образца."
        text = opening + " " + passage
        previous = plan_queries(text, strategy=CITATION_STRATEGY)
        candidate = plan_queries(text, strategy=DENSE_STRATEGY)
        self.assertEqual(len(previous), 1)
        self.assertFalse(any(q["start"] >= len(opening) for q in previous))
        self.assertEqual(candidate[:len(previous)], previous)
        self.assertTrue(any(len(opening) < q["start"] and q["end"] <= len(text) for q in candidate))

    def test_explicit_limit_and_existing_queries_are_preserved(self):
        text = (
            "Сотрудник проверил порядок записи перед открытием журнала. "
            "Наблюдатель отметил показание прибора после завершения повторного опыта. "
            "Результат сравнили с прежним измерением и сохранили в таблице. "
            "Затем дежурный передал заполненный журнал следующей смене лаборатории."
        )
        for limit in (1, 2, 3, 6):
            previous = plan_queries(text, limit=limit, strategy=CITATION_STRATEGY)
            candidate = plan_queries(text, limit=limit, strategy=DENSE_STRATEGY)
            self.assertEqual(candidate[:len(previous)], previous)
            self.assertLessEqual(len(candidate), limit)
            self.assertEqual(len(candidate), len({q["query"] for q in candidate}))

    def test_six_query_plan_for_long_text_is_identical(self):
        text = "\n\n".join(" ".join(f"абзац{p}измерение{i}" for i in range(100)) for p in range(6))
        previous = plan_queries(text, strategy=CITATION_STRATEGY)
        self.assertEqual(len(previous), 6)
        self.assertEqual(plan_queries(text, strategy=DENSE_STRATEGY), previous)

    def test_repeated_sentence_does_not_fill_budget_with_duplicate_requests(self):
        text = "Контролёр проверил каждую строку после окончания измерения.\n" * 3
        candidate = plan_queries(text, strategy=DENSE_STRATEGY)
        self.assertEqual(len(candidate), 1)
        self.assertEqual(len({q["query"] for q in candidate}), 1)

    def test_citation_priority_and_unicode_offsets_survive_added_queries(self):
        quote = "Каждое измерение требует проверки единиц и условий проведения опыта"
        text = "😀 Автор инструкции уточняет: «" + quote + "». "
        text += "Заполненный журнал передали следующей смене лаборатории после окончания работы."
        previous = plan_queries(text, strategy=CITATION_STRATEGY)
        candidate = plan_queries(text, strategy=DENSE_STRATEGY)
        self.assertEqual(candidate[:len(previous)], previous)
        self.assertEqual(candidate[0]["start"], text.index(quote))
        for query in candidate:
            words = " ".join(t.value for t in _tokens(text[query["start"]:query["end"]]))
            self.assertEqual(query["query"], '"' + words + '"')

    def test_one_word_lines_have_a_bounded_fallback(self):
        text = "\n".join(f"измерение{i}" for i in range(30))
        previous = plan_queries(text, strategy=CITATION_STRATEGY)
        candidate = plan_queries(text, strategy=DENSE_STRATEGY)
        self.assertEqual(candidate[:len(previous)], previous)
        self.assertGreater(len(candidate), len(previous))
        self.assertLessEqual(len(candidate), 6)


if __name__ == "__main__":
    unittest.main()
