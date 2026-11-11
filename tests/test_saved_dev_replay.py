"""Saved replay must never silently search, fetch, or invent source links."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.replay_saved_dev_search import CachedPages, FrozenSearch, annotate_replay, dump, load_queries
from plagiarism.web_fetch import FetchError
from plagiarism.web_search import WebSearchError

URL = "https://example.org/page"
CHILD = "https://example.org/child"
TEXT = "The editor carefully compared the original records and preserved the final source material."


def page(url=URL, links=None):
    result = {"url": url, "text": TEXT, "text_sha256": sha256(TEXT.encode()).hexdigest(), "title": "Owned fixture"}
    if links is not None:
        result["links"] = links
    return result


class SavedReplaySafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_search_only_returns_the_exact_saved_phrase_reply(self):
        records = {'"owned phrase"': {"state": "complete", "request_id": "original",
                                      "results": [{"url": URL}]}}
        search = FrozenSearch(records)
        self.assertEqual(search.search('"owned phrase"', "new"), [{"url": URL}])
        with self.assertRaises(WebSearchError) as caught:
            search.search('"new unsaved phrase"', "other")
        self.assertEqual(caught.exception.code, "saved_search_cache_miss")
        self.assertEqual(search.missing_queries, ['"new unsaved phrase"'])

    def test_default_page_cache_never_instantiates_a_network_fetcher(self):
        with patch("scripts.replay_saved_dev_search.Fetcher") as network:
            cached = CachedPages({URL: page()}, {URL, CHILD}, cache_directory=self.root)
            self.assertNotIn("links", cached.fetch(URL))
            with self.assertRaises(FetchError) as caught:
                cached.fetch(CHILD)
            self.assertEqual(caught.exception.code, "saved_page_cache_miss")
            network.assert_not_called()
        self.assertEqual(cached.fresh_attempts, 0)
        self.assertEqual(cached.missing_links, {URL})

    def test_known_link_discoveries_are_flags_and_never_invent_anchor_labels(self):
        cached = CachedPages({URL: page()}, {URL}, cache_directory=self.root,
                             known_parent_links={URL: {CHILD}})
        self.assertNotIn("links", cached.fetch(URL))
        self.assertEqual(cached.known_parents_missing_links, {URL: [CHILD]})
        search = FrozenSearch({})
        report = {"web": {"status": "complete", "errors": []}, "issues": [],
                  "stats": {"total_words": 10, "matched_words": 3, "uniqueness_percent": 70.0}}
        annotate_replay(report, search, cached, {})
        self.assertEqual(report["web"]["status"], "partial")
        self.assertIsNone(report["stats"]["uniqueness_percent"])
        self.assertFalse(report["offline_replay"]["replay_metadata_complete"])

    def test_explicit_refresh_preserves_real_links_and_authorizes_only_discovered_children(self):
        real = Mock()
        real.fetch.return_value = page(links=[{"url": CHILD, "text": "A real preserved anchor"}])
        cached = CachedPages({URL: page(), CHILD: page(CHILD)}, {URL}, cache_directory=self.root,
                             refresh_missing_links=True, known_parent_links={URL: {CHILD}}, fetcher=real)
        self.assertEqual(cached.fetch(URL)["links"][0]["text"], "A real preserved anchor")
        self.assertEqual(cached.fetch(CHILD)["url"], CHILD)
        self.assertEqual(real.fetch.call_count, 1)
        with self.assertRaises(FetchError) as caught:
            cached.fetch("https://example.org/gold-never-discovered")
        self.assertEqual(caught.exception.code, "saved_url_not_discovered")

    def test_page_intent_is_saved_before_fetch_and_failed_outcome_is_never_retried(self):
        path = self.root / (sha256(URL.encode()).hexdigest() + ".json")
        def fail(url):
            self.assertEqual(json.loads(path.read_text())["state"], "submitted")
            raise FetchError("Owned timeout", "fetch_timeout")
        real = Mock()
        real.fetch.side_effect = fail
        cached = CachedPages({}, {URL}, cache_directory=self.root, fill_missing=True, fetcher=real)
        with self.assertRaises(FetchError):
            cached.fetch(URL)
        retry = CachedPages({}, {URL}, cache_directory=self.root, fill_missing=True, fetcher=real)
        with self.assertRaises(FetchError) as caught:
            retry.fetch(URL)
        self.assertEqual(caught.exception.code, "fetch_timeout")
        self.assertEqual(real.fetch.call_count, 1)

    def test_uncertain_page_intent_is_retained_even_in_fill_mode(self):
        path = self.root / (sha256(URL.encode()).hexdigest() + ".json")
        dump(path, {"requested_url": URL, "state": "submitted"})
        real = Mock()
        cached = CachedPages({}, {URL}, cache_directory=self.root, fill_missing=True, fetcher=real)
        with self.assertRaises(FetchError) as caught:
            cached.fetch(URL)
        self.assertEqual(caught.exception.code, "saved_page_outcome_unknown")
        real.fetch.assert_not_called()

    def test_missing_per_query_files_are_never_reconstructed_from_flat_urls(self):
        digest = "b" * 64
        dump(self.root / "D01-request.json", {"request_id": "a" * 32, "text_sha256": digest, "planned_queries": 2})
        dump(self.root / "D01.json", {"web": {"discovered_urls": [URL]}})
        records, metadata = load_queries(self.root, "D01", digest)
        self.assertEqual(records, {})
        self.assertEqual(metadata["status"], "per_query_replies_not_saved")


if __name__ == "__main__":
    unittest.main()
