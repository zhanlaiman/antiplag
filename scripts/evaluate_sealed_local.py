#!/usr/bin/env python3
"""Offline matcher diagnostic on the previously sealed 70; never searches the web."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re

from plagiarism.matching import analyze


ROOT = Path(__file__).resolve().parents[1]
MATERIALS = ROOT / "data/private/web-evaluation/materials/final-sealed"
OUTPUT = ROOT / "data/private/web-evaluation/final-70-local-matcher.json"
FROZEN_COMMIT = "5996c3adaeab60d8e0a8df17f4962e2afdc502aa"


def covered_words(text, start, end, exact_matches):
    return sum(bool(re.fullmatch(r"\W*", text[a:b]) is None and
                    any(match["start"] <= a and b <= match["end"]
                        for match in exact_matches))
               for a, b in ((start + m.start(), start + m.end())
                            for m in re.finditer(r"\w+", text[start:end], re.UNICODE)))


def main():
    key = json.loads((MATERIALS / "key.json").read_text())
    negative_sources = {}
    for case in key["cases"]:
        for evidence in case["expected_evidence"]:
            path = MATERIALS / evidence["source_snapshot_path"]
            body = path.read_text()
            assert sha256(body.encode()).hexdigest() == evidence["source_text_sha256"]
            negative_sources[evidence["source_id"]] = {
                "id": evidence["source_id"], "title": evidence["source_id"],
                "url": evidence["source_urls"][0], "text": body}
    rows = []
    for case in key["cases"]:
        text = (MATERIALS / case["input_path"]).read_text()
        assert sha256(text.encode()).hexdigest() == case["text_sha256"]
        sources = []
        for evidence in case["expected_evidence"]:
            path = MATERIALS / evidence["source_snapshot_path"]
            body = path.read_text()
            assert sha256(body.encode()).hexdigest() == evidence["source_text_sha256"]
            if not any(s["id"] == evidence["source_id"] for s in sources):
                sources.append({"id": evidence["source_id"], "title": evidence["source_id"],
                                "url": evidence["source_urls"][0], "text": body})
        # Controls must see real comparison pages; an empty source list proves nothing.
        comparison_sources = sources if case["expected_evidence"] else list(negative_sources.values())
        result = analyze(text, comparison_sources)
        details = []
        for evidence in case["expected_evidence"]:
            start, end = evidence["target_span"]
            gold_words = len(re.findall(r"\w+", text[start:end], re.UNICODE))
            exact = [m for m in result["matches"]
                     if m["kind"] == "exact" and m["source_id"] == evidence["source_id"]]
            found_words = covered_words(text, start, end, exact)
            details.append({"lexical": evidence["needs_lexical_highlight"],
                            "expected_words": gold_words, "exact_words": found_words,
                            "fully_highlighted": found_words == gold_words if gold_words else False})
        rows.append({"id": case["id"], "scenario": case["scenario"],
                     "synthetic": case["synthetic"], "source_snapshots": len(comparison_sources),
                     "expected": details, "matched_pairs": len(result["matches"]),
                     "matched_characters": result["stats"]["matched_characters"],
                     "unexpected_pairs_need_review": (len(result["matches"]) if not details else None)})
    lexical = [e for row in rows for e in row["expected"] if e["lexical"]]
    negatives = [row for row in rows if not row["expected"]]
    summary = {"cases": len(rows), "scenarios": dict(Counter(row["scenario"] for row in rows)),
               "lexical_evidence": len(lexical),
               "lexical_fully_highlighted": sum(e["fully_highlighted"] for e in lexical),
               "lexical_words_found": sum(e["exact_words"] for e in lexical),
               "lexical_words_expected": sum(e["expected_words"] for e in lexical),
               "negative_controls": len(negatives),
               "negative_controls_with_matches": sum(row["matched_pairs"] > 0 for row in negatives),
               "negative_source_pool_size": len(negative_sources),
               "false_highlights_confirmed": None,
               "false_highlights_reviewed": 0,
               "limitations": "Known gold source snapshots were supplied directly to the matcher. This does not test search, URL selection, or live source readability. Annotation is unreviewed."}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({"frozen_commit": FROZEN_COMMIT, "summary": summary,
                                  "cases": rows}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
