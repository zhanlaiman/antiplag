#!/usr/bin/env python3
"""Compare fixed fetch-order hypotheses on captured DEV search responses only.

No provider, loader, final collection, or unfamiliar holdout is used. Correct
URLs enter scoring after orders have been generated from results/text alone.
"""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens, analyze
from plagiarism.web_evaluation import _url
from plagiarism.web_search import MAX_URLS, _canonical_url
from scripts.compare_saved_fetch_order import domain_order, query_order, hybrid_order
from scripts.run_dev_query_probe import dump

BASE = ROOT / "data/private/web-evaluation"
DEV = BASE / "materials/dev-30"
RUNS = ("dev-dense-six-live-v1", "dev-dense-balanced-live-v2", "dev-final-control-live-v1",
        "dev-selected-remaining-a-v1", "dev-selected-remaining-b-v1",
        "dev-selected-remaining-c-v1", "dev-selected-remaining-d-v1")
OUT = BASE / "fetch-balance-dev-v1"


def four_domain_order(groups):
    ordered = domain_order(groups)[:MAX_URLS // 3]
    seen = set(ordered)
    for url in query_order(groups):
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def path_word_score(url, text):
    document_words = {t.value for t in _tokens(text) if len(t.value) >= 4 and not t.value.isdecimal()}
    path_words = {t.value for t in _tokens(unquote(urlsplit(url).path)) if len(t.value) >= 4 and not t.value.isdecimal()}
    return len(document_words & path_words)


def path_relevant_order(groups, text):
    # Stable ties retain the current domain/query balance. No title, source
    # labels, source text, or expected URL is provided to this selector.
    previous = hybrid_order(groups)
    document_words = {t.value for t in _tokens(text) if len(t.value) >= 4 and not t.value.isdecimal()}
    def relevance(url):
        words = {t.value for t in _tokens(unquote(urlsplit(url).path)) if len(t.value) >= 4 and not t.value.isdecimal()}
        return len(document_words & words)
    return sorted(previous, key=lambda url: -relevance(url))


def load_groups(directory, identifier, planned):
    groups, details = [], []
    for index in range(planned):
        call = sha256((identifier + "\0" + str(index)).encode()).hexdigest()
        path = directory / (call + "-search.json")
        if not path.exists():
            return None, {"missing": index}
        record = json.loads(path.read_text())
        if record.get("request_id") != call or record.get("state") != "complete":
            return None, {"not_complete": index, "state": record.get("state")}
        groups.append(record["results"])
        details.append({"query_index": index + 1, "query": record["query"], "results": record["results"]})
    return groups, details


def sources_cache(directories):
    cached = {}
    for directory in directories:
        report_paths = list(directory.glob("D[0-3][0-9].json"))
        if directory.name == "dev-d02-clause-pair-live-v1":
            report_paths.extend(directory / (variant + ".json") for variant in ("original", "clause") if (directory / (variant + ".json")).exists())
        for path in sorted(report_paths):
            report = json.loads(path.read_text())
            for source in report.get("sources", []):
                if source.get("representation_of"):
                    continue
                cached[_url(source["url"])] = {"source": source, "artifact": str(path.relative_to(ROOT)),
                    "page": {"url": source["url"], "requested_url": source["url"], "title": source["title"],
                             "text": source["text"], "text_sha256": sha256(source["text"].encode()).hexdigest(),
                             "_replay_origin": {"path": str(path.relative_to(ROOT)), "source": "saved_report_without_link_metadata"}}}
        for path in sorted(directory.glob("page-*-fetch.json")):
            record = json.loads(path.read_text())
            if record.get("state") != "complete":
                continue
            fetched = record["fetched"]
            source = {"id": "web-" + sha256(fetched["url"].encode()).hexdigest()[:24],
                      "url": fetched["url"], "title": fetched["title"], "text": fetched["text"]}
            for url in (record["requested_url"], fetched["url"]):
                cached[_url(url)] = {"source": source, "artifact": str(path.relative_to(ROOT)), "page": fetched}
    return cached


def expected_rows(case, orders, same_report, cache):
    read = {_url(source["url"]) for source in same_report.get("sources", [])} if same_report else set()
    rows = []
    for index, gold in enumerate(case["expected_evidence"]):
        wanted = {_url(url) for url in gold["source_urls"]}
        rows.append({"fragment_index": index, "source_id": gold["source_id"], "lexical": gold["needs_lexical_highlight"],
            "url_present": bool(wanted & set(orders["current_hybrid6"])),
            "same_snapshot_content_saved": bool(wanted & read), "any_dev_cache_content_saved": bool(wanted & set(cache)),
            "orders": {name: {"positions_1based": [i + 1 for i, url in enumerate(order) if url in wanted],
                               "selected_first12": bool(wanted & set(order[:MAX_URLS]))} for name, order in orders.items()}})
    return rows


def summarize(rows):
    counts, changes = Counter(), {name: {"gained": [], "lost": []} for name in ("prefix4", "path_relevant")}
    for row in rows:
        if row["status"] != "ready":
            continue
        counts["snapshots"] += 1
        unique_sources = {}
        for evidence in row["evidence"]:
            if not evidence["url_present"]:
                continue
            unique_sources.setdefault(evidence["source_id"], evidence)
            if not evidence["lexical"]:
                continue
            counts["lexical_fragments_with_expected_url"] += 1
            old = evidence["orders"]["current_hybrid6"]["selected_first12"]
            for name in ("current_hybrid6", "prefix4", "path_relevant"):
                counts[name + "_lexical_selected"] += evidence["orders"][name]["selected_first12"]
            for name in ("prefix4", "path_relevant"):
                new = evidence["orders"][name]["selected_first12"]
                if old != new:
                    changes[name]["gained" if new else "lost"].append({"run": row["run"], "id": row["id"], "fragment_index": evidence["fragment_index"]})
        counts["expected_sources_with_url"] += len(unique_sources)
        for evidence in unique_sources.values():
            for name in ("current_hybrid6", "prefix4", "path_relevant"):
                counts[name + "_expected_sources_selected"] += evidence["orders"][name]["selected_first12"]
        for source in row["observed_exact_sources"]:
            old = any(position <= MAX_URLS for position in source["positions_1based"]["current_hybrid6"])
            if old:
                counts["observed_exact_source_urls_selected_current"] += 1
                for name in ("prefix4", "path_relevant"):
                    new = any(position <= MAX_URLS for position in source["positions_1based"][name])
                    counts[name + "_observed_exact_sources_retained"] += new
                    if not new:
                        changes[name].setdefault("observed_source_losses", []).append({"run": row["run"], "id": row["id"], "url": source["url"]})
    return {"counts": dict(counts), "changes": changes}


def main():
    OUT.mkdir(mode=0o700, exist_ok=True)
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only fixed DEV cases are accepted")
    by_id = {c["id"]: c for c in cases}
    directories = [BASE / name for name in RUNS if (BASE / name / "run.json").exists()]
    configurations = [(directory, json.loads((directory / "run.json").read_text()), None) for directory in directories]
    pair = BASE / "dev-d02-clause-pair-live-v1"
    if (pair / "run.json").exists():
        configurations.append((pair, json.loads((pair / "run.json").read_text()), ("original", "clause")))
    cache = sources_cache([BASE / "dev-live-30-v1"] + [item[0] for item in configurations])
    rows = []
    for directory, run, variants in configurations:
        entries = [("D02", variant) for variant in variants] if variants else [(cid, cid) for cid in run["case_ids"]]
        for cid, stem in entries:
            if cid not in by_id:
                raise ValueError("Non-DEV captured case")
            request = directory / (stem + "-request.json")
            if not request.exists():
                rows.append({"run": directory.name, "id": cid, "status": "not_submitted"})
                continue
            submission = json.loads(request.read_text())
            groups, details = load_groups(directory, submission["request_id"], submission["planned_queries"])
            if groups is None:
                rows.append({"run": directory.name, "id": cid, "status": "search_incomplete", "details": details})
                continue
            text = (DEV / by_id[cid]["input_path"]).read_text()
            if sha256(text.encode()).hexdigest() != by_id[cid]["text_sha256"]:
                raise ValueError("Changed DEV input")
            # All orders are constructed before accessing expected evidence.
            orders = {"domain": domain_order(groups), "query": query_order(groups),
                      "current_hybrid6": hybrid_order(groups), "prefix4": four_domain_order(groups),
                      "path_relevant": path_relevant_order(groups, text)}
            orders = {name: [_url(url) for url in order] for name, order in orders.items()}
            report_path = directory / (stem + ".json")
            report = json.loads(report_path.read_text()) if report_path.exists() else None
            observed = []
            for url in sorted({_url(m["source_url"]) for m in report.get("matches", []) if m["kind"] == "exact"}) if report else []:
                aliases = {alias for alias, entry in cache.items() if _url(entry["source"]["url"]) == url} | {url}
                positions = {name: [i + 1 for i, value in enumerate(order) if value in aliases] for name, order in orders.items()}
                observed.append({"url": url, "positions_1based": positions,
                    "path_score": path_word_score(url, text), "direct_search_url": bool(positions["current_hybrid6"])})
            rows.append({"run": directory.name, "created_at": run["created_at"], "id": cid, "status": "ready",
                "variant": stem if stem != cid else None,
                "orders": orders, "query_records": details, "report_saved": report is not None,
                "evidence": expected_rows(by_id[cid], orders, report, cache), "observed_exact_sources": observed,
                "changed_unloaded_urls_path_candidate": [url for url in orders["path_relevant"][:MAX_URLS]
                    if url not in orders["current_hybrid6"][:MAX_URLS] and url not in cache]})
    latest = {}
    for row in rows:
        if row["status"] == "ready" and (row["id"] not in latest or row["created_at"] > latest[row["id"]]["created_at"]):
            latest[row["id"]] = row
    result = {"schema_version": 1, "new_searches": 0, "new_fetches": 0,
        "metric_scope": "Initial search URL selection only; existing follow-up insertion can further change actual loads. Uncached URLs permit position-only conclusions.",
        "all_captured_snapshots": summarize(rows), "latest_per_document": summarize(list(latest.values())),
        "unique_dev_documents_ready": len(latest), "cases": rows}
    dump(OUT / "comparison.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
