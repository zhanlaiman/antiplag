"""Tavily basic search: bounded URL discovery, never matching evidence.

Request fields and basic-search credit cost follow the official API reference:
https://docs.tavily.com/documentation/api-reference/endpoint/search
"""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener

from plagiarism.auth_provider import NoRedirect
from plagiarism.search_credentials import key_for
from plagiarism.web_search import WebSearchError


SEARCH_ENDPOINT = "https://api.tavily.com/search"
MAX_RESPONSE_BYTES = 2_000_000


def _url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 33 for char in value):
        raise ValueError("invalid result URL")
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
            or parts.port is not None and not 1 <= parts.port <= 65535):
        raise ValueError("invalid result URL")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


class TavilySearch:
    provider = "tavily"

    def __init__(self, budget, *, exact_match=False):
        if type(exact_match) is not bool:
            raise ValueError("Exact phrase policy requires a boolean")
        self.exact_match = exact_match
        self.query_policy = "tavily-basic-exact-phrases-v1" if exact_match else "tavily-basic-legacy-v1"
        self.key = key_for("tavily")
        if not self.key or len(self.key) > 1024 or any(not 33 <= ord(char) <= 126 for char in self.key):
            raise WebSearchError("Поиск Tavily ещё не подключён. Нужен серверный ключ API.", "web_not_configured")
        self.budget = budget
        self.opener = build_opener(NoRedirect())
        self.last_usage = None

    def search(self, query, request_id):
        if (not isinstance(query, str) or not query.strip() or len(query) > 400
                or any(ord(char) < 32 for char in query)
                or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)):
            raise WebSearchError("Некорректный поисковый запрос.", "invalid_search_query")
        if self.exact_match and (not query.startswith('"') or not query.endswith('"') or query.count('"') != 2):
            raise WebSearchError("Для точного поиска нужна одна фраза в кавычках.", "invalid_search_query")
        body = {"query": query, "search_depth": "basic", "auto_parameters": False,
                "topic": "general", "max_results": 5, "include_answer": False,
                "include_raw_content": False, "include_usage": True}
        if self.exact_match:
            body["exact_match"] = True
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except UnicodeError:
            raise WebSearchError("Некорректный поисковый запрос.", "invalid_search_query") from None
        request = Request(SEARCH_ENDPOINT, data=data, headers={"Authorization": "Bearer " + self.key,
                          "Accept": "application/json", "Content-Type": "application/json"})
        self.last_usage = None
        self.budget.reserve(request_id)
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("oversize response")
            result = json.loads(raw)
            if (not isinstance(result, dict) or "error" in result or "detail" in result
                    or result.get("success") is False or result.get("status") in {"error", "failed", "failure"}
                    or not isinstance(result.get("results"), list) or len(result["results"]) > 5):
                raise ValueError("invalid search response")
            urls = []
            seen = set()
            for item in result["results"]:
                if not isinstance(item, dict):
                    raise ValueError("invalid search result")
                url = _url(item.get("url"))
                if self.key in url:
                    raise ValueError("secret reflected in result URL")
                if url not in seen:
                    urls.append({"url": url})
                    seen.add(url)
            usage = result.get("usage")
            if usage is not None:
                if not isinstance(usage, dict) or type(usage.get("credits")) is not int or not 0 <= usage["credits"] <= 1:
                    raise ValueError("unexpected basic-search credit usage")
                self.last_usage = {"credits": usage["credits"]}
        except HTTPError as error:
            error.close()
            code = ("search_rate_limited" if error.code == 429 else "search_budget_exhausted" if error.code in {402, 432, 433}
                    else "search_credentials_invalid" if error.code in {401, 403} else "search_provider_error")
            raise WebSearchError("Поисковый сервис Tavily не выполнил запрос. Проверка неполная.", code) from None
        except (URLError, OSError, ValueError, UnicodeError, TypeError):
            raise WebSearchError("Не удалось получить корректный ответ Tavily. Проверка неполная.", "search_provider_error") from None
        self.budget.finish(request_id)
        return urls
