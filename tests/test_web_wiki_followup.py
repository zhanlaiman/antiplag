"""Authored in-memory fixtures only; no private corpus, API, page or Store reads."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0,str(ROOT))
from plagiarism.matching import analyze,_tokens
from plagiarism.web_fetch import FetchError
from plagiarism import web_search as web

SPEC=importlib.util.spec_from_file_location('post_wiki_production_under_test',ROOT/'plagiarism/web_wiki_followup.py')
draft=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(draft)

class _PrototypeContractAdapter:
    # The same sixteen behavioral fixtures exercise the production entrypoint.
    @staticmethod
    def extend_report(text,legacy,plan,lookup_wiki,lookup_page,*,initial_elapsed_seconds=0.0,clock=None):
        import time
        now=clock or time.monotonic
        class Search:
            provider='mediawiki'
            halt_code=None
            last_usage=None
            def search(self,query,call_id):
                record=lookup_wiki(query)
                self.last_usage={'public_queries':0,'cost_kzt':0,'cached':True}
                if not isinstance(record,dict) or record.get('state')!='complete':
                    self.halt_code='cached_wiki_unavailable'
                    raise web.WebSearchError('Authored unavailable reply','saved_wiki_unavailable')
                return record['results']
        aliases=[s['url'] for s in legacy['sources']]
        aliases.extend(e['final_url'] for e in legacy.get('offline_replay',{}).get('cached_physical_outcomes',[]) if e.get('final_url'))
        return draft.append_post_wiki(text,legacy,plan,primary_urls=legacy['web']['discovered_urls'],
            attempted_urls=legacy['web']['attempted_urls'],final_aliases=aliases,
            request_id=legacy['id'],started_at=now()-initial_elapsed_seconds,
            secondary_searcher=Search(),fetcher=lookup_page,clock=now)
probe=_PrototypeContractAdapter()

OLD='Архивист внимательно переписал старинные номера и проверил подписи владельцев.'
NEW='Исследователь сопоставил редкие снимки кометы и обнаружил изменения яркости хвоста.'
TAIL='Далее редактор подготовил отдельную заметку о сроках обсуждения.'
NEGATIVE='Астрономы института обсуждают расстояния между звёздами. Летом техник Павел настраивает телескоп, ремонтирует крепления и обновляет таблицы наблюдений.'
OLD_REQUEST='https://example.org/old-request'
OLD_FINAL='https://example.org/old-final.pdf'
OLD_FAILED='https://example.org/failed'
A='https://example.org/a'
B='https://example.org/b'
C='https://example.org/c'
FOLLOW='https://example.org/not-selected-followup'

def sha(text):return sha256(text.encode()).hexdigest()
def plan_for(text):
    tokens=_tokens(text)
    starts=[0,4,8,12,16,20]
    result=[]
    for start in starts:
        window=tokens[start:start+6]
        result.append({'query':'"'+' '.join(t.value for t in window)+'"','start':window[0].start,'end':window[-1].end})
    return result

def fixture(*,full=False,no_sources=False):
    text=OLD+'\n\n'+NEW+'\n\n'+TAIL
    primary_text=text if full else OLD
    sources=[] if no_sources else [
      {'id':'web-old-primary','title':'Saved PDF primary','url':OLD_FINAL,'text':primary_text,
       'provenance':{'origin':'web_fetch','text_sha256':sha(primary_text),'fetched_at':'2026-11-01T00:00:00+00:00'}},
      {'id':'web-old-view','title':'Saved independent PDF view','url':OLD_FINAL,
       'text':'Приложение содержит другие общие сведения о настройке лабораторных приборов.',
       'representation_of':'web-old-primary',
       'provenance':{'origin':'web_fetch','text_sha256':sha('Приложение содержит другие общие сведения о настройке лабораторных приборов.'),'fetched_at':'2026-11-01T00:00:00+00:00'}}]
    matched=analyze(text,[s for s in sources if not s.get('representation_of')])
    matches=matched['matches']
    covered=sum(any(m['kind']=='exact' and m['start']<=t.start and t.end<=m['end'] for m in matches) for t in _tokens(text))
    plan=plan_for(text)
    report={'id':'a'*32,'scope':'web_search','text':text,'sources':sources,'matches':matches,
      'stats':{'total_words':len(_tokens(text)),'matched_words':covered,'uniqueness_percent':None,'source_count':len(sources)},
      'issues':[],
      'web':{'status':'partial','provider':'tavily','query_strategy':web.DENSE_STRATEGY,
       'queries_planned':6,'queries_attempted':6,'queries_completed':6,'queries_unattempted':0,
       'query_spans':[{k:q[k] for k in ('start','end')} for q in plan],
       'named_references_enabled':True,'zero_fallback_enabled':True,
       'attempted_urls':[OLD_REQUEST,OLD_FAILED],
       'discovered_urls':[OLD_REQUEST,OLD_FAILED,'https://example.org/unread1','https://example.org/unread2'],
       'skipped_urls':['https://example.org/unread1','https://example.org/unread2'],
       'urls_attempted':2,'urls_discovered':4,'urls_found':4,'urls_supported':4,
       'search_urls_discovered':4,'sources_checked':0 if no_sources else 1,
       'supplemental_text_views':0 if no_sources else 1,'urls_outside_limit':2,
       'errors':[{'stage':'fetch','url':OLD_FAILED,'code':'robots_denied'},
                 {'stage':'limit','code':'url_limit_reached','count':2}],
       'elapsed_seconds':0.0,'max_page_attempts':24},
      'offline_replay':{'kind':'authored_fixture','network_queries':0,'fresh_page_load_attempts':0,
       'cached_physical_outcomes':[{'requested_url':OLD_REQUEST,'final_url':OLD_FINAL,'state':'read_saved_body'}]}}
    if no_sources:
        report['web']['status']='failed'
        report['web']['errors'].insert(0,{'stage':'fetch','url':OLD_REQUEST,'code':'empty_text'})
        report['offline_replay']['cached_physical_outcomes']=[
            {'requested_url':OLD_REQUEST,'state':'saved_failure','code':'empty_text'},
            {'requested_url':OLD_FAILED,'state':'saved_failure','code':'robots_denied'}]
    return text,report,plan

def page(url,text=NEW,*,links=()):
    return {'url':url,'title':'Authored public source','text':text,'text_sha256':sha(text),
      'fetched_at':'2026-11-01T01:00:00+00:00','links':list(links)}

class Clock:
    def __init__(self):self.seconds=0.0
    def __call__(self):return self.seconds
    def advance(self,seconds):self.seconds+=seconds

class PostWikiGuards(unittest.TestCase):
    def extend(self,text,legacy,plan,groups=None,pages=None,*,elapsed=0.0,clock=None):
        qcalls=[];pcalls=[];groups=groups if groups is not None else [[],[]]
        def wiki(query):
            qcalls.append(query)
            value=groups[len(qcalls)-1]
            return value if isinstance(value,dict) else {'state':'complete','results':value}
        def fetch(url):
            pcalls.append(url)
            value=(pages or {}).get(url)
            if isinstance(value,Exception):raise value
            if value is None:raise AssertionError('Unexpected page lookup '+url)
            return deepcopy(value)
        report,audit=probe.extend_report(text,legacy,plan,wiki,fetch,initial_elapsed_seconds=elapsed,clock=clock or Clock())
        return report,audit,qcalls,pcalls

    def test_full_grounded_input_still_checks_another_source(self):
        text,old,plan=fixture(full=True)
        old['stats']['matched_words']=0  # Stale stats cannot change selection or inflate coverage.
        report,audit,qs,ps=self.extend(text,old,plan,[[{'url':A}],[]],{A:page(A,text)})
        self.assertEqual(qs,[q['query'] for q in plan[:2]]);self.assertEqual(ps,[A])
        self.assertEqual(report['sources'][:2],old['sources']);self.assertTrue(audit['prefix_preserved'])
        self.assertTrue(audit['old_exact_coverage_preserved'])
        self.assertEqual(report['stats']['matched_words'],len(_tokens(text)))
        self.assertEqual(report['web']['sources_checked'],2)

    def test_fake_full_stats_cannot_close_partial_gate(self):
        text,old,plan=fixture();old['stats']['matched_words']=old['stats']['total_words'];old['stats']['uniqueness_percent']=0
        report,audit,qs,_=self.extend(text,old,plan)
        self.assertFalse(audit['gate_full_input']);self.assertEqual(audit['primary_exact_words'],9)
        self.assertEqual(qs,[q['query'] for q in plan[:2]])
        self.assertEqual(report['stats']['matched_words'],9)

    def test_round_robin_two_urls_excludes_requested_and_final_alias(self):
        text,old,plan=fixture()
        groups=[[{'url':OLD_REQUEST},{'url':A},{'url':C}],
                [{'url':OLD_FINAL},{'url':B},{'url':A}]]
        report,audit,qs,ps=self.extend(text,old,plan,groups,{A:page(A),B:page(B,NEGATIVE)})
        self.assertEqual(ps,[A,B]);self.assertEqual(audit['extra_attempted_urls'],[A,B]);self.assertEqual(len(qs),2)
        self.assertEqual(report['web']['attempted_urls'][:2],old['web']['attempted_urls'])
        self.assertEqual(report['sources'][:2],old['sources']);self.assertTrue(audit['old_exact_coverage_preserved'])

    def test_repeated_results_deduplicate_before_two_url_cap(self):
        text,old,plan=fixture();groups=[[{'url':A},{'url':B},{'url':C}],[{'url':A},{'url':C}]]
        _,audit,_,ps=self.extend(text,old,plan,groups,{A:page(A),B:page(B,NEGATIVE)})
        self.assertEqual(ps,[A,B]);self.assertEqual(audit['extra_attempted_urls'],[A,B])

    def test_extra_body_does_not_trigger_recursive_followup(self):
        text,old,plan=fixture();groups=[[{'url':A}],[]]
        report,_,_,ps=self.extend(text,old,plan,groups,{A:page(A,links=[{'url':FOLLOW,'label':NEW}])})
        self.assertEqual(ps,[A]);self.assertNotIn(FOLLOW,report['web']['attempted_urls'])

    def test_pdf_view_prefix_and_old_word_coverage_are_preserved(self):
        text,old,plan=fixture();before=deepcopy(old)
        report,audit,_,_=self.extend(text,old,plan,[[{'url':A}],[]],{A:page(A)})
        self.assertEqual(old,before);self.assertEqual(report['sources'][:2],before['sources'])
        self.assertEqual(report['sources'][1]['representation_of'],'web-old-primary')
        self.assertTrue(audit['old_exact_coverage_preserved']);self.assertEqual(report['stats']['matched_words'],19)
        self.assertEqual(report['web']['sources_checked'],2)  # primary+view is one physical old page.

    def test_old_matched_PDF_spacing_view_keeps_its_grounded_coverage(self):
        from plagiarism.pdf_evidence import analyze_with_supplements
        text,old,plan=fixture()
        primary,view=old['sources']
        primary['text']=OLD.replace('старинные','ста ринные')
        primary['provenance']['text_sha256']=sha(primary['text'])
        view['text']=OLD;view['provenance']['text_sha256']=sha(OLD)
        matched=analyze_with_supplements(text,[primary],[view])
        old['matches']=matched['matches']
        old['stats']['matched_words']=9
        self.assertTrue(any(m['source_id']==view['id'] for m in old['matches']))
        before=deepcopy(old)
        report,audit,_,_=self.extend(text,old,plan,[[{'url':A}],[]],{A:page(A)})
        self.assertEqual(report['sources'][:2],before['sources'])
        self.assertEqual(report['stats']['matched_words'],19)
        self.assertTrue(audit['old_exact_coverage_preserved'])
        self.assertTrue(any(m['source_id']==view['id'] for m in report['matches']))

    def test_snippet_never_substitutes_for_negative_source_body(self):
        text,old,plan=fixture();groups=[[{'url':A,'snippet':text}],[]]
        report,_,_,_=self.extend(text,old,plan,groups,{A:page(A,NEGATIVE)})
        self.assertEqual(report['stats']['matched_words'],9)
        self.assertFalse(any(m.get('source_url')==A for m in report['matches']))

    def test_API_deadline_blocks_new_requests_but_does_not_trim_legacy(self):
        text,old,plan=fixture();report,audit,qs,ps=self.extend(text,old,plan,elapsed=286)
        self.assertTrue(audit['deadline_API']);self.assertEqual(qs,[]);self.assertEqual(ps,[])
        self.assertEqual(report['sources'][:2],old['sources']);self.assertEqual(report['web']['attempted_urls'][:2],old['web']['attempted_urls'])

    def test_fetch_deadline_has_twenty_second_headroom(self):
        text,old,plan=fixture();groups=[[{'url':A}],[]]
        report,audit,qs,ps=self.extend(text,old,plan,groups,{A:page(A)},elapsed=281)
        self.assertEqual(len(qs),2);self.assertTrue(audit['deadline_fetch']);self.assertEqual(ps,[])
        self.assertIn(A,audit['unattempted_extra_urls']);self.assertIn(A,report['web']['skipped_urls'])
        self.assertEqual(audit['unknown_extra_urls'],[]);self.assertEqual(report['web']['status'],'partial')
        self.assertIsNone(report['stats']['uniqueness_percent']);self.assertEqual(report['stats']['matched_words'],9)

    def test_slow_first_API_blocks_second_API_and_fetch(self):
        text,old,plan=fixture();clock=Clock();qs=[];ps=[]
        def wiki(query):qs.append(query);clock.advance(10);return {'state':'complete','results':[{'url':A}]}
        def fetch(url):ps.append(url);return page(url)
        report,audit=probe.extend_report(text,old,plan,wiki,fetch,initial_elapsed_seconds=280,clock=clock)
        self.assertEqual(len(qs),1);self.assertEqual(ps,[]);self.assertTrue(audit['deadline_API']);self.assertTrue(audit['deadline_fetch'])
        self.assertEqual(report['stats']['matched_words'],9)

    def test_unknown_page_is_explicit_and_never_one_hundred_unique(self):
        text,old,plan=fixture(no_sources=True)
        report,audit,_,ps=self.extend(text,old,plan,[[{'url':A}],[]],{A:FetchError('No saved page body','saved_page_cache_miss')})
        self.assertEqual(ps,[A]);self.assertEqual(audit['unknown_extra_urls'],[A]);self.assertEqual(report['web']['sources_checked'],0)
        self.assertIsNone(report['stats']['uniqueness_percent']);self.assertEqual(report['stats']['matched_words'],0)

    def test_no_sources_legacy_can_gain_one_read_source(self):
        text,old,plan=fixture(no_sources=True)
        report,audit,_,_=self.extend(text,old,plan,[[{'url':A}],[]],{A:page(A)})
        self.assertEqual(report['web']['sources_checked'],1);self.assertEqual(report['stats']['matched_words'],10)
        self.assertIsNone(report['stats']['uniqueness_percent']);self.assertTrue(audit['old_exact_coverage_preserved'])

    def test_citation_priority_query_is_not_resent_to_Wiki(self):
        text,old,plan=fixture();text='«'+text[:len(OLD)]+'»'+text[len(OLD):]
        # Rebuild the authored report with the quoted target and real excerpt offsets.
        source=old['sources'][0];matches=analyze(text,[source])['matches'];old['text']=text;old['matches']=matches
        quote=web._citation_queries(text)[0];base=plan_for(text);plan=[quote]+[q for q in base if q['query']!=quote][:5]
        old['web']['query_spans']=[{k:q[k] for k in ('start','end')} for q in plan]
        _,_,qs,_=self.extend(text,old,plan)
        self.assertEqual(qs,[q['query'] for q in plan[1:3]]);self.assertNotIn(quote['query'],qs)

    def test_all_citation_priority_queries_leave_no_Wiki_slot(self):
        text,old,_=fixture()
        text+='\n\n'+' '.join('«Authored clause number '+str(i)+' has unique wording for citation priority verification only»' for i in range(6))
        plan=web._citation_queries(text)
        self.assertEqual(len(plan),6)
        old['text']=text;old['stats']['total_words']=len(_tokens(text))
        old['web']['query_spans']=[{k:q[k] for k in ('start','end')} for q in plan]
        report,audit,qs,ps=self.extend(text,old,plan)
        self.assertEqual(qs,[]);self.assertEqual(ps,[])
        self.assertEqual(report['sources'][:2],old['sources']);self.assertEqual(audit['post_query_calls'],[])

    def test_provider_request_metadata_counts_eight_not_six(self):
        text,old,plan=fixture();report,_,qs,_=self.extend(text,old,plan)
        self.assertEqual(len(qs),2)
        self.assertEqual(report['web']['queries_attempted'],8)
        self.assertEqual(report['web']['query_spans'][:6],old['web']['query_spans'])
        self.assertEqual(report['web']['max_page_attempts'],26)

class ProductionEntrypointGuards(unittest.TestCase):
    def invoke(self,search,*,legacy=None,text=None,plan=None,full=None,fetch=None,clock=None,started=0):
        if legacy is None:text,legacy,plan=fixture()
        return draft.append_post_wiki(text,legacy,plan,primary_urls=full or legacy['web']['discovered_urls'],
            attempted_urls=legacy['web']['attempted_urls'],final_aliases={OLD_REQUEST:OLD_FINAL},
            request_id='c'*32,started_at=started,secondary_searcher=search,
            fetcher=fetch or (lambda u:page(u)),clock=clock or Clock())

    def test_rate_error_preserves_primary_and_stops_remaining_secondary(self):
        class Rate:
            provider='mediawiki';last_usage=None;halt_code=None
            def __init__(self):self.calls=[]
            def search(self,q,call):
                self.calls.append((q,call));self.last_usage={'public_queries':1,'cost_kzt':0,'cached':False}
                self.halt_code='search_rate_limited';raise web.WebSearchError('typed fixture rate error','search_rate_limited')
        search=Rate();report,audit=self.invoke(search)
        self.assertEqual(len(search.calls),1);self.assertTrue(audit['secondary_halted'])
        self.assertEqual(audit['actual_new_public_queries'],1);self.assertEqual(report['stats']['matched_words'],9)
        self.assertEqual(report['web']['queries_attempted'],7);self.assertEqual(report['web']['post_wiki_queries_planned'],2)
        self.assertIsNone(report['stats']['uniqueness_percent'])

    def test_unknown_saved_ID_is_not_retried_or_counted_as_known_zero(self):
        class Unknown:
            provider='mediawiki';last_usage=None;halt_code=None
            def __init__(self):self.calls=[]
            def search(self,q,call):
                self.calls.append(call);raise web.WebSearchError('Prior durable intent outcome unknown','search_already_attempted')
        search=Unknown();report,audit=self.invoke(search)
        self.assertEqual(len(search.calls),1);self.assertTrue(audit['secondary_halted'])
        self.assertIsNone(audit['actual_new_public_queries']);self.assertEqual(audit['public_query_usage_unknown_calls'],1)
        self.assertEqual(report['stats']['matched_words'],9);self.assertTrue(audit['prefix_preserved'])

    def test_cached_query_usage_zero_is_separate_from_two_logical_calls(self):
        class Cached:
            provider='mediawiki';last_usage=None;halt_code=None
            def __init__(self):self.ids=[]
            def search(self,q,call):self.ids.append(call);self.last_usage={'public_queries':0,'cost_kzt':0,'cached':True};return []
        search=Cached();report,audit=self.invoke(search)
        self.assertEqual(len(search.ids),2);self.assertEqual(audit['actual_new_public_queries'],0)
        self.assertEqual(report['web']['total_provider_requests'],8)
        self.assertEqual(report['web']['post_wiki_new_public_queries'],0);self.assertTrue(report['web']['post_wiki_enabled'])
        expected=[sha256(('c'*32+'\0mediawiki-post\0'+str(i)).encode()).hexdigest() for i in range(2)]
        self.assertEqual(search.ids,expected)

    def test_full_URL_accounting_does_not_use_truncated_legacy_prefix(self):
        text,old,plan=fixture();full=[OLD_REQUEST,OLD_FAILED]+[f'https://example.org/full-{i}' for i in range(38)]
        old['web'].update(discovered_urls=full[:30],skipped_urls=full[2:30],urls_found=40,urls_supported=40,urls_discovered=40,search_urls_discovered=40,urls_outside_limit=38,
            urls_rejected=[{'url':'unsupported://authored','code':'unsupported_result_url'}])
        class Cached:
            provider='mediawiki';halt_code=None;last_usage=None
            def search(self,q,call):self.last_usage={'public_queries':0,'cost_kzt':0,'cached':True};return []
        report,_=self.invoke(Cached(),legacy=old,text=text,plan=plan,full=full)
        self.assertEqual(report['web']['urls_discovered'],40);self.assertEqual(report['web']['urls_found'],40)
        self.assertEqual(report['web']['urls_supported'],40);self.assertEqual(report['web']['urls_outside_limit'],38)
        self.assertIn(full[-1],report['web']['skipped_urls']);self.assertEqual(report['web']['urls_rejected'],old['web']['urls_rejected'])

    def test_already_named_URL_returned_by_Wiki_counts_as_a_direct_search_URL(self):
        text,old,plan=fixture();full=[OLD_REQUEST,OLD_FAILED,A,'https://example.org/spare']
        old['web'].update(discovered_urls=full,skipped_urls=full[2:],search_urls_discovered=2,
            input_references=[{'url':A,'title':'Authored named reference'}])
        class Search:
            provider='mediawiki';last_usage=None;halt_code=None
            def search(self,q,call):self.last_usage={'public_queries':0,'cost_kzt':0,'cached':True};return [{'url':A}]
        report,_=self.invoke(Search(),legacy=old,text=text,plan=plan,full=full)
        self.assertEqual(report['web']['urls_discovered'],6)
        self.assertEqual(report['web']['search_urls_discovered'],4)
        self.assertEqual(report['web']['urls_found'],4)
        self.assertEqual(report['web']['sources_checked'],2)

    def test_stale_no_sources_removed_only_after_a_new_read_source(self):
        text,old,plan=fixture(no_sources=True)
        old['web']['errors'].append({'stage':'fetch','code':'no_sources_checked','message':'None available'})
        old['issues'].append({'kind':'no_sources','message':'None read'})
        class Search:
            provider='mediawiki';last_usage=None;halt_code=None
            def search(self,q,call):self.last_usage={'public_queries':0,'cost_kzt':0,'cached':True};return [{'url':A}]
        report,_=self.invoke(Search(),legacy=old,text=text,plan=plan)
        self.assertEqual(report['web']['sources_checked'],1)
        self.assertFalse(any(e.get('code')=='no_sources_checked' for e in report['web']['errors']))
        self.assertFalse(any(i.get('kind')=='no_sources' for i in report['issues']))
        self.assertTrue(any(e.get('code')=='robots_denied' for e in report['web']['errors']))
        def missing(url):raise FetchError('Fixture cache miss','saved_page_cache_miss')
        report,_=self.invoke(Search(),legacy=old,text=text,plan=plan,fetch=missing)
        self.assertTrue(any(e.get('code')=='no_sources_checked' for e in report['web']['errors']))
        self.assertTrue(any(i.get('kind')=='no_sources' for i in report['issues']))
        self.assertEqual(report['web']['sources_checked'],0);self.assertIsNone(report['stats']['uniqueness_percent'])

    def test_wrong_paid_provider_is_rejected_before_callback(self):
        class Paid:
            provider='tavily'
            def search(self,*args):raise AssertionError('Must reject before any paid callback')
        with self.assertRaises(ValueError):self.invoke(Paid())

    def test_observed_pdf_page_limit_is_a_known_failure_not_an_unknown_body(self):
        class Search:
            provider='mediawiki';last_usage=None;halt_code=None
            def search(self,q,call):return [{'url':A}]
        def fetch(url):raise FetchError('Too many PDF pages','pdf_page_limit')
        report,audit=self.invoke(Search(),fetch=fetch)
        self.assertEqual(audit['unknown_extra_urls'],[])
        self.assertEqual(audit['failed_extra_urls'],[A])
        self.assertIsNone(report['stats']['uniqueness_percent'])

    def test_original_start_budget_is_used_not_legacy_elapsed_metadata(self):
        class Search:
            provider='mediawiki'
            def search(self,*args):raise AssertionError('No time for an API request')
        clock=Clock();clock.advance(286)
        report,audit=self.invoke(Search(),clock=clock,started=0)
        self.assertTrue(audit['deadline_API']);self.assertEqual(report['web']['post_wiki_queries_attempted'],0)
        self.assertEqual(report['web']['queries_attempted'],6)

    def test_module_import_requires_no_plagiarism_modules(self):
        import builtins
        from unittest.mock import patch
        original=builtins.__import__
        def guarded(name,*args,**kwargs):
            if name.startswith('plagiarism'):raise AssertionError('Circular eager import '+name)
            return original(name,*args,**kwargs)
        spec=importlib.util.spec_from_file_location('post_wiki_import_guard',ROOT/'plagiarism/web_wiki_followup.py')
        with patch.object(builtins,'__import__',guarded):
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(callable(module.append_post_wiki))

def _whole_plan(text):
    tokens=_tokens(text)
    return {'query':'"'+' '.join(t.value[:40] for t in tokens)+'"','start':tokens[0].start,'end':tokens[-1].end}

class FrozenB2ClauseGuards(unittest.TestCase):
    def test_one_or_two_prefix_words_same_end_primary_untouched(self):
        for text in ['Уточнение — архивные сведения помогают сотрудникам готовить точные описи старинных документов.','Комментарий автора — независимые наблюдения уточняют период обращения далекой малой планеты.']:
            q=_whole_plan(text);primary=[deepcopy(q)];before=deepcopy(primary)
            chosen,audit=draft.shrink_queries(text,primary,[q])
            self.assertEqual(primary,before);self.assertEqual(chosen[0]['end'],q['end']);self.assertGreater(chosen[0]['start'],q['start'])
            self.assertEqual(audit[0]['reason'],'strict_em_dash_prefix_clipped')

    def test_hyphen_endash_colon_semicolon_and_unspaced_emdash_do_not_trigger(self):
        for dash in ['-','–',':',';','—']:
            text='Уточнение'+(' '+dash+' ' if dash!='—' else dash)+'архивные сведения помогают сотрудникам готовить точные описи старинных документов.'
            q=_whole_plan(text);self.assertEqual(draft.shrink_queries(text,[q],[q])[0],[q])

    def test_multiple_dashes_long_prefix_or_short_suffix_rejected(self):
        for text in ['Уточнение — архивные сведения — помогают сотрудникам готовить точные описи старинных документов.','Комментарий этого автора — архивные сведения помогают сотрудникам готовить точные описи.','Уточнение — всего лишь пять простых слов.']:
            q=_whole_plan(text);self.assertEqual(draft.shrink_queries(text,[q],[q])[0],[q])

    def test_citation_priority_never_shortened_or_selected(self):
        text='«Уточнение — архивные сведения помогают сотрудникам готовить точные описи старинных документов»'
        q=web._citation_queries(text)[0];self.assertEqual(draft.shrink_queries(text,[q],[q])[0],[q])
        self.assertEqual(draft.select_post_queries(text,[q]),[])

    def test_duplicate_against_any_original_query_keeps_original(self):
        text='Уточнение — архивные сведения помогают сотрудникам готовить точные описи старинных документов.'
        q=_whole_plan(text);suffix=text[text.index('архивные'):];other=_whole_plan(suffix);other.update(start=text.index('архивные'),end=q['end'])
        chosen,audit=draft.shrink_queries(text,[q,other],[q]);self.assertEqual(chosen,[q]);self.assertEqual(audit[0]['reason'],'duplicate_original_or_prior_clip')

    def test_count_order_and_subsets_only(self):
        text='Комментарий — архивные сведения помогают сотрудникам готовить точные описи старинных документов. Другие независимые рассуждения оставляют проверку неизменной.'
        q=_whole_plan(text[:text.index(' Другие')]);second=_whole_plan(text[text.index('Другие'):]);offset=text.index('Другие');second={**second,'start':second['start']+offset,'end':second['end']+offset}
        chosen,_=draft.shrink_queries(text,[q,second],[q,second]);self.assertEqual(len(chosen),2);self.assertEqual(chosen[1],second)
        self.assertTrue(q['start']<=chosen[0]['start']<chosen[0]['end']==q['end'])

    def test_duplicate_against_prior_clip_keeps_the_second_original(self):
        suffix='архивные сведения помогают сотрудникам готовить точные описи старинных документов'
        first='Уточнение — '+suffix+'.'
        text=first+' Комментарий — '+suffix+'.'
        q1=_whole_plan(first);offset=len(first)+1;q2=_whole_plan(text[offset:]);q2.update(start=q2['start']+offset,end=q2['end']+offset)
        selected,audit=draft.shrink_queries(text,[q1,q2],[q1,q2])
        self.assertNotEqual(selected[0]['query'],q1['query']);self.assertEqual(selected[1],q2)
        self.assertEqual(audit[1]['reason'],'duplicate_original_or_prior_clip')

    def test_public_selector_preserves_primary_index_and_helper_uses_same_queries(self):
        text='Уточнение — архивные сведения помогают сотрудникам готовить точные описи старинных документов.'
        q=_whole_plan(text);before=deepcopy(q);chosen=draft.select_post_queries(text,[q])
        self.assertEqual(q,before);self.assertEqual(chosen[0]['primary_query_index'],0);self.assertEqual(chosen[0]['end'],q['end'])
        unchanged=draft.select_post_queries(text,[q],secondary_query_policy=draft.UNCHANGED_SECONDARY_QUERY_POLICY)
        self.assertEqual(unchanged[0]['query'],q['query']);self.assertNotEqual(chosen[0]['query'],q['query'])

    def test_progress_callbacks_are_before_secondary_search_and_fetch(self):
        text,old,plan=fixture();events=[]
        class Search:
            provider='mediawiki';last_usage=None;halt_code=None
            def search(self,q,call):events.append('search');self.last_usage={'public_queries':0,'cost_kzt':0,'cached':True};return [{'url':A}]
        def progress(stage,current,total,phase):events.append('progress:'+phase)
        def fetch(url):events.append('fetch');return page(url)
        report,audit=draft.append_post_wiki(text,old,plan,primary_urls=old['web']['discovered_urls'],
            attempted_urls=old['web']['attempted_urls'],final_aliases={OLD_REQUEST:OLD_FINAL},request_id='d'*32,
            started_at=0,secondary_searcher=Search(),fetcher=fetch,clock=Clock(),progress=progress)
        self.assertEqual(events[:4],['progress:web_search','search','progress:web_search','search'])
        self.assertLess(events.index('progress:web_fetch'),events.index('fetch'))
        self.assertEqual(report['web']['discovery_strategy'],'primary-mediawiki-post-v3-paragraph-clause')
        self.assertEqual(report['web']['primary_provider'],'tavily');self.assertEqual(report['web']['secondary_provider'],'mediawiki')
        self.assertEqual(audit['post_query_calls'][0]['query'],draft.select_post_queries(text,plan)[0]['query'])

if __name__=='__main__':unittest.main()
