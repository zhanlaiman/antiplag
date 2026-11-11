from copy import deepcopy
from hashlib import sha256
import unittest

from scripts.dev_report_provenance import (
    cohort_configuration, report_provenance, source_snapshot_signatures,
)


def digest(text):
    return sha256(text.encode()).hexdigest()


def fixture():
    raw = {"id": "authored-report-uuid", "sources": [], "web": {
        "query_strategy": "dense-authored", "url_strategy": "path-authored", "matcher_version": "lexical-authored",
        "named_references_enabled": True, "source_followup_policy": "productive-authored", "supplemental_text_views": 0}}
    run = {"query_strategy": "dense-authored", "url_strategy": "path-authored", "named_references": True,
           "pdf_alternative_engine": "pdfium", "html_extraction_version": "html-authored-v2",
           "git_head": "a" * 40, "source_code_sha256": {"plagiarism/web_fetch.py": "b" * 64,
                                                         "plagiarism/web_search.py": "c" * 64}}
    return raw, run


class DevReportProvenanceTests(unittest.TestCase):
    def test_configured_engine_and_flags_are_separate_from_observations(self):
        raw, run = fixture()
        result = report_provenance(raw, run, "owned-run", "d" * 64, case_id="D01",
                                   observed_fetch_extractions=["html-authored-v1"])
        self.assertEqual(result["pdf_configured_engine"], "pdfium")
        self.assertEqual(result["pdf_observed_views"], 0)
        self.assertEqual(result["html_extraction_version"], "html-authored-v2")
        self.assertEqual(result["observed_fetch_extractions"], ["html-authored-v1"])
        self.assertTrue(result["named_references_enabled"])
        self.assertEqual(result["case_id"], "D01")
        self.assertEqual(result["report_id"], "authored-report-uuid")
        unknown = report_provenance({"web": {"input_reference_urls_discovered": 0, "supplemental_text_views": 0}}, {}, "old", "e" * 64)
        self.assertIsNone(unknown["pdf_configured_engine"])
        self.assertIsNone(unknown["named_references_enabled"])
        self.assertIsNone(unknown["html_extraction_version"])
        disabled = report_provenance({}, {"pdf_alternative_engine": "none", "named_references": False}, "owned", "e" * 64)
        self.assertEqual(disabled["pdf_configured_engine"], "none")
        self.assertIs(disabled["named_references_enabled"], False)

    def test_full_known_equal_cohort_is_required_for_uniform(self):
        raw, run = fixture()
        expected = [f"D{i:02d}" for i in range(1, 31)]
        values = {cid: report_provenance(raw, run, "owned", "d" * 64, case_id=cid) for cid in expected}
        full = cohort_configuration(values, expected)
        self.assertTrue(full["complete"])
        self.assertTrue(full["uniform"])
        self.assertEqual(full["pending_ids"], [])
        partial = cohort_configuration({key: value for key, value in values.items() if key != "D30"}, expected)
        self.assertFalse(partial["uniform"])
        self.assertEqual(partial["pending_ids"], ["D30"])
        mixed = deepcopy(values)
        mixed["D30"]["configuration"]["html_extraction_version"] = "html-authored-v1"
        mixed["D30"]["configuration"]["source_code_sha256"]["plagiarism/web_fetch.py"] = "f" * 64
        result = cohort_configuration(mixed, expected)
        self.assertTrue(result["complete"])
        self.assertFalse(result["uniform"])
        self.assertEqual(set(result["differing_fields"]), {"html_extraction_version", "source_code_sha256"})
        unknown = deepcopy(values)
        unknown["D01"]["configuration"]["html_extraction_version"] = None
        self.assertFalse(cohort_configuration(unknown, expected)["uniform"])
        extra = {**values, "D31": values["D01"]}
        self.assertFalse(cohort_configuration(extra, expected)["uniform"])

    def test_conflicts_and_missing_launch_identity_stay_unknown(self):
        raw, run = fixture()
        run["named_references"] = False
        result = report_provenance(raw, run, "owned", "d" * 64, case_id="D01")
        self.assertIsNone(result["named_references_enabled"])
        self.assertIn("named_references_enabled", result["configuration_conflicts"])
        old = report_provenance(raw, {}, "old", "e" * 64, case_id="D01")
        cohort = cohort_configuration([old], ["D01"])
        self.assertTrue(cohort["complete"])
        self.assertFalse(cohort["uniform"])
        self.assertIn("source_code_sha256", cohort["unknown_fields_by_case"]["D01"])
        self.assertIn("git_head", cohort["unknown_fields_by_case"]["D01"])

    def test_source_signatures_check_every_stored_hash_and_parent_identity(self):
        primary = {"id": "p", "url": "https://example.test/owned.pdf", "text": "Authored primary text",
                   "provenance": {"text_sha256": digest("Authored primary text")}}
        view = {"id": "v", "url": primary["url"], "text": "Authored additional view", "representation_of": "p",
                "provenance": {"text_sha256": digest("Authored additional view")}}
        raw = {"sources": [view, primary]}
        signatures = source_snapshot_signatures(raw)
        self.assertEqual(signatures["v"]["representation_of"], "p")
        self.assertEqual(signatures["p"]["text_sha256"], digest(primary["text"]))
        for mutation in ("changed_primary", "changed_view", "orphan", "different_url", "duplicate_id", "missing_hash"):
            with self.subTest(mutation=mutation):
                changed = deepcopy(raw)
                if mutation == "changed_primary":
                    changed["sources"][1]["text"] += " changed"
                elif mutation == "changed_view":
                    changed["sources"][0]["text"] += " changed"
                elif mutation == "orphan":
                    changed["sources"][0]["representation_of"] = "missing"
                elif mutation == "different_url":
                    changed["sources"][0]["url"] += "?different=1"
                elif mutation == "duplicate_id":
                    changed["sources"][0]["id"] = "p"
                else:
                    changed["sources"][0]["provenance"] = {}
                with self.assertRaises(ValueError):
                    source_snapshot_signatures(changed)


if __name__ == "__main__":
    unittest.main()
