from threading import Event
from pathlib import Path
import tempfile
import time
import unittest

from plagiarism.jobs import Jobs
from plagiarism.llm import OllamaError
from plagiarism.storage import Store


class JobsTests(unittest.TestCase):
    def wait_terminal(self, jobs, identifier):
        for _ in range(200):
            result = jobs.get(identifier)
            if result["status"] in {"complete", "failed", "cancelled"}:
                return result
            time.sleep(0.01)
        self.fail("Job did not finish")

    def test_model_request_is_idempotent_and_recoverable_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "jobs.sqlite3")
            jobs = Jobs()
            calls = []
            identifier = "a" * 32

            def operation(progress):
                calls.append(1)
                return {"rewrite": {"text": "Авторский текст со ссылкой."}}

            try:
                jobs.submit_model(identifier, "/api/rewrite", {"mode": "quote", "report_id": "report"}, operation, store)
                completed = self.wait_terminal(jobs, identifier)
                repeated = jobs.submit_model(identifier, "/api/rewrite", {"report_id": "report", "mode": "quote"}, operation, store)
                self.assertEqual(repeated["result"], completed["result"])
                with self.assertRaises(ValueError):
                    jobs.submit_model(identifier, "/api/rewrite", {"mode": "paraphrase"}, operation, store)
            finally:
                jobs.close()
            restarted = Jobs()
            try:
                self.assertEqual(restarted.get(identifier, store)["result"], completed["result"])
                repeated = restarted.submit_model(identifier, "/api/rewrite", {"mode": "quote", "report_id": "report"}, operation, store)
                self.assertEqual(repeated["status"], "complete")
                self.assertEqual(calls, [1])
                self.assertNotIn("request_fingerprint", repeated)
            finally:
                restarted.close()

    def test_failed_model_job_preserves_code_and_evidence_details_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "jobs.sqlite3")
            jobs = Jobs()

            def operation(progress):
                raise OllamaError("Нужна ручная проверка", code="unconfirmed_exact", details={"evidence": "😀 Точная выдержка"})

            try:
                job = jobs.submit_model("b" * 32, "/api/judge", {}, operation, store)
                result = self.wait_terminal(jobs, job["id"])
                self.assertEqual(result["code"], "unconfirmed_exact")
            finally:
                jobs.close()
            saved = store.get("model-job", job["id"])
            self.assertEqual(saved["details"], {"evidence": "😀 Точная выдержка"})
            self.assertEqual(saved["status"], "failed")

    def test_restart_never_repeats_reserved_unfinished_model_compute(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "jobs.sqlite3")
            identifier = "c" * 32
            store.save("model-request", {"id": "model-request:" + identifier, "path": "/api/judge",
                                          "request_fingerprint": "recorded-fingerprint"})
            jobs = Jobs()
            try:
                recovered = jobs.get(identifier, store)
                self.assertEqual(recovered["status"], "failed")
                self.assertEqual(recovered["code"], "job_interrupted")
                self.assertEqual(store.get("model-job", identifier)["code"], "job_interrupted")
            finally:
                jobs.close()

    def test_interrupted_candidate_attempt_replaces_older_positive_with_failed_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "jobs.sqlite3")
            store.save("candidate-judgment", {"retrieval_id": "retrieval", "candidate_id": "candidate",
                                              "judgment": {"relation": "exact"}})
            identifier = "d" * 32
            attempt = {"retrieval_id": "retrieval", "candidate_id": "candidate", "model": "qwen3:4b"}
            store.save("model-request", {"id": "model-request:" + identifier, "path": "/api/candidate-judge",
                                          "request_fingerprint": "fingerprint", "candidate_attempt": attempt})
            jobs = Jobs()
            try:
                job = jobs.get(identifier, store)
                self.assertEqual(job["code"], "job_interrupted")
                latest = store.candidate_judgments("retrieval")[0]
                self.assertNotIn("judgment", latest)
                self.assertEqual(latest["code"], "job_interrupted")
                self.assertEqual(latest["job_id"], identifier)
            finally:
                jobs.close()

    def test_model_jobs_share_three_active_limit_and_one_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "jobs.sqlite3")
            jobs = Jobs()
            started, release = Event(), Event()
            running = []

            def operation(progress):
                running.append(1)
                started.set()
                release.wait(timeout=3)
                return {}

            try:
                jobs.submit_model("1" * 32, "/api/judge", {}, operation, store)
                self.assertTrue(started.wait(timeout=2))
                jobs.submit_model("2" * 32, "/api/judge", {}, operation, store)
                jobs.submit_model("3" * 32, "/api/judge", {}, operation, store)
                with self.assertRaises(ValueError):
                    jobs.submit_model("4" * 32, "/api/judge", {}, operation, store)
                self.assertEqual(len(running), 1)
                self.assertIsNone(store.get("model-request", "model-request:" + "4" * 32))
            finally:
                release.set()
                jobs.close()

    def test_cancellation_stops_before_next_operation_and_keeps_progress(self):
        started, release = Event(), Event()
        jobs = Jobs()

        def operation(progress):
            progress("Первый шаг", 1, 2)
            started.set()
            release.wait(timeout=3)
            progress("Второй шаг", 2, 2)
            self.fail("Cancelled work must not continue")

        try:
            job = jobs.submit("cancel", operation)
            self.assertTrue(started.wait(timeout=2))
            self.assertEqual(jobs.cancel(job["id"])["status"], "cancelling")
            release.set()
            for _ in range(100):
                result = jobs.get(job["id"])
                if result["status"] == "cancelled":
                    break
                time.sleep(0.01)
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(result["progress"], {"current": 1, "total": 2})
        finally:
            release.set()
            jobs.close()

    def test_repeated_submission_reuses_active_job_and_saves_progress(self):
        started, release = Event(), Event()
        jobs = Jobs()

        def operation(progress):
            progress("Обработка", 1, 2)
            started.set()
            release.wait(timeout=3)
            return {"id": "result"}

        try:
            first = jobs.submit(("report", "hybrid"), operation)
            self.assertTrue(started.wait(timeout=2))
            repeated = jobs.submit(("report", "hybrid"), operation)
            self.assertEqual(first["id"], repeated["id"])
            self.assertEqual(jobs.get(first["id"])["progress"], {"current": 1, "total": 2})
            release.set()
            for _ in range(100):
                result = jobs.get(first["id"])
                if result["status"] == "complete":
                    break
                time.sleep(0.01)
            self.assertEqual(result["result"], {"id": "result"})
        finally:
            release.set()
            jobs.close()


if __name__ == "__main__":
    unittest.main()
