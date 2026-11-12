"""Authored temporary metadata only; never opens actual publication artifacts."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import publication_query_delta_accounting as accounting


def digest(value):
    return sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n')
    return digest(path.read_bytes())


class DeltaAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'own-data'
        self.directory = self.base / accounting.SERIES[1]
        primary = [{'query': f'"authored frozen query {i}"', 'start': i * 12, 'end': i * 12 + 11} for i in range(6)]
        wiki = [{**q, 'primary_query_index': i} for i, q in enumerate(primary[:2])]
        bindings = []
        for provider, plan, suffix in (('tavily', primary[:5], 'search'), ('mediawiki', wiki, 'mediawiki')):
            for i, q in enumerate(plan):
                call = digest(provider + str(i))
                binding = {'provider': provider, 'case_id': 'P3-06', 'query_index': i, 'query': q['query'],
                    'provider_call_id': call, 'sha256': digest(call),
                    'path': f'data/private/web-evaluation/publication-first-use-v3/{call}-{suffix}.json'}
                if provider == 'mediawiki': binding['primary_query_index'] = q['primary_query_index']
                bindings.append(binding)
        self.configuration = {'case_id': 'P3-06', 'primary_query_index': 5, 'query': primary[5],
            'context_commit': accounting.CONTEXT_COMMIT, 'query_module_sha256': accounting.QUERY_MODULE_SHA,
            'strategy': accounting.STRATEGY, 'first_use': False, 'evaluation_only': True, 'used_for_tuning': False,
            'caps': deepcopy(accounting.CAPS), 'cached_logical_primary': 5, 'cached_logical_Wiki': 2,
            'primary_plan': primary, 'Wiki_plan': wiki, 'cached_bindings': bindings,
            'code_sha256': {'authored.py': 'f' * 64},
            **{k: digest(k) for k in ('input_sha256', 'historical_run_sha256', 'historical_manifest_sha256', 'historical_local_sha256', 'parent_plans_sha256')}}
        self.save_run()

    def save_run(self, directory=None):
        directory = directory or self.directory
        identity = digest(json.dumps(self.configuration, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
        self.call = digest(identity + '\0P3-06\0primary-delta\0' + '5')
        self.run = {'id': identity, 'call_id': self.call, 'configuration': self.configuration}
        self.run_sha = write(directory / 'run.json', self.run)

    def add_receipt(self, usage=None, state='complete'):
        self.record = {'request_id': self.call, 'case_id': 'P3-06', 'query_index': 0,
            'query': self.configuration['query']['query'], 'search_depth': 'basic', 'state': state,
            'results': [], 'usage': usage}
        write(self.directory / 'start-intent.json', {'run_sha256': self.run_sha, 'call_id': self.call, 'at': 'authored'})
        write(self.directory / 'provider-intent.json', {'request_id': self.call, 'case_id': 'P3-06', 'query_index': 0,
            'primary_query_index': 5, 'query': self.configuration['query']['query'], 'state': 'submitted'})
        self.receipt = self.directory / (self.call + '-search.json')
        write(self.receipt, self.record)

    def row(self):
        rows, hashes = accounting.collect(self.base, self.root)
        return rows[-1], hashes

    def test_unstarted_v1_zero_and_completed_v2_one_credit_have_receipt_hashes(self):
        self.save_run(self.base / accounting.SERIES[0])
        self.add_receipt({'credits': 1})
        rows, hashes = accounting.collect(self.base, self.root)
        self.assertEqual([(r['query_attempts'], r['total_basic_credits']) for r in rows], [(0, 0), (1, 1)])
        row = rows[1]
        self.assertEqual((row['actual_submitted_provider_intents'], row['provider_records_complete']), (1, 1))
        self.assertEqual((row['new_Wiki_queries'], row['new_page_GETs']), (0, 0))
        self.assertEqual((row['cached_primary_slots_not_new_expense'], row['cached_Wiki_slots_not_new_expense']), (5, 2))
        relative = self.receipt.relative_to(self.root).as_posix()
        self.assertEqual(hashes[relative], digest(self.receipt.read_bytes()))
        self.assertEqual(row['receipt_sha256'][relative], hashes[relative])

    def test_none_missing_and_explicit_zero_usage_are_distinct(self):
        self.add_receipt(None)
        for usage in (None, 'missing', {'credits': 0}):
            record = deepcopy(self.record)
            if usage == 'missing': record.pop('usage')
            else: record['usage'] = usage
            write(self.receipt, record)
            row, _ = self.row()
            self.assertEqual(row['confirmed_provider_credits'], 0)
            self.assertEqual(row['provider_usage_unknown_calls'], int(usage != {'credits': 0}))
            self.assertEqual(row['total_basic_credits'], 0 if usage == {'credits': 0} else None)
        for bad in ({'credits': True}, {'credits': -1}, {'credits': 2}, {}, []):
            write(self.receipt, {**self.record, 'usage': bad})
            with self.assertRaisesRegex(ValueError, 'credit usage'): self.row()

    def test_start_or_provider_intent_without_receipt_and_submitted_outcome_stay_unknown(self):
        self.add_receipt({'credits': 1})
        raw = self.receipt.read_bytes()
        self.receipt.unlink()
        row, _ = self.row()
        self.assertEqual((row['query_attempts'], row['provider_marker_records_missing']), (0, 1))
        self.assertEqual(row['minimum_attempts_consistent_with_reports'], 1)
        self.assertIsNone(row['total_basic_credits'])
        (self.directory / 'provider-intent.json').unlink()
        row, _ = self.row()
        self.assertEqual(row['unknown_start_without_provider_record'], 1)
        self.assertIsNone(row['total_basic_credits'])
        self.receipt.write_bytes(raw)
        row, _ = self.row()
        self.assertTrue(any(i['code'] == 'durable_intent_missing' for i in row['accounting_issues']))
        self.add_receipt({'credits': 1}, 'submitted')
        row, _ = self.row()
        self.assertEqual(row['confirmed_provider_credits'], 1)
        self.assertIsNone(row['total_basic_credits'])

    def test_manifest_caps_bindings_identity_and_case_fail_closed(self):
        original = deepcopy(self.configuration)
        changes = [lambda c: c['caps'].update(new_source_GET=1), lambda c: c['caps'].update(new_Wiki=False),
            lambda c: c.update(case_id='P3-05'), lambda c: c.update(primary_query_index=True),
            lambda c: c['cached_bindings'][0].update(query='unbound'),
            lambda c: c['cached_bindings'][0].update(path='../elsewhere.json'),
            lambda c: c['cached_bindings'][5].update(primary_query_index=False),
            lambda c: c['cached_bindings'][0].update(query_index=1)]
        for change in changes:
            self.configuration = deepcopy(original); change(self.configuration); self.save_run()
            with self.assertRaises(ValueError): self.row()
        self.configuration = original; self.save_run()
        write(self.directory / 'run.json', {**self.run, 'call_id': 'e' * 64})
        with self.assertRaisesRegex(ValueError, 'identity'): self.row()

    def test_unbound_extra_or_wrong_provider_receipts_and_intents_are_rejected(self):
        self.add_receipt({'credits': 1})
        for field, value in (('request_id', 'e' * 64), ('case_id', 'P3-05'), ('query_index', True),
                ('query', 'not bound'), ('search_depth', 'advanced'), ('provider', 'mediawiki')):
            write(self.receipt, {**self.record, field: value})
            with self.assertRaises(ValueError): self.row()
        write(self.receipt, self.record)
        for filename in ('e' * 64 + '-search.json', 'extra-mediawiki.json', 'source-http-001.json', 'page-001-fetch.json'):
            path = self.directory / filename; write(path, {})
            with self.assertRaises(ValueError): self.row()
            path.unlink()
        path = self.directory / 'start-intent.json'
        write(path, {'run_sha256': 'a' * 64, 'call_id': self.call})
        with self.assertRaisesRegex(ValueError, 'start intent'): self.row()

    def test_failed_post_quota_is_unavailable_and_malformed_quota_is_rejected(self):
        self.add_receipt({'credits': 1})
        path = self.directory / 'quota-after.json'
        write(path, {'unavailable': 'HTTPError', 'new_searches_allowed': False})
        row, _ = self.row()
        self.assertEqual(row['quota_observations']['quota-after.json'], {'unavailable': 'HTTPError', 'new_searches_allowed': False})
        for malformed in (-1, True, 'unknown'):
            write(path, {'paygo_usage': malformed})
            with self.assertRaisesRegex(ValueError, 'quota counter'): self.row()

    def test_only_own_metadata_is_read_no_private_code_bodies_network_or_mutation(self):
        self.add_receipt({'credits': 1})
        write(self.directory / 'pages/forbidden.json', {'body': 'not accounting'})
        write(self.base / 'other-series/run.json', {'prose': 'not accounting'})
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        real_read = Path.read_bytes
        def limited_read(path):
            if path.parent != self.directory: raise AssertionError('No page, cache binding or other-series read')
            return real_read(path)
        with patch.object(Path, 'read_bytes', limited_read), patch('socket.socket', side_effect=AssertionError('No network')):
            row, hashes = self.row()
        self.assertEqual(row['total_basic_credits'], 1)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(any('forbidden' in p or 'other-series' in p for p in hashes))
        self.assertEqual(accounting.collect(self.root / 'absent', self.root), ([], {}))

    def test_duplicate_nonfinite_json_and_symlink_receipts_fail_closed(self):
        self.add_receipt({'credits': 1})
        raw = self.receipt.read_bytes()
        for malformed in ('{"usage":null,"usage":null}', '{"usage":NaN}'):
            self.receipt.write_text(malformed)
            with self.assertRaises(ValueError): self.row()
        self.receipt.write_bytes(raw)
        target = self.directory / 'opaque.json'; target.write_bytes(raw)
        self.receipt.unlink(); self.receipt.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlinks'): self.row()


if __name__ == '__main__': unittest.main()
