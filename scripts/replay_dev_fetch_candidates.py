#!/usr/bin/env python3
"""Replay URL ordering, then one follow-up cap on the same saved DEV data.

The production module is never edited. A Git snapshot runs in isolated module
namespaces. All search replies/pages are frozen; socket operations are denied.
"""
from copy import deepcopy
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens
from plagiarism.web_evaluation import _url
from scripts.audit_dev_fetch_balance import BASE, DEV, RUNS, OUT, four_domain_order, path_relevant_order, sources_cache
from scripts.replay_saved_dev_search import FrozenSearch, CachedPages, load_queries, annotate_replay, PAGE_CACHE
from scripts.run_dev_query_probe import dump
from scripts.analyze_web_dev_checks import copied_target_tokens


def module_from_source(source):
    module = ModuleType("offline_fetch_probe")
    module.__file__ = str(ROOT / "plagiarism/web_search.py")
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def replacement_cap_source(source):
    setup = "link_discoveries, linked_urls = [], set()"
    full = "if len(ordered) >= MAX_URLS:"
    pop = "displaced = ordered.pop(replaceable[-1])"
    if any(source.count(marker) != 1 for marker in (setup, full, pop)):
        raise ValueError("Rebase the one-replacement budget hypothesis")
    return (source.replace(setup, setup + "\n    followup_replacements = 0")
            .replace(full, full + "\n                        if followup_replacements >= 1:\n                            continue")
            .replace(pop, pop + "\n                        followup_replacements += 1"))


def productive_budget_source(source):
    setup = "link_discoveries, linked_urls = [], set()"
    full = "if len(ordered) >= MAX_URLS:"
    pop = "displaced = ordered.pop(replaceable[-1])"
    branch = "            if query_strategy in (CITATION_STRATEGY, COVERAGE_STRATEGY, DENSE_STRATEGY) and url not in linked_urls and not fully_covered:"
    if any(source.count(marker) != 1 for marker in (setup, full, pop, branch)):
        raise ValueError("Rebase the productive replacement-credit hypothesis")
    linked_outcome = '''            if url in linked_urls:
                previous_link_words = _word_coverage(text, confirmed_matches)["matched_words"]
                linked_matches = analyze(text, [sources[-1]])["matches"]
                confirmed_matches.extend(linked_matches)
                linked_coverage = _word_coverage(text, confirmed_matches)
                if url in priority_link_urls and linked_coverage["matched_words"] > previous_link_words:
                    priority_link_credit = 1
                fully_covered = linked_coverage["matched_words"] == linked_coverage["total_words"]
'''
    return (source.replace(setup, setup + "\n    priority_link_credit, priority_link_urls = 1, set()")
        .replace(branch, linked_outcome + branch)
        .replace(full, full + "\n                        if priority_link_credit < 1:\n                            continue")
        .replace(pop, pop + "\n                        priority_link_credit -= 1\n                        priority_link_urls.add(related)"))


def describe(report, case):
    tokens = _tokens(report["text"])
    self_url = case.get("published_target", {}).get("url")
    self_url = _url(self_url) if self_url else None
    exact = [m for m in report["matches"] if m["kind"] == "exact" and _url(m["source_url"]) != self_url]
    expected = []
    for gold in case["expected_evidence"]:
        if not gold["needs_lexical_highlight"]:
            continue
        wanted = copied_target_tokens(tokens, gold)
        expected.append({"words": len(wanted), "covered": sum(any(m["start"] <= t.start and t.end <= m["end"] for m in exact) for t in wanted)})
    return {"matched_words": report["stats"]["matched_words"], "expected_lexical_coverage": expected,
        "sources": [s["url"] for s in report["sources"]], "attempted_urls": report["web"]["attempted_urls"],
        "linked_urls": report["web"]["link_discoveries"], "replay": report["offline_replay"],
        "matches": [{k: m.get(k) for k in ("kind", "source_url", "excerpt", "source_excerpt", "start", "end")} for m in report["matches"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default="cc9444ee58a3ae9dbed9e1139b2c9af17c36af71", help="Explicit uncapped baseline commit; never silently use current HEAD")
    parser.add_argument("--experiment", choices=("fixed-caps", "productive"), default="productive")
    args = parser.parse_args()
    comparison = json.loads((OUT / "comparison.json").read_text())
    latest = {}
    for row in comparison["cases"]:
        if row["status"] == "ready" and (row["id"] not in latest or row["created_at"] > latest[row["id"]]["created_at"]):
            latest[row["id"]] = row
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only fixed DEV cases are accepted")
    by_id = {c["id"]: c for c in cases}
    commit = subprocess.check_output(["git", "rev-parse", "--verify", args.snapshot + "^{commit}"], cwd=ROOT, text=True).strip()
    source = subprocess.check_output(["git", "show", commit + ":plagiarism/web_search.py"], cwd=ROOT, text=True)
    predicate = 'if related and not fully_covered and not _word_coverage(text, page_matches)["matched_words"]:'
    if source.count(predicate) != 1:
        raise ValueError("Rebase the single follow-up-budget hypothesis on this snapshot")
    baseline = module_from_source(source)
    cap_one = module_from_source(source.replace(predicate, predicate.replace("if related and ", "if related and len(linked_urls) < 1 and ")))
    replacement_one = module_from_source(replacement_cap_source(source))
    productive = module_from_source(productive_budget_source(source))
    directories = [BASE / name for name in RUNS if (BASE / name / "run.json").exists()]
    cache = sources_cache([BASE / "dev-live-30-v1"] + directories)
    pool = {url: item["page"] for url, item in cache.items()}
    denied = []
    def no_network(event, arguments):
        if event.startswith("socket."):
            denied.append(event)
            raise PermissionError("Saved DEV replay has no network")
    sys.addaudithook(no_network)
    rows = []
    for cid in sorted(latest):
        row = latest[cid]
        case = by_id[cid]
        text = (DEV / case["input_path"]).read_text()
        if sha256(text.encode()).hexdigest() != case["text_sha256"]:
            raise ValueError("Changed DEV input")
        directory = BASE / row["run"]
        records, identity = load_queries(directory, cid, case["text_sha256"])
        results = {}
        policies = (("current", "productive_budget", "path_productive_budget") if args.experiment == "productive" else
                    ("current", "prefix4", "path", "cap1", "path_cap1", "replacement_cap1", "path_replacement_cap1"))
        for policy in policies:
            module = (productive if policy in ("productive_budget", "path_productive_budget") else
                      cap_one if policy in ("cap1", "path_cap1") else
                      replacement_one if policy in ("replacement_cap1", "path_replacement_cap1") else baseline)
            search = FrozenSearch(records)
            pages = CachedPages(deepcopy(pool), search.allowed_urls, cache_directory=PAGE_CACHE,
                                fill_missing=False, refresh_missing_links=False)
            previous = module._fetch_order
            def selector(urls, groups, strategy):
                raw = [[{"url": url} for url in group] for group in groups]
                return four_domain_order(raw) if policy == "prefix4" else path_relevant_order(raw, text)
            with patch.object(module, "_fetch_order", previous if policy in ("current", "cap1", "replacement_cap1", "productive_budget") else selector):
                report = module.run(text, request_id=sha256((commit + cid + policy).encode()).hexdigest()[:32],
                    searcher=search, fetcher=pages, query_strategy=module.DENSE_STRATEGY,
                    url_strategy=module.BALANCED_URL_STRATEGY)
            if denied or pages.fresh_attempts or search.missing_queries:
                raise ValueError("Replay lost its frozen-data guarantee")
            report["web"]["url_strategy"] = "diagnostic-" + policy
            annotate_replay(report, search, pages, {"source_run": row["run"], "policy": policy,
                "snapshot_commit": commit, "query_identity": identity, "new_searches": 0})
            results[policy] = describe(report, case)
            if cid in ("D04", "D09", "D12", "D30"):
                dump(OUT / (cid + "-" + policy + "-replay.json"), report)
        actual = json.loads((directory / (cid + ".json")).read_text())
        output = {"id": cid, "source_run": row["run"], "actual_fresh_matched_words": actual["stats"]["matched_words"], "policies": results}
        rows.append(output)
        dump(OUT / (cid + "-replay-comparison.json"), output)
        print(cid, "fresh", actual["stats"]["matched_words"], "replay", {name: result["matched_words"] for name, result in results.items()}, flush=True)
    result = {"schema_version": 1, "snapshot_commit": commit, "experiment": args.experiment, "new_searches": 0, "new_fetches": 0,
        "scope": "All latest captured DEV cases, frozen search and page/link snapshots. Cache/link misses remain explicit and are not fetched.",
        "cases": rows}
    dump(OUT / ("dynamic-productive-budget-comparison.json" if args.experiment == "productive" else "dynamic-replay-comparison.json"), result)


if __name__ == "__main__":
    main()
