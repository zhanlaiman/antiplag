#!/usr/bin/env python3
"""D10 only: replay the two saved replies; no provider, Fetcher, or final data."""
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search as web
from plagiarism.matching import analyze
from plagiarism.web_fetch import FetchError
from scripts.run_dev_query_probe import dump

BASE = ROOT / "data/private/web-evaluation"
OLD = BASE / "dev-final-control-live-v1"
FRESH = BASE / "dev-final-selected-b-v1"
OUT = BASE / "final-selected-dev-audit-v1/D10-regression"


def saved_reply(directory, report, case_id="D10"):
    rid = json.loads((directory / (case_id + "-request.json")).read_text())["request_id"]
    plans = web.plan_queries(report["text"], strategy=report["web"]["query_strategy"])
    records, groups, urls = [], [], []
    for index, plan in enumerate(plans):
        call_id = sha256((rid + "\0" + str(index)).encode()).hexdigest()
        record = json.loads((directory / (call_id + "-search.json")).read_text())
        if record["state"] != "complete" or record["query"] != plan["query"]:
            raise ValueError("Saved D10 provider call does not match its exact blind plan")
        records.append(record)
        group = [web._canonical_url(r["url"]) for r in record["results"]]
        groups.append(group)
        for url in group:
            if url and url not in urls:
                urls.append(url)
    balanced = web._fetch_order(urls, groups, web.BALANCED_URL_STRATEGY)
    return plans, records, balanced, web._rank_url_paths(balanced, report["text"])


def main():
    old, fresh = (json.loads((d / "D10.json").read_text()) for d in (OLD, FRESH))
    if old["text"] != fresh["text"]:
        raise ValueError("D10 target changed")
    old_plans, old_reply, old_order, _ = saved_reply(OLD, old)
    fresh_plans, fresh_reply, balanced_order, path_order = saved_reply(FRESH, fresh)
    if old_plans != fresh_plans:
        raise ValueError("D10 query plans changed; rank-only diagnosis would be invalid")
    # The previously confirmed alternate page is identified by actual old pairs,
    # not by any gold-source URL. Expected URL scoring is done only below.
    matched_ids = {m["source_id"] for m in old["matches"] if m["kind"] == "exact"}
    old_matched_pages = [s for s in old["sources"] if s["id"] in matched_ids]
    if len(old_matched_pages) != 1:
        raise ValueError("The frozen old D10 counterexample no longer has one matched page")
    relevant = old_matched_pages[0]["url"]
    records_by_url = {}
    wanted = set(fresh["web"]["attempted_urls"])
    for path in sorted(FRESH.glob("page-*-fetch.json")):
        record = json.loads(path.read_text())
        url = record["requested_url"]
        if url in wanted:
            if url in records_by_url and records_by_url[url] != record:
                raise ValueError("Ambiguous saved fetch response for D10 URL")
            records_by_url[url] = record
    if set(records_by_url) != wanted:
        raise ValueError("Fresh attempted-page captures are incomplete")
    rows = []
    OUT.mkdir(mode=0o700, exist_ok=True)
    OUT.chmod(0o700)
    for strategy in (web.BALANCED_URL_STRATEGY, web.PATH_URL_STRATEGY):
        class Search:
            provider = "saved-tavily"
            count = 0
            def search(self, query, request_id):
                record = fresh_reply[self.count]
                self.count += 1
                if query != record["query"]:
                    raise ValueError("Replay changed a D10 query")
                return record["results"]
        class Pages:
            missing = []
            def fetch(self, url):
                record = records_by_url.get(url)
                if record is None:
                    self.missing.append(url)
                    raise FetchError("Saved page absent; no network fallback", "offline_cache_miss")
                if record["state"] != "complete":
                    raise FetchError("Replayed saved refusal", record["code"])
                return record["fetched"]
        pages, search = Pages(), Search()
        replay = web.run(fresh["text"], "offline-D10-rank-diagnosis", searcher=search, fetcher=pages,
                         query_strategy=fresh["web"]["query_strategy"], url_strategy=strategy,
                         use_named_references=fresh["web"]["named_references_enabled"], use_zero_fallback=False)
        if search.count != 6:
            raise ValueError("Replay did not use exactly the saved six responses")
        dump(OUT / (strategy + ".json"), replay)
        rows.append({"url_strategy": strategy, "matched_words": replay["stats"]["matched_words"],
            "relevant_url_attempted": relevant in replay["web"]["attempted_urls"],
            "missing_page_captures": pages.missing, "attempted_urls": replay["web"]["attempted_urls"],
            "same_attempted_url_set_as_fresh": set(replay["web"]["attempted_urls"]) == wanted,
            "same_attempted_url_order_as_fresh": replay["web"]["attempted_urls"] == fresh["web"]["attempted_urls"]})
    control = analyze(fresh["text"], old_matched_pages)
    conditional_words = web._word_coverage(fresh["text"], control["matches"])["matched_words"]
    def ranks(reply):
        return [{"query_index": i, "provider_rank": rank + 1}
                for i, q in enumerate(reply) for rank, value in enumerate(q["results"])
                if web._canonical_url(value["url"]) == relevant]
    result = {"case_id": "D10", "network_calls": 0, "provider_calls": 0,
        "old_report_id": old["id"], "fresh_report_id": fresh["id"],
        "old_matched_words": old["stats"]["matched_words"], "fresh_matched_words": fresh["stats"]["matched_words"],
        "text_sha256": sha256(fresh["text"].encode()).hexdigest(), "query_plans_identical": True,
        "alternate_matching_url": relevant, "old_reply_ranks": ranks(old_reply), "fresh_reply_ranks": ranks(fresh_reply),
        "old_balanced_initial_position": old_order.index(relevant) + 1,
        "old_actual_attempt_position": old["web"]["attempted_urls"].index(relevant) + 1,
        "fresh_balanced_initial_position": balanced_order.index(relevant) + 1,
        "fresh_path_initial_position": path_order.index(relevant) + 1,
        "saved_old_source_still_highlights_words": conditional_words,
        "conditional_source_sha256": sha256(old_matched_pages[0]["text"].encode()).hexdigest(),
        "fresh_balanced_vs_path_replay": rows,
        "conclusion": "Same phrases, changed provider rank. Alternate matching page remains discovered but both fresh orders place it19>12. Fresh saved-reply BALANCED and PATH both lose it; saved old page still matches11 words. No fresh extraction or matching failure was observed because this page was not read.",
        "uncertainty": "No claim about current content/accessibility of the unattempted page; matching uses the earlier saved snapshot. The original annotated PhotoWiki URL is absent from both replies; the recovered11 words had come from a valid alternate source."}
    dump(OUT / "summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
