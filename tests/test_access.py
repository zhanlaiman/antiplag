from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from pathlib import Path
import stat
import tempfile
from threading import Barrier
import unittest
from uuid import uuid4

from plagiarism.access import AccessError, AccessLedger, canonical_email


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "access.sqlite3"
        self.moment = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
        self.ledger = AccessLedger(self.path, now=lambda: self.moment)
        self.account = self.identity("first", "first@example.org")
        self.device = self.ledger.create_device()

    def identity(self, subject, email):
        return self.ledger.ensure_identity("https://identity.example", subject, email, True)["id"]

    def check(self, text="Учебный текст", account=None, device=None, request_id=None, fingerprint=None):
        return self.ledger.reserve_check(account or self.account, device or self.device, request_id or uuid4().hex,
                                         text, fingerprint or sha256(text.encode()).hexdigest())

    def report(self):
        check = self.check("Клетка.")
        self.ledger.mark_started(self.account, check["request_id"])
        self.ledger.commit(self.account, check["request_id"])
        self.ledger.bind_report(self.account, check["request_id"], check["request_id"])
        return check["request_id"]

    def operation(self, report_id, kind="judge", request_id=None, fingerprint=None):
        return self.ledger.reserve_operation(self.account, report_id, request_id or uuid4().hex,
                                              kind, fingerprint or "a" * 64)

    def assert_code(self, code, operation):
        with self.assertRaises(AccessError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_verified_provider_principals_stay_separate_despite_same_email(self):
        self.assert_code("email_not_verified", lambda: self.ledger.ensure_identity("issuer", "subject", "x@example.org", False))
        a = self.ledger.ensure_identity("one", "same", "x@gmail.com", True)
        b = self.ledger.ensure_identity("two", "same", "x@gmail.com", True)
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(a["plan"], "free")
        self.assertEqual(a, self.ledger.ensure_identity("one", "same", "changed@example.org", True))
        self.assertNotEqual(self.ledger.ensure_identity("ab", "c", "x@example.org", True)["id"],
                            self.ledger.ensure_identity("a", "bc", "x@example.org", True)["id"])

    def test_sessions_are_opaque_hashed_expiring_and_revocable(self):
        token = self.ledger.create_session(self.account, ttl_seconds=10)
        self.assertEqual(self.ledger.session(token)["id"], self.account)
        with self.ledger.connect() as connection:
            stored = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
        self.assertEqual(stored, sha256(token.encode()).hexdigest())
        self.assertNotEqual(stored, token)
        self.moment += timedelta(seconds=10)
        self.assertIsNone(self.ledger.session(token))
        token = self.ledger.create_session(self.account)
        self.assertTrue(self.ledger.revoke_session(token))
        self.assertFalse(self.ledger.revoke_session(token))
        self.assertIsNone(self.ledger.session(token))
        self.assertIsNone(self.ledger.session("not a real token"))

    def test_only_issued_device_cookie_is_accepted(self):
        self.assertTrue(self.ledger.valid_device(self.device))
        self.assertFalse(self.ledger.valid_device("x" * 43))
        self.assert_code("invalid_device", lambda: self.check(device="x" * 43))

    def test_original_unicode_codepoints_and_spaces_count_at_exact_limit(self):
        text = "😀е\u0301 й\n" + "я" * 5994
        self.assertEqual(len(text), 6000)
        self.assertEqual(self.check(text)["characters"], 6000)
        usage = self.ledger.usage(self.account, self.device)
        self.assertEqual(usage["remaining"]["characters"], 0)
        self.assert_code("daily_quota_exceeded", lambda: self.check("я"))
        self.assertEqual(self.ledger.usage(self.account)["used"]["checks"], 1)

    def test_three_new_analyses_per_day_and_idempotency_conflicts(self):
        identifier = uuid4().hex
        first = self.check("Один", request_id=identifier, fingerprint="b" * 64)
        repeated = self.check("Один", request_id=identifier, fingerprint="b" * 64)
        self.assertFalse(first["reused"])
        self.assertTrue(repeated["reused"])
        self.assert_code("request_conflict", lambda: self.check("Другой", request_id=identifier, fingerprint="c" * 64))
        self.check("Два")
        self.check("Три")
        self.assert_code("daily_quota_exceeded", lambda: self.check("Четыре"))
        self.assertEqual(self.ledger.usage(self.account)["used"]["checks"], 3)

    def test_atomic_character_reservation_across_connections(self):
        self.check("я" * 5999)
        barrier = Barrier(2)

        def reserve(_):
            ledger = AccessLedger(self.path, now=lambda: self.moment)
            barrier.wait(timeout=3)
            try:
                ledger.reserve_check(self.account, self.device, uuid4().hex, "я", "a" * 64)
                return "accepted"
            except AccessError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(reserve, range(2)))
        self.assertCountEqual(outcomes, ["accepted", "daily_quota_exceeded"])
        self.assertEqual(self.ledger.usage(self.account)["used"]["characters"], 6000)

    def test_atomic_same_request_does_not_charge_twice(self):
        barrier = Barrier(2)
        identifier = uuid4().hex

        def reserve(_):
            barrier.wait(timeout=3)
            return self.check("я" * 4000, request_id=identifier)["reused"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(reserve, range(2)))
        self.assertCountEqual(outcomes, [False, True])
        self.assertEqual(self.ledger.usage(self.account)["used"]["characters"], 4000)

    def test_model_operation_limit_is_atomic_for_concurrent_requests(self):
        report = self.report()
        for _ in range(59):
            self.operation(report)
        barrier = Barrier(2)

        def reserve(_):
            barrier.wait(timeout=3)
            try:
                self.operation(report)
                return "accepted"
            except AccessError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(reserve, range(2)))
        self.assertCountEqual(outcomes, ["accepted", "daily_quota_exceeded"])
        self.assertEqual(self.ledger.usage(self.account)["used"]["model_ops"], 60)

    def test_gmail_alias_quota_is_shared_without_merging_accounts(self):
        first = self.identity("gmail-one", "John.Doe+first@googlemail.com")
        second = self.identity("gmail-two", "johndoe+second@gmail.com")
        self.assertNotEqual(first, second)
        self.check("я" * 6000, account=first)
        self.assert_code("daily_quota_exceeded", lambda: self.check("я", account=second, device=self.ledger.create_device()))
        self.assertEqual(self.ledger.session(self.ledger.create_session(first))["id"], first)
        self.assertEqual(self.ledger.session(self.ledger.create_session(second))["id"], second)
        self.assertNotEqual(canonical_email("john.doe+tag@example.org"), canonical_email("johndoe@example.org"))

    def test_shared_device_has_shared_quota_for_different_verified_accounts(self):
        self.check("я" * 6000)
        second = self.identity("second", "second@example.org")
        self.assert_code("daily_quota_exceeded", lambda: self.check("я", account=second))
        self.assertEqual(self.ledger.usage(second, self.device)["used"]["characters"], 6000)

    def test_quota_resets_at_utc_midnight_not_client_timezone(self):
        self.moment = datetime(2026, 11, 11, 23, 59, 59, tzinfo=timezone.utc)
        first = self.check("я" * 6000)
        self.assertEqual(self.ledger.usage(self.account)["reset_at"], "2026-11-12T00:00:00+00:00")
        self.moment += timedelta(seconds=1)
        self.assertEqual(self.ledger.usage(self.account)["remaining"]["characters"], 6000)
        self.check("я" * 6000)
        repeated = self.check("я" * 6000, request_id=first["request_id"])
        self.assertEqual(repeated["day"], "2026-11-11")
        self.assertTrue(repeated["reused"])

    def test_explicit_release_only_before_work_started_and_never_retries_released_id(self):
        first = self.check("я" * 6000)
        self.ledger.release(self.account, first["request_id"])
        self.assertEqual(self.ledger.usage(self.account)["used"]["characters"], 0)
        self.assertEqual(self.check("я" * 6000, request_id=first["request_id"])["state"], "released")
        second = self.check("я" * 6000)
        self.ledger.mark_started(self.account, second["request_id"])
        self.assert_code("reservation_state_conflict", lambda: self.ledger.release(self.account, second["request_id"]))
        reopened = AccessLedger(self.path, now=lambda: self.moment)
        self.assertEqual(reopened.usage(self.account)["remaining"]["characters"], 0)
        self.assertFalse(self.ledger.commit(self.account, second["request_id"])["reused"])
        self.assertTrue(self.ledger.commit(self.account, second["request_id"])["reused"])

    def test_model_operations_are_limited_and_repeated_id_is_free(self):
        report = self.report()
        first = self.operation(report)
        self.assertTrue(self.operation(report, request_id=first["request_id"])["reused"])
        for _ in range(59):
            self.operation(report)
        self.assertEqual(self.ledger.usage(self.account)["used"]["model_ops"], 60)
        self.assert_code("daily_quota_exceeded", lambda: self.operation(report))
        self.operation(report, kind="lexical_retrieval")
        self.assert_code("report_operation_limit", lambda: self.operation(report, kind="lexical_retrieval"))

    def test_rewrites_and_semantic_retrieval_have_additional_limits(self):
        report = self.report()
        for _ in range(3):
            self.operation(report, kind="rewrite")
        self.assert_code("daily_quota_exceeded", lambda: self.operation(report, kind="rewrite"))
        self.operation(report, kind="semantic_retrieval")
        self.moment += timedelta(days=1)
        self.assert_code("report_operation_limit", lambda: self.operation(report, kind="semantic_retrieval"))
        foreign = self.identity("other", "other@example.org")
        self.assert_code("check_required", lambda: self.ledger.reserve_operation(foreign, report, uuid4().hex, "judge", "a" * 64))

    def test_old_report_cannot_bypass_current_device_model_or_rewrite_limit(self):
        report = self.report()
        for kind, limit in (("judge", 60), ("rewrite", 3)):
            with self.subTest(kind=kind):
                device = self.ledger.create_device()
                other = self.identity("other-" + kind, kind + "@example.org")
                check = self.check(account=other, device=device)
                self.ledger.mark_started(other, check["request_id"])
                self.ledger.commit(other, check["request_id"])
                self.ledger.bind_report(other, check["request_id"], check["request_id"])
                for _ in range(limit):
                    self.ledger.reserve_operation(other, check["request_id"], uuid4().hex, kind,
                                                  "a" * 64, device_id=device)
                remaining_key = "rewrites" if kind == "rewrite" else "model_ops"
                self.assertEqual(self.ledger.usage(self.account, device)["remaining"][remaining_key], 0)
                self.assert_code("daily_quota_exceeded", lambda: self.ledger.reserve_operation(
                    self.account, report, uuid4().hex, kind, "a" * 64, device_id=device))
        self.assertEqual(self.ledger.usage(self.account)["used"]["model_ops"], 0)

    def test_operation_retry_and_release_keep_original_scopes_after_device_change(self):
        report = self.report()
        observer = self.identity("observer", "observer@example.org")
        current, later = self.ledger.create_device(), self.ledger.create_device()
        for first_device in (None, current):
            with self.subTest(legacy_scopes=first_device is None):
                identifier = uuid4().hex
                first = self.ledger.reserve_operation(self.account, report, identifier, "judge", "a" * 64,
                                                       device_id=first_device)
                repeated = self.ledger.reserve_operation(self.account, report, identifier, "judge", "a" * 64,
                                                          device_id=later)
                self.assertFalse(first["reused"])
                self.assertTrue(repeated["reused"])
                charged_device = self.device if first_device is None else current
                self.assertEqual(self.ledger.usage(observer, charged_device)["used"]["model_ops"], 1)
                self.assertEqual(self.ledger.usage(observer, later)["used"]["model_ops"], 0)
                self.ledger.release(self.account, identifier)
                self.assertEqual(self.ledger.usage(observer, charged_device)["used"]["model_ops"], 0)
                self.assertEqual(self.ledger.usage(observer, later)["used"]["model_ops"], 0)
                self.assertEqual(self.ledger.reserve_operation(self.account, report, identifier, "judge", "a" * 64,
                    device_id=later)["state"], "released")

    def test_semantic_cost_is_internal_conservative_and_preserved_for_retry_and_release(self):
        report = self.report()
        self.assert_code("daily_quota_exceeded", lambda: self.ledger.reserve_operation(
            self.account, report, uuid4().hex, "semantic_retrieval", "a" * 64,
            device_id=self.device, model_ops_cost=64))
        self.assertEqual(self.ledger.usage(self.account)["used"]["model_ops"], 0)
        for kind, cost in (("judge", 0), ("judge", 2), ("rewrite", 2), ("lexical_retrieval", 0),
                           ("lexical_retrieval", 2), ("semantic_retrieval", 0), ("semantic_retrieval", True)):
            with self.subTest(kind=kind, cost=cost):
                self.assert_code("invalid_operation_cost", lambda: self.ledger.reserve_operation(
                    self.account, report, uuid4().hex, kind, "a" * 64, model_ops_cost=cost))
        identifier = uuid4().hex
        self.ledger.reserve_operation(self.account, report, identifier, "semantic_retrieval", "a" * 64,
                                      device_id=self.device, model_ops_cost=3)
        self.assertEqual(self.ledger.usage(self.account, self.device)["used"]["model_ops"], 3)
        repeated = self.ledger.reserve_operation(self.account, report, identifier, "semantic_retrieval", "a" * 64,
                                                 device_id=self.ledger.create_device(), model_ops_cost=1)
        self.assertTrue(repeated["reused"])
        self.assertEqual(self.ledger.usage(self.account, self.device)["used"]["model_ops"], 3)
        self.ledger.release(self.account, identifier)
        self.assertEqual(self.ledger.usage(self.account, self.device)["used"]["model_ops"], 0)
        self.assertEqual(self.ledger.reserve_operation(self.account, report, identifier, "semantic_retrieval", "a" * 64,
            device_id=self.device, model_ops_cost=2)["state"], "released")
        self.assertEqual(self.ledger.usage(self.account, self.device)["used"]["model_ops"], 0)

    def test_challenge_has_atomic_five_attempt_limit_and_single_use(self):
        challenge = self.ledger.create_challenge("first@example.org", self.device)
        with ThreadPoolExecutor(max_workers=8) as executor:
            allowed = list(executor.map(lambda _: self.ledger.check_challenge(challenge, "first@example.org", self.device), range(8)))
        self.assertEqual(sum(allowed), 5)
        with ThreadPoolExecutor(max_workers=2) as executor:
            consumed = list(executor.map(lambda _: self.ledger.consume_challenge(challenge), range(2)))
        self.assertEqual(sum(consumed), 1)
        self.assertFalse(self.ledger.check_challenge(challenge, "first@example.org", self.device))

    def test_challenge_requires_email_device_and_expiry_without_storing_otp(self):
        challenge = self.ledger.create_challenge("first@example.org", self.device, ttl=10)
        self.assertFalse(self.ledger.consume_challenge(challenge))
        self.assertFalse(self.ledger.check_challenge(challenge, "other@example.org", self.device))
        self.assertFalse(self.ledger.check_challenge(challenge, "first@example.org", self.ledger.create_device()))
        self.moment += timedelta(seconds=10)
        self.assertFalse(self.ledger.check_challenge(challenge, "first@example.org", self.device))
        self.assertFalse(self.ledger.consume_challenge(challenge))

    def test_rate_limit_is_persistent_transient_hmac_and_not_identity(self):
        network = "203.0.113.45"
        self.assertEqual(self.ledger.hit_rate("signup_network", network, 2, 10), 0)
        self.assertEqual(self.ledger.hit_rate("signup_network", network, 2, 10), 0)
        reopened = AccessLedger(self.path, now=lambda: self.moment)
        self.assertEqual(reopened.hit_rate("signup_network", network, 2, 10), 10)
        self.moment += timedelta(seconds=9)
        self.assertEqual(reopened.hit_rate("signup_network", network, 2, 10), 1)
        self.moment += timedelta(seconds=1)
        self.assertEqual(reopened.hit_rate("signup_network", network, 2, 10), 0)
        with self.ledger.connect() as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(network, dump)
        self.assertEqual(self.ledger.session(self.ledger.create_session(self.account))["id"], self.account)

    def test_secret_and_database_permissions_and_no_plaintext_tokens_email_or_text(self):
        session = self.ledger.create_session(self.account)
        text = "Уникальный личный текст, который нельзя сохранять в ledger."
        self.check(text)
        self.ledger.create_challenge("first@example.org", self.device)
        with self.ledger.connect() as connection:
            dump = "\n".join(connection.iterdump())
        for private in (session, self.device, text, "first@example.org"):
            self.assertNotIn(private, dump)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.ledger.secret_path).st_mode), 0o600)
        self.assertEqual(self.ledger.hash_signal("value", "one"), AccessLedger(self.path).hash_signal("value", "one"))
        self.assertNotEqual(self.ledger.hash_signal("value", "one"), self.ledger.hash_signal("value", "two"))

    def test_csrf_is_bound_to_both_device_and_session_and_rejects_invalid_unicode(self):
        token = self.ledger.create_session(self.account)
        csrf = self.ledger.csrf(self.device, token)
        self.assertTrue(self.ledger.verify_csrf(self.device, csrf, token))
        self.assertFalse(self.ledger.verify_csrf(self.device, csrf, "other"))
        self.assertFalse(self.ledger.verify_csrf(self.ledger.create_device(), csrf, token))
        self.assertFalse(self.ledger.verify_csrf(self.device, "😀", token))
        self.assertFalse(self.ledger.verify_csrf(None, csrf, token))

    def test_missing_secret_never_silently_resets_existing_quota_identity(self):
        self.check("я" * 6000)
        self.ledger.secret_path.unlink()
        self.assert_code("access_key_missing", lambda: AccessLedger(self.path))
        self.assertFalse(self.ledger.secret_path.exists())


if __name__ == "__main__":
    unittest.main()
