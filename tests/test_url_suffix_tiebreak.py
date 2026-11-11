"""Authored selector checks; no DEV or unfamiliar publication is opened."""
import unittest
from scripts.probe_url_suffix_tiebreak import fold, rank_with_rule


class UrlSuffixTieBreakTests(unittest.TestCase):
    def setUp(self):
        self.rule = {"minimum_root_characters": 4, "suffixes": ["иями", "ами", "ями", "ия", "ие", "ые", "ов", "а", "и", "ы"]}

    def test_authored_inflections_share_a_root_without_cascading(self):
        self.assertEqual(fold("калибровка", self.rule), fold("калибровки", self.rule))
        self.assertEqual(fold("соревнованиями", self.rule), "соревнован")

    def test_short_roots_and_non_russian_tokens_are_preserved_or_excluded(self):
        self.assertEqual(fold("мера", self.rule), "мера")
        self.assertIsNone(fold("robots", self.rule))
        self.assertIsNone(fold("word123", self.rule))

    def test_existing_exact_priority_cannot_be_overridden_by_many_folded_hits(self):
        exact = "https://example.test/проверки"
        folded = "https://example.test/проверка-приборы-калибровка"
        text = "Проверки приборов калибровки"
        self.assertEqual(rank_with_rule([folded, exact], text, self.rule)[0], exact)

    def test_secondary_score_changes_only_ties_and_preserves_zero_tie_order(self):
        first, second = "https://example.test/alpha", "https://example.test/beta"
        related = "https://example.test/%D0%BA%D0%B0%D0%BB%D0%B8%D0%B1%D1%80%D0%BE%D0%B2%D0%BA%D0%B0"
        self.assertEqual(rank_with_rule([first, related, second], "Калибровки", self.rule), [related, first, second])


if __name__ == "__main__":
    unittest.main()
