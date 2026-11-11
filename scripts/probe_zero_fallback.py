"""Local prototype of a frozen, initial-zero-only extra reading phase.

The caller supplies an original report and the complete order of already saved
search URLs. No discovery, network, source oracle, or recursive links exist.
"""
from copy import deepcopy
from hashlib import sha256
import time

from plagiarism import web_search as web
from plagiarism.pdf_evidence import analyze_with_supplements
from plagiarism.web_fetch import FetchError
from plagiarism.web_evaluation import _report
from scripts.dev_report_provenance import source_snapshot_signatures

STRATEGY = "zero-after-twelve-extra-twelve-local-v1"
FIRST_PHASE_LIMIT = 12
EXTRA_LIMIT = 12
OVERALL_LIMIT = 24
PROPOSED_PRODUCTION_SECONDS = 300


def grounded_exact_words(text, report):
    source_snapshot_signatures(report)
    # This synthetic validation identity contains only the submitted text hash;
    # no answer-key source IDs, spans or URLs participate in opening the gate.
    checked = _report(report, {"id": "zero-fallback-prefix",
        "text_sha256": sha256(text.encode()).hexdigest()})
    intervals = [{"kind": "exact", "start": item["target_span"][0], "end": item["target_span"][1]}
                 for item in checked["matches"]]
    return web._word_coverage(text, intervals)["matched_words"]


def extend_report(text, original_report, search_order, lookup_page):
    """Initial gate only: after zero exact words, examine up to12 extra URLs.

    lookup_page returns a legitimate saved Fetcher payload or raises FetchError.
    An unavailable snapshot stays an explicit unknown/failed attempt. Expected
    source labels and document annotations are not arguments to this function.
    """
    started = time.perf_counter()
    report = deepcopy(original_report)
    if text != report.get("text") or len(search_order) > 30:
        raise ValueError("Frozen report text and at most thirty search URLs required")
    prefix = report["web"]["attempted_urls"]
    if len(prefix) != report["web"]["urls_attempted"] or len(prefix) > FIRST_PHASE_LIMIT:
        raise ValueError("Original physical reading phase exceeds twelve")
    signatures = source_snapshot_signatures(report)
    words_before = grounded_exact_words(text, report)
    audit = {"strategy": STRATEGY, "gate": "original_phase_grounded_exact_words_equal_zero",
        "initial_gate_words": words_before, "triggered": words_before == 0,
        "first_phase_attempted_urls": list(prefix), "first_phase_source_signatures": signatures,
        "extra_limit": EXTRA_LIMIT, "overall_limit": OVERALL_LIMIT,
        "initial_gate_only_continue_after_extra_match": True,
        "no_new_queries": True, "no_recursive_followups": True, "network_calls": 0,
        "proposed_production_time_budget_seconds": PROPOSED_PRODUCTION_SECONDS,
        "cached_time_is_not_live_latency": True, "extra_attempted_urls": [], "events": [],
        "unknown_extra_urls": [], "failed_extra_urls": []}
    original_limit = {"urls_outside_limit": report["web"].get("urls_outside_limit"),
        "skipped_urls": deepcopy(report["web"].get("skipped_urls", [])),
        "limit_errors": deepcopy([error for error in report["web"]["errors"] if error.get("code") == "url_limit_reached"])}
    if words_before:
        audit.update(prefix_preserved=True, cached_fallback_seconds=round(time.perf_counter() - started, 6))
        return report, audit
    primary = [s for s in report["sources"] if not s.get("representation_of")]
    supplements = [s for s in report["sources"] if s.get("representation_of")]
    seen_requests = {web._canonical_url(url) for url in prefix}
    final_urls = {web._canonical_url(source["url"]) for source in primary}
    remaining, seen = [], set(seen_requests | final_urls)
    for value in search_order:
        url = web._canonical_url(value)
        if url and url not in seen:
            seen.add(url)
            remaining.append(url)
    for url in remaining[:min(EXTRA_LIMIT, OVERALL_LIMIT - len(prefix))]:
        audit["extra_attempted_urls"].append(url)
        event = {"requested_url": url, "basis": "remaining_saved_search_url"}
        audit["events"].append(event)
        try:
            page = lookup_page(url)
            final = web._canonical_url(page["url"])
            if not final or not isinstance(page.get("text"), str) or len(page["text"].strip()) < 35:
                raise ValueError("Invalid saved source body")
            actual_hash = sha256(page["text"].encode()).hexdigest()
            if page.get("text_sha256", actual_hash) != actual_hash:
                raise ValueError("Saved source body hash changed")
            if final in final_urls:
                event.update(state="duplicate_final_url", final_url=final)
                continue
            source = {"id": "web-" + sha256(final.encode()).hexdigest()[:24],
                "title": page.get("title") or final, "url": final, "text": page["text"],
                "provenance": {"origin": "saved_web_fetch_for_local_zero_fallback", "fetched_at": page.get("fetched_at"),
                    "text_sha256": actual_hash, "license_status": "not_verified", "training_consent": False,
                    "snapshot_origin": page.get("_snapshot_origin", page.get("_replay_origin"))}}
            views = web._supplemental_source_views(source, page)
            # Validate the new page/view with the same matcher before appending;
            # failure cannot mutate any existing source snapshot.
            analyze_with_supplements(text, [source], views)
            primary.append(source)
            supplements.extend(views)
            report["sources"].extend([source] + views)
            final_urls.add(final)
            event.update(state="read_saved_body", final_url=final, text_sha256=actual_hash,
                         snapshot_origin=source["provenance"]["snapshot_origin"], supplemental_views=len(views))
        except (RuntimeError, OSError, ValueError, KeyError) as error:
            code = getattr(error, "code", "invalid_saved_page")
            event.update(state="unknown_content" if code == "saved_page_cache_miss" else "saved_failed_fetch", code=code)
            audit["unknown_extra_urls" if code == "saved_page_cache_miss" else "failed_extra_urls"].append(url)
            report["web"]["errors"].append({"stage": "zero_fallback", "url": url, "code": code,
                "message": "Дополнительная страница не проверена; содержимое не считается уникальным."})
    result = analyze_with_supplements(text, primary, supplements)
    coverage = web._word_coverage(text, result["matches"])
    result["stats"].update(coverage, uniqueness_percent=None)
    if coverage["matched_words"]:
        total, unmatched = coverage["total_words"], coverage["total_words"] - coverage["matched_words"]
        result["stats"]["uniqueness_upper_bound_percent"] = ((1000 * unmatched + total - 1) // total) / 10
    report.update(matches=result["matches"], issues=result["issues"], stats=result["stats"])
    report["web"].update(status="partial", urls_attempted=len(prefix) + len(audit["extra_attempted_urls"]),
        attempted_urls=prefix + audit["extra_attempted_urls"], sources_checked=len(primary),
        supplemental_text_views=len(supplements), zero_fallback_strategy=STRATEGY,
        zero_fallback_is_local_saved_data_only=True)
    # Retain the first-phase accounting explicitly, then describe the current
    # unread queue rather than carrying an obsolete twelve-page limit count.
    checked_urls = seen_requests | final_urls | set(audit["extra_attempted_urls"])
    all_search = list(dict.fromkeys(web._canonical_url(value) for value in search_order if web._canonical_url(value)))
    remaining_search = [url for url in all_search if url not in checked_urls]
    nonsearch_skips = [url for url in original_limit["skipped_urls"]
        if web._canonical_url(url) not in checked_urls and web._canonical_url(url) not in set(all_search)]
    current_skipped = list(dict.fromkeys(remaining_search + nonsearch_skips))
    report["web"].update(first_phase_url_limit_metadata=original_limit,
        skipped_urls=current_skipped, urls_outside_limit=len(current_skipped))
    report["web"]["errors"] = [error for error in report["web"]["errors"] if error.get("code") != "url_limit_reached"]
    if current_skipped:
        report["web"]["errors"].append({"stage": "limit", "code": "url_limit_reached", "count": len(current_skipped),
            "message": "Часть найденных ссылок не проверена из-за лимита загрузок."})
    audit.update(remaining_search_urls=remaining_search, current_unattempted_search_count=len(remaining_search))
    report["web"]["limitations"] = [line for line in report["web"].get("limitations", [])
        if "двенадцати страниц" not in line] + ["Локальный эксперимент: до24 попыток по сохранённым данным; новых сетевых загрузок нет."]
    if not any(issue.get("kind") == "partial_web_check" for issue in report["issues"]):
        report["issues"].append({"kind": "partial_web_check", "message": "Локальный повтор ограничен сохранёнными источниками."})
    prefix_after = {key: value for key, value in source_snapshot_signatures(report).items() if key in signatures}
    if prefix_after != signatures or report["sources"][:len(original_report["sources"])] != original_report["sources"]:
        raise ValueError("Fallback changed an original source snapshot")
    audit.update(prefix_preserved=True, cached_fallback_seconds=round(time.perf_counter() - started, 6),
                 grounded_words_after=grounded_exact_words(text, report))
    return report, audit
