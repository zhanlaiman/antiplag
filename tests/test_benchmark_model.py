import json
import unittest
from unittest.mock import Mock

from plagiarism import llm
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


if __name__ == "__main__":
    unittest.main()
