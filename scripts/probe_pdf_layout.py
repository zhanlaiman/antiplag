#!/usr/bin/env python3
"""Compare plain/layout extraction on identical bounded DEV PDF bytes.

Production code is unchanged. Fetching is opt-in, uses the normal robots-aware
Fetcher, and is limited to three fixed DEV URLs with persistent attempt markers.
Parsing always uses the existing isolated worker and all of its resource limits.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import difflib
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import pdf_source
from plagiarism.matching import _tokens, analyze
from plagiarism.web_fetch import Fetcher, _normalize_whitespace
from scripts.run_dev_query_probe import dump

LIVE = ROOT / "data/private/web-evaluation/dev-live-30-v1"
DEV = ROOT / "data/private/web-evaluation/materials/dev-30"
OUT = ROOT / "data/private/web-evaluation/pdf-layout-probe-v1"
SELECTIONS = (("D06", "mat_obr_rez.pdf"), ("D20", "8_Fotoapparat_8.pdf"),
              ("D16", "kniga-pro-bukvy2-44-51.pdf"))
NEGATIVE = ("На занятии обсуждали измерения температуры в помещении. Ученики по очереди записывали показания "
            "в собственные таблицы, затем сравнивали записи с соседями. Учитель предложил проверить, "
            "изменится ли результат возле открытого окна. Новую серию наблюдений решили провести завтра.")


def digest(value):
    return sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def write_bytes(path, data):
    path.write_bytes(data)
    path.chmod(0o600)


def read_or_fetch(label, url, allow_fetch):
    metadata_path, fetch_path = OUT / (label + "-raw.json"), OUT / (label + "-fetch.json")
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text())
        path = (OUT / metadata["path"]).resolve()
        if not path.is_relative_to(OUT.resolve()) or metadata["requested_url"] != url:
            raise ValueError("Cached PDF identity changed")
        data = path.read_bytes()
        if digest(data) != metadata["sha256"] or len(data) > pdf_source.MAX_PDF_BYTES or not data.startswith(b"%PDF-"):
            raise ValueError("Cached PDF hash/size changed")
        return data, metadata
    if fetch_path.exists():
        return None, {"unavailable": "prior_fetch_has_no_captured_pdf", "retry_performed": False,
                      "previous": json.loads(fetch_path.read_text())}
    if not allow_fetch:
        return None, {"unavailable": "raw_not_cached", "new_fetch_allowed": False}
    if len(list(OUT.glob("D*-fetch.json"))) >= 3:
        raise ValueError("Three direct-fetch attempts already recorded")
    dump(fetch_path, {"state": "submitted", "requested_url": url, "at": datetime.now(timezone.utc).isoformat()})
    original = pdf_source.extract_pdf
    def capture(data, *args, **kwargs):
        if len(data) <= pdf_source.MAX_PDF_BYTES and data.startswith(b"%PDF-"):
            raw_path = OUT / "raw" / (digest(data) + ".pdf")
            write_bytes(raw_path, data)
            dump(metadata_path, {"requested_url": url, "path": str(raw_path.relative_to(OUT)),
                "sha256": digest(data), "bytes": len(data), "captured_at": datetime.now(timezone.utc).isoformat()})
        return original(data, *args, **kwargs)
    try:
        with patch.object(pdf_source, "extract_pdf", capture):
            fetched = Fetcher().fetch(url)
        dump(fetch_path, {"state": "complete", "requested_url": url, "fetched": fetched})
    except Exception as error:
        dump(fetch_path, {"state": "failed_or_unknown", "requested_url": url,
                          "code": getattr(error, "code", type(error).__name__)})
    return read_or_fetch(label, url, False)


def worker_copies():
    source = Path(pdf_source.__file__).read_text()
    extraction = 'text = page.extract_text() or ""'
    if source.count(extraction) != 1:
        raise ValueError("Rebase the one-line extraction-mode experiment")
    metadata = '"extraction": "pypdf-text-v1",'
    if source.count(metadata) != 1:
        raise ValueError("Rebase page-length diagnostic metadata")
    shared = source.replace(metadata, '"page_text_lengths": [len(part) for part in parts], ' + metadata)
    paths = {}
    for mode in ("plain", "layout"):
        candidate = shared if mode == "plain" else shared.replace(extraction, 'text = page.extract_text(extraction_mode="layout") or ""')
        path = OUT / (mode + "-isolated-worker.py")
        write_bytes(path, candidate.encode())
        paths[mode] = path
    return paths, {"original_worker_sha256": digest(source), "plain_worker_sha256": digest(paths["plain"].read_bytes()),
        "layout_worker_sha256": digest(paths["layout"].read_bytes()),
        "shared_instrumentation": "Per-page text lengths added to JSON for locating the render page; extraction algorithms unchanged.",
        "single_behavioral_change": 'page.extract_text() -> page.extract_text(extraction_mode="layout")'}


def parse(data, worker, mode):
    start = perf_counter()
    try:
        with patch.object(pdf_source, "__file__", str(worker)):
            result = pdf_source.extract_pdf(data)
        return {"status": "complete", "mode": mode, "elapsed_seconds": round(perf_counter() - start, 4),
                "result": result, "normalized_text": _normalize_whitespace(result["text"])}
    except Exception as error:
        return {"status": "failed", "mode": mode, "elapsed_seconds": round(perf_counter() - start, 4),
                "code": getattr(error, "code", type(error).__name__)}


def exact_coverage(target, source):
    result = analyze(target, [{"id": "pdf-source", "title": "Saved public PDF", "url": "https://saved.test/source.pdf", "text": source}])
    tokens = _tokens(target)
    exact = [m for m in result["matches"] if m["kind"] == "exact"]
    return {"words": len(tokens), "covered_words": sum(any(m["start"] <= t.start and t.end <= m["end"] for m in exact) for t in tokens),
            "exact_matches": len(exact), "all_matches": len(result["matches"])}


def compare(label, case, baseline_source, input_text, parsed):
    texts = {mode: item["normalized_text"] for mode, item in parsed.items() if item["status"] == "complete"}
    rows = {mode: {"raw_characters": len(item["result"]["text"]), "characters": len(texts[mode]),
                  "tokens": len(_tokens(texts[mode])), "text_sha256": digest(texts[mode]),
                  "pages": item["result"]["page_count"], "elapsed_seconds": item["elapsed_seconds"],
                  "baseline_snapshot_equal": texts[mode] == baseline_source["text"],
                  "actual_case": exact_coverage(input_text, texts[mode]),
                  "authored_same_topic_negative": exact_coverage(NEGATIVE, texts[mode])}
            for mode, item in parsed.items() if mode in texts}
    baseline_tokens = _tokens(baseline_source["text"])
    controls = []
    for start in (20, len(baseline_tokens) // 2, max(20, len(baseline_tokens) - 60)):
        selected = baseline_tokens[start:start + 20]
        excerpt = baseline_source["text"][selected[0].start:selected[-1].end]
        controls.append({"kind": "synthetic_literal_from_saved_plain_snapshot", "baseline_token_start": start,
                         "text": excerpt, "coverage": {mode: exact_coverage(excerpt, text) for mode, text in texts.items()}})
    result = {"label": label, "modes": rows, "literal_controls": controls}
    if label == "D06":
        quote = case["expected_evidence"][0]["target_quote"]
        result["expected_quote"] = {mode: exact_coverage(quote, text) for mode, text in texts.items()}
        for mode, text in texts.items():
            needle = "ве личины" if "ве личины" in text else "величины"
            index = text.find("истинного значения измеряемой")
            rows[mode]["expected_context"] = text[max(0, index - 80):index + 150] if index >= 0 else None
        if "plain" in parsed and parsed["plain"]["status"] == "complete":
            raw = parsed["plain"]["result"]["text"]
            index = raw.find("ве личины")
            cumulative = 0
            for page, length in enumerate(parsed["plain"]["result"]["page_text_lengths"], 1):
                if cumulative <= index < cumulative + length:
                    result["split_word_page_1based"] = page
                    break
                cumulative += length + 2
    if len(texts) == 2:
        before, after = texts["plain"], texts["layout"]
        before_chars, after_chars = [Counter(c for c in text if not c.isspace()) for text in (before, after)]
        before_words, after_words = [Counter(t.value for t in _tokens(text)) for text in (before, after)]
        result["content_retention"] = {"non_whitespace_sequence_equal": "".join(before.split()) == "".join(after.split()),
            "non_whitespace_characters_lost": sum((before_chars - after_chars).values()),
            "non_whitespace_characters_added": sum((after_chars - before_chars).values()),
            "token_occurrences_lost": sum((before_words - after_words).values()),
            "token_occurrences_added": sum((after_words - before_words).values()),
            "lost_token_examples": (before_words - after_words).most_common(40),
            "added_token_examples": (after_words - before_words).most_common(40)}
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="plain", tofile="layout"))
        write_bytes(OUT / (label + "-normalized.diff"), diff.encode())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="Allow up to three fixed public DEV PDF loads")
    args = parser.parse_args()
    os.umask(0o077)
    OUT.mkdir(mode=0o700, exist_ok=True)
    (OUT / "raw").mkdir(mode=0o700, exist_ok=True)
    workers, worker_metadata = worker_copies()
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only fixed DEV metadata are allowed")
    rows = []
    for label, suffix in SELECTIONS:
        report = json.loads((LIVE / (label + ".json")).read_text())
        source = next(s for s in report["sources"] if s["url"].endswith(suffix))
        case = next(c for c in cases if c["id"] == label)
        if case["input_path"] != f"inputs/{label}.txt":
            raise ValueError("Unexpected DEV input path")
        text = (DEV / case["input_path"]).read_text()
        if digest(text) != case["text_sha256"]:
            raise ValueError("Changed DEV input")
        data, raw = read_or_fetch(label, source["url"], args.fetch)
        if data is None:
            rows.append({"label": label, "status": "raw_unavailable", "details": raw})
            print(label, "raw unavailable", flush=True)
            continue
        parsed = {}
        for mode in ("plain", "layout"):
            parsed[mode] = parse(data, workers[mode], mode)
            dump(OUT / (label + "-" + mode + ".json"), parsed[mode])
        comparison = compare(label, case, source, text, parsed)
        comparison.update(raw=raw, parse_status={mode: {k: v for k, v in item.items() if k not in ("result", "normalized_text")}
                                                 for mode, item in parsed.items()})
        rows.append(comparison)
        print(label, json.dumps({"parse_status": comparison["parse_status"], "modes": comparison["modes"],
              "expected_quote": comparison.get("expected_quote"), "content_retention": comparison.get("content_retention")}, ensure_ascii=False), flush=True)
    result = {"schema_version": 1, "live_search_queries": 0, "paid_api_calls": 0,
        "direct_fetch_attempts_recorded": len(list(OUT.glob("D*-fetch.json"))),
        "worker": worker_metadata, "limits": {"seconds": pdf_source.MAX_SECONDS, "pages": pdf_source.MAX_PAGES,
            "bytes": pdf_source.MAX_PDF_BYTES, "memory_bytes": pdf_source.MAX_MEMORY_BYTES}, "cases": rows,
        "promoted_to_production": False}
    dump(OUT / "comparison.json", result)


if __name__ == "__main__":
    main()
