import io
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from plagiarism import search_tavily
from plagiarism.auth_provider import NoRedirect
from plagiarism.web_search import WebSearchError


class Budget:
    def __init__(self):
        self.reserved = set()
        self.finished = []

    def reserve(self, identifier):
        if identifier in self.reserved:
            raise WebSearchError("Already attempted", "search_already_attempted")
        self.reserved.add(identifier)

    def finish(self, identifier):
        self.finished.append(identifier)


def response(value):
    return io.BytesIO(json.dumps(value).encode())


class TavilySearchTests(unittest.TestCase):
    def setUp(self):
        runtime = tempfile.TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        environment = patch.dict(os.environ, {"ISTOK_TAVILY_API_KEY": "tvly-private-test-key", "ISTOK_RUNTIME_DIR": runtime.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.budget = Budget()
        self.searcher = search_tavily.TavilySearch(self.budget)
        self.searcher.opener = Mock()

    def test_fixed_basic_request_has_no_automatic_two_credit_search_or_key_in_url(self):
        self.searcher.opener.open.return_value = response({"results": [{"url": "https://example.org/article"}], "usage": {"credits": 1}})
        result = self.searcher.search('"Редактор сравнил зелёные листья"', "request-1")
        self.assertEqual(result, [{"url": "https://example.org/article"}])
        self.assertEqual(self.searcher.provider, "tavily")
        request = self.searcher.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.tavily.com/search")
        self.assertEqual(request.get_header("Authorization"), "Bearer tvly-private-test-key")
        body = json.loads(request.data)
        self.assertEqual(body, {"query": '"Редактор сравнил зелёные листья"', "search_depth": "basic", "auto_parameters": False,
                               "topic": "general", "max_results": 5, "include_answer": False, "include_raw_content": False, "include_usage": True})
        self.assertNotIn("tvly-private-test-key", request.full_url)
        self.assertNotIn(b"tvly-private-test-key", request.data)
        self.assertNotIn("tvly-private-test-key", json.dumps(result))
        self.assertEqual(self.searcher.opener.open.call_args.kwargs, {"timeout": 15})
        self.assertEqual(self.searcher.last_usage, {"credits": 1})
        self.assertEqual(self.budget.finished, ["request-1"])

    def test_budget_is_reserved_before_call_and_duplicate_cannot_make_second_request(self):
        def opened(request, timeout):
            self.assertIn("request-1", self.budget.reserved)
            self.assertEqual(self.budget.finished, [])
            return response({"results": [], "usage": {"credits": 1}})

        self.searcher.opener.open.side_effect = opened
        self.assertEqual(self.searcher.search("valid query", "request-1"), [])
        with self.assertRaises(WebSearchError) as raised:
            self.searcher.search("valid query", "request-1")
        self.assertEqual(raised.exception.code, "search_already_attempted")
        self.assertEqual(self.searcher.opener.open.call_count, 1)

    def test_malformed_or_http_200_error_is_not_successful_empty_search(self):
        invalid = [{}, [], {"error": "private provider details", "results": []},
                   {"detail": {"error": "bad request"}, "results": []}, {"success": False, "results": []},
                   {"status": "error", "results": []}, {"results": {}}, {"results": [None]}, {"results": [{}]},
                   {"results": [{"url": "javascript:alert(1)"}]}, {"results": [{"url": "https://user:password@example.org"}]},
                   {"results": [{"url": "https://example.org:bad"}]},
                   {"results": [{"url": "https://example.org/?key=tvly-private-test-key"}]},
                   {"results": [], "usage": {"credits": 2}}, {"results": [], "usage": {"credits": True}}]
        for number, data in enumerate(invalid):
            with self.subTest(data=data):
                self.searcher.opener.open.return_value = response(data)
                with self.assertRaises(WebSearchError) as raised:
                    self.searcher.search("valid query", f"request-{number}")
                self.assertEqual(raised.exception.code, "search_provider_error")
                self.assertNotIn("private provider details", str(raised.exception))
        self.assertEqual(self.budget.finished, [])

    def test_url_results_are_deduplicated_without_returning_snippets_as_evidence(self):
        self.searcher.opener.open.return_value = response({"results": [
            {"url": "https://EXAMPLE.org/article#first", "content": "not matching evidence", "title": "Title"},
            {"url": "https://example.org/article#second"}, {"url": "https://other.example/article"}], "usage": {"credits": 1}})
        result = self.searcher.search("valid query", "request-1")
        self.assertEqual(result, [{"url": "https://example.org/article"}, {"url": "https://other.example/article"}])
        self.assertTrue(all(set(item) == {"url"} for item in result))

    def test_http_failures_are_closed_sanitized_not_retried_and_not_finished(self):
        for status, code in ((302, "search_provider_error"), (401, "search_credentials_invalid"),
                             (429, "search_rate_limited"), (432, "search_budget_exhausted"), (500, "search_provider_error")):
            with self.subTest(status=status):
                raw = io.BytesIO(b"tvly-private-test-key-and-provider-details")
                self.searcher.opener.open.side_effect = HTTPError("https://api.tavily.com/search", status, "private-error", {}, raw)
                with self.assertRaises(WebSearchError) as raised:
                    self.searcher.search("valid query", f"status-{status}")
                self.assertEqual(raised.exception.code, code)
                self.assertTrue(raw.closed)
                self.assertNotIn("tvly-private-test-key", str(raised.exception))
        self.assertEqual(self.searcher.opener.open.call_count, 5)
        self.assertEqual(self.budget.finished, [])

    def test_transport_and_invalid_json_do_not_finish_or_automatically_retry(self):
        for number, effect in enumerate((URLError("tvly-private-test-key"), TimeoutError("private timeout"))):
            self.searcher.opener.open.side_effect = effect
            with self.assertRaises(WebSearchError) as raised:
                self.searcher.search("valid query", f"network-{number}")
            self.assertNotIn("tvly-private-test-key", str(raised.exception))
        self.searcher.opener.open.side_effect = None
        for number, raw in enumerate((b"invalid json", b"<html>proxy error</html>", b"x" * (search_tavily.MAX_RESPONSE_BYTES + 1))):
            stream = io.BytesIO(raw)
            self.searcher.opener.open.return_value = stream
            with self.assertRaises(WebSearchError):
                self.searcher.search("valid query", f"malformed-{number}")
            self.assertTrue(stream.closed)
        self.assertEqual(self.budget.finished, [])

    def test_missing_key_or_invalid_input_does_not_reserve_or_contact_provider(self):
        for key in ("", "abc\ndef", "key with space"):
            with patch.dict(os.environ, {"ISTOK_TAVILY_API_KEY": key}):
                with self.assertRaises(WebSearchError) as raised:
                    search_tavily.TavilySearch(self.budget)
                self.assertEqual(raised.exception.code, "web_not_configured")
        for query in ("", "x" * 401, "line\nbreak", "\ud800"):
            with self.assertRaises(WebSearchError):
                self.searcher.search(query, "request-1")
        self.searcher.opener.open.assert_not_called()
        self.assertEqual(self.budget.reserved, set())

    def test_redirects_are_disabled_before_authorization_can_leave_fixed_host(self):
        with patch.object(search_tavily, "build_opener", return_value=Mock()) as builder:
            search_tavily.TavilySearch(self.budget)
        handler = builder.call_args.args[0]
        self.assertIsInstance(handler, NoRedirect)
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://attacker.example"))

    def test_adapter_uses_private_key_loader_without_returning_key(self):
        with patch.object(search_tavily, "key_for", return_value="tvly-private-file-fixture") as loader:
            searcher = search_tavily.TavilySearch(self.budget)
        loader.assert_called_once_with("tavily")
        searcher.opener = Mock()
        searcher.opener.open.return_value = response({"results": [], "usage": {"credits": 1}})
        self.assertEqual(searcher.search("valid query", "request-1"), [])
        request = searcher.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer tvly-private-file-fixture")


if __name__ == "__main__":
    unittest.main()
