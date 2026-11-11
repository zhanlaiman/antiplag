#!/usr/bin/env python3
"""Check production URL selection against a frozen all-DEV candidate replay."""
import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search
from scripts.audit_dev_fetch_balance import BASE, DEV, OUT, RUNS, sources_cache
from scripts.replay_dev_fetch_candidates import describe
from scripts.replay_saved_dev_search import CachedPages, FrozenSearch, PAGE_CACHE, annotate_replay, load_queries
from scripts.run_dev_query_probe import dump


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-policy", choices=("replacement_cap1", "path_replacement_cap1", "productive_budget", "path_productive_budget"), required=True)
    parser.add_argument("--url-strategy", required=True)
    args = parser.parse_args()
    reference_path = OUT / ("dynamic-productive-budget-comparison.json" if args.reference_policy.endswith("productive_budget")
                            else "dynamic-replay-comparison.json")
    raw = reference_path.read_bytes()
    reference = json.loads(raw)
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [case["id"] for case in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only the fixed 30 DEV cases are allowed")
    by_id = {case["id"]: case for case in cases}
    directories = [BASE / name for name in RUNS if (BASE / name / "run.json").exists()]
    pool = {url: item["page"] for url, item in sources_cache([BASE / "dev-live-30-v1"] + directories).items()}
    denied = []
    def no_network(event, arguments):
        if event.startswith("socket."):
            denied.append(event)
            raise PermissionError("Production verification uses saved data only")
    sys.addaudithook(no_network)
    results = []
    for row in reference["cases"]:
        case = by_id[row["id"]]
        text = (DEV / case["input_path"]).read_text()
        if sha256(text.encode()).hexdigest() != case["text_sha256"]:
            raise ValueError("DEV input changed")
        records, identity = load_queries(BASE / row["source_run"], case["id"], case["text_sha256"])
        search = FrozenSearch(records)
        pages = CachedPages(deepcopy(pool), search.allowed_urls, cache_directory=PAGE_CACHE,
                            fill_missing=False, refresh_missing_links=False)
        report = web_search.run(text, searcher=search, fetcher=pages, query_strategy=web_search.DENSE_STRATEGY,
                                url_strategy=args.url_strategy)
        if denied or pages.fresh_attempts or search.missing_queries:
            raise ValueError("Verification attempted network or an unrecorded query")
        annotate_replay(report, search, pages, {"new_searches": 0, "query_identity": identity,
            "reference_policy": args.reference_policy, "reference_sha256": sha256(raw).hexdigest()})
        actual, expected = describe(report, case), row["policies"][args.reference_policy]
        differences = [key for key in ("matched_words", "expected_lexical_coverage", "sources", "attempted_urls", "linked_urls", "matches")
                       if actual[key] != expected[key]]
        results.append({"id": case["id"], "differences": differences, "actual": actual})
        dump(OUT / (case["id"] + "-production-" + args.reference_policy + ".json"), report)
    result = {"reference_sha256": sha256(raw).hexdigest(), "reference_policy": args.reference_policy,
        "url_strategy": args.url_strategy, "new_searches": 0, "new_fetches": 0, "cases": results,
        "reproduced_cases": sum(not r["differences"] for r in results), "checked_cases": len(results)}
    dump(OUT / ("production-" + args.reference_policy + "-verification.json"), result)
    print(json.dumps({key: result[key] for key in ("reference_policy", "reproduced_cases", "checked_cases", "new_searches", "new_fetches")}))
    if result["reproduced_cases"] != 30:
        raise SystemExit("Production differs from the frozen selected candidate")


if __name__ == "__main__":
    main()
