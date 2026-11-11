import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from plagiarism import llm
from scripts import benchmark_model
from scripts.benchmark_model import record_request


class BenchmarkRecorderTests(unittest.TestCase):
    def test_records_usage_and_thinking_count_without_private_text(self):
        private_text = "Internal reasoning that must not be saved."
        response = {"message": {"content": "Final response.", "thinking": private_text},
                    "prompt_eval_count": 20, "eval_count": 10, "done_reason": "stop"}
        request = Mock(return_value=response)
        records = []
        returned = record_request(request, records, "/api/chat", {"model": "local:model", "think": False})
        self.assertIs(returned, response)
        self.assertEqual(records[0]["thinking_characters"], len(private_text))
        self.assertEqual(records[0]["prompt_eval_count"], 20)
        self.assertNotIn(private_text, json.dumps(records))
        self.assertNotIn("Final response.", json.dumps(records))

    def test_records_request_error_and_reraises_the_original_error(self):
        error = llm.OllamaError("Сервис недоступен")
        records = []
        with self.assertRaises(llm.OllamaError) as raised:
            record_request(Mock(side_effect=error), records, "/api/chat", {"model": "local:model"})
        self.assertIs(raised.exception, error)
        self.assertEqual(records[0]["status"], "error")
        self.assertEqual(records[0]["error_code"], "model_error")

    def test_non_chat_requests_are_passed_through_without_logging(self):
        response, records = {"models": []}, []
        request = Mock(return_value=response)
        self.assertIs(record_request(request, records, "/api/ps", timeout=2), response)
        self.assertEqual(records, [])
        request.assert_called_once_with("/api/ps", None, 2)


class ResourceSnapshotTests(unittest.TestCase):
    def test_linux_system_memory_preserves_units_and_ignores_other_fields(self):
        meminfo = "MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\nMemFree: 400 kB\n"
        with patch.object(benchmark_model.sys, "platform", "linux"), \
             patch.object(benchmark_model.Path, "read_text", return_value=meminfo), \
             patch.object(benchmark_model.shutil, "which", return_value=None), \
             patch.object(benchmark_model.subprocess, "run") as run:
            result = benchmark_model.memory_snapshot()
        memory = result["system_memory"]
        self.assertEqual(memory["scope"], "system")
        self.assertEqual(memory["values"]["MemTotal"], {"value": 16777216, "unit": "kB"})
        self.assertEqual(memory["values"]["SwapTotal"]["value"], 0)
        self.assertNotIn("MemFree", memory["values"])
        self.assertEqual(result["gpu_devices"]["status"], "not_installed")
        run.assert_not_called()

    def test_gpu_csv_handles_multiple_devices_and_unavailable_memory(self):
        output = '"NVIDIA Test, Model", 24576, 1024, 550.90.07\nNVIDIA Other Model, 8192, N/A, 550.90.07\n'
        with patch.object(benchmark_model.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
             patch.object(benchmark_model.subprocess, "run", return_value=Mock(stdout=output)) as run:
            result = benchmark_model.gpu_snapshot()
        self.assertEqual(result["scope"], "device")
        self.assertEqual(result["devices"][0]["name"], "NVIDIA Test, Model")
        self.assertEqual(result["devices"][0]["memory_total"], {"value": 24576, "unit": "MiB"})
        self.assertEqual(result["devices"][0]["memory_used"]["value"], 1024)
        self.assertIsNone(result["devices"][1]["memory_used"]["value"])
        self.assertEqual(run.call_args.args[0][1], "--query-gpu=name,memory.total,memory.used,driver_version")

    def test_gpu_tool_failure_is_nonfatal_and_does_not_store_raw_output(self):
        error = subprocess.CalledProcessError(1, "nvidia-smi", output="private identifier", stderr="private path")
        with patch.object(benchmark_model.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
             patch.object(benchmark_model.subprocess, "run", side_effect=error):
            result = benchmark_model.gpu_snapshot()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["returncode"], 1)
        self.assertNotIn("private", json.dumps(result))

    def test_gpu_timeout_and_malformed_output_are_nonfatal(self):
        for response in (subprocess.TimeoutExpired("nvidia-smi", 2), Mock(stdout="unexpected output")):
            with self.subTest(response=response):
                with patch.object(benchmark_model.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
                     patch.object(benchmark_model.subprocess, "run") as run:
                    if isinstance(response, Exception):
                        run.side_effect = response
                    else:
                        run.return_value = response
                    self.assertEqual(benchmark_model.gpu_snapshot()["status"], "error")

    def test_unreadable_or_partial_meminfo_does_not_break_snapshot(self):
        with patch.object(benchmark_model.Path, "read_text", side_effect=PermissionError("private path")):
            result = benchmark_model.linux_memory_snapshot()
        self.assertEqual(result["status"], "error")
        self.assertNotIn("private", json.dumps(result))
        with patch.object(benchmark_model.Path, "read_text", return_value="MemTotal: 12 kB\nSwapTotal: invalid kB\n"):
            result = benchmark_model.linux_memory_snapshot()
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["values"]["MemAvailable"])
        self.assertIsNone(result["values"]["SwapTotal"])

    def test_existing_mac_snapshot_fields_are_preserved(self):
        with patch.object(benchmark_model.sys, "platform", "darwin"), \
             patch.object(benchmark_model.subprocess, "run", side_effect=[Mock(stdout="17179869184\n"), Mock(stdout="used = 1M\n")]):
            result = benchmark_model.memory_snapshot()
        self.assertEqual(result, {"platform": "darwin", "hw.memsize": "17179869184", "vm.swapusage": "used = 1M"})


if __name__ == "__main__":
    unittest.main()
