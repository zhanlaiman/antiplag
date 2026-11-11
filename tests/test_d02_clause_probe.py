import unittest

from plagiarism import web_search
from plagiarism.matching import _tokens
from scripts.run_d02_clause_pair import plans_from_text


class ClausePairPlanningTests(unittest.TestCase):
    def test_single_query_variants_are_grounded_and_restore_the_original_planner(self):
        text = ("Во время обхода обсуждали новую особенность — лабораторные измерения должны сопровождаться "
                "независимой проверкой полученных численных результатов. "
                "Затем дежурный закрыл журнал и передал ключ сменщику.")
        previous = web_search._distinctive_windows
        plans, predicate = plans_from_text(text)
        self.assertIs(web_search._distinctive_windows, previous)
        self.assertEqual(set(plans), {"original", "clause"})
        self.assertEqual(len(plans["original"]), 1)
        self.assertEqual(len(plans["clause"]), 1)
        self.assertEqual(len(_tokens(plans["original"][0]["query"])), 10)
        self.assertEqual(len(_tokens(plans["clause"][0]["query"])), 9)
        for plan in plans.values():
            query = plan[0]
            self.assertEqual(query["query"], '"' + " ".join(t.value for t in _tokens(text[query["start"]:query["end"]])) + '"')
        self.assertNotEqual(predicate["before"], predicate["after"])

    def test_unchanged_query_is_not_presented_as_a_two_variant_experiment(self):
        text = "Лабораторные измерения должны сопровождаться независимой проверкой полученных численных результатов."
        previous = web_search._distinctive_windows
        with self.assertRaises(ValueError):
            plans_from_text(text)
        self.assertIs(web_search._distinctive_windows, previous)


if __name__ == "__main__":
    unittest.main()
