"""One native synthetic fixture: reader privacy, protected state and authentic clone."""
from contextlib import closing
from hashlib import sha256
from http.client import HTTPResponse
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from plagiarism.matching import analyze
from plagiarism.saas import SESSION_COOKIE, report_identifier
from plagiarism.storage import Store
from plagiarism.tenants import PUBLIC_PACK_IDS
from tests import test_integrated_beta as native
from tests import test_integrated_pool_jobs as pool_native


TEXT = ('Синтетическая лаборатория наблюдала за учебными образцами. '
        'Исследователи сверяли условные измерения и описывали придуманные результаты опыта.')
JOB, OLD = '1' * 32, '2' * 32
RECEIPT_KIND, RECEIPT_ID = 'report-reuse-receipt', 'report-reuse-ack:' + JOB
LIMIT = 256 * 1024


class FallbackFoundationTests(unittest.TestCase):
    def test_native_privacy_and_protected_state_survive_authentic_clone(self):
        deadline = time.monotonic() + 30
        started = time.monotonic()
        samples, leaks, workers = [], [], []
        report_marker, receipt_marker = secrets.token_hex(32), secrets.token_hex(32)
        private = {'private-field': '_report_reuse', 'report-marker': report_marker,
                   'receipt-marker': receipt_marker, 'receipt-id': RECEIPT_ID, 'receipt-kind': RECEIPT_KIND}
        original_read = HTTPResponse.read
        original_connection = native.HTTPConnection
        case = pool_native.IntegratedPoolJobTests()

        def remaining():
            left = deadline - time.monotonic()
            self.assertGreater(left, 0, 'Native fixture exceeded its 30s deadline')
            return left

        def bounded_connection(*args, **kwargs):
            kwargs['timeout'] = min(kwargs.get('timeout', 5), remaining())
            return original_connection(*args, **kwargs)

        def bounded_read(response, amount=None):
            remaining()
            data = original_read(response, LIMIT + 1 if amount is None else min(amount, LIMIT + 1))
            self.assertLessEqual(len(data), LIMIT, 'Native response exceeds 256KiB')
            return data

        def observe(surface, value):
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.assertLessEqual(len(raw), LIMIT)
            found = [label for label, token in private.items() if token.encode() in raw]
            samples.append({'surface': surface, 'bytes': len(raw), 'private_tokens': found})
            if found: leaks.append({'surface': surface, 'private_tokens': found})

        def request(browser, path, body=None, *, allowed=(200,), surface=None):
            remaining()
            status, value, headers = browser.request(path, body)
            self.assertIn(status, allowed, (path, status))
            observe(surface or path, value)
            return value, headers

        def rowsets(root):
            remaining()
            result = {}
            for path in sorted(root.rglob('*.sqlite3')):
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
                    result[str(path.relative_to(root))] = {
                        name: db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid').fetchall()
                        for (name,) in tables}
            return result

        def digest(value):
            return sha256(repr(value).encode()).hexdigest()

        def snapshot(root, clone):
            inventory = {}
            for source in sorted(root.rglob('*')):
                self.assertFalse(source.is_symlink())
                self.assertTrue(source.resolve().is_relative_to(root.resolve()))
                if source.is_dir():
                    target = clone / source.relative_to(root)
                    target.mkdir(mode=0o700, parents=True, exist_ok=True); target.chmod(0o700)
                    continue
                self.assertTrue(stat.S_ISREG(source.stat().st_mode))
                if source.name.endswith(('-wal', '-shm', '-journal')): continue
                name = str(source.relative_to(root)); target = clone / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if source.suffix == '.sqlite3':
                    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as db, closing(sqlite3.connect(target)) as dest:
                        db.backup(dest)
                    inventory[name] = {'kind': 'sqlite-backup', 'mode': '0600'}
                else:
                    shutil.copyfile(source, target)
                    self.assertEqual(sha256(source.read_bytes()).digest(), sha256(target.read_bytes()).digest())
                    inventory[name] = {'kind': 'sidecar', 'sha256': sha256(target.read_bytes()).hexdigest(), 'mode': '0600'}
                target.chmod(0o600)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(set(inventory), {str(p.relative_to(clone)) for p in clone.rglob('*') if p.is_file()})
            required = {'private/user-access.sqlite3.key', 'runtime/guest-signing.key',
                        'runtime/user-access-initialized', 'runtime/pool-bootstrap.json', 'search-pool-vault/vault.json'}
            self.assertTrue(required <= set(inventory))
            for name in required: self.assertEqual((root / name).stat().st_mode & 0o777, 0o600)
            return inventory

        case.setUpClass()
        try:
            with patch.object(native, 'HTTPConnection', side_effect=bounded_connection), patch.object(HTTPResponse, 'read', bounded_read):
                case.setUp()
                case.calls = []
                self.assertTrue(all(key.startswith(('offline-', 'tvly-offline-')) for key in case.keys.values()))
                for provider, key in case.keys.items(): private['synthetic-key-' + provider] = key
                browser = native.IntegratedBetaTests.browser(case)
                tenant = native.IntegratedBetaTests.guest(case, browser)
                user = native.IntegratedBetaTests.register(case, browser, username='synthetic_foundation')
                self.assertEqual(native.IntegratedBetaTests.guest(case, browser), tenant)
                tenant_path = case.root / 'tenants' / tenant / 'istok.sqlite3'
                store = Store(tenant_path)
                # Valid already-initialized library state; no bundled real texts.
                store.save('tenant-bootstrap', {'id': 'public-source-packs-v1', 'packs': list(PUBLIC_PACK_IDS)})
                source = store.add_source('Синтетический учебный источник', TEXT, url='')

                def payload(text):
                    result = analyze(text, [source])
                    result.update(text=text, sources=[source], scope='web_search',
                        web={'provider': 'synthetic', 'status': 'partial', 'sources_checked': 1,
                             'queries_planned': 0, 'queries_completed': 0, 'urls_attempted': 0,
                             'errors': [], 'limitations': ['Только синтетическая fixture, без интернет-поиска.']})
                    result['stats'].update(uniqueness_percent=None, uniqueness_upper_bound_percent=0)
                    return result

                legacy = store.save('report', {**payload(TEXT), 'id': OLD, 'created_at': '2024-02-01T12:00:00+00:00'})
                self.assertTrue(legacy['matches'])
                store.review_match(OLD, legacy['matches'][0]['id'], 'confirmed', 'Синтетическая пометка')
                old_public, _ = request(browser, '/api/reports/' + OLD, surface='legacy-before')
                self.assertEqual(old_public['report']['created_at'], legacy['created_at'])

                def runner(text, identifier, progress, **kwargs):
                    case.calls.append((identifier, 'synthetic'))
                    return {**payload(text), '_report_reuse': {'version': 1, 'text_hmac': report_marker,
                        'job_id': JOB, 'request_fingerprint': '3' * 64}}
                case.service.beta.runner = runner
                submitted, _ = request(browser, '/api/web-check-jobs',
                    {'request_id': JOB, 'text': TEXT, 'internet_consent': True}, surface='POST-web-job')
                self.assertEqual(submitted['job']['id'], JOB)
                while True:
                    status, value, _ = browser.request('/api/jobs/' + JOB)
                    self.assertEqual(status, 200); observe('poll-web-job', value)
                    if value['job']['status'] in {'complete', 'failed', 'cancelled'}: break
                    time.sleep(min(.01, remaining()))
                self.assertEqual(value['job']['status'], 'complete')
                with closing(case.service.beta.connect()) as db:
                    pin = db.execute('SELECT pin_json FROM pool_job_pins WHERE guest=? AND request_id=?', (tenant, JOB)).fetchone()
                self.assertIsNotNone(pin); self.assertEqual(len(json.loads(pin[0])['active']), 4)
                new_id = report_identifier(JOB)
                with case.service.beta.tenants.context(tenant) as context:
                    context.jobs.items[JOB]['_future'].result(timeout=remaining())
                    saved = context.store.get('model-job', JOB)
                    context.store.save(RECEIPT_KIND, {'id': RECEIPT_ID, 'version': 1,
                        'namespace': 'istok-web-report-terminal-ack-v1', 'tenant': tenant,
                        'job_id': JOB, 'request_fingerprint': saved['request_fingerprint'],
                        'report_id': new_id, 'text_hmac': report_marker,
                        'terminal_digest': '4' * 64, 'signature': receipt_marker})
                    self.assertEqual(context.store.get('report', new_id)['_report_reuse']['text_hmac'], report_marker)
                    self.assertIsNone(context.store.get('report', RECEIPT_ID))
                    self.assertIsNone(context.store.get('model-job', RECEIPT_ID))
                revoked_token = case.service.accounts.access.ledger.create_session(user['id'])
                self.assertTrue(case.service.accounts.access.ledger.revoke_session(revoked_token))
                revoked = native.Browser(case); revoked.cookies = {**browser.cookies, SESSION_COOKIE: revoked_token}
                with closing(case.service.beta.connect()) as db, db:
                    db.execute('CREATE TABLE IF NOT EXISTS owner_session_revocations(token_sha256 TEXT PRIMARY KEY,expires_at INTEGER NOT NULL)')
                    db.execute('INSERT INTO owner_session_revocations VALUES (?,?)', ('5' * 64, int(time.time()) + 3600))
                baseline = rowsets(case.root)
                self.assertEqual(len(baseline[str(tenant_path.relative_to(case.root))]['sources']), 1)
                self.assertEqual(len(store.records('report')), 2)
                self.assertEqual(len(case.calls), 1)

                def read_all(phase):
                    for identifier in (OLD, new_id):
                        current, _ = request(browser, '/api/reports/' + identifier, surface=phase + ':report:' + identifier)
                        if identifier == OLD: self.assertEqual(current, old_public)
                        for path in ('/api/report-exports/' + identifier, '/reports/' + identifier + '.html'):
                            request(browser, path, surface=phase + ':HTML')
                    request(browser, '/api/jobs/' + JOB, surface=phase + ':job')
                    history, _ = request(browser, '/api/reports', surface=phase + ':history')
                    self.assertEqual(len(history['reports']), 2)
                    request(browser, '/api/data', surface=phase + ':data')
                    archive, _ = request(browser, '/api/data-export', surface=phase + ':ZIP')
                    with ZipFile(BytesIO(archive)) as exported:
                        self.assertTrue(exported.namelist())
                        for name in exported.namelist(): observe(phase + ':ZIP/' + name, exported.read(name))
                    request(browser, '/api/reports/' + RECEIPT_ID, allowed=(400, 403, 404), surface=phase + ':receipt-as-report')
                    request(browser, '/api/jobs/' + RECEIPT_ID, allowed=(400, 403, 404), surface=phase + ':receipt-as-job')
                    request(revoked, '/api/reports/' + new_id, allowed=(401,), surface=phase + ':revoked-session')

                read_all('live')
                self.assertEqual(rowsets(case.root), baseline)
                original_root = case.root
                workers.extend(case.service.beta.tenants.coordinator.executor._threads)
                with TemporaryDirectory(prefix='foundation-clone-') as directory:
                    clone = Path(directory) / 'state'; clone.mkdir(mode=0o700)
                    sidecars = snapshot(original_root, clone)
                    self.assertEqual(rowsets(clone), baseline)
                    case.stop(); case.root = clone
                    try:
                        case.start(restart=True)
                        self.assertEqual(native.IntegratedBetaTests.guest(case, browser), tenant)
                        self.assertEqual(rowsets(clone), baseline)
                        read_all('restarted')
                        after = rowsets(clone); self.assertEqual(after, baseline)
                        with case.service.beta.tenants.context(tenant) as context:
                            self.assertEqual(context.store.get('report', new_id)['_report_reuse']['text_hmac'], report_marker)
                            self.assertEqual(context.store.get(RECEIPT_KIND, RECEIPT_ID)['signature'], receipt_marker)
                        for name, detail in sidecars.items():
                            if detail['kind'] == 'sidecar':
                                self.assertEqual(sha256((clone / name).read_bytes()).hexdigest(), detail['sha256'])
                            self.assertEqual((clone / name).stat().st_mode & 0o777, 0o600)
                        workers.extend(case.service.beta.tenants.coordinator.executor._threads)
                    finally: case.stop()
                self.assertEqual(len(case.calls), 1)
                self.assertEqual(case.network, []); self.assertEqual(case.events, [])
                proof = {'source_startup': True, 'authentic_clone_startup': True,
                    'synthetic_engine_executions': len(case.calls), 'jobs': 1, 'reports': 2,
                    'protected_sql_rowsets_unchanged': True, 'protected_sql_sha256': digest(baseline),
                    'sidecars': sidecars, 'private_stamp_and_receipt_retained': True,
                    'outbound': 0, 'provider_adapter_calls': 0, 'native_pool_pin': True,
                    'samples': samples, 'privacy_leaks': leaks,
                    'source_sha256': {name: sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()
                        for name in ('server.py', 'plagiarism/data_management.py')}}
        finally:
            try: case.tearDown()
            finally: case.doCleanups()
        self.assertFalse(any(worker.is_alive() for worker in workers))
        proof.update(elapsed_seconds=round(time.monotonic() - started, 3), owned_workers_closed=True)
        remaining()
        print('FALLBACK_FOUNDATION_PROOF ' + json.dumps(proof, ensure_ascii=False, sort_keys=True))
        self.assertEqual(leaks, [], 'Private serializer leak after authentic fixture/clone startup')


if __name__ == '__main__': unittest.main()
