#!/usr/bin/env python3
"""Compare production BALANCED/PATH on exactly the final DEV30 replies.

Each actual request is bound to its own numbered full Fetcher outcome first.
Other final30 snapshots may supply a saved URL/redirect alias; absent contents
remain unknown unless the explicit page-only fill mode is selected. No search
provider or historical source pool is used.
"""
from copy import deepcopy
import argparse
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search
from plagiarism.input_references import extract_named_references
from plagiarism.web_fetch import FetchError, Fetcher
from scripts.analyze_web_dev_checks import inspect
from scripts.dev_report_provenance import source_snapshot_signatures
from scripts.replay_saved_dev_search import FrozenSearch, annotate_replay, canonical, load_queries
from scripts.run_dev_query_probe import dump
from scripts.summarize_night_results import BASE, DEV, DEV_KEY_SHA, FINAL_BATCHES

OUT = BASE / "dev-final-same-reply-url-v1"
CODE_FILES = ("plagiarism/web_search.py", "plagiarism/matching.py", "plagiarism/pdf_evidence.py",
              "plagiarism/input_references.py", "plagiarism/web_fetch.py", "plagiarism/pdf_source.py",
              "scripts/replay_final_url_policies.py")


def digest(value):
    return sha256(value.encode()).hexdigest()


def match_identity(match):
    return tuple(match.get(key) for key in ("kind", "source_id", "source_url", "start", "end",
                                            "source_start", "source_end", "excerpt", "source_excerpt", "quoted"))


def actual_outcomes(directory, ids):
    """Recover case boundaries from physical attempt counts, not guessed URLs."""
    paths = sorted(directory.glob("page-*-fetch.json"))
    numbered = []
    for path in paths:
        match = re.fullmatch(r"page-(\d{4})-[a-f0-9]{16}-fetch\.json", path.name)
        if match is None:
            raise ValueError("Unexpected saved Fetcher filename")
        numbered.append((int(match.group(1)), path))
    if [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
        raise ValueError("Saved Fetcher sequence has gaps or duplicates")
    cursor, result = 0, {}
    for cid in ids:
        report_path = directory / (cid + ".json")
        raw = json.loads(report_path.read_text())
        expected = raw["web"]["attempted_urls"]
        if raw.get("offline_replay") or len(expected) != raw["web"]["urls_attempted"] or len(expected) > 12:
            raise ValueError("Actual fresh physical attempt count is inconsistent")
        failures = {canonical(error["url"]): error["code"] for error in raw["web"]["errors"]
                    if error.get("stage") == "fetch" and error.get("url")}
        case_records = {}
        for url, (_, path) in zip(expected, numbered[cursor:cursor + len(expected)]):
            record = json.loads(path.read_text())
            normalized = canonical(url)
            if canonical(record.get("requested_url")) != normalized or normalized in case_records:
                raise ValueError("Saved Fetcher attempt does not match the actual report")
            record = {**record, "_artifact": str(path.relative_to(ROOT)), "_artifact_sha256": sha256(path.read_bytes()).hexdigest(),
                      "_case_id": cid, "_report_id": raw["id"]}
            if record.get("state") == "complete":
                page = record.get("fetched")
                if (not isinstance(page, dict) or not isinstance(page.get("text"), str)
                        or digest(page["text"]) != page.get("text_sha256")):
                    raise ValueError("Changed complete Fetcher text")
                canonical(page["url"])
                if not isinstance(page.get("links"), list):
                    raise ValueError("Actual full Fetcher record lost its links")
            elif record.get("state") == "failed_or_unknown" and normalized in failures:
                record["_typed_report_code"] = failures[normalized]
            else:
                raise ValueError("Unknown/in-flight physical outcome cannot be treated as a complete snapshot")
            case_records[normalized] = record
        if len(case_records) != len(expected):
            raise ValueError("Missing actual per-case Fetcher outcomes")
        cursor += len(expected)
        result[cid] = {"report": raw, "report_path": str(report_path.relative_to(ROOT)),
            "report_sha256": sha256(report_path.read_bytes()).hexdigest(), "records": case_records}
    if cursor != len(numbered):
        raise ValueError("Unassigned saved physical Fetcher outcomes")
    return result


class BaselineFills:
    """Durable bounded page-only fills; a submitted/failed URL is never retried."""
    def __init__(self, directory, enabled=False, limit=48, fetcher=None):
        if type(limit) is not int or not 0 <= limit <= 48:
            raise ValueError("Baseline direct-page cap must be at most48")
        self.directory, self.enabled, self.limit, self.fetcher = directory, enabled, limit, fetcher
        self.new_attempts = self.new_successes = 0
        self.in_fetch = self.cap_exhausted = False
        self.records = {}
        for path in sorted(directory.glob("*.json")):
            self._load(path)

    def _load(self, path):
        value = json.loads(path.read_text())
        url = canonical(value.get("requested_url"))
        if path.name != digest(url) + ".json":
            raise ValueError("Wrong baseline fill identity")
        state = value.get("state")
        if state not in {"submitted", "failed_or_unknown", "complete"}:
            raise ValueError("Invalid saved baseline fill state")
        record = {**value, "_artifact": str(path.relative_to(ROOT)), "_artifact_sha256": sha256(path.read_bytes()).hexdigest(),
                  "_case_id": value["discovery"]["case_id"], "_report_id": "direct-baseline-fill"}
        if state == "complete":
            page = value["fetched"]
            if (not isinstance(page.get("text"), str) or page.get("text_sha256") != digest(page["text"])
                    or not isinstance(page.get("links"), list)):
                raise ValueError("Changed saved baseline fill page")
            canonical(page["url"])
        else:
            record["_typed_report_code"] = value.get("code", "saved_page_outcome_unknown")
        self.records[url] = record
        if state == "complete":
            self.records.setdefault(canonical(record["fetched"]["url"]), record)
        return record

    def get(self, url, discovery, allow_new):
        url = canonical(url)
        existing = self.records.get(url)
        if existing is not None:
            return existing, False
        path = self.directory / (digest(url) + ".json")
        if path.exists():
            return self._load(path), False
        if not self.enabled or not allow_new:
            return None, False
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                return self._load(path), False
            if len(list(self.directory.glob("*.json"))) >= self.limit:
                self.cap_exhausted = True
                return None, False
            intent = {"schema_version": 1, "requested_url": url, "state": "submitted",
                "submitted_at": datetime.now(timezone.utc).isoformat(), "discovery": discovery,
                "basis": "algorithm-selected baseline URL from frozen discovery; no gold URLs supplied"}
            with path.open("x") as stream:
                json.dump(intent, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o600)
        # The durable submitted intent counts against the budget before GET.
        self.new_attempts += 1
        self.in_fetch = True
        try:
            if self.fetcher is None:
                self.fetcher = Fetcher()
            page = self.fetcher.fetch(url)
            if (not isinstance(page, dict) or not isinstance(page.get("text"), str)
                    or page.get("text_sha256") != digest(page["text"]) or not isinstance(page.get("links"), list)):
                raise ValueError("Normal Fetcher returned invalid page identity")
            canonical(page["url"])
            intent.update(state="complete", fetched=page)
            self.new_successes += 1
        except Exception as error:
            intent.update(state="failed_or_unknown", code=getattr(error, "code", "fetch_error"), error_type=type(error).__name__)
        finally:
            self.in_fetch = False
        intent["completed_at"] = datetime.now(timezone.utc).isoformat()
        dump(path, intent)
        return self._load(path), True


class FinalPages:
    """Only known final physical outcomes; failures are not successful pages."""
    def __init__(self, own, shared, allowed, *, fills=None, allow_fill=False, case_id=None, search_urls=None):
        self.own, self.shared, self.allowed_urls = own, shared, set(allowed)
        self.calls, self.page_load_events, self.cache_misses, self.saved_failures = [], [], [], []
        self.missing_links, self.known_parents_missing_links = set(), {}
        self.fresh_attempts = self.fresh_successes = self.cached_page_loads = 0
        self.fills, self.allow_fill, self.case_id = fills, allow_fill, case_id
        self.search_urls, self.link_parents = set(search_urls or []), {}

    def fetch(self, url):
        url = canonical(url)
        if url not in self.allowed_urls or len(self.calls) >= 12:
            raise ValueError("Replay attempted an undiscovered URL or exceeded twelve loads")
        self.calls.append(url)
        record = self.own.get(url)
        origin = "same_case_final_fetch"
        if record is None:
            record, origin = self.shared.get(url), "other_final_case_saved_fetch_or_redirect_alias"
        fresh = False
        if record is None and self.fills is not None:
            discovery = {"case_id": self.case_id, "kind": "saved_query_result" if url in self.search_urls else "selected_same_host_parent_link",
                         "parent_url": self.link_parents.get(url)}
            if self.allow_fill and url not in self.search_urls and url not in self.link_parents:
                raise ValueError("A new baseline page was not derived from saved search or an actual parent link")
            record, fresh = self.fills.get(url, discovery, self.allow_fill)
            origin = "new_authorized_baseline_fetch" if fresh else "saved_baseline_fill"
            if fresh:
                self.fresh_attempts += 1
                self.fresh_successes += bool(record and record["state"] == "complete")
        event = {"requested_url": url, "origin": origin}
        self.page_load_events.append(event)
        if record is None:
            self.cache_misses.append(url)
            event.update(state="unknown_content")
            raise FetchError("Selected page was not captured in final30; no network is permitted", "saved_page_cache_miss")
        event.update(artifact=record["_artifact"], artifact_sha256=record["_artifact_sha256"],
                     captured_case_id=record["_case_id"], captured_report_id=record["_report_id"])
        if record["state"] != "complete":
            code = record["_typed_report_code"]
            event.update(state="saved_failed_fetch", code=code)
            self.saved_failures.append({"url": url, "code": code, "artifact": record["_artifact"]})
            if record["state"] == "submitted":
                self.cache_misses.append(url)
            raise FetchError("The same captured physical fetch failed", code)
        page = deepcopy(record["fetched"])
        if page["text_sha256"] != digest(page["text"]):
            raise ValueError("Changed final page text")
        self.cached_page_loads += not fresh
        event.update(state="read", final_url=canonical(page["url"]), text_sha256=page["text_sha256"], link_metadata_available=True)
        for link in page["links"]:
            normalized = web_search._canonical_url(link.get("url"))
            if normalized:
                self.allowed_urls.add(normalized)
                self.link_parents.setdefault(normalized, canonical(page["url"]))
        return page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fill-baseline", action="store_true", help="Explicitly authorized normalFetcher page-only fills; never search")
    parser.add_argument("--max-new-page-attempts", type=int, default=48)
    args = parser.parse_args()
    if args.fill_baseline and args.max_new_page_attempts != 48:
        parser.error("The authorized cumulative page-fill ceiling is exactly48; no higher limit is accepted")
    if sha256((DEV / "key.json").read_bytes()).hexdigest() != DEV_KEY_SHA:
        raise ValueError("Frozen DEV key changed")
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [case["id"] for case in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only DEV30 is allowed")
    loaded = {}
    for directory, ids in FINAL_BATCHES:
        loaded.update(actual_outcomes(BASE / directory, ids))
    if args.fill_baseline:
        # New pages use the same configured optional extractor as the final
        # experiment; stored primary/failure snapshots above are never changed.
        os.environ["ISTOK_PDF_ALTERNATIVE_ENGINE"] = "pdfium"
    fills = BaselineFills(OUT / "fills", enabled=args.fill_baseline, limit=args.max_new_page_attempts)
    shared = {}
    for cid in loaded:
        for url, record in loaded[cid]["records"].items():
            if record["state"] == "complete":
                shared[url] = record
                shared[canonical(record["fetched"]["url"])] = record
    # Failed outcomes can explain absent contents, but never overwrite a known
    # successfully saved page. Own-case failures always take priority above.
    for cid in loaded:
        for url, record in loaded[cid]["records"].items():
            shared.setdefault(url, record)
    code_hashes = {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in CODE_FILES}
    denied = []
    def no_network(event, arguments):
        if event.startswith("socket.") and not fills.in_fetch:
            denied.append(event)
            raise PermissionError("Final same-reply replay forbids all network")
    sys.addaudithook(no_network)
    rows, reports = [], {"balanced": {}, "path": {}}
    for case in cases:
        cid = case["id"]
        actual = loaded[cid]["report"]
        text = (DEV / case["input_path"]).read_text()
        if digest(text) != case["text_sha256"] or text != actual["text"]:
            raise ValueError("Changed actual DEV text")
        directory = BASE / next(name for name, ids in FINAL_BATCHES if cid in ids)
        records, identity = load_queries(directory, cid, case["text_sha256"])
        row = {"id": cid, "actual_report_id": actual["id"], "actual_report_sha256": loaded[cid]["report_sha256"], "policies": {}}
        for policy, strategy in (("balanced", web_search.BALANCED_URL_STRATEGY), ("path", web_search.PATH_URL_STRATEGY)):
            search = FrozenSearch(records)
            allowed = search.allowed_urls | {canonical(item["url"]) for item in extract_named_references(text)}
            pages = FinalPages(loaded[cid]["records"], shared, allowed, fills=fills,
                allow_fill=args.fill_baseline and policy == "balanced", case_id=cid, search_urls=search.allowed_urls)
            report = web_search.run(text, request_id=digest(cid + strategy)[:32], searcher=search, fetcher=pages,
                query_strategy=web_search.DENSE_STRATEGY, url_strategy=strategy, use_named_references=True,
                use_zero_fallback=False)
            if denied or search.missing_queries or (pages.fresh_attempts and not (args.fill_baseline and policy == "balanced")):
                raise ValueError("Frozen data guarantee violated")
            report.update(id=digest("same-final-url-v1" + cid + policy)[:32], created_at=actual.get("created_at"))
            if policy == "path":
                checks = {"matched_words": report["stats"]["matched_words"] == actual["stats"]["matched_words"],
                    "physical_sources_read": report["web"]["sources_checked"] == actual["web"]["sources_checked"],
                    "physical_attempt_order": report["web"]["attempted_urls"] == actual["web"]["attempted_urls"],
                    "all_match_evidence": {match_identity(m) for m in report["matches"]} == {match_identity(m) for m in actual["matches"]},
                    "all_source_snapshot_hashes": source_snapshot_signatures(report) == source_snapshot_signatures(actual),
                    "no_unknown_actual_contents": not pages.cache_misses}
                if not all(checks.values()):
                    raise ValueError("Current PATH does not exactly reproduce actual final report " + cid + ": " + str(checks))
                row["path_reproduces_actual"] = checks
            annotate_replay(report, search, pages, {"case_id": cid, "policy": policy, "same_final_query_identity": identity,
                "actual_report_id": actual["id"], "main_store_written": False, "new_searches": 0,
                "saved_failed_fetches": pages.saved_failures, "page_pool": "final30 full Fetcher records only"})
            (OUT / policy).mkdir(parents=True, exist_ok=True, mode=0o700)
            dump(OUT / policy / (cid + ".json"), report)
            reports[policy][cid] = report
            row["policies"][policy] = {"matched_words": report["stats"]["matched_words"],
                "physical_load_attempts": len(pages.calls), "physical_sources_read": report["web"]["sources_checked"],
                "unknown_content_urls": pages.cache_misses, "saved_failed_fetches": pages.saved_failures,
                "attempted_urls": pages.calls, "read_urls": [s["url"] for s in report["sources"] if not s.get("representation_of")]}
        rows.append(row)
        print(cid, {key: value["matched_words"] for key, value in row["policies"].items()}, flush=True)
    analyses = {policy: inspect(DEV, OUT / policy) for policy in reports}
    for policy, analysis in analyses.items():
        dump(OUT / policy / "analysis.json", analysis)
    changes = []
    for a, b in zip(analyses["balanced"]["cases"], analyses["path"]["cases"]):
        for index, (ea, eb) in enumerate(zip(a["evidence"], b["evidence"])):
            if not ea["lexical"]:
                continue
            delta = eb["literal_covered_words"] - ea["literal_covered_words"]
            if delta:
                unknown = rows[int(a["id"][1:]) - 1]["policies"]["balanced"]["unknown_content_urls"]
                changes.append({"id": a["id"], "fragment_index": index, "known_saved_copied_word_delta_path_minus_balanced": delta,
                    "balanced_unknown_contents": unknown,
                    "gain_is_conditional_on_missing_baseline_content": delta > 0 and bool(unknown)})
    if code_hashes != {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in CODE_FILES}:
        raise ValueError("Production code changed during same-reply comparison")
    result = {"schema_version": 1, "documents": 30, "new_searches": 0, "new_fetches": fills.new_attempts, "code_sha256": code_hashes,
        "new_direct_page_attempts_this_invocation": fills.new_attempts,
        "new_direct_page_successes_this_invocation": fills.new_successes,
        "total_baseline_fill_intents": len(list((OUT / "fills").glob("*.json"))),
        "baseline_fill_states": {state: sum(json.loads(path.read_text())["state"] == state
            for path in (OUT / "fills").glob("*.json")) for state in ("submitted", "complete", "failed_or_unknown")},
        "baseline_fill_cap": fills.limit, "baseline_fill_cap_exhausted": fills.cap_exhausted,
        "actual_path_reproduced_reports": sum(all(row["path_reproduces_actual"].values()) for row in rows),
        "summaries": {policy: analysis["summary"] for policy, analysis in analyses.items()},
        "unknown_content_attempts": {policy: sum(len(row["policies"][policy]["unknown_content_urls"]) for row in rows) for policy in reports},
        "word_changes": changes, "cases": rows,
        "limitation": "Original final30 outcomes remain unchanged. Explicitly authorized baseline fill snapshots retain their own timestamps and failures; absent snapshots stay unknown. Failed loads do not establish page uniqueness."}
    dump(OUT / "comparison.json", result)
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
