"""Choose one verified free provider before a job starts, never replay a search."""
import json
import math
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from plagiarism.auth_provider import NoRedirect
from plagiarism.search_credentials import key_for
from plagiarism.search_pool import PoolBudget, checked_grant, fingerprint
from plagiarism.search_provider_policy import FreeQuotaExhausted, PROVIDERS
from plagiarism.web_search import WebSearchError

MAX_QUOTA_BYTES = 65536


def confirm_tavily(key, needed, opener=None):
    opener = opener or build_opener(NoRedirect())
    request = Request('https://api.tavily.com/usage',
                      headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    try:
        with opener.open(request, timeout=12) as response:
            raw = response.read(MAX_QUOTA_BYTES + 1)
        if len(raw) > MAX_QUOTA_BYTES:
            raise ValueError('Quota response cap')
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or not isinstance(envelope.get('account'), dict):
            raise ValueError('Invalid quota envelope')
        account = envelope['account']
        used, paid = account.get('plan_usage'), account.get('paygo_usage')
        if (account.get('current_plan') != 'Researcher' or type(account.get('plan_limit')) is not int
                or account['plan_limit'] != 1000 or type(used) not in (int, float)
                or not math.isfinite(used) or not 0 <= used <= 1000
                or type(paid) not in (int, float) or paid != 0):
            raise ValueError('Unverified free plan')
        if used + needed > 900:
            raise FreeQuotaExhausted()
        return {'remaining': int(900 - used), 'guard': 900}
    except FreeQuotaExhausted:
        raise
    except HTTPError as error:
        error.close()
    except (URLError, OSError, ValueError, TypeError, AttributeError, UnicodeError):
        pass
    raise WebSearchError('Не удалось подтвердить бесплатную квоту Tavily.', 'free_quota_unconfirmed') from None


def configured_grants():
    raw = os.environ.get('ISTOK_SEARCH_POOL_GRANTS', '{}')
    try:
        if len(raw) > 16384:
            raise ValueError('Grant policy cap')
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) - {'serper', 'exa'}:
            raise ValueError('Invalid grants')
        return value
    except (ValueError, TypeError):
        raise WebSearchError('Политика бесплатных лимитов недоступна.', 'free_quota_unconfirmed') from None


class SelectedSearch:
    """Once selected, an unknown outcome stops this job without another provider."""
    def __init__(self, searcher, skipped):
        self.searcher, self.skipped = searcher, list(skipped)
        self.provider = searcher.provider
        self.last_usage = None

    def search(self, query, request_id):
        try:
            result = self.searcher.search(query, request_id)
            self.last_usage = self.searcher.last_usage
            return result
        except (WebSearchError, OSError, ValueError):
            raise WebSearchError('Поиск остановлен: результат запроса не подтверждён. Автоматический повтор отключён.',
                                 'search_pool_stopped') from None


def select_search(path, needed, *, limit=180, persist=lambda: None, grants=None,
                  factories=None, keys=None, tavily_probe=confirm_tavily):
    # The site ceiling is checked before ANY provider HTTP, and again atomically
    # with every attempt. Changing a provider/key never bypasses the shared calls.
    shared = PoolBudget(path, provider='selection', resource='selection', credential_fingerprint='',
                        limit=limit, persist=persist)
    shared.preflight(needed)
    if factories is None:
        from plagiarism.search_serpapi import YandexSerpAPISearch
        from plagiarism.search_serper import SerperSearch
        from plagiarism.search_tavily import TavilySearch
        from plagiarism.search_exa import ExaSearch
        factories = {'serpapi-yandex': YandexSerpAPISearch, 'serper': SerperSearch,
                     'tavily': TavilySearch, 'exa': ExaSearch}
    grants = configured_grants() if grants is None else grants
    skipped = []
    for provider in PROVIDERS:
        key = key_for(provider) if keys is None else keys.get(provider, '')
        if not isinstance(key, str) or not key or len(key) > 1024 or any(not 33 <= ord(c) <= 126 for c in key):
            raise WebSearchError('Поисковый сервис не подключён.', 'web_not_configured')
        grant = checked_grant(grants.get(provider), provider, key) if provider in ('serper', 'exa') else None
        resource = ('grant:' + provider + ':' + grant['account_ref'] + ':' + grant['grant_id']
                    if grant else 'live:' + ('serpapi' if provider == 'serpapi-yandex' else provider))
        budget = PoolBudget(path, provider=provider, resource=resource, credential_fingerprint=fingerprint(key),
                            grant=grant, limit=limit, persist=persist)
        try:
            budget.preflight(needed)
            search = factories[provider](budget)
            # Existing adapters pin their key on construction. Do not silently
            # combine a grant with a concurrently changed credential.
            if search.key != key:
                raise WebSearchError('Ключ поиска изменился. Запустите новую проверку.', 'free_quota_unconfirmed')
            if provider == 'serpapi-yandex':
                search.confirm_free(needed)
            elif provider == 'tavily':
                tavily_probe(key, needed)
            return SelectedSearch(search, skipped)
        except FreeQuotaExhausted:
            skipped.append({'provider': provider, 'reason': 'confirmed_free_quota_exhausted'})
    raise WebSearchError('Бесплатные лимиты всех поисковых сервисов исчерпаны.', 'search_budget_exhausted')
