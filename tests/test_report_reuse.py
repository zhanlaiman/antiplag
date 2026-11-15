"""Tenant-private historical lookup; native ACK faults, never external providers."""
from contextlib import closing, ExitStack
from concurrent.futures import Future
from hashlib import sha256, pbkdf2_hmac
from http.client import HTTPConnection, HTTPResponse, RemoteDisconnected
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import socket
from tempfile import TemporaryDirectory
from threading import Event, Lock, RLock, Thread, local
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile
from io import BytesIO

from cloud.web_beta import BetaHandler, DurableTenants
from cloud.web_beta import create_server
from cloud.report_reuse_runtime import ReportReuseRuntime
from plagiarism import report_reuse as reuse
from plagiarism.access import AccessError
from plagiarism.jobs import Jobs
from plagiarism.matching import analyze
from plagiarism.saas import fingerprint, report_identifier
from plagiarism.storage import Store
from plagiarism.search_pool import fingerprint as key_fingerprint


TENANT, OTHER, JOB = 'a' * 64, 'b' * 64, '1' * 32
TEXT = ' Точный синтетический текст ёжика.\nБез нормализации. '


def report(text=TEXT):
    result = analyze(text, [])
    result.update({'text': text, 'sources': [], 'scope': 'web_search',
            'web': {'provider': 'serpapi-yandex', 'status': 'partial',
                    'query_strategy': 'hybrid-citation-v11-short-pure',
                    'queries_planned': 6, 'queries_completed': 6,
                    'sources_checked': 0, 'urls_attempted': 0, 'errors': [], 'limitations': [],
                    'automatic_search': {'consent_version': 'search-pool-v1'}}})
    result['stats']['uniqueness_percent'] = None
    return result


class NativeReuseScenario:
    """Shared local HTTP/browser fixture; real handlers and persistence, synthetic runner."""
    def __init__(self, snapshot=None):
        self.temporary = TemporaryDirectory(); self.root = Path(self.temporary.name)
        if snapshot: shutil.copytree(snapshot, self.root, dirs_exist_ok=True)
        self.closed = False; self.run_report = report
        keys = {p: ('tvly-' if p == 'tavily' else 'offline-' + p + '-') + '7' * 32
                for p in ('serpapi', 'tavily', 'serper', 'exa')}
        self.environment = patch.dict(os.environ, {'ISTOK_AUTOMATIC_SEARCH': '1', 'ISTOK_SEARCH_PROVIDER': 'serpapi-yandex',
            'ISTOK_RUNTIME_DIR': str(self.root / 'runtime'),
            **{'ISTOK_' + p.upper() + '_API_KEY': k for p, k in keys.items()}}, clear=True)
        self.environment.start(); self.calls = []; self.persist_calls = []; self.network = []
        self.fault = None; self.entered = Event(); self.release = Event(); self.stage = local()
        original_save = Store.save
        def save(store, kind, data):
            self.stage.kind = kind
            return original_save(store, kind, data)
        self.save_patch = patch.object(Store, 'save', save); self.save_patch.start()
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0)); self.port = reserved.getsockname()[1]
        self.origin = f'http://127.0.0.1:{self.port}'
        def persist():
            kind = getattr(self.stage, 'kind', None); self.persist_calls.append(kind)
            target = 'model-job' if self.fault in {'original', 'delay_original'} else reuse.RECEIPT_KIND
            if self.fault and kind == target:
                self.entered.set()
                if self.fault.startswith('delay_'): self.release.wait(5)
                else: raise OSError('Synthetic persist fault')
        def runner(text, *args, **kwargs):
            self.calls.append(text); return self.run_report(text)
        now = time.time()
        account = {'account_id': 'synthetic-reuse', 'account_status': 'Active', 'plan_name': 'Free Plan',
                   'plan_monthly_price': 0, 'searches_per_month': 250, 'plan_searches_left': 250,
                   'total_searches_left': 250, 'extra_credits': 0, 'this_month_usage': 0}
        packet = {'version': 1, 'packet_id': 'a' * 32, 'captured_at': now,
            'credential_fingerprints': {p: key_fingerprint(k) for p, k in keys.items()},
            'proofs': {p: {'key_fingerprint': key_fingerprint(keys[p]), 'checked_at': now,
                'usage': account if p == 'serpapi' else {'account': {'current_plan': 'Researcher',
                    'plan_limit': 1000, 'plan_usage': 0, 'paygo_usage': 0, 'paygo_limit': None},
                    'key': {'usage': 0, 'limit': None}}} for p in ('serpapi', 'tavily')}}
        grants = {p: {'provider': p, 'approved': True, 'credential_fingerprint': key_fingerprint(keys[p]),
            'account_ref': 'synthetic-reuse', 'grant_id': 'synthetic-reuse-grant', 'cap': 100,
            'expires_at': now + 3600, 'cash_spend_limit_usd': 0, 'max_unit_cost_usd': .004 if p == 'exa' else 0,
            'proof_sha256': 'a' * 64} for p in ('serper', 'exa')}
        original_connect, original_connect_ex, original_dns = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
        def connect(sock, address):
            if address != ('127.0.0.1', self.port):
                self.network.append('outside owned localhost'); raise AssertionError('External connection forbidden')
            return original_connect(sock, address)
        def dns(host, *args, **kwargs):
            if host != '127.0.0.1':
                self.network.append('outside owned localhost DNS'); raise AssertionError('External DNS forbidden')
            return original_dns(host, *args, **kwargs)
        def connect_ex(sock, address):
            if address != ('127.0.0.1', self.port):
                self.network.append('outside owned localhost'); raise AssertionError('External connection forbidden')
            return original_connect_ex(sock, address)
        self.socket_patch = patch('socket.socket.connect', connect); self.socket_patch.start()
        self.socket_ex_patch = patch('socket.socket.connect_ex', connect_ex); self.socket_ex_patch.start()
        self.dns_patch = patch('socket.getaddrinfo', dns); self.dns_patch.start()
        self.service = create_server(self.root, self.origin, host='127.0.0.1', port=self.port,
            allow_http=True, integrated=True, registration_enabled=True, business_writes_enabled=True,
            persist=persist, runner=runner, pool_bootstrap=packet, pool_keys=keys, pool_grants=grants)
        self.thread = Thread(target=self.service.serve_forever, daemon=True); self.thread.start()

    def client(self): return NativeClient(self)

    def wait(self, client, job_id):
        for _ in range(300):
            status, data = client.request('/api/jobs/' + job_id)
            if status == 200 and data['job']['status'] in {'complete', 'failed', 'cancelled'}:
                return data['job']
            time.sleep(.01)
        raise AssertionError('Native job did not settle')

    def published(self, tenant, job_id):
        path = self.root / 'tenants' / tenant / 'istok.sqlite3'
        for _ in range(300):
            with closing(sqlite3.connect(path)) as db:
                if db.execute('SELECT 1 FROM records WHERE kind=? AND id=?',
                              (reuse.RECEIPT_KIND, 'report-reuse-ack:' + job_id)).fetchone():
                    assert self.service.beta.tenants.report_reuse.wait_idle(3)
                    return
            time.sleep(.01)
        raise AssertionError('Receipt was not published')

    def close(self):
        if self.closed: return
        self.closed = True
        self.release.set()
        workers = list(self.service.beta.tenants.coordinator.executor._threads)
        try:
            self.service.shutdown(); self.service.server_close(); self.service.beta.tenants.close(); self.thread.join(5)
            assert not self.thread.is_alive() and not self.service.beta.tenants.report_reuse.worker.is_alive()
            assert not any(worker.is_alive() for worker in workers)
            with socket.socket() as probe:
                probe.settimeout(1); assert probe.connect_ex(('127.0.0.1', self.port)) != 0
            assert not self.network
        finally:
            self.dns_patch.stop(); self.socket_ex_patch.stop(); self.socket_patch.stop(); self.save_patch.stop(); self.environment.stop()
            self.temporary.cleanup()

    def snapshot(self, destination):
        shutil.copytree(self.root, destination, dirs_exist_ok=True)
        for path in self.root.rglob('*.sqlite3'):
            with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(destination / path.relative_to(self.root))) as target:
                source.backup(target)


class NativeClient:
    def __init__(self, scenario): self.scenario, self.cookies, self.csrf = scenario, {}, ''

    def request(self, path, body=None, *, method=None, origin=None, raw=None):
        headers = {'Host': f'127.0.0.1:{self.scenario.port}', 'Origin': origin or self.scenario.origin,
                   'Cookie': '; '.join(k + '=' + v for k, v in self.cookies.items())}
        data = raw if raw is not None else json.dumps(body) if body is not None else None
        if data is not None: headers.update({'Content-Type': 'application/json', 'X-Istok-CSRF': self.csrf})
        connection = HTTPConnection('127.0.0.1', self.scenario.port, timeout=5)
        try:
            try: connection.request(method or ('POST' if data is not None else 'GET'), path, data, headers)
            except BrokenPipeError: pass  # The native body-size guard can reject before reading bytes.
            response = connection.getresponse(); content = response.read()
            for name, value in response.getheaders():
                if name.lower() == 'set-cookie':
                    jar = SimpleCookie(); jar.load(value)
                    for key, item in jar.items(): self.cookies[key] = item.value
            self.headers = dict(response.getheaders())
            result = json.loads(content) if response.getheader('Content-Type', '').startswith('application/json') else content
            if isinstance(result, dict) and 'csrf_token' in result: self.csrf = result['csrf_token']
            return response.status, result
        finally: connection.close()

    def bootstrap(self): return self.request('/api/session')[1]['beta']['id']

    def check(self, text=TEXT, job_id=JOB):
        return self.request('/api/web-check-jobs', {'request_id': job_id, 'text': text,
            'internet_consent': True, 'search_provider': 'auto', 'provider_consent_version': 'search-pool-v1'})


class NativeReuseTests(unittest.TestCase):
    def setUp(self):
        self.scenario = NativeReuseScenario(); self.addCleanup(self.scenario.close)
        self.client = self.scenario.client(); self.tenant = self.client.bootstrap()

    def test_native_hit_open_new_check_exact_retry_and_complete_serializer(self):
        self.assertEqual(self.client.check()[0], 200)
        self.assertEqual(self.scenario.wait(self.client, JOB)['status'], 'complete')
        self.scenario.published(self.tenant, JOB)
        with self.scenario.service.beta.tenants.context(self.tenant) as tenant, closing(sqlite3.connect(tenant.store.path)) as db:
            raw = db.execute("SELECT data FROM records WHERE kind='model-job' AND id=?", (JOB,)).fetchone()[0]
            receipt = json.loads(db.execute('SELECT data FROM records WHERE kind=?', (reuse.RECEIPT_KIND,)).fetchone()[0])
        self.assertIn('синтетический', raw)
        self.assertEqual(receipt['terminal_digest'], sha256(raw.encode('utf-8')).hexdigest())
        self.assertNotEqual(receipt['terminal_digest'], sha256(json.dumps(json.loads(raw), ensure_ascii=True).encode()).hexdigest())
        before = len(self.scenario.persist_calls)
        status, data = self.client.request('/api/reports/reuse', {'text': TEXT})
        self.assertEqual(status, 200); self.assertEqual(data['match']['report_id'], report_identifier(JOB))
        self.assertEqual(len(self.scenario.persist_calls), before)
        status, opened = self.client.request('/api/reports/' + report_identifier(JOB))
        self.assertEqual(status, 200); self.assertNotIn('_report_reuse', json.dumps(opened))
        self.assertEqual(len(self.scenario.calls), 1)
        self.client.check(job_id='5' * 32); self.scenario.wait(self.client, '5' * 32)
        self.assertEqual(len(self.scenario.calls), 2)
        self.client.check(job_id='5' * 32)
        self.assertEqual(len(self.scenario.calls), 2)

    def test_native_original_failure_and_delayed_ack_never_ready_even_after_eviction(self):
        self.scenario.fault = 'delay_original'
        self.client.check()
        self.assertTrue(self.scenario.entered.wait(3))
        before = len(self.scenario.persist_calls)
        self.assertEqual(self.client.request('/api/reports/reuse', {'text': TEXT})[1], {'match': None})
        self.assertEqual(len(self.scenario.persist_calls), before)
        self.scenario.fault = 'original'; self.scenario.release.set()
        # The already delayed call succeeds; exercise a separate post-SQLite exception.
        self.scenario.wait(self.client, JOB)
        self.scenario.fault = 'original'; self.client.check(job_id='6' * 32)
        failed = self.scenario.wait(self.client, '6' * 32)
        self.assertEqual((failed['status'], failed['code']), ('failed', 'job_storage_error'))
        path = self.scenario.root / 'tenants' / self.tenant / 'istok.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            saved = json.loads(db.execute('SELECT data FROM records WHERE id=?', ('6' * 32,)).fetchone()[0])
            self.assertEqual(saved['status'], 'complete')
            self.assertIsNone(db.execute('SELECT 1 FROM records WHERE id=?', ('report-reuse-ack:' + '6' * 32,)).fetchone())
        self.scenario.service.beta.tenants.entries.clear()
        self.scenario.fault = None
        self.assertNotEqual((self.client.request('/api/reports/reuse', {'text': TEXT})[1].get('match') or {}).get('report_id'), report_identifier('6' * 32))

    def test_native_lookup_rejections_never_persist_or_create_tenant(self):
        for path, body, options, expected in [
                ('/api/reports/reuse', {'text': TEXT}, {}, 200),
                ('/api/reports/reuse', {'text': TEXT, 'guest': OTHER}, {}, 400),
                ('/api/reports/reuse', {'text': TEXT}, {'origin': 'https://foreign.invalid'}, 403),
                ('/api/reports/reuse', None, {'method': 'GET'}, 405),
                ('/api/reports/reuse', None, {'raw': '{'}, 400)]:
            # Rejections also exercise the exact read-only send_json exemption.
            self.scenario.service.beta.tenants.report_reuse.buckets.clear()
            before = len(self.scenario.persist_calls)
            self.assertEqual(self.client.request(path, body, **options)[0], expected)
            self.assertEqual(len(self.scenario.persist_calls), before)
            self.assertEqual(list((self.scenario.root / 'tenants').glob('*')), [])

    def test_native_registered_alias_claim_logout_and_cross_tenant(self):
        self.client.check(); self.scenario.wait(self.client, JOB); self.scenario.published(self.tenant, JOB)
        other = self.scenario.client(); other.bootstrap()
        self.assertEqual(other.request('/api/reports/reuse', {'text': TEXT})[1], {'match': None})
        old = self.scenario.client(); old.cookies = dict(self.client.cookies)
        self.assertEqual(self.client.request('/api/account/session')[0], 200)
        self.assertEqual(self.client.request('/api/account/register', {'username': 'reuse_fixture', 'password': secrets.token_urlsafe(24)})[0], 200)
        self.assertIsNotNone(self.client.request('/api/reports/reuse', {'text': TEXT})[1]['match'])
        self.assertEqual(old.request('/api/reports/reuse', {'text': TEXT})[0], 401)
        revoked = self.scenario.client(); revoked.cookies = dict(self.client.cookies)
        self.assertEqual(self.client.request('/api/account/logout', {})[0], 200)
        self.assertEqual(self.client.request('/api/reports/reuse', {'text': TEXT})[1], {'match': None})
        self.assertEqual(old.request('/api/reports/reuse', {'text': TEXT})[0], 401)
        self.assertEqual(revoked.request('/api/reports/reuse', {'text': TEXT})[0], 401)

    def test_native_original_failure_snapshot_restart_and_positive_published_restart(self):
        self.scenario.fault = 'original'
        with self.assertLogs(level='ERROR'):
            self.client.check(); failed = self.scenario.wait(self.client, JOB)
        self.assertEqual((failed['status'], failed['code']), ('failed', 'job_storage_error'))
        self.scenario.fault = None
        self.scenario.service.beta.persist()  # An unrelated later commit can retain the SQL-complete row.
        with TemporaryDirectory() as snapshots:
            snapshot = Path(snapshots) / 'failed'; self.scenario.snapshot(snapshot)
            self.scenario.close()
            restarted = NativeReuseScenario(snapshot); self.addCleanup(restarted.close)
            reader = restarted.client(); reader.cookies = dict(self.client.cookies)
            reader.bootstrap()
            # Native recovery sees the stale complete row. It must not mint authority.
            self.assertEqual(reader.request('/api/jobs/' + JOB)[1]['job']['status'], 'complete')
            self.assertEqual(reader.request('/api/reports/reuse', {'text': TEXT})[1], {'match': None})
            self.assertEqual(restarted.calls, [])
            reader.check(job_id='7' * 32); restarted.wait(reader, '7' * 32)
            restarted.published(self.tenant, '7' * 32)
            positive = Path(snapshots) / 'published'; restarted.snapshot(positive); restarted.close()
            final = NativeReuseScenario(positive); self.addCleanup(final.close)
            reader2 = final.client(); reader2.cookies = dict(reader.cookies); reader2.bootstrap()
            self.assertEqual(reader2.request('/api/reports/reuse', {'text': TEXT})[1]['match']['report_id'], report_identifier('7' * 32))
            self.assertEqual(final.calls, [])

    def test_native_ack2_delay_does_not_hold_job_lock_or_permit(self):
        self.scenario.fault = 'delay_receipt'
        self.client.check()
        self.assertTrue(self.scenario.entered.wait(3))
        job = self.scenario.wait(self.client, JOB)
        self.assertEqual(job['status'], 'complete')
        with self.scenario.service.beta.tenants.context(self.tenant) as tenant:
            self.assertTrue(tenant.jobs.lock.acquire(blocking=False)); tenant.jobs.lock.release()
        # The valid local receipt proves ACK1 even before ACK2 returns.
        self.assertIsNotNone(self.client.request('/api/reports/reuse', {'text': TEXT})[1]['match'])
        self.client.check(job_id='8' * 32)
        self.assertEqual(self.scenario.wait(self.client, '8' * 32)['status'], 'complete')
        self.assertEqual(len(self.scenario.calls), 2)
        self.scenario.release.set()
        self.scenario.published(self.tenant, '8' * 32)

    def test_native_ack2_failure_keeps_complete_and_surviving_signed_receipt_after_restart(self):
        self.scenario.fault = 'receipt'
        with self.assertLogs(level='WARNING'):
            self.client.check(); self.assertEqual(self.scenario.wait(self.client, JOB)['status'], 'complete')
            self.scenario.published(self.tenant, JOB)
        with TemporaryDirectory() as directory:
            snapshot = Path(directory) / 'ack2-unknown'; self.scenario.snapshot(snapshot); self.scenario.close()
            restarted = NativeReuseScenario(snapshot); self.addCleanup(restarted.close)
            reader = restarted.client(); reader.cookies = dict(self.client.cookies); reader.bootstrap()
            self.assertEqual(reader.request('/api/reports/reuse', {'text': TEXT})[1]['match']['report_id'], report_identifier(JOB))
            self.assertEqual(reader.request('/api/jobs/' + JOB)[1]['job']['status'], 'complete')
            self.assertEqual(restarted.calls, [])

    def test_native_private_marker_and_receipt_never_escape_public_serializers(self):
        self.scenario.run_report = lambda text: {**report(text), '_report_reuse': {'forged': 'client-marker'}}
        self.client.check(); self.scenario.wait(self.client, JOB); self.scenario.published(self.tenant, JOB)
        with self.scenario.service.beta.tenants.context(self.tenant) as tenant:
            stored = tenant.store.get('report', report_identifier(JOB))
            marker = stored['_report_reuse']; self.assertNotIn('forged', marker)
            receipt = tenant.store.get(reuse.RECEIPT_KIND, 'report-reuse-ack:' + JOB)
        for path in ('/api/jobs/' + JOB, '/api/reports/' + report_identifier(JOB), '/api/reports',
                     '/api/data', '/api/report-exports/' + report_identifier(JOB),
                     '/reports/' + report_identifier(JOB) + '.html'):
            status, body = self.client.request(path)
            self.assertEqual(status, 200, (path, body))
            raw = json.dumps(body) if isinstance(body, dict) else body.decode()
            for private in ('_report_reuse', 'report-reuse-ack:', marker['text_hmac'], receipt['signature']):
                self.assertNotIn(private, raw, path)
        status, data = self.client.request('/api/data-export'); self.assertEqual(status, 200)
        with ZipFile(BytesIO(data)) as archive:
            exported = '\n'.join(archive.read(name).decode() for name in archive.namelist())
            for private in ('_report_reuse', 'report-reuse-ack:', marker['text_hmac'], receipt['signature']):
                self.assertNotIn(private, exported)

    def test_native_rejection_matrix_is_readonly_even_when_tenant_and_persist_forbidden(self):
        tenants = self.scenario.service.beta.tenants
        with patch.object(tenants, 'context', side_effect=AssertionError('Lookup created tenant')), \
             patch.object(self.scenario.service.beta, 'admit_space', side_effect=AssertionError('Lookup admitted tenant')), \
             patch.object(self.scenario.service.beta, 'mutation', side_effect=AssertionError('Lookup mutated ledger')):
            for path, body, raw, expected in [
                    ('/api/reports/reuse', {'text': TEXT}, None, 200),
                    ('/api/reports/reuse?guest=' + OTHER, {'text': TEXT}, None, 400),
                    ('/api/reports/reuse', {'text': 'x' * 10001}, None, 400),
                    ('/api/reports/reuse', {'text': '\ud800'}, None, 400),
                    ('/api/reports/reuse', None, '{', 400),
                    ('/api/reports/reuse', None, ' ' * (2 * 1024 * 1024 + 1), 400)]:
                tenants.report_reuse.buckets.clear()
                before = len(self.scenario.persist_calls)
                status, body = self.client.request(path, body, raw=raw)
                self.assertEqual(status, expected, (status, body))
                self.assertEqual(self.client.headers.get('Cache-Control'), 'no-store')
                self.assertEqual(len(self.scenario.persist_calls), before)
        # A following mutation retains its normal persist hook.
        before = len(self.scenario.persist_calls)
        self.assertEqual(self.client.request('/api/dictionary', {'word': 'синтетический', 'action': 'add'})[0], 200)
        self.assertGreater(len(self.scenario.persist_calls), before)

    def test_native_no_manifest_when_runner_changes_text_and_no_lookup_write_for_broken_database(self):
        self.scenario.run_report = lambda text: report(text + ' изменено')
        self.client.check(); self.scenario.wait(self.client, JOB)
        self.assertTrue(self.scenario.service.beta.tenants.report_reuse.wait_idle())
        self.assertEqual(self.client.request('/api/reports/reuse', {'text': TEXT})[1], {'match': None})
        database = self.scenario.service.beta.database
        backup = database.with_suffix('.held'); database.rename(backup)
        try:
            before = len(self.scenario.persist_calls)
            self.assertEqual(self.client.request('/api/reports/reuse', {'text': TEXT})[0], 503)
            self.assertFalse(database.exists()); self.assertEqual(len(self.scenario.persist_calls), before)
        finally: backup.rename(database)

    def test_native_corrupt_identity_database_is_generic_readonly_failure(self):
        database = self.scenario.service.beta.database
        backup = database.with_suffix('.held'); database.rename(backup)
        database.write_bytes(b'synthetic-invalid-database')
        try:
            before = len(self.scenario.persist_calls)
            status, body = self.client.request('/api/reports/reuse', {'text': TEXT})
            self.assertEqual((status, body['code']), (503, 'report_reuse_unavailable'))
            self.assertEqual(database.read_bytes(), b'synthetic-invalid-database')
            self.assertEqual(len(self.scenario.persist_calls), before)
        finally: backup.replace(database)

    def test_native_other_device_canonical_access_and_trash_after_lookup(self):
        self.client.check(); self.scenario.wait(self.client, JOB); self.scenario.published(self.tenant, JOB)
        self.client.request('/api/account/session')
        credentials = {'username': 'reuse_other_device', 'password': secrets.token_urlsafe(24)}
        self.assertEqual(self.client.request('/api/account/register', credentials)[0], 200)
        reader = self.scenario.client(); reader.bootstrap(); reader.request('/api/account/session')
        self.assertEqual(reader.request('/api/account/login', credentials)[0], 200)
        self.assertEqual(reader.bootstrap(), self.tenant)
        identifier = reader.request('/api/reports/reuse', {'text': TEXT})[1]['match']['report_id']
        self.assertEqual(self.client.request('/api/data/report/' + identifier + '/trash', {})[0], 200)
        self.assertEqual(reader.request('/api/reports/' + identifier)[0], 404)
        self.assertEqual(reader.request('/api/reports/reuse', {'text': TEXT})[1], {'match': None})
        self.assertEqual(self.client.request('/api/data/report/' + identifier + '/restore', {})[0], 200)
        self.assertIsNotNone(reader.request('/api/reports/reuse', {'text': TEXT})[1]['match'])
        self.assertEqual(len(self.scenario.calls), 1)

    def test_native_expired_account_token_cannot_reuse_or_open_claimed_reports(self):
        self.client.check(); self.scenario.wait(self.client, JOB); self.scenario.published(self.tenant, JOB)
        self.client.request('/api/account/session')
        self.assertEqual(self.client.request('/api/account/register', {'username': 'reuse_expiry', 'password': secrets.token_urlsafe(24)})[0], 200)
        before = len(self.scenario.persist_calls)
        with patch.object(self.scenario.service.accounts.access.ledger, '_now', return_value=time.time() + 400 * 86400):
            self.assertEqual(self.client.request('/api/reports/reuse', {'text': TEXT})[0], 401)
            self.assertEqual(len(self.scenario.persist_calls), before)
            self.assertEqual(self.client.request('/api/reports/' + report_identifier(JOB))[0], 401)
        self.assertEqual(len(self.scenario.calls), 1)

    def test_native_owner_large_exact_text_and_guest_rejection_keep_existing_limits(self):
        text = 'Синтетический текст. ' * 501
        self.assertGreater(len(text), 10000)
        self.assertEqual(self.client.request('/api/reports/reuse', {'text': text})[0], 400)
        password = secrets.token_urlsafe(24); salt = secrets.token_hex(32)
        digest = pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
        with patch.dict(os.environ, {'ISTOK_OWNER_USERNAME': 'reuse_owner', 'ISTOK_OWNER_SALT': salt, 'ISTOK_OWNER_DIGEST': digest}):
            self.assertEqual(self.client.request('/api/owner/login', {'username': 'reuse_owner', 'password': password})[0], 200)
            tenant = self.client.bootstrap()
            self.assertEqual(self.client.check(text=text)[0], 200)
            self.assertEqual(self.scenario.wait(self.client, JOB)['status'], 'complete'); self.scenario.published(tenant, JOB)
            self.scenario.service.beta.tenants.report_reuse.buckets.clear()
            self.assertEqual(self.client.request('/api/reports/reuse', {'text': text})[1]['match']['report_id'], report_identifier(JOB))
            self.assertEqual(len(self.scenario.calls), 1)
            previous = self.scenario.client(); previous.cookies = dict(self.client.cookies)
            self.assertEqual(self.client.request('/api/owner/logout', {})[0], 200)
            self.assertEqual(self.client.request('/api/reports/reuse', {'text': TEXT})[1], {'match': None})
            self.assertEqual(previous.request('/api/reports/reuse', {'text': TEXT})[1], {'match': None})
            self.assertEqual(previous.request('/api/reports/' + report_identifier(JOB))[0], 404)

    def test_native_keepalive_resets_exact_readonly_flag_for_following_mutation(self):
        with patch.object(BetaHandler, 'protocol_version', 'HTTP/1.1'):
            connection = HTTPConnection('127.0.0.1', self.scenario.port, timeout=3)
            self.addCleanup(connection.close)
            headers = {'Origin': self.scenario.origin, 'Content-Type': 'application/json',
                       'Cookie': '; '.join(k + '=' + v for k, v in self.client.cookies.items())}
            before = len(self.scenario.persist_calls)
            connection.request('POST', '/api/reports/reuse', '{', headers)
            response = connection.getresponse(); response.read(); self.assertEqual(response.status, 400)
            sock = connection.sock; self.assertIsNotNone(sock)
            self.assertEqual(len(self.scenario.persist_calls), before)
            connection.request('POST', '/api/dictionary', json.dumps({'word': 'синтетический', 'action': 'add'}), headers)
            response = connection.getresponse(); response.read(); self.assertEqual(response.status, 200)
            self.assertIs(connection.sock, sock); self.assertGreater(len(self.scenario.persist_calls), before)
            self.client.request('/api/account/session')
            self.assertEqual(self.client.request('/api/account/register', {'username': 'reuse_keepalive', 'password': secrets.token_urlsafe(24)})[0], 200)
            headers.update({'Cookie': '; '.join(k + '=' + v for k, v in self.client.cookies.items()), 'X-Istok-CSRF': self.client.csrf})
            before = len(self.scenario.persist_calls)
            connection.request('POST', '/api/reports/reuse', '{', headers)
            response = connection.getresponse(); response.read(); self.assertEqual(response.status, 400)
            self.assertEqual(len(self.scenario.persist_calls), before)
            connection.request('POST', '/api/account/logout', '{}', headers)
            response = connection.getresponse(); response.read(); self.assertEqual(response.status, 200)
            self.assertIs(connection.sock, sock); self.assertGreater(len(self.scenario.persist_calls), before)
            connection.close()

    def test_native_malformed_keepalive_closes_unread_body_without_pipeline_execution(self):
        errors=[]
        def observe_error(*args):errors.append('Unexpected native handler exception')
        with patch.object(BetaHandler,'protocol_version','HTTP/1.1'),patch.object(self.scenario.service,'handle_error',observe_error):
            connection=HTTPConnection('127.0.0.1',self.scenario.port,timeout=3)
            self.addCleanup(connection.close)
            cookie='; '.join(k+'='+v for k,v in self.client.cookies.items())
            headers={'Origin':self.scenario.origin,'Cookie':cookie,'Content-Type':'application/json'}
            before=len(self.scenario.persist_calls)
            connection.request('POST','/api/reports/reuse',json.dumps({'text':TEXT}),headers)
            first=connection.getresponse();self.assertEqual(first.status,200);first.read()
            self.assertEqual(len(self.scenario.persist_calls),before)
            sock=connection.sock;self.assertIsNotNone(sock)
            body=json.dumps({'word':'синтетический','action':'add'}).encode('ascii')
            malformed=(f'POST http://[broken/ HTTP/1.1\r\nHost: 127.0.0.1:{self.scenario.port}\r\n'
                f'Origin: {self.scenario.origin}\r\nCookie: {cookie}\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{{}}').encode('ascii')
            pipeline=(f'POST /api/dictionary HTTP/1.1\r\nHost: 127.0.0.1:{self.scenario.port}\r\n'
                f'Origin: {self.scenario.origin}\r\nCookie: {cookie}\r\nContent-Type: application/json\r\n'
                f'Content-Length: {len(body)}\r\n\r\n').encode('ascii')+body
            # Raw bytes bypass only HTTPConnection's client-side URL validation.
            sock.sendall(malformed+pipeline)
            response=HTTPResponse(sock,method='POST');response.begin()
            self.assertEqual(response.status,400)
            self.assertEqual(response.getheader('Connection'),'close')
            self.assertEqual(json.loads(response.read()),{'error':'Некорректный адрес запроса.','code':'invalid_request'})
            with self.assertRaises(RemoteDisconnected):HTTPResponse(sock,method='POST').begin()
            self.assertEqual(errors,[])
            self.assertEqual(len(self.scenario.persist_calls),before+1)
            self.assertFalse((self.scenario.root/'tenants'/self.tenant).exists())
            connection.close()
        # The pipelined request was otherwise valid and can mutate on a new connection.
        self.assertEqual(self.client.request('/api/dictionary',{'word':'синтетический','action':'add'})[0],200)
        self.assertTrue((self.scenario.root/'tenants'/self.tenant).exists())

    def test_request_reset_and_serializer_bind_to_current_target_without_reparse(self):
        handler=object.__new__(BetaHandler);handler.server=self.scenario.service
        handler.command='POST';handler.path='http://[broken/'
        handler.response_headers=[];handler._reuse_readonly_response=True
        handler._reuse_readonly_path='/api/reports/reuse'
        handler._reuse_readonly_binding=('POST','/api/reports/reuse');handler.logout_persist_handled=True
        before=len(self.scenario.persist_calls)
        with patch('server.Handler.send_json') as send,ExitStack() as leases:
            handler._dispatch('POST',leases)
            self.assertFalse(handler._reuse_readonly_response)
            self.assertIsNone(handler._reuse_readonly_path);self.assertIsNone(handler._reuse_readonly_binding)
            self.assertFalse(handler.logout_persist_handled);self.assertTrue(handler.close_connection)
            self.assertEqual(send.call_args.args[1],400)
        self.assertEqual(len(self.scenario.persist_calls),before+1)
        handler.path='/api/reports/reuse';handler._reuse_readonly_response=True
        handler._reuse_readonly_path='/api/reports/reuse'
        handler._reuse_readonly_binding=('POST',handler.path)
        with patch('cloud.web_beta.urlsplit',side_effect=AssertionError('Serializer reparsed target')),patch('server.Handler.send_json') as send:
            before=len(self.scenario.persist_calls)
            handler.send_json({});self.assertEqual(len(self.scenario.persist_calls),before)
            for path,method,binding,cached in [
                ('http://[broken/','POST',('POST','/api/reports/reuse'),'/api/reports/reuse'),
                ('/api/reports/reuse','POST',('DELETE','/api/reports/reuse'),'/api/reports/reuse'),
                ('/api/reports/reuse','POST',('POST','/api/reports/reuse'),'/api/other')]:
                handler.path=path;handler.command=method;handler._reuse_readonly_binding=binding;handler._reuse_readonly_path=cached
                previous=len(self.scenario.persist_calls);handler.send_json({})
                self.assertEqual(len(self.scenario.persist_calls),previous+1)
            self.assertEqual(send.call_count,4)

    def test_native_library_result_is_readable_and_replayable_without_reuse_manifest(self):
        body = {'text': TEXT, 'request_id': JOB}
        self.assertEqual(self.client.request('/api/library-check-jobs', body)[0], 200)
        job = self.scenario.wait(self.client, JOB)
        self.assertEqual(job['status'], 'complete'); self.assertEqual(job['result']['report']['scope'], 'local_library')
        self.assertNotIn('_report_reuse', json.dumps(job))
        with self.scenario.service.beta.tenants.context(self.tenant) as tenant:
            self.assertNotIn('_report_reuse', tenant.store.get('report', report_identifier(JOB)))
        self.assertEqual(self.client.request('/api/reports/reuse', {'text': TEXT})[1], {'match': None})
        with patch('server.analyze', side_effect=AssertionError('Library replay did new work')):
            self.assertEqual(self.client.request('/api/library-check-jobs', body)[1]['job']['result'], job['result'])
        self.assertEqual(self.scenario.calls, [])


class ReuseStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.key = secrets.token_bytes(32)
        self.persist = lambda: None
        self.tenants = DurableTenants(self.root / 'tenants', lambda: self.persist())
        self.runtime = ReportReuseRuntime(self.root, self.key, lambda: self.persist())
        self.tenants.report_reuse = self.runtime
        self.addCleanup(self.tenants.close)
        self.addCleanup(self.runtime.close)

    def save_job(self, *, status='complete', job_id=JOB, text=TEXT, stamped=True, sources=None, tenant_id=TENANT):
        body = {'request_id': job_id, 'text': text, 'internet_consent': True}
        fp = fingerprint({'path': '/api/web-check', 'body': body})
        with self.tenants.context(tenant_id) as tenant:
            data = report(text)
            if sources is not None: data['sources'] = sources
            if stamped: data['_report_reuse'] = reuse.manifest(self.key, tenant_id, text, job_id, fp)
            saved = tenant.store.save('report', {**data, 'id': report_identifier(job_id)})
            terminal = {'id': job_id, 'path': '/api/web-check', 'status': status,
                        'request_fingerprint': fp, 'result': {'report': {'id': saved['id']}}}
            future = Future()
            live = {**terminal, '_future': future, '_persist_pending': True}
            tenant.jobs.items[job_id] = live
            stored = tenant.store.save('model-job', terminal)
            return tenant, live, future, stored

    def settle(self, live, future):
        live['_persist_pending'] = False
        future.set_result(None)
        self.assertTrue(self.runtime.wait_idle(3))

    def lookup(self, text=TEXT, tenant=TENANT):
        return reuse.lookup(self.root, self.key, tenant, text)

    def test_receipt_waits_for_future_and_matches_actual_terminal_bytes(self):
        tenant, live, future, stored = self.save_job()
        self.assertIsNone(self.lookup())
        self.settle(live, future)
        match = self.lookup()
        self.assertEqual(match['report_id'], report_identifier(JOB))
        self.assertEqual((match['provider'], match['coverage_status']), ('serpapi-yandex', 'partial'))
        with closing(sqlite3.connect(tenant.store.path)) as db:
            raw = db.execute("SELECT data FROM records WHERE id=? AND kind='model-job'", (JOB,)).fetchone()[0]
            receipt = json.loads(db.execute('SELECT data FROM records WHERE kind=?', (reuse.RECEIPT_KIND,)).fetchone()[0])
        self.assertEqual(receipt['terminal_digest'], sha256(raw.encode()).hexdigest())
        self.assertNotIn('text_hmac', json.dumps(match)); self.assertNotIn(TEXT, json.dumps(match))
        self.tenants.entries.clear()
        self.assertEqual(self.lookup(), match)
        self.assertEqual(reuse.lookup(self.root, self.key, TENANT, TEXT), match)

    def test_original_post_sqlite_ack_failure_witness_never_mints_receipt(self):
        tenant, live, future, _ = self.save_job(job_id='2' * 32, status='failed')
        # Reuse the native _persist fault, not a fake lookup or returned service.
        job_id = '3' * 32; fp = 'd' * 64
        data = report(); data['_report_reuse'] = reuse.manifest(self.key, TENANT, TEXT, job_id, fp)
        tenant.store.save('report', {**data, 'id': report_identifier(job_id)})
        native = Jobs(); self.addCleanup(native.close)
        native.items[job_id] = {'id': job_id, 'path': '/api/web-check', 'status': 'complete',
            'request_fingerprint': fp, 'result': {'report': {'id': report_identifier(job_id)}},
            '_store': tenant.store, '_persist_pending': True, '_future': Future()}
        self.persist = lambda: (_ for _ in ()).throw(OSError('synthetic original ACK failure'))
        with self.assertLogs(level='ERROR'): native._persist(native.items[job_id])
        self.assertEqual(native.items[job_id]['code'], 'job_storage_error')
        with closing(sqlite3.connect(tenant.store.path)) as db:
            self.assertEqual(json.loads(db.execute('SELECT data FROM records WHERE id=?', (job_id,)).fetchone()[0])['status'], 'complete')
            self.assertEqual(db.execute('SELECT count(*) FROM records WHERE kind=?', (reuse.RECEIPT_KIND,)).fetchone()[0], 0)
        self.assertIsNone(self.lookup())
        self.persist = lambda: None
        self.tenants.entries.clear()
        self.assertIsNone(self.lookup())

    def test_exact_bytes_tenant_legacy_trash_and_invalid_receipt(self):
        tenant, live, future, _ = self.save_job(); self.settle(live, future)
        for changed in (TEXT.strip(), TEXT.lower(), TEXT.replace('\n', '\r\n'), TEXT.replace('ё', 'е\u0308')):
            self.assertIsNone(self.lookup(changed))
        self.assertIsNone(self.lookup(tenant=OTHER))
        tenant.store.trash_item('report', report_identifier(JOB))
        self.assertIsNone(self.lookup())
        tenant.store.restore_item('report', report_identifier(JOB))
        with closing(sqlite3.connect(tenant.store.path)) as db, db:
            db.execute("UPDATE records SET data=json_set(data,'$.signature',?) WHERE kind=?", ('0' * 64, reuse.RECEIPT_KIND))
        with self.assertRaises(AccessError) as error: self.lookup()
        self.assertEqual(error.exception.status, 503)

    def test_failed_future_and_receipt_ack_failure_do_not_rewrite_original_outcome(self):
        tenant, live, future, _ = self.save_job()
        live['_persist_pending'] = False; live['status'] = 'failed'; future.set_result(None)
        self.assertTrue(self.runtime.wait_idle(2)); self.assertIsNone(self.lookup())
        tenant, live, future, _ = self.save_job(job_id='4' * 32)
        self.persist = lambda: (_ for _ in ()).throw(OSError('synthetic receipt ACK2 failure'))
        self.settle(live, future)
        self.assertEqual(live['status'], 'complete')
        self.assertEqual(self.lookup()['report_id'], report_identifier('4' * 32))

    def test_lookup_is_select_only_and_bounded_scan_never_false_misses(self):
        tenant, live, future, _ = self.save_job(); self.settle(live, future)
        original = sqlite3.connect; statements = []
        before = sha256(tenant.store.path.read_bytes()).digest()
        def ro(path, **options):
            self.assertIn('mode=ro', path)
            db = original(path, **options); db.set_trace_callback(statements.append); return db
        with patch('plagiarism.report_reuse.sqlite3.connect', side_effect=ro): self.assertIsNotNone(self.lookup())
        self.assertTrue(all(sql.lstrip().upper().startswith('SELECT ') for sql in statements))
        self.assertEqual(sha256(tenant.store.path.read_bytes()).digest(), before)
        with patch.object(reuse, 'REPORT_LIMIT', 0):
            with self.assertRaises(AccessError) as error: self.lookup('no matching text')
        self.assertEqual(error.exception.status, 503)
        with patch.object(reuse, 'VM_LIMIT', 0):
            with self.assertRaises(AccessError): self.lookup()

    def test_future_cancel_exception_pending_and_lock_contention_all_drop_optional_receipt(self):
        for index, mode in enumerate(('cancel', 'exception', 'pending', 'lock')):
            with self.subTest(mode=mode):
                tenant, live, future, _ = self.save_job(job_id=f'{index + 10:032x}')
                if mode == 'cancel': future.cancel()
                elif mode == 'exception': future.set_exception(OSError('synthetic future error'))
                elif mode == 'pending': future.set_result(None)
                else:
                    entered, release = Event(), Event()
                    def hold():
                        with tenant.jobs.lock: entered.set(); release.wait(3)
                    thread = Thread(target=hold); thread.start(); self.assertTrue(entered.wait(1))
                    try: live['_persist_pending'] = False; future.set_result(None)
                    finally: release.set(); thread.join(2)
                    self.assertFalse(thread.is_alive())
                self.assertTrue(self.runtime.wait_idle())
                self.assertEqual(live['status'], 'complete')
                self.assertIsNone(self.lookup())

    def test_capture_registration_and_callback_faults_leave_native_save_successful(self):
        with patch.object(self.runtime, 'capture', side_effect=OSError('synthetic capture fault')), self.assertLogs(level='WARNING'):
            _, live, future, _ = self.save_job(job_id='a' * 32)
        self.settle(live, future); self.assertIsNone(self.lookup())
        with patch.object(Future, 'add_done_callback', side_effect=RuntimeError('synthetic registration fault')), self.assertLogs(level='WARNING'):
            _, live, future, _ = self.save_job(job_id='b' * 32)
        self.settle(live, future); self.assertIsNone(self.lookup())
        _, live, future, _ = self.save_job(job_id='c' * 32)
        with patch.object(self.runtime.queue, 'put_nowait', side_effect=RuntimeError('synthetic callback fault')), self.assertLogs(level='WARNING'):
            self.settle(live, future)
        self.assertEqual(live['status'], 'complete'); self.assertIsNone(self.lookup())

    def test_publisher_pre_sqlite_failure_queue_saturation_and_bounded_shutdown(self):
        entered, release = Event(), Event(); saved = []
        capture = (TENANT, JOB, 'c' * 64, report_identifier(JOB), 'd' * 64)
        def raw_save(kind, data):
            entered.set(); release.wait(3); saved.append(kind)
            raise OSError('synthetic pre-SQLite receipt failure')
        with patch.object(reuse, 'make_receipt', return_value={'id': 'minimal-synthetic-receipt'}):
            self.runtime.queue.put_nowait((capture, raw_save)); self.assertTrue(entered.wait(1))
            for _ in range(8): self.runtime.queue.put_nowait((capture, raw_save))
            job = {'id': JOB, 'request_fingerprint': capture[2], 'status': 'complete', '_persist_pending': False, '_future': Future()}
            owner = SimpleNamespace(items={JOB: job}, lock=RLock())
            self.runtime.after_ack(capture, owner, raw_save); job['_future'].set_result(None)
            self.assertEqual(self.runtime.queue.qsize(), 8)
            self.assertEqual(job['status'], 'complete')
            start = time.monotonic(); self.runtime.close(wait=False)
            self.assertLess(time.monotonic() - start, .5); self.assertEqual(self.runtime.queue.qsize(), 0)
            release.set(); self.runtime.close()
            self.assertFalse(self.runtime.worker.is_alive()); self.assertEqual(len(saved), 1)

    def test_source_snapshot_integrity_and_source_limits_are_fail_closed(self):
        source = {'id': 'own-web-page', 'text': 'Синтетическая страница', 'url': 'https://synthetic.invalid/page'}
        tenant, live, future, _ = self.save_job(sources=[source]); self.settle(live, future)
        self.assertIsNotNone(self.lookup())
        with patch.object(reuse, 'REF_LIMIT', 0):
            with self.assertRaises(AccessError): self.lookup()
        with closing(sqlite3.connect(tenant.store.path)) as db, db:
            raw = db.execute('SELECT data FROM source_snapshots').fetchone()[0]
            db.execute("UPDATE source_snapshots SET data=json_set(data,'$.text','изменённая страница')")
        with self.assertRaises(AccessError): self.lookup()
        with closing(sqlite3.connect(tenant.store.path)) as db, db:
            db.execute('UPDATE source_snapshots SET data=?', (raw,)); db.execute('DELETE FROM source_snapshots')
        with self.assertRaises(AccessError): self.lookup()

    def test_terminal_digest_tampering_missing_receipt_and_invalid_metadata(self):
        tenant, live, future, _ = self.save_job(); self.settle(live, future)
        with closing(sqlite3.connect(tenant.store.path)) as db, db:
            db.execute("UPDATE records SET data=json_set(data,'$.stage','подменённый этап') WHERE kind='model-job'")
        with self.assertRaises(AccessError): self.lookup()
        with closing(sqlite3.connect(tenant.store.path)) as db, db:
            db.execute('DELETE FROM records WHERE kind=?', (reuse.RECEIPT_KIND,))
        self.assertIsNone(self.lookup())

    def test_authentic_receipt_from_another_tenant_or_job_never_authorizes_own_tuple(self):
        own, live, future, _ = self.save_job(); self.settle(live, future)
        other, live, future, _ = self.save_job(tenant_id=OTHER); self.settle(live, future)
        self.assertIsNotNone(self.lookup(tenant=OTHER))
        receipt = other.store.get(reuse.RECEIPT_KIND, 'report-reuse-ack:' + JOB)
        with closing(sqlite3.connect(own.store.path)) as db, db:
            db.execute('UPDATE records SET data=? WHERE kind=?', (json.dumps(receipt), reuse.RECEIPT_KIND))
        with self.assertRaises(AccessError): self.lookup()
        _, live, future, _ = self.save_job(job_id='9' * 32, text='Другой синтетический текст'); self.settle(live, future)
        receipt = own.store.get(reuse.RECEIPT_KIND, 'report-reuse-ack:' + '9' * 32)
        with closing(sqlite3.connect(own.store.path)) as db, db:
            db.execute('UPDATE records SET data=? WHERE kind=? AND id=?', (json.dumps(receipt), reuse.RECEIPT_KIND, 'report-reuse-ack:' + JOB))
        with self.assertRaises(AccessError): self.lookup()

    def test_history_match_byte_deadline_limits_and_legacy_exclusion(self):
        _, live, future, _ = self.save_job(stamped=False); self.settle(live, future)
        self.assertIsNone(self.lookup())
        tenant, live, future, _ = self.save_job(job_id='d' * 32); self.settle(live, future)
        with patch.object(reuse, 'MATCH_LIMIT', 0):
            with self.assertRaises(AccessError): self.lookup()
        with patch.object(reuse, 'BYTE_LIMIT', 64):
            with self.assertRaises(AccessError): self.lookup()
        with patch.object(reuse, 'DEADLINE', 0):
            with self.assertRaises(AccessError): self.lookup()
        for index in range(129):
            tenant.store.save('report', {'text': 'другой текст', 'sources': [], 'created_at': '2099-01-01T00:00:00+00:00', 'id': f'{index:032x}'})
        with self.assertRaises(AccessError) as error: self.lookup('нет отчёта')
        self.assertEqual(error.exception.status, 503)

    def test_unavailable_database_and_symlink_do_not_create_or_follow_storage(self):
        self.assertIsNone(self.lookup()); self.assertFalse((self.root / 'tenants' / TENANT).exists())
        target = self.root / 'elsewhere'; target.mkdir()
        (self.root / 'tenants').mkdir(exist_ok=True)
        (self.root / 'tenants' / TENANT).symlink_to(target, target_is_directory=True)
        with self.assertRaises(AccessError): self.lookup()
        self.assertEqual(list(target.iterdir()), [])

    def test_busy_and_corrupt_tenant_database_fail_closed_with_no_long_wait(self):
        tenant, live, future, _ = self.save_job(); self.settle(live, future)
        with closing(sqlite3.connect(tenant.store.path)) as db:
            db.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            with self.assertRaises(AccessError): self.lookup()
            self.assertLess(time.monotonic() - started, .5)
            db.rollback()
        with closing(sqlite3.connect(tenant.store.path)) as db, db: db.execute('DROP TABLE source_snapshots')
        # Even an empty refs list must be in a database with its native schema.
        with closing(sqlite3.connect(tenant.store.path)) as db, db:
            db.execute("UPDATE records SET data=json_set(data,'$.sources_refs',json(?)) WHERE kind='report'", (json.dumps(['e' * 64]),))
        with self.assertRaises(AccessError): self.lookup()

    def test_actual_ninth_candidate_and_129_snapshot_refs_are_not_false_misses(self):
        for index in range(9): self.save_job(job_id=f'{index + 30:032x}')
        with self.assertRaises(AccessError): self.lookup()
        source = {'id': 'own-web-page', 'text': 'Синтетический снимок', 'url': 'https://synthetic.invalid'}
        _, live, future, _ = self.save_job(job_id='f' * 32, text='Другой точный текст', sources=[source] * 129)
        self.settle(live, future)
        with self.assertRaises(AccessError): self.lookup('Другой точный текст')


class ReuseAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.; self.runtime = ReportReuseRuntime('/unused', b'synthetic', lambda: self.fail('Admission persisted'), clock=lambda: self.now)
        self.addCleanup(self.runtime.close)

    def denied(self, tenant):
        with self.assertRaises(AccessError) as error:
            with self.runtime.admission(tenant): self.fail('Unexpected admission')
        self.assertEqual((error.exception.status, error.exception.code), (503, 'report_reuse_unavailable'))

    def test_token_refill_global_window_and_capacity_leave_no_active_leases(self):
        for _ in range(3):
            with self.runtime.admission(TENANT): pass
        self.denied(TENANT); self.now += 10
        with self.runtime.admission(TENANT): pass
        self.runtime.recent.clear()
        for index in range(60):
            with self.runtime.admission(f'{index:064x}'): pass
        self.denied(OTHER); self.now += 60
        with self.runtime.admission(OTHER): pass
        self.assertEqual(self.runtime.active, set())
        self.runtime.buckets = {f'{index:064x}': (3., self.now, self.now) for index in range(256)}
        self.denied(OTHER); self.assertEqual(len(self.runtime.buckets), 256)
        self.now += 121
        with self.runtime.admission(OTHER): pass
        self.assertEqual(len(self.runtime.buckets), 1)

    def test_inflight_contention_and_exception_release(self):
        with ExitStack() as stack:
            for index in range(4): stack.enter_context(self.runtime.admission(f'{index:064x}'))
            self.denied(OTHER); self.denied(f'{0:064x}')
        self.assertEqual(self.runtime.active, set())
        self.runtime.lock.acquire()
        try: self.denied(OTHER)
        finally: self.runtime.lock.release()
        with self.assertRaises(ValueError):
            with self.runtime.admission(OTHER): raise ValueError('synthetic request failure')
        self.assertNotIn(OTHER, self.runtime.active)


class ReusePhysicalAccountingTests(unittest.TestCase):
    def test_native_real_pipeline_lookup_open_replay_zero_and_explicit_new_id_six_more(self):
        from tests.test_integrated_pool_jobs import IntegratedPoolJobTests, TEXT as pipeline_text
        case = IntegratedPoolJobTests(); case.setUpClass(); case.setUp()
        try:
            client, tenant = case.client(); case.submit(client, JOB); job = case.finish(client, JOB)
            self.assertEqual(job['status'], 'complete')
            runtime = case.service.beta.tenants.report_reuse
            for _ in range(300):
                match = reuse.lookup(case.root, case.service.beta.key, tenant, pipeline_text)
                if match: break
                time.sleep(.01)
            self.assertIsNotNone(match); self.assertTrue(runtime.wait_idle())
            self.assertEqual(sum(path == 'search.json' for path, _ in case.events), 6)
            before = len(case.events)
            self.assertEqual(client.request('/api/reports/reuse', {'text': pipeline_text})[0], 200)
            self.assertEqual(client.request('/api/reports/' + match['report_id'])[0], 200)
            case.submit(client, JOB); self.assertEqual(len(case.events), before)
            case.submit(client, 'e' * 32); self.assertEqual(case.finish(client, 'e' * 32)['status'], 'complete')
            self.assertEqual(sum(path == 'search.json' for path, _ in case.events), 12)
            with closing(sqlite3.connect(case.root / 'search-budget.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0], 12)
                self.assertEqual(db.execute('SELECT count(*) FROM pool_calls').fetchone()[0], 12)
            # Vault/provider readiness and new consent are irrelevant to an old report.
            vault, _ = case.service.pool_admin.vault.read()
            case.service.pool_admin.vault._write('pending.json', {'previous': vault})
            before = len(case.events)
            self.assertIsNotNone(client.request('/api/reports/reuse', {'text': pipeline_text})[1]['match'])
            self.assertEqual(client.request('/api/reports/' + match['report_id'])[0], 200)
            self.assertEqual(len(case.events), before)
            admin = case.admin()
            self.assertEqual(admin.request('/api/dashboard/accounts/' + tenant + '/block', {'reason': 'Synthetic lookup boundary'})[0], 200)
            self.assertEqual(client.request('/api/reports/reuse', {'text': pipeline_text})[0], 403)
            self.assertEqual(client.request('/api/reports/' + match['report_id'])[0], 403)
            self.assertEqual(len(case.events), before)
        finally:
            case.tearDown(); case.doCleanups()


if __name__ == '__main__': unittest.main()
