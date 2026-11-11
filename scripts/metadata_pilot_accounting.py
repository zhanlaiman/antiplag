"""Read-only accounting for the two explicitly declared DEV metadata pilots.

Only pilot manifests, bound provider-call records, the three report files,
quota observations and the preserved v1 diagnosis are eligible. No page
snapshots, other datasets, credentials, API clients or quota calls are used.
"""
from hashlib import sha256
import json
import math
from pathlib import Path
import re

from scripts.run_dev_metadata_pilot import (CALL_CAP, CASE_IDS, QUERIES_PER_CASE,
    call_map, case_request_id, digest)


SERIES = ("dev-metadata-pilot-v1", "dev-metadata-pilot-v2")
DIAGNOSIS = "failed-before-post-diagnosis.json"
_HEX32 = re.compile(r"[a-f0-9]{32}")
_HEX64 = re.compile(r"[a-f0-9]{64}")
_CALL_FILE = re.compile(r"([0-5])-([a-f0-9]{64})\.json")


def _read(path, directory, root, hashes):
    # Reject symlinks before opening a file: an accounting artifact must never
    # redirect a read into a page snapshot or another dataset.
    relative = path.relative_to(directory)
    cursor = directory
    for part in (None, *relative.parts):
        cursor = cursor if part is None else cursor / part
        if cursor.is_symlink():
            raise ValueError("Pilot accounting does not follow artifact symlinks")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(directory.resolve()) or not resolved.is_relative_to(root):
        raise ValueError("Pilot accounting artifact leaves the declared root")
    raw = path.read_bytes()
    hashes[resolved.relative_to(root).as_posix()] = sha256(raw).hexdigest()
    return json.loads(raw)


def _manifest(run, directory):
    if not isinstance(run, dict):
        raise ValueError("Invalid pilot manifest")
    config = run.get("configuration")
    if (not isinstance(config, dict) or run.get("run_name") != directory.name
            or not isinstance(run.get("id"), str) or not _HEX32.fullmatch(run["id"])
            or config.get("case_ids") != list(CASE_IDS)
            or config.get("max_actual_provider_calls") != CALL_CAP
            or config.get("max_queries_per_case") != QUERIES_PER_CASE):
        raise ValueError("Pilot manifest does not declare the exact eighteen-call DEV scope")
    plans, inputs, identifiers = (config.get(name) for name in ("query_plans", "input_sha256", "request_ids"))
    if any(not isinstance(value, dict) or set(value) != set(CASE_IDS) for value in (plans, inputs, identifiers)):
        raise ValueError("Pilot manifest has an unexpected case binding")
    for cid in CASE_IDS:
        if (not isinstance(inputs[cid], str) or not _HEX64.fullmatch(inputs[cid])
                or not isinstance(identifiers[cid], str) or not _HEX32.fullmatch(identifiers[cid])
                or identifiers[cid] != case_request_id(run["id"], cid, inputs[cid])
                or not isinstance(plans[cid], list) or len(plans[cid]) != QUERIES_PER_CASE):
            raise ValueError("Pilot input, request or query-count binding changed")
        seen = set()
        for item in plans[cid]:
            if not isinstance(item, dict):
                raise ValueError("Invalid frozen query plan")
            query, start, end = (item.get(name) for name in ("query", "start", "end"))
            if (not isinstance(query, str) or not 1 <= len(query) <= 400 or query in seen
                    or any(ord(char) < 32 for char in query)
                    or type(start) is not int or type(end) is not int or not 0 <= start < end <= 200_000):
                raise ValueError("Invalid frozen query or input span")
            seen.add(query)
    if config.get("query_plans_sha256") != digest(plans):
        raise ValueError("Pilot frozen query-plan digest changed")
    calls = call_map(run)
    if len(calls) != CALL_CAP:
        raise ValueError("Pilot provider-call identities are not unique")
    return config, calls


def _usage(record):
    usage = record.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict) or type(usage.get("credits")) is not int or not 0 <= usage["credits"] <= 1:
        raise ValueError("Invalid explicit Basic-search credit usage")
    return usage["credits"]


def _quota_observation(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid saved quota observation")
    numeric = ("plan_usage", "plan_limit", "paygo_usage", "paygo_limit", "local_monthly_attempts")
    for name in numeric:
        number = value.get(name)
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or number < 0):
            raise ValueError("Invalid saved quota counter")
    return {name: value[name] for name in ("current_plan", *numeric, "checked_at", "unavailable", "new_searches_allowed")
            if name in value}


def collect(base, root):
    """Return compatible expense rows and hashes; perform no writes or API calls."""
    base, root = Path(base), Path(root).resolve()
    if not base.resolve().is_relative_to(root):
        raise ValueError("Declared pilot base must stay inside the accounting root")
    rows, hashes = [], {}
    for name in SERIES:
        directory = base / name
        if not directory.exists():
            continue
        run_path = directory / "run.json"
        run = _read(run_path, directory, root, hashes)
        config, expected = _manifest(run, directory)
        manifest_hash = hashes[run_path.resolve().relative_to(root).as_posix()]
        paths = sorted((directory / "calls").glob("*/*.json"))
        if len(paths) > CALL_CAP:
            raise ValueError("Pilot has more than eighteen provider-call records")
        records, by_case = [], {cid: 0 for cid in CASE_IDS}
        for path in paths:
            match = _CALL_FILE.fullmatch(path.name)
            if path.parent.name not in CASE_IDS or not match or match[2] not in expected:
                raise ValueError("Unbound pilot provider-call artifact")
            identifier, identity = match[2], expected[match[2]]
            if identity["case_id"] != path.parent.name or identity["query_index"] != int(match[1]):
                raise ValueError("Provider-call filename does not match the frozen case/index")
            record = _read(path, directory, root, hashes)
            if (not isinstance(record, dict) or record.get("request_id") != identifier
                    or type(record.get("query_index")) is not int
                    or not isinstance(record.get("span"), dict)
                    or any(type(record["span"].get(key)) is not int for key in ("start", "end"))
                    or any(record.get(key) != value for key, value in identity.items())
                    or record.get("search_depth") != "basic"
                    or record.get("state") not in ("submitted", "complete", "failed_or_unknown")):
                raise ValueError("Provider-call record does not match the frozen manifest")
            records.append(record)
            by_case[identity["case_id"]] += 1
        if name == SERIES[0] and records:
            raise ValueError("Preserved failed-before-POST v1 must have zero provider-call records")

        saved, report_queries, report_attempts_match = [], 0, True
        for cid in CASE_IDS:
            path = directory / (cid + ".json")
            if not path.exists():
                continue
            report = _read(path, directory, root, hashes)
            metadata = report.get("web") if isinstance(report, dict) else None
            attempted = metadata.get("queries_attempted") if isinstance(metadata, dict) else None
            if (not isinstance(metadata, dict) or type(attempted) is not int or not 0 <= attempted <= QUERIES_PER_CASE
                    or metadata.get("query_strategy") != config.get("query_strategy")
                    or metadata.get("url_strategy") != config.get("experimental_strategy")
                    or metadata.get("frozen_query_plan") != config["query_plans"][cid]
                    or metadata.get("frozen_manifest_sha256") != manifest_hash):
                raise ValueError("Saved pilot report metadata does not match the frozen manifest")
            if attempted != by_case[cid]:
                raise ValueError("Saved pilot report attempts do not match its durable provider-call markers; accounting is incomplete")
            saved.append(cid)
            report_queries += attempted
            report_attempts_match = report_attempts_match and attempted == by_case[cid]
        if name == SERIES[0] and saved:
            raise ValueError("Preserved failed-before-POST v1 cannot have completed reports")

        observations = {}
        for label in ("before", "after"):
            path = directory / ("quota-" + label + ".json")
            if path.exists():
                observations[label] = _quota_observation(_read(path, directory, root, hashes))
        diagnosis = directory / DIAGNOSIS
        diagnosis_path = None
        if name == SERIES[0] and diagnosis.exists():
            _read(diagnosis, directory, root, hashes)
            diagnosis_path = diagnosis.resolve().relative_to(root).as_posix()
        usage = [_usage(record) for record in records]
        known = [credits for credits in usage if credits is not None]
        rows.append({"series": name, "saved_documents": len(saved), "saved_case_ids": saved,
            "planned_documents": len(CASE_IDS), "planned_queries": CALL_CAP,
            "queries_in_completed_reports": report_queries, "query_attempts": len(records),
            "report_attempts_match_provider_markers": report_attempts_match,
            "provider_records_complete": sum(r["state"] == "complete" for r in records),
            "provider_records_failed_or_unknown": sum(r["state"] != "complete" for r in records),
            "provider_records_submitted": sum(r["state"] == "submitted" for r in records),
            "submitted_documents": sum(count > 0 for count in by_case.values()),
            "submitted_documents_basis": "durable_provider_call_markers", "attempts_by_case": by_case,
            "confirmed_provider_credits": sum(known), "records_with_credit_usage": len(known),
            "records_without_credit_usage": len(records) - len(known),
            "provider_credit_usage_complete": len(known) == len(records),
            "quota_observations": observations, "failure_diagnosis_artifact": diagnosis_path,
            "note": "Attempts count durable provider-call markers, including submitted or unknown outcomes. Confirmed credits sum explicit usage only; missing usage is unknown, never proof of zero cost."})
    return rows, hashes
