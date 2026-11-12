"""Independent authored temporary artifacts; never opens actual publication data."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plagiarism.saas import report_identifier
from scripts import publication_pilot_accounting as accounting


def digest(value):
    return sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
    return digest(path.read_bytes())


class PublicationAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "own-data"
        self.out = self.base / accounting.SERIES
        p = accounting.pilot
        self.texts = {cid: " ".join("authored" + cid + "word" + str(i) for i in range(70)) for cid in p.IDS}
        self.primary = {cid: [{"query": '"Authored ' + cid + ' frozen phrase number ' + str(i) + '"', "start": i * 10, "end": i * 10 + 9}
            for i in range(6)] for cid in p.IDS}
        self.wiki = {cid: [{**deepcopy(q), "primary_query_index": i} for i, q in enumerate(self.primary[cid][:2])] for cid in p.IDS}
        self.manifest = {"cases": [{"id": cid, "text_sha256": digest(self.texts[cid])} for cid in p.IDS],
            "input_texts": self.texts, "preparation_failed_ids": list(p.FAILED_IDS), "plan_sha256": p.PLAN_SHA, "amendment_sha256": p.AMENDMENT_SHA}
        self.local = {"first_use": True, "used_for_tuning": False, "plans": self.primary, "wiki_plans": self.wiki}
        choice = {"frozen": True, "git_head": "a" * 40, "frozen_at": "authored-before-reveal", "plan_sha256": p.PLAN_SHA,
            "amendment_sha256": p.AMENDMENT_SHA, "wiki_budget_amendment_sha256": p.WIKI_AMENDMENT_SHA,
            "actual_case_ids": list(p.IDS), "discovery_strategy": p.DISCOVERY, "wiki_followup": True,
            "maximum_total_fresh_queries": 40, "maximum_queries_per_document": 8, "maximum_fresh_tavily_queries": 30,
            "maximum_fresh_mediawiki_queries": 10, "maximum_pages_per_document": 26, "query_strategy": p.web.DENSE_STRATEGY,
            "url_strategy": p.web.PATH_URL_STRATEGY, "named_references": True, "zero_fallback": True,
            "matcher_version": "authored-old-matcher", "html_extraction_version": "authored-old-extractor", "code_sha256": {"historical.py": "f" * 64}}
        canonical = digest(json.dumps(choice, ensure_ascii=False, sort_keys=True, allow_nan=False))
        raw_sha = digest(json.dumps(choice, ensure_ascii=False, indent=2))
        runid = digest(p.PLAN_SHA + p.AMENDMENT_SHA + p.WIKI_AMENDMENT_SHA + raw_sha + canonical)[:32]
        self.run = {"id": runid, "first_use": True, "used_for_tuning": False,
            "manifest_sha256": write(self.out / "manifest.json", self.manifest), "local_sha256": write(self.out / "local-first-use.json", self.local),
            "configuration": {"choice": choice, "choice_sha256": raw_sha, "choice_canonical_sha256": canonical, "code_sha256": choice["code_sha256"]}}
        self.run_sha = write(self.out / "run.json", self.run)
        self.requests = {cid: digest(runid + cid + digest(self.texts[cid]))[:32] for cid in p.IDS}
        self.paid, self.free = {}, {}
        for cid in p.IDS:
            rid = self.requests[cid]
            for index, plan in enumerate(self.primary[cid]):
                self.paid[digest(rid + "\0" + str(index))] = {"case_id": cid, "query_index": index, "query": plan["query"]}
            for index, plan in enumerate(self.wiki[cid]):
                self.free[digest(rid + "\0mediawiki-post\0" + str(index))] = {"case_id": cid, "query_index": index,
                    "primary_query_index": index, "query": plan["query"]}

    def add_markers(self):
        for provider, pool, suffix in (("tavily", self.paid, "search"), ("mediawiki", self.free, "mediawiki")):
            for call, item in pool.items():
                record = {"request_id": call, **item, "provider": provider, "state": "complete", "results": []}
                if provider == "tavily":
                    record.update(search_depth="basic", usage={"credits": 1})
                else:
                    record.update(usage={"public_queries": 1, "cost_kzt": 0, "cached": False})
                write(self.out / (call + "-" + suffix + ".json"), record)
                write(self.out / (call + "-" + provider + "-intent.json"), {"request_id": call, **item, "provider": provider, "state": "submitted"})

    def add_reports(self):
        p, choice = accounting.pilot, self.run["configuration"]["choice"]
        for cid in p.IDS:
            spans = lambda rows: [{k: q[k] for k in ("start", "end")} for q in rows]
            write(self.out / (cid + ".json"), {"id": report_identifier(self.requests[cid]), "text": self.texts[cid], "web": {
                "query_strategy": choice["query_strategy"], "url_strategy": choice["url_strategy"], "matcher_version": choice["matcher_version"],
                "named_references_enabled": True, "zero_fallback_enabled": True, "post_wiki_enabled": True, "discovery_strategy": p.DISCOVERY,
                "primary_queries_attempted": 6, "post_wiki_queries_attempted": 2, "queries_attempted": 8, "total_provider_requests": 8,
                "primary_query_spans": spans(self.primary[cid]), "query_spans": spans(self.primary[cid] + self.wiki[cid]),
                "post_wiki_query_plan": self.wiki[cid], "primary_provider": "tavily", "secondary_provider": "mediawiki",
                "max_provider_requests": 8, "max_page_attempts": 26, "urls_attempted": 0}})
            write(self.out / (cid + "-request.json"), {"request_id": self.requests[cid], "text_sha256": digest(self.texts[cid]), "planned_queries": 8})
            write(self.out / (cid + "-submission-intent.json"), {"request_id": self.requests[cid], "text_sha256": digest(self.texts[cid]), "run_sha256": self.run_sha})

    def rows(self):
        paid, free, hashes = accounting.collect(self.base, self.root)
        return paid[0], free[0], hashes

    def test_complete_paid_and_anonymous_rows_never_mix_monthly_credits(self):
        self.add_markers(); self.add_reports()
        paid, free, hashes = self.rows()
        self.assertEqual((paid["query_attempts"], paid["confirmed_provider_credits"], paid["total_basic_credits"]), (30, 30, 30))
        self.assertEqual((free["anonymous_provider_intents"], free["total_new_anonymous_queries"]), (10, 10))
        self.assertFalse(free["credit_API"])
        self.assertEqual(free["new_Tavily_calls"], 0)
        self.assertNotIn("confirmed_provider_credits", free)
        self.assertEqual(paid["saved_documents"], 5)
        self.assertEqual(paid["preparation_failed_case_ids"], ["P3-01", "P3-02", "P3-03"])
        self.assertEqual(len(hashes), 98)

    def test_reports_without_markers_preserve_unknown_instead_of_hardcoded_results(self):
        self.add_reports()
        paid, free, _ = self.rows()
        self.assertEqual((paid["query_attempts"], paid["minimum_attempts_consistent_with_reports"]), (0, 30))
        self.assertEqual(paid["confirmed_provider_credits"], 0)
        self.assertIsNone(paid["total_basic_credits"])
        self.assertEqual(free["anonymous_provider_intents"], 0)
        self.assertEqual(free["minimum_attempts_consistent_with_reports"], 10)
        self.assertIsNone(free["total_new_anonymous_queries"])

    def test_lost_paid_marker_is_unknown_once_and_durable_intent_is_hashed(self):
        self.add_markers(); self.add_reports()
        call = next(iter(self.paid)); (self.out / (call + "-search.json")).unlink()
        paid, free, hashes = self.rows()
        self.assertEqual(paid["query_attempts"], 29)
        self.assertEqual(paid["minimum_attempts_consistent_with_reports"], 30)
        self.assertEqual(paid["provider_marker_records_missing"], 1)
        self.assertEqual(paid["provider_usage_unknown_calls"], 1)
        self.assertIsNone(paid["total_basic_credits"])
        self.assertTrue(any(key.endswith(call + "-tavily-intent.json") for key in hashes))

    def test_paid_none_and_explicit_zero_stay_distinct_from_unknown_public_usage(self):
        self.add_markers(); self.add_reports()
        for call in self.paid:
            path = self.out / (call + "-search.json"); record = json.loads(path.read_text()); record["usage"] = {"credits": 0}; write(path, record)
        call = next(iter(self.free)); path = self.out / (call + "-mediawiki.json")
        record = json.loads(path.read_text()); record["usage"] = None; write(path, record)
        paid, free, _ = self.rows()
        self.assertEqual(paid["total_basic_credits"], 0)
        self.assertEqual(paid["records_with_credit_usage"], 30)
        self.assertIsNone(free["total_new_anonymous_queries"])
        call = next(iter(self.paid)); path = self.out / (call + "-search.json")
        record = json.loads(path.read_text()); record["usage"] = None; write(path, record)
        paid, _, _ = self.rows()
        self.assertEqual(paid["records_without_credit_usage"], 1)
        self.assertIsNone(paid["total_basic_credits"])

    def test_forged_marker_provider_call_query_or_index_is_rejected(self):
        self.add_markers(); self.add_reports()
        call = next(iter(self.free)); path = self.out / (call + "-mediawiki.json"); original = json.loads(path.read_text())
        for field, value in (("request_id", "f" * 64), ("query", "Another query"), ("provider", "tavily"), ("query_index", 1)):
            with self.subTest(field=field):
                write(path, {**original, field: value})
                with self.assertRaises(ValueError): self.rows()
        write(path, original)
        paid_call = next(iter(self.paid)); paid_path = self.out / (paid_call + "-search.json")
        paid_record = json.loads(paid_path.read_text()); paid_record["provider"] = "mediawiki"; write(paid_path, paid_record)
        with self.assertRaisesRegex(ValueError, "provider kind"): self.rows()
        paid_record.pop("provider"); write(paid_path, paid_record)
        path.rename(self.out / ("e" * 64 + "-mediawiki.json"))
        with self.assertRaisesRegex(ValueError, "Unbound"): self.rows()

    def test_report_count_mismatch_retains_subtotal_but_total_unknown(self):
        self.add_markers(); self.add_reports()
        cid = accounting.pilot.IDS[0]; path = self.out / (cid + ".json"); report = json.loads(path.read_text())
        report["web"].update(primary_queries_attempted=0, queries_attempted=2, total_provider_requests=2)
        write(path, report)
        paid, free, _ = self.rows()
        self.assertEqual(paid["confirmed_provider_credits"], 30)
        self.assertIsNone(paid["total_basic_credits"])
        self.assertTrue(any(x["code"] == "markers_beyond_reported_attempts" for x in paid["accounting_issues"]))

    def test_changed_frozen_manifest_run_and_submission_binding_fail_closed(self):
        self.add_markers(); self.add_reports()
        original = (self.out / "manifest.json").read_bytes()
        write(self.out / "manifest.json", {**self.manifest, "preparation_failed_ids": []})
        with self.assertRaisesRegex(ValueError, "SHA256"): self.rows()
        (self.out / "manifest.json").write_bytes(original)
        bad = deepcopy(self.run); bad["id"] = "b" * 32; write(self.out / "run.json", bad)
        with self.assertRaisesRegex(ValueError, "identity"): self.rows()
        write(self.out / "run.json", self.run)
        path = self.out / (accounting.pilot.IDS[0] + "-submission-intent.json")
        record = json.loads(path.read_text()); record["run_sha256"] = "c" * 64; write(path, record)
        with self.assertRaisesRegex(ValueError, "request intent"): self.rows()

    def test_saved_failed_post_quota_is_unavailable_not_zero_or_retried(self):
        self.add_markers(); self.add_reports()
        write(self.out / "quota-after.json", {"unavailable": "HTTPError", "new_searches_allowed": False})
        paid, free, _ = self.rows()
        self.assertEqual(paid["quota_observations"]["quota-after.json"], {"unavailable": "HTTPError", "new_searches_allowed": False})

    def test_no_current_code_source_reservations_replan_pages_or_network_and_no_mutation(self):
        self.add_markers(); self.add_reports()
        write(self.out / "pages/never-read.json", {"private_prose": "not accounting"})
        write(self.base / "other-series/never-read.json", {"other": True})
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(accounting.pilot, "load_run", side_effect=AssertionError("No live-loader")), \
                patch.object(accounting.pilot, "code_hashes", side_effect=AssertionError("No current-code read")), \
                patch.object(accounting.pilot, "metadata", side_effect=AssertionError("No source reservation")), \
                patch.object(accounting.pilot.web, "plan_queries", side_effect=AssertionError("No replanning")), \
                patch("socket.socket", side_effect=AssertionError("No network")):
            paid, free, hashes = self.rows()
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(any("never-read" in path for path in hashes))
        self.assertEqual(accounting.collect(self.root / "missing", self.root), ([], [], {}))
        path = next(self.out.glob("*-search.json")); path.unlink(); path.symlink_to(self.out / "pages/never-read.json")
        with self.assertRaisesRegex(ValueError, "symlinks"): self.rows()


if __name__ == "__main__":
    unittest.main()
