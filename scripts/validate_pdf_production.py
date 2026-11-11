#!/usr/bin/env python3
"""Offline production PDF integration, then one frozen RFC8259 first-use check."""
import argparse
from datetime import datetime, timezone
from email.message import Message
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import pdf_source, web_fetch, web_search
from plagiarism.matching import _tokens, analyze
from plagiarism.pdf_evidence import POLICY, analyze_with_supplements
from plagiarism.web_evaluation import _report
from scripts.audit_pdfium_views import MATH_ADVERSARIAL
from scripts.probe_pdf_evidence_views import BOUNDARY_PROBES
from scripts.probe_pdf_layout import NEGATIVE
from scripts.run_dev_query_probe import dump

PRIVATE = ROOT / "data/private/web-evaluation"
KNOWN = PRIVATE / "pdf-layout-probe-v1"
OUT = PRIVATE / "pdf-production-validation-v1"
RESERVED = PRIVATE / "pdf-unfamiliar-reserved-v1"
FREEZE = ROOT / "docs/baselines/night-pdf-choice-2026-11-12.json"
DEFAULT_PYTHON = "/Users/air/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"


def digest(data):
    return sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def extract_without_network(data, url, python):
    """Run the actual Fetcher transport with only its socket boundary stubbed."""
    fetcher, headers = web_fetch.Fetcher(), Message()
    headers["Content-Type"] = "application/pdf"
    started = perf_counter()
    with patch.dict(os.environ, {"ISTOK_PDF_ALTERNATIVE_ENGINE": "pdfium", "ISTOK_PDFIUM_PYTHON": python}), \
            patch.object(fetcher, "_robots", return_value=True), \
            patch.object(fetcher, "_request", return_value=(200, headers, data, len(data))):
        fetched = fetcher.fetch(url)
    source = {"id": "web-" + digest(fetched["url"])[:24], "title": fetched["title"], "url": fetched["url"],
              "text": fetched["text"], "provenance": {"origin": "frozen_pdf_bytes",
                  "text_sha256": fetched["text_sha256"], "raw_pdf_sha256": digest(data),
                  "pdf_pages": fetched["pdf_pages"]}}
    supplements = web_search._supplemental_source_views(source, fetched)
    return fetched, source, supplements, round(perf_counter() - started, 4)


def score(target, primary, supplements, bad_word=None):
    before = analyze(target, [primary])
    after = analyze_with_supplements(target, [primary], supplements)
    tokens = _tokens(target)
    def describe(result):
        exact = [m for m in result["matches"] if m["kind"] == "exact"]
        covered = {t.start for t in tokens if any(m["start"] <= t.start and t.end <= m["end"] for m in exact)}
        return {"words": len(tokens), "covered_words": len(covered), "matches": result["matches"],
                "all_matches": len(result["matches"]), "source_count": result["stats"]["source_count"],
                "checked_source_count": result["stats"]["checked_source_count"],
                "bad_word_highlighted": any(t.start in covered for t in tokens if bad_word and t.value == bad_word)}
    report = {**after, "text": target, "scope": "web_search", "sources": [primary] + supplements,
              "web": {"status": "complete", "errors": [], "discovered_urls": [primary["url"]]}}
    proof = _report(report, {"id": "offline-pdf-control", "text_sha256": digest(target)})
    return {"target": target, "target_sha256": digest(target), "plain": describe(before), "production": describe(after),
            "invalid_highlights": proof["invalid_highlights"]}


def code_hashes():
    return {name: digest((ROOT / name).read_bytes()) for name in
            ("plagiarism/pdf_source.py", "plagiarism/pdf_evidence.py", "plagiarism/web_search.py",
             "plagiarism/web_evaluation.py", "plagiarism/web_fetch.py")}


def parser_builds(python):
    code = "import json; from pypdfium2.version import PYPDFIUM_INFO, PDFIUM_INFO; print(json.dumps({'pypdfium2':str(PYPDFIUM_INFO),'native_pdfium':str(PDFIUM_INFO)}))"
    completed = subprocess.run([python, "-I", "-c", code], capture_output=True, text=True, timeout=3, check=True,
        env={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    return {"primary_pypdf": "7.0.1", **json.loads(completed.stdout)}


def known_validation(python):
    OUT.mkdir(mode=0o700, exist_ok=True)
    comparison = json.loads((KNOWN / "comparison.json").read_text())
    prior = {r["label"]: r for r in json.loads((KNOWN / "pdfium-filtered-union-audit.json").read_text())["cases"]}
    dev = PRIVATE / "materials/dev-30"
    cases = json.loads((dev / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only frozen DEV is accepted")
    rows, all_controls = [], []
    for entry in comparison["cases"]:
        label, raw = entry["label"], entry["raw"]
        if label not in ("D06", "D20", "D16"):
            raise ValueError("Unexpected known PDF")
        path = (KNOWN / raw["path"]).resolve()
        if not path.is_relative_to(KNOWN.resolve()):
            raise ValueError("Unexpected PDF path")
        data = path.read_bytes()
        if digest(data) != raw["sha256"]:
            raise ValueError("Frozen raw PDF changed")
        fetched, primary, supplements, elapsed = extract_without_network(data, raw["requested_url"], python)
        saved_plain = json.loads((KNOWN / (label + "-plain.json")).read_text())["normalized_text"]
        saved_other = json.loads((KNOWN / (label + "-pdfium.json")).read_text())["normalized_text"]
        if (primary["text"] != saved_plain or len(supplements) != 1
                or supplements[0]["text"] != saved_other or fetched["optional_status"]["status"] != "complete"):
            raise ValueError("Production extraction differs from the selected saved views")
        dump(OUT / (label + "-fetched.json"), fetched)
        controls = [score(c["text"], primary, supplements) for c in entry["literal_controls"]]
        all_controls.extend(controls)
        case = next(c for c in cases if c["id"] == label)
        target = (dev / case["input_path"]).read_text()
        if digest(target) != case["text_sha256"]:
            raise ValueError("Known DEV text changed")
        actual = score(target, primary, supplements)
        negative = score(NEGATIVE, primary, supplements)
        row = {"id": label, "raw_sha256": digest(data), "source_url": raw["requested_url"],
            "extraction_seconds": elapsed, "optional_status": fetched["optional_status"],
            "primary_text_sha256": digest(primary["text"]), "supplement_text_sha256": digest(supplements[0]["text"]),
            "actual_case": actual, "literal_controls": controls, "independent_negative": negative}
        assert actual["production"]["covered_words"] == prior[label]["actual_case"]["filtered_union"]["covered_words"]
        assert not negative["production"]["matches"]
        if label == "D06":
            row["expected_quote"] = score(case["expected_evidence"][0]["target_quote"], primary, supplements)
            assert row["expected_quote"]["production"]["covered_words"] == 8
            row["math_adversarial"] = []
            for probe, old in zip(MATH_ADVERSARIAL, prior[label]["math_adversarial_controls"]):
                measured = score(probe["text"], primary, supplements)
                assert measured["production"]["covered_words"] == old["result"]["filtered_union"]["covered_words"]
                row["math_adversarial"].append({"id": probe["id"], "result": measured})
        if label == "D16":
            visual = json.loads((KNOWN / "visual-control.json").read_text())
            row["visual_positive"] = score(visual["text"], primary, supplements)
            assert row["visual_positive"]["production"]["covered_words"] == 8
            row["layout_adversarial"] = []
            for probe, old in zip(BOUNDARY_PROBES, prior[label]["visual_adversarial_controls"]):
                measured = score(probe["text"], primary, supplements, probe["bad_word"])
                assert measured["production"]["covered_words"] == old["result"]["filtered_union"]["covered_words"]
                assert not measured["production"]["bad_word_highlighted"]
                row["layout_adversarial"].append({"id": probe["id"], "result": measured})
        assert all(not c["invalid_highlights"] for c in controls + [actual, negative])
        rows.append(row)
        print(label, "actual", actual["plain"]["covered_words"], "->", actual["production"]["covered_words"],
              "controls", len(controls), "seconds", elapsed, flush=True)
    assert len(all_controls) == 9
    assert all(c["production"]["covered_words"] >= c["plain"]["covered_words"] for c in all_controls)
    result = {"schema_version": 1, "status": "passed", "checked_at": datetime.now(timezone.utc).isoformat(),
        "policy": POLICY, "source_code_sha256": code_hashes(), "new_fetches": 0, "search_queries": 0,
        "parser_builds": parser_builds(python),
        "models_called": 0, "pdf_documents": 3, "literal_controls": 9,
        "literal_controls_retained": 9, "independent_control_source_pairs": 3,
        "independent_controls_with_highlights": 0, "layout_adversarial": 3, "math_adversarial": 2,
        "adversarial_new_source_unfaithful_highlights": 0, "cases": rows}
    dump(OUT / "known-integration.json", result)
    print("Known production integration passed", flush=True)


def first_use(python):
    known = json.loads((OUT / "known-integration.json").read_text())
    if known["status"] != "passed":
        raise ValueError("Complete the known integration first")
    frozen = json.loads(FREEZE.read_text())
    if frozen["policy"] != POLICY or frozen["implementation_file_sha256"] != code_hashes():
        raise ValueError("Policy/source hashes differ from the frozen first-use choice")
    attempt = OUT / "RFC8259-first-use-attempt.json"
    if attempt.exists():
        raise ValueError("First-use attempt is already recorded; never silently rerun")
    plan = frozen["reserved_first_use_plan"]
    dump(attempt, {"state": "started", "at": datetime.now(timezone.utc).isoformat(),
        "freeze_sha256": digest(FREEZE.read_bytes()), "policy": POLICY,
        "selection_formula": "min(floor(number_of_primary_tokens*fraction), number_of_primary_tokens-length)",
        "quote_lengths_words": plan["quote_lengths_words"], "positions_fraction": plan["positions_fraction"]})
    data = (RESERVED / "RFC8259.pdf").read_bytes()
    if digest(data) != frozen["reserved_pdf_sha256"]:
        raise ValueError("Reserved PDF changed")
    metadata = json.loads((RESERVED / "RFC8259-attempt.json").read_text())
    fetched, primary, supplements, elapsed = extract_without_network(data, metadata["requested_url"], python)
    if digest(primary["text"]) != metadata["plain_sha256"] or len(supplements) != 1:
        raise ValueError("Reserved primary extraction changed or optional view unavailable")
    dump(OUT / "RFC8259-fetched.json", fetched)
    tokens = _tokens(primary["text"])
    quotes = []
    for length in plan["quote_lengths_words"]:
        for fraction in plan["positions_fraction"]:
            start = min(int(len(tokens) * fraction), len(tokens) - length)
            a, b = tokens[start].start, tokens[start + length - 1].end
            target = primary["text"][a:b]
            quotes.append({"length_words": length, "position_fraction": fraction, "primary_span": [a, b],
                           "result": score(target, primary, supplements)})
    controls = [score(text, primary, supplements) for text in plan["independent_controls"]]
    result = {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(), "first_use": True,
        "used_for_tuning": False, "policy": POLICY, "freeze_sha256": digest(FREEZE.read_bytes()),
        "source_code_sha256": code_hashes(), "raw_pdf_sha256": digest(data), "source_url": metadata["requested_url"],
        "source_pdf_documents": 1, "dependent_artificial_quotes": len(quotes), "independent_controls": len(controls),
        "search_queries": 0, "new_fetches": 0, "models_called": 0, "extraction_seconds": elapsed,
        "primary_text_sha256": digest(primary["text"]), "supplement_text_sha256": digest(supplements[0]["text"]),
        "parser_versions": parser_builds(python), "quotes": quotes, "controls": controls,
        "plain_copied_words": sum(q["result"]["plain"]["covered_words"] for q in quotes),
        "production_copied_words": sum(q["result"]["production"]["covered_words"] for q in quotes),
        "expected_words": sum(q["length_words"] for q in quotes),
        "controls_with_highlights": sum(bool(c["production"]["matches"]) for c in controls),
        "invalid_highlights": sum(len(c["result"]["invalid_highlights"]) for c in quotes) + sum(len(c["invalid_highlights"]) for c in controls),
        "interpretation": "One previously closed PDF, nine dependent literal quotes from its primary extraction and two fixed authored controls; retention check, not a general accuracy or unseen-recall estimate."}
    dump(OUT / "RFC8259-first-use.json", result)
    dump(attempt, {"state": "complete", "at": result["checked_at"], "result": "RFC8259-first-use.json",
                   "freeze_sha256": result["freeze_sha256"], "raw_sha256": result["raw_pdf_sha256"]})
    print(json.dumps({key: result[key] for key in ("source_pdf_documents", "dependent_artificial_quotes", "independent_controls",
        "plain_copied_words", "production_copied_words", "expected_words", "controls_with_highlights", "invalid_highlights", "extraction_seconds")}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first-use-rfc8259", action="store_true")
    parser.add_argument("--pdfium-python", default=DEFAULT_PYTHON)
    args = parser.parse_args()
    os.umask(0o077)
    def no_network(event, arguments):
        if event.startswith("socket."):
            raise PermissionError("PDF integration has no network")
    sys.addaudithook(no_network)
    first_use(args.pdfium_python) if args.first_use_rfc8259 else known_validation(args.pdfium_python)


if __name__ == "__main__":
    main()
