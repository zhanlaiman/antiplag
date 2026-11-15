"""Real HTTP + SQLite adapter for the parent-owned beta dispatch integration."""

from contextlib import closing
from hashlib import pbkdf2_hmac, sha256
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import server
from cloud import dashboard
from cloud.web_beta import BetaHandler, create_server
from plagiarism import access
from plagiarism.access import AccessLedger
from plagiarism.matching import analyze


class IntegratedHandler(BetaHandler):
    """Same beta identity flow; parent inserts the named guards in its owned file."""
    def log_message(self, *args):
        pass

    def _dispatch(self, method, leases):
        if dashboard.dispatch(self, method):
            return
        path = urlsplit(self.path).path
        self.api_token = None
        self.logout_persist_handled = False
        logout = method == 'POST' and path == '/api/owner/logout'
        login = method == 'POST' and path == '/api/owner/login'
        if path.startswith('/api/') or path.startswith('/reports/'):
            try:
                if path.startswith('/api/v1/'):
                    if not self.server.beta.trusted_api(self.headers):
                        raise access.AccessError('Origin rejected', 'api_origin_forbidden', 403)
                    if len(self.headers.get_all('Authorization') or []) != 1:
                        raise access.AccessError('One token required', 'api_auth_required', 401)
                    self.api_token = self.server.beta.api_keys.authenticate(self.headers.get('Authorization'))
                    self.guest = self.api_token['guest']; self.owner = self.guest; cookie = None
                else:
                    if not self.trusted_request():
                        raise access.AccessError('Origin rejected', 'origin_forbidden', 403)
                    if logout:
                        self.owner = self.guest = None; cookie = None
                    else:
                        self.owner = self.server.beta.owner_identity(self.headers)
                        self.guest, cookie = (self.owner, None) if self.owner else self.server.beta.identity(self.headers)
                if not (login or logout):
                    dashboard.guard_account(self.server.beta, self.guest)
                if cookie:
                    self.response_headers.append(('Set-Cookie', cookie))
                self.tenant = None
                if not ((method == 'GET' and path in {'/api/session', '/api/status'}) or login or logout):
                    self.server.beta.admit_space(self.guest)
                    self.tenant = leases.enter_context(self.server.beta.tenants.context(self.guest))
                if method not in {'GET', 'HEAD'} and not (login or logout):
                    self.server.beta.mutation(self.guest)
            except access.AccessError as error:
                self.send_json({'error': str(error), 'code': error.code}, error.status)
                return
        return server.Handler._dispatch(self, method, leases)


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.login_name = 'testadmin_' + secrets.token_hex(6)
        cls.login_password = secrets.token_urlsafe(32)
        salt = secrets.token_hex(32)
        cls.config = dashboard.DashboardConfig(cls.login_name, salt, dashboard.password_digest(cls.login_password, salt))

    def setUp(self):
        self.temp = TemporaryDirectory(); self.root = Path(self.temp.name)
        self.clock = time.time(); self.calls = []
        self.environment = patch.dict(os.environ, {}); self.environment.start()
        self.start()

    def start(self, config=None):
        def runner(text, rid, progress):
            self.calls.append(rid)
            return analyze(text, [{'id': 'fixture', 'title': 'Fixture', 'text': text, 'url': ''}])
        self.service = create_server(self.root, 'http://beta.example', host='127.0.0.1', port=0,
                                     allow_http=True, runner=runner)
        self.service.RequestHandlerClass = IntegratedHandler
        self.admin = dashboard.DashboardRuntime(self.service.beta, config or self.config, now=lambda: self.clock)
        self.service.dashboard = self.admin
        original = self.service.beta._admit_space
        def guarded_admit(db, guest, month):
            # owner_login invokes this after resolving the stable owner target.
            dashboard.guard_account(self.service.beta, guest)
            return original(db, guest, month)
        self.service.beta._admit_space = guarded_admit
        self.thread = Thread(target=self.service.serve_forever, daemon=True); self.thread.start()

    def stop(self):
        self.service.shutdown(); self.service.server_close(); self.service.beta.tenants.close(); self.thread.join()

    def tearDown(self):
        self.stop(); self.environment.stop(); self.temp.cleanup()

    def http(self, path, body=None, cookie=None, *, csrf=None, origin='http://beta.example', method=None, authorization=None, extra=None, raw=None):
        headers = {'Host': 'beta.example'}
        if origin is not None: headers['Origin'] = origin
        if cookie: headers['Cookie'] = cookie
        if csrf: headers['X-CSRF-Token'] = csrf
        if authorization: headers['Authorization'] = authorization
        if body is not None or raw is not None: headers['Content-Type'] = 'application/json'
        if extra: headers.update(extra)
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        connection = HTTPConnection('127.0.0.1', self.service.server_port, timeout=10)
        try:
            connection.request(method or ('POST' if data is not None else 'GET'), path, data, headers)
            response = connection.getresponse(); content = response.read(); response_headers = dict(response.getheaders())
            result = json.loads(content) if response_headers.get('Content-Type', '').startswith('application/json') else content
            return response.status, result, response_headers
        finally:
            connection.close()

    def login(self, **kwargs):
        status, payload, headers = self.http('/api/dashboard/login', {'username': self.login_name, 'password': self.login_password}, **kwargs)
        self.assertEqual(status, 200, payload)
        return headers['Set-Cookie'].split(';')[0], payload['csrf_token']

    def guest(self):
        status, payload, headers = self.http('/api/session'); self.assertEqual(status, 200)
        cookie, identifier = headers['Set-Cookie'].split(';')[0], payload['beta']['id']
        self.assertEqual(self.http('/api/dictionary', {'word': 'тест', 'action': 'add'}, cookie)[0], 200)
        return cookie, identifier

    def block(self, identifier, blocked=True, reason='fixture policy'):
        cookie, csrf = self.login()
        return self.http(f'/api/dashboard/accounts/{identifier}/{"block" if blocked else "unblock"}', {'reason': reason}, cookie, csrf=csrf)

    def test_anonymous_guest_owner_and_bearer_do_not_authorize_admin_or_touch_pool(self):
        guest, _ = self.guest()
        pool = Mock(return_value={'never': 'unauthorized'})
        self.admin.pool_route = pool
        for cookie in (None, guest, 'istok_owner=' + secrets.token_urlsafe(32), dashboard.COOKIE + '=' + secrets.token_urlsafe(32)):
            for path in ('/api/dashboard/accounts', '/api/dashboard/audit', '/api/dashboard/missing', '/api/dashboard/pool'):
                with self.subTest(cookie_kind=bool(cookie), path=path):
                    status, result, headers = self.http(path, cookie=cookie, authorization='Bearer fake')
                    self.assertEqual(status, 401); self.assertEqual(result['code'], 'dashboard_auth_required')
                    self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.http('/api/dashboard/session')[1], {'authenticated': False, 'configured': True})
        pool.assert_not_called()

    def test_login_cookie_hashed_storage_csrf_logout_replay_and_restart(self):
        cookie, csrf = self.login()
        token = cookie.split('=', 1)[1]
        self.assertEqual(self.http('/api/dashboard/session', cookie=cookie)[1]['csrf_token'], csrf)
        with self.admin.connect(readonly=True) as db:
            row = db.execute('SELECT token_hash FROM admin_sessions').fetchone()
            self.assertEqual(row[0], sha256(token.encode()).hexdigest()); self.assertNotEqual(row[0], token)
        self.stop(); self.start()
        self.assertTrue(self.http('/api/dashboard/session', cookie=cookie)[1]['authenticated'])
        for token_value in (None, '0' * 64):
            self.assertEqual(self.http('/api/dashboard/logout', {}, cookie, csrf=token_value)[0], 403)
        status, result, headers = self.http('/api/dashboard/logout', {}, cookie, csrf=csrf)
        self.assertEqual(status, 200); self.assertFalse(result['authenticated']); self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.http('/api/dashboard/accounts', cookie=cookie)[0], 401)
        self.stop(); self.start()
        self.assertEqual(self.http('/api/dashboard/accounts', cookie=cookie)[0], 401)

    def test_secure_flags_expiry_config_rotation_and_relogin_rotation(self):
        self.service.beta.secure = True
        status, _, headers = self.http('/api/dashboard/login', {'username': self.login_name, 'password': self.login_password})
        self.assertEqual(status, 200)
        for value in ('HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/', 'Max-Age=3600'):
            self.assertIn(value, headers['Set-Cookie'])
        cookie = headers['Set-Cookie'].split(';')[0]
        fresh, _ = self.login(cookie=cookie)
        self.assertEqual(self.http('/api/dashboard/accounts', cookie=cookie)[0], 401)
        self.clock += 3601
        self.assertEqual(self.http('/api/dashboard/accounts', cookie=fresh)[0], 401)
        self.clock -= 3601
        cookie, _ = self.login()
        self.admin.config = dashboard.DashboardConfig('different_admin', self.config.salt, self.config.digest)
        self.assertEqual(self.http('/api/dashboard/accounts', cookie=cookie)[0], 401)

    def test_wrong_password_and_persistent_ip_and_global_rate_limits(self):
        for i in range(10):
            status, payload, _ = self.http('/api/dashboard/login', {'username': self.login_name if i % 2 else 'unknown_name', 'password': 'wrong'})
            self.assertEqual(status, 401); self.assertEqual(payload['code'], 'dashboard_login_failed')
        self.stop(); self.start()
        status, result, headers = self.http('/api/dashboard/login', {'username': self.login_name, 'password': self.login_password})
        self.assertEqual(status, 429); self.assertEqual(headers['Retry-After'], '900')
        self.clock += 901
        self.login()
        with self.admin.connect() as db:
            db.execute('DELETE FROM admin_login_attempts')
            db.executemany('INSERT INTO admin_login_attempts VALUES (?,?)', [(str(i), self.clock) for i in range(40)])
        self.assertEqual(self.http('/api/dashboard/login', {'username': self.login_name, 'password': self.login_password})[0], 429)

    def test_config_rotation_then_rollback_never_resurrects_prior_cookie(self):
        cookie, _ = self.login()
        changed = dashboard.DashboardConfig('rotated_admin_fixture', self.config.salt, self.config.digest)
        self.stop(); self.start(config=changed)
        self.assertEqual(self.http('/api/dashboard/accounts', cookie=cookie)[0], 401)
        self.stop(); self.start(config=self.config)
        self.assertEqual(self.http('/api/dashboard/accounts', cookie=cookie)[0], 401)

    def test_origin_fetch_metadata_host_and_body_validation(self):
        body = {'username': self.login_name, 'password': self.login_password}
        for origin in (None, 'https://other.example', 'null'):
            self.assertEqual(self.http('/api/dashboard/login', body, origin=origin)[0], 403)
        self.assertEqual(self.http('/api/dashboard/login', body, extra={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.http('/api/dashboard/session', extra={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.http('/api/dashboard/login', {**body, 'role': 'admin'})[0], 400)
        for raw in (b'{', b'[]', b' ' * 8193):
            self.assertEqual(self.http('/api/dashboard/login', raw=raw)[0], 400)
        self.assertEqual(self.http('/api/dashboard/login', body, extra={'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.http('/api/dashboard/login', {'username': 5, 'password': []})[0], 400)
        cookie, csrf = self.login()
        self.assertEqual(self.http('/api/dashboard/logout', {}, cookie, csrf=csrf, origin='http://other.example')[0], 403)
        self.assertEqual(self.http('/api/dashboard/missing', cookie=cookie)[0], 404)
        self.assertEqual(self.http('/api/dashboard/accounts?offset=-1', cookie=cookie)[0], 400)
        self.assertEqual(self.http('/api/dashboard/accounts?offset=1&offset=2', cookie=cookie)[0], 400)
        duplicate = cookie + '; ' + cookie
        self.assertEqual(self.http('/api/dashboard/accounts', cookie=duplicate)[0], 401)

    def test_admin_routes_and_static_never_allocate_guests_or_consume_quota(self):
        for path in ('/dashboard', '/dashboard.html', '/dashboard.js', '/dashboard.css'):
            status, _, headers = self.http(path, origin=None)
            self.assertEqual(status, 200); self.assertEqual(headers['X-Robots-Tag'], 'noindex, nofollow')
        cookie, _ = self.login()
        self.http('/api/dashboard/accounts', cookie=cookie)
        with closing(self.service.beta.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM active_guests').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM rates').fetchone()[0], 0)
        self.assertEqual(list((self.root / 'tenants').glob('*')), [])

    def test_block_unblock_every_request_path_isolated_and_persists(self):
        alice, identifier = self.guest(); bob, other = self.guest()
        key = self.service.beta.api_keys.create(identifier, 'fixture', ['quota:read'], 1)['token']
        admin_cookie, _ = self.login()
        self.assertEqual(self.block(identifier)[0], 200)
        routes = [('/api/session', None), ('/api/status', None), ('/api/reports', None),
                  ('/api/jobs/' + 'a' * 32, None), ('/reports/' + 'a' * 32 + '.html', None),
                  ('/api/check', {'text': 'Достаточно длинный тестовый текст для проверки.'}),
                  ('/api/web-check-jobs', {'request_id': 'a' * 32, 'text': 'test', 'internet_consent': True}),
                  ('/api/library-check-jobs', {'request_id': 'b' * 32, 'text': 'test'})]
        for path, body in routes:
            status, payload, _ = self.http(path, body, alice)
            self.assertEqual(status, 403, path); self.assertEqual(payload['code'], 'account_blocked')
        self.assertEqual(self.http('/api/v1/quota', authorization='Bearer ' + key, origin=None)[0], 403)
        self.assertEqual(self.http('/api/session', cookie=bob)[0], 200)
        self.assertEqual(self.http('/api/dashboard/accounts', cookie=admin_cookie)[0], 200)
        self.assertEqual(self.calls, [])
        self.stop(); self.start()
        self.assertEqual(self.http('/api/session', cookie=alice)[0], 403)
        self.assertEqual(self.block(identifier, False, 'fixture restored')[0], 200)
        self.assertEqual(self.http('/api/session', cookie=alice)[0], 200)
        with self.admin.connect(readonly=True) as db:
            events = list(db.execute("SELECT action,account_id,reason FROM admin_audit WHERE action IN ('block','unblock') ORDER BY id"))
            self.assertEqual([tuple(row) for row in events], [('block', identifier, 'fixture policy'), ('unblock', identifier, 'fixture restored')])
        self.assertNotEqual(identifier, other)

    def test_stable_owner_relogin_is_blocked_and_logout_still_available(self):
        guest, identifier = self.guest(); salt = secrets.token_hex(32); password = secrets.token_urlsafe(24)
        env = {'ISTOK_OWNER_USERNAME': 'owner_fixture', 'ISTOK_OWNER_SALT': salt,
               'ISTOK_OWNER_DIGEST': pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()}
        with patch.dict(os.environ, env):
            status, _, headers = self.http('/api/owner/login', {'username': 'owner_fixture', 'password': password}, guest)
            self.assertEqual(status, 200); owner_cookie = headers['Set-Cookie'].split(';')[0]
            cookie, _ = self.login()
            self.assertEqual(self.http('/api/dashboard/accounts', cookie=owner_cookie)[0], 401)
            self.assertEqual(self.block(identifier)[0], 200)
            self.assertEqual(self.http('/api/session', cookie=owner_cookie)[0], 403)
            newcomer, _ = self.guest()
            status, payload, _ = self.http('/api/owner/login', {'username': 'owner_fixture', 'password': password}, newcomer)
            self.assertEqual(status, 403); self.assertEqual(payload['code'], 'account_blocked')
            self.assertEqual(self.http('/api/owner/logout', {}, owner_cookie)[0], 200)
            self.assertEqual(self.http('/api/dashboard/accounts', cookie=cookie)[0], 200)

    def test_block_reason_target_validation_csrf_and_pool_gate(self):
        _, identifier = self.guest(); cookie, csrf = self.login()
        path = f'/api/dashboard/accounts/{identifier}/block'
        self.assertEqual(self.http(path, {'reason': 'valid'}, cookie)[0], 403)
        for body in ({'reason': ''}, {'reason': 'a' * 241}, {'reason': 'a\nb'}, {'reason': 'valid', 'blocked': True}):
            self.assertEqual(self.http(path, body, cookie, csrf=csrf)[0], 400)
        self.assertEqual(self.http('/api/dashboard/accounts/' + 'f' * 64 + '/block', {'reason': 'valid'}, cookie, csrf=csrf)[0], 404)
        calls = []
        def pool(handler, method, route_path, body, *, authorize):
            authorize(handler, method); calls.append(route_path); return {'safe': True}
        self.admin.pool_route = pool
        self.assertEqual(self.http('/api/dashboard/pool/stage', {'provider': 'fixture'}, cookie)[0], 403)
        self.assertEqual(calls, [])
        self.assertEqual(self.http('/api/dashboard/pool/stage', {'provider': 'fixture'}, cookie, csrf=csrf)[1], {'safe': True})
        self.admin.pool_route = Mock(side_effect=access.AccessError('PRIVATE_POOL_KEY', 'PRIVATE_POOL_CODE', 400))
        status, result, _ = self.http('/api/dashboard/pool/stage', {}, cookie, csrf=csrf)
        self.assertEqual(status, 400); self.assertEqual(result['code'], 'dashboard_pool_request_failed')
        self.assertNotIn('PRIVATE_POOL', json.dumps(result))

    def test_missing_corrupt_control_database_fails_closed_without_recreation(self):
        cookie, identifier = self.guest(); admin_cookie, _ = self.login()
        database = self.admin.database; saved = database.with_suffix('.saved'); database.rename(saved)
        try:
            self.assertEqual(self.http('/api/session', cookie=cookie)[0], 503)
            self.assertEqual(self.http('/api/dashboard/accounts', cookie=admin_cookie)[0], 503)
            self.assertFalse(database.exists())
        finally: saved.rename(database)
        with self.admin.connect() as db:
            db.execute('ALTER TABLE account_blocks RENAME TO invalid_controls')
        self.assertEqual(self.http('/api/session', cookie=cookie)[0], 503)
        self.assertEqual(self.calls, [])

    def test_restart_after_control_database_loss_refuses_silent_bootstrap(self):
        _, identifier = self.guest(); self.block(identifier)
        database = self.admin.database; saved = database.with_suffix('.saved'); database.rename(saved)
        try:
            with self.assertRaises(access.AccessError) as caught:
                dashboard.DashboardRuntime(self.service.beta, self.config)
            self.assertEqual(caught.exception.code, 'account_control_unavailable')
            self.assertFalse(database.exists())
        finally:
            saved.rename(database)
        self.stop(); self.start()
        with self.assertRaises(access.AccessError) as caught:
            dashboard.guard_account(self.service.beta, identifier)
        self.assertEqual(caught.exception.code, 'account_blocked')

    def test_metadata_counts_actual_reports_admissions_characters_and_safe_codes(self):
        cookie, identifier = self.guest(); _, other = self.guest()
        now = time.strftime('%Y-%m-%dT12:00:00+00:00', time.gmtime())
        with self.service.beta.tenants.context(identifier) as tenant:
            tenant.store.save('report', {'id': 'r1', 'text': 'PRIVATE_TEXT_SENTINEL', 'stats': {'total_characters': 17}, 'created_at': now})
            tenant.store.save('report', {'id': 'r2', 'text': 'PRIVATE_TEXT_SENTINEL', 'stats': {'total_characters': 23}, 'created_at': now})
            tenant.store.save('report', {'id': 'old', 'text': 'PRIVATE_TEXT_SENTINEL', 'created_at': '2020-01-01T00:00:00+00:00'})
            tenant.store.trash_item('report', 'r2')
            tenant.store.save('model-job', {'id': 'job1', 'status': 'failed', 'code': 'job_interrupted', 'error': 'PRIVATE_ERROR_SENTINEL', 'details': {'secret': 'PRIVATE_KEY_SENTINEL'}, 'created_at': now})
            tenant.store.save('model-job', {'id': 'job2', 'status': 'failed', 'code': 'PRIVATE_CODE_SENTINEL', 'created_at': now})
        self.service.beta.admit_check(identifier, 'a' * 32, 'admitted')
        self.service.beta.admit_check(identifier, 'b' * 32, 'not started'); self.service.beta.release_unstarted_check(identifier, 'b' * 32)
        admin_cookie, _ = self.login(); status, result, _ = self.http('/api/dashboard/accounts', cookie=admin_cookie)
        self.assertEqual(status, 200)
        serialized = json.dumps(result)
        for secret in ('PRIVATE_TEXT_SENTINEL', 'PRIVATE_ERROR_SENTINEL', 'PRIVATE_KEY_SENTINEL', 'PRIVATE_CODE_SENTINEL'):
            self.assertNotIn(secret, serialized)
        account = next(a for a in result['accounts'] if a['id'] == identifier); metrics = account['metrics']
        self.assertEqual((metrics['reports_total'], metrics['reports_active']), (3, 2))
        self.assertEqual((metrics['characters_total'], metrics['characters_today'], metrics['unknown_character_reports']), (40, 40, 1))
        self.assertEqual(account['web_checks_admitted'], {'total': 1, 'today': 1})
        self.assertEqual((metrics['errors_today'], metrics['errors_total']), (2, 2))
        self.assertEqual({e['code'] for e in metrics['recent_errors']}, {'job_interrupted', 'job_failed'})
        self.assertEqual(result['payments'], {'configured': False, 'source': None, 'purchases': None})
        self.assertFalse(result['registration']['beta_connected'])
        other_metrics = next(a for a in result['accounts'] if a['id'] == other)['metrics']
        self.assertEqual(other_metrics['reports_total'], 0)

    def test_metadata_legacy_and_symlink_are_not_migrated_or_read(self):
        identifier = 'd' * 64; path = self.root / 'tenants' / identifier / 'istok.sqlite3'; path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('CREATE TABLE records(kind TEXT);')
        cookie, _ = self.login()
        result = self.http('/api/dashboard/accounts', cookie=cookie)[1]
        self.assertFalse(next(a for a in result['accounts'] if a['id'] == identifier)['metrics']['available'])
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sqlite_master WHERE type="table"').fetchone()[0], 1)
        linked = self.root / 'tenants' / ('e' * 64); linked.symlink_to(path.parent, target_is_directory=True)
        self.assertFalse(self.admin.inventory.metadata('e' * 64)['available'])

    def test_optional_existing_saas_ledger_is_metadata_only_and_paginated(self):
        ledger = AccessLedger(self.root / 'saas' / 'access.sqlite3')
        user = ledger.ensure_identity('https://fixture.example', 'subject', 'fixture@example.org', True)
        self.admin.inventory.account_ledger = ledger
        with closing(self.service.beta.connect()) as db, db:
            db.executemany('INSERT INTO guests VALUES (?,?)', [(f'{i:064x}', '2026-11') for i in range(55)])
        cookie, _ = self.login()
        first = self.http('/api/dashboard/accounts', cookie=cookie)[1]
        second = self.http('/api/dashboard/accounts?offset=50', cookie=cookie)[1]
        self.assertEqual(first['total'], 56); self.assertEqual(len(first['accounts']), 50); self.assertTrue(first['has_more'])
        self.assertEqual(len(second['accounts']), 6); self.assertFalse(second['has_more'])
        self.assertEqual(first['registration']['ledger'], 'connected')
        accounts = first['accounts'] + second['accounts']
        registered = next(a for a in accounts if a['id'] == user['id'])
        self.assertEqual(registered['kind'], 'registered'); self.assertNotIn('email_key', json.dumps(accounts))
        self.assertNotIn('fixture@example.org', json.dumps(accounts))

    def test_config_loader_hash_only_private_file_and_unconfigured_fail_closed(self):
        path = self.root / 'admin-config.json'
        path.write_text(json.dumps({'username': self.config.username, 'salt': self.config.salt, 'digest': self.config.digest})); path.chmod(0o600)
        with patch.dict(os.environ, {'ISTOK_DASHBOARD_CONFIG': str(path)}):
            self.assertEqual(dashboard.DashboardConfig.from_env(), self.config)
            path.chmod(0o644)
            with self.assertRaises(ValueError): dashboard.DashboardConfig.from_env()
            path.chmod(0o600); path.write_text(json.dumps({'username': self.login_name, 'password': self.login_password}))
            with self.assertRaises(ValueError): dashboard.DashboardConfig.from_env()
        self.admin.config = None
        self.assertEqual(self.http('/api/dashboard/login', {'username': self.login_name, 'password': self.login_password})[0], 503)
        self.assertFalse(self.http('/api/dashboard/session')[1]['configured'])

    def test_queued_operation_rechecks_block_before_work(self):
        _, identifier = self.guest(); self.block(identifier)
        work = Mock()
        def operation(progress):
            dashboard.guard_account(self.service.beta, identifier)
            return work()
        with self.service.beta.tenants.context(identifier) as tenant:
            job = tenant.jobs.submit_model('c' * 32, '/api/library-check', {'text': 'fixture'}, operation, tenant.store)
            for _ in range(100):
                job = tenant.jobs.get(job['id'], tenant.store)
                if job['status'] == 'failed': break
                time.sleep(.01)
            self.assertEqual(job['status'], 'failed'); self.assertEqual(job['code'], 'account_blocked')
        work.assert_not_called()


if __name__ == '__main__':
    unittest.main()
