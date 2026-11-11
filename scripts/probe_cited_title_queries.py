#!/usr/bin/env python3
"""One local-only, input-only cited-title query hypothesis on frozen DEV-30.

Preserve a quotation's first ten-word query. Replace at most one later query
overlapping that anchor by >=80% with a six-word quoted phrase plus title
context explicitly cited immediately afterwards. No production code changes,
provider, fetcher, final collection, or source annotations enter the planner.
"""
from bisect import bisect_left
from collections import Counter
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens
import plagiarism.web_search as web
from scripts.compare_web_queries_offline import shared_run
from scripts.probe_query_blindspots import (
    DEV, HOSTS, DONORS, compose, dump, host_text, load_sources, positions,
)

OUT = ROOT / "data/private/web-evaluation/query-cited-title-v1"
QUOTE = r'«([^«»]+)»|“([^“”]+)”|„([^„“]+)“|"([^"\n]+)"'
TITLE = re.compile(r'\b(?:статья|статье|article)\s*[:—–-]?\s*(?:' + QUOTE + ')', re.I)
PAIRS = (("citation_v2", "citation_cited_title"), ("dense_v4", "dense_cited_title"))
FIELDS = (
    "whole_query_in_insertion", "quoted_phrase_in_insertion",
    "whole_query_in_insertion_and_source", "at_least_six_shared_words",
)


def digest(text):
    return sha256(text.encode()).hexdigest()


def quoted_group(match):
    group = next(i for i in range(1, 5) if match.group(i) is not None)
    return match.span(group)


def cited_titles(text):
    """Return only an immediately attached, explicit parenthetical article title."""
    tokens = _tokens(text)
    starts = [token.start for token in tokens]
    found = []
    for quote in re.finditer(QUOTE, text):
        start, end = quoted_group(quote)
        first, last = bisect_left(starts, start), bisect_left(starts, end)
        if last - first < 10:
            continue
        parenthetical = re.match(r'\s*\(([^()\r\n]{1,300})\)', text[quote.end():])
        if parenthetical is None:
            continue
        title = TITLE.search(parenthetical.group(1))
        if title is None:
            continue
        a, b = quoted_group(title)
        offset = quote.end() + parenthetical.start(1)
        title_start, title_end = offset + a, offset + b
        title_tokens = _tokens(text[title_start:title_end])
        if not title_tokens:
            continue
        found.append({"quote_span": [start, end], "quote_token_span": [first, last],
                      "title_span": [title_start, title_end],
                      "title_tokens": [token.value for token in title_tokens],
                      "explicit_citation_span": [quote.end() + parenthetical.start(),
                                                 quote.end() + parenthetical.end()]})
    return found


def units_for_span(tokens, start, end, role):
    return [{"value": token.value[:40], "start": token.start, "end": token.end, "role": role}
            for token in tokens if start <= token.start and token.end <= end]


def cited_title_plan(text, baseline):
    """The sole planner input is submitted text; retain the actual query count."""
    planned = [dict(query) for query in baseline]
    tokens = _tokens(text)
    patterns = cited_titles(text)
    changes = []
    for pattern in patterns:
        first, last = pattern["quote_token_span"]
        anchor_tokens = tokens[first:first + 10]
        anchor_query = '"' + " ".join(token.value[:40] for token in anchor_tokens) + '"'
        anchor_index = next((i for i, query in enumerate(planned)
                             if query["query"] == anchor_query
                             and query["start"] == anchor_tokens[0].start
                             and query["end"] == anchor_tokens[-1].end), None)
        if anchor_index is None:
            continue
        anchor_positions = set(range(first, first + 10))
        for index in range(anchor_index + 1, len(planned)):
            query = planned[index]
            secondary = {i for i in range(first, last)
                         if query["start"] <= tokens[i].start and tokens[i].end <= query["end"]}
            if (not secondary or not pattern["quote_span"][0] <= query["start"]
                    or query["end"] > pattern["quote_span"][1]):
                continue
            overlap = len(secondary & anchor_positions) / len(secondary)
            if overlap < .8:
                continue
            phrase = tokens[first:first + 6]
            value = '"' + " ".join(token.value[:40] for token in phrase) + '" ' + " ".join(word[:40] for word in pattern["title_tokens"])
            if len(value) > 390 or value in {item["query"] for item in planned}:
                continue
            phrase_span = [phrase[0].start, phrase[-1].end]
            title_start, title_end = pattern["title_span"]
            units = units_for_span(tokens, *phrase_span, "quoted_phrase")
            units += units_for_span(tokens, title_start, title_end, "title_context")
            replacement = {"query": value, "start": phrase_span[0], "end": title_end,
                           "input_spans": [phrase_span, pattern["title_span"]],
                           "quoted_phrase_span": phrase_span, "title_context_span": pattern["title_span"],
                           "sent_units": units, "discontiguous_input": True}
            if [unit["value"] for unit in units] != [token.value for token in _tokens(value)]:
                raise ValueError("Actual query and recorded input units disagree")
            planned[index] = replacement
            changes.append({**pattern, "query_index": index, "preserved_anchor_index": anchor_index,
                            "secondary_overlap_fraction": overlap, "old_query": query,
                            "replacement": replacement})
            break
        if changes:
            break
    if len(planned) != len(baseline) or len(planned) > 6:
        raise ValueError("Actual request count changed")
    if changes:
        anchor_index = changes[0]["preserved_anchor_index"]
        if planned[anchor_index] != baseline[anchor_index]:
            raise ValueError("First quoted ten-word query changed")
    return planned, {"explicit_cited_title_patterns": patterns, "changes": changes}


@lru_cache(maxsize=1024)
def all_plans(text):
    citation = web.plan_queries(text, limit=6, strategy=web.CITATION_STRATEGY)
    dense = citation if len(citation) == 6 else web.plan_queries(text, limit=6, strategy=web.DENSE_STRATEGY)
    citation_new, citation_audit = cited_title_plan(text, citation)
    dense_new, dense_audit = cited_title_plan(text, dense)
    return ({"citation_v2": citation, "citation_cited_title": citation_new,
             "dense_v4": dense, "dense_cited_title": dense_new},
            {"citation_cited_title": citation_audit, "dense_cited_title": dense_audit})


def evidence_score(plan, text_tokens, span, source_words):
    whole = phrase = whole_source = False
    best_shared = 0
    query_details = []
    for query in plan:
        units = query.get("sent_units") or units_for_span(text_tokens, query["start"], query["end"], "quoted_phrase")
        sent_words = [token.value for token in _tokens(query["query"])]
        if [unit["value"] for unit in units] != sent_words:
            raise ValueError("Scoring input spans do not describe the sent query")
        in_span = lambda unit: span[0] <= unit["start"] and unit["end"] <= span[1]
        full_inside = bool(units) and all(in_span(unit) for unit in units)
        phrase_units = [unit for unit in units if unit["role"] == "quoted_phrase"]
        phrase_inside = bool(phrase_units) and all(in_span(unit) for unit in phrase_units)
        selected = [unit["value"] for unit in units if in_span(unit)]
        shared = shared_run(selected, source_words) if selected else 0
        in_source = bool(sent_words) and shared_run(sent_words, source_words) == len(sent_words)
        whole |= full_inside
        phrase |= phrase_inside
        whole_source |= full_inside and in_source
        best_shared = max(best_shared, shared)
        query_details.append({"whole_query_in_insertion": full_inside,
                              "quoted_phrase_in_insertion": phrase_inside,
                              "whole_query_in_source_span": in_source,
                              "shared_consecutive_words_from_insertion": shared})
    return {"whole_query_in_insertion": whole, "quoted_phrase_in_insertion": phrase,
            "whole_query_in_insertion_and_source": whole_source,
            "at_least_six_shared_words": best_shared >= 6,
            "max_shared_consecutive_words": best_shared, "queries": query_details}


def score_row(identifier, text, evidence, **metadata):
    plans, audit = all_plans(text)
    tokens = _tokens(text)
    checked = []
    # Plans are complete before annotations or source text are used for scoring.
    for item in evidence:
        source_words = [token.value for token in _tokens(item["source_quote"])]
        checked.append({**{key: value for key, value in item.items() if key != "source_quote"},
                        "scores": {name: evidence_score(plan, tokens, item["span"], source_words)
                                   for name, plan in plans.items()}})
    return {"id": identifier, "text_sha256": digest(text), "plans": plans,
            "actual_query_counts": {name: len(plan) for name, plan in plans.items()},
            "planner_audit": audit, "evidence": checked, **metadata}


def summarize(rows):
    counts = {name: Counter() for pair in PAIRS for name in pair}
    transitions = {right: {field: Counter() for field in FIELDS} for _, right in PAIRS}
    examples = {right: {field: [] for field in FIELDS} for _, right in PAIRS}
    for row in rows:
        for name, plan in row["plans"].items():
            counts[name]["documents"] += 1
            counts[name]["actual_planned_queries"] += len(plan)
        for left, right in PAIRS:
            counts[right]["changed_plans"] += row["plans"][left] != row["plans"][right]
            counts[right]["recognized_cited_title_patterns"] += len(row["planner_audit"][right]["explicit_cited_title_patterns"])
        for index, item in enumerate(row["evidence"]):
            if not item.get("lexical", True):
                continue
            for name, score in item["scores"].items():
                counts[name]["lexical_fragments"] += 1
                for field in FIELDS:
                    counts[name][field] += score[field]
            for left, right in PAIRS:
                for field in FIELDS:
                    a, b = item["scores"][left][field], item["scores"][right][field]
                    transition = "retained" if a and b else "gained" if b else "lost" if a else "missed_both"
                    transitions[right][field][transition] += 1
                    if a != b:
                        examples[right][field].append({"id": row["id"], "fragment_index": index,
                                                       "source_id": item["source_id"], "transition": transition})
    return {"counts": {key: dict(value) for key, value in counts.items()},
            "transitions": {name: {field: dict(value) for field, value in fields.items()}
                            for name, fields in transitions.items()}, "changes": examples}


def evaluate_dev():
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [case["id"] for case in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(case["split"] != "dev" for case in cases):
        raise ValueError("Only frozen D01–D30 DEV is accepted")
    rows = []
    for case in cases:
        path = (DEV / case["input_path"]).resolve()
        if not path.is_relative_to(DEV.resolve()):
            raise ValueError("Input path leaves DEV")
        text = path.read_text()
        if digest(text) != case["text_sha256"]:
            raise ValueError("DEV input changed")
        all_plans(text)
        evidence = [{"span": item["target_span"], "source_quote": item["source_quote"],
                     "source_id": item["source_id"], "lexical": item["needs_lexical_highlight"]}
                    for item in case["expected_evidence"]]
        rows.append(score_row(case["id"], text, evidence))
    return write_result("dev-comparison.json", rows, "known_dev")


def source_quotes(evidence, pages):
    return [{**item, "source_quote": pages[item["source_id"]]["text"][slice(*item["source_span"])]}
            for item in evidence]


def evaluate_artificial(pages, short=False):
    rows = []
    for host_id in HOSTS:
        if short:
            tokens = _tokens(pages[host_id]["text"])
            host = pages[host_id]["text"][tokens[80].start:tokens[114].end]
            slots = (("beginning", 0), ("middle", 17), ("end", 35))
        else:
            host = host_text(pages[host_id])
            slots = positions(host)
        for donor_id in DONORS if short else DONORS[:2]:
            for length in (10, 20, 40):
                for position, index in slots:
                    for layout in ("inline", "paragraph"):
                        text, evidence = compose(host, [(index, position, pages[donor_id], length)], layout)
                        rows.append(score_row(f"{'S' if short else 'A'}{len(rows) + 1:04d}", text,
                                              source_quotes(evidence, pages), synthetic=True,
                                              host=host_id, layout=layout))
        if short:
            continue
        paired = [(slots[0], slots[2]), (slots[1], slots[-1]),
                  (("quarter", 150), ("three_quarters", 450)), (slots[3], slots[-1])]
        for length in (10, 20, 40):
            for pair in paired:
                for layout in ("inline", "paragraph"):
                    insertions = [(pair[0][1], pair[0][0], pages[DONORS[1]], length),
                                  (pair[1][1], pair[1][0], pages[DONORS[2]], length)]
                    text, evidence = compose(host, insertions, layout)
                    rows.append(score_row(f"A{len(rows) + 1:04d}", text, source_quotes(evidence, pages),
                                          synthetic=True, host=host_id, layout=layout))
    return write_result("short-comparison.json" if short else "artificial-insertions.json", rows,
                        "artificial_short" if short else "artificial_long")


def write_result(filename, rows, kind):
    result = {"schema_version": 1, "data_kind": kind, "documents": len(rows),
              "insertions_or_fragments": sum(len(row["evidence"]) for row in rows),
              "live_queries": 0, "live_fetches": 0, "correct_url_found": None,
              "confirmed_highlight": None, **summarize(rows), "cases": rows}
    dump(OUT / filename, result)
    return {key: value for key, value in result.items() if key != "cases"}


def pattern_checks():
    passage = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    examples = [(f'«{passage}» (статья «Title»).', True),
                (f'«{passage}» (в статье «Title»).', True),
                (f'“{passage}” (article “Title”).', True),
                (f'«{passage}» (книга «Title»).', False),
                (f'«{passage}» (Википедия, «Title»).', False),
                (f'«{passage}» Текст. (статья «Title»).', False),
                (f'«{passage}» (статья без названия).', False)]
    result = []
    for text, expected in examples:
        actual = bool(cited_titles(text))
        if actual != expected:
            raise ValueError("Explicit title pattern check failed")
        result.append({"text": text, "expected_pattern": expected, "actual_pattern": actual})
    return result


def authored_counterexample():
    """Freeze the mixed-quotation loss; never specialize the planner around it."""
    source = "точные лабораторные измерения требуют регулярной калибровки датчиков стабильной температуры помещения"
    prefix = "«И вот "
    text = prefix + source + "» (статья «Лабораторная практика»)."
    span = [len(prefix), len(prefix) + len(source)]
    row = score_row("CITED-TITLE-LOSS", text,
                    [{"span": span, "source_span": [0, len(source)], "source_quote": source,
                      "source_id": "authored-independent-source", "lexical": True}],
                    synthetic=True, submitted_text=text, authored_source_text=source,
                    purpose="Mixed quotation: two author words precede a ten-word copy; no DEV or external source used.")
    evidence = row["evidence"][0]
    old, new = [evidence["scores"][name] for name in ("dense_v4", "dense_cited_title")]
    if (not old["whole_query_in_insertion_and_source"] or new["whole_query_in_insertion_and_source"]
            or len(row["planner_audit"]["dense_cited_title"]["changes"]) != 1):
        raise ValueError("Authored counterexample changed; inspect without tuning the planner")
    result = {"schema_version": 1, "live_queries": 0, "live_fetches": 0,
              "correct_url_found": None, "confirmed_highlight": None,
              "decision": "Loss: overlap does not make a whole-copy secondary query redundant.",
              "case": row}
    dump(OUT / "authored-counterexample.json", result)
    return {"path": "authored-counterexample.json", "whole_phrase_in_insertion_and_source": [True, False],
            "actual_query_counts": [len(row["plans"]["dense_v4"]), len(row["plans"]["dense_cited_title"])],
            "best_shared_words": [old["max_shared_consecutive_words"], new["max_shared_consecutive_words"]],
            "replacement_shared_words": new["queries"][1]["shared_consecutive_words_from_insertion"]}


def main():
    # Fail closed if a future import or edit accidentally attempts network use.
    def no_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
            raise RuntimeError("Offline cited-title probe forbids network")
    sys.addaudithook(no_network)
    if OUT.exists():
        raise ValueError("Retain the first frozen run; choose a new explicit experiment for another run")
    OUT.mkdir(mode=0o700)
    checks = pattern_checks()
    dev = evaluate_dev()
    pages = load_sources()
    short = evaluate_artificial(pages, short=True)
    long = evaluate_artificial(pages)
    counterexample = authored_counterexample()
    summary = {"schema_version": 1, "live_queries": 0, "live_fetches": 0,
               "production_changed": False, "default_strategy_changed": False,
               "script_sha256": digest(Path(__file__).read_text()),
               "planner_input": "Submitted text only; gold and source snapshots used after planning solely for local scoring.",
               "pattern": TITLE.pattern, "max_replacements_per_document": 1,
               "anchor_words": 6, "preserved_quote_words": 10, "minimum_secondary_token_overlap": .8,
               "comparison": "Same actual query counts within each baseline/candidate pair; maximum six unchanged.",
               "metric_limit": "Title context is discontiguous. Full-query containment and six-word source-shared clues are separate. Neither is retrieval, URL, fetch, or highlight success.",
               "pattern_checks": checks, "dev": dev, "short_artificial": short, "long_artificial": long,
               "authored_counterexample": counterexample,
               "decision": "Do not promote: no measured retrieval gain and an independent authored whole-copy query loss."}
    dump(OUT / "summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "pattern_checks"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
