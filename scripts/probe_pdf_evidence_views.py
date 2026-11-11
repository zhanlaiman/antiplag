#!/usr/bin/env python3
"""Union separate saved PDF evidence views without creating cross-view text."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens, analyze
from scripts.probe_pdf_layout import DEV, OUT, NEGATIVE
from scripts.run_dev_query_probe import dump

BOUNDARY_PROBES = (
    {"id": "joined_preposition", "text": "или равна потолщине дополнительному штриху кроме тех случаев когда",
     "bad_word": "потолщине", "source_reading": "по толщине", "page": 4},
    {"id": "joined_material", "text": "изтвердого и пружинящего материала например из вороненой",
     "bad_word": "изтвердого", "source_reading": "из твердого", "page": 4},
    {"id": "column_splice", "text": "Три барочные антиквы Засеч Три шрифта промышленной рево",
     "bad_word": None, "source_reading": "Separate left/right column paragraphs; no such contiguous sentence", "page": 3},
)


def score(target, views, bad_word=None):
    tokens = _tokens(target)
    all_matches, by_view = [], {}
    for name, source in views.items():
        # Each analyze call sees one whole view. The views are never joined.
        matches = analyze(target, [{"id": "pdf-" + name, "title": name,
            "url": "https://saved.test/same.pdf", "text": source}])["matches"]
        exact = [m for m in matches if m["kind"] == "exact"]
        covered = {t.start for t in tokens if any(m["start"] <= t.start and t.end <= m["end"] for m in exact)}
        bad = [t for t in tokens if bad_word and t.value == bad_word]
        by_view[name] = {"covered_words": len(covered), "words": len(tokens),
            "bad_word_highlighted": any(t.start in covered for t in bad), "matches": exact}
        all_matches.extend(exact)
    union = {t.start for t in tokens if any(m["start"] <= t.start and t.end <= m["end"] for m in all_matches)}
    return {"views": by_view, "union": {"covered_words": len(union), "words": len(tokens),
        "bad_word_highlighted": any(t.start in union for t in tokens if bad_word and t.value == bad_word)}}


def main():
    comparison = json.loads((OUT / "comparison.json").read_text())
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    rows, literal_retained = [], 0
    for record in comparison["cases"]:
        label = record["label"]
        views = {mode: json.loads((OUT / (label + "-" + mode + ".json")).read_text())["normalized_text"]
                 for mode in ("plain", "layout") if record["parse_status"][mode]["status"] == "complete"}
        if "plain" not in views:
            continue
        case = next(c for c in cases if c["id"] == label)
        target = (DEV / case["input_path"]).read_text()
        controls = []
        for control in record["literal_controls"]:
            result = score(control["text"], views)
            literal_retained += result["union"]["covered_words"] == result["views"]["plain"]["covered_words"]
            controls.append({"text": control["text"], "result": result})
        row = {"label": label, "actual_case": score(target, views), "literal_controls": controls,
               "authored_same_topic_negative": score(NEGATIVE, views)}
        if label == "D06":
            row["expected_quote"] = score(case["expected_evidence"][0]["target_quote"], views)
        if label == "D16":
            visual = json.loads((OUT / "visual-control.json").read_text())
            row["visually_transcribed_positive"] = {"text": visual["text"], "result": score(visual["text"], views)}
            row["artificial_boundary_negatives"] = [{**probe, "visually_validated": True,
                "result": score(probe["text"], views, probe["bad_word"])} for probe in BOUNDARY_PROBES]
        rows.append(row)
    result = {"schema_version": 1, "new_searches": 0, "new_fetches": 0, "production_changed": False,
        "method": "Separate matching calls per complete saved view, union of target word positions; no text concatenation.",
        "plain_literal_controls_preserved": literal_retained, "plain_literal_controls": 9,
        "decision": "Reject unfiltered union: supplementary layout supplies source-unfaithful word boundaries and column adjacency.",
        "cases": rows}
    dump(OUT / "separate-view-union.json", result)
    print(json.dumps({"literal_controls_preserved": literal_retained,
        "D06_expected": rows[0].get("expected_quote"),
        "boundary_negatives": [{"id": p["id"], "plain": p["result"]["views"]["plain"]["covered_words"],
            "layout": p["result"]["views"]["layout"]["covered_words"], "union": p["result"]["union"]}
            for row in rows for p in row.get("artificial_boundary_negatives", [])]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
