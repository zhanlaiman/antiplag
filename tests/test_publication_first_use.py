"""Authored source/host fixtures only; actual EPA and other reservations stay closed."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from plagiarism.saas import report_identifier
from scripts import run_publication_first_use as pilot


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    return pilot.digest(path.read_bytes())


class PublicationFirstUseTests(unittest.TestCase):
    def setUp(self):
        # These are newly authored fake runs against the current production
        # policy. Keep the real historical driver's pinned version untouched.
        from plagiarism.web_wiki_followup import DISCOVERY_STRATEGY
        current_policy = patch.object(pilot, "DISCOVERY", DISCOVERY_STRATEGY)
        current_policy.start()
        self.addCleanup(current_policy.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "own-run"
        self.body = self.root / "reserved-attempt.json"
        paragraphs = [" ".join(f"publication{block}word{index}" for index in range(70)) for block in range(6)]
        self.source_text = "\n\n".join(paragraphs)
        source_url = "https://authored-publication.test/source"
        outcome_sha = write(self.body, {"state": "complete", "fetched": {"url": source_url,
            "text": self.source_text, "text_sha256": pilot.digest(self.source_text), "title": "Authored source"}})
        selections = []
        for length, rank in ((10, 1), (20, 2), (40, 3)):
            first = self.source_text.index(paragraphs[rank])
            end = first + pilot._tokens(paragraphs[rank])[length - 1].end
            selections.append({"insertion_words": length, "eligible_block_rank_zero_based": rank,
                "source_character_span": [first, end], "selected_raw_substring_sha256": pilot.digest(self.source_text[first:end])})
        epa = {"final_url": source_url, "text_sha256": pilot.digest(self.source_text),
               "eligible_newline_blocks": 6, "selected_insertions": selections}
        epa_path = self.root / "source-metadata.json"
        epa_sha = write(epa_path, epa)
        hosts = {}
        for name, prefix in (("A", "theatre"), ("B", "архив")):
            text = " ".join(prefix + str(index) for index in range(180))
            path = self.root / ("host-" + name + ".txt")
            path.write_text(text)
            hosts[name] = {"path": path.name, "text_sha256": pilot.digest(text)}
        plan_path = self.root / "sampling-plan.json"
        plan_sha = write(plan_path, {"sampler": "Authored metadata only"})
        assignments = [{"id": cid, "base_id": "B", "target_base_word_index": position,
            "insertion_words": selection["insertion_words"], "source_character_span": selection["source_character_span"],
            "selected_raw_substring_sha256": selection["selected_raw_substring_sha256"]}
            for cid, position, selection in zip(pilot.IDS[:3], (180, 0, 90), selections)]
        self.amendment = {"original_sampling_plan_sha256": plan_sha, "sampler_unchanged": True,
            "actual_future_case_ids": list(pilot.IDS), "failed_cases_not_replaced": list(pilot.FAILED_IDS),
            "future_maximum_total_queries": 30, "epa_sampler_metadata_sha256": epa_sha,
            "source_outcomes_sha256": {"epa-start-saving": outcome_sha}, "hosts": hosts, "assignments": assignments}
        amendment_path = self.root / "amendment.json"
        amendment_sha = write(amendment_path, self.amendment)
        budget_path = self.root / "wiki-budget.json"
        budget_sha = write(budget_path, {"actual_case_ids": list(pilot.IDS),
            "preparation_failed_case_ids_not_replaced": list(pilot.FAILED_IDS), "primary_tavily_basic_cap_total": 30,
            "secondary_anonymous_mediawiki_cap_total": 10, "physical_provider_api_cap_total": 40,
            "physical_page_attempt_cap_per_case": 26, "sampler_raw_spans_ranks_hosts_assignments_and_controls_unchanged": True})
        self.code = {"server.py": "f" * 64}
        self.choice = {"frozen": True, "git_head": "a" * 40, "plan_sha256": plan_sha,
            "amendment_sha256": amendment_sha, "actual_case_ids": list(pilot.IDS),
            "wiki_budget_amendment_sha256": budget_sha, "maximum_total_fresh_queries": 40, "maximum_queries_per_document": 8,
            "maximum_fresh_tavily_queries": 30, "maximum_fresh_mediawiki_queries": 10,
            "maximum_pages_per_document": 26, "wiki_followup": True,
            "matcher_version": pilot.MATCHER_VERSION, "html_extraction_version": pilot.HTML_EXTRACTION_VERSION,
            "discovery_strategy": pilot.DISCOVERY, "query_strategy": pilot.web.DENSE_STRATEGY,
            "url_strategy": pilot.web.PATH_URL_STRATEGY, "named_references": True, "zero_fallback": True,
            "pdf_alternative_engine": "pdfium", "frozen_at": "2026-01-01T00:00:00Z", "code_sha256": self.code}
        self.choice_path = self.root / "choice.json"
        write(self.choice_path, self.choice)
        patches = [patch.object(pilot, "ROOT", self.root), patch.object(pilot, "OUT", self.out),
            patch.object(pilot, "PLAN", plan_path), patch.object(pilot, "PLAN_SHA", plan_sha),
            patch.object(pilot, "AMENDMENT", amendment_path), patch.object(pilot, "AMENDMENT_SHA", amendment_sha),
            patch.object(pilot, "WIKI_AMENDMENT", budget_path), patch.object(pilot, "WIKI_AMENDMENT_SHA", budget_sha),
            patch.object(pilot, "EPA_METADATA", epa_path), patch.object(pilot, "EPA_ATTEMPT", self.body),
            patch.object(pilot, "CHOICE", self.choice_path), patch.object(pilot, "code_hashes", return_value=self.code),
            patch.object(pilot.subprocess, "check_output", return_value="a" * 40), patch.object(pilot.subprocess, "run")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def revealed(self):
        pilot.reveal("a" * 40)
        return pilot.load_run()

    def add_markers(self, run, manifest, local):
        allowed = pilot.frozen_calls(run, manifest, local["plans"])
        for call, identity in allowed.items():
            write(self.out / (call + "-search.json"), {**identity, "request_id": call, "state": "complete",
                "search_depth": "basic", "usage": {"credits": 1}, "results": [{"url": "https://authored-publication.test/source"}]})
        for call, identity in pilot.frozen_wiki_calls(run, manifest, local["wiki_plans"]).items():
            write(self.out / (call + "-mediawiki.json"), {**identity, "request_id": call, "provider": "mediawiki",
                "state": "complete", "usage": {"public_queries": 1, "cost_kzt": 0, "cached": False}, "results": []})
        return allowed

    def add_reports(self, run, manifest, local):
        for case in manifest["cases"]:
            cid = case["id"]
            write(self.out / (cid + ".json"), {"id": report_identifier(pilot.request_id(run, case)),
                "text": manifest["input_texts"][cid], "scope": "web_search", "sources": [], "matches": [], "stats": {},
                "web": {"query_strategy": self.choice["query_strategy"], "url_strategy": self.choice["url_strategy"],
                    "named_references_enabled": True, "zero_fallback_enabled": True, "matcher_version": pilot.MATCHER_VERSION,
                    "discovery_strategy": self.choice["discovery_strategy"], "urls_attempted": 0,
                    "post_wiki_enabled": True, "primary_provider": "tavily", "secondary_provider": "mediawiki",
                    "primary_queries_attempted": len(local["plans"][cid]), "post_wiki_queries_attempted": len(local["wiki_plans"][cid]),
                    "queries_attempted": len(local["plans"][cid]) + len(local["wiki_plans"][cid]),
                    "total_provider_requests": len(local["plans"][cid]) + len(local["wiki_plans"][cid]),
                    "max_provider_requests": 8, "max_page_attempts": 26, "post_wiki_query_plan": local["wiki_plans"][cid],
                    "primary_query_spans": [{k: p[k] for k in ("start", "end")} for p in local["plans"][cid]],
                    "query_spans": [{k: p[k] for k in ("start", "end")} for p in local["plans"][cid] + local["wiki_plans"][cid]],
                    "discovered_urls": ["https://authored-publication.test/source"], "errors": []}})

    def test_default_metadata_never_opens_publication_body_or_initializes_provider(self):
        original_read = Path.read_bytes
        body = self.body
        def read(path):
            if path == body:
                raise AssertionError("Default metadata opened publication prose")
            return original_read(path)
        with patch.object(Path, "read_bytes", read), patch.object(pilot, "checked_source", side_effect=AssertionError("source")), \
                patch("plagiarism.search_tavily.TavilySearch", side_effect=AssertionError("provider")), redirect_stdout(io.StringIO()) as output:
            pilot.main([])
        self.assertIn("METADATA_ONLY", output.getvalue())
        self.assertFalse(self.out.exists())

    def test_incomplete_choice_discovery_and_code_freeze_fail_before_body_read(self):
        for change in ({"zero_fallback": None}, {"wiki_followup": None}, {"discovery_strategy": None}, {"discovery_strategy": "unimplemented-hybrid"},
                       {"code_sha256": {}}, {"actual_case_ids": list(pilot.IDS) + ["P3-01"]}):
            write(self.choice_path, {**self.choice, **change})
            with self.subTest(change=change), patch.object(pilot, "checked_source", side_effect=AssertionError("Must remain closed")) as source:
                with self.assertRaises(ValueError):
                    pilot.reveal("a" * 40)
                source.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_reveal_is_one_shot_has_intent_before_body_and_five_cases_with_seventy_known_words(self):
        original = pilot.checked_source
        def source(meta):
            self.assertTrue((self.out / "reveal-intent.json").exists())
            return original(meta)
        with patch.object(pilot, "checked_source", side_effect=source) as body:
            summary = pilot.reveal("a" * 40)
            self.assertEqual(body.call_count, 1)
            with self.assertRaises(FileExistsError):
                pilot.reveal("a" * 40)
            self.assertEqual(body.call_count, 1)
        run, manifest, local = pilot.load_run()
        self.assertEqual(tuple(case["id"] for case in manifest["cases"]), pilot.IDS)
        self.assertEqual(manifest["preparation_failed_ids"], list(pilot.FAILED_IDS))
        self.assertEqual(summary["copied_words"], 70)
        self.assertEqual(summary["source_known_words"], 70)
        self.assertEqual(summary["control_exact_pairs"], 0)
        self.assertEqual(len(pilot.frozen_calls(run, manifest, local["plans"])), 30)
        self.assertEqual(len(pilot.frozen_wiki_calls(run, manifest, local["wiki_plans"])), 10)
        self.assertEqual((self.out / "run.json").stat().st_mode & 0o777, 0o600)
        for case, sizes in zip(manifest["cases"][:3], ([180, 10], [20, 180], [90, 40, 90])):
            self.assertEqual([len(pilot._tokens(block)) for block in manifest["input_texts"][case["id"]].split("\n\n")], sizes)

    def test_corrupt_source_or_frozen_rank_cannot_be_replaced_or_resampled(self):
        self.body.write_text("Changed authored source")
        with self.assertRaises(ValueError):
            pilot.reveal("a" * 40)
        self.assertTrue((self.out / "reveal-intent.json").exists())
        with patch.object(pilot, "checked_source", side_effect=AssertionError("No second source read")):
            with self.assertRaises(FileExistsError):
                pilot.reveal("a" * 40)
        meta = pilot.metadata()
        meta["epa"]["selected_insertions"][0]["eligible_block_rank_zero_based"] += 1
        with self.assertRaisesRegex(ValueError, "ranks/spans"):
            pilot.construct_cases(meta, {"id": "authored", "url": "https://authored-publication.test/source", "text": self.source_text})

    def test_five_case_call_ids_are_stable_bounded_and_not_v2_global_ids(self):
        run, manifest, local = self.revealed()
        before = pilot.frozen_calls(run, manifest, local["plans"])
        self.assertEqual(before, pilot.frozen_calls(deepcopy(run), deepcopy(manifest), deepcopy(local["plans"])))
        self.assertEqual(len(before), 30)
        self.assertTrue(all(identity["case_id"] in pilot.IDS for identity in before.values()))
        extra = deepcopy(local["plans"])
        extra[pilot.IDS[0]].append(deepcopy(extra[pilot.IDS[0]][0]))
        with self.assertRaises(ValueError):
            pilot.frozen_calls(run, manifest, extra)

    def test_real_shared_recorder_caps_thirty_total_and_six_per_actual_case(self):
        run, manifest, local = self.revealed()
        allowed = pilot.frozen_calls(run, manifest, local["plans"])
        real = Mock(last_usage={"credits": 1})
        real.search.return_value = []
        recorder = pilot.PublicationSearch(real, self.root / "fake-records", allowed, max_queries=30, max_per_document=6)
        (self.root / "fake-records").mkdir()
        for call, identity in allowed.items():
            recorder.search(identity["query"], call)
        self.assertEqual(real.search.call_count, 30)
        with self.assertRaises(ValueError):
            recorder.search("unfrozen", "f" * 64)
        self.assertEqual(real.search.call_count, 30)
        first = {call: item for call, item in allowed.items() if item["case_id"] == pilot.IDS[0]}
        extra = {"case_id": pilot.IDS[0], "query_index": 6, "query": '"Authored seventh call"'}
        output = self.root / "per-case-cap"
        output.mkdir()
        limited = pilot.PublicationSearch(real, output, {**first, "e" * 64: extra}, max_queries=30, max_per_document=6)
        real.search.reset_mock()
        for call, item in first.items():
            limited.search(item["query"], call)
        with self.assertRaises(RuntimeError):
            limited.search(extra["query"], "e" * 64)
        self.assertEqual(real.search.call_count, 6)

    def test_finance_missing_markers_or_usage_stay_unknown_and_explicit_zero_is_known(self):
        run, manifest, local = self.revealed()
        reports = {cid: {"tavily": len(plan), "mediawiki": len(local["wiki_plans"][cid])} for cid, plan in local["plans"].items()}
        result = pilot.accounting(self.out, run, manifest, local["plans"], reports, local["wiki_plans"])
        self.assertEqual(len(result["missing_provider_call_ids"]), 40)
        self.assertEqual(result["confirmed_credit_subtotal"], 0)
        self.assertIsNone(result["total_basic_credits"])
        allowed = self.add_markers(run, manifest, local)
        self.assertEqual(pilot.accounting(self.out, run, manifest, local["plans"], reports, local["wiki_plans"])["total_basic_credits"], 30)
        call = next(iter(allowed))
        path = self.out / (call + "-search.json")
        record = json.loads(path.read_text())
        record["usage"] = {"credits": 0}
        write(path, record)
        self.assertEqual(pilot.accounting(self.out, run, manifest, local["plans"], reports, local["wiki_plans"])["total_basic_credits"], 29)
        record["usage"] = None
        write(path, record)
        result = pilot.accounting(self.out, run, manifest, local["plans"], reports, local["wiki_plans"])
        self.assertEqual(result["unknown_usage_call_ids"], [call])
        self.assertIsNone(result["total_basic_credits"])

    def test_finance_markers_beyond_completed_report_keep_subtotal_but_total_unknown(self):
        run, manifest, local = self.revealed()
        allowed = self.add_markers(run, manifest, local)
        for reported in (0, 5):
            with self.subTest(reported=reported):
                reports = {cid: {"tavily": reported, "mediawiki": 2} for cid in pilot.IDS}
                result = pilot.accounting(self.out, run, manifest, local["plans"], reports, local["wiki_plans"])
                self.assertEqual(result["provider_attempts"], 40)
                self.assertEqual(result["confirmed_credit_subtotal"], 30)
                self.assertEqual(result["missing_provider_call_ids"], [])
                self.assertIsNone(result["total_basic_credits"])
                extra = {call for issue in result["issues"] if issue["code"] == "markers_beyond_reported_attempts"
                         for call in issue["call_ids"]}
                self.assertEqual(extra, {call for call, identity in allowed.items() if identity["query_index"] >= reported})

    def test_score_only_reads_own_artifacts_without_reservation_replan_or_network(self):
        run, manifest, local = self.revealed()
        self.add_markers(run, manifest, local)
        self.add_reports(run, manifest, local)
        with patch.object(pilot, "metadata", side_effect=AssertionError("Reservation read")), \
                patch.object(pilot, "checked_source", side_effect=AssertionError("Publication body reopened")), \
                patch.object(pilot.web, "plan_queries", side_effect=AssertionError("Replan")), \
                patch("plagiarism.search_tavily.TavilySearch", side_effect=AssertionError("Provider")):
            result = pilot.score_only()
        self.assertEqual(len(result["cases"]), 5)
        self.assertEqual(result["accounting"]["total_basic_credits"], 30)
        evidence = result["cases"][0]["evidence"][0]
        self.assertTrue(evidence["expected_url_returned_directly"])
        self.assertFalse(evidence["expected_url_read"])
        self.assertEqual(evidence["grounded_words_any_source"], 0)
        self.assertEqual(result["manual_pair_review"], "pending")

    def test_live_requires_reviewed_run_before_any_client_initialization(self):
        self.revealed()
        with patch("plagiarism.search_tavily.TavilySearch", side_effect=AssertionError("No provider")):
            with self.assertRaisesRegex(ValueError, "reviewed immutable run"):
                pilot.live("a" * 40, "b" * 64)
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            pilot.main(["--live"])

    def test_secondary_ids_use_ordinal_and_caps_do_not_borrow_paid_slots(self):
        run, manifest, local = self.revealed()
        allowed = pilot.frozen_wiki_calls(run, manifest, local["wiki_plans"])
        self.assertEqual(len(allowed), 10)
        primary = pilot.frozen_calls(run, manifest, local["plans"])
        self.assertFalse(allowed.keys() & primary.keys())
        for case in manifest["cases"]:
            for index in range(2):
                call = pilot.digest(pilot.request_id(run, case) + "\0mediawiki-post\0" + str(index))
                self.assertEqual(allowed[call]["query"], local["wiki_plans"][case["id"]][index]["query"])
        real = Mock(last_usage={"public_queries": 1, "cost_kzt": 0, "cached": False}, last_response={"authored": True})
        real.search.return_value = []
        recorder = pilot.RecordedWiki(real, self.out, allowed)
        first = {call: identity for call, identity in allowed.items() if identity["case_id"] == pilot.IDS[0]}
        for call, identity in first.items():
            recorder.search(identity["query"], call)
        self.assertEqual(real.search.call_count, 2)
        extra = {"case_id": pilot.IDS[0], "query_index": 2, "primary_query_index": 2, "query": '"Authored extra phrase"'}
        extra_call = "f" * 64
        recorder.allowed[extra_call] = extra
        with self.assertRaises(pilot.web.WebSearchError):
            recorder.search(extra["query"], extra_call)
        self.assertEqual(real.search.call_count, 2)
        self.assertFalse((self.out / (extra_call + "-mediawiki-intent.json")).exists())
        for call, identity in list(allowed.items()):
            if call not in first and call != extra_call:
                recorder.search(identity["query"], call)
        self.assertEqual(real.search.call_count, 10)

    def test_unknown_provider_intent_blocks_repeat_even_if_outcome_marker_was_lost(self):
        run, manifest, local = self.revealed()
        allowed = pilot.frozen_calls(run, manifest, local["plans"])
        call, identity = next(iter(allowed.items()))
        real = Mock(last_usage=None)
        real.search.side_effect = RuntimeError("Authored uncertain outcome")
        recorder = pilot.PublicationSearch(real, self.out, allowed, max_queries=30, max_per_document=6)
        with self.assertRaises(RuntimeError):
            recorder.search(identity["query"], call)
        (self.out / (call + "-search.json")).unlink()
        with self.assertRaises(pilot.web.WebSearchError):
            recorder.search(identity["query"], call)
        self.assertEqual(real.search.call_count, 1)
        result = pilot.accounting(self.out, run, manifest, local["plans"], {}, local["wiki_plans"])
        self.assertIn(call, result["missing_provider_call_ids"])
        self.assertIsNone(result["total_basic_credits"])

    def test_get_first_recovers_existing_job_and_orphan_marker_forbids_post(self):
        run, manifest, local = self.revealed()
        case = manifest["cases"][0]
        cid, rid = case["id"], pilot.request_id(run, case)
        allowed = pilot.frozen_calls(run, manifest, local["plans"])
        call, identity = next((c, v) for c, v in allowed.items() if v["case_id"] == cid)
        write(self.out / (call + "-search.json"), {**identity, "request_id": call, "state": "submitted", "search_depth": "basic"})
        http = Mock(return_value=(200, {"job": {"id": rid, "status": "complete"}}))
        preflight = Mock(side_effect=lambda: pilot.preflight_new_job(run, manifest, local, case))
        job = pilot.recover_or_submit(http, self.out / (cid + "-request.json"), rid, case, manifest["input_texts"][cid], 8, preflight)
        self.assertEqual(job["status"], "complete")
        self.assertEqual(http.call_args.args, ("/api/jobs/" + rid,))
        preflight.assert_not_called()
        (self.out / (cid + "-request.json")).unlink()
        http.reset_mock(return_value=True)
        http.return_value = (404, {})
        with patch.object(pilot, "quota", side_effect=AssertionError("No quota before orphan refusal")):
            with self.assertRaisesRegex(RuntimeError, "forbids another POST"):
                pilot.recover_or_submit(http, self.out / (cid + "-request.json"), rid, case, manifest["input_texts"][cid], 8, preflight)
        self.assertEqual(http.call_count, 1)
        write(self.out / (cid + "-request.json"), {"request_id": rid, "text_sha256": case["text_sha256"], "planned_queries": 8})
        with self.assertRaisesRegex(RuntimeError, "do not POST again"):
            pilot.recover_or_submit(http, self.out / (cid + "-request.json"), rid, case, manifest["input_texts"][cid], 8, preflight)
        self.assertEqual(http.call_count, 2)

    def test_preflight_requires_paid_quota_and_keeps_unknown_prior_usage_unresolved(self):
        run, manifest, local = self.revealed()
        case = manifest["cases"][0]
        with patch.object(pilot, "quota", return_value={"authored": "quota"}) as quota:
            pilot.preflight_new_job(run, manifest, local, case)
            quota.assert_called_once_with(30)
            with self.assertRaises(RuntimeError):
                pilot.preflight_new_job(run, manifest, local, case)
            self.assertEqual(quota.call_count, 1)
        (self.out / (case["id"] + "-submission-intent.json")).unlink()
        allowed = self.add_markers(run, manifest, local)
        self.add_reports(run, manifest, local)
        (self.out / (case["id"] + ".json")).unlink()
        for call, identity in {**allowed, **pilot.frozen_wiki_calls(run, manifest, local["wiki_plans"])}.items():
            if identity["case_id"] == case["id"]:
                for path in self.out.glob(call + "-*.json"):
                    path.unlink()
        other = next(call for call, identity in allowed.items() if identity["case_id"] != case["id"])
        (self.out / (other + "-search.json")).unlink()
        with patch.object(pilot, "quota", side_effect=AssertionError("Missing accounting is not quota proof")):
            with self.assertRaisesRegex(RuntimeError, "Unreconciled"):
                pilot.preflight_new_job(run, manifest, local, case)

    def test_normal_localhost_queue_store_utf8_fake_providers_and_get_resume(self):
        from plagiarism.storage import Store
        run, manifest, local = self.revealed()
        primary = Mock(provider="tavily", key="authored-not-a-secret", last_usage={"credits": 1})
        # A matching snippet cannot ground proof when the fetched body differs.
        primary.search.return_value = [{"url": "https://authored-publication.test/source", "content": self.source_text}]
        wiki = Mock(provider="mediawiki", last_usage={"public_queries": 1, "cost_kzt": 0, "cached": False}, last_response={"authored": True})
        wiki.search.return_value = [{"url": "https://ru.wikipedia.org/wiki/Authored_fixture"}]
        body = "A completely unrelated fetched body has no copied publication words."
        fetcher = Mock()
        fetcher.fetch.side_effect = lambda url: {"url": url, "requested_url": url, "title": "Authored page", "text": body,
            "text_sha256": pilot.digest(body), "links": []}
        store = Store(self.root / "only-authored-store.sqlite3")
        reviewed = pilot.digest((self.out / "run.json").read_bytes())
        with patch("plagiarism.search_tavily.TavilySearch", return_value=primary), patch.object(pilot.web, "SearchBudget", return_value=object()), \
                patch("plagiarism.search_mediawiki.MediaWikiSearch", return_value=wiki), patch("plagiarism.web_fetch.Fetcher", return_value=fetcher), \
                patch("plagiarism.storage.Store", return_value=store), patch.object(pilot.web, "status", return_value={"configured": True}), \
                patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No external request in authored smoke")), \
                patch.object(pilot, "quota", return_value={"authored": "quota"}) as quota:
            pilot.live("a" * 40, reviewed)
            before = {path.name: path.read_bytes() for path in self.out.glob("P3-??.json")}
            pilot.live("a" * 40, reviewed)
        self.assertEqual(primary.search.call_count, 30)
        self.assertEqual(wiki.search.call_count, 10)
        self.assertEqual(fetcher.fetch.call_count, 10)
        self.assertEqual([call.args[0] for call in quota.call_args_list], [30, 24, 18, 12, 6, 0, 0])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.out.glob("P3-??.json")})
        result = pilot.score_only()
        self.assertEqual(result["accounting"]["provider_attempts"], 40)
        self.assertEqual(result["accounting"]["total_basic_credits"], 30)
        self.assertEqual(result["accounting"]["total_new_public_queries"], 10)
        self.assertTrue(result["cases"][0]["evidence"][0]["expected_url_read"])
        self.assertEqual(result["cases"][0]["evidence"][0]["grounded_words_any_source"], 0)
        for case in manifest["cases"]:
            report = json.loads((self.out / (case["id"] + ".json")).read_text())
            self.assertEqual(report["text"], manifest["input_texts"][case["id"]])
            self.assertEqual(report["web"]["primary_queries_attempted"], 6)
            self.assertEqual(report["web"]["post_wiki_queries_attempted"], 2)
            self.assertEqual(report["web"]["post_wiki_new_public_queries"], 2)
        self.assertTrue(all(json.loads(path.read_text())["fetched"]["text"] == body for path in (self.out / "pages").glob("*/*.json")))

    def test_pages_cap_twenty_six_and_unknown_intent_never_becomes_empty_or_retried(self):
        run, manifest, local = self.revealed()
        real = Mock()
        real.fetch.side_effect = lambda url: {"url": url, "text": "Independent authored fetched prose", "links": []}
        pages = pilot.PublicationPages(self.out, real, None)
        pages.cid = pilot.IDS[0]
        for index in range(26):
            pages.fetch("https://authored.test/page/" + str(index))
        with self.assertRaises(pilot.web.WebSearchError):
            pages.fetch("https://authored.test/page/27")
        self.assertEqual(real.fetch.call_count, 26)
        pages.cid = pilot.IDS[1]
        url = "https://authored.test/uncertain"
        path = pilot.page_path(self.out, pages.cid, url)
        write(path, {"case_id": pages.cid, "requested_url": url, "state": "submitted"})
        before = path.read_bytes()
        with self.assertRaises(pilot.web.WebSearchError):
            pages.fetch(url)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("fetched", json.loads(path.read_text()))
        self.assertEqual(real.fetch.call_count, 26)

    def test_finance_provider_identity_and_free_usage_do_not_hide_paid_unknowns(self):
        run, manifest, local = self.revealed()
        allowed = self.add_markers(run, manifest, local)
        reports = {cid: {"tavily": 6, "mediawiki": 2} for cid in pilot.IDS}
        wiki_call = next(iter(pilot.frozen_wiki_calls(run, manifest, local["wiki_plans"])))
        wiki_path = self.out / (wiki_call + "-mediawiki.json")
        record = json.loads(wiki_path.read_text())
        record["usage"] = None
        write(wiki_path, record)
        state = pilot.accounting(self.out, run, manifest, local["plans"], reports, local["wiki_plans"])
        self.assertEqual(state["total_basic_credits"], 30)
        self.assertEqual(state["confirmed_public_queries_subtotal"], 9)
        self.assertIsNone(state["total_new_public_queries"])
        self.assertEqual(state["unknown_public_usage_call_ids"], [wiki_call])
        paid_call = next(iter(allowed))
        path = self.out / (paid_call + "-search.json")
        record = json.loads(path.read_text())
        record["usage"] = None
        write(path, record)
        state = pilot.accounting(self.out, run, manifest, local["plans"], reports, local["wiki_plans"])
        self.assertEqual(state["confirmed_credit_subtotal"], 29)
        self.assertIsNone(state["total_basic_credits"])
        record["query"] = "A different forged query"
        write(path, record)
        with self.assertRaisesRegex(ValueError, "Unbound"):
            pilot.accounting(self.out, run, manifest, local["plans"], reports, local["wiki_plans"])

    def test_report_limits_and_attempted_secondary_spans_are_strict(self):
        run, manifest, local = self.revealed()
        self.add_reports(run, manifest, local)
        case = manifest["cases"][0]
        report = json.loads((self.out / (case["id"] + ".json")).read_text())
        report["web"].update(post_wiki_queries_attempted=0, queries_attempted=6, total_provider_requests=6,
            query_spans=report["web"]["primary_query_spans"], urls_attempted=26)
        self.assertEqual(pilot.validate_report(report, run, case, local), {"tavily": 6, "mediawiki": 0})
        report["web"]["urls_attempted"] = 27
        with self.assertRaises(ValueError):
            pilot.validate_report(report, run, case, local)
        report["web"].update(urls_attempted=0, primary_queries_attempted=0, total_provider_requests=False, queries_attempted=False)
        with self.assertRaisesRegex(ValueError, "provider counts"):
            pilot.validate_report(report, run, case, local)

    def test_control_match_needs_pair_review_and_changed_source_policy_is_rejected(self):
        run, manifest, local = self.revealed()
        self.add_markers(run, manifest, local)
        self.add_reports(run, manifest, local)
        path = self.out / (pilot.IDS[3] + ".json")
        raw = json.loads(path.read_text())
        source = {"id": "authored-web-control", "url": "https://authored-other.test/control", "text": raw["text"]}
        raw.update(sources=[source], matches=pilot.analyze(raw["text"], [source])["matches"])
        write(path, raw)
        result = pilot.score_only()
        row = next(item for item in result["cases"] if item["id"] == pilot.IDS[3])
        self.assertTrue(row["control"])
        self.assertEqual(row["pairs"][0]["review"], "pending_both_excerpts")
        raw["web"]["discovery_strategy"] = "unfrozen-policy"
        write(path, raw)
        with self.assertRaisesRegex(ValueError, "source policy"):
            pilot.score_only()


if __name__ == "__main__":
    unittest.main()
