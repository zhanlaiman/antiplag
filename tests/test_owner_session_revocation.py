"""Proposed reader contract, exercised against real SQLite and loopback HTTP."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256, pbkdf2_hmac
import hmac
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
import unittest
from unittest.mock import Mock, patch

from cloud.web_beta import create_server
from plagiarism.access import AccessError
from plagiarism.web_search import SearchBudget
from scripts.check_beta_backup import verify_backup


class OwnerSessionFixture(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix='istok-owner-session-')
        self.root = Path(self.directory.name)
        salt = 'c3' * 32
        self.environment = patch.dict(os.environ, {
            'ISTOK_OWNER_USERNAME': 'synthetic-owner', 'ISTOK_OWNER_SALT': salt,
            'ISTOK_OWNER_DIGEST': pbkdf2_hmac('sha256', b'local-only-password', bytes.fromhex(salt), 600000).hex(),
        })
        self.environment.start()
        self.runner = Mock(side_effect=AssertionError('Search execution forbidden'))
        self.start()
        _, session, guest = self.http('/api/session')
        self.owner_id = session['beta']['id']
        self.guest_cookie = guest.split(';')[0]
        self.token_a = self.login(self.guest_cookie)
        self.token_b = self.login(self.token_a)
        self.assertNotEqual(self.token_a, self.token_b)
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_a}), self.owner_id)
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)

    def start(self):
        self.service = create_server(self.root, 'http://beta.example', host='127.0.0.1', port=0,
                                     allow_http=True, runner=self.runner)
        self.runtime = self.service.beta
        self.thread = Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.service.shutdown()
        self.service.server_close()
        self.runtime.tenants.close()
        self.thread.join()

    def tearDown(self):
        try:
            self.runner.assert_not_called()
        finally:
            self.stop()
            self.environment.stop()
            self.directory.cleanup()

    def http(self, path, body=None, cookie=None, *, origin='http://beta.example', host='beta.example', authorization=None):
        connection = HTTPConnection('127.0.0.1', self.service.server_port, timeout=5)
        headers = {'Host': host}
        if origin is not None: headers['Origin'] = origin
        if cookie: headers['Cookie'] = cookie
        if authorization: headers['Authorization'] = authorization
        if body is not None: headers['Content-Type'] = 'application/json'
        try:
            connection.request('POST' if body is not None else 'GET', path,
                               json.dumps(body).encode() if body is not None else None, headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}, response.getheader('Set-Cookie')
        finally:
            connection.close()

    def login(self, cookie):
        status, payload, response_cookie = self.http('/api/owner/login', {
            'username': 'synthetic-owner', 'password': 'local-only-password',
        }, cookie)
        self.assertEqual(status, 200, payload)
        return 'istok_owner=' + response_cookie.split('istok_owner=', 1)[1].split(';')[0]

    @staticmethod
    def digest(cookie):
        return sha256(cookie.split('=', 1)[1].encode()).hexdigest()

    def seed_revocation(self, cookie=None, expires_at=None):
        cookie = cookie or self.token_a
        expiry = int(cookie.split('.')[1]) if expires_at is None else expires_at
        with closing(self.runtime.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS owner_session_revocations(token_sha256 TEXT PRIMARY KEY,expires_at INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO owner_session_revocations VALUES (?,?)', (self.digest(cookie), expiry))

    def table_exists(self):
        with closing(self.runtime.connect()) as db:
            return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_session_revocations'").fetchone())

    def logical_state(self):
        with closing(self.runtime.connect()) as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='owner_session_revocations' ORDER BY name")]
            return {name: db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall() for name in tables}


class OwnerSessionReaderTests(OwnerSessionFixture):
    def test_legacy_snapshot_remains_valid_without_startup_or_read_ddl(self):
        self.assertFalse(self.table_exists())
        before = self.logical_state()
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_a}), self.owner_id)
        self.stop(); self.start()
        self.assertFalse(self.table_exists())
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)
        self.assertEqual(self.logical_state(), before)

    def test_full_token_hash_denies_only_a_and_survives_restart_without_data_changes(self):
        report_id = '1' * 32
        with self.runtime.tenants.context(self.owner_id) as tenant:
            tenant.store.save('report', {'id': report_id, 'text': 'Synthetic preserved report', 'matches': [], 'sources': [], 'stats': {}})
        api_key = self.runtime.api_keys.create(self.owner_id, 'Synthetic preserved key', ['reports:read'])['token']
        before = self.logical_state()
        signing_key = (self.runtime.runtime / 'guest-signing.key').read_bytes()
        saved = self.http('/api/reports/' + report_id, cookie=self.token_a)[1]
        self.seed_revocation()
        self.assertIsNone(self.runtime.owner_identity({'Cookie': self.token_a}))
        self.assertFalse(self.http('/api/session', cookie=self.token_a)[1]['beta']['owner'])
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)
        self.assertEqual(self.http('/api/reports/' + report_id, cookie=self.token_b)[1], saved)
        self.assertEqual(self.logical_state(), before)
        self.stop(); self.start()
        self.assertIsNone(self.runtime.owner_identity({'Cookie': self.token_a}))
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)
        self.assertEqual(self.http('/api/reports/' + report_id, cookie=self.token_b)[1], saved)
        self.assertEqual((self.runtime.runtime / 'guest-signing.key').read_bytes(), signing_key)
        self.assertEqual(self.logical_state(), before)
        self.assertEqual(self.http('/api/v1/reports/' + report_id, origin=None, authorization='Bearer ' + api_key)[0], 200)

    def test_expiry_metadata_cannot_restore_a_revoked_valid_token(self):
        self.seed_revocation(expires_at=1)
        self.assertIsNone(self.runtime.owner_identity({'Cookie': self.token_a}))
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)

    def test_hash_must_cover_the_complete_verified_token(self):
        partial = self.token_a.split('=', 1)[1].rsplit('.', 1)[0]
        self.seed_revocation()
        with closing(self.runtime.connect()) as db, db:
            db.execute('UPDATE owner_session_revocations SET token_sha256=?', (sha256(partial.encode()).hexdigest(),))
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_a}), self.owner_id)
        self.seed_revocation()
        self.assertIsNone(self.runtime.owner_identity({'Cookie': self.token_a}))

    def test_malformed_revocation_schema_fails_closed_before_admission(self):
        with closing(self.runtime.connect()) as db, db:
            db.execute('CREATE TABLE owner_session_revocations(wrong_column TEXT)')
        before = self.logical_state()
        status, result, _ = self.http('/api/session', cookie=self.token_a)
        self.assertEqual((status, result.get('code')), (503, 'owner_session_unavailable'))
        self.assertEqual(self.logical_state(), before)

    def test_missing_database_fails_closed_without_creating_an_empty_database(self):
        original = self.runtime.database
        moved = original.with_suffix('.held')
        original.rename(moved)
        try:
            with self.assertRaises(AccessError) as caught:
                self.runtime.owner_identity({'Cookie': self.token_a})
            self.assertEqual((caught.exception.status, caught.exception.code), (503, 'owner_session_unavailable'))
            self.assertFalse(original.exists())
        finally:
            moved.rename(original)

    def test_corrupt_database_fails_closed(self):
        original = self.runtime.database.read_bytes()
        self.runtime.database.write_bytes(b'Synthetic invalid SQLite bytes')
        try:
            with self.assertRaises(AccessError) as caught:
                self.runtime.owner_identity({'Cookie': self.token_a})
            self.assertEqual(caught.exception.status, 503)
        finally:
            self.runtime.database.write_bytes(original)

    def test_database_open_failure_fails_closed(self):
        with patch('cloud.web_beta.sqlite3.connect', side_effect=OSError('Synthetic open failure')):
            with self.assertRaises(AccessError) as caught:
                self.runtime.owner_identity({'Cookie': self.token_a})
            self.assertEqual(caught.exception.status, 503)

    def test_invalid_expired_or_missing_cookie_never_creates_revocation_state(self):
        expired_value = self.owner_id + '.1.synthetic'
        expired = 'istok_owner=' + expired_value + '.' + hmac.new(self.runtime.key, ('owner.' + expired_value).encode(), sha256).hexdigest()
        forged = self.token_a[:-1] + ('0' if self.token_a[-1] != '0' else '1')
        before = self.logical_state()
        for cookie in ['', 'istok_owner=invalid', forged, expired]:
            with self.subTest(cookie_kind='expired' if cookie == expired else 'invalid-or-absent'):
                self.assertIsNone(self.runtime.owner_identity({'Cookie': cookie}))
        self.assertFalse(self.table_exists())
        self.assertEqual(self.logical_state(), before)


class CommitFault:
    def __init__(self, connection, committed):
        self.connection, self.committed = connection, committed
    def execute(self, *args): return self.connection.execute(*args)
    def __enter__(self): return self
    def __exit__(self, kind, value, traceback):
        if kind:
            self.connection.rollback()
            return False
        if self.committed: self.connection.commit()
        else: self.connection.rollback()
        raise sqlite3.OperationalError('Synthetic uncertain commit' if self.committed else 'Synthetic failed commit')
    def close(self): self.connection.close()


class OwnerSessionWriterTests(OwnerSessionFixture):
    def rows(self):
        if not self.table_exists(): return []
        with closing(self.runtime.connect()) as db:
            return db.execute('SELECT token_sha256,expires_at FROM owner_session_revocations ORDER BY token_sha256').fetchall()

    def test_logout_revokes_only_a_and_keeps_other_nonce_api_key_data_and_new_login(self):
        report_id = '2' * 32
        with self.runtime.tenants.context(self.owner_id) as tenant:
            tenant.store.save('report', {'id': report_id, 'text': 'Synthetic unchanged report', 'matches': [], 'sources': [], 'stats': {}})
        token = self.runtime.api_keys.create(self.owner_id, 'Synthetic unaffected key', ['reports:read'])['token']
        saved = self.http('/api/reports/' + report_id, cookie=self.token_b)[1]
        before = self.logical_state()
        key = (self.runtime.runtime / 'guest-signing.key').read_bytes()
        status, result, cookie = self.http('/api/owner/logout', {'token': self.token_b}, self.token_a)
        self.assertEqual((status, result), (200, {'ok': True}))
        self.assertIn('Max-Age=0', cookie)
        self.assertNotIn('istok_beta=', cookie)
        self.assertIsNone(self.runtime.owner_identity({'Cookie': self.token_a}))
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)
        self.assertEqual(self.logical_state(), before)
        self.assertEqual(self.http('/api/reports/' + report_id, cookie=self.token_b)[1], saved)
        self.assertEqual(self.http('/api/v1/reports/' + report_id, origin=None, authorization='Bearer ' + token)[0], 200)
        self.stop(); self.start()
        self.assertIsNone(self.runtime.owner_identity({'Cookie': self.token_a}))
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)
        self.assertEqual((self.runtime.runtime / 'guest-signing.key').read_bytes(), key)
        new_token = self.login(self.token_b)
        self.assertNotEqual(new_token, self.token_a)
        self.assertEqual(self.runtime.owner_identity({'Cookie': new_token}), self.owner_id)
        self.assertEqual(self.http('/api/reports/' + report_id, cookie=new_token)[1], saved)
        self.assertEqual(self.rows(), [(self.digest(self.token_a), int(self.token_a.split('.')[1]))])
        self.assertNotIn(self.token_a.split('=', 1)[1].encode(), self.runtime.database.read_bytes())
        with closing(self.runtime.connect()) as db:
            self.assertEqual([row[1] for row in db.execute('PRAGMA table_info(owner_session_revocations)')], ['token_sha256', 'expires_at'])

    def test_logout_bypasses_only_admission_and_mutation_at_both_caps(self):
        month = datetime.now(timezone.utc).strftime('%Y-%m')
        with closing(self.runtime.connect()) as db, db:
            db.execute('UPDATE rates SET count=40 WHERE guest=?', (self.owner_id,))
            db.executemany('INSERT OR IGNORE INTO active_guests VALUES (?,?)', [(format(i, '064x'), month) for i in range(199)])
            self.assertEqual(db.execute('SELECT count(*) FROM active_guests WHERE month=?', (month,)).fetchone()[0], 200)
        before = self.logical_state()
        tenants = set((self.root / 'tenants').glob('*/istok.sqlite3'))
        with patch.object(self.runtime, 'admit_space', side_effect=AssertionError('Logout must not admit')) as admission, \
                patch.object(self.runtime, 'mutation', side_effect=AssertionError('Logout must not charge')) as mutation, \
                patch.object(self.runtime.tenants, 'context', side_effect=AssertionError('Logout must not lease a tenant')) as lease, \
                patch.object(self.runtime, '_check_storage', side_effect=AccessError('Synthetic full storage', 'beta_storage_limit', 429)) as storage:
            self.assertEqual(self.http('/api/owner/logout', {}, self.token_a)[0], 200)
            admission.assert_not_called(); mutation.assert_not_called(); lease.assert_not_called(); storage.assert_not_called()
        self.assertEqual(self.logical_state(), before)
        self.assertEqual(set((self.root / 'tenants').glob('*/istok.sqlite3')), tenants)
        self.assertIsNone(self.runtime.owner_identity({'Cookie': self.token_a}))

    def test_invalid_expired_and_no_cookie_clear_without_tenant_quota_or_denylist(self):
        expired_value = self.owner_id + '.1.synthetic'
        expired = 'istok_owner=' + expired_value + '.' + hmac.new(self.runtime.key, ('owner.' + expired_value).encode(), sha256).hexdigest()
        forged = self.token_a[:-1] + ('0' if self.token_a[-1] != '0' else '1')
        before, tenants = self.logical_state(), set((self.root / 'tenants').glob('*/istok.sqlite3'))
        with patch.object(self.runtime, 'persist') as persist:
            for cookie in [None, 'istok_owner=invalid', forged, expired]:
                status, result, cleared = self.http('/api/owner/logout', {}, cookie)
                self.assertEqual((status, result), (200, {'ok': True}))
                self.assertIn('Max-Age=0', cleared)
                self.assertNotIn('istok_beta=', cleared)
            persist.assert_not_called()
        self.assertFalse(self.table_exists())
        self.assertEqual(self.logical_state(), before)
        self.assertEqual(set((self.root / 'tenants').glob('*/istok.sqlite3')), tenants)
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)

    def test_reordered_compatible_columns_revoke_the_correct_token(self):
        with closing(self.runtime.connect()) as db, db:
            db.execute('CREATE TABLE owner_session_revocations(expires_at INTEGER NOT NULL,token_sha256 TEXT PRIMARY KEY)')
        self.assertEqual(self.http('/api/owner/logout', {}, self.token_a)[0], 200)
        self.assertEqual(self.rows(), [(self.digest(self.token_a), int(self.token_a.split('.')[1]))])
        self.assertIsNone(self.runtime.owner_identity({'Cookie': self.token_a}))
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)

    def test_unrelated_unique_constraint_never_acknowledges_an_unrecorded_token(self):
        fields = self.token_b.split('=', 1)[1].split('.')
        fields[1] = self.token_a.split('.')[1]
        value = '.'.join(fields[:3])
        other = 'istok_owner=' + value + '.' + hmac.new(self.runtime.key, ('owner.' + value).encode(), sha256).hexdigest()
        self.assertEqual(self.runtime.owner_identity({'Cookie': other}), self.owner_id)
        with closing(self.runtime.connect()) as db, db:
            db.execute('CREATE TABLE owner_session_revocations(token_sha256 TEXT PRIMARY KEY,expires_at INTEGER NOT NULL UNIQUE)')
        self.assertEqual(self.http('/api/owner/logout', {}, self.token_a)[0], 200)
        status, result, cleared = self.http('/api/owner/logout', {}, other)
        self.assertEqual((status, result.get('code')), (503, 'owner_logout_unavailable'))
        self.assertIsNone(cleared)
        self.assertIsNone(self.runtime.owner_identity({'Cookie': self.token_a}))
        self.assertEqual(self.runtime.owner_identity({'Cookie': other}), self.owner_id)
        self.assertEqual(self.rows(), [(self.digest(self.token_a), int(self.token_a.split('.')[1]))])

    def test_repeat_logout_has_one_row_and_flushes_each_explicit_attempt(self):
        before = self.logical_state()
        with patch.object(self.runtime, 'persist') as persist:
            for _ in range(2):
                status, _, cleared = self.http('/api/owner/logout', {}, self.token_a)
                self.assertEqual(status, 200); self.assertIn('Max-Age=0', cleared)
            self.assertEqual(persist.call_count, 2)
        self.assertEqual(self.rows(), [(self.digest(self.token_a), int(self.token_a.split('.')[1]))])
        self.assertEqual(self.logical_state(), before)

    def test_untrusted_and_malformed_requests_never_revoke_or_skip_blanket_persist(self):
        before = self.logical_state()
        with patch.object(self.runtime, 'persist') as persist:
            for fields in [{'host': 'evil.example'}, {'origin': 'https://evil.example'}, {'origin': None}]:
                status, _, cleared = self.http('/api/owner/logout', {}, self.token_a, **fields)
                self.assertEqual(status, 403); self.assertNotIn('istok_owner=', cleared or '')
            for content_type, body in [('text/plain', b'{}'), ('application/json', b'{'), ('application/json', b'[]')]:
                connection = HTTPConnection('127.0.0.1', self.service.server_port, timeout=5)
                try:
                    connection.request('POST', '/api/owner/logout', body, {'Host': 'beta.example', 'Origin': 'http://beta.example', 'Cookie': self.token_a, 'Content-Type': content_type})
                    response = connection.getresponse(); response.read()
                    self.assertEqual(response.status, 400)
                    self.assertNotIn('istok_owner=', response.getheader('Set-Cookie') or '')
                finally: connection.close()
            self.assertEqual(persist.call_count, 6)
        self.assertFalse(self.table_exists())
        self.assertEqual(self.logical_state(), before)
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_a}), self.owner_id)

    def test_failure_before_commit_is_not_acknowledged_or_marked_revoked(self):
        before = self.logical_state()
        connect = sqlite3.connect
        with patch('cloud.web_beta.sqlite3.connect', side_effect=lambda *args, **kwargs: CommitFault(connect(*args, **kwargs), False)), patch.object(self.runtime, 'persist') as persist:
            status, result, cleared = self.http('/api/owner/logout', {}, self.token_a)
            self.assertEqual((status, result.get('code')), (503, 'owner_logout_unavailable'))
            self.assertIsNone(cleared)
            persist.assert_not_called()
        self.assertFalse(self.table_exists())
        self.assertEqual(self.logical_state(), before)
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_a}), self.owner_id)

    def test_commit_then_exception_keeps_revocation_and_explicit_retry_reflushes(self):
        before = self.logical_state()
        connect = sqlite3.connect
        with patch('cloud.web_beta.sqlite3.connect', side_effect=lambda *args, **kwargs: CommitFault(connect(*args, **kwargs), True)), patch.object(self.runtime, 'persist') as persist:
            status, result, cleared = self.http('/api/owner/logout', {}, self.token_a)
            self.assertEqual((status, result.get('code')), (503, 'owner_logout_unavailable'))
            self.assertIsNone(cleared); persist.assert_not_called()
        self.assertIsNone(self.runtime.owner_identity({'Cookie': self.token_a}))
        self.assertEqual(self.logical_state(), before)
        with patch.object(self.runtime, 'persist') as persist:
            self.assertEqual(self.http('/api/owner/logout', {}, self.token_a)[0], 200)
            persist.assert_called_once_with()
        self.assertEqual(len(self.rows()), 1)

    def test_missing_database_is_not_recreated_or_acknowledged_by_logout(self):
        original = self.runtime.database
        moved = original.with_suffix('.held')
        before = self.logical_state()
        original.rename(moved)
        try:
            status, payload, cookie = self.http('/api/owner/logout', {}, self.token_a)
            self.assertEqual((status, payload.get('code')), (503, 'owner_logout_unavailable'))
            self.assertIsNone(cookie)
            self.assertFalse(original.exists())
        finally:
            if original.exists():
                original.rename(original.with_suffix('.unexpected-new'))
            moved.rename(original)
        self.assertEqual(self.logical_state(), before)
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_a}), self.owner_id)

    def test_persist_failure_before_or_after_external_commit_never_deletes_local_revocation(self):
        for copied_first in [False, True]:
            token = self.login(self.token_b)
            before = self.logical_state()
            with TemporaryDirectory(prefix='istok-owner-persist-') as directory:
                snapshot = Path(directory) / 'beta.sqlite3'
                def uncertain():
                    if copied_first:
                        with closing(self.runtime.connect()) as original, closing(sqlite3.connect(snapshot)) as destination:
                            original.backup(destination)
                    raise RuntimeError('Synthetic unknown persistence outcome')
                with patch.object(self.runtime, 'persist', side_effect=uncertain) as persist:
                    status, result, cleared = self.http('/api/owner/logout', {}, token)
                    self.assertEqual((status, result.get('code')), (503, 'owner_logout_unavailable'))
                    self.assertIsNone(cleared); persist.assert_called_once_with()
                self.assertIsNone(self.runtime.owner_identity({'Cookie': token}))
                self.assertEqual(self.logical_state(), before)
                if copied_first:
                    with closing(sqlite3.connect(snapshot)) as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM owner_session_revocations WHERE token_sha256=?', (self.digest(token),)).fetchone()[0], 1)
                with patch.object(self.runtime, 'persist') as persist:
                    self.assertEqual(self.http('/api/owner/logout', {}, token)[0], 200)
                    persist.assert_called_once_with()
                self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)

    def test_concurrent_duplicate_logout_has_one_revocation_and_preserves_state(self):
        before = self.logical_state()
        barrier = Barrier(2)
        def logout(_):
            barrier.wait()
            return self.http('/api/owner/logout', {}, self.token_a)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(logout, range(2))), [200, 200])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.logical_state(), before)
        self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)

    def test_backup_and_restore_preserve_revoked_a_valid_b_api_key_reports_and_ledger(self):
        report_id = '3' * 32
        with self.runtime.tenants.context(self.owner_id) as tenant:
            tenant.store.save('report', {'id': report_id, 'text': 'Synthetic backup report', 'matches': [], 'sources': [], 'stats': {}})
        api_key = self.runtime.api_keys.create(self.owner_id, 'Synthetic backup key', ['reports:read'])['token']
        budget = SearchBudget(self.root / 'search-budget.sqlite3', limit=180)
        budget.reserve('synthetic-reserved-ledger-entry')
        saved = self.http('/api/reports/' + report_id, cookie=self.token_b)[1]
        before = self.logical_state()
        signing_key = (self.runtime.runtime / 'guest-signing.key').read_bytes()
        self.assertEqual(self.http('/api/owner/logout', {}, self.token_a)[0], 200)
        self.assertEqual(self.logical_state(), before)
        with TemporaryDirectory(prefix='istok-owner-backup-') as directory:
            snapshot, restored = Path(directory) / 'snapshot', Path(directory) / 'restored'
            snapshot.mkdir()
            for original in self.root.rglob('*.sqlite3'):
                destination = snapshot / original.relative_to(self.root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(original.resolve().as_uri() + '?mode=ro', uri=True)) as source, closing(sqlite3.connect(destination)) as target:
                    source.backup(target)
            (snapshot / 'runtime').mkdir(exist_ok=True)
            shutil.copyfile(self.runtime.runtime / 'guest-signing.key', snapshot / 'runtime/guest-signing.key')
            entries = [{'path': str(path.relative_to(snapshot)), 'bytes': path.stat().st_size, 'sha256': sha256(path.read_bytes()).hexdigest()} for path in snapshot.rglob('*') if path.is_file()]
            (snapshot / 'manifest.json').write_text(json.dumps({'files': entries}))
            self.assertEqual(verify_backup(snapshot)['unfinished_or_unresolved_jobs'], 0)
            shutil.copytree(snapshot, restored)
            self.assertEqual(verify_backup(restored), verify_backup(snapshot))
            original_root = self.root
            self.stop(); self.root = restored; self.start()
            try:
                self.assertEqual(self.logical_state(), before)
                self.assertIsNone(self.runtime.owner_identity({'Cookie': self.token_a}))
                self.assertEqual(self.runtime.owner_identity({'Cookie': self.token_b}), self.owner_id)
                self.assertEqual((self.runtime.runtime / 'guest-signing.key').read_bytes(), signing_key)
                self.assertEqual(self.http('/api/reports/' + report_id, cookie=self.token_b)[1], saved)
                with closing(sqlite3.connect(self.root / 'search-budget.sqlite3')) as db:
                    self.assertEqual(db.execute('SELECT id,state FROM calls').fetchall(), [('synthetic-reserved-ledger-entry', 'started')])
                self.assertEqual(self.http('/api/v1/reports/' + report_id, origin=None, authorization='Bearer ' + api_key)[0], 200)
            finally:
                self.stop(); self.root = original_root; self.start()
