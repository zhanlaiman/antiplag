"""Offline release evidence: fixed-sample outcomes and each retrieval stage.

Never sends search/page requests. Expected URLs are used only here for scoring.
Private snapshots and excerpt review pairs stay in the ignored runtime folder.
"""
from collections import Counter
from contextlib import closing
from difflib import SequenceMatcher
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from plagiarism.matching import _tokens, literal_equivalent
from plagiarism.web_evaluation import _url, _report

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.runtime/release-20261113'
FINAL=ROOT/'data/private/web-evaluation/final-70-firstuse'
MATERIALS=ROOT/'data/private/web-evaluation/materials/final-sealed'


def saved_report(identifier):
    for path in (FINAL/'isolated-site/tenants').glob('*/istok.sqlite3'):
        with closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)) as db:
            row=db.execute("SELECT data FROM records WHERE kind='report' AND id=?",(identifier,)).fetchone()
            if not row:continue
            report=json.loads(row[0])
            if 'sources_refs' in report:
                report['sources']=[json.loads(db.execute('SELECT data FROM source_snapshots WHERE hash=?',(h,)).fetchone()[0])
                                   for h in report.pop('sources_refs')]
            return report
    raise ValueError('Complete job has no private report snapshot')


def tokens(text):return [t.value for t in _tokens(text)]


def evaluate(report,case,reviews):
    checked=_report(report,case)
    text=report['text'];target=_tokens(text);web=report['web']
    exact=[m for m in report['matches'] if m['kind']=='exact']
    evidence=[]
    for index,gold in enumerate(case['expected_evidence']):
        a,b=gold['target_span'];wanted=[t for t in target if a<=t.start and t.end<=b]
        source_words=tokens(gold['source_quote'])
        copied=[wanted[i] for block in SequenceMatcher(None,[t.value for t in wanted],source_words,autojunk=False).get_matching_blocks()
                for i in range(block.a,block.a+block.size)]
        urls={_url(u) for u in gold['source_urls']};discovered=bool(urls & {_url(u) for u in web.get('discovered_urls',[])})
        queued=bool(urls & {_url(u) for u in web.get('attempted_urls',[])})
        pages=[s for s in report['sources'] if _url(s['url']) in urls]
        phrase=any(a<=q['start'] and q['end']<=b and ' '.join(tokens(text[q['start']:q['end']])) in ' '.join(source_words)
                   for q in web.get('query_spans',[]))
        extracted=any(' '.join(source_words) in ' '.join(tokens(s['text'])) for s in pages)
        covered=lambda matches:sum(any(m['start']<=t.start and t.end<=m['end'] for m in matches) for t in copied)
        words=covered(exact);expected_words=covered([m for m in exact if _url(m['source_url']) in urls])
        lexical=gold['needs_lexical_highlight'];whole=bool(copied) and words==len(copied)
        # Later positive evidence overrides an earlier phrase/URL gap: a source
        # already read and extracted cannot be lost at the query-selection stage.
        loss=('found' if whole else 'highlight_incomplete' if extracted else 'source_quote_not_extracted' if pages
              else 'expected_url_unread' if queued else 'expected_url_not_queued' if discovered
              else 'no_internal_phrase_observed' if not phrase else 'expected_url_not_returned')
        evidence.append({'index':index,'lexical':lexical,'internal_phrase':phrase,'expected_url_returned':discovered,
                         'expected_url_queued':queued,'expected_url_read':bool(pages),'source_quote_extracted':extracted,
                         'copied_words':len(copied),'highlighted_words':words,'highlighted_expected_source_words':expected_words,
                         'whole_insertion':whole,'earliest_observed_loss':loss if lexical else 'semantic_out_of_scope'})
    for m in report['matches']:
        if not any(g['target_span'][0]<m['end'] and m['start']<g['target_span'][1] for g in case['expected_evidence']):
            reviews.append({'case_id':case['id'],'kind':m['kind'],'target':m['excerpt'],'source':m['source_excerpt'],
                            'url':m['source_url'],'literal_equivalent':literal_equivalent(m['excerpt'],m['source_excerpt']),
                            'review':'pending'})
    return {'id':case['id'],'scenario':case.get('scenario','new'),'outcome':'complete','pages_read':web['sources_checked'],
            'matches':len(report['matches']),'invalid_evidence':len(checked['invalid_highlights']),
            'primary_queries':web.get('primary_queries_attempted',web['queries_attempted']),
            'wikipedia_queries':web.get('post_wiki_queries_attempted',0),'page_attempts':web['urls_attempted'],'evidence':evidence}


def summarize(rows):
    complete=[r for r in rows if r['outcome']=='complete'];e=[e for r in complete for e in r['evidence']];lex=[x for x in e if x['lexical']]
    return {'assigned_documents':len(rows),'complete':len(complete),'failed':sum(r['outcome']=='failed' for r in rows),
            'unstarted':sum(r['outcome']=='unstarted' for r in rows),'completed_without_read_pages':sum(r['pages_read']==0 for r in complete),
            'source_pairs_scored':len(e),'expected_urls_returned':sum(x['expected_url_returned'] for x in e),
            'expected_urls_queued':sum(x['expected_url_queued'] for x in e),'expected_urls_read':sum(x['expected_url_read'] for x in e),
            'source_quotes_extracted':sum(x['source_quote_extracted'] for x in e),'lexical_insertions':len(lex),
            'internal_phrases':sum(x['internal_phrase'] for x in lex),'whole_insertions':sum(x['whole_insertion'] for x in lex),
            'copied_words':sum(x['copied_words'] for x in lex),'highlighted_words':sum(x['highlighted_words'] for x in lex),
            'loss_observations':dict(Counter(x['earliest_observed_loss'] for x in lex if not x['whole_insertion'])),
            'primary_queries':sum(r['primary_queries'] for r in complete),'wikipedia_queries':sum(r['wikipedia_queries'] for r in complete),
            'pages_read':sum(r['pages_read'] for r in complete),'page_attempts':sum(r['page_attempts'] for r in complete),'invalid_evidence':sum(r['invalid_evidence'] for r in complete),
            'negative_documents':sum(not r['evidence'] for r in complete),'negative_documents_with_matches':sum(not r['evidence'] and r['matches']>0 for r in complete)}


def main():
    prior_path=OUT/'review-pairs.json'
    prior_reviews=json.loads(prior_path.read_text()) if prior_path.exists() else []
    reviews=[];cases={c['id']:c for c in json.loads((MATERIALS/'key.json').read_text())['cases']};fixed=[]
    for entry in json.loads((FINAL/'manifest.json').read_text())['cases']:
        path=FINAL/(entry['id']+'.json')
        if not path.exists():fixed.append({'id':entry['id'],'outcome':'unstarted'});continue
        job=json.loads(path.read_text()).get('job',{})
        if job.get('status')!='complete':fixed.append({'id':entry['id'],'outcome':'failed','code':job.get('code')});continue
        report=saved_report(job['result']['report']['id']);fixed.append(evaluate(report,cases[entry['id']],reviews))
        (OUT/(entry['id']+'.report.json')).write_text(json.dumps(report,ensure_ascii=False))
    new=[];base=OUT/'new-sample'
    for c in json.loads((base/'manifest.json').read_text())['cases']:
        path=base/(c['id']+'.report.json')
        if not path.exists():new.append({'id':c['id'],'outcome':'failed' if (base/(c['id']+'.failure.json')).exists() else 'unstarted'});continue
        report=json.loads(path.read_text());gold=[]
        if c['words']:
            source=json.loads((base/c['source_path']).read_text())['text'];a,b=c['source_span']
            gold=[{'target_span':c['target_span'],'source_quote':source[a:b],'source_urls':[c['url']],'needs_lexical_highlight':True}]
        new.append(evaluate(report,{**c,'expected_evidence':gold},reviews))
    review_key=lambda r:tuple(r[k] for k in ('case_id','kind','target','source','url'))
    decisions={review_key(r):r['review'] for r in prior_reviews if r.get('review')!='pending'}
    for r in reviews:r['review']=decisions.get(review_key(r),'pending')
    result={'fixed20':{'summary':summarize(fixed),'cases':fixed},'new4':{'summary':summarize(new),'cases':new},
            'limitations':'Fixed20 was already opened before this release; no longer independent. New4 is a small convenience sample. Phrase gaps are observations, not proof that changing a query would recover the source. Semantic cases are scored separately from literal insertions.'}
    (OUT/'quality-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));(OUT/'review-pairs.json').write_text(json.dumps(reviews,ensure_ascii=False,indent=2))
    print(json.dumps({k:v['summary'] for k,v in result.items() if isinstance(v,dict)},ensure_ascii=False))


if __name__=='__main__':main()
