import json
from pathlib import Path
import tempfile
import unittest

from scripts.run_dev_metadata_pilot import QueueHTTPError
from scripts.run_zero_fallback_cached_queue import saved_job_or_submit


class CachedQueueRecoveryTests(unittest.TestCase):
    def test_one_new_post_follows_get_and_durable_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            calls = []
            def http(path, body=None):
                calls.append((path, body))
                if body is None:
                    raise QueueHTTPError(404)
                marker = json.loads((out / "D08-request.json").read_text())
                self.assertEqual(marker["request_id"], body["request_id"])
                self.assertIs(body["internet_consent"], True)
                return {"job": {"status": "queued"}}
            result = saved_job_or_submit(http, out, "D08", "a" * 32, "авторский текст", "b" * 64)
            self.assertEqual(result["status"], "queued")
            self.assertEqual([path for path, _ in calls], ["/api/jobs/" + "a" * 32, "/api/web-check-jobs"])

    def test_unknown_saved_submission_never_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            def missing(path, body=None):
                self.assertIsNone(body)
                raise QueueHTTPError(404)
            # Simulate a submission whose POST outcome is not known.
            from hashlib import sha256
            (out / "D10-request.json").write_text(json.dumps({"request_id": "c" * 32,
                "text_sha256": sha256(b"text").hexdigest(), "manifest_sha256": "d" * 64}))
            with self.assertRaisesRegex(RuntimeError, "no repeated POST"):
                saved_job_or_submit(missing, out, "D10", "c" * 32, "text", "d" * 64)

    def test_completed_job_is_recovered_by_get_only(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def http(path, body=None):
                calls.append((path, body))
                return {"job": {"status": "complete", "result": {"report": {"id": "saved"}}}}
            result = saved_job_or_submit(http, Path(directory), "D04", "e" * 32, "text", "f" * 64)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(calls, [("/api/jobs/" + "e" * 32, None)])


if __name__ == "__main__":
    unittest.main()
