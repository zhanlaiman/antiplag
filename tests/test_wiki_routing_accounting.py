"""Authored temporary routing artifacts only; no actual DEV/source reads."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plagiarism.saas import report_identifier
from scripts import wiki_routing_accounting as a


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


class WikiRoutingAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.base=self.root/'data/private/web-evaluation';self.out=self.base/a.SERIES
        self.texts={cid:'Authored independent text '+cid for cid in a.IDS};plans={};routes={};reuse={}
        for n,cid in enumerate(a.IDS):
            plans[cid]=[{'query':f'"Authored fixture {cid} phrase {i}"','start':i*2,'end':i*2+1} for i in range(5 if n<13 else 6)]
            routes[cid]=[{**q,'index':i,'citation_protected':False,'provider':'mediawiki' if i<2 else 'tavily'} for i,q in enumerate(plans[cid])]
        for cid in ('D01','D02','D20'):
            path=self.base/'dev-mediawiki-singleton-v3'/(a.digest('old'+cid)+'-mediawiki.json');q=plans[cid][0]['query']
            write(path,{'state':'complete','query':q,'parameters':{**a.PARAMS,'srsearch':q},'results':[]})
            reuse[cid+':0']={'path':str(path.relative_to(self.root)),'sha256':sha256(path.read_bytes()).hexdigest()}
        c={'version':a.VERSION,'case_ids':list(a.IDS),'maximum_new_Wiki_GETs':57,'maximum_source_HTTP_requests':2160,
           'maximum_new_Fetcher_attempts':720,'new_Tavily_queries':0,'no_retries':True,'no_Tavily_fallback':True,
           'default_changed':False,'api_parameters':a.PARAMS,'plans':plans,'routes':routes,'exact_singleton_reuse':reuse,
           'input_sha256':{cid:a.digest(text) for cid,text in self.texts.items()}}
        rid=a.digest(json.dumps(c,sort_keys=True,ensure_ascii=False,allow_nan=False))
        self.run={'id':rid,'configuration':c,'request_ids':{cid+'-'+role:a.digest(rid+cid+role)[:32] for cid in a.IDS for role in ('baseline','selected')}}
        write(self.out/'run.json',self.run)
        _,self.allowed,_,_=a.identity(self.run)

    def add_markers(self):
        for call,identity in self.allowed.items():write(self.out/(call+'-mediawiki.json'),{
            **identity,'request_id':call,'parameters':{**a.PARAMS,'srsearch':identity['query']},'state':'complete'})

    def add_reports(self):
        c=self.run['configuration']
        for cid in a.IDS:
            for role in ('baseline','selected'):
                plan=c['plans'][cid];key=cid+'-'+role
                events=[{'index':i,'query':q['query'],'provider':'tavily' if role=='baseline' else c['routes'][cid][i]['provider'],
                         'state':'complete','new_Tavily_calls':0} for i,q in enumerate(plan)]
                write(self.out/(key+'.json'),{'id':report_identifier(self.run['request_ids'][key]),'text':self.texts[cid],
                    'web':{'queries_attempted':len(plan),'query_strategy':a.VERSION+':'+role,
                           'query_spans':[{k:q[k] for k in ('start','end')} for q in plan],
                           'routed_query_events':events,'page_outcome_events':[]}})

    def row(self):return a.collect(self.base,self.root)[0][0]

    def test_complete_334logical_slots_are_only57new_anonymous_gets_no_tavily_credits(self):
        self.add_markers();self.add_reports();row=self.row()
        self.assertEqual(row['saved_reports'],60);self.assertEqual(row['logical_queries_processed'],334)
        self.assertEqual(row['selected_Wiki_slots'],60);self.assertEqual(row['exact_cached_Wiki_slots'],3)
        self.assertEqual(row['selected_cached_Tavily_slots'],107);self.assertEqual(row['actual_new_mediawiki_intents'],57)
        self.assertEqual(row['total_new_mediawiki_gets'],57);self.assertTrue(row['accounting_complete'])
        self.assertEqual(row['new_Tavily_calls'],0);self.assertFalse(row['credit_API'])
        self.assertNotIn('total_basic_credits',row)

    def test_reports_with_missing_markers_preserve_unknown_total_not_zero(self):
        self.add_reports();row=self.row()
        self.assertEqual(row['actual_new_mediawiki_intents'],0)
        self.assertEqual(row['minimum_new_mediawiki_attempts_consistent_with_reports'],57)
        self.assertIsNone(row['total_new_mediawiki_gets']);self.assertFalse(row['accounting_complete'])

    def test_changed_cached_query_or_marker_cannot_fill_a_bound_slot(self):
        self.add_markers();self.add_reports();path=next(self.out.glob('*-mediawiki.json'));r=json.loads(path.read_text());r['query']='shorter altered phrase';write(path,r)
        with self.assertRaisesRegex(ValueError,'frozen identity'):self.row()
        path.unlink();origin=next(iter(self.run['configuration']['exact_singleton_reuse'].values()));p=self.root/origin['path'];r=json.loads(p.read_text());r['query']='another query';write(p,r)
        with self.assertRaisesRegex(ValueError,'Cached Wiki'):self.row()

    def test_pdf_page_limit_is_observed_failure_while_budget_cap_is_unknown(self):
        self.add_markers();self.add_reports();path=self.out/'D24-selected.json';r=json.loads(path.read_text())
        r['web']['page_outcome_events']=[{'state':'failed','code':'pdf_page_limit'},{'state':'failed','code':'source_http_cap'}];write(path,r)
        row=self.row();self.assertEqual(row['selected_unknown_page_outcomes'],1)
        self.assertEqual(row['selected_observed_typed_failures'],{'pdf_page_limit':1})
        self.assertFalse(a.unknown_page_outcome({'state':'failed','code':'pdf_page_limit'}))

    def test_wrong_report_id_and_route_fingerprint_fail_closed(self):
        self.add_reports();p=self.out/'D01-selected.json';r=json.loads(p.read_text());r['id']='b'*32;write(p,r)
        with self.assertRaisesRegex(ValueError,'report/input'):self.row()
        bad=deepcopy(self.run);bad['configuration']['routes']['D03'][0]['query']='new unbound query'
        with self.assertRaisesRegex(ValueError,'identity changed'):a.identity(bad)

    def test_no_other_data_or_runtime_python_execution_and_no_mutation(self):
        self.add_markers();self.add_reports();write(self.base/'unrelated/driver.py',{'never':'execute'})
        before={p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('socket.socket',side_effect=AssertionError('network')):rows,hashes=a.collect(self.base,self.root)
        after={p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after);self.assertEqual(rows[0]['actual_new_mediawiki_intents'],57)
        self.assertFalse(any('unrelated' in p or p.endswith('.py') for p in hashes))
        p=next(self.out.glob('*-mediawiki.json'));p.unlink();p.symlink_to(self.base/'unrelated/driver.py')
        with self.assertRaisesRegex(ValueError,'symlinks'):self.row()
        self.assertEqual(a.collect(self.root/'absent',self.root),([],{}))


if __name__=='__main__':unittest.main()
