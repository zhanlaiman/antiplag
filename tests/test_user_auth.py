"""Local user registration through real SQLite, HTTP and parent-integration adapter."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import ContextVar
from hashlib import pbkdf2_hmac
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread, current_thread
import time
import unittest
from unittest.mock import Mock, patch

from cloud import dashboard, user_auth
from cloud.web_beta import create_server
from plagiarism.access import AccessError
from plagiarism import password_auth, user_access
from plagiarism.user_access import UserAccess
from tests.test_dashboard import IntegratedHandler


class Browser:
    def __init__(self, service):
        self.service, self.cookies, self.csrf = service, {}, ''

    def request(self, path, body=None, *, extra=None, origin='http://beta.example', csrf=True, raw=None, method=None):
        headers = {'Host': 'beta.example', 'Cookie': '; '.join(f'{k}={v}' for k, v in self.cookies.items())}
        if origin is not None: headers['Origin'] = origin
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers['Content-Type'] = 'application/json'
            if csrf: headers['X-Istok-CSRF'] = self.csrf
        headers.update(extra or {})
        connection = HTTPConnection('127.0.0.1', self.service.server_port, timeout=10)
        try:
            connection.request(method or ('POST' if data is not None else 'GET'), path, data, headers)
            response = connection.getresponse(); content = response.read(); pairs = response.getheaders()
            for key, value in pairs:
                if key.lower() == 'set-cookie':
                    cookies = SimpleCookie(); cookies.load(value)
                    for name, item in cookies.items():
                        if item['max-age'] == '0': self.cookies.pop(name, None)
                        else: self.cookies[name] = item.value
            result = json.loads(content) if response.getheader('Content-Type', '').startswith('application/json') else content
            if isinstance(result, dict) and 'csrf_token' in result: self.csrf = result['csrf_token']
            return response.status, result, dict(pairs)
        finally:
            connection.close()

    def bootstrap(self):
        return self.request('/api/account/session')


class UserAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = secrets.token_urlsafe(28)
        cls.admin_password = secrets.token_urlsafe(28); salt = secrets.token_hex(32)
        cls.admin_config = dashboard.DashboardConfig('qa_admin_fixture', salt, dashboard.password_digest(cls.admin_password, salt))

    def setUp(self):
        self.temp = TemporaryDirectory(); self.root = Path(self.temp.name); self.clock = time.time(); self.calls = []
        self.environment = patch.dict(os.environ, {}); self.environment.start()
        self.start()

    def start(self, *, registration_enabled=True):
        self.service = create_server(self.root, 'http://beta.example', host='127.0.0.1', port=0, allow_http=True,
                                     runner=lambda *args, **kwargs: self.calls.append(args))
        self.service.RequestHandlerClass = IntegratedHandler
        self.admin = dashboard.DashboardRuntime(self.service.beta, self.admin_config, now=lambda: self.clock)
        self.service.dashboard = self.admin
        self.auth = user_auth.UserAuthRuntime(self.service.beta, registration_enabled=registration_enabled, now=lambda: self.clock)
        self.service.accounts = self.auth
        owner_context = ContextVar('owner_adoption', default=False)
        original_login, original_admit = self.service.beta.owner_login, self.service.beta._admit_space
        def owner_login(body, guest):
            token = owner_context.set(True)
            try: return original_login(body, guest)
            finally: owner_context.reset(token)
        def admit(db, guest, month):
            if owner_context.get(): self.auth.guard_owner_adoption(guest)
            dashboard.guard_account(self.service.beta, guest)
            return original_admit(db, guest, month)
        self.service.beta.owner_login, self.service.beta._admit_space = owner_login, admit
        self.thread = Thread(target=self.service.serve_forever, daemon=True); self.thread.start()

    def stop(self):
        self.service.shutdown(); self.service.server_close(); self.service.beta.tenants.close(); self.thread.join()

    def tearDown(self):
        self.stop(); self.environment.stop(); self.temp.cleanup()

    def browser(self):
        browser = Browser(self.service); status, payload, _ = browser.bootstrap(); self.assertEqual(status, 200, payload)
        return browser

    def guest_id(self, browser):
        return self.service.beta.identity({'Cookie': '; '.join(f'{k}={v}' for k, v in browser.cookies.items())})[0]

    def register(self, browser, name='first_user'):
        status, payload, _ = browser.request('/api/account/register', {'username': name, 'password': self.password})
        self.assertEqual(status, 200, payload); return payload['user']

    def seed_report(self, guest, name='fixture-report', characters=51):
        self.service.beta.admit_space(guest)
        with self.service.beta.tenants.context(guest) as tenant:
            tenant.store.save('report', {'id': name, 'text': 'PRIVATE_ACCOUNT_TEXT', 'stats': {'total_characters': characters}})

    def admin_browser(self):
        browser = Browser(self.service)
        status, payload, _ = browser.request('/api/dashboard/login', {'username': self.admin_config.username, 'password': self.admin_password})
        self.assertEqual(status, 200); return browser, payload['csrf_token']

    def test_registration_preserves_data_and_never_claims_verified_email_or_owner(self):
        browser = self.browser(); guest = self.guest_id(browser); self.seed_report(guest)
        user = self.register(browser)
        self.assertEqual(user['guest_id'], guest); self.assertNotEqual(user['id'], guest)
        self.assertFalse(user['email_verified']); self.assertEqual(user['auth_method'], 'password')
        self.assertEqual(user['plan'], 'free')
        status, session, _ = browser.request('/api/session'); self.assertEqual(status, 200)
        self.assertEqual(session['beta']['id'], guest); self.assertFalse(session['beta']['owner'])
        self.assertEqual(session['beta']['max_characters'], 10000)
        status, reports, _ = browser.request('/api/reports'); self.assertEqual(status, 200)
        self.assertIn('fixture-report', json.dumps(reports))
        for path, expected in (('/api/owner/statistics', 403), ('/api/api-keys', 403), ('/api/dashboard/accounts', 401)):
            self.assertEqual(browser.request(path)[0], expected, path)
        with self.auth.access.ledger.connect() as db:
            row = db.execute('SELECT email_key FROM accounts').fetchone(); self.assertEqual(row[0], '')
            values = json.dumps([tuple(row) for row in db.execute('SELECT account_id,login_key,salt,digest FROM local_credentials')])
            self.assertNotIn('first_user', values); self.assertNotIn(self.password, values)
        self.assertEqual(self.calls, [])

    def test_old_guest_cookie_denied_after_claim_logout_expiry_and_restart(self):
        browser = self.browser(); old = dict(browser.cookies); guest = self.guest_id(browser)
        self.seed_report(guest); user = self.register(browser)
        replay = Browser(self.service); replay.cookies = old
        for path in ('/api/session', '/api/status', '/api/reports', '/reports/' + 'a' * 32 + '.html'):
            status, payload, _ = replay.request(path)
            self.assertEqual(status, 401, path); self.assertEqual(payload['code'], 'account_login_required')
        account_cookie = browser.cookies['istok_session']
        self.clock += 43201
        self.assertEqual(browser.request('/api/reports')[0], 401)
        self.clock -= 43201
        self.assertEqual(browser.request('/api/account/logout', {})[0], 200)
        self.assertNotEqual(self.guest_id(browser), guest)
        self.assertEqual(replay.request('/api/reports')[0], 401)
        browser.cookies['istok_session'] = account_cookie
        self.assertFalse(browser.bootstrap()[1]['authenticated'])
        self.stop(); self.start(registration_enabled=False)
        replay.service = self.service
        self.assertEqual(replay.request('/api/session')[0], 401)
        self.assertEqual(self.auth.public_bindings()[0]['guest_id'], user['guest_id'])

    def test_login_restores_original_space_never_merges_other_guest_and_logout_preserves_it(self):
        first = self.browser(); original = self.guest_id(first); self.seed_report(original, 'original')
        user = self.register(first)
        second = self.browser(); other = self.guest_id(second); self.seed_report(other, 'other-browser')
        status, result, _ = second.request('/api/account/login', {'username': 'FIRST_USER', 'password': self.password})
        self.assertEqual(status, 200); self.assertEqual(result['user'], user)
        self.assertEqual(second.request('/api/session')[1]['beta']['id'], original)
        reports = json.dumps(second.request('/api/reports')[1]); self.assertIn('original', reports); self.assertNotIn('other-browser', reports)
        self.assertEqual(second.request('/api/account/logout', {})[0], 200)
        self.assertEqual(second.request('/api/session')[1]['beta']['id'], other)
        self.assertIn('other-browser', json.dumps(second.request('/api/reports')[1]))
        self.assertTrue(first.bootstrap()[1]['authenticated'])

    def test_reader_disables_registration_but_retains_login_logout_resolver_and_guard(self):
        first = self.browser(); user = self.register(first)
        self.stop(); self.start(registration_enabled=False)
        first.service = self.service
        self.assertEqual(first.request('/api/session')[1]['beta']['id'], user['guest_id'])
        second = self.browser(); self.assertFalse(second.bootstrap()[1]['registration_enabled'])
        status, payload, _ = second.request('/api/account/register', {'username': 'second_user', 'password': self.password})
        self.assertEqual(status, 503); self.assertEqual(payload['code'], 'user_registration_disabled')
        self.assertEqual(second.request('/api/account/login', {'username': 'first_user', 'password': self.password})[0], 200)
        self.assertEqual(second.request('/api/account/logout', {})[0], 200)
        with self.auth.access.ledger.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM accounts').fetchone()[0], 1)

    def test_csrf_origin_cookie_body_and_unsigned_guest_negatives(self):
        browser = self.browser(); body = {'username': 'first_user', 'password': self.password}
        for origin in (None, 'null', 'https://other.example'):
            self.assertEqual(browser.request('/api/account/register', body, origin=origin)[0], 403)
        self.assertEqual(browser.request('/api/account/register', body, csrf=False)[0], 403)
        self.assertEqual(browser.request('/api/account/register', {**body, 'guest_id': 'a' * 64})[0], 400)
        self.assertEqual(browser.request('/api/account/register', raw=b'[]')[0], 400)
        self.assertEqual(browser.request('/api/account/register', raw=b'x' * 8193)[0], 400)
        self.assertEqual(browser.request('/api/account/register', body, extra={'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(browser.request('/api/account/session', extra={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        original = browser.cookies.pop('istok_beta')
        self.assertEqual(browser.request('/api/account/register', body)[0], 401)
        browser.cookies['istok_beta'] = original + 'forged'
        self.assertEqual(browser.request('/api/account/register', body)[0], 401)
        browser.cookies['istok_beta'] = original
        raw = '; '.join(f'{k}={v}' for k, v in browser.cookies.items()) + '; istok_device=duplicate'
        self.assertEqual(browser.request('/api/account/session', extra={'Cookie': raw})[0], 400)
        self.assertEqual(self.auth.public_bindings(), [])

    def test_password_limits_duplicate_registration_generic_wrong_login_and_rate_restart(self):
        browser = self.browser()
        for body in ({'username': 'bad@name', 'password': self.password}, {'username': 'valid', 'password': 'short'}, {'username': 'valid', 'password': '123456789012345'}):
            self.assertEqual(browser.request('/api/account/register', body)[0], 400)
        self.register(browser)
        second = self.browser()
        self.assertEqual(second.request('/api/account/register', {'username': 'FIRST_USER', 'password': self.password})[0], 409)
        for name in ('first_user', 'unknown_user'):
            status, payload, _ = second.request('/api/account/login', {'username': name, 'password': 'incorrect'})
            self.assertEqual(status, 401); self.assertEqual(payload['code'], 'user_login_failed')
        for _ in range(13):
            second.request('/api/account/login', {'username': 'first_user', 'password': 'incorrect'})
        self.stop(); self.start(); second.service = self.service
        status, payload, headers = second.request('/api/account/login', {'username': 'first_user', 'password': self.password})
        self.assertEqual(status, 429); self.assertGreater(int(headers['Retry-After']), 0)
        self.clock += 901
        self.assertEqual(second.request('/api/account/login', {'username': 'first_user', 'password': self.password})[0], 200)

    def test_bounded_hash_memory_and_both_transports_preserve_busy_retry(self):
        browser = self.browser(); self.register(browser); other = self.browser()
        self.assertTrue(password_auth.HASH_SLOTS.acquire(False)); self.assertTrue(password_auth.HASH_SLOTS.acquire(False))
        try:
            with patch.object(password_auth, 'scrypt') as hashing:
                status, payload, headers = other.request('/api/account/login', {'username': 'first_user', 'password': self.password})
                self.assertEqual(status, 429); self.assertEqual(payload['code'], 'auth_busy'); self.assertEqual(headers['Retry-After'], '5')
                status, payload, headers = other.request('/api/dashboard/login', {'username': self.admin_config.username, 'password': self.admin_password})
                self.assertEqual(status, 429); self.assertEqual(headers['Retry-After'], '5'); hashing.assert_not_called()
        finally:
            password_auth.HASH_SLOTS.release(); password_auth.HASH_SLOTS.release()

    def test_blocks_on_either_alias_deduplicate_dashboard_and_unblock_both(self):
        browser = self.browser(); guest = self.guest_id(browser); self.seed_report(guest, characters=123)
        user = self.register(browser); admin, csrf = self.admin_browser()
        with self.admin.connect() as db:
            db.execute('INSERT INTO account_blocks VALUES (?,?,?,?)', (user['id'], 1, 'fixture logical block', self.clock))
        self.assertEqual(browser.request('/api/session')[0], 403)
        self.assertTrue(browser.bootstrap()[1]['blocked'])
        key = self.service.beta.api_keys.create(guest, 'existing-key-fixture', ['quota:read'], 1)['token']
        api = Browser(self.service)
        self.assertEqual(api.request('/api/v1/quota', origin=None, extra={'Authorization': 'Bearer ' + key})[0], 403)
        status, result, _ = admin.request('/api/dashboard/accounts')
        self.assertEqual(status, 200); self.assertEqual(result['total'], 1)
        row = result['accounts'][0]; self.assertEqual(row['id'], guest); self.assertEqual(row['registered_account_id'], user['id'])
        self.assertEqual(row['kind'], 'registered'); self.assertEqual(row['metrics']['characters_total'], 123)
        self.assertTrue(row['blocked']); self.assertTrue(result['registration']['beta_connected'])
        status, _, _ = admin.request(f'/api/dashboard/accounts/{user["id"]}/unblock', {'reason': 'fixture restored'}, extra={'X-CSRF-Token': csrf})
        self.assertEqual(status, 200); self.assertEqual(browser.request('/api/session')[0], 200)
        with self.admin.connect(readonly=True) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT blocked FROM account_blocks')], [0, 0])
        self.assertEqual(browser.request('/api/account/logout', {})[0], 200)

    def test_device_get_is_persisted_valid_device_reads_do_not_write_and_creation_is_limited(self):
        callback = Mock(); self.auth.access._persist = callback
        browser = self.browser(); self.assertGreaterEqual(callback.call_count, 1); callback.reset_mock()
        self.assertEqual(browser.bootstrap()[0], 200); callback.assert_not_called()
        for _ in range(120): self.auth.access.ledger.hit_rate('local-bootstrap-ip', '127.0.0.1', 120, 3600)
        fresh = Browser(self.service); self.assertEqual(fresh.bootstrap()[0], 429)

    def test_missing_database_key_schema_and_live_incomplete_triples_fail_closed(self):
        browser = self.browser(); self.register(browser)
        path = self.auth.access.path; saved = path.with_suffix('.saved'); path.rename(saved)
        try:
            self.assertEqual(browser.request('/api/session')[0], 503); self.assertFalse(path.exists())
            with self.assertRaises(AccessError): user_auth.UserAuthRuntime(self.service.beta)
            self.assertFalse(path.exists())
        finally: saved.rename(path)
        key = self.auth.access.key_path; backup = key.with_suffix('.saved'); key.rename(backup)
        try:
            self.assertEqual(browser.request('/api/session')[0], 503)
            with self.assertRaises(AccessError): user_auth.UserAuthRuntime(self.service.beta)
            self.assertFalse(key.exists())
        finally: backup.rename(key)
        with self.auth.access.ledger.connect() as db: db.execute('DELETE FROM account_guest_bindings')
        self.assertEqual(browser.request('/api/session')[0], 503)
        with self.assertRaises(AccessError): user_auth.UserAuthRuntime(self.service.beta)

    def test_existing_bad_schema_is_never_repaired_on_restart(self):
        path = self.auth.access.path
        with self.auth.access.ledger.connect() as db: db.execute('ALTER TABLE account_guest_bindings RENAME TO bad_bindings')
        with self.assertRaises(AccessError): user_auth.UserAuthRuntime(self.service.beta)
        with closing(sqlite3.connect(path)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='account_guest_bindings'").fetchone())

    def test_complete_registry_row_loss_cannot_reopen_old_guest_capability(self):
        browser = self.browser(); old = dict(browser.cookies); self.register(browser)
        with self.auth.access.ledger.connect() as db:
            db.execute('DELETE FROM sessions'); db.execute('DELETE FROM account_guest_bindings')
            db.execute('DELETE FROM local_credentials'); db.execute('DELETE FROM accounts')
        replay = Browser(self.service); replay.cookies = old
        self.assertEqual(replay.request('/api/session')[0], 503)
        with self.assertRaises(AccessError): user_auth.UserAuthRuntime(self.service.beta)

    def test_failed_claim_marker_publish_blocks_login_until_serialized_recovery(self):
        browser = self.browser(); original_replace = user_access.os.replace
        def unavailable_marker(source, target):
            if Path(target) == self.auth.access.marker: raise OSError('fixture storage failure')
            return original_replace(source, target)
        with patch.object(user_access.os, 'replace', side_effect=unavailable_marker):
            status, _, _ = browser.request('/api/account/register', {'username': 'first_user', 'password': self.password})
            self.assertEqual(status, 503)
        self.assertEqual(browser.request('/api/account/login', {'username': 'first_user', 'password': self.password})[0], 503)
        self.assertEqual(browser.request('/api/session')[0], 503)
        # Restart can safely add the missing protection, under the same durable lock.
        self.stop(); self.start(); browser.service = self.service
        self.assertEqual(browser.bootstrap()[0], 200)
        self.assertEqual(browser.request('/api/account/login', {'username': 'first_user', 'password': self.password})[0], 200)

    def test_initializer_cannot_lower_count_after_concurrent_successful_claim(self):
        first = self.browser(); self.register(first)
        second = self.browser(); guest = self.guest_id(second); device = second.cookies['istok_device']
        paused, release, hashed = Event(), Event(), Event(); outcomes = []
        original_replace, original_hash = user_access.os.replace, user_access.password_digest
        def replace(source, target):
            if Path(target) == self.auth.access.marker and current_thread().name == 'qa-initializer':
                paused.set()
                if not release.wait(5): raise RuntimeError('fixture timeout')
            return original_replace(source, target)
        def hashing(*args):
            result = original_hash(*args)
            if current_thread().name == 'qa-signup': hashed.set()
            return result
        def initialize():
            try: user_auth.UserAuthRuntime(self.service.beta); outcomes.append('initialized')
            except Exception as error: outcomes.append(type(error).__name__)
        def signup():
            try: self.auth.access.register('second_user', self.password, guest, device, 'fixture-ip'); outcomes.append('registered')
            except Exception as error: outcomes.append(type(error).__name__)
        with patch.object(user_access.os, 'replace', side_effect=replace), patch.object(user_access, 'password_digest', side_effect=hashing):
            initializer = Thread(target=initialize, name='qa-initializer'); initializer.start(); self.assertTrue(paused.wait(5))
            registration = Thread(target=signup, name='qa-signup')
            try:
                with closing(sqlite3.connect(self.service.beta.database, timeout=0)) as probe:
                    with self.assertRaises(sqlite3.OperationalError) as blocked:
                        probe.execute('BEGIN IMMEDIATE')
                    self.assertIn('locked', str(blocked.exception).lower())
                registration.start(); self.assertTrue(hashed.wait(5))
                self.assertEqual(self.auth.access.marker.read_text(), 'user-access-v1:1\n')
            finally:
                release.set(); initializer.join(10)
                if registration.ident is not None: registration.join(10)
        self.assertFalse(initializer.is_alive()); self.assertFalse(registration.is_alive())
        self.assertEqual(set(outcomes), {'initialized', 'registered'})
        self.assertEqual(self.auth.access.marker.read_text(), 'user-access-v1:2\n')

    def test_missing_session_foreign_key_partial_unique_and_owner_view_fail_closed(self):
        with self.auth.access.ledger.connect() as db:
            db.execute('DROP TABLE sessions')
            db.execute('CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,account_id TEXT NOT NULL,expires_at REAL NOT NULL,revoked_at REAL)')
        with self.assertRaises(AccessError): user_auth.UserAuthRuntime(self.service.beta)
        # Independent fresh fixture for the partial unique constraint variant.
        with TemporaryDirectory() as directory:
            from contextlib import nullcontext
            access = UserAccess(Path(directory), guard=lambda _: None, claim=lambda _: nullcontext())
            with access.ledger.connect() as db:
                db.execute('DROP TABLE local_credentials')
                db.execute('CREATE TABLE local_credentials(account_id TEXT PRIMARY KEY REFERENCES accounts(id),login_key TEXT,salt TEXT,digest TEXT)')
                db.execute('CREATE UNIQUE INDEX fake_unique ON local_credentials(login_key) WHERE 0')
            with self.assertRaises(AccessError): UserAccess(Path(directory), guard=lambda _: None, claim=lambda _: nullcontext())
        with closing(self.service.beta.connect()) as db, db:
            db.execute("CREATE VIEW owner_account AS SELECT 1 AS id,'" + 'a' * 64 + "' AS guest")
        with self.assertRaises(AccessError):
            with self.auth.claim('a' * 64): pass

    def test_concurrent_same_guest_registration_has_one_complete_winner(self):
        browser = self.browser(); guest = self.guest_id(browser); device = browser.cookies['istok_device']; barrier = Barrier(2)
        def signup(index):
            barrier.wait()
            try: return self.auth.access.register('parallel_' + str(index), self.password, guest, device, 'fixture-ip')[1]
            except AccessError as error: return error.code
        with ThreadPoolExecutor(max_workers=2) as executor: results = list(executor.map(signup, range(2)))
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertIn('account_registration_conflict', results)
        self.assertEqual(len(self.auth.public_bindings()), 1)

    def owner_environment(self):
        salt = secrets.token_hex(32)
        return {'ISTOK_OWNER_USERNAME': 'owner_fixture', 'ISTOK_OWNER_SALT': salt,
                'ISTOK_OWNER_DIGEST': pbkdf2_hmac('sha256', self.password.encode(), bytes.fromhex(salt), 600000).hex()}

    def test_owner_mode_and_first_adoption_cannot_claim_registered_user_space(self):
        browser = self.browser(); guest = self.guest_id(browser); self.register(browser)
        with patch.dict(os.environ, self.owner_environment()):
            status, payload, _ = browser.request('/api/owner/login', {'username': 'owner_fixture', 'password': self.password})
            self.assertEqual(status, 403); self.assertEqual(payload['code'], 'account_space_protected')
            other = self.browser()
            status, _, _ = other.request('/api/owner/login', {'username': 'owner_fixture', 'password': self.password})
            self.assertEqual(status, 200)
            self.assertEqual(other.request('/api/account/register', {'username': 'owner_regular', 'password': self.password})[0], 403)
            self.assertEqual(other.request('/api/account/login', {'username': 'first_user', 'password': self.password})[0], 403)
        self.assertEqual(self.auth.public_bindings()[0]['guest_id'], guest)

    def test_registration_and_first_owner_adoption_use_same_durable_claim_lock(self):
        browser = self.browser(); guest = self.guest_id(browser); device = browser.cookies['istok_device']; barrier = Barrier(2)
        def register():
            barrier.wait()
            try: self.auth.access.register('race_user', self.password, guest, device, 'fixture-ip'); return 'registered'
            except AccessError as error: return error.code
        def owner():
            barrier.wait()
            try: self.service.beta.owner_login({'username': 'owner_fixture', 'password': self.password}, guest); return 'owner'
            except AccessError as error: return error.code
        with patch.dict(os.environ, self.owner_environment()), ThreadPoolExecutor(max_workers=2) as executor:
            first, second = executor.submit(register), executor.submit(owner); results = [first.result(), second.result()]
        self.assertEqual(sum(value in {'registered', 'owner'} for value in results), 1, results)
        self.assertEqual(len(self.auth.public_bindings()), int('registered' in results))


if __name__ == '__main__':
    unittest.main()
