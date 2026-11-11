from http.client import HTTPConnection
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import server
from plagiarism import access, auth_provider, learning, saas, tenants
from plagiarism.jobs import Jobs
from plagiarism.storage import Store


TEXT = "Редактор описал четыре зелёных листа и подробно сравнил форму каждой жилки."


class Browser:
    def __init__(self, httpd):
        self.httpd, self.cookies, self.csrf = httpd, {}, ""

    def request(self, method, path, body=None, headers=None):
        request_headers = {"Content-Type": "application/json", "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        if method != "GET":
            request_headers.update(Origin=f"http://127.0.0.1:{self.httpd.server_port}", **{"X-Istok-CSRF": self.csrf})
        request_headers.update(headers or {})
        connection = HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            connection.request(method, path, json.dumps(body).encode() if body is not None else None, request_headers)
            response = connection.getresponse()
            for key, value in response.getheaders():
                if key.lower() == "set-cookie":
                    cookie = SimpleCookie(value)
                    for name, item in cookie.items():
                        self.cookies[name] = item.value
            raw = response.read()
            payload = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw.decode()
            if isinstance(payload, dict) and payload.get("csrf_token"):
                self.csrf = payload["csrf_token"]
            return response.status, payload
        finally:
            connection.close()


class SaaSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        packs = patch.object(tenants.source_packs, "load_pack", side_effect=lambda name: {"sources": [
            {"id": "public-" + name, "title": "Public fixture", "text": TEXT, "url": "https://example.org/source"}]})
        packs.start()
        self.addCleanup(packs.stop)
        config = auth_provider.AuthConfig(f"http://127.0.0.1:{self.httpd.server_port}", "https://test-project.supabase.co",
                                          "server-only-key", "configured-site-key", "configured-secret-key")
        provider = Mock()

        def verified(email, code):
            if code != "123456":
                raise auth_provider.AccessError("Wrong code", "invalid_login", 401)
            return {"issuer": config.supabase_url + "/auth/v1", "subject": email,
                    "email": email, "email_verified": True}

        provider.verify_code.side_effect = verified
        self.runtime = saas.SaaSRuntime(config, access.AccessLedger(root / "access.sqlite3"),
                                        tenants.TenantRegistry(root / "tenants"), provider,
                                        learning_queue=learning.LearningQueue(root / "learning.sqlite3"))
        self.addCleanup(self.runtime.close)
        self.httpd.saas = self.runtime
        self.browser = Browser(self.httpd)

    def login(self, browser=None, email="editor@example.org"):
        browser = browser or self.browser
        status, payload = browser.request("GET", "/api/session")
        self.assertEqual(status, 200)
        status, challenge = browser.request("POST", "/api/auth/request", {"email": email, "captcha_token": "mock-captcha"})
        self.assertEqual(status, 200, challenge)
        status, result = browser.request("POST", "/api/auth/verify", {"email": email, "challenge_id": challenge["challenge_id"], "code": "123456"})
        self.assertEqual(status, 200, result)
        status, session = browser.request("GET", "/api/session")
        self.assertEqual(status, 200)
        return session

    def check(self, browser=None, text=TEXT, request_id=None):
        browser = browser or self.browser
        request_id = request_id or uuid4().hex
        status, result = browser.request("POST", "/api/check", {"text": text, "request_id": request_id})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["report"]["id"], saas.report_identifier(request_id))
        return result["report"]

    def wait_job(self, identifier):
        for _ in range(200):
            status, result = self.browser.request("GET", "/api/jobs/" + identifier)
            self.assertEqual(status, 200, result)
            if result["job"]["status"] in {"complete", "failed", "cancelled"}:
                return result["job"]
            time.sleep(0.005)
        self.fail("Job did not finish")

    def test_guest_session_is_lazy_and_private_routes_require_authentication(self):
        with patch.object(self.runtime.provider, "request_code") as provider:
            status, session = self.browser.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertIsNone(session["user"])
        self.assertEqual(session["auth"]["method"], "email_otp")
        self.assertEqual(len(session["csrf_token"]), 64)
        self.assertEqual(len(self.runtime.tenants.entries), 0)
        provider.assert_not_called()
        for path in ("/api/status", "/api/sources", "/api/reports", "/reports/" + "a" * 32 + ".html"):
            status, result = self.browser.request("GET", path)
            self.assertEqual(status, 401, result)

    def test_csrf_host_origin_and_real_peer_rate_inputs_are_enforced(self):
        self.browser.request("GET", "/api/session")
        for headers in ({"X-Istok-CSRF": ""}, {"Origin": "https://attacker.example"}, {"Host": "attacker.example"}):
            status, _ = self.browser.request("POST", "/api/auth/request", {"email": "a@example.org", "captcha_token": "x"}, headers)
            self.assertEqual(status, 403)
        self.runtime.provider.request_code.assert_not_called()
        with patch.object(self.runtime.ledger, "hit_rate", wraps=self.runtime.ledger.hit_rate) as rate:
            status, _ = self.browser.request("POST", "/api/auth/request", {"email": "a@example.org", "captcha_token": "x"}, {"X-Forwarded-For": "9.9.9.9"})
        self.assertEqual(status, 200)
        self.assertIn(unittest.mock.call("ip-request", "127.0.0.1", 30, 600), rate.call_args_list)

    def test_login_trusted_identity_logout_and_cookie_contract(self):
        session = self.login()
        self.assertEqual(session["mode"], "saas")
        self.assertEqual(session["quota"]["remaining"], 6000)
        self.assertEqual(session["user"]["plan"], "free")
        self.assertTrue(session["user"]["email_verified"])
        self.assertNotIn("email", session["user"])
        secure = saas.SaaSRuntime(auth_provider.AuthConfig("https://app.example.org", "https://test-project.supabase.co",
            "server-key", "site-key", "secret-key"), self.runtime.ledger, self.runtime.tenants, self.runtime.provider)
        cookie = secure.cookie(saas.SESSION_COOKIE, "opaque-token")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Secure", cookie)
        token = self.browser.cookies[saas.SESSION_COOKIE]
        status, _ = self.browser.request("POST", "/api/auth/logout", {})
        self.assertEqual(status, 200)
        self.assertIsNone(self.runtime.ledger.session(token))
        self.assertEqual(self.browser.request("GET", "/api/reports")[0], 401)

    def test_check_quota_precedes_analyze_and_repeated_request_reuses_owned_report(self):
        self.login()
        with patch.object(server, "analyze", wraps=server.analyze) as analyze:
            request_id = uuid4().hex
            report = self.check(request_id=request_id)
            self.check(request_id=request_id)
            self.assertEqual(analyze.call_count, 1)
            status, error = self.browser.request("POST", "/api/check", {"request_id": uuid4().hex, "text": "a" * 6001})
            self.assertEqual(status, 429, error)
            self.assertEqual(analyze.call_count, 1)
        status, changed = self.browser.request("POST", "/api/check", {"request_id": request_id, "text": TEXT + " changed"})
        self.assertEqual(status, 409, changed)
        self.assertEqual(self.browser.request("GET", "/api/session")[1]["usage"]["used"]["checks"], 1)

    def test_reports_documents_and_source_uploads_are_owner_scoped(self):
        first = self.login()
        report = self.check()
        other = Browser(self.httpd)
        self.login(other, "other@example.org")
        for path in ("/api/reports/" + report["id"], "/api/report-exports/" + report["id"], "/reports/" + report["id"] + ".html"):
            self.assertEqual(other.request("GET", path)[0], 404)
        for number in range(10):
            status, result = self.browser.request("POST", "/api/sources", {"title": str(number), "text": "Private text", "url": ""})
            self.assertEqual(status, 200, result)
        self.assertEqual(self.browser.request("POST", "/api/sources", {"title": "11", "text": "x", "url": ""})[0], 429)
        self.assertEqual(other.request("POST", "/api/sources", {"title": "too long", "text": "x" * 20001})[0], 400)
        self.assertEqual(len(other.request("GET", "/api/sources")[1]["sources"]), 2)
        self.assertNotIn(first["user"]["id"], json.dumps(other.request("GET", "/api/reports")[1]))

    def test_model_operations_require_saved_report_and_cannot_bypass_job_or_text_entitlement(self):
        self.login()
        report = self.check()
        source = self.browser.request("GET", "/api/sources")[1]["sources"][0]
        identifier = uuid4().hex
        operation = {"report_id": report["id"], "source_id": source["id"], "text": TEXT, "operation_id": identifier}
        with patch.object(server.llm, "judge", return_value={"relation": "exact"}) as judge:
            self.assertEqual(self.browser.request("POST", "/api/compare", operation)[0], 403)
            wrong = {**operation, "text": TEXT + " bypass"}
            self.assertEqual(self.browser.request("POST", "/api/model-jobs", {"request_id": identifier, "path": "/api/compare", "body": wrong})[0], 403)
            self.assertEqual(judge.call_count, 0)
            status, result = self.browser.request("POST", "/api/model-jobs", {"request_id": identifier, "path": "/api/compare", "body": operation})
            self.assertEqual(status, 200, result)
            self.assertEqual(self.wait_job(identifier)["status"], "complete")
            self.browser.request("POST", "/api/model-jobs", {"request_id": identifier, "path": "/api/compare", "body": operation})
            self.assertEqual(judge.call_count, 1)
        self.assertEqual(self.browser.request("GET", "/api/session")[1]["usage"]["used"]["model_ops"], 1)

    def test_diagnostic_routes_are_not_public_model_or_training_endpoints(self):
        self.login()
        for method, path in (("GET", "/api/examples"), ("GET", "/api/evaluations"), ("POST", "/api/export"),
                             ("POST", "/api/reviews"), ("POST", "/api/evaluate"), ("POST", "/api/demo")):
            status, _ = self.browser.request(method, path, {} if method == "POST" else None)
            self.assertEqual(status, 403)

    def test_web_check_needs_explicit_consent_and_configuration_before_admission(self):
        self.login()
        with patch.object(server.web_search, "status", return_value={"configured": False, "error": "Not configured"}), \
             patch.object(server.web_search, "run") as run:
            status, result = self.browser.request("POST", "/api/web-check-jobs", {"request_id": uuid4().hex, "text": TEXT})
            self.assertEqual(status, 400, result)
            status, result = self.browser.request("POST", "/api/web-check-jobs", {"request_id": uuid4().hex, "text": TEXT, "internet_consent": True})
            self.assertEqual(status, 503, result)
        run.assert_not_called()
        self.assertEqual(self.browser.request("GET", "/api/session")[1]["usage"]["used"]["checks"], 0)

    def test_web_check_is_durable_once_only_and_keeps_report_and_job_ids_distinct(self):
        session = self.login()
        identifier = uuid4().hex
        body = {"request_id": identifier, "text": TEXT, "internet_consent": True}
        payload = {"text": TEXT, "matches": [], "sources": [], "stats": {"similarity_percent": 0, "uniqueness_percent": 100},
                   "scope": "web_search", "web": {"status": "complete"}}
        with patch.object(server.web_search, "status", return_value={"configured": True}), \
             patch.object(server.web_search, "run", return_value=payload) as run:
            status, result = self.browser.request("POST", "/api/web-check-jobs", body)
            self.assertEqual(status, 200, result)
            job = self.wait_job(identifier)
            self.assertEqual(job["status"], "complete", job)
            report = job["result"]["report"]
            self.assertEqual(report["id"], saas.report_identifier(identifier))
            self.assertNotEqual(report["id"], job["id"])
            self.assertEqual(self.browser.request("POST", "/api/web-check-jobs", body)[0], 200)
            self.assertEqual(run.call_count, 1)
            self.assertNotEqual(run.call_args.args[1], identifier)
        self.assertEqual(self.runtime.ledger.usage(session["user"]["id"])["used"]["checks"], 1)
        with self.runtime.tenants.context(session["user"]["id"]) as tenant:
            self.assertEqual(tenant.store.get("model-job", identifier)["status"], "complete")
            self.assertIsNotNone(tenant.store.get("report", report["id"]))

    def test_web_check_rejects_existing_other_kind_identifier_before_compute(self):
        session = self.login()
        report = self.check()
        with patch.object(server.web_search, "status", return_value={"configured": True}), \
             patch.object(server.web_search, "run") as run:
            status, result = self.browser.request("POST", "/api/web-check-jobs", {
                "request_id": report["id"], "text": TEXT, "internet_consent": True})
            self.assertEqual(status, 409, result)
        run.assert_not_called()
        self.assertEqual(self.runtime.ledger.usage(session["user"]["id"])["used"]["checks"], 1)

    def test_web_snapshot_hybrid_cost_is_reserved_before_compute_and_ignores_client_discount(self):
        session = self.login()
        large = [{"id": "web-fixture-" + str(number), "title": "Fixture", "url": "https://example.org/" + str(number),
                  "text": " ".join(f"ветка{number}_{word}" for word in range(6000))} for number in range(12)]
        small = [{"id": "web-small", "title": "Fixture", "url": "https://example.org/small",
                  "text": " ".join(f"росток{word}" for word in range(1200))}]

        def web_report(sources):
            identifier = uuid4().hex
            payload = {"text": TEXT, "sources": sources, "matches": [], "stats": {}, "scope": "web_search"}
            with patch.object(server.web_search, "status", return_value={"configured": True}), \
                 patch.object(server.web_search, "run", return_value=payload):
                status, result = self.browser.request("POST", "/api/web-check-jobs", {
                    "request_id": identifier, "text": TEXT, "internet_consent": True})
                self.assertEqual(status, 200, result)
                job = self.wait_job(identifier)
                self.assertEqual(job["status"], "complete", job)
            return job["result"]["report"]

        report = web_report(large)
        self.assertEqual(server.retrieval.estimate_embedding_calls(TEXT, large), 64)
        with patch.object(server.retrieval, "retrieve") as retrieve, patch.object(server.llm, "embed") as embed:
            status, result = self.browser.request("POST", "/api/retrieval-jobs", {
                "report_id": report["id"], "mode": "hybrid", "operation_id": uuid4().hex, "model_ops_cost": 1})
            self.assertEqual(status, 429, result)
            self.assertEqual(result["code"], "daily_quota_exceeded")
            retrieve.assert_not_called()
            embed.assert_not_called()
        self.assertEqual(self.runtime.ledger.usage(session["user"]["id"])["used"]["model_ops"], 0)
        report = web_report(small)
        cost = server.retrieval.estimate_embedding_calls(TEXT, small)
        self.assertEqual(cost, 2)
        identifier = uuid4().hex
        body = {"report_id": report["id"], "mode": "hybrid", "operation_id": identifier, "model_ops_cost": 0}
        with patch.object(server.retrieval, "retrieve", return_value={"candidates": [], "stats": {}, "mode": "hybrid"}) as retrieve:
            status, result = self.browser.request("POST", "/api/retrieval-jobs", body)
            self.assertEqual(status, 200, result)
            self.assertEqual(self.wait_job(identifier)["status"], "complete")
            self.assertEqual(self.browser.request("POST", "/api/retrieval-jobs", body)[0], 200)
            self.assertEqual(retrieve.call_count, 1)
        self.assertEqual(self.runtime.ledger.usage(session["user"]["id"])["used"]["model_ops"], cost)

    def test_learning_feedback_uses_only_owned_saved_pair_and_can_be_withdrawn(self):
        session = self.login()
        report = self.check()
        pair = report["matches"][0]
        body = {"report_id": report["id"], "pair_type": "match", "pair_id": pair["id"],
                "user_label": "same_topic", "user_comment": "Needs editor review", "text": "FORGED BODY TEXT"}
        self.assertEqual(self.browser.request("POST", "/api/learning-feedback", body)[0], 400)
        body.update(consent=True, consent_version="2026-11-11-v1")
        status, result = self.browser.request("POST", "/api/learning-feedback", body)
        self.assertEqual(status, 200, result)
        identifier = result["feedback"]["id"]
        saved = self.runtime.learning.get(session["user"]["id"], identifier)
        self.assertEqual(saved["payload"]["text"], pair["excerpt"])
        self.assertNotIn("FORGED BODY TEXT", json.dumps(saved))
        self.assertEqual(self.runtime.learning.export_approved(), [])
        other = Browser(self.httpd)
        self.login(other, "other@example.org")
        self.assertEqual(other.request("DELETE", "/api/learning-feedback/" + identifier)[0], 404)
        self.assertEqual(other.request("POST", "/api/learning-feedback", body)[0], 404)
        self.assertEqual(self.browser.request("DELETE", "/api/learning-feedback/" + identifier)[0], 200)
        self.assertIsNone(self.runtime.learning.get(session["user"]["id"], identifier)["payload"])

    def test_bulk_pair_job_checks_maximum_twenty_and_admits_every_pair(self):
        session = self.login()
        report = self.check()
        with self.runtime.tenants.context(session["user"]["id"]) as tenant:
            record = tenant.store.save("retrieval", {"report_id": report["id"], "candidates": [
                {"id": f"candidate-{number}", "query_excerpt": TEXT, "source_excerpt": TEXT,
                 "query_start": 0, "query_end": len(TEXT), "source_start": 0, "source_end": len(TEXT),
                 "source_title": "Fixture", "source_url": "", "source_id": "fixture", "rank": number}
                for number in range(25)]})
        identifier = uuid4().hex
        with patch.object(server.llm, "judge", return_value={"relation": "same_topic"}) as judge:
            status, result = self.browser.request("POST", "/api/candidate-check-jobs", {"retrieval_id": record["id"], "operation_id": identifier})
            self.assertEqual(status, 200, result)
            job = self.wait_job(identifier)
            self.assertEqual(job["status"], "complete", job)
            self.assertEqual(judge.call_count, 20)
            self.assertEqual(job["result"]["counts"]["total"], 20)
        usage = self.browser.request("GET", "/api/session")[1]["usage"]
        self.assertEqual(usage["used"]["model_ops"], 21)

    def test_old_report_and_batch_children_use_current_device_model_allowance(self):
        first = self.login()
        report = self.check()
        with self.runtime.tenants.context(first["user"]["id"]) as tenant:
            record = tenant.store.save("retrieval", {"report_id": report["id"], "candidates": [
                {"id": f"candidate-{number}", "query_excerpt": TEXT, "source_excerpt": TEXT,
                 "query_start": 0, "query_end": len(TEXT), "source_start": 0, "source_end": len(TEXT),
                 "source_title": "Fixture", "source_url": "", "source_id": "fixture", "rank": number}
                for number in range(2)]})
        other = Browser(self.httpd)
        second = self.login(other, "other@example.org")
        other_report = self.check(other)
        current_device = other.cookies[saas.DEVICE_COOKIE]
        for _ in range(59):
            self.runtime.ledger.reserve_operation(second["user"]["id"], other_report["id"], uuid4().hex,
                                                  "judge", "a" * 64, device_id=current_device)
        # The first account signs in on an already used device, keeping its old report.
        self.browser.cookies[saas.DEVICE_COOKIE] = current_device
        usage = self.browser.request("GET", "/api/session")[1]["usage"]
        self.assertEqual(usage["remaining"]["model_ops"], 1)
        identifier = uuid4().hex
        body = {"retrieval_id": record["id"], "operation_id": identifier}
        with patch.object(server.llm, "judge") as judge, patch.object(server.llm, "rewrite") as rewrite, \
             patch.object(server.retrieval, "retrieve") as retrieve:
            # The batch admission uses the final slot. Neither child may use the
            # older report device's untouched budget to start a comparison.
            status, result = self.browser.request("POST", "/api/candidate-check-jobs", body)
            self.assertEqual(status, 200, result)
            job = self.wait_job(identifier)
            self.assertEqual(job["status"], "complete", job)
            self.assertEqual(job["result"]["counts"]["errors"], 2)
            self.assertEqual({item["code"] for item in job["result"]["judgments"]}, {"daily_quota_exceeded"})
            self.assertEqual(self.browser.request("POST", "/api/candidate-check-jobs", body)[0], 200)
            for path in ("/api/judge", "/api/rewrite"):
                operation_id = uuid4().hex
                status, result = self.browser.request("POST", "/api/model-jobs", {
                    "request_id": operation_id, "path": path, "body": {"operation_id": operation_id,
                    "report_id": report["id"], "match_id": report["matches"][0]["id"]}})
                self.assertEqual(status, 429, result)
                self.assertEqual(result["code"], "daily_quota_exceeded")
            status, result = self.browser.request("POST", "/api/retrieval-jobs", {
                "report_id": report["id"], "mode": "hybrid", "operation_id": uuid4().hex})
            self.assertEqual(status, 429, result)
            judge.assert_not_called()
            rewrite.assert_not_called()
            retrieve.assert_not_called()
        self.assertEqual(self.runtime.ledger.usage(first["user"]["id"])["used"]["model_ops"], 1)
        self.assertEqual(self.browser.request("GET", "/api/session")[1]["usage"]["remaining"]["model_ops"], 0)

    def test_missing_public_configuration_fails_before_binding_or_legacy_store(self):
        with patch.dict(os.environ, {"ISTOK_APP_MODE": "saas"}, clear=True), patch("sys.argv", ["server.py"]):
            with patch.object(server, "ThreadingHTTPServer") as bind, patch.object(server, "Store") as legacy:
                with self.assertRaises(ValueError):
                    server.main()
        bind.assert_not_called()
        legacy.assert_not_called()


class LocalWebQueueTests(unittest.TestCase):
    def test_public_web_report_exposes_actual_snapshot_count_without_source_text(self):
        report = {"scope": "web_search", "source_snapshots_count": 99,
                  "sources": [{"text": "Readable public snapshot"}, {"text": "  "}, {}]}
        public = server.public_report(report)
        self.assertEqual(public["source_snapshots_count"], 1)
        self.assertNotIn("sources", public)
        self.assertNotIn("Readable public snapshot", json.dumps(public))
        self.assertEqual(server.public_report({"scope": "web_search", "sources": []})["source_snapshots_count"], 0)

    def test_local_web_job_saves_derived_report_once_without_account_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "local.sqlite3")
            jobs = Jobs()
            self.addCleanup(jobs.close)
            handler = server.Handler.__new__(server.Handler)
            handler.server = SimpleNamespace(store=store, jobs=jobs)
            identifier = uuid4().hex
            body = {"request_id": identifier, "text": TEXT, "internet_consent": True}
            payload = {"text": TEXT, "sources": [], "matches": [], "stats": {}, "scope": "web_search"}
            with patch.object(server.web_search, "status", return_value={"configured": True}), \
                 patch.object(server.web_search, "run", return_value=payload) as run:
                self.assertEqual(handler.route("POST", "/api/web-check-jobs", body)["job"]["id"], identifier)
                deadline = time.monotonic() + 2
                while not jobs.is_idle() and time.monotonic() < deadline:
                    time.sleep(0.005)
                job = jobs.get(identifier, store)
                self.assertEqual(job["status"], "complete", job)
                handler.route("POST", "/api/web-check-jobs", body)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[1], identifier)
                self.assertIsNotNone(store.get("report", saas.report_identifier(identifier)))
            jobs.close()


if __name__ == "__main__":
    unittest.main()
