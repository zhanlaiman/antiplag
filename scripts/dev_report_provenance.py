"""Pure configuration and source identities for saved DEV report summaries.

No files, network, defaults, current checkout, or evaluation labels are read.
Missing historical metadata remains unknown. Observed extraction output never
establishes which optional extractor was configured at launch.
"""
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
import json
import re


CONFIGURATION_FIELDS = (
    "query_strategy", "url_strategy", "matcher_version", "named_references_enabled",
    "pdf_configured_engine", "source_followup_policy", "html_extraction_version",
    "source_code_sha256", "git_head",
)
_SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")
_GIT = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})\Z")


def _string(value):
    if not isinstance(value, str) or not value.strip() or value.strip().lower() in {"unknown", "unavailable", "not_recorded", "unset", "null"}:
        return None
    return value.strip()


def _boolean(value):
    return value if type(value) is bool else None


def _code_hashes(value):
    if isinstance(value, str):
        return value.lower() if _SHA256.fullmatch(value) else None
    if isinstance(value, Mapping) and value and all(isinstance(key, str) and key
            and isinstance(item, str) and _SHA256.fullmatch(item) for key, item in value.items()):
        return {key: value[key].lower() for key in sorted(value)}
    return None


def _select(field, candidates, convert, conflicts):
    values = [converted for value in candidates if (converted := convert(value)) is not None]
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        conflicts[field] = deepcopy(values)
        return None
    return deepcopy(values[0])


def report_provenance(raw, run, directory, report_sha, *, case_id=None, observed_fetch_extractions=None):
    """Describe one report using only explicit supplied report/run metadata.

    ``case_id`` is supplied by the collector: report ``id`` is a report UUID.
    ``observed_fetch_extractions`` optionally supplies actual saved-fetch
    extraction labels; omitted observations stay distinct from configuration.
    Explicit conflicting report/run policy metadata makes that field unknown.
    """
    if not isinstance(raw, Mapping) or not isinstance(run, Mapping):
        raise ValueError("Report and launch metadata must be mappings")
    web = raw.get("web", {})
    web = web if isinstance(web, Mapping) else {}
    launch = run.get("launch_code_metadata", {})
    launch = launch if isinstance(launch, Mapping) else {}
    conflicts = {}
    configuration = {
        "query_strategy": _select("query_strategy", (web.get("query_strategy"), run.get("query_strategy")), _string, conflicts),
        "url_strategy": _select("url_strategy", (web.get("url_strategy"), run.get("url_strategy")), _string, conflicts),
        "matcher_version": _select("matcher_version", (web.get("matcher_version"), raw.get("matcher_version"), run.get("matcher_version")), _string, conflicts),
        "named_references_enabled": _select("named_references_enabled", (web.get("named_references_enabled"), run.get("named_references_enabled"), run.get("named_references"), run.get("use_named_references")), _boolean, conflicts),
        # None is unknown. Only an explicitly recorded string such as "none"
        # establishes a disabled engine; zero observed views cannot do so.
        "pdf_configured_engine": _select("pdf_configured_engine", (run.get("pdf_alternative_engine"), run.get("pdf_supplemental_engine")), _string, conflicts),
        "source_followup_policy": _select("source_followup_policy", (web.get("source_followup_policy"), run.get("source_followup_policy")), _string, conflicts),
        "html_extraction_version": _select("html_extraction_version", (run.get("html_extraction_version"), launch.get("html_extraction_version")), _string, conflicts),
        "source_code_sha256": _code_hashes(run.get("source_code_sha256")),
        "git_head": run.get("git_head").lower() if isinstance(run.get("git_head"), str) and _GIT.fullmatch(run["git_head"]) else None,
    }
    sources = raw.get("sources")
    sources = sources if isinstance(sources, list) else None
    views = [source for source in sources or [] if isinstance(source, Mapping) and source.get("representation_of")]
    count = web.get("supplemental_text_views")
    observed_views = count if type(count) is int and count >= 0 else len(views) if sources is not None else None
    view_extractions = sorted({_string(source.get("provenance", {}).get("extraction")) for source in views
                               if isinstance(source.get("provenance"), Mapping)
                               and _string(source["provenance"].get("extraction")) is not None})
    source_extractions = {_string(source.get("provenance", {}).get("extraction")) for source in sources or []
                          if isinstance(source, Mapping) and isinstance(source.get("provenance"), Mapping)
                          and _string(source["provenance"].get("extraction")) is not None}
    if observed_fetch_extractions is not None:
        if isinstance(observed_fetch_extractions, (str, bytes)):
            raise ValueError("Observed fetch extractions must be a collection of labels")
        source_extractions.update(value for item in observed_fetch_extractions if (value := _string(item)) is not None)
    return {"case_id": case_id if case_id is not None else raw.get("case_id"),
            "report_id": raw.get("id"), "directory": str(directory), "report_sha256": report_sha,
            **deepcopy(configuration), "configuration": configuration, "configuration_conflicts": conflicts,
            "pdf_observed_views": observed_views, "pdf_observed_extractions": view_extractions,
            "pdf_evidence_policy_observed": web.get("pdf_evidence_policy"),
            "observed_fetch_extractions": sorted(source_extractions) if source_extractions or observed_fetch_extractions is not None else None}


def cohort_configuration(provenances, expected_ids):
    """Require the exact complete cohort and all known equal launch fields."""
    expected = list(expected_ids)
    if not expected or any(not isinstance(cid, str) or not cid for cid in expected) or len(set(expected)) != len(expected):
        raise ValueError("Expected case identities must be a nonempty unique list")
    if isinstance(provenances, Mapping):
        entries = {cid: value for cid, value in provenances.items() if value is not None}
    else:
        entries = {}
        for value in provenances:
            cid = value.get("case_id") if isinstance(value, Mapping) else None
            if not isinstance(cid, str) or not cid or cid in entries:
                raise ValueError("Each provenance needs a unique explicit case_id")
            entries[cid] = value
    if any(not isinstance(value, Mapping) for value in entries.values()):
        raise ValueError("Each saved provenance must be a mapping")
    complete_ids = [cid for cid in expected if cid in entries]
    pending = [cid for cid in expected if cid not in entries]
    unexpected = sorted(set(entries) - set(expected))
    complete = not pending and not unexpected
    unknown, groups, values_by_field = {}, {}, {field: set() for field in CONFIGURATION_FIELDS}
    configurations = {}
    for cid in complete_ids:
        entry = entries[cid]
        config = entry.get("configuration", entry)
        if not isinstance(config, Mapping):
            raise ValueError("Configuration must be a mapping")
        fingerprint = {field: deepcopy(config.get(field)) for field in CONFIGURATION_FIELDS}
        configurations[cid] = fingerprint
        missing = [field for field, value in fingerprint.items() if value is None]
        if entry.get("configuration_conflicts"):
            missing = sorted(set(missing) | set(entry["configuration_conflicts"]))
        if missing:
            unknown[cid] = missing
        serialized = json.dumps(fingerprint, sort_keys=True, ensure_ascii=False)
        groups.setdefault(serialized, []).append(cid)
        for field, value in fingerprint.items():
            values_by_field[field].add(json.dumps(value, sort_keys=True, ensure_ascii=False))
    uniform = complete and not unknown and len(groups) == 1
    return {"complete": complete, "expected_ids": expected, "complete_ids": complete_ids,
            "pending_ids": pending, "unexpected_ids": unexpected, "uniform": uniform,
            "uniform_configuration": deepcopy(configurations[complete_ids[0]]) if uniform else None,
            "unknown_fields_by_case": unknown,
            "differing_fields": [field for field, values in values_by_field.items() if len(values) > 1],
            "configuration_groups": [{"case_ids": ids, "configuration": json.loads(key)} for key, ids in groups.items()]}


def source_snapshot_signatures(raw):
    """Validate stored text hashes and single-parent supplemental identities."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("sources"), list):
        raise ValueError("A report must explicitly provide its source snapshots")
    result, sources = {}, {}
    for source in raw["sources"]:
        if not isinstance(source, Mapping):
            raise ValueError("Malformed source snapshot")
        identifier, url, text = source.get("id"), source.get("url"), source.get("text")
        if (not isinstance(identifier, str) or not identifier or identifier in result
                or not isinstance(url, str) or not url or not isinstance(text, str)):
            raise ValueError("Missing or duplicate source identity")
        provenance = source.get("provenance", {})
        if not isinstance(provenance, Mapping):
            raise ValueError("Malformed source provenance")
        stored = [value for value in (source.get("text_sha256"), provenance.get("text_sha256")) if value is not None]
        actual = sha256(text.encode()).hexdigest()
        if not stored or any(not isinstance(value, str) or value != actual for value in stored):
            raise ValueError("Missing or changed stored source text hash")
        parent = source.get("representation_of")
        if parent is not None and (not isinstance(parent, str) or not parent):
            raise ValueError("Malformed supplemental parent identity")
        result[identifier] = {"id": identifier, "url": url, "text_sha256": actual, "representation_of": parent}
        sources[identifier] = source
    for identifier, signature in result.items():
        parent_id = signature["representation_of"]
        if parent_id is None:
            continue
        parent = result.get(parent_id)
        if (parent is None or parent_id == identifier or parent["representation_of"] is not None
                or parent["url"] != signature["url"]):
            raise ValueError("Supplemental snapshot lacks the same-URL primary parent")
    return result
