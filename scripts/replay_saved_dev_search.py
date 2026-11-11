#!/usr/bin/env python3
"""Replay DEV search snapshots, with network access disabled by default.

Frozen per-query replies are never reconstructed from a report's flat URL list.
Reports supply text snapshots, not invented links. Explicit page-only fill flags
use the normal robots-respecting Fetcher; they never enable a search provider.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search
from plagiarism.web_fetch import FetchError, Fetcher

PRIVATE = ROOT / "data/private/web-evaluation"
DEV = PRIVATE / "materials/dev-30"
BASELINE = PRIVATE / "dev-live-30-v1"
DENSE = PRIVATE / "dev-dense-six-live-v1"
PAGE_CACHE = PRIVATE / "dev-replay-page-cache-v1"


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def canonical(url):
    result = web_search._canonical_url(url)
    if not result:
        raise ValueError("Invalid saved public URL")
    return result


class FrozenSearch:
    provider = "tavily"

    def __init__(self, records):
        self.records, self.calls, self.missing_queries = records, [], []
        self.allowed_urls = {canonical(item["url"]) for record in records.values()
                             if record.get("state") == "complete" for item in record.get("results", [])}

    def search(self, query, request_id):
        if len(self.calls) >= web_search.MAX_QUERIES:
            raise web_search.WebSearchError("Replay search cap exceeded", "saved_search_cap")
        saved = self.records.get(query)
        self.calls.append({"query": query, "replay_call_id": request_id,
                           "original_call_id": saved.get("request_id") if saved else None})
        if not saved:
            self.missing_queries.append(query)
            raise web_search.WebSearchError("No frozen reply for this phrase", "saved_search_cache_miss")
        if saved.get("state") != "complete":
            raise web_search.WebSearchError("Saved search outcome was incomplete",
                                           saved.get("code", "saved_search_outcome_unknown"))
        return deepcopy(saved["results"])


def load_queries(directory, cid, text_hash):
    request_path = directory / (cid + "-request.json")
    if not request_path.exists():
        return {}, {"status": "request_not_saved"}
    request = json.loads(request_path.read_text())
    if (request.get("text_sha256") != text_hash or not re.fullmatch(r"[a-f0-9]{32}", request.get("request_id", ""))
            or type(request.get("planned_queries")) is not int or not 1 <= request["planned_queries"] <= 6):
        raise ValueError("Frozen request identity does not match DEV input")
    records, unavailable = {}, []
    for index in range(request["planned_queries"]):
        call_id = sha256((request["request_id"] + "\0" + str(index)).encode()).hexdigest()
        path = directory / (call_id + "-search.json")
        if not path.exists():
            unavailable.append(index)
            continue
        saved = json.loads(path.read_text())
        if saved.get("request_id") != call_id or not isinstance(saved.get("query"), str):
            raise ValueError("Wrong frozen per-query response identity")
        if saved.get("state") == "complete":
            results = saved.get("results")
            if not isinstance(results, list) or len(results) > 5 or any(not isinstance(item, dict) for item in results):
                raise ValueError("Malformed frozen search reply")
            for item in results:
                canonical(item.get("url"))
        previous = records.get(saved["query"])
        if previous and previous != saved:
            raise ValueError("Ambiguous saved replies for an identical phrase")
        records[saved["query"]] = saved
    return records, {"status": "frozen_per_query_replies" if records else "per_query_replies_not_saved",
                     "original_request_id": request["request_id"], "missing_query_indexes": unavailable,
                     "warning": "Flat report discovered_urls are never used as per-query search replies."}


def report_source_pages(report, origin):
    """Restore each primary and its stored PDF view as ONE fetched-page payload."""
    sources = report.get("sources", [])
    by_id = {source["id"]: source for source in sources}
    if len(by_id) != len(sources):
        raise ValueError("Duplicate saved source identity")
    pages = {}
    for source in sources:
        text, provenance = source["text"], source.get("provenance", {})
        digest = sha256(text.encode()).hexdigest()
        if provenance.get("text_sha256", digest) != digest:
            raise ValueError("Changed cached source text")
        if source.get("representation_of"):
            continue
        url = canonical(source["url"])
        pages[source["id"]] = {"url": url, "requested_url": url, "title": source.get("title", ""),
            "text": text, "text_sha256": digest, "fetched_at": provenance.get("fetched_at"),
            "_replay_origin": dict(origin)}
    for source in sources:
        parent_id = source.get("representation_of")
        if not parent_id:
            continue
        parent = by_id.get(parent_id)
        if (parent_id not in pages or parent is None
                or canonical(source["url"]) != canonical(parent["url"])):
            raise ValueError("Saved supplemental view has no matching primary page")
        page, provenance = pages[parent_id], source.get("provenance", {})
        if provenance.get("extraction") != "pdfium-text-v1" or provenance.get("parser_version") != "5.14.0":
            page.setdefault("_replay_unavailable_supplemental_ids", []).append(source["id"])
            continue
        if page.get("supplemental_texts"):
            raise ValueError("More than one saved supplemental PDF view")
        value = {"id": "pdfium-v1", "text": source["text"],
            "text_sha256": sha256(source["text"].encode()).hexdigest(),
            "parser_version": provenance["parser_version"], "extraction": provenance["extraction"]}
        # Missing historical metadata stays missing; it is never inferred from
        # gold labels or another fetch of the same URL.
        if "raw_pdf_sha256" in provenance:
            page["raw_pdf_sha256"] = value["raw_pdf_sha256"] = provenance["raw_pdf_sha256"]
        if "pdf_pages" in provenance:
            page["pdf_pages"] = value["page_count"] = provenance["pdf_pages"]
        page.update(content_type="application/pdf", supplemental_texts=[value])
    statuses = {canonical(item["url"]): {key: value for key, value in item.items() if key != "url"}
                for item in report.get("web", {}).get("optional_pdf_status", [])}
    result = {}
    for page in pages.values():
        if page["url"] in statuses:
            page["optional_status"] = statuses[page["url"]]
        result[page["url"]] = page
    return result


def load_source_pool(baseline=BASELINE, dense=DENSE):
    """All 30 baseline and the six chosen fresh reports; no gold source files."""
    pool, origins, known_links = {}, [], {}
    dense_run = json.loads((dense / "run.json").read_text())
    chosen = dense_run["case_ids"]
    if len(chosen) > 6 or any(not re.fullmatch(r"D(?:0[1-9]|[12][0-9]|30)", cid) for cid in chosen):
        raise ValueError("Only the chosen six fresh DEV reports may supply the extra cache")
    for directory, ids in ((baseline, [f"D{i:02}" for i in range(1, 31)]), (dense, chosen)):
        for cid in ids:
            path = directory / (cid + ".json")
            if not path.exists():
                continue
            report = json.loads(path.read_text())
            origins.append({"case_id": cid, "report_id": report.get("id"), "path": str(path.relative_to(ROOT)),
                            "sha256": sha256(path.read_bytes()).hexdigest()})
            for link in report.get("web", {}).get("link_discoveries", []):
                parent = canonical(link["parent_url"])
                known_links.setdefault(cid, {}).setdefault(parent, set()).add(canonical(link["url"]))
            # Choose a complete per-report snapshot atomically. A later plain
            # snapshot must not inherit an older view from another PDF fetch.
            pool.update(report_source_pages(report, {"case_id": cid, "report_id": report.get("id"),
                        "source": "saved_report_without_link_metadata"}))
    return pool, origins, known_links


class CachedPages:
    def __init__(self, pool, allowed_urls, *, cache_directory=PAGE_CACHE, fill_missing=False,
                 refresh_missing_links=False, known_parent_links=None, fetcher=None):
        self.pool, self.allowed_urls = pool, set(allowed_urls)
        self.cache_directory = cache_directory
        self.fill_missing, self.refresh_missing_links = fill_missing, refresh_missing_links
        self.known_parent_links = known_parent_links or {}
        self.fetcher = fetcher  # Instantiated only on an explicitly authorized miss.
        self.calls, self.cache_misses, self.missing_links = [], [], set()
        self.page_load_events = []
        self.known_parents_missing_links, self.fresh_attempts, self.fresh_successes = {}, 0, 0
        self.cached_page_loads = 0
        # Preserve actual fetched link metadata and redirect aliases across
        # replay invocations. Only this DEV page cache is inspected.
        full = []
        for path in cache_directory.glob("*.json"):
            entry = json.loads(path.read_text())
            if entry.get("state") == "complete":
                self._validate_page(entry.get("fetched"))
                full.append(entry)
        for entry in sorted(full, key=lambda item: item.get("saved_at", "")):
            page = entry["fetched"]
            self.pool[canonical(entry["requested_url"])] = page
            self.pool[canonical(page["url"])] = page

    def fetch(self, url):
        url = canonical(url)
        if len(self.calls) >= web_search.MAX_URLS:
            raise FetchError("Replay page cap exceeded", "saved_page_cap")
        if url not in self.allowed_urls:
            raise FetchError("URL did not come from frozen discovery or saved page links", "saved_url_not_discovered")
        self.calls.append(url)
        event = {"requested_url": url}
        self.page_load_events.append(event)
        path = self.cache_directory / (sha256(url.encode()).hexdigest() + ".json")
        entry = json.loads(path.read_text()) if path.exists() else None
        if entry and entry.get("requested_url") != url:
            raise ValueError("Wrong page-cache identity")
        if entry and entry.get("state") != "complete":
            # Never repeat uncertain/failed page loading automatically.
            event.update(origin="saved_page_outcome", state=entry.get("state"),
                         code=entry.get("code", "saved_page_outcome_unknown"))
            raise FetchError(entry.get("message", "Saved page failure or unknown outcome"),
                             entry.get("code", "saved_page_outcome_unknown"))
        page = entry.get("fetched") if entry and entry.get("state") == "complete" else self.pool.get(url)
        lacks_links = page is not None and "links" not in page
        refresh = lacks_links and self.refresh_missing_links and url in self.known_parent_links
        if page is None or refresh:
            if not ((page is None and self.fill_missing) or refresh):
                self.cache_misses.append(url)
                event.update(origin="network_disabled", state="cache_miss")
                raise FetchError("Selected page was not saved; network is disabled", "saved_page_cache_miss")
            self.fresh_attempts += 1
            event.update(origin="direct_page_refresh" if refresh else "direct_page_fill", state="submitted")
            if self.fetcher is None:
                self.fetcher = Fetcher()
            # A crash during a direct page fetch leaves an explicit uncertain
            # record, so even optional fill mode cannot silently repeat it.
            dump(path, {"requested_url": url, "state": "submitted",
                        "saved_at": datetime.now(timezone.utc).isoformat()})
            try:
                page = self.fetcher.fetch(url)
                self._validate_page(page)
                dump(path, {"requested_url": url, "state": "complete", "fetched": page,
                            "saved_at": datetime.now(timezone.utc).isoformat()})
                self.fresh_successes += 1
                self.pool[url] = page
                self.pool[canonical(page["url"])] = page
            except (RuntimeError, OSError, ValueError) as error:
                event.update(state="failed_or_unknown", code=getattr(error, "code", "fetch_error"))
                dump(path, {"requested_url": url, "state": "failed_or_unknown",
                            "code": getattr(error, "code", "fetch_error"), "message": str(error),
                            "saved_at": datetime.now(timezone.utc).isoformat()})
                raise
        else:
            self.cached_page_loads += 1
            event.update(origin="historical_report_snapshot" if "_replay_origin" in page else "saved_full_page_fetch",
                         snapshot_origin=page.get("_replay_origin"))
        self._validate_page(page)
        event.update(state="read", final_url=canonical(page["url"]),
                     link_metadata_available="links" in page, text_sha256=sha256(page["text"].encode()).hexdigest())
        if "links" not in page:
            self.missing_links.add(url)
            if url in self.known_parent_links:
                self.known_parents_missing_links[url] = sorted(self.known_parent_links[url])
        else:
            # Only actual preserved labels are ever passed to _related_link.
            for link in page["links"]:
                normalized = web_search._canonical_url(link.get("url"))
                if normalized:
                    self.allowed_urls.add(normalized)
        return deepcopy(page)

    @staticmethod
    def _validate_page(page):
        if not isinstance(page, dict) or not isinstance(page.get("text"), str):
            raise ValueError("Invalid saved page")
        canonical(page["url"])
        if page.get("text_sha256", sha256(page["text"].encode()).hexdigest()) != sha256(page["text"].encode()).hexdigest():
            raise ValueError("Changed saved page text")
        if "links" in page and (not isinstance(page["links"], list) or any(not isinstance(link, dict) for link in page["links"])):
            raise ValueError("Invalid saved link metadata")


def annotate_replay(report, search, pages, metadata):
    metadata.update(network_queries=0, fresh_page_load_attempts=pages.fresh_attempts,
                    fresh_page_loads=pages.fresh_successes, cached_page_loads=pages.cached_page_loads,
                    search_cache_misses=search.missing_queries, page_cache_misses=pages.cache_misses,
                    missing_link_metadata=sorted(pages.missing_links),
                    known_parents_missing_links=pages.known_parents_missing_links,
                    page_load_events=pages.page_load_events,
                    frozen_query_calls=search.calls, underlying_web_status=report["web"]["status"])
    incomplete = bool(search.missing_queries or pages.cache_misses or pages.missing_links)
    metadata["replay_metadata_complete"] = not incomplete
    if pages.missing_links:
        report["web"]["errors"].append({"stage": "replay", "code": "saved_link_metadata_missing",
            "count": len(pages.missing_links), "message": "Снимки страниц не сохраняли ссылки; переходы полностью не воспроизведены."})
    if incomplete:
        report["web"]["status"] = "partial"
        report["stats"]["uniqueness_percent"] = None
        total, matched = report["stats"]["total_words"], report["stats"]["matched_words"]
        if matched:
            report["stats"]["uniqueness_upper_bound_percent"] = ((1000 * (total - matched) + total - 1) // total) / 10
        if not any(issue.get("kind") == "partial_web_check" for issue in report["issues"]):
            report["issues"].append({"kind": "partial_web_check", "message": "Локальный повтор ограничен сохранёнными данными."})
    report["offline_replay"] = metadata
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--saved", type=Path, default=DENSE)
    parser.add_argument("--query-strategy")
    parser.add_argument("--url-strategy")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fill-missing-pages", action="store_true")
    parser.add_argument("--refresh-missing-links", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    saved = args.saved.resolve()
    if saved not in {DENSE.resolve(), BASELINE.resolve()}:
        parser.error("Only the explicit dense DEV run or original DEV-30 baseline is accepted.")
    key = DEV / "key.json"
    cases = json.loads(key.read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only frozen DEV-30 is supported")
    run = json.loads((saved / "run.json").read_text())
    if run["input_key_sha256"] != sha256(key.read_bytes()).hexdigest():
        raise ValueError("Frozen DEV key changed")
    ids = args.ids or run.get("case_ids", list(run["planned_queries"]))
    if len(set(ids)) != len(ids) or any(cid not in {c["id"] for c in cases} for cid in ids):
        parser.error("Choose unique D01–D30 cases only")
    strategy = args.query_strategy or run["query_strategy"]
    if args.url_strategy and "url_strategy" not in inspect.signature(web_search.run).parameters:
        parser.error("This code version does not expose run(url_strategy=...)")
    if args.url_strategy and hasattr(web_search, "_fetch_order"):
        # Validate without reading a page or consuming a search reply.
        web_search._fetch_order([], [], args.url_strategy)
    out = args.output or PRIVATE / ("dev-saved-replay-" + uuid4().hex[:12])
    if out.resolve().parent != PRIVATE.resolve() or not out.name.startswith("dev-"):
        parser.error("Replay output must be a new dev-* directory in private web-evaluation")
    out.mkdir(mode=0o700, exist_ok=False)
    pool, origins, known_links = load_source_pool()
    invocation = {"id": uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(), "case_ids": ids,
        "query_strategy": strategy, "url_strategy": args.url_strategy,
        "frozen_search_run": str(saved.relative_to(ROOT)), "source_snapshot_reports": origins,
        "fill_missing_pages": args.fill_missing_pages, "refresh_missing_links": args.refresh_missing_links,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "web_search_code_sha256": sha256((ROOT / "plagiarism/web_search.py").read_bytes()).hexdigest(),
        "input_key_sha256": run["input_key_sha256"], "network_queries": 0}
    dump(out / "run.json", invocation)
    summary = []
    for cid in ids:
        case = next(c for c in cases if c["id"] == cid)
        text = (DEV / case["input_path"]).read_text()
        if sha256(text.encode()).hexdigest() != case["text_sha256"]:
            raise ValueError("DEV input changed")
        records, search_metadata = load_queries(saved, cid, case["text_sha256"])
        search = FrozenSearch(records)
        pages = CachedPages(pool, search.allowed_urls, fill_missing=args.fill_missing_pages,
                            refresh_missing_links=args.refresh_missing_links, known_parent_links=known_links.get(cid))
        options = {"query_strategy": strategy}
        if args.url_strategy:
            options["url_strategy"] = args.url_strategy
        identifier = sha256((invocation["id"] + cid).encode()).hexdigest()[:32]
        report = web_search.run(text, identifier, searcher=search, fetcher=pages, **options)
        report.update(id=identifier, created_at=datetime.now(timezone.utc).isoformat())
        if report["web"]["queries_attempted"] > 6 or report["web"]["urls_attempted"] > 12:
            raise ValueError("Replay exceeded document bounds")
        original_path = saved / (cid + ".json")
        original_id = json.loads(original_path.read_text()).get("id") if original_path.exists() else None
        metadata = {"case_id": cid, "original_report_id": original_id,
                    "search_snapshot_run": str(saved.relative_to(ROOT)), "search_snapshot": search_metadata,
                    "original_baseline_search_replies_reconstructed": False,
                    "main_store_written": False, "network_enabled_for_pages": args.fill_missing_pages or args.refresh_missing_links}
        annotate_replay(report, search, pages, metadata)
        dump(out / (cid + ".json"), report)
        summary.append({"id": cid, "report_id": identifier, "queries_replayed": len(search.calls),
            "matched_words": report["stats"]["matched_words"], "pages_read": report["web"]["sources_checked"],
            "page_cache_misses": len(pages.cache_misses), "missing_link_metadata": len(pages.missing_links),
            "fresh_page_load_attempts": pages.fresh_attempts, "metadata_complete": report["offline_replay"]["replay_metadata_complete"]})
        print(json.dumps(summary[-1]), flush=True)
    dump(out / "summary.json", {"network_queries": 0, "cases": summary})
    print("Saved private replay", out)


if __name__ == "__main__":
    main()
