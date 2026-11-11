import copy
from pathlib import Path
import tempfile
import unittest
from hashlib import sha256
import json
from unittest.mock import patch

from scripts.collect_selected_dev_review import pair_signature
from scripts.summarize_night_results import (FINAL_BATCHES, DEV_KEY_SHA, batch_progress,
                                           batch_search_expense, refresh_draft, source_pair_transitions)


class SelectedDevReviewTests(unittest.TestCase):
    def setUp(self):
        self.match = {"kind": "exact", "source_id": "authored", "source_url": "https://example.test/source",
            "start": 4, "end": 24, "source_start": 0, "source_end": 20,
            "excerpt": "An authored fragment", "source_excerpt": "An authored fragment"}

    def test_review_signature_binds_both_excerpts_and_source_bounds(self):
        original = pair_signature("D11", "saved-report", self.match)
        for field, value in (("excerpt", "A different target"), ("source_excerpt", "A different source"),
                             ("source_start", 1), ("source_url", "https://example.test/other")):
            changed = {**self.match, field: value}
            self.assertNotEqual(pair_signature("D11", "saved-report", changed), original)
        self.assertNotEqual(pair_signature("D11", "new-report", self.match), original)

    def test_review_signature_is_stable_under_dictionary_order(self):
        reversed_keys = dict(reversed(list(self.match.items())))
        self.assertEqual(pair_signature("D11", "saved-report", self.match), pair_signature("D11", "saved-report", reversed_keys))

    def test_final_review_signature_requires_the_same_complete_source_snapshot(self):
        original = pair_signature("D11", "saved-report", self.match, "a" * 64)
        self.assertNotEqual(original, pair_signature("D11", "saved-report", self.match, "b" * 64))
        self.assertNotEqual(original, pair_signature("D11", "saved-report", self.match))

    def test_final_batches_partition_all_thirty_without_legacy_fallback(self):
        self.assertEqual([cid for _, ids in FINAL_BATCHES for cid in ids], [f"D{i:02d}" for i in range(1, 31)])
        self.assertTrue(all(len(ids) == 6 for _, ids in FINAL_BATCHES))
        self.assertTrue(all(name.startswith("dev-final-selected-") for name, _ in FINAL_BATCHES))

    def test_saved_expense_includes_submitted_calls_and_rejects_unbound_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            location = base / "dev-final-selected-a-v1"
            location.mkdir()
            run = {"case_ids": ["D01"], "input_key_sha256": DEV_KEY_SHA, "planned_queries": {"D01": 6}}
            (location / "run.json").write_text(json.dumps(run))
            identifier = "a" * 32
            (location / "D01-request.json").write_text(json.dumps({"request_id": identifier, "planned_queries": 6}))
            call_id = sha256((identifier + "\0" + "0").encode()).hexdigest()
            search_path = location / (call_id + "-search.json")
            search_path.write_text(json.dumps({"request_id": call_id, "state": "submitted"}))
            with patch("scripts.summarize_night_results.BASE", base), patch("scripts.summarize_night_results.ROOT", base):
                expense, _ = batch_search_expense(location.name, ("D01",), [])
                self.assertEqual(expense["query_attempts"], 1)
                self.assertEqual(expense["queries_in_completed_reports"], 0)
                self.assertEqual(expense["records_with_credit_usage"], 0)
                self.assertEqual(batch_progress(location.name, ("D01",), [])["submitted_without_report"], ["D01"])
                search_path.write_text(json.dumps({"request_id": "b" * 64, "state": "complete", "usage": {"credits": 1}}))
                with self.assertRaises(ValueError):
                    batch_search_expense(location.name, ("D01",), [])

    def test_progress_draft_does_not_claim_pending_cases_completed(self):
        summary = {"saved_documents": 2, "expected_source_pairs_discovered": 1, "expected_source_pairs": 1,
            "expected_source_pairs_read": 1, "fully_highlighted_any_source": 1, "lexical_fragments": 1,
            "lexical_documents": 1, "copied_words_highlighted_any_source": 6, "copied_words": 6,
            "confirmed_false_similar_source_pairs": 0, "documents_without_read_pages": 0, "network_queries_this_run": 2}
        result = {"latest_fresh_union_completed": {"before": summary, "after": copy.deepcopy(summary),
            "pending_report_ids": ["D03"], "lost_fragments": []},
            "expenses": {"new_attempts_in_resumed_work": 2, "attempts_since_initial30_started": 2,
                "monthly_ledger_attempts": 4, "attempts_before_initial30": 2,
                "quota_snapshot": {"plan_limit": 1000, "paygo_usage": 0}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.md"
            path.write_text("Draft\n<!-- selected-fresh-start -->old<!-- selected-fresh-end -->\n"
                            "<!-- expense-start -->old<!-- expense-end -->")
            refresh_draft(result, path)
            text = path.read_text()
            self.assertIn("2/30 DEV", text)
            self.assertIn("Ожидаются новые отчёты ещё по 1 случаям: D03", text)
            self.assertNotIn("Все 30 имеют новый последний отчёт", text)

    def test_source_losses_count_unique_document_source_pairs(self):
        def row(discovered, read):
            e = {"source_id": "same-source", "expected_url_discovered": discovered, "expected_url_read": read}
            return {"id": "D09", "status": "saved", "evidence": [copy.deepcopy(e), copy.deepcopy(e)]}
        result = source_pair_transitions({"cases": [row(True, True)]}, {"cases": [row(True, False)]})
        self.assertEqual(result["expected_url_discovered"]["lost"], [])
        self.assertEqual(result["expected_url_read"]["lost"], [{"id": "D09", "source_id": "same-source"}])


if __name__ == "__main__":
    unittest.main()
