"""Discover public sources, fetch evidence, and measure lexical word coverage.

No language model is called. Search snippets never become matching evidence.
"""

from datetime import datetime, timezone
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
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener
from uuid import uuid4

from plagiarism.auth_provider import NoRedirect
from plagiarism.matching import MATCHER_VERSION, MAX_TEXT_CHARS, MIN_WORDS, _tokens, analyze
from plagiarism.search_credentials import key_for, selected_provider


ROOT = Path(__file__).resolve().parent.parent
MAX_QUERIES = 6
MAX_URLS = 12
QUERY_STRATEGY = "uniform-quoted-v1"
DISTINCTIVE_STRATEGY = "distinctive-paragraph-v1"
HYBRID_STRATEGY = "hybrid-paragraph-v1"
DEFAULT_QUERY_STRATEGY = HYBRID_STRATEGY
SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class WebSearchError(RuntimeError):
    def __init__(self, message, code="web_search_failed", details=None):
        super().__init__(message)
        self.code, self.details = code, details


def _distinctive_windows(text, tokens, width, count):
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
                return sorted(chosen)
    return sorted(chosen)


def _hybrid_queries(text, count):
    """Split the same request allowance between two text-only planners."""
    if count == 1:
        return plan_queries(text, limit=1, strategy=DISTINCTIVE_STRATEGY)
    uniform = plan_queries(text, limit=(count + 1) // 2, strategy=QUERY_STRATEGY)
    distinctive = plan_queries(text, limit=count // 2, strategy=DISTINCTIVE_STRATEGY)
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


def plan_queries(text, limit=MAX_QUERIES, strategy=DEFAULT_QUERY_STRATEGY):
    if not isinstance(text, str) or not 35 <= len(text) <= MAX_TEXT_CHARS:
        raise ValueError("Для проверки по интернету нужен текст от 35 до 200 000 символов.")
    if type(limit) is not int or not 1 <= limit <= MAX_QUERIES:
        raise ValueError("Неверный лимит поисковых запросов.")
    if strategy not in (QUERY_STRATEGY, DISTINCTIVE_STRATEGY, HYBRID_STRATEGY):
        raise ValueError("Неизвестный способ выбора поисковых фраз.")
    tokens = _tokens(text)
    if len(tokens) < MIN_WORDS:
        raise ValueError("Для поиска источников нужно не менее шести слов.")
    width = min(10, len(tokens))
    count = min(limit, max(1, (len(tokens) + 79) // 80))
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
    return {"provider": provider, "configured": configured,
            "max_queries": MAX_QUERIES, "max_urls": MAX_URLS,
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


def run(text, request_id=None, progress=None, searcher=None, fetcher=None, query_strategy=None):
    """Produce a report payload, preserving every failure and the bounded scope.

    This is a first lexical web search, not a guarantee over every internet page.
    If a planned query or attempted page fails, no final uniqueness is claimed.
    """
    started = time.monotonic()
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
            for result in results:
                discovered += 1
                raw_url = result.get("url")
                url = _canonical_url(raw_url)
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
    # Round-robin domains prevents one large site from taking the whole fetch cap.
    grouped = {}
    for url in urls:
        grouped.setdefault(urlsplit(url).hostname, []).append(url)
    ordered = []
    while any(grouped.values()) and len(ordered) < MAX_URLS:
        for group in grouped.values():
            if group and len(ordered) < MAX_URLS:
                ordered.append(group.pop(0))
    outside_limit = max(0, len(urls) - len(ordered))
    selected_urls = set(ordered)
    skipped_urls = [url for url in urls if url not in selected_urls]
    if outside_limit:
        errors.append({"stage": "limit", "code": "url_limit_reached", "count": outside_limit,
                       "message": "Часть найденных ссылок не проверена из-за лимита загрузок."})
    sources, final_urls = [], set()
    urls_attempted = 0
    attempted_urls = []
    for index, url in enumerate(ordered):
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
        except (RuntimeError, OSError, ValueError, KeyError) as error:
            errors.append({"stage": "fetch", "url": url, "code": getattr(error, "code", "fetch_error"),
                           "message": "Страница недоступна или не разрешает автоматическую загрузку."})
    if not sources:
        errors.append({"stage": "coverage", "code": "no_sources_checked",
                       "message": "Не проверено ни одного источника. Уникальность не установлена."})
    result = analyze(text, sources)
    words = _word_coverage(text, result["matches"])
    complete = not errors and not queries_unattempted
    total, unmatched = words["total_words"], words["total_words"] - words["matched_words"]
    uniqueness = round(100 * unmatched / total, 1) if complete else None
    result["stats"].update(words, uniqueness_percent=uniqueness)
    if not complete and words["matched_words"]:
        # An upper bound must round upward, including below one decimal place.
        result["stats"]["uniqueness_upper_bound_percent"] = ((1000 * unmatched + total - 1) // total) / 10
    from plagiarism import pdf_source, web_fetch

    result.update(text=text, sources=sources, scope="web_search", web={
        "provider": provider, "status": "complete" if complete else "partial",
        "query_strategy": query_strategy, "matcher_version": MATCHER_VERSION,
        "queries_planned": len(queries), "queries_attempted": queries_attempted, "queries_completed": queries_completed,
        "queries_unattempted": queries_unattempted, "query_stop_code": stopped_code,
        "query_spans": [{"start": item["start"], "end": item["end"]} for item in queries],
        "urls_found": len(urls) + len(rejected_urls), "urls_supported": len(urls), "urls_discovered": discovered,
        "discovered_urls": urls[:30], "attempted_urls": attempted_urls[:MAX_URLS], "skipped_urls": skipped_urls[:30],
        "urls_attempted": urls_attempted, "urls_deduplicated": duplicates, "sources_checked": len(sources),
        "urls_outside_limit": outside_limit, "urls_rejected": rejected_urls, "errors": errors,
        "elapsed_seconds": round(time.monotonic() - started, 3), "score_basis": "exact_word_coverage",
        "default_fetch_limits": {"max_response_bytes": web_fetch.MAX_RESPONSE_BYTES,
            "max_text_characters": web_fetch.MAX_TEXT_CHARACTERS, "max_redirects": web_fetch.MAX_REDIRECTS,
            "total_timeout_seconds": web_fetch.TOTAL_TIMEOUT, "pdf_max_pages": pdf_source.MAX_PAGES,
            "pdf_timeout_seconds": pdf_source.MAX_SECONDS},
        "limitations": ["Оценка относится к найденным доступным страницам, а не ко всему интернету.",
                        "Выполняется не более шести поисковых запросов и загрузка не более двенадцати страниц.",
                        "Закрытые, неиндексируемые и недоступные материалы могут остаться незамеченными.",
                        "Пересказ и перевод требуют отдельной смысловой сверки; они не включены в этот процент."]})
    if not complete:
        result["issues"].append({"kind": "partial_web_check", "message": "Проверка по интернету неполная. Итоговая уникальность не установлена; найденные совпадения сохранены."})
    if progress:
        progress("Сохраняем проверку уникальности", len(ordered), len(ordered), phase="saving")
    return result
