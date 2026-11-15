"""Proposed reader contract, exercised against real SQLite and loopback HTTP."""
from contextlib import closing
from hashlib import sha256, pbkdf2_hmac
import hmac
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from cloud.web_beta import create_server
from plagiarism.access import AccessError


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
