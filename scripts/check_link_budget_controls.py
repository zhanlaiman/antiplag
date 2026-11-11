#!/usr/bin/env python3
"""Authored two-source navigation controls for frozen replacement hypotheses."""
import argparse
import subprocess
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.replay_dev_fetch_candidates import module_from_source, productive_budget_source, replacement_cap_source
from scripts.audit_dev_fetch_balance import OUT
from scripts.run_dev_query_probe import dump

FIRST = "Астроном проверил положение телескопа перед повторным наблюдением далёкого созвездия."
SECOND = "Лаборант измерил проводимость образца после полного охлаждения испытательной камеры."
HOST = ("Редактор составил пояснения к расписанию и подготовил карточки для школьников. "
        "Позже участники обсудили форму дневника и распределили дежурства на следующую неделю. "
        "Отдельную заметку решили посвятить маршруту прогулки возле здания музея.")
PARENTS = ("https://astronomy.test/start", "https://laboratory.test/start")
CHILDREN = ("https://astronomy.test/evidence", "https://laboratory.test/evidence")


class SavedSearch:
    provider = "authored-fixture"
    def __init__(self, full):
        self.full, self.calls = full, 0
        self.urls = list(PARENTS) + ([f"https://unrelated{i}.test/page" for i in range(10)] if full else [])
    def search(self, query, request_id):
        # At most five results, as in the real Basic adapter. Repeated calls
        # expose twelve unique URLs only in the full-queue scenario.
        start = self.calls * 5
        self.calls += 1
        return [{"url": self.urls[(start + i) % len(self.urls)]} for i in range(5 if self.full else 2)]


class SavedPages:
    def __init__(self, first_productive):
        self.first_productive, self.calls = first_productive, []
    def fetch(self, url):
        self.calls.append(url)
        if url == PARENTS[0]:
            return {"url": url, "text": "На странице приведены часы работы выставочного центра и расписание открытых встреч.",
                    "links": [{"url": CHILDREN[0], "text": "Положение телескопа"}]}
        if url == PARENTS[1]:
            return {"url": url, "text": "Здесь размещены сведения о времени работы научного музея и правилах посещения.",
                    "links": [{"url": CHILDREN[1], "text": "Испытательной камеры"}]}
        text = (FIRST if self.first_productive else "В таблице перечислены остановки автобуса и часы работы музея.") if url == CHILDREN[0] else SECOND if url == CHILDREN[1] else "В таблице перечислены остановки городского автобуса и часы работы музея."
        return {"url": url, "text": text, "links": []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default="cc9444ee58a3ae9dbed9e1139b2c9af17c36af71")
    args = parser.parse_args()
    source = subprocess.check_output(["git", "show", args.snapshot + ":plagiarism/web_search.py"], cwd=ROOT, text=True)
    predicate = 'if related and not fully_covered and not _word_coverage(text, page_matches)["matched_words"]:'
    modules = {"current": module_from_source(source),
        "flat_cap1": module_from_source(source.replace(predicate, predicate.replace("if related and ", "if related and len(linked_urls) < 1 and "))),
        "replacement_cap1": module_from_source(replacement_cap_source(source)),
        "productive_budget": module_from_source(productive_budget_source(source))}
    scenarios = []
    for name, full, first_productive in (("free_two_productive", False, True), ("full_two_productive", True, True),
                                         ("full_first_unproductive_then_useful", True, False)):
        results = []
        for policy, module in modules.items():
            search, pages = SavedSearch(full), SavedPages(first_productive)
            report = module.run(FIRST + "\n\n" + SECOND + "\n\n" + HOST, searcher=search, fetcher=pages,
                                query_strategy=module.DENSE_STRATEGY, url_strategy=module.BALANCED_URL_STRATEGY)
            results.append({"policy": policy, "matched_words": report["stats"]["matched_words"],
                "physical_loads": len(pages.calls), "search_calls": search.calls, "loaded_urls": pages.calls,
                "links": report["web"]["link_discoveries"]})
        scenarios.append({"id": name, "synthetic": True, "full_initial_queue": full,
                          "first_link_has_new_exact_words": first_productive, "results": results})
    result = {"schema_version": 1, "snapshot": args.snapshot, "new_searches": 0, "new_fetches": 0,
        "synthetic": True, "max_results_per_fake_call": 5, "scenarios": scenarios,
        "remaining_limit": "After an unproductive pressure replacement later genuine links can be missed; this is an allocation tradeoff under a fixed12-page cap."}
    dump(OUT / "authored-link-budget-controls.json", result)
    print({s["id"]: {r["policy"]: r["matched_words"] for r in s["results"]} for s in scenarios})


if __name__ == "__main__":
    main()
