"""Append a bounded public-source phase after an immutable primary report.

The caller supplies the already completed report, transports and original
monotonic start. No provider, network, storage or private data is constructed
here. Imports of plagiarism modules are deferred to avoid a web_search cycle.
"""
from copy import deepcopy
from hashlib import sha256
from collections.abc import Mapping
import math
import re
import time

DISCOVERY_STRATEGY = 'primary-mediawiki-post-v2-clause'
B1_DISCOVERY_STRATEGY = 'primary-mediawiki-post-v1'
DEFAULT_SECONDARY_QUERY_POLICY = 'strict-em-dash-prefix-v2'
UNCHANGED_SECONDARY_QUERY_POLICY = 'unchanged-v1'
FROZEN_SHRINK_SOURCE_SHA256 = 'a9d5c134885ac9bb0affc9dc3b719d3a2378cca2b104268153b535ee3d749f88'
MAX_SECONDS = 300
API_HEADROOM = 15
FETCH_HEADROOM = 20
MAX_POST_QUERIES = 2
MAX_POST_PAGES = 2
MAX_TOTAL_PAGES = 26
NO_SOURCE_CODES = frozenset({'no_sources_checked','no_sources','no_sources_read','no_web_sources','no_web_sources_read'})
UNKNOWN_BODY_CODES = frozenset({'saved_page_cache_miss','offline_cache_miss','saved_page_invalid',
    'page_outcome_unknown','fetch_failed_or_unknown','source_http_cap','page_probe_cap','page_outcome_submitted'})


def _safe_code(error, fallback):
    value = getattr(error, 'code', None)
    return value if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', value) else fallback


def _urls(value, canonical):
    if isinstance(value, Mapping):
        value = [*value.keys(), *value.values()]
    if isinstance(value, (str, bytes)):
        raise ValueError('An ordered URL collection is required')
    return list(dict.fromkeys(canonical(url) for url in value))


def _usage(searcher):
    value = getattr(searcher, 'last_usage', None)
    if (not isinstance(value, Mapping) or type(value.get('public_queries')) is not int
            or not 0 <= value['public_queries'] <= 1 or value.get('cost_kzt') != 0
            or type(value.get('cost_kzt')) not in (int, float)
            or not math.isfinite(value['cost_kzt']) or type(value.get('cached')) is not bool):
        return None
    return {key: value[key] for key in ('public_queries','cost_kzt','cached')}


def shrink_queries(text,primary_plan,secondary_plan):
    # Exact frozen B2 function; only plagiarism imports move inside to avoid cycles.
    from plagiarism.matching import _tokens
    from plagiarism import web_search as web
    protected={q['query'] for q in web._citation_queries(text)}
    original={q['query'] for q in [*primary_plan,*secondary_plan]}
    accepted=set();result=[];audit=[]
    tokens=_tokens(text)
    for query in secondary_plan:
        changed=deepcopy(query);reason='unchanged'
        window=[t for t in tokens if query['start']<=t.start and t.end<=query['end']]
        gaps=[i for i in range(1,len(window)) if re.fullmatch(r'\s+—\s+',text[window[i-1].end:window[i].start])]
        if query['query'] in protected:reason='citation_protected'
        elif len(gaps)!=1:reason='not_exactly_one_standalone_em_dash'
        elif gaps[0] not in (1,2):reason='prefix_not_one_or_two_tokens'
        elif len(window)-gaps[0]<6:reason='suffix_shorter_than_six_tokens'
        else:
            suffix=window[gaps[0]:]
            proposed='"'+' '.join(t.value[:40] for t in suffix)+'"'
            if proposed in original or proposed in accepted:reason='duplicate_original_or_prior_clip'
            else:
                changed={**query,'query':proposed,'start':suffix[0].start};reason='strict_em_dash_prefix_clipped'
        accepted.add(changed['query']);result.append(changed)
        audit.append({'original':deepcopy(query),'selected':deepcopy(changed),'reason':reason,'primary_unchanged':True})
    return result,audit


def select_post_queries(text, primary_plan, *, secondary_query_policy=DEFAULT_SECONDARY_QUERY_POLICY):
    """Select B1 slots first, then apply the exact frozen B2 clause rule.

    Returned query/start/end drive the real callback. primary_query_index is
    bookkeeping metadata; the supplied primary plan is never modified.
    """
    from plagiarism import web_search as web
    protected={q['query'] for q in web._citation_queries(text)}
    chosen=[{**deepcopy(q),'primary_query_index':i} for i,q in enumerate(primary_plan)
            if q['query'] not in protected][:MAX_POST_QUERIES]
    if secondary_query_policy==UNCHANGED_SECONDARY_QUERY_POLICY:
        return chosen
    if secondary_query_policy!=DEFAULT_SECONDARY_QUERY_POLICY:
        raise ValueError('Unknown secondary query policy')
    return shrink_queries(text,primary_plan,chosen)[0]


def append_post_wiki(text, legacy_report, primary_plan, *, primary_urls,
                     attempted_urls, final_aliases, request_id, started_at,
                     secondary_searcher, fetcher, clock=None, progress=None,
                     secondary_query_policy=DEFAULT_SECONDARY_QUERY_POLICY):
    """Return (report, audit); the supplied primary report is never mutated.

    primary_urls is the full supported discovery order, before any report
    truncation, including primary named references/followups. final_aliases
    may be URL strings or a requested->final mapping. Transports expose
    search(query, stable_call_id)->[{url}] and fetch(url)->normal page dict;
    equivalent callables are accepted. Search snippets never become evidence.
    """
    from plagiarism import web_search as web
    from plagiarism.matching import _tokens
    from plagiarism.html_evidence_merge import analyze_with_supplements
    from plagiarism.html_evidence import POLICY as HTML_EVIDENCE_POLICY
    from plagiarism.pdf_evidence import POLICY as PDF_EVIDENCE_POLICY
    from plagiarism.web_evaluation import _report
    from plagiarism.web_fetch import FetchError

    if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
            or any(ord(c) < 32 for c in request_id)):
        raise ValueError('Invalid primary request identity')
    if type(started_at) not in (int, float) or not math.isfinite(started_at):
        raise ValueError('Invalid original monotonic start')
    clock = clock or time.monotonic
    first_now = clock()
    if type(first_now) not in (int, float) or not math.isfinite(first_now) or first_now < started_at:
        raise ValueError('Invalid monotonic clock/start relation')
    previous_now = first_now
    def elapsed():
        nonlocal previous_now
        now = clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < previous_now:
            raise ValueError('Monotonic clock moved backwards')
        previous_now = now
        return now - started_at
    def canonical(url):
        result = web._canonical_url(url)
        if result is None:
            raise ValueError('Unsupported source URL')
        return result
    def grounded_starts(report):
        proof = _report(report, {'id':'post-wiki-proof','text_sha256':sha256(text.encode()).hexdigest()})
        return {t.start for t in _tokens(text)
                if any(m['target_span'][0] <= t.start and t.end <= m['target_span'][1]
                       for m in proof['matches'])}

    if not isinstance(legacy_report, dict) or legacy_report.get('text') != text:
        raise ValueError('Primary report target changed')
    report = deepcopy(legacy_report)
    data = report['web']
    if not isinstance(primary_plan, (list, tuple)) or not 1 <= len(primary_plan) <= 6:
        raise ValueError('Expected one through six unchanged primary phrases')
    for item in primary_plan:
        if (not isinstance(item, dict) or not isinstance(item.get('query'), str)
                or not 1 <= len(item['query']) <= 400 or any(ord(c) < 32 for c in item['query'])
                or type(item.get('start')) is not int or type(item.get('end')) is not int
                or not 0 <= item['start'] < item['end'] <= len(text)):
            raise ValueError('Invalid primary phrase/span')
    primary_spans = [{key:q[key] for key in ('start','end')} for q in primary_plan]
    if data.get('query_spans') != primary_spans:
        raise ValueError('Primary phrase spans differ from the completed report')
    prefix_attempts = list(attempted_urls)
    if prefix_attempts != data.get('attempted_urls') or len(prefix_attempts) > 24:
        raise ValueError('Completed primary attempt prefix changed or exceeds24')
    prefix_sources = deepcopy(report['sources'])
    old_coverage = grounded_starts(report)
    tokens = _tokens(text)
    full_primary = _urls(primary_urls, canonical)
    prefix_canonical = _urls(prefix_attempts, canonical)
    aliases = set(_urls(final_aliases, canonical))
    original_finals = {canonical(s['url']) for s in prefix_sources}
    if not set(prefix_canonical) <= set(full_primary):
        raise ValueError('Full primary discovery list omits an attempted URL')
    if not {canonical(u) for u in data.get('discovered_urls', [])} <= set(full_primary):
        raise ValueError('Full primary discovery list omits reported URLs')
    excluded = set(prefix_canonical) | aliases | original_finals
    old_planned = data.get('queries_planned', len(primary_plan))
    old_attempted = data.get('queries_attempted', len(primary_plan))
    old_completed = data.get('queries_completed', old_attempted)
    if (any(type(n) is not int for n in (old_planned,old_attempted,old_completed))
            or old_planned != len(primary_plan) or not 0 <= old_completed <= old_attempted <= old_planned):
        raise ValueError('Invalid completed primary query counters')
    search = getattr(secondary_searcher, 'search', secondary_searcher)
    fetch = getattr(fetcher, 'fetch', fetcher)
    if not callable(search) or not callable(fetch):
        raise ValueError('Injected search/fetch callbacks are required')
    provider = getattr(secondary_searcher, 'provider', 'mediawiki')
    if provider != 'mediawiki':
        raise ValueError('Only the public MediaWiki secondary provider is allowed')
    chosen = select_post_queries(text,primary_plan,secondary_query_policy=secondary_query_policy)
    policy = DISCOVERY_STRATEGY if secondary_query_policy==DEFAULT_SECONDARY_QUERY_POLICY else B1_DISCOVERY_STRATEGY
    original_chosen = select_post_queries(text,primary_plan,secondary_query_policy=UNCHANGED_SECONDARY_QUERY_POLICY)
    audit = {'policy':policy,'secondary_query_policy':secondary_query_policy,
        'secondary_query_transform':[{'original':a,'selected':b,'primary_unchanged':True} for a,b in zip(original_chosen,chosen)],'gate_full_input':len(old_coverage)==len(tokens),
        'primary_exact_words':len(old_coverage),'post_query_calls':[],
        'extra_attempted_urls':[],'unknown_extra_urls':[],'failed_extra_urls':[],
        'unattempted_extra_urls':[],'deadline_API':False,'deadline_fetch':False,
        'secondary_halted':False,'prefix_preserved':True,'old_exact_coverage_preserved':True,
        'primary_elapsed_seconds':first_now-started_at,'known_new_public_queries':0,
        'public_query_usage_unknown_calls':0,'actual_new_public_queries':None,
        'max_extra_pages':MAX_POST_PAGES,'max_total_pages':MAX_TOTAL_PAGES}
    errors = [deepcopy(e) for e in data.get('errors', [])
              if not (e.get('stage')=='limit' and e.get('code')=='url_limit_reached')]
    groups = []
    completed = raw_results = 0
    for slot,q in enumerate(chosen):
        if progress:
            progress('Ищем дополнительные источники в Википедии',old_attempted+slot,old_planned+len(chosen),phase='web_search')
        if elapsed()+API_HEADROOM > MAX_SECONDS:
            audit['deadline_API'] = True
            break
        call_id = sha256((request_id+'\0mediawiki-post\0'+str(slot)).encode()).hexdigest()
        event = {**q,'call_id':call_id,'query_index':old_planned+slot}
        audit['post_query_calls'].append(event)
        usage_reset = True
        try:
            setattr(secondary_searcher, 'last_usage', None)
        except (AttributeError, TypeError):
            usage_reset = False
        try:
            results = search(q['query'], call_id)
            if not isinstance(results, list) or len(results) > 5:
                raise ValueError('Invalid public search result group')
            group = [canonical(item['url']) for item in results]
            groups.append(group)
            raw_results += len(group)
            completed += 1
            event['state'] = 'complete'
        except Exception as error:
            code = _safe_code(error, 'secondary_search_error')
            event.update(state='unavailable',code=code)
            groups.append([])
            errors.append({'stage':'search','query_index':old_planned+slot,'code':code,
                'message':'Дополнительный поиск по Википедии недоступен; замены на платный поиск нет.'})
            audit['secondary_halted'] = True
        usage = _usage(secondary_searcher) if usage_reset else None
        event['usage'] = usage
        if usage is None:
            audit['public_query_usage_unknown_calls'] += 1
        else:
            audit['known_new_public_queries'] += usage['public_queries']
        if audit['secondary_halted'] or getattr(secondary_searcher, 'halt_code', None):
            audit['secondary_halted'] = True
            break

    all_wiki = []
    seen = set()
    for rank in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if rank < len(group) and group[rank] not in seen:
                seen.add(group[rank]);all_wiki.append(group[rank])
    selected = [url for url in all_wiki if url not in excluded][:MAX_POST_PAGES]
    sources = deepcopy(prefix_sources)
    finals = set(original_finals)
    added = duplicates = 0
    for url in selected:
        if progress:
            progress('Читаем дополнительные источники',len(prefix_attempts)+len(audit['extra_attempted_urls']),MAX_TOTAL_PAGES,phase='web_fetch')
        if elapsed()+FETCH_HEADROOM > MAX_SECONDS:
            audit['deadline_fetch'] = True
            break
        audit['extra_attempted_urls'].append(url)
        try:
            page = fetch(url)
            if not isinstance(page, dict):
                raise FetchError('Source body unavailable', 'saved_page_cache_miss')
            final = canonical(page['url'])
            if final in excluded or final in finals:
                duplicates += 1
                continue
            body = page['text']
            if not isinstance(body, str) or len(body.strip()) < 35:
                raise FetchError('Source body is empty', 'empty_text')
            if len(body) > web.MAX_TEXT_CHARS:
                raise FetchError('Source body exceeds its text limit', 'extracted_text_too_large')
            body_sha = sha256(body.encode()).hexdigest()
            if page.get('text_sha256', body_sha) != body_sha:
                raise ValueError('Source body SHA changed')
            source = {'id':'web-'+sha256(final.encode()).hexdigest()[:24],
                'url':final,'title':page.get('title') or final,'text':body,
                'provenance':{'origin':'web_fetch','fetched_at':page.get('fetched_at'),
                    'text_sha256':body_sha,'license_status':'not_verified','training_consent':False}}
            if any(s['id']==source['id'] for s in sources):
                raise ValueError('New source identity collides')
            new_views = web._supplemental_source_views(source, page)
            sources.extend([source,*new_views])
            finals.add(final);added += 1
        except Exception as error:
            code = _safe_code(error, 'saved_page_invalid')
            unknown = code in UNKNOWN_BODY_CODES
            audit['unknown_extra_urls' if unknown else 'failed_extra_urls'].append(url)
            errors.append({'stage':'fetch','url':url,'code':code,
                'message':'Дополнительную страницу не удалось проверить; уникальность не подтверждена.'})
    audit['unattempted_extra_urls'] = [u for u in selected if u not in audit['extra_attempted_urls']]
    primary = [s for s in sources if not s.get('representation_of')]
    views = [s for s in sources if s.get('representation_of')]
    report['sources'] = sources
    result = analyze_with_supplements(text, primary, views)
    report['matches'] = result['matches']
    new_coverage = grounded_starts(report)
    if not old_coverage <= new_coverage:
        raise AssertionError('Post phase lost grounded primary word coverage')
    if added:
        errors = [e for e in errors if e.get('code') not in NO_SOURCE_CODES]
    new_attempts = prefix_attempts + audit['extra_attempted_urls']
    discovered = list(dict.fromkeys([*full_primary,*all_wiki]))
    now_read = {canonical(s['url']) for s in primary}
    attempted_set = {canonical(u) for u in new_attempts}
    remaining = [u for u in discovered if u not in attempted_set | now_read | aliases]
    if remaining:
        errors.append({'stage':'limit','code':'url_limit_reached','count':len(remaining),
            'message':'Не все найденные страницы прочитаны; проверка неполная.'})
    calls = len(audit['post_query_calls'])
    unattempted = old_planned+len(chosen)-old_attempted-calls
    partial = bool(errors or remaining or unattempted or completed < calls
                   or audit['deadline_API'] or audit['deadline_fetch'] or not primary)
    stats = {**report['stats'],**result['stats'],**web._word_coverage(text, report['matches'])}
    stats.update(source_count=len({m['source_url'] for m in report['matches']}),
                 checked_source_count=len(now_read))
    stats['uniqueness_percent'] = (None if partial else
        round(100 * (stats['total_words'] - stats['matched_words']) / stats['total_words'], 1))
    if partial and stats['matched_words'] and stats['total_words']:
        stats['uniqueness_upper_bound_percent'] = ((1000*(stats['total_words']-stats['matched_words'])+stats['total_words']-1)//stats['total_words'])/10
    else:
        stats.pop('uniqueness_upper_bound_percent', None)
    report['stats'] = stats
    issues = deepcopy(legacy_report.get('issues', []))
    if added:
        issues = [i for i in issues if i.get('kind') not in NO_SOURCE_CODES]
    issues.extend(i for i in result.get('issues', []) if i not in issues)
    if not partial:
        issues = [i for i in issues if i.get('kind') != 'partial_web_check']
    if partial and not any(i.get('kind')=='partial_web_check' for i in issues):
        issues.append({'kind':'partial_web_check','message':'Проверка ограничена доступными источниками; найденные совпадения сохранены.'})
    report['issues'] = issues
    novel_urls = len(set(all_wiki) - set(full_primary))
    limitations = list(data.get('limitations', []))
    bounded_scope = ('Выполняется не более шести запросов к основному поиску, двух дополнительных '
        'запросов к русской Википедии и двадцати шести попыток чтения страниц.')
    limit_index = next((i for i, note in enumerate(limitations)
        if isinstance(note, str) and note.startswith('Выполняется не более шести поисковых запросов')), None)
    if limit_index is None:
        limitations.append(bounded_scope)
    else:
        limitations[limit_index] = bounded_scope
    data.update(discovery_strategy=policy,post_wiki_enabled=True,
        primary_provider=data.get('primary_provider',data.get('provider')),secondary_provider='mediawiki',
        post_wiki_query_policy=secondary_query_policy,
        primary_queries_planned=old_planned,primary_queries_attempted=old_attempted,
        primary_queries_completed=old_completed,primary_query_spans=deepcopy(primary_spans),
        primary_max_page_attempts=data.get('max_page_attempts',24),
        post_wiki_policy=policy,post_wiki_queries_planned=len(chosen),
        post_wiki_queries_attempted=calls,post_wiki_queries_completed=completed,
        post_wiki_query_plan=deepcopy(chosen),total_provider_requests=old_attempted+calls,
        max_provider_requests=8,queries_planned=old_planned+len(chosen),
        queries_attempted=old_attempted+calls,queries_completed=old_completed+completed,
        queries_unattempted=unattempted,
        query_spans=deepcopy(primary_spans)+[{k:q[k] for k in ('start','end')} for q in audit['post_query_calls']],
        post_wiki_extra_attempts=len(audit['extra_attempted_urls']),
        post_wiki_attempted_urls=list(audit['extra_attempted_urls']),post_wiki_max_extra_pages=2,
        max_page_attempts=26,attempted_urls=new_attempts,urls_attempted=len(new_attempts),
        sources_checked=len(now_read),supplemental_text_views=len(views),
        pdf_text_views=sum(s.get('provenance',{}).get('evidence_policy')==PDF_EVIDENCE_POLICY for s in views),
        html_text_views=sum(s.get('provenance',{}).get('evidence_policy')==HTML_EVIDENCE_POLICY for s in views),
        pdf_evidence_policy=PDF_EVIDENCE_POLICY if any(s.get('provenance',{}).get('evidence_policy')==PDF_EVIDENCE_POLICY for s in views) else None,
        html_evidence_policy=HTML_EVIDENCE_POLICY if any(s.get('provenance',{}).get('evidence_policy')==HTML_EVIDENCE_POLICY for s in views) else None,
        discovered_urls=discovered,urls_discovered=data.get('urls_discovered',len(full_primary))+raw_results,
        urls_found=data.get('urls_found',len(full_primary))+novel_urls,
        urls_supported=data.get('urls_supported',len(full_primary))+novel_urls,
        urls_deduplicated=data.get('urls_deduplicated',0)+raw_results-novel_urls+duplicates,
        search_urls_discovered=data.get('search_urls_discovered',len(full_primary))+raw_results,
        skipped_urls=remaining,urls_outside_limit=len(remaining),errors=errors,
        post_wiki_deadline_API=audit['deadline_API'],post_wiki_deadline_fetch=audit['deadline_fetch'],
        post_wiki_unknown_urls=list(audit['unknown_extra_urls']),
        post_wiki_unattempted_urls=list(audit['unattempted_extra_urls']),
        post_wiki_public_queries_confirmed=audit['known_new_public_queries'],
        post_wiki_public_query_usage_unknown_calls=audit['public_query_usage_unknown_calls'],
        elapsed_seconds=elapsed(),status='partial' if partial else 'complete',limitations=limitations)
    audit['actual_new_public_queries'] = audit['known_new_public_queries'] if not audit['public_query_usage_unknown_calls'] else None
    data['post_wiki_new_public_queries'] = audit['actual_new_public_queries']
    audit.update(prefix_preserved=sources[:len(prefix_sources)]==prefix_sources and data['attempted_urls'][:len(prefix_attempts)]==prefix_attempts,
        old_exact_coverage_preserved=old_coverage<=new_coverage,final_exact_words=len(new_coverage),
        extra_source_rows_added=len(sources)-len(prefix_sources),extra_primary_sources_added=added,
        total_provider_requests=old_attempted+calls,post_elapsed_seconds=elapsed()-(first_now-started_at))
    if not audit['prefix_preserved']:
        raise AssertionError('Post phase changed primary source or attempted-URL prefix')
    if calls and 'offline_replay' in report:
        data['primary_offline_replay'] = report.pop('offline_replay')
        data.pop('offline_replay', None)
        data['hybrid_reply_provenance'] = {'primary_replies_replayed': True,
            'secondary_slots': calls, 'secondary_new_public_queries': audit['actual_new_public_queries']}
    return report, audit
