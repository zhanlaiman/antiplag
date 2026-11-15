"""Global ID/admission counters over real SQLite; native admin HTTP stays private."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cloud.dashboard import DashboardConfig, password_digest
from cloud.web_beta import BetaHandler, create_server
from plagiarism.access import AccessError
from plagiarism.user_accounts import AccountInventory


GUEST, LATE, OWNER, ALIAS = f'{1:064x}', f'{60:064x}', 'f' * 64, 'e' * 64
EXPECTED = {'registered_total': 1, 'guest_total': 59, 'owner_total': 1,
            'checks_today': 4, 'checking_accounts_today': 3}


def seed(path, alias=ALIAS):
    today = datetime.now(timezone.utc).date()
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript('CREATE TABLE IF NOT EXISTS guests(id TEXT PRIMARY KEY,month TEXT);'
                         'CREATE TABLE IF NOT EXISTS active_guests(id TEXT,month TEXT);'
                         'CREATE TABLE IF NOT EXISTS checks(guest TEXT,id TEXT,fingerprint TEXT,day TEXT);'
                         'CREATE TABLE IF NOT EXISTS unstarted_checks(guest TEXT,id TEXT);'
                         'CREATE TABLE IF NOT EXISTS rates(guest TEXT,day TEXT,count INTEGER);'
                         'CREATE TABLE IF NOT EXISTS owner_account(id INTEGER PRIMARY KEY,guest TEXT);')
        db.executemany('INSERT INTO guests VALUES (?,?)',
                       [(f'{i:064x}', '2026-11') for i in range(1, 61)] + [(alias, '2026-11')])
        db.execute('INSERT INTO owner_account VALUES (1,?)', (OWNER,))
        db.executemany('INSERT INTO checks VALUES (?,?,?,?)',
                       [(GUEST, 'shared', 'fixture', str(today)), (alias, 'alias', 'fixture', str(today)),
                        (LATE, 'later-page', 'fixture', str(today)), (OWNER, 'owner', 'fixture', str(today)),
                        (f'{2:064x}', 'shared', 'fixture', str(today)),
                        (GUEST, 'yesterday', 'fixture', str(today - timedelta(days=1)))])
        db.execute('INSERT INTO unstarted_checks VALUES (?,?)', (f'{2:064x}', 'shared'))


class DashboardUsageSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.database = self.root / 'beta.sqlite3'
        seed(self.database)
        bindings = [{'guest_id': GUEST, 'account_id': ALIAS, 'created_at': 1}]
        beta = SimpleNamespace(root=self.root, database=self.database,
                               accounts=SimpleNamespace(public_bindings=lambda: bindings),
                               tenants=SimpleNamespace(coordinator=SimpleNamespace(lock=Lock(), active=0, max_active=2)))
        self.inventory = AccountInventory(beta)

    def test_global_summary_is_identical_across_fifty_row_pages(self):
        first, second = self.inventory.page({}), self.inventory.page({}, 50)
        self.assertEqual((first['total'], len(first['accounts']), len(second['accounts'])), (61, 50, 11))
        self.assertEqual(first['summary'], EXPECTED)
        self.assertEqual(second['summary'], EXPECTED)
        self.assertNotIn(LATE, {row['id'] for row in first['accounts']})

    def test_registered_alias_counts_as_one_canonical_account_without_changing_rows(self):
        page = self.inventory.page({})
        self.assertEqual(page['summary']['registered_total'], 1)
        self.assertEqual(page['summary']['checking_accounts_today'], 3)
        self.assertNotIn(ALIAS, {row['id'] for row in page['accounts']})
        row = next(row for row in page['accounts'] if row['id'] == GUEST)
        self.assertEqual(row['web_checks_admitted'], {'total': 2, 'today': 1})
        self.assertEqual(row['kind'], 'registered')

    def test_today_counts_exclude_unstarted_and_other_days_with_guest_scoped_ids(self):
        self.assertEqual(self.inventory.page({})['summary']['checks_today'], 4)
        # A released admission for another guest has the same request ID as an accepted one.
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM checks').fetchone()[0], 6)

    def test_inventory_uses_only_selects_and_does_not_rewrite_database(self):
        original = sqlite3.connect; statements, denied, reads = [], [], []
        before = sha256(self.database.read_bytes()).digest()
        def authorize(action, table, column, *unused):
            if action == sqlite3.SQLITE_READ: reads.append((table, column))
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
                return sqlite3.SQLITE_OK
            denied.append(action); return sqlite3.SQLITE_DENY
        def readonly(path, **options):
            self.assertTrue(path.endswith('?mode=ro')); self.assertTrue(options['uri'])
            db = original(path, **options); db.set_authorizer(authorize); db.set_trace_callback(statements.append)
            return db
        with patch('plagiarism.user_accounts.sqlite3.connect', side_effect=readonly):
            self.assertEqual(self.inventory.page({})['summary'], EXPECTED)
        self.assertFalse(denied); self.assertTrue(statements)
        self.assertTrue(all(sql.lstrip().upper().startswith('SELECT ') for sql in statements))
        self.assertTrue(all(column not in {'data', 'login_key', 'salt', 'digest', 'ip', 'email_key'} for _, column in reads))
        self.assertEqual(sha256(self.database.read_bytes()).digest(), before)

    def test_unavailable_identity_sources_return_503_instead_of_zero(self):
        self.inventory.account_ledger = SimpleNamespace(path=self.root / 'missing-ledger.sqlite3')
        with self.assertRaises(AccessError) as error:
            self.inventory.page({})
        self.assertEqual(error.exception.status, 503)
        self.inventory.account_ledger = None
        self.database.rename(self.database.with_suffix('.saved'))
        with self.assertRaises(AccessError) as error:
            self.inventory.page({})
        self.assertEqual(error.exception.status, 503)

    def test_native_admin_http_exposes_global_summary_and_denies_anonymous(self):
        with patch.dict(os.environ, {}, clear=True):
            password, salt = secrets.token_urlsafe(24), secrets.token_hex(32)
            config = DashboardConfig('usage_summary_qa', salt, password_digest(password, salt))
            runner = Mock(side_effect=AssertionError('No model or search allowed'))
            service = create_server(self.root / 'http', 'http://beta.example', host='127.0.0.1', port=0,
                                    allow_http=True, integrated=True, registration_enabled=True,
                                    business_writes_enabled=False, dashboard_config=config, runner=runner)
            thread = Thread(target=service.serve_forever, daemon=True); thread.start()
            try:
                self.assertIs(service.RequestHandlerClass, BetaHandler)
                account_access = service.accounts.access
                device = account_access.ledger.create_device()
                _, account = account_access.register('usage_fixture', secrets.token_urlsafe(24), GUEST, device, '127.0.0.1')
                seed(service.beta.database, account['id'])
                cookie = ''
                def request(path, body=None):
                    conn = HTTPConnection('127.0.0.1', service.server_port, timeout=5)
                    headers = {'Host': 'beta.example', 'Origin': 'http://beta.example', 'Cookie': cookie,
                               'Content-Type': 'application/json'}
                    try:
                        conn.request('GET' if body is None else 'POST', path,
                                     None if body is None else json.dumps(body), headers)
                        response = conn.getresponse()
                        return response.status, json.loads(response.read()), response.getheader('Set-Cookie', '').split(';')[0]
                    finally: conn.close()
                original_connect = socket.socket.connect
                def local_only(sock, address):
                    self.assertEqual(address, ('127.0.0.1', service.server_port))
                    return original_connect(sock, address)
                with patch('socket.socket.connect', local_only):
                    self.assertEqual(request('/api/dashboard/accounts')[0], 401)
                    status, _, cookie = request('/api/dashboard/login', {'username': config.username, 'password': password})
                    self.assertEqual(status, 200)
                    for suffix in ('', '?offset=50'):
                        status, page, _ = request('/api/dashboard/accounts' + suffix)
                        self.assertEqual(status, 200); self.assertEqual(page['summary'], EXPECTED)
                        self.assertEqual(page['total'], 61)
                runner.assert_not_called()
            finally:
                service.shutdown(); service.server_close(); service.beta.tenants.close(); thread.join(5)
                self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
