#!/usr/bin/env python3
"""One local PDF-text hypothesis using the already bundled PDFium parser.

No fetches, installs, or production changes. Each parse runs in an isolated
worker with the production byte/page/text/memory/CPU/output limits and 5s wall cap.
"""
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
PDFIUM_PYTHON = Path("/Users/air/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3")


def source_limits():
    spec = importlib.util.spec_from_file_location("pdf_limits", ROOT / "plagiarism/pdf_source.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worker():
    limits = source_limits()
    logging.disable(logging.CRITICAL)
    try:
        memory = limits._limits()
        sys.addaudithook(limits._no_network)
        data = sys.stdin.buffer.read(limits.MAX_PDF_BYTES + 1)
        if len(data) > limits.MAX_PDF_BYTES:
            raise limits.PDFError("pdf_resource_limit")
        if not data.startswith(b"%PDF-"):
            raise limits.PDFError("pdf_parse_failed")
        import pypdfium2
        from importlib.metadata import version
        document = pypdfium2.PdfDocument(data)
        try:
            count = len(document)
            if count > limits.MAX_PAGES:
                raise limits.PDFError("pdf_page_limit")
            if count < 1:
                raise limits.PDFError("pdf_no_text")
            parts, total = [], 0
            for index in range(count):
                page = document[index]
                try:
                    text_page = page.get_textpage()
                    try:
                        text = text_page.get_text_range()
                    finally:
                        text_page.close()
                finally:
                    page.close()
                total += len(text)
                if total > limits.MAX_TEXT_CHARACTERS:
                    raise limits.PDFError("pdf_resource_limit")
                parts.append(text)
            text = "\n\n".join(parts).strip()
            if len(text) < 35 or "\0" in text:
                raise limits.PDFError("pdf_no_text")
            result = {"text": text, "page_text_lengths": list(map(len, parts)), "page_count": count,
                "parser": "pypdfium2", "parser_version": version("pypdfium2"),
                "memory_limit": memory, "memory_limit_bytes": limits.MAX_MEMORY_BYTES}
        finally:
            document.close()
    except limits.PDFError as error:
        result = {"error": error.code}
    except ImportError:
        result = {"error": "pdf_parser_unavailable"}
    except MemoryError:
        result = {"error": "pdf_resource_limit"}
    except Exception:
        result = {"error": "pdf_parse_failed"}
    raw = json.dumps(result, ensure_ascii=False).encode()
    if len(raw) > limits.MAX_OUTPUT_BYTES:
        raw = b'{"error":"pdf_resource_limit"}'
    sys.stdout.buffer.write(raw)


def extract(data):
    limits = source_limits()
    if len(data) > limits.MAX_PDF_BYTES or not data.startswith(b"%PDF-"):
        raise limits.PDFError("pdf_resource_limit")
    start = perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="istok-pdfium-probe-") as directory:
            with tempfile.TemporaryFile() as output:
                process = subprocess.run([str(PDFIUM_PYTHON), "-I", "-B", str(Path(__file__).resolve()), "--worker"],
                    input=data, stdout=output, stderr=subprocess.DEVNULL, cwd=directory,
                    env={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
                    timeout=limits.MAX_SECONDS, close_fds=True, check=False)
                output.seek(0)
                raw = output.read(limits.MAX_OUTPUT_BYTES + 1)
        if process.returncode in (95, -9, -24, -25) or len(raw) > limits.MAX_OUTPUT_BYTES:
            raise limits.PDFError("pdf_resource_limit")
        if process.returncode:
            raise limits.PDFError("pdf_parse_failed")
        result = json.loads(raw)
        if result.get("error"):
            raise limits.PDFError(result["error"])
        if (result.get("parser") != "pypdfium2" or not isinstance(result.get("text"), str)
                or len(result["text"]) > limits.MAX_TEXT_CHARACTERS
                or type(result.get("page_count")) is not int or not 1 <= result["page_count"] <= limits.MAX_PAGES):
            raise limits.PDFError("pdf_parse_failed")
        return {"status": "complete", "elapsed_seconds": round(perf_counter() - start, 4), "result": result}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "code": "pdf_timeout", "elapsed_seconds": round(perf_counter() - start, 4)}
    except Exception as error:
        return {"status": "failed", "code": getattr(error, "code", type(error).__name__),
                "elapsed_seconds": round(perf_counter() - start, 4)}


def main():
    sys.path.insert(0, str(ROOT))
    from collections import Counter
    from plagiarism.web_fetch import _normalize_whitespace
    from scripts.probe_pdf_layout import OUT, DEV, NEGATIVE, digest, exact_coverage
    from scripts.probe_pdf_evidence_views import BOUNDARY_PROBES, score
    from scripts.run_dev_query_probe import dump
    comparison = json.loads((OUT / "comparison.json").read_text())
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    rows = []
    for saved in comparison["cases"]:
        label = saved["label"]
        raw = saved["raw"]
        data = (OUT / raw["path"]).read_bytes()
        if digest(data) != raw["sha256"]:
            raise ValueError("Raw PDF hash changed")
        parsed = extract(data)
        if parsed["status"] != "complete":
            dump(OUT / (label + "-pdfium.json"), parsed)
            rows.append({"label": label, **parsed})
            print(label, parsed, flush=True)
            continue
        text = _normalize_whitespace(parsed["result"]["text"])
        parsed["normalized_text"] = text
        dump(OUT / (label + "-pdfium.json"), parsed)
        plain = json.loads((OUT / (label + "-plain.json")).read_text())["normalized_text"]
        before, after = [Counter(c for c in value if not c.isspace()) for value in (plain, text)]
        case = next(c for c in cases if c["id"] == label)
        actual = (DEV / case["input_path"]).read_text()
        row = {"label": label, "status": "complete", "elapsed_seconds": parsed["elapsed_seconds"],
            "parser_version": parsed["result"]["parser_version"], "characters": len(text),
            "non_whitespace_characters_lost": sum((before - after).values()),
            "non_whitespace_characters_added": sum((after - before).values()),
            "actual_case": score(actual, {"plain": plain, "pdfium": text}),
            "authored_same_topic_negative": exact_coverage(NEGATIVE, text),
            "literal_controls": [{"text": control["text"], "pdfium": exact_coverage(control["text"], text)}
                                 for control in saved["literal_controls"]]}
        if label == "D06":
            row["expected_quote"] = exact_coverage(case["expected_evidence"][0]["target_quote"], text)
        if label == "D16":
            visual = json.loads((OUT / "visual-control.json").read_text())
            row["visual_positive"] = exact_coverage(visual["text"], text)
            row["artificial_boundary_negatives"] = [{**probe, "result": score(probe["text"], {"pdfium": text}, probe["bad_word"])}
                                                   for probe in BOUNDARY_PROBES]
        rows.append(row)
        print(label, json.dumps({k: v for k, v in row.items() if k not in ("actual_case", "literal_controls", "artificial_boundary_negatives")}, ensure_ascii=False), flush=True)
    dump(OUT / "pdfium-comparison.json", {"schema_version": 1, "new_fetches": 0, "new_searches": 0,
        "production_changed": False, "interpreter": str(PDFIUM_PYTHON), "cases": rows})


if __name__ == "__main__":
    worker() if sys.argv[1:] == ["--worker"] else main()
