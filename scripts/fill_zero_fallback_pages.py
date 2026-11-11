#!/usr/bin/env python3
"""Prepare/status frozen ZERO page fills; --fill requires a separate START.

At most160 durable direct-page intents, sequential normal Fetcher only, no
search API, model, oracle, recursive navigation or retry of uncertain outcomes.
The source is the fixed V3 comparison and its selected unknown extra URLs.
"""
import argparse
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search as web
from plagiarism.web_evaluation import _report
from plagiarism.web_fetch import Fetcher, TOTAL_TIMEOUT
from scripts.dev_report_provenance import source_snapshot_signatures

BASE = ROOT / "data/private/web-evaluation"
COMPARISON_SERIES = "dev-zero-fallback-local-v3"
OUTPUT_SERIES = "dev-zero-fallback-fill-v1"
COMPARISON_SHA256 = "fd153ae10842f7d8467113cdf3dec073a67b815647e3ea3fbf0b84f2cfee3b17"
REFRESH_D10_URL = "https://ru.wikipedia.org/wiki/%D0%94%D0%B0%D0%B3%D0%B5%D1%80%D0%BE%D1%82%D0%B8%D0%BF%D0%B8%D1%8F"
MAX_ATTEMPTS = 160
CASE_BUDGET_SECONDS = 300
FINAL_BATCHES = tuple((f"dev-final-selected-{letter}-v1", tuple(f"D{i:02d}" for i in range(start, start + 6)))
                      for letter, start in zip("abcde", (1, 7, 13, 19, 25)))
CODE_FILES = ("scripts/fill_zero_fallback_pages.py", "plagiarism/web_fetch.py", "plagiarism/pdf_source.py")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return sha256(value.encode()).hexdigest()


def _write(path, value, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    destination = path if exclusive else path.with_suffix(path.suffix + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(destination, flags, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    destination.chmod(0o600)
    if not exclusive:
        destination.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read(path, root):
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Frozen input leaves the explicit workspace")
    raw = path.read_bytes()
    return json.loads(raw), sha256(raw).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _url(value):
    result = web._canonical_url(value)
    if result is None or result != value:
        raise ValueError("Frozen selected URL must already be canonical")
    return result


def _identity(root):
    return {name: sha256((root / name).read_bytes()).hexdigest() for name in CODE_FILES}


def _refresh(row, base, root, hashes):
    audit = row["audit"]
    if row["id"] != "D10" or REFRESH_D10_URL not in audit["extra_attempted_urls"]:
        return False
    event = next((event for event in audit.get("events", []) if event.get("requested_url") == REFRESH_D10_URL), {})
    origin = event.get("snapshot_origin")
    if (event.get("state") != "read_saved_body" or not isinstance(origin, dict)
            or not str(origin.get("path", "")).endswith("/dev-final-control-live-v1/D10.json")):
        return False
    path = base / COMPARISON_SERIES / "after/D10.json"
    report, actual = _read(path, root)
    hashes[path.relative_to(root).as_posix()] = actual
    text = report.get("text")
    if not isinstance(text, str):
        raise ValueError("Frozen D10 replay has no submitted text")
    checked = _report(report, {"id": "zero-fill-d10-refresh", "text_sha256": _hash(text)})
    return any(match["url"] == REFRESH_D10_URL for match in checked["matches"])


def prepare(base=BASE, root=ROOT):
    """Offline only: bind the exact comparison, code, elapsed budgets and allowlist."""
    base, root = Path(base), Path(root)
    if os.environ.get("ISTOK_PDF_ALTERNATIVE_ENGINE") != "pdfium":
        raise ValueError("Prepare/fill require the same explicit ISTOK_PDF_ALTERNATIVE_ENGINE=pdfium environment")
    comparison_path = base / COMPARISON_SERIES / "comparison.json"
    comparison, comparison_hash = _read(comparison_path, root)
    if comparison_hash != COMPARISON_SHA256:
        raise ValueError("Only the explicitly frozen ZERO V3 comparison is eligible")
    plan = comparison.get("frozen_plan", {})
    candidate_hash = plan.get("candidate_sha256")
    if not isinstance(candidate_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", candidate_hash):
        raise ValueError("Frozen candidate SHA256 is missing")
    if sha256((root / "scripts/probe_zero_fallback.py").read_bytes()).hexdigest() != candidate_hash:
        raise ValueError("Candidate code differs from the frozen comparison")
    rows = comparison.get("cases")
    if not isinstance(rows, list) or [row.get("id") for row in rows] != [f"D{i:02d}" for i in range(1, 31)]:
        raise ValueError("Only the fixed DEV30 comparison is supported")
    hashes = {comparison_path.relative_to(root).as_posix(): comparison_hash}
    selections, case_inputs = {}, {}
    for row in rows:
        cid, audit = row["id"], row["audit"]
        extras, unknown = audit.get("extra_attempted_urls"), audit.get("unknown_extra_urls")
        prefix = audit.get("first_phase_attempted_urls")
        if (not isinstance(extras, list) or len(extras) > 12 or len(set(extras)) != len(extras)
                or not isinstance(unknown, list) or len(set(unknown)) != len(unknown)
                or not set(unknown) <= set(extras) or not isinstance(prefix, list) or len(prefix) > 12
                or audit.get("prefix_preserved") is not True):
            raise ValueError("Comparison contains an invalid frozen extra/prefix selection")
        if extras and (audit.get("triggered") is not True or audit.get("initial_gate_words") != 0):
            raise ValueError("Extra URLs were not selected by the frozen initial-zero gate")
        for url in prefix + extras:
            _url(url)
        refresh = _refresh(row, base, root, hashes)
        selected = set(unknown) | ({REFRESH_D10_URL} if refresh else set())
        if not selected:
            continue
        directory = next(name for name, ids in FINAL_BATCHES if cid in ids)
        report_path = base / directory / (cid + ".json")
        original, report_hash = _read(report_path, root)
        metadata = original.get("web", {})
        elapsed = metadata.get("elapsed_seconds")
        if (original.get("id") != row.get("original_report_id") or not _number(elapsed)
                or metadata.get("attempted_urls") != prefix or metadata.get("urls_attempted") != len(prefix)):
            raise ValueError("Original elapsed time or first phase differs from the frozen comparison")
        if source_snapshot_signatures(original) != audit.get("first_phase_source_signatures"):
            raise ValueError("Original source snapshots differ from the frozen first phase")
        relative = report_path.relative_to(root).as_posix()
        hashes[relative] = report_hash
        case_inputs[cid] = {"original_report": relative, "original_report_sha256": report_hash,
                            "original_elapsed_seconds": elapsed, "first_phase_attempted_urls": prefix,
                            "first_phase_source_signatures": audit.get("first_phase_source_signatures", {})}
        for index, url in enumerate(extras):
            if url not in selected:
                continue
            entry = selections.setdefault(url, {"requested_url": url, "selected_case_ids": [], "selection": []})
            entry["selected_case_ids"].append(cid)
            entry["selection"].append({"case_id": cid, "extra_index": index,
                "kind": "approved_d10_old_positive_refresh" if refresh and url == REFRESH_D10_URL else "frozen_zero_unknown_extra"})
    configuration = {"schema_version": 1, "source_comparison": comparison_path.relative_to(root).as_posix(),
        "source_comparison_sha256": comparison_hash, "candidate_sha256": candidate_hash,
        "input_artifacts_sha256": hashes, "source_code_sha256": _identity(root),
        "pdf_alternative_engine": "pdfium", "maximum_cumulative_direct_attempts": MAX_ATTEMPTS,
        "per_case_total_elapsed_budget_seconds": CASE_BUDGET_SECONDS,
        "minimum_start_headroom_seconds": TOTAL_TIMEOUT, "concurrency": 1, "new_searches": 0,
        "case_inputs": case_inputs, "selected_urls": list(selections.values())}
    out = base / OUTPUT_SERIES
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    out.chmod(0o700)
    path = out / "run.json"
    if path.exists():
        run = json.loads(path.read_text())
        if run.get("configuration") != configuration:
            raise ValueError("Saved fill manifest differs from frozen inputs; only status is allowed")
    else:
        run = {"prepared_at": datetime.now(timezone.utc).isoformat(), "configuration": configuration}
        _write(path, run, exclusive=True)
    return out, run


def _valid_page(page):
    if (not isinstance(page, dict) or not isinstance(page.get("text"), str)
            or page.get("text_sha256") != _hash(page["text"]) or not isinstance(page.get("links"), list)
            or web._canonical_url(page.get("url")) is None):
        raise ValueError("Normal Fetcher returned an invalid page snapshot")
    _json(page)


def _records(out, run):
    config = run["configuration"]
    allowed = {_hash(item["requested_url"]): item for item in config["selected_urls"]}
    paths = sorted((out / "fills").glob("*.json"))
    if len(paths) > MAX_ATTEMPTS:
        raise ValueError("Cumulative direct-page intent cap exceeded")
    records = {}
    for path in paths:
        identity = allowed.get(path.stem)
        if identity is None or path.is_symlink():
            raise ValueError("Unbound direct-page intent")
        try:
            record = json.loads(path.read_text())
        except (ValueError, UnicodeError):
            # An interrupted durable claim is uncertain, never permission to retry.
            record = {"requested_url": identity["requested_url"], "state": "submitted",
                      "selected_case_ids": identity["selected_case_ids"], "unreadable_intent": True}
        if not record.get("unreadable_intent") and (
                record.get("requested_url") != identity["requested_url"]
                or record.get("selected_case_ids") != identity["selected_case_ids"]
                or record.get("source_comparison_sha256") != config["source_comparison_sha256"]
                or record.get("candidate_sha256") != config["candidate_sha256"]
                or record.get("state") not in ("submitted", "complete", "failed_or_unknown")):
            raise ValueError("Saved direct-page outcome differs from the frozen selection")
        if record["state"] == "complete":
            _valid_page(record.get("fetched"))
        elapsed = record.get("elapsed_seconds")
        if elapsed is not None and not _number(elapsed):
            raise ValueError("Invalid saved direct-page elapsed time")
        records[identity["requested_url"]] = record
    return records


def status(out, run):
    records = _records(out, run)
    return {"manifest": str(out / "run.json"), "manifest_sha256": sha256((out / "run.json").read_bytes()).hexdigest(),
        "selected_unique_urls": len(run["configuration"]["selected_urls"]), "durable_attempt_intents": len(records),
        "complete": sum(value["state"] == "complete" for value in records.values()),
        "failed_or_unknown": sum(value["state"] != "complete" for value in records.values()),
        "maximum_cumulative_direct_attempts": MAX_ATTEMPTS, "new_searches": 0, "concurrency": 1}


def fill(out, run, expected_manifest_sha256, *, fetcher=None, clock=time.perf_counter):
    """Execute only an already reviewed manifest; tests inject a synthetic Fetcher."""
    if sha256((out / "run.json").read_bytes()).hexdigest() != expected_manifest_sha256:
        raise ValueError("Fill START requires the exact reviewed manifest SHA256")
    if json.loads((out / "run.json").read_text()) != run:
        raise ValueError("Fill configuration differs from its reviewed manifest")
    if os.environ.get("ISTOK_PDF_ALTERNATIVE_ENGINE") != "pdfium":
        raise ValueError("Fill START requires the frozen pdfium environment")
    with (out / "fill.lock").open("a") as lock:
        os.chmod(out / "fill.lock", 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        records = _records(out, run)
        config = run["configuration"]
        spent = {cid: 0 for cid in config["case_inputs"]}
        aliases = {}
        for url, record in records.items():
            duration = record.get("elapsed_seconds", TOTAL_TIMEOUT)
            duration = TOTAL_TIMEOUT if duration is None else duration
            if record["state"] == "submitted":
                duration = max(TOTAL_TIMEOUT, duration)
            for cid in record["selected_case_ids"]:
                spent[cid] += duration
            if record["state"] == "complete":
                aliases[web._canonical_url(record["fetched"]["url"])] = url
        skipped, reused_aliases, new_attempts = [], [], 0
        try:
            for entry in config["selected_urls"]:
                url = entry["requested_url"]
                if url in records:
                    continue
                if url in aliases:
                    reused_aliases.append({"requested_url": url, "known_record_requested_url": aliases[url]})
                    continue
                if len(records) >= MAX_ATTEMPTS:
                    skipped.append({"requested_url": url, "reason": "cumulative_direct_attempt_cap"})
                    continue
                blocked = [cid for cid in entry["selected_case_ids"] if
                    config["case_inputs"][cid]["original_elapsed_seconds"] + spent[cid] + TOTAL_TIMEOUT > CASE_BUDGET_SECONDS]
                if blocked:
                    skipped.append({"requested_url": url, "reason": "per_case_total_elapsed_budget", "case_ids": blocked,
                        "remaining_seconds": {cid: max(0, CASE_BUDGET_SECONDS - config["case_inputs"][cid]["original_elapsed_seconds"] - spent[cid]) for cid in blocked}})
                    continue
                path = out / "fills" / (_hash(url) + ".json")
                intent = {**entry, "state": "submitted", "submitted_at": datetime.now(timezone.utc).isoformat(),
                    "source_comparison_sha256": config["source_comparison_sha256"], "candidate_sha256": config["candidate_sha256"],
                    "manifest_sha256": expected_manifest_sha256}
                try:
                    _write(path, intent, exclusive=True)
                except FileExistsError:
                    records = _records(out, run)
                    continue
                records[url] = intent
                new_attempts += 1
                started = clock()
                try:
                    if fetcher is None:
                        fetcher = Fetcher()
                    fetched = fetcher.fetch(url)
                    _valid_page(fetched)
                    intent.update(state="complete", fetched=fetched)
                    aliases[web._canonical_url(fetched["url"])] = url
                except Exception as error:
                    code = getattr(error, "code", "fetch_error")
                    intent.update(state="failed_or_unknown", code=code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "fetch_error",
                                  error_type=type(error).__name__)
                finally:
                    elapsed = max(0, clock() - started)
                    intent.update(elapsed_seconds=elapsed, completed_at=datetime.now(timezone.utc).isoformat())
                    for cid in entry["selected_case_ids"]:
                        spent[cid] += elapsed
                    _write(path, intent)
        finally:
            summary = {**status(out, run), "new_attempts_in_this_invocation": new_attempts,
                "extra_elapsed_seconds_by_case": spent, "skipped_unknown_urls": skipped,
                "reused_redirect_aliases": reused_aliases, "new_searches": 0}
            _write(out / "fill-summary.json", summary)
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--status", action="store_true")
    modes.add_argument("--fill", action="store_true")
    parser.add_argument("--start-confirmation", choices=("START",))
    parser.add_argument("--max-new-page-attempts", type=int)
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.fill and (args.start_confirmation != "START" or args.max_new_page_attempts != MAX_ATTEMPTS
            or not isinstance(args.manifest_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", args.manifest_sha256)):
        parser.error("--fill requires --start-confirmation START --max-new-page-attempts 160 --manifest-sha256 REVIEWED_SHA256")
    if args.status:
        out = BASE / OUTPUT_SERIES
        run = json.loads((out / "run.json").read_text())
    else:
        out, run = prepare()
        if args.fill:
            print(json.dumps(fill(out, run, args.manifest_sha256), ensure_ascii=False, indent=2))
            return
    print(json.dumps(status(out, run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
