"""Fail-closed budget and persisted request identity checks, without network."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.run_dev_query_probe import confirmed_quota, saved_request_identifier


class DevProbeSafetyTests(unittest.TestCase):
    def account(self):
        return {"current_plan": "Researcher", "plan_usage": 100, "plan_limit": 1000,
                "paygo_usage": 0, "paygo_limit": None}

    def test_free_headroom_uses_the_larger_account_or_local_counter(self):
        self.assertEqual(confirmed_quota(self.account(), 884, 16)["paygo_usage"], 0)
        for account, local, planned in ((self.account(), 884, 17),
                                       ({**self.account(), "plan_usage": 899}, 100, 2)):
            with self.subTest(local=local, planned=planned), self.assertRaises(RuntimeError):
                confirmed_quota(account, local, planned)

    def test_paid_or_unknown_usage_never_authorizes_search(self):
        for field in ("plan_usage", "paygo_usage"):
            for value in (float("nan"), float("inf"), -1, None, False, "0"):
                with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                    confirmed_quota({**self.account(), field: value}, 100, 2)
        with self.assertRaises(RuntimeError):
            confirmed_quota({**self.account(), "paygo_usage": 1}, 100, 2)

    def test_saved_submission_identifier_and_input_must_match_before_recovery(self):
        identifier, digest = "a" * 32, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "D01-request.json"
            saved = {"request_id": identifier, "text_sha256": digest, "planned_queries": 2}
            path.write_text(json.dumps(saved))
            self.assertEqual(saved_request_identifier(path, identifier, digest, 2), identifier)
            for changed in ({"request_id": "c" * 32}, {"text_sha256": "d" * 64}, {"planned_queries": 3}):
                path.write_text(json.dumps({**saved, **changed}))
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    saved_request_identifier(path, identifier, digest, 2)


if __name__ == "__main__":
    unittest.main()
