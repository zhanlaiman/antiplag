#!/usr/bin/env python3
"""Audit every changed token boundary and a filtered separate-view PDF union.

Reads only the three frozen DEV PDFs' extracted views. It never loads a PDF,
fetches a page, installs anything, or changes production matching.
"""
from collections import Counter
import argparse
from difflib import SequenceMatcher
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens, analyze
from plagiarism.web_fetch import _normalize_whitespace
from scripts.probe_pdf_layout import OUT, DEV, NEGATIVE
from scripts.probe_pdf_evidence_views import BOUNDARY_PROBES
from scripts.run_dev_query_probe import dump

FUNCTION_WORDS = set("в во к ко с со на по из за от до о об при и а но у не ни ли же то бы".split())
MATH_ADVERSARIAL = (
    {"id": "page_number_formula_sequence", "pages": [17, 18],
     "text": "Среднее квадратическое отклонение Sd 18 5 5 1 14,8 10 4"},
    {"id": "subscript_order_sequence", "pages": [14],
     "text": "средние арифметические x x xm 1 2 а также определить доверительные границы погрешностей"},
)


def invalid_evidence_character(character):
    value = ord(character)
    return 0xD800 <= value <= 0xDFFF or 0xFDD0 <= value <= 0xFDEF or value & 0xFFFF in (0xFFFE, 0xFFFF)


def valid_new_match(match):
    return not any(invalid_evidence_character(c) for key in ("excerpt", "source_excerpt") for c in match[key])


def primary_character_index(plain):
    characters, offsets = [], []
    for offset, character in enumerate(plain):
        if not character.isspace():
            characters.append(character)
            offsets.append(offset)
    return "".join(characters), offsets


def ordered_primary_support(excerpt, plain, index=None):
    """Locate the same characters, ignoring whitespace only; never rewrite."""
    compact, offsets = index if index is not None else primary_character_index(plain)
    wanted = "".join(character for character in excerpt if not character.isspace())
    position = compact.find(wanted)
    if not wanted or position < 0:
        return None
    start, end = offsets[position], offsets[position + len(wanted) - 1] + 1
    return {"plain_span": [start, end], "plain_text_sha256": sha256(plain.encode()).hexdigest(),
        "plain_support_sha256": sha256(plain[start:end].encode()).hexdigest(),
        "ordered_non_whitespace_sha256": sha256(wanted.encode()).hexdigest()}


def exact_matches(target, source, view):
    return [m for m in analyze(target, [{"id": view, "title": view,
        "url": "https://saved.test/same.pdf", "text": source}])["matches"] if m["kind"] == "exact"]


def filtered_union(target, plain, pdfium, bad_word=None, require_order=True):
    original = exact_matches(target, plain, "plain")
    supplementary = exact_matches(target, pdfium, "pdfium")
    index = primary_character_index(plain)
    accepted, rejected = [], []
    for match in supplementary:
        if match["source_excerpt"] != pdfium[match["source_start"]:match["source_end"]]:
            raise ValueError("Supplementary evidence does not match its saved source slice")
        if not valid_new_match(match):
            rejected.append({**match, "rejection": "invalid_unicode_in_evidence"})
            continue
        support = ordered_primary_support(match["source_excerpt"], plain, index)
        if require_order and support is None:
            rejected.append({**match, "rejection": "ordered_characters_not_in_primary"})
            continue
        accepted.append({**match, "primary_support": support,
                         "supplementary_text_sha256": sha256(pdfium.encode()).hexdigest()})
    tokens = _tokens(target)
    def covered(matches):
        selected = {t.start for t in tokens if any(m["start"] <= t.start and t.end <= m["end"] for m in matches)}
        return {"words": len(tokens), "covered_words": len(selected),
                "bad_word_highlighted": any(t.start in selected for t in tokens if bad_word and t.value == bad_word)}
    primary_coverage, union_coverage = covered(original), covered(original + accepted)
    return {"plain": primary_coverage, "pdfium_unfiltered": covered(supplementary),
        "filtered_union": union_coverage, "additional_covered_words": union_coverage["covered_words"] - primary_coverage["covered_words"],
        "accepted_supplementary_matches_may_duplicate_primary": True, "accepted_new_matches": accepted,
        "rejected_new_matches": rejected, "plain_matches_unchanged": original}


def page_ranges(result):
    text, lengths = result["text"], result["page_text_lengths"]
    if sum(lengths) + 2 * (len(lengths) - 1) != len(text):
        raise ValueError("Global stripping changed page offsets; instrument explicit page text before auditing")
    offset = total = 0
    ranges = []
    for page, length in enumerate(lengths, 1):
        count = len(_tokens(_normalize_whitespace(text[offset:offset + length])))
        ranges.append((total, total + count, page))
        total += count
        offset += length + 2
    return ranges


def all_boundary_changes(plain, pdfium, pdfium_result):
    before, after = _tokens(plain), _tokens(pdfium)
    pages = page_ranges(pdfium_result)
    changes = []
    for tag, a, b, c, d in SequenceMatcher(None, [t.value for t in before], [t.value for t in after], autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old, new = [t.value for t in before[a:b]], [t.value for t in after[c:d]]
        kind = "formula_order_or_other_change"
        if len(old) > 1 and len(new) == 1 and "".join(old) == new[0]:
            kind = "prose_join" if re.fullmatch(r"[а-яё]+", new[0]) else "mathematical_join"
        elif len(old) == 1 and len(new) > 1 and "".join(new) == old[0]:
            kind = "split"
        page = next((number for start, end, number in pages if start <= min(c, len(after) - 1) < end), None)
        start_a = before[a].start if a < len(before) else len(plain)
        end_a = before[b - 1].end if b > a else start_a
        start_b = after[c].start if c < len(after) else len(pdfium)
        end_b = after[d - 1].end if d > c else start_b
        changes.append({"kind": kind, "before": old, "after": new, "pdfium_page": page,
            "short_function_word_join": kind == "prose_join" and bool(set(old) & FUNCTION_WORDS),
            "before_token_span": [a, b], "after_token_span": [c, d],
            "plain_context": plain[max(0, start_a - 70):end_a + 90],
            "pdfium_context": pdfium[max(0, start_b - 70):end_b + 90]})
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unicode-only", action="store_true", help="Repeat the previous gate without primary-order support")
    args = parser.parse_args()
    evaluate = lambda target, plain, pdfium, bad_word=None: filtered_union(target, plain, pdfium, bad_word, require_order=not args.unicode_only)
    saved = json.loads((OUT / "comparison.json").read_text())
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    rows, totals = [], Counter()
    for entry in saved["cases"]:
        label = entry["label"]
        plain = json.loads((OUT / (label + "-plain.json")).read_text())["normalized_text"]
        other = json.loads((OUT / (label + "-pdfium.json")).read_text())
        pdfium = other["normalized_text"]
        case = next(c for c in cases if c["id"] == label)
        target = (DEV / case["input_path"]).read_text()
        changes = all_boundary_changes(plain, pdfium, other["result"])
        controls = [evaluate(control["text"], plain, pdfium) for control in entry["literal_controls"]]
        for control in controls:
            totals["literal_controls"] += 1
            totals["literal_controls_preserved"] += control["filtered_union"]["covered_words"] >= control["plain"]["covered_words"]
            totals["supplementary_matches_rejected_in_literal_controls"] += len(control["rejected_new_matches"])
        negative = evaluate(NEGATIVE, plain, pdfium)
        totals["independent_negative_pairs"] += 1
        totals["independent_negative_pairs_with_exact_highlight"] += negative["filtered_union"]["covered_words"] > 0
        row = {"label": label, "all_boundary_changes": changes, "counts": dict(Counter(x["kind"] for x in changes)),
            "same_pdf_raw_sha256": entry["raw"]["sha256"],
            "actual_case": evaluate(target, plain, pdfium), "literal_controls": controls,
            "independent_negative": negative}
        if label == "D06":
            row["expected_quote"] = evaluate(case["expected_evidence"][0]["target_quote"], plain, pdfium)
            row["math_adversarial_controls"] = [{**probe, "classification": "Source-unfaithful formula/page order confirmed by rendered original pages",
                "result": evaluate(probe["text"], plain, pdfium)} for probe in MATH_ADVERSARIAL]
        if label == "D16":
            visual = json.loads((OUT / "visual-control.json").read_text())
            row["visual_positive"] = evaluate(visual["text"], plain, pdfium)
            row["visual_adversarial_controls"] = [{**probe, "result": evaluate(probe["text"], plain, pdfium, probe["bad_word"])} for probe in BOUNDARY_PROBES]
        rows.append(row)
    result = {"schema_version": 1, "new_fetches": 0, "new_searches": 0, "production_changed": False,
        "policy": ("Preserve all plain matches; supplementary matches must have identical saved source slices and valid Unicode excerpts. "
                   + ("Require the same non-whitespace characters in the same order within primary plain. " if not args.unicode_only else "Primary character-order gate disabled for the previous baseline. ")
                   + "No character mappings."),
        "authored_negative_texts": 1, "negative_source_pairs": 3,
        "require_primary_character_order": not args.unicode_only,
        "plain_control_retention_is_a_construction_invariant": True, "integration_or_UI_validated": False,
        "totals": dict(totals), "cases": rows}
    dump(OUT / ("pdfium-unicode-only-union-audit.json" if args.unicode_only else "pdfium-filtered-union-audit.json"), result)
    print(json.dumps({"totals": result["totals"], "changes": {r["label"]: r["counts"] for r in rows},
        "D06": rows[0]["expected_quote"]["filtered_union"],
        "D16_visual_adversarial": [{"id": p["id"], "result": p["result"]["filtered_union"]} for r in rows for p in r.get("visual_adversarial_controls", [])]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
