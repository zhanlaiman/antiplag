import io
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from plagiarism import pdf_source


ASCII = "The author compared four green leaves and carefully recorded the shape of every vein."
UNICODE = "Редактор сравнил четыре зелёных листа и подробно записал форму каждой жилки."


def owned_pdf(text=ASCII, pages=1, image_only=False, compressed=None):
    """Tiny, authored PDF fixtures, including an explicit Unicode CMap."""
    objects = [b"", b""]

    def add(value):
        objects.append(value)
        return len(objects)

    def stream(data, extra=b""):
        return b"<< /Length " + str(len(data)).encode() + b" " + extra + b">>\nstream\n" + data + b"\nendstream"

    if text.isascii():
        font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        encoded = b"(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode() + b")"
    else:
        font = add(b"<< /Type /Font /Subtype /Type0 /BaseFont /Fixture /Encoding /Identity-H /DescendantFonts [4 0 R] /ToUnicode 5 0 R >>")
        add(b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Fixture /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 500 >>")
        codes = {char: number for number, char in enumerate(dict.fromkeys(text), 1)}
        pairs = [f"<{number:04X}> <{char.encode('utf-16-be').hex()}>" for char, number in codes.items()]
        cmap = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
                "/CMapName /Fixture def /CMapType 2 def\n1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
                + str(len(pairs)) + " beginbfchar\n" + "\n".join(pairs)
                + "\nendbfchar endcmap CMapName currentdict /CMap defineresource pop end end")
        add(stream(cmap.encode()))
        encoded = ("<" + "".join(f"{codes[char]:04X}" for char in text) + ">").encode()
    image = add(stream(b"\x00\x00\x00", b"/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 ")) if image_only else None
    page_ids = []
    for _ in range(pages):
        data = b"q 10 0 0 10 72 700 cm /Im0 Do Q" if image_only else b"BT /F1 12 Tf 72 720 Td " + encoded + b" Tj ET"
        content = add(stream(zlib.compress(compressed) if compressed is not None else data,
                             b"/Filter /FlateDecode " if compressed is not None else b""))
        images = b" /XObject << /Im0 " + str(image).encode() + b" 0 R >>" if image else b""
        page_ids.append(add(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 "
                            + str(font).encode() + b" 0 R >>" + images + b" >> /Contents " + str(content).encode() + b" 0 R >>"))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = b"<< /Type /Pages /Count " + str(pages).encode() + b" /Kids [" + b" ".join(f"{page} 0 R".encode() for page in page_ids) + b"] >>"
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = output.tell()
    output.write(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.write(f"{offset:010} 00000 n \n".encode())
    output.write(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return output.getvalue()


class PDFBoundaryTests(unittest.TestCase):
    def valid(self):
        return {"text": ASCII, "title": "Owned fixture", "page_count": 1, "parser_version": pdf_source.PARSER_VERSION,
                "extraction": "pypdf-text-v1", "memory_limit": "address-space", "memory_limit_bytes": pdf_source.MAX_MEMORY_BYTES}

    def mocked_worker(self, result=None, returncode=0):
        def run(command, **kwargs):
            self.assertEqual(command[1:3], ["-I", "-B"])
            self.assertTrue(kwargs["close_fds"])
            self.assertNotIn("ISTOK_TAVILY_API_KEY", kwargs["env"])
            self.assertNotIn("MODAL_CONFIG_PATH", kwargs["env"])
            self.assertLessEqual(kwargs["timeout"], 5)
            kwargs["stdout"].write(json.dumps(result if result is not None else self.valid()).encode())
            return SimpleNamespace(returncode=returncode)
        return run

    def test_worker_receives_bytes_without_inheriting_provider_secrets(self):
        data = owned_pdf()
        with patch.object(pdf_source.subprocess, "run", side_effect=self.mocked_worker()) as run:
            result = pdf_source.extract_pdf(data, timeout=17)
        self.assertEqual(result["text"], ASCII)
        self.assertIs(run.call_args.kwargs["input"], data)

    def test_size_magic_and_expired_deadline_fail_before_spawning(self):
        with patch.object(pdf_source.subprocess, "run") as run:
            for data, timeout, code in ((b"x", 1, "pdf_parse_failed"),
                                        (b"%PDF-" + b"x" * pdf_source.MAX_PDF_BYTES, 1, "pdf_resource_limit"),
                                        (owned_pdf(), 0, "pdf_timeout")):
                with self.subTest(code=code), self.assertRaises(pdf_source.PDFError) as caught:
                    pdf_source.extract_pdf(data, timeout)
                self.assertEqual(caught.exception.code, code)
            run.assert_not_called()

    def test_timeout_and_worker_resource_exit_are_typed(self):
        with patch.object(pdf_source.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 1)):
            with self.assertRaises(pdf_source.PDFError) as caught:
                pdf_source.extract_pdf(owned_pdf())
        self.assertEqual(caught.exception.code, "pdf_timeout")
        with patch.object(pdf_source.subprocess, "run", side_effect=self.mocked_worker(returncode=95)):
            with self.assertRaises(pdf_source.PDFError) as caught:
                pdf_source.extract_pdf(owned_pdf())
        self.assertEqual(caught.exception.code, "pdf_resource_limit")

    def test_empty_or_invalid_worker_result_never_becomes_success(self):
        for value in ({"error": "pdf_no_text"}, {**self.valid(), "text": ""}, {**self.valid(), "page_count": 41}, []):
            with self.subTest(value=value), patch.object(pdf_source.subprocess, "run", side_effect=self.mocked_worker(value)):
                with self.assertRaises(pdf_source.PDFError):
                    pdf_source.extract_pdf(owned_pdf())

    def test_parser_audit_blocks_sockets_and_external_helpers(self):
        for event in ("socket.__new__", "socket.getaddrinfo", "subprocess.Popen", "os.system"):
            with self.subTest(event=event), self.assertRaises(PermissionError):
                pdf_source._no_network(event, ())


class RealOptionalPDFWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            found = subprocess.run([pdf_source._python(), "-I", "-c", "import pypdf;print(pypdf.__version__)"],
                                   capture_output=True, timeout=3, check=True).stdout.decode().strip()
        except (OSError, subprocess.SubprocessError):
            raise unittest.SkipTest("Optional pinned pypdf is not installed")
        if found != pdf_source.PARSER_VERSION:
            raise unittest.SkipTest("Optional pinned pypdf is not installed")

    def test_ascii_and_unicode_owned_text_are_extracted_in_worker(self):
        for text in (ASCII, UNICODE):
            with self.subTest(text=text):
                result = pdf_source.extract_pdf(owned_pdf(text))
                self.assertIn(text, result["text"])
                self.assertEqual(result["page_count"], 1)
                self.assertIn(result["memory_limit"], {"address-space", "rss-watchdog"})

    def test_image_only_page_page_cap_and_decompression_bomb_are_not_empty_success(self):
        cases = ((owned_pdf(image_only=True), "pdf_no_text"), (owned_pdf(pages=41), "pdf_page_limit"),
                 (owned_pdf(compressed=b"x" * (pdf_source.MAX_STREAM_BYTES + 1)), "pdf_resource_limit"))
        for data, code in cases:
            with self.subTest(code=code), self.assertRaises(pdf_source.PDFError) as caught:
                pdf_source.extract_pdf(data)
            self.assertEqual(caught.exception.code, code)

    def test_owned_encrypted_fixture_is_unsupported(self):
        command = "import io,sys;from pypdf import PdfReader,PdfWriter;w=PdfWriter();w.append_pages_from_reader(PdfReader(io.BytesIO(sys.stdin.buffer.read())));w.encrypt('owned-fixture-password');b=io.BytesIO();w.write(b);sys.stdout.buffer.write(b.getvalue())"
        data = subprocess.run([pdf_source._python(), "-I", "-c", command], input=owned_pdf(),
                              capture_output=True, timeout=3, check=True).stdout
        with self.assertRaises(pdf_source.PDFError) as caught:
            pdf_source.extract_pdf(data)
        self.assertEqual(caught.exception.code, "pdf_encrypted")


if __name__ == "__main__":
    unittest.main()
