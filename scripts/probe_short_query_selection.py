#!/usr/bin/env python3
"""Compare two separate short-text query hypotheses locally on DEV only."""
from collections import Counter
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens, MIN_WORDS
import plagiarism.web_search as web
from scripts.compare_web_queries_offline import probe_evidence

DEV = ROOT / "data/private/web-evaluation/materials/dev-30"
OUT = ROOT / "data/private/web-evaluation/query-blindspots-v1"


def fixed_width_windows(text, tokens, width, count, fixed_width=False):
    """Keep ranking rules, but compare total information at one fixed width.

    The original divides by each candidate's own length, so a short sentence
    may beat a longer phrase despite containing less total information.
    """
    words = [t.value for t in tokens]
    counts, pairs = Counter(words), Counter(zip(words, words[1:]))
    weights = [min(len(w), 12) / counts[w] if len(w) > 3 and not w.isdecimal() else 0 for w in words]
    pair_weights = [1 / pairs[p] for p in zip(words, words[1:])]
    segments, paragraph, first = [], 0, 0
    for i in range(1, len(tokens)):
        gap = text[tokens[i - 1].end:tokens[i].start]
        if re.search(r"[.!?…\r\n]", gap):
            segments.append((paragraph, first, i))
            paragraph += int("\n" in gap or "\r" in gap)
            first = i
    segments.append((paragraph, first, len(tokens)))
    candidates = []
    for paragraph, start, end in segments:
        size = min(width, end - start)
        if size < MIN_WORDS:
            continue
        for i in range(start, end - size + 1):
            weight = (sum(weights[i:i + size]) + 3 * sum(pair_weights[i:i + size - 1])) / width
            candidates.append((-weight, i, i + size, paragraph))
    if not candidates:
        return None
    candidates.sort()
    chosen, covered, paragraphs, phrases = [], set(), set(), set()
    for distinct_paragraph in (True, False):
        for _, start, end, paragraph in candidates:
            if (distinct_paragraph and paragraph in paragraphs) or not covered.isdisjoint(range(start, end)):
                continue
            phrase = " ".join(w[:40] for w in words[start:end])
            if phrase in phrases:
                continue
            chosen.append((start, end))
            covered.update(range(start, end))
            paragraphs.add(paragraph)
            phrases.add(phrase)
            if len(chosen) == count:
                return sorted(chosen)
    return sorted(chosen)


def paragraph_minimum_two(text):
    """Preserve the first request and cover another substantial paragraph.

    This is deliberately measured as an extra-query hypothesis, not as a
    same-query-budget gain. No expected spans enter this planner.
    """
    planned = web.plan_queries(text, strategy=web.CITATION_STRATEGY)
    if len(planned) != 1 or len(_tokens(text)) > 80:
        return planned
    for paragraph in re.finditer(r"[^\r\n]+", text):
        if paragraph.start() <= planned[0]["start"] < paragraph.end():
            continue
        fragment = paragraph.group()
        if len(fragment) < 35 or len(_tokens(fragment)) < MIN_WORDS:
            continue
        query = web.plan_queries(fragment, limit=1, strategy=web.DISTINCTIVE_STRATEGY)[0]
        query = {**query, "start": query["start"] + paragraph.start(), "end": query["end"] + paragraph.start()}
        if query["query"] != planned[0]["query"]:
            return planned + [query]
    return planned


def fixed_width_plan(text):
    with patch.object(web, "_distinctive_windows", fixed_width_windows):
        return web.plan_queries(text, strategy=web.CITATION_STRATEGY)


def dense_six_plan(text):
    """Retain every existing request, then fill the existing maximum of six.

    This increases actual requests for short inputs. It does not increase the
    site's six-request ceiling. The additional windows depend on text only.
    """
    planned = web.plan_queries(text, strategy=web.CITATION_STRATEGY)
    if len(planned) == 6:
        return planned
    tokens = _tokens(text)
    width = min(10, len(tokens))
    windows = web._distinctive_windows(text, tokens, width, 6)
    if windows is None:
        starts = [round(i * (len(tokens) - width) / 5) for i in range(6)]
        windows = [(start, start + width) for start in starts]
    else:
        words = [token.value for token in tokens]
        counts, pairs = Counter(words), Counter(zip(words, words[1:]))
        weights = [min(len(word), 12) / counts[word] if len(word) > 3 and not word.isdecimal() else 0 for word in words]
        pair_weights = [1 / pairs[pair] for pair in zip(words, words[1:])]
        def order(window):
            start, end = window
            score = (sum(weights[start:end]) + 3 * sum(pair_weights[start:end - 1])) / (end - start)
            return -score, start, end
        windows.sort(key=order)
    seen = {q["query"] for q in planned}
    for start, end in windows:
        window = tokens[start:end]
        query = '"' + " ".join(token.value[:40] for token in window) + '"'
        if len(query) > 390:
            query = query[:389].rsplit(" ", 1)[0] + '"'
            window = window[:len(query[1:-1].split())]
        if query not in seen:
            planned.append({"query": query, "start": window[0].start, "end": window[-1].end})
            seen.add(query)
        if len(planned) == 6:
            break
    return planned


def evaluate():
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only the fixed 30 DEV cases are accepted")
    names = ("citation_v2", "fixed_width_information", "short_paragraph_minimum_two", "dense_six")
    counts = {name: Counter() for name in names}
    rows = []
    for case in cases:
        text = (DEV / case["input_path"]).read_text()
        if sha256(text.encode()).hexdigest() != case["text_sha256"]:
            raise ValueError("Changed DEV text")
        plans = {"citation_v2": web.plan_queries(text, strategy=web.CITATION_STRATEGY),
                 "fixed_width_information": fixed_width_plan(text),
                 "short_paragraph_minimum_two": paragraph_minimum_two(text),
                 "dense_six": dense_six_plan(text)}
        tokens = _tokens(text)
        evidence = []
        for gold in case["expected_evidence"]:
            scores = {name: probe_evidence(plan, tokens, gold, [t.value for t in _tokens(gold["source_quote"])])
                      for name, plan in plans.items()}
            evidence.append({"source_id": gold["source_id"], "lexical": gold["needs_lexical_highlight"], "scores": scores})
            for name, score in scores.items():
                if gold["needs_lexical_highlight"]:
                    counts[name]["lexical_fragments"] += 1
                    counts[name]["whole_phrase_in_target_and_source"] += score["whole_query_in_both_spans"]
                    counts[name]["at_least_six_consecutive_shared_words"] += score["shared_consecutive_words"] >= 6
                else:
                    counts[name]["semantic_fragments"] += 1
                    counts[name]["semantic_target_touched"] += score["position_words"] > 0
        for name, plan in plans.items():
            counts[name]["documents"] += 1
            counts[name]["queries"] += len(plan)
            counts[name]["changed_plans"] += plan != plans["citation_v2"]
        rows.append({"id": case["id"], "plans": plans, "evidence": evidence})
    transitions = {}
    for name in names[1:]:
        changes = Counter()
        changed = []
        for row in rows:
            for e in row["evidence"]:
                if not e["lexical"]:
                    continue
                a = e["scores"]["citation_v2"]["whole_query_in_both_spans"]
                b = e["scores"][name]["whole_query_in_both_spans"]
                label = "retained" if a and b else "gained" if b else "lost" if a else "missed_both"
                changes[label] += 1
                if a != b:
                    changed.append({"id": row["id"], "source_id": e["source_id"], "transition": label})
        transitions[name] = {"counts": dict(changes), "changes": changed}
    result = {"schema_version": 1, "synthetic_dev": True, "live_queries": 0, "live_fetches": 0,
              "correct_url_found": None, "confirmed_highlight": None,
              "counts": {k: dict(v) for k, v in counts.items()}, "transitions": transitions, "cases": rows}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "short-query-hypotheses.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False, indent=2))


def artificial_short(candidate_name="fixed-width"):
    """Separate one-query matrix: 35 host words plus a 10/20/40-word insert."""
    from scripts.probe_query_blindspots import load_sources, HOSTS, DONORS, compose, dump, digest
    pages = load_sources()
    suffix = "fixed-width" if candidate_name == "fixed-width" else "dense-six"
    output = ROOT / f"data/private/web-evaluation/query-blindspots-{suffix}-v1"
    output.mkdir(parents=True, exist_ok=True)
    rows, counts, loss = [], Counter(), None
    queries = Counter()
    for host_id in HOSTS:
        tokens = _tokens(pages[host_id]["text"])
        host = pages[host_id]["text"][tokens[80].start:tokens[114].end]
        for donor_id in DONORS:
            for length in (10, 20, 40):
                for label, index in (("beginning", 0), ("middle", 17), ("end", 35)):
                    for layout in ("inline", "paragraph"):
                        text, evidence = compose(host, [(index, label, pages[donor_id], length)], layout)
                        plans = {"citation_v2": web.plan_queries(text, strategy=web.CITATION_STRATEGY),
                                 candidate_name: fixed_width_plan(text) if candidate_name == "fixed-width" else dense_six_plan(text)}
                        if len(plans["citation_v2"]) != 1 or any(len(plan) > 6 for plan in plans.values()):
                            raise ValueError("Short comparison exceeds its stated budget")
                        for name, plan in plans.items():
                            queries[name] += len(plan)
                        scores = {name: any(evidence[0]["span"][0] <= q["start"] and q["end"] <= evidence[0]["span"][1]
                                            for q in plan) for name, plan in plans.items()}
                        previous, candidate = scores.values()
                        label = ("retained" if previous and candidate else "gained" if candidate
                                 else "lost" if previous else "missed_both")
                        counts[label] += 1
                        row = {"id": f"S{len(rows) + 1:04d}", "synthetic": True, "host": host_id,
                               "layout": layout, "evidence": evidence, "text_sha256": digest(text),
                               "scores": scores, "plans": plans, "transition": label}
                        rows.append(row)
                        if loss is None and label == "lost":
                            loss = {**row, "text": text}
    result = {"schema_version": 1, "synthetic": True, "documents": len(rows), "insertions": len(rows),
              "actual_queries": dict(queries), "live_queries": 0, "live_fetches": 0,
              "transitions": dict(counts), "cases": rows}
    dump(output / "short-artificial-insertions.json", result)
    if loss:
        dump(output / "short-loss-example.json", loss)
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artificial-short", action="store_true")
    parser.add_argument("--candidate", choices=("fixed-width", "dense-six"), default="fixed-width")
    args = parser.parse_args()
    artificial_short(args.candidate) if args.artificial_short else evaluate()
