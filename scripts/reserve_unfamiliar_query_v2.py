#!/usr/bin/env python3
"""Reserve two fixed publications without inspecting prose or selecting quotes.

Only DEV source-pool URL metadata is checked. No other collection is opened.
The normal Fetcher handles robots/redirects and all normal byte/time limits.
Submitted/failed attempts are never retried; no replacement URLs are allowed.
"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.web_fetch import Fetcher

OUT = ROOT / "data/private/web-evaluation/query-unfamiliar-reserved-v2"
POOL = ROOT / "data/private/web-evaluation/materials/dev-source-pool.json"
URLS = (("RFC9309", "https://www.rfc-editor.org/rfc/rfc9309.txt"),
        ("CSSOverview", "https://www.w3.org/Style/CSS/Overview.en.html"))


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def write_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(raw)
    temporary.chmod(0o600)
    temporary.replace(path)
    return digest(raw)


def url_identity(url):
    parsed = urlsplit(url)
    return (parsed.hostname.lower().removeprefix("www."), unquote(parsed.path).rstrip("/"), parsed.query)


def pool_urls(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from pool_urls(child)
    elif isinstance(value, list):
        for child in value:
            yield from pool_urls(child)
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        # URL inventory only: no prose tokenization, ranking, or excerpts.
        yield value


class RecordingFetcher(Fetcher):
    def __init__(self, label, directory):
        super().__init__()
        self.label, self.directory, self.sequence = label, directory, 0

    def _request(self, target, overall_deadline, allow_pdf=False):
        self.sequence += 1
        stem = self.label + "-http-" + str(self.sequence).zfill(2)
        attempt = self.directory / (stem + "-attempt.json")
        if attempt.exists():
            raise RuntimeError("A transport attempt already exists; no retry")
        record = {"state": "submitted", "url": target["url"], "started_at": now()}
        write_json(attempt, record)
        try:
            status, headers, body, wire = super()._request(target, overall_deadline, allow_pdf=allow_pdf)
            raw_path = self.directory / (stem + ".bin")
            raw_path.write_bytes(body)
            raw_path.chmod(0o600)
            record.update(state="complete", completed_at=now(), status=status, headers=headers,
                body_path=raw_path.name, body_sha256=digest(body), decoded_bytes=len(body), wire_bytes=wire)
            write_json(attempt, record)
            return status, headers, body, wire
        except Exception as error:
            record.update(state="failed_or_unknown", completed_at=now(), code=getattr(error, "code", type(error).__name__))
            write_json(attempt, record)
            raise


def attach_transport_metadata(record, label, directory):
    """Use saved transport metadata only; never decode/open document prose."""
    paths = sorted(directory.glob(label + "-http-*-attempt.json"))
    if not paths:
        return record
    path = paths[-1]
    response = json.loads(path.read_text())
    if response.get("state") != "complete":
        return record
    record = {**record, "last_http_response_artifact": path.name,
        "last_http_response_sha256": digest(path.read_bytes()), "http_status": response["status"],
        "raw_body_path": response["body_path"], "raw_body_sha256": response["body_sha256"]}
    if record["state"] != "complete":
        record.update(bytes_read=response["wire_bytes"], decoded_bytes=response["decoded_bytes"],
                      text_characters=None, text_sha256=None)
    return record


def reserve(*, fetch=False, directory=OUT, pool=POOL, fetcher_factory=RecordingFetcher, urls=URLS):
    pool_raw = pool.read_bytes()
    known = {url_identity(u) for u in pool_urls(json.loads(pool_raw))}
    if any(url_identity(url) in known for _, url in urls):
        raise ValueError("A fixed URL already exists in DEV source-pool; reservation stopped without fetching")
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    manifest_path = directory / "reservation.json"
    config = {"urls": [url for _, url in urls], "dev_source_pool_sha256": digest(pool_raw)}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if any(manifest.get(k) != v for k, v in config.items()):
            raise ValueError("Frozen URL list or checked DEV source-pool changed")
    else:
        manifest = {"schema_version": 1, **config, "reserved_at": now(), "phase": "reserved_only",
            "dev_source_pool_url_absence_confirmed": True, "checked_collection": "DEV source-pool only",
            "query_variant_selected": None, "prose_inspected": False, "excerpts_selected": False,
            "query_plans_built": False, "maximum_source_attempts": len(urls), "search_queries": 0, "model_calls": 0,
            "paid_api_calls": 0, "money_spent_kzt": 0, "final_materials_read": False,
            "first_use_rule": "Do not inspect prose, choose excerpts, or build query plans until the parent records the final variant.",
            "provenance_note": "Fixed publications named in this reservation. Only URL absence in DEV source-pool was checked; no other corpus or sealed-final data was inspected."}
        write_json(manifest_path, manifest)
    rows = []
    for label, url in urls:
        path = directory / (label + "-attempt.json")
        if path.exists():
            record = json.loads(path.read_text())
            if record.get("requested_url") != url:
                raise ValueError("Saved attempt identity changed; no new GET")
            record = attach_transport_metadata(record, label, directory)
            write_json(path, record)
            rows.append({"id": label, **record, "previous_attempt_preserved": True})
            continue
        if not fetch:
            rows.append({"id": label, "state": "reserved_not_fetched"})
            continue
        record = {"state": "submitted", "requested_url": url, "started_at": now()}
        write_json(path, record)  # Before Fetcher can perform any GET, including robots.
        try:
            fetched = fetcher_factory(label, directory).fetch(url)
            if not isinstance(fetched, dict) or not isinstance(fetched.get("text"), str):
                raise ValueError("Invalid normal Fetcher response")
            text_hash = digest(fetched["text"])
            if fetched.get("text_sha256") != text_hash:
                raise ValueError("Fetched text hash mismatch")
            response_path = directory / (label + "-full-response.json")
            response_hash = write_json(response_path, fetched)
            text_path = directory / (label + ".txt")
            text_path.write_text(fetched["text"], encoding="utf-8")
            text_path.chmod(0o600)
            record.update(state="complete", completed_at=now(), final_url=fetched["url"],
                bytes_read=fetched["bytes_read"], decoded_bytes=fetched["decoded_bytes"], text_characters=len(fetched["text"]),
                text_sha256=text_hash, text_path=text_path.name, full_response_path=response_path.name,
                full_response_sha256=response_hash, fetched_at=fetched["fetched_at"], content_type=fetched["content_type"],
                robots_url=fetched["robots_url"], extraction=fetched["extraction"])
        except Exception as error:
            record.update(state="failed_or_unknown", completed_at=now(), code=getattr(error, "code", type(error).__name__))
        record = attach_transport_metadata(record, label, directory)
        write_json(path, record)
        rows.append({"id": label, **record})
    result = {"schema_version": 1, "reserved_at": manifest["reserved_at"], "prose_inspected": False,
        "excerpts_selected": False, "search_queries": 0, "model_calls": 0, "cases": rows}
    write_json(directory / "metadata-summary.json", result)
    # Output metadata only: not title, body, links, header prose, or excerpts.
    return {"directory": str(directory), "sources": [{k: row[k] for k in (
        "id", "state", "code", "bytes_read", "decoded_bytes", "text_characters", "text_sha256", "raw_body_sha256", "full_response_sha256") if k in row} for row in rows]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-two-fixed", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(reserve(fetch=args.fetch_two_fixed), ensure_ascii=False, indent=2))
