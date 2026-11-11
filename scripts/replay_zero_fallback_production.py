#!/usr/bin/env python3
"""Explicit production False/True ZERO comparison on final DEV30 saved data."""
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search as web
from plagiarism.web_fetch import FetchError
from scripts.analyze_web_dev_checks import inspect, compare
from scripts.dev_report_provenance import source_snapshot_signatures
from scripts.replay_final_url_policies import match_identity
from scripts.replay_saved_dev_search import FrozenSearch, canonical, load_queries
from scripts.replay_zero_fallback_dev import pool_pages
from scripts.run_dev_query_probe import dump
from scripts.summarize_night_results import BASE, DEV, DEV_KEY_SHA, FINAL_BATCHES

OUT = BASE / "dev-zero-fallback-production-v1"


class PrefixAndExtraPages:
    def __init__(self, original, lookup, search_urls):
        self.original, self.lookup, self.search_urls = original, lookup, set(search_urls)
        self.prefix = original["report"]["web"]["attempted_urls"]
        self.calls, self.unknown, self.events, self.validation_errors = [], [], [], []

    def fetch(self, url):
        url = canonical(url)
        position = len(self.calls)
        if position >= 24 or (position < len(self.prefix) and url != self.prefix[position]):
            self.validation_errors.append(url)
            raise ValueError("Production changed its original attempt prefix or exceeded24")
        self.calls.append(url)
        event = {"requested_url": url, "phase": "original" if position < len(self.prefix) else "extra"}
        self.events.append(event)
        if position < len(self.prefix):
            record = self.original["records"][url]
            event.update(snapshot_origin=record["_artifact"], snapshot_sha256=record["_artifact_sha256"])
            if record["state"] != "complete":
                event.update(state="saved_failure", code=record["_typed_report_code"])
                raise FetchError("Original known physical outcome", record["_typed_report_code"])
            page = record["fetched"]
        else:
            if url not in self.search_urls:
                self.validation_errors.append(url)
                raise ValueError("An extra URL was not in the saved search replies")
            try:
                page = self.lookup(url)
            except FetchError as error:
                event.update(state="unknown" if error.code == "saved_page_cache_miss" else "saved_failure", code=error.code)
                if error.code == "saved_page_cache_miss":
                    self.unknown.append(url)
                raise
            event["snapshot_origin"] = page.get("_snapshot_origin")
        actual = sha256(page["text"].encode()).hexdigest()
        if page.get("text_sha256", actual) != actual:
            self.validation_errors.append(url)
            raise ValueError("Changed cached page body")
        event.update(state="read_saved_body", final_url=page["url"], text_sha256=actual)
        return page


def main():
    if sha256((DEV / "key.json").read_bytes()).hexdigest() != DEV_KEY_SHA:
        raise ValueError("DEV key changed")
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only fixed DEV30 allowed")
    code = sha256((ROOT / "plagiarism/web_search.py").read_bytes()).hexdigest()
    denied = []
    def no_network(event, arguments):
        if event.startswith("socket."):
            denied.append(event)
            raise PermissionError("Production comparison is cached only")
    sys.addaudithook(no_network)
    lookup, originals, origins = pool_pages()
    rows = []
    for name in ("disabled", "enabled"):
        (OUT / name).mkdir(parents=True, mode=0o700, exist_ok=True)
    for case in cases:
        cid = case["id"]
        original = originals[cid]
        raw = original["report"]
        text = (DEV / case["input_path"]).read_text()
        if text != raw["text"] or sha256(text.encode()).hexdigest() != case["text_sha256"]:
            raise ValueError("Changed original input")
        directory = BASE / next(name for name, ids in FINAL_BATCHES if cid in ids)
        records, identity = load_queries(directory, cid, case["text_sha256"])
        row = {"id": cid, "actual_original_report_id": raw["id"], "policies": {}}
        for enabled in (False, True):
            name = "enabled" if enabled else "disabled"
            search = FrozenSearch(records)
            pages = PrefixAndExtraPages(original, lookup, search.allowed_urls)
            identifier = sha256(("production-zero-v1" + cid + name + code).encode()).hexdigest()[:32]
            report = web.run(text, request_id=identifier, searcher=search, fetcher=pages,
                query_strategy=web.DENSE_STRATEGY, url_strategy=web.PATH_URL_STRATEGY,
                use_named_references=True, use_zero_fallback=enabled)
            if denied or pages.validation_errors or search.missing_queries:
                raise ValueError("Production cached replay lost its invariants")
            prefix_count = len(raw["sources"])
            prefix_ok = (report["web"]["attempted_urls"][:len(pages.prefix)] == pages.prefix
                and report["sources"][:prefix_count] == raw["sources"]
                and {k: v for k, v in source_snapshot_signatures(report).items() if k in source_snapshot_signatures(raw)}
                    == source_snapshot_signatures(raw))
            if not prefix_ok:
                raise ValueError("Production changed original source snapshots for " + cid)
            if not enabled and ({match_identity(m) for m in report["matches"]} != {match_identity(m) for m in raw["matches"]}
                    or report["stats"]["matched_words"] != raw["stats"]["matched_words"]
                    or report["web"]["attempted_urls"] != pages.prefix):
                raise ValueError("ExplicitFalse did not reproduce original report " + cid)
            report.update(id=identifier, offline_replay={"kind": "production-zero-explicit-" + str(enabled),
                "network_queries": 0, "fresh_page_load_attempts": 0, "fresh_page_loads": 0,
                "main_store_written": False, "original_report_id": raw["id"], "prefix_preserved": prefix_ok,
                "unknown_selected_pages": pages.unknown, "query_identity": identity,
                "cached_physical_outcomes": pages.events})
            if pages.unknown:
                report["web"]["status"] = "partial"
                report["stats"]["uniqueness_percent"] = None
            dump(OUT / name / (cid + ".json"), report)
            row["policies"][name] = {"matched_words_all": report["stats"]["matched_words"],
                "attempted_urls": report["web"]["attempted_urls"], "prefix_preserved": prefix_ok,
                "unknown_selected_pages": pages.unknown, "extra_attempts": report["web"]["zero_fallback_extra_attempts"],
                "initial_exact_words": report["web"]["zero_fallback_initial_exact_words"],
                "sources_read": report["web"]["sources_checked"], "deadline_reached": report["web"]["zero_fallback_deadline_reached"]}
        rows.append(row)
        print(cid, {key: value["matched_words_all"] for key, value in row["policies"].items()}, flush=True)
    before, after = inspect(DEV, OUT / "disabled"), inspect(DEV, OUT / "enabled")
    dump(OUT / "disabled-analysis.json", before)
    dump(OUT / "enabled-analysis.json", after)
    if code != sha256((ROOT / "plagiarism/web_search.py").read_bytes()).hexdigest():
        raise ValueError("Production code changed during replay")
    result = {"schema_version": 1, "production_web_search_sha256": code, "new_searches": 0, "new_fetches": 0,
        "scope": "ExplicitFalse/True normal production run; same final replies, immutable original prefix, legitimate filled extra pool",
        "comparison": compare(before, after), "prefix_preserved_reports": sum(row["policies"]["enabled"]["prefix_preserved"] for row in rows),
        "explicit_false_original_reports_reproduced": 30,
        "unknown_selected_pages": {name: sum(len(row["policies"][name]["unknown_selected_pages"]) for row in rows) for name in ("disabled", "enabled")},
        "saved_pool_artifacts_sha256": origins, "cases": rows}
    dump(OUT / "comparison.json", result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"cases", "saved_pool_artifacts_sha256"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
