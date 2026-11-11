from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from plagiarism.learning import LearningError, LearningQueue


A, B = "a" * 64, "b" * 64


def payload(**changes):
    return {"text": "Автор описал зелёные линии на четырёх листьях.",
            "source_text": "Наблюдатель отметил четыре листа с зелёными жилками.",
            "source_url": "https://example.org/owned-source",
            "source_provenance": {"publisher": "Автор", "license_name": "Permission under review"},
            "user_label": "paraphrase", "user_comment": "Похож набор подробностей.",
            "model_version": {"model": "qwen3.5:9b", "judge_version": "sentence-selection-v5-literal-availability"},
            **changes}


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.timestamp = datetime(2026, 11, 11, 12, tzinfo=timezone.utc).timestamp()
        self.queue = LearningQueue(Path(self.temporary.name) / "private" / "feedback.sqlite3", now=lambda: self.timestamp)

    def submit(self, **changes):
        return self.queue.submit(A, "report-1", "pair-1", payload(**changes), "learning-v1", consent=True)

    def approve(self, identifier, **changes):
        return self.queue.review(identifier, "approve", "Выдержки, метка и разрешение проверены человеком.",
                                 human_label="same_topic", rights_confirmed=True, **changes)

    def test_submission_is_pending_immutable_and_not_automatically_training_data(self):
        original = payload()
        accepted = self.queue.submit(A, "report-1", "pair-1", original, "learning-v1")
        self.assertEqual(accepted["state"], "pending")
        self.assertEqual(accepted["rights_status"], "not_verified")
        expected_hash = sha256(json.dumps(original, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(accepted["payload_sha256"], expected_hash)
        original["text"] = "Changed after submission"
        original["model_version"]["model"] = "changed-model"
        saved = self.queue.get(A, accepted["id"])
        self.assertNotEqual(saved["payload"]["text"], original["text"])
        self.assertEqual(saved["payload"]["model_version"]["model"], "qwen3.5:9b")
        self.assertEqual(self.queue.export_approved(), [])

    def test_consent_must_be_boolean_true_and_version_is_required(self):
        for consent in (False, None, 1, "true", [], {}):
            with self.subTest(consent=consent):
                with self.assertRaises(LearningError) as raised:
                    self.queue.submit(A, "report-1", "pair-1", payload(), "learning-v1", consent=consent)
                self.assertEqual(raised.exception.code, "consent_required")
        for version in ("", None, "v" * 81):
            with self.subTest(version=version):
                with self.assertRaises(LearningError):
                    self.queue.submit(A, "report-1", "pair-1", payload(), version)
        with closing(sqlite3.connect(self.queue.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM learning_feedback").fetchone()[0], 0)

    def test_poisoned_labels_full_uploads_and_unbounded_metadata_are_rejected(self):
        invalid = [payload(user_label="plagiarism"), payload(user_label=["exact"]),
                   payload(text="x" * 12000), payload(attachments=["private.pdf"]),
                   payload(source_provenance={"raw_document": "x" * 4001}),
                   payload(source_url="javascript:alert(1)"),
                   payload(source_url="https://username:password@example.org/file"),
                   payload(model_version={"model": "qwen3.5:9b", "judge_version": "v5", "model_digest": "unknown"})]
        for item in invalid:
            with self.subTest(keys=list(item)):
                with self.assertRaises(LearningError):
                    self.queue.submit(A, "report-1", "pair-1", item, "learning-v1")

    def test_exact_dedupe_is_owner_scoped_and_does_not_spend_daily_capacity(self):
        original = payload()
        first = self.queue.submit(A, "report-1", "pair-1", original, "learning-v1")
        reordered = dict(reversed(list(original.items())))
        for _ in range(12):
            duplicate = self.queue.submit(A, "report-1", "pair-1", reordered, "learning-v1")
            self.assertEqual(duplicate["id"], first["id"])
        for number in range(9):
            self.queue.submit(A, "report-1", f"other-{number}", payload(), "learning-v1")
        with self.assertRaises(LearningError) as raised:
            self.queue.submit(A, "report-1", "eleventh", payload(), "learning-v1")
        self.assertEqual(raised.exception.code, "feedback_daily_limit")
        other = self.queue.submit(B, "report-1", "pair-1", original, "learning-v1")
        self.assertNotEqual(first["id"], other["id"])
        self.timestamp += 24 * 60 * 60
        self.queue.submit(A, "report-1", "next-day", payload(), "learning-v1")

    def test_daily_cap_is_atomic_under_concurrent_submissions(self):
        def submit(number):
            try:
                self.queue.submit(A, "report-1", f"pair-{number}", payload(), "learning-v1")
                return "accepted"
            except LearningError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(submit, range(16)))
        self.assertEqual(results.count("accepted"), 10)
        self.assertEqual(results.count("feedback_daily_limit"), 6)

    def test_only_offline_human_review_with_explicit_rights_can_approve(self):
        accepted = self.submit()
        for label, rights in ((None, True), ("invented", True), ("exact", False), ("exact", 1), ("exact", "true")):
            with self.subTest(label=label, rights=rights):
                with self.assertRaises(LearningError):
                    self.queue.review(accepted["id"], "approve", "Checked", human_label=label, rights_confirmed=rights)
        self.assertEqual(self.queue.export_approved(), [])
        self.approve(accepted["id"])
        exported = self.queue.export_approved()
        self.assertEqual(len(exported), 1)
        row = exported[0]
        self.assertEqual(row["user_label"], "paraphrase")
        self.assertEqual(row["expected"], "same_topic")
        self.assertEqual(row["snapshot_sha256"], accepted["payload_sha256"])
        self.assertIs(row["synthetic"], False)
        self.assertIs(row["consented"], True)
        self.assertIs(row["rights_confirmed"], True)
        self.assertIs(row["rights_verified"], False)
        self.assertIs(row["human_reviewed"], True)
        self.assertEqual(row["split"], "unassigned")
        self.assertNotIn("account_id", row)

    def test_rejected_and_pending_examples_are_never_exported(self):
        rejected = self.submit()
        self.queue.review(rejected["id"], "reject", "Недостаточно оснований для метки.")
        self.queue.submit(A, "report-2", "pair-1", payload(), "learning-v1")
        self.assertEqual(self.queue.export_approved(), [])

    def test_foreign_owner_cannot_read_or_revoke_and_revocation_removes_raw_payload(self):
        secret = "WITHDRAW_THIS_UNIQUE_RAW_PAYLOAD_88194"
        accepted = self.submit(text=secret)
        with self.assertRaises(KeyError):
            self.queue.get(B, accepted["id"])
        with self.assertRaises(KeyError):
            self.queue.revoke(B, accepted["id"])
        self.approve(accepted["id"])
        self.assertEqual(len(self.queue.export_approved()), 1)
        revoked = self.queue.revoke(A, accepted["id"])
        self.assertEqual(revoked["state"], "revoked")
        self.assertFalse(revoked["consented"])
        self.assertFalse(revoked["payload_available"])
        self.assertEqual(revoked["payload_sha256"], accepted["payload_sha256"])
        self.assertIsNone(self.queue.get(A, accepted["id"])["payload"])
        self.assertEqual(self.queue.export_approved(), [])
        self.assertNotIn(secret.encode(), self.queue.path.read_bytes())
        with self.assertRaises(LearningError):
            self.approve(accepted["id"])
        duplicate = self.submit(text=secret)
        self.assertEqual(duplicate["id"], accepted["id"])
        self.assertEqual(duplicate["state"], "revoked")
        self.assertFalse(duplicate["payload_available"])

    def test_revoke_does_not_restore_daily_submission_capacity(self):
        for number in range(10):
            item = self.queue.submit(A, "report-1", f"pair-{number}", payload(), "learning-v1")
            self.queue.revoke(A, item["id"])
        with self.assertRaises(LearningError) as raised:
            self.queue.submit(A, "report-1", "new-pair", payload(), "learning-v1")
        self.assertEqual(raised.exception.code, "feedback_daily_limit")

    def test_changed_snapshot_is_rejected_instead_of_exporting_poisoned_training_data(self):
        accepted = self.submit()
        self.approve(accepted["id"])
        changed = json.dumps(payload(text="Externally modified source text."), ensure_ascii=False)
        with closing(sqlite3.connect(self.queue.path)) as connection, connection:
            connection.execute("UPDATE learning_feedback SET payload=? WHERE id=?", (changed, accepted["id"]))
        with self.assertRaises(LearningError) as raised:
            self.queue.export_approved()
        self.assertEqual(raised.exception.code, "feedback_snapshot_changed")

    def test_database_is_private_and_symlinks_are_rejected(self):
        self.assertEqual(self.queue.path.stat().st_mode & 0o777, 0o600)
        self.queue.path.chmod(0o644)
        LearningQueue(self.queue.path)
        self.assertEqual(self.queue.path.stat().st_mode & 0o777, 0o600)
        link = self.queue.path.parent / "link.sqlite3"
        link.symlink_to(self.queue.path)
        with self.assertRaises(ValueError):
            LearningQueue(link)


if __name__ == "__main__":
    unittest.main()
