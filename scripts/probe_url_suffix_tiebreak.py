#!/usr/bin/env python3
"""One frozen Russian-inflection tie break, DEV saved-data only, no network."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search as web
from plagiarism.matching import _tokens
from plagiarism.web_fetch import FetchError
from scripts.analyze_web_dev_checks import inspect, compare
from scripts.audit_dev_fetch_balance import BASE, DEV, RUNS, OUT as FETCH_OUT, sources_cache
from scripts.replay_saved_dev_search import PAGE_CACHE, FrozenSearch, load_queries, canonical, annotate_replay
from scripts.replay_final_feature_ablations import FeaturePages, load_pdf_payloads, load_named_payload
from scripts.run_dev_query_probe import dump

RULE = ROOT / "docs/baselines/url-suffix-tiebreak-2026-11-12.json"
RULE_SHA = "b8aa0f08cad11c80c2bd98248e00ec92c98b7154229be1205d88e4e08f06ef65"
OUT = BASE / "url-suffix-tiebreak-v1"


def load_rule():
    raw = RULE.read_bytes()
    if sha256(raw).hexdigest() != RULE_SHA:
        raise ValueError("Frozen suffix rule changed; do not retune after case outcomes")
    return json.loads(raw)


def fold(word, rule):
    if not re.fullmatch("[а-яё]+", word):
        return None
    word = word.replace("ё", "е")
    for ending in sorted(rule["suffixes"], key=lambda value: -len(value)):
        if word.endswith(ending) and len(word) - len(ending) >= rule["minimum_root_characters"]:
            return word[:-len(ending)]
    return word


def rank_with_rule(ordered, text, rule):
    words = {t.value for t in _tokens(text) if len(t.value) >= 4 and not t.value.isdecimal()}
    folded = {root for word in words if (root := fold(word, rule)) is not None}
    def score(url):
        path_words = {t.value for t in _tokens(unquote(urlsplit(url).path)) if len(t.value) >= 4 and not t.value.isdecimal()}
        path_roots = {root for word in path_words if (root := fold(word, rule)) is not None}
        return (-len(words & path_words), -len(folded & path_roots))
    return sorted(ordered, key=score)


def selected_baseline_crosscheck(result):
    """Compare current replay to the previously frozen selected-feature replay."""
    count = 0
    for row in result["cases"]:
        if row["cohort"] != "previous30":
            continue
        path = BASE / "dev-final-feature-ablations-v1/plus_named_references" / (row["id"] + ".json")
        expected = json.loads(path.read_text())["stats"]["matched_words"]
        row["current_matches_frozen_selected_feature_replay"] = row["variants"]["current"]["matched_words"] == expected
        count += row["current_matches_frozen_selected_feature_replay"]
    if count != 30:
        raise ValueError("Current control did not reproduce all thirty frozen selected-feature reports")
    result["summaries"]["previous30"]["current_reproduces_selected_feature_baseline"] = count
    return result


def main():
    rule = load_rule()
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only DEV30 is permitted")
    by_id = {c["id"]: c for c in cases}
    captured = json.loads((FETCH_OUT / "comparison.json").read_text())
    latest = {}
    for row in captured["cases"]:
        if row["status"] == "ready" and (row["id"] not in latest or row["created_at"] > latest[row["id"]]["created_at"]):
            latest[row["id"]] = row
    if set(latest) != set(by_id) or any(r["run"] not in RUNS for r in latest.values()):
        raise ValueError("Expected the frozen prior30 captured searches")
    cohorts = {"previous30": {cid: BASE / row["run"] for cid, row in latest.items()},
               "fresh12": {f"D{i:02d}": BASE / f"dev-final-selected-{'a' if i <= 6 else 'b'}-v1" for i in range(1, 13)}}
    if any(not (directory / (cid + ".json")).exists() for cid, directory in cohorts["fresh12"].items()):
        raise ValueError("Fixed freshD01–D12 not all ready; never silently substitute a cohort")
    directories = [BASE / name for name in RUNS]
    pool = {url: entry["page"] for url, entry in sources_cache([BASE / "dev-live-30-v1", *directories]).items()}
    pdf_pages, _ = load_pdf_payloads()
    d14 = by_id["D14"]
    named_page, _ = load_named_payload(d14, (DEV / d14["input_path"]).read_text())
    pool[canonical(named_page["url"])] = named_page
    # Load the same saved page-cache snapshots once; no fill or refresh exists.
    cache_failures = {}
    for path in PAGE_CACHE.glob("*.json"):
        record = json.loads(path.read_text())
        if record.get("state") == "complete":
            page = record["fetched"]
            pool[canonical(record["requested_url"])] = pool[canonical(page["url"])] = page
        else:
            cache_failures[canonical(record["requested_url"])] = record.get("code", "saved_page_outcome_unknown")
    denied = []
    def no_network(event, arguments):
        if event.startswith("socket."):
            denied.append(event)
            raise PermissionError("Suffix experiment forbids network")
    sys.addaudithook(no_network)
    current_rank = web._rank_url_paths
    summaries, all_rows = {}, []
    for cohort, entries in cohorts.items():
        rows = []
        for cid, directory in sorted(entries.items()):
            case = by_id[cid]
            observed = json.loads((directory / (cid + ".json")).read_text())
            text = observed["text"]
            if sha256(text.encode()).hexdigest() != case["text_sha256"]:
                raise ValueError("Changed DEV input")
            records, identity = load_queries(directory, cid, case["text_sha256"])
            case_pool, failures = deepcopy(pool), dict(cache_failures)
            fresh_captured = set()
            if cohort == "fresh12":
                wanted = set(observed["web"]["attempted_urls"])
                for path in sorted(directory.glob("page-*-fetch.json")):
                    record = json.loads(path.read_text())
                    url = record["requested_url"]
                    if url not in wanted:
                        continue
                    fresh_captured.add(canonical(url))
                    if record.get("state") == "complete":
                        page = record["fetched"]
                        case_pool[canonical(url)] = case_pool[canonical(page["url"])] = page
                        failures.pop(canonical(url), None)
                    else:
                        failures[canonical(url)] = record.get("code", "saved_page_outcome_unknown")
                if fresh_captured != {canonical(u) for u in wanted}:
                    raise ValueError("Incomplete fresh attempted-page captures")
            item = {"id": cid, "cohort": cohort, "saved_run": directory.name, "variants": {}}
            for variant in ("current", "suffix_secondary"):
                search = FrozenSearch(records)
                class Pages(FeaturePages):
                    def fetch(self, url):
                        key = canonical(url)
                        if key in failures:
                            if key not in self.allowed_urls:
                                raise FetchError("Not discovered", "saved_url_not_discovered")
                            self.calls.append(key)
                            self.page_load_events.append({"requested_url": key, "state": "failed_or_unknown", "origin": "saved_refusal", "code": failures[key]})
                            raise FetchError("Saved refusal", failures[key])
                        return super().fetch(url)
                pages = Pages(deepcopy(case_pool), search.allowed_urls | {canonical(named_page["url"])},
                    cache_directory=OUT / "empty-no-network-cache", fill_missing=False, refresh_missing_links=False,
                    pdf_payloads=pdf_pages, enable_pdf=True)
                rank = current_rank if variant == "current" else lambda ordered, submitted: rank_with_rule(ordered, submitted, rule)
                with patch.object(web, "_rank_url_paths", rank):
                    report = web.run(text, request_id="suffix-offline-" + cid, searcher=search, fetcher=pages,
                        query_strategy=web.DENSE_STRATEGY, url_strategy=web.PATH_URL_STRATEGY, use_named_references=True)
                if denied or search.missing_queries or pages.fresh_attempts or pages.validation_errors:
                    raise ValueError("Frozen replay guarantee failed")
                report.update(id=sha256((cohort + cid + variant).encode()).hexdigest()[:32], created_at=datetime.now(timezone.utc).isoformat())
                annotate_replay(report, search, pages, {"case_id": cid, "cohort": cohort, "variant": variant,
                    "source_run": directory.name, "query_identity": identity, "main_store_written": False})
                output = OUT / cohort / variant
                output.mkdir(mode=0o700, parents=True, exist_ok=True)
                dump(output / (cid + ".json"), report)
                item["variants"][variant] = {"matched_words": report["stats"]["matched_words"],
                    "observed_matched_words": observed["stats"]["matched_words"],
                    "reproduces_observed_matched_words": report["stats"]["matched_words"] == observed["stats"]["matched_words"],
                    "attempted_urls": report["web"]["attempted_urls"], "missing_body_urls": pages.cache_misses,
                    "missing_link_metadata": sorted(pages.missing_links),
                    "nonfresh_page_attempts": [u for u in report["web"]["attempted_urls"] if cohort == "fresh12" and canonical(u) not in fresh_captured]}
            rows.append(item)
            print(cohort, cid, {k: v["matched_words"] for k, v in item["variants"].items()}, flush=True)
        before, after = (inspect(DEV, OUT / cohort / variant) for variant in ("current", "suffix_secondary"))
        comparison = compare(before, after)
        comparison["current_reproduces_observed_matched_words"] = sum(r["variants"]["current"]["reproduces_observed_matched_words"] for r in rows)
        comparison["page_placement_changes"] = [{"id": r["id"],
            "added_attempts": sorted(set(r["variants"]["suffix_secondary"]["attempted_urls"]) - set(r["variants"]["current"]["attempted_urls"])),
            "lost_attempts": sorted(set(r["variants"]["current"]["attempted_urls"]) - set(r["variants"]["suffix_secondary"]["attempted_urls"]))}
            for r in rows if set(r["variants"]["current"]["attempted_urls"]) != set(r["variants"]["suffix_secondary"]["attempted_urls"])]
        summaries[cohort] = comparison
        all_rows.extend(rows)
    result = {"schema_version": 1, "rule_sha256": RULE_SHA, "network_calls": 0, "new_search_queries": 0,
        "new_fetches": 0, "models": 0, "final_materials_read": False,
        "claim": "Conditional saved-data replay. Fresh12 page bodies use same-run captures where available; newly selected URLs may use older saved bodies or remain unknown. No fresh improvement claim.",
        "summaries": summaries, "cases": all_rows}
    selected_baseline_crosscheck(result)
    dump(OUT / "comparison.json", result)
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
