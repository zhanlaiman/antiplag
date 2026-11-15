"""Independent native BetaHandler/pool execution QA; all credentials synthetic.

Only the owned local HTTP port is reachable. Search, selection, queue, persistence,
handlers and account guards are real; only provider adapter I/O is replaced.
"""
from copy import deepcopy
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from cloud.dashboard import DashboardConfig, password_digest
from cloud.web_beta import BetaHandler, create_server
from plagiarism import saas
from plagiarism.search_exa import ExaSearch
from plagiarism.search_mediawiki import MediaWikiSearch
from plagiarism.search_pool import PoolBudget, fingerprint
from plagiarism.search_provider_policy import CONSENT_VERSION
from plagiarism.search_serpapi import YandexSerpAPISearch
from plagiarism.search_serper import SerperSearch
from plagiarism.search_tavily import TavilySearch


ORIGIN = "http://beta.example"
TEXT = " ".join([
    "Сотрудники экспериментальной лаборатории изучали распределение влаги в образцах древесины при различных температурах и сопоставляли результаты последовательных измерений.",
    "Для каждого независимого опыта исследователи подготовили отдельный протокол наблюдений и подробно описали использованные инструменты проверки.",
    "После завершения измерений авторы сравнили полученные значения с контрольными данными и сформулировали осторожные выводы о выявленных закономерностях.",
    "Дополнительная серия опытов позволила уточнить влияние продолжительности хранения материала на итоговые характеристики проверенных образцов.",
] * 3)


def account(used=0):
    return {"account_id": "independent-qa-account", "account_status": "Active", "plan_name": "Free Plan",
            "plan_monthly_price": 0, "searches_per_month": 250, "this_month_usage": used,
            "plan_searches_left": 250-used, "total_searches_left": 250-used, "extra_credits": 0}


class Client:
    def __init__(self, case):
        self.case, self.cookies, self.csrf, self.admin_csrf = case, {}, "", ""

    def request(self, path, body=None):
        headers = {"Host": "beta.example", "Origin": ORIGIN,
                   "Cookie": "; ".join(k + "=" + v for k, v in self.cookies.items())}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
            headers["X-CSRF-Token" if path.startswith("/api/dashboard") else "X-Istok-CSRF"] = (
                self.admin_csrf if path.startswith("/api/dashboard") else self.csrf)
        connection = HTTPConnection("127.0.0.1", self.case.service.server_port, timeout=5)
        try:
            connection.request("GET" if body is None else "POST", path, data, headers)
            response = connection.getresponse(); raw = response.read()
            for name, value in response.getheaders():
                if name.lower() == "set-cookie":
                    cookies = SimpleCookie(); cookies.load(value)
                    for key, item in cookies.items():
                        if item["max-age"] == "0": self.cookies.pop(key, None)
                        else: self.cookies[key] = item.value
            result = json.loads(raw)
            if "csrf_token" in result:
                if path.startswith("/api/dashboard"): self.admin_csrf = result["csrf_token"]
                else: self.csrf = result["csrf_token"]
            # Responses must never disclose even our synthetic full credentials.
            for key in self.case.private_keys: self.case.assertNotIn(key, raw.decode())
            return response.status, result
        finally:
            connection.close()


class IntegratedPoolJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = secrets.token_urlsafe(28); salt = secrets.token_hex(32)
        cls.config = DashboardConfig("pool_execution_qa", salt, password_digest(cls.password, salt))

    def setUp(self):
        self.temporary = TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name); self.service = None
        self.events, self.network, self.releases = [], [], []
        self.mode = "normal"
        self.keys = {"serpapi": "offline-serpapi-" + "1" * 32,
                     "serper": "offline-serper-" + "2" * 32,
                     "tavily": "tvly-offline-" + "3" * 32,
                     "exa": "offline-exa-" + "4" * 32}
        self.private_keys = set(self.keys.values())
        now = time.time()
        self.grants = {provider: {"provider": provider, "credential_fingerprint": fingerprint(self.keys[provider]),
            "approved": True, "cash_spend_limit_usd": 0, "max_unit_cost_usd": 0.004 if provider == "exa" else 0,
            "cap": 300, "expires_at": now + 3600, "grant_id": "synthetic-grant-one", "account_ref": "synthetic-account",
            "proof_sha256": "a" * 64} for provider in ("serper", "exa")}
        self.packet = {"version": 1, "packet_id": "a" * 32, "captured_at": now,
            "credential_fingerprints": {p: fingerprint(k) for p, k in self.keys.items()},
            "proofs": {p: {"key_fingerprint": fingerprint(self.keys[p]), "checked_at": now,
                          "usage": account() if p == "serpapi" else {
                              "account": {"current_plan": "Researcher", "plan_limit": 1000, "plan_usage": 809,
                                          "paygo_usage": 0, "paygo_limit": None}, "key": {"usage": 809, "limit": None}}}
                       for p in ("serpapi", "tavily")}}
        environment = patch.dict(os.environ, {"ISTOK_AUTOMATIC_SEARCH": "1", "ISTOK_WEB_WIKI_FOLLOWUP": "0",
            "ISTOK_WEB_RELATED_FALLBACK": "0", "ISTOK_WEB_ZERO_FALLBACK": "0", "ISTOK_WEB_NAMED_REFERENCES": "0",
            "ISTOK_SERPAPI_API_KEY": "unrelated-environment-" + "9" * 32}, clear=True)
        environment.start(); self.addCleanup(environment.stop)
        original_connect, original_connect_ex, original_dns = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
        def validate(address):
            if (self.service is None or not isinstance(address, tuple) or address[0] != "127.0.0.1"
                    or address[1] != self.service.server_port):
                self.network.append(str(address)); raise AssertionError("Outbound network forbidden")
        def connect(sock, address):
            validate(address)
            return original_connect(sock, address)
        def connect_ex(sock, address):
            validate(address)
            return original_connect_ex(sock, address)
        def dns(host, *args, **kwargs):
            if host not in ("127.0.0.1", "localhost"):
                self.network.append(str(host)); raise AssertionError("Provider DNS forbidden")
            return original_dns(host, *args, **kwargs)
        for target, replacement in (("socket.socket.connect", connect), ("socket.socket.connect_ex", connect_ex),
                                    ("socket.getaddrinfo", dns)):
            guard = patch(target, replacement); guard.start(); self.addCleanup(guard.stop)
        def request(adapter, path, parameters, timeout):
            self.events.append((path, adapter.key))
            if path == "account.json":
                if self.mode == "unknown": return {"error": "synthetic invalid quota"}
                if self.mode == "quota429": raise HTTPError("https://synthetic.invalid", 429, "rate", {}, BytesIO())
                return account()
            if self.mode == "search429": raise HTTPError("https://synthetic.invalid", 429, "rate", {}, BytesIO())
            return {"search_metadata": {"status": "Success"}, "organic_results": []}
        boundary = patch.object(YandexSerpAPISearch, "_request", request); boundary.start(); self.addCleanup(boundary.stop)
        self.fallbacks = []
        for cls in (SerperSearch, TavilySearch, ExaSearch, MediaWikiSearch):
            boundary = patch.object(cls, "search", side_effect=AssertionError("Unexpected fallback/provider call"))
            spy = boundary.start(); self.fallbacks.append(spy); self.addCleanup(boundary.stop)
        self.start()

    def start(self, restart=False):
        self.service = create_server(self.root, ORIGIN, host="127.0.0.1", port=0, allow_http=True,
            integrated=True, registration_enabled=True, business_writes_enabled=True, automatic_monthly_limit=900,
            pool_bootstrap=None if restart else self.packet, pool_keys=None if restart else self.keys,
            pool_grants=self.grants, dashboard_config=self.config)
        self.assertIs(self.service.RequestHandlerClass, BetaHandler)
        self.thread = Thread(target=self.service.serve_forever, daemon=True); self.thread.start()
        self.service.pool_admin.usage_probe = lambda provider, key: deepcopy(self.packet["proofs"][provider]["usage"])

    def stop(self):
        for release in self.releases: release.set()
        if self.service:
            port=self.service.server_port
            self.service.shutdown(); self.service.server_close(); self.service.beta.tenants.close()
            self.thread.join(timeout=5); self.assertFalse(self.thread.is_alive())
            with socket.socket() as probe:
                probe.settimeout(1);self.assertNotEqual(probe.connect_ex(("127.0.0.1",port)),0)
            self.service = None

    def tearDown(self):
        try: self.stop()
        finally:
            self.assertEqual(self.network, [])
            for spy in self.fallbacks: spy.assert_not_called()

    def client(self):
        client = Client(self); self.assertEqual(client.request("/api/account/session")[0], 200)
        status, payload = client.request("/api/session"); self.assertEqual(status, 200, payload)
        return client, payload["beta"]["id"]

    def admin(self):
        client = Client(self)
        status, payload = client.request("/api/dashboard/login", {"username": self.config.username, "password": self.password})
        self.assertEqual(status, 200, payload); return client

    @staticmethod
    def body(identifier, auto=True):
        return {"request_id": identifier, "text": TEXT, "internet_consent": True,
                **({"search_provider": "auto", "provider_consent_version": CONSENT_VERSION} if auto else {})}

    def submit(self, client, identifier, auto=True):
        status, result = client.request("/api/web-check-jobs", self.body(identifier, auto))
        self.assertEqual(status, 200, result); return result["job"]

    def finish(self, client, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, result = client.request("/api/jobs/" + identifier); self.assertEqual(status, 200, result)
            if result["job"]["status"] in ("complete", "failed", "cancelled"): return result["job"]
            time.sleep(.01)
        self.fail("Native job did not finish")

    def hold_worker(self):
        entered, release = Event(), Event(); self.releases.append(release)
        def hold(): entered.set(); release.wait(10)
        future = self.service.beta.tenants.coordinator.executor.submit(hold)
        self.assertTrue(entered.wait(2)); return release, future

    def pin(self, guest, body):
        operation = {key: value for key, value in body.items()}
        return self.service.pool_runtime.get_pin(guest, body["request_id"], saas.fingerprint({"path": "/api/web-check", "body": operation}))

    def test_queued_job_executes_old_credential_after_rotation_and_new_job_uses_new(self):
        client, guest = self.client(); admin = self.admin(); old = self.keys["serpapi"]
        release, held = self.hold_worker(); queued = self.submit(client, "1" * 32)
        self.assertEqual(queued["status"], "queued"); self.assertEqual(self.events, [])
        pin = self.pin(guest, self.body("1" * 32)); old_id = pin["active"]["serpapi"]
        new = "offline-rotated-" + "5" * 32
        self.private_keys.add(new)
        status, result = admin.request("/api/dashboard/pool/stage", {"provider": "serpapi", "api_key": new}); self.assertEqual(status, 200, result)
        new_id = result["credential_id"]
        for action in ("validate", "activate"):
            self.assertEqual(admin.request("/api/dashboard/pool/" + action, {"credential_id": new_id})[0], 200)
        self.assertNotEqual(old_id, new_id)
        self.assertEqual(self.pin(guest,self.body("1"*32)),pin)
        release.set(); held.result(2)
        self.assertEqual(self.finish(client, "1" * 32)["status"], "complete")
        self.assertTrue(self.events); self.assertTrue(all(key == old for _, key in self.events))
        self.events.clear(); self.submit(client, "2" * 32)
        self.assertEqual(self.finish(client, "2" * 32)["status"], "complete")
        self.assertEqual(self.pin(guest,self.body("2"*32))["active"]["serpapi"],new_id)
        self.assertTrue(self.events)
        self.assertTrue(all(key == new for _, key in self.events))

    def test_disable_does_not_retarget_queued_job_and_new_all_disabled_job_stops(self):
        client, _ = self.client(); admin = self.admin(); release, held = self.hold_worker()
        self.submit(client, "3" * 32)
        for provider in self.keys:
            self.assertEqual(admin.request("/api/dashboard/pool/disable", {"provider": provider})[0], 200)
        release.set(); held.result(2)
        self.assertEqual(self.finish(client, "3" * 32)["status"], "complete")
        before = len(self.events); self.submit(client, "4" * 32)
        job = self.finish(client, "4" * 32)
        self.assertEqual((job["status"], job["code"]), ("failed", "search_budget_exhausted"))
        self.assertEqual(len(self.events), before)

    def test_exact_replay_changed_body_and_restart_never_issue_new_search(self):
        client, _ = self.client(); self.submit(client, "5" * 32); job = self.finish(client, "5" * 32)
        self.assertEqual(job["status"], "complete"); before = len(self.events)
        self.assertEqual(self.submit(client, "5" * 32)["result"], job["result"])
        status, _ = client.request("/api/web-check-jobs", {**self.body("5" * 32), "text": TEXT + " Новый текст."})
        self.assertEqual(status, 409); self.assertEqual(len(self.events), before)
        self.stop(); self.start(restart=True)
        self.assertEqual(self.submit(client, "5" * 32)["result"], job["result"])
        self.assertEqual(len(self.events), before)

    def test_orphan_pin_plus_pending_vault_denies_new_job_before_provider(self):
        client, guest = self.client(); body = self.body("6" * 32)
        self.service.pool_runtime.pin(guest, body["request_id"], saas.fingerprint({"path": "/api/web-check", "body": body}))
        state, _ = self.service.pool_admin.vault.read()
        self.service.pool_admin.vault._write("pending.json", {"previous": state})
        status, result = client.request("/api/web-check-jobs", body)
        self.assertEqual((status, result["code"]), (503, "pool_recovery_required")); self.assertEqual(self.events, [])

    def test_pending_vault_still_allows_exact_saved_result_after_restart_without_search(self):
        client, _ = self.client();self.submit(client,"0"*32);saved=self.finish(client,"0"*32)
        self.assertEqual(saved["status"],"complete");before=len(self.events)
        state,_=self.service.pool_admin.vault.read();self.service.pool_admin.vault._write("pending.json",{"previous":state})
        self.assertEqual(self.submit(client,"0"*32)["result"],saved["result"])
        self.stop();self.start(restart=True)
        self.assertEqual(self.submit(client,"0"*32)["result"],saved["result"])
        self.assertEqual(client.request("/api/web-check-jobs",self.body("9"*32))[0],503)
        self.assertEqual(len(self.events),before)

    def test_uncertain_durable_request_recovers_interrupted_without_new_search_under_pending(self):
        client,guest=self.client();body=self.body("9"*32)
        body_hash=saas.fingerprint({"path":"/api/web-check","body":body})
        self.service.pool_runtime.pin(guest,body["request_id"],body_hash)
        self.service.beta.admit_check(guest,body["request_id"],TEXT)
        with self.service.beta.tenants.context(guest) as tenant:
            tenant.store.save("model-request",{"id":"model-request:"+body["request_id"],
                "path":"/api/web-check","request_fingerprint":body_hash})
        state,_=self.service.pool_admin.vault.read();self.service.pool_admin.vault._write("pending.json",{"previous":state})
        self.stop();self.start(restart=True)
        job=self.submit(client,"9"*32)
        self.assertEqual((job["status"],job["code"]),("failed","job_interrupted"))
        self.assertEqual(self.submit(client,"9"*32)["code"],"job_interrupted")
        self.assertEqual(self.events,[])

    def test_unknown_quota_and_preflight429_stop_without_fallback(self):
        client, _ = self.client()
        daily=client.request("/api/session")[1]["beta"]["daily_remaining"]
        for index, mode in enumerate(("unknown", "quota429")):
            self.mode = mode; before = len(self.events); identifier = str(index + 7) * 32
            self.submit(client, identifier); job = self.finish(client, identifier)
            self.assertEqual((job["status"], job["code"]), ("failed", "free_quota_unconfirmed"))
            self.assertEqual([path for path, _ in self.events[before:]], ["account.json"])
            stopped=len(self.events);self.assertEqual(self.submit(client,identifier)["code"],"free_quota_unconfirmed")
            self.assertEqual(len(self.events),stopped)
        self.assertEqual(client.request("/api/session")[1]["beta"]["daily_remaining"],daily)

    def test_search429_stops_selected_provider_without_fallback(self):
        client, _ = self.client(); self.mode = "search429"
        self.submit(client, "a" * 32); job = self.finish(client, "a" * 32)
        self.assertEqual(job["status"], "complete")
        errors = job["result"]["report"]["web"]["errors"]
        self.assertIn("search_pool_stopped", [row["code"] for row in errors])
        self.assertEqual([path for path, _ in self.events], ["account.json", "search.json"])
        self.assertEqual(self.submit(client,"a"*32)["result"],job["result"])
        self.assertEqual(len(self.events),2)
        with sqlite3.connect(self.root/"search-budget.sqlite3") as db:
            self.assertEqual(db.execute("SELECT state FROM calls").fetchall(),[("started",)])

    def test_blocked_queued_job_and_new_admission_guard_before_provider(self):
        client, guest = self.client(); admin = self.admin(); release, held = self.hold_worker()
        self.submit(client, "b" * 32)
        status, result = admin.request("/api/dashboard/accounts/" + guest + "/block", {"reason": "Synthetic queue boundary"})
        self.assertEqual(status, 200, result)
        self.assertEqual(client.request("/api/web-check-jobs", self.body("c" * 32))[0], 403)
        release.set(); held.result(2)
        with self.service.beta.tenants.context(guest) as tenant:
            deadline=time.monotonic()+3
            while tenant.jobs.get("b"*32,tenant.store)["status"] not in ("failed","complete") and time.monotonic()<deadline: time.sleep(.01)
            job=tenant.jobs.get("b"*32,tenant.store)
        self.assertEqual((job["status"],job["code"]),("failed","account_blocked")); self.assertEqual(self.events, [])

    def test_legacy180_and_auto900_share_historical177_without_reset(self):
        client, _ = self.client(); path=self.root/"search-budget.sqlite3"
        budget=PoolBudget(path,provider="seed",resource="seed",credential_fingerprint="",limit=180)
        month=datetime.now(timezone.utc).strftime("%Y-%m")
        with budget.connect() as db:
            db.executemany("INSERT INTO calls VALUES(?,?,'complete')",[("historical-"+str(i),month) for i in range(177)])
        self.submit(client,"d"*32,auto=False); legacy=self.finish(client,"d"*32)
        self.assertEqual((legacy["status"],legacy["code"]),("failed","search_budget_exhausted"));self.assertEqual(self.events,[])
        self.submit(client,"e"*32);self.assertEqual(self.finish(client,"e"*32)["status"],"complete")
        with budget.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM calls WHERE id LIKE 'historical-%'").fetchone()[0],177)
            self.assertEqual(db.execute("SELECT count(*) FROM calls").fetchone()[0],183)
        self.assertEqual(self.service.pool_admin.status()["monthly_used"],183)

    def test_missing_credential_state_denies_http_admission_without_fallback(self):
        client, _ = self.client()
        def corrupt(state):
            state["records"][state["active"]["serpapi"]]["api_key"]=""
        self.service.pool_admin.vault.mutate(corrupt)
        status, result=client.request("/api/web-check-jobs",self.body("f"*32))
        self.assertEqual(status,503,result);self.assertEqual(self.events,[])


if __name__ == "__main__": unittest.main()
