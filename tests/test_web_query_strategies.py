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


class CitationStrategyTests(unittest.TestCase):
    def test_long_citation_is_searched_without_surrounding_editorial_words(self):
        from plagiarism.web_search import CITATION_STRATEGY
        quote = "Испытательная установка записывает изменение частоты сигнала после охлаждения образца"
        for left, right in [("«", "»"), ("“", "”"), ("„", "“"), ('"', '"')]:
            text = "Автор исследования уточняет " + left + quote + right + ". " + "Обсуждение результатов продолжалось после окончания опыта. " * 12
            queries = plan_queries(text, strategy=CITATION_STRATEGY)
            selected = [q for q in queries if q["start"] == text.index(quote)]
            self.assertTrue(selected)
            self.assertLessEqual(selected[0]["end"], text.index(quote) + len(quote))
            self.assertLessEqual(len(queries), (len(_tokens(text)) + 79) // 80)

    def test_short_dated_title_retains_six_words_of_context(self):
        from plagiarism.web_search import CITATION_STRATEGY
        text = "В дневнике есть запись о необычной выставке 1937 года «Город машин»."
        queries = plan_queries(text, strategy=CITATION_STRATEGY)
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]["query"], '"необычной выставке 1937 года город машин"')
        unconnected = "В 1937 году это произошло. Теперь речь идёт о «Городе машин»."
        from plagiarism.web_search import _citation_queries
        self.assertEqual(_citation_queries(unconnected), [])

    def test_many_identical_quotes_respect_request_cap_and_unicode_offsets(self):
        from plagiarism.web_search import CITATION_STRATEGY
        text = '😀 «Каждое измерение требует проверки единиц и условий проведения опыта».\n' * 50
        for limit in (1, 2, 3, 6):
            queries = plan_queries(text, limit=limit, strategy=CITATION_STRATEGY)
            self.assertLessEqual(len(queries), limit)
            self.assertEqual(len(queries), len({q["query"] for q in queries}))
            for q in queries:
                self.assertEqual(" ".join(t.value for t in _tokens(text[q["start"]:q["end"]])), q["query"][1:-1])


class ParagraphCoverageTests(unittest.TestCase):
    def test_second_request_covers_another_paragraph_instead_of_the_opening_again(self):
        from plagiarism.web_search import COVERAGE_STRATEGY
        first = " ".join("характеристика%d" % i for i in range(70))
        second = " ".join("измерение%d" % i for i in range(25))
        text = first + "\n\n" + second
        queries = plan_queries(text, strategy=COVERAGE_STRATEGY)
        self.assertEqual(len(queries), 2)
        self.assertLess(queries[0]["start"], len(first))
        self.assertGreater(queries[1]["start"], len(first))

    def test_quotes_keep_priority_and_single_request_behavior_is_unchanged(self):
        from plagiarism.web_search import COVERAGE_STRATEGY, CITATION_STRATEGY
        quote = "Испытательная установка записывает изменение частоты сигнала после охлаждения образца"
        text = "Контекст редактора обсуждает старые записи измерений. " * 12 + "\n«" + quote + "»"
        for strategy in (COVERAGE_STRATEGY, CITATION_STRATEGY):
            self.assertTrue(any(q["start"] == text.index(quote) for q in plan_queries(text, strategy=strategy)))
        self.assertEqual(plan_queries(text, limit=1, strategy=COVERAGE_STRATEGY),
                         plan_queries(text, limit=1, strategy=CITATION_STRATEGY))

    def test_default_preserves_request_budget_and_grounded_offsets(self):
        for size in (6, 80, 81, 160, 400, 710):
            text = "\n\n".join(" ".join("слово%d" % i for i in range(start, min(start + 40, size)))
                                  for start in range(0, size, 40))
            for limit in (1, 2, 3, 6):
                queries = plan_queries(text, limit=limit)
                self.assertLessEqual(len(queries), min(limit, (size + 79) // 80))
                self.assertEqual(len(queries), len({q["query"] for q in queries}))
                for q in queries:
                    self.assertEqual(" ".join(t.value for t in _tokens(text[q["start"]:q["end"]])), q["query"][1:-1])


class RelatedPageTests(unittest.TestCase):
    def test_follows_one_relevant_link_and_counts_it_inside_page_budget(self):
        from plagiarism.web_search import CITATION_STRATEGY
        text = "Исследователь внимательно изучил старинные чертежи большой обсерватории."
        first, related = "https://source.test/start", "https://source.test/reference"
        class Search:
            def search(self, query, request_id):
                return [{"url": first}]
        class Pages:
            def __init__(self):
                self.calls = []
            def fetch(self, url):
                self.calls.append(url)
                return {"url": url, "text": text if url == related else "Описание здания содержит только сведения о площади помещений.",
                        "links": [{"url": related, "text": "Чертежи обсерватории"},
                                  {"url": "https://different.test/", "text": "Чертежи обсерватории"}]}
        pages = Pages()
        report = run(text, searcher=Search(), fetcher=pages, query_strategy=CITATION_STRATEGY)
        self.assertEqual(pages.calls, [first, related])
        self.assertEqual(report["web"]["urls_attempted"], 2)
        self.assertEqual(report["stats"]["matched_words"], len(_tokens(text)))
        self.assertEqual(report["web"]["link_discoveries"][0]["parent_url"], first)
        self.assertEqual(report["web"]["urls_discovered"], 2)
        self.assertEqual(report["web"]["search_urls_discovered"], 1)
        self.assertEqual(report["web"]["linked_urls_discovered"], 1)
        self.assertEqual(report["web"]["urls_found"], 2)

    def test_link_discovery_cannot_exceed_twelve_loads(self):
        from plagiarism.web_search import CITATION_STRATEGY
        class Search:
            def search(self, query, request_id):
                return [{"url": "https://source.test/page%d" % i} for i in range(12)]
        class Pages:
            def __init__(self):
                self.calls = []
            def fetch(self, url):
                self.calls.append(url)
                return {"url": url, "text": "На этой странице опубликованы сведения о часах работы.",
                        "links": [{"url": "https://source.test/extra", "text": "Обсерватории"}]}
        pages = Pages()
        report = run("Исследователь изучил старинные чертежи большой обсерватории перед поездкой.", searcher=Search(),
                     fetcher=pages, query_strategy=CITATION_STRATEGY)
        self.assertEqual(len(pages.calls), 12)
        self.assertEqual(report["web"]["link_discoveries"], [])

    def test_full_document_match_prevents_unnecessary_related_page_loads(self):
        from plagiarism.web_search import CITATION_STRATEGY
        text = "Исследователь внимательно изучил старинные чертежи большой обсерватории."
        class Search:
            def search(self, query, request_id):
                return [{"url": "https://source.test/original"}, {"url": "https://source.test/other"}]
        class Pages:
            def __init__(self):
                self.calls = []
            def fetch(self, url):
                self.calls.append(url)
                return {"url": url, "text": text if url.endswith("original") else "На странице приведены сведения о часах работы.",
                        "links": [{"url": "https://source.test/shop", "text": "Чертежи обсерватории"}]}
        pages = Pages()
        report = run(text, searcher=Search(), fetcher=pages, query_strategy=CITATION_STRATEGY)
        self.assertEqual(len(pages.calls), 2)
        self.assertEqual(report["web"]["link_discoveries"], [])
        self.assertEqual(report["stats"]["matched_words"], len(_tokens(text)))

    def test_hidden_and_navigation_links_are_excluded_from_source_candidates(self):
        from plagiarism.web_fetch import _TextExtractor
        parser = _TextExtractor()
        parser.feed('<nav><a href="/nav">Обсерватории</a></nav><a hidden href="/hidden">Чертежи</a>'
                    '<p><a href="/ref">Чертежи <b>обсерватории</b></a></p>')
        parser.close()
        self.assertEqual(parser.links, [{"url": "/ref", "text": "Чертежи обсерватории"}])


if __name__ == "__main__":
    unittest.main()
