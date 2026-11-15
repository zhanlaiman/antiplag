"""Bounded Serper URL discovery; free-grant authorization belongs to the pool.

The adapter never retries, approves a grant, or treats result snippets as evidence.
Serper's published credit policy: https://serper.dev/
"""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, build_opener

from plagiarism.auth_provider import NoRedirect
from plagiarism.search_credentials import key_for
from plagiarism.search_tavily import _url
from plagiarism.web_search import WebSearchError


SEARCH_ENDPOINT = "https://google.serper.dev/search"
MAX_RESPONSE_BYTES = 1_000_000


class SerperSearch:
    provider = "serper"

    def __init__(self, budget, key=None):
        if not callable(getattr(budget, "reserve", None)) or not callable(getattr(budget, "finish", None)):
            raise TypeError("A durable search budget is required")
        self.key = key_for(self.provider) if key is None else key
        if (not isinstance(self.key, str) or not self.key or len(self.key) > 1024
                or any(not 33 <= ord(char) <= 126 for char in self.key)):
            raise WebSearchError("Поиск Serper ещё не подключён.", "web_not_configured")
        self.budget = budget
        self.opener = build_opener(NoRedirect())
        self.last_usage = None

    def search(self, query, request_id):
        self.last_usage = None
        if (not isinstance(query, str) or not query.strip() or len(query) > 400
                or any(ord(char) < 32 or ord(char) == 127 for char in query)
                or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)):
            raise WebSearchError("Некорректный поисковый запрос.", "invalid_search_query")
        russian = bool(re.search(r"[а-яё]", query, re.I))
        body = {"q": query, "num": 5, "hl": "ru" if russian else "en", "gl": "ru" if russian else "us"}
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except UnicodeError:
            raise WebSearchError("Некорректный поисковый запрос.", "invalid_search_query") from None
        request = Request(SEARCH_ENDPOINT, data=data, headers={"X-API-KEY": self.key,
                          "Accept": "application/json", "Content-Type": "application/json"})
        self.budget.reserve(request_id)
        try:
            with self.opener.open(request, timeout=25) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Response cap")
            result = json.loads(raw)
            if (not isinstance(result, dict) or any(key in result for key in ("error", "errors", "detail"))
                    or result.get("success") is False or result.get("status") in {"error", "failed", "failure"}
                    or "statusCode" in result and (type(result["statusCode"]) is not int or result["statusCode"] != 200)
                    or not isinstance(result.get("organic"), list) or len(result["organic"]) > 100
                    or any(not isinstance(item, dict) for item in result["organic"])):
                raise ValueError("Invalid search response")
            credits = result.get("credits", 1)
            if type(credits) is not int or not 0 <= credits <= 1:
                raise ValueError("Unexpected search credit usage")
            urls = []
            seen = set()
            unresolved = False
            for item in result["organic"][:5]:
                url = _url(item.get("link"))
                url.encode("utf-8")
                decoded = url
                while self.key not in decoded:
                    updated = unquote(decoded)
                    if updated == decoded:
                        break
                    decoded = updated
                if self.key in decoded or any(ord(char) == 127 for char in url):
                    raise ValueError("Invalid result URL")
                parsed = urlsplit(url)
                if (parsed.hostname.rstrip(".") in {"google.com", "www.google.com"}
                        and unquote(parsed.path).rstrip("/") in {"/goto", "/url"}):
                    unresolved = True
                if url not in seen:
                    urls.append({"url": url})
                    seen.add(url)
        except HTTPError as error:
            status = error.code
            error.close()
            code = ("search_credentials_invalid" if status in {401, 403} else "search_rate_limited" if status == 429
                    else "search_budget_exhausted" if status == 402 else "search_provider_error")
            raise WebSearchError("Поисковый сервис Serper не выполнил запрос. Проверка неполная.", code) from None
        except (URLError, OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise WebSearchError("Не удалось получить корректный ответ Serper. Проверка неполная.", "search_provider_error") from None
        self.budget.finish(request_id)
        self.last_usage = {"credits": credits}
        if unresolved:
            raise WebSearchError("Google вернул служебные ссылки вместо адресов источников. Проверка неполная.",
                                 "search_redirect_unresolved")
        return urls
