from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from plagiarism import retrieval, source_packs, tenants
from plagiarism.jobs import JobCoordinator, Jobs
from plagiarism.storage import Store


A, B, C = "a" * 64, "b" * 64, "c" * 64
LOAD_PUBLIC_PACK = source_packs.load_pack


class TenantTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data" / "tenants"
        packs = patch.object(tenants.source_packs, "load_pack", side_effect=lambda name: {
            "sources": [{"id": name, "title": "Public fixture", "text": "Public article.", "url": ""}]})
        packs.start()
        self.addCleanup(packs.stop)

    def registry(self, **options):
        registry = tenants.TenantRegistry(self.root, **options)
        self.addCleanup(registry.close)
        return registry

    def wait_idle(self, jobs):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if jobs.is_idle():
                return
            time.sleep(0.005)
        self.fail("Jobs did not release their queue capacity")

    def test_new_accounts_seed_only_30_bundled_public_sources_and_leave_legacy_untouched(self):
        self.root.parent.mkdir(parents=True)
        legacy = Store(self.root.parent / "istok.sqlite3")
        legacy.add_source("PRIVATE LEGACY", "Private source text.")
        legacy.save("report", {"text": "Private report text."})
        previous = sha256(legacy.path.read_bytes()).hexdigest()
        registry = self.registry()
        with patch.object(tenants.source_packs, "load_pack", side_effect=LOAD_PUBLIC_PACK):
            with registry.context(A) as account:
                sources = account.store.sources()
                self.assertEqual(len(sources), 30)
                self.assertTrue(all(source["id"].startswith("wiki-ru-") for source in sources))
                self.assertEqual(account.store.records("report"), [])
                self.assertEqual(account.store.path, self.root.resolve() / A / "istok.sqlite3")
        self.assertEqual(sha256(legacy.path.read_bytes()).hexdigest(), previous)

    def test_account_data_reviews_cache_and_identical_job_ids_are_isolated(self):
        registry = self.registry()
        with registry.context(A) as first, registry.context(B) as second:
            first.store.add_source("Private source", "A private text.", source_id="private")
            report = first.store.save("report", {"text": "A report."})
            first.store.review("same-example", "exact", "A private comment")
            self.assertIsNone(second.store.get("report", report["id"]))
            self.assertNotIn("private", {source["id"] for source in second.store.sources()})
            self.assertEqual(second.store.reviews(), [])
            self.assertNotEqual(first.embedding_cache_path, second.embedding_cache_path)
            with patch.object(retrieval.llm, "embed", return_value=[[1.0, 0.0]]) as embed:
                for context in (first, second):
                    cache = retrieval.EmbeddingCache(context.embedding_cache_path)
                    _, misses = cache.vectors(["same content"], "bge-m3", "digest")
                    self.assertEqual(misses, 1)
                self.assertEqual(embed.call_count, 2)
            identifier = "1" * 32
            first.jobs.submit_model(identifier, "/api/judge", {}, lambda progress: {"owner": "A"}, first.store)
            second.jobs.submit_model(identifier, "/api/judge", {}, lambda progress: {"owner": "B"}, second.store)
            self.wait_idle(first.jobs)
            self.wait_idle(second.jobs)
            self.assertEqual(first.jobs.get(identifier, first.store)["result"], {"owner": "A"})
            self.assertEqual(second.jobs.get(identifier, second.store)["result"], {"owner": "B"})
            other = first.jobs.submit("only-A", lambda progress: {})
            self.wait_idle(first.jobs)
            with self.assertRaises(KeyError):
                second.jobs.get(other["id"], second.store)

    def test_user_ids_cannot_select_arbitrary_directories(self):
        registry = self.registry()
        for identifier in ("../istok", "A" * 64, "a" * 63, "g" * 64, None, "/" + A):
            with self.subTest(identifier=identifier):
                with self.assertRaises(ValueError):
                    with registry.context(identifier):
                        self.fail("Invalid account acquired a context")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_symlink_cannot_redirect_account_to_another_store(self):
        registry = self.registry()
        outside = self.root.parent / "other"
        outside.mkdir()
        (self.root / A).symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ссылкой"):
            with registry.context(A):
                self.fail("Symlink account was opened")
        self.assertEqual(list(outside.iterdir()), [])

    def test_leases_and_background_work_prevent_eviction(self):
        registry = self.registry(max_contexts=1)
        started, release = Event(), Event()
        self.addCleanup(release.set)

        def operation(progress):
            started.set()
            release.wait(timeout=3)
            return {"done": True}

        with registry.context(A) as account:
            with self.assertRaises(ValueError):
                with registry.context(B):
                    self.fail("Leased account was evicted")
            job = account.jobs.submit("background", operation)
            self.assertTrue(started.wait(timeout=1))
        with self.assertRaises(ValueError):
            with registry.context(B):
                self.fail("Account with background work was evicted")
        release.set()
        self.wait_idle(account.jobs)
        self.assertEqual(account.jobs.get(job["id"])["status"], "complete")
        with registry.context(B) as next_account:
            self.assertTrue(account.jobs.closed)
            next_account.jobs.submit("shared-worker-still-open", lambda progress: {})
            self.wait_idle(next_account.jobs)

    def test_eviction_preserves_private_data_and_does_not_reseed_deleted_sources(self):
        registry = self.registry(max_contexts=1)
        with registry.context(A) as account:
            report = account.store.save("report", {"text": "Private saved document."})
            for source in account.store.sources():
                account.store.delete_source(source["id"])
        with registry.context(B):
            pass
        with registry.context(A) as reopened:
            self.assertIsNot(reopened, account)
            self.assertEqual(reopened.store.get("report", report["id"])["text"], "Private saved document.")
            self.assertEqual(reopened.store.sources(), [])
        registry.close()
        self.assertTrue(registry.coordinator.closed)
        with self.assertRaises(ValueError):
            with registry.context(B):
                self.fail("Closed registry accepted work")

    def test_per_account_three_and_global_six_limits_share_one_worker(self):
        registry = self.registry()
        started, release, second_started = Event(), Event(), Event()
        self.addCleanup(release.set)

        def block(progress):
            started.set()
            release.wait(timeout=3)
            return {}

        with registry.context(A) as first, registry.context(B) as second, registry.context(C) as third:
            first.jobs.submit_model("1" * 32, "/api/judge", {}, block, first.store)
            self.assertTrue(started.wait(timeout=1))
            for char in "23":
                first.jobs.submit_model(char * 32, "/api/judge", {}, lambda progress: {}, first.store)
            with self.assertRaises(ValueError):
                first.jobs.submit_model("4" * 32, "/api/judge", {}, lambda progress: {}, first.store)
            self.assertIsNone(first.store.get("model-request", "model-request:" + "4" * 32))
            for char in "123":
                second.jobs.submit_model(char * 32, "/api/judge", {}, lambda progress: second_started.set(), second.store)
            self.assertFalse(second_started.is_set())
            with self.assertRaisesRegex(ValueError, "Общая очередь"):
                third.jobs.submit_model("5" * 32, "/api/judge", {}, lambda progress: {}, third.store)
            self.assertIsNone(third.store.get("model-request", "model-request:" + "5" * 32))
            release.set()
            self.wait_idle(first.jobs)
            self.wait_idle(second.jobs)
            self.assertTrue(second_started.is_set())
            self.assertEqual(registry.coordinator.active, 0)

    def test_queued_cancellation_releases_global_slot_and_persists_failure(self):
        registry = self.registry(max_active=2)
        started, release, forbidden = Event(), Event(), Event()
        self.addCleanup(release.set)

        def block(progress):
            started.set()
            release.wait(timeout=3)

        with registry.context(A) as first, registry.context(B) as second:
            first.jobs.submit("running", block)
            self.assertTrue(started.wait(timeout=1))
            identifier = "2" * 32
            second.jobs.submit_model(identifier, "/api/candidate-judge", {}, lambda progress: forbidden.set(),
                                     second.store, candidate_attempt={"retrieval_id": "r", "candidate_id": "c"})
            self.assertEqual(second.jobs.cancel(identifier)["status"], "cancelled")
            self.assertEqual(registry.coordinator.active, 1)
            self.assertFalse(forbidden.is_set())
            self.assertEqual(second.store.get("model-job", identifier)["status"], "cancelled")
            self.assertEqual(second.store.candidate_judgments("r")[0]["code"], "job_cancelled")
            second.jobs.submit("capacity-reused", lambda progress: {})
            release.set()
            self.wait_idle(first.jobs)
            self.wait_idle(second.jobs)
            self.assertEqual(registry.coordinator.active, 0)

    def test_errors_and_submission_or_storage_failure_do_not_leak_permits(self):
        registry = self.registry(max_active=1)
        with registry.context(A) as account:
            with patch.object(account.jobs.executor, "submit", side_effect=RuntimeError("executor stopped")):
                with self.assertRaisesRegex(RuntimeError, "executor stopped"):
                    account.jobs.submit_model("1" * 32, "/api/judge", {}, lambda progress: {}, account.store)
                with self.assertRaisesRegex(RuntimeError, "executor stopped"):
                    account.jobs.submit("retrieval-submission-failed", lambda progress: {})
            self.assertEqual(registry.coordinator.active, 0)
            self.assertTrue(account.jobs.is_idle())
            self.assertEqual(account.store.get("model-job", "1" * 32)["code"], "job_submission_failed")
            with patch.object(account.store, "save", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    account.jobs.submit_model("2" * 32, "/api/judge", {}, lambda progress: {}, account.store)
            self.assertEqual(registry.coordinator.active, 0)

            def fail(progress):
                raise ValueError("Operation rejected")

            with self.assertLogs(level="ERROR"):
                job = account.jobs.submit("operation-error", fail)
                self.wait_idle(account.jobs)
            self.assertEqual(account.jobs.get(job["id"])["status"], "failed")
            self.assertEqual(registry.coordinator.active, 0)
            account.jobs.submit("after-error", lambda progress: {})
            self.wait_idle(account.jobs)
            save = account.store.save

            def fail_outcome(kind, value):
                if kind == "model-job":
                    raise OSError("outcome disk unavailable")
                return save(kind, value)

            with patch.object(account.store, "save", side_effect=fail_outcome), self.assertLogs(level="ERROR"):
                account.jobs.submit_model("3" * 32, "/api/judge", {}, lambda progress: {}, account.store)
                self.wait_idle(account.jobs)
            self.assertEqual(account.jobs.get("3" * 32)["code"], "job_storage_error")
            self.assertEqual(registry.coordinator.active, 0)

            def interrupted_worker(progress):
                raise SystemExit("worker interrupted")

            with self.assertLogs(level="ERROR"):
                job = account.jobs.submit("interrupted-worker", interrupted_worker)
                self.wait_idle(account.jobs)
            self.assertEqual(account.jobs.get(job["id"])["status"], "failed")
            self.assertEqual(registry.coordinator.active, 0)

    def test_central_shutdown_cancels_other_accounts_queued_work_without_running_it(self):
        registry = self.registry()
        started, release, forbidden = Event(), Event(), Event()
        self.addCleanup(release.set)

        def running(progress):
            started.set()
            release.wait(timeout=3)
            progress("After wait", 1, 1)

        with registry.context(A) as first, registry.context(B) as second:
            first_job = first.jobs.submit("running", running)
            self.assertTrue(started.wait(timeout=1))
            second_job = second.jobs.submit("queued", lambda progress: forbidden.set())
        closing = Thread(target=registry.close)
        closing.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not first.jobs.closed:
            time.sleep(0.005)
        self.assertTrue(first.jobs.closed)
        release.set()
        closing.join(timeout=2)
        self.assertFalse(closing.is_alive())
        self.assertFalse(forbidden.is_set())
        self.assertEqual(first.jobs.get(first_job["id"])["status"], "cancelled")
        self.assertEqual(second.jobs.get(second_job["id"])["status"], "cancelled")
        self.assertEqual(registry.coordinator.active, 0)

    def test_closing_one_jobs_context_does_not_shutdown_shared_executor(self):
        coordinator = JobCoordinator()
        self.addCleanup(coordinator.close)
        first, second = Jobs(coordinator=coordinator), Jobs(coordinator=coordinator)
        self.addCleanup(second.close)
        first.close()
        job = second.submit("still-usable", lambda progress: {"ok": True})
        self.wait_idle(second)
        self.assertTrue(second.get(job["id"])["result"]["ok"])
        executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(executor.shutdown)
        external = Jobs(executor=executor)
        external.close()
        self.assertEqual(executor.submit(lambda: 7).result(timeout=1), 7)


if __name__ == "__main__":
    unittest.main()
