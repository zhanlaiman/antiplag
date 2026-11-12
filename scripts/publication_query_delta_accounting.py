"""Pure metadata accounting for the fixed P3-06 one-Basic delta.

Reads only own manifests, three operational receipt names and saved quota
observations. It never loads inputs/pages or decodes raw-response bodies, private code,
current code/HEAD, a database, credentials, providers or a CLI. Cached historical
bindings are validated as metadata and are never charged again.
"""
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
import re

SERIES = ("publication-p3-06-query-delta-v1", "publication-p3-06-query-delta-v2")
CASE_ID = "P3-06"
CONTEXT_COMMIT = "3c75c445917ecbdda8085e6cfa9c9e0d04a69a44"
QUERY_MODULE_SHA = "155c3558534033d17001f2e90d9249f5ca02a20daf0da909cbd3219330ab54e1"
STRATEGY = "hybrid-citation-v5-paragraph-redundancy"
CAPS = {"new_included_free_Tavily_Basic": 1, "new_Wiki": 0, "new_source_GET": 0, "max_results": 5}
HEX64 = re.compile(r"[a-f0-9]{64}")


def _hash(value):
    return sha256(value.encode()).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _hex(value):
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate accounting JSON key")
        value[key] = item
    return value


def _read(path, directory, root, hashes):
    relative = path.relative_to(directory)
    cursor = directory
    for part in (None, *relative.parts):
        cursor = cursor if part is None else cursor / part
        if cursor.is_symlink():
            raise ValueError("Accounting never follows artifact symlinks")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(directory.resolve()) or not resolved.is_relative_to(root):
        raise ValueError("Accounting receipt leaves its fixed series/root")
    raw = path.read_bytes()
    hashes[resolved.relative_to(root).as_posix()] = sha256(raw).hexdigest()
    def invalid_constant(value):
        raise ValueError("Non-finite accounting JSON")
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=invalid_constant)


def _query(item):
    if not isinstance(item, dict):
        raise ValueError("Invalid frozen primary query")
    q, start, end = (item.get(k) for k in ("query", "start", "end"))
    if (not isinstance(q, str) or not 1 <= len(q) <= 400 or any(ord(c) < 32 for c in q)
            or type(start) is not int or type(end) is not int or not 0 <= start < end <= 200_000):
        raise ValueError("Invalid frozen query/span")
    return q


def manifest_identity(run, name):
    c = run.get("configuration") if isinstance(run, dict) else None
    if (name not in SERIES or not isinstance(c, dict) or c.get("case_id") != CASE_ID
            or type(c.get("primary_query_index")) is not int or c["primary_query_index"] != 5
            or c.get("context_commit") != CONTEXT_COMMIT or c.get("query_module_sha256") != QUERY_MODULE_SHA
            or c.get("strategy") != STRATEGY or c.get("first_use") is not False
            or c.get("evaluation_only") is not True or c.get("used_for_tuning") is not False
            or c.get("caps") != CAPS or any(type(c["caps"].get(k)) is not int for k in CAPS)
            or type(c.get("cached_logical_primary")) is not int or c["cached_logical_primary"] != 5
            or type(c.get("cached_logical_Wiki")) is not int or c["cached_logical_Wiki"] != 2):
        raise ValueError("Wrong fixed one-included-FREE-Basic scope")
    for key in ("input_sha256", "historical_run_sha256", "historical_manifest_sha256", "historical_local_sha256", "parent_plans_sha256"):
        if not _hex(c.get(key)):
            raise ValueError("Malformed frozen metadata hash")
    codes = c.get("code_sha256")
    if (not isinstance(codes, dict) or not codes
            or any(not isinstance(k, str) or not k or not _hex(v) for k, v in codes.items())):
        raise ValueError("Missing frozen code identity metadata")
    primary, wiki = c.get("primary_plan"), c.get("Wiki_plan")
    if not isinstance(primary, list) or len(primary) != 6 or not isinstance(wiki, list) or len(wiki) != 2:
        raise ValueError("Expected six primary and two cached Wiki slots")
    if len({_query(q) for q in primary}) != 6:
        raise ValueError("Duplicate primary phrases")
    if c.get("query") != primary[5]:
        raise ValueError("New query must be the sole declared primary slot five")
    for q in wiki:
        _query(q)
        index = q.get("primary_query_index")
        if type(index) is not int or not 0 <= index < 6:
            raise ValueError("Malformed cached Wiki slot")
    bindings = c.get("cached_bindings")
    if not isinstance(bindings, list) or len(bindings) != 7:
        raise ValueError("Exactly five old primary and two old Wiki bindings required")
    seen = set()
    for b in bindings:
        if not isinstance(b, dict) or b.get("provider") not in ("tavily", "mediawiki"):
            raise ValueError("Malformed cached provider binding")
        provider, index = b["provider"], b.get("query_index")
        if type(index) is not int or index not in (range(5) if provider == "tavily" else range(2)):
            raise ValueError("Wrong historical cached query index")
        expected = primary[index] if provider == "tavily" else wiki[index]
        path = b.get("path")
        suffix = "search" if provider == "tavily" else "mediawiki"
        if (b.get("case_id") != CASE_ID or b.get("query") != expected["query"]
                or not _hex(b.get("sha256")) or not _hex(b.get("provider_call_id"))
                or not isinstance(path, str) or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                or not path.startswith("data/private/web-evaluation/publication-first-use-v3/")
                or PurePosixPath(path).name != b["provider_call_id"] + "-" + suffix + ".json"
                or (provider == "mediawiki" and (type(b.get("primary_query_index")) is not int
                    or b["primary_query_index"] != expected["primary_query_index"]))):
            raise ValueError("Cached binding differs from its frozen query/case/hash")
        if (provider, index) in seen:
            raise ValueError("Duplicate cached slot")
        seen.add((provider, index))
    if seen != {*(('tavily', i) for i in range(5)), *(('mediawiki', i) for i in range(2))}:
        raise ValueError("Incomplete historical slot bindings")
    identity = _hash(_canonical(c))
    call = _hash(identity + "\0P3-06\0primary-delta\0" + "5")
    if run.get("id") != identity or run.get("call_id") != call:
        raise ValueError("Canonical delta run/call identity changed")
    return c, call


def _usage(record):
    value = record.get("usage")
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get("credits")) is not int or not 0 <= value["credits"] <= 1:
        raise ValueError("Malformed explicit Basic credit usage")
    return value["credits"]


def _quota(value):
    if not isinstance(value, dict):
        raise ValueError("Malformed quota metadata")
    keys = ("plan_usage", "plan_limit", "paygo_usage", "paygo_limit", "local_monthly_attempts")
    for key in keys:
        number = value.get(key)
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or number < 0):
            raise ValueError("Malformed quota counter")
    return {k: value[k] for k in ("current_plan", *keys, "checked_at", "unavailable", "new_searches_allowed") if k in value}


def collect(base, root):
    """Return standard expense rows and receipt hashes without other I/O."""
    base, root = Path(base), Path(root).resolve()
    if not base.resolve().is_relative_to(root):
        raise ValueError("Accounting base leaves root")
    rows, hashes = [], {}
    for name in SERIES:
        directory = base / name
        if not directory.exists():
            continue
        read = lambda path: _read(path, directory, root, hashes)
        run_path = directory / "run.json"
        c, call = manifest_identity(read(run_path), name)
        run_sha = hashes[run_path.resolve().relative_to(root).as_posix()]
        for pattern in ("*-mediawiki.json", "source-http-*.json", "page-*-fetch.json", "*-provider-intent.json"):
            if list(directory.glob(pattern)):
                raise ValueError("Unexpected Wiki/page/additional provider operation")
        paths = sorted(directory.glob("*-search.json"))
        if len(paths) > 1 or any(p.name != call + "-search.json" for p in paths):
            raise ValueError("Extra or unbound Basic provider record")
        start_path, intent_path = directory / "start-intent.json", directory / "provider-intent.json"
        start = read(start_path) if start_path.exists() else None
        intent = read(intent_path) if intent_path.exists() else None
        if start is not None and (not isinstance(start, dict) or start.get("run_sha256") != run_sha or start.get("call_id") != call):
            raise ValueError("Unbound delta start intent")
        expected = {"request_id": call, "case_id": CASE_ID, "query_index": 0, "query": c["query"]["query"]}
        if intent is not None and (not isinstance(intent, dict) or any(intent.get(k) != v for k, v in expected.items())
                or type(intent.get("query_index")) is not int or type(intent.get("primary_query_index")) is not int
                or intent["primary_query_index"] != 5 or intent.get("state") != "submitted"):
            raise ValueError("Unbound submitted provider intent")
        record = read(paths[0]) if paths else None
        if record is not None and (not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items())
                or type(record.get("query_index")) is not int or record.get("search_depth") != "basic"
                or record.get("provider", "tavily") != "tavily"
                or record.get("state") not in {"submitted", "complete", "failed_or_unknown"}):
            raise ValueError("Receipt differs from the frozen one-Basic call")
        issues = []
        expected_attempt = start is not None or intent is not None or record is not None
        missing = int(expected_attempt and record is None)
        if missing:
            issues.append({"code": "provider_record_missing", "call_id": call})
        if record is not None and (start is None or intent is None):
            issues.append({"code": "durable_intent_missing", "call_id": call})
        if record is not None and record["state"] == "submitted":
            issues.append({"code": "provider_outcome_submitted", "call_id": call})
        usage = _usage(record) if record is not None else None
        unknown_usage = int(record is not None and usage is None) + missing
        complete = not issues
        credit_complete = complete and unknown_usage == 0
        quotas = {}
        for filename in ("quota-before.json", "quota-after.json", "root-quota-before.json"):
            path = directory / filename
            if path.exists():
                quotas[filename] = _quota(read(path))
        receipts = {path: value for path, value in hashes.items() if path.startswith(directory.resolve().relative_to(root).as_posix() + "/")}
        rows.append({"series": name, "planned_documents": 0, "saved_documents": 0,
            "planned_queries": 1, "diagnostic_unique_cases": 1, "query_attempts": int(record is not None),
            "queries_in_completed_reports": 0, "minimum_attempts_consistent_with_reports": int(expected_attempt),
            "provider_records_complete": int(record is not None and record["state"] == "complete"),
            "provider_records_submitted": int(record is not None and record["state"] == "submitted"),
            "provider_records_failed_or_unknown": int(record is not None and record["state"] == "failed_or_unknown"),
            "actual_submitted_provider_intents": int(intent is not None), "start_intents": int(start is not None),
            "unknown_start_without_provider_record": int(start is not None and record is None),
            "provider_marker_records_missing": missing, "provider_usage_unknown_calls": unknown_usage,
            "records_with_credit_usage": int(record is not None and usage is not None),
            "records_without_credit_usage": int(record is not None and usage is None),
            "confirmed_provider_credits": usage if usage is not None else 0,
            "total_basic_credits": (usage if record is not None else 0) if credit_complete else None,
            "provider_accounting_complete": complete, "provider_credit_usage_complete": credit_complete,
            "accounting_issues": issues, "quota_observations": quotas, "receipt_sha256": receipts,
            "cached_primary_slots_not_new_expense": 5, "cached_Wiki_slots_not_new_expense": 2,
            "new_Wiki_queries": 0, "new_page_GETs": 0, "new_calls_prematurely_inferred": False,
            "note": "Included FREE Basic delta only. An unstarted manifest counts zero; start without receipt or missing usage keeps total unknown. Historical five primary/two Wiki bindings are not charged again."})
    return rows, hashes
