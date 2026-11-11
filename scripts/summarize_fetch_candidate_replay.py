#!/usr/bin/env python3
"""Summarize frozen fetch-policy comparisons with the shared DEV denominators."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.web_evaluation import _url
from scripts.audit_dev_fetch_balance import DEV, OUT
from scripts.run_dev_query_probe import dump


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", choices=("fixed-caps", "productive"), default="productive")
    args = parser.parse_args()
    path = OUT / ("dynamic-productive-budget-comparison.json" if args.experiment == "productive" else "dynamic-replay-comparison.json")
    data = json.loads(path.read_text())
    cases = {c["id"]: c for c in json.loads((DEV / "key.json").read_text())["cases"]}
    if set(cases) != {f"D{i:02d}" for i in range(1, 31)} or any(c["split"] != "dev" for c in cases.values()):
        raise ValueError("Only fixed DEV metadata are accepted")
    policies = {}
    for policy in data["cases"][0]["policies"]:
        counts = {"documents": len(data["cases"]), "copied_words_total": 0, "copied_words_covered": 0,
            "lexical_fragments": 0, "lexical_fragments_full": 0, "expected_case_sources_total": 0,
            "expected_case_sources_read": 0, "matched_words_all": 0, "documents_without_read_pages": 0}
        losses = []
        for row in data["cases"]:
            if row["id"] not in cases:
                raise ValueError("Non-DEV replay")
            output = row["policies"][policy]
            read = {_url(u) for u in output["sources"]}
            unique = {e["source_id"]: e for e in cases[row["id"]]["expected_evidence"]}
            counts["expected_case_sources_total"] += len(unique)
            counts["expected_case_sources_read"] += sum(bool(read & {_url(u) for u in evidence["source_urls"]}) for evidence in unique.values())
            counts["matched_words_all"] += output["matched_words"]
            counts["documents_without_read_pages"] += not output["sources"]
            for evidence in output["expected_lexical_coverage"]:
                counts["copied_words_total"] += evidence["words"]
                counts["copied_words_covered"] += evidence["covered"]
                counts["lexical_fragments"] += 1
                counts["lexical_fragments_full"] += evidence["covered"] == evidence["words"]
            previous = {_url(m["source_url"]) for m in row["policies"]["current"]["matches"] if m["kind"] == "exact"}
            current = {_url(m["source_url"]) for m in output["matches"] if m["kind"] == "exact"}
            if previous - current:
                losses.append({"id": row["id"], "source_urls": sorted(previous - current)})
        policies[policy] = {"counts": counts, "observed_matching_source_losses": losses}
    result = {"source_snapshot_commit": data["snapshot_commit"], "artifact_sha256": sha256(path.read_bytes()).hexdigest(),
        "new_searches": 0, "new_direct_page_fetches_for_metadata": 1, "parent_refresh_artifact": "D20-parent-refresh.json",
        "copied_word_protocol": "shared copied_target_tokens helper", "scope": "URL/navigation only, primary text snapshots; no named-reference hints or supplementary PDF views",
        "current_reproduces_actual_fresh_matched_words": all(row["actual_fresh_matched_words"] == row["policies"]["current"]["matched_words"] for row in data["cases"]),
        "policies": policies}
    dump(OUT / ("productive-summary.json" if args.experiment == "productive" else "summary.json"), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
