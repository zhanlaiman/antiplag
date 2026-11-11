"""Discover public sources, fetch evidence, and measure lexical word coverage.

No language model is called. Search snippets never become matching evidence.
"""

from datetime import datetime, timezone
from bisect import bisect_left
from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener
from uuid import uuid4

from plagiarism.auth_provider import NoRedirect
from plagiarism.matching import MATCHER_VERSION, MAX_TEXT_CHARS, MIN_WORDS, _tokens, analyze
from plagiarism.search_credentials import key_for, selected_provider
from plagiarism.pdf_evidence import POLICY as PDF_EVIDENCE_POLICY, analyze_with_supplements
from plagiarism.input_references import extract_named_references


ROOT = Path(__file__).resolve().parent.parent
MAX_QUERIES = 6
MAX_URLS = 12
SOURCE_FOLLOWUP_POLICY = "productive-priority-v1"
QUERY_STRATEGY = "uniform-quoted-v1"
DISTINCTIVE_STRATEGY = "distinctive-paragraph-v1"
HYBRID_STRATEGY = "hybrid-paragraph-v1"
CITATION_STRATEGY = "hybrid-citation-v2"
COVERAGE_STRATEGY = "hybrid-paragraph-v3"
DENSE_STRATEGY = "hybrid-citation-v4-dense"
DEFAULT_QUERY_STRATEGY = DENSE_STRATEGY
DOMAIN_URL_STRATEGY = "domain-round-robin-v1"
BALANCED_URL_STRATEGY = "hybrid-domain-query-v2"
PATH_URL_STRATEGY = "hybrid-input-path-v3"
DEFAULT_URL_STRATEGY = PATH_URL_STRATEGY
DEFAULT_NAMED_REFERENCES = True
DEFAULT_ZERO_FALLBACK = True
DEFAULT_WIKI_FOLLOWUP = True
ZERO_FALLBACK_POLICY = "initial-zero-extra-twelve-v1"
ZERO_FALLBACK_EXTRA_URLS = 12
ZERO_FALLBACK_SECONDS = 300
SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
FETCH_FAILURE_MESSAGES = {
    "fetch_timeout": "Сайт не ответил за отведённое время.",
    "robots_denied": "Сайт запрещает автоматическое чтение этой страницы.",
    "robots_unavailable": "Не удалось получить правила автоматического чтения сайта.",
    "unsupported_encoding": "Сайт использует неподдерживаемый способ сжатия страницы.",
}


class WebSearchError(RuntimeError):
    def __init__(self, message, code="web_search_failed", details=None):
        super().__init__(message)
        self.code, self.details = code, details


def _distinctive_windows(text, tokens, width, count, ranked=False):
    """Rank phrases within sentences using only frequencies in the submitted text."""
    words = [token.value for token in tokens]
    counts = Counter(words)
    pairs = Counter(zip(words, words[1:]))
    # Very short words and serial numbers by themselves carry little topic information.
    weights = [min(len(word), 12) / counts[word] if len(word) > 3 and not word.isdecimal()
               else 0 for word in words]
    pair_weights = [1 / pairs[pair] for pair in zip(words, words[1:])]
    segments, paragraph, first = [], 0, 0
    for i in range(1, len(tokens)):
        gap = text[tokens[i - 1].end:tokens[i].start]
        if re.search(r"[.!?…\r\n]", gap):
            segments.append((paragraph, first, i))
            paragraph += int("\n" in gap or "\r" in gap)
            first = i
    segments.append((paragraph, first, len(tokens)))
    candidates = []
    for paragraph, start, end in segments:
        size = min(width, end - start)
        if size < MIN_WORDS:
            continue
        for i in range(start, end - size + 1):
            weight = (sum(weights[i:i + size]) + 3 * sum(pair_weights[i:i + size - 1])) / size
            candidates.append((-weight, i, i + size, paragraph))
    if not candidates:
        # A list of one-word lines still admits the original token windows.
        return None
    candidates.sort()
    weights_by_window = {(start, end): weight for weight, start, end, _ in candidates}
    def selected_windows(chosen):
        return sorted(chosen, key=lambda window: (weights_by_window[window], *window)) if ranked else sorted(chosen)
    chosen, covered, paragraphs, phrases = [], set(), set(), set()
    for distinct_paragraph in (True, False):
        for _, start, end, paragraph in candidates:
            if (distinct_paragraph and paragraph in paragraphs) or not covered.isdisjoint(range(start, end)):
                continue
            phrase = ' '.join(word[:40] for word in words[start:end])
            if phrase in phrases:
                continue
            chosen.append((start, end))
            covered.update(range(start, end))
            paragraphs.add(paragraph)
            phrases.add(phrase)
            if len(chosen) == count:
                return selected_windows(chosen)
    return selected_windows(chosen)


def _citation_queries(text):
    """Use quotation boundaries and dated titles as text-only source clues."""
    tokens = _tokens(text)
    candidates = []
    starts = [token.start for token in tokens]
    for match in re.finditer(r'«([^«»]+)»|“([^“”]+)”|„([^„“]+)“|"([^"\n]+)"', text):
        group = next(i for i in range(1, 5) if match.group(i) is not None)
        start, end = match.span(group)
        quoted_start = bisect_left(starts, start)
        first, last = quoted_start, bisect_left(starts, end)
        if first == last:
            continue
        if last - first < MIN_WORDS:
            # A two-word title alone is weak. A nearby year supplies useful context.
            first = max(0, last - MIN_WORDS)
            context = tokens[first:quoted_start]
            if last - first < MIN_WORDS or not any(re.fullmatch(r"(?:1[0-9]{3}|20[0-9]{2})", t.value) for t in context):
                continue
            # Do not attach a title to a previous sentence or paragraph.
            if re.search(r"[.!?\n]", text[tokens[first].start:tokens[quoted_start].start]):
                continue
        window = tokens[first:min(last, first + 10)]
        query = '"' + ' '.join(token.value[:40] for token in window) + '"'
        if len(query) <= 390:
            candidates.append({"query": query, "start": window[0].start, "end": window[-1].end})
    return candidates


def _hybrid_queries(text, count, priority=(), diverse=False):
    """Split the same request allowance between two text-only planners."""
    if count == 1:
        return list(priority[:1]) or plan_queries(text, limit=1, strategy=DISTINCTIVE_STRATEGY)
    uniform = plan_queries(text, limit=(count + 1) // 2, strategy=QUERY_STRATEGY)
    distinctive = list(priority)
    if diverse:
        covered_paragraphs = {text.count("\n", 0, item["start"]) for item in uniform}
        alternatives = plan_queries(text, limit=MAX_QUERIES, strategy=DISTINCTIVE_STRATEGY)
        distinctive.extend(item for item in alternatives if text.count("\n", 0, item["start"]) not in covered_paragraphs)
    distinctive.extend(plan_queries(text, limit=count // 2, strategy=DISTINCTIVE_STRATEGY))
    candidates = []
    for i in range(max(len(uniform), len(distinctive))):
        candidates.extend(group[i] for group in (uniform, distinctive) if i < len(group))
    # Repeated wording can make both planners choose the same phrase.
    candidates.extend(plan_queries(text, limit=count, strategy=DISTINCTIVE_STRATEGY))
    candidates.extend(plan_queries(text, limit=count, strategy=QUERY_STRATEGY))
    result, seen = [], set()
    for candidate in candidates:
        if candidate["query"] in seen:
            continue
        seen.add(candidate["query"])
        result.append(candidate)
        if len(result) == count:
            break
    return result


def _dense_queries(text, limit):
    """Keep the previous plan and fill unused slots with distinct phrases."""
    planned = plan_queries(text, limit=limit, strategy=CITATION_STRATEGY)
    if len(planned) == limit:
        return planned
    tokens = _tokens(text)
    width = min(10, len(tokens))
    windows = _distinctive_windows(text, tokens, width, MAX_QUERIES, ranked=True)
    if windows is None:
        starts = [round(i * (len(tokens) - width) / (MAX_QUERIES - 1)) for i in range(MAX_QUERIES)]
        windows = [(start, start + width) for start in starts]
    seen = {item["query"] for item in planned}
    for start, end in windows:
        window = tokens[start:end]
        query = '"' + ' '.join(token.value[:40] for token in window) + '"'
        if len(query) > 390:
            query = query[:389].rsplit(' ', 1)[0] + '"'
            window = window[:len(query[1:-1].split())]
        if query in seen:
            continue
        planned.append({"query": query, "start": window[0].start, "end": window[-1].end})
        seen.add(query)
        if len(planned) == limit:
            break
    return planned


def plan_queries(text, limit=MAX_QUERIES, strategy=DEFAULT_QUERY_STRATEGY):
    if not isinstance(text, str) or not 35 <= len(text) <= MAX_TEXT_CHARS:
        raise ValueError("Для проверки по интернету нужен текст от 35 до 200 000 символов.")
    if type(limit) is not int or not 1 <= limit <= MAX_QUERIES:
        raise ValueError("Неверный лимит поисковых запросов.")
    if strategy not in (QUERY_STRATEGY, DISTINCTIVE_STRATEGY, HYBRID_STRATEGY, CITATION_STRATEGY, COVERAGE_STRATEGY, DENSE_STRATEGY):
        raise ValueError("Неизвестный способ выбора поисковых фраз.")
    tokens = _tokens(text)
    if len(tokens) < MIN_WORDS:
        raise ValueError("Для поиска источников нужно не менее шести слов.")
    if strategy == DENSE_STRATEGY:
        return _dense_queries(text, limit)
    width = min(10, len(tokens))
    count = min(limit, max(1, (len(tokens) + 79) // 80))
    if strategy == COVERAGE_STRATEGY:
        return _hybrid_queries(text, count, _citation_queries(text), diverse=True)
    if strategy == CITATION_STRATEGY:
        return _hybrid_queries(text, count, _citation_queries(text))
    if strategy == HYBRID_STRATEGY:
        return _hybrid_queries(text, count)
    windows = _distinctive_windows(text, tokens, width, count) if strategy == DISTINCTIVE_STRATEGY else None
    if windows is None:
        starts = [round(number * (len(tokens) - width) / max(count - 1, 1)) for number in range(count)]
        windows = [(start, start + width) for start in starts]
    queries, seen = [], set()
    for start, end in windows:
        window = tokens[start:end]
        words = [token.value for token in window]
        # Very long encoded identifiers must not consume a provider's query budget.
        query = '"' + ' '.join(word[:40] for word in words) + '"'
        if len(query) > 390:
            query = query[:389].rsplit(' ', 1)[0] + '"'
            window = window[:len(query[1:-1].split())]
        if query in seen:
            continue
        seen.add(query)
        queries.append({"query": query, "start": window[0].start, "end": window[-1].end})
    return queries


def status():
    provider = selected_provider()
    configured = (bool(key_for(provider)) and provider in {"tavily", "brave"}
                  and (provider != "brave" or os.environ.get("ISTOK_BRAVE_STORAGE_RIGHTS") == "1"))
    error = ("Поиск по интернету ещё не подключён. Нужен серверный ключ поискового API."
             if provider == "tavily" else
             "Поиск по интернету ещё не подключён. Нужны ключ Brave и подтверждённые условия хранения результатов."
             if provider == "brave" else "Выбранный поисковый сервис недоступен.")
    zero_fallback = os.environ.get("ISTOK_WEB_ZERO_FALLBACK", "1" if DEFAULT_ZERO_FALLBACK else "0") == "1"
    wiki_followup = os.environ.get("ISTOK_WEB_WIKI_FOLLOWUP", "1" if DEFAULT_WIKI_FOLLOWUP else "0") == "1"
    return {"provider": provider, "configured": configured,
            "max_queries": MAX_QUERIES + (2 if wiki_followup else 0),
            "max_primary_queries": MAX_QUERIES, "max_secondary_queries": 2 if wiki_followup else 0,
            "secondary_provider": "mediawiki" if wiki_followup else None,
            "wiki_followup_enabled": wiki_followup,
            "max_urls": 26 if wiki_followup else MAX_URLS + ZERO_FALLBACK_EXTRA_URLS if zero_fallback else MAX_URLS,
            "max_initial_urls": MAX_URLS, "zero_fallback_enabled": zero_fallback,
            "error": None if configured else error}


class SearchBudget:
    """Count all attempted provider calls; an uncertain call is never retried."""
    def __init__(self, path=None, limit=900, now=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Тестовый лимит поискового API должен быть от 1 до 1000 запросов в месяц.")
        self.path = Path(path or ROOT / "data/search-budget.sqlite3")
        self.limit, self.now = limit, now or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, month TEXT NOT NULL, state TEXT NOT NULL)")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def reserve(self, identifier):
        month = self.now().astimezone(timezone.utc).strftime("%Y-%m")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM calls WHERE id=?", (identifier,)).fetchone():
                raise WebSearchError("Этот поисковый запрос уже отправлялся. Автоматический повтор отключён.", "search_already_attempted")
            used = connection.execute("SELECT COUNT(*) FROM calls WHERE month=?", (month,)).fetchone()[0]
            if used >= self.limit:
                raise WebSearchError("Достигнут общий месячный лимит поискового API.", "search_budget_exhausted")
            connection.execute("INSERT INTO calls VALUES(?,?,'started')", (identifier, month))

    def finish(self, identifier):
        with self.connect() as connection:
            connection.execute("UPDATE calls SET state='complete' WHERE id=?", (identifier,))


class BraveSearch:
    provider = "brave"

    def __init__(self, budget=None):
        self.key = key_for("brave")
        if (not self.key or len(self.key) > 1024 or any(not 33 <= ord(char) <= 126 for char in self.key)
                or os.environ.get("ISTOK_BRAVE_STORAGE_RIGHTS") != "1"):
            raise WebSearchError("Нужны серверный ключ Brave и подтверждённые условия хранения результатов.", "web_not_configured")
        self.budget = budget or SearchBudget()
        self.opener = build_opener(NoRedirect())

    def search(self, query, request_id):
        if (not isinstance(query, str) or not query.strip() or len(query) > 400
                or any(ord(character) < 32 for character in query)
                or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)):
            raise WebSearchError("Некорректный поисковый запрос.", "invalid_search_query")
        self.budget.reserve(request_id)
        parameters = {"q": query, "count": 5, "result_filter": "web", "safesearch": "moderate"}
        if re.search(r"[а-яё]", query, re.I):
            parameters["search_lang"] = "ru"
        request = Request(SEARCH_ENDPOINT + "?" + urlencode(parameters), headers={
            "Accept": "application/json", "X-Subscription-Token": self.key})
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("oversize")
            data = json.loads(raw)
            if (not isinstance(data, dict) or any(key in data for key in ("error", "errors", "detail"))
                    or data.get("success") is False or data.get("status") in {"error", "failed", "failure"}
                    or not isinstance(data.get("web"), dict)):
                raise ValueError("invalid search response")
            results = data["web"].get("results")
            if not isinstance(results, list) or len(results) > 5:
                raise ValueError("invalid results")
            clean = []
            for result in results[:5]:
                if (not isinstance(result, dict) or not isinstance(result.get("url"), str)
                        or len(result["url"]) > 4096 or self.key in result["url"]):
                    raise ValueError("invalid search result")
                clean.append({"url": result["url"]})
            self.budget.finish(request_id)
            return clean
        except HTTPError as error:
            code = error.code
            error.close()
            raise WebSearchError("Поисковый сервис не выполнил запрос. Проверка неполная.",
                                 "search_rate_limited" if code == 429 else "search_credentials_invalid"
                                 if code in {401, 403} else "search_budget_exhausted" if code == 402 else "search_provider_error") from None
        except (URLError, OSError, ValueError, UnicodeError, AttributeError, TypeError):
            raise WebSearchError("Поисковый сервис недоступен. Проверка неполная.", "search_provider_error") from None


def _canonical_url(value):
    try:
        if not isinstance(value, str) or len(value) > 4096 or "\\" in value:
            return None
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
            return None
        if any(ord(character) < 33 or ord(character) == 127 for character in value):
            return None
        host = parts.hostname.lower().rstrip(".")
        if not host or not host.isascii() or "%" in host:
            return None
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            return None
        authority = "[" + host + "]" if ":" in host else host
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            authority += ":" + str(port)
        return urlunsplit((parts.scheme.lower(), authority,
                           quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                           quote(parts.query, safe="/%?:@!$&'()*+,;=-._~"), ""))
    except (ValueError, TypeError):
        return None


def _rejected_url(value):
    """Identify an unavailable result without persisting credentials or query data."""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parts = urlsplit(value)
        if parts.hostname:
            return urlunsplit((parts.scheme, parts.hostname, parts.path[:500], "", "")).encode("utf-8", errors="replace").decode("utf-8")
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _word_coverage(text, matches):
    tokens = _tokens(text)
    intervals = [(item["start"], item["end"]) for item in matches if item.get("kind") == "exact"]
    covered = sum(any(start <= token.start and token.end <= end for start, end in intervals) for token in tokens)
    return {"total_words": len(tokens), "matched_words": covered,
            "matched_word_percent": round(100 * covered / len(tokens), 1) if tokens else 0}


def _supplemental_source_views(source, fetched):
    """Keep one bounded, independently stored view of the same physical PDF."""
    if fetched.get("content_type") != "application/pdf":
        return []
    values = fetched.get("supplemental_texts", [])
    if not isinstance(values, list) or len(values) > 1:
        return []
    raw_digest = fetched.get("raw_pdf_sha256")
    pages = fetched.get("pdf_pages")
    if (not isinstance(raw_digest, str) or re.fullmatch(r"[0-9a-f]{64}", raw_digest) is None
            or type(pages) is not int or not 1 <= pages <= 40):
        return []
    views = []
    for value in values:
        if (not isinstance(value, dict) or value.get("id") != "pdfium-v1"
                or value.get("parser_version") != "5.14.0" or value.get("extraction") != "pdfium-text-v1"
                or value.get("raw_pdf_sha256") != raw_digest or value.get("page_count") != pages
                or not isinstance(value.get("text"), str) or len(value["text"].strip()) < 35
                or len(source["text"]) + len(value["text"]) > MAX_TEXT_CHARS or "\0" in value["text"]):
            continue
        try:
            digest = sha256(value["text"].encode()).hexdigest()
        except UnicodeError:
            continue
        if value.get("text_sha256") not in (None, digest):
            continue
        views.append({"id": source["id"] + "-pdfium-v1", "representation_of": source["id"],
            "title": source["title"], "url": source["url"], "text": value["text"],
            "provenance": {**source["provenance"], "text_sha256": digest,
                           "extraction": "pdfium-text-v1", "parser_version": "5.14.0",
                           "evidence_policy": PDF_EVIDENCE_POLICY,
                           "raw_pdf_sha256": raw_digest, "pdf_pages": pages}})
    return views


def _related_link(fetched, text, seen):
    """Select one on-site content link by wording in the submitted document."""
    words = {t.value for t in _tokens(text) if len(t.value) >= 5 and not t.value.isdecimal()}
    candidates = []
    origin = urlsplit(fetched["url"]).netloc
    for item in fetched.get("links", []):
        url = _canonical_url(item.get("url"))
        if not url or url in seen or urlsplit(url).netloc != origin or urlsplit(url).query:
            continue
        label = {t.value for t in _tokens(item.get("text", "")) if len(t.value) >= 5 and not t.value.isdecimal()}
        common = words & label
        if common:
            candidates.append((len(common) / max(1, len(label)), url))
    return max(candidates)[1] if candidates else None


def _fetch_order(urls, query_groups, strategy):
    """Order discovered URLs without inspecting expected sources or page text."""
    if strategy not in (DOMAIN_URL_STRATEGY, BALANCED_URL_STRATEGY, PATH_URL_STRATEGY):
        raise ValueError("Неизвестный способ отбора страниц для чтения.")
    grouped, allowed = {}, set()
    for url in urls:
        if url not in allowed:
            allowed.add(url)
            grouped.setdefault(urlsplit(url).hostname, []).append(url)
    domains = []
    while any(grouped.values()):
        for group in grouped.values():
            if group:
                domains.append(group.pop(0))
    if strategy == DOMAIN_URL_STRATEGY:
        return domains
    ordered = domains[:MAX_URLS // 2]
    seen = set(ordered)
    # Preserve provider rank within each query, including unsupported-result gaps.
    for rank in range(max(map(len, query_groups), default=0)):
        for group in query_groups:
            if rank >= len(group):
                continue
            url = group[rank]
            if url in allowed and url not in seen:
                seen.add(url)
                ordered.append(url)
    # Callers may have discovered URLs outside the supplied query groups.
    ordered.extend(url for url in domains if url not in seen)
    return ordered


def _rank_url_paths(ordered, text):
    """Stably promote URLs whose path contains words from the input document."""
    words = {token.value for token in _tokens(text) if len(token.value) >= 4 and not token.value.isdecimal()}
    def relevance(url):
        path_words = {token.value for token in _tokens(unquote(urlsplit(url).path))
                      if len(token.value) >= 4 and not token.value.isdecimal()}
        return len(words & path_words)
    return sorted(ordered, key=lambda url: -relevance(url))


def run(text, request_id=None, progress=None, searcher=None, fetcher=None, query_strategy=None, url_strategy=None,
        use_named_references=None, use_zero_fallback=None, use_wiki_followup=None, wiki_searcher=None):
    """Produce a report payload, preserving every failure and the bounded scope.

    This is a first lexical web search, not a guarantee over every internet page.
    If a planned query or attempted page fails, no final uniqueness is claimed.
    """
    started = time.monotonic()
    custom_io = searcher is not None or fetcher is not None
    if use_wiki_followup is None:
        configured = os.environ.get("ISTOK_WEB_WIKI_FOLLOWUP")
        if configured not in (None, "0", "1"):
            raise ValueError("Неверная настройка дополнительного поиска Wikipedia.")
        enabled = DEFAULT_WIKI_FOLLOWUP if configured is None else configured == "1"
        # Supplying primary test/replay adapters must not construct a real
        # secondary provider implicitly. An explicit opt-in or adapter enables it.
        use_wiki_followup = enabled and (not custom_io or wiki_searcher is not None)
    if type(use_wiki_followup) is not bool:
        raise ValueError("Неверная настройка дополнительного поиска Wikipedia.")
    if url_strategy is None:
        url_strategy = os.environ.get("ISTOK_WEB_URL_STRATEGY", DEFAULT_URL_STRATEGY)
    if url_strategy not in (DOMAIN_URL_STRATEGY, BALANCED_URL_STRATEGY, PATH_URL_STRATEGY):
        raise ValueError("Неизвестный способ отбора страниц для чтения.")
    if use_named_references is None:
        configured = os.environ.get("ISTOK_WEB_NAMED_REFERENCES")
        if configured not in (None, "0", "1"):
            raise ValueError("Неверная настройка указанных в тексте источников.")
        use_named_references = DEFAULT_NAMED_REFERENCES if configured is None else configured == "1"
    if type(use_named_references) is not bool:
        raise ValueError("Неверная настройка указанных в тексте источников.")
    if use_zero_fallback is None:
        configured = os.environ.get("ISTOK_WEB_ZERO_FALLBACK")
        if configured not in (None, "0", "1"):
            raise ValueError("Неверная настройка дополнительного чтения страниц.")
        use_zero_fallback = DEFAULT_ZERO_FALLBACK if configured is None else configured == "1"
    if type(use_zero_fallback) is not bool:
        raise ValueError("Неверная настройка дополнительного чтения страниц.")
    max_page_attempts = MAX_URLS + ZERO_FALLBACK_EXTRA_URLS if use_zero_fallback else MAX_URLS
    if query_strategy is None:
        query_strategy = os.environ.get("ISTOK_WEB_QUERY_STRATEGY", DEFAULT_QUERY_STRATEGY)
    queries = plan_queries(text, strategy=query_strategy)
    request_id = request_id or uuid4().hex
    if searcher is None:
        provider = selected_provider()
        if provider == "tavily":
            from plagiarism.search_tavily import TavilySearch
            searcher = TavilySearch(SearchBudget())
        elif provider == "brave":
            searcher = BraveSearch()
        else:
            raise WebSearchError("Выбранный поисковый сервис недоступен.", "web_not_configured")
    provider = getattr(searcher, "provider", selected_provider())
    if fetcher is None:
        from plagiarism.web_fetch import Fetcher
        fetcher = Fetcher()
    urls, seen, errors = [], set(), []
    query_groups = []
    rejected_urls, rejected_seen = [], set()
    queries_attempted = queries_completed = discovered = duplicates = 0
    stopped_code = None
    for index, item in enumerate(queries):
        if progress:
            progress("Ищем опубликованные источники", index, len(queries), phase="web_search")
        try:
            queries_attempted += 1
            call_id = sha256((request_id + "\0" + str(index)).encode()).hexdigest()
            results = searcher.search(item["query"], call_id)
            if not isinstance(results, list) or any(not isinstance(result, dict) for result in results):
                raise WebSearchError("Некорректный ответ поискового сервиса.", "search_provider_error")
            queries_completed += 1
            group = []
            query_groups.append(group)
            for result in results:
                discovered += 1
                raw_url = result.get("url")
                url = _canonical_url(raw_url)
                group.append(url)
                if not url:
                    marker = sha256(str(raw_url).encode("utf-8", errors="replace")).hexdigest()
                    if marker not in rejected_seen:
                        rejected_seen.add(marker)
                        rejected = {"url": _rejected_url(raw_url), "code": "unsupported_result_url", "query_index": index}
                        rejected_urls.append(rejected)
                        errors.append({**rejected, "stage": "discover", "message": "Найденную ссылку нельзя безопасно проверить."})
                    else:
                        duplicates += 1
                elif url not in seen:
                    seen.add(url)
                    urls.append(url)
                else:
                    duplicates += 1
        except (WebSearchError, OSError, ValueError) as error:
            errors.append({"stage": "search", "query_index": index, "code": getattr(error, "code", "search_error"),
                           "message": "Не удалось выполнить один из поисковых запросов."})
            if getattr(error, "code", "") in {"search_rate_limited", "search_budget_exhausted",
                                                "search_credentials_invalid", "search_already_attempted"}:
                stopped_code = error.code
                break
    queries_unattempted = len(queries) - queries_attempted
    # Preserve the complete search-only PATH order before references, the
    # twelve-page prefix, and productive-link replacements change its queue.
    fallback_search_order = (_rank_url_paths(_fetch_order(urls, query_groups, PATH_URL_STRATEGY), text)
                             if use_zero_fallback else [])
    input_references = extract_named_references(text) if use_named_references else []
    reference_urls = [reference["url"] for reference in input_references]
    for url in reference_urls:
        if url not in seen:
            seen.add(url)
            urls.append(url)
    ordered = _fetch_order(urls, query_groups, url_strategy)
    if url_strategy == PATH_URL_STRATEGY:
        ordered = _rank_url_paths(ordered, text)
    if reference_urls:
        ordered = list(dict.fromkeys(reference_urls + ordered))
    ordered = ordered[:MAX_URLS]
    outside_limit = max(0, len(urls) - len(ordered))
    selected_urls = set(ordered)
    skipped_urls = [url for url in urls if url not in selected_urls]
    if outside_limit:
        errors.append({"stage": "limit", "code": "url_limit_reached", "count": outside_limit,
                       "message": "Часть найденных ссылок не проверена из-за лимита загрузок."})
    sources, final_urls = [], set()
    supplements, optional_pdf_status = [], []
    urls_attempted = 0
    attempted_urls = []
    link_discoveries, linked_urls = [], set()
    priority_link_replacements = 0
    priority_link_credit, priority_link_urls = 1, set()
    confirmed_matches, fully_covered = [], False
    zero_phase = {"triggered": False, "grounded_initial_words": None,
                  "in_extra_phase": False, "extra_attempts": 0, "deadline_reached": False, "prefix_sources": []}

    def reading_queue():
        # Two phases preserve the original mutable prefix. A handled final
        # fetch failure or continue resumes this generator; an unhandled
        # exception leaves the function without opening another phase.
        yield from enumerate(ordered)
        if not use_zero_fallback:
            return
        zero_phase["prefix_sources"] = list(sources) + list(supplements)
        gate = analyze_with_supplements(text, sources, supplements)
        zero_phase["grounded_initial_words"] = _word_coverage(text, gate["matches"])["matched_words"]
        if zero_phase["grounded_initial_words"]:
            return
        zero_phase["triggered"] = True
        zero_phase["in_extra_phase"] = True
        from plagiarism.web_fetch import TOTAL_TIMEOUT
        for candidate in fallback_search_order:
            if candidate in attempted_urls or candidate in final_urls:
                continue
            if zero_phase["extra_attempts"] >= ZERO_FALLBACK_EXTRA_URLS or urls_attempted >= max_page_attempts:
                break
            if time.monotonic() - started + TOTAL_TIMEOUT > ZERO_FALLBACK_SECONDS:
                zero_phase["deadline_reached"] = True
                errors.append({"stage": "limit", "code": "extra_fetch_time_budget_reached",
                    "message": "Дополнительное чтение остановлено из-за общего времени проверки."})
                break
            ordered.append(candidate)
            zero_phase["extra_attempts"] += 1
            yield len(ordered) - 1, candidate

    for index, url in reading_queue():
        if progress:
            progress("Сверяем доступные страницы", index, len(ordered), phase="web_fetch")
        try:
            if url in final_urls:
                duplicates += 1
                continue
            urls_attempted += 1
            attempted_urls.append(url)
            fetched = fetcher.fetch(url)
            final = _canonical_url(fetched["url"])
            if not final:
                raise ValueError("invalid fetched URL")
            if final in final_urls:
                duplicates += 1
                continue
            if not isinstance(fetched.get("text"), str) or len(fetched["text"].strip()) < 35:
                raise ValueError("insufficient source text")
            final_urls.add(final)
            sources.append({"id": "web-" + sha256(final.encode()).hexdigest()[:24],
                            "title": fetched.get("title") or urlsplit(final).hostname, "url": final,
                            "text": fetched["text"], "provenance": {
                                "origin": "web_fetch", "fetched_at": fetched.get("fetched_at"),
                                "text_sha256": sha256(fetched["text"].encode()).hexdigest(),
                                "license_status": "not_verified", "training_consent": False}})
            page_supplements = _supplemental_source_views(sources[-1], fetched)
            supplements.extend(page_supplements)
            if fetched.get("content_type") == "application/pdf" and isinstance(fetched.get("optional_status"), dict):
                optional_pdf_status.append({**{key: fetched["optional_status"][key]
                    for key in ("engine", "status", "code", "reason") if key in fetched["optional_status"]}, "url": final})
            if url in linked_urls:
                previous_link_words = _word_coverage(text, confirmed_matches)["matched_words"]
                linked_matches = analyze_with_supplements(text, [sources[-1]], page_supplements)["matches"]
                confirmed_matches.extend(linked_matches)
                linked_coverage = _word_coverage(text, confirmed_matches)
                if url in priority_link_urls and linked_coverage["matched_words"] > previous_link_words:
                    priority_link_credit = 1
                fully_covered = linked_coverage["matched_words"] == linked_coverage["total_words"]
            if not zero_phase["in_extra_phase"] and query_strategy in (CITATION_STRATEGY, COVERAGE_STRATEGY, DENSE_STRATEGY) and url not in linked_urls and not fully_covered:
                page_matches = analyze_with_supplements(text, [sources[-1]], page_supplements)["matches"]
                confirmed_matches.extend(page_matches)
                coverage = _word_coverage(text, confirmed_matches)
                fully_covered = coverage["matched_words"] == coverage["total_words"]
                related = _related_link(fetched, text, seen | final_urls)
                if related and not fully_covered and not _word_coverage(text, page_matches)["matched_words"]:
                    if len(ordered) >= MAX_URLS:
                        # A priority slot is renewed only after the previous
                        # linked page adds novel exact evidence. Failed or
                        # unproductive navigation cannot consume the tail.
                        if priority_link_credit < 1:
                            continue
                        # A full initial queue must not disable a relevant
                        # source link. Replace an unread search result while
                        # retaining the same twelve-load ceiling.
                        replaceable = [i for i in range(index + 1, len(ordered)) if ordered[i] not in linked_urls]
                        if not replaceable:
                            continue
                        displaced = ordered.pop(replaceable[-1])
                        priority_link_replacements += 1
                        priority_link_credit -= 1
                        priority_link_urls.add(related)
                        skipped_urls.append(displaced)
                        outside_limit += 1
                        limit_error = next((e for e in errors if e["code"] == "url_limit_reached"), None)
                        if limit_error is None:
                            errors.append({"stage": "limit", "code": "url_limit_reached", "count": outside_limit,
                                           "message": "Часть найденных ссылок не проверена из-за лимита загрузок."})
                        else:
                            limit_error["count"] = outside_limit
                        ordered.insert(index + 1, related)
                    else:
                        ordered.append(related)
                    seen.add(related)
                    urls.append(related)
                    linked_urls.add(related)
                    link_discoveries.append({"parent_url": final, "url": related, "basis": "input_anchor_overlap"})
        except (RuntimeError, OSError, ValueError, KeyError) as error:
            code = getattr(error, "code", "fetch_error")
            errors.append({"stage": "fetch", "url": url, "code": code,
                           "message": FETCH_FAILURE_MESSAGES.get(code, "Страница недоступна или не разрешает автоматическую загрузку.")})
    if zero_phase["triggered"]:
        # Failed attempts stay attempts; unrequested search/link/reference URLs
        # remain outside the checked scope. Redirect aliases already read do not.
        skipped_urls = list(dict.fromkeys(url for url in urls
            if url not in attempted_urls and url not in final_urls))
        outside_limit = len(skipped_urls)
        errors = [error for error in errors if error.get("code") != "url_limit_reached"]
        if outside_limit:
            errors.append({"stage": "limit", "code": "url_limit_reached", "count": outside_limit,
                           "message": "Часть найденных ссылок не проверена из-за лимита загрузок."})
    if not sources:
        errors.append({"stage": "coverage", "code": "no_sources_checked",
                       "message": "Не проверено ни одного источника. Уникальность не установлена."})
    result = analyze_with_supplements(text, sources, supplements)
    words = _word_coverage(text, result["matches"])
    complete = not errors and not queries_unattempted
    total, unmatched = words["total_words"], words["total_words"] - words["matched_words"]
    uniqueness = round(100 * unmatched / total, 1) if complete else None
    result["stats"].update(words, uniqueness_percent=uniqueness)
    if not complete and words["matched_words"]:
        # An upper bound must round upward, including below one decimal place.
        result["stats"]["uniqueness_upper_bound_percent"] = ((1000 * unmatched + total - 1) // total) / 10
    from plagiarism import pdf_source, web_fetch

    source_snapshots = sources + supplements
    if zero_phase["triggered"]:
        prefix_ids = {source["id"] for source in zero_phase["prefix_sources"]}
        source_snapshots = zero_phase["prefix_sources"] + [source for source in source_snapshots if source["id"] not in prefix_ids]
    result.update(text=text, sources=source_snapshots, scope="web_search", web={
        "provider": provider, "status": "complete" if complete else "partial",
        "post_wiki_enabled": False,
        "query_strategy": query_strategy, "url_strategy": url_strategy, "matcher_version": MATCHER_VERSION,
        "zero_fallback_enabled": use_zero_fallback,
        "zero_fallback_policy": ZERO_FALLBACK_POLICY if use_zero_fallback else None,
        "zero_fallback_triggered": zero_phase["triggered"],
        "zero_fallback_initial_exact_words": zero_phase["grounded_initial_words"],
        "zero_fallback_extra_attempts": zero_phase["extra_attempts"],
        "zero_fallback_deadline_reached": zero_phase["deadline_reached"],
        "zero_fallback_total_budget_seconds": ZERO_FALLBACK_SECONDS if use_zero_fallback else None,
        "max_page_attempts": max_page_attempts,
        "queries_planned": len(queries), "queries_attempted": queries_attempted, "queries_completed": queries_completed,
        "queries_unattempted": queries_unattempted, "query_stop_code": stopped_code,
        "query_spans": [{"start": item["start"], "end": item["end"]} for item in queries],
        "urls_found": len(urls) + len(rejected_urls), "urls_supported": len(urls),
        "urls_discovered": discovered + len(link_discoveries) + len(input_references),
        "search_urls_discovered": discovered, "linked_urls_discovered": len(link_discoveries),
        "named_references_enabled": use_named_references,
        "input_reference_urls_discovered": len(input_references),
        "input_references": input_references,
        "link_discoveries": link_discoveries,
        "priority_link_replacements": priority_link_replacements,
        "source_followup_policy": SOURCE_FOLLOWUP_POLICY,
        "priority_link_budget_remaining": priority_link_credit,
        "discovered_urls": list(dict.fromkeys(urls[:30] + reference_urls + [link["url"] for link in link_discoveries])),
        "attempted_urls": attempted_urls[:max_page_attempts], "skipped_urls": skipped_urls[:30],
        "urls_attempted": urls_attempted, "urls_deduplicated": duplicates, "sources_checked": len(sources),
        "supplemental_text_views": len(supplements),
        "pdf_evidence_policy": PDF_EVIDENCE_POLICY if supplements else None,
        "optional_pdf_status": optional_pdf_status,
        "urls_outside_limit": outside_limit, "urls_rejected": rejected_urls, "errors": errors,
        "elapsed_seconds": round(time.monotonic() - started, 3), "score_basis": "exact_word_coverage",
        "default_fetch_limits": {"max_response_bytes": web_fetch.MAX_RESPONSE_BYTES,
            "max_text_characters": web_fetch.MAX_TEXT_CHARACTERS, "max_redirects": web_fetch.MAX_REDIRECTS,
            "max_robots_redirects": web_fetch.MAX_ROBOTS_REDIRECTS,
            "total_timeout_seconds": web_fetch.TOTAL_TIMEOUT, "pdf_max_pages": pdf_source.MAX_PAGES,
            "pdf_timeout_seconds": pdf_source.MAX_SECONDS},
        "limitations": ["Оценка относится к найденным доступным страницам, а не ко всему интернету.",
                        ("Выполняется не более шести поисковых запросов и загрузка не более двадцати четырёх страниц."
                         if use_zero_fallback else "Выполняется не более шести поисковых запросов и загрузка не более двенадцати страниц."),
                        "Закрытые, неиндексируемые и недоступные материалы могут остаться незамеченными.",
                        "Пересказ и перевод требуют отдельной смысловой сверки; они не включены в этот процент."]})
    if not complete:
        result["issues"].append({"kind": "partial_web_check", "message": "Проверка по интернету неполная. Итоговая уникальность не установлена; найденные совпадения сохранены."})
    if use_wiki_followup:
        from plagiarism.web_wiki_followup import append_post_wiki
        if wiki_searcher is None:
            from plagiarism.search_mediawiki import MediaWikiSearch
            wiki_searcher = MediaWikiSearch()
        if progress:
            progress("Дополнительно ищем в русской Википедии", 0, 2, phase="web_search")
        result, _ = append_post_wiki(text, result, queries, primary_urls=urls,
            attempted_urls=attempted_urls, final_aliases=final_urls, request_id=request_id,
            started_at=started, secondary_searcher=wiki_searcher, fetcher=fetcher, progress=progress)
    if progress:
        progress("Сохраняем проверку уникальности", len(ordered), len(ordered), phase="saving")
    return result
