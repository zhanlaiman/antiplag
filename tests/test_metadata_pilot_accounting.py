"""Authored temporary accounting fixtures only; never read live pilot artifacts."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import metadata_pilot_accounting as accounting
from scripts import run_dev_metadata_pilot as pilot


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def manifest(name):
    identifier = "1" * 32 if name.endswith("v1") else "2" * 32
    plans = {cid: [{"query": f'"Frozen query number {index} for case {cid}"', "start": index * 20,
                    "end": index * 20 + 19} for index in range(6)] for cid in pilot.CASE_IDS}
    inputs = {cid: sha256(("input-" + cid).encode()).hexdigest() for cid in pilot.CASE_IDS}
    return {"id": identifier, "run_name": name, "configuration": {
        "case_ids": list(pilot.CASE_IDS), "max_actual_provider_calls": 18, "max_queries_per_case": 6,
        "query_strategy": "frozen-dense", "experimental_strategy": "frozen-metadata-rank",
        "query_plans": plans, "query_plans_sha256": pilot.digest(plans), "input_sha256": inputs,
        "request_ids": {cid: pilot.case_request_id(identifier, cid, inputs[cid]) for cid in pilot.CASE_IDS}}}


class MetadataPilotAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "data/private/web-evaluation"
        self.runs = {name: manifest(name) for name in accounting.SERIES}
        for name, run in self.runs.items():
            write(self.base / name / "run.json", run)

    def add_call(self, identifier, identity, *, state="complete", credits=1, name=accounting.SERIES[1]):
        record = {**identity, "request_id": identifier, "search_depth": "basic", "state": state}
        if credits is not None:
            record["usage"] = {"credits": credits}
        path = self.base / name / "calls" / identity["case_id"] / (str(identity["query_index"]) + "-" + identifier + ".json")
        write(path, record)
        return path, record

    def add_all_calls(self):
        for identifier, identity in pilot.call_map(self.runs[accounting.SERIES[1]]).items():
            self.add_call(identifier, identity)

    def add_reports(self):
        name = accounting.SERIES[1]
        run = self.runs[name]
        directory = self.base / name
        manifest_hash = sha256((directory / "run.json").read_bytes()).hexdigest()
        for cid in pilot.CASE_IDS:
            write(directory / (cid + ".json"), {"web": {
                "queries_attempted": 6, "query_strategy": run["configuration"]["query_strategy"],
                "url_strategy": run["configuration"]["experimental_strategy"],
                "frozen_query_plan": run["configuration"]["query_plans"][cid],
                "frozen_manifest_sha256": manifest_hash}})

    def test_v1_zero_v2_eighteen_with_usage_reports_quota_and_root_relative_hashes(self):
        self.add_all_calls()
        self.add_reports()
        diagnosis = self.base / accounting.SERIES[0] / accounting.DIAGNOSIS
        write(diagnosis, {"post_transmitted": False, "provider_call_records": 0})
        write(self.base / accounting.SERIES[1] / "quota-before.json",
              {"current_plan": "Researcher", "plan_usage": 200, "paygo_usage": 0, "plan_limit": 1000})
        write(self.base / accounting.SERIES[1] / "quota-after.json", {"unavailable": True, "new_searches_allowed": False})
        rows, hashes = accounting.collect(self.base, self.root)
        v1, v2 = rows
        self.assertEqual(v1["query_attempts"], 0)
        self.assertEqual(v1["confirmed_provider_credits"], 0)
        self.assertEqual(v1["saved_documents"], 0)
        self.assertEqual(v2["query_attempts"], 18)
        self.assertEqual(v2["confirmed_provider_credits"], 18)
        self.assertEqual(v2["records_with_credit_usage"], 18)
        self.assertEqual(v2["provider_records_complete"], 18)
        self.assertEqual(v2["saved_documents"], 3)
        self.assertEqual(v2["queries_in_completed_reports"], 18)
        self.assertEqual(v2["attempts_by_case"], {cid: 6 for cid in pilot.CASE_IDS})
        self.assertTrue(v2["provider_credit_usage_complete"])
        self.assertTrue(v2["report_attempts_match_provider_markers"])
        relative = diagnosis.relative_to(self.root).as_posix()
        self.assertEqual(v1["failure_diagnosis_artifact"], relative)
        self.assertEqual(hashes[relative], sha256(diagnosis.read_bytes()).hexdigest())
        self.assertEqual(len(hashes), 26)  # 2 manifests + 18 calls + 3 reports + 2 quota + diagnosis.
        self.assertTrue(all(not Path(path).is_absolute() for path in hashes))

    def test_submitted_attempt_has_unknown_usage_not_zero_cost_proof(self):
        identifier, identity = next(iter(pilot.call_map(self.runs[accounting.SERIES[1]]).items()))
        self.add_call(identifier, identity, state="submitted", credits=None)
        rows, _ = accounting.collect(self.base, self.root)
        row = rows[1]
        self.assertEqual(row["query_attempts"], 1)
        self.assertEqual(row["provider_records_complete"], 0)
        self.assertEqual(row["provider_records_failed_or_unknown"], 1)
        self.assertEqual(row["provider_records_submitted"], 1)
        self.assertEqual(row["confirmed_provider_credits"], 0)
        self.assertEqual(row["records_with_credit_usage"], 0)
        self.assertEqual(row["records_without_credit_usage"], 1)
        self.assertFalse(row["provider_credit_usage_complete"])
        self.assertIn("missing usage is unknown", row["note"])

    def test_explicit_zero_usage_is_known_separate_from_missing_usage(self):
        calls = list(pilot.call_map(self.runs[accounting.SERIES[1]]).items())
        self.add_call(*calls[0], credits=0)
        self.add_call(*calls[1], state="failed_or_unknown", credits=None)
        self.add_call(*calls[2], credits=1)
        row = accounting.collect(self.base, self.root)[0][1]
        self.assertEqual(row["query_attempts"], 3)
        self.assertEqual(row["confirmed_provider_credits"], 1)
        self.assertEqual(row["records_with_credit_usage"], 2)
        self.assertEqual(row["records_without_credit_usage"], 1)

    def test_wrong_call_binding_is_rejected(self):
        identifier, identity = next(iter(pilot.call_map(self.runs[accounting.SERIES[1]]).items()))
        path, record = self.add_call(identifier, identity)
        for change in ({"query": "changed"}, {"request_id": "f" * 64}, {"case_id": "D04"},
                       {"query_index": 1}, {"query_index": False}, {"span": {"start": 1, "end": 20}},
                       {"span": {"start": False, "end": identity["span"]["end"]}}, {"search_depth": "advanced"}):
            with self.subTest(change=change):
                write(path, {**record, **change})
                with self.assertRaises(ValueError):
                    accounting.collect(self.base, self.root)

    def test_wrong_case_or_filename_is_rejected_without_reading_its_contents(self):
        unexpected = self.base / accounting.SERIES[1] / "calls/D99" / ("0-" + "f" * 64 + ".json")
        unexpected.parent.mkdir(parents=True)
        unexpected.write_text("not JSON; must not be opened")
        with self.assertRaisesRegex(ValueError, "Unbound"):
            accounting.collect(self.base, self.root)

    def test_more_than_eighteen_records_and_more_than_six_plans_are_rejected(self):
        self.add_all_calls()
        extra = self.base / accounting.SERIES[1] / "calls/D08" / ("0-" + "f" * 64 + ".json")
        write(extra, {})
        with self.assertRaisesRegex(ValueError, "more than eighteen"):
            accounting.collect(self.base, self.root)
        extra.unlink()
        run = self.runs[accounting.SERIES[1]]
        run["configuration"]["query_plans"]["D08"].append({"query": "extra", "start": 130, "end": 140})
        run["configuration"]["query_plans_sha256"] = pilot.digest(run["configuration"]["query_plans"])
        write(self.base / accounting.SERIES[1] / "run.json", run)
        with self.assertRaises(ValueError):
            accounting.collect(self.base, self.root)

    def test_frozen_request_or_query_digest_mismatch_is_rejected(self):
        name = accounting.SERIES[1]
        run = self.runs[name]
        run["configuration"]["request_ids"]["D08"] = "f" * 32
        write(self.base / name / "run.json", run)
        with self.assertRaisesRegex(ValueError, "binding changed"):
            accounting.collect(self.base, self.root)
        run = manifest(name)
        run["configuration"]["query_plans_sha256"] = "f" * 64
        write(self.base / name / "run.json", run)
        with self.assertRaisesRegex(ValueError, "digest changed"):
            accounting.collect(self.base, self.root)

    def test_invalid_usage_is_rejected_instead_of_being_reported_free(self):
        identifier, identity = next(iter(pilot.call_map(self.runs[accounting.SERIES[1]]).items()))
        path, record = self.add_call(identifier, identity)
        for credits in (True, -1, 2, "1", float("nan"), 0.5):
            with self.subTest(credits=credits):
                write(path, {**record, "usage": {"credits": credits}})
                with self.assertRaises(ValueError):
                    accounting.collect(self.base, self.root)

    def test_v1_provider_records_are_rejected_rather_than_silently_counted_zero(self):
        name = accounting.SERIES[0]
        identifier, identity = next(iter(pilot.call_map(self.runs[name]).items()))
        self.add_call(identifier, identity, name=name)
        with self.assertRaisesRegex(ValueError, "v1 must have zero"):
            accounting.collect(self.base, self.root)

    def test_mismatched_report_metadata_is_rejected(self):
        self.add_reports()
        path = self.base / accounting.SERIES[1] / "D08.json"
        report = json.loads(path.read_text())
        report["web"]["frozen_manifest_sha256"] = "f" * 64
        write(path, report)
        with self.assertRaisesRegex(ValueError, "report metadata"):
            accounting.collect(self.base, self.root)

    def test_saved_reports_without_call_markers_cannot_be_accounted_as_zero(self):
        self.add_reports()
        with self.assertRaisesRegex(ValueError, "accounting is incomplete"):
            accounting.collect(self.base, self.root)

    def test_report_marker_mismatch_is_checked_per_case_even_when_totals_equal(self):
        name = accounting.SERIES[1]
        for identifier, identity in pilot.call_map(self.runs[name]).items():
            if identity["case_id"] == "D08" and identity["query_index"] < 4 or identity["case_id"] == "D10" and identity["query_index"] < 5:
                self.add_call(identifier, identity)
        self.add_reports()
        for cid, attempted in (("D08", 5), ("D10", 4), ("D04", 0)):
            path = self.base / name / (cid + ".json")
            report = json.loads(path.read_text())
            report["web"]["queries_attempted"] = attempted
            write(path, report)
        with self.assertRaisesRegex(ValueError, "accounting is incomplete"):
            accounting.collect(self.base, self.root)

    def test_collect_is_read_only_and_ignores_all_other_directories(self):
        write(self.base / accounting.SERIES[0] / accounting.DIAGNOSIS, {"post_transmitted": False})
        self.add_all_calls()
        for relative in ("materials/final-70/key.json", "query-unfamiliar-reserved-v1/manifest.json",
                         "dev-metadata-pilot-v3/run.json", "dev-metadata-pilot-v2/pages/D08/page.json"):
            path = self.base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("invalid JSON: ineligible artifact must not be read")
        before = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch.object(pilot, "quota", side_effect=AssertionError("must not call quota")), \
             patch.object(pilot, "prepare", side_effect=AssertionError("must not prepare")), \
             patch.object(pilot, "live", side_effect=AssertionError("must not run live")), \
             patch.object(pilot, "source_identity", side_effect=AssertionError("must not read code")):
            rows, hashes = accounting.collect(self.base, self.root)
        after = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual([row["query_attempts"] for row in rows], [0, 18])
        self.assertFalse(any("final-70" in name or "unfamiliar" in name or "/pages/" in name for name in hashes))

    def test_artifact_symlink_cannot_redirect_a_read_outside_pilot(self):
        name = accounting.SERIES[1]
        identifier, identity = next(iter(pilot.call_map(self.runs[name]).items()))
        outside = self.root / "ineligible.json"
        outside.write_text("must not read this")
        path = self.base / name / "calls" / identity["case_id"] / (str(identity["query_index"]) + "-" + identifier + ".json")
        path.parent.mkdir(parents=True)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            accounting.collect(self.base, self.root)

    def test_missing_pilot_directories_have_no_implied_attempts(self):
        self.assertEqual(accounting.collect(self.root / "absent", self.root), ([], {}))


if __name__ == "__main__":
    unittest.main()
