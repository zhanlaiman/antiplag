"""Offline release-policy checks; native readers never renew search authority."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

from cloud import modal_web
from cloud.dashboard import password_digest
from cloud.web_beta import BetaHandler, create_server
from plagiarism import search_pool
from plagiarism.access import AccessError
from plagiarism.matching import analyze
from plagiarism.search_provider_policy import CONSENT_VERSION, FreeQuotaExhausted
from plagiarism.search_router import select_search
from plagiarism.search_serper import SerperSearch
from plagiarism.web_search import WebSearchError
from tests.test_integrated_beta import Browser, ORIGIN
from tests.test_integrated_deploy import packet


def policies(keys, expiry):
    return {provider: {"approved": True, "provider": provider,
            "credential_fingerprint": search_pool.fingerprint(keys[provider]),
            "grant_id": "synthetic-" + provider, "account_ref": "synthetic-account",
            "cap": 100, "expires_at": expiry, "proof_sha256": "a" * 64,
            "cash_spend_limit_usd": 0, "max_unit_cost_usd": .004 if provider == "exa" else 0}
            for provider in ("serper", "exa")}


def invalid_extras():
    cycle = []; cycle.append(cycle)
    return [("nan", float("nan")), ("positive-inf", float("inf")),
            ("negative-inf", -float("inf")), ("set", {1}), ("bytes", b"invalid"),
            ("object", object()), ("cycle", cycle)]


class ExpiredGrantBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.keys, self.proof = packet()
        self.sdk = MagicMock()
        for guard in (patch.dict("sys.modules", {"modal": self.sdk}),
                      patch.object(modal_web, "ROOT", Path(self.temporary.name))):
            guard.start(); self.addCleanup(guard.stop)

    def build(self, grants):
        return modal_web.build_app(self.temporary.name, self.keys["tavily"],
            google_key=self.keys["serpapi"], serper_key=self.keys["serper"], exa_key=self.keys["exa"],
            automatic_search=True, integrated=True, default_search_provider="serpapi-yandex",
            dashboard_config={"username": "syntheticadmin", "salt": "a" * 64, "digest": "b" * 64},
            pool_bootstrap=self.proof, pool_grants=grants)

    def test_current_extra_nan_fails_before_sdk(self):
        grants = policies(self.keys, time.time() + 3600)
        grants["serper"]["extra"] = {"unused": [float("nan")]}
        with self.assertRaises((WebSearchError, ValueError, TypeError)):
            self.build(grants)
        self.assertEqual(self.sdk.mock_calls, [])

    def test_expired_builder_preserves_nested_json_policy(self):
        grants = policies(self.keys, time.time() - 60)
        grants["serper"]["extra"] = {"unicode": "данные", "items": [1, None, {"kept": True}]}
        before = deepcopy(grants)
        encoded = json.dumps(grants, sort_keys=True, allow_nan=False)
        self.build(grants)
        self.assertEqual(self.sdk.Secret.from_dict.call_args.args[0]["ISTOK_SEARCH_POOL_GRANTS"], encoded)
        self.assertEqual(grants, before)
        self.assertEqual(self.sdk.App.return_value.function.call_args.kwargs["max_containers"], 1)

    def test_secret_uses_string_cached_before_sdk_construction(self):
        grants = policies(self.keys, time.time() - 60)
        grants["serper"]["extra"] = ["original"]
        encoded = json.dumps(grants, sort_keys=True, allow_nan=False)
        def sdk_effect(*args, **kwargs):
            grants["serper"]["extra"].append(object())
            return self.sdk.volume
        self.sdk.Volume.from_name.side_effect = sdk_effect
        self.build(grants)
        self.assertEqual(self.sdk.Secret.from_dict.call_args.args[0]["ISTOK_SEARCH_POOL_GRANTS"], encoded)

    def test_all_extra_serialization_failures_precede_sdk(self):
        for expired in (False, True):
            for nested in (False, True):
                for name, value in invalid_extras():
                    with self.subTest(expired=expired, nested=nested, extra=name):
                        grants = policies(self.keys, time.time() + (-60 if expired else 3600))
                        grants["exa"]["extra"] = {"unused": [value]} if nested else value
                        self.sdk.reset_mock()
                        with self.assertRaises((WebSearchError, ValueError, TypeError, RecursionError)):
                            self.build(grants)
                        self.assertEqual(self.sdk.mock_calls, [])

    def test_known_field_failures_precede_sdk(self):
        changes = [{"approved": False}, {"provider": "wrong"}, {"credential_fingerprint": "f" * 64},
                   {"grant_id": "bad id"}, {"account_ref": None}, {"proof_sha256": "bad"},
                   {"cash_spend_limit_usd": 1}, {"cash_spend_limit_usd": False},
                   {"max_unit_cost_usd": True}, {"cap": True}, {"cap": 2501},
                   {"expires_at": 0}, {"expires_at": -1}, {"expires_at": True},
                   {"expires_at": float("nan")}, {"expires_at": float("inf")},
                   {"expires_at": -float("inf")}]
        for provider in ("serper", "exa"):
            for change in changes:
                with self.subTest(provider=provider, field=list(change)):
                    grants = policies(self.keys, time.time() + 3600); grants[provider].update(change)
                    self.sdk.reset_mock()
                    with self.assertRaises((WebSearchError, ValueError, TypeError)):
                        self.build(grants)
                    self.assertEqual(self.sdk.mock_calls, [])
        for grants in ({}, {"serper": None}, {"serper": {}, "exa": {}, "other": {}}):
            self.sdk.reset_mock()
            with self.assertRaises((WebSearchError, ValueError, TypeError)):
                self.build(grants)
            self.assertEqual(self.sdk.mock_calls, [])

    def test_direct_validator_keeps_extras_but_current_authorization_rejects_expiry(self):
        for provider in ("serper", "exa"):
            policy = policies(self.keys, time.time() - 60)[provider]
            policy["extra"] = {"retained": [None, "данные", 1]}; before = deepcopy(policy)
            result = search_pool.validate_grant_policy(policy, provider, self.keys[provider])
            self.assertEqual(result, before); self.assertIsNot(result, policy); self.assertEqual(policy, before)
            with self.assertRaises(WebSearchError) as error:
                search_pool.checked_grant(policy, provider, self.keys[provider])
            self.assertEqual(error.exception.code, "free_quota_unconfirmed")
            self.assertNotIsInstance(error.exception, FreeQuotaExhausted)
            for name, value in invalid_extras():
                for nested in (False, True):
                    with self.subTest(provider=provider, extra=name, nested=nested):
                        candidate = {**policy, "extra": {"unused": [value]} if nested else value}
                        with self.assertRaises(WebSearchError) as invalid:
                            search_pool.validate_grant_policy(candidate, provider, self.keys[provider])
                        self.assertEqual(invalid.exception.code, "free_quota_unconfirmed")
        self.assertEqual(self.sdk.mock_calls, [])


class ExpiredGrantBudgetTests(unittest.TestCase):
    def test_expiry_between_preflight_and_real_adapter_reserve_never_sends_or_charges(self):
        with TemporaryDirectory() as directory:
            keys, _ = packet(); expiry = time.time() + 60; clock = [expiry - 1]
            policy = policies(keys, expiry)["serper"]
            budget = search_pool.PoolBudget(Path(directory) / "budget.sqlite3", provider="serper",
                resource="grant:serper:synthetic-account:synthetic-serper",
                credential_fingerprint=search_pool.fingerprint(keys["serper"]), grant=policy,
                now=lambda: datetime.fromtimestamp(clock[0], timezone.utc))
            budget.preflight(1)
            wire = Mock(side_effect=AssertionError("Provider wire forbidden"))
            with patch("plagiarism.search_serper.build_opener", return_value=Mock(open=wire)):
                adapter = SerperSearch(budget, key=keys["serper"])
                clock[0] = expiry + 1
                with self.assertRaises(WebSearchError) as error:
                    adapter.search("synthetic phrase", "expired-at-reserve")
            self.assertEqual(error.exception.code, "free_quota_unconfirmed")
            self.assertNotIsInstance(error.exception, FreeQuotaExhausted); wire.assert_not_called()
            with budget.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM calls").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM pool_calls").fetchone()[0], 0)
                self.assertEqual(json.loads(db.execute("SELECT policy FROM pool_grants").fetchone()[0])["expires_at"], expiry)


class ExpiredGrantNativeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name); self.root = self.base / "state"
        self.keys, self.proof = packet(); self.expiry = time.time() + 60
        self.grants = policies(self.keys, self.expiry); self.service = None
        self.runner = Mock(side_effect=AssertionError("Provider execution forbidden"))
        self.outbound = []
        environment = patch.dict(os.environ, {"ISTOK_AUTOMATIC_SEARCH": "1", "ISTOK_WEB_WIKI_FOLLOWUP": "0"}, clear=True)
        environment.start(); self.addCleanup(environment.stop)
        connect, connect_ex, lookup = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
        def allowed(address):
            if not (self.service and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1", "localhost"}
                    and address[1] == self.service.server_port):
                self.outbound.append(str(address)); raise AssertionError("Only owned loopback HTTP permitted")
        def guarded_connect(sock, address): allowed(address); return connect(sock, address)
        def guarded_connect_ex(sock, address): allowed(address); return connect_ex(sock, address)
        def guarded_lookup(host, *args, **kwargs):
            if host not in {"127.0.0.1", "::1", "localhost"}:
                self.outbound.append(str(host)); raise AssertionError("Foreign DNS forbidden")
            return lookup(host, *args, **kwargs)
        for target, replacement in (("socket.socket.connect", guarded_connect),
                ("socket.socket.connect_ex", guarded_connect_ex), ("socket.getaddrinfo", guarded_lookup)):
            guard = patch(target, replacement); guard.start(); self.addCleanup(guard.stop)
        self.addCleanup(self.stop)
        self.admin_password = "Synthetic-admin-password-77!"
        self.config = {"username": "syntheticadmin", "salt": "b" * 64,
                       "digest": password_digest(self.admin_password, "b" * 64)}
        self.start(self.proof)
        self.client = Browser(self); self.assertEqual(self.client.request("/api/account/session")[0], 200)
        self.assertEqual(self.client.request("/api/account/register",
            {"username": "expired_fixture", "password": "Synthetic-user-password-77!"})[0], 200)
        self.guest = self.client.request("/api/session")[1]["beta"]["id"]
        self.admin = Browser(self)
        self.assertEqual(self.admin.request("/api/dashboard/login",
            {"username": self.config["username"], "password": self.admin_password})[0], 200)
        with self.service.beta.tenants.context(self.guest) as tenant:
            self.report = tenant.store.save("report", {**analyze("Synthetic saved report", []),
                "id": "1" * 32, "text": "Synthetic saved report", "sources": [], "scope": "web_search"})
        with closing(self.service.beta.connect()) as db, db:
            db.execute("CREATE TABLE owner_session_revocations(token_sha256 TEXT PRIMARY KEY,expires_at INTEGER NOT NULL)")
            db.execute("INSERT INTO owner_session_revocations VALUES (?,?)", ("2" * 64, int(self.expiry + 3600)))
        runtime = self.service.beta.pool_runtime
        self.pin = runtime.pin(self.guest, "3" * 32, "4" * 64)
        budget = search_pool.PoolBudget(self.root / "search-budget.sqlite3", provider="serper",
            resource="grant:serper:synthetic-account:synthetic-serper", credential_fingerprint=search_pool.fingerprint(self.keys["serper"]),
            grant=self.grants["serper"], limit=900)
        budget.reserve("synthetic-existing"); budget.finish("synthetic-existing")
        self.stop()

    def start(self, bootstrap=None):
        self.service = create_server(self.root, ORIGIN, host="127.0.0.1", port=0, allow_http=True,
            runner=self.runner, integrated=True, registration_enabled=True, business_writes_enabled=True,
            automatic_monthly_limit=900, pool_grants=self.grants, pool_keys=self.keys,
            pool_bootstrap=bootstrap, dashboard_config=self.config)
        self.assertIs(self.service.RequestHandlerClass, BetaHandler)
        self.thread = Thread(target=self.service.serve_forever, daemon=True); self.thread.start()

    def stop(self):
        if self.service:
            self.service.shutdown(); self.service.server_close(); self.service.beta.tenants.close()
            self.thread.join(); self.service = None

    def clone(self, name):
        target = self.base / name; shutil.copytree(self.base / "state", target)
        for original in (self.base / "state").rglob("*.sqlite3"):
            with closing(sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True)) as source, \
                 closing(sqlite3.connect(target / original.relative_to(self.base / "state"))) as destination:
                source.backup(destination)
        self.root = target

    def snapshot(self):
        protected = {"records", "source_snapshots", "match_reviews", "owner_session_revocations",
                     "accounts", "sessions", "local_credentials", "account_guest_bindings",
                     "calls", "pool_calls", "pool_grants", "pool_job_pins"}
        rows = {}
        for path in sorted(self.root.rglob("*.sqlite3")):
            with closing(sqlite3.connect(path)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} & protected
                rows[str(path.relative_to(self.root))] = {name: sorted(db.execute('SELECT * FROM "' + name + '"').fetchall(), key=repr)
                                                        for name in sorted(tables)}
        files = [self.root / "runtime/pool-bootstrap.json", *sorted((self.root / "search-pool-vault").rglob("*"))]
        hashes = {str(p.relative_to(self.root)): sha256(p.read_bytes()).hexdigest()
                  for p in files if p.is_file() and not p.name.endswith(".lock")}
        return rows, hashes

    def test_completed_vault_expired_restart_keeps_native_reads_and_all_historical_state(self):
        self.clone("restart"); before = self.snapshot(); policy_bytes = json.dumps(self.grants, sort_keys=True)
        with patch("time.time", return_value=self.expiry + 1):
            self.start()
            for path in ("/api/reports/" + self.report["id"], "/api/reports", "/api/status"):
                self.assertEqual(self.client.request(path)[0], 200, path)
            report = self.client.request("/api/reports/" + self.report["id"])[1]["report"]
            self.assertEqual(report["created_at"], self.report["created_at"])
            self.assertEqual(report["text"], self.report["text"]); self.assertEqual(report["stats"], self.report["stats"])
            status, pool, _ = self.admin.request("/api/dashboard/pool"); self.assertEqual(status, 200, pool)
            for row in pool["providers"]:
                if row["provider"] in ("serper", "exa"):
                    self.assertFalse(row["grant_approved"]); self.assertEqual(row["health"], "blocked")
            runtime = self.service.beta.pool_runtime
            for provider in ("serper", "exa"):
                identifier = runtime.pool_admin.vault.read()[0]["active"][provider]
                with self.assertRaises(AccessError) as error: runtime.pool_admin.activate(identifier)
                self.assertEqual(error.exception.code, "pool_grant_required")
            factories = {name: Mock(side_effect=AssertionError("Expired policy must stop selection"))
                         for name in ("serper", "exa", "tavily")}
            with self.assertRaises(WebSearchError) as error:
                select_search(self.root / "search-budget.sqlite3", 1, keys=self.keys, grants=self.grants,
                    factories=factories, disabled=("serpapi-yandex",))
            self.assertEqual(error.exception.code, "free_quota_unconfirmed")
            self.assertNotIsInstance(error.exception, FreeQuotaExhausted)
            for factory in factories.values(): factory.assert_not_called()
            self.assertEqual(runtime.grants_for(self.pin), self.grants)
            self.assertEqual(json.dumps(self.grants, sort_keys=True), policy_bytes)
            self.stop()
        self.assertEqual(self.snapshot(), before)
        self.runner.assert_not_called(); self.assertEqual(self.outbound, [])

    def test_expired_empty_and_missing_or_partial_bootstrap_never_reseed_authority(self):
        with patch("time.time", return_value=self.expiry + 1):
            self.root = self.base / "empty"
            with self.assertRaises(AccessError) as error: self.start(self.proof)
            self.assertEqual(error.exception.code, "pool_unconfigured")
            self.assertFalse((self.root / "search-pool-vault/vault.json").exists())
            for fault in ("marker", "vault", "pins", "partial"):
                with self.subTest(fault=fault):
                    self.clone("missing-" + fault)
                    marker = self.root / "runtime/pool-bootstrap.json"
                    if fault == "marker": marker.unlink()
                    elif fault == "vault": (self.root / "search-pool-vault/vault.json").unlink()
                    elif fault == "pins":
                        with sqlite3.connect(self.root / "beta.sqlite3") as db: db.execute("DROP TABLE pool_job_pins")
                    else:
                        value = json.loads(marker.read_text()); value["status"] = "in_progress"
                        marker.write_text(json.dumps(value)); marker.chmod(0o600)
                    before = self.snapshot()
                    if fault == "partial":
                        self.start(self.proof)
                        with self.assertRaises(AccessError) as error:
                            self.service.beta.pool_runtime.pin(self.guest, "5" * 32, "6" * 64)
                        self.assertEqual(error.exception.code, "pool_recovery_required")
                        status, response, _ = self.client.request("/api/web-check-jobs", {
                            "request_id": "5" * 32, "text": "Synthetic phrase for dispatch", "internet_consent": True,
                            "search_provider": "auto", "provider_consent_version": CONSENT_VERSION})
                        self.assertEqual((status, response["code"]), (503, "pool_recovery_required"))
                        self.stop()
                    else:
                        with self.assertRaises(AccessError): self.start(self.proof)
                    self.assertEqual(self.snapshot(), before)
        self.runner.assert_not_called(); self.assertEqual(self.outbound, [])


if __name__ == "__main__":
    unittest.main()
