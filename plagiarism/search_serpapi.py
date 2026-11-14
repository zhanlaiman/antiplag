"""Bounded Google discovery through SerpApi Free; URLs are never evidence."""
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from plagiarism.auth_provider import NoRedirect
from plagiarism.search_credentials import key_for
from plagiarism.search_tavily import _url
from plagiarism.web_search import WebSearchError

MAX_RESPONSE_BYTES = 1_000_000
FREE_MONTHLY_SEARCHES = 250


def free_quota(account, needed):
    """Return only non-sensitive, verified free-plan counters."""
    counters = ('searches_per_month','plan_searches_left','extra_credits',
                'total_searches_left','this_month_usage')
    if (not isinstance(account,dict) or type(needed) is not int or not 1 <= needed <= 6
            or account.get('account_status') != 'Active'
            or account.get('plan_name') != 'Free Plan'
            or type(account.get('plan_monthly_price')) not in (int,float)
            or account['plan_monthly_price'] != 0
            or any(type(account.get(k)) is not int for k in counters)
            or account['searches_per_month'] != FREE_MONTHLY_SEARCHES
            or account['extra_credits'] != 0
            or not 0 <= account['this_month_usage'] <= FREE_MONTHLY_SEARCHES
            or not needed <= account['total_searches_left'] == account['plan_searches_left'] <= FREE_MONTHLY_SEARCHES):
        raise WebSearchError('Не удалось подтвердить бесплатный тариф и остаток Google-поиска.', 'free_quota_unconfirmed')
    return {k:account[k] for k in (*counters,'plan_name','plan_monthly_price')}


class SerpAPISearch:
    provider = 'serpapi'
    label = 'Google'

    def __init__(self,budget):
        self.key = key_for('serpapi')
        if not self.key or len(self.key)>1024 or any(not 33<=ord(c)<=126 for c in self.key):
            raise WebSearchError(self.label+'-поиск ещё не подключён.', 'web_not_configured')
        self.budget=budget;self.opener=build_opener(NoRedirect());self.last_usage=None
        self.free_slots=0;self.free_at=None

    def _request(self,path,parameters,timeout):
        # SerpApi's documented authentication is a query parameter. No request
        # URL, raw exception or provider response is returned to a visitor/log.
        url='https://serpapi.com/'+path+'?'+urlencode({**parameters,'api_key':self.key})
        request=Request(url,headers={'Accept':'application/json','User-Agent':'Istok/0.1'})
        with self.opener.open(request,timeout=timeout) as response:
            body=response.read(MAX_RESPONSE_BYTES+1)
        if len(body)>MAX_RESPONSE_BYTES:
            raise ValueError('Response cap')
        value=json.loads(body)
        if not isinstance(value,dict):
            raise ValueError('Invalid object')
        return value

    def confirm_free(self,needed):
        self.free_slots=0;self.free_at=None
        try:
            quota=free_quota(self._request('account.json',{},12),needed)
            self.free_slots=needed;self.free_at=time.monotonic()
            return quota
        except HTTPError as error:
            error.close()
        except (WebSearchError,URLError,OSError,ValueError,TypeError,UnicodeError):
            pass
        raise WebSearchError('Не удалось подтвердить бесплатный поиск '+self.label+'. Запросы не отправлены.', 'free_quota_unconfirmed') from None

    def search(self,query,request_id):
        if (not isinstance(query,str) or not query.strip() or len(query)>400
                or any(ord(c)<32 for c in query)
                or not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',request_id)):
            raise WebSearchError('Некорректный поисковый запрос.', 'invalid_search_query')
        try:
            query.encode('utf-8')
        except UnicodeError:
            raise WebSearchError('Некорректный поисковый запрос.', 'invalid_search_query') from None
        parameters={'engine':'google','q':query,'num':5,'google_domain':'google.com',
                    'hl':'ru' if re.search(r'[а-яё]',query,re.I) else 'en','output':'json'}
        self.last_usage=None
        if self.free_slots<1 or self.free_at is None or not 0<=time.monotonic()-self.free_at<=300:
            raise WebSearchError('Бесплатная квота поиска '+self.label+' не подтверждена.','free_quota_unconfirmed')
        self.budget.reserve(request_id)
        self.free_slots-=1
        try:
            result=self._request('search.json',parameters,25)
            if result.get('search_metadata',{}).get('status')!='Success' or result.get('error'):
                raise ValueError('Provider did not finish')
            organic=result.get('organic_results',[])
            if not isinstance(organic,list) or len(organic)>100 or any(not isinstance(item,dict)for item in organic):
                raise ValueError('Invalid organic results')
            urls=[];seen=set()
            for item in organic[:5]:
                url=_url(item.get('link'))
                if self.key in url:
                    raise ValueError('Reflected credential')
                parsed=urlsplit(url)
                if parsed.hostname in ('google.com','www.google.com') and parsed.path.rstrip('/') in ('/goto','/url'):
                    # A known provider incident can return encrypted Google
                    # interstitials as successful organic links. They are not
                    # source URLs. Do not infer paths from displayed text or
                    # follow a redirect whose robots rules forbid reading it.
                    self.budget.finish(request_id)
                    self.last_usage={'credits':1}
                    raise WebSearchError('Google вернул служебные ссылки вместо адресов источников. Поиск неполный; уникальность не установлена.', 'search_redirect_unresolved')
                if url not in seen:
                    urls.append({'url':url});seen.add(url)
        except HTTPError as error:
            status=error.code;error.close()
            code='search_credentials_invalid' if status in (401,403) else 'search_rate_limited' if status==429 else 'search_provider_error'
            raise WebSearchError('Поисковый сервис не выполнил запрос. Проверка неполная.',code) from None
        except (URLError,OSError,ValueError,TypeError,AttributeError,UnicodeError):
            raise WebSearchError('Поисковый сервис не выполнил запрос. Проверка неполная.','search_provider_error') from None
        self.budget.finish(request_id)
        self.last_usage={'credits':1}
        return urls


class YandexSerpAPISearch(SerpAPISearch):
    provider = 'serpapi-yandex'
    label = 'Яндекс'

    def _request(self,path,parameters,timeout):
        if path=='search.json':
            parameters={'engine':'yandex','text':parameters['q'],'yandex_domain':'yandex.ru',
                        'lang':'ru','groups_on_page':5,'fix_typo':'false','output':'json'}
        result=super()._request(path,parameters,timeout)
        # Captured successful empty responses use this exact documented shape.
        # Unknown and contradictory errors retain the ordinary failure path.
        if (path=='search.json' and result.get('search_metadata',{}).get('status')=='Success'
                and result.get('error')=="Yandex hasn't returned any results for this query."
                and result.get('organic_results',[])==[]):
            return {**result,'error':None,'organic_results':[]}
        return result
