"""Offline v6/v7 query comparison; gold labels enter scoring only, never planning."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

from plagiarism.matching import MIN_WORDS, _tokens
from plagiarism.query_clauses import CLAUSE_STRATEGY
from plagiarism.query_paragraphs import SHORT_PARAGRAPH_STRATEGY
from plagiarism.web_search import plan_queries
from scripts.compare_web_queries_offline import probe_evidence


def compare(materials):
    materials = Path(materials).resolve()
    cases = json.loads((materials / "key.json").read_text())["cases"]
    counts, shared, rows = Counter(), Counter(), []
    for case in cases:
        path = (materials / case["input_path"]).resolve()
        if not path.is_relative_to(materials):
            raise ValueError("Input path leaves materials directory")
        text = path.read_text()
        if sha256(text.encode()).hexdigest() != case["text_sha256"]:
            raise ValueError("Input hash changed")
        plans = {"v6": plan_queries(text, strategy=SHORT_PARAGRAPH_STRATEGY),
                 "v7": plan_queries(text, strategy=CLAUSE_STRATEGY)}
        assert len(plans["v6"]) == len(plans["v7"]) <= 6
        assert sum(a != b for a, b in zip(plans["v6"], plans["v7"])) <= 1
        counts["documents"] += 1
        counts["changed_documents"] += plans["v6"] != plans["v7"]
        evidence = []
        for gold in case["expected_evidence"]:
            if not gold["needs_lexical_highlight"]:
                continue
            scores = {name: probe_evidence(plan, _tokens(text), gold,
                       [word.value for word in _tokens(gold["source_quote"])]) for name, plan in plans.items()}
            def transition(a, b):
                return "retained" if a and b else "gained" if b else "lost" if a else "missed_both"
            counts[transition(*(scores[n]["whole_query_in_both_spans"] for n in ("v6", "v7")))] += 1
            shared[transition(*(scores[n]["shared_consecutive_words"] >= MIN_WORDS for n in ("v6", "v7")))] += 1
            evidence.append(scores)
        rows.append({"id": case["id"], "text_sha256": case["text_sha256"], "plans": plans, "evidence": evidence})
    return {"live_searches": 0, "live_fetches": 0, "summary": dict(counts),
            "shared_six_word_transitions": dict(shared), "cases": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.materials)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    args.output.chmod(0o600)
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
