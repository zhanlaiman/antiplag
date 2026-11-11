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
from plagiarism.web_search import plan_queries


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
        queries = plan_queries(text, strategy=raw["web"]["query_strategy"])
        self_url = case.get("published_target", {}).get("url")
        self_url = _url(self_url) if self_url else None
        exact = [m for m in raw["matches"] if m["kind"] == "exact" and _url(m["source_url"]) != self_url]
        read = {_url(s["url"]) for s in raw["sources"]}
        discovered = report["discovered_urls"] or set()
        evidence = []
        for gold in case["expected_evidence"]:
            start, end = gold["target_span"]
            wanted = [t for t in tokens if start <= t.start and t.end <= end]
            covered = [t for t in wanted if any(m["start"] <= t.start and t.end <= m["end"] for m in exact)]
            urls = {_url(u) for u in gold["source_urls"]}
            literal = gold["needs_lexical_highlight"]
            original_words = [t.value for t in _tokens(gold["source_quote"])]
            aligned = SequenceMatcher(None, [t.value for t in wanted], original_words, autojunk=False)
            copied = [wanted[i] for block in aligned.get_matching_blocks() for i in range(block.a, block.a + block.size)]
            copied_covered = [t for t in copied if any(m["start"] <= t.start and t.end <= m["end"] for m in exact)]
            probe = probe_evidence(queries, tokens, gold, [t.value for t in _tokens(gold["source_quote"])])
            if not literal:
                stage = "semantic_check_pending"
            elif copied and len(copied_covered) == len(copied):
                stage = "highlighted"
            elif covered:
                stage = "partial_highlight"
            elif urls & read:
                stage = "matching"
            elif urls & discovered:
                stage = "fetch_or_load_limit"
            elif probe["shared_consecutive_words"] < 6:
                stage = "phrase_selection"
            else:
                stage = "search_results"
            evidence.append({"lexical": literal, "expected_words": len(wanted), "covered_words": len(covered),
                             "literal_expected_words": len(copied), "literal_covered_words": len(copied_covered),
                             "expected_url_discovered": bool(urls & discovered), "expected_url_read": bool(urls & read),
                             "stage": stage, "query_shared_words": probe["shared_consecutive_words"]})
        unexpected = []
        for match in raw["matches"]:
            if self_url and _url(match["source_url"]) == self_url:
                continue
            words = [t for t in tokens if match["start"] <= t.start and t.end <= match["end"]]
            outside = [t for t in words if not any(e["target_span"][0] <= t.start and t.end <= e["target_span"][1] for e in case["expected_evidence"])]
            if len(outside) >= 6:
                unexpected.append({k: match.get(k) for k in ("kind", "source_url", "excerpt", "source_excerpt")})
        rows.append({"id": case["id"], "status": "saved", "scenario": case["scenario"], "report_id": raw["id"],
                     "queries": raw["web"]["queries_attempted"], "pages": raw["web"]["sources_checked"],
                     "elapsed": raw["web"]["elapsed_seconds"], "coverage": raw["web"]["status"],
                     "evidence": evidence, "invalid_highlights": report["invalid_highlights"],
                     "unexpected_matches": unexpected, "control": not bool(case["expected_evidence"]),
                     "self_matches": sum(_url(m["source_url"]) == self_url for m in raw["matches"]) if self_url else 0,
                     "errors": dict(Counter(e["code"] for e in raw["web"]["errors"]))})
    return {"schema_version": 1, "reviewed_cases": sum(c["reviewed"] for c in cases), "cases": rows}


def table(bundle):
    rows = ["| Случай | Тип | Подсвечено слов участков | Причина неполной находки | Запросов | Страниц | Неожиданных подсветок |",
            "| --- | --- | --- | --- | ---: | ---: | ---: |"]
    for c in bundle["cases"]:
        if c["status"] != "saved":
            rows.append(f"| {c['id']} | Ожидается | — | — | — | — | — |")
            continue
        words = " + ".join(
            f"{e['literal_covered_words']}/{e['literal_expected_words']}" if e["lexical"] else "Смысловая сверка"
            for e in c["evidence"]) or "—"
        labels = {"highlighted": "Выделено", "partial_highlight": "Неполная подсветка", "matching": "Сопоставление",
                  "fetch_or_load_limit": "Чтение или лимит страниц", "phrase_selection": "Выбор фразы",
                  "search_results": "Выдача", "semantic_check_pending": "Смысловая сверка"}
        stages = ", ".join(dict.fromkeys(labels[e["stage"]] for e in c["evidence"])) or "Контроль"
        rows.append(f"| [{c['id']}](http://127.0.0.1:8766/#check/{c['report_id']}) | {c['scenario']} | {words} | {stages} | {c['queries']} | {c['pages']} | {len(c['unexpected_matches'])} |")
    return "\n".join(rows) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", type=Path, default=ROOT / "data/private/web-evaluation/materials/dev-30")
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--table", type=Path)
    args = parser.parse_args()
    bundle = inspect(args.dev, args.reports)
    args.output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2))
    if args.table:
        args.table.write_text(table(bundle))
    print("Saved", sum(c["status"] == "saved" for c in bundle["cases"]), "of 30; review labels unchanged.")


if __name__ == "__main__":
    main()
