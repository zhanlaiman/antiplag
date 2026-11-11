from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from plagiarism.web_fetch import FetchError
from scripts.replay_final_url_policies import BaselineFills, FinalPages, actual_outcomes


URL = "https://example.test/request"
FINAL = "https://example.test/article/"
TEXT = "Редактор подготовил независимое описание расписания школьной библиотеки перед открытием выставки."


def success():
    return {"state": "complete", "requested_url": URL, "_artifact": "authored.json", "_artifact_sha256": "a" * 64,
            "_case_id": "D01", "_report_id": "authored-report", "fetched": {"url": FINAL, "text": TEXT,
                "text_sha256": sha256(TEXT.encode()).hexdigest(), "links": []}}


class FinalUrlReplayTests(unittest.TestCase):
    def test_exact_numbered_fetch_segment_must_agree_with_the_actual_attempt_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {"id": "authored-report", "web": {"attempted_urls": [URL], "urls_attempted": 1, "errors": []}}
            (root / "D01.json").write_text(json.dumps(report))
            path = root / "page-0001-aaaaaaaaaaaaaaaa-fetch.json"
            path.write_text(json.dumps(success()))
            # Use a local root for the manifest's display paths only.
            from unittest.mock import patch
            with patch("scripts.replay_final_url_policies.ROOT", root):
                loaded = actual_outcomes(root, ("D01",))
                self.assertEqual(loaded["D01"]["records"][URL]["fetched"]["url"], FINAL)
                changed = success()
                changed["requested_url"] = "https://example.test/other"
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    actual_outcomes(root, ("D01",))

    def test_own_failed_outcome_is_not_replaced_by_other_cases_success(self):
        failed = {**success(), "state": "failed_or_unknown", "_typed_report_code": "robots_denied"}
        pages = FinalPages({URL: failed}, {URL: success()}, {URL})
        with self.assertRaises(FetchError) as raised:
            pages.fetch(URL)
        self.assertEqual(raised.exception.code, "robots_denied")
        self.assertEqual(pages.cached_page_loads, 0)
        self.assertEqual(pages.saved_failures[0]["code"], "robots_denied")

    def test_uncaptured_content_stays_unknown_instead_of_becoming_an_empty_page(self):
        pages = FinalPages({}, {}, {URL})
        with self.assertRaises(FetchError) as raised:
            pages.fetch(URL)
        self.assertEqual(raised.exception.code, "saved_page_cache_miss")
        self.assertEqual(pages.cache_misses, [URL])
        self.assertEqual(pages.page_load_events[0]["state"], "unknown_content")
        self.assertEqual(pages.fresh_attempts, 0)

    def test_real_saved_redirect_alias_and_source_hash_are_preserved(self):
        saved = success()
        pages = FinalPages({}, {FINAL: saved}, {FINAL})
        page = pages.fetch(FINAL)
        self.assertEqual(page["text"], TEXT)
        self.assertEqual(page["text_sha256"], sha256(TEXT.encode()).hexdigest())
        self.assertEqual(pages.page_load_events[0]["captured_report_id"], "authored-report")
        saved["fetched"]["text"] = TEXT + " Изменение."
        with self.assertRaises(ValueError):
            FinalPages({}, {FINAL: saved}, {FINAL}).fetch(FINAL)

    def test_fill_intent_precedes_get_and_cumulative_cap_survives_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "fills"
            calls = []
            class Pages:
                def fetch(self, url):
                    calls.append(url)
                    intent = json.loads((cache / (sha256(url.encode()).hexdigest() + ".json")).read_text())
                    self_state = intent["state"]
                    if self_state != "submitted":
                        raise AssertionError("GET was not preceded by durable intent")
                    return success()["fetched"]
            from unittest.mock import patch
            with patch("scripts.replay_final_url_policies.ROOT", root):
                fills = BaselineFills(cache, enabled=True, limit=1, fetcher=Pages())
                record, fresh = fills.get(URL, {"case_id": "D03"}, True)
                self.assertTrue(fresh)
                self.assertEqual(record["state"], "complete")
                reopened = BaselineFills(cache, enabled=True, limit=1, fetcher=Pages())
                _, fresh = reopened.get(URL, {"case_id": "D03"}, True)
                self.assertFalse(fresh)
                self.assertEqual(reopened.get("https://example.test/unseen", {"case_id": "D03"}, True), (None, False))
                self.assertTrue(reopened.cap_exhausted)
                self.assertEqual(calls, [URL])

    def test_unknown_fill_intent_is_never_resubmitted_and_stays_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "fills"
            cache.mkdir()
            intent = {"requested_url": URL, "state": "submitted", "discovery": {"case_id": "D03"}}
            (cache / (sha256(URL.encode()).hexdigest() + ".json")).write_text(json.dumps(intent))
            from unittest.mock import Mock, patch
            fetcher = Mock()
            with patch("scripts.replay_final_url_policies.ROOT", root):
                fills = BaselineFills(cache, enabled=True, limit=48, fetcher=fetcher)
                pages = FinalPages({}, {}, {URL}, fills=fills, allow_fill=True, case_id="D03", search_urls={URL})
                with self.assertRaises(FetchError) as raised:
                    pages.fetch(URL)
                self.assertEqual(raised.exception.code, "saved_page_outcome_unknown")
                self.assertEqual(pages.cache_misses, [URL])
                fetcher.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
