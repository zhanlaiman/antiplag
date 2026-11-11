#!/usr/bin/env python3
"""Ablate production URL/PDF/reference features on frozen DEV30 data only.

No provider or Fetcher is instantiated. Searches are exact saved per-query
replies; pages are saved snapshots. PDF views attach only to an identical
primary text, and input-reference discovery uses the production input parser.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import web_search
from plagiarism.input_references import extract_named_references
from plagiarism.web_evaluation import _url
from scripts.analyze_web_dev_checks import inspect
from scripts.audit_dev_fetch_balance import BASE, DEV, RUNS, OUT as FETCH_OUT, sources_cache
from scripts.replay_saved_dev_search import CachedPages, FrozenSearch, PAGE_CACHE, annotate_replay, canonical, load_queries
from scripts.run_dev_query_probe import dump

OUT = BASE / "dev-final-feature-ablations-v1"
PDF_DIR = BASE / "pdf-production-validation-v1"
NAMED_DIR = BASE / "dev-named-reference-v1"
POLICIES = (("path_productive_primary", False, False),
            ("plus_saved_pdf_views", True, False),
            ("plus_named_references", True, True))
CODE_PATHS = ("plagiarism/web_search.py", "plagiarism/matching.py", "plagiarism/pdf_evidence.py",
              "plagiarism/input_references.py", "scripts/replay_final_feature_ablations.py")


def text_digest(text):
    return sha256(text.encode()).hexdigest()


def validate_pdf_payload(page):
    """Validate evidence identity without weakening any production checks."""
    CachedPages._validate_page(page)
    raw = page.get("raw_pdf_sha256")
    values = page.get("supplemental_texts")
    if (page.get("content_type") != "application/pdf" or not isinstance(raw, str)
            or len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw)
            or type(page.get("pdf_pages")) is not int or not 1 <= page["pdf_pages"] <= 40
            or not isinstance(values, list) or len(values) != 1):
        raise ValueError("Malformed saved PDF evidence payload")
    value = values[0]
    if (not isinstance(value, dict) or value.get("id") != "pdfium-v1" or value.get("extraction") != "pdfium-text-v1"
            or value.get("parser_version") != "5.14.0" or value.get("raw_pdf_sha256") != raw
            or value.get("page_count") != page["pdf_pages"] or not isinstance(value.get("text"), str)
            or value.get("text_sha256") != text_digest(value["text"])
            or page.get("text_sha256") != text_digest(page["text"])):
        raise ValueError("Saved PDF view identity changed")
    return canonical(page["url"])


def load_pdf_payloads():
    pages, origins = {}, []
    for cid in ("D06", "D16", "D20"):
        path = PDF_DIR / (cid + "-fetched.json")
        page = json.loads(path.read_text())
        url = validate_pdf_payload(page)
        if url in pages:
            raise ValueError("Duplicate saved PDF URL")
        pages[url] = page
        origins.append({"path": str(path.relative_to(ROOT)), "file_sha256": sha256(path.read_bytes()).hexdigest(),
            "url": url, "primary_text_sha256": text_digest(page["text"]),
            "raw_pdf_sha256": page["raw_pdf_sha256"],
            "view_text_sha256": page["supplemental_texts"][0]["text_sha256"]})
    return pages, origins


def load_named_payload(case, text):
    """The sole added page comes from a previously authorized normal fetch."""
    if case.get("id") != "D14" or text_digest(text) != case.get("text_sha256"):
        raise ValueError("Named page requires the exact validated DEV input")
    path = NAMED_DIR / "D14-fetch-response.json"
    saved = json.loads(path.read_text())
    page = saved.get("response")
    hints = extract_named_references(text)
    if (case["id"] != "D14" or saved.get("state") != "complete" or saved.get("fetch_invocations") != 1
            or saved.get("input_text_sha256") != case["text_sha256"]
            or len(hints) != 1 or canonical(saved.get("requested_url")) != canonical(hints[0]["url"])):
        raise ValueError("Named page is not the proven input-derived DEV fetch")
    CachedPages._validate_page(page)
    digest = text_digest(page["text"])
    if canonical(page["url"]) != canonical(hints[0]["url"]) or digest != "9c889daf3404df5aa9555e89ef5687cc6e07cd914eb1f290a0d6cbbde3e91659":
        raise ValueError("Changed named-reference source snapshot")
    return page, {"path": str(path.relative_to(ROOT)), "file_sha256": sha256(path.read_bytes()).hexdigest(),
        "url": page["url"], "text_sha256": digest, "fetched_at": page.get("fetched_at"),
        "claim": "Input-derived source hint; agreement is established by matching, not the citation."}


class FeaturePages(CachedPages):
    def __init__(self, *args, pdf_payloads, enable_pdf, **kwargs):
        super().__init__(*args, **kwargs)
        self.pdf_payloads, self.enable_pdf = pdf_payloads, enable_pdf
        self.applied_views, self.validation_errors = [], []

    def fetch(self, url):
        page = super().fetch(url)
        # The same primary payload is used in all variants. No existing cache
        # supplement may leak into the primary-only ablation.
        page.pop("supplemental_texts", None)
        page.pop("optional_status", None)
        saved = self.pdf_payloads.get(canonical(page["url"]))
        if self.enable_pdf and saved is not None:
            try:
                validate_pdf_payload(saved)
            except (ValueError, UnicodeError):
                self.validation_errors.append(canonical(page["url"]))
                raise
            if text_digest(page["text"]) != saved["text_sha256"]:
                self.validation_errors.append(canonical(page["url"]))
                raise ValueError("Saved PDF view does not belong to this primary text")
            for key in ("content_type", "raw_pdf_sha256", "pdf_pages", "supplemental_texts", "optional_status"):
                page[key] = deepcopy(saved[key])
            self.applied_views.append({"requested_url": url, "url": page["url"],
                "primary_text_sha256": saved["text_sha256"],
                "view_text_sha256": saved["supplemental_texts"][0]["text_sha256"]})
        return page


def match_key(match):
    return (match["kind"], _url(match["source_url"]), match["start"], match["end"],
            match["source_start"], match["source_end"], match["source_excerpt"])


def match_excerpt(match):
    return {key: match.get(key) for key in ("kind", "source_url", "start", "end", "source_start", "source_end",
                                            "excerpt", "source_excerpt", "quoted", "pdf_spacing_evidence")}


def main():
    for policy, _, _ in POLICIES:
        (OUT / policy).mkdir(parents=True, exist_ok=True, mode=0o700)
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only frozen DEV30 is allowed")
    by_id = {case["id"]: case for case in cases}
    texts = {case["id"]: (DEV / case["input_path"]).read_text() for case in cases}
    if any(text_digest(texts[case["id"]]) != case["text_sha256"] for case in cases):
        raise ValueError("Changed DEV input")
    comparison = json.loads((FETCH_OUT / "comparison.json").read_text())
    latest = {}
    for row in comparison["cases"]:
        if row["status"] == "ready" and (row["id"] not in latest or row["created_at"] > latest[row["id"]]["created_at"]):
            latest[row["id"]] = row
    if set(latest) != set(by_id) or any(row["run"] not in RUNS for row in latest.values()):
        raise ValueError("Expected the same thirty captured DEV searches")
    historical = json.loads((FETCH_OUT / "dynamic-productive-budget-comparison.json").read_text())
    historical_words = {row["id"]: row["policies"]["path_productive_budget"]["matched_words"] for row in historical["cases"]}
    directories = [BASE / name for name in RUNS if (BASE / name / "run.json").exists()]
    pool = {url: item["page"] for url, item in sources_cache([BASE / "dev-live-30-v1"] + directories).items()}
    pdf_pages, pdf_origins = load_pdf_payloads()
    named_page, named_origin = load_named_payload(by_id["D14"], texts["D14"])
    code_hashes = {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in CODE_PATHS}
    denied = []
    def no_network(event, arguments):
        if event.startswith("socket."):
            denied.append(event)
            raise PermissionError("Final feature ablation forbids all network operations")
    sys.addaudithook(no_network)
    rows, reports = [], {name: {} for name, _, _ in POLICIES}
    for case in cases:
        cid, text = case["id"], texts[case["id"]]
        row = latest[cid]
        records, query_identity = load_queries(BASE / row["run"], cid, case["text_sha256"])
        item = {"id": cid, "source_run": row["run"], "policies": {}}
        for policy, pdf_enabled, named_enabled in POLICIES:
            search = FrozenSearch(records)
            hints = extract_named_references(text) if named_enabled else []
            allowed = search.allowed_urls | {canonical(hint["url"]) for hint in hints}
            pages = FeaturePages(deepcopy(pool), allowed, cache_directory=PAGE_CACHE,
                fill_missing=False, refresh_missing_links=False, pdf_payloads=pdf_pages, enable_pdf=pdf_enabled)
            # Cache availability is identical across variants. Only the
            # production input-derived discovery flag changes eligibility.
            pages.pool[canonical(named_page["url"])] = deepcopy(named_page)
            identifier = sha256(("final-feature-ablation-v1\0" + cid + "\0" + policy).encode()).hexdigest()[:32]
            report = web_search.run(text, request_id=identifier, searcher=search, fetcher=pages,
                query_strategy=web_search.DENSE_STRATEGY, url_strategy=web_search.PATH_URL_STRATEGY,
                use_named_references=named_enabled)
            if denied or pages.fresh_attempts or search.missing_queries or pages.validation_errors:
                raise ValueError("Frozen-data guarantee violated")
            if report["web"]["urls_attempted"] > 12 or report["web"]["queries_attempted"] > 6:
                raise ValueError("Production physical limits exceeded")
            if policy == POLICIES[0][0] and report["stats"]["matched_words"] != historical_words[cid]:
                raise ValueError("Production primary-only replay changed for " + cid)
            report.update(id=identifier, created_at=datetime.now(timezone.utc).isoformat())
            annotate_replay(report, search, pages, {"case_id": cid, "policy": policy,
                "source_run": row["run"], "query_identity": query_identity, "new_searches": 0,
                "main_store_written": False, "applied_saved_pdf_views": pages.applied_views,
                "named_source_snapshot": named_origin if named_enabled else None})
            dump(OUT / policy / (cid + ".json"), report)
            reports[policy][cid] = report
            item["policies"][policy] = {"matched_words": report["stats"]["matched_words"],
                "physical_load_attempts": report["web"]["urls_attempted"],
                "physical_sources_read": report["web"]["sources_checked"],
                "supplemental_views": report["web"]["supplemental_text_views"],
                "input_reference_urls_discovered": report["web"]["input_reference_urls_discovered"],
                "replay_metadata_complete": report["offline_replay"]["replay_metadata_complete"],
                "page_cache_misses": pages.cache_misses, "missing_link_metadata": sorted(pages.missing_links)}
        rows.append(item)
        print(cid, {name: value["matched_words"] for name, value in item["policies"].items()}, flush=True)
    summaries, deltas = {}, []
    for policy, _, _ in POLICIES:
        analyzed = inspect(DEV, OUT / policy)
        dump(OUT / policy / "analysis.json", analyzed)
        raw = [reports[policy][cid] for cid in by_id]
        summaries[policy] = {**analyzed["summary"],
            "physical_load_attempts": sum(r["web"]["urls_attempted"] for r in raw),
            "physical_sources_read": sum(r["web"]["sources_checked"] for r in raw),
            "supplemental_views": sum(r["web"]["supplemental_text_views"] for r in raw),
            "documents_with_supplemental_views": sum(bool(r["web"]["supplemental_text_views"]) for r in raw),
            "replay_metadata_complete_documents": sum(r["offline_replay"]["replay_metadata_complete"] for r in raw),
            "page_cache_miss_attempts": sum(len(r["offline_replay"]["page_cache_misses"]) for r in raw),
            "documents_with_missing_link_metadata": sum(bool(r["offline_replay"]["missing_link_metadata"]) for r in raw),
            "frozen_query_calls": sum(r["web"]["queries_attempted"] for r in raw)}
    for before, after in zip(POLICIES, POLICIES[1:]):
        changed = []
        for cid in by_id:
            a, b = reports[before[0]][cid], reports[after[0]][cid]
            a_matches, b_matches = {match_key(m): m for m in a["matches"]}, {match_key(m): m for m in b["matches"]}
            added, removed = b_matches.keys() - a_matches.keys(), a_matches.keys() - b_matches.keys()
            read_a = {_url(s["url"]) for s in a["sources"] if not s.get("representation_of")}
            read_b = {_url(s["url"]) for s in b["sources"] if not s.get("representation_of")}
            if added or removed or read_a != read_b:
                changed.append({"id": cid, "matched_words_before": a["stats"]["matched_words"],
                    "matched_words_after": b["stats"]["matched_words"], "lost_physical_source_urls": sorted(read_a - read_b),
                    "added_physical_source_urls": sorted(read_b - read_a),
                    "added_matches": [match_excerpt(b_matches[key]) for key in sorted(added)],
                    "removed_matches": [match_excerpt(a_matches[key]) for key in sorted(removed)]})
        deltas.append({"before": before[0], "after": after[0], "changed_cases": changed})
    if code_hashes != {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in CODE_PATHS}:
        raise ValueError("Production code changed during replay; rerun for an unambiguous version")
    result = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "code_sha256": code_hashes, "documents": 30, "new_search_queries": 0, "new_fetches": 0,
        "final_materials_read": False, "main_store_written": False,
        "scope": "Normal production run; same captured DEV queries/primary pages/real link metadata. Unknown pages/links remain partial.",
        "pdf_origins": pdf_origins, "named_reference_origin": named_origin,
        "summaries": summaries, "incremental_match_and_source_deltas": deltas, "cases": rows}
    dump(OUT / "comparison.json", result)
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
