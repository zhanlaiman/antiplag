#!/usr/bin/env python3
"""Prepare/status a frozen DEV metadata pilot; network requires explicit START.

Only D08, D10 and D04 are eligible, with six unchanged Tavily Basic requests
each. Discovery metadata may order pages; only the normal fetched page bodies
reach the matcher. Preparation and status never initialize credentials or API
clients. Run --help for the separate, manifest-bound live invocation.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Thread
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search as web
from scripts.probe_search_metadata import METADATA_VERSION, rank_by_snippet_overlap, retain_results
from scripts.run_dev_query_probe import quota

CASE_IDS = ("D08", "D10", "D04")
QUERIES_PER_CASE = 6
CALL_CAP = 18
STRATEGY = "experimental-path-own-query-metadata-run6-v1"
DEFAULT_RUN_NAME = "dev-metadata-pilot-v1"
SOURCE_FILES = ("scripts/run_dev_metadata_pilot.py", "scripts/probe_search_metadata.py",
    "scripts/run_dev_query_probe.py", "plagiarism/search_tavily.py", "plagiarism/web_search.py",
    "plagiarism/matching.py", "plagiarism/web_fetch.py", "plagiarism/pdf_source.py",
    "plagiarism/pdf_evidence.py", "plagiarism/input_references.py", "plagiarism/jobs.py",
    "plagiarism/storage.py", "server.py")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical_json(value).encode()).hexdigest()


def private_dump(path, value, *, secret=None, exclusive=False):
    """Persist before side effects; an empty interrupted exclusive file blocks retries."""
    raw = (canonical_json(value) + "\n").encode("utf-8")
    if secret and secret.encode() in raw:
        raise ValueError("Reflected credential cannot be persisted")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    destination = path if exclusive else path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    destination.chmod(0o600)
    if not exclusive:
        destination.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def source_identity():
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def git_head():
    """Read-only provenance; unrelated worktree changes do not block preparation."""
    try:
        value = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        return value if re.fullmatch(r"[a-f0-9]{40}", value) else None
    except (OSError, subprocess.CalledProcessError):
        return None


def case_request_id(run_id, case_id, text_hash):
    if case_id not in CASE_IDS or not re.fullmatch(r"[a-f0-9]{64}", text_hash):
        raise ValueError("Only the frozen DEV inputs are eligible")
    return sha256((run_id + "\0" + case_id + "\0" + text_hash).encode()).hexdigest()[:32]


def call_identifier(request_id, index):
    if type(index) is not int or not 0 <= index < QUERIES_PER_CASE:
        raise ValueError("Each case is bounded to six calls")
    return sha256((request_id + "\0" + str(index)).encode()).hexdigest()


def validate_scope(plans):
    if set(plans) != set(CASE_IDS) or any(not isinstance(p, list) or len(p) != QUERIES_PER_CASE for p in plans.values()):
        raise ValueError("Exactly D08, D10, D04 with six queries each are required")
    if sum(map(len, plans.values())) != CALL_CAP:
        raise ValueError("The fixed pilot is capped at eighteen Basic calls")


def prepare(run_name=DEFAULT_RUN_NAME):
    """Read the DEV key and only the three selected input files; freeze before API."""
    if not re.fullmatch(r"dev-[a-z0-9-]+", run_name):
        raise ValueError("A private DEV run name is required")
    dev = ROOT / "data/private/web-evaluation/materials/dev-30"
    key_raw = (dev / "key.json").read_bytes()
    cases = json.loads(key_raw)["cases"]
    selected = [next(c for c in cases if c["id"] == cid) for cid in CASE_IDS]
    texts = {}
    for case in selected:
        path = (dev / case["input_path"]).resolve()
        if case.get("split") != "dev" or not path.is_relative_to(dev.resolve()):
            raise ValueError("Input must stay inside DEV-30")
        text = path.read_text()
        if sha256(text.encode()).hexdigest() != case["text_sha256"]:
            raise ValueError("Frozen DEV input hash changed")
        texts[case["id"]] = text
    plans = {cid: web.plan_queries(texts[cid], strategy=web.DENSE_STRATEGY) for cid in CASE_IDS}
    validate_scope(plans)
    code = source_identity()
    candidate = {"id": STRATEGY, "metadata_version": METADATA_VERSION, "minimum_own_query_contiguous_words": 6,
        "independent_title_and_content_chunks": True, "fallback": web.PATH_URL_STRATEGY,
        "metadata_is_matching_evidence": False, "source_sha256": {
            name: code[name] for name in ("scripts/run_dev_metadata_pilot.py", "scripts/probe_search_metadata.py")}}
    configuration = {"schema_version": 1, "case_ids": list(CASE_IDS), "query_strategy": web.DENSE_STRATEGY,
        "experimental_strategy": STRATEGY, "candidate": candidate, "candidate_sha256": digest(candidate),
        "input_key_sha256": sha256(key_raw).hexdigest(),
        "input_sha256": {c["id"]: c["text_sha256"] for c in selected}, "query_plans": plans,
        "query_plans_sha256": digest(plans), "source_code_sha256": code,
        "search_request_contract": {"search_depth": "basic", "include_usage": True, "max_results": 5},
        "max_actual_provider_calls": CALL_CAP, "max_queries_per_case": QUERIES_PER_CASE,
        "named_references_enabled": False,
        "pdf_alternative_engine": os.environ.get("ISTOK_PDF_ALTERNATIVE_ENGINE")}
    run_id = digest({"run_name": run_name, "configuration": configuration})[:32]
    configuration["request_ids"] = {cid: case_request_id(run_id, cid, configuration["input_sha256"][cid]) for cid in CASE_IDS}
    out = ROOT / "data/private/web-evaluation" / run_name
    out.mkdir(parents=True, mode=0o700, exist_ok=True)
    out.chmod(0o700)
    manifest = out / "run.json"
    if manifest.exists():
        run = json.loads(manifest.read_text())
        if run.get("configuration") != configuration or run.get("id") != run_id:
            raise ValueError("Frozen pilot inputs, candidate or code changed; use GET recovery, never resubmit")
    else:
        run = {"id": run_id, "run_name": run_name, "configuration": configuration,
               "prepared_at": datetime.now(timezone.utc).isoformat(), "prepared_without_api": True,
               "git_head_at_prepare": git_head()}
        private_dump(manifest, run, exclusive=True)
    return out, run, texts


def call_map(run):
    config = run["configuration"]
    validate_scope(config["query_plans"])
    return {call_identifier(config["request_ids"][cid], index):
            {"case_id": cid, "query_index": index, "query": item["query"], "span": {k: item[k] for k in ("start", "end")}}
            for cid in CASE_IDS for index, item in enumerate(config["query_plans"][cid])}


def call_path(out, identity, call_id):
    return out / "calls" / identity["case_id"] / (str(identity["query_index"]) + "-" + call_id + ".json")


class MetadataOpener:
    """Tee bounded bytes in memory; return the original response bytes to TavilySearch."""
    def __init__(self, opener, secret):
        self.opener, self.secret, self.results = opener, secret, None

    def open(self, request, timeout):
        original = self.opener.open(request, timeout=timeout)
        outer = self

        class Response:
            def __enter__(self):
                self.response = original.__enter__()
                return self

            def __exit__(self, *args):
                return original.__exit__(*args)

            def read(self, limit):
                raw = self.response.read(limit)
                from plagiarism.search_tavily import MAX_RESPONSE_BYTES
                if len(raw) <= MAX_RESPONSE_BYTES:
                    try:
                        payload = json.loads(raw)
                        outer.results = retain_results(payload["results"], secret=outer.secret)
                    except (KeyError, ValueError, TypeError, UnicodeError):
                        outer.results = None
                return raw

        return Response()


def safe_code(error, secret=None):
    code = getattr(error, "code", "failed_or_unknown")
    return code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) and not (secret and secret in code) else "failed_or_unknown"


class RecordingSearch:
    provider = "tavily"

    def __init__(self, out, run, real):
        self.out, self.calls, self.real = out, call_map(run), real
        self.tee = MetadataOpener(real.opener, real.key)
        real.opener = self.tee

    def search(self, query, request_id):
        identity = self.calls.get(request_id)
        if identity is None or query != identity["query"]:
            raise web.WebSearchError("Unfrozen provider call blocked", "search_budget_exhausted")
        path = call_path(self.out, identity, request_id)
        markers = list((self.out / "calls").glob("*/*.json"))
        if path.exists():
            raise web.WebSearchError("Submitted provider calls cannot be repeated", "search_already_attempted")
        if len(markers) >= CALL_CAP or len([p for p in markers if p.parent.name == identity["case_id"]]) >= QUERIES_PER_CASE:
            raise web.WebSearchError("Frozen pilot cap exhausted", "search_budget_exhausted")
        record = {**identity, "request_id": request_id, "search_depth": "basic", "state": "submitted"}
        try:
            private_dump(path, record, secret=self.real.key, exclusive=True)
        except FileExistsError:
            raise web.WebSearchError("Submitted provider calls cannot be repeated", "search_already_attempted") from None
        self.tee.results = None
        try:
            urls = self.real.search(query, request_id)  # Actual reserve/finish/idempotency and request body remain unchanged.
            metadata = self.tee.results
            if metadata is None or [r["url"] for r in metadata] != [r["url"] for r in urls]:
                raise web.WebSearchError("Sanitized metadata response unavailable", "search_provider_error")
            record.update(state="complete", results=metadata, usage=self.real.last_usage)
            private_dump(path, record, secret=self.real.key)
            return urls  # URL-only adapter output; metadata cannot enter matching evidence.
        except Exception as error:
            record.update(state="failed_or_unknown", code=safe_code(error, self.real.key))
            private_dump(path, record, secret=self.real.key)
            raise web.WebSearchError("Pilot search failed or is uncertain; no retry", record["code"]) from None


def page_path(out, cid, url):
    return out / "pages" / cid / (sha256(url.encode()).hexdigest() + ".json")


def clean_fetched(fetched, secret):
    # Preserve normal extraction and PDF evidence fields, never raw headers/body.
    keys = ("url", "requested_url", "title", "text", "links", "text_sha256", "fetched_at", "content_type",
        "encoding", "bytes_read", "decoded_bytes", "robots_url", "extraction", "pdf_pages", "pdf_parser_version",
        "supplemental_texts", "optional_status", "raw_pdf_sha256")
    result = {key: fetched[key] for key in keys if key in fetched}
    if secret and secret in canonical_json(result):
        raise ValueError("Reflected credential in fetched content")
    return result


class RecordingPages:
    def __init__(self, out, cid, real, secret):
        self.out, self.cid, self.real, self.secret = out, cid, real, secret

    def fetch(self, url):
        path = page_path(self.out, self.cid, url)
        record = {"requested_url": url, "case_id": self.cid, "state": "submitted"}
        try:
            private_dump(path, record, secret=self.secret, exclusive=True)
        except FileExistsError:
            raise web.WebSearchError("Page was already attempted; no refill or retry", "page_already_attempted") from None
        try:
            fetched = clean_fetched(self.real.fetch(url), self.secret)
            record.update(state="complete", fetched=fetched)
            private_dump(path, record, secret=self.secret)
            return fetched
        except Exception as error:
            record.update(state="failed_or_unknown", code=safe_code(error, self.secret))
            private_dump(path, record, secret=self.secret)
            raise web.WebSearchError("Page unavailable in frozen pilot", record["code"]) from None


def saved_queries(out, run, cid, *, include_failed=False):
    records = []
    for identifier, identity in call_map(run).items():
        if identity["case_id"] != cid:
            continue
        path = call_path(out, identity, identifier)
        if path.exists():
            record = json.loads(path.read_text())
            if record.get("request_id") != identifier or record.get("query") != identity["query"]:
                raise ValueError("Saved query identity changed")
            if record.get("state") == "complete" or include_failed:
                records.append(record)
    return records


def ranking_records(records):
    # Match production's canonical URL view while retaining original provider ranks.
    result = []
    for record in records:
        values, seen = [], set()
        for item in record["results"]:
            url = web._canonical_url(item["url"])
            if url and url not in seen:
                values.append({**item, "url": url})
                seen.add(url)
        result.append({"query": record["query"], "results": values})
    return result


@contextmanager
def metadata_order(text, records, audit):
    original = web._fetch_order

    def ordered(urls, groups, strategy):
        if strategy != web.BALANCED_URL_STRATEGY:
            raise ValueError("Experimental wrapper requires the inert BALANCED enum")
        baseline = web._rank_url_paths(original(urls, groups, web.PATH_URL_STRATEGY), text)
        selected, reasons = rank_by_snippet_overlap(baseline, ranking_records(records), projected=True)
        for reason in reasons.values():
            # Failed earlier calls are absent from ranking groups, but audit indices
            # must still point at the original six-query plan.
            reason["query_index"] = records[reason["query_index"]]["query_index"]
        audit.update(baseline_path_order=baseline, candidate_order=selected, reasons=reasons)
        return selected

    web._fetch_order = ordered
    try:
        yield
    finally:
        web._fetch_order = original


def candidate_run(original, out, run, searcher, real_pages, text, request_id, progress=None):
    config = run["configuration"]
    cid = next((cid for cid in CASE_IDS if config["request_ids"][cid] == request_id), None)
    if cid is None or sha256(text.encode()).hexdigest() != config["input_sha256"][cid]:
        raise ValueError("Queue request does not match frozen pilot")
    if web.plan_queries(text, strategy=web.DENSE_STRATEGY) != config["query_plans"][cid]:
        raise ValueError("Query plan changed")
    # The order hook runs only after searches have completed, so load their frozen replies there.
    audit = {}
    original_order = web._fetch_order

    def order(urls, groups, strategy):
        web._fetch_order = original_order
        try:
            with metadata_order(text, saved_queries(out, run, cid), audit):
                return web._fetch_order(urls, groups, strategy)
        finally:
            web._fetch_order = order

    web._fetch_order = order
    try:
        result = original(text, request_id, progress=progress, searcher=searcher,
            fetcher=RecordingPages(out, cid, real_pages, searcher.real.key), query_strategy=web.DENSE_STRATEGY,
            url_strategy=web.BALANCED_URL_STRATEGY, use_named_references=False)
    finally:
        web._fetch_order = original_order
    result["web"].update(url_strategy=STRATEGY, experimental_strategy=STRATEGY,
        underlying_url_enum=web.BALANCED_URL_STRATEGY, baseline_url_strategy=web.PATH_URL_STRATEGY,
        metadata_ranking={**audit, "purpose": "ordering_only", "may_be_used_as_matching_evidence": False},
        frozen_query_plan=config["query_plans"][cid], frozen_candidate_sha256=config["candidate_sha256"],
        frozen_manifest_sha256=sha256((out / "run.json").read_bytes()).hexdigest())
    return result


class QueueHTTPError(RuntimeError):
    def __init__(self, status):
        super().__init__("Queue HTTP " + str(status))
        self.status = status


def queue_case(http, out, run, cid, text, *, allow_submit):
    """GET first even when a report/marker already exists; unknown never becomes POST."""
    config = run["configuration"]
    identifier = config["request_ids"][cid]
    path = out / (cid + "-request.json")
    marker = {"request_id": identifier, "text_sha256": config["input_sha256"][cid],
              "planned_queries": QUERIES_PER_CASE, "manifest_sha256": sha256((out / "run.json").read_bytes()).hexdigest()}
    saved_report = (out / (cid + ".json")).exists()
    prior_call = any((out / "calls" / cid).glob("*.json"))
    if path.exists() and json.loads(path.read_text()) != marker:
        raise ValueError("Saved submission identity changed; no POST allowed")
    try:
        return http("/api/jobs/" + identifier)["job"]
    except QueueHTTPError as error:
        if error.status != 404 or path.exists() or saved_report or prior_call or not allow_submit:
            raise RuntimeError("Unknown prior queue outcome; no POST allowed") from None
    private_dump(path, marker, exclusive=True)
    return http("/api/web-check-jobs", {"request_id": identifier, "text": text, "internet_consent": True})["job"]


def replay_baseline(out, run, cid, text, original=web.run):
    """PATH uses the same replies and only already fetched pages; missing is partial."""
    config = run["configuration"]
    records = {r["request_id"]: r for r in saved_queries(out, run, cid, include_failed=True)}
    unknown = []

    class Search:
        provider = "tavily"
        def search(self, query, identifier):
            record = records.get(identifier)
            if record is None or record["query"] != query:
                raise web.WebSearchError("Search response missing in frozen replay", "search_snapshot_missing")
            if record.get("state") != "complete":
                code = safe_code(type("SavedSearchError", (), {"code": record.get("code")})())
                raise web.WebSearchError("Frozen provider call failed or is uncertain", code)
            return [{"url": value["url"]} for value in record["results"]]

    class Pages:
        def fetch(self, url):
            path = page_path(out, cid, url)
            if not path.exists():
                unknown.append(url)
                raise web.WebSearchError("Page was not fetched; offline replay is unknown", "page_snapshot_missing")
            record = json.loads(path.read_text())
            if record.get("requested_url") != url:
                raise ValueError("Frozen page identity changed")
            if record.get("state") != "complete":
                raise web.WebSearchError("Frozen page attempt failed", record.get("code", "page_snapshot_unknown"))
            return record["fetched"]

    report = original(text, config["request_ids"][cid], searcher=Search(), fetcher=Pages(),
        query_strategy=web.DENSE_STRATEGY, url_strategy=web.PATH_URL_STRATEGY, use_named_references=False)
    report["web"].update(replay="same-frozen-search-replies-and-fetches", unknown_unfetched_urls=unknown,
        page_refills_allowed=False, conditional_on_saved_pages=True, frozen_query_plan=config["query_plans"][cid])
    if unknown:
        # An observed zero in saved bodies is not proof about unseen pages.
        report["web"].update(status="partial", exact_coverage_is_lower_bound=True,
            unfetched_page_evidence="unknown", coverage_interpretation="Observed saved page bodies only; missing page evidence remains unknown")
        report["stats"]["uniqueness_percent"] = None
    private_dump(out / (cid + "-baseline-path.json"), report)
    return report


def status(out, run):
    return {"state": "PREPARED", "run_name": run["run_name"], "manifest": str(out / "run.json"),
        "manifest_sha256": sha256((out / "run.json").read_bytes()).hexdigest(), "case_ids": list(CASE_IDS),
        "planned_basic_queries": CALL_CAP, "actual_submitted_markers": len(list((out / "calls").glob("*/*.json"))),
        "saved_reports": [cid for cid in CASE_IDS if (out / (cid + ".json")).exists()], "api_calls_in_prepare_status": 0}


def live(out, run, texts, manifest_sha256):
    if sha256((out / "run.json").read_bytes()).hexdigest() != manifest_sha256:
        raise ValueError("START requires the exact reviewed manifest SHA256")
    if run["configuration"]["source_code_sha256"] != source_identity():
        raise ValueError("Code changed since preparation; no new provider calls")
    lock = open(out / "live.lock", "a")
    os.chmod(out / "live.lock", 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _live_locked(out, run, texts)
    finally:
        lock.close()


def save_quota_after(out):
    """One best-effort usage observation; unavailable usage is never retried here."""
    try:
        result = quota(0)
    except Exception:
        result = {"unavailable": True, "new_searches_allowed": False}
    private_dump(out / "quota-after.json", result)


def _live_locked(out, run, texts):
    from functools import partial
    import server
    from plagiarism.jobs import Jobs
    from plagiarism.saas import report_identifier
    from plagiarism.search_tavily import TavilySearch
    from plagiarism.storage import Store
    from plagiarism.web_fetch import Fetcher
    fresh = [cid for cid in CASE_IDS if not (out / (cid + "-request.json")).exists() and not (out / (cid + ".json")).exists()]
    if fresh:
        # Use the full eighteen-call allowance even on a partially completed START.
        private_dump(out / "quota-before.json", quota(CALL_CAP))
    real = TavilySearch(web.SearchBudget())
    searcher = RecordingSearch(out, run, real)
    original = web.run
    web.run = partial(candidate_run, original, out, run, searcher, Fetcher())
    service = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    service.saas, service.store, service.jobs = None, Store(), Jobs()
    Thread(target=service.serve_forever, daemon=True).start()

    def http(path, body=None):
        connection = HTTPConnection("127.0.0.1", service.server_port, timeout=20)
        try:
            connection.request("GET" if body is None else "POST", path,
                None if body is None else canonical_json(body), {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                response.read()
                raise QueueHTTPError(response.status)
            return json.load(response)
        finally:
            connection.close()

    try:
        for cid in CASE_IDS:
            identifier = run["configuration"]["request_ids"][cid]
            job = queue_case(http, out, run, cid, texts[cid], allow_submit=cid in fresh)
            started = time.monotonic()
            while job["status"] not in ("complete", "failed", "cancelled"):
                if time.monotonic() - started > 420:
                    raise RuntimeError("Queue timeout; only GET recovery is allowed")
                time.sleep(0.5)
                job = http("/api/jobs/" + identifier)["job"]
            report = service.store.get("report", report_identifier(identifier))
            if report is None:
                private_dump(out / (cid + "-error.json"), {"request_id": identifier, "status": job["status"], "code": safe_code(type("E", (), {"code": job.get("code")})())})
                continue
            config = run["configuration"]
            if (report["text"] != texts[cid] or report["web"]["url_strategy"] != STRATEGY
                    or report["web"]["query_strategy"] != web.DENSE_STRATEGY
                    or report["web"]["queries_attempted"] > QUERIES_PER_CASE
                    or report["web"]["urls_attempted"] > web.MAX_URLS
                    or report["web"]["query_spans"] != [{k: p[k] for k in ("start", "end")} for p in config["query_plans"][cid]]):
                raise ValueError("Persisted queue report does not match the frozen pilot")
            private_dump(out / (cid + ".json"), report, secret=real.key)
            replay_baseline(out, run, cid, texts[cid], original=original)
            print(cid, "saved", "queries", report["web"]["queries_attempted"], "pages", report["web"]["urls_attempted"], flush=True)
    finally:
        try:
            service.shutdown()
            service.server_close()
            service.jobs.close()
        finally:
            web.run = original
            save_quota_after(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="Offline default: freeze manifest and query plans")
    mode.add_argument("--status", action="store_true", help="Read saved manifest/artifact status, without API")
    mode.add_argument("--live", action="store_true", help="Separately authorized live START only")
    parser.add_argument("--run-name", default=DEFAULT_RUN_NAME)
    parser.add_argument("--start-confirmation", choices=("START",))
    parser.add_argument("--max-new-queries", type=int)
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.live and (args.start_confirmation != "START" or args.max_new_queries != CALL_CAP
            or not args.manifest_sha256 or not re.fullmatch(r"[a-f0-9]{64}", args.manifest_sha256)):
        parser.error("Live execution requires --start-confirmation START --max-new-queries 18 --manifest-sha256 REVIEWED_SHA256")
    if args.status:
        if not re.fullmatch(r"dev-[a-z0-9-]+", args.run_name):
            parser.error("A private DEV run name is required")
        out = ROOT / "data/private/web-evaluation" / args.run_name
        run = json.loads((out / "run.json").read_text())
    else:
        out, run, texts = prepare(args.run_name)
        if args.live:
            live(out, run, texts, args.manifest_sha256)
    print(json.dumps(status(out, run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
