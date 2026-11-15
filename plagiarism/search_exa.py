"""Exa Lite URL discovery with a fixed request; pool grants authorize spending.

Protocol: https://exa.ai/docs/reference/search
Pricing: https://exa.ai/docs/admin/pricing
No contents, synthesis, or automatic search mode is requested.
"""

from decimal import Decimal
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import unquote
from urllib.request import Request, build_opener

from plagiarism.auth_provider import NoRedirect
from plagiarism.search_credentials import key_for
from plagiarism.search_tavily import _url
from plagiarism.web_search import WebSearchError


SEARCH_ENDPOINT = "https://api.exa.ai/search"
MAX_RESPONSE_BYTES = 1_000_000
MAX_COST_DOLLARS = Decimal("0.004")


class ExaSearch:
    provider = "exa"

    def __init__(self, budget, key=None):
        if not callable(getattr(budget, "reserve", None)) or not callable(getattr(budget, "finish", None)):
            raise TypeError("A durable search budget is required")
        self.key = key_for(self.provider) if key is None else key
        if (not isinstance(self.key, str) or not self.key or len(self.key) > 1024
                or any(not 33 <= ord(char) <= 126 for char in self.key)):
            raise WebSearchError("Поиск Exa ещё не подключён.", "web_not_configured")
        self.budget = budget
        self.opener = build_opener(NoRedirect())
        self.last_usage = None

    def search(self, query, request_id):
        self.last_usage = None
        if (not isinstance(query, str) or not query.strip() or len(query) > 400
                or any(ord(char) < 32 or ord(char) == 127 for char in query)
                or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)):
            raise WebSearchError("Некорректный поисковый запрос.", "invalid_search_query")
        body = {"query": query, "type": "lite", "numResults": 5}
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except UnicodeError:
            raise WebSearchError("Некорректный поисковый запрос.", "invalid_search_query") from None
        request = Request(SEARCH_ENDPOINT, data=data, headers={"x-api-key": self.key,
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
                    or not isinstance(result.get("results"), list) or len(result["results"]) > 5):
                raise ValueError("Invalid search response")
            usage = {"credits": 1}
            if "costDollars" in result:
                cost = result["costDollars"]
                if not isinstance(cost, dict) or type(cost.get("total")) not in (int, float):
                    raise ValueError("Invalid search cost")
                total = Decimal(str(cost["total"]))
                if not total.is_finite() or not 0 <= total <= MAX_COST_DOLLARS:
                    raise ValueError("Unexpected search cost")
                usage["cost_dollars"] = float(total)
            urls = []
            seen = set()
            for item in result["results"]:
                if not isinstance(item, dict):
                    raise ValueError("Invalid search result")
                url = _url(item.get("url"))
                url.encode("utf-8")
                decoded = url
                while self.key not in decoded:
                    updated = unquote(decoded)
                    if updated == decoded:
                        break
                    decoded = updated
                if self.key in decoded or any(ord(char) == 127 for char in url):
                    raise ValueError("Invalid result URL")
                if url not in seen:
                    urls.append({"url": url})
                    seen.add(url)
        except HTTPError as error:
            status = error.code
            error.close()
            code = ("search_credentials_invalid" if status in {401, 403} else "search_rate_limited" if status == 429
                    else "search_budget_exhausted" if status == 402 else "search_provider_error")
            raise WebSearchError("Поисковый сервис Exa не выполнил запрос. Проверка неполная.", code) from None
        except (URLError, OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise WebSearchError("Не удалось получить корректный ответ Exa. Проверка неполная.", "search_provider_error") from None
        self.budget.finish(request_id)
        self.last_usage = usage
        return urls
