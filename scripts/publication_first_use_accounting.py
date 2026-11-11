"""Pure parameterized reconciliation of one publication run's durable artifacts."""
import json


def reconcile(out, allowed, plan_sizes, reports):
    def read(path):
        if path.is_symlink() or not path.resolve().is_relative_to(out.resolve()):
            raise ValueError("Publication accounting refuses symlinks/outside artifacts")
        return json.loads(path.read_text())
    records, expected, issues = {}, set(), []
    for suffix in ("search", "mediawiki"):
        for path in out.glob("*-" + suffix + ".json"):
            call = path.name.removesuffix("-" + suffix + ".json")
            identity, record = allowed.get(call), read(path)
            if (not identity or record.get("request_id") != call
                    or suffix != ("search" if identity["provider"] == "tavily" else "mediawiki")
                    or any(record.get(k) != v for k, v in identity.items() if k != "provider")
                    or type(record.get("query_index")) is not int
                    or (identity["provider"] == "tavily" and record.get("search_depth") != "basic")
                    or (identity["provider"] == "mediawiki" and record.get("provider") != "mediawiki")
                    or record.get("state") not in ("submitted", "complete", "failed_or_unknown")):
                raise ValueError("Unbound publication provider marker")
            records[call] = record
    for provider in ("tavily", "mediawiki"):
        for path in out.glob("*-" + provider + "-intent.json"):
            call, intent = path.name.removesuffix("-" + provider + "-intent.json"), read(path)
            if (call not in allowed or intent.get("request_id") != call or allowed[call]["provider"] != provider
                    or any(intent.get(k) != v for k, v in allowed[call].items())):
                raise ValueError("Unbound publication provider intent")
            expected.add(call)
    for cid, sizes in plan_sizes.items():
        for provider, size in sizes.items():
            reported = reports.get(cid)
            n = reported.get(provider) if isinstance(reported, dict) else reported if provider == "tavily" else None
            subset = {c: v for c, v in allowed.items() if v["case_id"] == cid and v["provider"] == provider}
            actual = [v["query_index"] for c, v in subset.items() if c in records]
            if n is not None and (type(n) is not int or not 0 <= n <= size):
                raise ValueError("Invalid saved logical query count")
            minimum = max(n or 0, max(actual, default=-1) + 1)
            expected.update(c for c, v in subset.items() if v["query_index"] < minimum)
            extra = [c for c, v in subset.items() if c in records and n is not None and v["query_index"] >= n]
            if extra:
                issues.append({"id": cid, "provider": provider, "code": "markers_beyond_reported_attempts", "call_ids": sorted(extra)})
        if any((out / (cid + suffix)).exists() for suffix in ("-request.json", "-submission-intent.json")) and cid not in reports:
            issues.append({"id": cid, "code": "submitted_report_missing"})
    missing, known, unknown, public, unknown_public = expected - records.keys(), [], [], [], []
    for call, record in records.items():
        if allowed[call]["provider"] == "mediawiki":
            usage = record.get("usage")
            if usage is None:
                unknown_public.append(call)
            elif (not isinstance(usage, dict) or type(usage.get("public_queries")) is not int
                    or not 0 <= usage["public_queries"] <= 1 or type(usage.get("cost_kzt")) is not int or usage["cost_kzt"] != 0):
                raise ValueError("Invalid anonymous MediaWiki usage metadata")
            else:
                public.append(usage["public_queries"])
            continue  # Anonymous free usage cannot fill a missing paid-credit field.
        usage = record.get("usage")
        if usage is None:
            unknown.append(call)
        elif not isinstance(usage, dict) or type(usage.get("credits")) is not int or not 0 <= usage["credits"] <= 1:
            raise ValueError("Invalid explicit Basic-search credit usage")
        else:
            known.append(usage["credits"])
    if missing:
        issues.append({"code": "provider_markers_missing", "call_ids": sorted(missing)})
    counts = {p: sum(allowed[c]["provider"] == p for c in records) for p in ("tavily", "mediawiki")}
    return {"provider_attempts": len(records), "tavily_attempts": counts["tavily"], "mediawiki_attempts": counts["mediawiki"],
        "minimum_provider_attempts": len(records) + len(missing), "confirmed_credit_subtotal": sum(known),
        "confirmed_public_queries_subtotal": sum(public), "unknown_public_usage_call_ids": sorted(unknown_public),
        "total_new_public_queries": sum(public) if not issues and not unknown_public else None,
        "unknown_usage_call_ids": sorted(unknown), "missing_provider_call_ids": sorted(missing), "issues": issues,
        "total_basic_credits": sum(known) if not issues and not unknown else None}
