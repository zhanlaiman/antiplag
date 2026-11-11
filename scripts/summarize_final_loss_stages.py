#!/usr/bin/env python3
"""Evidence-bound final DEV loss stages; never edits gold or auto-review labels."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.analyze_web_dev_checks import inspect, summarize
from scripts.summarize_night_results import BASE, DEV, DEV_KEY_SHA, FINAL_BATCHES
from scripts.run_dev_query_probe import dump

OUT = BASE / "final-selected-dev-audit-v1/confirmed-loss-stages.json"


def read_evidence(relative):
    path = BASE / relative
    raw = path.read_bytes()
    return json.loads(raw), {"artifact": str(path.relative_to(ROOT)), "sha256": sha256(raw).hexdigest()}


def main():
    if sha256((DEV / "key.json").read_bytes()).hexdigest() != DEV_KEY_SHA:
        raise ValueError("Changed DEV key")
    rows, files = [], {}
    for directory, allowed in FINAL_BATCHES:
        for row in inspect(DEV, BASE / directory)["cases"]:
            if row["status"] == "saved":
                if row["id"] not in allowed:
                    raise ValueError("Wrong final DEV case")
                rows.append(row)
                path = BASE / directory / (row["id"] + ".json")
                files[row["id"]] = {"artifact": str(path.relative_to(ROOT)), "sha256": sha256(path.read_bytes()).hexdigest()}
    if {row["id"] for row in rows} != {f"D{i:02d}" for i in range(1, 31)}:
        raise ValueError("Final30 is incomplete")
    d08, d08_origin = read_evidence("dev-final-d08-regression-v1/comparison.json")
    d10, d10_origin = read_evidence("final-selected-dev-audit-v1/D10-regression/summary.json")
    d20, d20_origin = read_evidence("final-selected-dev-audit-v1/D20-regression/summary.json")
    keyed = {row["id"]: row for row in rows}
    if (d08["case_id"] != "D08" or d08["query_strings_changed"]
            or d08["queue_comparison"]["new_reply"]["expected_url_positions"] != [21]
            or any(d08["same_new_reply_replays"][name]["matched_words"] != 0
                   or d08["same_new_reply_replays"][name]["page_cache_misses"]
                   or d08["same_new_reply_replays"][name]["missing_link_metadata"]
                   for name in ("balanced_productive_followups", "path_productive_followups"))):
        raise ValueError("D08 saved selection proof changed")
    if (d10["fresh_report_id"] != keyed["D10"]["report_id"] or not d10["query_plans_identical"]
            or d10["fresh_path_initial_position"] != 19 or d10["fresh_balanced_initial_position"] != 19
            or d10["saved_old_source_still_highlights_words"] != 11
            or any(r["matched_words"] or r["relevant_url_attempted"] or r["missing_page_captures"]
                   for r in d10["fresh_balanced_vs_path_replay"])):
        raise ValueError("D10 alternate-source selection proof changed")
    if (d20["fresh_report_id"] != keyed["D20"]["report_id"] or not d20["query_plans_identical"]
            or d20["fresh_parent_ranks"] or d20["saved_old_first_source_still_highlights_words"] != 11
            or any(r["matched_words"] != 14 or r["target_url_discovered"] or r["parent_url_discovered"] or r["missing_page_captures"]
                   for r in d20["same_fresh_reply_replays"])):
        raise ValueError("D20 retrieval-path proof changed")
    records = []
    for row in rows:
        for index, evidence in enumerate(row["evidence"]):
            if not evidence["lexical"]:
                continue
            lost = evidence["literal_expected_words"] - evidence["literal_covered_words"]
            if not lost:
                continue
            automated = evidence["earliest_confirmed_cause"]
            stage, reason, proof = automated, None, []
            if row["id"] == "D08" and index == 0:
                stage, reason, proof = "page_selection", "Expected page remains discovered at21>12 in both orders; saved reply replay complete for actual selected pages.", [d08_origin]
            elif row["id"] == "D10" and index == 0:
                stage, reason, proof = "page_selection", "The original annotated URL is absent, but a separately validated alternate page still contains all11 copied words and remains discovered at19>12 in both orders. This overlay classifies copied-fragment loss, not original-URL recall.", [d10_origin]
            elif row["id"] == "D20" and index == 0:
                stage, reason, proof = "url_retrieval", "The prior direct-result parent of the correct source link disappeared; neither parent nor expected page is now discovered.", [d20_origin]
            records.append({"case_id": row["id"], "report_id": row["report_id"], "report": files[row["id"]],
                "fragment_index": index, "copied_words_lost": lost, "automated_gold_url_stage": automated,
                "reviewed_copied_fragment_stage": stage, "reason": reason, "proof": proof,
                "expected_url_discovered": evidence["expected_url_discovered"],
                "expected_url_read": evidence["expected_url_read"],
                "whole_query_in_insertion_and_source": evidence["whole_query_in_both_spans"]})
    stages = Counter(r["reviewed_copied_fragment_stage"] for r in records)
    words = Counter()
    for row in records:
        words[row["reviewed_copied_fragment_stage"]] += row["copied_words_lost"]
    metrics = summarize(rows)
    if sum(words.values()) != metrics["copied_words"] - metrics["copied_words_highlighted_any_source"]:
        raise ValueError("Unaccounted copied-word deficit")
    result = {"schema_version": 1, "network_calls": 0, "final_materials_read": False,
        "scope": "Uniform final selected-code DEV30;18 lexical fragments.11 semantic fragments are excluded and remain unverified semantically.",
        "classification_basis": "Earliest confirmed loss of the copied fragment, accepting validated alternate literal sources. Original annotated-URL recall remains a separate metric. Uncertain query-vs-retrieval cases stay uncertain; gold labels are unchanged.",
        "documents": 30, "literal_fragments": 18, "semantic_fragments_separate": 11,
        "whole_query_in_both_spans": metrics["whole_query_in_both_spans"],
        "copied_words": metrics["copied_words"], "copied_words_highlighted": metrics["copied_words_highlighted_any_source"],
        "fully_highlighted_fragments": metrics["fully_highlighted_any_source"],
        "expected_source_pairs": metrics["expected_source_pairs"],
        "expected_source_pairs_found": metrics["expected_source_pairs_discovered"],
        "expected_source_pairs_read": metrics["expected_source_pairs_read"],
        "lost_fragments_by_stage": dict(stages), "lost_copied_words_by_stage": dict(words),
        "confirmed_phrase_selection_only_losses": stages.get("phrase_selection", 0),
        "confirmed_extraction_losses_remaining": stages.get("extraction", 0),
        "confirmed_matching_losses_remaining": stages.get("matching", 0),
        "warning": "No observed downstream deficit means no demonstrated loss there in these18 fragments; it does not establish general extractor/matcher recall. D02 remains unresolved.",
        "losses": records}
    dump(OUT, result)
    print(json.dumps({k:v for k,v in result.items() if k != "losses"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
