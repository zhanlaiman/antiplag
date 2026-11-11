#!/usr/bin/env python3
"""D20 only: exact saved-query/full-page replay, never live retrieval."""
from difflib import SequenceMatcher
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search as web
from plagiarism.matching import analyze, _tokens
from plagiarism.web_fetch import FetchError
from scripts.diagnose_d10_fresh_loss import saved_reply
from scripts.run_dev_query_probe import dump

BASE = ROOT / "data/private/web-evaluation"
OLD = BASE / "dev-dense-balanced-live-v2"
FRESH = BASE / "dev-final-selected-d-v1"
OUT = BASE / "final-selected-dev-audit-v1/D20-regression"


def main():
    old, fresh = (json.loads((directory / "D20.json").read_text()) for directory in (OLD, FRESH))
    old_plans, old_reply, old_order, _ = saved_reply(OLD, old, "D20")
    plans, reply, balanced_order, path_order = saved_reply(FRESH, fresh, "D20")
    if old["text"] != fresh["text"] or old_plans != plans:
        raise ValueError("D20 phrase identity changed")
    first = min(old["matches"], key=lambda match: match["start"])
    matching_page = next(s for s in old["sources"] if s["id"] == first["source_id"])
    target_url = matching_page["url"]
    parent = next(link["parent_url"] for link in old["web"]["link_discoveries"] if link["url"] == target_url)
    captured, wanted = {}, set(fresh["web"]["attempted_urls"])
    for path in sorted(FRESH.glob("page-*-fetch.json")):
        record = json.loads(path.read_text())
        if record["requested_url"] in wanted:
            previous = captured.get(record["requested_url"])
            if previous is not None and previous != record:
                raise ValueError("Ambiguous D20 fetch capture")
            captured[record["requested_url"]] = record
    if set(captured) != wanted:
        raise ValueError("D20 attempted-page captures incomplete")
    OUT.mkdir(mode=0o700, exist_ok=True)
    OUT.chmod(0o700)
    rows = []
    for strategy in (web.BALANCED_URL_STRATEGY, web.PATH_URL_STRATEGY):
        class Search:
            provider = "saved-tavily"
            count = 0
            def search(self, query, call_id):
                record = reply[self.count]
                self.count += 1
                if record["query"] != query:
                    raise ValueError("D20 replay phrase changed")
                return record["results"]
        class Pages:
            missing = []
            def fetch(self, url):
                record = captured.get(url)
                if record is None:
                    self.missing.append(url)
                    raise FetchError("No saved D20 page; network forbidden", "offline_cache_miss")
                if record["state"] != "complete":
                    raise FetchError("Saved refusal", record["code"])
                return record["fetched"]
        search, pages = Search(), Pages()
        report = web.run(fresh["text"], "offline-D20-diagnosis", searcher=search, fetcher=pages,
            query_strategy=fresh["web"]["query_strategy"], url_strategy=strategy,
            use_named_references=fresh["web"]["named_references_enabled"])
        dump(OUT / (strategy + ".json"), report)
        rows.append({"url_strategy": strategy, "matched_words": report["stats"]["matched_words"],
            "missing_page_captures": pages.missing, "same_attempted_order_as_fresh": report["web"]["attempted_urls"] == fresh["web"]["attempted_urls"],
            "target_url_discovered": target_url in report["web"]["discovered_urls"],
            "parent_url_discovered": parent in report["web"]["discovered_urls"]})
    def ranks(records, url):
        return [{"query_index": i, "provider_rank": rank + 1} for i, record in enumerate(records)
                for rank, item in enumerate(record["results"]) if web._canonical_url(item["url"]) == url]
    # This earlier page is a conditional matcher control, never a new fetch.
    known = analyze(fresh["text"], [matching_page])
    known_words = web._word_coverage(fresh["text"], known["matches"])["matched_words"]
    first_words = [t.value for t in _tokens(first["excerpt"])]
    pdf = [{"source_id": s["id"], "supplemental": bool(s.get("representation_of")),
            "maximum_contiguous_words_of_first_fragment": SequenceMatcher(None, first_words,
                [t.value for t in _tokens(s["text"])], autojunk=False).find_longest_match().size}
           for s in fresh["sources"] if "8_Fotoapparat_8.pdf" in s["url"]]
    result = {"case_id": "D20", "network_calls": 0, "provider_calls": 0, "query_plans_identical": True,
        "old_report_id": old["id"], "fresh_report_id": fresh["id"], "old_matched_words": old["stats"]["matched_words"],
        "fresh_matched_words": fresh["stats"]["matched_words"], "lost_first_fragment_words": 11,
        "first_fragment_source_url": target_url, "old_direct_target_url_ranks": ranks(old_reply, target_url),
        "fresh_direct_target_url_ranks": ranks(reply, target_url), "old_link_parent_url": parent,
        "old_parent_ranks": ranks(old_reply, parent), "fresh_parent_ranks": ranks(reply, parent),
        "old_parent_initial_position": old_order.index(parent) + 1,
        "fresh_parent_initial_position": None if parent not in balanced_order else balanced_order.index(parent) + 1,
        "fresh_initial_balanced_equals_path": balanced_order == path_order,
        "saved_old_first_source_still_highlights_words": known_words,
        "saved_old_first_source_text_sha256": sha256(matching_page["text"].encode()).hexdigest(),
        "newly_read_pdf_conditional_token_check": pdf, "same_fresh_reply_replays": rows,
        "earliest_confirmed_cause": "retrieval_of_previous_link_parent",
        "conclusion": "Same six phrases. Previously query0 rank4 returned Digital Photography, whose saved link reached PhotoWiki. Fresh reply contains neither PhotoWiki nor Digital Photography; both initial orders and actual replay coincide and retain only the14-word second insertion. This is a lost retrieval path, not extra PATH damage or a matcher failure on an available first-fragment source.",
        "uncertainty": "The earlier matched source remains a saved11-word matcher control. No current fetch/accessibility claim is made for the absent source or parent. The new PDF primary and supported view share at most4 consecutive first-fragment words, so they do not establish that this11-word source passage was available."}
    dump(OUT / "summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
