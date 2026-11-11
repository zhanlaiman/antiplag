#!/usr/bin/env python3
"""Read-only DEV30 probe: preserve phase one; append saved search URLs on zero."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search as web
from plagiarism.web_fetch import FetchError
from scripts.analyze_web_dev_checks import inspect, compare
from scripts.audit_dev_fetch_balance import RUNS
from scripts.probe_zero_fallback import STRATEGY, extend_report
from scripts.replay_final_url_policies import actual_outcomes
from scripts.replay_saved_dev_search import canonical, load_queries, report_source_pages
from scripts.run_dev_query_probe import dump
from scripts.summarize_night_results import BASE, DEV, DEV_KEY_SHA, FINAL_BATCHES

OUT = BASE / "dev-zero-fallback-local-v4"
OLD_REPORT_RUNS = ("dev-live-30-v1", "dev-live-targeted-v3", *RUNS)


def pool_pages():
    """Only actual queued DEV reports/Fetcher captures, never source-oracle files."""
    pool, origins = {}, {}
    def store(url, page=None, code=None, *, path, cid=None):
        origin = {"path": str(path.relative_to(ROOT)), "artifact_sha256": sha256(path.read_bytes()).hexdigest(),
                  "case_id": cid, "kind": "actual_dev_web_report_or_normal_fetcher_capture"}
        origins[origin["path"]] = origin["artifact_sha256"]
        pool[canonical(url)] = {"page": page, "code": code, "origin": origin}
    for name in OLD_REPORT_RUNS:
        directory = BASE / name
        for path in sorted(directory.glob("D[0-3][0-9].json")):
            raw = json.loads(path.read_text())
            if raw.get("offline_replay"):
                raise ValueError("An unapproved replay entered the actual report cache")
            for url, page in report_source_pages(raw, {"path": str(path.relative_to(ROOT))}).items():
                store(url, page, path=path, cid=path.stem)
        for path in sorted(directory.glob("page-*-fetch.json")):
            record = json.loads(path.read_text())
            if record.get("state") == "complete":
                page = record["fetched"]
                for url in (record["requested_url"], page["url"]):
                    store(url, page, path=path)
            elif record.get("state") == "failed_or_unknown":
                code = record.get("code", "saved_page_outcome_unknown")
                store(record["requested_url"], code=code if isinstance(code, str) else "saved_page_outcome_unknown", path=path)
    originals = {}
    for name, ids in FINAL_BATCHES:
        loaded = actual_outcomes(BASE / name, ids)
        originals.update(loaded)
        for cid, item in loaded.items():
            for url, record in item["records"].items():
                path = ROOT / record["_artifact"]
                if record["state"] == "complete":
                    page = record["fetched"]
                    for alias in (url, page["url"]):
                        store(alias, page, path=path, cid=cid)
                else:
                    store(url, code=record["_typed_report_code"], path=path, cid=cid)
    # Explicit normalFetcher fills of a competing selector are legitimate page
    # snapshots, not an oracle. They cannot introduce a URL into search_order.
    for path in sorted((BASE / "dev-final-same-reply-url-v1/fills").glob("*.json")):
        record = json.loads(path.read_text())
        if record["state"] == "complete":
            page = record["fetched"]
            for url in (record["requested_url"], page["url"]):
                store(url, page, path=path, cid=record["discovery"]["case_id"])
        else:
            store(record["requested_url"], code=record.get("code", "saved_page_outcome_unknown"), path=path,
                  cid=record["discovery"]["case_id"])
    for cid in ("D08", "D10", "D04"):
        for path in sorted((BASE / "dev-metadata-pilot-v2/pages" / cid).glob("*.json")):
            record = json.loads(path.read_text())
            if record["state"] == "complete":
                page = record["fetched"]
                for url in (record["requested_url"], page["url"]):
                    store(url, page, path=path, cid=cid)
            else:
                store(record["requested_url"], code=record.get("code", "saved_page_outcome_unknown"), path=path, cid=cid)
    fills = BASE / "dev-zero-fallback-fill-v1"
    if (fills / "run.json").exists():
        manifest = json.loads((fills / "run.json").read_text())
        configuration = manifest["configuration"]
        if configuration["source_comparison_sha256"] != "fd153ae10842f7d8467113cdf3dec073a67b815647e3ea3fbf0b84f2cfee3b17":
            raise ValueError("Fresh fallback fills belong to another source comparison")
        allowed = {entry["requested_url"] for entry in configuration["selected_urls"]}
        for path in sorted((fills / "fills").glob("*.json")):
            record = json.loads(path.read_text())
            if (record["requested_url"] not in allowed or path.stem != sha256(record["requested_url"].encode()).hexdigest()
                    or record["source_comparison_sha256"] != configuration["source_comparison_sha256"]):
                raise ValueError("Unbound fresh extra-page snapshot")
            cid = record["selected_case_ids"][0]
            if record["state"] == "complete":
                page = record["fetched"]
                for url in (record["requested_url"], page["url"]):
                    store(url, page, path=path, cid=cid)
            elif record["state"] == "submitted":
                store(record["requested_url"], code="saved_page_cache_miss", path=path, cid=cid)
            else:
                store(record["requested_url"], code=record.get("code", "saved_page_outcome_unknown"), path=path, cid=cid)
    def lookup(url):
        record = pool.get(canonical(url))
        if record is None:
            raise FetchError("No legitimate saved snapshot; network disabled", "saved_page_cache_miss")
        if record["page"] is None:
            error = FetchError("Saved normal fetch failed; no retry", record["code"])
            error.snapshot_origin = record["origin"]
            raise error
        return {**record["page"], "_snapshot_origin": record["origin"]}
    return lookup, originals, origins


def main():
    OUT.mkdir(mode=0o700, exist_ok=True)
    plan = {"strategy": STRATEGY, "minimum_initial_exact_words": 0, "first_phase_maximum": 12,
        "additional_maximum": 12, "overall_maximum": 24, "initial_gate_only": True,
        "continue_after_new_match": True, "new_queries": False, "recursive_followups": False,
        "new_gets": False, "allowed_old_report_runs": list(OLD_REPORT_RUNS),
        "additional_saved_pool": [name for name, _ in FINAL_BATCHES] + ["dev-final-same-reply-url-v1/fills", "dev-metadata-pilot-v2/pages", "dev-zero-fallback-fill-v1/fills"],
        "excluded": "No oracle/source-key snapshot folders, final70, first-use or RFC material",
        "candidate_sha256": sha256((ROOT / "scripts/probe_zero_fallback.py").read_bytes()).hexdigest(),
        "runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "frozen_before_scoring_at": datetime.now(timezone.utc).isoformat()}
    path = OUT / "frozen-plan.json"
    if path.exists():
        old = json.loads(path.read_text())
        if {k: v for k, v in old.items() if k != "frozen_before_scoring_at"} != {k: v for k, v in plan.items() if k != "frozen_before_scoring_at"}:
            raise ValueError("Frozen local fallback changed; preserve this result and choose a new diagnostic version")
        plan = old
    else:
        dump(path, plan)
    if sha256((DEV / "key.json").read_bytes()).hexdigest() != DEV_KEY_SHA:
        raise ValueError("DEV key changed")
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only fixed DEV30 is accepted")
    denied = []
    def no_network(event, arguments):
        if event.startswith("socket."):
            denied.append(event)
            raise PermissionError("Zero fallback probe is strictly local")
    sys.addaudithook(no_network)
    lookup, originals, origins = pool_pages()
    rows = []
    (OUT / "before").mkdir(mode=0o700, exist_ok=True)
    (OUT / "after").mkdir(mode=0o700, exist_ok=True)
    for case in cases:
        cid = case["id"]
        original = originals[cid]["report"]
        text = (DEV / case["input_path"]).read_text()
        if text != original["text"] or sha256(text.encode()).hexdigest() != case["text_sha256"]:
            raise ValueError("Original input changed")
        directory = BASE / next(name for name, ids in FINAL_BATCHES if cid in ids)
        records, _ = load_queries(directory, cid, case["text_sha256"])
        groups = [[web._canonical_url(value["url"]) for value in record["results"]]
                  for record in records.values()]
        urls = list(dict.fromkeys(url for group in groups for url in group if url))
        full_order = web._rank_url_paths(web._fetch_order(urls, groups, web.PATH_URL_STRATEGY), text)
        report, audit = extend_report(text, original, full_order, lookup)
        if denied:
            raise ValueError("Network attempted")
        report["offline_replay"] = {"kind": "zero_fallback_saved_data_only", "main_store_written": False,
            "new_searches": 0, "new_fetches": 0, "original_report_id": original["id"],
            "unknown_selected_pages": audit["unknown_extra_urls"], "prefix_preserved": audit["prefix_preserved"]}
        report["id"] = sha256((STRATEGY + cid).encode()).hexdigest()[:32]
        dump(OUT / "before" / (cid + ".json"), original)
        dump(OUT / "after" / (cid + ".json"), report)
        row = {"id": cid, "before_matched_words_all": original["stats"]["matched_words"],
            "after_matched_words_all": report["stats"]["matched_words"], "original_report_id": original["id"],
            "audit": audit}
        rows.append(row)
        print(cid, row["before_matched_words_all"], row["after_matched_words_all"],
              "extra", len(audit["extra_attempted_urls"]), "unknown", len(audit["unknown_extra_urls"]), flush=True)
    before, after = inspect(DEV, OUT / "before"), inspect(DEV, OUT / "after")
    dump(OUT / "before-analysis.json", before)
    dump(OUT / "after-analysis.json", after)
    result = {"schema_version": 1, "frozen_plan": plan, "new_searches": 0, "new_fetches": 0,
        "comparison": compare(before, after), "triggered_documents": sum(r["audit"]["triggered"] for r in rows),
        "extra_scheduled_attempts": sum(len(r["audit"]["extra_attempted_urls"]) for r in rows),
        "unknown_extra_contents": sum(len(r["audit"]["unknown_extra_urls"]) for r in rows),
        "typed_failed_extra_attempts": sum(len(r["audit"]["failed_extra_urls"]) for r in rows),
        "all_first_phase_snapshots_preserved": all(r["audit"]["prefix_preserved"] for r in rows),
        "cached_extension_seconds": sum(r["audit"]["cached_fallback_seconds"] for r in rows),
        "saved_pool_artifacts_sha256": origins, "cases": rows,
        "latency_note": "Only cached CPU time measured. A real extra phase adds at most12 HTTP loads and needs a total-check deadline around300s; this is not a same-twelve efficiency claim.",
        "limitation": "Unknown extra page bodies are explicit. Known-word gains on historical/current saved snapshots need targeted normal-Fetcher confirmation before production promotion."}
    dump(OUT / "comparison.json", result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"cases", "saved_pool_artifacts_sha256"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
