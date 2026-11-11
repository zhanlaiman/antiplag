"""Authored temporary paired artifacts only; no private pilot code or inputs."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from plagiarism.saas import report_identifier
from scripts import short_paragraph_pilot_accounting as accounting


def digest(value):
    return sha256(value.encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def authored_manifest():
    texts = {cid: ("Authored independent paragraph for " + cid + ". ") * 30 for cid in accounting.CASE_IDS}
    plans = {}
    for cid in accounting.CASE_IDS:
        baseline = [{"query": f'"Authored baseline phrase {index} for {cid}"', "start": index * 20, "end": index * 20 + 19}
                    for index in range(6)]
        selected = deepcopy(baseline[:5]) + [{"query": f'"Authored replacement phrase for {cid}"', "start": 140, "end": 179}]
        plans[cid] = {"baseline": baseline, "selected": selected}
    config = {"version": accounting.VERSION, "hypothesis_freeze_sha256": accounting.HYPOTHESIS_SHA,
        "case_ids": list(accounting.CASE_IDS), "max_actual_provider_calls": 22, "default_changed": False,
        "plans": plans, "input_sha256": {cid: digest(text) for cid, text in texts.items()},
        "code_sha256": {"authored-code.py": "f" * 64}}
    run_id = digest(json.dumps(config, sort_keys=True, ensure_ascii=False, allow_nan=False))
    requests = {cid + "-" + role: digest(run_id + cid + role)[:32]
                for cid in accounting.CASE_IDS for role in accounting.ROLES}
    allowed, jobs = {}, {}
    for cid in accounting.CASE_IDS:
        if cid != "D30":
            for index, item in enumerate(plans[cid]["baseline"]):
                allowed[digest(run_id + cid + "baseline" + str(index))] = {
                    "case_id": cid + "-baseline", "query_index": index, "query": item["query"]}
        new = digest(run_id + cid + "new6")
        allowed[new] = {"case_id": cid + "-selected", "query_index": 0, "query": plans[cid]["selected"][5]["query"]}
        for role in accounting.ROLES:
            job = cid + "-" + role
            jobs[job] = {}
            for index, item in enumerate(plans[cid][role]):
                provider = None if cid == "D30" else digest(run_id + cid + "baseline" + str(index))
                if role == "selected" and index == 5:
                    provider = new
                jobs[job][digest(requests[job] + "\0" + str(index))] = {"query": item["query"], "provider_call_id": provider}
    return {"id": run_id, "configuration": config, "request_ids": requests}, texts, allowed, jobs


class ShortParagraphAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "data/private/web-evaluation"
        self.directory = self.base / accounting.SERIES[0]
        self.run, self.texts, self.allowed, self.jobs = authored_manifest()
        write(self.directory / "run.json", self.run)

    def add_markers(self):
        for call, identity in self.allowed.items():
            write(self.directory / (call + "-search.json"), {**identity, "request_id": call,
                "search_depth": "basic", "state": "complete", "usage": {"credits": 1}})

    def add_reports(self):
        seen = set()
        manifest_sha = sha256((self.directory / "run.json").read_bytes()).hexdigest()
        for job, mapping in self.jobs.items():
            cid, role = job.rsplit("-", 1)
            events = []
            for logical, item in mapping.items():
                provider = item["provider_call_id"]
                kind = "saved_prior_D30" if provider is None else "shared_fresh_reply" if provider in seen else "new_provider_call"
                if provider:
                    seen.add(provider)
                events.append({"query_call_id": logical, **item, "kind": kind, "state": "complete"})
            plan = self.run["configuration"]["plans"][cid][role]
            write(self.directory / (job + ".json"), {"id": report_identifier(self.run["request_ids"][job]), "text": self.texts[cid],
                "web": {"queries_attempted": 6, "query_strategy": accounting.VERSION + ":" + role,
                    "experimental_query_plan": plan, "query_spans": [{k: p[k] for k in ("start", "end")} for p in plan],
                    "source_code_sha256": self.run["configuration"]["code_sha256"],
                    "experiment_manifest_sha256": manifest_sha, "actual_query_events": events}})
            write(self.directory / (job + "-request.json"), {"request_id": self.run["request_ids"][job],
                "text_sha256": self.run["configuration"]["input_sha256"][cid], "planned_queries": 6})

    def row(self):
        return accounting.collect(self.base, self.root)[0][0]

    def test_canonical_formulas_match_fixed_twenty_two_provider_and_forty_eight_logical_ids(self):
        _, allowed, jobs = accounting.manifest_identity(self.run, accounting.SERIES[0])
        self.assertEqual(allowed, self.allowed)
        self.assertEqual(jobs, self.jobs)
        self.assertEqual(len(allowed), 22)
        self.assertEqual(sum(map(len, jobs.values())), 48)
        self.assertEqual(sum(item["provider_call_id"] is None for job in jobs.values() for item in job.values()), 11)

    def test_complete_reports_count_twenty_two_calls_without_charging_fifteen_reuses_or_eleven_old_replies(self):
        self.add_markers()
        self.add_reports()
        rows, hashes = accounting.collect(self.base, self.root)
        row = rows[0]
        self.assertEqual(row["saved_documents"], 8)
        self.assertEqual(row["submitted_documents"], 8)
        self.assertEqual(row["query_attempts"], 22)
        self.assertEqual(row["queries_in_completed_reports"], 48)
        self.assertEqual(row["total_basic_credits"], 22)
        self.assertTrue(row["provider_credit_usage_complete"])
        self.assertEqual(row["query_events_by_kind"], {"new_provider_call": 22, "shared_fresh_reply": 15, "saved_prior_D30": 11})
        self.assertEqual(len(hashes), 39)
        for relative, value in hashes.items():
            self.assertEqual(sha256((self.root / relative).read_bytes()).hexdigest(), value)

    def test_missing_all_markers_counts_twenty_two_unknown_canonical_calls_rather_than_forty_eight_or_zero(self):
        self.add_reports()
        row = self.row()
        self.assertEqual(row["query_attempts"], 0)
        self.assertEqual(row["queries_in_completed_reports"], 48)
        self.assertEqual(row["minimum_attempts_consistent_with_reports"], 22)
        self.assertEqual(row["provider_marker_records_missing"], 22)
        self.assertEqual(row["provider_usage_unknown_calls"], 22)
        self.assertEqual(row["confirmed_provider_credits"], 0)
        self.assertIsNone(row["total_basic_credits"])

    def test_missing_shared_marker_is_one_missing_call_even_when_both_roles_reference_it(self):
        self.add_markers()
        self.add_reports()
        common = next(iter(self.jobs["A0008-baseline"].values()))["provider_call_id"]
        (self.directory / (common + "-search.json")).unlink()
        row = self.row()
        self.assertEqual(row["query_attempts"], 21)
        self.assertEqual(row["provider_marker_records_missing"], 1)
        self.assertEqual(row["minimum_attempts_consistent_with_reports"], 22)
        self.assertFalse(row["provider_accounting_complete"])
        self.assertIsNone(row["total_basic_credits"])

    def test_explicit_zero_is_known_and_missing_usage_remains_unknown(self):
        self.add_markers()
        self.add_reports()
        identifiers = list(self.allowed)
        first = self.directory / (identifiers[0] + "-search.json")
        record = json.loads(first.read_text())
        record["usage"] = {"credits": 0}
        write(first, record)
        self.assertEqual(self.row()["total_basic_credits"], 21)
        second = self.directory / (identifiers[1] + "-search.json")
        record = json.loads(second.read_text())
        record["usage"] = None
        write(second, record)
        row = self.row()
        self.assertEqual(row["records_with_credit_usage"], 21)
        self.assertEqual(row["confirmed_provider_credits"], 20)
        self.assertEqual(row["provider_usage_unknown_calls"], 1)
        self.assertIsNone(row["total_basic_credits"])

    def test_changed_manifest_request_or_pair_shape_is_rejected(self):
        for mutate in (lambda r: r.update(id="f" * 64),
                       lambda r: r["request_ids"].update({"D30-selected": "f" * 32}),
                       lambda r: r["configuration"]["plans"]["A0008"]["selected"].__setitem__(5, deepcopy(r["configuration"]["plans"]["A0008"]["baseline"][0]))):
            changed = deepcopy(self.run)
            mutate(changed)
            write(self.directory / "run.json", changed)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                accounting.collect(self.base, self.root)

    def test_forged_provider_identity_and_invalid_credit_usage_are_rejected(self):
        self.add_markers()
        path = self.directory / (next(iter(self.allowed)) + "-search.json")
        original = json.loads(path.read_text())
        for change in ({"query": "wrong"}, {"request_id": "f" * 64}, {"query_index": False},
                       {"case_id": "D30-selected"}, {"search_depth": "advanced"}, {"usage": {"credits": True}}):
            write(path, {**original, **change})
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.row()

    def test_report_input_plan_code_and_manifest_bindings_are_strict(self):
        self.add_markers()
        self.add_reports()
        path = self.directory / "A0008-selected.json"
        original = json.loads(path.read_text())
        for key, value in (("id", "f" * 32), ("text", "forged"), ("queries_attempted", True),
                           ("query_strategy", "wrong"), ("experimental_query_plan", []), ("query_spans", []),
                           ("source_code_sha256", {}), ("experiment_manifest_sha256", "f" * 64)):
            changed = deepcopy(original)
            (changed if key in ("id", "text") else changed["web"])[key] = value
            write(path, changed)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.row()

    def test_wrong_event_kind_provider_or_repeated_logical_id_leaves_total_unknown(self):
        self.add_markers()
        self.add_reports()
        path = self.directory / "A0008-selected.json"
        original = json.loads(path.read_text())
        for change in ({"provider_call_id": "f" * 64}, {"query": "wrong"}, {"kind": "saved_prior_D30"}, {"state": "submitted"}):
            changed = deepcopy(original)
            changed["web"]["actual_query_events"][0].update(change)
            write(path, changed)
            with self.subTest(change=change):
                row = self.row()
                self.assertFalse(row["provider_accounting_complete"])
                self.assertIsNone(row["total_basic_credits"])
        changed = deepcopy(original)
        changed["web"]["actual_query_events"][1] = deepcopy(changed["web"]["actual_query_events"][0])
        write(path, changed)
        self.assertFalse(self.row()["provider_accounting_complete"])

    def test_submitted_without_report_and_postquota_failure_remain_explicit(self):
        self.add_markers()
        self.add_reports()
        (self.directory / "A0008-selected.json").unlink()
        write(self.directory / "quota-before.json", {"paygo_usage": 0, "local_monthly_attempts": 538})
        write(self.directory / "quota-after.json", {"unavailable": "HTTPError", "new_calls_allowed": False})
        row = self.row()
        self.assertFalse(row["provider_accounting_complete"])
        self.assertIsNone(row["total_basic_credits"])
        self.assertEqual(row["quota_observations"]["quota-before.json"]["paygo_usage"], 0)
        self.assertNotIn("paygo_usage", row["quota_observations"]["quota-after.json"])

    def test_readonly_scope_ignores_private_python_pages_inputs_and_other_series(self):
        self.add_markers()
        self.add_reports()
        for relative in ("dev-short-paragraph-pair-v2/probe.py", "dev-short-paragraph-pair-v2/pages/D30/page.json",
                         "dev-short-paragraph-pair-v2/D30-input.json", "query-unfamiliar-reserved-v2/manifest.json",
                         "materials/final-70/key.json", "dev-mediawiki-singleton-v3/run.json"):
            path = self.base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not eligible JSON or executable code")
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        rows, hashes = accounting.collect(self.base, self.root)
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(rows[0]["query_attempts"], 22)
        self.assertFalse(any("reserved" in path or "final-70" in path or "/pages/" in path or path.endswith(".py") for path in hashes))

    def test_symlink_and_outside_base_rejected_and_missing_series_has_no_implied_calls(self):
        self.add_markers()
        path = next(self.directory.glob("*-search.json"))
        path.unlink()
        target = self.root / "outside.json"
        write(target, {})
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.row()
        with self.assertRaisesRegex(ValueError, "inside"):
            accounting.collect(self.root.parent, self.root)
        self.assertEqual(accounting.collect(self.root / "absent", self.root), ([], {}))


if __name__ == "__main__":
    unittest.main()
