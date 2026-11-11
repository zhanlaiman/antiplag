#!/usr/bin/env python3
"""Reserve exactly two official PDFs; never inspect prose or choose excerpts.

Direct fetching is explicit and journalled. The normal Fetcher enforces robots,
URL, byte, page, and extraction limits. No search API or model is available here.
"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import pdf_source
from plagiarism.web_fetch import Fetcher
from scripts.run_dev_query_probe import dump

OUT = ROOT / "data/private/web-evaluation/pdf-unfamiliar-reserved-v1"
URLS = (("RFC8259", "https://www.rfc-editor.org/rfc/pdfrfc/rfc8259.txt.pdf"),
        ("RFC2606", "https://www.rfc-editor.org/rfc/pdfrfc/rfc2606.txt.pdf"))
REPLACEMENT = ("RFC3339", "https://www.rfc-editor.org/rfc/pdfrfc/rfc3339.txt.pdf")


def digest(data):
    return sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-two-official", action="store_true")
    parser.add_argument("--fetch-authorized-rfc3339-replacement", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    OUT.mkdir(mode=0o700, exist_ok=True)
    manifest = OUT / "reservation.json"
    if manifest.exists():
        reservation = json.loads(manifest.read_text())
        if reservation["urls"] != [url for _, url in URLS]:
            raise ValueError("Frozen reservation URL list changed")
    else:
        reservation = {"schema_version": 1, "reserved_at": datetime.now(timezone.utc).isoformat(),
            "phase": "reserved_only", "urls": [url for _, url in URLS], "view_variant_selected": None,
            "prose_inspected": False, "quotes_selected": False, "search_queries": 0,
            "model_calls": 0, "paid_api_calls": 0, "maximum_direct_pdf_attempts": 2,
            "provenance_note": "Official English RFC publications, reserved outside Russian DEV and sealed-final families. Final texts/labels/results were never read.",
            "first_use_rule": "Choose and record the extraction-view variant before inspecting prose, constructing excerpts, or evaluating these PDFs.",
            "baseline_parser_version": pdf_source.PARSER_VERSION}
        dump(manifest, reservation)
    if args.fetch_authorized_rfc3339_replacement:
        reservation.setdefault("replacement_authorizations", [{"requested_url": REPLACEMENT[1],
            "reserved_at": datetime.now(timezone.utc).isoformat(),
            "basis": "Parent explicitly authorized one RFC3339 replacement after the RFC2606 PDF URL failed. No retry or URL guessing.",
            "excluded_failed_id": "RFC2606", "excluded_failed_body_seen_by_parent": True}])
        reservation["maximum_direct_pdf_attempts"] = 3
        reservation["holdout_pdf_ids"] = ["RFC8259", "RFC3339"]
        dump(manifest, reservation)
    rows = []
    selections = URLS + ((REPLACEMENT,) if args.fetch_authorized_rfc3339_replacement else ())
    for label, url in selections:
        attempt = OUT / (label + "-attempt.json")
        if attempt.exists():
            state = json.loads(attempt.read_text())
            rows.append({"id": label, "state": state["state"], "previous_attempt_preserved": True})
            continue
        if not (args.fetch_two_official or label == "RFC3339" and args.fetch_authorized_rfc3339_replacement):
            rows.append({"id": label, "state": "reserved_not_fetched"})
            continue
        dump(attempt, {"state": "submitted", "requested_url": url,
                       "at": datetime.now(timezone.utc).isoformat()})
        original = pdf_source.extract_pdf
        def capture(data, *positional, **options):
            if len(data) > pdf_source.MAX_PDF_BYTES or not data.startswith(b"%PDF-"):
                raise ValueError("Normal PDF boundary did not hold")
            raw = OUT / (label + ".pdf")
            raw.write_bytes(data); raw.chmod(0o600)
            dump(OUT / (label + "-raw.json"), {"requested_url": url, "path": raw.name,
                "sha256": digest(data), "bytes": len(data), "captured_at": datetime.now(timezone.utc).isoformat()})
            baseline = original(data, *positional, **options)
            dump(OUT / (label + "-baseline-worker.json"), baseline)
            return baseline
        try:
            with patch.object(pdf_source, "extract_pdf", capture):
                fetched = Fetcher().fetch(url)
            if fetched.get("content_type") != "application/pdf" or not (OUT / (label + ".pdf")).exists():
                raise ValueError("Expected a bounded PDF, not another response type")
            dump(OUT / (label + "-baseline-fetch.json"), fetched)
            plain = OUT / (label + "-plain.txt")
            plain.write_text(fetched["text"]); plain.chmod(0o600)
            raw = json.loads((OUT / (label + "-raw.json")).read_text())
            record = {"state": "complete", "requested_url": url, "final_url": fetched["url"],
                "fetched_at": fetched["fetched_at"], "robots_url": fetched["robots_url"],
                "raw_sha256": raw["sha256"], "raw_bytes": raw["bytes"],
                "plain_path": plain.name, "plain_sha256": digest(fetched["text"]),
                "plain_characters": len(fetched["text"]), "pdf_pages": fetched.get("pdf_pages"),
                "extraction": fetched["extraction"], "parser_version": fetched.get("pdf_parser_version")}
            dump(attempt, record)
            rows.append({"id": label, **record})
        except Exception as error:
            record = {"state": "failed_or_unknown", "requested_url": url,
                      "code": getattr(error, "code", type(error).__name__),
                      "http_status": getattr(error, "details", {}).get("status") if isinstance(getattr(error, "details", None), dict) else None,
                      "raw_captured": (OUT / (label + ".pdf")).exists()}
            dump(attempt, record)
            rows.append({"id": label, **record})
    # Metadata only: neither a PDF page nor any extracted prose is printed.
    dump(OUT / "metadata-summary.json", {"reserved_at": reservation["reserved_at"], "prose_inspected": False,
        "quotes_selected": False, "search_queries": 0, "cases": rows})
    print(json.dumps({"directory": str(OUT), "reserved_at": reservation["reserved_at"], "cases": rows}, indent=2))


if __name__ == "__main__":
    main()
