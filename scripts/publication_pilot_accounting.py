"""Historical V3 expenses, separated into paid Tavily and anonymous Wiki rows.

Reads one run's frozen artifacts only. No current-HEAD/code checks, reserved
publication files, score recomputation, provider construction or network calls.
"""
import json
from pathlib import Path
import re

from scripts.metadata_pilot_accounting import _read, _quota_observation
from scripts.publication_first_use_accounting import reconcile
from scripts import run_publication_first_use as pilot

SERIES = "publication-first-use-v3"
HEX64 = re.compile(r"[a-f0-9]{64}")


def identity(run, manifest, local):
    config = run.get("configuration", {})
    choice = config.get("choice", {})
    canonical = pilot.digest(json.dumps(choice, ensure_ascii=False, sort_keys=True, allow_nan=False))
    required = {"frozen": True, "plan_sha256": pilot.PLAN_SHA, "amendment_sha256": pilot.AMENDMENT_SHA,
        "wiki_budget_amendment_sha256": pilot.WIKI_AMENDMENT_SHA, "actual_case_ids": list(pilot.IDS),
        "discovery_strategy": pilot.DISCOVERY, "wiki_followup": True, "maximum_total_fresh_queries": 40,
        "maximum_queries_per_document": 8, "maximum_fresh_tavily_queries": 30,
        "maximum_fresh_mediawiki_queries": 10, "maximum_pages_per_document": 26}
    codes = config.get("code_sha256")
    if (run.get("first_use") is not True or run.get("used_for_tuning") is not False
            or any(choice.get(k) != v for k, v in required.items())
            or type(choice.get("wiki_followup")) is not bool or type(choice.get("named_references")) is not bool
            or type(choice.get("zero_fallback")) is not bool
            or not re.fullmatch(r"[a-f0-9]{40}", choice.get("git_head", ""))
            or any(not isinstance(choice.get(k), str) or not choice[k] for k in ("matcher_version", "html_extraction_version", "frozen_at"))
            or not HEX64.fullmatch(config.get("choice_sha256", "")) or config.get("choice_canonical_sha256") != canonical
            or not isinstance(codes, dict) or not codes or codes != choice.get("code_sha256")
            or any(not isinstance(value, str) or not HEX64.fullmatch(value) for value in codes.values())
            or run.get("id") != pilot.digest(pilot.PLAN_SHA + pilot.AMENDMENT_SHA + pilot.WIKI_AMENDMENT_SHA
                + config["choice_sha256"] + canonical)[:32]):
        raise ValueError("Wrong frozen publication first-use identity or complete choice")
    cases, plans, secondary = manifest.get("cases", []), local.get("plans", {}), local.get("wiki_plans", {})
    texts = manifest.get("input_texts", {})
    if (tuple(c.get("id") for c in cases) != pilot.IDS or set(texts) != set(pilot.IDS)
            or manifest.get("preparation_failed_ids") != list(pilot.FAILED_IDS)
            or manifest.get("plan_sha256") != pilot.PLAN_SHA or manifest.get("amendment_sha256") != pilot.AMENDMENT_SHA
            or local.get("first_use") is not True or local.get("used_for_tuning") is not False
            or set(plans) != set(pilot.IDS) or set(secondary) != set(pilot.IDS)
            or any(not isinstance(texts[c["id"]], str) or pilot.digest(texts[c["id"]]) != c.get("text_sha256") for c in cases)):
        raise ValueError("Publication five-case input/plan scope changed")
    for cid in pilot.IDS:
        for query in secondary[cid]:
            index = query.get("primary_query_index")
            if (type(index) is not int or not 0 <= index < len(plans[cid])
                    or query.get("end") != plans[cid][index].get("end")
                    or not plans[cid][index]["start"] <= query.get("start", -1) < query["end"]):
                raise ValueError("Secondary slot lost its frozen primary span binding")
    primary = pilot.frozen_calls(run, manifest, plans)
    wiki = pilot.frozen_wiki_calls(run, manifest, secondary)
    allowed = {**{call: {**item, "provider": "tavily"} for call, item in primary.items()},
               **{call: {**item, "provider": "mediawiki"} for call, item in wiki.items()}}
    sizes = {cid: {"tavily": len(plans[cid]), "mediawiki": len(secondary[cid])} for cid in pilot.IDS}
    return primary, wiki, allowed, sizes


def collect(base, root):
    base, root = Path(base), Path(root).resolve()
    if not base.resolve().is_relative_to(root):
        raise ValueError("Publication accounting base leaves declared root")
    directory = base / SERIES
    if not directory.exists():
        return [], [], {}
    hashes = {}
    read = lambda path: _read(path, directory, root, hashes)
    run = read(directory / "run.json")
    values = {}
    for name, field in (("manifest.json", "manifest_sha256"), ("local-first-use.json", "local_sha256")):
        path = directory / name
        values[name] = read(path)
        if hashes[path.resolve().relative_to(root).as_posix()] != run.get(field):
            raise ValueError("Publication frozen artifact SHA256 changed")
    manifest, local = values["manifest.json"], values["local-first-use.json"]
    primary, wiki, allowed, sizes = identity(run, manifest, local)
    records = {}
    for suffix in ("search", "mediawiki", "tavily-intent", "mediawiki-intent"):
        for path in sorted(directory.glob("*-" + suffix + ".json")):
            call = path.name.removesuffix("-" + suffix + ".json")
            if call not in allowed:
                raise ValueError("Unbound publication accounting artifact")
            value = read(path)
            if suffix in ("search", "mediawiki"):
                expected_provider = "tavily" if suffix == "search" else "mediawiki"
                if value.get("provider", expected_provider) != expected_provider:
                    raise ValueError("Publication marker declares another provider kind")
                records[call] = value
    reports, observations = {}, {}
    run_sha = hashes[(directory / "run.json").resolve().relative_to(root).as_posix()]
    for case in manifest["cases"]:
        cid, rid = case["id"], pilot.request_id(run, case)
        path = directory / (cid + ".json")
        if path.exists():
            reports[cid] = pilot.validate_report(read(path), run, case, local)
        for suffix in ("-request.json", "-submission-intent.json", "-error.json"):
            path = directory / (cid + suffix)
            if not path.exists():
                continue
            value = read(path)
            if (value.get("request_id") != rid
                    or (suffix != "-error.json" and value.get("text_sha256") != case["text_sha256"])
                    or (suffix == "-request.json" and value.get("planned_queries") != sum(sizes[cid].values()))
                    or (suffix == "-submission-intent.json" and value.get("run_sha256") != run_sha)):
                raise ValueError("Publication request intent is not bound to its frozen run/input")
    for name in ("quota-after.json", *(cid + "-quota-before.json" for cid in pilot.IDS)):
        path = directory / name
        if path.exists():
            observations[name] = _quota_observation(read(path))
    state = reconcile(directory, allowed, sizes, reports)
    paid = {c: r for c, r in records.items() if c in primary}
    free = {c: r for c, r in records.items() if c in wiki}
    missing = set(state["missing_provider_call_ids"])
    known = sum(r.get("usage") is not None for r in paid.values())
    row = {"series": SERIES, "provider": "tavily", "saved_documents": len(reports), "planned_documents": len(sizes),
        "preparation_failed_case_ids": list(pilot.FAILED_IDS), "planned_queries": len(primary), "query_attempts": len(paid),
        "provider_records_complete": sum(r["state"] == "complete" for r in paid.values()),
        "provider_records_failed_or_unknown": sum(r["state"] != "complete" for r in paid.values()),
        "queries_in_completed_reports": sum(v["tavily"] for v in reports.values()),
        "minimum_attempts_consistent_with_reports": len(paid) + len(missing & primary.keys()),
        "provider_marker_records_missing": len(missing & primary.keys()), "confirmed_provider_credits": state["confirmed_credit_subtotal"],
        "records_with_credit_usage": known, "records_without_credit_usage": len(paid) - known,
        "provider_usage_unknown_calls": len(state["unknown_usage_call_ids"]) + len(missing & primary.keys()),
        "provider_accounting_complete": not state["issues"], "provider_credit_usage_complete": state["total_basic_credits"] is not None,
        "total_basic_credits": state["total_basic_credits"], "accounting_issues": state["issues"], "quota_observations": observations}
    anonymous = {"series": SERIES + "-mediawiki", "provider": "mediawiki", "credit_API": False,
        "saved_documents": len(reports), "planned_anonymous_queries": len(wiki), "anonymous_provider_intents": len(free),
        "provider_records_complete": sum(r["state"] == "complete" for r in free.values()),
        "queries_in_completed_reports": sum(v["mediawiki"] for v in reports.values()),
        "minimum_attempts_consistent_with_reports": len(free) + len(missing & wiki.keys()),
        "provider_marker_records_missing": len(missing & wiki.keys()),
        "confirmed_new_anonymous_queries": state["confirmed_public_queries_subtotal"],
        "unknown_usage_call_ids": state["unknown_public_usage_call_ids"], "total_new_anonymous_queries": state["total_new_public_queries"],
        "money_spent_kzt": 0, "new_Tavily_calls": 0,
        "accounting_complete": not state["issues"] and state["total_new_public_queries"] is not None}
    return [row], [anonymous], hashes
