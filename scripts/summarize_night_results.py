#!/usr/bin/env python3
"""Deterministic counters from explicit saved DEV/query-first-use artifacts.

No search, fetching, models, Store writes, or held-out corpus access exists.
Replays are private artifacts, never after-report UI links with recycled IDs.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.analyze_web_dev_checks import inspect, compare
from scripts.dev_report_provenance import cohort_configuration, report_provenance
from scripts.metadata_pilot_accounting import collect as metadata_pilot_expenses
from scripts.run_dev_query_probe import dump

BASE = ROOT / "data/private/web-evaluation"
DEV = BASE / "materials/dev-30"
DEV_KEY_SHA = "0070f13da001bb9a80db991c5c2ee200ee695206102b10768083059a004f162f"
INITIAL_PRIORITY = ("dev-live-targeted-v3", "dev-dense-six-live-v1", "dev-dense-balanced-live-v2", "dev-final-control-live-v1")
BATCHES = (
    ("dev-selected-remaining-a-v1", ("D01", "D02", "D06", "D09", "D11", "D12")),
    ("dev-selected-remaining-b-v1", ("D14", "D15", "D17", "D18", "D19", "D21")),
    ("dev-selected-remaining-c-v1", tuple(f"D{i:02d}" for i in range(22, 28))),
    ("dev-selected-remaining-d-v1", ("D28", "D29", "D30")),
)
FINAL_BATCHES = tuple((f"dev-final-selected-{letter}-v1", tuple(f"D{i:02d}" for i in range(start, start + 6)))
                      for letter, start in zip("abcde", (1, 7, 13, 19, 25)))
ALL_BATCHES = BATCHES + FINAL_BATCHES
PRIORITY = INITIAL_PRIORITY + tuple(name for name, _ in ALL_BATCHES)
FINAL_PLANNED_QUERIES = 167
EXPECTED_FRESH = ("D03", "D04", "D05", "D07", "D08", "D10", "D13", "D16", "D20")
EXPENSE_REPORTS = (
    ("initial_dev30", "dev-live-30-v1", tuple(f"D{i:02d}.json" for i in range(1, 31))),
    ("exact_search_rejected", "dev-exact-search-pilot", tuple(f"D{i:02d}.json" for i in (1, 2, 3, 4, 7, 11, 14, 16))),
    ("paragraph_search_rejected", "dev-paragraph-search-pilot", tuple(f"D{i:02d}.json" for i in (21, 25, 29))),
    ("targeted_matcher_v3", "dev-live-targeted-v3", tuple(f"D{i:02d}.json" for i in (4, 7, 10, 13))),
    ("dense_six", "dev-dense-six-live-v1", tuple(f"D{i:02d}.json" for i in (3, 4, 5, 8, 16, 20))),
    ("dense_balanced", "dev-dense-balanced-live-v2", ("D04.json", "D20.json")),
    ("d02_diagnostic_pair", "dev-d02-clause-pair-live-v1", ("original.json", "clause.json")),
    ("query_first_use", "query-unfamiliar-first-use-v1", tuple(f"QH{i:02d}.json" for i in range(1, 9))),
    ("final_controls", "dev-final-control-live-v1", ("D07.json", "D10.json", "D13.json")),
)


def diagnostic_pilot_expenses(base, root):
    # Lazy import avoids a module cycle through the format pilot's shared DEV
    # key constant. Neither collector reads corpus inputs or initializes APIs.
    from scripts.query_format_accounting import collect as query_format_expenses
    metadata_rows, metadata_hashes = metadata_pilot_expenses(base, root)
    format_rows, format_hashes = query_format_expenses(base, root)
    return metadata_rows + format_rows, {**metadata_hashes, **format_hashes}


def source_pair_transitions(before, after):
    left = {r["id"]: r for r in before["cases"] if r["status"] == "saved"}
    right = {r["id"]: r for r in after["cases"] if r["status"] == "saved"}
    common = left.keys() & right.keys()
    result = {}
    for field in ("expected_url_discovered", "expected_url_read"):
        def found(rows):
            return {(cid, e["source_id"]) for cid in common for e in rows[cid]["evidence"] if e[field]}
        a, b = found(left), found(right)
        result[field] = {"gained": [{"id": cid, "source_id": sid} for cid, sid in sorted(b - a)],
                         "lost": [{"id": cid, "source_id": sid} for cid, sid in sorted(a - b)]}
    return result


def batch_progress(directory, allowed_ids, completed):
    """Keep missing, submitted and terminal outcomes separate from reports."""
    location = BASE / directory
    errors = [cid for cid in allowed_ids if cid not in completed and (location / (cid + "-error.json")).exists()]
    running = [cid for cid in allowed_ids if cid not in completed and cid not in errors
               and (location / (cid + "-request.json")).exists()]
    return {"directory": directory, "allowed_ids": list(allowed_ids), "completed_reports": list(completed),
        "terminal_failure_artifacts": errors, "submitted_without_report": running,
        "pending_report_ids": [cid for cid in allowed_ids if cid not in completed and cid not in errors]}


def batch_search_expense(directory, allowed_ids, completed_rows):
    """Count saved attempts, including in-flight outcomes, without API calls."""
    location = BASE / directory
    run_path = location / "run.json"
    run = json.loads(run_path.read_text()) if run_path.exists() else {}
    if run and (run.get("case_ids") != list(allowed_ids) or run.get("input_key_sha256") != DEV_KEY_SHA):
        raise ValueError("Batch manifest differs from its declared DEV scope")
    expected_call_ids, request_count = set(), 0
    for cid in allowed_ids:
        request_path = location / (cid + "-request.json")
        if not request_path.exists():
            continue
        request = json.loads(request_path.read_text())
        identifier, planned = request.get("request_id"), request.get("planned_queries")
        if (not isinstance(identifier, str) or len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier)
                or type(planned) is not int or not 1 <= planned <= 6):
            raise ValueError("Invalid saved batch submission")
        expected_call_ids.update(sha256((identifier + "\0" + str(i)).encode()).hexdigest() for i in range(planned))
        request_count += 1
    records, hashes = [], {}
    for path in sorted(location.glob("*-search.json")):
        raw = path.read_bytes()
        record = json.loads(raw)
        call_id = path.name.removesuffix("-search.json")
        if call_id not in expected_call_ids or record.get("request_id") != call_id:
            raise ValueError("Unbound or repeated search expense record")
        records.append(record)
        hashes[str(path.relative_to(ROOT))] = sha256(raw).hexdigest()
    planned = run.get("planned_queries", {})
    if planned and (set(planned) != set(allowed_ids) or any(type(v) is not int or not 1 <= v <= 6 for v in planned.values())):
        raise ValueError("Invalid declared batch query plan")
    confirmed_credits = 0
    known_usage = 0
    for record in records:
        usage = record.get("usage")
        credits = usage.get("credits") if isinstance(usage, dict) else None
        if type(credits) in (int, float) and credits >= 0:
            confirmed_credits += credits
            known_usage += 1
    return {"series": directory, "saved_documents": len(completed_rows), "planned_documents": len(allowed_ids),
        "queries_in_completed_reports": sum(row["queries"] for row in completed_rows),
        "query_attempts": len(records), "provider_records_complete": sum(r["state"] == "complete" for r in records),
        "provider_records_failed_or_unknown": sum(r["state"] != "complete" for r in records),
        "submitted_documents": request_count, "planned_queries": sum(planned.values()) if planned else None,
        "confirmed_provider_credits": confirmed_credits, "records_with_credit_usage": known_usage,
        "note": "Saved attempts include running jobs; accuracy uses only completed reports. Unknown usage is not assumed free or zero."}, hashes


def final_selected_summary(before=None):
    """DEV-only final stage; never fill missing final reports with older runs."""
    if sha256((DEV / "key.json").read_bytes()).hexdigest() != DEV_KEY_SHA:
        raise ValueError("Changed DEV key")
    before = before or inspect(DEV, BASE / "dev-live-30-v1")
    rows, provenance, states, expenses = [], [], [], []
    hashes = {str((DEV / "key.json").relative_to(ROOT)): DEV_KEY_SHA}
    for row in before["cases"]:
        path = BASE / "dev-live-30-v1" / (row["id"] + ".json")
        if row["status"] == "saved":
            hashes[str(path.relative_to(ROOT))] = sha256(path.read_bytes()).hexdigest()
    for directory, allowed_ids in FINAL_BATCHES:
        bundle = inspect(DEV, BASE / directory)
        completed = [row for row in bundle["cases"] if row["status"] == "saved"]
        if any(row["id"] not in allowed_ids or row["data_kind"] != "fresh_internet_search" for row in completed):
            raise ValueError("Wrong case or replay in the final fresh DEV batch")
        run_path = BASE / directory / "run.json"
        run = json.loads(run_path.read_text()) if run_path.exists() else {}
        if run_path.exists():
            hashes[str(run_path.relative_to(ROOT))] = sha256(run_path.read_bytes()).hexdigest()
        for row in completed:
            path = BASE / directory / (row["id"] + ".json")
            raw_bytes = path.read_bytes()
            raw = json.loads(raw_bytes)
            digest = sha256(raw_bytes).hexdigest()
            hashes[str(path.relative_to(ROOT))] = digest
            provenance.append(report_provenance(raw, run, directory, digest, case_id=row["id"]))
        rows.extend(completed)
        states.append(batch_progress(directory, allowed_ids, [row["id"] for row in completed]))
        expense, expense_hashes = batch_search_expense(directory, allowed_ids, completed)
        expenses.append(expense)
        hashes.update(expense_hashes)
    planned = sum(e["planned_queries"] or 0 for e in expenses)
    attempts = sum(e["query_attempts"] for e in expenses)
    if attempts > FINAL_PLANNED_QUERIES or planned > FINAL_PLANNED_QUERIES:
        raise ValueError("Final selected DEV30 exceeded its explicit167-query ceiling")
    configuration = cohort_configuration(provenance, [f"D{i:02d}" for i in range(1, 31)])
    result = compare(before, {"cases": rows})
    previous_selected, previous_paths = {}, {}
    for directory in INITIAL_PRIORITY + tuple(name for name, _ in BATCHES):
        for row in inspect(DEV, BASE / directory)["cases"]:
            if row["status"] == "saved":
                if row["data_kind"] != "fresh_internet_search":
                    raise ValueError("A replay entered the previous fresh comparator")
                previous_selected[row["id"]] = row
                previous_paths[row["id"]] = BASE / directory / (row["id"] + ".json")
    if set(previous_selected) != {f"D{i:02d}" for i in range(1, 31)}:
        raise ValueError("The declared previous selected30 comparator is incomplete")
    previous_comparison = compare({"cases": list(previous_selected.values())}, {"cases": rows})
    previous_comparison["limitation"] = "Previous latest fresh reports from several stages; compare only matching completed final IDs, alongside the initial30 comparator."
    previous_comparison["source_artifacts"] = {cid: str(previous_paths[cid].relative_to(ROOT)) for cid in previous_comparison["compared_case_ids"]}
    hashes.update({str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest() for path in previous_paths.values()})
    result["previous_selected_fresh_comparison"] = previous_comparison
    result["source_pair_transitions"] = source_pair_transitions(before, {"cases": rows})
    result.update(provenance=provenance, configuration=configuration, batch_states=states,
        pending_report_ids=[cid for cid in (f"D{i:02d}" for i in range(1, 31)) if cid not in {r["id"] for r in rows}],
        completed_reports=len(rows), expected_reports=30, planned_query_ceiling=FINAL_PLANNED_QUERIES,
        budget={"planned_queries_in_started_batches": planned, "saved_query_attempts": attempts,
            "queries_in_completed_reports": sum(row["queries"] for row in rows),
            "confirmed_provider_credits": sum(e["confirmed_provider_credits"] for e in expenses),
            "records_with_credit_usage": sum(e["records_with_credit_usage"] for e in expenses), "series": expenses},
        limitation="Only completed reports from five explicitly declared final batches; missing cases remain pending. Uniform configuration is claimed only if all30 explicit fingerprints agree.")
    audit_path = BASE / "final-selected-dev-audit-v1/reviewed-pairs.json"
    if audit_path.exists():
        audit = json.loads(audit_path.read_text())
        result["manual_remaining_batch_audit"] = {k: v for k, v in audit.items() if k != "cases"}
        result["manual_false_highlight_audit"] = {
            "confirmed_false_pairs": audit.get("confirmed_false_pair_count"),
            "confirmed_false_target_regions": audit.get("confirmed_false_target_regions"),
            "ambiguous_pairs": audit.get("ambiguous_pair_count"),
            "reviewed_reports": audit["reviewed_reports"], "completed_reports_in_audit": audit["completed_reports"],
            "matches_completed_report_count": audit["completed_reports"] == len(rows),
            "all_completed_reports_reviewed": audit["reviewed_reports"] == audit["completed_reports"] == len(rows)}
        hashes[str(audit_path.relative_to(ROOT))] = sha256(audit_path.read_bytes()).hexdigest()
    return result, hashes


def build():
    if sha256((DEV / "key.json").read_bytes()).hexdigest() != DEV_KEY_SHA:
        raise ValueError("DEV key changed; refuse silently reading another collection")
    dev_cases = json.loads((DEV / "key.json").read_text())["cases"]
    case_counts = {"documents": len(dev_cases),
        "literal_positive_documents": sum(any(e["needs_lexical_highlight"] for e in c["expected_evidence"]) for c in dev_cases),
        "semantic_only_documents": sum(bool(c["expected_evidence"]) and not any(e["needs_lexical_highlight"] for e in c["expected_evidence"]) for c in dev_cases),
        "control_documents": sum(not c["expected_evidence"] for c in dev_cases),
        "literal_fragments": sum(e["needs_lexical_highlight"] for c in dev_cases for e in c["expected_evidence"]),
        "semantic_fragments": sum(not e["needs_lexical_highlight"] for c in dev_cases for e in c["expected_evidence"])}
    before = inspect(DEV, BASE / "dev-live-30-v1")
    replay = inspect(DEV, BASE / "dev-matching-replay-v4")
    latest, chosen_paths = {}, {}
    for directory in INITIAL_PRIORITY:
        bundle = inspect(DEV, BASE / directory)
        for row in bundle["cases"]:
            if row["status"] == "saved":
                if row["data_kind"] != "fresh_internet_search":
                    raise ValueError("A saved-page replay entered the fresh union")
                latest[row["id"]] = row
                chosen_paths[row["id"]] = BASE / directory / (row["id"] + ".json")
    if tuple(sorted(latest)) != EXPECTED_FRESH:
        raise ValueError("Latest fresh union differs from the declared nine-case scope")
    fresh = {"cases": [latest[cid] for cid in EXPECTED_FRESH]}
    fresh_compare = compare(before, fresh)
    fresh_compare["source_pair_transitions"] = source_pair_transitions(before, fresh)
    initial_paths = dict(chosen_paths)
    initial_rows = dict(latest)
    batch_states, batch_expenses, batch_record_hashes = [], [], {}
    for directory, allowed_ids in ALL_BATCHES:
        bundle = inspect(DEV, BASE / directory)
        completed = []
        for row in bundle["cases"]:
            if row["status"] == "saved":
                if row["id"] not in allowed_ids or row["data_kind"] != "fresh_internet_search":
                    raise ValueError("Unexpected case or replay in a selected-code batch")
                latest[row["id"]] = row
                chosen_paths[row["id"]] = BASE / directory / (row["id"] + ".json")
                completed.append(row["id"])
        batch_states.append(batch_progress(directory, allowed_ids, completed))
        expense, hashes = batch_search_expense(directory, allowed_ids, [latest[cid] for cid in completed])
        batch_expenses.append(expense)
        batch_record_hashes.update(hashes)
    latest_bundle = {"cases": [latest[cid] for cid in sorted(latest)]}
    completed_fresh = compare(before, latest_bundle)
    completed_fresh["source_pair_transitions"] = source_pair_transitions(before, latest_bundle)
    replay_compare = compare(before, replay)
    source_files = {str((DEV / "key.json").relative_to(ROOT))}
    for directory in ("dev-live-30-v1", "dev-matching-replay-v4"):
        source_files.update(str((BASE / directory / (f"D{i:02d}.json")).relative_to(ROOT)) for i in range(1, 31))
    source_files.update(str(p.relative_to(ROOT)) for p in chosen_paths.values())
    source_files.update(batch_record_hashes)
    expenses = []
    for label, directory, filenames in EXPENSE_REPORTS:
        attempts = 0
        for filename in filenames:
            path = BASE / directory / filename
            raw = json.loads(path.read_text())
            if raw.get("offline_replay"):
                raise ValueError("A replay cannot count as fresh expense")
            count = raw["web"]["queries_attempted"]
            if type(count) is not int or count < 0 or count > 6:
                raise ValueError("Invalid saved query expense")
            attempts += count
            source_files.add(str(path.relative_to(ROOT)))
        expenses.append({"series": label, "saved_documents": len(filenames), "query_attempts": attempts})
    expenses.extend(batch_expenses)
    diagnostic_expenses, diagnostic_hashes = diagnostic_pilot_expenses(BASE, ROOT)
    expenses.extend(diagnostic_expenses)
    source_files.update(diagnostic_hashes)
    quota_candidates = []
    for directory in ("dev-final-control-live-v1", *(name for name, _ in ALL_BATCHES)):
        for filename in ("quota-before.json", "quota-after.json"):
            path = BASE / directory / filename
            if not path.exists():
                continue
            q = json.loads(path.read_text())
            source_files.add(str(path.relative_to(ROOT)))
            if "local_monthly_attempts" in q and "paygo_usage" in q:
                quota_candidates.append((q.get("checked_at", ""), q))
    for row in diagnostic_expenses:
        for q in row["quota_observations"].values():
            if "local_monthly_attempts" in q and "paygo_usage" in q:
                quota_candidates.append((q.get("checked_at", ""), q))
    quota = max(quota_candidates, key=lambda item: item[0])[1]
    initial_quota_path = BASE / "dev-live-30-v1/quota-before.json"
    pre_initial = json.loads(initial_quota_path.read_text())["local_monthly_attempts"]
    source_files.add(str(initial_quota_path.relative_to(ROOT)))
    with sqlite3.connect(ROOT / "data/search-budget.sqlite3") as connection:
        ledger = connection.execute("SELECT COUNT(*) FROM calls WHERE month=?", ("2026-11",)).fetchone()[0]
    accounted = sum(s["query_attempts"] for s in expenses)
    total = ledger - pre_initial
    pre_resumed = pre_initial + sum(s["query_attempts"] for s in expenses if s["series"] in {
        "initial_dev30", "exact_search_rejected", "paragraph_search_rejected", "targeted_matcher_v3"})
    resumed = ledger - pre_resumed
    first_path = BASE / "query-unfamiliar-first-use-v1/fresh-first-use.json"
    local_path = BASE / "query-unfamiliar-first-use-v1/local-first-use.json"
    source_files.update(str(p.relative_to(ROOT)) for p in (first_path, local_path))
    first = json.loads(first_path.read_text())
    local = json.loads(local_path.read_text())
    cache_path = BASE / "dev-saved-replay-comparison.json"
    cache = json.loads(cache_path.read_text())
    source_files.add(str(cache_path.relative_to(ROOT)))
    fresh_examples = [{"id": cid, "source_artifact": str(initial_paths[cid].relative_to(ROOT)),
                       "report_id": initial_rows[cid]["report_id"],
                       "ui_url": "http://127.0.0.1:8766/#check/" + initial_rows[cid]["report_id"]}
                      for cid in EXPECTED_FRESH]
    completed_examples, policies, completed_provenance = [], {}, []
    for cid in sorted(latest):
        raw = json.loads(chosen_paths[cid].read_text())
        policy = raw["web"].get("url_strategy", "domain-round-robin-v1 (legacy metadata)")
        policies.setdefault(policy, []).append(cid)
        completed_examples.append({"id": cid, "source_artifact": str(chosen_paths[cid].relative_to(ROOT)),
            "report_id": raw["id"], "query_strategy": raw["web"]["query_strategy"], "url_strategy": policy,
            "matcher_version": raw["web"]["matcher_version"], "ui_url": "http://127.0.0.1:8766/#check/" + raw["id"]})
        run_path = chosen_paths[cid].parent / "run.json"
        run = json.loads(run_path.read_text()) if run_path.exists() else {}
        completed_provenance.append(report_provenance(raw, run, chosen_paths[cid].parent.name,
            sha256(chosen_paths[cid].read_bytes()).hexdigest(), case_id=cid))
    audit_path = BASE / "selected-dev-audit-v1/reviewed-pairs.json"
    manual_audit = None
    if audit_path.exists():
        audit = json.loads(audit_path.read_text())
        source_files.add(str(audit_path.relative_to(ROOT)))
        manual_audit = {k: v for k, v in audit.items() if k != "cases"}
    hashes = {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in sorted(source_files)}
    hashes.update(batch_record_hashes)
    final_selected, final_hashes = final_selected_summary(before)
    hashes.update(final_hashes)
    return {"schema_version": 1, "scope": "30 DEV plus eight separately reserved query-first-use cases; final collection unopened",
        "network_calls_by_this_script": 0, "store_writes": 0,
        "case_counts": case_counts,
        "original_30": before["summary"], "saved_matcher_replay_30": replay_compare,
        "saved_matcher_replay_ui_warning": "No after-report UI URLs: replay snapshots may reuse original report IDs while containing changed matches. Verify private JSON instead.",
        "latest_fresh_union_9": {**fresh_compare, "selection_priority_low_to_high": list(INITIAL_PRIORITY),
                                "provenance": fresh_examples,
                                "limitation": "Nine unique DEV cases from several fresh stages; not a fresh run of all 30, and not all nine ran the final defaults."},
        "latest_fresh_union_completed": {**completed_fresh, "selection_priority_low_to_high": list(PRIORITY),
            "provenance": completed_examples, "queue_policy_case_ids": policies, "batch_states": batch_states,
            "per_case_configuration": completed_provenance,
            "configuration": cohort_configuration(completed_provenance, [f"D{i:02d}" for i in range(1, 31)]),
            "manual_remaining_batch_audit": manual_audit,
            "pending_report_ids": [cid for cid in (f"D{i:02d}" for i in range(1, 31)) if cid not in latest],
            "limitation": "Latest completed reports from several stages, not a simultaneous fresh30 run. Four earlier domain-policy cases have a separate same-cache selected-policy crosscheck. Pending/failed new runs are not silently replaced by their original baseline reports."},
        "final_selected_fresh30": final_selected,
        "same_cache_six_case_crosscheck": cache["comparison"],
        "query_first_use": {"local": local["summary"], "fresh": first["summary"],
                            "provenance_note": first["provenance_note"], "used_for_tuning": first["used_for_tuning"],
                            "fresh_baseline_not_run": first["baseline_fresh_search_not_run"]},
        "expenses": {"series": expenses, "attempts_since_initial30_started": total,
            "new_attempts_in_resumed_work": resumed, "quota_snapshot": quota,
            "monthly_ledger_attempts": ledger, "attempts_before_initial30": pre_initial,
            "attempts_accounted_by_series": accounted, "ledger_minus_accounted_series": total - accounted,
            "attempts_since_latest_quota_snapshot": max(0, ledger - quota["local_monthly_attempts"]),
            "provider_plan_usage_may_lag": True, "money_spent_kzt": 0,
            "note": "Attempts and confirmed provider credits are separate; per-call usage is fully known only where recorded."},
        "input_artifacts_sha256": dict(sorted(hashes.items()))}


def refresh_draft(result, path):
    """Replace only marked numeric sections; keep the report explicitly a draft."""
    cohort = result.get("final_selected_fresh30", result["latest_fresh_union_completed"])
    a, b = cohort["before"], cohort["after"]
    n = b["saved_documents"]
    pending = cohort["pending_report_ids"]
    final_stage = "final_selected_fresh30" in result
    uniform = cohort.get("configuration", {}).get("uniform", False)
    manual_false = cohort.get("manual_false_highlight_audit")
    false_after = (str(manual_false["confirmed_false_pairs"])
                   if manual_false and manual_false["all_completed_reports_reviewed"]
                   and manual_false["confirmed_false_pairs"] is not None
                   else str(b["confirmed_false_similar_source_pairs"]) + " (ручной разбор не завершён)"
                   if final_stage else str(b["confirmed_false_similar_source_pairs"]))
    status = (f"Ожидаются новые отчёты ещё по {len(pending)} случаям: {', '.join(pending)}. Они не включены в этот подсчёт."
              if pending else "Все 30 завершены с одинаковой подтверждённой конфигурацией."
              if final_stage and uniform else "Все 30 завершены; конфигурации перечислены по случаям, единообразие не подтверждено."
              if final_stage else "Все 30 имеют новый последний отчёт; это объединение этапов, а не одновременный прогон.")
    lines = [f"Свежий поиск: последние завершённые отчёты **{n}/30 DEV-случаев**, сравненные с теми же исходными случаями. {status}", "",
        "| Показатель | До → после | Объём проверки |", "| --- | --- | --- |",
        f"| Ожидаемый исходный URL найден | {a['expected_source_pairs_discovered']}/{a['expected_source_pairs']} → {b['expected_source_pairs_discovered']}/{b['expected_source_pairs']} | Пары «документ — источник», {n} документов |",
        f"| Ожидаемый исходный URL прочитан | {a['expected_source_pairs_read']}/{a['expected_source_pairs']} → {b['expected_source_pairs_read']}/{b['expected_source_pairs']} | Те же пары |",
        f"| Фрагменты с полным покрытием скопированных слов | {a['fully_highlighted_any_source']}/{a['lexical_fragments']} → {b['fully_highlighted_any_source']}/{b['lexical_fragments']} | {b['lexical_fragments']} фрагментов в {b['lexical_documents']} положительных документах |",
        f"| Подтверждённые скопированные слова | {a['copied_words_highlighted_any_source']}/{a['copied_words']} → {b['copied_words_highlighted_any_source']}/{b['copied_words']} | Те же дословные фрагменты |",
        f"| Подтверждённые ложные подсветки | {a['confirmed_false_similar_source_pairs']} → {false_after} | Все {n} завершённых документов; спорные пары отдельно |",
        f"| Документы без прочитанных страниц | {a['documents_without_read_pages']}/{n} → {b['documents_without_read_pages']}/{n} | Все завершённые |",
        f"| Поисковых запросов | {a['network_queries_this_run']} → {b['network_queries_this_run']} | Те же {n}; максимум шесть на документ |", "",
        ("Это отдельный финальный прогон пяти батчей. Незавершённые случаи не подставлены из ранних этапов. Часть совпадений найдена на альтернативных страницах."
         if final_stage else "Это результаты нескольких этапов. Часть совпадений найдена на альтернативных страницах. Четыре ранних случая D03/D05/D08/D16 запускались с прежним порядком чтения; для них есть отдельное сравнение на одинаковом кеше шести случаев без потери прежних слов.")]
    losses = cohort["lost_fragments"]
    if losses:
        lines += ["", "Зафиксированные потери относительно исходного прогона: " + "; ".join(
            f"{r['id']}, фрагмент {r['fragment_index'] + 1}: {r['copied_word_delta']} слов" for r in losses) + ". Они включены в таблицу."]
        if not final_stage and any(r["id"] == "D09" for r in losses):
            lines += ["В D09 нужный URL есть в выдаче, но оказался за лимитом чтения. Прочитанные зеркала содержат другую формулировку; это потеря на отборе страницы, а не подтверждённая ошибка matcher."]
    prior_losses = cohort.get("previous_selected_fresh_comparison", {}).get("lost_fragments", [])
    if prior_losses:
        lines += ["", "Потери относительно предыдущих свежих выбранных отчётов (включая улучшения, которых не было в исходных30): "
                  + "; ".join(f"{r['id']}, фрагмент {r['fragment_index'] + 1}: {r['copied_word_delta']} слов" for r in prior_losses)
                  + ". Эти потери не скрыты улучшением относительно исходного прогона."]
    source_losses = cohort.get("source_pair_transitions", {}).get("expected_url_discovered", {}).get("lost", [])
    if source_losses:
        lines += ["Строгий ожидаемый URL больше не найден в " + ", ".join(sorted({r["id"] for r in source_losses}))
                  + "; совпавшие слова на альтернативной странице считаются отдельно."]
    audit = cohort.get("manual_remaining_batch_audit")
    if audit:
        decisions = audit["pair_decisions"]
        lines += ["", f"Ручной разбор новых батчей: {audit['reviewed_reports']}/{audit['completed_reports']} отчётов; "
                  f"нерассмотренных пар {decisions.get('needs_pair_review', 0)}. Спорное происхождение общей правовой формулы D19: "
                  f"{decisions.get('ambiguous_common_legal_origin', 0)} пар; факт буквального совпадения подтверждён, источник заимствования не установлен."]
    expense = result["expenses"]
    activity = ("часть попыток относится к ещё выполняемым документам. " if pending
                else "учтённые поисковые серии завершены. ")
    accounting = (f"Неучтённая разница журнала и серий: {expense['ledger_minus_accounted_series']}. "
                  if expense.get("ledger_minus_accounted_series", 0) else "Неучтённых попыток нет. ")
    cost = (f"Расход на момент снимка: {expense['new_attempts_in_resumed_work']} новых поисковых попыток в продолженной работе; "
        f"{expense['attempts_since_initial30_started']} с начала исходного прогона, включая его 46. Месячный журнал: "
        f"{expense['monthly_ledger_attempts']} = {expense['attempts_before_initial30']} прежних + {expense['attempts_since_initial30_started']}; "
        + activity + accounting + "Последний подтверждённый бесплатный лимит — "
        f"{expense['quota_snapshot']['plan_limit']}, платный перерасход — {expense['quota_snapshot']['paygo_usage']}; расходы **0 ₸**. "
        "Счётчик провайдера отстаёт. Full reset применил владелец вручную; агент его не применял и прекратил проверки лимита по просьбе владельца.")
    text = path.read_text()
    for name, body in (("selected-fresh", "\n".join(lines)), ("expense", cost)):
        start, end = f"<!-- {name}-start -->", f"<!-- {name}-end -->"
        if text.count(start) != 1 or text.count(end) != 1:
            raise ValueError("Draft update markers missing or duplicated")
        prefix, rest = text.split(start, 1)
        _, suffix = rest.split(end, 1)
        text = prefix + start + "\n" + body + "\n" + end + suffix
    path.write_text(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--refresh-draft", action="store_true")
    parser.add_argument("--final-only", action="store_true", help="Read only original DEV30 and the five final DEV batches; no first-use material")
    args = parser.parse_args()
    if args.final_only:
        if args.refresh_draft:
            parser.error("Final-only output omits historic expense accounting; refresh the whole draft with the full summary separately.")
        final, hashes = final_selected_summary()
        result = {"schema_version": 1, "scope": "Only DEV30 baseline and five final selected-code DEV batches",
            "network_calls_by_this_script": 0, "store_writes": 0, "final_selected_fresh30": final,
            "input_artifacts_sha256": hashes}
    else:
        result = build()
    output = args.output or BASE / ("night-final-selected-2026-11-12.json" if args.final_only else "night-summary-2026-11-12.json")
    dump(output, result)
    if args.refresh_draft:
        refresh_draft(result, ROOT / "docs/MORNING_REPORT_2026-11-12.md")
    if args.final_only:
        print(json.dumps({k: v for k, v in result["final_selected_fresh30"].items()
                          if k not in {"provenance", "batch_states"}}, ensure_ascii=False, indent=2))
        sys.exit(0)
    print(json.dumps({"original30": result["original_30"],
        "replay30_after": result["saved_matcher_replay_30"]["after"],
        "fresh9_before": result["latest_fresh_union_9"]["before"],
        "fresh9_after": result["latest_fresh_union_9"]["after"],
        "latest_completed_after": result["latest_fresh_union_completed"]["after"],
        "awaiting_new_reports": result["latest_fresh_union_completed"]["pending_report_ids"],
        "final_selected_after": result["final_selected_fresh30"]["after"],
        "awaiting_final_reports": result["final_selected_fresh30"]["pending_report_ids"],
        "expenses": result["expenses"]}, ensure_ascii=False, indent=2))
