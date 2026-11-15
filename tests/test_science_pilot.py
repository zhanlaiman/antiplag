from copy import deepcopy
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import io
from pathlib import Path
import tempfile
import tarfile
import shutil
import unittest
from unittest.mock import patch

from plagiarism.matching import _tokens, analyze
from scripts.night_evaluation_io import EvaluationStopped, digest, read_hashed, write_hashed
from scripts.science_pilot import verify_dataset, inside
from scripts.science_pilot_run import CaseBoundSearch, authorized_binding, execute_cases, bind


STRATEGY = 'hybrid-citation-v13-empty-nine-suffix-six'


class Client:
    def __init__(self):
        self.calls = []; self.quotas = []; self.last_usage = None; self.fail = False
    def confirm_free(self, count):
        self.quotas.append(count)
        return {'total_searches_left': 100, 'plan_monthly_price': 0, 'extra_credits': 0}
    def search(self, query, request_id):
        self.calls.append((query, request_id))
        if self.fail:
            raise TimeoutError('synthetic unknown delivery')
        self.last_usage = {'credits': 1}
        return []


class PilotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.corpus = self.root / 'corpus'; self.corpus.mkdir()
        self.out = self.root / 'out'

    def seal(self):
        rows = sorted([{'path': p.relative_to(self.corpus).as_posix(), 'bytes': p.stat().st_size,
                'sha256': digest(p.read_bytes())} for p in self.corpus.rglob('*')
                if p.is_file() and p.name not in {'freeze.json', 'SHA256SUMS'}], key=lambda r: r['path'])
        canonical = ''.join(f"{r['sha256']}  {r['path']}\n" for r in rows)
        self.put('freeze.json', {'payload_files': rows, 'payload_sha256': digest(canonical)})
        (self.corpus / 'SHA256SUMS').write_text(canonical + digest((self.corpus / 'freeze.json').read_bytes()) + '  freeze.json\n')

    def put(self, name, data):
        path = self.corpus / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False))

    def fixture(self):
        quote = 'Ёжик берёза парус корзина мята сирень облако ветка пряник'
        sources = []; cases = []
        for label in ['A', 'B']:
            source = quote + ' ' + label
            path = f'sources/{label}.txt'; (self.corpus / 'sources').mkdir(exist_ok=True)
            (self.corpus / path).write_text(source)
            sources.append({'id': label, 'family_id': 'fixture-' + label, 'source_snapshot_path': path,
                            'source_text_sha256': digest(source), 'source_urls': ['https://example.org/' + label]})
            text = '🧪 Пролог. ' + quote + '. Финал.'; a = text.index(quote); b = a + len(quote)
            self.put(f'{label}.json', {})
            (self.corpus / f'{label}.txt').write_text(text)
            cases.append({'id': label, 'input_file': f'{label}.txt', 'text_sha256': digest(text),
                'total_words': len(_tokens(text)), 'expected_insertion_words': 9,
                'request_id': ('a' if label == 'A' else 'b') * 32, 'expected_evidence': [{
                    'source_id': label, 'quote': quote, 'target_span': [a, b], 'source_span': [0, len(quote)],
                    'source_urls': sources[-1]['source_urls'],
                    'target_utf8_span': [len(text[:a].encode()), len(text[:b].encode())],
                    'source_utf8_span': [0, len(quote.encode())], 'target_token_span': [1, 10],
                    'source_token_span': [0, 9]}]})
        text = 'Совсем иное учебное упражнение с самостоятельным описанием порядка действий.'
        (self.corpus / 'N.txt').write_text(text)
        cases.append({'id': 'N', 'input_file': 'N.txt', 'text_sha256': digest(text), 'total_words': len(_tokens(text)),
                      'expected_insertion_words': 0, 'request_id': 'c' * 32, 'expected_evidence': []})
        manifest = {'cases': cases, 'internet_case_ids': ['N', 'A', 'B'], 'conditional_case_ids': ['N', 'A', 'B'],
                    'physical_cap': 18, 'per_case_primary_cap': 6, 'candidate_count': 1, 'candidate_received': False}
        self.put('manifest.json', manifest); self.put('sources.json', {'sources': sources})
        self.put('run-order.json', {'case_ids': ['N', 'A', 'B']}); self.seal()
        return manifest

    def test_valid_unicode_bounds_and_fixed_denominator(self):
        self.fixture(); identity, manifest, sources = verify_dataset(self.corpus)
        self.assertEqual(manifest['internet_case_ids'], ['N', 'A', 'B'])
        self.assertEqual(len(sources), 2)

    def test_payload_change_fails_integrity(self):
        self.fixture(); (self.corpus / 'A.txt').write_text('changed')
        with self.assertRaises(ValueError): verify_dataset(self.corpus)

    def test_source_target_and_utf8_forgery_fail_after_reseal(self):
        manifest = self.fixture()
        for field, value in [('quote', 'invented'), ('target_utf8_span', [1, 2]), ('source_span', [1, 40])]:
            changed = deepcopy(manifest); changed['cases'][0]['expected_evidence'][0][field] = value
            self.put('manifest.json', changed); self.seal()
            with self.subTest(field=field), self.assertRaises(EvaluationStopped): verify_dataset(self.corpus)

    def test_duplicate_UUID_or_wrong_order_rejected(self):
        manifest = self.fixture(); manifest['cases'][0]['request_id'] = manifest['cases'][1]['request_id']
        self.put('manifest.json', manifest); self.seal()
        with self.assertRaises(EvaluationStopped): verify_dataset(self.corpus)
        manifest['cases'][0]['request_id'] = 'a' * 32; manifest['internet_case_ids'] = ['A', 'N', 'B']
        self.put('manifest.json', manifest); self.seal()
        with self.assertRaises(EvaluationStopped): verify_dataset(self.corpus)

    def test_traversal_path_is_not_read(self):
        with self.assertRaises(EvaluationStopped): inside(self.corpus, '../outside')

    def test_seventh_primary_slot_blocked_including_cached_queries(self):
        client = Client(); search = CaseBoundSearch(client, self.out, [], 18); search.begin_case()
        for n in range(6): search.search('same phrase', f'r{n}')
        with self.assertRaises(EvaluationStopped): search.search('same phrase', 'seventh')
        self.assertEqual(len(client.calls), 1); self.assertEqual(len(search.logical), 6)

    def test_nineteenth_physical_attempt_stops_before_quota_or_transport(self):
        client = Client(); search = CaseBoundSearch(client, self.out, [], 18)
        for n in range(18):
            if n % 6 == 0: search.begin_case()
            search.search('phrase ' + str(n), f'r{n}')
        search.begin_case()
        with self.assertRaises(EvaluationStopped): search.search('extra phrase', 'r19')
        self.assertEqual(len(client.calls), 18); self.assertEqual(len(client.quotas), 18)

    def test_unknown_delivery_not_retried_after_restart(self):
        client = Client(); client.fail = True
        search = CaseBoundSearch(client, self.out, [], 18); search.begin_case()
        with self.assertRaises(EvaluationStopped): search.search('phrase', 'r1')
        search = CaseBoundSearch(client, self.out, [], 18); search.begin_case()
        self.assertEqual(search.physical, 1)
        with self.assertRaises(EvaluationStopped): search.search('phrase', 'different-id')
        self.assertEqual(len(client.calls), 1)

    def test_wrong_candidate_or_missing_audit_denied_before_client(self):
        config = {'query_strategy': STRATEGY, 'candidate_sha': 'a' * 40, 'dataset': {'payload_sha256': 'dataset'},
                  'plan_sha256': 'plan', 'at': datetime.now(timezone.utc).isoformat(),
                  'driver_files': {}, 'source_files': {}, 'source': str(self.root)}
        write_hashed(self.out / 'binding.json', config)
        auth = {'query_strategy': STRATEGY, 'approved': True, 'candidate_sha': 'b' * 40, 'dataset_payload_sha256': 'dataset',
                'plan_sha256': 'plan', 'physical_cap': 18, 'case_primary_cap': 6,
                'authorized_at': (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}
        write_hashed(self.out / 'wrong.json', auth)
        with patch('plagiarism.search_serpapi.YandexSerpAPISearch', side_effect=AssertionError('No client')):
            with self.assertRaises(EvaluationStopped): authorized_binding(self.out, self.out / 'wrong.json')
            auth['candidate_sha'] = 'a' * 40; write_hashed(self.out / 'no-audit.json', auth)
            with self.assertRaises(EvaluationStopped): authorized_binding(self.out, self.out / 'no-audit.json')

    def test_first_three_cases_zero_pages_null_and_completed_run_unchanged(self):
        manifest = self.fixture(); client = Client()
        def pipeline(text, **kwargs):
            self.assertEqual(kwargs['query_strategy'], STRATEGY)
            for n in range(6): kwargs['searcher'].search(f'synthetic query {n}', digest(kwargs['request_id'] + '\0' + str(n)))
            report = analyze(text, [])
            return {**report, 'text': text, 'sources': [],
                    'stats': {**report['stats'], 'checked_source_count': 0, 'uniqueness_percent': None},
                    'web': {'status': 'partial', 'post_wiki_enabled': False, 'discovered_urls': [], 'queries_attempted': 6,
                            'query_strategy': kwargs['query_strategy']}}
        with patch('plagiarism.search_serpapi.YandexSerpAPISearch', return_value=client), patch(
                'plagiarism.web_search.run', side_effect=pipeline):
            receipt = execute_cases(self.corpus, manifest, self.out, strategy=STRATEGY)
        self.assertEqual((receipt['physical_calls'], receipt['logical_primary_calls'], receipt['cache_hits']), (6, 18, 12))
        self.assertTrue(all(row['false_100'] is False for row in receipt['rows']))
        self.assertEqual(receipt['rows'][0]['miss_stage'], 'negative_indeterminate_zero_pages')
        before = {p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        with patch('plagiarism.search_serpapi.YandexSerpAPISearch', side_effect=AssertionError('No client')):
            self.assertEqual(execute_cases(self.corpus, manifest, self.out, strategy=STRATEGY), receipt)
        self.assertEqual(before, {p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()})

    def test_unknown_case_restart_stops_before_client(self):
        manifest = self.fixture(); client = Client(); client.fail = True
        def pipeline(text, **kwargs):
            kwargs['searcher'].search('synthetic unknown', 'unknown-case-request')
        with patch('plagiarism.search_serpapi.YandexSerpAPISearch', return_value=client), patch(
                'plagiarism.web_search.run', side_effect=pipeline):
            with self.assertRaises(EvaluationStopped): execute_cases(self.corpus, manifest, self.out, strategy=STRATEGY)
        before = {p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        with patch('plagiarism.search_serpapi.YandexSerpAPISearch', side_effect=AssertionError('No client')):
            with self.assertRaises(EvaluationStopped): execute_cases(self.corpus, manifest, self.out, strategy=STRATEGY)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue((self.out / 'results/N/failure.json').exists())
        self.assertFalse((self.out / 'run-complete.json').exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()})

    def test_bind_keeps_archive_identity_and_rejects_later_source_edit(self):
        self.fixture(); plan = self.root / 'PILOT_PLAN.md'; plan.write_text('Synthetic plan')
        stream = io.BytesIO(); data = b'fixture_only = True\n'
        with tarfile.open(fileobj=stream, mode='w') as archive:
            member = tarfile.TarInfo('unit.py'); member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        with patch('scripts.science_pilot_run.subprocess.check_output', return_value=stream.getvalue()) as git:
            bind(self.corpus, self.root, 'a' * 40, self.out, plan, STRATEGY)
        git.assert_called_once_with(['git', '-C', str(self.root), 'archive', 'a' * 40])
        binding = read_hashed(self.out / 'binding.json')
        self.assertEqual(binding['source_files'], {'unit.py': digest(data)})
        with patch('scripts.science_pilot_run.subprocess.check_output', side_effect=AssertionError('No second archive')):
            with self.assertRaises(EvaluationStopped): bind(self.corpus, self.root, 'b' * 40, self.root / 'second-out', plan, STRATEGY)
        auth = {'query_strategy': STRATEGY, 'approved': True, 'candidate_sha': 'a' * 40,
                'dataset_payload_sha256': binding['dataset']['payload_sha256'],
                'plan_sha256': binding['plan_sha256'], 'physical_cap': 18, 'case_primary_cap': 6,
                'authorized_at': (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
                'audit_receipt': 'synthetic local audit', 'owner_message_reference': 'synthetic approval'}
        auth.update({key: binding[key] for key in ['pilot_id', 'authorized_corpus_root',
                     'allocation_anchor', 'output_root', 'plan_origin']})
        write_hashed(self.out / 'auth.json', auth)
        self.assertEqual(authorized_binding(self.out, self.out / 'auth.json'), binding)
        copy_root = self.root / 'copied-authority'; copy_root.mkdir()
        copied = copy_root / 'corpus'; shutil.copytree(self.corpus, copied)
        copied_plan = copy_root / 'PILOT_PLAN.md'; copied_plan.write_text(plan.read_text())
        with patch('scripts.science_pilot_run.subprocess.check_output', return_value=stream.getvalue()):
            bind(copied, self.root, 'a' * 40, copy_root / 'out', copied_plan, STRATEGY)
        with self.assertRaises(EvaluationStopped):
            authorized_binding(copy_root / 'out', self.out / 'auth.json')
        auth['query_strategy'] = 'hybrid-citation-v11-short-pure-tail'
        write_hashed(self.out / 'wrong-strategy.json', auth)
        with self.assertRaises(EvaluationStopped): authorized_binding(self.out, self.out / 'wrong-strategy.json')
        (self.out / 'source/unit.py').write_text('changed')
        with self.assertRaises(EvaluationStopped): authorized_binding(self.out, self.out / 'auth.json')

    def test_missing_unknown_and_older_strategies_fail_before_transport(self):
        from scripts.science_pilot_run import run_bound
        for strategy in [None, '', 'unknown', 'hybrid-citation-v10-short-center', 'hybrid-citation-v11-short-pure-tail', 'hybrid-citation-v12-empty-nine-unquoted']:
            with self.subTest(strategy=strategy), patch(
                    'plagiarism.search_serpapi.YandexSerpAPISearch', side_effect=AssertionError('No client')):
                with self.assertRaises(EvaluationStopped):
                    run_bound(self.out, self.root / 'nonexistent-auth.json', strategy)
                with self.assertRaises(EvaluationStopped):
                    bind(self.corpus, self.root, 'a' * 40, self.out, self.root / 'no-plan', strategy)
        self.assertFalse(self.out.exists())

    def test_wrong_native_report_strategy_is_preserved_and_never_continued(self):
        manifest = self.fixture(); client = Client()
        def pipeline(text, **kwargs):
            return {'text': text, 'web': {'query_strategy': 'wrong-native-fallback'}}
        with patch('plagiarism.search_serpapi.YandexSerpAPISearch', return_value=client), patch(
                'plagiarism.web_search.run', side_effect=pipeline):
            with self.assertRaises(EvaluationStopped): execute_cases(self.corpus, manifest, self.out, strategy=STRATEGY)
        report = self.out / 'results/N/report.json'
        self.assertEqual(read_hashed(report)['web']['query_strategy'], 'wrong-native-fallback')
        before = {p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        with patch('plagiarism.search_serpapi.YandexSerpAPISearch', side_effect=AssertionError('No client')):
            with self.assertRaises(EvaluationStopped): execute_cases(self.corpus, manifest, self.out, strategy=STRATEGY)
        self.assertEqual(before, {p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()})

    def test_copied_freeze_cannot_allocate_another_budget_under_same_plan(self):
        self.fixture(); plan = self.root / 'approved-plan.md'; plan.write_text('Synthetic plan')
        copy = self.root / 'copy-parent/corpus'; shutil.copytree(self.corpus, copy)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w'): pass
        with patch('scripts.science_pilot_run.subprocess.check_output', return_value=archive.getvalue()) as git:
            bind(self.corpus, self.root, 'a' * 40, self.out, plan, STRATEGY)
            with self.assertRaises(EvaluationStopped):
                bind(copy, self.root, 'a' * 40, self.root / 'second-out', plan, STRATEGY)
        self.assertEqual(git.call_count, 1)

    def test_interrupted_wrong_strategy_without_failure_marker_stops_on_resume(self):
        manifest = self.fixture(); client = Client()
        from scripts import science_pilot_run as driver
        original = driver.write_hashed
        def interrupted(path, value):
            if Path(path).name == 'failure.json': raise KeyboardInterrupt('synthetic crash')
            return original(path, value)
        def fallback(text, **kwargs):
            return {**analyze(text, []), 'text': text, 'sources': [],
                    'stats': {'checked_source_count': 0, 'uniqueness_percent': None},
                    'web': {'query_strategy': 'wrong', 'queries_attempted': 0}}
        with patch('plagiarism.search_serpapi.YandexSerpAPISearch', return_value=client), patch(
                'plagiarism.web_search.run', side_effect=fallback), patch.object(driver, 'write_hashed', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): execute_cases(self.corpus, manifest, self.out, strategy=STRATEGY)
        self.assertFalse((self.out / 'results/N/failure.json').exists())
        before = {p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        with patch('plagiarism.search_serpapi.YandexSerpAPISearch', side_effect=AssertionError('No client after invalid retained report')):
            with self.assertRaises(EvaluationStopped): execute_cases(self.corpus, manifest, self.out, strategy=STRATEGY)
        self.assertEqual(before, {p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()})

    def test_retained_text_source_highlight_and_request_context_fail_before_client(self):
        manifest = self.fixture(); case = manifest['cases'][2]
        text = (self.corpus / case['input_file']).read_text()
        base = {**analyze(text, []), 'text': text, 'sources': [],
                'stats': {'checked_source_count': 0, 'uniqueness_percent': None},
                'web': {'query_strategy': STRATEGY, 'post_wiki_enabled': False, 'queries_attempted': 0}}
        variants = []
        report = deepcopy(base); report['text'] += ' changed'; variants.append(report)
        report = deepcopy(base); report.pop('sources'); variants.append(report)
        report = deepcopy(base); report['sources'] = [{'id': 's', 'url': 'https://example.org/source',
            'text': 'Invented snapshot', 'provenance': {'text_sha256': '0' * 64}}]; variants.append(report)
        report = deepcopy(base); report['matches'] = [{'kind': 'exact', 'source_id': 'absent'}]; variants.append(report)
        report = deepcopy(base); report['id'] = 'different-request'; variants.append(report)
        for index, report in enumerate(variants):
            out = self.root / f'invalid-{index}'; directory = out / 'results/N'
            write_hashed(directory / 'intent.json', {'id': 'N', 'request_id': case['request_id'],
                'text_sha256': case['text_sha256'], 'query_strategy': STRATEGY, 'primary_max': 6})
            write_hashed(directory / 'report.json', report); write_hashed(directory / 'queries.json', [])
            before = {p: p.read_bytes() for p in out.rglob('*') if p.is_file()}
            with self.subTest(index=index), patch('plagiarism.search_serpapi.YandexSerpAPISearch', side_effect=AssertionError('No client')):
                with self.assertRaises(EvaluationStopped): execute_cases(self.corpus, manifest, out, strategy=STRATEGY)
            self.assertEqual(before, {p: p.read_bytes() for p in out.rglob('*') if p.is_file()})

    def test_valid_intercase_resume_restores_counters_without_reexecuting_case(self):
        manifest = self.fixture(); client = Client()
        from scripts import science_pilot_run as driver
        original = driver.write_hashed
        def interrupted(path, value):
            if Path(path).parts[-2:] == ('A', 'intent.json'): raise KeyboardInterrupt('between first cases')
            return original(path, value)
        def pipeline(text, **kwargs):
            for n in range(6): kwargs['searcher'].search('shared-query-' + str(n), digest(kwargs['request_id'] + '\0' + str(n)))
            return {**analyze(text, []), 'text': text, 'sources': [],
                    'stats': {'checked_source_count': 0, 'uniqueness_percent': None},
                    'web': {'status': 'partial', 'post_wiki_enabled': False, 'query_strategy': STRATEGY, 'queries_attempted': 6}}
        with patch('plagiarism.search_serpapi.YandexSerpAPISearch', return_value=client), patch(
                'plagiarism.web_search.run', side_effect=pipeline), patch.object(driver, 'write_hashed', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): execute_cases(self.corpus, manifest, self.out, strategy=STRATEGY)
        saved = (self.out / 'results/N/report.json').read_bytes(); resumed = Client()
        with patch('plagiarism.search_serpapi.YandexSerpAPISearch', return_value=resumed), patch(
                'plagiarism.web_search.run', side_effect=pipeline):
            receipt = execute_cases(self.corpus, manifest, self.out, strategy=STRATEGY)
        self.assertEqual((receipt['physical_calls'], receipt['logical_primary_calls'], receipt['cache_hits']), (6, 18, 12))
        self.assertEqual(resumed.calls, [])
        self.assertEqual((self.out / 'results/N/report.json').read_bytes(), saved)

    def test_actual_native_pipeline_nonempty_sources_matches_receipt_contract(self):
        from plagiarism import web_search
        from scripts import science_pilot_run as driver
        manifest = self.fixture()
        # This base checkout registers v11; exercise its real native pipeline,
        # changing only the test's policy allowlist, never the native report.
        strategy = web_search.SHORT_PURE_STRATEGY
        class NativeClient(Client):
            def search(self, query, request_id):
                super().search(query, request_id)
                return [{'url': 'https://example.org/A'}]
        class Pages:
            def __init__(self, text): self.text, self.calls = text, []
            def fetch(self, url):
                self.calls.append(url)
                return {'url': url, 'title': 'Fixture source', 'text': self.text,
                        'fetched_at': '2026-11-15T00:00:00Z', 'links': []}
        class Wiki:
            provider = 'mediawiki'
            last_usage = None
            def __init__(self): self.calls = []
            def search(self, query, request_id): self.calls.append((query, request_id)); return []
        client = NativeClient(); pages = Pages((self.corpus / 'sources/A.txt').read_text()); wiki = Wiki()
        with patch.object(driver, 'APPROVED_STRATEGIES', frozenset({strategy})), patch(
                'plagiarism.search_serpapi.YandexSerpAPISearch', return_value=client), patch(
                'plagiarism.web_fetch.Fetcher', return_value=pages), patch(
                'plagiarism.search_mediawiki.MediaWikiSearch', return_value=wiki):
            receipt = execute_cases(self.corpus, manifest, self.out, strategy=strategy)
        self.assertEqual(receipt['cases'], 3)
        self.assertGreater(len(client.calls), 0); self.assertGreater(len(pages.calls), 0)
        self.assertGreater(len(wiki.calls), 0)
        self.assertTrue(any(row['any_source_words_matched'] == 9 for row in receipt['rows']))
        for cid in manifest['internet_case_ids']:
            report = read_hashed(self.out / 'results' / cid / 'report.json')
            self.assertNotIn('request_id', report['web'])
            self.assertGreater(len(report['sources']), 0)
            self.assertEqual(report['text'], (self.corpus / next(c['input_file'] for c in manifest['cases'] if c['id'] == cid)).read_text())
        before = {p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        with patch.object(driver, 'APPROVED_STRATEGIES', frozenset({strategy})), patch(
                'plagiarism.search_serpapi.YandexSerpAPISearch', side_effect=AssertionError('No client for complete native run')):
            self.assertEqual(execute_cases(self.corpus, manifest, self.out, strategy=strategy), receipt)
        self.assertEqual(before, {p: p.read_bytes() for p in self.out.rglob('*') if p.is_file()})

    def test_actual_native_zero_pages_partial_remains_null(self):
        from plagiarism import web_search
        from scripts import science_pilot_run as driver
        manifest = self.fixture(); client = Client()
        class Wiki:
            provider = 'mediawiki'; last_usage = None
            def search(self, query, request_id): return []
        pages = type('NoPages', (), {'fetch': lambda self, url: (_ for _ in ()).throw(AssertionError('No discovered URLs'))})()
        with patch.object(driver, 'APPROVED_STRATEGIES', frozenset({web_search.SHORT_PURE_STRATEGY})), patch(
                'plagiarism.search_serpapi.YandexSerpAPISearch', return_value=client), patch(
                'plagiarism.web_fetch.Fetcher', return_value=pages), patch(
                'plagiarism.search_mediawiki.MediaWikiSearch', return_value=Wiki()):
            receipt = execute_cases(self.corpus, manifest, self.out, strategy=web_search.SHORT_PURE_STRATEGY)
        self.assertEqual(receipt['cases'], 3)
        for cid in manifest['internet_case_ids']:
            report = read_hashed(self.out / 'results' / cid / 'report.json')
            self.assertEqual(report['stats']['checked_source_count'], 0)
            self.assertIsNone(report['stats']['uniqueness_percent'])
            self.assertEqual(report['web']['status'], 'partial')

    def test_actual_native_intercase_resume_keeps_first_report_and_primary_counts(self):
        from plagiarism import web_search
        from scripts import science_pilot_run as driver
        manifest = self.fixture(); original = driver.write_hashed
        def interrupted(path, value):
            if Path(path).parts[-2:] == ('A', 'intent.json'): raise KeyboardInterrupt('between native cases')
            return original(path, value)
        class NativeClient(Client):
            def search(self, query, request_id):
                super().search(query, request_id); return [{'url': 'https://example.org/A'}]
        source = (self.corpus / 'sources/A.txt').read_text()
        class Pages:
            def fetch(self, url): return {'url': url, 'title': 'Fixture source', 'text': source,
                                         'fetched_at': '2026-11-15T00:00:00Z', 'links': []}
        class Wiki:
            provider = 'mediawiki'; last_usage = None
            def search(self, query, request_id): return []
        client = NativeClient(); resumed = NativeClient(); strategy = web_search.SHORT_PURE_STRATEGY
        with patch.object(driver, 'APPROVED_STRATEGIES', frozenset({strategy})), patch(
                'plagiarism.search_serpapi.YandexSerpAPISearch', return_value=client), patch(
                'plagiarism.web_fetch.Fetcher', return_value=Pages()), patch(
                'plagiarism.search_mediawiki.MediaWikiSearch', return_value=Wiki()), patch.object(driver, 'write_hashed', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): execute_cases(self.corpus, manifest, self.out, strategy=strategy)
        saved = {p: p.read_bytes() for p in (self.out / 'results/N').iterdir()}
        with patch.object(driver, 'APPROVED_STRATEGIES', frozenset({strategy})), patch(
                'plagiarism.search_serpapi.YandexSerpAPISearch', return_value=resumed), patch(
                'plagiarism.web_fetch.Fetcher', return_value=Pages()), patch(
                'plagiarism.search_mediawiki.MediaWikiSearch', return_value=Wiki()):
            receipt = execute_cases(self.corpus, manifest, self.out, strategy=strategy)
        self.assertEqual(saved, {p: p.read_bytes() for p in (self.out / 'results/N').iterdir()})
        self.assertEqual(receipt['physical_calls'], len(client.calls) + len(resumed.calls))
        self.assertEqual(receipt['logical_primary_calls'], sum(read_hashed(self.out / 'results' / cid / 'report.json')['web']['primary_queries_attempted'] for cid in manifest['internet_case_ids']))

    def test_actual_nonwiki_contract_and_tampered_wiki_counts_before_client(self):
        from plagiarism import web_search
        from scripts import science_pilot_run as driver
        manifest = self.fixture(); case = manifest['cases'][0]
        text = (self.corpus / case['input_file']).read_text()
        source = (self.corpus / 'sources/A.txt').read_text()
        class Search(Client):
            def search(self, query, request_id):
                super().search(query, request_id); return [{'url': 'https://example.org/A'}]
        class Pages:
            def fetch(self, url): return {'url': url, 'title': 'Fixture source', 'text': source,
                                         'fetched_at': '2026-11-15T00:00:00Z', 'links': []}
        search = CaseBoundSearch(Search(), self.out, [], 18); search.begin_case()
        strategy = web_search.SHORT_PURE_STRATEGY
        report = web_search.run(text, request_id=case['request_id'], searcher=search, fetcher=Pages(),
            query_strategy=strategy, use_wiki_followup=False, use_related_fallback=False)
        self.assertIs(report['web']['post_wiki_enabled'], False)
        self.assertNotIn('primary_queries_attempted', report['web'])
        intent = {'id': case['id'], 'request_id': case['request_id'], 'text_sha256': case['text_sha256'],
                  'query_strategy': strategy, 'primary_max': 6}
        directory = self.out / 'results/A'
        write_hashed(directory / 'intent.json', intent); write_hashed(directory / 'report.json', report)
        write_hashed(directory / 'queries.json', search.logical)
        self.assertEqual(driver.validate_case_receipt(self.corpus, case, directory, strategy), search.logical)
        variants = []
        bad = deepcopy(report); bad['web']['post_wiki_enabled'] = True; variants.append(bad)
        bad = deepcopy(report); bad['web'].update(post_wiki_enabled=True, primary_queries_attempted=len(search.logical) + 1); variants.append(bad)
        bad = deepcopy(report); bad['web']['queries_attempted'] += 1; variants.append(bad)
        bad = deepcopy(report); bad['web'].pop('post_wiki_enabled'); variants.append(bad)
        for index, bad in enumerate(variants):
            out = self.root / f'bad-native-count-{index}'; directory = out / 'results/A'
            write_hashed(directory / 'intent.json', intent); write_hashed(directory / 'report.json', bad)
            write_hashed(directory / 'queries.json', search.logical)
            before = {p: p.read_bytes() for p in out.rglob('*') if p.is_file()}
            with self.subTest(index=index), patch.object(driver, 'APPROVED_STRATEGIES', frozenset({strategy})), patch(
                    'plagiarism.search_serpapi.YandexSerpAPISearch', side_effect=AssertionError('No client for invalid provider counts')):
                with self.assertRaises(EvaluationStopped): execute_cases(self.corpus, manifest, out, strategy=strategy)
            self.assertEqual(before, {p: p.read_bytes() for p in out.rglob('*') if p.is_file()})


if __name__ == '__main__': unittest.main()
