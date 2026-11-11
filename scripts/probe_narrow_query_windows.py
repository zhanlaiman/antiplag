#!/usr/bin/env python3
"""Local DEV-only comparison of six-word versus ten-word extra query windows.

The preserved citation plan and distinctive scoring rules are unchanged. No
provider, fetcher, gold URL, model, or held-out material enters either planner.
"""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens
import plagiarism.web_search as web
from scripts.compare_web_queries_offline import probe_evidence
from scripts.probe_query_blindspots import artificial, compose, digest, dump, load_sources, HOSTS, DONORS
from scripts.probe_short_query_selection import dense_six_plan

DEV = ROOT / "data/private/web-evaluation/materials/dev-30"
OUT = ROOT / "data/private/web-evaluation/query-narrow-windows-v1"


def dense_width_plan(text, width=6):
    planned = web.plan_queries(text, strategy=web.CITATION_STRATEGY)
    if len(planned) == web.MAX_QUERIES:
        return planned
    tokens = _tokens(text)
    width = min(width, len(tokens))
    windows = web._distinctive_windows(text, tokens, width, web.MAX_QUERIES, ranked=True)
    if windows is None:
        starts = [round(i * (len(tokens) - width) / (web.MAX_QUERIES - 1)) for i in range(web.MAX_QUERIES)]
        windows = [(start, start + width) for start in starts]
    seen = {q["query"] for q in planned}
    for start, end in windows:
        window = tokens[start:end]
        query = '"' + " ".join(t.value[:40] for t in window) + '"'
        if len(query) > 390:
            query = query[:389].rsplit(" ", 1)[0] + '"'
            window = window[:len(query[1:-1].split())]
        if query in seen:
            continue
        planned.append({"query": query, "start": window[0].start, "end": window[-1].end})
        seen.add(query)
        if len(planned) == web.MAX_QUERIES:
            break
    return planned


def plans_for(text):
    baseline = dense_six_plan(text)
    # Assert the reproduced ten-word helper remains the current dense planner.
    if baseline != web.plan_queries(text, strategy=web.DENSE_STRATEGY):
        raise ValueError("Ten-word baseline no longer equals the production candidate")
    if dense_width_plan(text, 10) != baseline:
        raise ValueError("Width-only reproduction changed the baseline")
    narrow = dense_width_plan(text, 6)
    prefix = web.plan_queries(text, strategy=web.CITATION_STRATEGY)
    if narrow[:len(prefix)] != prefix or len(narrow) > 6:
        raise ValueError("Preserved request prefix or six-request ceiling changed")
    if len(narrow) < len(baseline):
        raise ValueError("Same-count control cannot retain the baseline's actual request count")
    return {"dense10": baseline, "dense6": narrow, "dense6_same_actual_count": narrow[:len(baseline)]}


def evaluate_dev():
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only D01–D30 DEV is supported")
    rows, counts = [], {name: Counter() for name in ("dense10", "dense6", "dense6_same_actual_count")}
    for c in cases:
        text = (DEV / c["input_path"]).read_text()
        if sha256(text.encode()).hexdigest() != c["text_sha256"]:
            raise ValueError("Changed DEV input")
        plans = plans_for(text)
        evidence = []
        for index, gold in enumerate(c["expected_evidence"]):
            score = {name: probe_evidence(p, _tokens(text), gold, [t.value for t in _tokens(gold["source_quote"])])
                     for name, p in plans.items()}
            evidence.append({"fragment_index": index, "lexical": gold["needs_lexical_highlight"], "scores": score})
            for name, s in score.items():
                if gold["needs_lexical_highlight"]:
                    counts[name]["lexical_fragments"] += 1
                    counts[name]["whole_phrase_in_target_and_source"] += s["whole_query_in_both_spans"]
                    counts[name]["at_least_six_shared_words"] += s["shared_consecutive_words"] >= 6
                else:
                    counts[name]["semantic_fragments_excluded"] += 1
        for name, p in plans.items():
            counts[name]["documents"] += 1
            counts[name]["actual_planned_queries"] += len(p)
        rows.append({"id": c["id"], "plans": plans, "evidence": evidence})
    changes, shared_changes = {}, {}
    for name in ("dense6", "dense6_same_actual_count"):
        changed, totals = [], Counter()
        for r in rows:
            for e in r["evidence"]:
                if not e["lexical"]:
                    continue
                a = e["scores"]["dense10"]["whole_query_in_both_spans"]
                b = e["scores"][name]["whole_query_in_both_spans"]
                label = "retained" if a and b else "gained" if b else "lost" if a else "missed_both"
                totals[label] += 1
                if a != b:
                    changed.append({"id": r["id"], "fragment_index": e["fragment_index"], "transition": label})
        changes[name] = {"counts": dict(totals), "changes": changed}
        shared, totals = [], Counter()
        for r in rows:
            for e in r["evidence"]:
                if not e["lexical"]:
                    continue
                a = e["scores"]["dense10"]["shared_consecutive_words"] >= 6
                b = e["scores"][name]["shared_consecutive_words"] >= 6
                label = "retained" if a and b else "gained" if b else "lost" if a else "missed_both"
                totals[label] += 1
                if a != b:
                    shared.append({"id": r["id"], "fragment_index": e["fragment_index"], "transition": label})
        shared_changes[name] = {"counts": dict(totals), "changes": shared}
    result = {"schema_version": 1, "data_kind": "known_dev", "live_queries": 0,
              "correct_url_found": None, "confirmed_highlight": None,
              "counts": {k: dict(v) for k, v in counts.items()}, "transitions": changes,
              "six_shared_word_transitions": shared_changes, "cases": rows}
    dump(OUT / "dev-comparison.json", result)
    lost = next((c for c in shared_changes["dense6"]["changes"] if c["transition"] == "lost"), None)
    if lost:
        case = next(c for c in cases if c["id"] == lost["id"])
        row = next(r for r in rows if r["id"] == lost["id"])
        dump(OUT / "dev-shared-loss-example.json", {**lost, "text": (DEV / case["input_path"]).read_text(),
             "plans": row["plans"], "evidence": row["evidence"]})
    return {k: v for k, v in result.items() if k != "cases"}


def evaluate_short(pages):
    rows, counts, actual, losses = [], Counter(), Counter(), {}
    for host_id in HOSTS:
        tokens = _tokens(pages[host_id]["text"])
        host = pages[host_id]["text"][tokens[80].start:tokens[114].end]
        for donor_id in DONORS:
            for length in (10, 20, 40):
                for position, index in (("beginning", 0), ("middle", 17), ("end", 35)):
                    for layout in ("inline", "paragraph"):
                        text, evidence = compose(host, [(index, position, pages[donor_id], length)], layout)
                        plans = plans_for(text)
                        scores = {name: any(evidence[0]["span"][0] <= q["start"] and q["end"] <= evidence[0]["span"][1]
                                            for q in p) for name, p in plans.items()}
                        row = {"id": f"N{len(rows) + 1:04d}", "synthetic": True, "host": host_id,
                               "layout": layout, "evidence": evidence, "text_sha256": digest(text),
                               "scores": scores, "plans": plans}
                        rows.append(row)
                        for name, p in plans.items():
                            actual[name] += len(p)
                            counts[name] += scores[name]
                            if scores["dense10"] and not scores[name] and name not in losses:
                                losses[name] = {**row, "text": text}
    transitions = {}
    for name in ("dense6", "dense6_same_actual_count"):
        transitions[name] = dict(Counter("retained" if r["scores"]["dense10"] and r["scores"][name]
                                         else "gained" if r["scores"][name] else "lost" if r["scores"]["dense10"]
                                         else "missed_both" for r in rows))
    result = {"schema_version": 1, "synthetic": True, "documents": len(rows), "insertions": len(rows),
              "live_queries": 0, "live_fetches": 0, "correct_url_found": None, "confirmed_highlight": None,
              "whole_phrase_inside": dict(counts), "actual_planned_queries": dict(actual),
              "transitions": transitions, "cases": rows}
    dump(OUT / "short-comparison.json", result)
    for name, loss in losses.items():
        dump(OUT / (name + "-short-loss.json"), loss)
    return {k: v for k, v in result.items() if k != "cases"}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    OUT.chmod(0o700)
    pages = load_sources()
    dev = evaluate_dev()
    short = evaluate_short(pages)
    # These 348 long documents already use six citation queries. The prefix
    # preservation rule makes both widths identical, but recompute to verify.
    large = artificial(pages, OUT, planners={
        "dense10": lambda text: plans_for(text)["dense10"],
        "dense6": lambda text: plans_for(text)["dense6"]})
    summary = {"dev": dev, "short_artificial": short, "long_artificial": large}
    dump(OUT / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
