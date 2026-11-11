"""Exercise route coordination without opening sockets or contacting a model."""

import json
import os
from pathlib import Path
import tempfile
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import server
from plagiarism import cloud_transport
from plagiarism.jobs import Jobs
from plagiarism.storage import Store
from tests.test_cloud_contract import manifest


class ModelRouteTests(unittest.TestCase):
    def test_background_evaluation_reports_pair_progress_and_preserves_final_total(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Jobs()
            handler = server.Handler.__new__(server.Handler)
            handler.server = SimpleNamespace(store=Store(Path(directory) / "data.sqlite3"), jobs=jobs)
            compared, release = Event(), Event()
            cases = [{"split": "test"} for _ in range(8)]

            def evaluate(model, split, on_result=None):
                self.assertTrue(callable(on_result))
                for done in range(1, 9):
                    on_result({"example_id": str(done)}, done, 8)
                    if done == 3:
                        compared.set()
                        release.wait(timeout=3)
                return {"model": model, "results": [{} for _ in range(8)], "metrics": {"total": 8}}

            try:
                with patch.object(server.evaluation, "examples", return_value=cases), \
                        patch.object(server.evaluation, "run", side_effect=evaluate):
                    job = handler.route("POST", "/api/model-jobs", {"request_id": "e" * 32,
                                        "path": "/api/evaluate", "body": {"model": "test:model", "split": "test"}})["job"]
                    self.assertTrue(compared.wait(timeout=2))
                    running = jobs.get(job["id"])
                    self.assertEqual(running["phase"], "comparing")
                    self.assertEqual(running["stage"], "Сравниваем учебные пары")
                    self.assertEqual(running["progress"], {"current": 3, "total": 8})
                    release.set()
                    for _ in range(200):
                        terminal = jobs.get(job["id"])
                        if terminal["status"] in {"complete", "failed"}:
                            break
                        time.sleep(0.01)
                    self.assertEqual(terminal["status"], "complete", terminal)
                    self.assertEqual(terminal["progress"], {"current": 8, "total": 8})
            finally:
                release.set()
                jobs.close()

    def test_legacy_evaluation_does_not_require_progress_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "data.sqlite3")
            handler = server.Handler.__new__(server.Handler)
            result = {"metrics": {"total": 8}, "results": []}
            with patch.object(server.evaluation, "run", return_value=result) as evaluate:
                response = handler.model_operation(store, "/api/evaluate", {"split": "test", "model": "test:model"})
            evaluate.assert_called_once_with("test:model", "test")
            self.assertEqual(response["run"]["metrics"], {"total": 8})

    def test_status_polls_read_only_manifest_and_preserve_embedding_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "cloud-models.json").write_text(json.dumps(manifest()))
            (runtime / "local-inference-disabled").touch()
            handler = server.Handler.__new__(server.Handler)
            handler.server = SimpleNamespace(store=Store(runtime / "data.sqlite3"))
            with patch.dict(os.environ, {"ISTOK_RUNTIME_DIR": directory,
                                         "ISTOK_CLOUD_MANIFEST": str(runtime / "cloud-models.json")}), \
                    patch.object(server.llm, "status", side_effect=cloud_transport.status), \
                    patch.object(cloud_transport, "_service") as remote, \
                    patch.object(cloud_transport.importlib, "import_module") as sdk:
                for _ in range(100):
                    response = handler.route("GET", "/api/status", {})
                remote.assert_not_called()
                sdk.assert_not_called()
            self.assertEqual(response["model"]["provider"], "modal")
            self.assertEqual(response["model"]["status_kind"], "configuration")
            self.assertEqual(response["embedding"]["provider"], "modal")
            self.assertTrue(response["embedding"]["available"])
            self.assertTrue(response["embedding"]["can_submit"])
            self.assertTrue(response["embedding"]["local_inference_disabled"])
            self.assertIsNone(response["model"]["last_success_at"])
            self.assertFalse((runtime / "cloud-calls").exists())

    def test_generic_model_path_cannot_queue_an_arbitrary_api_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Jobs()
            handler = server.Handler.__new__(server.Handler)
            store = Store(Path(directory) / "data.sqlite3")
            handler.server = SimpleNamespace(store=store, jobs=jobs)
            try:
                with self.assertRaises(ValueError):
                    handler.route("POST", "/api/model-jobs", {"request_id": "f" * 32,
                                  "path": "/api/sources", "body": {"text": "private text"}})
                self.assertFalse(jobs.items)
                self.assertIsNone(store.get("model-request", "model-request:" + "f" * 32))
            finally:
                jobs.close()


if __name__ == "__main__":
    unittest.main()
