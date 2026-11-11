"""Read-only expenses for the fixed short-paragraph paired DEV pilot.

Only the declared manifest, provider markers, eight reports/request markers
and saved quota observations are read. No private Python, pages, inputs,
replanning, Store, provider, or quota calls are executed.
"""
from hashlib import sha256
import json
from pathlib import Path
import re

from plagiarism.saas import report_identifier
from scripts.metadata_pilot_accounting import _read, _quota_observation, _usage

SERIES = ("dev-short-paragraph-pair-v2",)
CASE_IDS = ("A0008", "A0056", "A0041", "D30")
ROLES = ("baseline", "selected")
VERSION = "short-paragraph-replacement-v1"
HYPOTHESIS_SHA = "61105cbf088dc05f0780b38ee67d398d3bdefbc0b3626cac9bdeaf320476cd31"
CALL_CAP = 22
HEX64 = re.compile(r"[a-f0-9]{64}")


def _digest(value):
    return sha256(value.encode()).hexdigest()


def manifest_identity(run, name):
    """Rebuild public ID formulas without loading or executing the pilot."""
    config = run.get("configuration") if isinstance(run, dict) else None
    if (name not in SERIES or not isinstance(config, dict)
            or config.get("version") != VERSION or config.get("hypothesis_freeze_sha256") != HYPOTHESIS_SHA
            or config.get("case_ids") != list(CASE_IDS) or config.get("max_actual_provider_calls") != CALL_CAP
            or config.get("default_changed") is not False):
        raise ValueError("Wrong fixed short-paragraph paired-pilot scope")
    plans, inputs = config.get("plans"), config.get("input_sha256")
    code = config.get("code_sha256")
    if (not isinstance(code, dict) or not code
            or any(not isinstance(k, str) or not isinstance(v, str) or not HEX64.fullmatch(v)
                   for k, v in code.items())):
        raise ValueError("Paired source-code identity is missing or malformed")
    if any(not isinstance(v, dict) or set(v) != set(CASE_IDS) for v in (plans, inputs)):
        raise ValueError("Unexpected paired input or plan binding")
    for cid in CASE_IDS:
        if not isinstance(inputs[cid], str) or not HEX64.fullmatch(inputs[cid]):
            raise ValueError("Invalid paired input SHA")
        roles = plans[cid]
        if not isinstance(roles, dict) or set(roles) != set(ROLES):
            raise ValueError("Expected exactly baseline and selected plans")
        for plan in roles.values():
            if not isinstance(plan, list) or len(plan) != 6:
                raise ValueError("Each paired plan must contain six queries")
            for item in plan:
                if not isinstance(item, dict):
                    raise ValueError("Invalid paired query")
                q, start, end = (item.get(k) for k in ("query", "start", "end"))
                if (not isinstance(q, str) or not 1 <= len(q) <= 400 or any(ord(c) < 32 for c in q)
                        or type(start) is not int or type(end) is not int or not 0 <= start < end <= 200_000):
                    raise ValueError("Invalid paired query or input span")
        a, b = roles["baseline"], roles["selected"]
        left, right = {p["query"] for p in a}, {p["query"] for p in b}
        if len(left) != 6 or len(right) != 6 or a[:5] != b[:5] or len(left & right) != 5:
            raise ValueError("Pair must share exactly five queries and replace only its sixth")
    fingerprint = _digest(json.dumps(config, sort_keys=True, ensure_ascii=False, allow_nan=False))
    requests = {cid + "-" + role: _digest(fingerprint + cid + role)[:32]
                for cid in CASE_IDS for role in ROLES}
    if run.get("id") != fingerprint or run.get("request_ids") != requests:
        raise ValueError("Paired stable request identity changed")
    allowed, jobs = {}, {}
    for cid in CASE_IDS:
        live = {}
        if cid != "D30":
            for i, item in enumerate(plans[cid]["baseline"]):
                call = _digest(fingerprint + cid + "baseline" + str(i))
                allowed[call] = {"case_id": cid + "-baseline", "query_index": i, "query": item["query"]}
                live[item["query"]] = call
        new = plans[cid]["selected"][5]["query"]
        call = _digest(fingerprint + cid + "new6")
        allowed[call] = {"case_id": cid + "-selected", "query_index": 0, "query": new}
        live[new] = call
        for role in ROLES:
            job = cid + "-" + role
            jobs[job] = {_digest(requests[job] + "\0" + str(i)):
                         {"query": p["query"], "provider_call_id": live.get(p["query"])}
                         for i, p in enumerate(plans[cid][role])}
    if len(allowed) != CALL_CAP:
        raise ValueError("Paired canonical provider identities must total twenty-two")
    return config, allowed, jobs


def collect(base, root):
    """Return compatible expense rows/hashes, with unknown credits kept unknown."""
    base, root = Path(base), Path(root).resolve()
    if not base.resolve().is_relative_to(root):
        raise ValueError("Accounting base must remain inside its declared root")
    rows, hashes = [], {}
    for name in SERIES:
        directory = base / name
        if not directory.exists():
            continue
        run_path = directory / "run.json"
        run = _read(run_path, directory, root, hashes)
        config, allowed, jobs = manifest_identity(run, name)
        manifest_sha = hashes[run_path.resolve().relative_to(root).as_posix()]
        paths = sorted(directory.glob("*-search.json"))
        if len(paths) > CALL_CAP:
            raise ValueError("More than twenty-two paired provider markers")
        records = {}
        for path in paths:
            match = re.fullmatch(r"([a-f0-9]{64})-search\.json", path.name)
            if not match or match[1] not in allowed:
                raise ValueError("Unbound paired provider marker")
            call, expected = match[1], allowed[match[1]]
            r = _read(path, directory, root, hashes)
            if (not isinstance(r, dict) or r.get("request_id") != call
                    or any(r.get(k) != v for k, v in expected.items())
                    or type(r.get("query_index")) is not int or r.get("search_depth") != "basic"
                    or r.get("state") not in {"submitted", "complete", "failed_or_unknown"}):
                raise ValueError("Provider marker differs from its frozen paired call")
            records[call] = r
        saved, submitted, reported, expected_ids, issues, event_kinds = [], [], 0, set(), [], {}
        by_job = {}
        for job, mapping in jobs.items():
            cid, role = job.rsplit("-", 1)
            marker = directory / (job + "-request.json")
            if marker.exists():
                request = _read(marker, directory, root, hashes)
                if (request.get("request_id") != run["request_ids"][job]
                        or request.get("text_sha256") != config["input_sha256"][cid]
                        or request.get("planned_queries") != 6):
                    raise ValueError("Saved paired submission identity changed")
                submitted.append(job)
            path = directory / (job + ".json")
            if not path.exists():
                if marker.exists():
                    issues.append({"job": job, "code": "submitted_report_missing"})
                continue
            r = _read(path, directory, root, hashes)
            web = r.get("web") if isinstance(r, dict) else None
            n = web.get("queries_attempted") if isinstance(web, dict) else None
            plan = config["plans"][cid][role]
            if (not isinstance(web, dict) or type(n) is not int or not 0 <= n <= 6
                    or r.get("id") != report_identifier(run["request_ids"][job])
                    or not isinstance(r.get("text"), str) or _digest(r["text"]) != config["input_sha256"][cid]
                    or web.get("query_strategy") != VERSION + ":" + role
                    or web.get("experimental_query_plan") != plan
                    or web.get("query_spans") != [{k: p[k] for k in ("start", "end")} for p in plan]
                    or web.get("source_code_sha256") != config.get("code_sha256")
                    or web.get("experiment_manifest_sha256") != manifest_sha):
                raise ValueError("Saved paired report does not match its frozen input/plan/code")
            prefix = dict(list(mapping.items())[:n])
            expected_ids.update(v["provider_call_id"] for v in prefix.values() if v["provider_call_id"])
            events = web.get("actual_query_events", [])
            valid = isinstance(events, list) and len(events) == n
            seen = set()
            if isinstance(events, list):
                for event in events:
                    event_id = event.get("query_call_id") if isinstance(event, dict) else None
                    item = prefix.get(event_id) if isinstance(event_id, str) else None
                    if (not item or event.get("query_call_id") in seen or event.get("query") != item["query"]
                            or event.get("provider_call_id") != item["provider_call_id"]):
                        valid = False
                        continue
                    seen.add(event["query_call_id"])
                    kind = event.get("kind")
                    if kind not in ({"new_provider_call", "shared_fresh_reply"} if item["provider_call_id"] else {"saved_prior_D30"}):
                        valid = False
                    actual = records.get(item["provider_call_id"])
                    if actual and event.get("state") != actual["state"]:
                        valid = False
                    if isinstance(kind, str):
                        event_kinds[kind] = event_kinds.get(kind, 0) + 1
            valid = valid and seen == set(prefix)
            if not valid:
                issues.append({"job": job, "code": "logical_query_mapping_incomplete"})
            by_job[job] = {"logical_queries": n, "mapping_complete": valid}
            saved.append(job)
            reported += n
        missing = expected_ids - records.keys()
        if missing:
            issues.append({"code": "provider_markers_missing", "call_ids": sorted(missing)})
        unclaimed = records.keys() - expected_ids
        if unclaimed:
            issues.append({"code": "provider_markers_without_reported_attempt", "call_ids": sorted(unclaimed)})
        usage = [_usage(r) for r in records.values()]
        known = [u for u in usage if u is not None]
        complete = not issues
        credit_complete = complete and len(known) == len(records)
        quotas = {}
        for filename in ("quota-before.json", "quota-after.json", *(job + "-quota-before.json" for job in jobs)):
            path = directory / filename
            if path.exists():
                quotas[filename] = _quota_observation(_read(path, directory, root, hashes))
        rows.append({"series": name, "saved_documents": len(saved), "saved_job_ids": saved,
            "planned_documents": 8, "diagnostic_unique_cases": 4, "planned_queries": CALL_CAP,
            "query_attempts": len(records), "queries_in_completed_reports": reported,
            "minimum_attempts_consistent_with_reports": len(records) + len(missing),
            "provider_records_complete": sum(r["state"] == "complete" for r in records.values()),
            "provider_records_failed_or_unknown": sum(r["state"] != "complete" for r in records.values()),
            "provider_records_submitted": sum(r["state"] == "submitted" for r in records.values()),
            "submitted_documents": len(submitted), "accounting_by_job": by_job, "query_events_by_kind": event_kinds,
            "provider_marker_records_missing": len(missing), "provider_accounting_complete": complete,
            "accounting_issues": issues, "confirmed_provider_credits": sum(known),
            "records_with_credit_usage": len(known), "records_without_credit_usage": len(records) - len(known),
            "provider_usage_unknown_calls": len(records) - len(known) + len(missing),
            "provider_credit_usage_complete": credit_complete,
            "total_basic_credits": sum(known) if credit_complete else None, "quota_observations": quotas,
            "note": "48 logical queries can reuse 22 actual provider calls. Only durable canonical markers count as new attempts; shared replies and old D30 replies are not charged twice. Missing markers/usage leave total credits unknown."})
    return rows, hashes
