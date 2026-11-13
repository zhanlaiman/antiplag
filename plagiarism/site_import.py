"""Small public-site preview, reusing the pinned-DNS/robots-aware fetcher."""
from urllib.parse import urljoin,urlsplit
from plagiarism.web_fetch import Fetcher,FetchError,_validate_url

MAX_PAGES=3
MAX_TOTAL_CHARACTERS=200000

def public_page_url(value):
    try:target=_validate_url(value)
    except FetchError as error:raise ValueError(str(error)) from None
    if urlsplit(target['url']).query:
        raise ValueError('Укажите публичный адрес страницы без параметров URL.')
    return target

def fetch_site(url,limit=MAX_PAGES,fetcher=None):
    target=public_page_url(url)
    if type(limit) is not int or not 1<=limit<=MAX_PAGES:raise ValueError('За один раз загружаем от одной до трёх страниц.')
    fetcher=fetcher or Fetcher(semantic_notes=False)
    queue=[target['url']];seen=set();pages=[];errors=[];attempted=[];total=0;origin=target['origin']
    while queue and len(attempted)<limit:
        current=queue.pop(0)
        if current in seen:continue
        seen.add(current);attempted.append(current)
        try:
            page=fetcher.fetch(current)
            final=public_page_url(page['url'])
            if not pages:origin=final['origin']
            elif final['origin']!=origin:raise FetchError('Переход на другой сайт пропущен.','site_origin_changed')
            if any(p['url']==final['url'] for p in pages):continue
            text=page['text']
            if total+len(text)>MAX_TOTAL_CHARACTERS:raise FetchError('Суммарный текст страниц больше 200000 знаков.','site_text_limit')
            total+=len(text)
            pages.append({k:page[k] for k in ['title','text','fetched_at','text_sha256','extraction'] if k in page}|{'url':final['url']})
            for link in page.get('links',[])[:100]:
                try:following=public_page_url(urljoin(final['url'],link['url']))
                except (ValueError,FetchError,KeyError):continue
                if following['origin']!=origin or following['url'] in seen or following['url'] in queue:continue
                path=urlsplit(following['url']).path.rsplit('/',1)[-1]
                if '.' in path and not path.lower().endswith(('.html','.htm','.pdf')):continue
                queue.append(following['url'])
                if len(queue)>=MAX_PAGES:break
        except (FetchError,ValueError) as error:errors.append({'url':current,'code':getattr(error,'code','invalid_url'),'message':str(error)})
    return {'pages':pages,'errors':errors,'attempted_urls':attempted,'max_pages':limit,
            'scope':'Только выбранная страница и до двух ссылок на том же сайте; это не полный обход сайта.'}

class ExcludingFetcher:
    """Never count the submitted site's own host as evidence of borrowing."""
    def __init__(self,source_url,delegate=None):
        self.host=public_page_url(source_url)['host'];self.delegate=delegate or Fetcher()
    def fetch(self,url):
        if _validate_url(url)['host']==self.host:
            raise FetchError('Собственный сайт исключён из сравнения.','self_source_excluded')
        result=self.delegate.fetch(url)
        if _validate_url(result['url'])['host']==self.host:
            raise FetchError('Перенаправление на собственный сайт исключено.','self_source_excluded')
        return result
