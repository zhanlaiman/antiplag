"""Versioned destinations for explicit automatic-search consent."""
import os

from plagiarism import access
from plagiarism.web_search import WebSearchError

CONSENT_VERSION = 'search-pool-v1'
PROVIDERS = ('serpapi-yandex', 'serper', 'tavily', 'exa')


class FreeQuotaExhausted(WebSearchError):
    """Only verified free-plan counters may cause provider selection to advance."""
    def __init__(self):
        super().__init__('Подтверждённой бесплатной квоты недостаточно.', 'free_quota_unconfirmed')


def contract():
    return {'enabled': os.environ.get('ISTOK_AUTOMATIC_SEARCH') == '1',
            'consent_version': CONSENT_VERSION, 'providers': list(PROVIDERS)}


def request_fields(body):
    """Return the exact additional fingerprint fields, preserving legacy bodies."""
    provider = body.get('search_provider')
    version = body.get('provider_consent_version')
    if provider == 'auto':
        if not contract()['enabled']:
            raise access.AccessError('Автоматический поиск пока недоступен.', 'automatic_search_disabled', 503)
        if version != CONSENT_VERSION or body.get('provider_consent') is not None:
            raise access.AccessError('Подтвердите актуальный список поисковых сервисов.', 'provider_consent_required', 400)
        return {'search_provider': 'auto', 'provider_consent_version': CONSENT_VERSION}
    if version is not None:
        raise access.AccessError('Неверная версия согласия на поиск.', 'invalid_search_provider', 400)
    return None
