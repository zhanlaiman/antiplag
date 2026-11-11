#!/usr/bin/env python3
"""Score saved reports only; no search, page fetch, or model execution."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plagiarism.web_evaluation import EvaluationError, score_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--allow-test", action="store_true", help="Explicitly permit the frozen test split.")
    parser.add_argument("--frozen-config-sha256", help="Expected SHA-256 of the frozen baseline configuration.")
    parser.add_argument("--output", type=Path, help="Save the full JSON score artifact.")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    try:
        result = score_manifest(args.manifest, split=args.split, allow_test=args.allow_test,
                                frozen_config_sha256=args.frozen_config_sha256)
    except EvaluationError as error:
        parser.exit(2, str(error) + "\n")
    serialized = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    if args.format == "json":
        print(serialized, end="")
    else:
        summary = result["summary"]
        print(f"Cases: {summary['selected_case_count']}; reviewed: {summary['reviewed_case_count']}; excluded unreviewed: {summary['excluded_unreviewed_case_count']}")
        print("id\tscenario\toutcome\tevidence found\tcoverage\tquery errors")
        for case in result["cases"]:
            found = sum(item["outcome"] == "found" for item in case["evidence"])
            print(f"{case['id']}\t{case['scenario']}\t{case['outcome']}\t{found}/{len(case['evidence'])}\t{case['coverage']}\t{len(case['query_errors'])}")
        for scenario, metrics in summary["by_scenario"].items():
            details = [f"{key}={metrics[key]['numerator']}/{metrics[key]['denominator']}" for key in
                       ("source_discovery", "source_reading", "lexical_evidence_found", "nonlexical_source_read", "evidence_found", "unexpected_exact_highlights")]
            print(scenario + ": " + "; ".join(details))
        print("Zero observed highlights in a partial report do not prove uniqueness.")
        print("For needs_lexical_highlight=false, found means source read, not paraphrase/translation detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
