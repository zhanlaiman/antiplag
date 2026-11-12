"""Bounded public-page fetching with pinned DNS, robots and inert text extraction."""

from collections import OrderedDict
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import http.client
import ipaddress
import queue
import re
import socket
import ssl
from threading import BoundedSemaphore, Event, Lock, Thread, Timer
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import zlib


USER_AGENT = "IstokSourceChecker/0.1"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TEXT_CHARACTERS = 200_000
MAX_REDIRECTS = 3
MAX_ROBOTS_REDIRECTS = 5
REQUEST_TIMEOUT = 8
TOTAL_TIMEOUT = 20
HTML_EXTRACTION_VERSION = "html-parser-v4-edit-controls"
HTML_NOTES_EXTRACTION_VERSION = "html-parser-v3-semantic-notes"
_DNS_SLOTS = BoundedSemaphore(4)
_REDIRECTS = {301, 302, 303, 307, 308}


class FetchError(RuntimeError):
    def __init__(self, message, code="fetch_failed", details=None):
        super().__init__(message)
        self.code, self.details = code, details


def _public_address(value):
    try:
        if not isinstance(value, str) or "%" in value:
            return False
        address = ipaddress.ip_address(value)
        return (address.is_global and not address.is_reserved and not address.is_multicast
                and not address.is_loopback and not address.is_link_local
                and (address.version == 4 or not (address.ipv4_mapped or address.sixtofour or address.teredo)))
    except ValueError:
        return False


def _validate_url(url):
    if (not isinstance(url, str) or not 1 <= len(url) <= 4096
            or "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
        raise FetchError("Некорректный публичный URL.", "invalid_url")
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or not parts.netloc.isascii() or parts.username is not None or parts.password is not None:
            raise ValueError()
        host = (parts.hostname or "").lower().rstrip(".")
        if not host or "%" in host:
            raise ValueError()
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as error:
        raise FetchError("Разрешены только публичные http/https ссылки без логина и пароля.", "invalid_url") from error
    if port != (443 if parts.scheme == "https" else 80):
        raise FetchError("Разрешены только стандартные порты 80 и 443.", "port_forbidden")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _public_address(host):
            raise FetchError("Непубличный сетевой адрес запрещён.", "address_not_public")
        host = str(literal)
    else:
        labels = host.split(".")
        if (len(host) > 253 or len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                or any(label.startswith("xn--") for label in labels)
                or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home.arpa"))):
            raise FetchError("Этот адрес сайта не поддерживается.", "hostname_forbidden")
    authority = "[" + host + "]" if ":" in host else host
    try:
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query_string = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
    except UnicodeError as error:
        raise FetchError("Некорректный публичный URL.", "invalid_url") from error
    canonical = urlunsplit((parts.scheme, authority, path, query_string, ""))
    return {"url": canonical, "scheme": parts.scheme, "host": host, "port": port,
            "origin": parts.scheme + "://" + authority, "target": path + ("?" + query_string if query_string else "")}


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, scheme, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self.scheme, self.address = scheme, address

    def connect(self):
        deadline = time.monotonic() + self.timeout
        self.sock = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            if self.scheme == "https":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                self.sock.settimeout(remaining)
                # Default context verifies the certificate chain and original DNS name.
                self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.sock.close()
            raise


def _connector(scheme, host, port, resolved_ip, timeout):
    return _PinnedConnection(scheme, host, port, resolved_ip, timeout)


def _resolver(host, port):
    return [item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)]


def _normalize_whitespace(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "form", "noscript", "template", "svg", "canvas", "iframe", "aside", "head", "title"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BLOCK = {"p", "div", "section", "article", "li", "ul", "ol", "table", "tr", "br", "hr", "blockquote", "pre", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6"}
    CELL = {"td", "th"}
    CLOSE_P = {"address", "article", "aside", "blockquote", "details", "dialog", "div", "dl", "fieldset",
               "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup",
               "hr", "main", "menu", "nav", "ol", "p", "pre", "search", "section", "table", "ul"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.pieces, self.title = [], [], []
        self.nodes = 0
        self.links, self.anchor = [], None

    def _word_boundary(self):
        if self.pieces and self.pieces[-1] and not self.pieces[-1][-1].isspace():
            self.pieces.append(" ")

    def _close_scoped(self, closing, boundaries):
        for index in range(len(self.stack) - 1, -1, -1):
            tag, ignored = self.stack[index]
            if tag in closing:
                if not any(frame[1] for frame in self.stack):
                    if tag in self.BLOCK:
                        self.pieces.append("\n\n")
                    elif tag in self.CELL:
                        self._word_boundary()
                if self.anchor is not None and any(t == "a" for t, _ in self.stack[index:]):
                    self.links.append(self.anchor)
                    self.anchor = None
                del self.stack[index:]
                return
            # A hidden target may end normally. An ignored frame above it is
            # a barrier: implied closures must never expose that frame's data.
            if ignored or tag in boundaries:
                return

    def _implicit_ends(self, tag):
        protected = self.SKIP | {"html"}
        if tag == "body":
            self._close_scoped({"head"}, protected - {"head"})
        if tag in self.CLOSE_P:
            self._close_scoped({"p"}, protected | {"button", "table", "td", "th", "select", "object"})
        if tag == "li":
            self._close_scoped({"li"}, protected | {"ul", "ol", "menu"})
        elif tag in {"dt", "dd"}:
            self._close_scoped({"dt", "dd"}, protected | {"dl"})
        elif tag in self.CELL:
            self._close_scoped(self.CELL, protected | {"tr", "table"})
        elif tag == "tr":
            self._close_scoped({"tr"}, protected | {"table"})
        elif tag in {"tbody", "thead", "tfoot"}:
            self._close_scoped({"tbody", "thead", "tfoot"}, protected | {"table"})

    def handle_starttag(self, tag, attributes):
        self._implicit_ends(tag)
        self.nodes += 1
        if self.nodes > 50_000 or len(self.stack) >= 128:
            raise FetchError("HTML страницы слишком сложный.", "html_too_complex")
        values = dict(attributes)
        if tag == "a":
            self.anchor = None
        style = values.get("style") or ""
        edit_control = tag == "span" and "mw-editsection" in (values.get("class") or "").split()
        ignored = (tag in self.SKIP or "hidden" in values or str(values.get("aria-hidden", "")).lower() == "true"
                   or edit_control
                   or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", style, re.I))
                   or values.get("role") in {"navigation", "banner", "contentinfo", "search"}
                   or bool(re.search(r"(?:^|\s)(?:cookie-banner|cookie-consent|site-header|site-footer)(?:\s|$)",
                                     (values.get("class") or "") + " " + (values.get("id") or ""), re.I)))
        if edit_control and not any(frame[1] for frame in self.stack):
            self._word_boundary()
        if tag == "a" and not ignored and not any(frame[1] for frame in self.stack) and len(self.links) < 1024:
            href = values.get("href", "")
            if href and len(href) <= 4096:
                self.anchor = {"url": href, "text": ""}
        if tag in self.BLOCK and not ignored and not any(frame[1] for frame in self.stack):
            self.pieces.append("\n\n")
        elif tag in self.CELL and not ignored and not any(frame[1] for frame in self.stack):
            self._word_boundary()
        if tag not in self.VOID:
            self.stack.append((tag, ignored))

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            self.links.append(self.anchor)
            self.anchor = None
        if tag in self.BLOCK and not any(frame[1] for frame in self.stack):
            self.pieces.append("\n\n")
        elif tag in self.CELL and any(frame[0] == tag for frame in self.stack) and not any(frame[1] for frame in self.stack):
            self._word_boundary()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.anchor is not None and not any(frame[1] for frame in self.stack):
            self.anchor["text"] = (self.anchor["text"] + data)[:500]
        if any(tag == "title" for tag, _ in self.stack) and all(not ignored or tag in {"head", "title"} for tag, ignored in self.stack):
            if sum(map(len, self.title)) < 1000:
                self.title.append(data[:1000])
        elif not any(frame[1] for frame in self.stack):
            self.pieces.append(data)


class Fetcher:
    def __init__(self, resolver=None, connector=None, clock=None, semantic_notes=True):
        if type(semantic_notes) is not bool:
            raise TypeError("semantic_notes must be a bool")
        self.semantic_notes = semantic_notes
        self.resolver = resolver or _resolver
        self.connector = connector or _connector
        self.clock = clock or time.monotonic
        self.robots = OrderedDict()
        self._robots_lock = Lock()

    def _remaining(self, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise FetchError("Время загрузки страницы истекло.", "fetch_timeout")
        return remaining

    def _resolve(self, target, deadline):
        try:
            literal = ipaddress.ip_address(target["host"])
        except ValueError:
            literal = None
        if literal is not None:
            return [str(literal)]
        if not _DNS_SLOTS.acquire(timeout=self._remaining(deadline)):
            raise FetchError("Служба разрешения адресов занята.", "dns_timeout")
        result = queue.Queue(maxsize=1)

        def resolve():
            try:
                result.put((True, list(self.resolver(target["host"], target["port"]))))
            except Exception:
                result.put((False, None))
            finally:
                _DNS_SLOTS.release()

        try:
            Thread(target=resolve, daemon=True, name="istok-public-dns").start()
        except RuntimeError as error:
            _DNS_SLOTS.release()
            raise FetchError("Служба разрешения адресов недоступна.", "dns_failed") from error
        try:
            success, addresses = result.get(timeout=self._remaining(deadline))
        except queue.Empty as error:
            raise FetchError("Время разрешения адреса истекло.", "dns_timeout") from error
        self._remaining(deadline)
        if not success or not addresses or len(addresses) > 32:
            raise FetchError("Не удалось получить адрес сайта.", "dns_failed")
        if any(not _public_address(address) for address in addresses):
            raise FetchError("Сайт разрешается в непубличный адрес.", "address_not_public")
        return list(dict.fromkeys(addresses))

    def _timeout(self, connection, response, deadline):
        remaining = self._remaining(deadline)
        transport = getattr(connection, "sock", None)
        if transport is None and response is not None:
            transport = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if transport is not None:
            transport.settimeout(remaining)
        return remaining

    def _read_body(self, connection, response, deadline):
        length = response.getheader("Content-Length")
        if length and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
            raise FetchError("Страница превышает допустимый размер.", "response_too_large")
        encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
        # Some servers call an uncompressed response "none"; byte limits still apply.
        if encoding not in {"identity", "none", "gzip"}:
            raise FetchError("Этот способ сжатия страницы не поддерживается.", "unsupported_encoding")
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
        output = bytearray()
        wire = 0
        read = getattr(response, "read1", response.read)
        while True:
            self._timeout(connection, response, deadline)
            block = read(min(65536, MAX_RESPONSE_BYTES - wire + 1))
            if not block:
                break
            wire += len(block)
            if wire > MAX_RESPONSE_BYTES:
                raise FetchError("Страница превышает допустимый размер.", "response_too_large")
            try:
                piece = inflater.decompress(block, MAX_RESPONSE_BYTES - len(output) + 1) if inflater else block
            except zlib.error as error:
                raise FetchError("Некорректно сжатая страница.", "invalid_encoding") from error
            output.extend(piece)
            if len(output) > MAX_RESPONSE_BYTES:
                raise FetchError("Распакованная страница превышает допустимый размер.", "response_too_large")
        if inflater and (not inflater.eof or inflater.unused_data):
            raise FetchError("Некорректно сжатая страница.", "invalid_encoding")
        return bytes(output), wire

    def _request(self, target, overall_deadline, allow_pdf=False):
        deadline = min(overall_deadline, self.clock() + REQUEST_TIMEOUT)
        addresses = self._resolve(target, deadline)
        connection = response = None
        expired = Event()
        watchdog = None

        def abort():
            expired.set()
            transport = getattr(connection, "sock", None)
            if transport is None and response is not None:
                transport = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if transport is not None:
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                    transport.close()
                except OSError:
                    pass

        try:
            connection = self.connector(target["scheme"], target["host"], target["port"], addresses[0], self._remaining(deadline))
            watchdog = Timer(self._remaining(deadline), abort)
            watchdog.daemon = True
            watchdog.start()
            connection.request("GET", target["target"], headers={
                "User-Agent": USER_AGENT, "Accept": "text/html, application/pdf;q=0.9, text/plain;q=0.8",
                "Accept-Encoding": "gzip", "Connection": "close", "Host": urlsplit(target["origin"]).netloc,
            })
            self._timeout(connection, None, deadline)
            response = connection.getresponse()
            self._remaining(deadline)
            if expired.is_set():
                raise FetchError("Время загрузки страницы истекло.", "fetch_timeout")
            headers = {name: response.getheader(name) for name in ("Location", "Content-Type")}
            if response.status in _REDIRECTS or response.status >= 400:
                return response.status, headers, b"", 0
            content_type = (headers["Content-Type"] or "").split(";", 1)[0].strip().lower()
            if content_type not in ({"text/html", "text/plain", "application/pdf"} if allow_pdf else {"text/html", "text/plain"}):
                raise FetchError("Поддерживаются HTML, обычный текст и текстовые PDF.", "unsupported_content_type")
            body, wire = self._read_body(connection, response, deadline)
            return response.status, headers, body, wire
        except FetchError:
            raise
        except (TimeoutError, socket.timeout) as error:
            raise FetchError("Время загрузки страницы истекло.", "fetch_timeout") from error
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as error:
            if expired.is_set():
                raise FetchError("Время загрузки страницы истекло.", "fetch_timeout") from error
            raise FetchError("Не удалось безопасно загрузить страницу.", "connection_failed") from error
        finally:
            if watchdog is not None:
                watchdog.cancel()
            for resource in (response, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except (OSError, http.client.HTTPException):
                        pass

    @staticmethod
    def _decode(body, content_type):
        match = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", content_type or "", re.I)
        if not match:
            match = re.search(br"<meta[^>]+charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", body[:8192], re.I)
        encoding = match.group(1) if match else "utf-8-sig"
        if isinstance(encoding, bytes):
            encoding = encoding.decode("ascii")
        aliases = {"utf8": "utf-8", "utf-8": "utf-8", "utf-8-sig": "utf-8-sig",
                   "windows-1251": "cp1251", "cp1251": "cp1251", "windows-1252": "cp1252",
                   "iso-8859-1": "latin-1", "latin1": "latin-1", "utf-16": "utf-16"}
        selected = aliases.get(encoding.lower())
        if not selected:
            raise FetchError("Кодировка страницы не поддерживается.", "unsupported_charset")
        try:
            return body.decode(selected), selected
        except UnicodeError as error:
            raise FetchError("Не удалось прочитать текст страницы.", "invalid_text_encoding") from error

    def _robots(self, target, deadline):
        origin = target["origin"]
        with self._robots_lock:
            cached = self.robots.get(origin)
            if cached and cached[0] > self.clock():
                self.robots.move_to_end(origin)
                return cached[1]
        robots_url = origin + "/robots.txt"
        current = _validate_url(robots_url)
        policy = None
        for step in range(MAX_ROBOTS_REDIRECTS + 1):
            status, headers, body, _ = self._request(current, deadline)
            if status in _REDIRECTS:
                if step == MAX_ROBOTS_REDIRECTS or not headers["Location"]:
                    raise FetchError("Не удалось проверить robots.txt.", "robots_unavailable")
                # RFC 9309 section 2.3.1.2 binds redirected rules to the original origin.
                following = _validate_url(urljoin(current["url"], headers["Location"]))
                current = following
                continue
            if status == 404:
                policy = True
            elif status >= 500:
                raise FetchError("Сайт временно не отдаёт правила автоматического чтения.", "robots_unavailable")
            elif status in {401, 403}:
                policy = False
            elif 200 <= status < 300:
                text, _ = self._decode(body, headers["Content-Type"])
                policy = RobotFileParser(robots_url)
                policy.parse(text.splitlines())
            else:
                policy = False
            break
        with self._robots_lock:
            self.robots[origin] = (self.clock() + 300, policy)
            self.robots.move_to_end(origin)
            while len(self.robots) > 64:
                self.robots.popitem(last=False)
        return policy

    def fetch(self, url):
        deadline = self.clock() + TOTAL_TIMEOUT
        target = _validate_url(url)
        requested_url = target["url"]
        for step in range(MAX_REDIRECTS + 1):
            policy = self._robots(target, deadline)
            if policy is False or (policy is not True and not policy.can_fetch(USER_AGENT, target["url"])):
                raise FetchError("Загрузка запрещена правилами robots.txt.", "robots_denied")
            status, headers, body, wire = self._request(target, deadline, allow_pdf=True)
            if status in _REDIRECTS:
                if step == MAX_REDIRECTS or not headers["Location"]:
                    raise FetchError("Слишком много перенаправлений страницы.", "too_many_redirects")
                target = _validate_url(urljoin(target["url"], headers["Location"]))
                continue
            if not 200 <= status < 300:
                raise FetchError("Страница недоступна для загрузки.", "http_error", {"status": status})
            mime = headers["Content-Type"].split(";", 1)[0].strip().lower()
            title = target["host"]
            pdf = None
            supplemental_texts = []
            optional_status = None
            links = []
            html_semantic_view = None
            if mime == "application/pdf":
                from plagiarism.pdf_source import PDFError, extract_pdf
                try:
                    pdf = extract_pdf(body, timeout=self._remaining(deadline))
                except PDFError as error:
                    raise FetchError(str(error), error.code) from None
                text = _normalize_whitespace(pdf["text"])
                optional_status = pdf.get("optional_status")
                if optional_status is not None:
                    try:
                        variants = pdf.get("supplemental_texts", [])
                        if not isinstance(optional_status, dict) or not isinstance(variants, list) or len(variants) > 1:
                            raise ValueError("Invalid optional PDF views")
                        for supplemental in variants:
                            if not isinstance(supplemental, dict) or not isinstance(supplemental.get("text"), str):
                                raise ValueError("Invalid optional PDF text")
                            normalized = _normalize_whitespace(supplemental["text"])
                            if (len(normalized.strip()) < 35 or "\0" in normalized
                                    or len(text) + len(normalized) > MAX_TEXT_CHARACTERS):
                                raise ValueError("Invalid optional PDF text")
                            supplemental_texts.append({**supplemental, "text": normalized,
                                                       "text_sha256": sha256(normalized.encode("utf-8")).hexdigest()})
                    except (KeyError, TypeError, ValueError):
                        supplemental_texts = []
                        optional_status = {"engine": "pdfium", "status": "failed", "code": "pdf_parse_failed"}
                title = _normalize_whitespace(pdf.get("title", ""))[:300] or title
                encoding = "pdf-text"
            elif mime == "text/html":
                decoded, encoding = self._decode(body, headers["Content-Type"])
                parser = _TextExtractor()
                try:
                    for offset in range(0, len(decoded), 16384):
                        self._remaining(deadline)
                        parser.feed(decoded[offset:offset + 16384])
                    parser.close()
                except (ValueError, AssertionError) as error:
                    raise FetchError("Не удалось разобрать текст HTML-страницы.", "invalid_html") from error
                text = _normalize_whitespace("".join(parser.pieces))
                title = _normalize_whitespace("".join(parser.title))[:300] or title
                links = [{"url": urljoin(target["url"], link["url"]), "text": link["text"]} for link in parser.links]
                # The complete original extraction above is authoritative for
                # title/links and all original validity/complexity boundaries.
                if self.semantic_notes:
                    from .html_notes import HTMLNotesError, mask_semantic_notes
                    if not text or "\0" in text:
                        raise FetchError("На странице не найден доступный текст.", "empty_text")
                    if len(text) > MAX_TEXT_CHARACTERS:
                        raise FetchError("Извлечённый текст превышает 200000 символов.", "text_too_large")
                    try:
                        self._remaining(deadline)
                        masked, marker_ranges, note_audit = mask_semantic_notes(decoded, void_tags=_TextExtractor.VOID)
                        self._remaining(deadline)
                        if masked != decoded and not note_audit["ancestry_uncertain"]:
                            cleaned = _TextExtractor()
                            for offset in range(0, len(masked), 16384):
                                self._remaining(deadline)
                                cleaned.feed(masked[offset:offset + 16384])
                            cleaned.close()
                            candidate = _normalize_whitespace("".join(cleaned.pieces))
                            # web.run requires at least35characters. Removing
                            # notes must not turn a formerly usable source short.
                            if len(candidate.strip()) >= 35 and candidate != text:
                                from .html_evidence import build_derivation, digest, POLICY, VIEW_ID
                                proof = build_derivation(text, candidate, marker_ranges, sha256(body).hexdigest())
                                html_semantic_view = {"id": VIEW_ID, "policy": POLICY,
                                    "text": candidate, "text_sha256": digest(candidate),
                                    "extraction": HTML_NOTES_EXTRACTION_VERSION,
                                    "raw_html_sha256": sha256(body).hexdigest(), "derivation": proof}
                    except (HTMLNotesError, ValueError, AssertionError):
                        # Uncertain optional inspection retains original text.
                        pass
            else:
                decoded, encoding = self._decode(body, headers["Content-Type"])
                text = _normalize_whitespace(decoded)
            self._remaining(deadline)
            if not text or "\0" in text:
                raise FetchError("На странице не найден доступный текст.", "empty_text")
            if len(text) > MAX_TEXT_CHARACTERS:
                raise FetchError("Извлечённый текст превышает 200000 символов.", "text_too_large")
            return {"url": target["url"], "requested_url": requested_url, "title": title, "text": text, "links": links,
                    "text_sha256": sha256(text.encode("utf-8")).hexdigest(),
                    "fetched_at": datetime.now(timezone.utc).isoformat(), "content_type": mime,
                    "encoding": encoding, "bytes_read": wire, "decoded_bytes": len(body),
                    "robots_url": target["origin"] + "/robots.txt",
                    "extraction": pdf["extraction"] if pdf else HTML_EXTRACTION_VERSION if mime == "text/html" else "plain-v1",
                    **({"html_semantic_view": html_semantic_view,
                        "raw_html_sha256": sha256(body).hexdigest()} if html_semantic_view else {}),
                    **({"pdf_pages": pdf["page_count"], "pdf_parser_version": pdf["parser_version"]} if pdf else {}),
                    **({"supplemental_texts": supplemental_texts, "optional_status": optional_status,
                        "raw_pdf_sha256": sha256(body).hexdigest()} if pdf and optional_status is not None else {})}
        raise FetchError("Слишком много перенаправлений страницы.", "too_many_redirects")
