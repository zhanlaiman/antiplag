"""Saved replay must never silently search, fetch, or invent source links."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import replay_saved_dev_search as replay
from scripts.replay_saved_dev_search import CachedPages, FrozenSearch, annotate_replay, dump, load_queries, load_source_pool, report_source_pages
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

    def pdf_report(self):
        original = TEXT.replace("material", "mate rial")
        primary = {"id": "primary", "url": URL, "title": "Owned PDF", "text": original,
                   "provenance": {"text_sha256": sha256(original.encode()).hexdigest()}}
        view = {"id": "primary-pdfium-v1", "representation_of": "primary", "url": URL,
                "title": "Owned PDF", "text": TEXT,
                "provenance": {"text_sha256": sha256(TEXT.encode()).hexdigest(), "parser_version": "5.14.0",
                    "extraction": "pdfium-text-v1", "raw_pdf_sha256": "a" * 64, "pdf_pages": 1}}
        return {"id": "owned-report", "sources": [view, primary], "web": {
            "optional_pdf_status": [{"url": URL, "engine": "pdfium", "status": "complete", "code": None}]}}

    def test_flat_same_url_snapshots_restore_primary_and_a_bounded_supplement(self):
        from plagiarism import web_search
        from plagiarism.pdf_evidence import analyze_with_supplements
        report = self.pdf_report()
        restored = report_source_pages(report, {"source": "owned"})
        self.assertEqual(len(restored), 1)
        page = restored[URL]
        self.assertEqual(page["text"], report["sources"][1]["text"])
        self.assertEqual(page["supplemental_texts"][0]["text"], TEXT)
        self.assertEqual(page["supplemental_texts"][0]["page_count"], 1)
        self.assertEqual(page["optional_status"]["status"], "complete")
        primary = report["sources"][1]
        views = web_search._supplemental_source_views(primary, page)
        self.assertEqual(len(views), 1)
        result = analyze_with_supplements(TEXT, [primary], views)
        self.assertEqual(result["stats"]["source_count"], 1)
        self.assertEqual(result["stats"]["checked_source_count"], 1)
        self.assertTrue(any(m["kind"] == "exact" and m["excerpt"] == TEXT.rstrip(".") for m in result["matches"]))

    def test_latest_primary_without_supplement_does_not_inherit_an_older_view(self):
        baseline, dense = self.root / "baseline", self.root / "dense"
        dump(dense / "run.json", {"case_ids": ["D01"]})
        old = self.pdf_report()
        dump(baseline / "D01.json", old)
        latest = deepcopy(old["sources"][1])
        latest["text"] = "A newer independently saved source body has no optional PDF view attached."
        latest["provenance"]["text_sha256"] = sha256(latest["text"].encode()).hexdigest()
        dump(dense / "D01.json", {"id": "latest", "sources": [latest]})
        with patch.object(replay, "ROOT", self.root):
            pool, _, _ = load_source_pool(baseline, dense)
        self.assertEqual(pool[URL]["text"], latest["text"])
        self.assertNotIn("supplemental_texts", pool[URL])

    def test_orphan_view_cannot_become_a_primary_page(self):
        report = self.pdf_report()
        report["sources"] = report["sources"][:1]
        with self.assertRaises(ValueError):
            report_source_pages(report, {})


if __name__ == "__main__":
    unittest.main()
