#!/usr/bin/env python3
"""Bounded, recoverable two-query DEV D02 diagnosis through the HTTP queue.

Only the full D02 input enters either planner. The clause variant changes one
segmentation predicate in an isolated process; it is not a production policy.
At most two Tavily Basic calls are possible, after a fresh free-quota check.
"""
from datetime import datetime, timezone
from contextvars import ContextVar
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_dev_query_probe import dump, quota, saved_request_identifier

DEV = ROOT / "data/private/web-evaluation/materials/dev-30"
OUT = ROOT / "data/private/web-evaluation/dev-d02-clause-pair-live-v1"
VARIANTS = {"original": "dev-d02-original-single-query-v1", "clause": "dev-d02-dash-clause-single-query-v1"}


def plans_from_text(text):
    from plagiarism import web_search as web
    from scripts.probe_dash_clause_queries import DASH_WINDOWS, OLD, NEW
    original = web.plan_queries(text, limit=1, strategy=web.CITATION_STRATEGY)
    with patch.object(web, "_distinctive_windows", DASH_WINDOWS):
        clause = web.plan_queries(text, limit=1, strategy=web.CITATION_STRATEGY)
    if len(original) != 1 or len(clause) != 1 or original == clause:
        raise ValueError("The diagnosis needs two different single-query plans")
    return {"original": original, "clause": clause}, {"before": OLD, "after": NEW,
        "distinctive_function_sha256": sha256(inspect.getsource(web._distinctive_windows).encode()).hexdigest()}


def summarize(text, case, reports):
    from plagiarism.matching import _tokens
    from plagiarism.web_evaluation import _url
    rows = []
    for variant, report in reports.items():
        evidence = []
        for gold in case["expected_evidence"]:
            urls = {_url(url) for url in gold["source_urls"]}
            wanted = [t for t in _tokens(text) if gold["target_span"][0] <= t.start and t.end <= gold["target_span"][1]]
            matching = [m for m in report["matches"] if m["kind"] == "exact"]
            covered = lambda matches: sum(any(m["start"] <= t.start and t.end <= m["end"] for m in matches) for t in wanted)
            evidence.append({"expected_words": len(wanted),
                "expected_url_discovered": bool(urls & {_url(u) for u in report["web"]["discovered_urls"]}),
                "expected_url_read": bool(urls & {_url(s["url"]) for s in report["sources"]}),
                "covered_words_any_source": covered(matching),
                "covered_words_expected_source": covered([m for m in matching if _url(m["source_url"]) in urls])})
        rows.append({"variant": variant, "query_strategy": report["web"]["query_strategy"], "report_id": report["id"],
            "queries_attempted": report["web"]["queries_attempted"], "pages": report["web"]["sources_checked"],
            "web_status": report["web"]["status"], "matched_words": report["stats"]["matched_words"],
            "evidence": evidence, "matches": [{k: m.get(k) for k in
                ("kind", "source_url", "start", "end", "source_start", "source_end", "excerpt", "source_excerpt")} for m in report["matches"]],
            "errors": report["web"]["errors"]})
    return {"schema_version": 1, "case_id": "D02", "fresh_comparison": True, "rows": rows,
        "limits": ["One fresh response per variant; this does not estimate search-engine stability.",
                   "Both variants use a single-query citation plan with the normal source-link behavior.",
                   "The global dash-boundary policy was rejected on other artificial examples; no default was changed."]}


def main():
    os.umask(0o077)
    import server
    from plagiarism import web_search as web
    from plagiarism.jobs import Jobs
    from plagiarism.search_tavily import TavilySearch
    from plagiarism.storage import Store
    from plagiarism.saas import report_identifier

    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only fixed DEV metadata are supported")
    case = next(c for c in cases if c["id"] == "D02")
    if case["input_path"] != "inputs/D02.txt":
        raise ValueError("Unexpected D02 path")
    text = (DEV / case["input_path"]).read_text()
    text_hash = sha256(text.encode()).hexdigest()
    if text_hash != case["text_sha256"]:
        raise ValueError("D02 input changed")
    plans, predicate = plans_from_text(text)
    OUT.mkdir(mode=0o700, exist_ok=True)
    OUT.chmod(0o700)
    configuration = {"case_id": "D02", "text_sha256": text_hash, "plans": plans, "strategies": VARIANTS,
                     "single_predicate_change": predicate, "max_new_queries": 2,
                     "url_strategy": web.DOMAIN_URL_STRATEGY, "source_link_expansion": True}
    manifest = OUT / "run.json"
    if manifest.exists():
        run = json.loads(manifest.read_text())
        if any(run.get(k) != v for k, v in configuration.items()):
            raise ValueError("Saved pair cannot change its input or plans")
    else:
        run = {**configuration, "id": sha256(OUT.name.encode()).hexdigest()[:32],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
        dump(manifest, run)
    requests = {variant: sha256((run["id"] + variant + text_hash).encode()).hexdigest()[:32] for variant in VARIANTS}
    allowed_calls = {sha256((identifier + "\0" + "0").encode()).hexdigest() for identifier in requests.values()}
    unsent = [variant for variant in VARIANTS if not (OUT / (variant + ".json")).exists()
              and not (OUT / (variant + "-request.json")).exists()]
    if unsent:
        try:
            # The authorization covers this exact pair; a fresh full-pair
            # preflight is required even when only one variant remains unsent.
            dump(OUT / "quota-before.json", quota(2))
        except Exception as error:
            dump(OUT / "quota-before.json", {"unavailable": type(error).__name__, "new_searches_allowed": False})
            print("Free allowance unconfirmed; no fresh search started.", flush=True)
            return

    real = TavilySearch(web.SearchBudget())
    class RecordingSearch:
        provider = "tavily"
        def search(self, query, request_id):
            path = OUT / (request_id + "-search.json")
            if request_id not in allowed_calls or path.exists():
                raise web.WebSearchError("Pair call already exists or is outside its two-call cap.", "search_already_attempted")
            if sum((OUT / (call + "-search.json")).exists() for call in allowed_calls) >= 2:
                raise web.WebSearchError("The pair has used its two-call allowance.", "search_budget_exhausted")
            record = {"request_id": request_id, "query": query, "state": "submitted", "search_depth": "basic"}
            dump(path, record)
            try:
                results = real.search(query, request_id)
                record.update(state="complete", results=results, usage=real.last_usage)
                dump(path, record)
                return results
            except Exception as error:
                record.update(state="failed_or_unknown", code=getattr(error, "code", type(error).__name__))
                dump(path, record)
                raise

    original_plan, original_run = web.plan_queries, web.run
    active_variant = ContextVar("d02_pair_variant", default=None)
    variant_by_request = {identifier: variant for variant, identifier in requests.items()}
    variant_by_strategy = {strategy: variant for variant, strategy in VARIANTS.items()}
    def experimental_plan(value, limit=web.MAX_QUERIES, strategy=web.DEFAULT_QUERY_STRATEGY):
        variant = variant_by_strategy.get(strategy)
        if strategy == web.CITATION_STRATEGY and active_variant.get() is not None:
            variant = active_variant.get()
        if variant is not None:
            if value != text or type(limit) is not int or not 1 <= limit <= web.MAX_QUERIES:
                raise ValueError("Experimental planner accepts only the unchanged D02 input")
            return [dict(item) for item in plans[variant]]
        return original_plan(value, limit=limit, strategy=strategy)
    def experimental_run(value, request_id=None, progress=None, **kwargs):
        if value != text or request_id not in variant_by_request or kwargs:
            raise ValueError("Unexpected request outside the D02 pair")
        variant = variant_by_request[request_id]
        token = active_variant.set(variant)
        try:
            report = original_run(value, request_id=request_id, progress=progress, searcher=RecordingSearch(),
                query_strategy=web.CITATION_STRATEGY, url_strategy=web.DOMAIN_URL_STRATEGY,
                use_named_references=False, use_zero_fallback=False)
        finally:
            active_variant.reset(token)
        report["web"]["query_strategy"] = VARIANTS[variant]
        report["web"]["experimental_plan"] = {"case_id": "D02", "variant": variant,
            "base_strategy": web.CITATION_STRATEGY, "source_link_expansion": True, "promoted_to_default": False}
        return report
    web.plan_queries, web.run = experimental_plan, experimental_run
    service = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    service.saas, service.store, service.jobs = None, Store(), Jobs()
    Thread(target=service.serve_forever, daemon=True).start()
    def http(path, body=None):
        connection = HTTPConnection("127.0.0.1", service.server_port, timeout=20)
        try:
            connection.request("POST" if body is not None else "GET", path,
                json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.load(response)
        finally:
            connection.close()
    reports = {}
    try:
        for variant in VARIANTS:
            path, request_path = OUT / (variant + ".json"), OUT / (variant + "-request.json")
            request_id = requests[variant]
            if request_path.exists():
                saved_request_identifier(request_path, request_id, text_hash, 1)
            # Always inspect the stable identifier before any possible POST.
            status, saved = http("/api/jobs/" + request_id)
            if status == 200:
                job = saved["job"]
            elif status == 404 and not request_path.exists() and not path.exists():
                dump(request_path, {"request_id": request_id, "text_sha256": text_hash, "planned_queries": 1,
                                    "variant": variant, "query_plan": plans[variant]})
                status, submitted = http("/api/web-check-jobs", {"request_id": request_id, "text": text, "internet_consent": True})
                if status != 200:
                    raise RuntimeError("Queue submission returned HTTP " + str(status))
                job = submitted["job"]
            else:
                raise RuntimeError("Saved request outcome unavailable; no repeat POST allowed")
            started = time.monotonic()
            while job["status"] not in ("complete", "failed", "cancelled"):
                if time.monotonic() - started > 420:
                    raise RuntimeError("Queue timeout; preserve identifier and recover with GET")
                time.sleep(.5)
                status, saved = http("/api/jobs/" + request_id)
                if status != 200:
                    raise RuntimeError("Queue recovery GET failed; no repeat POST allowed")
                job = saved["job"]
            report = service.store.get("report", report_identifier(request_id))
            if report is None:
                dump(OUT / (variant + "-error.json"), job)
                print(variant, "terminal failure", job.get("code"), flush=True)
                continue
            if (report["text"] != text or report["web"]["query_strategy"] != VARIANTS[variant]
                    or report["web"]["queries_attempted"] > 1
                    or report["web"]["query_spans"] != [{k: item[k] for k in ("start", "end")} for item in plans[variant]]):
                raise ValueError("Persisted report differs from the approved one-query plan")
            dump(path, report)
            reports[variant] = report
            print(variant, "queries", report["web"]["queries_attempted"], "pages", report["web"]["sources_checked"],
                  "matched_words", report["stats"]["matched_words"], "report", report["id"], flush=True)
    finally:
        service.shutdown()
        service.server_close()
        service.jobs.close()
        web.plan_queries, web.run = original_plan, original_run
    dump(OUT / "comparison.json", summarize(text, case, reports))
    try:
        dump(OUT / "quota-after.json", quota(0))
    except Exception as error:
        dump(OUT / "quota-after.json", {"unavailable": type(error).__name__, "new_searches_allowed": False})


if __name__ == "__main__":
    main()
