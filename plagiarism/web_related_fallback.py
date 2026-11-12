"""Append at most two previously discovered on-site alternatives; no API."""
from copy import deepcopy
from hashlib import sha256
import time

POLICY = 'post-check-two-tied-source-links-v2'
MAX_EXTRA_PAGES = 2
MAX_SECONDS = 300


def append_related(text, previous, parents, *, fetcher, started_at, clock=None, progress=None):
    from plagiarism import web_search as web
    from plagiarism.matching import _coverage
    from plagiarism.html_evidence_merge import analyze_with_supplements
    from plagiarism.web_fetch import FetchError, TOTAL_TIMEOUT
    clock = clock or time.monotonic
    if previous.get('text') != text:
        raise ValueError('Related phase target differs from completed report')
    data = previous['web']
    words = web._word_coverage(text, previous['matches'])
    if not parents or words['matched_words'] == words['total_words']:
        return previous
    excluded = set(data.get('discovered_urls', ())) | set(data.get('skipped_urls', ())) | set(data['attempted_urls'])
    excluded.update(s['url'] for s in previous['sources'])
    words = {t.value for t in web._tokens(text) if len(t.value) >= 5 and not t.value.isdecimal()}
    def anchor_score(parent, url):
        scores = []
        for item in parent.get('links', []):
            if web._canonical_url(item.get('url')) == url:
                label = {t.value for t in web._tokens(item.get('text', '')) if len(t.value) >= 5 and not t.value.isdecimal()}
                scores.append(len(words & label) / max(1, len(label)))
        return max(scores, default=0)
    candidates = []
    for parent in parents:
        followed = [item['url'] for item in data['link_discoveries'] if item['parent_url'] == parent['url']]
        required = max((anchor_score(parent, url) for url in followed), default=0)
        if not required:
            continue
        while len(candidates) < MAX_EXTRA_PAGES:
            url = web._related_link(parent, text, excluded)
            if not url or anchor_score(parent, url) != required:
                break
            excluded.add(url)
            candidates.append({'url':url, 'parent_url':parent['url'], 'basis':'input_anchor_overlap_alternative'})
        if len(candidates) == MAX_EXTRA_PAGES:
            break
    if not candidates:
        return previous
    report = deepcopy(previous)
    data = report['web']
    audit = {'policy':POLICY, 'candidates':candidates, 'attempted_urls':[], 'read_urls':[],
        'deadline_reached':False, 'page_limit_reached':False, 'new_search_queries':0}
    errors = data['errors']
    final_urls = {s['url'] for s in report['sources']}
    identifiers = {m['id'] for m in report['matches']}
    new_number = 1
    for entry in candidates:
        url = entry['url']
        if url in final_urls:
            continue
        if len(data['attempted_urls']) >= data['max_page_attempts']:
            audit['page_limit_reached'] = True
            break
        if clock() - started_at + TOTAL_TIMEOUT > MAX_SECONDS:
            audit['deadline_reached'] = True
            break
        if progress:
            progress('Читаем альтернативные ссылки источников',len(data['attempted_urls']),data['max_page_attempts'],phase='web_fetch')
        audit['attempted_urls'].append(url)
        data['attempted_urls'].append(url)
        try:
            page = fetcher.fetch(url)
            final = web._canonical_url(page['url'])
            if not final:
                raise FetchError('Invalid related source URL','saved_page_invalid')
            if final in final_urls:
                data['urls_deduplicated'] += 1
                continue
            body = page['text']
            if not isinstance(body,str) or len(body.strip()) < 35 or len(body) > web.MAX_TEXT_CHARS:
                raise FetchError('Related body outside text limits','empty_text')
            body_sha = sha256(body.encode()).hexdigest()
            if page.get('text_sha256',body_sha) != body_sha:
                raise FetchError('Related body SHA differs','saved_page_invalid')
            source = {'id':'web-'+sha256(final.encode()).hexdigest()[:24],
                'title':page.get('title') or final, 'url':final, 'text':body,
                'provenance':{'origin':'web_fetch','fetched_at':page.get('fetched_at'),
                    'text_sha256':body_sha,'license_status':'not_verified','training_consent':False}}
            views = web._supplemental_source_views(source,page)
            addition = analyze_with_supplements(text,[source],views)
            report['sources'].extend([source,*views])
            if page.get('content_type') == 'application/pdf' and isinstance(page.get('optional_status'), dict):
                data.setdefault('optional_pdf_status', []).append({**{key:page['optional_status'][key] for key in ('engine','status','code','reason') if key in page['optional_status']},'url':final})
            for match in addition['matches']:
                while 'related-match-'+str(new_number) in identifiers:
                    new_number += 1
                identifier = 'related-match-'+str(new_number)
                identifiers.add(identifier)
                new_number += 1
                report['matches'].append({**match,'id':identifier})
            final_urls.add(final)
            audit['read_urls'].append(final)
        except (RuntimeError,OSError,ValueError,KeyError,TypeError) as error:
            code = getattr(error,'code','fetch_error')
            errors.append({'stage':'fetch','url':url,'code':code,
                'message':'Альтернативную страницу не удалось прочитать; проверка неполная.'})
    new_urls = [c['url'] for c in candidates]
    data['discovered_urls'] = list(dict.fromkeys([*data['discovered_urls'],*new_urls]))
    data['link_discoveries'].extend(candidates)
    data['linked_urls_discovered'] += len(candidates)
    for key in ('urls_discovered','urls_found','urls_supported'):
        data[key] += len(candidates)
    remaining = [u for u in new_urls if u not in data['attempted_urls'] and u not in final_urls]
    data['skipped_urls'] = list(dict.fromkeys([*data['skipped_urls'],*remaining]))
    data['urls_outside_limit'] += len(remaining)
    if remaining:
        errors.append({'stage':'limit','code':'related_page_limit_reached','count':len(remaining),
            'message':'Не все альтернативные ссылки проверены; окончательный процент не установлен.'})
    primary = [s for s in report['sources'] if not s.get('representation_of')]
    supplements = [s for s in report['sources'] if s.get('representation_of')]
    data.update(urls_attempted=len(data['attempted_urls']),sources_checked=len(primary),
        supplemental_text_views=len(supplements),
        html_text_views=sum(s.get('provenance',{}).get('evidence_policy')==web.HTML_EVIDENCE_POLICY for s in supplements),
        pdf_text_views=sum(s.get('provenance',{}).get('evidence_policy')==web.PDF_EVIDENCE_POLICY for s in supplements),
        related_fallback=audit,elapsed_seconds=round(clock()-started_at,3))
    if data['html_text_views']:
        data['html_evidence_policy'] = web.HTML_EVIDENCE_POLICY
    if data['pdf_text_views']:
        data['pdf_evidence_policy'] = web.PDF_EVIDENCE_POLICY
    stats = report['stats']
    stats.update(web._word_coverage(text,report['matches']),matched_characters=_coverage(report['matches']),
        source_count=len({m['source_url'] for m in report['matches']}),checked_source_count=len(primary))
    stats['similarity_percent'] = round(100*stats['matched_characters']/len(text),1)
    partial = data['status']!='complete' or bool(errors) or bool(remaining)
    data['status'] = 'partial' if partial else 'complete'
    stats['uniqueness_percent'] = None if partial else round(100*(stats['total_words']-stats['matched_words'])/stats['total_words'],1)
    if partial and stats['matched_words']:
        stats['uniqueness_upper_bound_percent'] = ((1000*(stats['total_words']-stats['matched_words'])+stats['total_words']-1)//stats['total_words'])/10
    else:
        stats.pop('uniqueness_upper_bound_percent',None)
    if partial and not any(i.get('kind')=='partial_web_check' for i in report['issues']):
        report['issues'].append({'kind':'partial_web_check','message':'Проверка источников неполная; найденные совпадения сохранены.'})
    return report
