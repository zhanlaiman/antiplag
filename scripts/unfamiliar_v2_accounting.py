"""Read-only expenses from the one frozen eight-document first-use V2 run.

Only its run, hash-bound manifest/local plan, reports, provider markers and
saved quota observations are eligible. No source reservations, replanning,
matching, Store access, provider or quota calls are performed.
"""
from hashlib import sha256
from pathlib import Path
import re

from plagiarism.saas import report_identifier
from scripts.metadata_pilot_accounting import _read, _quota_observation
from scripts import run_unfamiliar_query_v2 as pilot

SERIES = ("query-unfamiliar-first-use-v2",)
HEX64 = re.compile(r"[a-f0-9]{64}")


def _identity(run, manifest, local):
    config = run.get("configuration", {})
    choice, choice_sha = config.get("choice", {}), config.get("choice_sha256")
    if (run.get("first_use") is not True or run.get("used_for_tuning") is not False
            or run.get("final_materials_read") is not False
            or not isinstance(choice_sha, str) or not HEX64.fullmatch(choice_sha)
            or run.get("id") != sha256((pilot.PLAN_SHA + choice_sha).encode()).hexdigest()[:32]
            or choice.get("frozen") is not True
            or not isinstance(choice.get("git_head"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", choice["git_head"])
            or choice.get("plan_sha256") != pilot.PLAN_SHA
            or choice.get("amendment_sha256") != pilot.AMENDMENT_SHA
            or choice.get("maximum_total_fresh_queries") != pilot.MAX_QUERIES
            or choice.get("maximum_queries_per_document") != pilot.MAX_PER_DOCUMENT
            or manifest.get("plan_sha256") != pilot.PLAN_SHA
            or manifest.get("amendment_sha256") != pilot.AMENDMENT_SHA
            or local.get("first_use") is not True or local.get("used_for_tuning") is not False
            or local.get("live_search_queries") != 0):
        raise ValueError("Wrong frozen first-use V2 accounting scope")
    pilot.zero_fallback(choice)
    cases, rows, texts = manifest.get("cases", []), local.get("cases", []), manifest.get("input_texts", {})
    if (tuple(c.get("id") for c in cases) != pilot.IDS
            or tuple(r.get("id") for r in rows) != pilot.IDS or set(texts) != set(pilot.IDS)
            or any(not isinstance(texts[c["id"]], str)
                   or sha256(texts[c["id"]].encode()).hexdigest() != c.get("text_sha256") for c in cases)):
        raise ValueError("First-use V2 input identity changed")
    plans = {r["id"]: r["plans"]["selected"] for r in rows}
    return choice, cases, plans, pilot.frozen_calls(run, cases, plans)


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
        values = {}
        for filename, field in (("manifest.json", "manifest_sha256"), ("local-first-use.json", "local_sha256")):
            path = directory / filename
            values[filename] = _read(path, directory, root, hashes)
            if hashes[path.resolve().relative_to(root).as_posix()] != run.get(field):
                raise ValueError("Frozen first-use V2 artifact hash changed")
        choice, cases, plans, allowed = _identity(run, values["manifest.json"], values["local-first-use.json"])
        # Hash and boundary-check exactly the same marker set account_calls reads.
        for path in sorted(directory.glob("*-search.json")):
            identifier = path.name.removesuffix("-search.json")
            if identifier not in allowed:
                raise ValueError("Unbound first-use V2 provider marker")
            _read(path, directory, root, hashes)
        reports = []
        for case in cases:
            path = directory / (case["id"] + ".json")
            if not path.exists():
                continue
            report = _read(path, directory, root, hashes)
            pilot.validate_report_variant(report, choice)
            attempted = report["web"].get("queries_attempted")
            if (type(attempted) is not int or not 0 <= attempted <= len(plans[case["id"]])
                    or report.get("id") != report_identifier(pilot.request_id(run, case))
                    or not isinstance(report.get("text"), str)
                    or sha256(report["text"].encode()).hexdigest() != case["text_sha256"]
                    or report["web"].get("query_spans") != [
                        {k: p[k] for k in ("start", "end")} for p in plans[case["id"]]]):
                raise ValueError("Saved report is not bound to its frozen first-use V2 input and plan")
            reports.append({"id": case["id"], "status": "report",
                            "queries_attempted": attempted})
        accounting = pilot.account_calls(directory, run, cases, plans, reports)
        summary = accounting["summary"]
        observations = {}
        for filename in ("quota-after.json", *(cid + "-quota-before.json" for cid in pilot.IDS)):
            path = directory / filename
            if path.exists():
                observations[filename] = _quota_observation(_read(path, directory, root, hashes))
        rows.append({"series": name, "saved_documents": len(reports),
            "planned_documents": len(cases), "planned_queries": len(allowed),
            "query_attempts": summary["provider_attempts"],
            "provider_records_complete": summary["provider_complete"],
            "provider_records_failed_or_unknown": summary["provider_attempts"] - summary["provider_complete"],
            "queries_in_completed_reports": summary["queries_in_completed_reports"],
            "minimum_attempts_consistent_with_reports": summary["minimum_provider_attempts_consistent_with_reports_and_ids"],
            "provider_marker_records_missing": summary["provider_marker_records_missing"],
            "provider_accounting_complete": summary["provider_accounting_complete"],
            "confirmed_provider_credits": summary["known_basic_credits"],
            "records_with_credit_usage": summary["provider_records_with_credit_usage"],
            "records_without_credit_usage": summary["provider_records_without_credit_usage"],
            "provider_usage_unknown_calls": summary["provider_usage_unknown_calls"],
            "provider_credit_usage_complete": summary["provider_credit_usage_complete"],
            "total_basic_credits": summary["total_basic_credits"],
            "accounting_by_case": accounting["cases"], "accounting_issues": accounting["issues"],
            "quota_observations": observations,
            "note": "First-use V2 only; no source or score recomputation. Durable markers are attempts; explicit usage is the confirmed credit subtotal. Missing markers or usage leave total credits unknown."})
    return rows, hashes
