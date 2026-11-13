"""Authored temporary accounting files only; no real series or corpus reads."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plagiarism.saas import report_identifier
from scripts import query_format_accounting as accounting
from scripts import run_query_format_pairs as pilot


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def manifest():
    texts = {cid: ("Authored independent text for " + cid + ". ") * 30 for cid,_ in pilot.SPEC}
    original = {cid: [{"query": f'"Authored unchanged query {i} for {cid}"', "start": i*20, "end": i*20+19} for i in range(6)] for cid,_ in pilot.SPEC}
    jobs = {}
    for cid,index in pilot.SPEC:
        for fmt,values in (("quoted",original[cid]),("unquoted",pilot.remove_outer_quotes(original[cid]))):
            jobs[cid+"-"+fmt] = {"case_id":cid,"format":fmt,"base_query_index":index,
                "text_sha256":sha256(texts[cid].encode()).hexdigest(),"plan":[values[index]]}
    config = {"version":pilot.VERSION,"spec":[list(s) for s in pilot.SPEC],"formats":list(pilot.FORMATS),
        "max_provider_calls":6,"max_calls_per_job":1,"named_references":False,"default_changed":False,
        "original_dense_plans":original,"jobs":jobs}
    name=accounting.SERIES[0]
    fingerprint=sha256(json.dumps(config,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    rid=sha256((name+fingerprint).encode()).hexdigest()[:32]
    return {"id":rid,"run_name":name,"configuration":config,
        "request_ids":{key:sha256((rid+key+job['text_sha256']).encode()).hexdigest()[:32] for key,job in jobs.items()}},texts


class QueryFormatAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.base=self.root/'data/private/web-evaluation'
        self.directory=self.base/accounting.SERIES[0];self.run,self.texts=manifest()
        write(self.directory/'run.json',self.run)

    def add_calls(self, missing_usage=False):
        paths=[]
        for i,(identifier,identity) in enumerate(pilot.call_map(self.run).items()):
            record={**identity,'request_id':identifier,'state':'complete','search_depth':'basic'}
            if not (missing_usage and i==0):record['usage']={'credits':0 if i==1 else 1}
            path=self.directory/(identifier+'-search.json');write(path,record);paths.append(path)
        return paths

    def add_reports(self):
        for key,job in self.run['configuration']['jobs'].items():
            report={'id':report_identifier(self.run['request_ids'][key]),'text':self.texts[job['case_id']],
                'web':{'queries_attempted':1,'query_strategy':pilot.VERSION+':'+job['format'],
                    'diagnostic_query_plan':job,'query_spans':[{k:p[k] for k in ('start','end')} for p in job['plan']]}}
            write(self.directory/(key+'.json'),report)

    def test_complete_six_markers_reports_and_explicit_zero_are_accounted(self):
        self.add_calls();self.add_reports()
        write(self.directory/'quota-after.json',{'paygo_usage':0,'local_monthly_attempts':506})
        rows,hashes=accounting.collect(self.base,self.root);row=rows[0]
        self.assertEqual(row['query_attempts'],6);self.assertEqual(row['saved_documents'],6)
        self.assertEqual(row['diagnostic_unique_cases'],3);self.assertEqual(row['queries_in_completed_reports'],6)
        self.assertEqual(row['confirmed_provider_credits'],5);self.assertEqual(row['records_with_credit_usage'],6)
        self.assertTrue(row['provider_accounting_complete']);self.assertTrue(row['provider_credit_usage_complete'])
        self.assertEqual(row['total_basic_credits'],5);self.assertEqual(len(hashes),14)

    def test_reports_with_no_markers_are_unknown_not_zero_spend(self):
        self.add_reports();row=accounting.collect(self.base,self.root)[0][0]
        self.assertEqual(row['query_attempts'],0);self.assertEqual(row['queries_in_completed_reports'],6)
        self.assertEqual(row['minimum_attempts_consistent_with_reports'],6)
        self.assertEqual(row['provider_marker_records_missing'],6);self.assertEqual(row['provider_usage_unknown_calls'],6)
        self.assertFalse(row['provider_accounting_complete']);self.assertFalse(row['provider_credit_usage_complete'])
        self.assertEqual(row['confirmed_provider_credits'],0);self.assertIsNone(row['total_basic_credits'])

    def test_missing_usage_differs_from_explicit_zero(self):
        self.add_calls(missing_usage=True);self.add_reports();row=accounting.collect(self.base,self.root)[0][0]
        self.assertTrue(row['provider_accounting_complete']);self.assertFalse(row['provider_credit_usage_complete'])
        self.assertEqual(row['confirmed_provider_credits'],4);self.assertEqual(row['records_with_credit_usage'],5)
        self.assertEqual(row['records_without_credit_usage'],1);self.assertIsNone(row['total_basic_credits'])

    def test_equal_totals_cannot_hide_different_job_marker(self):
        path=self.add_calls()[0]
        marker_job=json.loads(path.read_text())['case_id']
        for other in self.directory.glob('*-search.json'):
            if other != path:other.unlink()
        self.add_reports()
        reported_job=next(key for key in self.run['configuration']['jobs'] if key != marker_job)
        for key in self.run['configuration']['jobs']:
            p=self.directory/(key+'.json');r=json.loads(p.read_text());r['web']['queries_attempted']=int(key==reported_job);write(p,r)
        row=accounting.collect(self.base,self.root)[0][0]
        self.assertEqual(row['query_attempts'],row['queries_in_completed_reports'])
        self.assertEqual({item['job'] for item in row['accounting_mismatches']},{marker_job,reported_job})
        self.assertFalse(row['provider_accounting_complete']);self.assertIsNone(row['total_basic_credits'])

    def test_wrong_query_case_index_call_id_and_depth_are_rejected(self):
        path=self.add_calls()[0];original=json.loads(path.read_text())
        for change in ({'query':'changed'},{'case_id':'D20-unquoted'},{'query_index':1},{'query_index':False},
                       {'request_id':'f'*64},{'search_depth':'advanced'}):
            with self.subTest(change=change):
                write(path,{**original,**change})
                with self.assertRaises(ValueError):accounting.collect(self.base,self.root)

    def test_invalid_usage_and_more_than_six_markers_are_rejected(self):
        path=self.add_calls()[0];original=json.loads(path.read_text())
        for credits in (True,-1,2,'1',0.5):
            with self.subTest(credits=credits):
                write(path,{**original,'usage':{'credits':credits}})
                with self.assertRaises(ValueError):accounting.collect(self.base,self.root)
        write(path,original);write(self.directory/('f'*64+'-search.json'),{})
        with self.assertRaisesRegex(ValueError,'More than six'):accounting.collect(self.base,self.root)

    def test_changed_report_id_text_plan_or_format_is_rejected(self):
        self.add_calls();self.add_reports();key=next(iter(self.run['configuration']['jobs']))
        path=self.directory/(key+'.json');original=json.loads(path.read_text())
        for field,value in (('id','f'*32),('text','wrong input'),('query_strategy','wrong'),('diagnostic_query_plan',{}),('query_spans',[])):
            r=deepcopy(original)
            if field in ('id','text'):r[field]=value
            else:r['web'][field]=value
            write(path,r)
            with self.subTest(field=field),self.assertRaises(ValueError):accounting.collect(self.base,self.root)

    def test_changed_frozen_run_or_quote_words_are_rejected(self):
        r=deepcopy(self.run);r['id']='f'*32;write(self.directory/'run.json',r)
        with self.assertRaisesRegex(ValueError,'stable request'):accounting.collect(self.base,self.root)
        r=deepcopy(self.run);r['configuration']['jobs']['D02-unquoted']['plan'][0]['query']='repaired different words';write(self.directory/'run.json',r)
        with self.assertRaisesRegex(ValueError,'changed words'):accounting.collect(self.base,self.root)

    def test_read_only_scope_ignores_pages_other_series_and_inputs(self):
        self.add_calls();self.add_reports()
        for relative in ('materials/final-70/key.json','query-unfamiliar-reserved-v2/RFC9309.txt',
                         'dev-query-format-pair-v2/run.json','dev-query-format-pair-v1/pages/D01-quoted/page.json'):
            p=self.base/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('not eligible JSON')
        before={p.relative_to(self.root).as_posix():p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(pilot,'prepare',side_effect=AssertionError('no prepare')),patch.object(pilot,'quota',side_effect=AssertionError('no quota')):
            rows,hashes=accounting.collect(self.base,self.root)
        after={p.relative_to(self.root).as_posix():p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after);self.assertEqual(rows[0]['query_attempts'],6)
        self.assertFalse(any('final-70' in p or 'unfamiliar' in p or '/pages/' in p for p in hashes))

    def test_symlink_and_absent_series_are_handled_without_other_reads(self):
        target=self.root/'ineligible.json';target.write_text('not eligible')
        self.add_calls();path=next(self.directory.glob('*-search.json'));path.unlink();path.symlink_to(target)
        with self.assertRaisesRegex(ValueError,'symlinks'):accounting.collect(self.base,self.root)
        self.assertEqual(accounting.collect(self.root/'absent',self.root),([],{}))

    def test_summary_includes_all_six_collectors_without_other_reads(self):
        from scripts import summarize_night_results as summary
        from scripts import unfamiliar_v2_accounting as first_use
        from scripts import short_paragraph_pilot_accounting as short_pilot
        from scripts import publication_pilot_accounting as publication
        from scripts import publication_query_delta_accounting as publication_delta
        metadata=[{'series':'metadata','query_attempts':18}]
        formats=[{'series':'format','query_attempts':6}]
        v2=[{'series':'first_use_v2','query_attempts':32}]
        short=[{'series':'short_paragraph','query_attempts':22}]
        publications=[{'series':'publication','query_attempts':30}]
        delta=[{'series':'publication_delta','query_attempts':1}]
        with patch.object(summary,'metadata_pilot_expenses',return_value=(metadata,{'metadata.json':'a'*64})) as a, \
             patch.object(accounting,'collect',return_value=(formats,{'format.json':'b'*64})) as b, \
             patch.object(first_use,'collect',return_value=(v2,{'v2.json':'c'*64})) as c, \
             patch.object(short_pilot,'collect',return_value=(short,{'short.json':'d'*64})) as d, \
             patch.object(publication,'collect',return_value=(publications,[],{'publication.json':'e'*64})) as e, \
             patch.object(publication_delta,'collect',return_value=(delta,{'delta.json':'f'*64})) as f:
            rows,hashes=summary.diagnostic_pilot_expenses(self.base,self.root)
        self.assertEqual(rows,metadata+formats+v2+short+publications+delta);self.assertEqual(sum(r['query_attempts'] for r in rows),109)
        self.assertEqual(hashes,{'metadata.json':'a'*64,'format.json':'b'*64,'v2.json':'c'*64,'short.json':'d'*64,'publication.json':'e'*64,'delta.json':'f'*64})
        a.assert_called_once_with(self.base,self.root);b.assert_called_once_with(self.base,self.root)
        c.assert_called_once_with(self.base,self.root)
        d.assert_called_once_with(self.base,self.root)
        e.assert_called_once_with(self.base,self.root)
        f.assert_called_once_with(self.base,self.root)


if __name__=='__main__':unittest.main()
