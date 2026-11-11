from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from plagiarism import web_search


TEXT = 'Редактор сравнил четыре зелёных листа и записал форму каждой жилки. Затем он описал необычный узор.'


class Search:
    def __init__(self, results=None, fail=False):
        self.results = results if results is not None else [{'url': 'https://example.org/article'}]
        self.fail = fail
        self.calls = []

    def search(self, query, request_id):
        self.calls.append((query, request_id))
        if self.fail:
            raise web_search.WebSearchError('unavailable', 'search_rate_limited')
        return self.results


class Pages:
    def __init__(self, fail=(), text=TEXT):
        self.fail, self.text, self.calls = set(fail), text, []

    def fetch(self, url):
        self.calls.append(url)
        if url in self.fail:
            raise RuntimeError('blocked')
        return {'url': url, 'title': 'Действительно прочитанная публикация', 'text': self.text,
                'fetched_at': '2026-11-11T14:00:00Z'}


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        # Every test uses fake credentials/provider selection; never read an operator key.
        self.provider = patch.object(web_search, 'selected_provider', return_value='brave')
        self.keys = patch.object(web_search, 'key_for', return_value='')
        self.provider.start()
        self.keys.start()
        self.addCleanup(self.provider.stop)
        self.addCleanup(self.keys.stop)

    def test_timeout_and_site_denial_have_distinct_public_messages(self):
        from plagiarism.web_fetch import FetchError
        for code, message in (("fetch_timeout", "Сайт не ответил"), ("robots_denied", "Сайт запрещает")):
            pages = Mock()
            pages.fetch.side_effect = FetchError("internal detail", code)
            report = web_search.run(TEXT, searcher=Search(), fetcher=pages)
            error = next(e for e in report["web"]["errors"] if e["stage"] == "fetch")
            self.assertEqual(error["code"], code)
            self.assertIn(message, error["message"])
            self.assertNotIn("internal detail", error["message"])
            self.assertIsNone(report["stats"]["uniqueness_percent"])

    def test_full_copy_has_zero_word_uniqueness_including_with_quote_formatting(self):
        for text in (TEXT, '«' + TEXT + '»'):
            report = web_search.run(text, searcher=Search(), fetcher=Pages())
            self.assertEqual(report['stats']['uniqueness_percent'], 0)
            self.assertEqual(report['web']['status'], 'complete')
            self.assertEqual(report['scope'], 'web_search')
            self.assertTrue(report['matches'])
            self.assertFalse(report['sources'][0]['provenance']['training_consent'])

    def test_duplicate_sources_do_not_double_count_words(self):
        found = [{'url': 'https://example.org/one'}, {'url': 'https://other.example.org/two'}]
        report = web_search.run(TEXT, searcher=Search(found), fetcher=Pages())
        self.assertEqual(report['stats']['matched_words'], report['stats']['total_words'])
        self.assertEqual(report['stats']['matched_word_percent'], 100)

    def test_snippet_is_never_used_as_evidence_and_blocked_page_does_not_claim_100_percent(self):
        url = 'https://example.org/article'
        report = web_search.run(TEXT, searcher=Search([{'url': url, 'description': TEXT}]), fetcher=Pages(fail=[url]))
        self.assertEqual(report['matches'], [])
        self.assertIsNone(report['stats']['uniqueness_percent'])
        self.assertEqual(report['web']['status'], 'partial')
        self.assertTrue(report['web']['errors'])

    def test_partial_failure_keeps_real_matches_but_no_final_uniqueness(self):
        bad = 'https://other.example.org/blocked'
        report = web_search.run(TEXT, searcher=Search([{'url': 'https://example.org/article'}, {'url': bad}]), fetcher=Pages(fail=[bad]))
        self.assertTrue(report['matches'])
        self.assertIsNone(report['stats']['uniqueness_percent'])
        self.assertEqual(report['web']['sources_checked'], 1)
        self.assertEqual(report['stats']['uniqueness_upper_bound_percent'], 0)

    def test_failed_search_is_not_reported_as_success_and_does_not_fetch_pages(self):
        pages = Pages()
        report = web_search.run(TEXT, searcher=Search(fail=True), fetcher=pages)
        self.assertIsNone(report['stats']['uniqueness_percent'])
        self.assertEqual(report['web']['queries_completed'], 0)
        self.assertFalse(pages.calls)

    def test_successful_empty_search_is_distinct_from_provider_failure(self):
        report = web_search.run(TEXT, searcher=Search([]), fetcher=Pages())
        self.assertEqual(report['web']['status'], 'partial')
        self.assertIsNone(report['stats']['uniqueness_percent'])
        self.assertEqual(report['web']['queries_completed'], 1)
        self.assertEqual(report['web']['sources_checked'], 0)
        self.assertEqual([error['code'] for error in report['web']['errors']], ['no_sources_checked'])
        self.assertEqual(report['web']['urls_found'], 0)
        self.assertIn('не ко всему интернету', report['web']['limitations'][0])

    def test_exact_word_coverage_survives_larger_fuzzy_candidate(self):
        words = [f'term{number:03}' for number in range(20)]
        text = ' '.join(words) + '.'
        source = words.copy()
        source[6], source[14] = 'changed006', 'changed014'
        report = web_search.run(text, searcher=Search(), fetcher=Pages(text=' '.join(source) + '.'))
        self.assertEqual(report['stats']['matched_words'], 18)
        self.assertEqual(report['stats']['total_words'], 20)
        self.assertEqual(report['stats']['uniqueness_percent'], 10)
        self.assertEqual(sum(match['kind'] == 'exact' for match in report['matches']), 3)
        self.assertTrue(any(match['kind'] == 'similar' for match in report['matches']))

    def test_partial_upper_bound_rounds_up_from_integer_counts_even_near_zero(self):
        text, source = ' '.join(['aa'] * 1999 + ['bb']), ' '.join(['aa'] * 1999)
        bad = 'https://other.example.org/blocked'
        report = web_search.run(text, searcher=Search([{'url': 'https://example.org/article'}, {'url': bad}]),
                                fetcher=Pages(fail=[bad], text=source))
        self.assertEqual(len(text), 5999)
        self.assertEqual(report['stats']['matched_words'], 1999)
        self.assertEqual(report['stats']['total_words'], 2000)
        self.assertEqual(report['stats']['matched_word_percent'], 100.0)
        self.assertIsNone(report['stats']['uniqueness_percent'])
        self.assertEqual(report['stats']['uniqueness_upper_bound_percent'], 0.1)

    def test_complete_uniqueness_uses_raw_counts_not_rounded_coverage(self):
        text, source = ' '.join(['aa'] * 1999 + ['bb']), ' '.join(['aa'] * 1999)
        report = web_search.run(text, searcher=Search(), fetcher=Pages(text=source))
        self.assertEqual(report['stats']['matched_word_percent'], 100.0)
        self.assertEqual(report['stats']['uniqueness_percent'], 0.1)

    def test_unicode_query_positions_and_bounded_query_count_are_real_input_offsets(self):
        text = '😀 ' + ' '.join('растение' + str(number) for number in range(1000))
        queries = web_search.plan_queries(text, strategy=web_search.QUERY_STRATEGY)
        self.assertEqual(len(queries), 6)
        self.assertEqual(queries[0]['start'], 2)
        self.assertIn('растение0', text[queries[0]['start']:queries[0]['end']])
        self.assertEqual(queries[-1]['end'], len(text))
        self.assertTrue(all(len(item['query']) <= 400 for item in queries))

    def test_fetch_cap_is_bounded_and_domains_are_interleaved(self):
        results = [{'url': f'https://example.org/{number}'} for number in range(15)] + [{'url': 'https://second.example.org/one'}]
        pages = Pages()
        report = web_search.run(TEXT, searcher=Search(results), fetcher=pages)
        self.assertEqual(len(pages.calls), 12)
        self.assertEqual(pages.calls[1], 'https://second.example.org/one')
        self.assertEqual(report['web']['urls_outside_limit'], 4)
        self.assertEqual(report['web']['status'], 'partial')
        self.assertIsNone(report['stats']['uniqueness_percent'])

    def test_unvisited_discovered_copy_does_not_claim_100_percent_uniqueness(self):
        text = ' '.join('лист' + str(number) for number in range(600))
        urls = [{'url': f'https://source{number}.org/article'} for number in range(13)]
        # The balanced queue keeps early domains and first results from later
        # queries; the fifth result of query two remains outside twelve loads.
        unvisited = urls[9]['url']

        class Batches(Search):
            def search(self, query, request_id):
                index = len(self.calls)
                self.calls.append((query, request_id))
                return urls[index * 5:(index + 1) * 5]

        class Corpus(Pages):
            def fetch(self, url):
                self.calls.append(url)
                return {'url': url, 'text': text if url == unvisited else
                        'Другой материал посвящён морским глубинам, кораблям и холодным течениям.'}

        pages = Corpus()
        report = web_search.run(text, searcher=Batches(), fetcher=pages)
        self.assertEqual(len(pages.calls), 12)
        self.assertNotIn(unvisited, pages.calls)
        self.assertEqual(report['web']['status'], 'partial')
        self.assertIsNone(report['stats']['uniqueness_percent'])
        self.assertNotIn('uniqueness_upper_bound_percent', report['stats'])
        self.assertEqual(report['web']['urls_outside_limit'], 1)
        self.assertIn(unvisited, report['web']['discovered_urls'])
        self.assertIn(unvisited, report['web']['skipped_urls'])
        self.assertNotIn(unvisited, report['web']['attempted_urls'])
        self.assertEqual(report['web']['attempted_urls'], pages.calls)
        self.assertLessEqual(len(report['web']['discovered_urls']), 30)
        self.assertLessEqual(len(report['web']['attempted_urls']), 12)
        self.assertEqual(report['web']['query_strategy'], web_search.DEFAULT_QUERY_STRATEGY)
        self.assertEqual(report['web']['matcher_version'], web_search.MATCHER_VERSION)
        self.assertEqual(report['web']['default_fetch_limits']['max_response_bytes'], 1024 * 1024)
        self.assertTrue(any(error['code'] == 'url_limit_reached' for error in report['web']['errors']))

    def test_unsupported_discovered_urls_are_counted_and_prevent_final_uniqueness(self):
        pages = Pages()
        report = web_search.run(TEXT, searcher=Search([{'url': 'ftp://example.org/article'},
                                                      {'url': 'https://secret:password@example.org/private?token=private'}]), fetcher=pages)
        self.assertFalse(pages.calls)
        self.assertEqual(report['web']['urls_found'], 2)
        self.assertEqual(report['web']['urls_supported'], 0)
        self.assertEqual(len(report['web']['urls_rejected']), 2)
        self.assertEqual(report['web']['status'], 'partial')
        self.assertIsNone(report['stats']['uniqueness_percent'])
        self.assertNotIn('password', json.dumps(report['web']))
        self.assertNotIn('token=private', json.dumps(report['web']))

    def test_rate_limit_and_budget_stop_without_spamming_or_claiming_unattempted_queries_complete(self):
        text = ' '.join('лист' + str(number) for number in range(600))
        for code in ('search_rate_limited', 'search_budget_exhausted'):
            class Blocked(Search):
                def search(self, query, request_id):
                    self.calls.append((query, request_id))
                    raise web_search.WebSearchError('blocked', code)
            search = Blocked()
            report = web_search.run(text, searcher=search, fetcher=Pages())
            self.assertEqual(len(search.calls), 1)
            self.assertEqual(report['web']['queries_planned'], 6)
            self.assertEqual(report['web']['queries_attempted'], 1)
            self.assertEqual(report['web']['queries_completed'], 0)
            self.assertEqual(report['web']['queries_unattempted'], 5)
            self.assertEqual(report['web']['query_stop_code'], code)
            self.assertIsNone(report['stats']['uniqueness_percent'])

    def test_invalid_first_alias_does_not_discard_valid_later_evidence(self):
        found = [{'url': 'https://example.org/go'}, {'url': 'https://example.org/article'}]

        class AliasPages(Pages):
            def fetch(self, url):
                self.calls.append(url)
                return {'url': 'https://example.org/article', 'text': 'short refusal' if len(self.calls) == 1 else TEXT}

        report = web_search.run(TEXT, searcher=Search(found), fetcher=AliasPages())
        self.assertTrue(report['matches'])
        self.assertEqual(report['web']['sources_checked'], 1)
        self.assertEqual(report['web']['status'], 'partial')
        self.assertIsNone(report['stats']['uniqueness_percent'])

    def test_known_final_url_is_not_fetched_again_and_standard_port_aliases_are_canonical(self):
        found = [{'url': 'https://example.org/go'}, {'url': 'https://EXAMPLE.ORG.:443/article'},
                 {'url': 'https://example.org/article'}]

        class AliasPages(Pages):
            def fetch(self, url):
                self.calls.append(url)
                return {'url': 'https://example.org/article', 'text': TEXT}

        pages = AliasPages()
        report = web_search.run(TEXT, searcher=Search(found), fetcher=pages)
        self.assertEqual(pages.calls, ['https://example.org/go'])
        self.assertEqual(report['web']['urls_attempted'], 1)
        self.assertEqual(report['web']['urls_deduplicated'], 2)
        self.assertEqual(report['web']['sources_checked'], 1)
        self.assertEqual(report['web']['status'], 'complete')
        self.assertEqual(web_search._canonical_url('http://example.org:80/a?id=2&tag=one'), 'http://example.org/a?id=2&tag=one')
        self.assertEqual(web_search._canonical_url('https://example.org/a?id=2&tag=one'), 'https://example.org/a?id=2&tag=one')

    def test_brave_200_error_and_invalid_schema_are_errors_not_successful_empty_results(self):
        with patch.object(web_search, 'key_for', return_value='fixture-brave-private-key'), \
                patch.dict(os.environ, {'ISTOK_BRAVE_STORAGE_RIGHTS': '1'}):
            provider = web_search.BraveSearch(budget=Mock())
        for payload in ({'error': {'message': 'failed'}}, {}, {'web': None},
                        {'web': {'results': 'bad'}}, {'web': {'results': [{'title': 'missing URL'}]}}):
            response = Mock()
            response.read.return_value = json.dumps(payload).encode()
            provider.opener = Mock()
            provider.opener.open.return_value.__enter__ = Mock(return_value=response)
            provider.opener.open.return_value.__exit__ = Mock(return_value=None)
            with self.assertRaises(web_search.WebSearchError) as caught:
                provider.search('"учебные слова"', 'fixture-request')
            self.assertEqual(caught.exception.code, 'search_provider_error')
            provider.budget.finish.assert_not_called()
        response.read.return_value = b'{"web":{"results":[]}}'
        self.assertEqual(provider.search('"учебные слова"', 'other-request'), [])
        provider.budget.finish.assert_called_once_with('other-request')

    def test_provider_status_tavily_needs_key_brave_also_needs_storage_rights_and_invalid_never_falls_back(self):
        with patch.object(web_search, 'key_for', return_value='fixture-key'), \
                patch.dict(os.environ, {'ISTOK_BRAVE_STORAGE_RIGHTS': '0'}):
            with patch.object(web_search, 'selected_provider', return_value='tavily'):
                self.assertTrue(web_search.status()['configured'])
                self.assertEqual(web_search.status()['provider'], 'tavily')
            with patch.object(web_search, 'selected_provider', return_value='brave'):
                self.assertFalse(web_search.status()['configured'])
                with patch.dict(os.environ, {'ISTOK_BRAVE_STORAGE_RIGHTS': '1'}):
                    self.assertTrue(web_search.status()['configured'])
            with patch.object(web_search, 'selected_provider', return_value='unavailable'), \
                    patch.object(web_search, 'BraveSearch') as brave:
                self.assertFalse(web_search.status()['configured'])
                with self.assertRaises(web_search.WebSearchError) as caught:
                    web_search.run(TEXT, fetcher=Pages())
                self.assertEqual(caught.exception.code, 'web_not_configured')
                brave.assert_not_called()

    def test_run_factory_selects_tavily_at_runtime_with_budget_and_reports_real_provider(self):
        search = Search([])
        search.provider = 'tavily'
        budget = Mock()
        with patch.object(web_search, 'selected_provider', return_value='tavily'), \
                patch.object(web_search, 'SearchBudget', return_value=budget), \
                patch('plagiarism.search_tavily.TavilySearch', return_value=search) as tavily, \
                patch.object(web_search, 'BraveSearch') as brave:
            report = web_search.run(TEXT, fetcher=Pages())
        tavily.assert_called_once_with(budget)
        brave.assert_not_called()
        self.assertEqual(report['web']['provider'], 'tavily')

    def test_run_factory_preserves_explicit_brave_and_does_not_switch_to_tavily(self):
        search = Search([])
        search.provider = 'brave'
        with patch.object(web_search, 'selected_provider', return_value='brave'), \
                patch.object(web_search, 'BraveSearch', return_value=search) as brave, \
                patch('plagiarism.search_tavily.TavilySearch') as tavily:
            report = web_search.run(TEXT, fetcher=Pages())
        brave.assert_called_once_with()
        tavily.assert_not_called()
        self.assertEqual(report['web']['provider'], 'brave')

    def test_attempt_budget_is_atomic_and_replay_never_calls_provider_again(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = web_search.SearchBudget(Path(directory) / 'budget.sqlite3', limit=1)
            def reserve(identifier):
                try:
                    budget.reserve(identifier)
                    return 'allowed'
                except web_search.WebSearchError as error:
                    return error.code
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(reserve, ['a', 'b']))
            self.assertEqual(outcomes.count('allowed'), 1)
            self.assertEqual(outcomes.count('search_budget_exhausted'), 1)
            winner = 'a' if outcomes[0] == 'allowed' else 'b'
            self.assertEqual(reserve(winner), 'search_already_attempted')

    def test_attempts_count_against_the_month_even_when_finish_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            month = [datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc)]
            budget = web_search.SearchBudget(Path(directory) / 'budget.sqlite3', limit=1, now=lambda: month[0])
            budget.reserve('uncertain')
            with self.assertRaises(web_search.WebSearchError):
                budget.reserve('next')
            month[0] = datetime(2026, 12, 1, tzinfo=timezone.utc)
            budget.reserve('new-month')


if __name__ == '__main__':
    unittest.main()
