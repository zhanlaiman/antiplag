#!/usr/bin/env python3
"""Compare two text-independent URL orders on frozen DEV search responses.

No search, loading, or matching is performed. Missing pages give queue positions
only. Gold URLs are read solely after both orders have been constructed.
"""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.web_search import _canonical_url, MAX_URLS
from plagiarism.web_evaluation import _url

DEV = ROOT / "data/private/web-evaluation/materials/dev-30"


def domain_order(groups):
    seen, by_domain = set(), {}
    for results in groups:
        for raw in results:
            url = _canonical_url(raw.get("url"))
            if url and url not in seen:
                seen.add(url)
                by_domain.setdefault(urlsplit(url).hostname, []).append(url)
    ordered = []
    while any(by_domain.values()):
        for group in by_domain.values():
            if group:
                ordered.append(group.pop(0))
    return ordered


def query_order(groups):
    seen, ordered = set(), []
    for rank in range(max(map(len, groups), default=0)):
        for results in groups:
            if rank >= len(results):
                continue
            url = _canonical_url(results[rank].get("url"))
            if url and url not in seen:
                seen.add(url)
                ordered.append(url)
    return ordered


def hybrid_order(groups):
    """Reserve half the cap for domains, then cover queries by result rank."""
    previous = domain_order(groups)
    ordered = previous[:MAX_URLS // 2]
    seen = set(ordered)
    for url in query_order(groups):
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def compare(directory):
    run = json.loads((directory / "run.json").read_text())
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    by_id = {c["id"]: c for c in cases}
    if set(by_id) != {f"D{i:02d}" for i in range(1, 31)} or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only fixed DEV metadata are accepted")
    rows, totals = [], Counter()
    for case_id in run["case_ids"]:
        if case_id not in by_id:
            raise ValueError("Non-DEV requested")
        request_path = directory / (case_id + "-request.json")
        if not request_path.exists():
            rows.append({"id": case_id, "status": "not_submitted"})
            continue
        request = json.loads(request_path.read_text())
        groups, pending = [], []
        for index in range(request["planned_queries"]):
            call_id = sha256((request["request_id"] + "\0" + str(index)).encode()).hexdigest()
            path = directory / (call_id + "-search.json")
            if not path.exists():
                pending.append(index)
                continue
            saved = json.loads(path.read_text())
            if saved["request_id"] != call_id or saved["state"] != "complete":
                pending.append(index)
                continue
            groups.append(saved["results"])
        if pending:
            rows.append({"id": case_id, "status": "search_incomplete", "missing_query_indexes": pending})
            continue
        # The planners below cannot access case labels or expected URLs.
        orders = {"domain_round_robin": domain_order(groups), "query_round_robin": query_order(groups),
                  "hybrid_half_domains_queries": hybrid_order(groups)}
        orders = {name: [_url(url) for url in order] for name, order in orders.items()}
        full_path = directory / (case_id + ".json")
        report = json.loads(full_path.read_text()) if full_path.exists() else None
        read = {_url(s["url"]) for s in report.get("sources", [])} if report else set()
        evidence = []
        for gold in by_id[case_id]["expected_evidence"]:
            wanted = {_url(u) for u in gold["source_urls"]}
            found = bool(wanted & set(orders["domain_round_robin"]))
            score = {name: {"positions_1based": [i + 1 for i, url in enumerate(order) if url in wanted],
                            "selected_first12": bool(wanted & set(order[:MAX_URLS]))} for name, order in orders.items()}
            evidence.append({"source_id": gold["source_id"], "lexical": gold["needs_lexical_highlight"],
                             "expected_url_present": found, "loaded_content_saved": bool(wanted & read), "orders": score})
            if found:
                totals["expected_fragments_with_url"] += 1
                for name, result in score.items():
                    totals[name + "_selected"] += result["selected_first12"]
                previous = score["domain_round_robin"]["selected_first12"]
                for name in list(orders)[1:]:
                    candidate = score[name]["selected_first12"]
                    totals[name + "_gained"] += not previous and candidate
                    totals[name + "_lost"] += previous and not candidate
        totals["cases_with_complete_searches"] += 1
        observed_highlights = []
        for url in sorted({m["source_url"] for m in report.get("matches", []) if m["kind"] == "exact"}) if report else []:
            normalized = _url(url)
            observed_highlights.append({"url": url, "classification": "Observed exact highlight; not a new manual review",
                                        "positions_1based": {name: [i + 1 for i, item in enumerate(order) if item == normalized]
                                                             for name, order in orders.items()}})
        rows.append({"id": case_id, "status": "complete_search", "report_saved": bool(report),
                     "orders": orders, "evidence": evidence, "observed_highlight_sources": observed_highlights})
    return {"schema_version": 1, "live_queries": 0, "live_fetches": 0, "page_cap": MAX_URLS,
            "interpretation": "Only URL placement. A new position is not proof that unread content can be loaded or highlighted.",
            "totals": dict(totals), "cases": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved", type=Path, default=ROOT / "data/private/web-evaluation/dev-dense-six-live-v1")
    args = parser.parse_args()
    result = compare(args.saved)
    path = args.saved / "fetch-order-comparison.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)
    print(json.dumps({"totals": result["totals"], "cases": [{k: v for k, v in row.items() if k != "orders"} for row in result["cases"]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
