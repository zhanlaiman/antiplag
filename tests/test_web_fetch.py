from collections import defaultdict, deque
import gzip
from hashlib import sha256
import ssl
from threading import Event
import unittest
from unittest.mock import Mock, patch

from plagiarism import pdf_source, web_fetch


class Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.body, self.status, self.position, self.closed = body, status, 0, False
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.headers.update({key.lower(): value for key, value in (headers or {}).items()})

    def getheader(self, name):
        return self.headers.get(name.lower())

    def read(self, amount):
        value = self.body[self.position:self.position + amount]
        self.position += len(value)
        return value

    read1 = read

    def close(self):
        self.closed = True


class Fixture:
    def __init__(self):
        self.routes = defaultdict(deque)
        self.requests, self.connections, self.lookups = [], [], []
        self.addresses = {}
        self.now = 0

    def add(self, host, target, response):
        self.routes[(host, target)].append(response)
        return response

    def resolver(self, host, port):
        self.lookups.append((host, port))
        values = self.addresses.get(host, ["93.184.216.34"])
        return values.popleft() if isinstance(values, deque) else values

    def connector(self, scheme, host, port, address, timeout):
        fixture = self

        class Connection:
            def __init__(self):
                self.sock = Mock()
                self.closed = False

            def request(self, method, target, headers):
                self.target = target
                fixture.requests.append((host, target, method, headers))

            def getresponse(self):
                values = fixture.routes[(host, self.target)]
                if values:
                    value = values.popleft()
                    return value() if callable(value) else value
                if self.target == "/robots.txt":
                    return Response(status=404)
                raise AssertionError("Unexpected fixture URL")

            def close(self):
                self.closed = True

        connection = Connection()
        self.connections.append((scheme, host, port, address, timeout, connection))
        return connection

    def fetcher(self):
        return web_fetch.Fetcher(resolver=self.resolver, connector=self.connector, clock=lambda: self.now)


class WebFetchTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(web_fetch.socket, "create_connection", side_effect=AssertionError("Network forbidden"))
        self.dns = patch.object(web_fetch.socket, "getaddrinfo", side_effect=AssertionError("DNS network forbidden"))
        self.network.start()
        self.dns.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.dns.stop)
        self.fixture = Fixture()

    def assert_code(self, code, operation):
        with self.assertRaises(web_fetch.FetchError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_public_html_strips_boilerplate_and_hidden_content_preserving_paragraphs_and_hash(self):
        html = """<html><head><title>Статья &amp; источник</title><style>bad style</style></head>
          <body><header>Меню</header><nav>Навигация</nav><script>SECRET SCRIPT</script>
          <main><p>Первый <strong>абзац</strong> &amp; факт.</p><p>Второй абзац.</p>
          <div hidden>Скрыто</div><div aria-hidden="true">Скрыто ARIA</div>
          <div style="display: none">CSS hidden</div><form>Личные поля</form></main>
          <footer>Подвал</footer></body></html>"""
        page = self.fixture.add("example.org", "/article", Response(html.encode()))
        result = self.fixture.fetcher().fetch("https://example.org/article#part")
        self.assertEqual(result["url"], "https://example.org/article")
        self.assertEqual(result["title"], "Статья & источник")
        self.assertEqual(result["text"], "Первый абзац & факт.\n\nВторой абзац.")
        self.assertEqual(result["text_sha256"], sha256(result["text"].encode()).hexdigest())
        self.assertEqual(result["content_type"], "text/html")
        self.assertEqual(result["extraction"], "html-parser-v2-implied-ends")
        self.assertTrue(page.closed)
        self.assertTrue(all(item[-1].closed for item in self.fixture.connections))
        self.assertTrue(all(item[3] == "93.184.216.34" for item in self.fixture.connections))
        self.assertEqual(self.fixture.requests[-1][3]["User-Agent"], "IstokSourceChecker/0.1")

    def test_unicode_path_is_encoded_but_unicode_or_encoded_hostname_cannot_bypass_checks(self):
        self.fixture.add("example.org", "/%D0%A1%D1%82%D0%B0%D1%82%D1%8C%D1%8F", Response(b"<p>Article</p>"))
        result = self.fixture.fetcher().fetch("https://example.org/Статья")
        self.assertIn("%D0", result["url"])
        for url in ("https://localhost。example/a", "https://ｅxample.org/a", "https://%31%32%37.0.0.1/a", "https://xn--e1afmkfd.xn--p1ai/a"):
            with self.subTest(url=url), self.assertRaises(web_fetch.FetchError):
                self.fixture.fetcher().fetch(url)

    def test_credentials_nonstandard_ports_local_addresses_and_protocols_are_rejected_before_connection(self):
        for url in ("file:///etc/passwd", "ftp://example.org/a", "http://user:pass@example.org/a",
                    "https://example.org:8443/a", "http://example.org:443/a", "https://localhost/a",
                    "http://127.0.0.1/a", "http://169.254.169.254/a", "http://10.0.0.1/a",
                    "http://[::1]/a", "http://[::ffff:127.0.0.1]/a", "https://example.org\\@127.0.0.1/a",
                    "https://example.org/a\r\nHeader:value"):
            with self.subTest(url=url), self.assertRaises(web_fetch.FetchError):
                self.fixture.fetcher().fetch(url)
        self.assertFalse(self.fixture.connections)

    def test_every_resolved_address_must_be_public(self):
        self.fixture.addresses["example.org"] = ["93.184.216.34", "127.0.0.1"]
        self.assert_code("address_not_public", lambda: self.fixture.fetcher().fetch("https://example.org/a"))
        self.assertFalse(self.fixture.connections)

    def test_dns_rebinding_between_robots_and_page_is_rejected_without_private_connection(self):
        self.fixture.addresses["example.org"] = deque([["93.184.216.34"], ["169.254.169.254"]])
        self.assert_code("address_not_public", lambda: self.fixture.fetcher().fetch("https://example.org/a"))
        self.assertEqual(len(self.fixture.connections), 1)
        self.assertEqual(self.fixture.connections[0][3], "93.184.216.34")

    def test_each_page_redirect_gets_robots_and_dns_checks_and_private_redirect_is_blocked(self):
        self.fixture.add("example.org", "/a", Response(status=302, headers={"Location": "https://other.org/b"}))
        self.fixture.add("other.org", "/b", Response(status=302, headers={"Location": "http://127.0.0.1/admin"}))
        self.assert_code("address_not_public", lambda: self.fixture.fetcher().fetch("https://example.org/a"))
        requested = [(host, target) for host, target, _, _ in self.fixture.requests]
        self.assertEqual(requested, [("example.org", "/robots.txt"), ("example.org", "/a"),
                                     ("other.org", "/robots.txt"), ("other.org", "/b")])

    def test_relative_redirect_succeeds_and_more_than_three_redirects_stops(self):
        self.fixture.add("example.org", "/a", Response(status=301, headers={"Location": "/b"}))
        self.fixture.add("example.org", "/b", Response(b"Done", headers={"Content-Type": "text/plain"}))
        result = self.fixture.fetcher().fetch("https://example.org/a")
        self.assertEqual(result["url"], "https://example.org/b")
        self.assertEqual(result["text"], "Done")
        fixture = Fixture()
        for number in range(4):
            fixture.add("example.org", "/" + str(number), Response(status=302, headers={"Location": "/" + str(number + 1)}))
        self.assert_code("too_many_redirects", lambda: fixture.fetcher().fetch("https://example.org/0"))
        self.assertFalse(any(target == "/4" for _, target, _, _ in fixture.requests))

    def test_robot_parser_disallow_and_status_denials_prevent_page_request(self):
        for response in (Response(b"User-agent: *\nDisallow: /private\n", headers={"Content-Type": "text/plain"}),
                         Response(status=403)):
            fixture = Fixture()
            fixture.add("example.org", "/robots.txt", response)
            self.assert_code("robots_denied", lambda: fixture.fetcher().fetch("https://example.org/private"))
            self.assertEqual(len(fixture.requests), 1)

    def test_server_error_in_robots_is_unavailable_and_prevents_page_loading(self):
        self.fixture.add("example.org", "/robots.txt", Response(status=503))
        self.assert_code("robots_unavailable", lambda: self.fixture.fetcher().fetch("https://example.org/article"))
        self.assertEqual(len(self.fixture.requests), 1)

    def test_redirected_robots_rules_apply_to_the_original_origin_and_stay_cached(self):
        self.fixture.add("example.org", "/robots.txt", Response(status=302, headers={"Location": "https://other.org/robots.txt"}))
        self.fixture.add("other.org", "/robots.txt", Response(b"User-agent: *\nDisallow: /private\n", headers={"Content-Type": "text/plain"}))
        self.fixture.add("example.org", "/public", Response(b"<p>A public article.</p>"))
        fetcher = self.fixture.fetcher()
        result = fetcher.fetch("https://example.org/public")
        self.assertEqual(result["text"], "A public article.")
        self.assert_code("robots_denied", lambda: fetcher.fetch("https://example.org/private"))
        self.assertEqual([(host, path) for host, path, _, _ in self.fixture.requests],
                         [("example.org", "/robots.txt"), ("other.org", "/robots.txt"), ("example.org", "/public")])
        self.assertEqual([host for host, port in self.fixture.lookups[:2]], ["example.org", "other.org"])

    def test_redirected_robots_reject_private_targets_before_request(self):
        self.fixture.add("example.org", "/robots.txt", Response(status=302, headers={"Location": "http://127.0.0.1/robots.txt"}))
        self.assert_code("address_not_public", lambda: self.fixture.fetcher().fetch("https://example.org/article"))
        self.assertEqual(len(self.fixture.connections), 1)

    def test_robots_follows_five_redirects_and_stops_before_a_sixth(self):
        for redirects in (5, 6):
            fixture = Fixture()
            for index in range(redirects):
                path = "/robots.txt" if index == 0 else "/policy%d" % index
                fixture.add("example.org", path, Response(status=302, headers={"Location": "/policy%d" % (index + 1)}))
            fixture.add("example.org", "/policy%d" % redirects,
                        Response(b"User-agent: *\nDisallow:\n", headers={"Content-Type": "text/plain"}))
            fixture.add("example.org", "/article", Response(b"<p>Public article</p>"))
            if redirects == 5:
                self.assertEqual(fixture.fetcher().fetch("https://example.org/article")["text"], "Public article")
            else:
                self.assert_code("robots_unavailable", lambda: fixture.fetcher().fetch("https://example.org/article"))
                self.assertFalse(any(path == "/policy6" for _, path, _, _ in fixture.requests))

    def test_nonstandard_none_content_encoding_is_raw_and_still_bounded(self):
        html = "<p>Текст открытой статьи с неподвижным изображением.</p>".encode()
        self.fixture.add("example.org", "/raw", Response(html, headers={"Content-Encoding": "none"}))
        result = self.fixture.fetcher().fetch("https://example.org/raw")
        self.assertEqual(result["text"], "Текст открытой статьи с неподвижным изображением.")
        fixture = Fixture()
        fixture.add("example.org", "/raw", Response(gzip.compress(html), headers={"Content-Encoding": "none"}))
        self.assert_code("invalid_text_encoding", lambda: fixture.fetcher().fetch("https://example.org/raw"))
        fixture = Fixture()
        fixture.add("example.org", "/big", Response(b"x" * (web_fetch.MAX_RESPONSE_BYTES + 1), headers={"Content-Encoding": "none"}))
        self.assert_code("response_too_large", lambda: fixture.fetcher().fetch("https://example.org/big"))

    def test_robots_cache_is_bounded_and_still_rechecks_page_dns(self):
        fetcher = self.fixture.fetcher()
        for number in range(65):
            host = f"site{number}.org"
            self.fixture.add(host, "/a", Response(b"<p>Public</p>"))
            fetcher.fetch("https://" + host + "/a")
        self.assertEqual(len(fetcher.robots), 64)
        self.assertNotIn("https://site0.org", fetcher.robots)
        before = len(self.fixture.requests)
        self.fixture.add("site64.org", "/b", Response(b"<p>Other</p>"))
        fetcher.fetch("https://site64.org/b")
        self.assertEqual(len(self.fixture.requests), before + 1)
        self.fixture.addresses["site64.org"] = ["10.0.0.1"]
        self.assert_code("address_not_public", lambda: fetcher.fetch("https://site64.org/c"))

    def test_wire_size_gzip_bomb_and_extracted_text_are_bounded(self):
        cases = [
            (Response(b"x" * (web_fetch.MAX_RESPONSE_BYTES + 1)), "response_too_large"),
            (Response(gzip.compress(b"x" * (web_fetch.MAX_RESPONSE_BYTES + 1)),
                      headers={"Content-Encoding": "gzip"}), "response_too_large"),
            (Response(b"x" * (web_fetch.MAX_TEXT_CHARACTERS + 1), headers={"Content-Type": "text/plain"}), "text_too_large"),
            (Response(b"<p>ok</p>", headers={"Content-Length": str(web_fetch.MAX_RESPONSE_BYTES + 1)}), "response_too_large"),
        ]
        for response, code in cases:
            with self.subTest(code=code):
                fixture = Fixture()
                fixture.add("example.org", "/a", response)
                self.assert_code(code, lambda: fixture.fetcher().fetch("https://example.org/a"))

    def test_gzip_plain_text_preserves_paragraphs_and_legacy_html_charset_decodes(self):
        plain = "Первый   абзац.\n\nВторой абзац."
        self.fixture.add("example.org", "/a", Response(gzip.compress(plain.encode()),
                         headers={"Content-Type": "text/plain; charset=utf-8", "Content-Encoding": "gzip"}))
        result = self.fixture.fetcher().fetch("https://example.org/a")
        self.assertEqual(result["text"], "Первый абзац.\n\nВторой абзац.")
        self.fixture.add("example.org", "/b", Response('<meta charset="windows-1251"><p>Клетка.</p>'.encode("cp1251"),
                         headers={"Content-Type": "text/html"}))
        result = self.fixture.fetcher().fetch("https://example.org/b")
        self.assertEqual(result["text"], "Клетка.")
        self.assertEqual(result["encoding"], "cp1251")

    def test_binary_invalid_gzip_and_deep_html_fail_explicitly(self):
        for response, code in ((Response(b"binary", headers={"Content-Type": "application/octet-stream"}), "unsupported_content_type"),
                               (Response(b"bad", headers={"Content-Encoding": "gzip"}), "invalid_encoding"),
                               (Response(b"<div>" * 200), "html_too_complex")):
            fixture = Fixture()
            fixture.add("example.org", "/a", response)
            self.assert_code(code, lambda: fixture.fetcher().fetch("https://example.org/a"))

    def test_pdf_text_snapshot_uses_existing_robots_dns_and_hash_guards(self):
        data = b"%PDF-1.4\nowned mock fixture"
        self.fixture.add("example.org", "/article.pdf", Response(data, headers={"Content-Type": "application/pdf"}))
        parsed = {"text": "Авторский текст о зелёных листьях и форме жилок.", "title": "PDF источник",
                  "page_count": 2, "parser_version": "7.0.1", "extraction": "pypdf-text-v1"}
        with patch.object(pdf_source, "extract_pdf", return_value=parsed) as extract:
            result = self.fixture.fetcher().fetch("https://example.org/article.pdf")
        self.assertEqual(extract.call_args.args[0], data)
        self.assertEqual(result["text"], parsed["text"])
        self.assertEqual(result["text_sha256"], sha256(parsed["text"].encode()).hexdigest())
        self.assertEqual(result["content_type"], "application/pdf")
        self.assertEqual(result["pdf_pages"], 2)
        self.assertEqual([(host, target) for host, target, _, _ in self.fixture.requests],
                         [("example.org", "/robots.txt"), ("example.org", "/article.pdf")])

    def test_pdf_parse_failure_and_pdf_served_as_robots_are_not_success(self):
        self.fixture.add("example.org", "/article.pdf", Response(b"%PDF-1.4\nbad", headers={"Content-Type": "application/pdf"}))
        with patch.object(pdf_source, "extract_pdf", side_effect=pdf_source.PDFError("pdf_no_text")):
            self.assert_code("pdf_no_text", lambda: self.fixture.fetcher().fetch("https://example.org/article.pdf"))
        fixture = Fixture()
        fixture.add("example.org", "/robots.txt", Response(b"%PDF-1.4\nnot robots", headers={"Content-Type": "application/pdf"}))
        self.assert_code("unsupported_content_type", lambda: fixture.fetcher().fetch("https://example.org/article.pdf"))
        self.assertEqual(len(fixture.requests), 1)

    def test_pdf_supplemental_text_is_normalized_hashed_and_keeps_noncharacters(self):
        data = b"%PDF-1.4\nowned"
        self.fixture.add("example.org", "/article.pdf", Response(data, headers={"Content-Type": "application/pdf"}))
        primary = "The editor compared the original documents before preparing the final public report."
        alternative = "The editor\r\n  compared\u00a0the original documents before preparing the final public report.\ufffe"
        parsed = {"text": primary, "page_count": 1, "parser_version": "7.0.1", "extraction": "pypdf-text-v1",
                  "optional_status": {"engine": "pdfium", "status": "complete", "code": None},
                  "raw_pdf_sha256": sha256(data).hexdigest(),
                  "supplemental_texts": [{"id": "pdfium-v1", "text": alternative, "parser_version": "5.14.0",
                                          "extraction": "pdfium-text-v1", "page_count": 1}]}
        with patch.object(pdf_source, "extract_pdf", return_value=parsed):
            result = self.fixture.fetcher().fetch("https://example.org/article.pdf")
        supplemental = result["supplemental_texts"][0]
        self.assertEqual(result["text"], primary)
        self.assertEqual(supplemental["text"], web_fetch._normalize_whitespace(alternative))
        self.assertIn("\ufffe", supplemental["text"])
        self.assertEqual(supplemental["text_sha256"], sha256(supplemental["text"].encode()).hexdigest())
        self.assertEqual(result["raw_pdf_sha256"], sha256(data).hexdigest())
        self.assertEqual(result["pdf_pages"], 1)
        self.assertEqual(len(self.fixture.requests), 2)

    def test_malformed_optional_views_do_not_discard_a_successful_primary_pdf(self):
        data = b"%PDF-1.4\nowned"
        self.fixture.add("example.org", "/article.pdf", Response(data, headers={"Content-Type": "application/pdf"}))
        primary = "The editor compared the original documents before preparing the final public report."
        parsed = {"text": primary, "page_count": 1, "parser_version": "7.0.1", "extraction": "pypdf-text-v1",
                  "optional_status": {"engine": "pdfium", "status": "complete", "code": None},
                  "supplemental_texts": [{"id": "one", "text": primary}, {"id": "two", "text": primary}]}
        with patch.object(pdf_source, "extract_pdf", return_value=parsed):
            result = self.fixture.fetcher().fetch("https://example.org/article.pdf")
        self.assertEqual(result["text"], primary)
        self.assertEqual(result["supplemental_texts"], [])
        self.assertEqual(result["optional_status"]["code"], "pdf_parse_failed")
        self.assertEqual(len(self.fixture.requests), 2)

    def test_per_request_and_total_deadlines_apply_across_redirects(self):
        def delayed():
            self.fixture.now += 9
            return Response(b"<p>Late</p>")
        self.fixture.add("example.org", "/a", delayed)
        self.assert_code("fetch_timeout", lambda: self.fixture.fetcher().fetch("https://example.org/a"))
        fixture = Fixture()
        for number in range(4):
            def redirect(number=number):
                fixture.now += 7
                return Response(status=302, headers={"Location": "/" + str(number + 1)})
            fixture.add("example.org", "/" + str(number), redirect)
        self.assert_code("fetch_timeout", lambda: fixture.fetcher().fetch("https://example.org/0"))

    def test_default_connector_connects_to_pinned_ip_with_original_tls_server_name(self):
        sock = Mock()
        wrapped = Mock()
        context = ssl.create_default_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        context_mock = Mock(wrap_socket=Mock(return_value=wrapped))
        with patch.object(web_fetch.socket, "create_connection", return_value=sock) as connect, \
                patch.object(web_fetch.ssl, "create_default_context", return_value=context_mock):
            connection = web_fetch._connector("https", "example.org", 443, "93.184.216.34", 8)
            connection.connect()
        connect.assert_called_once_with(("93.184.216.34", 443), timeout=8)
        context_mock.wrap_socket.assert_called_once_with(sock, server_hostname="example.org")
        self.assertIs(connection.sock, wrapped)

    def test_watchdog_aborts_a_stalled_header_read_and_returns_typed_timeout(self):
        stopped = Event()

        def connector(*args):
            connection = self.fixture.connector(*args)
            connection.sock.shutdown.side_effect = lambda _: stopped.set()
            return connection

        def slow_headers():
            self.assertTrue(stopped.wait(timeout=1))
            raise OSError("PRIVATE socket details must not reach the caller")

        self.fixture.add("example.org", "/a", slow_headers)
        fetcher = web_fetch.Fetcher(resolver=self.fixture.resolver, connector=connector)
        with patch.object(web_fetch, "REQUEST_TIMEOUT", 0.03):
            with self.assertRaises(web_fetch.FetchError) as caught:
                fetcher.fetch("https://example.org/a")
        self.assertEqual(caught.exception.code, "fetch_timeout")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertTrue(stopped.is_set())
        self.assertTrue(self.fixture.connections[-1][-1].closed)


if __name__ == "__main__":
    unittest.main()
