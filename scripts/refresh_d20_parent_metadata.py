#!/usr/bin/env python3
"""One explicitly authorized robots-aware D20 parent refresh; no search API."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.web_fetch import Fetcher
from scripts.audit_dev_fetch_balance import BASE, OUT
from scripts.replay_saved_dev_search import PAGE_CACHE, canonical
from scripts.run_dev_query_probe import dump


def main():
    origin = BASE / "dev-dense-balanced-live-v2/D20.json"
    report = json.loads(origin.read_text())
    parent = next(link["parent_url"] for link in report["web"]["link_discoveries"]
                  if "Цифровая_фотография" in unquote(link["parent_url"]))
    url = canonical(parent)
    log = OUT / "D20-parent-refresh.json"
    if log.exists():
        saved = json.loads(log.read_text())
        print("Existing refresh outcome retained; no repeat load:", saved["state"])
        return
    old = next(source for source in report["sources"] if canonical(source["url"]) == url)
    dump(log, {"state": "submitted", "requested_url": url, "new_searches": 0,
        "authorization": "One direct parent refresh authorized by root; preserve frozen search replies",
        "origin_report": str(origin.relative_to(ROOT)), "origin_sha256": sha256(origin.read_bytes()).hexdigest(),
        "submitted_at": datetime.now(timezone.utc).isoformat()})
    try:
        fetched = Fetcher().fetch(url)
        timestamp = datetime.now(timezone.utc).isoformat()
        result = {"state": "complete", "requested_url": url, "fetched": fetched, "saved_at": timestamp,
            "new_searches": 0, "new_direct_fetches": 1, "historical_search_unchanged": True,
            "previous_text_sha256": sha256(old["text"].encode()).hexdigest(),
            "new_text_sha256": sha256(fetched["text"].encode()).hexdigest(),
            "text_equal_to_historical": old["text"] == fetched["text"]}
        dump(log, result)
        PAGE_CACHE.mkdir(mode=0o700, exist_ok=True)
        dump(PAGE_CACHE / (sha256(url.encode()).hexdigest() + ".json"),
             {"requested_url": url, "state": "complete", "fetched": fetched, "saved_at": timestamp,
              "purpose": "Authorized D20 parent-link metadata recovery; a fresh page snapshot, not a fresh search reply"})
        print("One parent refreshed; text equal:", result["text_equal_to_historical"], "links", len(fetched["links"]))
    except Exception as error:
        dump(log, {"state": "failed_or_unknown", "requested_url": url, "new_searches": 0,
                   "code": getattr(error, "code", type(error).__name__), "retry_allowed": False})
        print("Refresh failed; no automatic retry:", getattr(error, "code", type(error).__name__))


if __name__ == "__main__":
    main()
