from threading import Event
import time
import unittest

from plagiarism.jobs import Jobs


class JobsTests(unittest.TestCase):
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
