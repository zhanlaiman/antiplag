import os
import unittest
from urllib.parse import quote
from unittest.mock import patch

from plagiarism.web_search import (
    BALANCED_URL_STRATEGY,
    CITATION_STRATEGY,
    DOMAIN_URL_STRATEGY,
    MAX_URLS,
    PATH_URL_STRATEGY,
    _fetch_order,
    _rank_url_paths,
    run,
)


def source_groups():
    groups = [[f"https://q{q}-rank{r}.test/page" for r in range(5)] for q in range(6)]
    groups[0][3] = "https://publisher.test/earlier-source"
    groups[3][0] = "https://publisher.test/later-source"
    return groups


class URLOrderTests(unittest.TestCase):
    def test_path_evidence_can_restore_a_later_result_on_the_same_host(self):
        groups = [[f"https://q{q}-rank{r}.test/page" for r in range(5)] for q in range(6)]
        expected = "https://encyclopedia.test/wiki/" + quote("Шахматные_часы")
        groups[0][0] = "https://encyclopedia.test/wiki/" + quote("Память")
        groups[1][2] = expected
        urls = list(dict.fromkeys(url for group in groups for url in group))
        old = _fetch_order(urls, groups, BALANCED_URL_STRATEGY)
        ordered = _rank_url_paths(_fetch_order(urls, groups, PATH_URL_STRATEGY),
                                  "Ребёнок осматривал доску и часы перед началом партии.")
        self.assertNotIn(expected, old[:MAX_URLS])
        self.assertEqual(ordered[0], expected)
        self.assertEqual(set(old), set(ordered))

    def test_only_nonnumeric_path_words_supply_evidence_and_ties_are_stable(self):
        urls = ["https://часы.test/page?topic=часы#приборы", "https://source.test/2026/год",
                "https://source.test/" + quote("приборы_и_часы")]
        self.assertEqual(_rank_url_paths(urls, "Автор обсуждает часы и приборы в 2026 году."),
                         [urls[2], urls[0], urls[1]])
        self.assertEqual(_rank_url_paths(urls, "Редактор осматривал старинную обсерваторию перед открытием."), urls)

    def test_balanced_order_keeps_two_sources_on_the_same_host_from_different_queries(self):
        groups = source_groups()
        urls = [url for group in groups for url in group]
        previous = _fetch_order(urls, groups, DOMAIN_URL_STRATEGY)
        candidate = _fetch_order(urls, groups, BALANCED_URL_STRATEGY)
        first, second = groups[0][3], groups[3][0]
        self.assertIn(first, previous[:MAX_URLS])
        self.assertNotIn(second, previous[:MAX_URLS])
        self.assertIn(first, candidate[:MAX_URLS])
        self.assertIn(second, candidate[:MAX_URLS])
        self.assertEqual(candidate[:MAX_URLS // 2], previous[:MAX_URLS // 2])
        self.assertEqual(set(candidate), set(previous))
        self.assertEqual(len(candidate), len(set(candidate)))

    def test_legacy_order_continues_to_rotate_domains(self):
        urls = ["https://one.test/a", "https://one.test/b", "https://two.test/a",
                "https://two.test/b", "https://three.test/a"]
        self.assertEqual(_fetch_order(urls, [urls], DOMAIN_URL_STRATEGY),
                         [urls[0], urls[2], urls[4], urls[1], urls[3]])

    def test_missing_or_repeated_query_entries_cannot_add_an_undiscovered_url(self):
        urls = ["https://one.test/a", "https://two.test/b"]
        groups = [[None, urls[0], urls[0]], ["https://not-discovered.test/c"]]
        for strategy in (DOMAIN_URL_STRATEGY, BALANCED_URL_STRATEGY):
            self.assertEqual(_fetch_order(urls, groups, strategy), urls)

    def test_runtime_records_selected_policy_and_keeps_twelve_loads(self):
        groups = source_groups()
        text = "\n\n".join(" ".join(f"абзац{p}измерение{i}" for i in range(100)) for p in range(6))

        class SavedSearch:
            provider = "saved-fixture"
            def __init__(self):
                self.calls = 0
            def search(self, query, request_id):
                result = [{"url": url} for url in groups[self.calls]]
                self.calls += 1
                return result

        class SavedPages:
            def __init__(self):
                self.calls = []
            def fetch(self, url):
                self.calls.append(url)
                return {"url": url, "text": "На этой странице опубликовано расписание посещения городского музея."}

        previous_pages, candidate_pages = SavedPages(), SavedPages()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ISTOK_WEB_URL_STRATEGY", None)
            previous = run(text, searcher=SavedSearch(), fetcher=previous_pages,
                           query_strategy=CITATION_STRATEGY, url_strategy=DOMAIN_URL_STRATEGY, use_zero_fallback=False)
            candidate = run(text, searcher=SavedSearch(), fetcher=candidate_pages,
                            query_strategy=CITATION_STRATEGY, url_strategy=BALANCED_URL_STRATEGY, use_zero_fallback=False)
        self.assertEqual(previous["web"]["url_strategy"], DOMAIN_URL_STRATEGY)
        self.assertEqual(candidate["web"]["url_strategy"], BALANCED_URL_STRATEGY)
        self.assertEqual(len(previous_pages.calls), MAX_URLS)
        self.assertEqual(len(candidate_pages.calls), MAX_URLS)
        self.assertNotIn(groups[3][0], previous_pages.calls)
        self.assertIn(groups[3][0], candidate_pages.calls)
        self.assertIn(groups[0][3], candidate_pages.calls)

    def test_invalid_policy_is_rejected_before_search_or_fetch(self):
        class UnexpectedSearch:
            def search(self, query, request_id):
                raise AssertionError("Search must not run")

        with patch("plagiarism.web_search.selected_provider", side_effect=AssertionError("Provider must not be read")):
            with self.assertRaises(ValueError):
                run("Прибор сохраняет показания после завершения повторного контрольного измерения.",
                    searcher=UnexpectedSearch(), fetcher=object(), url_strategy="unknown-policy")
        with self.assertRaises(ValueError):
            _fetch_order([], [], "unknown-policy")


if __name__ == "__main__":
    unittest.main()
