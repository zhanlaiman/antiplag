#!/usr/bin/env python3
"""Frozen, bounded first use of two preregistered RFC sources.

Default mode reads public plan metadata only. --reveal is a deliberate offline
gate after the parent freezes the complete tested variant. --live reads only
the resulting private run, submits through the normal HTTP queue, and retains
main-Store reports. --score-only never opens the reservations, plans, or network.
No DEV or sealed-final materials are opened by this tool.
"""
import argparse
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Thread
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_dev_query_probe import dump, quota, saved_request_identifier
from scripts.compare_web_queries_offline import conditional_highlight, probe_evidence
from plagiarism import web_search as web
from plagiarism.matching import MATCHER_VERSION, _tokens, analyze
from plagiarism.web_evaluation import _report, _url
from plagiarism.web_fetch import HTML_EXTRACTION_VERSION

PLAN = ROOT / "docs/baselines/query-first-use-v2-plan-2026-11-12.json"
PLAN_SHA = "b6e6a86cbb4c9e166b624115070aaa080f87b527acb9e9e93ed889bd17141bd4"
AMENDMENT = ROOT / "docs/baselines/query-first-use-v2-budget-amendment-2026-11-12.json"
AMENDMENT_SHA = "ead96eae5382de36740761431c131a21dbff46ec2ef10a58f42e524c80ed03c4"
CHOICE = ROOT / "docs/baselines/query-first-use-v2-choice-2026-11-12.json"
OUT = ROOT / "data/private/web-evaluation/query-unfamiliar-first-use-v2"
IDS = tuple(f"Q2-{i:02d}" for i in range(1, 9))
MAX_QUERIES = 48
MAX_PER_DOCUMENT = 6


def now():
    return datetime.now(timezone.utc).isoformat()


def checked_json(path, expected):
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != expected:
        raise ValueError("Preregistered metadata hash changed")
    return json.loads(raw)


def metadata():
    plan = checked_json(PLAN, PLAN_SHA)
    amendment = checked_json(AMENDMENT, AMENDMENT_SHA)
    if (amendment["original_plan_sha256"] != PLAN_SHA
            or amendment["maximum_total_fresh_queries"] != MAX_QUERIES
            or amendment["maximum_queries_per_document"] != MAX_PER_DOCUMENT
            or amendment["documents"] != 8
            or [a["id"] for a in plan["assignments"]] != list(IDS[:6])
            or sorted(plan["authored_controls"]) != list(IDS[6:])
            or [s["id"] for s in plan["sources"]] != ["RFC9309", "RFC1149"]):
        raise ValueError("Only the preregistered two-source, eight-document experiment is allowed")
    return plan


def code_hashes():
    paths = [ROOT / "server.py", *sorted((ROOT / "plagiarism").glob("*.py")), Path(__file__),
             ROOT / "scripts/run_dev_query_probe.py", ROOT / "scripts/compare_web_queries_offline.py"]
    return {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in paths}


def zero_fallback(choice):
    value = choice.get("zero_fallback")
    if type(value) is not bool:
        raise ValueError("Frozen choice requires an explicit boolean zero_fallback")
    return value


def execution_options(choice):
    return {"query_strategy": choice["query_strategy"], "url_strategy": choice["url_strategy"],
            "use_named_references": choice["named_references"], "use_zero_fallback": zero_fallback(choice)}


def validate_report_variant(report, choice):
    enabled = zero_fallback(choice)
    meta = report.get("web")
    if (not isinstance(meta, dict) or any(meta.get(key) != choice[value] for key, value in (
            ("query_strategy", "query_strategy"), ("url_strategy", "url_strategy"),
            ("matcher_version", "matcher_version"), ("named_references_enabled", "named_references")))
            or meta.get("zero_fallback_enabled") is not enabled
            or type(meta.get("urls_attempted")) is not int
            or not 0 <= meta["urls_attempted"] <= (24 if enabled else 12)):
        raise ValueError("Saved report differs from frozen variant, zero-fallback flag or physical URL bound")


def check_freeze(expected_head):
    """Must complete before any source file or source-bearing response is read."""
    if not isinstance(expected_head, str) or not re.fullmatch(r"[a-f0-9]{40}", expected_head):
        raise ValueError("An explicit tested 40-character Git commit is required")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if head != expected_head:
        raise ValueError("Current HEAD differs from the tested frozen commit")
    choice_raw = CHOICE.read_bytes()
    choice = json.loads(choice_raw)
    required = {"frozen": True, "git_head": expected_head, "plan_sha256": PLAN_SHA,
                "amendment_sha256": AMENDMENT_SHA, "matcher_version": MATCHER_VERSION,
                "html_extraction_version": HTML_EXTRACTION_VERSION}
    if any(choice.get(k) != v for k, v in required.items()):
        raise ValueError("Complete variant has not been explicitly frozen")
    zero_fallback(choice)
    if (choice.get("query_strategy") not in {web.CITATION_STRATEGY, web.DENSE_STRATEGY}
            or choice.get("url_strategy") not in {web.DOMAIN_URL_STRATEGY, web.BALANCED_URL_STRATEGY, web.PATH_URL_STRATEGY}
            or type(choice.get("named_references")) is not bool
            or choice.get("pdf_alternative_engine") not in {None, "pdfium"}
            or not isinstance(choice.get("frozen_at"), str)):
        raise ValueError("Frozen execution options are incomplete")
    source_paths = list(code_hashes())
    subprocess.run(["git", "ls-files", "--error-unmatch", "--", *source_paths],
                   cwd=ROOT, check=True, capture_output=True)
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", *source_paths],
                   cwd=ROOT, check=True, capture_output=True)
    return {"choice": choice, "choice_sha256": sha256(choice_raw).hexdigest(), "code_sha256": code_hashes()}


def checked_source(spec):
    """Called only after check_freeze, and never by live/scoring modes."""
    attempt_path = (ROOT / spec["attempt_artifact"]).resolve()
    allowed = {ROOT / "data/private/web-evaluation/query-unfamiliar-reserved-v2/RFC9309-attempt.json",
               ROOT / "data/private/web-evaluation/query-unfamiliar-reserved-v2-extra/RFC1149-attempt.json"}
    if attempt_path not in {p.resolve() for p in allowed}:
        raise ValueError("Source attempt leaves the two fixed reservations")
    attempt = json.loads(attempt_path.read_text())
    if (attempt.get("state") != "complete" or attempt.get("text_sha256") != spec["text_sha256"]
            or attempt.get("requested_url") != spec["requested_url"]
            or attempt.get("final_url") != spec["final_url"]):
        raise ValueError("Reserved source metadata changed")
    text_path = (attempt_path.parent / attempt["text_path"]).resolve()
    if text_path.parent != attempt_path.parent or text_path.name != spec["id"] + ".txt":
        raise ValueError("Reserved source path is not fixed")
    raw = text_path.read_bytes()
    if sha256(raw).hexdigest() != spec["text_sha256"]:
        raise ValueError("Reserved source text hash changed")
    text = raw.decode("utf-8")
    if len(text) != spec["text_characters"]:
        raise ValueError("Reserved source length changed")
    return {"id": spec["id"], "url": spec["final_url"], "title": spec["id"], "text": text}


def construct_cases(plan, pages):
    cases, texts = [], {}
    for assignment in plan["assignments"]:
        source = pages[assignment["source_id"]]
        base = plan["authored_bases"][assignment["base_id"]]
        words, base_words = _tokens(source["text"]), _tokens(base)
        numerator, denominator = assignment["source_start_fraction"]
        first = len(words) * numerator // denominator
        length, index = assignment["insertion_words"], assignment["target_base_word_index"]
        if len(base_words) != 35 or first + length > len(words) or index not in {0, 17, 35}:
            raise ValueError("Preregistered construction failed; no replacement span is allowed")
        source_span = [words[first].start, words[first + length - 1].end]
        original = source["text"][slice(*source_span)]
        inserted = " ".join(original.split())
        split = len(base) if index == len(base_words) else base_words[index].start
        prefix, suffix = base[:split].strip(), base[split:].strip()
        target = " ".join(part for part in (prefix, inserted, suffix) if part)
        start = len(prefix) + bool(prefix)
        evidence = {"source_id": source["id"], "source_urls": [source["url"]], "source_span": source_span,
                    "source_quote": original, "target_quote": inserted, "target_span": [start, start + len(inserted)]}
        if len(_tokens(inserted)) != length or len(_tokens(target)) != 35 + length:
            raise ValueError("Fixed insertion token count changed; no reselection is allowed")
        texts[assignment["id"]] = target
        cases.append({"id": assignment["id"], "synthetic": True, "control": False,
                      "text_sha256": sha256(target.encode()).hexdigest(), "input_words": len(_tokens(target)),
                      "expected_evidence": [evidence]})
    for cid, text in plan["authored_controls"].items():
        if len(_tokens(text)) != 35:
            raise ValueError("Authored control is not the preregistered 35 words")
        texts[cid] = text
        cases.append({"id": cid, "synthetic": True, "control": True, "text_sha256": sha256(text.encode()).hexdigest(),
                      "input_words": 35, "expected_evidence": []})
    if tuple(c["id"] for c in cases) != IDS:
        raise ValueError("Fixed document identities changed")
    return cases, texts


def reveal(expected_head):
    plan = metadata()
    configuration = check_freeze(expected_head)
    OUT.mkdir(mode=0o700, exist_ok=True)
    OUT.chmod(0o700)
    # An interrupted first reveal is evidence, not permission to generate again.
    if (OUT / "reveal-started.json").exists():
        run = load_run()
        if run["configuration"] != configuration:
            raise ValueError("First-use configuration changed")
        return run, json.loads((OUT / "local-first-use.json").read_text())
    dump(OUT / "reveal-started.json", {"started_at": now(), "configuration": configuration,
                                        "plan_sha256": PLAN_SHA, "amendment_sha256": AMENDMENT_SHA})
    pages = {s["id"]: checked_source(s) for s in plan["sources"]}
    cases, texts = construct_cases(plan, pages)
    # All blind plans are built before gold is consulted by a scoring function.
    plans = {cid: {"citation_v2": web.plan_queries(text, strategy=web.CITATION_STRATEGY),
                   "selected": web.plan_queries(text, strategy=configuration["choice"]["query_strategy"])}
             for cid, text in texts.items()}
    if (any(len(p["selected"]) > MAX_PER_DOCUMENT for p in plans.values())
            or sum(len(p["selected"]) for p in plans.values()) > MAX_QUERIES):
        raise ValueError("Fixed blind plans exceed the amended cap; no trimming is allowed")
    manifest = {"schema_version": 1, "cases": cases, "input_texts": texts,
                "source_sha256": {s["id"]: s["text_sha256"] for s in plan["sources"]},
                "plan_sha256": PLAN_SHA, "amendment_sha256": AMENDMENT_SHA}
    rows = []
    for case in cases:
        cid, text = case["id"], texts[case["id"]]
        result = analyze(text, list(pages.values()))
        evidence = []
        for gold in case["expected_evidence"]:
            wanted = [t for t in _tokens(text) if gold["target_span"][0] <= t.start and t.end <= gold["target_span"][1]]
            coverage = lambda strict: sum(any(m["kind"] == "exact" and m["start"] <= t.start and t.end <= m["end"]
                                       and (not strict or m["source_id"] == gold["source_id"])
                                       for m in result["matches"]) for t in wanted)
            evidence.append({"plans": {name: probe_evidence(p, _tokens(text), gold,
                              [t.value for t in _tokens(gold["source_quote"])]) for name, p in plans[cid].items()},
                "conditional_highlight": conditional_highlight(result["matches"], _tokens(text),
                                             _tokens(pages[gold["source_id"]]["text"]), gold),
                "expected_words": len(wanted), "any_source_words": coverage(False),
                "expected_source_words": coverage(True)})
        rows.append({"id": cid, "control": case["control"], "plans": plans[cid], "evidence": evidence,
                     "known_page_exact_pairs": sum(m["kind"] == "exact" for m in result["matches"]),
                     "known_page_similar_pairs": sum(m["kind"] == "similar" for m in result["matches"]),
                     "known_page_pairs_for_review": [{key: m.get(key) for key in (
                         "kind", "source_id", "source_url", "start", "end", "source_start", "source_end",
                         "excerpt", "source_excerpt", "quoted")} for m in result["matches"]]})
    evidence = [e for r in rows for e in r["evidence"]]
    summary = {"documents": 8, "artificial_insertions": 6, "independent_controls": 2,
               "whole_phrase_in_insertion_and_source": {name: sum(e["plans"][name]["whole_query_in_both_spans"] for e in evidence)
                                                       for name in ("citation_v2", "selected")},
               "planned_queries": {name: sum(len(r["plans"][name]) for r in rows) for name in ("citation_v2", "selected")},
               "known_page_full_fragments_any_source": sum(e["any_source_words"] == e["expected_words"] for e in evidence),
               "known_page_full_fragments_expected_source": sum(e["expected_source_words"] == e["expected_words"] for e in evidence),
               "known_page_copied_words_any_source": sum(e["any_source_words"] for e in evidence),
               "known_page_copied_words_expected_source": sum(e["expected_source_words"] for e in evidence),
               "known_page_copied_words_strict_gold_offset": sum(e["conditional_highlight"]["covered_words"] for e in evidence),
               "expected_copied_words": 140,
               "control_exact_pairs": sum(r["known_page_exact_pairs"] for r in rows if r["control"]),
               "control_similar_pairs": sum(r["known_page_similar_pairs"] for r in rows if r["control"])}
    local = {"schema_version": 1, "first_use": True, "used_for_tuning": False, "live_search_queries": 0,
             "summary": summary, "cases": rows}
    dump(OUT / "manifest.json", manifest)
    dump(OUT / "local-first-use.json", local)
    run = {"schema_version": 1, "id": sha256((PLAN_SHA + configuration["choice_sha256"]).encode()).hexdigest()[:32],
           "configuration": configuration, "manifest_sha256": sha256((OUT / "manifest.json").read_bytes()).hexdigest(),
           "local_sha256": sha256((OUT / "local-first-use.json").read_bytes()).hexdigest(),
           "revealed_at": now(), "first_use": True, "used_for_tuning": False, "final_materials_read": False}
    dump(OUT / "run.json", run)
    return run, local


def load_run():
    run = json.loads((OUT / "run.json").read_text())
    zero_fallback(run["configuration"]["choice"])
    manifest = checked_json(OUT / "manifest.json", run["manifest_sha256"])
    local = checked_json(OUT / "local-first-use.json", run["local_sha256"])
    if (tuple(c["id"] for c in manifest["cases"]) != IDS
            or tuple(r["id"] for r in local["cases"]) != IDS
            or set(manifest["input_texts"]) != set(IDS)
            or any(sha256(manifest["input_texts"][c["id"]].encode()).hexdigest() != c["text_sha256"]
                   for c in manifest["cases"])
            or any(len(r["plans"]["selected"]) > MAX_PER_DOCUMENT for r in local["cases"])
            or sum(len(r["plans"]["selected"]) for r in local["cases"]) > MAX_QUERIES):
        raise ValueError("Saved first-use inputs or query bounds changed")
    if code_hashes() != run["configuration"]["code_sha256"]:
        raise ValueError("Code changed after first use; no replan, live run, or new scoring is allowed")
    return run


def request_id(run, case):
    return sha256((run["id"] + case["id"] + case["text_sha256"]).encode()).hexdigest()[:32]


def frozen_calls(run, cases, plans):
    """The same saved identity map is used for live calls and offline accounting."""
    if (not isinstance(run.get("id"), str) or not re.fullmatch(r"[a-f0-9]{32}", run["id"])
            or tuple(c["id"] for c in cases) != IDS or set(plans) != set(IDS)
            or any(not isinstance(p, list) or len(p) > MAX_PER_DOCUMENT for p in plans.values())):
        raise ValueError("Frozen provider-call scope changed")
    allowed = {}
    for case in cases:
        if not isinstance(case.get("text_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", case["text_sha256"]):
            raise ValueError("Frozen provider input identity changed")
        rid = request_id(run, case)
        for index, item in enumerate(plans[case["id"]]):
            if not isinstance(item.get("query"), str) or not 1 <= len(item["query"]) <= 400:
                raise ValueError("Frozen provider query changed")
            allowed[sha256((rid + "\0" + str(index)).encode()).hexdigest()] = {
                "case_id": case["id"], "query_index": index, "query": item["query"]}
    if len(allowed) > MAX_QUERIES:
        raise ValueError("Frozen plans exceed the eight-document bounds")
    return allowed


def account_calls(out, run, cases, plans, report_rows):
    """Keep raw marker counts; reports can establish missing/unknown accounting."""
    allowed = frozen_calls(run, cases, plans)
    paths = sorted(out.glob("*-search.json"))
    if len(paths) > MAX_QUERIES:
        raise ValueError("Saved provider-call markers exceed the frozen cap")
    records, actual, known = [], {cid: set() for cid in IDS}, []
    for path in paths:
        filename = re.fullmatch(r"([a-f0-9]{64})-search\.json", path.name)
        if not filename or filename[1] not in allowed or path.is_symlink():
            raise ValueError("Unbound saved provider-call marker")
        identifier, identity = filename[1], allowed[filename[1]]
        record = json.loads(path.read_text())
        if (not isinstance(record, dict) or record.get("request_id") != identifier
                or any(record.get(k) != v for k, v in identity.items())
                or type(record.get("query_index")) is not int
                or record.get("search_depth") != "basic"
                or record.get("state") not in {"submitted", "complete", "failed_or_unknown"}):
            raise ValueError("Saved provider-call identity differs from the frozen plan")
        usage = record.get("usage")
        if usage is not None:
            if not isinstance(usage, dict) or type(usage.get("credits")) is not int or not 0 <= usage["credits"] <= 1:
                raise ValueError("Invalid explicit Basic-search credit usage")
            known.append(usage["credits"])
        records.append(record)
        actual[identity["case_id"]].add(identifier)
    reported = {r["id"]: r["queries_attempted"] for r in report_rows if r["status"] == "report"}
    by_case, issues, missing_total, minimum_attempts = [], [], 0, 0
    for cid in IDS:
        claimed = reported.get(cid)
        if claimed is not None and (type(claimed) is not int or not 0 <= claimed <= len(plans[cid])):
            raise ValueError("Report query count exceeds its frozen document plan")
        observed_indexes = [allowed[identifier]["query_index"] for identifier in actual[cid]]
        minimum = max(claimed or 0, max(observed_indexes, default=-1) + 1)
        prefix = {identifier for identifier, identity in allowed.items()
                  if identity["case_id"] == cid and identity["query_index"] < minimum}
        missing = prefix - actual[cid]
        extra = {identifier for identifier in actual[cid]
                 if claimed is not None and allowed[identifier]["query_index"] >= claimed}
        consistent = not missing and not extra
        item = {"id": cid, "report_queries_attempted": claimed, "actual_provider_markers": len(actual[cid]),
                "minimum_attempts_consistent_with_reports_and_ids": minimum,
                "missing_marker_call_ids": sorted(missing), "markers_beyond_reported_attempts": sorted(extra),
                "accounting_complete": consistent}
        by_case.append(item)
        if not consistent:
            issues.append({"case_id": cid, "code": "provider_call_accounting_incomplete"})
        missing_total += len(missing)
        minimum_attempts += minimum
    unknown_record_usage = len(records) - len(known)
    complete = not issues
    usage_complete = complete and unknown_record_usage == 0
    summary = {"provider_attempts": len(records), "provider_complete": sum(q["state"] == "complete" for q in records),
        "queries_in_completed_reports": sum(reported.values()),
        "minimum_provider_attempts_consistent_with_reports_and_ids": minimum_attempts,
        "provider_marker_records_missing": missing_total, "provider_accounting_complete": complete,
        "known_basic_credits": sum(known), "provider_records_with_credit_usage": len(known),
        "provider_records_without_credit_usage": unknown_record_usage,
        "provider_usage_unknown_calls": unknown_record_usage + missing_total,
        "provider_credit_usage_complete": usage_complete,
        "total_basic_credits": sum(known) if usage_complete else None,
        "credit_accounting_note": "known_basic_credits is an explicit-usage subtotal. Missing markers or usage are unknown, not evidence of zero total spend."}
    return {"summary": summary, "cases": by_case, "issues": issues}


class RecordedSearch:
    provider = "tavily"

    def __init__(self, real, out, allowed, *, max_queries=MAX_QUERIES, max_per_document=MAX_PER_DOCUMENT):
        if (type(max_queries) is not int or not 1 <= max_queries <= MAX_QUERIES
                or type(max_per_document) is not int or not 1 <= max_per_document <= MAX_PER_DOCUMENT):
            raise ValueError("Explicit provider caps must stay within the frozen bounds")
        self.real, self.out, self.allowed = real, out, allowed
        self.max_queries, self.max_per_document = max_queries, max_per_document

    def search(self, query, call_id):
        expected = self.allowed.get(call_id)
        if expected is None or query != expected["query"]:
            raise RuntimeError("Provider call differs from a frozen document plan")
        path = self.out / (call_id + "-search.json")
        if path.exists():
            raise RuntimeError("A submitted provider call is never automatically repeated")
        records = [json.loads(p.read_text()) for p in self.out.glob("*-search.json")]
        if len(records) >= self.max_queries or sum(r["case_id"] == expected["case_id"] for r in records) >= self.max_per_document:
            raise RuntimeError("Frozen provider-call cap exhausted")
        record = {"request_id": call_id, **expected, "search_depth": "basic", "state": "submitted", "submitted_at": now()}
        dump(path, record)
        try:
            results = self.real.search(query, call_id)
            record.update(state="complete", completed_at=now(), results=results, usage=self.real.last_usage)
            dump(path, record)
            return results
        except Exception as error:
            record.update(state="failed_or_unknown", completed_at=now(), code=getattr(error, "code", type(error).__name__))
            dump(path, record)
            raise


def recover_or_submit(http, request_path, rid, case, text, planned, preflight):
    """GET is always first; a saved/uncertain POST can never become a new POST."""
    saved = request_path.exists()
    if saved:
        saved_request_identifier(request_path, rid, case["text_sha256"], planned)
    status, result = http("/api/jobs/" + rid, missing_ok=True)
    if status == 200:
        if not saved:
            dump(request_path, {"request_id": rid, "text_sha256": case["text_sha256"], "planned_queries": planned,
                                "recovered_existing_before_post": True})
        return result["job"]
    if saved:
        raise RuntimeError("Saved request is unknown to Store; preserve identity and do not POST again")
    preflight()
    dump(request_path, {"request_id": rid, "text_sha256": case["text_sha256"], "planned_queries": planned,
                        "saved_before_post_at": now()})
    # Only the blind target text enters the backend; labels/URLs/spans stay here.
    return http("/api/web-check-jobs", {"request_id": rid, "text": text, "internet_consent": True})[1]["job"]


def live(expected_head):
    run = load_run()
    if check_freeze(expected_head) != run["configuration"]:
        raise ValueError("Live execution configuration differs from first use")
    manifest = checked_json(OUT / "manifest.json", run["manifest_sha256"])
    local = checked_json(OUT / "local-first-use.json", run["local_sha256"])
    choice = run["configuration"]["choice"]
    plans = {r["id"]: r["plans"]["selected"] for r in local["cases"]}
    if tuple(c["id"] for c in manifest["cases"]) != IDS:
        raise ValueError("Live run identities are not the fixed eight cases")
    import server
    from plagiarism.jobs import Jobs
    from plagiarism.storage import Store
    from plagiarism.search_tavily import TavilySearch
    from plagiarism.saas import report_identifier
    from plagiarism.web_fetch import Fetcher
    allowed = frozen_calls(run, manifest["cases"], plans)
    class Pages:
        def __init__(self):
            self.real, self.case_id = Fetcher(), None
            self.count = len(list(OUT.glob("page-*-fetch.json")))
        def fetch(self, url):
            self.count += 1
            path = OUT / (f"page-{self.count:04d}-" + sha256(url.encode()).hexdigest()[:16] + "-fetch.json")
            if path.exists():
                raise RuntimeError("A saved page capture cannot be overwritten")
            record = {"case_id": self.case_id, "requested_url": url, "state": "submitted", "submitted_at": now()}
            dump(path, record)
            try:
                fetched = self.real.fetch(url)
                record.update(state="complete", completed_at=now(), fetched=fetched)
                dump(path, record)
                return fetched
            except Exception as error:
                record.update(state="failed_or_unknown", completed_at=now(), code=getattr(error, "code", type(error).__name__))
                dump(path, record)
                raise
    pages = Pages()
    original = web.run
    old_engine = os.environ.get("ISTOK_PDF_ALTERNATIVE_ENGINE")
    if choice["pdf_alternative_engine"] is None:
        os.environ.pop("ISTOK_PDF_ALTERNATIVE_ENGINE", None)
    else:
        os.environ["ISTOK_PDF_ALTERNATIVE_ENGINE"] = choice["pdf_alternative_engine"]
    web.run = partial(original, searcher=RecordedSearch(TavilySearch(web.SearchBudget()), OUT, allowed), fetcher=pages,
                      **execution_options(choice))
    service = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    service.saas, service.store, service.jobs = None, Store(), Jobs()
    Thread(target=service.serve_forever, daemon=True).start()
    def http(path, body=None, missing_ok=False):
        connection = HTTPConnection("127.0.0.1", service.server_port, timeout=20)
        try:
            connection.request("POST" if body is not None else "GET", path,
                               json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.load(response)
            if response.status != 200 and not (missing_ok and response.status == 404):
                raise RuntimeError("Queue HTTP " + str(response.status) + "; never retry an uncertain POST")
            return response.status, result
        finally:
            connection.close()
    try:
        for case in manifest["cases"]:
            cid, rid = case["id"], request_id(run, case)
            path = OUT / (cid + ".json")
            if path.exists() or (OUT / (cid + "-error.json")).exists():
                print(cid, "already terminal", flush=True)
                continue
            pages.case_id = cid
            remaining = sum(len(plans[c["id"]]) for c in manifest["cases"]
                            if not (OUT / (c["id"] + ".json")).exists()
                            and not (OUT / (c["id"] + "-request.json")).exists()
                            and not (OUT / (c["id"] + "-error.json")).exists())
            def preflight():
                dump(OUT / (cid + "-quota-before.json"), quota(remaining))
            text = manifest["input_texts"][cid]
            job = recover_or_submit(http, OUT / (cid + "-request.json"), rid, case, text, len(plans[cid]), preflight)
            started = time.monotonic()
            while job["status"] not in {"complete", "failed", "cancelled"}:
                if time.monotonic() - started > 420:
                    raise RuntimeError("Queue timeout; saved request retained for GET recovery")
                time.sleep(.5)
                job = http("/api/jobs/" + rid)[1]["job"]
            report = service.store.get("report", report_identifier(rid))
            if report is None:
                dump(OUT / (cid + "-error.json"), job)
                print(cid, "terminal failure", flush=True)
                continue
            validate_report_variant(report, choice)
            if report["text"] != text or report["web"]["queries_attempted"] > len(plans[cid]):
                raise ValueError("Saved report differs from frozen execution bounds")
            dump(path, report)
            print(cid, "queries", report["web"]["queries_attempted"], "pages", report["web"]["sources_checked"],
                  "matched_words", report["stats"]["matched_words"], "report", report["id"], flush=True)
    finally:
        service.shutdown(); service.server_close(); service.jobs.close()
        web.run = original
        if old_engine is None:
            os.environ.pop("ISTOK_PDF_ALTERNATIVE_ENGINE", None)
        else:
            os.environ["ISTOK_PDF_ALTERNATIVE_ENGINE"] = old_engine
    try:
        dump(OUT / "quota-after.json", quota(0))
    except Exception as error:
        dump(OUT / "quota-after.json", {"unavailable": type(error).__name__, "new_searches_allowed": False})


def score_only():
    """Own frozen snapshots only: no reservation, new plan, Store, or network."""
    run = load_run()
    manifest = checked_json(OUT / "manifest.json", run["manifest_sha256"])
    local = checked_json(OUT / "local-first-use.json", run["local_sha256"])
    plans = {r["id"]: r["plans"]["selected"] for r in local["cases"]}
    choice, rows, pairs = run["configuration"]["choice"], [], []
    for case in manifest["cases"]:
        cid, path = case["id"], OUT / (case["id"] + ".json")
        if not path.exists():
            error = OUT / (cid + "-error.json")
            if not error.exists():
                raise ValueError("First-use run is incomplete; no fresh score may be finalized")
            rows.append({"id": cid, "status": "failed", "reason": json.loads(error.read_text())})
            continue
        report = json.loads(path.read_text())
        validate_report_variant(report, choice)
        checked = _report(report, case)
        evidence = []
        for gold in case["expected_evidence"]:
            wanted = [t for t in _tokens(report["text"]) if gold["target_span"][0] <= t.start and t.end <= gold["target_span"][1]]
            urls = {_url(u) for u in gold["source_urls"]}
            coverage = lambda strict: sum(any(m["target_span"][0] <= t.start and t.end <= m["target_span"][1]
                                      and (not strict or m["url"] in urls) for m in checked["matches"]) for t in wanted)
            evidence.append({"expected_words": len(wanted), "any_source_words": coverage(False),
                "expected_source_words": coverage(True), "expected_url_discovered": bool(urls & (checked["discovered_urls"] or set())),
                "expected_url_read": bool(urls & {_url(s["url"]) for s in report["sources"]})})
        invalid_indices = {m["match_index"] for m in checked["invalid_highlights"]}
        for index, match in enumerate(report["matches"]):
            if case["control"] or match.get("kind") == "similar" or not any(
                    gold["target_span"][0] <= match.get("start", -1) and match.get("end", -1) <= gold["target_span"][1]
                    for gold in case["expected_evidence"]):
                pairs.append({"case_id": cid, "report_id": report["id"], "match_index": index,
                    "control": case["control"], "kind": match.get("kind"), "source_url": match.get("source_url"),
                    "target_excerpt": match.get("excerpt"), "source_excerpt": match.get("source_excerpt"),
                    "target_span": [match.get("start"), match.get("end")],
                    "source_span": [match.get("source_start"), match.get("source_end")],
                    "grounded": index not in invalid_indices, "review": "needs_pair_review"})
        rows.append({"id": cid, "status": "report", "report_id": report["id"],
            "report_sha256": sha256(path.read_bytes()).hexdigest(), "control": case["control"],
            "queries_attempted": report["web"]["queries_attempted"], "pages_read": report["web"]["sources_checked"],
            "coverage": report["web"]["status"], "uniqueness_percent": report["stats"]["uniqueness_percent"],
            "exact_pairs": len(checked["matches"]), "similar_pairs": sum(m["kind"] == "similar" for m in report["matches"]),
            "invalid_highlights": checked["invalid_highlights"], "evidence": evidence})
    evidence = [e for r in rows for e in r.get("evidence", [])]
    accounting = account_calls(OUT, run, manifest["cases"], plans, rows)
    summary = {"documents": 8, "reports": sum(r["status"] == "report" for r in rows), "expected_fragments": 6,
        "expected_copied_words": 140, "expected_url_discovered": sum(e["expected_url_discovered"] for e in evidence),
        "expected_url_read": sum(e["expected_url_read"] for e in evidence),
        "full_fragments_any_source": sum(e["any_source_words"] == e["expected_words"] for e in evidence),
        "full_fragments_expected_source": sum(e["expected_source_words"] == e["expected_words"] for e in evidence),
        "copied_words_any_source": sum(e["any_source_words"] for e in evidence),
        "copied_words_expected_source": sum(e["expected_source_words"] for e in evidence),
        "independent_controls": 2, "control_exact_pairs": sum(r.get("exact_pairs", 0) for r in rows if r.get("control")),
        "control_similar_pairs": sum(r.get("similar_pairs", 0) for r in rows if r.get("control")),
        "unreviewed_pairs": len(pairs), "confirmed_false_pairs": 0 if not pairs else None,
        "documents_without_read_pages": sum(r.get("pages_read") == 0 for r in rows), **accounting["summary"]}
    result = {"schema_version": 2, "first_use": True, "used_for_tuning": False, "fresh_baseline_not_run": True,
              "summary": summary, "cases": rows, "unexpected_pairs_for_review": pairs,
              "provider_accounting": {"cases": accounting["cases"], "issues": accounting["issues"]}}
    previous = OUT / "fresh-first-use.json"
    if previous.exists():
        old = {r["id"]: r.get("report_sha256") for r in json.loads(previous.read_text())["cases"]}
        if old != {r["id"]: r.get("report_sha256") for r in rows}:
            raise ValueError("A first-use report changed after initial scoring")
    dump(previous, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--reveal", action="store_true", help="Offline source reveal only after explicit parent freeze")
    modes.add_argument("--live", action="store_true", help="One fresh selected-variant run of the already revealed fixed cases")
    modes.add_argument("--score-only", action="store_true", help="Own saved run/reports only; no source/planner/network")
    parser.add_argument("--expected-git-head")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.score_only:
        print(json.dumps(score_only()["summary"], ensure_ascii=False, indent=2))
    elif args.reveal:
        _, local = reveal(args.expected_git_head)
        print(json.dumps(local["summary"], ensure_ascii=False, indent=2))
    elif args.live:
        live(args.expected_git_head)
        print(json.dumps(score_only()["summary"], ensure_ascii=False, indent=2))
    else:
        plan = metadata()
        print(json.dumps({"phase": "preregistered_unrevealed", "documents": plan["documents"],
            "sources": [s["id"] for s in plan["sources"]], "max_queries": MAX_QUERIES,
            "source_prose_opened": False, "search_queries": 0}, indent=2))


if __name__ == "__main__":
    main()
