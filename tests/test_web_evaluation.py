from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plagiarism import web_evaluation as evaluation
from scripts import score_web_evaluation as cli


TEXT = "🧪 Автор описал строение листа, расположение жилок и особенности клеток."
URL = "https://example.org/article?version=1"
FROZEN = "f" * 64


class WebEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest_path = self.root / "manifest.json"
        self.report = {"text": TEXT, "scope": "web_search", "sources": [
            {"id": "source", "url": URL, "title": "Fixture", "text": TEXT}], "matches": [
            {"kind": "exact", "source_id": "source", "source_url": URL, "start": 0, "end": len(TEXT),
             "source_start": 0, "source_end": len(TEXT), "excerpt": TEXT, "source_excerpt": TEXT}],
            "web": {"status": "complete", "discovered_urls": [URL], "errors": [], "queries_attempted": 2,
                    "elapsed_seconds": 4.5}}
        self.case = {"id": "fixture-1", "document_id": "document-1", "family_id": "family-1", "split": "dev",
                     "scenario": "copy", "synthetic": True, "reviewed": True,
                     "text_sha256": sha256(TEXT.encode()).hexdigest(), "report_path": "report.json",
                     "expected_evidence": [{"source_urls": [URL], "target_span": [0, len(TEXT)],
                                             "source_span": [0, len(TEXT)], "needs_lexical_highlight": True}]}

    def save(self, cases=None, reports=None, **metadata):
        cases = deepcopy(cases if cases is not None else [self.case])
        reports = reports or {"report.json": self.report}
        for name, report in reports.items():
            (self.root / name).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        self.manifest_path.write_text(json.dumps({"schema_version": 1, **metadata, "cases": cases}, ensure_ascii=False), encoding="utf-8")
        return self.manifest_path

    def run_score(self, **options):
        return evaluation.score_manifest(self.manifest_path, **options)

    def test_offline_found_unicode_offsets_and_no_full_text_in_score(self):
        self.save()
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            result = self.run_score()
        case = result["cases"][0]
        self.assertEqual(case["outcome"], "found")
        self.assertEqual(case["evidence"][0]["target_overlap_fraction"], 1)
        self.assertEqual(result["summary"]["metrics"]["evidence_found"], {"numerator": 1, "denominator": 1, "unknown": 0})
        self.assertEqual(result["summary"]["wall_seconds_observed_sum"], 4.5)
        self.assertEqual(result["summary"]["query_attempts_observed_sum"], 2)
        self.assertEqual(result["summary"]["reviewed_real"]["case_count"], 0)
        self.assertEqual(result["summary"]["reviewed_synthetic"]["case_count"], 1)
        self.assertNotIn(TEXT, json.dumps(result, ensure_ascii=False))

    def test_stages_use_discovery_and_reading_without_turning_missing_telemetry_into_miss(self):
        for discovered, sources, matches, expected in (([], [], [], "source_not_discovered"),
                ([URL], [], [], "source_unread"), ([URL], self.report["sources"], [], "span_missed"),
                (None, [], [], "unknown")):
            with self.subTest(outcome=expected):
                report = deepcopy(self.report)
                report.update(sources=sources, matches=matches)
                if discovered is None:
                    report["web"].pop("discovered_urls")
                else:
                    report["web"]["discovered_urls"] = discovered
                self.save(reports={"report.json": report})
                result = self.run_score()
                self.assertEqual(result["cases"][0]["evidence"][0]["outcome"], expected)
                if discovered is None:
                    self.assertEqual(result["summary"]["metrics"]["source_discovery"], {"numerator": 0, "denominator": 0, "unknown": 1})
                    self.assertEqual(result["summary"]["metrics"]["evidence_found"], {"numerator": 0, "denominator": 1, "unknown": 1})
                    self.assertEqual(result["summary"]["metrics"]["all_expected_evidence_found"], {"numerator": 0, "denominator": 1, "unknown": 1})

    def test_partial_found_is_counted_but_query_errors_and_coverage_remain_visible(self):
        report = deepcopy(self.report)
        report["web"].update(status="partial", errors=[{"stage": "search", "code": "search_rate_limited"}])
        self.save(reports={"report.json": report})
        result = self.run_score()
        self.assertEqual(result["cases"][0]["outcome"], "found")
        self.assertTrue(result["cases"][0]["evidence"][0]["discovered"])
        self.assertEqual(result["cases"][0]["evidence"][0]["found_basis"], "exact_target_highlight")
        self.assertEqual(result["cases"][0]["coverage"], "partial")
        self.assertTrue(result["cases"][0]["query_errors"])
        self.assertEqual(result["summary"]["metrics"]["evidence_found"]["numerator"], 1)
        self.assertEqual(result["summary"]["metrics"]["technical_error_cases"], 1)

    def test_half_target_overlap_is_inclusive_and_query_parameters_are_not_equivalent(self):
        report = deepcopy(self.report)
        end = len(TEXT) // 2
        report["matches"][0].update(end=end, source_end=end, excerpt=TEXT[:end], source_excerpt=TEXT[:end])
        self.case["expected_evidence"][0]["target_span"] = [0, end * 2]
        self.save(reports={"report.json": report})
        self.assertEqual(self.run_score()["cases"][0]["outcome"], "found")
        report["matches"][0].update(end=end - 1, source_end=end - 1, excerpt=TEXT[:end - 1], source_excerpt=TEXT[:end - 1])
        self.save(reports={"report.json": report})
        self.assertEqual(self.run_score()["cases"][0]["outcome"], "span_missed")
        self.case["expected_evidence"][0]["source_urls"] = [URL.replace("version=1", "version=2")]
        self.save()
        self.assertEqual(self.run_score()["cases"][0]["outcome"], "source_not_discovered")

    def test_explicit_redirect_equivalent_and_nonlexical_expectation(self):
        redirected = "https://example.org/redirect-target"
        self.case["scenario"] = "translation"
        self.case["expected_evidence"][0].update(source_urls=[URL, redirected], needs_lexical_highlight=False)
        report = deepcopy(self.report)
        report["sources"][0]["url"] = redirected
        report["matches"] = []
        self.save(reports={"report.json": report})
        result = self.run_score()
        self.assertEqual(result["cases"][0]["outcome"], "found")
        self.assertTrue(result["cases"][0]["evidence"][0]["discovered"])
        self.assertEqual(result["cases"][0]["evidence"][0]["found_basis"], "source_read_only")
        self.assertEqual(result["summary"]["metrics"]["lexical_highlight"]["denominator"], 0)
        self.assertEqual(result["summary"]["metrics"]["lexical_evidence_found"], {"numerator": 0, "denominator": 0})
        self.assertEqual(result["summary"]["metrics"]["nonlexical_source_read"], {"numerator": 1, "denominator": 1})
        self.assertEqual(result["cases"][0]["editorial_interpretation"], "matching_evidence_only")

    def test_unicode_gold_url_matches_encoded_report_without_decoding_slashes_or_dropping_query(self):
        self.case["expected_evidence"][0]["source_urls"] = ["https://example.org/статья?тема=клетка"]
        encoded = "https://example.org/%d1%81%d1%82%d0%b0%d1%82%d1%8c%d1%8f?%d1%82%d0%b5%d0%bc%d0%b0=%d0%ba%d0%bb%d0%b5%d1%82%d0%ba%d0%b0"
        report = deepcopy(self.report)
        report["sources"][0]["url"] = encoded
        report["matches"][0]["source_url"] = encoded.upper().replace("HTTPS://EXAMPLE.ORG", "https://example.org")
        report["web"]["discovered_urls"] = [encoded]
        self.save(reports={"report.json": report})
        self.assertEqual(self.run_score()["cases"][0]["outcome"], "found")
        self.assertNotEqual(evaluation._url("https://example.org/a%2fb"), evaluation._url("https://example.org/a/b"))
        self.assertNotEqual(evaluation._url("https://example.org/a?q=1"), evaluation._url("https://example.org/a?q=2"))

    def test_gold_unread_source_span_is_not_validated_against_fetched_version(self):
        self.case["expected_evidence"][0]["source_span"] = [10000, 11000]
        report = deepcopy(self.report)
        report.update(sources=[], matches=[])
        self.save(reports={"report.json": report})
        self.assertEqual(self.run_score()["cases"][0]["outcome"], "source_unread")

    def test_optional_gold_snapshot_hash_span_and_quote_are_verified_independently(self):
        gold = self.case["expected_evidence"][0]
        gold.update(source_snapshot_path="gold.txt", source_text_sha256=sha256(TEXT.encode()).hexdigest(), source_quote=TEXT)
        (self.root / "gold.txt").write_text(TEXT, encoding="utf-8")
        report = deepcopy(self.report)
        report.update(sources=[], matches=[])
        self.save(reports={"report.json": report})
        self.assertTrue(self.run_score()["cases"][0]["evidence"][0]["source_snapshot_verified"])
        (self.root / "gold.txt").write_text(TEXT + " changed", encoding="utf-8")
        with self.assertRaisesRegex(evaluation.EvaluationError, "SHA-256 gold"):
            self.run_score()
        (self.root / "gold.txt").write_text(TEXT, encoding="utf-8")
        gold["source_span"] = [0, len(TEXT) + 1]
        self.save(reports={"report.json": report})
        with self.assertRaisesRegex(evaluation.EvaluationError, "диапазон"):
            self.run_score()

    def test_ungrounded_highlight_is_quarantined_and_never_becomes_found_or_clean_negative(self):
        report = deepcopy(self.report)
        report["matches"][0]["source_excerpt"] = "invented evidence"
        self.save(reports={"report.json": report})
        result = self.run_score()["cases"][0]
        self.assertEqual(result["outcome"], "span_missed")
        self.assertTrue(result["invalid_highlights"])
        self.assertEqual(result["coverage"], "unknown")
        self.case.update(scenario="independent", expected_evidence=[])
        self.save(reports={"report.json": report})
        result = self.run_score()
        self.assertEqual(result["cases"][0]["outcome"], "unexpected_exact_highlight")
        self.assertEqual(result["cases"][0]["validated_exact_highlights"], 0)
        self.assertEqual(result["summary"]["metrics"]["unexpected_exact_highlights"]["numerator"], 1)

    def test_exact_label_with_different_grounded_source_text_is_not_evidence(self):
        report = deepcopy(self.report)
        different = "Совсем другой подлинный текст источника, который не совпадает с документом."
        report["sources"][0]["text"] = different
        report["matches"][0].update(source_end=len(different), source_excerpt=different)
        self.save(reports={"report.json": report})
        case = self.run_score()["cases"][0]
        self.assertEqual(case["outcome"], "span_missed")
        self.assertTrue(case["invalid_highlights"])
        self.assertEqual(case["validated_exact_highlights"], 0)

    def test_line_wrap_exact_evidence_retains_the_original_snapshot_and_scores(self):
        from plagiarism.matching import analyze
        report = deepcopy(self.report)
        report["sources"][0]["text"] = TEXT.replace("особенности", "особен-\nности")
        report["matches"] = analyze(TEXT, report["sources"])["matches"]
        self.save(reports={"report.json": report})
        result = self.run_score()["cases"][0]
        self.assertEqual(result["outcome"], "found")
        self.assertEqual(result["invalid_highlights"], [])

    def test_paragraph_break_is_not_accepted_as_dehyphenated_exact_evidence(self):
        report = deepcopy(self.report)
        broken = TEXT.replace("особенности", "особен-\n\nности")
        report["sources"][0]["text"] = broken
        report["matches"][0].update(source_end=len(broken), source_excerpt=broken)
        self.save(reports={"report.json": report})
        result = self.run_score()["cases"][0]
        self.assertTrue(result["invalid_highlights"])
        self.assertEqual(result["validated_exact_highlights"], 0)

    def test_read_source_implies_discovery_even_with_old_or_redirect_audit(self):
        for audit in (None, ["https://example.org/original-redirect"]):
            with self.subTest(audit=audit):
                report = deepcopy(self.report)
                report["web"]["discovered_urls"] = audit
                self.save(reports={"report.json": report})
                result = self.run_score()
                gold = result["cases"][0]["evidence"][0]
                self.assertTrue(gold["discovered"])
                self.assertTrue(gold["discovery_observed_via_read"])
                self.assertEqual(result["summary"]["metrics"]["source_discovery"], {"numerator": 1, "denominator": 1, "unknown": 0})

    def test_common_allowed_overlap_is_not_an_unexpected_match_or_editorial_accusation(self):
        self.case.update(scenario="template", editorial_judgment="acceptable")
        self.save()
        result = self.run_score()
        self.assertEqual(result["cases"][0]["outcome"], "found")
        self.assertEqual(result["cases"][0]["editorial_judgment"], "acceptable")
        self.assertEqual(result["summary"]["metrics"]["unexpected_exact_highlights"], {"numerator": 0, "denominator": 0})

    def test_old_negative_without_discovery_audit_never_reports_complete_coverage(self):
        self.case.update(scenario="independent", expected_evidence=[])
        report = deepcopy(self.report)
        report.update(sources=[], matches=[])
        report["web"].pop("discovered_urls")
        self.save(reports={"report.json": report})
        result = self.run_score()
        self.assertEqual(result["cases"][0]["outcome"], "unknown")
        self.assertEqual(result["cases"][0]["coverage"], "unknown")
        self.assertEqual(result["summary"]["metrics"]["negative_document_coverage_complete"], 0)
        self.assertEqual(result["summary"]["metrics"]["negative_document_coverage_unresolved"], 1)

    def test_multiple_source_case_counts_each_expected_source_and_requires_all(self):
        self.case["scenario"] = "multiple_sources"
        self.case["expected_evidence"].append({**self.case["expected_evidence"][0],
                                              "source_urls": ["https://example.org/unread"]})
        report = deepcopy(self.report)
        report["web"]["discovered_urls"].append("https://example.org/unread")
        self.save(reports={"report.json": report})
        result = self.run_score()
        self.assertEqual(result["cases"][0]["outcome"], "partial_found")
        self.assertEqual(result["summary"]["metrics"]["evidence_found"], {"numerator": 1, "denominator": 2, "unknown": 0})
        self.assertEqual(result["summary"]["metrics"]["all_expected_evidence_found"], {"numerator": 0, "denominator": 1, "unknown": 0})

    def test_empty_source_snapshot_is_not_successful_reading(self):
        self.case["scenario"] = "paraphrase"
        self.case["expected_evidence"][0]["needs_lexical_highlight"] = False
        report = deepcopy(self.report)
        report["sources"][0]["text"] = " \n "
        report["matches"] = []
        self.save(reports={"report.json": report})
        with self.assertRaisesRegex(evaluation.EvaluationError, "пустой source snapshot"):
            self.run_score()

    def test_reused_report_annotations_do_not_double_count_wall_time_or_queries(self):
        first, second = deepcopy(self.case), deepcopy(self.case)
        first["expected_evidence"][0]["target_span"] = [0, 35]
        second["id"] = "fixture-2"
        second["report_path"] = "wrapped.json"
        second["expected_evidence"][0]["target_span"] = [35, len(TEXT)]
        self.save([first, second], {"report.json": self.report, "wrapped.json": {"report": self.report}})
        summary = self.run_score()["summary"]
        self.assertEqual(summary["selected_case_count"], 2)
        self.assertEqual(summary["costs_unique_report_count"], 1)
        self.assertEqual(summary["wall_seconds_observed_sum"], 4.5)
        self.assertEqual(summary["query_attempts_observed_sum"], 2)
        self.assertEqual(summary["wall_seconds_reported_cases"], 2)
        self.assertEqual(summary["wall_seconds_reported_reports"], 1)

    def test_gold_source_url_and_snapshot_hash_cannot_cross_splits(self):
        for reuse_hash in (False, True):
            with self.subTest(reuse_hash=reuse_hash):
                first, second = deepcopy(self.case), deepcopy(self.case)
                second.update(id="fixture-2", document_id="document-2", family_id="family-2", split="test",
                              text_sha256="a" * 64)
                if reuse_hash:
                    second["expected_evidence"][0]["source_urls"] = ["https://elsewhere.example.org/copy"]
                    for case in (first, second):
                        case["expected_evidence"][0].update(source_snapshot_path="gold.txt", source_text_sha256="b" * 64)
                self.save([first, second])
                with self.assertRaisesRegex(evaluation.EvaluationError, "dev и test"):
                    self.run_score()

    def test_negative_documents_are_deduplicated_and_partial_zero_is_not_success(self):
        first = {**self.case, "scenario": "independent", "expected_evidence": []}
        second = {**first, "id": "fixture-2", "scenario": "template"}
        self.save([first, second])
        result = self.run_score()
        self.assertEqual(result["summary"]["metrics"]["unexpected_exact_highlights"], {"numerator": 1, "denominator": 1})
        report = deepcopy(self.report)
        report["matches"] = []
        report["web"].update(status="partial", errors=[{"stage": "fetch", "code": "robots_denied"}])
        self.save([first, second], {"report.json": report})
        result = self.run_score()
        self.assertTrue(all(case["outcome"] == "unknown" for case in result["cases"]))
        self.assertEqual(result["summary"]["metrics"]["negative_document_coverage_complete"], 0)
        self.assertEqual(result["summary"]["metrics"]["negative_document_coverage_unresolved"], 1)

    def test_zero_read_sources_is_not_a_clean_negative_even_with_complete_status_and_url_audit(self):
        self.case.update(scenario="independent", expected_evidence=[])
        report = deepcopy(self.report)
        report.update(sources=[], matches=[])
        report["web"].update(status="complete", discovered_urls=[], errors=[])
        self.save(reports={"report.json": report})
        result = self.run_score()
        self.assertEqual(result["cases"][0]["coverage"], "unknown")
        self.assertEqual(result["cases"][0]["outcome"], "unknown")
        self.assertEqual(result["summary"]["metrics"]["negative_document_coverage_complete"], 0)
        self.assertEqual(result["summary"]["metrics"]["negative_document_coverage_unresolved"], 1)

    def test_quote_is_matching_evidence_not_an_editorial_accusation_and_pending_is_excluded(self):
        self.case.update(scenario="quote", reviewed=False)
        self.save()
        result = self.run_score()
        self.assertEqual(result["cases"][0]["outcome"], "found")
        self.assertEqual(result["cases"][0]["editorial_interpretation"], "quoted_overlap_is_not_an_accusation")
        self.assertEqual(result["summary"]["excluded_unreviewed_case_count"], 1)
        self.assertEqual(result["summary"]["metrics"]["evidence_found"]["denominator"], 0)

    def test_split_family_document_content_leakage_and_duplicate_annotations_are_rejected(self):
        for change in ({"id": "fixture-1"}, {"id": "fixture-2"},
                       {"id": "fixture-2", "document_id": "document-2", "split": "test"},
                       {"id": "fixture-2", "family_id": "family-2", "split": "test"},
                       {"id": "fixture-2", "family_id": "family-2", "document_id": "document-2"}):
            with self.subTest(change=change):
                self.save([self.case, {**self.case, **change}])
                with self.assertRaises(evaluation.EvaluationError):
                    self.run_score()

    def test_text_hash_spans_duplicate_gold_and_missing_private_snapshot_are_rejected(self):
        original = deepcopy(self.case)
        for mutate in (lambda case: case.update(text_sha256="0" * 64),
                       lambda case: case["expected_evidence"][0].update(target_span=[False, 2]),
                       lambda case: case["expected_evidence"][0].update(target_span=[0, len(TEXT) + 1]),
                       lambda case: case["expected_evidence"].append(deepcopy(case["expected_evidence"][0]))):
            case = deepcopy(original)
            mutate(case)
            self.save([case])
            with self.assertRaises(evaluation.EvaluationError):
                self.run_score()
        public = deepcopy(self.report)
        public.pop("sources")
        self.save([original], {"report.json": {"report": public}})
        with self.assertRaisesRegex(evaluation.EvaluationError, "публичного API"):
            self.run_score()

    def test_test_split_requires_both_explicit_permission_and_matching_frozen_hash(self):
        self.case["split"] = "test"
        self.save(frozen_config_sha256=FROZEN)
        for options in ({}, {"allow_test": True}, {"frozen_config_sha256": FROZEN},
                        {"allow_test": True, "frozen_config_sha256": "0" * 64}):
            with self.subTest(options=options), self.assertRaises(evaluation.EvaluationError):
                self.run_score(split="test", **options)
        self.assertEqual(self.run_score(split="test", allow_test=True, frozen_config_sha256=FROZEN)["cases"][0]["outcome"], "found")

    def test_cli_outputs_case_table_and_json_without_fabricating_dataset_size(self):
        self.save()
        output = self.root / "score.json"
        with redirect_stdout(StringIO()) as stream:
            self.assertEqual(cli.main([str(self.manifest_path), "--output", str(output)]), 0)
        self.assertIn("fixture-1\tcopy\tfound", stream.getvalue())
        self.assertEqual(json.loads(output.read_text())["summary"]["manifest_case_count"], 1)
        self.manifest_path.write_text('{"schema_version":1,"schema_version":1,"cases":[]}', encoding="utf-8")
        with self.assertRaises(evaluation.EvaluationError):
            self.run_score()


if __name__ == "__main__":
    unittest.main()
