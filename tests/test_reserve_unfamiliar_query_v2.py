from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from scripts.reserve_unfamiliar_query_v2 import reserve, URLS, write_json


class QueryReservationGuards(unittest.TestCase):
    """Authored responses and temporary files; no network or corpus prose."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.out = self.base / "reserved"
        self.pool = self.base / "pool.json"
        self.pool.write_text(json.dumps({"sources": [{"url": "https://example.test/known"}]}))

    def forbidden_fetcher(self, *args):
        self.fail("No new Fetcher may be created")

    def test_fixed_url_collision_stops_before_any_fetch(self):
        self.pool.write_text(json.dumps({"sources": [{"url": URLS[0][1]}]}))
        with self.assertRaisesRegex(ValueError, "already exists"):
            reserve(fetch=True, directory=self.out, pool=self.pool, fetcher_factory=self.forbidden_fetcher)
        self.assertFalse(self.out.exists())

    def test_reservation_without_flag_does_not_fetch(self):
        result = reserve(directory=self.out, pool=self.pool, fetcher_factory=self.forbidden_fetcher)
        self.assertEqual([r["state"] for r in result["sources"]], ["reserved_not_fetched"] * 2)

    def test_submitted_and_failed_attempts_are_never_retried(self):
        reserve(directory=self.out, pool=self.pool, fetcher_factory=self.forbidden_fetcher)
        for (label, url), state in zip(URLS, ("submitted", "failed_or_unknown")):
            write_json(self.out / (label + "-attempt.json"), {"requested_url": url, "state": state})
        result = reserve(fetch=True, directory=self.out, pool=self.pool, fetcher_factory=self.forbidden_fetcher)
        self.assertEqual([r["state"] for r in result["sources"]], ["submitted", "failed_or_unknown"])

    def test_attempt_precedes_fetch_and_success_is_idempotent_without_prose_output(self):
        calls = []
        test = self
        text = "Author-only fixture body that must never appear in summary output."
        class FakeFetcher:
            def __init__(self, label, directory):
                self.label, self.directory = label, directory
            def fetch(self, url):
                attempt = json.loads((self.directory / (self.label + "-attempt.json")).read_text())
                test.assertEqual(attempt["state"], "submitted")
                test.assertEqual(attempt["requested_url"], url)
                calls.append(url)
                return {"url": url, "title": "DO_NOT_DISPLAY_THIS_TITLE", "text": text, "links": [],
                    "text_sha256": sha256(text.encode()).hexdigest(), "bytes_read": len(text), "decoded_bytes": len(text),
                    "fetched_at": "2000-01-01T00:00:00Z", "content_type": "text/plain", "robots_url": "https://example.test/robots.txt",
                    "extraction": "plain-v1"}
        first = reserve(fetch=True, directory=self.out, pool=self.pool, fetcher_factory=FakeFetcher)
        self.assertEqual(calls, [url for _, url in URLS])
        self.assertEqual([r["state"] for r in first["sources"]], ["complete", "complete"])
        self.assertNotIn(text, json.dumps(first))
        self.assertNotIn("DO_NOT_DISPLAY_THIS_TITLE", json.dumps(first))
        reserve(fetch=True, directory=self.out, pool=self.pool, fetcher_factory=self.forbidden_fetcher)
        for path in self.out.iterdir():
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
