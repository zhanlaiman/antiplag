from copy import deepcopy
import unittest
from unittest.mock import patch

from plagiarism.matching import _tokens
from plagiarism.query_clauses import CLAUSE_STRATEGY, clause_boundary_queries
from plagiarism.web_search import SHORT_PARAGRAPH_STRATEGY, plan_queries


def query(text, start=0):
    words = _tokens(text[start:])
    return {"query": '"' + " ".join(w.value[:40] for w in words) + '"',
            "start": start + words[0].start, "end": start + words[-1].end}


class ClauseQueryTests(unittest.TestCase):
    def test_three_clause_separators_keep_original_unicode_offsets_and_six_words(self):
        for separator in (": ", "; ", " — ", " – "):
            with self.subTest(separator=separator):
                text = "🙂 Вводная ремарка" + separator + "Ёлка липа сосна берёза клён кедр"
                plan = [query(text)]
                result = clause_boundary_queries(text, plan, ())
                self.assertEqual(result[0]["query"], '"елка липа сосна береза клен кедр"')
                self.assertEqual(text[result[0]["start"]:result[0]["end"]], "Ёлка липа сосна берёза клён кедр")
                self.assertEqual(result[0]["start"], text.index("Ёлка"))
                self.assertEqual(len(result), len(plan))

    def test_five_word_suffix_protected_query_and_smaller_limits_are_unchanged(self):
        text = "Вводная ремарка: липа сосна берёза клён кедр"
        plan = [query(text)]
        self.assertEqual(clause_boundary_queries(text, plan, ()), plan)
        text += " пихта"
        plan = [query(text)]
        self.assertEqual(clause_boundary_queries(text, plan, (q["query"] for q in plan)), plan)
        for limit in (1, 3, 5):
            self.assertEqual(clause_boundary_queries(text, plan, (), limit=limit), plan)

    def test_hyphen_time_url_numeric_range_and_comma_are_not_clauses(self):
        for text in (
            "Из-за ветра сосна берёза клён кедр липа пихта",
            "Начало 12:30 затем сосна берёза клён кедр липа пихта",
            "Начало 12: 30 затем сосна берёза клён кедр липа пихта",
            "Период 2000 – 2010 затем сосна берёза клён кедр липа пихта",
            "Страница https://example.org затем сосна берёза клён кедр липа пихта",
            "Вводная ремарка, затем сосна берёза клён кедр липа пихта",
        ):
            with self.subTest(text=text):
                plan = [query(text)]
                self.assertEqual(clause_boundary_queries(text, plan, ()), plan)

    def test_only_last_boundary_is_used_and_duplicate_query_is_not_created(self):
        text = "Начало: сосна берёза клён кедр липа пихта; ещё пять коротких слов здесь"
        plan = [query(text)]
        self.assertEqual(clause_boundary_queries(text, plan, ()), plan)
        text = "Начало: сосна берёза клён кедр липа пихта"
        plan = [query(text), query(text, text.index("сосна"))]
        self.assertEqual(clause_boundary_queries(text, plan, ()), plan)

    def test_one_change_preserves_order_metadata_and_input_without_network(self):
        text = "Начало: сосна берёза клён кедр липа пихта. Конец: дуб вяз ель бук ясень осина"
        split = text.index(". ")
        plan = [query(text[:split]), query(text, split + 2)]
        plan[0]["metadata"] = {"nested": [1, 2]}
        original = deepcopy(plan)
        with patch("socket.socket", side_effect=AssertionError("Planner cannot use network")):
            result = clause_boundary_queries(text, plan, ())
        self.assertNotEqual(result[0], plan[0])
        self.assertEqual(result[1], plan[1])
        self.assertEqual(plan, original)
        self.assertEqual(result[0]["metadata"], plan[0]["metadata"])
        self.assertIsNot(result[0]["metadata"], plan[0]["metadata"])

    def test_opt_in_plan_preserves_legacy_strategy_count_and_citations(self):
        text = ("Здесь указаны материалы: сосна берёза клён кедр липа пихта дуб вяз ель. "
                + "Повторные измерения закончились после окончания планового занятия. " * 22)
        old = plan_queries(text, strategy=SHORT_PARAGRAPH_STRATEGY)
        new = plan_queries(text, strategy=CLAUSE_STRATEGY)
        self.assertEqual(len(new), len(old))
        self.assertEqual(len({q["query"] for q in new}), len(new))
        self.assertEqual(sum(a != b for a, b in zip(old, new)), 1)
        self.assertTrue(any(q["start"] == text.index("сосна") for q in new))
        for limit in range(1, 6):
            self.assertEqual(plan_queries(text, strategy=SHORT_PARAGRAPH_STRATEGY, limit=limit),
                             plan_queries(text, strategy=CLAUSE_STRATEGY, limit=limit))
        quoted = 'В записи сказано «предисловие: сосна берёза клён кедр липа пихта дуб». '
        from plagiarism.web_search import _citation_queries
        old = plan_queries(quoted, strategy=SHORT_PARAGRAPH_STRATEGY)
        new = plan_queries(quoted, strategy=CLAUSE_STRATEGY)
        for citation in _citation_queries(quoted):
            if citation in old:
                self.assertIn(citation, new)

    def test_runner_reads_a_source_for_short_clause_without_extra_queries_or_model(self):
        from plagiarism.web_search import run
        excerpt = "сосна берёза клён кедр липа пихта дуб вяз ель"
        text = ("Здесь указаны материалы: " + excerpt + ". "
                + "Повторные измерения закончились после окончания планового занятия. " * 22)
        old_plan = plan_queries(text, strategy=SHORT_PARAGRAPH_STRATEGY)
        new_plan = plan_queries(text, strategy=CLAUSE_STRATEGY)
        changed = next(b["query"] for a, b in zip(old_plan, new_plan) if a != b)
        class Search:
            def __init__(self):
                self.calls = []
            def search(self, phrase, request_id):
                self.calls.append((phrase, request_id))
                return [{"url": "https://example.org/botany"}] if phrase == changed else []
        class Fetch:
            def fetch(self, url):
                return {"url": url, "title": "Read source", "text": excerpt}
        before, after = Search(), Search()
        old = run(text, searcher=before, fetcher=Fetch(), query_strategy=SHORT_PARAGRAPH_STRATEGY)
        new = run(text, searcher=after, fetcher=Fetch(), query_strategy=CLAUSE_STRATEGY)
        self.assertEqual(len(before.calls), len(after.calls))
        self.assertEqual(new["web"]["query_strategy"], CLAUSE_STRATEGY)
        self.assertEqual(old["web"]["sources_checked"], 0)
        self.assertEqual(new["web"]["sources_checked"], 1)
        self.assertTrue(any(m["kind"] == "exact" and m["excerpt"] == excerpt for m in new["matches"]))


if __name__ == "__main__":
    unittest.main()
