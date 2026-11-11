#!/usr/bin/env python3
"""Three-policy local comparison on the frozen metadata pilot replies/pages."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search as web
from plagiarism.matching import _tokens
from plagiarism.web_evaluation import _report
from scripts.analyze_web_dev_checks import copied_target_tokens
from scripts.dev_report_provenance import source_snapshot_signatures
from scripts.probe_document_metadata import MIN_RUN, MAX_RUN, STRATEGY, document_index, longest_indexed_run, rank_document_metadata
from scripts.probe_search_metadata import _CHUNK_BREAK, longest_run, rank_by_snippet_overlap
from scripts.replay_final_url_policies import FinalPages, match_identity
from scripts.replay_saved_dev_search import FrozenSearch, annotate_replay, canonical
from scripts.run_dev_metadata_pilot import CASE_IDS, ranking_records, saved_queries
from scripts.run_dev_query_probe import dump

BASE = ROOT / "data/private/web-evaluation"
SOURCE = BASE / "dev-metadata-pilot-v2"
DEV = BASE / "materials/dev-30"
OUT = BASE / "dev-document-metadata-local-v1"


def main():
    OUT.mkdir(mode=0o700, exist_ok=True)
    plan = {"strategy": STRATEGY, "minimum_words": MIN_RUN, "maximum_words": MAX_RUN,
        "frozen_before_comparison": True, "frozen_at": datetime.now(timezone.utc).isoformat(),
        "candidate_sha256": sha256((ROOT / "scripts/probe_document_metadata.py").read_bytes()).hexdigest(),
        "comparison_script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_manifest_sha256": sha256((SOURCE / "run.json").read_bytes()).hexdigest(),
        "case_ids": list(CASE_IDS), "no_new_searches_or_fetches": True}
    path = OUT / "frozen-plan.json"
    if path.exists():
        old = json.loads(path.read_text())
        if {k: v for k, v in old.items() if k != "frozen_at"} != {k: v for k, v in plan.items() if k != "frozen_at"}:
            raise ValueError("Local candidate changed after freeze; preserve the previous comparison")
        plan = old
    else:
        dump(path, plan)
    run = json.loads((SOURCE / "run.json").read_text())
    if run["configuration"]["case_ids"] != list(CASE_IDS):
        raise ValueError("Only the declared three-case pilot is accepted")
    key = {c["id"]: c for c in json.loads((DEV / "key.json").read_text())["cases"]}
    denied = []
    def no_network(event, arguments):
        if event.startswith("socket."):
            denied.append(event)
            raise PermissionError("Metadata local comparison forbids network")
    sys.addaudithook(no_network)
    results = []
    for cid in CASE_IDS:
        case = key[cid]
        if case["split"] != "dev":
            raise ValueError("Only DEV input is allowed")
        text = (DEV / case["input_path"]).read_text()
        if sha256(text.encode()).hexdigest() != run["configuration"]["input_sha256"][cid]:
            raise ValueError("Changed pilot input")
        actual = json.loads((SOURCE / (cid + ".json")).read_text())
        records = saved_queries(SOURCE, run, cid, include_failed=True)
        if len(records) != 6 or any(r["state"] != "complete" for r in records):
            raise ValueError("Need all six complete frozen replies")
        projected = ranking_records(records)
        by_query = {record["query"]: record for record in records}
        own, shared = {}, {}
        for path in sorted((SOURCE / "pages" / cid).glob("*.json")):
            record = json.loads(path.read_text())
            url = canonical(record["requested_url"])
            record.update(_artifact=str(path.relative_to(ROOT)), _artifact_sha256=sha256(path.read_bytes()).hexdigest(),
                          _case_id=cid, _report_id=actual["id"])
            if record["state"] != "complete":
                record["_typed_report_code"] = record.get("code", "saved_page_outcome_unknown")
            else:
                page = record["fetched"]
                if sha256(page["text"].encode()).hexdigest() != page["text_sha256"]:
                    raise ValueError("Changed source body")
                shared[canonical(page["url"])] = record
            own[url] = shared[url] = record
        policies = {}
        for policy in ("path", "own_query", "whole_document"):
            search = FrozenSearch(by_query)
            pages = FinalPages(own, shared, search.allowed_urls)
            previous = web._fetch_order
            audit = {}
            def selector(urls, groups, strategy):
                baseline = web._rank_url_paths(previous(urls, groups, web.PATH_URL_STRATEGY), text)
                if policy == "own_query":
                    order, reasons = rank_by_snippet_overlap(baseline, projected, projected=True)
                    audit.update(reasons=reasons)
                elif policy == "whole_document":
                    order, detail = rank_document_metadata(baseline, text, projected, projected=True)
                    audit.update(detail)
                else:
                    order = baseline
                audit.update(baseline_path_order=baseline, selected_order=order)
                return order
            with patch.object(web, "_fetch_order", selector):
                report = web.run(text, request_id=run["configuration"]["request_ids"][cid],
                    searcher=search, fetcher=pages, query_strategy=web.DENSE_STRATEGY,
                    url_strategy=web.BALANCED_URL_STRATEGY, use_named_references=False, use_zero_fallback=False)
            if denied or pages.fresh_attempts or search.missing_queries:
                raise ValueError("Frozen replay guarantee violated")
            if policy == "own_query":
                if ({match_identity(m) for m in report["matches"]} != {match_identity(m) for m in actual["matches"]}
                        or source_snapshot_signatures(report) != source_snapshot_signatures(actual)
                        or report["web"]["attempted_urls"] != actual["web"]["attempted_urls"]):
                    raise ValueError("Own-query replay failed to reproduce actual pilot " + cid)
            report.update(id=sha256(("local-metadata" + policy + cid).encode()).hexdigest()[:32])
            annotate_replay(report, search, pages, {"case_id": cid, "policy": policy,
                "main_store_written": False, "new_searches": 0, "source_manifest_sha256": plan["source_manifest_sha256"]})
            (OUT / policy).mkdir(mode=0o700, exist_ok=True)
            dump(OUT / policy / (cid + ".json"), report)
            checked = _report(report, case)
            if checked["invalid_highlights"]:
                raise ValueError("Unfaithful stored highlight")
            tokens = _tokens(text)
            wanted = [t for e in case["expected_evidence"] if e["needs_lexical_highlight"] for t in copied_target_tokens(tokens, e)]
            exact = [m for m in report["matches"] if m["kind"] == "exact"]
            policies[policy] = {"matched_words_all": report["stats"]["matched_words"], "copied_words": len(wanted),
                "copied_words_covered": sum(any(m["start"] <= t.start and t.end <= m["end"] for m in exact) for t in wanted),
                "unknown_selected_pages": pages.cache_misses, "saved_failed_fetches": pages.saved_failures,
                "initial_order": audit["selected_order"], "ranking": audit,
                "sources_read": report["web"]["sources_checked"], "physical_attempts": len(pages.calls)}
        result = {"id": cid, "actual_report_id": actual["id"], "policies": policies}
        if cid == "D10":
            # Diagnosis is made AFTER policy execution. This returned URL/name
            # is never supplied to either selector or source-body matcher.
            rows = []
            index, _ = document_index(text)
            for record in records:
                for item in record["results"]:
                    if item.get("title") != "Дагеротипия — Википедия":
                        continue
                    views = [item.get("title", "")] + _CHUNK_BREAK.split(item.get("content", ""))
                    own_words = [t.value for t in _tokens(record["query"])]
                    own_run = max(longest_run(own_words, [t.value for t in _tokens(v)]) for v in views)
                    doc_run = max(longest_indexed_run(index, v) for v in views)
                    url = canonical(item["url"])
                    rows.append({"url": url, "query_index": record["query_index"], "provider_rank": item["provider_rank"],
                        "maximum_own_query_run_words": own_run, "maximum_document_run_at_least6": doc_run,
                        "positions": {p: v["initial_order"].index(url) + 1 for p, v in policies.items()},
                        "page_body_captured_in_pilot": url in own})
            result["dager_diagnosis"] = rows
        results.append(result)
        print(cid, {p: v["copied_words_covered"] for p, v in policies.items()}, flush=True)
    output = {"schema_version": 1, "frozen_plan": plan, "new_searches": 0, "new_fetches": 0,
        "actual_own_query_reports_reproduced": 3, "cases": results,
        "limitation": "Unknown selected page bodies remain unknown; metadata never enters matching evidence."}
    dump(OUT / "comparison.json", output)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
