#!/usr/bin/env python3
"""Five-case publication first use. Default reads metadata, never EPA prose."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from functools import partial
from hashlib import sha256
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
from plagiarism import web_search as web
from plagiarism.matching import MATCHER_VERSION, _tokens, analyze
from plagiarism.saas import report_identifier
from plagiarism.web_evaluation import _report, _url
from plagiarism.web_fetch import HTML_EXTRACTION_VERSION
from scripts.compare_web_queries_offline import probe_evidence
from scripts.run_dev_metadata_pilot import private_dump, queue_http, QueueHTTPError, RecordingPages, page_path
from scripts.run_dev_query_probe import quota
from scripts.publication_first_use_accounting import reconcile
from scripts.run_unfamiliar_query_v2 import execution_options, RecordedSearch, recover_or_submit

RESERVED = ROOT / "data/private/web-evaluation/publication-first-use-reserved-v1"
PLAN = RESERVED / "sampling-plan.json"
AMENDMENT = RESERVED / "amendments/long-host-v1/amendment.json"
WIKI_AMENDMENT = RESERVED / "amendments/wiki-followup-budget-v1/amendment.json"
EPA_METADATA = RESERVED / "epa-start-saving-metadata.json"
EPA_ATTEMPT = RESERVED / "attempts/epa-start-saving.json"
CHOICE = ROOT / "docs/baselines/publication-first-use-v3-choice-2026-11-12.json"
OUT = ROOT / "data/private/web-evaluation/publication-first-use-v3"
PLAN_SHA = "364ed55dfae9f1aa767eb4ea982bda45f89098b354c8d2b10c63a9a5a8fb83d4"
AMENDMENT_SHA = "5f326226b1fccbd22c612e6b80e4e6b9ca325640d0ce926adc1b8013ae17d6e2"
WIKI_AMENDMENT_SHA = "0fabb75cfc42d214965ca44e987f2783623c8c0dd4a289e93324c2d3efa083fa"
DISCOVERY = "primary-mediawiki-post-v2-clause"
IDS = ("P3-04", "P3-05", "P3-06", "P3-07", "P3-08")
FAILED_IDS = ("P3-01", "P3-02", "P3-03")
MAX_QUERIES, MAX_PER_CASE = 30, 6
MAX_WIKI, MAX_WIKI_PER_CASE, MAX_PHYSICAL, MAX_PAGES = 10, 2, 40, 26
SUPPORT = ("scripts/publication_first_use_accounting.py", "scripts/run_publication_first_use.py", "scripts/run_unfamiliar_query_v2.py",
           "scripts/run_dev_metadata_pilot.py", "scripts/run_dev_query_probe.py", "scripts/compare_web_queries_offline.py")


def digest(value):
    return sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def checked_json(path, expected):
    if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Snapshot leaves its explicit workspace")
    raw = path.read_bytes()
    if digest(raw) != expected:
        raise ValueError("Frozen snapshot SHA256 changed")
    return json.loads(raw)


def metadata():
    plan, amendment = checked_json(PLAN, PLAN_SHA), checked_json(AMENDMENT, AMENDMENT_SHA)
    if (amendment.get("original_sampling_plan_sha256") != PLAN_SHA or amendment.get("sampler_unchanged") is not True
            or tuple(amendment.get("actual_future_case_ids", [])) != IDS
            or tuple(amendment.get("failed_cases_not_replaced", [])) != FAILED_IDS
            or amendment.get("future_maximum_total_queries") != MAX_QUERIES):
        raise ValueError("Publication amendment scope changed")
    epa = checked_json(EPA_METADATA, amendment["epa_sampler_metadata_sha256"])
    budget = checked_json(WIKI_AMENDMENT, WIKI_AMENDMENT_SHA)
    expected = {"actual_case_ids": list(IDS), "preparation_failed_case_ids_not_replaced": list(FAILED_IDS),
        "primary_tavily_basic_cap_total": MAX_QUERIES, "secondary_anonymous_mediawiki_cap_total": MAX_WIKI,
        "physical_provider_api_cap_total": MAX_PHYSICAL, "physical_page_attempt_cap_per_case": MAX_PAGES,
        "sampler_raw_spans_ranks_hosts_assignments_and_controls_unchanged": True}
    if any(budget.get(k) != v for k, v in expected.items()):
        raise ValueError("Separate provider-budget amendment changed")
    return {"plan": plan, "amendment": amendment, "epa": epa, "wiki_budget": budget}


def secondary_plans(text, primary):
    from plagiarism.web_wiki_followup import select_post_queries
    return select_post_queries(text, primary)


def code_hashes():
    paths = [ROOT / "server.py", *sorted((ROOT / "plagiarism").glob("*.py")), *(ROOT / name for name in SUPPORT)]
    return {path.relative_to(ROOT).as_posix(): digest(path.read_bytes()) for path in paths}


def check_freeze(expected_head, *, include_metadata=True):
    if not isinstance(expected_head, str) or not re.fullmatch(r"[a-f0-9]{40}", expected_head):
        raise ValueError("An explicit tested Git HEAD is required before reveal or live")
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() != expected_head:
        raise ValueError("Current HEAD differs from the tested frozen commit")
    raw = CHOICE.read_bytes()
    choice = json.loads(raw)
    required = {"frozen": True, "git_head": expected_head, "plan_sha256": PLAN_SHA, "amendment_sha256": AMENDMENT_SHA,
        "wiki_budget_amendment_sha256": WIKI_AMENDMENT_SHA, "actual_case_ids": list(IDS),
        "maximum_total_fresh_queries": MAX_PHYSICAL, "maximum_queries_per_document": 8,
        "maximum_fresh_tavily_queries": MAX_QUERIES, "maximum_fresh_mediawiki_queries": MAX_WIKI,
        "maximum_pages_per_document": MAX_PAGES, "wiki_followup": True, "matcher_version": MATCHER_VERSION,
        "html_extraction_version": HTML_EXTRACTION_VERSION, "discovery_strategy": DISCOVERY}
    if any(choice.get(key) != value for key, value in required.items()):
        raise ValueError("Complete publication variant has not been frozen")
    if (choice.get("query_strategy") not in (web.CITATION_STRATEGY, web.DENSE_STRATEGY)
            or choice.get("url_strategy") not in (web.DOMAIN_URL_STRATEGY, web.BALANCED_URL_STRATEGY, web.PATH_URL_STRATEGY)
            or type(choice.get("named_references")) is not bool or choice.get("pdf_alternative_engine") not in (None, "pdfium")
            or not isinstance(choice.get("frozen_at"), str) or not choice["frozen_at"]):
        raise ValueError("Frozen execution options are incomplete")
    execution_options(choice)  # Requires an explicit boolean zero_fallback.
    if type(choice.get("wiki_followup")) is not bool:
        raise ValueError("Complete choice requires an explicit Wiki-followup boolean")
    # Import/signature checks happen before the first source-body read.
    from inspect import signature
    if not {"use_wiki_followup", "wiki_searcher"} <= set(signature(web.run).parameters):
        raise ValueError("Frozen production Wiki interface is unavailable")
    secondary_plans("", [])
    hashes = code_hashes()
    if choice.get("code_sha256") != hashes:
        raise ValueError("Choice code SHA256 differs from the tested implementation")
    for command in (["git", "ls-files", "--error-unmatch", "--", *hashes], ["git", "diff", "--exit-code", "HEAD", "--", *hashes]):
        subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
    if include_metadata:
        metadata()
    return {"choice": choice, "choice_sha256": digest(raw),
        "choice_canonical_sha256": digest(json.dumps(choice, ensure_ascii=False, sort_keys=True, allow_nan=False)), "code_sha256": hashes}


def checked_source(meta):
    """First publication-prose open: caller must already hold a reveal intent."""
    amendment, epa = meta["amendment"], meta["epa"]
    record = checked_json(EPA_ATTEMPT, amendment["source_outcomes_sha256"]["epa-start-saving"])
    page = record.get("fetched", {})
    if (record.get("state") != "complete" or not isinstance(page.get("text"), str)
            or digest(page["text"]) != epa["text_sha256"] or page.get("text_sha256") != epa["text_sha256"]
            or page.get("url") != epa["final_url"]):
        raise ValueError("Reserved EPA body identity changed; no replacement is allowed")
    return {"id": "epa-start-saving", "url": page["url"], "title": page.get("title", "EPA"), "text": page["text"]}


def construct_cases(meta, source):
    amendment = meta["amendment"]
    hosts = {}
    for name, item in amendment["hosts"].items():
        path = (ROOT / item["path"]).resolve()
        if not path.is_relative_to(ROOT.resolve()) or digest(path.read_bytes()) != item["text_sha256"]:
            raise ValueError("Authored long host identity changed")
        hosts[name] = path.read_text()
        if len(_tokens(hosts[name])) != 180 or len(hosts[name].splitlines()) != 1:
            raise ValueError("Expected one immutable 180-word host paragraph")
    cases, texts = [], {}
    assignments = {item["id"]: item for item in amendment["assignments"]}
    for cid in IDS[:3]:
        item = assignments[cid]
        start, end = item["source_character_span"]
        selected = next(row for row in meta["epa"]["selected_insertions"] if row["insertion_words"] == item["insertion_words"])
        rank = ((meta["epa"]["eligible_newline_blocks"] - 1) * {10: 1, 20: 2, 40: 3}[item["insertion_words"]]) // 4
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source["text"])
                or selected["source_character_span"] != [start, end] or selected["selected_raw_substring_sha256"] != item["selected_raw_substring_sha256"]
                or selected["eligible_block_rank_zero_based"] != rank):
            raise ValueError("Reserved EPA ranks/spans changed; do not resample")
        excerpt = source["text"][start:end]
        if digest(excerpt) != item["selected_raw_substring_sha256"] or len(_tokens(excerpt)) != item["insertion_words"]:
            raise ValueError("Frozen EPA substring changed; do not resample")
        host, position = hosts[item["base_id"]], item["target_base_word_index"]
        if position not in (0, 90, 180):
            raise ValueError("Invalid frozen insertion position")
        split = 0 if position == 0 else len(host) if position == 180 else _tokens(host)[90].start
        left, right = host[:split].rstrip(), host[split:].lstrip()
        prefix = left + "\n\n" if left else ""
        text = prefix + excerpt + ("\n\n" + right if right else "")
        texts[cid] = text
        cases.append({"id": cid, "text_sha256": digest(text), "expected_evidence": [{"source_id": source["id"],
            "source_urls": [source["url"]], "source_span": [start, end], "target_span": [len(prefix), len(prefix) + len(excerpt)],
            "source_quote": excerpt, "needs_lexical_highlight": True, "insertion_words": item["insertion_words"]}]})
    for cid, name in zip(IDS[3:], ("A", "B")):
        texts[cid] = hosts[name]
        cases.append({"id": cid, "text_sha256": digest(hosts[name]), "expected_evidence": []})
    return {"cases": cases, "input_texts": texts, "preparation_failed_ids": list(FAILED_IDS),
            "plan_sha256": PLAN_SHA, "amendment_sha256": AMENDMENT_SHA}, texts


def request_id(run, case):
    return digest(run["id"] + case["id"] + case["text_sha256"])[:32]


def frozen_calls(run, manifest, plans):
    if (not isinstance(run.get("id"), str) or not re.fullmatch(r"[a-f0-9]{32}", run["id"])
            or tuple(case["id"] for case in manifest["cases"]) != IDS or set(plans) != set(IDS)):
        raise ValueError("Only the five amended publication cases are eligible")
    allowed = {}
    for case in manifest["cases"]:
        if not isinstance(case.get("text_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", case["text_sha256"]):
            raise ValueError("Frozen case input identity changed")
        if not 1 <= len(plans[case["id"]]) <= MAX_PER_CASE:
            raise ValueError("Each case is bounded to six queries")
        rid = request_id(run, case)
        for index, item in enumerate(plans[case["id"]]):
            if not isinstance(item.get("query"), str) or not 1 <= len(item["query"]) <= 400 or any(ord(c) < 32 for c in item["query"]):
                raise ValueError("Frozen provider query is invalid")
            allowed[digest(rid + "\0" + str(index))] = {"case_id": case["id"], "query_index": index, "query": item["query"]}
    if len(allowed) > MAX_QUERIES:
        raise ValueError("Thirty-query publication cap exceeded")
    return allowed


def frozen_wiki_calls(run, manifest, plans):
    if set(plans) != set(IDS):
        raise ValueError("Secondary plans must bind exactly the same five cases")
    allowed = {}
    for case in manifest["cases"]:
        cid, rid = case["id"], request_id(run, case)
        if len(plans[cid]) > MAX_WIKI_PER_CASE:
            raise ValueError("At most two anonymous queries per case")
        for index, item in enumerate(plans[cid]):
            if (type(item.get("primary_query_index")) is not int or not 0 <= item["primary_query_index"] < MAX_PER_CASE
                    or not isinstance(item.get("query"), str) or not 1 <= len(item["query"]) <= 400
                    or any(ord(c) < 32 for c in item["query"])):
                raise ValueError("Secondary frozen query binding changed")
            allowed[digest(rid + "\0mediawiki-post\0" + str(index))] = {
                "case_id": cid, "query_index": index, "primary_query_index": item["primary_query_index"], "query": item["query"]}
    return allowed


def reveal(expected_head):
    configuration = check_freeze(expected_head)
    meta = metadata()
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_dump(OUT / "reveal-intent.json", {"configuration": configuration, "at": datetime.now(timezone.utc).isoformat()}, exclusive=True)
    source = checked_source(meta)
    manifest, texts = construct_cases(meta, source)
    plans = {cid: web.plan_queries(text, strategy=configuration["choice"]["query_strategy"]) for cid, text in texts.items()}
    wiki_plans = {cid: secondary_plans(texts[cid], plan) for cid, plan in plans.items()}
    local = {"first_use": True, "used_for_tuning": False, "cases": [], "plans": plans, "wiki_plans": wiki_plans}
    for case in manifest["cases"]:
        cid, text = case["id"], texts[case["id"]]
        matches = analyze(text, [source])["matches"]
        row = {"id": cid, "control": not case["expected_evidence"], "source_known_exact_pairs": sum(m["kind"] == "exact" for m in matches)}
        if case["expected_evidence"]:
            gold = case["expected_evidence"][0]
            wanted = [t for t in _tokens(text) if gold["target_span"][0] <= t.start and t.end <= gold["target_span"][1]]
            row.update(copied_words=len(wanted), source_known_words=sum(any(m["kind"] == "exact" and m["start"] <= t.start and t.end <= m["end"] for m in matches) for t in wanted),
                phrase=probe_evidence(plans[cid], _tokens(text), gold, [t.value for t in _tokens(gold["source_quote"])]))
        local["cases"].append(row)
    local["summary"] = {"actual_cases": 5, "preparation_failed_cases": 3, "copied_words": sum(r.get("copied_words", 0) for r in local["cases"]),
        "source_known_words": sum(r.get("source_known_words", 0) for r in local["cases"]),
        "whole_query_inside_insertion_and_source": sum(r.get("phrase", {}).get("whole_query_in_both_spans") is True for r in local["cases"]),
        "planned_queries": sum(map(len, plans.values())), "planned_anonymous_queries": sum(map(len, wiki_plans.values())), "controls": 2,
        "control_exact_pairs": sum(r["source_known_exact_pairs"] for r in local["cases"] if r["control"]), "live_queries": 0}
    private_dump(OUT / "manifest.json", manifest, exclusive=True)
    private_dump(OUT / "local-first-use.json", local, exclusive=True)
    run = {"id": digest(PLAN_SHA + AMENDMENT_SHA + WIKI_AMENDMENT_SHA + configuration["choice_sha256"] + configuration["choice_canonical_sha256"])[:32], "configuration": configuration,
        "manifest_sha256": digest((OUT / "manifest.json").read_bytes()), "local_sha256": digest((OUT / "local-first-use.json").read_bytes()),
        "first_use": True, "used_for_tuning": False}
    frozen_calls(run, manifest, plans)
    frozen_wiki_calls(run, manifest, wiki_plans)
    private_dump(OUT / "run.json", run, exclusive=True)
    return local["summary"]


def load_run():
    run = json.loads((OUT / "run.json").read_text())
    execution_options(run["configuration"]["choice"])
    if (run.get("first_use") is not True or run.get("used_for_tuning") is not False
            or run["configuration"].get("choice_canonical_sha256") != digest(json.dumps(run["configuration"]["choice"], ensure_ascii=False, sort_keys=True, allow_nan=False))
            or run.get("id") != digest(PLAN_SHA + AMENDMENT_SHA + WIKI_AMENDMENT_SHA + run["configuration"]["choice_sha256"] + run["configuration"]["choice_canonical_sha256"])[:32]
            or run["configuration"]["choice"].get("discovery_strategy") != DISCOVERY
            or run["configuration"]["choice"].get("wiki_followup") is not True):
        raise ValueError("Saved first-use identity or complete discovery choice changed")
    manifest = checked_json(OUT / "manifest.json", run["manifest_sha256"])
    local = checked_json(OUT / "local-first-use.json", run["local_sha256"])
    if code_hashes() != run["configuration"]["code_sha256"]:
        raise ValueError("Code changed after first use")
    if any(digest(manifest["input_texts"][case["id"]]) != case["text_sha256"] for case in manifest["cases"]):
        raise ValueError("Saved publication input changed")
    frozen_calls(run, manifest, local["plans"])
    frozen_wiki_calls(run, manifest, local["wiki_plans"])
    return run, manifest, local


def claim_call(out, allowed, query, call, provider):
    expected = allowed.get(call)
    suffix = "search" if provider == "tavily" else "mediawiki"
    if expected is None or query != expected["query"]:
        raise ValueError("Provider query differs from its frozen allowlist")
    prior = out / (call + "-" + suffix + ".json")
    if prior.exists() or prior.is_symlink():
        raise web.WebSearchError("Provider call already has a durable outcome", "search_already_attempted")
    paths = [p for name in ("tavily", "mediawiki") for p in out.glob("*-" + name + "-intent.json")]
    if any(p.is_symlink() for p in paths):
        raise ValueError("Provider intent is a symlink")
    intents = [json.loads(p.read_text()) for p in paths]
    own = [row for row in intents if row["provider"] == provider]
    limit, per_case = (MAX_QUERIES, MAX_PER_CASE) if provider == "tavily" else (MAX_WIKI, MAX_WIKI_PER_CASE)
    if len(intents) >= MAX_PHYSICAL or len(own) >= limit or sum(r["case_id"] == expected["case_id"] for r in own) >= per_case:
        raise web.WebSearchError("Frozen provider cap exhausted", "search_budget_exhausted")
    try:
        private_dump(out / (call + "-" + provider + "-intent.json"),
            {"request_id": call, "provider": provider, **expected, "state": "submitted"}, exclusive=True)
    except FileExistsError:
        raise web.WebSearchError("Unknown submitted call is never retried", "search_already_attempted") from None


class PublicationSearch(RecordedSearch):
    def search(self, query, call):
        claim_call(self.out, self.allowed, query, call, "tavily")
        return super().search(query, call)


class RecordedWiki:
    provider = "mediawiki"

    def __init__(self, real, out, allowed):
        self.real, self.out, self.allowed = real, out, allowed
        self.last_usage = None

    def search(self, query, call):
        claim_call(self.out, self.allowed, query, call, self.provider)
        record = {"request_id": call, **self.allowed[call], "state": "submitted", "provider": self.provider}
        path = self.out / (call + "-mediawiki.json")
        private_dump(path, record, exclusive=True)
        try:
            results = self.real.search(query, call)
            record.update(state="complete", results=results)
            return results
        except Exception as error:
            record.update(state="failed_or_unknown", code=getattr(error, "code", type(error).__name__))
            raise
        finally:
            self.last_usage = self.real.last_usage
            record.update(usage=self.last_usage, response=self.real.last_response)
            private_dump(path, record)


class PublicationPages:
    def __init__(self, out, real, secret):
        self.out, self.real, self.secret, self.cid = out, real, secret, None

    def fetch(self, url):
        path = page_path(self.out, self.cid, url)
        if path.exists() or path.is_symlink():
            raise web.WebSearchError("Saved page intent is never repeated", "page_already_attempted")
        if len(list((self.out / "pages" / self.cid).glob("*.json"))) >= MAX_PAGES:
            raise web.WebSearchError("Frozen physical page cap exhausted", "page_probe_cap")
        started = time.monotonic()
        try:
            return RecordingPages(self.out, self.cid, self.real, self.secret).fetch(url)
        finally:
            if path.exists() and not path.is_symlink():
                record = json.loads(path.read_text())
                record["elapsed_seconds"] = round(time.monotonic() - started, 6)
                private_dump(path, record, secret=self.secret)


def accounting(out, run, manifest, plans, reports, wiki_plans=None):
    primary = frozen_calls(run, manifest, plans)
    wiki = frozen_wiki_calls(run, manifest, wiki_plans) if wiki_plans is not None else {}
    allowed = {**{c: {**v, "provider": "tavily"} for c, v in primary.items()},
               **{c: {**v, "provider": "mediawiki"} for c, v in wiki.items()}}
    sizes = {cid: {"tavily": len(plans[cid]), "mediawiki": len((wiki_plans or {}).get(cid, []))} for cid in IDS}
    return reconcile(out, allowed, sizes, reports)


def validate_report(report, run, case, local):
    choice, cid = run["configuration"]["choice"], case["id"]
    meta = report.get("web", {})
    primary, secondary = local["plans"][cid], local["wiki_plans"][cid]
    spans = lambda plan: [{k: item[k] for k in ("start", "end")} for item in plan]
    counts = {"tavily": meta.get("primary_queries_attempted"), "mediawiki": meta.get("post_wiki_queries_attempted")}
    if (any(type(n) is not int or not 0 <= n <= len(plan) for n, plan in zip(counts.values(), (primary, secondary)))
            or any(type(meta.get(k)) is not int or meta[k] != sum(counts.values()) for k in ("queries_attempted", "total_provider_requests"))):
        raise ValueError("Saved report provider counts differ from its frozen bounds")
    if (report.get("id") != report_identifier(request_id(run, case)) or not isinstance(report.get("text"), str)
            or digest(report["text"]) != case["text_sha256"]
            or any(meta.get(k) != choice[v] for k, v in (("query_strategy", "query_strategy"), ("url_strategy", "url_strategy"),
                ("matcher_version", "matcher_version"), ("named_references_enabled", "named_references")))
            or meta.get("zero_fallback_enabled") is not choice["zero_fallback"] or meta.get("post_wiki_enabled") is not True
            or meta.get("discovery_strategy") != DISCOVERY or meta.get("primary_query_spans") != spans(primary)
            or meta.get("query_spans") != spans(primary + secondary[:counts["mediawiki"]])
            or meta.get("post_wiki_query_plan") != secondary or meta.get("primary_provider") != "tavily"
            or meta.get("secondary_provider") != "mediawiki" or meta.get("max_provider_requests") != 8
            or meta.get("max_page_attempts") != MAX_PAGES
            or type(meta.get("urls_attempted")) is not int or not 0 <= meta["urls_attempted"] <= MAX_PAGES):
        raise ValueError("Saved report request, source policy or frozen plan identity changed")
    return counts


def score_only():
    run, manifest, local = load_run()
    rows, reports = [], {}
    for case in manifest["cases"]:
        cid = case["id"]
        path = OUT / (cid + ".json")
        if not path.exists():
            rows.append({"id": cid, "status": "report_missing"})
            continue
        raw = json.loads(path.read_text())
        counts = validate_report(raw, run, case, local)
        checked = _report(raw, case)
        reports[cid] = counts
        direct = set()
        for provider, allowed, suffix in (("tavily", frozen_calls(run, manifest, local["plans"]), "search"),
                ("mediawiki", frozen_wiki_calls(run, manifest, local["wiki_plans"]), "mediawiki")):
            for call, identity in allowed.items():
                marker = OUT / (call + "-" + suffix + ".json")
                if identity["case_id"] != cid or identity["query_index"] >= counts[provider] or not marker.exists():
                    continue
                record = json.loads(marker.read_text())
                if record.get("state") == "complete":
                    direct.update(_url(item["url"]) for item in record.get("results", []))
        read_urls = {_url(source["url"]) for source in raw["sources"]}
        evidence = []
        for gold in case["expected_evidence"]:
            wanted = [t for t in _tokens(raw["text"]) if gold["target_span"][0] <= t.start and t.end <= gold["target_span"][1]]
            urls = {_url(url) for url in gold["source_urls"]}
            evidence.append({"copied_words": len(wanted), "expected_url_returned_directly": bool(urls & direct),
                "whole_query_inside_insertion_and_source": probe_evidence(local["plans"][cid], _tokens(raw["text"]), gold, [t.value for t in _tokens(gold["source_quote"])])["whole_query_in_both_spans"],
                "expected_url_discovered_any_path": bool(urls & (checked["discovered_urls"] or set())),
                "expected_url_read": bool(urls & read_urls), "grounded_words_any_source": sum(any(m["target_span"][0] <= t.start and t.end <= m["target_span"][1] for m in checked["matches"]) for t in wanted),
                "grounded_words_expected_source": sum(any(m["target_span"][0] <= t.start and t.end <= m["target_span"][1] and m["url"] in urls for m in checked["matches"]) for t in wanted)})
        rows.append({"id": cid, "status": "report", "control": not case["expected_evidence"], "evidence": evidence,
            "invalid_highlights": checked["invalid_highlights"], "pairs": [{**match, "review": "pending_both_excerpts"} for match in raw["matches"]]})
    result = {"first_use": True, "used_for_tuning": False, "preparation_failed_ids": list(FAILED_IDS), "cases": rows,
        "local_source_known": local["summary"], "accounting": accounting(OUT, run, manifest, local["plans"], reports, local["wiki_plans"]), "manual_pair_review": "pending"}
    private_dump(OUT / "scores.json", result)
    return result


def preflight_new_job(run, manifest, local, case):
    cid = case["id"]
    pools = (frozen_calls(run, manifest, local["plans"]), frozen_wiki_calls(run, manifest, local["wiki_plans"]))
    owned = {call for pool in pools for call, item in pool.items() if item["case_id"] == cid}
    if ((OUT / (cid + ".json")).exists() or (OUT / (cid + "-submission-intent.json")).exists()
            or any(any(OUT.glob(call + "-*.json")) for call in owned)):
        raise RuntimeError("Prior report/provider/submission intent forbids another POST")
    reports = {c["id"]: validate_report(json.loads(path.read_text()), run, c, local)
        for c in manifest["cases"] if (path := OUT / (c["id"] + ".json")).exists()}
    state = accounting(OUT, run, manifest, local["plans"], reports, local["wiki_plans"])
    if state["total_basic_credits"] is None or code_hashes() != run["configuration"]["code_sha256"]:
        raise RuntimeError("Unreconciled accounting or code change forbids a new job")
    remaining = sum(len(local["plans"][c["id"]]) for c in manifest["cases"]
        if not any((OUT / (c["id"] + suffix)).exists() for suffix in (".json", "-request.json", "-error.json", "-submission-intent.json")))
    private_dump(OUT / (cid + "-quota-before.json"), quota(remaining))
    private_dump(OUT / (cid + "-submission-intent.json"), {"request_id": request_id(run, case),
        "text_sha256": case["text_sha256"], "run_sha256": digest((OUT / "run.json").read_bytes())}, exclusive=True)


@contextmanager
def live_lock():
    with os.fdopen(os.open(OUT / "live.lock", os.O_CREAT | os.O_RDWR, 0o600), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def live(expected_head, reviewed_sha):
    if digest((OUT / "run.json").read_bytes()) != reviewed_sha:
        raise ValueError("Explicit START must name the reviewed immutable run SHA256")
    run, manifest, local = load_run()
    if check_freeze(expected_head) != run["configuration"]:
        raise ValueError("Live complete choice differs from the tested first use")
    import server
    from plagiarism.jobs import Jobs
    from plagiarism.storage import Store
    from plagiarism.search_tavily import TavilySearch
    from plagiarism.search_mediawiki import MediaWikiSearch
    from plagiarism.web_fetch import Fetcher
    with live_lock():
        real = TavilySearch(web.SearchBudget())
        primary = PublicationSearch(real, OUT, frozen_calls(run, manifest, local["plans"]), max_queries=MAX_QUERIES, max_per_document=MAX_PER_CASE)
        secondary = RecordedWiki(MediaWikiSearch(), OUT, frozen_wiki_calls(run, manifest, local["wiki_plans"]))
        pages, original = PublicationPages(OUT, Fetcher(), real.key), web.run
        old_engine = os.environ.get("ISTOK_PDF_ALTERNATIVE_ENGINE")
        engine = run["configuration"]["choice"]["pdf_alternative_engine"]
        if engine is None:
            os.environ.pop("ISTOK_PDF_ALTERNATIVE_ENGINE", None)
        else:
            os.environ["ISTOK_PDF_ALTERNATIVE_ENGINE"] = engine
        web.run = partial(original, searcher=primary, wiki_searcher=secondary, fetcher=pages,
            use_wiki_followup=True, **execution_options(run["configuration"]["choice"]))
        service = jobs = None
        serving = False
        try:
            service = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            jobs = Jobs()
            service.saas, service.store, service.jobs = None, Store(), jobs
            Thread(target=service.serve_forever, daemon=True).start()
            serving = True
            def http(path, body=None, missing_ok=False):
                try:
                    return 200, queue_http(service.server_port, path, body)
                except QueueHTTPError as error:
                    if missing_ok and error.status == 404:
                        return 404, {}
                    raise
            for case in manifest["cases"]:
                cid, rid = case["id"], request_id(run, case)
                pages.cid = cid
                planned = len(local["plans"][cid]) + len(local["wiki_plans"][cid])
                job = recover_or_submit(http, OUT / (cid + "-request.json"), rid, case, manifest["input_texts"][cid], planned,
                    partial(preflight_new_job, run, manifest, local, case))
                started = time.monotonic()
                while job["status"] not in ("complete", "failed", "cancelled"):
                    if time.monotonic() - started > 420:
                        raise RuntimeError("Queue timeout; preserve identity and GET only")
                    time.sleep(.1)
                    job = http("/api/jobs/" + rid)[1]["job"]
                report = service.store.get("report", report_identifier(rid))
                if report is None:
                    private_dump(OUT / (cid + "-error.json"), {"request_id": rid, "status": job["status"], "code": job.get("code")})
                    continue
                validate_report(report, run, case, local)
                private_dump(OUT / (cid + ".json"), report, secret=real.key)
        finally:
            web.run = original
            if old_engine is None:
                os.environ.pop("ISTOK_PDF_ALTERNATIVE_ENGINE", None)
            else:
                os.environ["ISTOK_PDF_ALTERNATIVE_ENGINE"] = old_engine
            try:
                if service is not None:
                    if serving:
                        service.shutdown()
                    service.server_close()
                if jobs is not None:
                    jobs.close()
            finally:
                try:
                    result = quota(0)
                except Exception as error:
                    result = {"unavailable": type(error).__name__, "new_searches_allowed": False}
                private_dump(OUT / "quota-after.json", result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reveal", action="store_true")
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--score-only", action="store_true")
    parser.add_argument("--expected-git-head")
    parser.add_argument("--start-confirmation", choices=("START",))
    parser.add_argument("--max-new-queries", type=int)
    parser.add_argument("--max-new-tavily-queries", type=int)
    parser.add_argument("--max-new-wiki-queries", type=int)
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.live and (args.start_confirmation != "START" or args.max_new_queries != MAX_PHYSICAL
            or args.max_new_tavily_queries != MAX_QUERIES or args.max_new_wiki_queries != MAX_WIKI
            or not re.fullmatch(r"[a-f0-9]{64}", args.manifest_sha256 or "")):
        parser.error("Live requires separate START, caps40physical/30paid/10Wiki and reviewed run SHA256")
    if args.reveal:
        result = reveal(args.expected_git_head)
    elif args.live:
        live(args.expected_git_head, args.manifest_sha256)
        result = score_only()["accounting"]
    elif args.score_only:
        result = score_only()["accounting"]
    else:
        metadata()
        result = {"state": "METADATA_ONLY", "publication_body_read_by_this_command": False,
            "actual_case_ids": list(IDS), "preparation_failed_ids": list(FAILED_IDS), "maximum_future_queries": MAX_PHYSICAL,
            "maximum_future_paid_queries": MAX_QUERIES, "maximum_future_anonymous_queries": MAX_WIKI}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
