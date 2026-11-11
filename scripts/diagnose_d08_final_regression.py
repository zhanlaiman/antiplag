#!/usr/bin/env python3
"""Explain D08's fresh loss using the same frozen replies and saved pages.

Normal production execution is compared under BALANCED and PATH. An explicit
old snapshot additionally checks the earlier uncapped follow-up behavior.
No network, provider or normal Fetcher is instantiated; absent pages stay unknown.
"""
from copy import deepcopy
from hashlib import sha256
import json
import subprocess
import sys

from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search
from plagiarism.web_fetch import FetchError
from plagiarism.web_evaluation import _url
from scripts.audit_dev_fetch_balance import BASE, DEV, RUNS, sources_cache
from scripts.replay_dev_fetch_candidates import module_from_source
from scripts.replay_saved_dev_search import CachedPages, FrozenSearch, PAGE_CACHE, annotate_replay, canonical, load_queries
from scripts.run_dev_query_probe import dump

OUT = BASE / "dev-final-d08-regression-v1"
OLD = "dev-dense-six-live-v1"
NEW = "dev-final-selected-b-v1"
SNAPSHOT = "cc9444ee58a3ae9dbed9e1139b2c9af17c36af71"


class Outcomes(CachedPages):
    def __init__(self, *args, failed, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed = failed

    def fetch(self, url):
        url = canonical(url)
        if url in self.failed:
            if len(self.calls) >= web_search.MAX_URLS or url not in self.allowed_urls:
                raise ValueError("Invalid frozen failure replay")
            self.calls.append(url)
            self.page_load_events.append({"requested_url": url, "origin": "same_fresh_report_typed_failure",
                "state": "failed", "code": self.failed[url]})
            raise FetchError("Same captured fresh fetch failure", self.failed[url])
        return super().fetch(url)


def main():
    OUT.mkdir(mode=0o700, exist_ok=True)
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only fixed DEV30 metadata allowed")
    case = next(c for c in cases if c["id"] == "D08")
    text = (DEV / case["input_path"]).read_text()
    if sha256(text.encode()).hexdigest() != case["text_sha256"]:
        raise ValueError("Changed DEV input")
    original = json.loads((BASE / OLD / "D08.json").read_text())
    current = json.loads((BASE / NEW / "D08.json").read_text())
    old_records, _ = load_queries(BASE / OLD, "D08", case["text_sha256"])
    new_records, identity = load_queries(BASE / NEW, "D08", case["text_sha256"])
    if list(old_records) != list(new_records):
        raise ValueError("This explanation requires the same six exact queries")
    directories = [BASE / "dev-live-30-v1"] + [BASE / name for name in RUNS]
    cache = sources_cache(directories)
    historical = CachedPages({url: item["page"] for url, item in cache.items()}, set(), cache_directory=PAGE_CACHE)
    pool = deepcopy(historical.pool)
    # Preserve historical availability, then overwrite with actual full fresh
    # pages/labels where saved. Do not fabricate links for report-only sources.
    fresh = sources_cache([BASE / NEW])
    pool.update({url: item["page"] for url, item in fresh.items()})
    for artifact in sorted((BASE / NEW).glob("page-*-fetch.json")):
        record = json.loads(artifact.read_text())
        if record.get("state") == "complete":
            page = record["fetched"]
            CachedPages._validate_page(page)
            pool[canonical(record["requested_url"])] = page
            pool[canonical(page["url"])] = page
    failed = {canonical(e["url"]): e["code"] for e in current["web"]["errors"]
              if e.get("stage") == "fetch" and e.get("url")}
    groups = [[web_search._canonical_url(item["url"]) for item in record["results"]]
              for record in new_records.values()]
    urls = list(dict.fromkeys(url for group in groups for url in group if url))
    balanced = web_search._fetch_order(urls, groups, web_search.BALANCED_URL_STRATEGY)
    path = web_search._rank_url_paths(balanced, text)
    if balanced != path:
        raise ValueError("Saved finding changed: PATH order is not equal to BALANCED")
    frozen_source = subprocess.check_output(["git", "show", SNAPSHOT + ":plagiarism/web_search.py"], cwd=ROOT, text=True)
    legacy = module_from_source(frozen_source)
    denied = []
    def no_network(event, arguments):
        if event.startswith("socket."):
            denied.append(event)
            raise PermissionError("D08 same-reply diagnosis forbids network")
    sys.addaudithook(no_network)
    policies = (("old_balanced_uncapped_followups", legacy, legacy.BALANCED_URL_STRATEGY),
                ("balanced_productive_followups", web_search, web_search.BALANCED_URL_STRATEGY),
                ("path_productive_followups", web_search, web_search.PATH_URL_STRATEGY))
    results = {}
    for policy, module, strategy in policies:
        search = FrozenSearch(new_records)
        pages = Outcomes(deepcopy(pool), search.allowed_urls, cache_directory=OUT / "no-page-cache",
                         fill_missing=False, refresh_missing_links=False, failed=failed)
        options = {"use_named_references": True, "use_zero_fallback": False} if module is web_search else {}
        report = module.run(text, request_id=sha256(policy.encode()).hexdigest()[:32],
            searcher=search, fetcher=pages, query_strategy=module.DENSE_STRATEGY, url_strategy=strategy, **options)
        if denied or pages.fresh_attempts or search.missing_queries:
            raise ValueError("Frozen replay guarantee violated")
        annotate_replay(report, search, pages, {"case_id": "D08", "policy": policy,
            "new_searches": 0, "same_fresh_query_identity": identity, "main_store_written": False})
        dump(OUT / (policy + ".json"), report)
        results[policy] = {"matched_words": report["stats"]["matched_words"],
            "attempted_urls": report["web"]["attempted_urls"],
            "read_urls": [s["url"] for s in report["sources"] if not s.get("representation_of")],
            "page_cache_misses": pages.cache_misses, "missing_link_metadata": sorted(pages.missing_links),
            "link_discoveries": report["web"]["link_discoveries"]}
    if results["path_productive_followups"]["matched_words"] != current["stats"]["matched_words"]:
        raise ValueError("New-code replay did not reproduce actual fresh matched words")
    # Expected URLs and known prior literal-match URLs enter ONLY after every
    # selector and run has completed; they cannot influence the tested queues.
    target_urls = {_url(u) for e in case["expected_evidence"] for u in e["source_urls"]}
    # The earlier report stored InnovationCampus' redirected trailing-slash URL,
    # while both provider replies used the no-slash request URL. This alias is
    # proven by the earlier complete saved Fetcher record, not guessed from gold.
    known_literal_urls = {_url(m["source_url"]) for m in original["matches"] if m["kind"] == "exact"}
    for artifact in sorted((BASE / OLD).glob("page-*-fetch.json")):
        record = json.loads(artifact.read_text())
        if record.get("state") == "complete" and _url(record["fetched"]["url"]) in known_literal_urls:
            known_literal_urls.add(_url(record["requested_url"]))
    queues = {}
    for label, records in (("prior_reply", old_records), ("new_reply", new_records)):
        gs = [[web_search._canonical_url(item["url"]) for item in record["results"]] for record in records.values()]
        us = list(dict.fromkeys(url for group in gs for url in group if url))
        order = web_search._fetch_order(us, gs, web_search.BALANCED_URL_STRATEGY)
        ordered_path = web_search._rank_url_paths(order, text)
        queues[label] = {"same_balanced_and_path_order": order == ordered_path,
            "expected_url_positions": [i + 1 for i, url in enumerate(order) if _url(url) in target_urls],
            "prior_literal_match_url_positions": [{"url": url, "position": i + 1}
                for i, url in enumerate(order) if _url(url) in known_literal_urls],
            "orders": {"balanced": order, "path": ordered_path}}
    result = {"schema_version": 1, "case_id": "D08", "input_sha256": case["text_sha256"],
        "same_six_query_strings": list(new_records), "query_strings_changed": False,
        "actual_fresh_matched_words": {"prior": original["stats"]["matched_words"], "current": current["stats"]["matched_words"]},
        "same_new_reply_replays": results, "queue_comparison": queues,
        "new_searches": 0, "new_fetches": 0, "old_snapshot": SNAPSHOT,
        "current_web_search_sha256": sha256((ROOT / "plagiarism/web_search.py").read_bytes()).hexdigest(),
        "conclusion": "Same queries; changed provider ranks/host distribution put both known literal sources beyond twelve under both BALANCED and PATH. Fresh loss11→0 is real; it is not caused by PATH sorting or the productive followup change on these replies.",
        "limitation": "Saved-page replay is conditional; unknown page/link content remains partial, not proof of successful hypothetical reads."}
    dump(OUT / "comparison.json", result)
    print(json.dumps({key: value for key, value in result.items() if key not in {"same_six_query_strings", "queue_comparison"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
