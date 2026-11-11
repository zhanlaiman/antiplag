import unittest
from unittest.mock import patch

from plagiarism import web_search
from plagiarism.matching import _tokens


QUOTE = "Точные лабораторные измерения требуют регулярной калибровки датчиков стабильной температуры помещения"
TEXT = '«' + QUOTE + '» (русская Википедия, статья «Метрология»).'
URL = "https://ru.wikipedia.org/wiki/%D0%9C%D0%B5%D1%82%D1%80%D0%BE%D0%BB%D0%BE%D0%B3%D0%B8%D1%8F"


class EmptySearch:
    provider = "authored"
    def __init__(self): self.calls = []
    def search(self, query, request_id):
        self.calls.append(query)
        return []


class Pages:
    def __init__(self, text=QUOTE): self.calls, self.text = [], text
    def fetch(self, url):
        self.calls.append(url)
        return {"url": url, "text": self.text, "title": "Authored source"}


class NamedReferencePipelineTests(unittest.TestCase):
    def run_check(self, enabled, text=TEXT, source=QUOTE, search=None):
        search, pages = search or EmptySearch(), Pages(source)
        with patch.object(web_search, "selected_provider", return_value="tavily"):
            report = web_search.run(text, searcher=search, fetcher=pages, use_named_references=enabled)
        return report, search, pages

    def test_an_input_reference_is_loaded_and_proved_without_an_extra_query(self):
        report, search, pages = self.run_check(True)
        self.assertEqual(search.calls, [q["query"] for q in web_search.plan_queries(TEXT)])
        self.assertEqual(pages.calls, [URL])
        self.assertEqual(report["stats"]["matched_words"], len(_tokens(QUOTE)))
        self.assertTrue(report["matches"][0]["quoted"])
        self.assertEqual(report["matches"][0]["source_excerpt"], QUOTE)
        self.assertEqual(report["matches"][0]["source_url"], URL)
        self.assertEqual(report["web"]["search_urls_discovered"], 0)
        self.assertEqual(report["web"]["input_reference_urls_discovered"], 1)
        self.assertEqual(report["web"]["input_references"][0]["source_claim"], "unverified_input_reference")

    def test_a_false_citation_never_becomes_evidence_by_its_name(self):
        source = "Редактор подготовил независимое описание устройства школьной библиотеки перед открытием выставки."
        report, _, pages = self.run_check(True, source=source)
        self.assertEqual(pages.calls, [URL])
        self.assertEqual(report["matches"], [])
        self.assertEqual(report["stats"]["matched_words"], 0)

    def test_disabled_reference_discovery_preserves_queries_and_does_not_load_a_hint(self):
        enabled, search1, _ = self.run_check(True)
        disabled, search2, pages = self.run_check(False)
        self.assertEqual(search1.calls, search2.calls)
        self.assertEqual(enabled["web"]["query_spans"], disabled["web"]["query_spans"])
        self.assertEqual(pages.calls, [])
        self.assertEqual(disabled["web"]["sources_checked"], 0)
        self.assertIsNone(disabled["stats"]["uniqueness_percent"])

    def test_search_result_and_named_reference_are_one_physical_load(self):
        class Search(EmptySearch):
            def search(self, query, request_id):
                self.calls.append(query)
                return [{"url": URL}]
        report, _, pages = self.run_check(True, search=Search())
        self.assertEqual(pages.calls, [URL])
        self.assertEqual(report["web"]["urls_supported"], 1)
        self.assertEqual(report["web"]["sources_checked"], 1)

    def test_invalid_option_is_rejected_before_any_search(self):
        search = EmptySearch()
        with self.assertRaises(ValueError):
            web_search.run(TEXT, searcher=search, fetcher=Pages(), use_named_references="yes")
        self.assertEqual(search.calls, [])

    def test_named_source_gets_a_slot_without_exceeding_twelve_reads(self):
        text = TEXT + "\n" + "Архив хранит отдельные записи о часах работы здания. " * 20
        class Search(EmptySearch):
            def search(self, query, request_id):
                index = len(self.calls)
                self.calls.append(query)
                return [{"url": f"https://noise{index}-{rank}.test/page"} for rank in range(5)]
        class FullPages(Pages):
            def fetch(self, url):
                self.calls.append(url)
                return {"url": url, "text": QUOTE if url == URL else
                        "Редактор подготовил независимое описание школьного опыта перед открытием выставки."}
        search, pages = Search(), FullPages()
        with patch.object(web_search, "selected_provider", return_value="tavily"):
            report = web_search.run(text, searcher=search, fetcher=pages, use_named_references=True)
        self.assertEqual(pages.calls[0], URL)
        self.assertEqual(len(pages.calls), 12)
        self.assertIn(URL, report["web"]["discovered_urls"])
        self.assertIn(URL, report["web"]["attempted_urls"])
        self.assertEqual(report["stats"]["matched_words"], len(_tokens(QUOTE)))


if __name__ == "__main__":
    unittest.main()
