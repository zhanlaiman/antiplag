"""Authored temporary comparisons and fake Fetchers; no real reads or GETs."""
from contextlib import redirect_stdout
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from plagiarism.matching import analyze
from plagiarism.web_fetch import FetchError
from scripts.dev_report_provenance import source_snapshot_signatures
from scripts import fill_zero_fallback_pages as driver


BODY = "Independent geographers measured coastal wetlands and documented seasonal sediment layers beside a quiet harbor."
PHRASE = "Астроном тщательно настроил телескоп перед наблюдением далёкого звёздного скопления"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


class Clock:
    def __init__(self):
        self.value = 0
    def __call__(self):
        return self.value


class FakeFetcher:
    def __init__(self, clock, *, duration=0, failed=(), before=None):
        self.clock, self.duration, self.failed, self.before = clock, duration, set(failed), before
        self.calls = []
    def fetch(self, url):
        self.calls.append(url)
        if self.before:
            self.before(url)
        self.clock.value += self.duration
        if url in self.failed:
            raise FetchError("Authored robots denial", "robots_denied")
        return {"url": url, "requested_url": url, "title": "Authored fetched page", "text": BODY,
            "text_sha256": sha256(BODY.encode()).hexdigest(), "links": [], "content_type": "text/plain",
            "fetched_at": "2026-01-01T00:00:00+00:00"}


class ZeroFallbackFillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "data/private/web-evaluation"
        self.candidate = self.root / "scripts/probe_zero_fallback.py"
        self.candidate.parent.mkdir()
        self.candidate.write_text("# Authored frozen candidate, never imported\n")
        self.comparison = {"frozen_plan": {"candidate_sha256": sha256(self.candidate.read_bytes()).hexdigest()},
            "cases": [{"id": f"D{i:02d}", "original_report_id": sha256(str(i).encode()).hexdigest()[:32],
                "audit": {"triggered": False, "initial_gate_words": 1, "prefix_preserved": True,
                    "first_phase_attempted_urls": [], "first_phase_source_signatures": {},
                    "extra_attempted_urls": [], "unknown_extra_urls": [], "events": []}} for i in range(1, 31)]}
        self.environment = patch.dict(os.environ, {"ISTOK_PDF_ALTERNATIVE_ENGINE": "pdfium"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.code = patch.object(driver, "_identity", return_value={"authored-code": "f" * 64})
        self.code.start()
        self.addCleanup(self.code.stop)

    def set_case(self, cid, count=1, *, elapsed=0, urls=None):
        row = next(row for row in self.comparison["cases"] if row["id"] == cid)
        prefix = [f"https://prefix.test/{cid}/{index}" for index in range(12)]
        extras = urls or [f"https://authored-fill.test/{cid}/{index}" for index in range(count)]
        row["audit"].update(triggered=True, initial_gate_words=0, first_phase_attempted_urls=prefix,
                            extra_attempted_urls=extras, unknown_extra_urls=list(extras))
        directory = next(name for name, ids in driver.FINAL_BATCHES if cid in ids)
        original = {"id": row["original_report_id"], "sources": [
            {"id": "prefix-" + str(index), "url": url, "text": BODY,
             "provenance": {"text_sha256": sha256(BODY.encode()).hexdigest()}}
            for index, url in enumerate(prefix)],
            "web": {"elapsed_seconds": elapsed, "attempted_urls": prefix, "urls_attempted": 12}}
        row["audit"]["first_phase_source_signatures"] = source_snapshot_signatures(original)
        write(self.base / directory / (cid + ".json"), original)
        return row

    def add_refresh(self):
        row = self.set_case("D10", urls=[driver.REFRESH_D10_URL])
        row["audit"]["unknown_extra_urls"] = []
        row["audit"]["events"] = [{"requested_url": driver.REFRESH_D10_URL, "state": "read_saved_body",
            "snapshot_origin": {"path": "data/private/web-evaluation/dev-final-control-live-v1/D10.json"}}]
        source = {"id": "authored-d10", "url": driver.REFRESH_D10_URL, "text": PHRASE, "title": "Authored old positive"}
        report = analyze(PHRASE, [source])
        report.update(text=PHRASE, sources=[source], scope="web_search", web={"errors": []})
        write(self.base / driver.COMPARISON_SERIES / "after/D10.json", report)
        return row

    def prepare(self):
        path = self.base / driver.COMPARISON_SERIES / "comparison.json"
        write(path, self.comparison)
        with patch.object(driver, "COMPARISON_SHA256", sha256(path.read_bytes()).hexdigest()):
            return driver.prepare(self.base, self.root)

    def execute(self, out, run, fetcher, clock):
        return driver.fill(out, run, sha256((out / "run.json").read_bytes()).hexdigest(), fetcher=fetcher, clock=clock)

    def test_prepare_status_are_offline_frozen_private_and_include_only_selected_unknowns_and_exact_refresh(self):
        self.set_case("D01", 2)
        self.add_refresh()
        with patch.object(driver, "Fetcher", side_effect=AssertionError("No fetcher in prepare/status")):
            out, run = self.prepare()
            state = driver.status(out, run)
        selected = run["configuration"]["selected_urls"]
        self.assertEqual(state["selected_unique_urls"], 3)
        self.assertEqual(state["durable_attempt_intents"], 0)
        self.assertEqual(selected[-1]["requested_url"], driver.REFRESH_D10_URL)
        self.assertEqual(selected[-1]["selection"][0]["kind"], "approved_d10_old_positive_refresh")
        self.assertEqual(run["configuration"]["maximum_cumulative_direct_attempts"], 160)
        self.assertEqual(run["configuration"]["pdf_alternative_engine"], "pdfium")
        self.assertEqual((out / "run.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)

    def test_durable_intent_precedes_each_get_and_first_phase_files_remain_unchanged(self):
        self.set_case("D01", 2)
        out, run = self.prepare()
        before = {path: path.read_bytes() for path in self.base.rglob("*.json")}
        def inspect_intent(url):
            path = out / "fills" / (driver._hash(url) + ".json")
            intent = json.loads(path.read_text())
            self.assertEqual(intent["state"], "submitted")
            self.assertEqual(intent["selected_case_ids"], ["D01"])
            self.assertEqual(intent["source_comparison_sha256"], run["configuration"]["source_comparison_sha256"])
            self.assertEqual(intent["candidate_sha256"], run["configuration"]["candidate_sha256"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        clock = Clock()
        fetcher = FakeFetcher(clock, duration=2, before=inspect_intent)
        result = self.execute(out, run, fetcher, clock)
        self.assertEqual(result["new_attempts_in_this_invocation"], 2)
        self.assertEqual(result["complete"], 2)
        self.assertEqual(result["extra_elapsed_seconds_by_case"]["D01"], 4)
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)
        record = json.loads(next((out / "fills").glob("*.json")).read_text())
        self.assertEqual(record["fetched"]["text"], BODY)
        self.assertEqual(record["elapsed_seconds"], 2)

    def test_success_typed_failure_and_submitted_intents_are_never_retried(self):
        self.set_case("D01", 3)
        out, run = self.prepare()
        entries = run["configuration"]["selected_urls"]
        unknown = entries[2]["requested_url"]
        path = out / "fills" / (driver._hash(unknown) + ".json")
        driver._write(path, {**entries[2], "state": "submitted",
            "source_comparison_sha256": run["configuration"]["source_comparison_sha256"],
            "candidate_sha256": run["configuration"]["candidate_sha256"]}, exclusive=True)
        unknown_bytes = path.read_bytes()
        clock = Clock()
        fetcher = FakeFetcher(clock, failed=[entries[1]["requested_url"]])
        first = self.execute(out, run, fetcher, clock)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(first["durable_attempt_intents"], 3)
        self.assertEqual(first["complete"], 1)
        self.assertEqual(first["failed_or_unknown"], 2)
        second = self.execute(out, run, fetcher, clock)
        self.assertEqual(second["new_attempts_in_this_invocation"], 0)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(path.read_bytes(), unknown_bytes)

    def test_empty_interrupted_intent_blocks_get_and_stays_untouched(self):
        self.set_case("D01")
        out, run = self.prepare()
        url = run["configuration"]["selected_urls"][0]["requested_url"]
        path = out / "fills" / (driver._hash(url) + ".json")
        path.parent.mkdir()
        path.write_bytes(b"")
        fetcher = Mock()
        result = self.execute(out, run, fetcher, Clock())
        fetcher.fetch.assert_not_called()
        self.assertEqual(result["durable_attempt_intents"], 1)
        self.assertEqual(result["failed_or_unknown"], 1)
        self.assertEqual(path.read_bytes(), b"")

    def test_cap_counts_all_intents_and_never_makes_attempt_one_hundred_sixty_one(self):
        for index in range(1, 31):
            self.set_case(f"D{index:02d}", 12)
        out, run = self.prepare()
        first = run["configuration"]["selected_urls"][0]
        path = out / "fills" / (driver._hash(first["requested_url"]) + ".json")
        path.parent.mkdir()
        path.write_bytes(b"")
        clock = Clock()
        fetcher = FakeFetcher(clock)
        result = self.execute(out, run, fetcher, clock)
        self.assertEqual(len(fetcher.calls), 159)
        self.assertEqual(result["durable_attempt_intents"], 160)
        self.assertEqual(len(list((out / "fills").glob("*.json"))), 160)
        self.assertEqual(len(result["skipped_unknown_urls"]), 200)
        self.assertTrue(all(item["reason"] == "cumulative_direct_attempt_cap" for item in result["skipped_unknown_urls"]))

    def test_case_budget_preserves_normal_fetcher_timeout_and_exports_explicit_unknowns(self):
        self.set_case("D01", 3, elapsed=260)
        out, run = self.prepare()
        clock = Clock()
        fetcher = FakeFetcher(clock, duration=15)
        result = self.execute(out, run, fetcher, clock)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(result["extra_elapsed_seconds_by_case"]["D01"], 30)
        self.assertEqual(len(result["skipped_unknown_urls"]), 1)
        skipped = result["skipped_unknown_urls"][0]
        self.assertEqual(skipped["reason"], "per_case_total_elapsed_budget")
        self.assertEqual(skipped["remaining_seconds"]["D01"], 10)
        resumed = self.execute(out, run, fetcher, clock)
        self.assertEqual(resumed["new_attempts_in_this_invocation"], 0)
        self.assertEqual(len(fetcher.calls), 2)

    def test_one_shared_url_is_deduplicated_and_respects_every_selected_case_budget(self):
        shared = "https://authored-fill.test/shared"
        self.set_case("D01", elapsed=281, urls=[shared])
        self.set_case("D02", elapsed=0, urls=[shared])
        out, run = self.prepare()
        self.assertEqual(len(run["configuration"]["selected_urls"]), 1)
        self.assertEqual(run["configuration"]["selected_urls"][0]["selected_case_ids"], ["D01", "D02"])
        fetcher = Mock()
        result = self.execute(out, run, fetcher, Clock())
        fetcher.fetch.assert_not_called()
        self.assertEqual(result["skipped_unknown_urls"][0]["case_ids"], ["D01"])

    def test_invalid_unknown_selector_and_changed_frozen_input_abort_before_any_get(self):
        row = self.set_case("D01")
        row["audit"]["unknown_extra_urls"].append("https://not-selected.test/page")
        with self.assertRaises(ValueError):
            self.prepare()
        row["audit"]["unknown_extra_urls"].pop()
        out, run = self.prepare()
        path = self.base / driver.COMPARISON_SERIES / "comparison.json"
        frozen_hash = sha256(path.read_bytes()).hexdigest()
        path.write_bytes(path.read_bytes() + b"\n")
        with patch.object(driver, "COMPARISON_SHA256", frozen_hash), self.assertRaisesRegex(ValueError, "frozen ZERO V3"):
            driver.prepare(self.base, self.root)
        self.assertEqual(driver.status(out, run)["durable_attempt_intents"], 0)

    def test_changed_first_phase_source_with_recomputed_hash_is_rejected_against_frozen_signatures(self):
        self.set_case("D01")
        directory = next(name for name, ids in driver.FINAL_BATCHES if "D01" in ids)
        path = self.base / directory / "D01.json"
        original = json.loads(path.read_text())
        original["sources"][0]["text"] += " Changed after frozen selection."
        original["sources"][0]["provenance"]["text_sha256"] = sha256(original["sources"][0]["text"].encode()).hexdigest()
        write(path, original)
        with self.assertRaisesRegex(ValueError, "source snapshots differ"):
            self.prepare()

    def test_old_positive_refresh_requires_exact_selected_url_origin_and_grounded_match(self):
        row = self.add_refresh()
        row["audit"]["events"][0]["snapshot_origin"]["path"] = "data/private/web-evaluation/unapproved/D10.json"
        out, run = self.prepare()
        self.assertEqual(run["configuration"]["selected_urls"], [])
        self.assertEqual(driver.status(out, run)["durable_attempt_intents"], 0)

    def test_start_flags_fail_before_preparation_and_wrong_manifest_before_get(self):
        with patch.object(driver, "prepare", side_effect=AssertionError("must not prepare")), patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                driver.main(["--fill"])
        self.set_case("D01")
        out, run = self.prepare()
        fetcher = Mock()
        with self.assertRaisesRegex(ValueError, "exact reviewed manifest"):
            driver.fill(out, run, "f" * 64, fetcher=fetcher)
        fetcher.fetch.assert_not_called()

    def test_status_does_not_reopen_current_inputs_or_construct_fetcher(self):
        self.set_case("D01")
        out, _ = self.prepare()
        with patch.object(driver, "BASE", self.base), patch.object(driver, "prepare", side_effect=AssertionError("no prepare")), \
             patch.object(driver, "Fetcher", side_effect=AssertionError("no fetcher")), redirect_stdout(io.StringIO()) as output:
            driver.main(["--status"])
        self.assertIn('"durable_attempt_intents": 0', output.getvalue())
        self.assertTrue((out / "run.json").exists())


if __name__ == "__main__":
    unittest.main()
