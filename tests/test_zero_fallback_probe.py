"""Authored offline evidence tests; no corpus, provider or page-fetch calls."""
from copy import deepcopy
from hashlib import sha256
import unittest
from unittest.mock import Mock

from plagiarism import web_search as web
from plagiarism.matching import _tokens, analyze
from plagiarism.pdf_evidence import PrimarySupport, analyze_with_supplements
from plagiarism.web_fetch import FetchError
from scripts.dev_report_provenance import source_snapshot_signatures
from scripts.probe_zero_fallback import extend_report, grounded_exact_words
from scripts.zero_fallback_fixtures import (FIRST9, SECOND9, FIRST_PAGE, NEGATIVE_PAIRS,
    negative_fixture, page, two_insertion_fixture)


def source_from_page(fetched):
    return {"id": "web-" + sha256(fetched["url"].encode()).hexdigest()[:24],
        "title": fetched["title"], "url": fetched["url"], "text": fetched["text"],
        "provenance": {"origin": "web_fetch", "fetched_at": fetched["fetched_at"],
            "text_sha256": fetched["text_sha256"], "license_status": "not_verified", "training_consent": False}}


def original_report(fixture, *, supplemental=False):
    sources = [source_from_page(fetched) for fetched in fixture["prefix_pages"]]
    views = []
    if supplemental:
        parent = sources[0]
        views.append({**deepcopy(parent), "id": parent["id"] + "-pdfium-v1", "representation_of": parent["id"],
                      "title": "Independent authored optional text view"})
    text = fixture["text"]
    result = analyze_with_supplements(text, sources, views)
    result["stats"].update(web._word_coverage(text, result["matches"]), uniqueness_percent=None)
    result.update(text=text, sources=sources + views, scope="web_search", web={
        "status": "partial", "query_strategy": web.DENSE_STRATEGY, "url_strategy": web.PATH_URL_STRATEGY,
        "queries_planned": 6, "queries_attempted": 6, "queries_completed": 6,
        "attempted_urls": [fetched["url"] for fetched in fixture["prefix_pages"]], "urls_attempted": 12,
        "sources_checked": 12, "supplemental_text_views": len(views),
        "discovered_urls": list(fixture["search_order"]), "urls_outside_limit": 14,
        "errors": [{"stage": "limit", "code": "url_limit_reached", "count": 14}],
        "limitations": ["Загрузка не более двенадцати страниц.", "Недоступные страницы остаются неизвестными."]})
    return result


def lookup(fixture, *, absent=(), failed=()):
    calls = []
    absent, failed = set(absent), set(failed)

    def read(url):
        calls.append(url)
        if url in absent or url not in fixture["pages"]:
            raise FetchError("Authored cache miss", "saved_page_cache_miss")
        if url in failed:
            raise FetchError("Authored saved robots failure", "robots_denied")
        return deepcopy(fixture["pages"][url])

    return read, calls


def report_with_claimed_pdf_view(fixture, primary_text):
    original = original_report(fixture)
    primary = original["sources"][0]
    primary["text"] = primary_text
    primary["provenance"]["text_sha256"] = sha256(primary_text.encode()).hexdigest()
    view = {**deepcopy(primary), "id": primary["id"] + "-pdfium-v1", "representation_of": primary["id"],
            "text": FIRST_PAGE}
    view["provenance"]["text_sha256"] = sha256(FIRST_PAGE.encode()).hexdigest()
    original["sources"].append(view)
    original["matches"] = [match for match in analyze(fixture["text"], [view])["matches"] if match["kind"] == "exact"]
    original["stats"]["matched_words"] = 9
    original["web"]["supplemental_text_views"] = 1
    return original


class ZeroFallbackProbeTests(unittest.TestCase):
    def assert_prefix_preserved(self, original, before, report, audit):
        self.assertEqual(original, before)
        self.assertEqual(report["sources"][:len(before["sources"])], before["sources"])
        self.assertTrue(audit["prefix_preserved"])
        signatures = source_snapshot_signatures(report)
        for identifier, expected in source_snapshot_signatures(before).items():
            self.assertEqual(signatures[identifier], expected)
        for key in ("queries_planned", "queries_attempted", "queries_completed", "query_strategy"):
            self.assertEqual(report["web"][key], before["web"][key])

    def test_three_independent_topic_negatives_remain_zero_after_all_twelve_extras(self):
        for topic in NEGATIVE_PAIRS:
            with self.subTest(topic=topic):
                fixture = negative_fixture(topic)
                original = original_report(fixture)
                before = deepcopy(original)
                read, calls = lookup(fixture)
                report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
                self.assertTrue(audit["triggered"])
                self.assertEqual(audit["initial_gate_words"], 0)
                self.assertEqual(audit["grounded_words_after"], 0)
                self.assertEqual(report["stats"]["matched_words"], 0)
                self.assertEqual(calls, fixture["search_order"][12:24])
                self.assertEqual(audit["extra_attempted_urls"], calls)
                self.assertEqual(report["web"]["urls_attempted"], 24)
                self.assertEqual(report["web"]["status"], "partial")
                self.assertIsNone(report["stats"]["uniqueness_percent"])
                self.assert_prefix_preserved(original, before, report, audit)

    def test_both_nine_word_insertions_are_grounded_and_first_match_does_not_stop_phase(self):
        fixture = two_insertion_fixture()
        original = original_report(fixture)
        before = deepcopy(original)
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
        self.assertEqual(len(_tokens(FIRST9)), 9)
        self.assertEqual(len(_tokens(SECOND9)), 9)
        self.assertEqual(audit["initial_gate_words"], 0)
        self.assertEqual(audit["grounded_words_after"], 18)
        self.assertEqual(report["stats"]["matched_words"], 18)
        self.assertEqual(calls, fixture["search_order"][12:24])
        self.assertEqual(report["web"]["urls_attempted"], 24)
        by_id = {source["id"]: source for source in report["sources"]}
        exact = [match for match in report["matches"] if match["kind"] == "exact"]
        for phrase, url in ((FIRST9, fixture["search_order"][12]), (SECOND9, fixture["search_order"][13])):
            match = next(match for match in exact if match["excerpt"] == phrase)
            source = by_id[match["source_id"]]
            self.assertEqual(source["url"], url)
            self.assertEqual(source["text"][match["source_start"]:match["source_end"]], phrase)
            self.assertEqual(fixture["text"][match["start"]:match["end"]], phrase)
        self.assertTrue(audit["initial_gate_only_continue_after_extra_match"])
        self.assertIsNone(report["stats"]["uniqueness_percent"])
        self.assert_prefix_preserved(original, before, report, audit)

    def test_initial_positive_opens_no_extras_and_leaves_second_insertion_unread(self):
        fixture = two_insertion_fixture(initial_positive=True)
        original = original_report(fixture)
        before = deepcopy(original)
        read = Mock(side_effect=AssertionError("Positive prefix must not open the fallback"))
        report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
        read.assert_not_called()
        self.assertFalse(audit["triggered"])
        self.assertEqual(audit["initial_gate_words"], 9)
        self.assertEqual(audit["extra_attempted_urls"], [])
        self.assertEqual(report, before)
        self.assertEqual(grounded_exact_words(fixture["text"], report), 9)
        self.assertNotIn(fixture["search_order"][13], report["web"]["attempted_urls"])
        self.assertFalse(any(match["kind"] == "exact" and match["excerpt"] == SECOND9 for match in report["matches"]))
        self.assertEqual(report["web"]["urls_outside_limit"], 14)
        self.assert_prefix_preserved(original, before, report, audit)

    def test_original_snapshot_hashes_ids_and_optional_views_stay_unchanged(self):
        fixture = two_insertion_fixture()
        original = original_report(fixture, supplemental=True)
        before = deepcopy(original)
        read, _ = lookup(fixture)
        report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
        self.assertEqual(report["web"]["sources_checked"], 24)
        self.assertEqual(report["web"]["supplemental_text_views"], 1)
        self.assertEqual([source for source in report["sources"] if source.get("representation_of")],
                         [before["sources"][-1]])
        self.assert_prefix_preserved(original, before, report, audit)

    def test_extra_pool_oracle_page_not_in_search_results_is_ignored(self):
        fixture = negative_fixture("astronomy")
        oracle = "https://authored-zero.test/oracle-not-discovered"
        fixture["pages"][oracle] = page(oracle, fixture["text"], "Unavailable source oracle")
        original = original_report(fixture)
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
        self.assertNotIn(oracle, calls)
        self.assertNotIn(oracle, [source["url"] for source in report["sources"]])
        self.assertEqual(audit["grounded_words_after"], 0)

    def test_extra_page_links_cannot_add_recursive_oracle_fetches(self):
        fixture = negative_fixture("astronomy")
        oracle = "https://authored-zero.test/linked-oracle"
        fixture["pages"][oracle] = page(oracle, fixture["text"])
        fixture["pages"][fixture["search_order"][12]]["links"] = [{"url": oracle, "text": fixture["text"]}]
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original_report(fixture), fixture["search_order"], read)
        self.assertEqual(calls, fixture["search_order"][12:24])
        self.assertNotIn(oracle, calls)
        self.assertTrue(audit["no_new_queries"])
        self.assertTrue(audit["no_recursive_followups"])
        self.assertEqual(audit["network_calls"], 0)
        self.assertEqual(report["stats"]["matched_words"], 0)

    def test_unknown_extra_is_explicit_and_never_empty_content_or_uniqueness_proof(self):
        fixture = negative_fixture("archive")
        missing = fixture["search_order"][12]
        original = original_report(fixture)
        read, calls = lookup(fixture, absent=(missing,))
        report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
        self.assertEqual(audit["unknown_extra_urls"], [missing])
        self.assertEqual(audit["events"][0]["state"], "unknown_content")
        self.assertNotIn(missing, [source["url"] for source in report["sources"]])
        self.assertEqual(report["web"]["sources_checked"], 23)
        self.assertEqual(report["stats"]["matched_words"], 0)
        self.assertIsNone(report["stats"]["uniqueness_percent"])
        self.assertEqual(report["web"]["status"], "partial")
        self.assertEqual(calls, fixture["search_order"][12:24])
        self.assertTrue(any(error.get("url") == missing and error.get("code") == "saved_page_cache_miss"
                            for error in report["web"]["errors"]))

    def test_saved_fetch_failure_counts_attempt_and_cannot_claim_empty_page(self):
        fixture = negative_fixture("print")
        blocked = fixture["search_order"][12]
        read, calls = lookup(fixture, failed=(blocked,))
        report, audit = extend_report(fixture["text"], original_report(fixture), fixture["search_order"], read)
        self.assertEqual(audit["failed_extra_urls"], [blocked])
        self.assertEqual(audit["unknown_extra_urls"], [])
        self.assertEqual(audit["events"][0]["code"], "robots_denied")
        self.assertEqual(len(calls), 12)
        self.assertEqual(report["web"]["urls_attempted"], 24)
        self.assertIsNone(report["stats"]["uniqueness_percent"])

    def test_fabricated_nonzero_stats_do_not_close_a_zero_evidence_gate(self):
        fixture = two_insertion_fixture()
        original = original_report(fixture)
        self.assertEqual(original["matches"], [])
        original["stats"].update(matched_words=999, matched_word_percent=100, uniqueness_percent=0)
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
        self.assertEqual(audit["initial_gate_words"], 0)
        self.assertTrue(audit["triggered"])
        self.assertEqual(len(calls), 12)
        self.assertEqual(report["stats"]["matched_words"], 18)

    def test_exact_label_with_unproven_source_slice_does_not_close_gate(self):
        fixture = two_insertion_fixture()
        original = original_report(fixture)
        forged = analyze(fixture["text"], [{"id": "external-claim", "text": FIRST_PAGE}])["matches"][0]
        forged["source_id"] = original["sources"][0]["id"]
        original["matches"] = [forged]
        original["stats"]["matched_words"] = 9
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
        self.assertTrue(audit["triggered"])
        self.assertEqual(audit["initial_gate_words"], 0)
        self.assertEqual(len(calls), 12)
        self.assertEqual(report["stats"]["matched_words"], 18)

    def test_actual_but_lexically_different_source_and_target_slices_are_not_proof(self):
        fixture = two_insertion_fixture()
        original = original_report(fixture)
        source = original["sources"][0]
        target_start = fixture["text"].index(FIRST9)
        source_end = _tokens(source["text"])[8].end
        original["matches"] = [{"kind": "exact", "source_id": source["id"],
            "start": target_start, "end": target_start + len(FIRST9), "excerpt": FIRST9,
            "source_start": 0, "source_end": source_end, "source_excerpt": source["text"][:source_end]}]
        original["stats"]["matched_words"] = 9
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
        self.assertEqual(audit["initial_gate_words"], 0)
        self.assertTrue(audit["triggered"])
        self.assertEqual(len(calls), 12)
        self.assertEqual(report["stats"]["matched_words"], 18)

    def test_changed_original_source_hash_raises_before_any_lookup(self):
        fixture = negative_fixture("astronomy")
        original = original_report(fixture)
        original["sources"][0]["text"] += " Изменение после сохранения."
        read = Mock(side_effect=AssertionError("Corrupt prefix must fail before extra reading"))
        with self.assertRaisesRegex(ValueError, "source text hash"):
            extend_report(fixture["text"], original, fixture["search_order"], read)
        read.assert_not_called()

    def test_negative_python_spans_are_quarantined_even_when_python_slices_match(self):
        fixture = two_insertion_fixture(initial_positive=True)
        original = original_report(fixture)
        match = next(match for match in original["matches"] if match["kind"] == "exact")
        source = next(source for source in original["sources"] if source["id"] == match["source_id"])
        match["start"] -= len(fixture["text"])
        match["source_start"] -= len(source["text"])
        original["matches"] = [match]
        self.assertEqual(fixture["text"][match["start"]:match["end"]], match["excerpt"])
        self.assertEqual(source["text"][match["source_start"]:match["source_end"]], match["source_excerpt"])
        self.assertEqual(grounded_exact_words(fixture["text"], original), 0)
        before = deepcopy(original)
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
        self.assertTrue(audit["triggered"])
        self.assertEqual(audit["initial_gate_words"], 0)
        self.assertEqual(len(calls), 12)
        self.assertEqual(audit["grounded_words_after"], 18)
        self.assert_prefix_preserved(original, before, report, audit)

    def test_highlight_source_url_mismatch_cannot_close_gate_with_real_equal_slices(self):
        fixture = two_insertion_fixture(initial_positive=True)
        original = original_report(fixture)
        match = next(match for match in original["matches"] if match["kind"] == "exact")
        match["source_url"] = "https://authored-zero.test/wrong-snapshot-url"
        original["matches"] = [match]
        self.assertEqual(grounded_exact_words(fixture["text"], original), 0)
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
        self.assertTrue(audit["triggered"])
        self.assertEqual(audit["initial_gate_words"], 0)
        self.assertEqual(len(calls), 12)
        self.assertEqual(audit["grounded_words_after"], 18)

    def test_forged_pdf_view_without_primary_ordered_support_does_not_close_gate(self):
        fixture = two_insertion_fixture()
        primary_bodies = (fixture["prefix_pages"][0]["text"],
                          "Переставленный первичный текст: " + " ".join(reversed(FIRST9.split())) + ".")
        for primary_text in primary_bodies:
            with self.subTest(primary_text=primary_text):
                original = report_with_claimed_pdf_view(fixture, primary_text)
                before = deepcopy(original)
                self.assertEqual(len(original["matches"]), 1)
                self.assertEqual(grounded_exact_words(fixture["text"], original), 0)
                read, calls = lookup(fixture)
                report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
                self.assertTrue(audit["triggered"])
                self.assertEqual(audit["initial_gate_words"], 0)
                self.assertEqual(len(calls), 12)
                self.assertEqual(audit["grounded_words_after"], 18)
                self.assertFalse(any(match["source_id"].endswith("-pdfium-v1") for match in report["matches"]))
                self.assert_prefix_preserved(original, before, report, audit)

    def test_supported_pdf_view_needs_explicit_primary_proof_and_valid_proof_closes_gate(self):
        fixture = two_insertion_fixture()
        primary_text = "Слитное извлечение: " + FIRST9.replace(" ", "") + ". Конец страницы."
        for attach_proof in (False, True):
            with self.subTest(attach_proof=attach_proof):
                original = report_with_claimed_pdf_view(fixture, primary_text)
                primary = original["sources"][0]
                match = original["matches"][0]
                proof = PrimarySupport(primary["text"], primary["id"]).evidence(match)
                self.assertIsNotNone(proof)
                if attach_proof:
                    match["pdf_spacing_evidence"] = proof
                before = deepcopy(original)
                read, calls = lookup(fixture)
                report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
                self.assertEqual(audit["initial_gate_words"], 9 if attach_proof else 0)
                self.assertEqual(audit["triggered"], not attach_proof)
                self.assertEqual(len(calls), 0 if attach_proof else 12)
                if attach_proof:
                    self.assertEqual(report, before)
                else:
                    self.assertEqual(audit["grounded_words_after"], 18)
                self.assert_prefix_preserved(original, before, report, audit)

    def test_tampered_extra_body_hash_is_failed_and_other_pages_are_still_scanned(self):
        fixture = two_insertion_fixture()
        corrupt = fixture["search_order"][12]
        fixture["pages"][corrupt]["text_sha256"] = "0" * 64
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original_report(fixture), fixture["search_order"], read)
        self.assertEqual(audit["failed_extra_urls"], [corrupt])
        self.assertEqual(len(calls), 12)
        self.assertEqual(audit["grounded_words_after"], 9)
        self.assertNotIn(corrupt, [source["url"] for source in report["sources"]])

    def test_duplicate_search_urls_do_not_spend_extra_slots(self):
        fixture = two_insertion_fixture()
        order = [fixture["search_order"][0] + "#duplicate-prefix", *fixture["search_order"],
                 fixture["search_order"][12] + "#duplicate-extra"]
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original_report(fixture), order, read)
        self.assertEqual(calls, fixture["search_order"][12:24])
        self.assertEqual(report["web"]["urls_attempted"], 24)
        self.assertEqual(audit["grounded_words_after"], 18)

    def test_discovered_pages_beyond_twenty_four_stay_unread_even_if_they_contain_everything(self):
        fixture = two_insertion_fixture()
        outside = fixture["search_order"][24]
        fixture["pages"][outside] = page(outside, fixture["text"])
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original_report(fixture), fixture["search_order"], read)
        self.assertNotIn(outside, calls)
        self.assertNotIn(fixture["search_order"][25], calls)
        self.assertEqual(audit["grounded_words_after"], 18)
        self.assertLess(report["stats"]["matched_words"], report["stats"]["total_words"])
        self.assertEqual(report["web"]["attempted_urls"][:12], fixture["search_order"][:12])

    def test_twenty_six_discovered_and_twenty_four_attempts_leave_two_current_unread_urls(self):
        fixture = two_insertion_fixture()
        original = original_report(fixture)
        original["web"]["skipped_urls"] = list(fixture["search_order"][12:])
        before = deepcopy(original)
        read, calls = lookup(fixture)
        report, audit = extend_report(fixture["text"], original, fixture["search_order"], read)
        remaining = fixture["search_order"][24:]
        self.assertEqual(len(fixture["search_order"]), 26)
        self.assertEqual(len(calls), 12)
        self.assertEqual(report["web"]["urls_attempted"], 24)
        self.assertEqual(audit["remaining_search_urls"], remaining)
        self.assertEqual(audit["current_unattempted_search_count"], 2)
        self.assertEqual(report["web"]["skipped_urls"], remaining)
        self.assertEqual(report["web"]["urls_outside_limit"], 2)
        limits = [error for error in report["web"]["errors"] if error.get("code") == "url_limit_reached"]
        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0]["count"], 2)
        preserved = report["web"]["first_phase_url_limit_metadata"]
        self.assertEqual(preserved["urls_outside_limit"], 14)
        self.assertEqual(preserved["skipped_urls"], fixture["search_order"][12:])
        self.assertEqual(preserved["limit_errors"][0]["count"], 14)
        self.assert_prefix_preserved(original, before, report, audit)

    def test_changed_target_or_oversized_initial_phase_is_rejected_without_lookup(self):
        fixture = negative_fixture("print")
        original = original_report(fixture)
        read = Mock(side_effect=AssertionError("Invalid original phase must be rejected"))
        with self.assertRaises(ValueError):
            extend_report(fixture["text"] + " changed", original, fixture["search_order"], read)
        original["web"]["attempted_urls"].append(fixture["search_order"][12])
        original["web"]["urls_attempted"] = 13
        with self.assertRaises(ValueError):
            extend_report(fixture["text"], original, fixture["search_order"], read)
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
