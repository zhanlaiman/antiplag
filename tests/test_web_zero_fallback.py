"""Authored ZERO production evidence tests; no corpus, provider or real fetches."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from plagiarism import web_search as web
from scripts.zero_fallback_fixtures import FIRST_PAGE, SECOND_PAGE, UNRELATED_PAGE, two_insertion_fixture, page




class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value


class Search:
    provider = 'authored'
    def __init__(self, groups, clock, seconds=0):
        self.groups, self.clock, self.seconds, self.calls = groups, clock, seconds, []
    def search(self, query, request_id):
        index = len(self.calls)
        self.calls.append((query, request_id))
        self.clock.value += self.seconds
        return [{'url': url} for url in self.groups[index]]


class Pages:
    def __init__(self, bodies, clock, seconds=0, failures=(), unhandled=()):
        self.bodies, self.clock, self.seconds = bodies, clock, seconds
        self.failures, self.unhandled, self.calls = set(failures), set(unhandled), []
    def fetch(self, url):
        self.calls.append(url)
        self.clock.value += self.seconds
        if url in self.unhandled: raise TypeError('Unhandled authored fetch failure')
        if url in self.failures: raise RuntimeError('Handled authored fetch failure')
        return deepcopy(self.bodies[url])


def setup(initial_positive=False, links=False):
    fixture = two_insertion_fixture()
    text = fixture['text'] + '\n\nКоординатор заранее проверил освещение сцены, запасные кабели, крепление занавеса и удобство проходов между рядами кресел. Отдельная группа распределила цветные браслеты, подготовила указатели возле входа и согласовала подробную программу выступлений с ведущими.'
    assert len(web.plan_queries(text, strategy=web.DENSE_STRATEGY)) == 6
    urls = list(fixture['search_order'])
    groups = [urls[start:start + 5] for start in range(0, 30, 5)]
    full = web._rank_url_paths(web._fetch_order(urls, groups, web.PATH_URL_STRATEGY), text)
    bodies = {url: page(url, UNRELATED_PAGE) for url in urls}
    bodies[full[12]] = page(full[12], FIRST_PAGE)
    bodies[full[13]] = page(full[13], SECOND_PAGE)
    if initial_positive: bodies[full[0]] = page(full[0], FIRST_PAGE)
    if links:
        for index, url in enumerate(urls):
            linked = f'https://authored-zero.test/related-{index}'
            bodies[url]['links'] = [{'url': linked, 'text': 'городского фестиваля'}]
            bodies[linked] = page(linked, UNRELATED_PAGE)
    return text, groups, bodies, full


def run(module, text, groups, bodies, enabled=False, seconds=0, search_seconds=0, failures=(), unhandled=(), progress=None):
    clock = Clock()
    search = Search(groups, clock, search_seconds)
    pages = Pages(bodies, clock, seconds, failures, unhandled)
    kwargs = dict(searcher=search, fetcher=pages, query_strategy=module.DENSE_STRATEGY,
                  url_strategy=module.PATH_URL_STRATEGY, use_named_references=False, progress=progress)
    kwargs['use_zero_fallback'] = enabled
    with patch.object(module.time, 'monotonic', clock):
        result = module.run(text, 'authored-production-candidate', **kwargs)
    return result, search, pages


class WebZeroFallbackTests(unittest.TestCase):
    def setUp(self):
        provider = patch.object(web, 'selected_provider', return_value='tavily')
        credentials = patch.object(web, 'key_for', return_value='authored-not-real')
        provider.start()
        credentials.start()
        self.addCleanup(provider.stop)
        self.addCleanup(credentials.stop)

    def test_final_defaults_use_false_named_reference_then_twenty_four_pages_and_status_tracks_override(self):
        text, groups, bodies, _ = setup()
        text += '\n\n(Википедия, статья «Метрология»).'
        reference = 'https://ru.wikipedia.org/wiki/%D0%9C%D0%B5%D1%82%D1%80%D0%BE%D0%BB%D0%BE%D0%B3%D0%B8%D1%8F'
        bodies[reference] = page(reference, UNRELATED_PAGE, 'Authored false citation')
        clock = Clock()
        search, pages = Search(groups, clock), Pages(bodies, clock)
        with patch.dict(web.os.environ, clear=False), patch.object(web.time, 'monotonic', clock):
            for key in list(web.os.environ):
                if key.startswith('ISTOK_WEB_'):
                    web.os.environ.pop(key)
            # Isolate the primary zero-fallback cap; the combined public
            # 8-query/26-page defaults have separate integration coverage.
            web.os.environ['ISTOK_WEB_WIKI_FOLLOWUP'] = '0'
            report = web.run(text, request_id='authored-final-defaults', searcher=search, fetcher=pages)
            self.assertEqual(pages.calls[0], reference)
            self.assertEqual(len(pages.calls), 24)
            self.assertEqual(len(search.calls), 6)
            self.assertEqual(report['stats']['matched_words'], 18)
            self.assertEqual(report['web']['query_strategy'], web.DEFAULT_QUERY_STRATEGY)
            self.assertEqual(report['web']['url_strategy'], web.PATH_URL_STRATEGY)
            self.assertIs(report['web']['named_references_enabled'], True)
            self.assertIs(report['web']['zero_fallback_enabled'], True)
            self.assertEqual(report['web']['zero_fallback_initial_exact_words'], 0)
            self.assertEqual(report['web']['max_page_attempts'], 24)
            self.assertEqual(web.status()['max_urls'], 24)
            self.assertEqual(web.status()['max_initial_urls'], 12)
            for value, cap in (('0', 12), ('1', 24)):
                web.os.environ['ISTOK_WEB_ZERO_FALLBACK'] = value
                state = web.status()
                self.assertEqual(state['max_urls'], cap)
                self.assertEqual(state['max_initial_urls'], 12)
                self.assertIs(state['zero_fallback_enabled'], value == '1')

    def test_explicit_false_and_true_preserve_initial_prefix_sources_and_queries(self):
        text, groups, bodies, _ = setup(links=True)
        before, a_search, a_pages = run(web, text, groups, bodies, enabled=False)
        after, b_search, b_pages = run(web, text, groups, bodies, enabled=True)
        self.assertEqual(len(a_pages.calls), 12)
        self.assertEqual(len(b_pages.calls), 24)
        self.assertEqual(b_pages.calls[:12], a_pages.calls)
        self.assertEqual(after['sources'][:len(before['sources'])], before['sources'])
        self.assertEqual(a_search.calls, b_search.calls)
        for key in ('query_strategy', 'url_strategy', 'matcher_version', 'queries_planned',
                    'queries_attempted', 'queries_completed', 'queries_unattempted', 'query_spans',
                    'named_references_enabled', 'input_references', 'link_discoveries',
                    'priority_link_replacements', 'source_followup_policy', 'priority_link_budget_remaining'):
            self.assertEqual(after['web'][key], before['web'][key], key)
        self.assertFalse(before['web']['zero_fallback_enabled'])
        self.assertTrue(after['web']['zero_fallback_enabled'])
        self.assertEqual(before['web']['max_page_attempts'], 12)
        self.assertEqual(after['web']['max_page_attempts'], 24)

    def test_zero_appends_twelve_search_pages_and_grounded_both_insertions(self):
        text, groups, bodies, full = setup()
        before, _, baseline_pages = run(web, text, groups, bodies, enabled=False)
        report, search, pages = run(web, text, groups, bodies, enabled=True)
        self.assertEqual(len(search.calls), 6)
        self.assertEqual(pages.calls[:12], baseline_pages.calls)
        self.assertEqual(pages.calls[12:], full[12:24])
        self.assertEqual(report['sources'][:12], before['sources'])
        self.assertEqual(report['stats']['matched_words'], 18)
        self.assertEqual(report['web']['urls_attempted'], 24)
        self.assertEqual(len(report['web']['attempted_urls']), 24)
        self.assertEqual(report['web']['zero_fallback_extra_attempts'], 12)
        self.assertEqual(report['web']['urls_outside_limit'], 2)
        self.assertEqual(report['web']['skipped_urls'], full[24:])
        self.assertEqual([error['count'] for error in report['web']['errors'] if error['code'] == 'url_limit_reached'], [2])
        self.assertEqual(report['web']['max_page_attempts'], 24)
        self.assertIn('Основной поиск ограничен шестью', report['web']['limitations'][1])

    def test_positive_prefix_keeps_legacy_twelve_and_second_insertion_unread(self):
        text, groups, bodies, full = setup(initial_positive=True)
        report, search, pages = run(web, text, groups, bodies, enabled=True)
        self.assertEqual(len(search.calls), 6)
        self.assertEqual(len(pages.calls), 12)
        self.assertEqual(report['stats']['matched_words'], 9)
        self.assertFalse(report['web']['zero_fallback_triggered'])
        self.assertEqual(report['web']['zero_fallback_initial_exact_words'], 9)
        self.assertNotIn(full[13], pages.calls)

    def test_productive_link_continue_and_displaced_tail_keep_legacy_prefix_then_search_only(self):
        text, groups, bodies, full = setup(links=True)
        before, _, old_pages = run(web, text, groups, bodies, enabled=False)
        report, search, pages = run(web, text, groups, bodies, enabled=True)
        self.assertEqual(pages.calls[:12], old_pages.calls)
        self.assertEqual(report['sources'][:12], before['sources'])
        self.assertEqual(len([url for url in old_pages.calls if '/related-' in url]), 1)
        remaining = [url for url in full if url not in old_pages.calls]
        self.assertEqual(pages.calls[12:], remaining[:12])
        self.assertTrue(all(url in full for url in pages.calls[12:]))
        self.assertEqual(len(search.calls), 6)
        self.assertEqual(len(pages.calls), 24)

    def test_handled_last_fetch_failure_still_opens_extra_phase(self):
        text, groups, bodies, full = setup()
        report, _, pages = run(web, text, groups, bodies, enabled=True, failures=(full[11],))
        self.assertEqual(len(pages.calls), 24)
        self.assertTrue(report['web']['zero_fallback_triggered'])
        self.assertEqual(report['stats']['matched_words'], 18)

    def test_final_aliases_from_prefix_or_extra_are_not_requested_again(self):
        for redirect_in_prefix in (False, True):
            with self.subTest(redirect_in_prefix=redirect_in_prefix):
                text, groups, bodies, full = setup()
                requested = full[0] if redirect_in_prefix else full[12]
                final_alias = full[12] if redirect_in_prefix else full[13]
                bodies[requested]['url'] = final_alias
                report, _, pages = run(web, text, groups, bodies, enabled=True)
                self.assertNotIn(final_alias, pages.calls[12:])
                self.assertEqual(len(pages.calls), 24)
                self.assertEqual(report['stats']['matched_words'], 9)
                self.assertEqual(report['web']['urls_outside_limit'], 1)
                self.assertNotIn(final_alias, report['web']['skipped_urls'])

    def test_all_twenty_four_failed_attempts_emit_no_sources_once_and_null_uniqueness(self):
        text, groups, bodies, full = setup()
        report, search, pages = run(web, text, groups, bodies, enabled=True, failures=full)
        self.assertEqual(len(search.calls), 6)
        self.assertEqual(len(pages.calls), 24)
        self.assertEqual(report['sources'], [])
        self.assertEqual(report['stats']['matched_words'], 0)
        self.assertIsNone(report['stats']['uniqueness_percent'])
        self.assertEqual(sum(error['code'] == 'no_sources_checked' for error in report['web']['errors']), 1)
        self.assertEqual(sum(error['stage'] == 'fetch' for error in report['web']['errors']), 24)
        self.assertEqual(report['web']['urls_outside_limit'], 2)

    def test_optional_pdf_snapshots_keep_the_original_prefix_and_gate_requires_primary_support(self):
        for primary_support in (False, True):
            with self.subTest(primary_support=primary_support):
                text, groups, bodies, full = setup()
                primary = FIRST_PAGE.replace('Астроном тщательно настроил телескоп перед наблюдением далёкого звёздного скопления',
                    'Астрономтщательнонастроилтелескоппереднаблюдениемдалёкогозвёздногоскопления') if primary_support else UNRELATED_PAGE
                fetched = page(full[0], primary)
                raw_hash = 'a' * 64
                fetched.update(content_type='application/pdf', pdf_pages=1, raw_pdf_sha256=raw_hash,
                    supplemental_texts=[{'id': 'pdfium-v1', 'parser_version': '5.14.0', 'extraction': 'pdfium-text-v1',
                        'text': FIRST_PAGE, 'raw_pdf_sha256': raw_hash, 'page_count': 1,
                        'text_sha256': sha256(FIRST_PAGE.encode()).hexdigest()}],
                    optional_status={'engine': 'pdfium', 'status': 'complete'})
                bodies[full[0]] = fetched
                before, _, old_pages = run(web, text, groups, bodies, enabled=False)
                report, _, pages = run(web, text, groups, bodies, enabled=True)
                self.assertEqual(pages.calls[:12], old_pages.calls)
                self.assertEqual(report['sources'][:len(before['sources'])], before['sources'])
                self.assertEqual(report['web']['supplemental_text_views'], 1)
                self.assertEqual(report['web']['zero_fallback_initial_exact_words'], 9 if primary_support else 0)
                self.assertEqual(len(pages.calls), 12 if primary_support else 24)
                self.assertEqual(report['stats']['matched_words'], 9 if primary_support else 18)

    def test_unhandled_last_fetch_error_cannot_open_extra_phase(self):
        text, groups, bodies, full = setup()
        clock = Clock()
        search = Search(groups, clock)
        pages = Pages(bodies, clock, unhandled=(full[11],))
        with patch.object(web.time, 'monotonic', clock), self.assertRaises(TypeError):
            web.run(text, searcher=search, fetcher=pages, query_strategy=web.DENSE_STRATEGY,
                          url_strategy=web.PATH_URL_STRATEGY, use_named_references=False, use_zero_fallback=True)
        self.assertEqual(len(pages.calls), 12)

    def test_initial_failures_do_not_emit_no_sources_before_extra_evidence(self):
        text, groups, bodies, full = setup()
        report, _, pages = run(web, text, groups, bodies, enabled=True, failures=full[:12])
        self.assertEqual(len(pages.calls), 24)
        self.assertEqual(report['stats']['matched_words'], 18)
        self.assertFalse(any(error['code'] == 'no_sources_checked' for error in report['web']['errors']))

    def test_deadline_never_trims_initial_twelve_and_requires_twenty_seconds_headroom_for_extras(self):
        text, groups, bodies, _ = setup()
        report, _, pages = run(web, text, groups, bodies, enabled=True, seconds=20, search_seconds=10)
        self.assertEqual(len(pages.calls), 12)
        self.assertTrue(report['web']['zero_fallback_deadline_reached'])
        self.assertEqual(report['web']['zero_fallback_extra_attempts'], 0)
        self.assertEqual(report['web']['urls_outside_limit'], 14)
        report, _, pages = run(web, text, groups, bodies, enabled=True, seconds=20)
        self.assertEqual(len(pages.calls), 15)
        self.assertEqual(report['web']['zero_fallback_extra_attempts'], 3)
        self.assertEqual(report['web']['urls_outside_limit'], 11)
        self.assertEqual(report['web']['elapsed_seconds'], 300)

    def test_strict_boolean_option_and_environment_override(self):
        text, groups, bodies, _ = setup()
        for flag in (0, 1, '1', [], {}):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                run(web, text, groups, bodies, enabled=flag)
        with patch.dict(web.os.environ, {'ISTOK_WEB_ZERO_FALLBACK': '1'}):
            report, _, pages = run(web, text, groups, bodies, enabled=None)
        self.assertEqual(len(pages.calls), 24)
        self.assertTrue(report['web']['zero_fallback_enabled'])
        with patch.dict(web.os.environ, {'ISTOK_WEB_ZERO_FALLBACK': 'invalid'}), self.assertRaises(ValueError):
            run(web, text, groups, bodies, enabled=None)


if __name__ == '__main__':
    unittest.main()
