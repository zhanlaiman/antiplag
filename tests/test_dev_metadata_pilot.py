"""Offline pilot guards and real-adapter behavior with synthetic replies only."""
from contextlib import redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from plagiarism import web_search as web
from plagiarism.search_tavily import TavilySearch
from scripts import run_dev_metadata_pilot as pilot


SECRET = "fixture-credential-never-save"
TEXT = "\n\n".join(
    f"Исследователь{i} тщательно проверил отдельный прибор{i} перед поездкой в лабораторию{i} для повторного наблюдения{i}."
    for i in range(8))
PAGE_BODY = "A publisher documented quiet coastal geography beside independently archived harbor charts and seasonal shipping schedules."


def frozen_run():
    plans = {cid: web.plan_queries(TEXT, strategy=web.DENSE_STRATEGY) for cid in pilot.CASE_IDS}
    pilot.validate_scope(plans)
    inputs = {cid: sha256(TEXT.encode()).hexdigest() for cid in pilot.CASE_IDS}
    return {"id": "a" * 32, "run_name": "dev-fixture", "configuration": {
        "query_plans": plans, "input_sha256": inputs, "candidate_sha256": "c" * 64,
        "request_ids": {cid: pilot.case_request_id("a" * 32, cid, inputs[cid]) for cid in pilot.CASE_IDS}}}


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class MemorySocket:
    """Byte transport for the real HTTPConnection serializer/parser; no sockets open."""
    def __init__(self, status=200, response=None):
        body = json.dumps(response or {"job": {"status": "queued"}}).encode("utf-8")
        self.reply = (f"HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("ascii") + body
        self.sent, self.closed = [], False

    def sendall(self, data):
        self.sent.append(bytes(data))

    def makefile(self, mode):
        if mode != "rb":
            raise AssertionError("Unexpected HTTP response stream mode")
        return io.BytesIO(self.reply)

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, payloads=None, error=None):
        self.payloads = payloads or [{"results": [{"url": "https://example.test/article"}], "usage": {"credits": 1}}]
        self.error, self.calls, self.bodies = error, 0, []

    def open(self, request, timeout):
        self.bodies.append(json.loads(request.data))
        self.calls += 1
        if self.error:
            raise self.error
        value = self.payloads[min(self.calls - 1, len(self.payloads) - 1)]
        return Reply(json.dumps(value).encode())


class Pages:
    def __init__(self):
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return {"url": url, "title": "Independent fetched page", "text": PAGE_BODY, "links": [],
                "content_type": "text/html", "headers": {"ignored": SECRET}, "fetched_at": "2026-11-12T00:00:00Z"}


class DevMetadataPilotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = Path(self.directory.name)
        self.run = frozen_run()
        pilot.private_dump(self.out / "run.json", self.run)
        self.provider = patch.object(web, "selected_provider", return_value="tavily")
        self.provider.start()
        self.addCleanup(self.provider.stop)

    def searcher(self, opener=None):
        with patch("plagiarism.search_tavily.key_for", return_value=SECRET):
            real = TavilySearch(web.SearchBudget(self.out / "budget.sqlite3"))
        real.opener = opener or FakeOpener()
        return pilot.RecordingSearch(self.out, self.run, real)

    def first_call(self):
        identifier, identity = next(iter(pilot.call_map(self.run).items()))
        return identifier, identity

    def test_request_and_call_ids_are_stable_separate_and_bounded(self):
        restored = json.loads(pilot.canonical_json(self.run))
        self.assertEqual(pilot.call_map(restored), pilot.call_map(self.run))
        self.assertEqual(len(pilot.call_map(restored)), 18)
        self.assertEqual(len(set(restored["configuration"]["request_ids"].values())), 3)
        with self.assertRaises(ValueError):
            pilot.call_identifier("a" * 32, 6)
        with self.assertRaises(ValueError):
            pilot.case_request_id("a" * 32, "D02", "b" * 64)
        plans = self.run["configuration"]["query_plans"]
        for wrong in ({**plans, "D02": plans["D08"]}, {**plans, "D08": plans["D08"][:5]},
                      {**plans, "D08": plans["D08"] + plans["D08"][:1]}):
            with self.subTest(lengths={k: len(v) for k, v in wrong.items()}), self.assertRaises(ValueError):
                pilot.validate_scope(wrong)

    def test_real_adapter_basic_body_and_budget_finish_unchanged(self):
        opener = FakeOpener()
        searcher = self.searcher(opener)
        identifier, identity = self.first_call()
        self.assertEqual(searcher.search(identity["query"], identifier), [{"url": "https://example.test/article"}])
        self.assertEqual(opener.bodies, [{"query": identity["query"], "search_depth": "basic", "auto_parameters": False,
            "topic": "general", "max_results": 5, "include_answer": False, "include_raw_content": False, "include_usage": True}])
        database = sqlite3.connect(self.out / "budget.sqlite3")
        try:
            self.assertEqual(database.execute("SELECT id,state FROM calls").fetchall(), [(identifier, "complete")])
        finally:
            database.close()
        record = json.loads(pilot.call_path(self.out, identity, identifier).read_text())
        self.assertEqual(record["span"], identity["span"])
        self.assertEqual(record["results"][0]["provider_rank"], 1)
        self.assertEqual(record["usage"], {"credits": 1})

    def test_full_eighteen_calls_and_six_per_case_have_no_nineteenth_call(self):
        opener = FakeOpener()
        searcher = self.searcher(opener)
        for identifier, identity in pilot.call_map(self.run).items():
            searcher.search(identity["query"], identifier)
        self.assertEqual(opener.calls, 18)
        for cid in pilot.CASE_IDS:
            self.assertEqual(len(list((self.out / "calls" / cid).glob("*.json"))), 6)
        identifier, identity = self.first_call()
        for query, call_id in ((identity["query"], identifier), (identity["query"] + " changed", identifier),
                               (identity["query"], "f" * 64)):
            with self.assertRaises(web.WebSearchError):
                searcher.search(query, call_id)
        self.assertEqual(opener.calls, 18)

    def test_existing_global_or_case_markers_block_before_provider(self):
        for global_cap in (True, False):
            with self.subTest(global_cap=global_cap), tempfile.TemporaryDirectory() as directory:
                out = Path(directory)
                searcher = self.searcher(FakeOpener())
                searcher.out = out
                for index in range(18 if global_cap else 6):
                    parent = "D10" if global_cap else "D08"
                    pilot.private_dump(out / "calls" / parent / ("old-" + str(index) + ".json"), {})
                identifier, identity = self.first_call()
                with self.assertRaises(web.WebSearchError) as caught:
                    searcher.search(identity["query"], identifier)
                self.assertEqual(caught.exception.code, "search_budget_exhausted")
                self.assertEqual(searcher.tee.opener.calls, 0)

    def test_durable_unknown_marker_blocks_repeat_even_without_budget_ledger(self):
        opener = FakeOpener(error=URLError("uncertain " + SECRET))
        searcher = self.searcher(opener)
        identifier, identity = self.first_call()
        with self.assertRaises(web.WebSearchError):
            searcher.search(identity["query"], identifier)
        path = pilot.call_path(self.out, identity, identifier)
        self.assertEqual(json.loads(path.read_text())["state"], "failed_or_unknown")
        self.assertNotIn(SECRET, path.read_text())
        (self.out / "budget.sqlite3").unlink()
        with self.assertRaises(web.WebSearchError) as caught:
            searcher.search(identity["query"], identifier)
        self.assertEqual(caught.exception.code, "search_already_attempted")
        self.assertEqual(opener.calls, 1)

    def test_invalid_optional_metadata_and_reflected_secrets_are_not_saved(self):
        payload = {"results": [{"url": "https://example.test/article", "title": SECRET,
            "content": "quoted " + SECRET, "score": float("nan"), "raw_headers": {"Authorization": SECRET}},
            {"url": "https://other.test/", "title": "bad\u0000metadata", "score": True}],
            "usage": {"credits": 1}, "unknown": SECRET}
        searcher = self.searcher(FakeOpener([payload]))
        identifier, identity = self.first_call()
        searcher.search(identity["query"], identifier)
        raw = pilot.call_path(self.out, identity, identifier).read_text()
        record = json.loads(raw)
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("raw_headers", raw)
        self.assertNotIn("title", record["results"][0])
        self.assertNotIn("content", record["results"][0])
        self.assertNotIn("score", record["results"][0])
        self.assertNotIn("title", record["results"][1])

    def test_reflected_secret_url_is_failed_without_raw_response(self):
        searcher = self.searcher(FakeOpener([{"results": [{"url": "https://example.test/" + SECRET}]}]))
        identifier, identity = self.first_call()
        with self.assertRaises(web.WebSearchError):
            searcher.search(identity["query"], identifier)
        raw = pilot.call_path(self.out, identity, identifier).read_text()
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("results", json.loads(raw))

    def test_saved_and_submitted_jobs_get_first_without_second_post(self):
        cid = "D08"
        http = Mock(side_effect=[pilot.QueueHTTPError(404), {"job": {"status": "running"}}])
        pilot.queue_case(http, self.out, self.run, cid, TEXT, allow_submit=True)
        self.assertEqual([call.args[0].split("/")[-1] for call in http.call_args_list],
                         [self.run["configuration"]["request_ids"][cid], "web-check-jobs"])
        recovery = Mock(return_value={"job": {"status": "complete"}})
        self.assertEqual(pilot.queue_case(recovery, self.out, self.run, cid, TEXT, allow_submit=True)["status"], "complete")
        self.assertEqual(recovery.call_count, 1)
        self.assertEqual(len(recovery.call_args.args), 1)
        unknown = Mock(side_effect=pilot.QueueHTTPError(404))
        with self.assertRaises(RuntimeError):
            pilot.queue_case(unknown, self.out, self.run, cid, TEXT, allow_submit=True)
        self.assertEqual(unknown.call_count, 1)

    def test_actual_http_connection_serializes_unicode_json_as_utf8_bytes(self):
        transport = MemorySocket()
        body = {"request_id": "a" * 32, "text": "Первую страницу проверили 🙂", "internet_consent": True}

        def connect(connection):
            connection.sock = transport

        with patch.object(pilot.HTTPConnection, "connect", connect):
            result = pilot.queue_http(12345, "/api/web-check-jobs", body)
        headers, payload = b"".join(transport.sent).split(b"\r\n\r\n", 1)
        expected = pilot.canonical_json(body).encode("utf-8")
        self.assertTrue(headers.startswith(b"POST /api/web-check-jobs HTTP/1.1\r\n"))
        self.assertIn(b"Content-Type: application/json", headers)
        self.assertIn(f"Content-Length: {len(expected)}".encode("ascii"), headers)
        self.assertEqual(payload, expected)
        self.assertEqual(json.loads(payload.decode("utf-8")), body)
        self.assertEqual(result, {"job": {"status": "queued"}})
        self.assertTrue(transport.closed)

    def test_actual_http_get_404_remains_recoverable_without_a_post_body(self):
        transport = MemorySocket(status=404)

        def connect(connection):
            connection.sock = transport

        with patch.object(pilot.HTTPConnection, "connect", connect), self.assertRaises(pilot.QueueHTTPError) as caught:
            pilot.queue_http(12345, "/api/jobs/" + "a" * 32)
        headers, payload = b"".join(transport.sent).split(b"\r\n\r\n", 1)
        self.assertTrue(headers.startswith(b"GET /api/jobs/"))
        self.assertEqual(payload, b"")
        self.assertEqual(caught.exception.status, 404)
        self.assertTrue(transport.closed)

    def test_lost_post_response_and_orphan_call_marker_never_resubmit(self):
        http = Mock(side_effect=[pilot.QueueHTTPError(404), TimeoutError("POST outcome unknown")])
        with self.assertRaises(TimeoutError):
            pilot.queue_case(http, self.out, self.run, "D08", TEXT, allow_submit=True)
        recovery = Mock(side_effect=pilot.QueueHTTPError(404))
        with self.assertRaises(RuntimeError):
            pilot.queue_case(recovery, self.out, self.run, "D08", TEXT, allow_submit=True)
        self.assertEqual(recovery.call_count, 1)
        (self.out / "D08-request.json").unlink()
        pilot.private_dump(self.out / "calls/D08/orphan.json", {"state": "submitted"})
        orphan = Mock(side_effect=pilot.QueueHTTPError(404))
        with self.assertRaises(RuntimeError):
            pilot.queue_case(orphan, self.out, self.run, "D08", TEXT, allow_submit=True)
        self.assertEqual(orphan.call_count, 1)

    def test_snippet_is_only_ordering_and_only_fetched_body_proves_matches(self):
        payload = {"results": [{"url": "https://example.test/article", "content": TEXT}], "usage": {"credits": 1}}
        searcher, pages = self.searcher(FakeOpener([payload])), Pages()
        identifier = self.run["configuration"]["request_ids"]["D08"]
        with patch.object(web, "_rank_url_paths", wraps=web._rank_url_paths) as path_rank:
            report = pilot.candidate_run(web.run, self.out, self.run, searcher, pages, TEXT, identifier)
        self.assertEqual(path_rank.call_count, 1)
        self.assertEqual(report["matches"], [])
        self.assertEqual(report["stats"]["matched_words"], 0)
        self.assertEqual(report["web"]["url_strategy"], pilot.STRATEGY)
        self.assertEqual(report["web"]["frozen_query_plan"], self.run["configuration"]["query_plans"]["D08"])
        self.assertTrue(report["web"]["metadata_ranking"]["reasons"])
        self.assertFalse(report["web"]["metadata_ranking"]["may_be_used_as_matching_evidence"])
        self.assertEqual(report["sources"][0]["text"], PAGE_BODY)
        saved = json.loads(pilot.page_path(self.out, "D08", pages.calls[0]).read_text())
        self.assertEqual(saved["fetched"]["text"], PAGE_BODY)
        self.assertNotIn("headers", saved["fetched"])
        self.assertNotIn(SECRET, pilot.canonical_json(saved))

    def test_same_reply_path_baseline_is_partial_for_unfetched_displaced_page(self):
        plans = self.run["configuration"]["query_plans"]["D08"]
        payloads = [{"results": [{"url": f"https://q{q}r{r}.test/article",
            **({"content": plans[q]["query"]} if q == 5 and r == 4 else {})} for r in range(5)],
            "usage": {"credits": 1}} for q in range(6)]
        opener, pages = FakeOpener(payloads), Pages()
        candidate = pilot.candidate_run(web.run, self.out, self.run, self.searcher(opener), pages, TEXT,
                                       self.run["configuration"]["request_ids"]["D08"])
        baseline = pilot.replay_baseline(self.out, self.run, "D08", TEXT)
        self.assertEqual(opener.calls, 6)
        self.assertEqual(len(pages.calls), 12)
        self.assertEqual(candidate["web"]["metadata_ranking"]["candidate_order"][0], "https://q5r4.test/article")
        self.assertTrue(baseline["web"]["unknown_unfetched_urls"])
        self.assertEqual(baseline["web"]["status"], "partial")
        self.assertIsNone(baseline["stats"]["uniqueness_percent"])
        self.assertFalse(baseline["web"]["page_refills_allowed"])
        self.assertTrue(baseline["web"]["exact_coverage_is_lower_bound"])
        self.assertEqual(baseline["web"]["unfetched_page_evidence"], "unknown")

    def test_ranking_audit_keeps_original_query_index_after_an_earlier_failure(self):
        query = self.run["configuration"]["query_plans"]["D08"][3]["query"]
        url = "https://example.test/target"
        record = {"query_index": 3, "query": query,
                  "results": pilot.retain_results([{"url": url, "content": query}])}
        audit = {}
        with pilot.metadata_order(TEXT, [record], audit):
            web._fetch_order([url], [[url]], web.BALANCED_URL_STRATEGY)
        self.assertEqual(audit["reasons"][url]["query_index"], 3)

    def test_replay_preserves_failed_provider_stop_without_calls_or_page_refills(self):
        identifier, identity = self.first_call()
        pilot.private_dump(pilot.call_path(self.out, identity, identifier),
            {**identity, "request_id": identifier, "state": "failed_or_unknown", "code": "search_rate_limited"})
        baseline = pilot.replay_baseline(self.out, self.run, "D08", TEXT)
        self.assertEqual(baseline["web"]["queries_attempted"], 1)
        self.assertEqual(baseline["web"]["query_stop_code"], "search_rate_limited")
        self.assertEqual(baseline["web"]["urls_attempted"], 0)

    def test_page_secret_failure_has_durable_failure_and_never_refetches(self):
        pages = Mock()
        pages.fetch.return_value = {"url": "https://example.test/a", "text": SECRET}
        recording = pilot.RecordingPages(self.out, "D08", pages, SECRET)
        with self.assertRaises(web.WebSearchError):
            recording.fetch("https://example.test/a")
        with self.assertRaises(web.WebSearchError):
            recording.fetch("https://example.test/a")
        self.assertEqual(pages.fetch.call_count, 1)
        raw = pilot.page_path(self.out, "D08", "https://example.test/a").read_text()
        self.assertNotIn(SECRET, raw)
        self.assertEqual(json.loads(raw)["state"], "failed_or_unknown")

    def test_default_prepare_and_status_never_initialize_live_clients_or_quota(self):
        root = self.out / "fixture-root"
        dev = root / "data/private/web-evaluation/materials/dev-30"
        dev.mkdir(parents=True)
        cases = []
        for cid in pilot.CASE_IDS:
            (dev / (cid + ".txt")).write_text(TEXT)
            cases.append({"id": cid, "split": "dev", "input_path": cid + ".txt", "text_sha256": sha256(TEXT.encode()).hexdigest()})
        (dev / "key.json").write_text(json.dumps({"cases": cases}))
        codes = {name: "f" * 64 for name in pilot.SOURCE_FILES}
        with patch.object(pilot, "ROOT", root), patch.object(pilot, "source_identity", return_value=codes), \
             patch.object(pilot, "git_head", return_value="e" * 40), \
             patch.object(pilot, "quota", side_effect=AssertionError("quota during prepare")), \
             patch("plagiarism.search_tavily.TavilySearch", side_effect=AssertionError("client during prepare")), \
             redirect_stdout(io.StringIO()) as output:
            pilot.main([])
            first = output.getvalue()
            pilot.main(["--status"])
        manifest = root / "data/private/web-evaluation" / pilot.DEFAULT_RUN_NAME / "run.json"
        frozen = json.loads(manifest.read_text())
        self.assertEqual(frozen["configuration"]["case_ids"], list(pilot.CASE_IDS))
        self.assertEqual(frozen["configuration"]["max_actual_provider_calls"], 18)
        self.assertEqual(frozen["configuration"]["candidate_sha256"], pilot.digest(frozen["configuration"]["candidate"]))
        self.assertEqual(frozen["git_head_at_prepare"], "e" * 40)
        self.assertIn('"api_calls_in_prepare_status": 0', first)
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(manifest.parent.stat().st_mode & 0o777, 0o700)

    def test_live_flags_fail_before_preparation_or_api(self):
        with patch.object(pilot, "prepare", side_effect=AssertionError("must not prepare")), redirect_stdout(io.StringIO()), \
             patch("sys.stderr", new_callable=io.StringIO):
            for arguments in (["--live"], ["--live", "--start-confirmation", "START", "--max-new-queries", "19",
                               "--manifest-sha256", "a" * 64]):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    pilot.main(arguments)

    def test_changed_code_or_manifest_blocks_start_before_live_work(self):
        self.run["configuration"]["source_code_sha256"] = {"code": "a" * 64}
        pilot.private_dump(self.out / "run.json", self.run)
        manifest_hash = sha256((self.out / "run.json").read_bytes()).hexdigest()
        with patch.object(pilot, "_live_locked", side_effect=AssertionError("must not execute")), \
             patch.object(pilot, "source_identity", return_value={"code": "b" * 64}):
            with self.assertRaises(ValueError):
                pilot.live(self.out, self.run, {}, manifest_hash)
            with self.assertRaises(ValueError):
                pilot.live(self.out, self.run, {}, "f" * 64)

    def test_quota_after_records_usage_or_unavailable_once_without_retry(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                quota = Mock(return_value={"paygo_usage": 0},
                             side_effect=RuntimeError("unavailable " + SECRET) if unavailable else None)
                with patch.object(pilot, "quota", quota):
                    pilot.save_quota_after(self.out)
                quota.assert_called_once_with(0)
                raw = (self.out / "quota-after.json").read_text()
                self.assertNotIn(SECRET, raw)
                self.assertEqual(json.loads(raw), {"unavailable": True, "new_searches_allowed": False}
                                 if unavailable else {"paygo_usage": 0})


if __name__ == "__main__":
    unittest.main()
