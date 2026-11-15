import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from plagiarism import search_serper
from plagiarism.auth_provider import NoRedirect
from plagiarism.web_search import WebSearchError
from tests.test_search_tavily import Budget, response


class SerperSearchTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.create_connection", side_effect=AssertionError("Network forbidden in adapter tests"))
        network.start()
        self.addCleanup(network.stop)
        self.key = "serper-private-test-key"
        self.budget = Budget()
        self.search = search_serper.SerperSearch(self.budget, key=self.key)
        self.search.opener = Mock()

    def test_fixed_post_preserves_query_and_never_returns_snippets_or_key(self):
        self.search.opener.open.return_value = response({"organic": [
            {"link": "https://EXAMPLE.org/paper#one", "snippet": "Never matching evidence", "title": "Title"},
            {"link": "https://example.org/paper#two"}], "credits": 1})
        phrase = '"Сосна берёза клён кедр липа пихта"'
        self.assertEqual(self.search.search(phrase, "request-1"), [{"url": "https://example.org/paper"}])
        request = self.search.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://google.serper.dev/search")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-api-key"), self.key)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {"q": phrase, "num": 5, "hl": "ru", "gl": "ru"})
        self.assertNotIn(self.key, request.full_url)
        self.assertNotIn(self.key.encode(), request.data)
        self.assertEqual(self.search.opener.open.call_args.kwargs, {"timeout": 25})
        self.assertEqual(self.search.last_usage, {"credits": 1})
        self.assertEqual(self.budget.finished, ["request-1"])

    def test_english_locale_and_successful_empty_result_do_not_add_other_requests(self):
        self.search.opener.open.return_value = response({"organic": []})
        self.assertEqual(self.search.search("valid botanical phrase", "request-1"), [])
        request = self.search.opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"q": "valid botanical phrase", "num": 5, "hl": "en", "gl": "us"})
        self.assertEqual(self.search.opener.open.call_count, 1)
        self.assertEqual(self.search.last_usage, {"credits": 1})

    def test_reservation_precedes_http_and_same_identifier_never_reposts(self):
        def opened(request, timeout):
            self.assertIn("request-1", self.budget.reserved)
            self.assertEqual(self.budget.finished, [])
            return response({"organic": [], "credits": 1})

        self.search.opener.open.side_effect = opened
        self.search.search("valid query", "request-1")
        with self.assertRaises(WebSearchError) as caught:
            self.search.search("valid query", "request-1")
        self.assertEqual(caught.exception.code, "search_already_attempted")
        self.assertEqual(self.search.opener.open.call_count, 1)

    def test_unknown_timeout_is_charged_and_cannot_be_repeated(self):
        self.search.opener.open.side_effect = TimeoutError(self.key)
        for _ in range(2):
            with self.assertRaises(WebSearchError) as caught:
                self.search.search("valid query", "request-1")
            self.assertNotIn(self.key, str(caught.exception))
        self.assertEqual(self.budget.reserved, {"request-1"})
        self.assertEqual(self.budget.finished, [])
        self.assertEqual(self.search.opener.open.call_count, 1)
        self.assertIsNone(self.search.last_usage)

    def test_http_200_errors_bad_schema_and_excess_credits_are_not_empty_success(self):
        bad = [{}, [], {"organic": [], "error": self.key}, {"organic": [], "errors": []},
               {"organic": [], "detail": self.key}, {"organic": [], "success": False},
               {"organic": [], "status": "failed"}, {"organic": [], "statusCode": 429},
               {"organic": [], "statusCode": True}, {"organic": None}, {"organic": [None]},
               {"organic": [{}]}, {"organic": [], "credits": 2}, {"organic": [], "credits": True},
               {"organic": [], "credits": -1}, {"organic": [], "credits": "1"},
               {"organic": [{"link": "https://example.org"}] * 101}]
        for index, value in enumerate(bad):
            with self.subTest(index=index):
                self.search.opener.open.return_value = response(value)
                with self.assertRaises(WebSearchError) as caught:
                    self.search.search("valid query", f"invalid-{index}")
                self.assertEqual(caught.exception.code, "search_provider_error")
                self.assertNotIn(self.key, str(caught.exception))
        self.assertEqual(self.budget.finished, [])

    def test_malicious_and_credential_reflecting_links_never_escape(self):
        encoded_key = "".join(f"%{ord(char):02X}" for char in self.key)
        bad = ["javascript:alert(1)", "file:///tmp/private", "//example.org/path", "https://user:pass@example.org",
               "https://example.org:bad", "https://example.org:65536", "https://example.org/\nprivate",
               "https://example.org/\x7fprivate", "https://example.org/\ud800", "https://example.org/?key=" + self.key,
               "https://example.org/?key=" + encoded_key, "https://example.org/?key=" + encoded_key.replace("%", "%25"),
               "https://example.org/" + "x" * 4096]
        for index, url in enumerate(bad):
            with self.subTest(index=index):
                self.search.opener.open.return_value = response({"organic": [{"link": url}], "credits": 1})
                with self.assertRaises(WebSearchError) as caught:
                    self.search.search("valid query", f"unsafe-{index}")
                self.assertEqual(caught.exception.code, "search_provider_error")
                self.assertNotIn(self.key, str(caught.exception))
        self.assertEqual(self.budget.finished, [])

    def test_output_is_bounded_to_first_five_organic_links(self):
        self.search.opener.open.return_value = response({"organic": [
            {"link": f"https://example.org/{index}", "snippet": "Not evidence"} for index in range(8)], "credits": 1})
        rows = self.search.search("valid query", "request-1")
        self.assertEqual(rows, [{"url": f"https://example.org/{index}"} for index in range(5)])
        self.assertTrue(all(set(row) == {"url"} for row in rows))

    def test_valid_google_interstitial_stops_without_following_or_guessing_displayed_link(self):
        urls = ("https://www.google.com/goto?url=opaque", "https://google.com/url?q=opaque",
                "https://www.google.com./g%6fto?url=opaque")
        for index, url in enumerate(urls):
            self.search.opener.open.return_value = response({"organic": [
                {"link": url, "displayed_link": "https://example.org/source", "snippet": "Not evidence"}], "credits": 1})
            with self.assertRaises(WebSearchError) as caught:
                self.search.search("valid query", f"redirect-{index}")
            self.assertEqual(caught.exception.code, "search_redirect_unresolved")
            self.assertIn(f"redirect-{index}", self.budget.finished)
            self.assertEqual(self.search.last_usage, {"credits": 1})
        self.assertEqual(self.search.opener.open.call_count, 3)

    def test_interstitial_does_not_mark_a_partially_invalid_response_complete(self):
        self.search.opener.open.return_value = response({"organic": [
            {"link": "https://google.com/goto?url=opaque"}, {"link": "javascript:alert(1)"}], "credits": 1})
        with self.assertRaises(WebSearchError) as caught:
            self.search.search("valid query", "request-1")
        self.assertEqual(caught.exception.code, "search_provider_error")
        self.assertEqual(self.budget.finished, [])

    def test_http_errors_are_closed_sanitized_and_never_retried(self):
        for status, code in ((302, "search_provider_error"), (401, "search_credentials_invalid"),
                             (403, "search_credentials_invalid"), (402, "search_budget_exhausted"),
                             (429, "search_rate_limited"), (500, "search_provider_error")):
            with self.subTest(status=status):
                stream = io.BytesIO(self.key.encode())
                self.search.opener.open.side_effect = HTTPError(search_serper.SEARCH_ENDPOINT, status, self.key, {}, stream)
                with self.assertRaises(WebSearchError) as caught:
                    self.search.search("valid query", f"http-{status}")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(self.key, str(caught.exception))
                self.assertTrue(stream.closed)
        self.assertEqual(self.search.opener.open.call_count, 6)
        self.assertEqual(self.budget.finished, [])

    def test_transport_invalid_json_and_response_cap_leave_attempt_unfinished(self):
        self.search.opener.open.side_effect = URLError(self.key)
        with self.assertRaises(WebSearchError):
            self.search.search("valid query", "network-1")
        self.search.opener.open.side_effect = None
        for index, raw in enumerate((b"not json", b"<html>error</html>", b"x" * (search_serper.MAX_RESPONSE_BYTES + 1))):
            stream = io.BytesIO(raw)
            self.search.opener.open.return_value = stream
            with self.assertRaises(WebSearchError):
                self.search.search("valid query", f"body-{index}")
            self.assertTrue(stream.closed)
        self.assertEqual(self.budget.finished, [])

    def test_invalid_input_key_or_missing_budget_stops_before_http(self):
        for key in ("", None, 12, "key with space", "key\nheader", "x" * 1025):
            with patch.object(search_serper, "key_for", return_value=""):
                with self.assertRaises(WebSearchError):
                    search_serper.SerperSearch(self.budget, key=key)
        with self.assertRaises(TypeError):
            search_serper.SerperSearch(None, key=self.key)
        for query, identifier in (("", "ok"), ("x" * 401, "ok"), ("line\nbreak", "ok"), ("\ud800", "ok"),
                                  ("query", ""), ("query", "bad/id"), ("query", "x" * 129)):
            with self.assertRaises(WebSearchError) as caught:
                self.search.search(query, identifier)
            self.assertEqual(caught.exception.code, "invalid_search_query")
        self.search.opener.open.assert_not_called()
        self.assertEqual(self.budget.reserved, set())

    def test_key_loader_and_no_redirect_handler_are_used_without_network(self):
        with patch.object(search_serper, "key_for", return_value=self.key) as loader:
            with patch.object(search_serper, "build_opener", return_value=Mock()) as builder:
                search_serper.SerperSearch(self.budget)
        loader.assert_called_once_with("serper")
        handler = builder.call_args.args[0]
        self.assertIsInstance(handler, NoRedirect)
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://attacker.example"))


if __name__ == "__main__":
    unittest.main()
