"""Authored temporary accounting artifacts; no real first-use inputs are read."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plagiarism.saas import report_identifier
from scripts import unfamiliar_v2_accounting as accounting
from scripts import run_unfamiliar_query_v2 as pilot


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


class FirstUseV2AccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.base = self.root / "data/private/web-evaluation"
        self.directory = self.base / accounting.SERIES[0]
        self.choice = {"frozen": True, "git_head": "b" * 40, "plan_sha256": pilot.PLAN_SHA,
            "amendment_sha256": pilot.AMENDMENT_SHA, "maximum_total_fresh_queries": 48,
            "maximum_queries_per_document": 6, "zero_fallback": True, "named_references": True,
            "query_strategy": "authored-query", "url_strategy": "authored-url", "matcher_version": "authored-matcher"}
        self.texts = {cid: "Authored independent input for " + cid for cid in pilot.IDS}
        self.cases = [{"id": cid, "text_sha256": sha256(self.texts[cid].encode()).hexdigest()} for cid in pilot.IDS]
        self.plans = {cid: [{"query": '"Authored independent input"', "start": 0, "end": 26}] for cid in pilot.IDS}
        self.manifest = {"cases": self.cases, "input_texts": self.texts,
                         "plan_sha256": pilot.PLAN_SHA, "amendment_sha256": pilot.AMENDMENT_SHA}
        self.local = {"first_use": True, "used_for_tuning": False, "live_search_queries": 0,
                      "cases": [{"id": cid, "plans": {"selected": self.plans[cid]}} for cid in pilot.IDS]}
        choice_sha = "a" * 64
        self.run = {"first_use": True, "used_for_tuning": False, "final_materials_read": False,
            "id": sha256((pilot.PLAN_SHA + choice_sha).encode()).hexdigest()[:32],
            "configuration": {"choice": self.choice, "choice_sha256": choice_sha}}
        self.save_manifests()

    def save_manifests(self):
        for filename, value, field in (("manifest.json", self.manifest, "manifest_sha256"),
                                       ("local-first-use.json", self.local, "local_sha256")):
            path = self.directory / filename; write(path, value)
            self.run[field] = sha256(path.read_bytes()).hexdigest()
        write(self.directory / "run.json", self.run)

    def add_markers(self, missing_usage=False):
        paths = []
        for i, (identifier, identity) in enumerate(pilot.frozen_calls(self.run, self.cases, self.plans).items()):
            record = {**identity, "request_id": identifier, "search_depth": "basic", "state": "complete"}
            if not (missing_usage and i == 0):
                record["usage"] = {"credits": 0 if i == 1 else 1}
            path = self.directory / (identifier + "-search.json"); write(path, record); paths.append(path)
        return paths

    def add_reports(self):
        for case in self.cases:
            cid = case["id"]
            write(self.directory / (cid + ".json"), {
                "id": report_identifier(pilot.request_id(self.run, case)), "text": self.texts[cid],
                "web": {"queries_attempted": len(self.plans[cid]), "query_strategy": self.choice["query_strategy"],
                    "url_strategy": self.choice["url_strategy"], "matcher_version": self.choice["matcher_version"],
                    "named_references_enabled": True, "zero_fallback_enabled": True, "urls_attempted": 24,
                    "query_spans": [{k: p[k] for k in ("start", "end")} for p in self.plans[cid]]}})

    def test_complete_explicit_zero_and_artifact_hashes(self):
        self.add_markers(); self.add_reports()
        write(self.directory / "quota-after.json", {"paygo_usage": 0, "local_monthly_attempts": 538})
        rows, hashes = accounting.collect(self.base, self.root); row = rows[0]
        self.assertEqual(row["saved_documents"], 8); self.assertEqual(row["query_attempts"], 8)
        self.assertEqual(row["confirmed_provider_credits"], 7); self.assertEqual(row["total_basic_credits"], 7)
        self.assertTrue(row["provider_credit_usage_complete"]); self.assertTrue(row["provider_accounting_complete"])
        self.assertEqual(row["provider_usage_unknown_calls"], 0); self.assertEqual(len(hashes), 20)
        for name, digest in hashes.items():
            self.assertEqual(sha256((self.root / name).read_bytes()).hexdigest(), digest)

    def test_reported_calls_with_no_markers_are_unknown_not_zero_total(self):
        self.add_reports(); row = accounting.collect(self.base, self.root)[0][0]
        self.assertEqual(row["query_attempts"], 0); self.assertEqual(row["queries_in_completed_reports"], 8)
        self.assertEqual(row["minimum_attempts_consistent_with_reports"], 8)
        self.assertEqual(row["provider_marker_records_missing"], 8); self.assertEqual(row["provider_usage_unknown_calls"], 8)
        self.assertFalse(row["provider_accounting_complete"]); self.assertIsNone(row["total_basic_credits"])

    def test_missing_usage_does_not_erase_confirmed_zero(self):
        self.add_markers(missing_usage=True); self.add_reports(); row = accounting.collect(self.base, self.root)[0][0]
        self.assertEqual(row["confirmed_provider_credits"], 6); self.assertEqual(row["records_with_credit_usage"], 7)
        self.assertEqual(row["records_without_credit_usage"], 1); self.assertIsNone(row["total_basic_credits"])
        self.assertTrue(row["provider_accounting_complete"]); self.assertFalse(row["provider_credit_usage_complete"])

    def test_failed_postquota_stays_unavailable_beside_successful_prequota(self):
        write(self.directory / "Q2-08-quota-before.json", {"current_plan": "Researcher", "plan_limit": 1000,
            "paygo_usage": 0, "local_monthly_attempts": 535, "checked_at": "2026-11-11T21:54:42+00:00"})
        write(self.directory / "quota-after.json", {"unavailable": "HTTPError", "new_searches_allowed": False})
        observations = accounting.collect(self.base, self.root)[0][0]["quota_observations"]
        self.assertEqual(observations["quota-after.json"], {"unavailable": "HTTPError", "new_searches_allowed": False})
        self.assertNotIn("paygo_usage", observations["quota-after.json"])
        self.assertEqual(observations["Q2-08-quota-before.json"]["local_monthly_attempts"], 535)

    def test_per_case_marker_prefix_cannot_be_hidden_by_equal_global_count(self):
        self.plans["Q2-01"].append({"query": '"Another authored query"', "start": 0, "end": 22})
        self.save_manifests(); paths = self.add_markers(); self.add_reports()
        # Nine claims/markers initially; replace the first prefix marker with
        # a report count swap that preserves the global eight-call total.
        paths[0].unlink()
        path = self.directory / "Q2-01.json"; report = json.loads(path.read_text()); report["web"]["queries_attempted"] = 1; write(path, report)
        row = accounting.collect(self.base, self.root)[0][0]
        self.assertEqual(row["query_attempts"], row["queries_in_completed_reports"])
        self.assertFalse(row["provider_accounting_complete"]); self.assertIsNone(row["total_basic_credits"])
        self.assertEqual(row["provider_marker_records_missing"], 1)

    def test_changed_frozen_local_or_manifest_bytes_are_rejected(self):
        for filename in ("manifest.json", "local-first-use.json"):
            with self.subTest(filename=filename):
                self.save_manifests(); path = self.directory / filename; path.write_text(path.read_text() + " ")
                with self.assertRaisesRegex(ValueError, "artifact hash changed"):
                    accounting.collect(self.base, self.root)

    def test_wrong_report_identity_plan_boolean_and_count_are_rejected(self):
        for mutation in (lambda r: r.update(id="a" * 32), lambda r: r.update(text="other authored input"),
                         lambda r: r["web"].update(query_spans=[]),
                         lambda r: r["web"].update(zero_fallback_enabled=False),
                         lambda r: r["web"].update(queries_attempted=None),
                         lambda r: r["web"].update(queries_attempted=True)):
            with self.subTest(mutation=mutation):
                self.add_reports(); path = self.directory / "Q2-01.json"; report = json.loads(path.read_text()); mutation(report); write(path, report)
                with self.assertRaises(ValueError): accounting.collect(self.base, self.root)

    def test_unbound_marker_and_changed_query_are_rejected(self):
        paths = self.add_markers(); path = paths[0]; record = json.loads(path.read_text()); record["query"] = "different query"; write(path, record)
        with self.assertRaisesRegex(ValueError, "identity differs"):
            accounting.collect(self.base, self.root)
        path.unlink(); write(self.directory / ("f" * 64 + "-search.json"), record)
        with self.assertRaisesRegex(ValueError, "Unbound"):
            accounting.collect(self.base, self.root)

    def test_artifact_symlinks_and_outside_base_are_rejected(self):
        target = self.root / "unrelated.json"; write(target, {})
        path = self.directory / "manifest.json"; path.unlink(); path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            accounting.collect(self.base, self.root)
        with self.assertRaisesRegex(ValueError, "inside"):
            accounting.collect(self.root.parent, self.root)

    def test_accounting_never_reveals_replans_scores_or_mutates(self):
        self.add_markers(); self.add_reports()
        write(self.directory / "page-unread-fetch.json", {"unread_source_body": "not eligible"})
        write(self.base / "query-unfamiliar-reserved-v2/metadata.json", {"closed": True})
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(pilot, "checked_source", side_effect=AssertionError("source read")), \
                patch.object(pilot, "metadata", side_effect=AssertionError("reservation read")), \
                patch.object(pilot, "load_run", side_effect=AssertionError("current-code dependency")), \
                patch.object(pilot, "score_only", side_effect=AssertionError("scorer")), \
                patch.object(pilot.web, "plan_queries", side_effect=AssertionError("planner")), \
                patch.object(pilot, "quota", side_effect=AssertionError("network")):
            rows, hashes = accounting.collect(self.base, self.root)
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after); self.assertEqual(rows[0]["query_attempts"], 8)
        self.assertFalse(any("reserved" in p or "page-" in p for p in hashes))
        self.assertEqual(accounting.collect(self.root / "absent", self.root), ([], {}))


if __name__ == "__main__": unittest.main()
