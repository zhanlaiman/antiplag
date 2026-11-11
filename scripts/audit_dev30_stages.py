#!/usr/bin/env python3
"""Audit ONLY the 30 frozen DEV reports. Never search or read a held-out split.

The stage judgements below are an explicit human-readable diagnostic review,
not a change to the corpus's reviewed=false labels or an accuracy benchmark.
"""
from collections import Counter
from difflib import SequenceMatcher
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens
from plagiarism.web_evaluation import _report, _url
from scripts.compare_web_queries_offline import probe_evidence

DEV = ROOT / "data/private/web-evaluation/materials/dev-30"
REPORTS = ROOT / "data/private/web-evaluation/dev-live-30-v1"
OUT = REPORTS / "stage-audit.json"

# These are earliest CONFIRMED causes of a lexical deficit in the frozen run.
# A missed phrase is not inferred merely because a URL is absent.
DEFICITS = {
    ("D01", 0): ("url_retrieval", "The entire 10-word query equals the copied fragment; expected URL absent; no saved page contains its six-word exact run."),
    ("D02", 0): ("unresolved_query_boundary_or_retrieval", "Query has unrelated leading word 'особенность' plus all nine copied words. Expected URL absent; the frozen run cannot isolate boundary contamination from retrieval."),
    ("D05", 0): ("phrase_selection", "The only query is outside the copied fragment. All five returned URLs fail reading, separately from this phrase miss."),
    ("D06", 0): ("text_extraction_partial", "Saved urfu.ru PDF text splits the final word as 'ве личины'; seven of eight copied words remain exact. No gold URL returned."),
    ("D08", 0): ("phrase_selection", "The only query is outside the copied fragment."),
    ("D10", 0): ("highlight_partial", "Gold page and full source clause are saved. Inserted 'ещё' divides literal copy into runs of six and five words; the five-word tail misses the exact-six threshold."),
    ("D14", 0): ("url_retrieval", "A full ten-word internal query was issued; expected URL and copied passage are absent from saved evidence."),
    ("D20", 1): ("phrase_selection", "The single query covers the first insertion, and none covers the second source."),
}


def audit():
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Expected exactly D01–D30, split=dev.")
    rows, fragments = [], []
    source_pairs, found_pairs, read_pairs = set(), set(), set()
    for c in cases:
        raw = json.loads((REPORTS / (c["id"] + ".json")).read_text())
        checked = _report(raw, c)
        text = (DEV / c["input_path"]).read_text()
        if raw["text"] != text or sha256(text.encode()).hexdigest() != c["text_sha256"]:
            raise ValueError("Changed input " + c["id"])
        tok = _tokens(text)
        # Use the actual saved spans, never a newly recomputed query plan.
        qs = []
        for q in raw["web"]["query_spans"]:
            words = [t for t in tok if q["start"] <= t.start and t.end <= q["end"]]
            qs.append({**q, "query": '"' + ' '.join(t.value[:40] for t in words) + '"'})
        discovered = checked["discovered_urls"]
        attempted = [_url(u) for u in raw["web"]["attempted_urls"]]
        read = {_url(s["url"]) for s in raw["sources"]}
        own_url = c.get("published_target", {}).get("url")
        own_url = _url(own_url) if own_url else None
        exact = [m for m in checked["matches"] if m["url"] != own_url]
        evidence_rows = []
        for index, e in enumerate(c["expected_evidence"]):
            source_snapshot = (DEV / e["source_snapshot_path"]).read_text()
            if sha256(source_snapshot.encode()).hexdigest() != e["source_text_sha256"] or source_snapshot[slice(*e["source_span"])] != e["source_quote"]:
                raise ValueError("Changed source " + c["id"])
            if text[slice(*e["target_span"])] != e["target_quote"]:
                raise ValueError("Changed target quote " + c["id"])
            wanted = [t for t in tok if e["target_span"][0] <= t.start and t.end <= e["target_span"][1]]
            aligned = SequenceMatcher(None, [t.value for t in wanted], [t.value for t in _tokens(e["source_quote"])], autojunk=False)
            copied = [wanted[i] for b in aligned.get_matching_blocks() for i in range(b.a, b.a + b.size)]
            covered = [t for t in copied if any(m["target_span"][0] <= t.start and t.end <= m["target_span"][1] for m in exact)]
            urls = {_url(u) for u in e["source_urls"]}
            pair = (c["id"], e["source_id"])
            source_pairs.add(pair)
            if urls & discovered:
                found_pairs.add(pair)
            if urls & read:
                read_pairs.add(pair)
            stage, reason = DEFICITS.get((c["id"], index), ("fully_highlighted", "All copied target words have grounded literal evidence; URL identity is counted separately."))
            if not e["needs_lexical_highlight"]:
                stage, reason = "semantic_out_of_scope", "The lexical web matcher does not establish semantic source attribution for translation or paraphrase."
            entry = {"id": c["id"], "fragment_index": index, "lexical_expected": e["needs_lexical_highlight"],
                     "target_quote": e["target_quote"], "source_quote": e["source_quote"],
                     "query_probe": probe_evidence(qs, tok, e, [t.value for t in _tokens(e["source_quote"])]),
                     "expected_source_url_discovered": bool(urls & discovered), "expected_source_url_read": bool(urls & read),
                     "expected_url_attempt_rank": min((i + 1 for i, u in enumerate(attempted) if u in urls), default=None),
                     "copied_words": len(copied), "copied_words_highlighted_any_nonself_source": len(covered),
                     "stage": stage, "reason": reason}
            if c["id"] == "D12":
                entry["source_attribution_warning"] = "The literal evidence is the target Goncharov essay and its FEB mirror. The expected Griboyedov source was neither discovered nor read. This is NOT expected-source success."
            fragments.append(entry)
            evidence_rows.append(entry)
        rows.append({"id": c["id"], "report_id": raw["id"], "scenario": c["scenario"],
                     "synthetic": c["synthetic"], "queries": qs, "pages_read": len(raw["sources"]),
                     "web_status": raw["web"]["status"], "uniqueness_percent": raw["stats"]["uniqueness_percent"],
                     "errors": dict(Counter(e["code"] for e in raw["web"]["errors"])),
                     "invalid_grounding": checked["invalid_highlights"], "evidence": evidence_rows})
    lexical = [e for e in fragments if e["lexical_expected"]]
    # Manual review of BOTH sides of every unexpected highlight in inspection.json.
    reviews = [
        {"id": "D12", "decision": "valid_target_republication", "source_pair_highlights": 1,
         "detail": "FEB source excerpt equals the entire Goncharov target. It is a real match outside the annotated embedded quote, not a false highlight. The exact self URL is excluded separately. It does not find Griboyedov."},
        {"id": "D13", "decision": "unsupported_similar_extension", "source_pair_highlights": 3, "distinct_target_regions": 1,
         "unsupported_words": 9,
         "detail": "All three source excerpts consist solely of the valid 16-word quotation. Their similar target spans also include the nine-word author attribution prefix, which the source excerpts do not support. Three exact quoted matches are correct and must remain."},
    ]
    return {"schema_version": 1, "scope": "diagnostic_review_of_30_dev_only", "network_calls": 0,
            "held_out_content_read": False, "corpus_review_labels_changed": False,
            "denominator_notes": {"source": "Unique (document, expected source_id), so D09's two fragments share one expected source.",
                                  "highlight": "Literal copied-word coverage, using grounded exact matches from any nonself URL. A mirror of the target can cover words without finding the originating expected source.",
                                  "deficit": "Earliest confirmed lexical cause; D02 remains unresolved. Full expected-source URL misses are measured separately.",
                                  "phrase": "Whole query is both inside target evidence span and contiguous in the source quote. This is not URL retrieval."},
            "summary": {"documents": 30, "reports": len(rows), "queries_attempted": sum(len(r["queries"]) for r in rows),
                        "expected_source_pairs": len(source_pairs), "expected_source_pairs_discovered": len(found_pairs),
                        "expected_source_pairs_read": len(read_pairs), "expected_fragment_source_pairs": len(fragments),
                        "expected_fragment_source_pairs_discovered": sum(e["expected_source_url_discovered"] for e in fragments),
                        "lexical_fragments": len(lexical), "lexical_documents": len({e["id"] for e in lexical}),
                        "lexical_fragments_full_any_source": sum(e["copied_words"] == e["copied_words_highlighted_any_nonself_source"] for e in lexical),
                        "lexical_fragments_partial_any_source": sum(0 < e["copied_words_highlighted_any_nonself_source"] < e["copied_words"] for e in lexical),
                        "lexical_fragments_zero": sum(e["copied_words_highlighted_any_nonself_source"] == 0 for e in lexical),
                        "lexical_fragments_whole_query_inside": sum(e["query_probe"]["whole_query_in_both_spans"] for e in lexical),
                        "lexical_deficits_by_confirmed_stage": dict(Counter(e["stage"] for e in lexical if e["stage"] != "fully_highlighted")),
                        "semantic_fragments_excluded": len(fragments) - len(lexical), "controls": 3,
                        "documents_without_read_pages": sum(r["pages_read"] == 0 for r in rows),
                        "confirmed_false_similar_source_pairs": 3, "confirmed_false_similar_target_regions": 1,
                        "invalid_grounded_matches": sum(len(r["invalid_grounding"]) for r in rows)},
            "unexpected_highlight_review": reviews, "cases": rows}


if __name__ == "__main__":
    result = audit()
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    OUT.chmod(0o600)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
