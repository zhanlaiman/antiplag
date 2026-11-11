import io
from contextlib import closing
import fcntl
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.error import HTTPError

from plagiarism.search_mediawiki import MediaWikiSearch, MAX_RESPONSE_BYTES
from plagiarism.web_search import WebSearchError


class Response(io.BytesIO):
    status = 200


class Opener:
    def __init__(self, value):
        self.value, self.calls = value, []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.value, Exception):
            raise self.value
        return Response(self.value)


def reply(**changes):
    row = {"ns": 0, "pageid": 1, "title": "Открытая страница", "snippet": "НЕ доказательство"}
    row.update(changes)
    return json.dumps({"query": {"search": [row]}}, ensure_ascii=False).encode()


class MediaWikiTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "calls.sqlite3"

    def searcher(self, value, **kwargs):
        opener = Opener(value)
        return MediaWikiSearch(self.path, opener=opener, now=lambda: 1800000000, sleep=lambda _: None, **kwargs), opener

    def test_only_fixed_public_url_without_snippet_or_credentials(self):
        search, opener = self.searcher(reply())
        results = search.search('"открытая фраза источника"', "case1")
        self.assertEqual(set(results[0]), {"url"})
        self.assertTrue(results[0]["url"].startswith("https://ru.wikipedia.org/wiki/"))
        request, timeout = opener.calls[0]
        self.assertIsNone(request.data)
        self.assertNotIn("Authorization", request.headers)
        self.assertIn("maxlag=5", request.full_url)
        self.assertEqual(timeout, 15)
        self.assertEqual(search.last_usage, {"public_queries": 1, "cost_kzt": 0, "cached": False})

    def test_complete_resume_uses_no_second_network_request(self):
        search, opener = self.searcher(reply())
        first = search.search("words for a query", "same")
        resumed, unused = self.searcher(AssertionError("network forbidden"))
        self.assertEqual(resumed.search("words for a query", "same"), first)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(unused.calls, [])
        self.assertEqual(resumed.last_usage["public_queries"], 0)
        with self.assertRaises(WebSearchError) as caught:
            resumed.search("different query", "same")
        self.assertEqual(caught.exception.code, "search_already_attempted")

    def test_durable_intent_exists_before_network_and_unknown_never_repeats(self):
        search, opener = self.searcher(OSError("connection lost"))
        original = opener.open
        def checked(request, timeout):
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute("SELECT state FROM calls").fetchone()[0], "submitted")
            return original(request, timeout)
        opener.open = checked
        with self.assertRaises(WebSearchError):
            search.search("words for a query", "uncertain")
        resumed, unused = self.searcher(reply())
        with self.assertRaises(WebSearchError) as caught:
            resumed.search("words for a query", "uncertain")
        self.assertEqual(caught.exception.code, "search_already_attempted")
        self.assertEqual(unused.calls, [])

    def test_rate_limit_and_retry_after_persist_across_instances(self):
        error = HTTPError("https://ru.wikipedia.org/w/api.php", 429, "slow", {"Retry-After": "3600"}, io.BytesIO())
        search, opener = self.searcher(error)
        with self.assertRaises(WebSearchError) as caught:
            search.search("query words", "rate")
        self.assertEqual(caught.exception.code, "search_rate_limited")
        resumed, unused = self.searcher(reply())
        with self.assertRaises(WebSearchError) as caught:
            resumed.search("other query words", "later")
        self.assertEqual(caught.exception.code, "search_rate_limited")
        self.assertEqual(unused.calls, [])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT value FROM settings").fetchone()[0], 1800003600)

    def test_maxlag_stops_second_request(self):
        search, opener = self.searcher(b'{"error":{"code":"maxlag"}}')
        for rid in ("one", "two"):
            with self.assertRaises(WebSearchError) as caught:
                search.search("query words", rid)
            self.assertEqual(caught.exception.code, "search_rate_limited")
        self.assertEqual(len(opener.calls), 1)

    def test_invalid_api_data_never_becomes_source_or_complete_call(self):
        for value in (reply(ns=1), reply(pageid=True), reply(title="bad\nname"),
                      b'{"query":{"search":[]},"x":NaN}', b'{}',
                      b'x' * (MAX_RESPONSE_BYTES + 1)):
            with self.subTest(value=value[:50]):
                self.path.unlink(missing_ok=True)
                search, opener = self.searcher(value)
                with self.assertRaises(WebSearchError):
                    search.search("query words", "bad")
                with closing(sqlite3.connect(self.path)) as db:
                    self.assertEqual(db.execute("SELECT state FROM calls").fetchone()[0], "failed_or_unknown")

    def test_invalid_input_performs_no_io(self):
        search, opener = self.searcher(reply())
        for query, rid in (("bad\nquery", "id"), ("\ud800", "id"), ("words", "../id")):
            with self.assertRaises(WebSearchError):
                search.search(query, rid)
        self.assertFalse(self.path.exists())
        self.assertEqual(opener.calls, [])

    def test_daily_cap_is_durable(self):
        search, opener = self.searcher(reply())
        search.search("first words", "first")
        from unittest.mock import patch
        with patch("plagiarism.search_mediawiki.DAILY_LIMIT", 1):
            resumed, unused = self.searcher(reply())
            with self.assertRaises(WebSearchError) as caught:
                resumed.search("second words", "second")
            self.assertEqual(caught.exception.code, "search_budget_exhausted")
            self.assertEqual(unused.calls, [])

    def test_concurrent_search_fails_promptly_without_waiting_or_network(self):
        search, opener = self.searcher(reply())
        with open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(WebSearchError) as caught:
                search.search("query words", "busy")
        self.assertEqual(caught.exception.code, "search_rate_limited")
        self.assertEqual(opener.calls, [])

    def test_damaged_ledger_fails_closed_without_network(self):
        self.path.write_bytes(b"not a sqlite database")
        search, opener = self.searcher(reply())
        with self.assertRaises(WebSearchError) as caught:
            search.search("query words", "damaged")
        self.assertEqual(caught.exception.code, "search_provider_error")
        self.assertEqual(opener.calls, [])

    def test_nested_json_is_a_recorded_failure_and_not_a_job_exception(self):
        search, opener = self.searcher(b'[' * 2000 + b'0' + b']' * 2000)
        with self.assertRaises(WebSearchError):
            search.search("query words", "nested")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state FROM calls").fetchone()[0], "failed_or_unknown")

    def test_absent_and_oversize_retry_after_still_persist_pause(self):
        for headers, expected in ((None, 900), ({"Retry-After": "9" * 5000}, 86400)):
            with self.subTest(headers_present=headers is not None):
                self.path.unlink(missing_ok=True)
                error = HTTPError("https://ru.wikipedia.org/w/api.php", 429, "slow", headers, io.BytesIO())
                search, opener = self.searcher(error)
                with self.assertRaises(WebSearchError):
                    search.search("query words", "limited")
                with closing(sqlite3.connect(self.path)) as db:
                    self.assertEqual(db.execute("SELECT value FROM settings").fetchone()[0], 1800000000 + expected)
                resumed, unused = self.searcher(reply())
                with self.assertRaises(WebSearchError) as caught:
                    resumed.search("other words", "blocked")
                self.assertEqual(caught.exception.code, "search_rate_limited")
                self.assertEqual(unused.calls, [])


if __name__ == "__main__":
    unittest.main()
