#!/usr/bin/env python3
"""Collect both excerpts for manual review of completed selected-code DEV runs.

Decisions are private, explicit, and bound to the entire exact saved pair.
An unseen or changed pair is never automatically treated as reviewed.
"""
from collections import Counter
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.summarize_night_results import BASE, BATCHES, FINAL_BATCHES, DEV, DEV_KEY_SHA
from scripts.dev_report_provenance import report_provenance, source_snapshot_signatures
from scripts.run_dev_query_probe import dump
from plagiarism.matching import _tokens
from plagiarism.web_evaluation import _report, _url

OUT = BASE / "selected-dev-audit-v1"
FINAL_OUT = BASE / "final-selected-dev-audit-v1"


def pair_signature(case_id, report_id, match, source_sha256=None):
    fields = ("kind", "source_id", "source_url", "start", "end", "source_start", "source_end", "excerpt", "source_excerpt")
    payload = [case_id, report_id, {key: match.get(key) for key in fields}]
    if source_sha256 is not None:
        payload.append({"source_text_sha256": source_sha256})
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def collect(final=False):
    if sha256((DEV / "key.json").read_bytes()).hexdigest() != DEV_KEY_SHA:
        raise ValueError("Changed DEV key")
    cases = {c["id"]: c for c in json.loads((DEV / "key.json").read_text())["cases"]}
    output = FINAL_OUT if final else OUT
    batches = FINAL_BATCHES if final else BATCHES
    decision_path = output / "review-decisions.json"
    decisions = json.loads(decision_path.read_text()) if decision_path.exists() else {}
    rows = []
    for directory, ids in batches:
        run_path = BASE / directory / "run.json"
        run = json.loads(run_path.read_text()) if run_path.exists() else {}
        for cid in ids:
            path = BASE / directory / (cid + ".json")
            if not path.exists():
                continue
            raw = json.loads(path.read_text())
            checked = _report(raw, cases[cid])
            snapshots = source_snapshot_signatures(raw)
            invalid = {e["match_index"] for e in checked["invalid_highlights"]}
            pairs = []
            for index, match in enumerate(raw["matches"]):
                source_hash = snapshots[match["source_id"]]["text_sha256"]
                sig = pair_signature(cid, raw["id"], match, source_hash if final else None)
                review = decisions.get(sig, {"decision": "needs_pair_review"})
                if final and sig in decisions and (review.get("case_id") != cid
                        or review.get("report_id") != raw["id"] or review.get("source_text_sha256") != source_hash):
                    raise ValueError("Final manual decision is not bound to this report and source snapshot")
                outside = [t for t in _tokens(match["excerpt"])
                           if not any(e["target_span"][0] <= t.start + match["start"]
                                      and t.end + match["start"] <= e["target_span"][1] for e in cases[cid]["expected_evidence"])]
                pairs.append({"signature": sig, "match_id": match["id"], "kind": match["kind"],
                    "source_url": match["source_url"], "target_excerpt": match["excerpt"],
                    "source_id": match["source_id"], "source_text_sha256": source_hash,
                    "source_excerpt": match["source_excerpt"], "quoted": match["quoted"],
                    "target_span": [match["start"], match["end"]], "outside_expected_words": len(outside),
                    "source_span": [match["source_start"], match["source_end"]],
                    "source_pair_validated": index not in invalid,
                    "review": review})
            report_sha = sha256(path.read_bytes()).hexdigest()
            rows.append({"id": cid, "scenario": cases[cid]["scenario"], "artifact": str(path.relative_to(ROOT)),
                "report_id": raw["id"], "report_sha256": sha256(path.read_bytes()).hexdigest(),
                "configuration": report_provenance(raw, run, directory, report_sha, case_id=cid),
                "source_snapshots": snapshots,
                "invalid_pairs": checked["invalid_highlights"], "pairs": pairs,
                "review_complete": not invalid and all(p["review"]["decision"] != "needs_pair_review" for p in pairs)})
    counts = Counter(p["review"]["decision"] for r in rows for p in r["pairs"])
    false_decisions = {"confirmed_false_highlight", "unsupported_similar_extension", "invalid_grounding",
                       "confirmed_false_similar_extension"}
    false_pairs = [(row, pair) for row in rows for pair in row["pairs"]
                   if not pair["source_pair_validated"] or pair["review"]["decision"] in false_decisions]
    completed_ids = {row["id"] for row in rows}
    expected_ids = [cid for _, ids in batches for cid in ids]
    result = {"schema_version": 2 if final else 1, "network_calls": 0,
        "scope": "Only completed reports in five final selected-code DEV batches" if final else "Only completed reports in four explicit remaining-DEV batches",
        "source_hash_bound_decisions": final,
        "expected_reports": len(expected_ids), "pending_report_ids": [cid for cid in expected_ids if cid not in completed_ids],
        "completed_reports": len(rows), "reviewed_reports": sum(r["review_complete"] for r in rows),
        "confirmed_false_pair_count": len(false_pairs),
        "confirmed_false_target_regions": len({(row["id"], *pair["target_span"]) for row, pair in false_pairs}),
        "ambiguous_pair_count": sum(count for decision, count in counts.items() if decision.startswith("ambiguous_")),
        "pair_decisions": dict(counts), "cases": rows}
    output.mkdir(mode=0o700, exist_ok=True)
    output.chmod(0o700)
    dump(output / "reviewed-pairs.json", result)
    return result


def merge_final_decisions():
    """Merge explicitly authored decisions only for presently validated pairs."""
    reviewed = collect(final=True)
    known = {pair["signature"]: (row, pair) for row in reviewed["cases"] for pair in row["pairs"]}
    merged = {}
    for path in sorted(FINAL_OUT.glob("decisions-D*.json")):
        for signature, decision in json.loads(path.read_text()).items():
            if signature not in known:
                raise ValueError("A decision names an unknown final saved pair")
            row, pair = known[signature]
            if (not isinstance(decision, dict) or not isinstance(decision.get("decision"), str)
                    or not decision.get("reason") or decision.get("case_id") != row["id"]
                    or decision.get("report_id") != row["report_id"]
                    or decision.get("source_text_sha256") != pair["source_text_sha256"]):
                raise ValueError("Unbound final pair decision")
            if signature in merged and merged[signature] != decision:
                raise ValueError("Conflicting final pair decisions")
            merged[signature] = decision
    dump(FINAL_OUT / "review-decisions.json", merged)
    return collect(final=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final", action="store_true", help="Review only five final30 batches using new source-hash-bound decisions")
    parser.add_argument("--merge-decisions", action="store_true", help="Validate and merge authored decisions-D*.json for final saved pairs")
    args = parser.parse_args()
    if args.merge_decisions and not args.final:
        parser.error("--merge-decisions requires the separate final review cohort")
    result = merge_final_decisions() if args.merge_decisions else collect(final=args.final)
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False, indent=2))
