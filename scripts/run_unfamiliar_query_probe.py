#!/usr/bin/env python3
"""One frozen first-use query experiment, with optional bounded HTTP execution.

This opens only query-unfamiliar-reserved-v1, never the final collection.
Prepare is offline. Live mode needs an explicit tested Git commit, confirmed
free headroom, zero paid overage, and the frozen chosen production defaults.
"""
import argparse
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_dev_query_probe import dump, quota, saved_request_identifier
from scripts.compare_web_queries_offline import conditional_highlight, probe_evidence
from plagiarism.matching import MATCHER_VERSION, _tokens, analyze
from plagiarism import web_search as web
from plagiarism.web_evaluation import _report, _url

RESERVED = ROOT / "data/private/web-evaluation/query-unfamiliar-reserved-v1"
OUT = ROOT / "data/private/web-evaluation/query-unfamiliar-first-use-v1"
CHOICE = ROOT / "docs/baselines/night-query-choice-2026-11-12.json"
MANIFEST_SHA = "62b312fa4cf4aab238e653f3f55f705e58a744f5dc961def36719b0756c5bc44"
MAX_QUERIES = 48


def checked_file(root, relative, expected):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Snapshot path leaves the reserved set")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != expected:
        raise ValueError("Reserved snapshot hash changed")
    return raw.decode("utf-8")


def load_reserved():
    choice_raw = CHOICE.read_bytes()
    choice = json.loads(choice_raw)
    if (choice["query_strategy"] != web.DENSE_STRATEGY or choice["url_strategy"] != web.BALANCED_URL_STRATEGY
            or choice["matcher_version"] != MATCHER_VERSION):
        raise ValueError("Chosen variant is not the frozen first-use variant")
    raw = (RESERVED / "manifest.json").read_bytes()
    if sha256(raw).hexdigest() != MANIFEST_SHA:
        raise ValueError("Reserved manifest changed; first-use cannot be silently restarted")
    manifest = json.loads(raw)
    if [c["id"] for c in manifest["cases"]] != [f"QH{i:02d}" for i in range(1, 9)]:
        raise ValueError("Only the fixed QH01–QH08 reservation is permitted")
    texts = {c["id"]: checked_file(RESERVED, c["input_path"], c["text_sha256"]) for c in manifest["cases"]}
    pages = {s["source_id"]: {"id": s["source_id"], "title": s["title"], "url": s["url"],
             "text": checked_file(RESERVED, s["snapshot_path"], s["text_sha256"])} for s in manifest["sources"]}
    if set(pages) != {"noaa-tides", "nasa-eclipse-safety"}:
        raise ValueError("Unexpected reserved source identities")
    for c in manifest["cases"]:
        if len(_tokens(texts[c["id"]])) != c["input_words"]:
            raise ValueError("Reserved input token count changed")
        for e in c["expected_evidence"]:
            source = checked_file(RESERVED, e["source_snapshot_path"], e["source_text_sha256"])
            if (source != pages[e["source_id"]]["text"] or source[slice(*e["source_span"])] != e["source_quote"]
                    or texts[c["id"]][slice(*e["target_span"])] != e["source_quote"]):
                raise ValueError("Reserved evidence span changed")
    configuration = {"manifest_sha256": MANIFEST_SHA, "choice_sha256": sha256(choice_raw).hexdigest(),
        "query_strategy": choice["query_strategy"], "url_strategy": choice["url_strategy"], "matcher_version": MATCHER_VERSION,
        "input_sha256": {c["id"]: c["text_sha256"] for c in manifest["cases"]},
        "source_sha256": {s["source_id"]: s["text_sha256"] for s in manifest["sources"]},
        "code_sha256": {name: sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in ("plagiarism/web_search.py", "plagiarism/matching.py")}}
    return manifest, texts, pages, configuration


def prepare(manifest, texts, pages, configuration):
    OUT.mkdir(mode=0o700, exist_ok=True)
    OUT.chmod(0o700)
    path = OUT / "run.json"
    if path.exists():
        run = json.loads(path.read_text())
        if run["configuration"] != configuration:
            raise ValueError("Frozen first-use run configuration changed")
    else:
        run = {"id": sha256((MANIFEST_SHA + configuration["choice_sha256"]).encode()).hexdigest()[:32],
               "configuration": configuration, "first_evaluation_started_at": datetime.now(timezone.utc).isoformat(),
               "git_head_at_first_use": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
               "provenance_note": manifest["provenance_note"], "used_for_tuning": False,
               "final_materials_read": False, "live_query_cap": MAX_QUERIES}
        dump(path, run)
    local_path = OUT / "local-first-use.json"
    if local_path.exists():
        return run, json.loads(local_path.read_text())
    # Every blind plan is built before any evidence is used for scoring.
    plans = {cid: {"citation_v2": web.plan_queries(text, strategy=web.CITATION_STRATEGY),
                   "chosen_dense": web.plan_queries(text, strategy=configuration["query_strategy"])} for cid, text in texts.items()}
    rows = []
    for c in manifest["cases"]:
        text = texts[c["id"]]
        result = analyze(text, list(pages.values()))
        evidence = []
        for e in c["expected_evidence"]:
            gold = {**e, "target_quote": e["source_quote"]}
            evidence.append({"plans": {name: probe_evidence(p, _tokens(text), gold, [t.value for t in _tokens(e["source_quote"])])
                                               for name, p in plans[c["id"]].items()},
                             "conditional_exact": conditional_highlight(result["matches"], _tokens(text),
                                                       _tokens(pages[e["source_id"]]["text"]), e)})
        rows.append({"id": c["id"], "synthetic": True, "plans": plans[c["id"]], "evidence": evidence,
                     "control": not bool(evidence), "saved_source_exact_pairs": sum(m["kind"] == "exact" for m in result["matches"]),
                     "saved_source_similar_pairs": sum(m["kind"] == "similar" for m in result["matches"])})
    positive = [e for row in rows for e in row["evidence"]]
    summary = {"documents": 8, "artificial_insertions": 6, "artificial_controls": 2,
        "whole_phrase_in_target_and_source": {name: sum(e["plans"][name]["whole_query_in_both_spans"] for e in positive)
                                               for name in ("citation_v2", "chosen_dense")},
        "actual_planned_queries": {name: sum(len(row["plans"][name]) for row in rows) for name in ("citation_v2", "chosen_dense")},
        "conditional_full_highlight": sum(e["conditional_exact"]["covered_words"] == e["conditional_exact"]["expected_words"] for e in positive),
        "conditional_copied_words": sum(e["conditional_exact"]["expected_words"] for e in positive),
        "conditional_highlighted_words": sum(e["conditional_exact"]["covered_words"] for e in positive),
        "control_exact_pairs": sum(row["saved_source_exact_pairs"] for row in rows if row["control"]),
        "control_similar_pairs": sum(row["saved_source_similar_pairs"] for row in rows if row["control"])}
    local = {"schema_version": 1, "data_kind": "first_use_for_query_selection", "used_for_tuning": False,
             "provenance_note": manifest["provenance_note"], "live_queries": 0, "summary": summary, "cases": rows}
    dump(local_path, local)
    return run, local


def live(manifest, texts, configuration, run, local, expected_head):
    current_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if current_head != expected_head or len(expected_head) != 40:
        raise ValueError("Explicit tested Git commit does not match current HEAD")
    if (web.DEFAULT_QUERY_STRATEGY != configuration["query_strategy"] or web.DEFAULT_URL_STRATEGY != configuration["url_strategy"]):
        raise ValueError("Tested production defaults do not match the frozen choice")
    subprocess.run(["git", "diff", "--exit-code", "--", "plagiarism/web_search.py", "plagiarism/matching.py"], cwd=ROOT, check=True, capture_output=True)
    planned = {row["id"]: len(row["plans"]["chosen_dense"]) for row in local["cases"]}
    if sum(planned.values()) > MAX_QUERIES:
        raise ValueError("First-use plan exceeds the 48-basic-query cap")
    unsent = [cid for cid in texts if not (OUT / (cid + ".json")).exists() and not (OUT / (cid + "-request.json")).exists()]
    if unsent:
        dump(OUT / "quota-before.json", quota(sum(planned[cid] for cid in unsent)))
    import server
    from plagiarism.jobs import Jobs
    from plagiarism.storage import Store
    from plagiarism.search_tavily import TavilySearch
    from plagiarism.saas import report_identifier
    real = TavilySearch(web.SearchBudget())
    class RecordingSearch:
        provider = "tavily"
        def search(self, query, request_id):
            path = OUT / (request_id + "-search.json")
            if path.exists():
                raise RuntimeError("A submitted provider call is never automatically repeated")
            if len(list(OUT.glob("*-search.json"))) >= MAX_QUERIES:
                raise RuntimeError("The frozen 48-basic-query cap is exhausted")
            record = {"request_id": request_id, "query": query, "search_depth": "basic", "state": "submitted"}
            dump(path, record)
            try:
                urls = real.search(query, request_id)
                record.update(state="complete", results=urls, usage=real.last_usage)
                dump(path, record)
                return urls
            except Exception as error:
                record.update(state="failed_or_unknown", code=getattr(error, "code", type(error).__name__))
                dump(path, record)
                raise
    original = web.run
    web.run = partial(original, searcher=RecordingSearch(), query_strategy=configuration["query_strategy"], url_strategy=configuration["url_strategy"])
    service = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    service.saas, service.store, service.jobs = None, Store(), Jobs()
    Thread(target=service.serve_forever, daemon=True).start()
    def http(path, body=None):
        connection = HTTPConnection("127.0.0.1", service.server_port, timeout=20)
        try:
            connection.request("POST" if body is not None else "GET", path,
                               json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.load(response)
            if response.status != 200:
                raise RuntimeError("Queue HTTP " + str(response.status) + "; saved identity retained, no automatic POST retry")
            return result
        finally:
            connection.close()
    try:
        for c in manifest["cases"]:
            cid = c["id"]
            path, request_path = OUT / (cid + ".json"), OUT / (cid + "-request.json")
            if path.exists():
                print(cid, "already saved", flush=True)
                continue
            request_id = sha256((run["id"] + cid + c["text_sha256"]).encode()).hexdigest()[:32]
            if request_path.exists():
                saved_request_identifier(request_path, request_id, c["text_sha256"], planned[cid])
                job = http("/api/jobs/" + request_id)["job"]
            else:
                dump(request_path, {"request_id": request_id, "text_sha256": c["text_sha256"], "planned_queries": planned[cid]})
                # Source URLs, labels, and expected spans never enter this body.
                job = http("/api/web-check-jobs", {"request_id": request_id, "text": texts[cid], "internet_consent": True})["job"]
            started = time.monotonic()
            while job["status"] not in ("complete", "failed", "cancelled"):
                if time.monotonic() - started > 420:
                    raise RuntimeError("Queue timeout; resume by saved GET, never a fresh request")
                time.sleep(.5)
                job = http("/api/jobs/" + request_id)["job"]
            report = service.store.get("report", report_identifier(request_id))
            if report is None:
                dump(OUT / (cid + "-error.json"), job)
                print(cid, "terminal failure", flush=True)
                continue
            if (report["text"] != texts[cid] or report["web"]["query_strategy"] != configuration["query_strategy"]
                    or report["web"]["queries_attempted"] > planned[cid] or report["web"]["urls_attempted"] > 12):
                raise ValueError("Persisted report differs from frozen bounded probe")
            dump(path, report)
            print(cid, "queries", report["web"]["queries_attempted"], "pages", report["web"]["sources_checked"],
                  "matched_words", report["stats"]["matched_words"], "report", report["id"], flush=True)
    finally:
        service.shutdown()
        service.server_close()
        service.jobs.close()
        web.run = original
    try:
        dump(OUT / "quota-after.json", quota(0))
    except Exception as error:
        dump(OUT / "quota-after.json", {"unavailable": type(error).__name__, "new_searches_allowed": False})


def score_live(manifest):
    """Score the completed first fresh run without changing its chosen variant."""
    manifest_raw = (RESERVED / "manifest.json").read_bytes()
    if sha256(manifest_raw).hexdigest() != MANIFEST_SHA or manifest != json.loads(manifest_raw):
        raise ValueError("Reserved manifest changed; frozen report scoring refused")
    frozen = json.loads((OUT / "run.json").read_text())["configuration"]
    rows = []
    for c in manifest["cases"]:
        path = OUT / (c["id"] + ".json")
        if not path.exists():
            error = OUT / (c["id"] + "-error.json")
            if not error.exists():
                raise ValueError("Fresh first-use run is still incomplete")
            rows.append({"id": c["id"], "status": "failed", "reason": json.loads(error.read_text())})
            continue
        raw = json.loads(path.read_text())
        if (raw["web"]["query_strategy"] != frozen["query_strategy"]
                or raw["web"]["url_strategy"] != frozen["url_strategy"]
                or raw["web"]["matcher_version"] != frozen["matcher_version"]):
            raise ValueError("Saved fresh report differs from the frozen evaluated variant")
        checked = _report(raw, c)
        tokens = _tokens(raw["text"])
        evidence = []
        for e in c["expected_evidence"]:
            wanted = [t for t in tokens if e["target_span"][0] <= t.start and t.end <= e["target_span"][1]]
            urls = {_url(u) for u in e["source_urls"]}
            def coverage(strict):
                return sum(any(m["target_span"][0] <= t.start and t.end <= m["target_span"][1]
                               and (not strict or m["url"] in urls) for m in checked["matches"]) for t in wanted)
            evidence.append({"expected_words": len(wanted), "any_source_words": coverage(False),
                "expected_source_words": coverage(True), "expected_url_discovered": bool(urls & checked["discovered_urls"]),
                "expected_url_read": bool(urls & {_url(s["url"]) for s in raw["sources"]})})
        rows.append({"id": c["id"], "status": "report", "report_id": raw["id"], "report_sha256": sha256(path.read_bytes()).hexdigest(),
            "query_strategy": raw["web"]["query_strategy"], "url_strategy": raw["web"]["url_strategy"],
            "matcher_version": raw["web"]["matcher_version"],
            "queries_attempted": raw["web"]["queries_attempted"], "pages_read": raw["web"]["sources_checked"],
            "coverage": raw["web"]["status"], "uniqueness_percent": raw["stats"]["uniqueness_percent"],
            "control": not bool(c["expected_evidence"]), "exact_pairs": len(checked["matches"]),
            "similar_pairs": sum(m["kind"] == "similar" for m in raw["matches"]),
            "invalid_matches": checked["invalid_highlights"], "evidence": evidence})
    evidence = [e for r in rows for e in r.get("evidence", [])]
    logs = [json.loads(p.read_text()) for p in OUT.glob("*-search.json")]
    summary = {"documents": 8, "reports": sum(r["status"] == "report" for r in rows),
        "expected_fragments": 6, "expected_source_url_discovered": sum(e["expected_url_discovered"] for e in evidence),
        "expected_source_url_read": sum(e["expected_url_read"] for e in evidence),
        "full_highlight_any_source": sum(e["any_source_words"] == e["expected_words"] for e in evidence),
        "full_highlight_expected_source": sum(e["expected_source_words"] == e["expected_words"] for e in evidence),
        "copied_words": 140, "any_source_words": sum(e["any_source_words"] for e in evidence),
        "expected_source_words": sum(e["expected_source_words"] for e in evidence),
        "control_documents": 2, "control_exact_pairs": sum(r.get("exact_pairs", 0) for r in rows if r.get("control")),
        "control_similar_pairs": sum(r.get("similar_pairs", 0) for r in rows if r.get("control")),
        "documents_without_read_pages": sum(r.get("pages_read") == 0 for r in rows),
        "provider_calls_submitted": len(logs), "provider_calls_complete": sum(q["state"] == "complete" for q in logs),
        "provider_calls_failed_or_unknown": sum(q["state"] != "complete" for q in logs),
        "known_basic_credits": sum((q.get("usage") or {}).get("credits", 0) for q in logs),
        "provider_usage_unknown_calls": sum(q.get("usage") is None for q in logs)}
    result = {"schema_version": 1, "data_kind": "first_fresh_query_evaluation_of_frozen_variant",
        "used_for_tuning": False, "baseline_fresh_search_not_run": True, "provenance_note": manifest["provenance_note"],
        "evaluated_matcher_version": frozen["matcher_version"], "scoring_validator_version": MATCHER_VERSION,
        "summary": summary, "cases": rows}
    previous = OUT / "fresh-first-use.json"
    if previous.exists():
        old_hashes = {r["id"]: r.get("report_sha256") for r in json.loads(previous.read_text())["cases"]}
        if any(old_hashes.get(r["id"]) != r.get("report_sha256") for r in rows):
            raise ValueError("A first-use report changed after its initial scoring")
    dump(OUT / "fresh-first-use.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--score-only", action="store_true", help="Validate already saved reports; never replan, search, or fetch.")
    parser.add_argument("--expected-git-head")
    args = parser.parse_args()
    if args.score_only:
        raw = (RESERVED / "manifest.json").read_bytes()
        if sha256(raw).hexdigest() != MANIFEST_SHA:
            raise ValueError("Reserved manifest changed")
        print(json.dumps(score_live(json.loads(raw))["summary"], ensure_ascii=False, indent=2))
        sys.exit(0)
    manifest, texts, pages, configuration = load_reserved()
    run, local = prepare(manifest, texts, pages, configuration)
    print(json.dumps(local["summary"], ensure_ascii=False, indent=2), flush=True)
    if args.live:
        if not args.expected_git_head:
            parser.error("Live execution requires the explicitly authorized tested commit")
        live(manifest, texts, configuration, run, local, args.expected_git_head)
        print(json.dumps(score_live(manifest)["summary"], ensure_ascii=False, indent=2), flush=True)
