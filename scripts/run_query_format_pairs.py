#!/usr/bin/env python3
"""Prepare a fixed six-call quoted/unquoted diagnosis; live needs separate START.

Only outer ASCII quotation marks change. Input text, original DENSE spans,
word sequences, order and count remain unchanged. No default is modified.
Expected URLs and annotation fields never enter planning or HTTP requests.
"""
import argparse
from contextvars import ContextVar
from copy import deepcopy
import fcntl
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
from threading import Thread
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search as web
from plagiarism.matching import _tokens
from scripts.run_dev_query_probe import quota
from scripts.run_dev_metadata_pilot import private_dump, RecordingPages
from scripts.run_unfamiliar_query_v2 import RecordedSearch, recover_or_submit
from scripts.summarize_night_results import DEV_KEY_SHA

SPEC = (("D01", 1), ("D02", 1), ("D20", 0))
FORMATS = ("quoted", "unquoted")
CALL_CAP = 6
DEFAULT_NAME = "dev-query-format-pair-v1"
VERSION = "outer-quotes-only-diagnostic-v1"
CODE_FILES = ("scripts/run_query_format_pairs.py", "scripts/run_unfamiliar_query_v2.py",
    "scripts/run_dev_metadata_pilot.py", "scripts/run_dev_query_probe.py", "server.py",
    "plagiarism/web_search.py", "plagiarism/search_tavily.py", "plagiarism/matching.py",
    "plagiarism/web_fetch.py", "plagiarism/pdf_source.py", "plagiarism/pdf_evidence.py",
    "plagiarism/input_references.py", "plagiarism/jobs.py", "plagiarism/storage.py")


def word_span_identity(plans):
    return [(p["start"], p["end"], [t.value for t in _tokens(p["query"])]) for p in plans]


def remove_outer_quotes(plans):
    """No strip, normalization, spelling repair, reordering, or span change."""
    result = deepcopy(plans)
    for item in result:
        query = item.get("query")
        if not isinstance(query, str) or len(query) < 3 or query[0] != '"' or query[-1] != '"' or query.count('"') != 2:
            raise ValueError("Expected one canonical pair of outer ASCII quotation marks")
        item["query"] = query[1:-1]
    if word_span_identity(plans) != word_span_identity(result):
        raise ValueError("Quote-only transformation changed words or spans")
    return result


def code_identity():
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in CODE_FILES}


def prepare(name=DEFAULT_NAME):
    if not re.fullmatch(r"dev-query-format-pair-v[1-9][0-9]*", name):
        raise ValueError("Only an explicit query-format diagnostic run name is allowed")
    dev = ROOT / "data/private/web-evaluation/materials/dev-30"
    raw = (dev / "key.json").read_bytes()
    if sha256(raw).hexdigest() != DEV_KEY_SHA:
        raise ValueError("Frozen DEV key changed")
    cases = json.loads(raw)["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only DEV30 metadata are supported")
    texts, original, jobs = {}, {}, {}
    for cid, index in SPEC:
        case = next(c for c in cases if c["id"] == cid)
        if case["input_path"] != "inputs/" + cid + ".txt":
            raise ValueError("Unexpected diagnostic DEV input path")
        text = (dev / case["input_path"]).read_text()
        if sha256(text.encode()).hexdigest() != case["text_sha256"]:
            raise ValueError("Diagnostic input changed")
        plans = web.plan_queries(text, strategy=web.DENSE_STRATEGY)
        unquoted = remove_outer_quotes(plans)
        if len(plans) != 6 or len(unquoted) != 6:
            raise ValueError("The unchanged DENSE plan must still contain six queries")
        texts[cid], original[cid] = text, plans
        for fmt, values in (("quoted", plans), ("unquoted", unquoted)):
            jobs[cid + "-" + fmt] = {"case_id": cid, "format": fmt, "base_query_index": index,
                "text_sha256": case["text_sha256"], "plan": [values[index]]}
    config = {"version": VERSION, "spec": [list(s) for s in SPEC], "formats": list(FORMATS),
        "input_key_sha256": DEV_KEY_SHA, "original_dense_plans": original, "jobs": jobs,
        "code_sha256": code_identity(), "max_provider_calls": CALL_CAP, "max_calls_per_job": 1,
        "base_query_strategy": web.DENSE_STRATEGY, "url_strategy": web.PATH_URL_STRATEGY,
        "named_references": False, "pdf_alternative_engine": os.environ.get("ISTOK_PDF_ALTERNATIVE_ENGINE"),
        "selection_is_diagnostic_only": True, "default_changed": False}
    fingerprint = sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    run = {"id": sha256((name + fingerprint).encode()).hexdigest()[:32], "run_name": name, "configuration": config}
    run["request_ids"] = {key: sha256((run["id"] + key + item["text_sha256"]).encode()).hexdigest()[:32]
                          for key, item in jobs.items()}
    out = ROOT / "data/private/web-evaluation" / name
    path = out / "run.json"
    if path.exists():
        if json.loads(path.read_text()) != run:
            raise ValueError("Prepared code/input/plan changed; preserve this run and prepare a new unsubmitted version")
    else:
        private_dump(path, run, exclusive=True)
    return out, run, texts


def call_map(run):
    jobs = run["configuration"]["jobs"]
    if len(jobs) != CALL_CAP or set(jobs) != {cid + "-" + fmt for cid, _ in SPEC for fmt in FORMATS}:
        raise ValueError("Diagnostic job scope changed")
    return {sha256((run["request_ids"][key] + "\0" + "0").encode()).hexdigest():
            {"case_id": key, "query_index": 0, "query": item["plan"][0]["query"]} for key, item in jobs.items()}


def candidate_runner(original_run, original_plan, out, run, texts, search, real_pages):
    active = ContextVar("query-format-diagnostic", default=None)
    by_request = {rid: key for key, rid in run["request_ids"].items()}
    jobs = run["configuration"]["jobs"]

    def planner(value, limit=web.MAX_QUERIES, strategy=web.DEFAULT_QUERY_STRATEGY):
        key = active.get()
        if key is not None and strategy == web.DENSE_STRATEGY:
            if value != texts[jobs[key]["case_id"]] or type(limit) is not int or not 1 <= limit <= 6:
                raise ValueError("Diagnostic planner accepts only its unchanged target text")
            return deepcopy(jobs[key]["plan"])
        return original_plan(value, limit=limit, strategy=strategy)

    def runner(value, request_id=None, progress=None, **kwargs):
        key = by_request.get(request_id)
        if key is None or value != texts[jobs[key]["case_id"]] or kwargs:
            raise ValueError("Request outside the frozen six-call diagnosis")
        token = active.set(key)
        try:
            result = original_run(value, request_id=request_id, progress=progress, searcher=search,
                fetcher=RecordingPages(out, key, real_pages, search.real.key),
                query_strategy=web.DENSE_STRATEGY, url_strategy=web.PATH_URL_STRATEGY,
                use_named_references=False, use_zero_fallback=False)
        finally:
            active.reset(token)
        result["web"].update(query_strategy=VERSION + ":" + jobs[key]["format"],
            diagnostic_query_plan=deepcopy(jobs[key]), default_changed=False,
            base_query_strategy=web.DENSE_STRATEGY)
        return result
    return planner, runner


def live(out, run, texts, reviewed_sha):
    if sha256((out / "run.json").read_bytes()).hexdigest() != reviewed_sha:
        raise ValueError("START requires the exact reviewed manifest SHA256")
    if run["configuration"]["code_sha256"] != code_identity():
        raise ValueError("Code changed after preparation")
    import server
    from plagiarism.jobs import Jobs
    from plagiarism.storage import Store
    from plagiarism.search_tavily import TavilySearch
    from plagiarism.saas import report_identifier
    from plagiarism.web_fetch import Fetcher
    lock = open(out / "live.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        real = TavilySearch(web.SearchBudget())
        search = RecordedSearch(real, out, call_map(run), max_queries=CALL_CAP, max_per_document=1)
        original_run, original_plan = web.run, web.plan_queries
        web.plan_queries, web.run = candidate_runner(original_run, original_plan, out, run, texts, search, Fetcher())
        service = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        service.saas, service.store, service.jobs = None, Store(), Jobs()
        Thread(target=service.serve_forever, daemon=True).start()
        def http(path, body=None, missing_ok=False):
            connection = HTTPConnection("127.0.0.1", service.server_port, timeout=20)
            try:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
                connection.request("POST" if body is not None else "GET", path, payload,
                                   {"Content-Type": "application/json; charset=utf-8"})
                response = connection.getresponse()
                result = json.load(response)
                if response.status != 200 and not (missing_ok and response.status == 404):
                    raise RuntimeError("Queue HTTP " + str(response.status) + "; no uncertain POST retry")
                return response.status, result
            finally:
                connection.close()
        try:
            for key, job_spec in run["configuration"]["jobs"].items():
                path = out / (key + ".json")
                marker = out / (key + "-request.json")
                rid = run["request_ids"][key]
                call_id = sha256((rid + "\0" + "0").encode()).hexdigest()
                if not marker.exists() and (path.exists() or (out / (call_id + "-search.json")).exists()):
                    # GET recovery remains possible; a missing sidecar cannot permit resubmission.
                    status, result = http("/api/jobs/" + rid, missing_ok=True)
                    if status != 200:
                        raise RuntimeError("Orphan report/provider marker; preserve identity and do not POST")
                    private_dump(marker, {"request_id": rid, "text_sha256": job_spec["text_sha256"], "planned_queries": 1}, exclusive=True)
                text = texts[job_spec["case_id"]]
                def preflight():
                    private_dump(out / (key + "-quota-before.json"), quota(CALL_CAP))
                job = recover_or_submit(http, marker, rid, job_spec, text, 1, preflight)
                started = time.monotonic()
                while job["status"] not in {"complete", "failed", "cancelled"}:
                    if time.monotonic() - started > 420:
                        raise RuntimeError("Queue timeout; recover saved identity with GET only")
                    time.sleep(.5)
                    job = http("/api/jobs/" + rid)[1]["job"]
                report = service.store.get("report", report_identifier(rid))
                if report is None:
                    private_dump(out / (key + "-error.json"), job)
                    continue
                if (report["text"] != text or report["web"]["queries_attempted"] > 1
                        or report["web"]["query_spans"] != [{k: p[k] for k in ("start", "end")} for p in job_spec["plan"]]
                        or report["web"]["query_strategy"] != VERSION + ":" + job_spec["format"]):
                    raise ValueError("Saved report differs from frozen one-query diagnosis")
                private_dump(path, report, secret=real.key)
                print(key, "queries", report["web"]["queries_attempted"], "matched_words", report["stats"]["matched_words"], "report", report["id"], flush=True)
        finally:
            service.shutdown(); service.server_close(); service.jobs.close()
            web.run, web.plan_queries = original_run, original_plan
    finally:
        lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-name", default=DEFAULT_NAME)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--start-confirmation", choices=("START",))
    parser.add_argument("--max-new-queries", type=int)
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.live and (args.start_confirmation != "START" or args.max_new_queries != CALL_CAP
            or not re.fullmatch(r"[a-f0-9]{64}", args.manifest_sha256 or "")):
        parser.error("Live requires START, --max-new-queries 6 and exact reviewed --manifest-sha256")
    out, run, texts = prepare(args.run_name)
    if args.live:
        live(out, run, texts, args.manifest_sha256)
    print(json.dumps({"state": "PREPARED" if not args.live else "LIVE_RETURNED", "calls_cap": CALL_CAP,
        "manifest_sha256": sha256((out / "run.json").read_bytes()).hexdigest(),
        "saved_reports": [key for key in run["configuration"]["jobs"] if (out / (key + ".json")).exists()],
        "default_changed": False}, indent=2))


if __name__ == "__main__":
    main()
