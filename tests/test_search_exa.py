import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from plagiarism import search_exa
from plagiarism.auth_provider import NoRedirect
from plagiarism.web_search import WebSearchError
from tests.test_search_tavily import Budget, response


class ExaSearchTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.create_connection", side_effect=AssertionError("Network forbidden in adapter tests"))
        network.start()
        self.addCleanup(network.stop)
        self.key = "exa-private-test-key"
        self.budget = Budget()
        self.search = search_exa.ExaSearch(self.budget, key=self.key)
        self.search.opener = Mock()

    def test_fixed_lite_post_has_no_content_or_ai_request_and_returns_only_urls(self):
        self.search.opener.open.return_value = response({"results": [
            {"url": "https://EXAMPLE.org/paper#one", "text": "Never matching evidence", "highlights": ["Not evidence"]},
            {"url": "https://example.org/paper#two", "summary": "Not evidence"}],
            "costDollars": {"total": 0.004, "search": {"neural": 0.004}}})
        phrase = '"Сосна берёза клён кедр липа пихта"'
        self.assertEqual(self.search.search(phrase, "request-1"), [{"url": "https://example.org/paper"}])
        request = self.search.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.exa.ai/search")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-api-key"), self.key)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {"query": phrase, "type": "lite", "numResults": 5})
        self.assertNotIn(self.key, request.full_url)
        self.assertNotIn(self.key.encode(), request.data)
        self.assertEqual(self.search.opener.open.call_args.kwargs, {"timeout": 25})
        self.assertEqual(self.search.last_usage, {"credits": 1, "cost_dollars": 0.004})
        self.assertEqual(self.budget.finished, ["request-1"])

    def test_missing_cost_still_consumes_one_reserved_request_without_claiming_zero_cost(self):
        self.search.opener.open.return_value = response({"results": []})
        self.assertEqual(self.search.search("valid query", "request-1"), [])
        self.assertEqual(self.search.last_usage, {"credits": 1})
        self.assertEqual(self.budget.finished, ["request-1"])
        self.assertEqual(self.search.opener.open.call_count, 1)

    def test_reservation_precedes_http_and_duplicate_never_reposts(self):
        def opened(request, timeout):
            self.assertIn("request-1", self.budget.reserved)
            self.assertEqual(self.budget.finished, [])
            return response({"results": [], "costDollars": {"total": 0.004}})

        self.search.opener.open.side_effect = opened
        self.search.search("valid query", "request-1")
        with self.assertRaises(WebSearchError) as caught:
            self.search.search("valid query", "request-1")
        self.assertEqual(caught.exception.code, "search_already_attempted")
        self.assertEqual(self.search.opener.open.call_count, 1)

    def test_unknown_timeout_stays_charged_and_same_identifier_is_not_replayed(self):
        self.search.opener.open.side_effect = TimeoutError(self.key)
        for _ in range(2):
            with self.assertRaises(WebSearchError) as caught:
                self.search.search("valid query", "request-1")
            self.assertNotIn(self.key, str(caught.exception))
        self.assertEqual(self.budget.reserved, {"request-1"})
        self.assertEqual(self.budget.finished, [])
        self.assertEqual(self.search.opener.open.call_count, 1)
        self.assertIsNone(self.search.last_usage)

    def test_cost_is_bounded_finite_numeric_and_never_accepted_from_bool_or_string(self):
        bad = [None, [], {}, {"total": True}, {"total": "0.004"}, {"total": -0.001},
               {"total": 0.004001}, {"total": float("nan")}, {"total": float("inf")}, {"total": float("-inf")}]
        for index, cost in enumerate(bad):
            with self.subTest(index=index):
                self.search.opener.open.return_value = response({"results": [], "costDollars": cost})
                with self.assertRaises(WebSearchError) as caught:
                    self.search.search("valid query", f"cost-{index}")
                self.assertEqual(caught.exception.code, "search_provider_error")
                self.assertIsNone(self.search.last_usage)
        self.assertEqual(self.budget.finished, [])

    def test_http_200_errors_missing_results_and_extra_results_fail_closed(self):
        bad = [{}, [], {"results": [], "error": self.key}, {"results": [], "errors": []},
               {"results": [], "detail": self.key}, {"results": [], "success": False},
               {"results": [], "status": "error"}, {"results": [], "statusCode": 402},
               {"results": [], "statusCode": True}, {"results": {}}, {"results": [None]},
               {"results": [{}]}, {"results": [{"url": "https://example.org"}] * 6}]
        for index, value in enumerate(bad):
            with self.subTest(index=index):
                self.search.opener.open.return_value = response(value)
                with self.assertRaises(WebSearchError) as caught:
                    self.search.search("valid query", f"invalid-{index}")
                self.assertEqual(caught.exception.code, "search_provider_error")
                self.assertNotIn(self.key, str(caught.exception))
        self.assertEqual(self.budget.finished, [])

    def test_malicious_and_credential_reflecting_urls_do_not_become_sources(self):
        encoded_key = "".join(f"%{ord(char):02X}" for char in self.key)
        bad = ["javascript:alert(1)", "file:///tmp/private", "//example.org/path", "https://user:pass@example.org",
               "https://example.org:bad", "https://example.org:65536", "https://example.org/\nprivate",
               "https://example.org/\x7fprivate", "https://example.org/\ud800", "https://example.org/?key=" + self.key,
               "https://example.org/?key=" + encoded_key, "https://example.org/?key=" + encoded_key.replace("%", "%25"),
               "https://example.org/" + "x" * 4096]
        for index, url in enumerate(bad):
            with self.subTest(index=index):
                self.search.opener.open.return_value = response({"results": [{"url": url}]})
                with self.assertRaises(WebSearchError) as caught:
                    self.search.search("valid query", f"unsafe-{index}")
                self.assertEqual(caught.exception.code, "search_provider_error")
                self.assertNotIn(self.key, str(caught.exception))
        self.assertEqual(self.budget.finished, [])

    def test_http_errors_are_closed_sanitized_and_not_retried(self):
        for status, code in ((302, "search_provider_error"), (401, "search_credentials_invalid"),
                             (403, "search_credentials_invalid"), (402, "search_budget_exhausted"),
                             (429, "search_rate_limited"), (500, "search_provider_error")):
            with self.subTest(status=status):
                stream = io.BytesIO(self.key.encode())
                self.search.opener.open.side_effect = HTTPError(search_exa.SEARCH_ENDPOINT, status, self.key, {}, stream)
                with self.assertRaises(WebSearchError) as caught:
                    self.search.search("valid query", f"http-{status}")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(self.key, str(caught.exception))
                self.assertTrue(stream.closed)
        self.assertEqual(self.search.opener.open.call_count, 6)
        self.assertEqual(self.budget.finished, [])

    def test_transport_invalid_json_and_oversize_response_leave_started_row(self):
        self.search.opener.open.side_effect = URLError(self.key)
        with self.assertRaises(WebSearchError):
            self.search.search("valid query", "network-1")
        self.search.opener.open.side_effect = None
        for index, raw in enumerate((b"not json", b"<html>error</html>", b"x" * (search_exa.MAX_RESPONSE_BYTES + 1))):
            stream = io.BytesIO(raw)
            self.search.opener.open.return_value = stream
            with self.assertRaises(WebSearchError):
                self.search.search("valid query", f"body-{index}")
            self.assertTrue(stream.closed)
        self.assertEqual(self.budget.finished, [])

    def test_invalid_key_input_or_budget_does_not_reserve_or_contact_provider(self):
        for key in ("", None, 12, "key with space", "key\nheader", "x" * 1025):
            with patch.object(search_exa, "key_for", return_value=""):
                with self.assertRaises(WebSearchError):
                    search_exa.ExaSearch(self.budget, key=key)
        with self.assertRaises(TypeError):
            search_exa.ExaSearch(None, key=self.key)
        for query, identifier in (("", "ok"), ("x" * 401, "ok"), ("line\nbreak", "ok"), ("\ud800", "ok"),
                                  ("query", ""), ("query", "bad/id"), ("query", "x" * 129)):
            with self.assertRaises(WebSearchError) as caught:
                self.search.search(query, identifier)
            self.assertEqual(caught.exception.code, "invalid_search_query")
        self.search.opener.open.assert_not_called()
        self.assertEqual(self.budget.reserved, set())

    def test_key_loader_and_redirect_refusal_preserve_fixed_authentication_boundary(self):
        with patch.object(search_exa, "key_for", return_value=self.key) as loader:
            with patch.object(search_exa, "build_opener", return_value=Mock()) as builder:
                search_exa.ExaSearch(self.budget)
        loader.assert_called_once_with("exa")
        handler = builder.call_args.args[0]
        self.assertIsInstance(handler, NoRedirect)
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://attacker.example"))


if __name__ == "__main__":
    unittest.main()
