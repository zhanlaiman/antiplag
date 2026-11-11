import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from plagiarism import cloud_transport as transport
from plagiarism.cloud_contract import model_digests
from tests.test_cloud_contract import chat, manifest


class CloudTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name)
        self.config = manifest()
        (self.runtime / "cloud-models.json").write_text(json.dumps(self.config), encoding="utf-8")
        self.environment = patch.dict(os.environ, {"ISTOK_RUNTIME_DIR": str(self.runtime),
                                                   "ISTOK_CLOUD_MANIFEST": str(self.runtime / "cloud-models.json")})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def service(self, response=None):
        call = Mock(object_id="fc-test123")
        call.get.return_value = {"deployment_id": self.config["deployment_id"],
                                 "model_digests": model_digests(self.config),
                                 "response": response or {"message": {"content": "{}"}}}
        service = Mock()
        service.request.spawn.return_value = call
        return service, call

    def test_repeated_status_and_tags_do_not_import_sdk_or_wake_gpu(self):
        (self.runtime / "local-inference-disabled").touch()
        with patch.object(transport.importlib, "import_module") as importer, patch.object(transport, "_service") as service:
            for _ in range(100):
                status = transport.status()
                tags = transport.request("/api/tags", timeout=2)
            importer.assert_not_called()
            service.assert_not_called()
        self.assertTrue(status["configured"])
        self.assertTrue(status["can_submit"])
        self.assertTrue(status["local_inference_disabled"])
        self.assertIsNone(status["last_success_at"])
        self.assertEqual(tags["models"][1]["digest"], "c" * 64)
        self.assertFalse((self.runtime / "cloud-calls").exists())

    def test_unknown_operation_or_model_is_rejected_before_sdk(self):
        with patch.object(transport, "_service") as service:
            for path, payload in (("/api/pull", chat()), ("/api/chat", chat() | {"model": "wrong"})):
                with self.assertRaises(transport.CloudTransportError) as caught:
                    transport.request(path, payload)
                self.assertEqual(caught.exception.code, "cloud_request_rejected")
            service.assert_not_called()

    def test_compute_sends_expected_version_records_call_without_text_and_updates_metadata(self):
        service, call = self.service()
        with patch.object(transport, "_service", return_value=service):
            response = transport.request("/api/chat", chat(), timeout=180)
        self.assertEqual(response, {"message": {"content": "{}"}})
        service.request.spawn.assert_called_once_with("/api/chat", chat(), self.config["deployment_id"])
        call.get.assert_called_once_with(timeout=780)
        call.cancel.assert_not_called()
        records = list((self.runtime / "cloud-calls").glob("*.json"))
        self.assertEqual(len(records), 1)
        saved = records[0].read_text(encoding="utf-8")
        self.assertNotIn("Авторский", saved)
        self.assertEqual(json.loads(saved)["call_id"], "fc-test123")
        self.assertEqual(json.loads(saved)["status"], "complete")
        self.assertIsNotNone(transport.status()["last_success_at"])

    def test_timeout_cancels_only_submitted_call_and_never_retries_or_exposes_provider_error(self):
        service, call = self.service()
        call.get.side_effect = TimeoutError("SECRET token abc123 and submitted private text")
        with patch.object(transport, "_service", return_value=service):
            with self.assertRaises(transport.CloudTransportError) as caught:
                transport.request("/api/chat", chat())
        self.assertEqual(caught.exception.code, "cloud_timeout")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(service.request.spawn.call_count, 1)
        call.cancel.assert_called_once_with()
        saved = json.loads(next((self.runtime / "cloud-calls").glob("*.json")).read_text())
        self.assertEqual(saved["cancellation"], "requested")

    def test_wrong_digest_is_not_returned_as_a_valid_generation(self):
        service, call = self.service()
        call.get.return_value["model_digests"] = {"qwen3:4b": "d" * 64}
        with patch.object(transport, "_service", return_value=service):
            with self.assertRaises(transport.CloudTransportError) as caught:
                transport.request("/api/chat", chat())
        self.assertEqual(caught.exception.code, "cloud_response_rejected")
        self.assertIsNone(transport.status()["last_success_at"])

    def test_missing_manifest_fails_without_network_or_local_model_fallback(self):
        (self.runtime / "cloud-models.json").unlink()
        with patch.object(transport, "_service") as service:
            self.assertFalse(transport.status()["configured"])
            with self.assertRaises(transport.CloudTransportError):
                transport.request("/api/chat", chat())
            service.assert_not_called()

    def test_class_identity_check_uses_only_control_plane_before_compute(self):
        self.config["class_id"] = "cs-expected"
        modal = Mock()
        remote_class = modal.Cls.from_name.return_value
        remote_class.object_id = "cs-other"
        with patch.object(transport.importlib, "import_module", return_value=modal):
            with self.assertRaises(transport.CloudTransportError) as caught:
                transport._service(self.config)
        self.assertEqual(caught.exception.code, "cloud_deployment_changed")
        remote_class.hydrate.assert_called_once_with()
        remote_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
