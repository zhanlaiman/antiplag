import gzip
from hashlib import sha256
import io
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter

from plagiarism import pdf_source, web_fetch
from tests.test_pdf_source import ASCII, owned_pdf
from tests.test_web_fetch import Fixture, Response


MIB = 1024 * 1024


def large_owned_pdf():
    """A valid authored one-page PDF with a separate, inert attachment."""
    writer = PdfWriter()
    writer.append_pages_from_reader(PdfReader(io.BytesIO(owned_pdf())))
    writer.add_attachment("owned-padding.bin", b"x" * (MIB + 4096))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class PDFFetchLimitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        for target in ("create_connection", "getaddrinfo"):
            guard = patch.object(web_fetch.socket, target, side_effect=AssertionError("Network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        environment = patch.dict(os.environ, {"ISTOK_PDF_PYTHON": sys.executable,
                                              "ISTOK_PDF_ALTERNATIVE_ENGINE": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def assert_code(self, code, operation):
        with self.assertRaises(web_fetch.FetchError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_large_valid_pdf_passes_fetch_and_real_isolated_worker(self):
        data = large_owned_pdf()
        self.assertGreater(len(data), MIB)
        self.assertLess(len(data), 8 * MIB)
        page = self.fixture.add("example.org", "/paper.pdf", Response(data, headers={
            "Content-Type": "Application/PDF; version=1.7", "Content-Length": str(len(data))}))
        result = self.fixture.fetcher().fetch("https://example.org/paper.pdf")
        self.assertEqual(result["text"], ASCII)
        self.assertEqual(result["text_sha256"], sha256(ASCII.encode()).hexdigest())
        self.assertEqual(result["pdf_pages"], 1)
        self.assertEqual(result["pdf_parser_version"], "7.0.1")
        self.assertEqual([(host, target) for host, target, _, _ in self.fixture.requests],
                         [("example.org", "/robots.txt"), ("example.org", "/paper.pdf")])
        self.assertEqual(page.position, len(data))
        self.assertTrue(page.closed)
        self.assertTrue(all(connection[-1].closed for connection in self.fixture.connections))

    def test_large_gzip_pdf_passes_the_same_real_worker(self):
        data = large_owned_pdf()
        self.fixture.add("example.org", "/paper.pdf", Response(gzip.compress(data), headers={
            "Content-Type": "application/pdf", "Content-Encoding": "gzip"}))
        result = self.fixture.fetcher().fetch("https://example.org/paper.pdf")
        self.assertEqual(result["text"], ASCII)
        self.assertEqual(result["pdf_pages"], 1)

    def test_pdf_declared_streamed_and_gzip_decoded_overflow_fail_before_parser(self):
        overflow = b"%PDF-1.4\n" + b"x" * (8 * MIB)
        cases = (
            Response(b"%PDF-1.4", headers={"Content-Type": "application/pdf", "Content-Length": str(8 * MIB + 1)}),
            Response(overflow, headers={"Content-Type": "application/pdf"}),
            Response(gzip.compress(overflow), headers={"Content-Type": "application/pdf", "Content-Encoding": "gzip"}),
        )
        for response in cases:
            with self.subTest(headers=response.headers), patch.object(pdf_source, "extract_pdf") as parse:
                fixture = Fixture()
                fixture.add("example.org", "/paper.pdf", response)
                self.assert_code("response_too_large", lambda: fixture.fetcher().fetch("https://example.org/paper.pdf"))
                parse.assert_not_called()
                self.assertTrue(response.closed)

    def test_html_plain_and_robots_keep_one_mib_wire_and_decoded_limits(self):
        for path, mime in (("/paper", "text/html"), ("/paper", "text/plain"), ("/robots.txt", "text/plain")):
            for compressed in (False, True):
                with self.subTest(path=path, mime=mime, compressed=compressed):
                    fixture = Fixture()
                    body = b"x" * (MIB + 1)
                    headers = {"Content-Type": mime}
                    if compressed:
                        body = gzip.compress(body)
                        headers["Content-Encoding"] = "gzip"
                    fixture.add("example.org", path, Response(body, headers=headers))
                    self.assert_code("response_too_large", lambda: fixture.fetcher().fetch("https://example.org/paper"))

    def test_direct_body_reader_keeps_original_one_mib_default(self):
        reader = self.fixture.fetcher()
        body, wire = reader._read_body(None, Response(b"x" * MIB), 20)
        self.assertEqual(len(body), MIB)
        self.assertEqual(wire, MIB)
        self.assert_code("response_too_large", lambda: reader._read_body(None, Response(b"x" * (MIB + 1)), 20))

    def test_invalid_pdf_and_malformed_length_never_become_evidence(self):
        for response, code in (
            (Response(b"%PDF-1.4\n" + b"x" * (MIB + 1), headers={"Content-Type": "application/pdf"}), "pdf_parse_failed"),
            (Response(large_owned_pdf(), headers={"Content-Type": "application/pdf", "Content-Length": "invalid"}), "response_too_large"),
        ):
            with self.subTest(code=code):
                fixture = Fixture()
                fixture.add("example.org", "/paper.pdf", response)
                self.assert_code(code, lambda: fixture.fetcher().fetch("https://example.org/paper.pdf"))

    def test_real_worker_stdin_rejects_above_eight_mib(self):
        completed = subprocess.run([sys.executable, "-I", "-B", pdf_source.__file__, "--worker"],
            input=b"%PDF-1.4\n" + b"x" * (8 * MIB), capture_output=True, timeout=5,
            env={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}, check=True)
        self.assertEqual(json.loads(completed.stdout), {"error": "pdf_resource_limit"})


if __name__ == "__main__":
    unittest.main()
