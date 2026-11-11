import unittest
from urllib.parse import quote

from scripts.audit_dev_fetch_balance import four_domain_order, path_relevant_order, path_word_score
from scripts.compare_saved_fetch_order import hybrid_order


class FetchPathPriorityTests(unittest.TestCase):
    def test_document_word_in_decoded_path_can_restore_a_later_result_on_one_host(self):
        groups = [[{"url": f"https://q{q}-rank{r}.test/page"} for r in range(5)] for q in range(6)]
        groups[0][0] = {"url": "https://encyclopedia.test/wiki/" + quote("Память")}
        expected = "https://encyclopedia.test/wiki/" + quote("Шахматные_часы")
        groups[1][2] = {"url": expected}
        text = "Ребёнок осматривал доску и часы перед началом партии."
        current = hybrid_order(groups)
        candidate = path_relevant_order(groups, text)
        self.assertNotIn(expected, current[:12])
        self.assertEqual(candidate[0], expected)
        self.assertEqual(set(current), set(candidate))

    def test_query_fragment_domain_and_numbers_are_not_path_evidence(self):
        text = "Автор обсуждает часы и приборы в 2026 году."
        self.assertEqual(path_word_score("https://часы.test/page?topic=часы#приборы", text), 0)
        self.assertEqual(path_word_score("https://source.test/2026/год", text), 0)
        self.assertEqual(path_word_score("https://source.test/" + quote("приборы_и_часы"), text), 2)

    def test_zero_overlap_and_equal_scores_keep_current_order(self):
        groups = [[{"url": "https://one.test/page"}, {"url": "https://one.test/next"}],
                  [{"url": "https://two.test/article"}]]
        self.assertEqual(path_relevant_order(groups, "Автор составляет маршрут прогулки по городу."), hybrid_order(groups))

    def test_four_domain_variant_keeps_its_fixed_prefix_without_inventing_urls(self):
        groups = [[{"url": f"https://q{q}-rank{r}.test/page"} for r in range(5)] for q in range(6)]
        candidate = four_domain_order(groups)
        self.assertEqual(candidate[:4], hybrid_order(groups)[:4])
        self.assertEqual(len(candidate), len(set(candidate)))
        self.assertEqual(set(candidate), set(hybrid_order(groups)))


if __name__ == "__main__":
    unittest.main()
