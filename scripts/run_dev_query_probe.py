#!/usr/bin/env python3
"""Explicit, resumable DEV-only search probe through the normal HTTP queue.

No final collection is read. New searches require a fresh quota response with
zero pay-as-you-go usage and conservative room in the included allowance.
Previously submitted requests are inspected by their saved identifier first.
"""
import argparse
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from threading import Thread
import time
from urllib.request import Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def dump(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def quota(planned):
    from plagiarism.auth_provider import NoRedirect
    from plagiarism.search_credentials import key_for
    request = Request("https://api.tavily.com/usage", headers={"Authorization": "Bearer " + key_for("tavily")})
    with build_opener(NoRedirect()).open(request, timeout=15) as response:
        result = json.load(response)
    account = result["account"]
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sqlite3.connect(ROOT / "data/search-budget.sqlite3") as database:
        local = database.execute("SELECT COUNT(*) FROM calls WHERE month=?", (month,)).fetchone()[0]
    state = confirmed_quota(account, local, planned)
    state.update(local_monthly_attempts=local, checked_at=datetime.now(timezone.utc).isoformat())
    return state


def confirmed_quota(account, local, planned):
    """Reject unknown/non-finite counters before authorizing any provider call."""
    state = {k: account.get(k) for k in ("current_plan", "plan_usage", "plan_limit", "paygo_usage", "paygo_limit")}
    numeric = lambda value: type(value) in (int, float) and math.isfinite(value) and value >= 0
    if (state["current_plan"] != "Researcher" or not numeric(state["plan_usage"])
            or type(state["plan_limit"]) is not int or state["plan_limit"] != 1000
            or not numeric(state["paygo_usage"]) or state["paygo_usage"] != 0
            or type(local) is not int or local < 0 or type(planned) is not int or planned < 0
            or max(local, state["plan_usage"]) + planned > 900):
        raise RuntimeError("Included free headroom and zero paid overage could not be confirmed; no new searches.")
    return state


def saved_request_identifier(path, expected_id, text_hash, planned):
    saved = json.loads(path.read_text())
    identifier = saved.get("request_id")
    if (not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier)
            or identifier != expected_id or saved.get("text_sha256") != text_hash
            or saved.get("planned_queries") != planned):
        raise ValueError("Saved submission identity does not match this DEV case; no new POST is allowed.")
    return identifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="+", required=True)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--url-strategy", help="Optional experimental page-selection policy")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--max-new-queries", type=int, required=True)
    args = parser.parse_args()
    if (not re.fullmatch(r"dev-[a-z0-9-]+", args.run_name) or len(set(args.ids)) != len(args.ids)
            or not args.ids or any(not re.fullmatch(r"D(?:0[1-9]|[12][0-9]|30)", cid) for cid in args.ids)
            or not 1 <= args.max_new_queries <= 36):
        parser.error("A bounded, explicit subset of D01–D30 is required.")
    os.umask(0o077)
    import server
    from plagiarism import web_search
    from plagiarism.jobs import Jobs
    from plagiarism.search_tavily import TavilySearch
    from plagiarism.storage import Store
    from plagiarism.saas import report_identifier
    from plagiarism.web_fetch import Fetcher

    dev = ROOT / "data/private/web-evaluation/materials/dev-30"
    key = dev / "key.json"
    cases = json.loads(key.read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only frozen DEV-30 is supported.")
    selected = [next(c for c in cases if c["id"] == cid) for cid in args.ids]
    texts = {c["id"]: (dev / c["input_path"]).read_text() for c in selected}
    if any(sha256(texts[c["id"]].encode()).hexdigest() != c["text_sha256"] for c in selected):
        raise ValueError("DEV input hash changed.")
    planned = {cid: len(web_search.plan_queries(text, strategy=args.strategy)) for cid, text in texts.items()}
    if sum(planned.values()) > args.max_new_queries:
        raise ValueError("Planned queries exceed the explicit probe cap.")
    out = ROOT / "data/private/web-evaluation" / args.run_name
    out.mkdir(mode=0o700, exist_ok=True)
    manifest = out / "run.json"
    configuration = {"case_ids": args.ids, "query_strategy": args.strategy, "url_strategy": args.url_strategy,
                     "input_key_sha256": sha256(key.read_bytes()).hexdigest(), "planned_queries": planned}
    if manifest.exists():
        run = json.loads(manifest.read_text())
        if any(run.get(k) != v for k, v in configuration.items()):
            raise ValueError("A saved run cannot change its inputs or strategy.")
    else:
        run = {**configuration, "id": sha256(args.run_name.encode()).hexdigest()[:32],
               "created_at": datetime.now(timezone.utc).isoformat(),
               "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
        dump(manifest, run)
    unsent = [cid for cid in args.ids if not (out / (cid + ".json")).exists()
              and not (out / (cid + "-request.json")).exists()]
    if unsent:
        dump(out / "quota-before.json", quota(sum(planned[cid] for cid in unsent)))

    real = TavilySearch(web_search.SearchBudget())
    class RecordingSearch:
        provider = "tavily"
        def search(self, query, request_id):
            path = out / (request_id + "-search.json")
            record = {"request_id": request_id, "query": query, "state": "submitted"}
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
    real_pages = Fetcher()
    class RecordingPages:
        def __init__(self):
            self.count = 0
        def fetch(self, url):
            self.count += 1
            path = out / (f"page-{self.count:04d}-" + sha256(url.encode()).hexdigest()[:16] + "-fetch.json")
            record = {"requested_url": url, "state": "submitted"}
            dump(path, record)
            try:
                fetched = real_pages.fetch(url)
                record.update(state="complete", fetched=fetched)
                dump(path, record)
                return fetched
            except Exception as error:
                record.update(state="failed_or_unknown", code=getattr(error, "code", type(error).__name__))
                dump(path, record)
                raise
    original_run = web_search.run
    web_search.run = partial(original_run, searcher=RecordingSearch(), fetcher=RecordingPages(),
                             query_strategy=args.strategy, url_strategy=args.url_strategy)
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
                raise RuntimeError("Queue HTTP " + str(response.status))
            return result
        finally:
            connection.close()
    try:
        for case in selected:
            cid = case["id"]
            path, request_path = out / (cid + ".json"), out / (cid + "-request.json")
            if path.exists():
                print(cid, "already saved", flush=True)
                continue
            request_id = sha256((run["id"] + cid + case["text_sha256"]).encode()).hexdigest()[:32]
            # GET first when a submission might already exist. An unknown
            # outcome is retained, never silently turned into a new POST.
            if request_path.exists():
                request_id = saved_request_identifier(request_path, request_id, case["text_sha256"], planned[cid])
                job = http("/api/jobs/" + request_id)["job"]
            else:
                dump(request_path, {"request_id": request_id, "text_sha256": case["text_sha256"],
                                    "planned_queries": planned[cid]})
                job = http("/api/web-check-jobs", {"request_id": request_id, "text": texts[cid],
                                                  "internet_consent": True})["job"]
            started = time.monotonic()
            while job["status"] not in ("complete", "failed", "cancelled"):
                if time.monotonic() - started > 420:
                    raise RuntimeError("Queue timeout; saved request retained for GET recovery.")
                time.sleep(0.5)
                job = http("/api/jobs/" + request_id)["job"]
            report = service.store.get("report", report_identifier(request_id))
            if job["status"] != "complete" and report is None:
                dump(out / (cid + "-error.json"), job)
                print(cid, "terminal failure", job.get("code"), flush=True)
                continue
            if report is None:
                raise ValueError("Completed queue job has no persisted report; no new POST is allowed.")
            if report["text"] != texts[cid] or report["web"]["query_strategy"] != args.strategy:
                raise ValueError("Saved report does not match the probe.")
            if report["web"]["queries_attempted"] > planned[cid] or report["web"]["urls_attempted"] > 12:
                raise ValueError("Probe exceeded its bounds.")
            dump(path, report)
            if job["status"] != "complete":
                dump(out / (cid + "-recovery.json"), {"request_id": request_id, "report_id": report["id"],
                     "job_status": job["status"], "job_code": job.get("code"),
                     "basis": "Report persisted before the interrupted job outcome; recovered without resubmission."})
            print(cid, "queries", report["web"]["queries_attempted"], "pages", report["web"]["sources_checked"],
                  "exact_words", report["stats"]["matched_words"], "report", report["id"], flush=True)
    finally:
        service.shutdown()
        service.server_close()
        service.jobs.close()
        web_search.run = original_run
    try:
        dump(out / "quota-after.json", quota(0))
    except Exception as error:
        dump(out / "quota-after.json", {"unavailable": type(error).__name__, "new_searches_allowed": False})


if __name__ == "__main__":
    main()
