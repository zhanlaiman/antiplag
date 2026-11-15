"""Real integrated BetaHandler with temporary state and no external I/O.

These tests never substitute the production handler or install account guards
in a fixture. The ordinary create_server integration is the system under test.
"""

from contextlib import closing
from hashlib import pbkdf2_hmac
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
import socket
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import Mock, patch

from cloud import dashboard
from cloud.web_beta import BetaHandler, create_server
from plagiarism.matching import analyze
from plagiarism.saas import report_identifier


ORIGIN = "http://beta.example"
TEXT = ("Синтетический редактор составил авторскую заметку о соснах и берёзах. "
        "Этот текст проверяет только локальную очередь и границы пользовательских пространств.")


class Browser:
    def __init__(self, case):
        self.case = case
        self.cookies = {}
        self.account_csrf = ""
        self.admin_csrf = ""

    def request(self, path, body=None, *, method=None, origin=ORIGIN, csrf=True, extra=None, raw=None):
        headers = {"Host": "beta.example"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        if origin is not None:
            headers["Origin"] = origin
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
            if csrf:
                header, token = (("X-CSRF-Token", self.admin_csrf) if path.startswith("/api/dashboard")
                                 else ("X-Istok-CSRF", self.account_csrf))
                if token:
                    headers[header] = token
        headers.update(extra or {})
        connection = HTTPConnection("127.0.0.1", self.case.service.server_port, timeout=5)
        try:
            connection.request(method or ("POST" if data is not None else "GET"), path, data, headers)
            response = connection.getresponse()
            content, pairs = response.read(), response.getheaders()
            for name, value in pairs:
                if name.lower() == "set-cookie":
                    parsed = SimpleCookie()
                    parsed.load(value)
                    for cookie_name, item in parsed.items():
                        if item["max-age"] == "0":
                            self.cookies.pop(cookie_name, None)
                        else:
                            self.cookies[cookie_name] = item.value
            result = json.loads(content) if response.getheader("Content-Type", "").startswith("application/json") else content
            if isinstance(result, dict) and "csrf_token" in result:
                if path.startswith("/api/dashboard"):
                    self.admin_csrf = result["csrf_token"]
                else:
                    self.account_csrf = result["csrf_token"]
            return response.status, result, dict(pairs)
        finally:
            connection.close()

    def clone(self):
        other = Browser(self.case)
        other.cookies = dict(self.cookies)
        other.account_csrf = self.account_csrf
        other.admin_csrf = self.admin_csrf
        return other


class IntegratedBetaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = secrets.token_urlsafe(28)
        cls.admin_password = secrets.token_urlsafe(28)
        salt = secrets.token_hex(32)
        cls.admin_config = dashboard.DashboardConfig(
            "integrated_qa_admin", salt, dashboard.password_digest(cls.admin_password, salt))
        cls.owner_password = secrets.token_urlsafe(28)
        cls.owner_salt = secrets.token_hex(32)
        cls.owner_digest = pbkdf2_hmac("sha256", cls.owner_password.encode(), bytes.fromhex(cls.owner_salt), 600000).hex()

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.calls = []
        self.outbound = []
        self.releases = []
        self.service = None
        environment = patch.dict(os.environ, {
            "ISTOK_RUNTIME_DIR": str(self.root / "initial-runtime"),
            "ISTOK_AUTOMATIC_SEARCH": "1", "ISTOK_WEB_WIKI_FOLLOWUP": "0",
            "ISTOK_OWNER_USERNAME": "integrated_qa_owner", "ISTOK_OWNER_SALT": self.owner_salt,
            "ISTOK_OWNER_DIGEST": self.owner_digest,
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_getaddrinfo = socket.getaddrinfo

        def validate(address):
            port = self.service.server_port if self.service is not None else None
            if (not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}
                    or address[1] != port):
                self.outbound.append(str(address))
                raise AssertionError("Only the owned local HTTP server is reachable")

        def connect(sock, address):
            validate(address)
            return original_connect(sock, address)

        def connect_ex(sock, address):
            validate(address)
            return original_connect_ex(sock, address)

        def getaddrinfo(host, *args, **kwargs):
            if host not in {"127.0.0.1", "::1", "localhost"}:
                self.outbound.append(str(host))
                raise AssertionError("Foreign DNS is forbidden")
            return original_getaddrinfo(host, *args, **kwargs)

        for target, value in (("socket.socket.connect", connect), ("socket.socket.connect_ex", connect_ex),
                              ("socket.getaddrinfo", getaddrinfo)):
            guard = patch(target, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.start()

    def start(self, *, registration_enabled=True, business_writes_enabled=True):
        def runner(text, identifier, progress, **kwargs):
            self.calls.append((identifier, kwargs))
            return {**analyze(text, []), "text": text, "sources": [], "scope": "web_search",
                    "web": {"provider": "synthetic", "queries_attempted": 0}}

        self.service = create_server(
            self.root, ORIGIN, host="127.0.0.1", port=0, allow_http=True, runner=runner,
            integrated=True, registration_enabled=registration_enabled,
            business_writes_enabled=business_writes_enabled, automatic_monthly_limit=180,
            pool_bootstrap=None, pool_keys=None, pool_grants=None, dashboard_config=self.admin_config)
        self.assertIs(self.service.RequestHandlerClass, BetaHandler)
        self.thread = Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        for release in self.releases:
            release.set()
        if self.service is not None:
            self.service.shutdown()
            self.service.server_close()
            self.service.beta.tenants.close()
            self.thread.join(timeout=5)
            self.assertFalse(self.thread.is_alive())
            self.service = None

    def tearDown(self):
        try:
            self.stop()
        finally:
            self.assertEqual(self.outbound, [], "Provider, model and foreign network calls are forbidden")

    def browser(self):
        browser = Browser(self)
        status, payload, _ = browser.request("/api/account/session")
        self.assertEqual(status, 200, payload)
        self.assertIn("istok_device", browser.cookies)
        self.assertIn("istok_beta", browser.cookies)
        return browser

    def guest(self, browser):
        status, payload, _ = browser.request("/api/session")
        self.assertEqual(status, 200, payload)
        return payload["beta"]["id"]

    def register(self, browser, username="integrated_user"):
        status, payload, _ = browser.request("/api/account/register", {"username": username, "password": self.password})
        self.assertEqual(status, 200, payload)
        return payload["user"]

    def admin(self):
        browser = Browser(self)
        status, payload, _ = browser.request("/api/dashboard/login", {
            "username": self.admin_config.username, "password": self.admin_password})
        self.assertEqual(status, 200, payload)
        self.assertTrue(browser.admin_csrf)
        return browser

    def block(self, admin, identifier, blocked=True):
        status, payload, _ = admin.request(
            f'/api/dashboard/accounts/{identifier}/{"block" if blocked else "unblock"}',
            {"reason": "Synthetic integration policy"})
        self.assertEqual(status, 200, payload)
        return payload

    def report(self, browser):
        status, source, _ = browser.request("/api/sources", {
            "title": "Synthetic integration source", "text": TEXT, "url": ""})
        self.assertEqual(status, 200, source)
        status, payload, _ = browser.request("/api/check", {
            "text": TEXT, "source_ids": [source["source"]["id"]]})
        self.assertEqual(status, 200, payload)
        return payload["report"]

    def wait_job(self, browser, identifier):
        for _ in range(100):
            status, payload, _ = browser.request("/api/jobs/" + identifier)
            self.assertEqual(status, 200, payload)
            if payload["job"]["status"] in {"complete", "failed", "cancelled"}:
                return payload["job"]
            time.sleep(.01)
        self.fail("The local job did not finish")

    def test_early_control_plane_dispatch_never_allocates_guest_or_reads_unauthorized_vault(self):
        self.assertIs(self.service.accounts, self.service.beta.accounts)
        self.assertIs(self.service.dashboard, self.service.beta.dashboard)
        self.assertIs(self.service.pool_admin, self.service.beta.pool_admin)
        anonymous = Browser(self)
        with patch.object(self.service.pool_admin.vault, "read", side_effect=AssertionError("Unauthorized vault read")) as vault:
            for path in ("/dashboard", "/account"):
                self.assertEqual(anonymous.request(path)[0], 200, path)
            self.assertFalse(anonymous.request("/api/dashboard/session")[1]["authenticated"])
            for path in ("/api/dashboard/accounts", "/api/dashboard/audit", "/api/dashboard/pool", "/pool.js", "/pool.css"):
                self.assertEqual(anonymous.request(path)[0], 401, path)
            status, _, headers = anonymous.request("/dashboard/pool")
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/dashboard?next=pool")
            vault.assert_not_called()
        self.assertEqual(anonymous.cookies, {})
        with closing(self.service.beta.connect()) as db:
            for table in ("guests", "active_guests", "checks"):
                self.assertEqual(db.execute("SELECT count(*) FROM " + table).fetchone()[0], 0, table)
        self.assertFalse(any((self.root / "tenants").glob("*/istok.sqlite3")))

    def test_admin_authentication_csrf_and_origin_precede_pool_body_and_vault_access(self):
        browser = self.browser()
        self.report(browser)
        identifier = self.guest(browser)
        admin = self.admin()
        path = f"/api/dashboard/accounts/{identifier}/block"
        status, payload, _ = admin.request(path, {"reason": "Synthetic policy"}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "dashboard_csrf_failed")
        self.assertEqual(admin.request(path, {"reason": "Synthetic policy"}, origin="https://foreign.example")[0], 403)
        with patch.object(self.service.pool_admin.vault, "read", side_effect=AssertionError("Unauthorized vault read")) as vault:
            self.assertEqual(browser.request("/api/dashboard/pool/stage", raw=b"{")[0], 401)
            self.assertEqual(admin.request("/api/dashboard/pool/stage", raw=b"{", csrf=False)[0], 403)
            vault.assert_not_called()
        self.assertEqual(browser.request("/api/session")[0], 200)
        self.block(admin, identifier)
        self.assertEqual(browser.request("/api/session")[0], 403)

    def test_registration_keeps_existing_tenant_reports_and_never_grants_owner_or_admin(self):
        browser = self.browser()
        guest = self.guest(browser)
        report = self.report(browser)
        stale = browser.clone()
        user = self.register(browser)
        self.assertEqual(user["guest_id"], guest)
        self.assertNotEqual(user["id"], guest)
        self.assertFalse(user["email_verified"])
        self.assertEqual((user["auth_method"], user["plan"]), ("password", "free"))
        session = browser.request("/api/session")[1]
        self.assertEqual(session["beta"]["id"], guest)
        self.assertFalse(session["beta"]["owner"])
        self.assertEqual(session["beta"]["max_characters"], 10000)
        self.assertEqual(browser.request("/api/reports/" + report["id"])[1]["report"]["id"], report["id"])
        self.assertEqual(browser.request("/api/owner/statistics")[0], 403)
        self.assertEqual(browser.request("/api/api-keys")[0], 403)
        self.assertEqual(browser.request("/api/dashboard/accounts")[0], 401)
        for path in ("/api/session", "/api/status", "/api/reports", "/api/reports/" + report["id"]):
            status, payload, _ = stale.request(path)
            self.assertEqual(status, 401, path)
            self.assertEqual(payload["code"], "account_login_required")
        self.assertEqual(self.calls, [])

    def test_logout_revokes_only_current_session_and_restores_second_browsers_own_guest(self):
        first = self.browser()
        original = self.guest(first)
        first_report = self.report(first)
        old_guest = first.clone()
        self.register(first)
        stolen_session = first.clone()
        second = self.browser()
        second_guest = self.guest(second)
        second_report = self.report(second)
        status, payload, _ = second.request("/api/account/login", {"username": "INTEGRATED_USER", "password": self.password})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.guest(second), original)
        self.assertEqual(first.request("/api/account/logout", {})[0], 200)
        self.assertNotEqual(self.guest(first), original)
        self.assertEqual(stolen_session.request("/api/reports")[0], 401)
        self.assertEqual(old_guest.request("/api/reports")[0], 401)
        self.assertEqual(second.request("/api/reports/" + first_report["id"])[0], 200)
        self.assertEqual(second.request("/api/account/logout", {})[0], 200)
        self.assertEqual(self.guest(second), second_guest)
        self.assertEqual(second.request("/api/reports/" + second_report["id"])[0], 200)
        self.assertEqual(second.request("/api/reports/" + first_report["id"])[0], 404)

    def test_valid_owner_and_scoped_api_credentials_never_authorize_separate_admin(self):
        owner = self.browser()
        status, payload, _ = owner.request("/api/owner/login", {
            "username": "integrated_qa_owner", "password": self.owner_password})
        self.assertEqual(status, 200, payload)
        self.assertTrue(owner.request("/api/session")[1]["beta"]["owner"])
        guest = self.guest(owner)
        token = self.service.beta.api_keys.create(guest, "Synthetic integration key", ["reports:read"], 1)["token"]
        api = Browser(self)
        self.assertEqual(api.request("/api/v1/reports", extra={"Authorization": "Bearer " + token}, origin=None)[0], 200)
        with patch.object(self.service.pool_admin.vault, "read", side_effect=AssertionError("Unauthorized vault read")) as vault:
            for path in ("/api/dashboard/accounts", "/api/dashboard/pool"):
                self.assertEqual(owner.request(path)[0], 401, path)
                self.assertEqual(api.request(path, extra={"Authorization": "Bearer " + token})[0], 401, path)
            vault.assert_not_called()

    def test_claimed_account_space_cannot_be_adopted_by_owner_login(self):
        browser = self.browser()
        user = self.register(browser)
        status, payload, _ = browser.request("/api/owner/login", {
            "username": "integrated_qa_owner", "password": self.owner_password})
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["code"], "account_space_protected")
        self.assertEqual(self.guest(browser), user["guest_id"])
        with closing(self.service.beta.connect()) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='owner_account'").fetchone()
            self.assertFalse(exists and db.execute("SELECT 1 FROM owner_account").fetchone())

    def test_owner_relogin_guards_stable_adopted_space_and_never_blocks_logout(self):
        owner = self.browser()
        login = {"username": "integrated_qa_owner", "password": self.owner_password}
        self.assertEqual(owner.request("/api/owner/login", login)[0], 200)
        stable = self.guest(owner)
        self.block(self.admin(), stable)
        self.assertEqual(owner.request("/api/session")[0], 403)
        fresh = self.browser()
        fresh_guest = self.guest(fresh)
        self.assertNotEqual(fresh_guest, stable)
        status, payload, _ = fresh.request("/api/owner/login", login)
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["code"], "account_blocked")
        self.assertNotIn("istok_owner", fresh.cookies)
        self.assertEqual(owner.request("/api/owner/logout", {})[0], 200)
        with closing(self.service.beta.connect()) as db:
            self.assertEqual(db.execute("SELECT guest FROM owner_account WHERE id=1").fetchone()[0], stable)

    def test_registered_account_alias_block_guards_cookie_api_reads_and_allows_logout(self):
        browser = self.browser()
        report = self.report(browser)
        user = self.register(browser)
        token = self.service.beta.api_keys.create(user["guest_id"], "Synthetic read key", ["reports:read"], 1)["token"]
        admin = self.admin()
        self.block(admin, user["id"])
        for path in ("/api/session", "/api/status", "/api/reports", "/api/reports/" + report["id"]):
            status, payload, _ = browser.request(path)
            self.assertEqual(status, 403, path)
            self.assertEqual(payload["code"], "account_blocked")
        api = Browser(self)
        status, payload, _ = api.request("/api/v1/reports", extra={"Authorization": "Bearer " + token}, origin=None)
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "account_blocked")
        self.assertTrue(browser.request("/api/account/session")[1]["blocked"])
        self.block(admin, user["guest_id"], blocked=False)
        self.assertEqual(browser.request("/api/reports/" + report["id"])[0], 200)
        self.block(admin, user["guest_id"])
        self.assertEqual(browser.request("/api/account/logout", {})[0], 200)

    def test_block_after_real_library_enqueue_prevents_native_work_before_execution(self):
        browser = self.browser()
        self.report(browser)
        guest = self.guest(browser)
        admin = self.admin()
        entered, release = Event(), Event()
        self.releases.append(release)

        def hold_worker():
            entered.set()
            return release.wait(5)

        blocker = self.service.beta.tenants.coordinator.executor.submit(hold_worker)
        self.assertTrue(entered.wait(2))
        identifier = "b" * 32
        with patch("server.analyze", side_effect=AssertionError("Blocked queued work executed")) as native_work:
            status, payload, _ = browser.request("/api/library-check-jobs", {"text": TEXT, "request_id": identifier})
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["job"]["status"], "queued")
            self.block(admin, guest)
            release.set()
            blocker.result(timeout=2)
            with self.service.beta.tenants.context(guest) as tenant:
                for _ in range(100):
                    job = tenant.jobs.get(identifier, tenant.store)
                    if job["status"] == "failed":
                        break
                    time.sleep(.01)
                self.assertEqual(job["status"], "failed", job)
                self.assertEqual(job["code"], "account_blocked")
                saved = tenant.store.get("model-job", identifier)
                self.assertEqual(saved["code"], "account_blocked")
                self.assertIsNone(tenant.store.get("report", report_identifier(identifier)))
            native_work.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "search-budget.sqlite3").exists())

    def test_unblocked_real_library_job_completes_and_replays_without_new_work(self):
        browser = self.browser()
        identifier = "c" * 32
        body = {"text": TEXT, "request_id": identifier}
        status, payload, _ = browser.request("/api/library-check-jobs", body)
        self.assertEqual(status, 200, payload)
        job = self.wait_job(browser, identifier)
        self.assertEqual(job["status"], "complete", job)
        self.assertEqual(job["result"]["report"]["scope"], "local_library")
        with patch("server.analyze", side_effect=AssertionError("Replay executed work")) as native_work:
            status, replay, _ = browser.request("/api/library-check-jobs", body)
            self.assertEqual(status, 200, replay)
            self.assertEqual(replay["job"]["result"], job["result"])
            native_work.assert_not_called()

    def test_reader_denies_business_writes_but_keeps_existing_login_logout_and_block_guards(self):
        registered = self.browser()
        user = self.register(registered)
        blocked = self.browser()
        self.report(blocked)
        blocked_guest = self.guest(blocked)
        self.block(self.admin(), blocked_guest)
        self.stop()
        self.start(registration_enabled=False, business_writes_enabled=False)
        newcomer = self.browser()
        status, payload, _ = newcomer.request("/api/account/register", {"username": "reader_new_user", "password": self.password})
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "user_registration_disabled")
        status, payload, _ = newcomer.request("/api/account/login", {"username": "integrated_user", "password": self.password})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.guest(newcomer), user["guest_id"])
        self.assertEqual(newcomer.request("/api/account/logout", {})[0], 200)
        self.assertEqual(blocked.request("/api/session")[0], 403)
        admin = self.admin()
        self.assertFalse(admin.request("/api/dashboard/session")[1]["business_writes_enabled"])
        self.assertEqual(admin.request("/api/dashboard/accounts")[0], 200)
        status, payload, _ = admin.request(f"/api/dashboard/accounts/{blocked_guest}/unblock", {"reason": "Reader cannot change controls"})
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "dashboard_readonly")
        with patch.object(self.service.pool_admin, "usage_probe", side_effect=AssertionError("Reader contacted provider")) as probe:
            status, payload, _ = admin.request("/api/dashboard/pool/validate", raw=b"{")
            self.assertEqual(status, 503)
            self.assertEqual(payload["code"], "dashboard_readonly")
            probe.assert_not_called()
        self.assertEqual(admin.request("/api/dashboard/logout", {})[0], 200)
        with self.service.accounts.access.ledger.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM accounts").fetchone()[0], 1)

    def test_missing_initialized_security_databases_fail_closed_without_recreation(self):
        browser = self.browser()
        self.register(browser)
        for database in (self.service.accounts.access.path, self.service.dashboard.database):
            with self.subTest(database=database.name):
                saved = database.with_name(database.name + ".test-held")
                database.rename(saved)
                try:
                    status, payload, _ = browser.request("/api/session")
                    self.assertEqual(status, 503, payload)
                    self.assertFalse(database.exists())
                finally:
                    saved.rename(database)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
