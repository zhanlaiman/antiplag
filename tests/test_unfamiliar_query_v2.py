"""Authored temporary artifacts only; reserved RFC prose is never opened."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import run_unfamiliar_query_v2 as probe


class UnfamiliarQueryV2Guards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = {"id": "Q2-01", "text_sha256": sha256(b"authored text").hexdigest()}
        self.rid = "a" * 32

    def test_default_mode_cannot_open_source_or_contact_network(self):
        fake = {"documents": 8, "sources": [{"id": "Authored A"}, {"id": "Authored B"}]}
        with patch.object(probe, "metadata", return_value=fake), \
                patch.object(probe, "checked_source", side_effect=AssertionError("source read")), \
                patch.object(probe, "quota", side_effect=AssertionError("network")), \
                patch("builtins.print"):
            probe.main([])

    def test_failed_freeze_stops_before_source_or_output_creation(self):
        with patch.object(probe, "OUT", self.root / "uncreated"), \
                patch.object(probe, "metadata", return_value={}), \
                patch.object(probe, "check_freeze", side_effect=ValueError("unfrozen")), \
                patch.object(probe, "checked_source", side_effect=AssertionError("source read")):
            with self.assertRaisesRegex(ValueError, "unfrozen"):
                probe.reveal("a" * 40)
            self.assertFalse((self.root / "uncreated").exists())

    def test_wrong_commit_stops_before_choice_or_source_read(self):
        with patch.object(probe.subprocess, "check_output", return_value="a" * 40), \
                patch.object(probe, "CHOICE", self.root / "missing-choice.json"):
            with self.assertRaisesRegex(ValueError, "tested frozen commit"):
                probe.check_freeze("b" * 40)

    def test_unfrozen_choice_is_rejected_before_git_or_source_work(self):
        path = self.root / "choice.json"
        path.write_text(json.dumps({"frozen": False}))
        with patch.object(probe, "CHOICE", path), \
                patch.object(probe.subprocess, "check_output", return_value="a" * 40), \
                patch.object(probe.subprocess, "run", side_effect=AssertionError("git source work")):
            with self.assertRaisesRegex(ValueError, "explicitly frozen"):
                probe.check_freeze("a" * 40)

    def test_fixed_construction_keeps_lengths_positions_and_original_source_span(self):
        base = " ".join(["Authored"] * 35)
        source = "\n\n".join(["alpha beta gamma delta epsilon zeta eta theta iota kappa"] * 30)
        assignments = []
        for index in range(6):
            assignments.append({"id": probe.IDS[index], "source_id": "authored-source", "base_id": "A",
                "insertion_words": (10, 20, 40)[index % 3], "source_start_fraction": ((1, 4), (1, 2), (3, 4))[index % 3],
                "target_base_word_index": (0, 17, 35, 35, 0, 17)[index]})
        plan = {"assignments": assignments, "authored_bases": {"A": base},
                "authored_controls": {cid: base for cid in probe.IDS[6:]}}
        cases, texts = probe.construct_cases(plan, {"authored-source": {
            "id": "authored-source", "url": "https://example.test/authored", "text": source}})
        self.assertEqual([c["id"] for c in cases], list(probe.IDS))
        for case, assignment in zip(cases[:6], assignments):
            evidence = case["expected_evidence"][0]
            self.assertEqual(len(probe._tokens(texts[case["id"]])), 35 + assignment["insertion_words"])
            self.assertEqual(source[slice(*evidence["source_span"])], evidence["source_quote"])
            self.assertEqual(texts[case["id"]][slice(*evidence["target_span"])], evidence["target_quote"])
            self.assertEqual(len(probe._tokens(evidence["target_quote"])), assignment["insertion_words"])

    def test_failed_construction_never_selects_another_span(self):
        plan = {"assignments": [{"id": "Q2-01", "source_id": "s", "base_id": "A", "insertion_words": 40,
            "source_start_fraction": [3, 4], "target_base_word_index": 17}],
            "authored_bases": {"A": " ".join(["Authored"] * 35)}, "authored_controls": {}}
        with self.assertRaisesRegex(ValueError, "no replacement"):
            probe.construct_cases(plan, {"s": {"id": "s", "url": "https://example.test/", "text": "short source"}})

    def test_authored_reveal_persists_eight_fixed_cases_once_without_network(self):
        base = " ".join(["Authored"] * 35)
        source = " ".join(["alpha beta gamma delta epsilon zeta eta theta iota kappa"] * 30)
        assignments = [{"id": probe.IDS[i], "source_id": "authored-source", "base_id": "A",
            "insertion_words": (10, 20, 40)[i % 3], "source_start_fraction": ((1, 4), (1, 2), (3, 4))[i % 3],
            "target_base_word_index": (0, 17, 35, 35, 0, 17)[i]} for i in range(6)]
        plan = {"sources": [{"id": "authored-source", "text_sha256": sha256(source.encode()).hexdigest()}],
            "assignments": assignments, "authored_bases": {"A": base},
            "authored_controls": {cid: base for cid in probe.IDS[6:]}}
        configuration = {"choice_sha256": "authored-choice", "code_sha256": {},
                         "choice": {"query_strategy": probe.web.DENSE_STRATEGY}}
        page = {"id": "authored-source", "url": "https://example.test/authored", "title": "Authored", "text": source}
        with patch.object(probe, "OUT", self.root), patch.object(probe, "metadata", return_value=plan), \
                patch.object(probe, "check_freeze", return_value=configuration), \
                patch.object(probe, "checked_source", return_value=page) as read_source, \
                patch.object(probe, "code_hashes", return_value={}), \
                patch.object(probe, "quota", side_effect=AssertionError("no network")):
            run, local = probe.reveal("a" * 40)
            self.assertEqual(local["summary"]["expected_copied_words"], 140)
            self.assertEqual(local["summary"]["known_page_copied_words_expected_source"], 140)
            self.assertLessEqual(local["summary"]["planned_queries"]["selected"], 48)
            self.assertEqual(read_source.call_count, 1)
            with patch.object(probe.web, "plan_queries", side_effect=AssertionError("no replan")):
                same_run, same_local = probe.reveal("a" * 40)
            self.assertEqual(same_run, run)
            self.assertEqual(same_local, local)
            self.assertEqual(read_source.call_count, 1)

    def test_saved_request_get404_can_never_post_again(self):
        path = self.root / "request.json"
        probe.dump(path, {"request_id": self.rid, "text_sha256": self.case["text_sha256"], "planned_queries": 6})
        http = Mock(return_value=(404, {}))
        preflight = Mock(side_effect=AssertionError("new quota or POST"))
        with self.assertRaisesRegex(RuntimeError, "do not POST again"):
            probe.recover_or_submit(http, path, self.rid, self.case, "authored text", 6, preflight)
        self.assertEqual(http.call_args_list[0].args, ("/api/jobs/" + self.rid,))
        self.assertEqual(http.call_count, 1)
        preflight.assert_not_called()

    def test_new_submission_gets_first_then_saves_identity_before_post(self):
        path = self.root / "request.json"
        calls = []
        def http(route, body=None, missing_ok=False):
            calls.append((route, body))
            if body is None:
                self.assertTrue(missing_ok)
                self.assertFalse(path.exists())
                return 404, {}
            self.assertTrue(path.exists())
            self.assertEqual(set(body), {"request_id", "text", "internet_consent"})
            self.assertEqual(body["text"], "authored text")
            return 200, {"job": {"status": "queued"}}
        preflight = Mock()
        job = probe.recover_or_submit(http, path, self.rid, self.case, "authored text", 6, preflight)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(calls[0][0], "/api/jobs/" + self.rid)
        preflight.assert_called_once()

    def test_preexisting_job_without_sidecar_is_recovered_by_get(self):
        http = Mock(return_value=(200, {"job": {"status": "complete"}}))
        preflight = Mock(side_effect=AssertionError("no new POST"))
        path = self.root / "request.json"
        job = probe.recover_or_submit(http, path, self.rid, self.case, "authored text", 6, preflight)
        self.assertEqual(job["status"], "complete")
        self.assertTrue(json.loads(path.read_text())["recovered_existing_before_post"])
        self.assertEqual(http.call_count, 1)

    def test_provider_attempt_is_saved_before_call_and_never_repeated(self):
        call_id = "b" * 64
        def failing(query, rid):
            record = json.loads((self.root / (call_id + "-search.json")).read_text())
            self.assertEqual(record["state"], "submitted")
            raise OSError("authored failure")
        real = Mock()
        real.search.side_effect = failing
        recorded = probe.RecordedSearch(real, self.root, {call_id: {"query": "authored query", "case_id": "Q2-01"}})
        with self.assertRaises(OSError):
            recorded.search("authored query", call_id)
        with self.assertRaisesRegex(RuntimeError, "never automatically repeated"):
            recorded.search("authored query", call_id)
        self.assertEqual(real.search.call_count, 1)

    def test_provider_rejects_changed_query_and_document_or_global_caps(self):
        real = Mock()
        expected = {"query": "authored query", "case_id": "Q2-01"}
        recorded = probe.RecordedSearch(real, self.root, {"new": expected})
        with self.assertRaisesRegex(RuntimeError, "differs from"):
            recorded.search("changed query", "new")
        for index in range(6):
            probe.dump(self.root / (f"{index}-search.json"), {"case_id": "Q2-01"})
        with self.assertRaisesRegex(RuntimeError, "cap exhausted"):
            recorded.search("authored query", "new")
        for index in range(6, 48):
            probe.dump(self.root / (f"{index}-search.json"), {"case_id": "Q2-02"})
        recorded.allowed["new"]["case_id"] = "Q2-08"
        with self.assertRaisesRegex(RuntimeError, "cap exhausted"):
            recorded.search("authored query", "new")
        real.search.assert_not_called()

    def test_score_only_uses_own_reports_without_reservations_plans_or_network(self):
        choice = {"query_strategy": "authored", "url_strategy": "authored", "matcher_version": "authored", "named_references": False}
        text, cases = "One authored sentence with seven separate words.", []
        for index, cid in enumerate(probe.IDS):
            case = {"id": cid, "text_sha256": sha256(text.encode()).hexdigest(), "control": index >= 6,
                "expected_evidence": [] if index >= 6 else [{"target_span": [0, len(text)], "source_urls": ["https://example.test/authored"]}]}
            cases.append(case)
            report = {"id": str(index) * 32, "text": text, "scope": "web_search", "sources": [], "matches": [],
                "web": {"query_strategy": "authored", "url_strategy": "authored", "matcher_version": "authored",
                    "named_references_enabled": False, "queries_attempted": 0, "sources_checked": 0,
                    "discovered_urls": [], "errors": [], "status": "partial"}, "stats": {"uniqueness_percent": None}}
            if index in {0, 7}:
                url = "https://example.test/mirror" if index == 0 else "https://example.test/authored"
                report["sources"] = [{"id": "authored-source", "url": url, "text": text}]
                report["matches"] = [{"kind": "exact", "source_id": "authored-source", "source_url": url,
                    "start": 0, "end": len(text), "source_start": 0, "source_end": len(text),
                    "excerpt": text, "source_excerpt": text}]
                report["web"]["sources_checked"] = 1
            probe.dump(self.root / (cid + ".json"), report)
        run = {"manifest_sha256": "fake", "configuration": {"choice": choice}}
        with patch.object(probe, "OUT", self.root), patch.object(probe, "load_run", return_value=run), \
                patch.object(probe, "checked_json", return_value={"cases": cases}), \
                patch.object(probe, "metadata", side_effect=AssertionError("reserve metadata read")), \
                patch.object(probe, "checked_source", side_effect=AssertionError("source read")), \
                patch.object(probe.web, "plan_queries", side_effect=AssertionError("replan")), \
                patch.object(probe, "quota", side_effect=AssertionError("network")):
            result = probe.score_only()
        self.assertEqual(result["summary"]["reports"], 8)
        self.assertEqual(result["summary"]["copied_words_any_source"], 7)
        self.assertEqual(result["summary"]["copied_words_expected_source"], 0)
        self.assertEqual(result["cases"][0]["evidence"][0]["any_source_words"], 7)
        self.assertEqual(result["cases"][0]["evidence"][0]["expected_source_words"], 0)
        self.assertEqual(result["summary"]["documents_without_read_pages"], 6)
        self.assertEqual(result["summary"]["control_exact_pairs"], 1)
        self.assertIsNone(result["summary"]["confirmed_false_pairs"])
        pair = result["unexpected_pairs_for_review"][0]
        self.assertEqual(pair["target_excerpt"], text)
        self.assertEqual(pair["source_excerpt"], text)
        self.assertTrue(pair["grounded"])
        self.assertEqual(pair["review"], "needs_pair_review")


if __name__ == "__main__":
    unittest.main()
