"""Read-only expenses for the one explicitly authorized six-call format pilot.

No preparation, corpus, source-page, credential, provider or quota reads exist.
Missing markers stay missing; completed reports cannot turn them into zero cost.
"""
from hashlib import sha256
import json
from pathlib import Path
import re

from plagiarism.saas import report_identifier
from scripts.metadata_pilot_accounting import _read, _quota_observation, _usage
from scripts.run_query_format_pairs import SPEC, FORMATS, VERSION, CALL_CAP, call_map, remove_outer_quotes

SERIES = ("dev-query-format-pair-v1",)
HEX64 = re.compile(r"[a-f0-9]{64}")


def manifest_identity(run, name):
    config = run.get("configuration")
    if (not isinstance(config, dict) or run.get("run_name") != name or config.get("version") != VERSION
            or config.get("spec") != [list(s) for s in SPEC] or config.get("formats") != list(FORMATS)
            or config.get("max_provider_calls") != CALL_CAP or config.get("max_calls_per_job") != 1
            or config.get("named_references") is not False or config.get("default_changed") is not False):
        raise ValueError("Wrong frozen six-call format-pilot scope")
    original, jobs = config.get("original_dense_plans"), config.get("jobs")
    if not isinstance(original, dict) or set(original) != {cid for cid, _ in SPEC} or not isinstance(jobs, dict):
        raise ValueError("Missing frozen format-pilot plans")
    for cid, index in SPEC:
        plans = original[cid]
        if not isinstance(plans, list) or len(plans) != 6:
            raise ValueError("Original DENSE plan is not the frozen six-query plan")
        unquoted = remove_outer_quotes(plans)
        pair = [jobs.get(cid + "-" + fmt) for fmt in FORMATS]
        for fmt, job, expected in zip(FORMATS, pair, (plans[index], unquoted[index])):
            if (not isinstance(job, dict) or job.get("case_id") != cid or job.get("format") != fmt
                    or job.get("base_query_index") != index or job.get("plan") != [expected]
                    or not isinstance(job.get("text_sha256"), str) or not HEX64.fullmatch(job["text_sha256"])):
                raise ValueError("Format job changed words, spans, index or input identity")
        if pair[0]["text_sha256"] != pair[1]["text_sha256"]:
            raise ValueError("Paired format jobs do not use the same input")
    fingerprint = sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    expected_run = sha256((name + fingerprint).encode()).hexdigest()[:32]
    identifiers = {key: sha256((expected_run + key + job["text_sha256"]).encode()).hexdigest()[:32] for key, job in jobs.items()}
    if run.get("id") != expected_run or run.get("request_ids") != identifiers:
        raise ValueError("Format-pilot stable request identity changed")
    return config, call_map(run)


def collect(base, root):
    base, root = Path(base), Path(root).resolve()
    if not base.resolve().is_relative_to(root):
        raise ValueError("Accounting base must remain inside its declared root")
    rows, hashes = [], {}
    for name in SERIES:
        directory = base / name
        if not directory.exists():
            continue
        run = _read(directory / "run.json", directory, root, hashes)
        config, allowed = manifest_identity(run, name)
        paths = sorted(directory.glob("*-search.json"))
        if len(paths) > CALL_CAP:
            raise ValueError("More than six provider-call markers")
        records, by_job = [], {}
        for path in paths:
            filename = re.fullmatch(r"([a-f0-9]{64})-search\.json", path.name)
            if not filename or filename[1] not in allowed:
                raise ValueError("Unbound format-pilot call artifact")
            identifier, expected = filename[1], allowed[filename[1]]
            record = _read(path, directory, root, hashes)
            if (not isinstance(record, dict) or record.get("request_id") != identifier
                    or any(record.get(k) != v for k, v in expected.items())
                    or type(record.get("query_index")) is not int or record.get("search_depth") != "basic"
                    or record.get("state") not in {"submitted", "complete", "failed_or_unknown"}):
                raise ValueError("Provider marker differs from the frozen format job")
            records.append(record)
            by_job[expected["case_id"]] = record
        saved, reported, missing, mismatched = [], 0, 0, []
        for key, job in config["jobs"].items():
            path = directory / (key + ".json")
            if not path.exists():
                continue
            report = _read(path, directory, root, hashes)
            web = report.get("web") if isinstance(report, dict) else None
            attempted = web.get("queries_attempted") if isinstance(web, dict) else None
            if (not isinstance(web, dict) or type(attempted) is not int or not 0 <= attempted <= 1
                    or report.get("id") != report_identifier(run["request_ids"][key])
                    or not isinstance(report.get("text"), str) or sha256(report["text"].encode()).hexdigest() != job["text_sha256"]
                    or web.get("query_strategy") != VERSION + ":" + job["format"]
                    or web.get("diagnostic_query_plan") != job
                    or web.get("query_spans") != [{k:p[k] for k in ("start", "end")} for p in job["plan"]]):
                raise ValueError("Saved report is not bound to this diagnostic job")
            actual = int(key in by_job)
            if attempted != actual:
                missing += max(0, attempted - actual)
                mismatched.append({"job": key, "reported_queries": attempted, "actual_markers": actual})
            saved.append(key)
            reported += attempted
        usage = [_usage(record) for record in records]
        known = [credits for credits in usage if credits is not None]
        accounting_complete = not mismatched
        usage_complete = accounting_complete and len(known) == len(records)
        observations = {}
        names = ["quota-before-reviewed.json", "quota-after.json"] + [key + "-quota-before.json" for key in config["jobs"]]
        for filename in names:
            path = directory / filename
            if path.exists():
                observations[filename] = _quota_observation(_read(path, directory, root, hashes))
        rows.append({"series": name, "saved_documents": len(saved), "saved_job_ids": saved,
            "planned_documents": 6, "diagnostic_unique_cases": 3, "planned_queries": 6,
            "queries_in_completed_reports": reported, "query_attempts": len(records),
            "minimum_attempts_consistent_with_reports": len(records) + missing,
            "provider_records_complete": sum(r["state"] == "complete" for r in records),
            "provider_records_failed_or_unknown": sum(r["state"] != "complete" for r in records),
            "submitted_documents": len(by_job), "attempts_by_job": {key:int(key in by_job) for key in config["jobs"]},
            "provider_marker_records_missing": missing, "provider_accounting_complete": accounting_complete,
            "accounting_mismatches": mismatched, "confirmed_provider_credits": sum(known),
            "records_with_credit_usage": len(known), "records_without_credit_usage": len(records) - len(known),
            "provider_usage_unknown_calls": len(records) - len(known) + missing,
            "provider_credit_usage_complete": usage_complete, "total_basic_credits": sum(known) if usage_complete else None,
            "quota_observations": observations,
            "note": "Six report jobs reuse three DEV inputs. Raw attempts count durable markers; confirmed credits are explicit usage only. Missing markers/usage are unknown, never zero total spend."})
    return rows, hashes
