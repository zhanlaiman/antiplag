import hashlib
import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.analyze_web_dev_checks import compare, inspect, copied_target_tokens
from plagiarism.matching import _tokens


class SavedDevInspectionTests(unittest.TestCase):
    """Small authored fixtures: no corpus text, network, or held-out material."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.dev = self.base / "dev"
        self.reports = self.base / "reports"
        self.dev.mkdir()
        self.reports.mkdir()
        self.prefix = "Вводная авторская строка поясняет музейную карточку для любознательного читателя. "
        self.quote = "Синий фонарь освещает тихую лестницу рядом со старой деревянной дверью"
        self.text = self.prefix + self.quote
        self.span = [len(self.prefix), len(self.text)]
        self.cases = [{"id": f"D{i:02d}", "split": "dev", "scenario": "independent", "reviewed": False,
                       "input_path": f"D{i:02d}.txt", "text_sha256": hashlib.sha256(self.text.encode()).hexdigest(),
                       "expected_evidence": []} for i in range(1, 31)]
        self.case = self.cases[11]
        self.case.update(scenario="quote", expected_evidence=[{
            "source_id": "original", "source_urls": ["https://original.example/article"],
            "target_span": self.span, "source_quote": self.quote, "needs_lexical_highlight": True}])
        self.case["published_target"] = {"url": "https://target.example/essay"}
        self.report = {
            "id": "authored-fixture", "scope": "web_search", "text": self.text,
            "stats": {}, "matches": [],
            "sources": [{"id": "mirror", "url": "https://mirror.example/essay", "text": self.text}],
            "web": {"query_strategy": "a-historical-plan", "query_spans": [{"start": self.span[0], "end": self.span[1]}],
                    "queries_attempted": 1, "sources_checked": 1, "elapsed_seconds": 1, "status": "partial",
                    "errors": [], "attempted_urls": ["https://mirror.example/essay"],
                    "discovered_urls": ["https://mirror.example/essay"]}}
        self.report["matches"] = [self.match("exact", self.span, self.span)]

    def match(self, kind, target_span, source_span):
        return {"kind": kind, "source_id": "mirror", "source_url": "https://mirror.example/essay",
                "start": target_span[0], "end": target_span[1], "source_start": source_span[0], "source_end": source_span[1],
                "excerpt": self.text[slice(*target_span)], "source_excerpt": self.text[slice(*source_span)]}

    def run_inspection(self):
        key = self.dev / "key.json"
        key.write_text(json.dumps({"cases": self.cases}))
        (self.dev / self.case["input_path"]).write_text(self.text)
        (self.reports / (self.case["id"] + ".json")).write_text(json.dumps(self.report))
        before = key.read_bytes()
        result = inspect(self.dev, self.reports)
        self.assertEqual(key.read_bytes(), before)
        return result, next(c for c in result["cases"] if c["status"] == "saved")

    def test_target_mirror_does_not_find_original_source(self):
        bundle, case = self.run_inspection()
        e = case["evidence"][0]
        self.assertEqual(e["literal_covered_words"], e["literal_expected_words"])
        self.assertEqual(e["literal_covered_expected_source_words"], 0)
        self.assertFalse(e["expected_url_discovered"])
        self.assertEqual(bundle["summary"]["fully_highlighted_any_source"], 1)
        self.assertEqual(bundle["summary"]["fully_highlighted_expected_source"], 0)
        self.assertEqual(bundle["summary"]["expected_source_pairs_discovered"], 0)

    def test_actual_saved_query_span_is_used_without_replanning(self):
        _, case = self.run_inspection()
        self.assertTrue(case["evidence"][0]["whole_query_in_both_spans"])
        self.assertEqual(case["evidence"][0]["query_shared_words"], 10)

    def test_target_insertion_is_excluded_from_copied_word_denominator(self):
        text = "amber birch cedar inserted dahlia elm fir"
        gold = {"target_span": [0, len(text)], "source_quote": "amber birch cedar dahlia elm fir"}
        copied = copied_target_tokens(_tokens(text), gold)
        self.assertEqual([t.value for t in copied], ["amber", "birch", "cedar", "dahlia", "elm", "fir"])
        self.assertEqual(len(_tokens(text)), 7)

    def test_expected_source_url_is_counted_separately(self):
        self.case["expected_evidence"][0]["source_urls"] = ["https://mirror.example/essay"]
        bundle, case = self.run_inspection()
        e = case["evidence"][0]
        self.assertEqual(e["literal_covered_expected_source_words"], e["literal_expected_words"])
        self.assertEqual(bundle["summary"]["expected_source_pairs_discovered"], 1)
        self.assertEqual(bundle["summary"]["fully_highlighted_expected_source"], 1)

    def test_contaminated_boundary_is_not_reported_as_proven_search_failure(self):
        self.report["matches"] = []
        self.report["web"]["query_spans"][0]["start"] = len(self.prefix) - len("читателя. ")
        _, case = self.run_inspection()
        self.assertEqual(case["evidence"][0]["earliest_confirmed_cause"], "unresolved_query_boundary_or_retrieval")

    def test_phrase_selection_miss_requires_no_overlap(self):
        self.report["matches"] = []
        self.report["web"]["query_spans"] = [{"start": 0, "end": len(self.prefix)}]
        _, case = self.run_inspection()
        self.assertEqual(case["evidence"][0]["earliest_confirmed_cause"], "phrase_selection")

    def test_replay_does_not_count_original_queries_as_new_network_use(self):
        self.report["offline_replay"] = {"network_queries": 0}
        bundle, case = self.run_inspection()
        self.assertEqual(case["queries"], 1)
        self.assertEqual(case["data_kind"], "saved_page_replay")
        self.assertEqual(bundle["summary"]["network_queries_this_run"], 0)

    def test_sparse_fresh_subset_has_its_own_denominator(self):
        bundle, _ = self.run_inspection()
        self.assertEqual(bundle["summary"]["saved_documents"], 1)
        self.assertEqual(bundle["summary"]["lexical_fragments"], 1)
        self.assertEqual(bundle["summary"]["expected_source_pairs"], 1)
        self.assertEqual(bundle["summary"]["network_queries_this_run"], 1)
        self.assertEqual(sum(c["status"] == "pending" for c in bundle["cases"]), 29)
        self.assertEqual(bundle["reviewed_cases"], 0)

    def test_ungrounded_exact_is_not_counted_as_coverage(self):
        self.report["matches"][0]["source_excerpt"] = "Это другая строка"
        bundle, case = self.run_inspection()
        self.assertEqual(len(case["invalid_highlights"]), 1)
        self.assertEqual(case["evidence"][0]["literal_covered_words"], 0)
        self.assertEqual(bundle["summary"]["fully_highlighted_any_source"], 0)

    def test_false_similar_extension_is_separate_from_valid_exact_quote(self):
        self.set_d13_reviewed_prefix()
        broad = self.match("similar", [0, len(self.text)], self.span)
        self.report["matches"].insert(0, broad)
        bundle, case = self.run_inspection()
        self.assertEqual(bundle["summary"]["confirmed_false_similar_source_pairs"], 1)
        self.assertEqual(bundle["summary"]["confirmed_false_similar_target_regions"], 1)
        self.assertEqual(bundle["summary"]["fully_highlighted_any_source"], 1)
        self.assertEqual(case["unexpected_matches"][0]["review"], "unsupported_similar_extension")

    def set_d13_reviewed_prefix(self):
        self.case["id"] = "D13"
        self.cases[11], self.cases[12] = self.cases[12], self.case
        self.cases[11]["id"] = "D12"
        # Authored quote; only the attribution wording matches the reviewed
        # error. No external article text is needed for this regression.
        self.prefix = "Приведём подсказку из статьи «Шахматные часы» русской Википедии: «"
        self.text = self.prefix + self.quote
        self.span = [len(self.prefix), len(self.text)]
        self.case["text_sha256"] = hashlib.sha256(self.text.encode()).hexdigest()
        self.case["expected_evidence"][0]["target_span"] = self.span
        self.report["text"] = self.text
        self.report["sources"][0]["text"] = self.text
        self.report["web"]["query_spans"] = [{"start": self.span[0], "end": self.span[1]}]
        self.report["matches"] = [self.match("exact", self.span, self.span)]

    def test_new_d13_source_supporting_prefix_is_not_automatically_false(self):
        self.set_d13_reviewed_prefix()
        broad = self.match("similar", [0, len(self.text)], [0, len(self.text)])
        self.report["matches"].insert(0, broad)
        self.report["sources"][0]["url"] = "https://new.example/independent-page"
        for m in self.report["matches"]:
            m["source_url"] = "https://new.example/independent-page"
        bundle, case = self.run_inspection()
        self.assertEqual(bundle["summary"]["confirmed_false_similar_source_pairs"], 0)
        self.assertEqual(case["unexpected_matches"][0]["review"], "needs_pair_review")

    def test_d13_without_grounded_exact_counterpart_needs_review(self):
        self.set_d13_reviewed_prefix()
        self.report["matches"] = [self.match("similar", [0, len(self.text)], self.span)]
        bundle, case = self.run_inspection()
        self.assertEqual(bundle["summary"]["confirmed_false_similar_source_pairs"], 0)
        self.assertEqual(case["unexpected_matches"][0]["review"], "needs_pair_review")

    def test_ungrounded_outside_exact_is_not_called_a_valid_literal_match(self):
        broad = self.match("exact", [0, len(self.text)], [0, len(self.text)])
        broad["source_excerpt"] = "Выдержка отсутствует в источнике"
        self.report["matches"] = [broad]
        bundle, case = self.run_inspection()
        self.assertEqual(bundle["summary"]["invalid_grounded_matches"], 1)
        self.assertEqual(case["unexpected_matches"][0]["review"], "invalid_grounding")

    def test_refuses_non_dev_key(self):
        self.cases[29]["split"] = "test"
        with self.assertRaisesRegex(ValueError, "Only the 30 DEV"):
            self.run_inspection()

    def test_comparison_uses_only_common_saved_cases(self):
        bundle, case = self.run_inspection()
        before = copy.deepcopy(bundle)
        extra = copy.deepcopy(case)
        extra["id"] = "D01"
        before["cases"][0] = extra
        result = compare(before, bundle)
        self.assertEqual(result["compared_case_ids"], ["D12"])
        self.assertEqual(result["before"]["saved_documents"], 1)
        self.assertEqual(result["before"]["lexical_fragments"], 1)
        self.assertEqual(result["after"]["lexical_fragments"], 1)
        self.assertEqual(result["lost_fragments"], [])


if __name__ == "__main__":
    unittest.main()
