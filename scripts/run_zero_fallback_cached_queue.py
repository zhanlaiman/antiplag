#!/usr/bin/env python3
"""Persist three saved-data ZERO examples through the ordinary HTTP queue.

Only loopback sockets are permitted. Search replies and physical page outcomes
come from the validated DEV replay pool; no provider or page request is made.
"""
from datetime import datetime, timezone
from hashlib import sha256
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plagiarism import web_search as web
from plagiarism.jobs import Jobs
from plagiarism.saas import report_identifier
from plagiarism.storage import Store
from scripts.dev_report_provenance import source_snapshot_signatures
from scripts.replay_final_url_policies import match_identity
from scripts.replay_saved_dev_search import FrozenSearch, load_queries
from scripts.replay_zero_fallback_dev import pool_pages
from scripts.replay_zero_fallback_production import PrefixAndExtraPages
from scripts.run_dev_metadata_pilot import QueueHTTPError, queue_http
from scripts.run_dev_query_probe import dump, source_identity
from scripts.summarize_night_results import BASE, DEV, DEV_KEY_SHA, FINAL_BATCHES

OUT = BASE / "dev-zero-fallback-cached-queue-v1"
IDS = ("D08", "D10", "D04")


def saved_job_or_submit(http, out, cid, identifier, text, manifest_hash):
    """A missing durable submission is the sole condition allowing one POST."""
    marker = out / (cid + "-request.json")
    expected = {"request_id": identifier, "text_sha256": sha256(text.encode()).hexdigest(),
                "manifest_sha256": manifest_hash}
    if marker.exists() and json.loads(marker.read_text()) != expected:
        raise ValueError("Changed saved cached-queue submission")
    try:
        return http("/api/jobs/" + identifier)["job"]
    except QueueHTTPError as error:
        if error.status != 404 or marker.exists() or (out / (cid + ".json")).exists():
            raise RuntimeError("Unknown prior job outcome; no repeated POST") from None
    dump(marker, expected)
    return http("/api/web-check-jobs", {"request_id": identifier, "text": text,
                                       "internet_consent": True})["job"]


def main():
    os.umask(0o077)
    for key in ("ISTOK_WEB_QUERY_STRATEGY", "ISTOK_WEB_URL_STRATEGY",
                "ISTOK_WEB_NAMED_REFERENCES", "ISTOK_WEB_ZERO_FALLBACK"):
        os.environ.pop(key, None)
    if sha256((DEV / "key.json").read_bytes()).hexdigest() != DEV_KEY_SHA:
        raise ValueError("DEV inputs changed")
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    selected = {case["id"]: case for case in cases if case["id"] in IDS}
    if set(selected) != set(IDS) or any(case["split"] != "dev" for case in selected.values()):
        raise ValueError("Only the three authorized DEV cases are supported")
    code = source_identity()
    configuration = {"case_ids": list(IDS), "source_code_sha256": code,
        "defaults": {"query": web.DEFAULT_QUERY_STRATEGY, "url": web.DEFAULT_URL_STRATEGY,
                     "named_references": web.DEFAULT_NAMED_REFERENCES,
                     "zero_fallback": web.DEFAULT_ZERO_FALLBACK},
        "production_comparison_sha256": sha256((BASE / "dev-zero-fallback-production-v1/comparison.json").read_bytes()).hexdigest(),
        "network_queries": 0, "fresh_page_load_attempts": 0}
    if configuration["defaults"] != {"query": web.DENSE_STRATEGY, "url": web.PATH_URL_STRATEGY,
                                      "named_references": True, "zero_fallback": True}:
        raise ValueError("The final production defaults are not activated")
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = OUT / "run.json"
    if manifest.exists():
        run = json.loads(manifest.read_text())
        if run["configuration"] != configuration:
            raise ValueError("Frozen cached queue manifest changed; recover without resubmission")
    else:
        identity = sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
        run = {"configuration": configuration, "id": identity,
               "request_ids": {cid: sha256((identity + cid).encode()).hexdigest()[:32] for cid in IDS},
               "created_at": datetime.now(timezone.utc).isoformat(),
               "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
        dump(manifest, run)
    manifest_hash = sha256(manifest.read_bytes()).hexdigest()
    lookup, originals, _ = pool_pages()
    inputs, replies, identities = {}, {}, {}
    for cid, case in selected.items():
        text = (DEV / case["input_path"]).read_text()
        if sha256(text.encode()).hexdigest() != case["text_sha256"] or text != originals[cid]["report"]["text"]:
            raise ValueError("Changed DEV case text")
        directory = BASE / next(name for name, ids in FINAL_BATCHES if cid in ids)
        replies[cid], identities[cid] = load_queries(directory, cid, case["text_sha256"])
        inputs[cid] = text
    by_request = {value: cid for cid, value in run["request_ids"].items()}
    original_run = web.run
    diagnostics = {}
    def cached_run(text, request_id=None, progress=None):
        cid = by_request[request_id]
        if text != inputs[cid]:
            raise ValueError("Unexpected queued text")
        search = FrozenSearch(replies[cid])
        pages = PrefixAndExtraPages(originals[cid], lookup, search.allowed_urls)
        # Configuration is intentionally omitted: this verifies real defaults.
        report = original_run(text, request_id, progress=progress, searcher=search, fetcher=pages)
        if {key: report["web"].get(key) for key in ("query_strategy", "url_strategy",
                "named_references_enabled", "zero_fallback_enabled")} != {
                "query_strategy": web.DENSE_STRATEGY, "url_strategy": web.PATH_URL_STRATEGY,
                "named_references_enabled": True, "zero_fallback_enabled": True}:
            raise ValueError("The queued report did not use the final defaults")
        raw = originals[cid]["report"]
        count = len(raw["sources"])
        prefix_ok = (report["web"]["attempted_urls"][:len(pages.prefix)] == pages.prefix
            and report["sources"][:count] == raw["sources"])
        if not prefix_ok or search.missing_queries or pages.unknown or pages.validation_errors:
            raise ValueError("Cached normal queue lost its saved-data invariants")
        expected = json.loads((BASE / "dev-zero-fallback-production-v1/enabled" / (cid + ".json")).read_text())
        if (report["stats"]["matched_words"] != expected["stats"]["matched_words"]
                or {match_identity(m) for m in report["matches"]} != {match_identity(m) for m in expected["matches"]}
                or source_snapshot_signatures(report) != source_snapshot_signatures(expected)):
            raise ValueError("Final defaults differ from the explicit production replay")
        provenance = {"kind": "saved-dev-search-and-page-replay", "network_queries": 0,
            "fresh_page_load_attempts": 0, "fresh_page_loads": 0,
            "original_report_id": raw["id"], "prefix_preserved": True,
            "manifest_sha256": manifest_hash, "source_code_sha256": code,
            "query_identity": identities[cid], "unknown_selected_pages": []}
        report["offline_replay"] = provenance
        report["web"]["offline_replay"] = provenance
        diagnostics[cid] = {"cached_query_count": len(search.calls), "cached_physical_outcomes": pages.events}
        return report
    import server
    web.run = cached_run
    service = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    service.saas, service.store, service.jobs = None, Store(), Jobs()
    external = []
    def no_external(event, arguments):
        if event == "socket.connect":
            address = arguments[1]
            if not isinstance(address, tuple) or address[:2] != ("127.0.0.1", service.server_port):
                external.append(str(address))
                raise PermissionError("Cached queue permits only its own loopback HTTP service")
        if event == "socket.getaddrinfo" and arguments[0] != "127.0.0.1":
            external.append(str(arguments[0]))
            raise PermissionError("External resolution is forbidden in the cached queue")
    sys.addaudithook(no_external)
    Thread(target=service.serve_forever, daemon=True).start()
    http = lambda path, body=None: queue_http(service.server_port, path, body)
    results = []
    try:
        for cid in IDS:
            identifier = run["request_ids"][cid]
            job = saved_job_or_submit(http, OUT, cid, identifier, inputs[cid], manifest_hash)
            started = time.monotonic()
            while job["status"] not in ("complete", "failed", "cancelled"):
                if time.monotonic() - started > 60:
                    raise RuntimeError("Cached queue timeout; saved request must be recovered by GET")
                time.sleep(0.1)
                job = http("/api/jobs/" + identifier)["job"]
            report_id = report_identifier(identifier)
            report = service.store.get("report", report_id)
            if job["status"] != "complete" or report is None:
                dump(OUT / (cid + "-error.json"), job)
                raise RuntimeError("Cached job did not persist a complete report")
            if report["offline_replay"]["manifest_sha256"] != manifest_hash:
                raise ValueError("Recovered report belongs to another replay")
            first = http("/api/reports/" + report_id)["report"]
            second = http("/api/reports/" + report_id)["report"]
            if first != second or report["sources"] != service.store.get("report", report_id)["sources"]:
                raise ValueError("Reopening changed the report or source snapshots")
            dump(OUT / (cid + ".json"), report)
            if cid in diagnostics:
                dump(OUT / (cid + "-cache-events.json"), diagnostics[cid])
            row = {"id": cid, "request_id": identifier, "report_id": report_id,
                   "report_url": "http://127.0.0.1:8766/#check/" + report_id,
                   "matched_words": report["stats"]["matched_words"], "reopened_identically": True,
                   "extra_attempts": report["web"]["zero_fallback_extra_attempts"]}
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        if external or source_identity() != code:
            raise ValueError("External socket attempted or code changed during the queued proof")
        dump(OUT / "summary.json", {"manifest_sha256": manifest_hash, "network_queries": 0,
            "fresh_page_load_attempts": 0, "external_socket_attempts": external,
            "main_store": str(service.store.path), "source_code_sha256": code, "cases": results})
    finally:
        service.shutdown()
        service.server_close()
        service.jobs.close()
        web.run = original_run


if __name__ == "__main__":
    main()
