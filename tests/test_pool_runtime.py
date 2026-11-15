"""Real private files/SQLite; every credential and quota response is synthetic."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cloud.pool_runtime import PoolRuntime, validate_bootstrap
from plagiarism.access import AccessError
from plagiarism.search_pool import PoolBudget
from plagiarism.web_search import SearchBudget, WebSearchError


def serp_usage(used=20):
    return {"account_id": "synthetic-account", "account_status": "Active", "plan_name": "Free Plan",
            "plan_monthly_price": 0, "searches_per_month": 250, "plan_searches_left": 250-used,
            "extra_credits": 0, "total_searches_left": 250-used, "this_month_usage": used}


def tavily_usage(used=20):
    return {"account": {"current_plan": "Researcher", "plan_usage": used, "plan_limit": 1000,
                        "paygo_usage": 0, "paygo_limit": 0}, "key": {"usage": used, "limit": 1000}}


class PoolRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="istok-pool-runtime-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        runtime = self.root / "runtime"; runtime.mkdir(mode=0o700)
        database = self.root / "beta.sqlite3"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE preserved(value TEXT)")
            db.execute("INSERT INTO preserved VALUES('synthetic-original')")
        self.beta = SimpleNamespace(root=self.root, runtime=runtime, database=database, persist=Mock())
        self.now = time.time()
        self.keys = {p: ("tvly-" if p == "tavily" else p + "-") + "synthetic-fixture-" + "x" * 20
                     for p in ("serpapi", "tavily", "serper", "exa")}
        self.grants = {p: {"provider": p, "approved": True,
                          "credential_fingerprint": sha256(self.keys[p].encode()).hexdigest(),
                          "account_ref": "synthetic-" + p, "grant_id": "synthetic-free-" + p,
                          "cap": 100, "expires_at": self.now + 3600, "cash_spend_limit_usd": 0,
                          "max_unit_cost_usd": 0.004 if p == "exa" else 0, "proof_sha256": "a" * 64}
                       for p in ("serper", "exa")}
        self.packet = {"version": 1, "packet_id": "b" * 32, "captured_at": self.now,
                       "proofs": {p: {"key_fingerprint": sha256(self.keys[p].encode()).hexdigest(),
                                      "checked_at": self.now, "usage": value}
                                  for p, value in [("serpapi", serp_usage()), ("tavily", tavily_usage())]},
                       "credential_fingerprints": {p: sha256(k.encode()).hexdigest() for p, k in self.keys.items()}}
        self.guest, self.request, self.body = "c" * 64, "d" * 32, "e" * 64

    def create(self, **kwargs):
        return PoolRuntime(self.beta, monthly_limit=900, grants=self.grants,
                           bootstrap=self.packet, keys=self.keys, **kwargs)

    def assert_code(self, code, operation):
        with self.assertRaises(AccessError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(self.keys["serpapi"], str(caught.exception))

    def seed_calls(self, count):
        budget = SearchBudget(self.root / "search-budget.sqlite3", limit=900)
        with budget.connect() as db:
            db.executemany("INSERT INTO calls VALUES(?,strftime('%Y-%m','now'),'complete')",
                           [("synthetic-" + str(i),) for i in range(count)])

    def test_unconfigured_controlplane_constructs_but_no_pin_or_fake_counts(self):
        runtime = PoolRuntime(self.beta)
        self.assertIs(self.beta.pool_admin, runtime.pool_admin)
        self.assertIsNone(runtime.get_pin(self.guest, self.request, self.body))
        self.assert_code("pool_unconfigured", lambda: runtime.pin(self.guest, self.request, self.body))
        self.assertIsNone(runtime.metrics()["monthly_used"])
        self.assertFalse(runtime.marker.exists())

    def test_pure_bootstrap_parser_freshness_paid_shape_fingerprint_and_redaction(self):
        accepted = validate_bootstrap(self.packet, self.keys, self.now)
        self.assertEqual(accepted, self.packet); self.assertIsNot(accepted, self.packet)
        variants = []
        for mutate in [lambda p: p.update(version=True),
                       lambda p: p.update(captured_at=self.now - 301),
                       lambda p: p["proofs"]["serpapi"].update(checked_at=self.now + 1),
                       lambda p: p["credential_fingerprints"].update(serper="0" * 64),
                       lambda p: p["proofs"]["tavily"]["usage"]["account"].update(paygo_usage=1),
                       lambda p: p["proofs"]["serpapi"]["usage"].update(api_key=self.keys["serpapi"]),
                       lambda p: p["proofs"]["serpapi"]["usage"].update(email="synthetic@example.test")]:
            candidate = deepcopy(self.packet); mutate(candidate); variants.append(candidate)
        for candidate in variants:
            with self.subTest():
                self.assert_code("pool_unconfigured", lambda: validate_bootstrap(candidate, self.keys, self.now))
        self.assertFalse((self.root / "search-pool-vault").exists())

    def test_bootstrap_uses_only_captured_proofs_then_restores_probe_and_clock(self):
        self.seed_calls(177)
        live = Mock(side_effect=AssertionError("Provider calls forbidden"))
        with patch("plagiarism.search_pool_admin.probe_usage", live):
            runtime = self.create()
        live.assert_not_called()
        self.assertIs(runtime.pool_admin.usage_probe, live)
        marker = json.loads(runtime.marker.read_text())
        self.assertEqual(marker["status"], "complete")
        self.assertEqual(runtime.marker.stat().st_mode & 0o777, 0o600)
        snapshot = runtime.pool_admin.runtime_snapshot()
        self.assertEqual(set(snapshot["active"]), set(self.keys))
        for p in ("serpapi", "tavily"):
            self.assertEqual(snapshot["active"][p]["verification"]["checked_at"], self.now)
        pin = runtime.pin(self.guest, self.request, self.body)
        self.assertEqual(runtime.credentials(pin)["serpapi-yandex"], self.keys["serpapi"])
        self.assertNotIn(self.keys["serpapi"], json.dumps(pin))
        self.assertEqual(runtime.metrics()["monthly_used"], 177)
        self.assertEqual(runtime.metrics()["providers"]["serpapi"]["local_remaining"], 723)
        status = runtime.pool_admin.status()
        self.assertEqual(status["monthly_limit"], 900)
        self.assertIsNone(next(p for p in status["providers"] if p["provider"] == "exa")["provider_remaining"])

    def test_pin_rotation_disable_restart_body_conflict_and_tenant_isolation(self):
        runtime = self.create(); old = runtime.pin(self.guest, self.request, self.body)
        runtime.pool_admin.stage("serpapi", self.keys["serpapi"])
        self.assertEqual(runtime.pin(self.guest, self.request, self.body), old)
        runtime.pool_admin.usage_probe = lambda *args: serp_usage()
        new_key = "synthetic-rotated-" + "y" * 24
        identifier = runtime.pool_admin.stage("serpapi", new_key)["credential_id"]
        runtime.pool_admin.validate(identifier); runtime.pool_admin.activate(identifier)
        self.assertEqual(runtime.credentials(old)["serpapi-yandex"], self.keys["serpapi"])
        new = runtime.pin(self.guest, "1" * 32, self.body)
        self.assertEqual(runtime.credentials(new)["serpapi-yandex"], new_key)
        runtime.pool_admin.disable("serpapi")
        disabled = runtime.pin(self.guest, "2" * 32, self.body)
        self.assertIn("serpapi", disabled["disabled"])
        self.assertEqual(runtime.get_pin(self.guest, self.request, self.body), old)
        self.assert_code("request_conflict", lambda: runtime.pin(self.guest, self.request, self.body))
        restarted = PoolRuntime(self.beta, monthly_limit=900, grants=self.grants)
        self.assertEqual(restarted.get_pin(self.guest, self.request, self.body), old)
        self.assertEqual(restarted.credentials(old)["serpapi-yandex"], self.keys["serpapi"])
        self.assert_code("request_conflict", lambda: restarted.get_pin(self.guest, self.request, "f" * 64))
        self.assertIsNone(restarted.get_pin("3" * 64, self.request, self.body))

    def test_completed_marker_keeps_historical_digest_without_reseeding_writer_packet(self):
        runtime = self.create()
        marker = runtime.marker.read_bytes()
        state = runtime.pool_admin.vault.read()[0]
        self.packet["packet_id"] = "f" * 32
        self.packet["captured_at"] = time.time()
        self.beta.persist.reset_mock()
        restarted = self.create()
        self.assertEqual(restarted.marker.read_bytes(), marker)
        self.assertEqual(restarted.pool_admin.vault.read()[0], state)
        self.beta.persist.assert_not_called()

    def test_pending_vault_denies_new_and_orphan_pins_but_resolves_old_ids(self):
        runtime = self.create(); pin = runtime.pin(self.guest, self.request, self.body)
        with patch.object(runtime.pool_admin.vault, "persist", side_effect=RuntimeError("Synthetic uncertain commit")):
            with self.assertRaises(AccessError):
                runtime.pool_admin.disable("serpapi")
        self.assertEqual(runtime.credentials(pin)["serpapi-yandex"], self.keys["serpapi"])
        self.assertEqual(runtime.get_pin(self.guest, self.request, self.body), pin)
        for request in [self.request, "f" * 32]:
            self.assert_code("pool_recovery_required", lambda request=request: runtime.pin(self.guest, request, self.body))
        runtime.pool_admin.vault.recover()
        self.assertEqual(runtime.pin(self.guest, self.request, self.body), pin)

    def test_partial_bootstrap_crash_is_not_reseeded_after_restart(self):
        count = 0
        def fail_after_intent():
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("Synthetic durability fault")
        self.beta.persist.side_effect = fail_after_intent
        self.assert_code("pool_recovery_required", self.create)
        self.assertEqual(json.loads((self.beta.runtime / "pool-bootstrap.json").read_text())["status"], "in_progress")
        previous = count
        self.beta.persist.side_effect = None
        restarted = self.create()
        self.assertEqual(count, previous)
        self.assert_code("pool_recovery_required", lambda: restarted.pin(self.guest, self.request, self.body))

    def test_failed_complete_barrier_preserves_versions_and_stays_closed(self):
        def fail_complete():
            path = self.beta.runtime / "pool-bootstrap.json"
            if path.exists() and json.loads(path.read_text())["status"] == "complete":
                raise RuntimeError("Synthetic ambiguous final persistence")
        self.beta.persist.side_effect = fail_complete
        self.assert_code("pool_recovery_required", self.create)
        self.beta.persist.side_effect = None
        restarted = self.create()
        self.assertEqual(len(restarted.pool_admin.vault.read()[0]["records"]), 4)
        self.assert_code("pool_recovery_required", lambda: restarted.pin(self.guest, self.request, self.body))
        admin = restarted.pool_admin
        state, _ = admin.vault.read()
        identifier = state["active"]["serpapi"]
        admin.usage_probe = lambda *args: serp_usage()
        for operation in [lambda: admin.stage("serpapi", self.keys["serpapi"]),
                          lambda: admin.validate(identifier),
                          lambda: admin.activate(identifier),
                          lambda: admin.disable("serpapi")]:
            with self.subTest(operation=operation):
                self.assert_code("pool_recovery_required", operation)
        self.assertEqual(admin.vault.read()[0], state)

    def test_stale_packet_fails_before_first_vault_mutation(self):
        self.packet["captured_at"] -= 301
        self.assert_code("pool_unconfigured", self.create)
        self.assertFalse((self.beta.runtime / "pool-bootstrap.json").exists())
        self.assertFalse((self.root / "search-pool-vault/vault.json").exists())

    def test_real_time_expiry_at_completion_blocks_partial_bootstrap_and_restores_hooks(self):
        def now():
            admin = getattr(self.beta, "pool_admin", None)
            complete = admin and len(admin.vault.read()[0]["active"]) == 4
            return self.now + (301 if complete else 0)
        with patch("cloud.pool_runtime.time.time", side_effect=now):
            self.assert_code("pool_recovery_required", self.create)
        runtime = self.beta.pool_runtime
        self.assertEqual(len(runtime.pool_admin.vault.read()[0]["active"]), 4)
        self.assertIs(runtime.pool_admin.clock, time.time)
        self.assertEqual(json.loads(runtime.marker.read_text())["status"], "in_progress")
        self.assert_code("pool_recovery_required", lambda: runtime.pin(self.guest, self.request, self.body))

    def test_actual_nullable_tavily_observation_is_guarded_not_paid_limit_disabled(self):
        self.packet["proofs"]["tavily"]["usage"] = {
            "account": {"current_plan": "Researcher", "plan_usage": 809, "plan_limit": 1000,
                        "paygo_usage": 0, "paygo_limit": None}, "key": {"usage": 809, "limit": None}}
        runtime = self.create()
        observed = runtime.pool_admin.runtime_snapshot()["active"]["tavily"]["verification"]
        self.assertEqual((observed["provider_remaining"], observed["guard_remaining"]), (191, 91))

    def test_initialized_marker_or_pin_table_loss_does_not_recreate_authority(self):
        runtime = self.create(); runtime.pin(self.guest, self.request, self.body)
        with sqlite3.connect(self.beta.database) as db:
            db.execute("DROP TABLE pool_job_pins")
        self.assert_code("pool_storage_unavailable", lambda: PoolRuntime(self.beta))
        with sqlite3.connect(self.beta.database) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='pool_job_pins'").fetchone())

    def test_nonempty_unmarked_vault_refuses_reseed(self):
        runtime = PoolRuntime(self.beta)
        runtime.pool_admin.stage("serpapi", self.keys["serpapi"])
        self.assert_code("pool_recovery_required", self.create)
        self.assertEqual(len(runtime.pool_admin.vault.read()[0]["records"]), 1)

    def test_concurrent_pin_writers_select_one_immutable_binding(self):
        runtime = self.create()
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: runtime.pin(self.guest, self.request, self.body), range(5)))
        self.assertTrue(all(result == results[0] for result in results))
        with sqlite3.connect(self.beta.database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pool_job_pins").fetchone()[0], 1)

    def test_grant_lifetime_spend_and_serpapi_alias_metrics_survive_rotation(self):
        runtime = self.create()
        grant = self.grants["serper"]
        resource = "grant:serper:" + grant["account_ref"] + ":" + grant["grant_id"]
        budget = PoolBudget(self.root / "search-budget.sqlite3", provider="serper", resource=resource,
                            credential_fingerprint=grant["credential_fingerprint"], grant=grant, limit=900)
        budget.reserve("synthetic-grant-call"); budget.finish("synthetic-grant-call")
        with budget.connect() as db:
            db.execute("UPDATE calls SET month='2000-01' WHERE id='synthetic-grant-call'")
        for provider in ["serpapi", "serpapi-yandex"]:
            account = PoolBudget(self.root / "search-budget.sqlite3", provider=provider, resource="live:serpapi",
                                 credential_fingerprint="f" * 64, limit=900)
            account.reserve("synthetic-" + provider)
        metrics = runtime.metrics()
        self.assertEqual(metrics["providers"]["serpapi"]["local_used"], 2)
        self.assertEqual(metrics["providers"]["serper"]["grant_remaining"], 99)
        self.assertEqual(metrics["monthly_used"], 2)

    def test_automatic_dispatch_explicit_pin_keys_disabled_and_legacy_stop180(self):
        self.seed_calls(177); runtime = self.create()
        runtime.pool_admin.disable("serpapi")
        pin = runtime.pin(self.guest, self.request, self.body)
        with patch("plagiarism.search_router.select_search", return_value="synthetic-selected") as select:
            self.assertEqual(runtime.searcher(pin, "auto", 6), "synthetic-selected")
        self.assertEqual(select.call_args.kwargs["limit"], 900)
        self.assertEqual(select.call_args.kwargs["disabled"], ["serpapi-yandex"])
        self.assertEqual(select.call_args.kwargs["keys"]["tavily"], self.keys["tavily"])
        with patch("plagiarism.search_router.confirm_tavily", side_effect=AssertionError("No quota HTTP permitted")) as quota:
            with self.assertRaises(WebSearchError) as caught:
                runtime.searcher(pin, "tavily", 6)
            self.assertEqual(caught.exception.code, "search_budget_exhausted")
            quota.assert_not_called()
        with self.assertRaises(WebSearchError) as caught:
            runtime.searcher(pin, "serpapi-yandex", 1)
        self.assertEqual(caught.exception.code, "web_not_configured")

    def test_pin_schema_and_provider_id_substitution_fail_closed(self):
        runtime = self.create(); pin = runtime.pin(self.guest, self.request, self.body)
        forged = deepcopy(pin); forged["active"]["serpapi"] = pin["active"]["tavily"]
        self.assert_code("pool_storage_unavailable", lambda: runtime.credentials(forged))
        with sqlite3.connect(self.beta.database) as db:
            db.execute("UPDATE pool_job_pins SET pin_json=?", ('{"version":2}',))
        self.assert_code("pool_storage_unavailable", lambda: runtime.get_pin(self.guest, self.request, self.body))

    def test_pin_persist_failure_is_not_acknowledged_and_explicit_retry_reflushes(self):
        runtime = self.create()
        self.beta.persist.side_effect = RuntimeError("Synthetic persistence failure")
        self.assert_code("pool_storage_unavailable", lambda: runtime.pin(self.guest, self.request, self.body))
        retained = runtime.get_pin(self.guest, self.request, self.body)
        self.beta.persist.side_effect = None
        self.assertEqual(runtime.pin(self.guest, self.request, self.body), retained)
