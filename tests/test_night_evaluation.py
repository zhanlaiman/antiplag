from copy import deepcopy
from contextlib import ExitStack
from hashlib import sha256
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from urllib.request import Request

from plagiarism.web_search import run
from scripts.night_evaluation import select_prefix
from scripts import night_evaluation as driver
from scripts.night_evaluation_diagnostics import query_profile
from scripts.night_evaluation_io import (CachedSearch, EvaluationStopped, RecordingOpener,
    digest, read_hashed, redact_response, verify_corpus, write_bytes, write_hashed, write_json)
from scripts.night_evaluation_score import aggregate, query_coverage, score_case


class TemporaryEvidence(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


class SelectionTests(unittest.TestCase):
    def fixtures(self, sizes):
        cases, order, plans = [], [], {}
        for family, size in enumerate(sizes):
            for length in [0, 6, 9, 12, 20]:
                cid = f'F{family}-{length}'
                cases.append({'id': cid, 'paired_group_id': f'F{family}', 'expected_insertion_words': length})
                order.append(cid)
                plans[cid] = [{'query': f'{family}-phrase-{q}'} for q in range(size)]
        return cases, order, plans, dict.fromkeys(order, 0)

    def test_maximal_prefix_does_not_skip_an_expensive_family(self):
        args = self.fixtures([3, 2, 1])
        result = select_prefix(*args, cap=4)
        self.assertEqual(result['families'], ['F0'])
        self.assertEqual(len(result['case_ids']), 5)
        self.assertEqual(result['physical_upper'], 3)
        self.assertEqual(result['prefix_bounds'][-1]['prefix_physical_upper'], 5)

    def test_original_relative_order_is_preserved(self):
        cases, order, plans, adaptive = self.fixtures([1, 1])
        order = order[::2] + order[1::2]
        result = select_prefix(cases, order, plans, adaptive, 2)
        self.assertEqual(result['case_ids'], order)
        self.assertEqual(result['physical_upper'], 2)

    def test_dynamic_slots_are_reserved_before_selection(self):
        cases, order, plans, adaptive = self.fixtures([1])
        adaptive = dict.fromkeys(order, 1)
        self.assertEqual(select_prefix(cases, order, plans, adaptive, 5)['case_ids'], [])
        result = select_prefix(cases, order, plans, adaptive, 6)
        self.assertEqual(result['physical_upper'], 6)

    def test_shared_identical_phrases_are_counted_once(self):
        cases, order, plans, adaptive = self.fixtures([2, 2])
        for cid in order:
            plans[cid] = [{'query': 'common-a'}, {'query': 'common-b'}]
        result = select_prefix(cases, order, plans, adaptive, 2)
        self.assertEqual(len(result['case_ids']), 10)
        self.assertEqual(result['physical_upper'], 2)

    def test_incomplete_family_and_duplicate_order_fail(self):
        cases, order, plans, adaptive = self.fixtures([1])
        with self.assertRaises(ValueError):
            select_prefix(cases[:-1], order[:-1], plans, adaptive, 10)
        with self.assertRaises(ValueError):
            select_prefix(cases, order[:-1] + [order[0]], plans, adaptive, 10)


class FakeClient:
    def __init__(self):
        self.calls, self.quota_checks, self.last_usage, self.fail = [], [], None, False

    def confirm_free(self, needed):
        self.quota_checks.append(needed)
        return {'plan_name': 'Free Plan', 'plan_monthly_price': 0, 'total_searches_left': 100}

    def search(self, query, request_id):
        self.calls.append((query, request_id))
        if self.fail:
            raise TimeoutError('synthetic uncertain transport')
        self.last_usage = {'credits': 1}
        return [{'url': 'https://example.org/result', 'title': 'Fixture result'}]


class TransportTests(TemporaryEvidence):
    def test_identical_phrase_reuses_first_response_and_detaches_mutation(self):
        client = FakeClient()
        cached = CachedSearch(client, self.root, ['phrase'], 36)
        first = cached.search('phrase', 'first')
        first[0]['title'] = 'consumer mutation'
        second = cached.search('phrase', 'second')
        self.assertEqual(second[0]['title'], 'Fixture result')
        self.assertEqual(client.calls, [('phrase', 'first')])
        self.assertEqual(client.quota_checks, [1])
        self.assertEqual((cached.physical, cached.hits, len(cached.logical)), (1, 1, 2))
        self.assertEqual(cached.last_usage, {'credits': 0})

    def test_unknown_first_delivery_is_not_retried_on_resume(self):
        client = FakeClient()
        client.fail = True
        cached = CachedSearch(client, self.root, ['phrase'], 36)
        with self.assertRaises(EvaluationStopped):
            cached.search('phrase', 'first')
        resumed = CachedSearch(client, self.root, ['phrase'], 36)
        with self.assertRaises(EvaluationStopped):
            resumed.search('phrase', 'new-request-id')
        self.assertEqual(client.calls, [('phrase', 'first')])
        self.assertEqual(len(list((self.root / 'physical').glob('*/intent.json'))), 1)
        self.assertEqual(resumed.physical, 1)

    def test_37th_physical_call_is_blocked_before_quota_or_transport(self):
        client = FakeClient()
        queries = [f'query-{n}' for n in range(37)]
        cached = CachedSearch(client, self.root, queries, 36)
        for n, query in enumerate(queries[:36]):
            cached.search(query, str(n))
        with self.assertRaises(EvaluationStopped):
            cached.search(queries[-1], 'thirty-seven')
        self.assertEqual(len(client.calls), 36)
        self.assertEqual(len(client.quota_checks), 36)

    def test_unplanned_query_cannot_open_transport(self):
        client = FakeClient()
        cached = CachedSearch(client, self.root, ['allowed'], 36)
        with self.assertRaises(EvaluationStopped):
            cached.search('not-frozen', 'bad')
        self.assertEqual(client.calls, [])
        self.assertEqual(client.quota_checks, [])

    def test_response_cache_tampering_is_detected(self):
        client = FakeClient()
        cached = CachedSearch(client, self.root, ['phrase'], 36)
        cached.search('phrase', 'first')
        completion = next((self.root / 'search-cache').glob('*.complete'))
        completion.write_text('{}')
        with self.assertRaises(EvaluationStopped):
            cached.search('phrase', 'second')
        self.assertEqual(len(client.calls), 1)

    def test_append_only_evidence_cannot_replace_first_report(self):
        path = self.root / 'report.json'
        write_hashed(path, {'first': True})
        with self.assertRaises(FileExistsError):
            write_hashed(path, {'first': False})
        self.assertEqual(read_hashed(path), {'first': True})

    def test_raw_response_is_captured_but_credentials_are_removed(self):
        secret = 'synthetic-credential-never-real'
        raw = json.dumps({'organic_results': [], 'api_key': secret,
                          'nested': {'url': 'https://example.org/?api_key=' + secret}}).encode()

        class Response:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self, maximum): return raw

        class Opener:
            def open(self, *args, **kwargs): return Response()

        recorder = RecordingOpener(Opener(), secret)
        recorder.destination = self.root
        with recorder.open(Request('https://serpapi.com/search.json')) as response:
            self.assertEqual(response.read(10000), raw)
        stored = (self.root / 'response.raw').read_text()
        self.assertNotIn(secret, stored)
        meta = json.loads((self.root / 'response-metadata.json').read_text())
        self.assertTrue(meta['redacted'])
        self.assertEqual(meta['raw_response_sha256'], sha256(raw).hexdigest())

    def test_non_sensitive_raw_response_preserves_bytes(self):
        raw = b'{ "results": [] }\n'
        self.assertEqual(redact_response(raw, 'unrelated-secret'), (raw, False))

    def test_corpus_inventory_rejects_payload_tampering(self):
        data = b'fixture-only'
        write_bytes(self.root / 'input.txt', data)
        checksum = sha256(data).hexdigest()
        canonical = f'{checksum}  input.txt\n'
        frozen = {'payload_files': [{'path': 'input.txt', 'bytes': len(data), 'sha256': checksum}],
                  'payload_sha256': sha256(canonical.encode()).hexdigest()}
        raw = (json.dumps(frozen) + '\n').encode()
        write_bytes(self.root / 'freeze.json', raw)
        write_bytes(self.root / 'SHA256SUMS', (canonical + sha256(raw).hexdigest() + '  freeze.json\n').encode())
        self.assertEqual(verify_corpus(self.root)['files_verified'], 2)
        (self.root / 'input.txt').write_text('different')
        with self.assertRaises(ValueError):
            verify_corpus(self.root)


class ResumeTests(TemporaryEvidence):
    def fixture(self, remaining=False):
        out, corpus = self.root / 'out', self.root / 'corpus'
        ids = ['saved', 'remaining'] if remaining else ['saved']
        write_json(corpus / 'manifest.json', {'cases': [
            {'id': cid, 'text_sha256': digest('synthetic text')} for cid in ids]})
        queries = ['synthetic-one', 'synthetic-two']
        original = CachedSearch(FakeClient(), out, queries, 36)
        for index, query in enumerate([*queries, queries[0]]):
            original.search(query, f'first-{index}')
        write_hashed(out / 'results/saved/report.json', {'id': 'first-report'})
        write_hashed(out / 'results/saved/queries.json', original.logical)
        write_hashed(out / 'quota-before.json', {'synthetic': True})
        config = {'corpus': str(corpus), 'physical_cap': 36,
            'selection': {'case_ids': ids, 'allowed_queries': queries},
            'request_ids': {'remaining': 'second-case'},
            'query_strategy': 'synthetic', 'matcher_version': 'synthetic'}
        return out, config, queries

    def resume(self, out, config, client, pipeline):
        with ExitStack() as stack:
            stack.enter_context(patch.object(driver, 'checked_config', return_value=config))
            stack.enter_context(patch('plagiarism.search_serpapi.YandexSerpAPISearch', return_value=client))
            stack.enter_context(patch('plagiarism.web_search.run', side_effect=pipeline))
            stack.enter_context(patch.object(driver, 'input_text', return_value='synthetic text'))
            driver.live_run(out)

    def test_completed_case_resume_preserves_all_three_counters(self):
        out, config, queries = self.fixture()
        first_bytes = {p: p.read_bytes() for p in out.rglob('*') if p.is_file()}
        client = FakeClient()
        self.resume(out, config, client, AssertionError('Completed case rerun'))
        receipt = read_hashed(out / 'run-complete.json')
        self.assertEqual((receipt['physical_calls'], receipt['logical_primary_calls'], receipt['cache_hits']), (2, 3, 1))
        self.assertEqual(client.calls, [])
        for path, data in first_bytes.items():
            self.assertEqual(path.read_bytes(), data)

    def test_partial_resume_adds_cache_hits_once_and_complete_run_is_immutable(self):
        out, config, queries = self.fixture(remaining=True)
        client = FakeClient()
        def pipeline(text, **kwargs):
            for index, query in enumerate([*queries, queries[0]]):
                kwargs['searcher'].search(query, f'second-{index}')
            return {'text': text, 'web': {'matcher_version': 'synthetic'},
                    'stats': {'checked_source_count': 0}}
        self.resume(out, config, client, pipeline)
        receipt = read_hashed(out / 'run-complete.json')
        self.assertEqual((receipt['physical_calls'], receipt['logical_primary_calls'], receipt['cache_hits']), (2, 6, 4))
        self.assertEqual(client.calls, [])
        self.assertEqual(len(read_hashed(out / 'results/remaining/queries.json')), 3)
        before = {p: p.read_bytes() for p in out.rglob('*') if p.is_file()}
        with patch.object(driver, 'checked_config', return_value=config), patch(
                'plagiarism.search_serpapi.YandexSerpAPISearch', side_effect=AssertionError('No client for complete run')):
            driver.live_run(out)
        self.assertEqual(before, {p: p.read_bytes() for p in out.rglob('*') if p.is_file()})

    def test_unresolved_case_after_restart_cannot_retry_unknown_delivery(self):
        out, config, queries = self.fixture(remaining=True)
        config['selection']['allowed_queries'].append('synthetic-unknown')
        client = FakeClient()
        client.fail = True
        cached = CachedSearch(client, out, config['selection']['allowed_queries'], 36)
        with self.assertRaises(EvaluationStopped):
            cached.search('synthetic-unknown', 'unknown-id')
        write_hashed(out / 'results/remaining/intent.json', {'request_id': 'second-case'})
        client.fail = False
        with self.assertRaises(EvaluationStopped):
            self.resume(out, config, client, AssertionError('Unresolved case rerun'))
        self.assertEqual(client.calls, [('synthetic-unknown', 'unknown-id')])
        self.assertFalse((out / 'run-complete.json').exists())

    def test_resume_with_mock_http_counts_only_one_new_physical_call(self):
        from plagiarism.search_serpapi import YandexSerpAPISearch
        from plagiarism.web_search import SearchBudget
        from tests.test_search_serpapi import account
        out, config, queries = self.fixture(remaining=True)
        config['selection']['allowed_queries'].append('synthetic-third')
        with patch('plagiarism.search_serpapi.key_for', return_value='synthetic-key'):
            client = YandexSerpAPISearch(SearchBudget(out / 'budget.sqlite3', limit=36))
        paths = []
        def response(request, **kwargs):
            path = urlsplit(request.full_url).path
            paths.append(path)
            value = account() if path == '/account.json' else {
                'search_metadata': {'status': 'Success'}, 'organic_results': []}
            return io.BytesIO(json.dumps(value).encode())
        client.opener = Mock()
        client.opener.open.side_effect = response
        def pipeline(text, **kwargs):
            for index, query in enumerate(['synthetic-one', 'synthetic-third']):
                kwargs['searcher'].search(query, f'http-{index}')
            return {'text': text, 'web': {'matcher_version': 'synthetic'},
                    'stats': {'checked_source_count': 0}}
        self.resume(out, config, client, pipeline)
        receipt = read_hashed(out / 'run-complete.json')
        self.assertEqual((receipt['physical_calls'], receipt['logical_primary_calls'], receipt['cache_hits']), (3, 5, 2))
        self.assertEqual(paths.count('/search.json'), 1)
        self.assertEqual(len(list((out / 'physical').glob('*/intent.json'))), 3)
        self.assertTrue((out / 'physical/http-1/response.raw').exists())


QUOTE = 'Квадратные приборы измеряют направление условного потока'
TEXT = '🧪 Заметка. ' + QUOTE + '. Конец упражнения.'
URL = 'https://example.org/fixture'


def evidence_fixture():
    start = TEXT.index(QUOTE)
    case = {'id': 'anonymous-fixture', 'text_sha256': sha256(TEXT.encode()).hexdigest(),
            'expected_insertion_words': 6, 'expected_evidence': [
                {'target_span': [start, start + len(QUOTE)], 'source_span': [0, len(QUOTE)], 'source_urls': [URL]}]}
    report = {'text': TEXT, 'sources': [{'id': 'fixture-source', 'url': URL, 'text': QUOTE}],
        'matches': [{'kind': 'exact', 'source_id': 'fixture-source', 'source_url': URL,
                     'start': start, 'end': start + len(QUOTE), 'source_start': 0, 'source_end': len(QUOTE),
                     'excerpt': QUOTE, 'source_excerpt': QUOTE}],
        'stats': {'checked_source_count': 1, 'uniqueness_percent': None},
        'web': {'status': 'partial', 'discovered_urls': [URL], 'errors': [], 'queries_attempted': 1}}
    return case, report


class ScoringTests(unittest.TestCase):
    def test_query_profile_separates_mixed_boundary_from_pure_evidence(self):
        case, report = evidence_fixture()
        spans = [case['expected_evidence'][0]['target_span']]
        mixed = query_profile(TEXT, spans, [QUOTE + ' Конец'])
        self.assertEqual((mixed['pure_queries'], mixed['mixed_queries']), (0, 1))
        self.assertEqual(mixed['touched_source_word_coverage'], 6)
        self.assertEqual(mixed['pure_source_word_coverage'], 0)
        pure = query_profile(TEXT, spans, [QUOTE])
        self.assertEqual((pure['pure_queries'], pure['mixed_queries']), (1, 0))
        self.assertEqual(pure['pure_source_word_coverage'], 6)

    def test_query_profile_counts_external_titles_and_controls_separately(self):
        case, report = evidence_fixture()
        spans = [case['expected_evidence'][0]['target_span']]
        row = query_profile(TEXT, spans, ['Unrelated external title'])
        self.assertEqual(row['nonliteral_queries'], 1)
        self.assertEqual(row['pure_source_word_coverage'], 0)
        self.assertFalse(query_profile(TEXT, [], [QUOTE])['applicable'])

    def test_unicode_bounds_and_partial_found_are_validated(self):
        case, report = evidence_fixture()
        row = score_case(case, report, ['"' + QUOTE + '"'])
        self.assertEqual(row['expected_words_matched'], 6)
        self.assertEqual(row['query_coverage_words'], 6)
        self.assertTrue(row['fully_found_expected_source'])
        self.assertEqual(row['coverage_status'], 'partial')

    def test_forged_second_excerpt_is_quarantined(self):
        case, report = evidence_fixture()
        report['matches'][0]['source_excerpt'] = 'invented'
        row = score_case(case, report)
        self.assertEqual(row['expected_words_matched'], 0)
        self.assertEqual(row['invalid_exact_pairs'], 1)

    def test_source_boundary_and_target_hash_tampering(self):
        case, report = evidence_fixture()
        report['matches'][0]['source_end'] += 1
        self.assertEqual(score_case(case, report)['invalid_exact_pairs'], 1)
        report['text'] += 'changed'
        with self.assertRaises(ValueError):
            score_case(case, report)

    def test_other_confirmed_source_does_not_become_gold_url(self):
        case, report = evidence_fixture()
        report['sources'][0]['url'] = 'https://example.org/other'
        report['matches'][0]['source_url'] = 'https://example.org/other'
        row = score_case(case, report)
        self.assertFalse(row['fully_found_expected_source'])
        self.assertTrue(row['fully_found_any_source'])
        self.assertEqual(row['other_source_words_matched'], 6)

    def test_zero_page_100_and_negative_indeterminacy(self):
        case, report = evidence_fixture()
        case.update(expected_evidence=[], expected_insertion_words=0)
        report.update(sources=[], matches=[], stats={'checked_source_count': 0, 'uniqueness_percent': 100})
        row = score_case(case, report)
        self.assertTrue(row['false_100'])
        self.assertEqual(row['miss_stage'], 'negative_indeterminate_zero_pages')

    def test_observed_title_anchor_is_not_literal_query_coverage(self):
        case, report = evidence_fixture()
        spans = [case['expected_evidence'][0]['target_span']]
        self.assertEqual(query_coverage(TEXT, ['A different observed publication title'], spans), 0)
        self.assertEqual(query_coverage(TEXT, [QUOTE], spans), 6)

    def test_missing_cases_remain_null_in_full_denominator(self):
        row = {'id': 'executed', 'insertion_words': 6, 'execution_status': 'complete',
               'expected_words_matched': 6, 'fully_found_expected_source': True}
        missing = {'id': 'not-run', 'insertion_words': 6, 'execution_status': 'not_executed',
                   'expected_words_matched': None, 'fully_found_expected_source': None}
        group = aggregate([row, missing])[0]
        self.assertEqual((group['total_cases'], group['executed'], group['unexecuted']), (2, 1, 1))
        self.assertEqual(group['expected_words_matched'], 6)
        self.assertEqual((group['expected_words_executed'], group['expected_words_total']), (6, 12))
        self.assertIsNone(missing['expected_words_matched'])

    def test_native_pipeline_zero_result_smoke_has_no_false_100(self):
        class Search:
            provider = 'serpapi-yandex'
            last_usage = {'credits': 0}
            def search(self, query, request_id): return []

        class Fetch:
            def fetch(self, url): raise AssertionError('No URLs should be read')

        report = run(TEXT, request_id='fixture-only', searcher=Search(), fetcher=Fetch(),
                     use_wiki_followup=False, use_related_fallback=False)
        self.assertEqual(report['stats']['checked_source_count'], 0)
        self.assertIsNone(report['stats']['uniqueness_percent'])
        self.assertEqual(report['web']['status'], 'partial')


if __name__ == '__main__':
    unittest.main()
