#!/usr/bin/env python3
"""Local-only artificial insertion and saved queue checks; never opens final data.

The six-query plans see submitted text only. Insertion spans and source URLs are
used afterwards for scoring. No search or fetch provider is constructed.
"""

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens
from plagiarism.web_evaluation import _url
from plagiarism.web_search import CITATION_STRATEGY, COVERAGE_STRATEGY, plan_queries

DEV = ROOT / "data/private/web-evaluation/materials/dev-30"
LIVE = ROOT / "data/private/web-evaluation/dev-live-30-v1"
HOSTS = ("wiki-ru-14541-156097738", "wiki-ru-174985-152884967", "wikisource-ru-1006313-5583492")
DONORS = ("wiki-ru-1921-153988549", "wiki-ru-168721-150358889", "wiki-ru-3024925-154960186")
STRATEGIES = (CITATION_STRATEGY, COVERAGE_STRATEGY)


def digest(text):
    return sha256(text.encode()).hexdigest()


def load_sources():
    pages = {}
    for metadata in json.loads((DEV / "sources.json").read_text()):
        path = (DEV / metadata["path"]).resolve()
        if not path.is_relative_to(DEV.resolve()):
            raise ValueError("Snapshot path leaves DEV")
        text = path.read_text()
        if digest(text) != metadata["provenance"]["text_sha256"]:
            raise ValueError("Changed snapshot " + metadata["id"])
        pages[metadata["id"]] = {**metadata, "text": text}
    return pages


def host_text(page):
    tokens = _tokens(page["text"])
    return page["text"][tokens[80].start:tokens[679].end]


def positions(text):
    tokens = _tokens(text)
    queries = plan_queries(text, limit=6, strategy=CITATION_STRATEGY)
    if len(queries) != 6:
        raise ValueError("Host does not admit six distinct queries")
    windows = sorted((sum(t.end <= q["start"] for t in tokens),
                      sum(t.end <= q["end"] for t in tokens)) for q in queries)
    gaps = [(a[1] + b[0]) // 2 for a, b in zip(windows, windows[1:]) if a[1] < b[0]]
    return [("beginning", 0), ("middle", len(tokens) // 2), ("end", len(tokens))] + [
        (f"between_queries_{i + 1}", value) for i, value in enumerate(gaps)]


def compose(host, insertions, layout):
    tokens = _tokens(host)
    inserts = sorted(insertions, key=lambda item: item[0])
    text, evidence, cursor = "", [], 0
    separator = "\n\n" if layout == "paragraph" else " "
    for word_index, label, donor, length in inserts:
        offset = tokens[word_index].start if word_index < len(tokens) else len(host)
        donor_tokens = _tokens(donor["text"])
        first, last = donor_tokens[100], donor_tokens[99 + length]
        excerpt = donor["text"][first.start:last.end]
        text += host[cursor:offset] + separator
        start = len(text)
        text += excerpt + separator
        evidence.append({"source_id": donor["id"], "source_span": [first.start, last.end],
                         "span": [start, start + len(excerpt)], "words": length, "position": label})
        cursor = offset
    return text + host[cursor:], evidence


def plan_evidence(text, evidence, planners):
    plans = {name: planner(text) for name, planner in planners.items()}
    if any(len(p) != 6 for p in plans.values()):
        raise ValueError("Artificial comparison requires exactly six actual queries")
    scores = {}
    for strategy, queries in plans.items():
        hits = [any(e["span"][0] <= q["start"] and q["end"] <= e["span"][1] for q in queries)
                for e in evidence]
        scores[strategy] = {"whole_phrase_in_insertions": hits,
                            "insertions_hit": sum(hits), "all_insertions_hit": all(hits)}
    return plans, scores


def artificial(pages, out, planners=None):
    planners = planners or {s: (lambda text, strategy=s: plan_queries(text, limit=6, strategy=strategy)) for s in STRATEGIES}
    baseline, candidate = planners
    rows, loss = [], None
    for host_id in HOSTS:
        host = host_text(pages[host_id])
        slots = positions(host)
        for donor_id in DONORS[:2]:
            for length in (10, 20, 40):
                for position, index in slots:
                    for layout in ("inline", "paragraph"):
                        text, evidence = compose(host, [(index, position, pages[donor_id], length)], layout)
                        plans, scores = plan_evidence(text, evidence, planners)
                        row = {"id": f"A{len(rows) + 1:04d}", "synthetic": True, "host": host_id,
                               "layout": layout, "evidence": evidence, "text_sha256": digest(text),
                               "scores": scores, "queries_per_strategy": 6}
                        rows.append(row)
                        if loss is None and scores[baseline]["insertions_hit"] > scores[candidate]["insertions_hit"]:
                            loss = {**row, "text": text, "plans": plans}
        paired_slots = [(slots[0], slots[2]), (slots[1], slots[-1]),
                        (("quarter", 150), ("three_quarters", 450)), (slots[3], slots[-1])]
        for length in (10, 20, 40):
            for pair in paired_slots:
                for layout in ("inline", "paragraph"):
                    insertions = [(pair[0][1], pair[0][0], pages[DONORS[1]], length),
                                  (pair[1][1], pair[1][0], pages[DONORS[2]], length)]
                    text, evidence = compose(host, insertions, layout)
                    plans, scores = plan_evidence(text, evidence, planners)
                    row = {"id": f"A{len(rows) + 1:04d}", "synthetic": True, "host": host_id,
                           "layout": layout, "evidence": evidence, "text_sha256": digest(text),
                           "scores": scores, "queries_per_strategy": 6}
                    rows.append(row)
                    if loss is None and scores[baseline]["insertions_hit"] > scores[candidate]["insertions_hit"]:
                        loss = {**row, "text": text, "plans": plans}
    summary = {}
    for label, selected in [("all", rows), ("single", [r for r in rows if len(r["evidence"]) == 1]),
                            ("double", [r for r in rows if len(r["evidence"]) == 2])]:
        summary[label] = {"documents": len(selected), "insertions": sum(len(r["evidence"]) for r in selected)}
        for strategy in planners:
            summary[label][strategy] = {"insertions_hit": sum(r["scores"][strategy]["insertions_hit"] for r in selected),
                                        "all_insertions_hit": sum(r["scores"][strategy]["all_insertions_hit"] for r in selected)}
    transitions = Counter()
    by_length = defaultdict(Counter)
    by_layout = defaultdict(Counter)
    for row in rows:
        for i, evidence in enumerate(row["evidence"]):
            left = row["scores"][baseline]["whole_phrase_in_insertions"][i]
            right = row["scores"][candidate]["whole_phrase_in_insertions"][i]
            name = "retained" if left and right else "gained" if right else "lost" if left else "missed_both"
            transitions[name] += 1
            by_length[str(evidence["words"])][name] += 1
            by_layout[row["layout"]][name] += 1
    result = {"schema_version": 1, "synthetic": True, "strategies": list(planners),
              "method": "Six actual query phrases, scored solely for whole-phrase containment in inserted spans. Plans receive text only.",
              "live_queries": 0, "live_fetches": 0, "correct_url_found": None, "confirmed_highlight": None,
              "summary": summary, "transitions": dict(transitions), "by_length": dict(by_length),
              "by_layout": dict(by_layout), "loss_example": "loss-example.json" if loss else None,
              "cases": rows}
    dump(out / "artificial-insertions.json", result)
    if loss:
        dump(out / "loss-example.json", loss)
    return {k: v for k, v in result.items() if k != "cases"}


def queue_check():
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only the fixed 30 DEV cases are accepted")
    rows = []
    counts = Counter()
    for case in cases:
        report = json.loads((LIVE / (case["id"] + ".json")).read_text())
        web = report["web"]
        discovered = [_url(u) for u in web["discovered_urls"]]
        attempted = [_url(u) for u in web["attempted_urls"]]
        read = {_url(s["url"]) for s in report["sources"]}
        search_only = discovered[:web["search_urls_discovered"]]
        sources = report["sources"]
        hashes = Counter(digest(s["text"]) for s in sources)
        duplicate_content = sum(n - 1 for n in hashes.values())
        duplicate_loads = web["urls_attempted"] - web["sources_checked"] - sum(e["stage"] == "fetch" for e in web["errors"])
        if duplicate_loads < 0:
            raise ValueError("Fetch accounting needs manual review: " + case["id"])
        counts["exact_content_duplicate_pages"] += duplicate_content
        counts["fetch_slots_with_duplicate_final_url"] += duplicate_loads
        counts["provider_or_redirect_duplicates_reported"] += web["urls_deduplicated"]
        counts["skipped_urls"] += len(web["skipped_urls"])
        counts["documents"] += 1
        found = []
        for evidence in case["expected_evidence"]:
            gold = {_url(u) for u in evidence["source_urls"]}
            hits = gold & set(discovered)
            if not hits:
                continue
            picked = gold & set(attempted)
            counts["expected_fragments_with_url"] += 1
            counts["expected_fragments_with_url_attempted"] += bool(picked)
            counts["expected_fragments_lost_at_cap"] += not bool(picked)
            found.append({"source_id": evidence["source_id"], "lexical": evidence["needs_lexical_highlight"],
                          "discovered_positions_1based": [i + 1 for i, u in enumerate(discovered) if u in gold],
                          "attempted_positions_1based": [i + 1 for i, u in enumerate(attempted) if u in gold],
                          "url_read": bool(gold & read),
                          "content_saved": bool(gold & read),
                          "naive_discovery_order_first12": bool(gold & set(search_only[:12]))})
        rows.append({"id": case["id"], "expected_urls_present": found,
                     "discovered": len(discovered), "attempted": len(attempted),
                     "outside_limit": len(web["skipped_urls"]), "exact_content_duplicate_pages": duplicate_content,
                     "fetch_slots_with_duplicate_final_url": duplicate_loads,
                     "provider_or_redirect_duplicates_reported": web["urls_deduplicated"]})
    return {"schema_version": 1, "live_queries": 0, "counts": dict(counts), "cases": rows,
            "limit": "Saved reports retain deduplicated URL order and loaded text, not all raw result positions or unread content. Missing content permits queue-position analysis only."}


def near_copy_check():
    """Flag large overlaps for manual review; shared templates are not reprints."""
    matched = []
    for number in range(1, 31):
        case_id = f"D{number:02d}"
        report = json.loads((LIVE / (case_id + ".json")).read_text())
        shingles, pairs = [], []
        for index, source in enumerate(report["sources"]):
            words = [t.value for t in _tokens(source["text"])]
            current = {tuple(words[i:i + 5]) for i in range(len(words) - 4)}
            shingles.append(current)
            if len(current) < 50:
                continue
            for earlier, other in enumerate(shingles[:-1]):
                if len(other) < 50:
                    continue
                overlap = len(current & other) / min(len(current), len(other))
                if overlap >= .8:
                    pairs.append({"left_url": report["sources"][earlier]["url"], "right_url": source["url"],
                                  "shorter_5gram_overlap": round(overlap, 4), "shorter_5grams": min(len(current), len(other))})
        if pairs:
            matched.append({"id": case_id, "pairs": pairs})
    return {"threshold": .8, "min_5grams": 50, "matches": matched,
            "interpretation": "Candidate overlap only; a shared template or excerpt is not proof of a whole-page reprint."}


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/private/web-evaluation/query-blindspots-v1")
    parser.add_argument("--candidate", choices=("coverage", "fixed-width", "dense-six"), default="coverage")
    parser.add_argument("--near-copies", action="store_true", help="Only check large overlaps between saved loaded pages")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    args.output.chmod(0o700)
    if args.near_copies:
        result = near_copy_check()
        dump(args.output / "near-copy-pages.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    planners = None
    if args.candidate == "fixed-width":
        from scripts.probe_short_query_selection import fixed_width_plan
        planners = {CITATION_STRATEGY: lambda text: plan_queries(text, limit=6, strategy=CITATION_STRATEGY),
                    "fixed-width-information": fixed_width_plan}
    elif args.candidate == "dense-six":
        from scripts.probe_short_query_selection import dense_six_plan
        planners = {CITATION_STRATEGY: lambda text: plan_queries(text, limit=6, strategy=CITATION_STRATEGY),
                    "dense-six": dense_six_plan}
    summary = artificial(load_sources(), args.output, planners)
    queue = queue_check()
    dump(args.output / "saved-queue-audit.json", queue)
    print(json.dumps({"artificial": summary, "queue": queue["counts"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
