#!/usr/bin/env python3
"""Inspect saved DEV reports without network calls or changing review decisions."""
import argparse
from collections import Counter
from hashlib import sha256
from difflib import SequenceMatcher
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens
from plagiarism.web_evaluation import _report, _url
from scripts.compare_web_queries_offline import probe_evidence
from scripts.audit_dev30_stages import DEFICITS


D13_REVIEWED_ATTRIBUTION = tuple(t.value for t in _tokens(
    "Приведём подсказку из статьи «Шахматные часы» русской Википедии:"))


def copied_target_tokens(tokens, gold):
    """Scoring only: exclude target insertions absent from the source quote.

    In D10, the extra word 'ещё' is inside the annotated target span but was
    not copied. Full copied coverage is therefore 11/11, not 11/12.
    """
    start, end = gold["target_span"]
    wanted = [t for t in tokens if start <= t.start and t.end <= end]
    original = [t.value for t in _tokens(gold["source_quote"])]
    aligned = SequenceMatcher(None, [t.value for t in wanted], original, autojunk=False)
    return [wanted[i] for block in aligned.get_matching_blocks() for i in range(block.a, block.a + block.size)]


def reviewed_similar_extension(case, match, exact, text):
    """Recognize the reviewed attribution error only with the same pair evidence.

    A new source/excerpt is not false merely because its document ID is D13.
    The exact counterpart must consume the candidate's ENTIRE source span;
    the only extra target words must be the reviewed attribution prefix.
    """
    if case["id"] != "D13" or match["kind"] != "similar":
        return False
    source_words = tuple(t.value for t in _tokens(match["source_excerpt"]))
    for gold in case["expected_evidence"]:
        start, end = gold["target_span"]
        if not gold["needs_lexical_highlight"] or match["start"] >= start or match["end"] != end:
            continue
        prefix_words = tuple(t.value for t in _tokens(text[match["start"]:start]))
        if prefix_words != D13_REVIEWED_ATTRIBUTION:
            continue
        if source_words != tuple(t.value for t in _tokens(gold["source_quote"])):
            continue
        if any(source_words[i:i + len(prefix_words)] == prefix_words
               for i in range(len(source_words) - len(prefix_words) + 1)):
            continue
        if any(m["source_id"] == match["source_id"] and _url(m["source_url"]) == _url(match["source_url"])
               and [m["start"], m["end"]] == [start, end]
               and [m["source_start"], m["source_end"]] == [match["source_start"], match["source_end"]]
               for m in exact):
            return True
    return False


def inspect(dev, reports):
    cases = json.loads((dev / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only the 30 DEV cases are supported.")
    rows = []
    for case in cases:
        path = reports / (case["id"] + ".json")
        if not path.exists():
            rows.append({"id": case["id"], "status": "pending"})
            continue
        raw = json.loads(path.read_text())
        report = _report(raw, case)
        text = (dev / case["input_path"]).read_text()
        if sha256(text.encode()).hexdigest() != case["text_sha256"]:
            raise ValueError("Changed DEV input " + case["id"])
        tokens = _tokens(text)
        # The planner may have changed since this report was created. Rebuild
        # only what the report records as actually sent, from its frozen spans.
        queries = []
        for q in raw["web"]["query_spans"]:
            words = [t for t in tokens if q["start"] <= t.start and t.end <= q["end"]]
            queries.append({**q, "query": '"' + ' '.join(t.value[:40] for t in words) + '"'})
        self_url = case.get("published_target", {}).get("url")
        self_url = _url(self_url) if self_url else None
        grounded = {(m["url"], *m["target_span"], *m["source_span"]) for m in report["matches"]}
        invalid_indices = {m["match_index"] for m in report["invalid_highlights"]}
        exact = [m for index, m in enumerate(raw["matches"]) if index not in invalid_indices
                 and m["kind"] == "exact" and _url(m["source_url"]) != self_url
                 and (_url(m["source_url"]), m["start"], m["end"], m["source_start"], m["source_end"]) in grounded]
        read = {_url(s["url"]) for s in raw["sources"]}
        discovered = report["discovered_urls"] or set()
        evidence = []
        for index, gold in enumerate(case["expected_evidence"]):
            start, end = gold["target_span"]
            wanted = [t for t in tokens if start <= t.start and t.end <= end]
            covered = [t for t in wanted if any(m["start"] <= t.start and t.end <= m["end"] for m in exact)]
            urls = {_url(u) for u in gold["source_urls"]}
            literal = gold["needs_lexical_highlight"]
            copied = copied_target_tokens(tokens, gold)
            copied_covered = [t for t in copied if any(m["start"] <= t.start and t.end <= m["end"] for m in exact)]
            source_faithful = [t for t in copied if any(m["start"] <= t.start and t.end <= m["end"]
                                                       and _url(m["source_url"]) in urls for m in exact)]
            probe = probe_evidence(queries, tokens, gold, [t.value for t in _tokens(gold["source_quote"])])
            if not literal:
                stage = "semantic_check_pending"
            elif copied and len(copied_covered) == len(copied):
                stage = "highlighted"
            elif copied_covered:
                stage = "partial_highlight"
            elif urls & read:
                stage = "matching"
            elif urls & discovered:
                stage = "fetch_or_load_limit"
            elif probe["shared_consecutive_words"] == 0:
                stage = "phrase_selection"
            elif not probe["whole_query_in_both_spans"]:
                stage = "unresolved_query_boundary_or_retrieval"
            else:
                stage = "search_results"
            known = DEFICITS.get((case["id"], index))
            # Reviewed partial extraction/matching causes apply to a replay
            # of the same saved evidence, not an arbitrary new search result.
            cause = stage
            reason = None
            if known and stage == "partial_highlight" and (raw.get("offline_replay") or reports.name == "dev-live-30-v1"):
                cause, reason = known
            if stage == "search_results":
                cause = "url_retrieval"
            if stage == "fetch_or_load_limit":
                attempted = {_url(u) for u in raw["web"]["attempted_urls"]}
                cause = "source_fetch_or_extraction" if urls & attempted else "page_selection"
            evidence.append({"lexical": literal, "expected_words": len(wanted), "covered_words": len(covered),
                             "literal_expected_words": len(copied), "literal_covered_words": len(copied_covered),
                             "literal_covered_expected_source_words": len(source_faithful), "source_id": gold["source_id"],
                             "expected_url_discovered": bool(urls & discovered), "expected_url_read": bool(urls & read),
                             "stage": stage, "earliest_confirmed_cause": cause, "review_reason": reason,
                             "query_shared_words": probe["shared_consecutive_words"],
                             "whole_query_in_both_spans": probe["whole_query_in_both_spans"]})
        unexpected = []
        for index, match in enumerate(raw["matches"]):
            if self_url and _url(match["source_url"]) == self_url:
                continue
            words = [t for t in tokens if match["start"] <= t.start and t.end <= match["end"]]
            outside = [t for t in words if not any(e["target_span"][0] <= t.start and t.end <= e["target_span"][1] for e in case["expected_evidence"])]
            if len(outside) >= 6:
                review = ("invalid_grounding" if index in invalid_indices
                          else "unsupported_similar_extension" if reviewed_similar_extension(case, match, exact, text)
                          else "valid_literal_match_outside_annotation" if match["kind"] == "exact" else "needs_pair_review")
                unexpected.append({**{k: match.get(k) for k in ("kind", "source_url", "excerpt", "source_excerpt")},
                                   "review": review, "outside_words": len(outside),
                                   "target_span": [match["start"], match["end"]]})
        rows.append({"id": case["id"], "status": "saved", "scenario": case["scenario"], "report_id": raw["id"],
                     "queries": raw["web"]["queries_attempted"], "pages": raw["web"]["sources_checked"],
                     "network_queries_this_run": 0 if raw.get("offline_replay") else raw["web"]["queries_attempted"],
                     "data_kind": "saved_page_replay" if raw.get("offline_replay") else "fresh_internet_search",
                     "elapsed": raw["web"]["elapsed_seconds"], "coverage": raw["web"]["status"],
                     "evidence": evidence, "invalid_highlights": report["invalid_highlights"],
                     "unexpected_matches": unexpected, "control": not bool(case["expected_evidence"]),
                     "self_matches": sum(_url(m["source_url"]) == self_url for m in raw["matches"]) if self_url else 0,
                     "errors": dict(Counter(e["code"] for e in raw["web"]["errors"]))})
    return {"schema_version": 2, "reviewed_cases": sum(c["reviewed"] for c in cases), "summary": summarize(rows), "cases": rows}


def summarize(rows):
    saved = [r for r in rows if r["status"] == "saved"]
    lexical = [e for r in saved for e in r["evidence"] if e["lexical"]]
    evidence = [e for r in saved for e in r["evidence"]]
    pairs = {(r["id"], e["source_id"]) for r in saved for e in r["evidence"]}
    discovered_pairs = {(r["id"], e["source_id"]) for r in saved for e in r["evidence"] if e["expected_url_discovered"]}
    read_pairs = {(r["id"], e["source_id"]) for r in saved for e in r["evidence"] if e["expected_url_read"]}
    false = [(r["id"], u) for r in saved for u in r["unexpected_matches"] if u["review"] == "unsupported_similar_extension"]
    summary = {"saved_documents": len(saved), "lexical_documents": sum(any(e["lexical"] for e in r["evidence"]) for r in saved),
               "lexical_fragments": len(lexical), "semantic_fragments_excluded": sum(not e["lexical"] for e in evidence),
               "expected_source_pairs": len(pairs), "expected_source_pairs_discovered": len(discovered_pairs),
               "expected_source_pairs_read": len(read_pairs),
               "whole_query_in_both_spans": sum(e["whole_query_in_both_spans"] for e in lexical),
               "fully_highlighted_any_source": sum(e["literal_covered_words"] == e["literal_expected_words"] for e in lexical),
               "fully_highlighted_expected_source": sum(e["literal_covered_expected_source_words"] == e["literal_expected_words"] for e in lexical),
               "copied_words": sum(e["literal_expected_words"] for e in lexical),
               "copied_words_highlighted_any_source": sum(e["literal_covered_words"] for e in lexical),
               "copied_words_highlighted_expected_source": sum(e["literal_covered_expected_source_words"] for e in lexical),
               "lexical_deficit_causes": dict(Counter(e["earliest_confirmed_cause"] for e in lexical if e["stage"] != "highlighted")),
               "confirmed_false_similar_source_pairs": len(false),
               "confirmed_false_similar_target_regions": len({(cid, *u["target_span"]) for cid, u in false}),
               "unexpected_needing_review": sum(u["review"] == "needs_pair_review" for r in saved for u in r["unexpected_matches"]),
               "invalid_grounded_matches": sum(len(r["invalid_highlights"]) for r in saved),
               "documents_without_read_pages": sum(r["pages"] == 0 for r in saved),
               "network_queries_this_run": sum(r["network_queries_this_run"] for r in saved)}
    return summary


def compare(before, after):
    """Compare only IDs actually saved on both sides; never fill missing cases."""
    left = {r["id"]: r for r in before["cases"] if r["status"] == "saved"}
    right = {r["id"]: r for r in after["cases"] if r["status"] == "saved"}
    ids = sorted(left.keys() & right.keys())
    gained, lost = [], []
    for cid in ids:
        for index, (a, b) in enumerate(zip(left[cid]["evidence"], right[cid]["evidence"])):
            if not a["lexical"]:
                continue
            delta = b["literal_covered_words"] - a["literal_covered_words"]
            if delta:
                (gained if delta > 0 else lost).append({"id": cid, "fragment_index": index, "copied_word_delta": delta})
    return {"compared_case_ids": ids, "before": summarize([left[cid] for cid in ids]),
            "after": summarize([right[cid] for cid in ids]), "gained_fragments": gained, "lost_fragments": lost,
            "before_data_kinds": sorted({left[cid]["data_kind"] for cid in ids}),
            "after_data_kinds": sorted({right[cid]["data_kind"] for cid in ids})}


def table(bundle):
    rows = ["Буквальное покрытие: любой подтверждённый URL / ожидаемый исходный URL. Источник и подсветка считаются отдельно.", "",
            "| Случай | Тип | Подсвечено слов: любые / исходные | Причина неполной находки | Запросов | Страниц | Неожиданных подсветок |",
            "| --- | --- | --- | --- | ---: | ---: | ---: |"]
    comparison = bundle.get("comparison")
    if comparison:
        before, after = comparison["before"], comparison["after"]
        rows[:0] = [f"Сравнимых документов: {len(comparison['compared_case_ids'])}. "
                    f"Полная подсветка любым источником: {before['fully_highlighted_any_source']}/{before['lexical_fragments']} → "
                    f"{after['fully_highlighted_any_source']}/{after['lexical_fragments']}; "
                    f"исходным URL: {before['fully_highlighted_expected_source']}/{before['lexical_fragments']} → "
                    f"{after['fully_highlighted_expected_source']}/{after['lexical_fragments']}. "
                    f"Подтверждённых ложных similar-пар: {before['confirmed_false_similar_source_pairs']} → "
                    f"{after['confirmed_false_similar_source_pairs']}.", ""]
    for c in bundle["cases"]:
        if c["status"] != "saved":
            rows.append(f"| {c['id']} | Ожидается | — | — | — | — | — |")
            continue
        words = " + ".join(
            f"{e['literal_covered_words']}/{e['literal_expected_words']} / {e['literal_covered_expected_source_words']}/{e['literal_expected_words']}" if e["lexical"] else "Смысловая сверка"
            for e in c["evidence"]) or "—"
        labels = {"highlighted": "Выделено", "partial_highlight": "Неполная подсветка", "matching": "Сопоставление",
                  "fetch_or_load_limit": "Чтение или лимит страниц", "phrase_selection": "Выбор фразы",
                  "search_results": "Выдача", "semantic_check_pending": "Смысловая сверка",
                  "unresolved_query_boundary_or_retrieval": "Граница фразы или выдача: не изолировано"}
        stages = ", ".join(dict.fromkeys(labels[e["stage"]] for e in c["evidence"])) or "Контроль"
        rows.append(f"| [{c['id']}](http://127.0.0.1:8766/#check/{c['report_id']}) | {c['scenario']} | {words} | {stages} | {c['queries']} | {c['pages']} | {len(c['unexpected_matches'])} |")
    return "\n".join(rows) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", type=Path, default=ROOT / "data/private/web-evaluation/materials/dev-30")
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--table", type=Path)
    parser.add_argument("--baseline", type=Path, help="Compare saved common IDs with reports from this directory.")
    args = parser.parse_args()
    bundle = inspect(args.dev, args.reports)
    if args.baseline:
        bundle["comparison"] = compare(inspect(args.dev, args.baseline), bundle)
    args.output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2))
    args.output.chmod(0o600)
    if args.table:
        args.table.write_text(table(bundle))
        args.table.chmod(0o600)
    print("Saved", sum(c["status"] == "saved" for c in bundle["cases"]), "of 30; review labels unchanged.")


if __name__ == "__main__":
    main()
