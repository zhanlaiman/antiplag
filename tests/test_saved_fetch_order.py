import unittest

from scripts.compare_saved_fetch_order import domain_order, hybrid_order, query_order


class SavedFetchOrderTests(unittest.TestCase):
    def test_two_sources_on_one_host_survive_mixed_query_and_domain_priority(self):
        groups = [[{"url": f"https://q{q}-rank{r}.test/page"} for r in range(5)] for q in range(6)]
        early_source = "https://publisher.test/earlier-source"
        later_source = "https://publisher.test/later-source"
        groups[0][3] = {"url": early_source}
        groups[3][0] = {"url": later_source}
        old, query, hybrid = domain_order(groups), query_order(groups), hybrid_order(groups)
        self.assertIn(early_source, old[:12])
        self.assertNotIn(later_source, old[:12])
        self.assertNotIn(early_source, query[:12])
        self.assertIn(later_source, query[:12])
        self.assertIn(early_source, hybrid[:12])
        self.assertIn(later_source, hybrid[:12])
        self.assertEqual(hybrid[:6], old[:6])
        self.assertEqual(set(hybrid), set(old))

    def test_invalid_and_repeated_results_do_not_take_queue_positions(self):
        groups = [
            [{"url": "https://one.test/a"}, {"url": "javascript:alert(1)"}],
            [{"url": "https://one.test/a#fragment"}, {"url": "https://two.test/b"}],
        ]
        for planner in (domain_order, query_order, hybrid_order):
            self.assertEqual(planner(groups), ["https://one.test/a", "https://two.test/b"])

    def test_small_queue_preserves_existing_order_and_every_url(self):
        groups = [[{"url": "https://one.test/a"}, {"url": "https://one.test/b"}],
                  [{"url": "https://two.test/a"}]]
        self.assertEqual(hybrid_order(groups), domain_order(groups))
        self.assertEqual(hybrid_order([]), [])


if __name__ == "__main__":
    unittest.main()
