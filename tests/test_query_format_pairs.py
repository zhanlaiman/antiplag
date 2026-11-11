"""Authored text/temp Store only; no DEV, holdout, credentials or external I/O."""
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from plagiarism import web_search as web
from plagiarism.search_tavily import TavilySearch
from plagiarism.storage import Store
from scripts import run_query_format_pairs as pilot

TEXT = "\n\n".join(f"Авторский исследователь{i} подготовил архивный прибор{i} перед поездкой на конференцию{i}, затем проверил тетрадь{i}, её обложку и подробный график{i}." for i in range(8))
SECRET = "authored-credential-never-read"


def fixture_run():
    base = web.plan_queries(TEXT, strategy=web.DENSE_STRATEGY)
    unquoted = pilot.remove_outer_quotes(base)
    jobs = {}
    for cid, index in pilot.SPEC:
        for fmt, values in (("quoted", base), ("unquoted", unquoted)):
            jobs[cid + "-" + fmt] = {"case_id": cid, "format": fmt, "base_query_index": index,
                "text_sha256": sha256(TEXT.encode()).hexdigest(), "plan": [deepcopy(values[index])]}
    run = {"id": "a" * 32, "configuration": {"jobs": jobs, "code_sha256": {"authored": "fixed"}}}
    run["request_ids"] = {key: sha256((run["id"] + key + value["text_sha256"]).encode()).hexdigest()[:32]
                          for key, value in jobs.items()}
    return run


class QueryFormatPairTests(unittest.TestCase):
    def test_outer_quotes_only_preserves_all_fields_words_and_input_plan(self):
        plans = [{"start": 12, "end": 70, "query": '"Ёж архивист проверил новый список документов"', "extra": {"authored": True}}]
        before = deepcopy(plans)
        result = pilot.remove_outer_quotes(plans)
        self.assertEqual(result[0]["query"], plans[0]["query"][1:-1])
        self.assertEqual(pilot.word_span_identity(result), pilot.word_span_identity(plans))
        self.assertEqual(plans, before)
        self.assertEqual({k:v for k,v in result[0].items() if k != "query"}, {k:v for k,v in plans[0].items() if k != "query"})

    def test_invalid_or_noncanonical_quote_wrappers_are_rejected(self):
        for value in ('bare words', '“other quotes”', '""', '"embedded " quote"', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pilot.remove_outer_quotes([{"query": value, "start": 0, "end": 10}])

    def test_all_authored_insert_sizes_positions_and_two_insertions_are_invariant(self):
        host = [f"volunteerword{i}" for i in range(128)]
        documents = []
        for size in (10, 20, 40):
            inserted = [f"archiveword{i}" for i in range(size)]
            for position in (0, 31, 64, 128):
                documents.append(" ".join(host[:position] + inserted + host[position:]))
        documents.append(" ".join(host[:31] + [f"firstsource{i}" for i in range(20)] + host[31:95] + [f"secondsource{i}" for i in range(20)] + host[95:]))
        self.assertEqual(len(documents), 13)
        for text in documents:
            original = web.plan_queries(text, strategy=web.DENSE_STRATEGY)
            changed = pilot.remove_outer_quotes(original)
            self.assertEqual(len(changed), len(original))
            self.assertEqual(pilot.word_span_identity(original), pilot.word_span_identity(changed))
            self.assertEqual([(p["start"], p["end"]) for p in original], [(p["start"], p["end"]) for p in changed])

    def test_fixed_six_identities_have_one_call_each_and_keep_dirty_words(self):
        run = fixture_run()
        calls = pilot.call_map(run)
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(set(run["request_ids"].values())), 6)
        self.assertEqual(set(c["query_index"] for c in calls.values()), {0})
        for cid, index in pilot.SPEC:
            a, b = (run["configuration"]["jobs"][cid + "-" + fmt]["plan"][0] for fmt in pilot.FORMATS)
            self.assertEqual(b["query"], a["query"][1:-1])
            self.assertEqual(a["start"], b["start"])
            self.assertEqual(a["end"], b["end"])
            self.assertEqual(run["configuration"]["jobs"][cid + "-quoted"]["base_query_index"], index)

    def test_live_flags_fail_before_preparation(self):
        with patch.object(pilot, "prepare", side_effect=AssertionError("no preparation")), patch("sys.stderr", new_callable=io.StringIO):
            for args in (["--live"], ["--live", "--start-confirmation", "START", "--max-new-queries", "7", "--manifest-sha256", "a" * 64]):
                with self.subTest(args=args), self.assertRaises(SystemExit):
                    pilot.main(args)

    def test_changed_manifest_or_code_blocks_before_live_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            out, run = Path(directory), fixture_run()
            pilot.private_dump(out / "run.json", run)
            good_hash = sha256((out / "run.json").read_bytes()).hexdigest()
            with patch.object(pilot, "code_identity", return_value={"changed": True}), \
                    patch("plagiarism.search_tavily.TavilySearch", side_effect=AssertionError("no client")):
                with self.assertRaisesRegex(ValueError, "reviewed manifest"):
                    pilot.live(out, run, {}, "b" * 64)
                with self.assertRaisesRegex(ValueError, "Code changed"):
                    pilot.live(out, run, {}, good_hash)

    def test_prepare_reads_only_three_authored_inputs_and_never_quota_or_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dev = root / "data/private/web-evaluation/materials/dev-30"
            (dev / "inputs").mkdir(parents=True)
            selected = {cid for cid, _ in pilot.SPEC}
            cases = []
            for index in range(1, 31):
                cid = f"D{index:02d}"
                cases.append({"id": cid, "split": "dev", "input_path": f"inputs/{cid}.txt", "text_sha256": sha256(TEXT.encode()).hexdigest()})
                if cid in selected:
                    (dev / f"inputs/{cid}.txt").write_text(TEXT)
            raw = json.dumps({"cases": cases}).encode()
            (dev / "key.json").write_bytes(raw)
            with patch.object(pilot, "ROOT", root), patch.object(pilot, "DEV_KEY_SHA", sha256(raw).hexdigest()), \
                    patch.object(pilot, "code_identity", return_value={"authored": "fixed"}), \
                    patch.object(pilot, "quota", side_effect=AssertionError("no quota")), \
                    patch("plagiarism.search_tavily.TavilySearch", side_effect=AssertionError("no client")):
                out, run, texts = pilot.prepare()
                self.assertEqual(set(texts), selected)
                self.assertEqual(len(run["configuration"]["jobs"]), 6)
                self.assertEqual((out / "run.json").stat().st_mode & 0o777, 0o600)

    def test_normal_http_queue_utf8_real_adapter_and_get_resume_use_only_six_fake_calls(self):
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self, *args):self.close()
        requests = []
        class Opener:
            def open(self, request, timeout):
                body = json.loads(request.data)
                self_outer.assertEqual(body["search_depth"], "basic")
                self_outer.assertFalse(body["auto_parameters"])
                requests.append(body)
                return Response(json.dumps({"results": [{"url": "https://example.test/authored"}], "usage": {"credits": 1}}).encode())
        class Pages:
            def fetch(self, url):
                return {"url": url, "text": "Independent coastal measurements describe quiet wetlands and seasonal harbor water observations for local mapping volunteers.", "links": []}
        self_outer = self
        with tempfile.TemporaryDirectory() as directory:
            out, run = Path(directory), fixture_run()
            texts = {cid: TEXT for cid, _ in pilot.SPEC}
            pilot.private_dump(out / "run.json", run)
            manifest_hash = sha256((out / "run.json").read_bytes()).hexdigest()
            budget = web.SearchBudget(out / "budget.sqlite3")
            store = Store(out / "store.sqlite3")
            original_run, original_plan = web.run, web.plan_queries
            def adapter(unused_budget):
                real = TavilySearch(budget)
                real.opener = Opener()
                return real
            with patch.object(pilot, "code_identity", return_value={"authored": "fixed"}), \
                    patch.object(pilot, "quota", return_value={"paygo_usage": 0}) as quota, \
                    patch.object(web, "SearchBudget", return_value=budget), \
                    patch.object(web, "selected_provider", return_value="tavily"), patch.object(web, "key_for", return_value=SECRET), \
                    patch("plagiarism.search_tavily.key_for", return_value=SECRET), \
                    patch("plagiarism.search_tavily.TavilySearch", side_effect=adapter), \
                    patch("plagiarism.storage.Store", return_value=store), \
                    patch("plagiarism.web_fetch.Fetcher", side_effect=Pages), redirect_stdout(io.StringIO()):
                pilot.live(out, run, texts, manifest_hash)
                self.assertEqual(len(requests), 6)
                self.assertEqual(quota.call_count, 6)
                pilot.live(out, run, texts, manifest_hash)
                self.assertEqual(len(requests), 6)
                self.assertEqual(quota.call_count, 6)
            self.assertIs(web.run, original_run)
            self.assertIs(web.plan_queries, original_plan)
            self.assertEqual([r["query"] for r in requests], [j["plan"][0]["query"] for j in run["configuration"]["jobs"].values()])
            for key in run["configuration"]["jobs"]:
                report = json.loads((out / (key + ".json")).read_text())
                self.assertEqual(report["web"]["queries_attempted"], 1)
                self.assertEqual(report["text"], TEXT)
                self.assertFalse(report["web"]["default_changed"])


if __name__ == "__main__":
    unittest.main()
