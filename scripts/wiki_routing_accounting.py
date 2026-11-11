"""Read-only accounting for completed DEV Wiki routing A, never a live client.

MediaWiki calls are anonymous, not Tavily credits. Cached logical slots never
enter either fresh-call counter. Only explicit run/report/marker files are read.
"""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re

from plagiarism.saas import report_identifier
from scripts.metadata_pilot_accounting import _read

SERIES = "dev-wiki-routing-causal-v1"
VERSION = "citation-priority-tavily-first-two-unprotected-wiki-v1"
IDS = tuple(f"D{i:02d}" for i in range(1, 31))
PARAMS = {"action": "query", "format": "json", "formatversion": 2, "list": "search", "maxlag": 5,
          "srlimit": 5, "srnamespace": 0, "srprop": "snippet"}
UNKNOWN_CODES = {"saved_page_cache_miss", "fetch_failed_or_unknown", "page_outcome_unknown", "page_probe_cap", "source_http_cap"}


def digest(value): return sha256(value.encode()).hexdigest()


def unknown_page_outcome(event):
    """An observed PDF page-limit failure is not a missing cached outcome."""
    return event.get("state") in {"submitted", "unavailable"} or event.get("code") in UNKNOWN_CODES


def identity(run):
    c = run.get("configuration", {})
    if (c.get("version") != VERSION or tuple(c.get("case_ids", [])) != IDS
            or c.get("maximum_new_Wiki_GETs") != 57 or c.get("maximum_source_HTTP_requests") != 2160
            or c.get("maximum_new_Fetcher_attempts") != 720 or c.get("new_Tavily_queries") != 0
            or c.get("no_retries") is not True or c.get("no_Tavily_fallback") is not True
            or c.get("default_changed") is not False or c.get("api_parameters") != PARAMS):
        raise ValueError("Wrong fixed Wiki routing A accounting scope")
    expected_id = digest(json.dumps(c, sort_keys=True, ensure_ascii=False, allow_nan=False))
    requests = {cid + "-" + role: digest(expected_id + cid + role)[:32] for cid in IDS for role in ("baseline", "selected")}
    if run.get("id") != expected_id or run.get("request_ids") != requests:
        raise ValueError("Frozen routing request identity changed")
    plans, routes, reuse = c.get("plans", {}), c.get("routes", {}), c.get("exact_singleton_reuse", {})
    if set(plans) != set(IDS) or set(routes) != set(IDS) or len(reuse) != 3:
        raise ValueError("Routing cases or three cached slots changed")
    allowed, slots, logical, cached_tavily = {}, {}, 0, 0
    for cid in IDS:
        if not 1 <= len(plans[cid]) <= 6 or len(routes[cid]) != len(plans[cid]): raise ValueError("Invalid routing plan length")
        chosen = 0; logical += len(plans[cid])
        for index, (plan, row) in enumerate(zip(plans[cid], routes[cid])):
            if ({k:row[k] for k in ("query", "start", "end")} != plan or type(row.get("index")) is not int
                    or row["index"] != index or type(row.get("citation_protected")) is not bool):
                raise ValueError("Route changed its query/span/index")
            wiki = not row["citation_protected"] and chosen < 2
            if wiki: chosen += 1
            if row.get("provider") != ("mediawiki" if wiki else "tavily"): raise ValueError("Route differs from frozen first-two policy")
            if not wiki: cached_tavily += 1; continue
            key = cid + ":" + str(index); call = digest(expected_id + cid + "wiki" + str(index))
            slots[key] = {"call":call, "case_id":cid, "query_index":index, "query":plan["query"]}
            if key not in reuse: allowed[call] = {k:slots[key][k] for k in ("case_id", "query_index", "query")}
    if logical != 167 or len(slots) != 60 or len(allowed) != 57 or not set(reuse) <= set(slots):
        raise ValueError("167 logical /60 Wiki /57 new-call bounds changed")
    return c, allowed, slots, cached_tavily


def collect(base, root):
    base, root = Path(base).resolve(), Path(root).resolve(); directory = base / SERIES
    if not base.resolve().is_relative_to(root): raise ValueError("Accounting base leaves declared root")
    if not directory.exists(): return [], {}
    hashes = {}; read = lambda path, scope=directory: _read(path, scope, root, hashes)
    run = read(directory / "run.json"); config, allowed, slots, cached_tavily = identity(run)
    records = {}
    for path in sorted(directory.glob("*-mediawiki.json")):
        call = path.name.removesuffix("-mediawiki.json"); expected = allowed.get(call)
        if not expected: raise ValueError("Unbound routing API marker")
        r = read(path)
        if (r.get("request_id") != call or any(r.get(k) != v for k,v in expected.items())
                or type(r.get("query_index")) is not int or r.get("parameters") != {**PARAMS, "srsearch":expected["query"]}
                or r.get("state") not in {"complete", "submitted", "failed_or_unknown"}):
            raise ValueError("Routing API marker differs from frozen identity")
        records[call] = r
    cached = 0
    for key, origin in config["exact_singleton_reuse"].items():
        path = root / origin["path"]; scope = base / "dev-mediawiki-singleton-v3"
        if path.parent != scope or not re.fullmatch(r"[a-f0-9]{64}-mediawiki\.json", path.name): raise ValueError("Unexpected cached Wiki origin")
        r = read(path, scope)
        if (hashes[path.resolve().relative_to(root).as_posix()] != origin["sha256"] or r.get("state") != "complete"
                or r.get("query") != slots[key]["query"] or r.get("parameters") != {**PARAMS, "srsearch":slots[key]["query"]}):
            raise ValueError("Cached Wiki query/parameters/hash changed")
        cached += 1
    expected_calls, issues, saved, logical, page_events = set(), [], [], 0, []
    for cid in IDS:
        for role in ("baseline", "selected"):
            key = cid + "-" + role; path = directory / (key + ".json")
            if not path.exists(): issues.append({"job":key,"code":"report_missing"}); continue
            r = read(path); w = r.get("web", {}); events = w.get("routed_query_events", []); plan = config["plans"][cid]; n = w.get("queries_attempted")
            if (r.get("id") != report_identifier(run["request_ids"][key]) or digest(r.get("text", "")) != config["input_sha256"][cid]
                    or w.get("query_strategy") != VERSION + ":" + role or type(n) is not int or not 0 <= n <= len(plan)
                    or w.get("query_spans") != [{k:p[k] for k in ("start", "end")} for p in plan]
                    or not isinstance(events, list) or len(events) != n):
                raise ValueError("Routing report/input/query identity changed")
            for index, event in enumerate(events):
                provider = "tavily" if role == "baseline" else config["routes"][cid][index]["provider"]
                if event.get("index") != index or event.get("query") != plan[index]["query"] or event.get("provider") != provider or event.get("new_Tavily_calls") != 0:
                    raise ValueError("Logical query event differs from frozen route")
                slot = slots.get(cid + ":" + str(index))
                if role == "selected" and provider == "mediawiki" and slot["call"] in allowed and event.get("state") != "unavailable_after_halt":
                    expected_calls.add(slot["call"])
            if role == "selected": page_events.extend(w.get("page_outcome_events", []))
            saved.append(key); logical += n
    missing, unreported = expected_calls - records.keys(), records.keys() - expected_calls
    if missing: issues.append({"code":"provider_markers_missing","call_ids":sorted(missing)})
    if unreported: issues.append({"code":"provider_markers_without_reported_event","call_ids":sorted(unreported)})
    wire = sorted(directory.glob("source-http-*.json")); pages = sorted((directory / "pages").glob("*.json"))
    if len(wire) > 2160 or len(pages) > 720: raise ValueError("Source attempt bounds exceeded")
    for path in (*wire, *pages): read(path)
    typed = Counter(e.get("code") for e in page_events if e.get("state") != "complete" and not unknown_page_outcome(e))
    row = {"series":SERIES,"saved_reports":len(saved),"planned_reports":60,"logical_queries_processed":logical,
        "selected_Wiki_slots":60,"exact_cached_Wiki_slots":cached,"selected_cached_Tavily_slots":cached_tavily,
        "baseline_cached_Tavily_slots":167,"actual_new_mediawiki_intents":len(records),
        "new_mediawiki_complete":sum(r["state"]=="complete" for r in records.values()),
        "new_mediawiki_failed_or_unknown":sum(r["state"]!="complete" for r in records.values()),
        "minimum_new_mediawiki_attempts_consistent_with_reports":len(records)+len(missing),
        "total_new_mediawiki_gets":len(records) if not issues else None,"new_Tavily_calls":0,
        "accounting_complete":not issues,"accounting_issues":issues,"source_HTTP_attempts_including_robots_redirects":len(wire),
        "new_normal_Fetcher_intents":len(pages),"selected_unknown_page_outcomes":sum(unknown_page_outcome(e) for e in page_events),
        "selected_observed_typed_failures":dict(sorted((k,v) for k,v in typed.items() if k)),
        "credit_API":False,"money_spent_kzt":0,
        "note":"Anonymous MediaWiki GETs are separate from Tavily credits/ledger. Known typed PDF page-limit failures are observed failures, not missing outcomes. No live client, source matching or private Python is executed."}
    return [row], hashes
