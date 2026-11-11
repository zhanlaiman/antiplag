#!/usr/bin/env python3
"""One offline hypothesis: whitespace-delimited em/en dash ends a clause.

Only the distinctive-window segmentation predicate changes in an in-memory
copy of the current function. The production module/file is restored after
each local plan. No source annotations enter planning and no network exists.
"""
from collections import Counter
from functools import lru_cache
from hashlib import sha256
import inspect
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import plagiarism.web_search as web
from plagiarism.matching import _tokens
from scripts.compare_web_queries_offline import probe_evidence
from scripts.probe_query_blindspots import artificial, compose, dump, load_sources, HOSTS, DONORS

DEV = ROOT / "data/private/web-evaluation/materials/dev-30"
OUT = ROOT / "data/private/web-evaluation/query-dash-clause-v1"
PAIRS = (("citation_v2", "citation_dash"), ("dense10", "dense10_dash"))
ORIGINAL_SOURCE = inspect.getsource(web._distinctive_windows)
OLD = 'if re.search(r"[.!?…\\r\\n]", gap):'
NEW = 'if re.search(r"[.!?…\\r\\n]", gap) or re.search(r"\\s[—–]\\s", gap):'
if ORIGINAL_SOURCE.count(OLD) != 1:
    raise ValueError("Distinctive function changed; manually rebase the single-predicate experiment")
namespace = dict(vars(web))
exec(compile(ORIGINAL_SOURCE.replace(OLD, NEW), "<isolated-dash-boundary>", "exec"), namespace)
DASH_WINDOWS = namespace["_distinctive_windows"]


def dash_plan(text, strategy):
    with patch.object(web, "_distinctive_windows", DASH_WINDOWS):
        return web.plan_queries(text, strategy=strategy)


@lru_cache(maxsize=512)
def all_plans(text):
    old = web.plan_queries(text, strategy=web.CITATION_STRATEGY)
    new = dash_plan(text, web.CITATION_STRATEGY)
    # Dense's first branch returns the citation prefix unchanged at six.
    dense_old = old if len(old) == 6 else web.plan_queries(text, strategy=web.DENSE_STRATEGY)
    dense_new = new if len(new) == 6 else dash_plan(text, web.DENSE_STRATEGY)
    plans = {"citation_v2": old, "citation_dash": new, "dense10": dense_old, "dense10_dash": dense_new}
    if any(len(p) > 6 for p in plans.values()):
        raise ValueError("Six-request ceiling changed")
    return plans


def summarize(rows, fields):
    counts = {name: Counter() for pair in PAIRS for name in pair}
    changes = {right: {field: Counter() for field in fields} for _, right in PAIRS}
    examples = {right: {field: [] for field in fields} for _, right in PAIRS}
    for r in rows:
        for name, plan in r["plans"].items():
            counts[name]["documents"] += 1
            counts[name]["actual_planned_queries"] += len(plan)
        for e in r["evidence"]:
            if not e.get("lexical", True):
                continue
            for name, score in e["scores"].items():
                counts[name]["fragments"] += 1
                for field in fields:
                    counts[name][field] += score[field]
            for left, right in PAIRS:
                for field in fields:
                    a, b = e["scores"][left][field], e["scores"][right][field]
                    label = "retained" if a and b else "gained" if b else "lost" if a else "missed_both"
                    changes[right][field][label] += 1
                    if a != b:
                        examples[right][field].append({"id": r["id"], "transition": label,
                                                       "fragment_index": e.get("fragment_index", 0)})
    return {"counts": {k: dict(v) for k, v in counts.items()},
            "transitions": {name: {field: dict(c) for field, c in byfield.items()} for name, byfield in changes.items()},
            "changes": examples}


def evaluate_dev():
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only D01–D30 DEV is supported")
    rows = []
    for c in cases:
        text = (DEV / c["input_path"]).read_text()
        if sha256(text.encode()).hexdigest() != c["text_sha256"]:
            raise ValueError("Changed DEV input")
        plans, evidence = all_plans(text), []
        for index, gold in enumerate(c["expected_evidence"]):
            scores = {}
            for name, p in plans.items():
                probe = probe_evidence(p, _tokens(text), gold, [t.value for t in _tokens(gold["source_quote"])])
                scores[name] = {"whole_query": probe["whole_query_in_both_spans"],
                                "six_shared_words": probe["shared_consecutive_words"] >= 6}
            evidence.append({"fragment_index": index, "lexical": gold["needs_lexical_highlight"], "scores": scores})
        rows.append({"id": c["id"], "plans": plans, "evidence": evidence})
    result = {"schema_version": 1, "data_kind": "known_dev", **summarize(rows, ("whole_query", "six_shared_words")), "cases": rows}
    dump(OUT / "dev-comparison.json", result)
    for left, right in PAIRS:
        loss = next((r for r in rows if any(e["lexical"] and e["scores"][left]["six_shared_words"]
                                           and not e["scores"][right]["six_shared_words"] for e in r["evidence"])), None)
        if loss:
            c = next(c for c in cases if c["id"] == loss["id"])
            dump(OUT / (right + "-dev-loss.json"), {**loss, "text": (DEV / c["input_path"]).read_text()})
    return {k: v for k, v in result.items() if k != "cases"}


def evaluate_short(pages):
    rows = []
    for host_id in HOSTS:
        tokens = _tokens(pages[host_id]["text"])
        host = pages[host_id]["text"][tokens[80].start:tokens[114].end]
        for donor_id in DONORS:
            for length in (10, 20, 40):
                for position, index in (("beginning", 0), ("middle", 17), ("end", 35)):
                    for layout in ("inline", "paragraph"):
                        text, evidence = compose(host, [(index, position, pages[donor_id], length)], layout)
                        plans = all_plans(text)
                        scores = {name: {"whole_query": any(evidence[0]["span"][0] <= q["start"] and q["end"] <= evidence[0]["span"][1]
                                                           for q in p)} for name, p in plans.items()}
                        rows.append({"id": f"E{len(rows) + 1:04d}", "synthetic": True, "host": host_id,
                                     "layout": layout, "plans": plans, "evidence": [{**evidence[0], "scores": scores}],
                                     "text_sha256": sha256(text.encode()).hexdigest()})
    result = {"schema_version": 1, "synthetic": True, **summarize(rows, ("whole_query",)), "cases": rows}
    dump(OUT / "short-comparison.json", result)
    return {k: v for k, v in result.items() if k != "cases"}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    OUT.chmod(0o700)
    dev = evaluate_dev()
    pages = load_sources()
    short = evaluate_short(pages)
    large = {}
    for left, right in PAIRS:
        directory = OUT / right
        directory.mkdir(exist_ok=True)
        large[right] = artificial(pages, directory, planners={
            left: lambda text, name=left: all_plans(text)[name],
            right: lambda text, name=right: all_plans(text)[name]})
    result = {"schema_version": 1, "live_queries": 0, "live_fetches": 0, "correct_url_found": None,
              "confirmed_highlight": None, "distinctive_function_sha256": sha256(ORIGINAL_SOURCE.encode()).hexdigest(),
              "single_predicate_change": NEW, "dev": dev, "short_artificial": short, "long_artificial": large}
    dump(OUT / "summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
