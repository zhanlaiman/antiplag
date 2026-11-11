import unittest

import plagiarism.web_search as web
from plagiarism.matching import _tokens
from scripts.probe_dash_clause_queries import DASH_WINDOWS, dash_plan


class DashClauseProbeTests(unittest.TestCase):
    # Authored text only: no DEV or held-out corpus content.
    before = "У макета важная особенность"
    after = "синяя лампа каждый вечер освещает деревянную лестницу возле двери"

    def test_whitespace_delimited_em_and_en_dashes_split_a_clause(self):
        for dash in ("—", "–"):
            with self.subTest(dash=dash):
                text = self.before + " " + dash + " " + self.after
                tokens = _tokens(text)
                self.assertEqual(DASH_WINDOWS(text, tokens, 10, 1), [(4, 13)])
                old = web._distinctive_windows(text, tokens, 10, 1)
                self.assertTrue(any(start < 4 < end for start, end in old))

    def test_standalone_hyphen_keeps_original_segmentation(self):
        text = self.before + " - " + self.after
        tokens = _tokens(text)
        self.assertEqual(DASH_WINDOWS(text, tokens, 10, 6), web._distinctive_windows(text, tokens, 10, 6))

    def test_unspaced_em_dash_keeps_original_segmentation(self):
        text = self.before + "—" + self.after
        tokens = _tokens(text)
        self.assertEqual(DASH_WINDOWS(text, tokens, 10, 6), web._distinctive_windows(text, tokens, 10, 6))

    def test_planning_restores_production_function(self):
        original = web._distinctive_windows
        text = self.before + " — " + self.after
        baseline = web.plan_queries(text, strategy=web.CITATION_STRATEGY)
        dash_plan(text, web.CITATION_STRATEGY)
        self.assertIs(web._distinctive_windows, original)
        self.assertEqual(web.plan_queries(text, strategy=web.CITATION_STRATEGY), baseline)


if __name__ == "__main__":
    unittest.main()
