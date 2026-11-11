"""Optional, isolated text extraction for downloaded public PDFs, without OCR."""

import importlib.util
from importlib.metadata import PackageNotFoundError, version
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from threading import Thread
import time


ROOT = Path(__file__).resolve().parent.parent
PARSER_VERSION = "7.0.1"
MAX_PDF_BYTES = 1024 * 1024
MAX_PAGES = 40
MAX_TEXT_CHARACTERS = 200_000
MAX_OUTPUT_BYTES = 1_300_000
MAX_MEMORY_BYTES = 512 * 1024 * 1024
MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 5
ERRORS = {
    "pdf_parser_unavailable": "Обработчик текстовых PDF не установлен или недоступен.",
    "pdf_parse_failed": "Не удалось безопасно прочитать текст PDF.",
    "pdf_encrypted": "Зашифрованный PDF не проверен.",
    "pdf_no_text": "В PDF нет достаточного текстового слоя; OCR не выполняется.",
    "pdf_page_limit": "PDF содержит более 40 страниц и не проверен.",
    "pdf_resource_limit": "PDF превысил ограничения обработки и не проверен.",
    "pdf_timeout": "Время обработки PDF истекло; источник не проверен.",
}


class PDFError(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in ERRORS else "pdf_parse_failed"
        super().__init__(ERRORS[self.code])


def _python():
    configured = os.environ.get("ISTOK_PDF_PYTHON")
    if configured:
        return str(Path(configured).absolute())
    if importlib.util.find_spec("pypdf") is not None:
        try:
            if version("pypdf") == PARSER_VERSION:
                return sys.executable
        except PackageNotFoundError:
            pass
    project = ROOT / ".runtime" / "modal-venv" / "bin" / "python"
    return str(project) if project.is_file() else sys.executable


def extract_pdf(data, timeout=MAX_SECONDS):
    """Return a bounded text snapshot or a typed failure, never an empty success."""
    if not isinstance(data, bytes) or len(data) > MAX_PDF_BYTES:
        raise PDFError("pdf_resource_limit")
    if not data.startswith(b"%PDF-"):
        raise PDFError("pdf_parse_failed")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise PDFError("pdf_timeout")
    try:
        with tempfile.TemporaryDirectory(prefix="istok-public-pdf-") as directory:
            # Only extracted JSON touches this anonymous temporary file. Raw PDF
            # bytes travel through stdin; provider credentials are not inherited.
            with tempfile.TemporaryFile() as output:
                process = subprocess.run([_python(), "-I", "-B", str(Path(__file__).resolve()), "--worker"],
                    input=data, stdout=output, stderr=subprocess.DEVNULL, cwd=directory,
                    env={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
                    timeout=min(timeout, MAX_SECONDS), close_fds=True, check=False)
                output.seek(0)
                raw = output.read(MAX_OUTPUT_BYTES + 1)
        if process.returncode in {95, -9, -24, -25}:
            raise PDFError("pdf_resource_limit")
        if len(raw) > MAX_OUTPUT_BYTES:
            raise PDFError("pdf_resource_limit")
        if process.returncode:
            raise PDFError("pdf_parse_failed")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise PDFError("pdf_parse_failed")
        if result.get("error"):
            raise PDFError(result["error"])
        text, pages = result.get("text"), result.get("page_count")
        if (not isinstance(text, str) or len(text.strip()) < 35 or "\0" in text
                or len(text) > MAX_TEXT_CHARACTERS or type(pages) is not int or not 1 <= pages <= MAX_PAGES
                or result.get("parser_version") != PARSER_VERSION):
            raise PDFError("pdf_parse_failed")
        return result
    except subprocess.TimeoutExpired:
        raise PDFError("pdf_timeout") from None
    except OSError:
        raise PDFError("pdf_parser_unavailable") from None
    except (ValueError, UnicodeError):
        raise PDFError("pdf_parse_failed") from None


def _limits():
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
        memory_limit = "address-space"
    except (ValueError, OSError):
        if sys.platform != "darwin":
            raise PDFError("pdf_parser_unavailable")
        # Darwin rejects AS/DATA/RSS rlimits. Monitor this worker's peak RSS;
        # parser-level decompression/object limits remain enabled independently.
        memory_limit = "rss-watchdog"

    def watch_memory():
        while True:
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if rss * (1 if sys.platform == "darwin" else 1024) > MAX_MEMORY_BYTES:
                os._exit(95)
            time.sleep(0.005)

    Thread(target=watch_memory, daemon=True).start()
    return memory_limit


def _no_network(event, arguments):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
        raise PermissionError("PDF worker cannot access network or start programs")


def _parse(data, memory_limit):
    try:
        import pypdf
    except ImportError:
        raise PDFError("pdf_parser_unavailable") from None
    if pypdf.__version__ != PARSER_VERSION:
        raise PDFError("pdf_parser_unavailable")
    configuration = dict(maximum_declared_stream_length=MAX_STREAM_BYTES,
        array_based_stream_maximum_output_length=MAX_STREAM_BYTES, zlib_maximum_output_length=MAX_STREAM_BYTES,
        lzw_maximum_output_length=MAX_STREAM_BYTES, run_length_maximum_output_length=MAX_STREAM_BYTES,
        brotli_maximum_output_length=MAX_STREAM_BYTES, jbig2_maximum_output_length=MAX_STREAM_BYTES,
        image_maximum_buffer_size=MAX_STREAM_BYTES, stream_decoding_work_maximum_length=8 * MAX_STREAM_BYTES,
        array_maximum_element_count=20000, dictionary_maximum_element_count=5000,
        stream_parsing_work_maximum_count=150000, page_tree_maximum_entries=512,
        page_tree_maximum_page_entries=1024, page_tree_maximum_depth=32,
        xform_maximum_invocations_per_extraction=250, jbig2dec_binary=None)
    with pypdf.apply_configuration(**configuration):
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False, root_object_recovery_limit=1000,
                                 xref_stream_maximum_entries=20000)
        if reader.is_encrypted:
            raise PDFError("pdf_encrypted")
        count = len(reader.pages)
        if count > MAX_PAGES:
            raise PDFError("pdf_page_limit")
        if count < 1:
            raise PDFError("pdf_no_text")
        parts, total = [], 0
        for page in reader.pages:
            text = page.extract_text() or ""
            total += len(text)
            if total > MAX_TEXT_CHARACTERS:
                raise PDFError("pdf_resource_limit")
            # A scanned page among text pages must not silently disappear.
            resources = page.get("/Resources")
            resources = resources.get_object() if resources is not None else {}
            objects = resources.get("/XObject")
            objects = objects.get_object() if objects is not None else {}
            if not text.strip() and any(item.get_object().get("/Subtype") == "/Image" for item in objects.values()):
                raise PDFError("pdf_no_text")
            parts.append(text)
        text = "\n\n".join(parts).strip()
        if len(text) < 35 or "\0" in text or len(re.findall(r"[^\W_]{2,}", text)) < 6:
            raise PDFError("pdf_no_text")
        title = str((reader.metadata or {}).get("/Title") or "")[:300]
        return {"text": text, "title": title, "page_count": count, "parser_version": PARSER_VERSION,
                "extraction": "pypdf-text-v1", "memory_limit": memory_limit, "memory_limit_bytes": MAX_MEMORY_BYTES}


def _worker():
    logging.disable(logging.CRITICAL)
    try:
        memory_limit = _limits()
        sys.addaudithook(_no_network)
        data = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            raise PDFError("pdf_resource_limit")
        result = _parse(data, memory_limit)
    except PDFError as error:
        result = {"error": error.code}
    except MemoryError:
        result = {"error": "pdf_resource_limit"}
    except Exception as error:
        name = type(error).__name__
        result = {"error": "pdf_resource_limit" if name == "LimitReachedError" else
                  "pdf_encrypted" if name in {"FileNotDecryptedError", "DependencyError"} else "pdf_parse_failed"}
    raw = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_OUTPUT_BYTES:
        raw = b'{"error":"pdf_resource_limit"}'
    sys.stdout.buffer.write(raw)


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker()
