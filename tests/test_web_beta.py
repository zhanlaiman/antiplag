from http.client import HTTPConnection
from http.cookies import SimpleCookie
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from unittest.mock import Mock,patch

from cloud.web_beta import BetaRuntime,create_server
from cloud.modal_web import source_manifest
from plagiarism.matching import analyze
from plagiarism.saas import report_identifier

TEXT='В мастерской сотрудники подготовили длинный деревянный стол для нового опыта. Каждый участник получил собственный блокнот и записал порядок действий. После завершения работы команда сравнила записи и составила общий список наблюдений.'


class WebBetaTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory();self.root=Path(self.directory.name)
        self.environment=patch.dict(os.environ,{});self.environment.start();self.calls=[]
        def runner(text,rid,progress):
            self.calls.append(rid)
            return analyze(text,[{'id':'source','title':'Авторский контроль','url':'https://example.org/control','text':TEXT}])
        self.runner=runner;self.start()

    def start(self):
        self.service=create_server(self.root,'http://beta.example',host='127.0.0.1',port=0,allow_http=True,runner=self.runner)
        self.thread=Thread(target=self.service.serve_forever,daemon=True);self.thread.start()

    def stop(self):
        self.service.shutdown();self.service.server_close();self.service.beta.tenants.close();self.thread.join()

    def tearDown(self):
        self.stop();self.environment.stop();self.directory.cleanup()

    def http(self,path,body=None,cookie=None,origin='http://beta.example',host='beta.example',authorization=None,method=None):
        connection=HTTPConnection('127.0.0.1',self.service.server_port,timeout=5)
        headers={'Host':host}
        if cookie:headers['Cookie']=cookie
        if origin:headers['Origin']=origin
        if authorization:headers['Authorization']=authorization
        if body is not None:headers['Content-Type']='application/json'
        try:
            connection.request(method or ('POST' if body is not None else 'GET'),path,None if body is None else json.dumps(body).encode(),headers)
            response=connection.getresponse();raw=response.read()
            return response.status,json.loads(raw) if raw else {},response.getheader('Set-Cookie')
        finally:connection.close()

    def guest(self):
        status,result,cookie=self.http('/api/session');self.assertEqual(status,200)
        return cookie.split(';')[0],result['beta']['id']

    def check(self,cookie,identifier):
        status,result,_=self.http('/api/web-check-jobs',{'request_id':identifier,'text':TEXT,'internet_consent':True},cookie)
        self.assertEqual(status,200,result)
        for _ in range(100):
            status,result,_=self.http('/api/jobs/'+identifier,cookie=cookie)
            if result['job']['status'] in {'complete','failed'}:break
            time.sleep(.01)
        self.assertEqual(result['job']['status'],'complete',result)
        return report_identifier(identifier)

    def test_guests_cannot_read_each_others_reports_and_state_survives_restart(self):
        alice,_=self.guest();bob,_=self.guest();identifier='a'*32
        report=self.check(alice,identifier)
        status,before,_=self.http('/api/reports/'+report,cookie=alice);self.assertEqual(status,200)
        self.assertEqual(self.http('/api/reports/'+report,cookie=bob)[0],404)
        self.stop();self.start()
        status,after,_=self.http('/api/reports/'+report,cookie=alice)
        self.assertEqual(status,200);self.assertEqual(before,after)
        self.check(bob,identifier);self.assertEqual(len(set(self.calls)),2)

    def test_data_lifecycle_isolation_restart_export_and_idempotent_job(self):
        from zipfile import ZipFile
        alice,_=self.guest();bob,_=self.guest();identifier='a'*32
        report=self.check(alice,identifier)
        original=self.http('/api/reports/'+report,cookie=alice)[1]
        remaining=self.http('/api/session',cookie=alice)[1]['beta']['daily_remaining']
        self.assertEqual(self.http('/api/data',cookie=alice)[1]['totals']['reports'],1)
        self.assertEqual(self.http('/api/data',cookie=bob)[1]['totals']['reports'],0)
        trash='/api/data/report/'+report+'/trash';restore='/api/data/report/'+report+'/restore'
        self.assertEqual(self.http(trash,{},bob)[0],404)
        self.assertEqual(self.http(trash,{},alice,origin=None)[0],403)
        self.assertEqual(self.http(trash,{},alice)[0],200)
        self.assertEqual(self.http('/api/reports/'+report,cookie=alice)[0],404)
        self.assertEqual(self.http('/api/report-exports/'+report,cookie=alice)[0],404)
        self.assertEqual(self.http('/api/reports',cookie=alice)[1]['reports'],[])
        self.assertEqual(self.http('/api/jobs/'+identifier,cookie=alice)[1]['job']['result'],{'report_removed':True,'report_id':report})
        status,reused,_=self.http('/api/web-check-jobs',{'request_id':identifier,'text':TEXT,'internet_consent':True},alice)
        self.assertEqual(status,200);self.assertTrue(reused['job']['result']['report_removed'])
        self.assertEqual(len(self.calls),1)
        connection=HTTPConnection('127.0.0.1',self.service.server_port,timeout=5)
        connection.request('GET','/api/data-export',headers={'Host':'beta.example','Cookie':alice})
        response=connection.getresponse();self.assertEqual(response.status,200)
        self.assertEqual(response.getheader('Content-Type'),'application/zip')
        with ZipFile(BytesIO(response.read())) as archive:
            self.assertEqual(json.loads(archive.read('reports.jsonl'))['id'],report)
            self.assertEqual(json.loads(archive.read('trash.jsonl'))['id'],report)
        connection.close()
        self.stop();self.start()
        self.assertTrue(self.http('/api/jobs/'+identifier,cookie=alice)[1]['job']['result']['report_removed'])
        self.assertEqual(self.http(restore,{},bob)[0],404)
        self.assertEqual(self.http(restore,{},alice)[0],200)
        self.assertEqual(self.http('/api/reports/'+report,cookie=alice)[1],original)
        self.assertEqual(self.http('/api/session',cookie=alice)[1]['beta']['daily_remaining'],remaining)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.http('/api/owner/statistics',cookie=bob)[0],403)

    def test_cross_origin_missing_origin_and_wrong_host_mutations_are_rejected(self):
        cookie,_=self.guest();body={'request_id':'b'*32,'text':TEXT,'internet_consent':True}
        for origin,host in [('https://other.example','beta.example'),(None,'beta.example'),('http://beta.example','evil.example')]:
            self.assertEqual(self.http('/api/web-check-jobs',body,cookie,origin,host)[0],403)
        self.assertEqual(self.calls,[])

    def test_local_configuration_models_evaluation_and_auth_are_unavailable(self):
        cookie,_=self.guest()
        for path,body in [('/api/local-search-config',None),('/api/examples',None),('/api/model-jobs',{}),('/api/auth/request',{})]:
            self.assertEqual(self.http(path,body,cookie)[0],403,path)
        self.assertEqual(self.http('/setup-search.html',cookie=cookie)[0],403)

    def test_web_check_requires_consent_and_respects_text_limit(self):
        cookie,_=self.guest();body={'request_id':'c'*32,'text':TEXT}
        self.assertEqual(self.http('/api/web-check-jobs',body,cookie)[0],400)
        body.update(internet_consent=True,text='x'*10001)
        self.assertEqual(self.http('/api/web-check-jobs',body,cookie)[0],400);self.assertEqual(self.calls,[])

    def test_public_authored_demo_works_without_search_or_models(self):
        cookie,_=self.guest();status,result,_=self.http('/api/demo',{},cookie)
        self.assertEqual(status,200);self.assertTrue(result['text']);self.assertTrue(result['sources'])
        self.assertEqual(self.calls,[])

    def test_dictionary_and_proofreader_are_personal_and_do_not_spend_search_quota(self):
        alice,_=self.guest();bob,_=self.guest()
        before=self.http('/api/session',cookie=alice)[1]['beta']['daily_remaining']
        self.assertEqual(self.http('/api/dictionary',{'word':'Данияр','action':'add'},alice)[0],200)
        self.assertEqual(self.http('/api/dictionary',cookie=alice)[1]['words'],['данияр'])
        self.assertEqual(self.http('/api/dictionary',cookie=bob)[1]['words'],[])
        self.assertEqual(self.http('/api/dictionary',{'word':'Данияр','action':'add'},bob,origin='https://other.example')[0],403)
        result=self.http('/api/proofread',{'text':'Он хочет учится. Очень  очень хорошо.'},alice)
        self.assertEqual(result[0],200)
        self.assertTrue(any(issue['kind']=='grammar' for issue in result[1]['issues']))
        self.assertEqual(self.http('/api/proofread',{'text':'x'*10001},alice)[0],400)
        self.assertEqual(self.http('/api/session',cookie=alice)[1]['beta']['daily_remaining'],before)
        self.assertEqual(self.calls,[])
        self.stop();self.start()
        self.assertEqual(self.http('/api/dictionary',cookie=alice)[1]['words'],['данияр'])

    def test_six_daily_checks_and_duplicate_request_does_not_run_again(self):
        cookie,_=self.guest();self.check(cookie,'d'*32);self.check(cookie,'d'*32);self.check(cookie,'e'*32)
        self.assertEqual(len(self.calls),2)
        for ch in '1234':self.check(cookie,ch*32)
        self.assertEqual(len(self.calls),6)
        self.assertEqual(self.http('/api/session',cookie=cookie)[1]['beta']['daily_checks'],6)
        self.assertEqual(self.http('/api/web-check-jobs',{'request_id':'f'*32,'text':TEXT,'internet_consent':True},cookie)[0],429)

    def test_site_preview_requires_consent_and_durable_limits_without_search(self):
        alice,_=self.guest();before=self.http('/api/session',cookie=alice)[1]['beta']['daily_remaining']
        body={'request_id':'c'*32,'url':'https://example.org/','site_consent':True,'limit':3}
        result={'pages':[{'title':'Открытая страница','text':TEXT,'url':'https://example.org/'}],'errors':[]}
        with patch('cloud.web_beta.fetch_site',return_value=result) as loader:
            self.assertEqual(self.http('/api/import-site',{**body,'site_consent':False},alice)[0],400)
            self.assertEqual(self.http('/api/import-site',{**body,'url':'http://127.0.0.1/'},alice)[0],400)
            loader.assert_not_called()
            self.assertEqual(self.http('/api/import-site',body,alice)[1],result)
            self.assertEqual(self.http('/api/import-site',body,alice)[0],409)
            self.assertEqual(self.http('/api/import-site',{**body,'request_id':'d'*32},alice)[0],200)
            self.stop();self.start()
            self.assertEqual(self.http('/api/import-site',{**body,'request_id':'e'*32},alice)[0],429)
            self.assertEqual(loader.call_count,2)
        self.assertEqual(self.http('/api/session',cookie=alice)[1]['beta']['daily_remaining'],before)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.http('/api/import-site',body,alice,origin=None)[0],403)

    def test_library_batch_jobs_keep_separate_reports_replay_and_budget_after_restart(self):
        alice,_=self.guest();bob,_=self.guest()
        before=self.http('/api/session',cookie=alice)[1]['beta']['daily_remaining']
        reports=[]
        for index in range(3):
            identifier=f'{index+10:032x}';body={'request_id':identifier,'text':TEXT+str(index)}
            self.assertEqual(self.http('/api/library-check-jobs',body,alice)[0],200)
            for _ in range(100):
                job=self.http('/api/jobs/'+identifier,cookie=alice)[1]['job']
                if job['status'] in {'complete','failed'}:break
                time.sleep(.01)
            self.assertEqual(job['status'],'complete',job)
            reports.append((body,job['result']['report']))
            self.assertEqual(self.http('/api/jobs/'+identifier,cookie=bob)[0],404)
            self.assertEqual(self.http('/api/library-check-jobs',{**body,'text':'Изменённый текст'},alice)[0],400)
        self.stop();self.start()
        for body,report in reports:
            replay=self.http('/api/library-check-jobs',body,alice)
            self.assertEqual(replay[0],200)
            self.assertEqual(replay[1]['job']['result']['report'],report)
            self.assertEqual(self.http('/api/reports/'+report['id'],cookie=alice)[1]['report'],report)
        self.assertEqual(len({report['id'] for _,report in reports}),3)
        self.assertEqual(self.http('/api/session',cookie=alice)[1]['beta']['daily_remaining'],before)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.http('/api/library-check-jobs',{'request_id':'f'*32,'text':'x'*10001},alice)[0],400)
        self.assertEqual(self.http('/api/library-check-jobs',{'request_id':'f'*32,'text':TEXT},alice,origin=None)[0],403)

    def test_optional_correction_is_isolated_replayed_exported_and_restorable_without_search(self):
        from hashlib import sha256
        from plagiarism.ai_proofreading import parse_response
        from plagiarism.correction_jobs import internal_id
        from zipfile import ZipFile
        calls=[]
        def corrector(text,store,identifier,progress):
            calls.append(identifier)
            return parse_response(text,{'done':True,'message':{'content':json.dumps({'edits':[{'original':'учится','replacement':'учиться','reason':'Тестовое объяснение.','kind':'grammar'}]})}})
        self.service.beta.corrector=corrector
        alice,guest=self.guest();bob,_=self.guest();identifier='6'*32
        body={'request_id':identifier,'text':'🙂 Он хочет учится.','ai_consent':True}
        before=self.http('/api/session',cookie=alice)[1]['beta']['daily_remaining']
        self.assertEqual(self.http('/api/correction-jobs',{**body,'ai_consent':False},alice)[0],400)
        self.assertEqual(self.http('/api/correction-jobs',{**body,'text':'x'*3001},alice)[0],400)
        self.assertEqual(self.http('/api/correction-jobs',body,alice,origin=None)[0],403)
        self.assertEqual(self.http('/api/correction-quota',cookie=alice)[1]['monthly_remaining'],10)
        self.assertEqual(self.http('/api/correction-jobs',body,alice)[0],200)
        for _ in range(100):
            job=self.http('/api/correction-jobs/'+identifier,cookie=alice)[1]['job']
            if job['status'] in {'complete','failed'}:break
            time.sleep(.01)
        self.assertEqual(job['status'],'complete',job);self.assertEqual(job['id'],identifier)
        record=job['result']['correction'];path='/api/corrections/'+record['id']
        self.assertEqual(self.http(path,cookie=alice)[1]['correction'],record)
        connection=HTTPConnection('127.0.0.1',self.service.server_port,timeout=5)
        connection.request('GET',path,headers={'Host':'beta.example','Cookie':alice})
        response=connection.getresponse()
        self.assertEqual(response.getheader('Content-Disposition'),f'attachment; filename="istok-correction-{record["id"]}.json"')
        self.assertEqual(json.loads(response.read())['correction'],record)
        connection.close()
        self.assertEqual(self.http(path,cookie=bob)[0],404)
        self.assertEqual(self.http('/api/correction-jobs/'+identifier,cookie=bob)[0],404)
        self.assertEqual(self.http('/api/jobs/'+internal_id(guest,identifier),cookie=alice)[0],404)
        self.assertEqual(self.http('/api/session',cookie=alice)[1]['beta']['daily_remaining'],before)
        self.assertEqual(self.http('/api/data',cookie=alice)[1]['corrections'][0]['id'],record['id'])
        trash='/api/data/correction/'+record['id']+'/trash';restore=trash.replace('/trash','/restore')
        self.assertEqual(self.http(trash,{},bob)[0],404)
        self.assertEqual(self.http(trash,{},alice)[0],200)
        self.assertEqual(self.http(path,cookie=alice)[0],404)
        self.assertTrue(self.http('/api/correction-jobs/'+identifier,cookie=alice)[1]['job']['result']['correction_removed'])
        self.assertTrue(self.http('/api/correction-jobs',body,alice)[1]['job']['result']['correction_removed'])
        connection=HTTPConnection('127.0.0.1',self.service.server_port,timeout=5)
        connection.request('GET','/api/data-export',headers={'Host':'beta.example','Cookie':alice})
        response=connection.getresponse();self.assertEqual(response.status,200)
        with ZipFile(BytesIO(response.read())) as archive:self.assertEqual(json.loads(archive.read('corrections.jsonl')),record)
        connection.close()
        remaining=self.http('/api/correction-quota',cookie=alice)[1]
        self.stop();self.start();self.service.beta.corrector=corrector
        self.assertEqual(self.http(restore,{},alice)[0],200)
        self.assertEqual(self.http('/api/correction-jobs',body,alice)[1]['job']['result']['correction'],record)
        self.assertEqual(self.http('/api/correction-quota',cookie=alice)[1],remaining)
        self.assertEqual(len(calls),1);self.assertEqual(self.calls,[])

    def test_uncertain_correction_failure_never_restarts_or_refunds_itself(self):
        calls=[]
        def fail(text,store,identifier,progress):calls.append(identifier);raise TimeoutError('Synthetic uncertain timeout')
        self.service.beta.corrector=fail
        alice,_=self.guest();identifier='5'*32
        body={'request_id':identifier,'text':'Авторский текст.','ai_consent':True}
        self.assertEqual(self.http('/api/correction-jobs',body,alice)[0],200)
        for _ in range(100):
            job=self.http('/api/correction-jobs/'+identifier,cookie=alice)[1]['job']
            if job['status']=='failed':break
            time.sleep(.01)
        self.assertEqual(job['status'],'failed')
        before=self.http('/api/correction-quota',cookie=alice)[1]
        self.stop();self.start();self.service.beta.corrector=fail
        self.assertEqual(self.http('/api/correction-jobs',body,alice)[1]['job']['status'],'failed')
        self.assertEqual(self.http('/api/correction-quota',cookie=alice)[1],before)
        self.assertEqual(len(calls),1)

    def test_owner_password_stable_workspace_and_no_guest_daily_limit(self):
        from hashlib import pbkdf2_hmac
        salt='12'*32
        with patch.dict(os.environ,{'ISTOK_OWNER_USERNAME':'owner','ISTOK_OWNER_SALT':salt,
                'ISTOK_OWNER_DIGEST':pbkdf2_hmac('sha256',b'test-password',bytes.fromhex(salt),600000).hex()}):
            guest,identifier=self.guest()
            self.assertEqual(self.http('/api/owner/login',{'username':'owner','password':'wrong'},guest)[0],401)
            status,_,cookie=self.http('/api/owner/login',{'username':'owner','password':'test-password'},guest)
            self.assertEqual(status,200);owner=cookie.split(';')[0]
            self.assertIn('HttpOnly',cookie)
            session=self.http('/api/session',cookie=owner)[1]['beta']
            self.assertTrue(session['owner']);self.assertEqual(session['id'],identifier)
            statistics=self.http('/api/owner/statistics',cookie=owner)
            self.assertEqual(statistics[0],200)
            self.assertEqual(statistics[1]['counts']['spaces'],1)
            self.assertNotIn(identifier,json.dumps(statistics[1]))
            for ch in '1234567':self.check(owner,ch*32)
            other,_=self.guest()
            status,_,again=self.http('/api/owner/login',{'username':'owner','password':'test-password'},other)
            self.assertEqual(status,200)
            self.assertEqual(self.http('/api/session',cookie=again.split(';')[0])[1]['beta']['id'],identifier)
            forged=owner[:-1]+('0' if owner[-1]!='0' else '1')
            self.assertFalse(self.http('/api/session',cookie=forged)[1]['beta']['owner'])
            self.assertEqual(self.http('/api/owner/login',{'username':'owner','password':'test-password'},guest,origin='https://evil.example')[0],403)
            status,_,cleared=self.http('/api/owner/logout',{},owner)
            self.assertEqual(status,200);self.assertIn('Max-Age=0',cleared)

    def test_invalid_cookie_creates_a_new_isolated_guest(self):
        cookie,_=self.guest();report=self.check(cookie,'1'*32)
        parts=SimpleCookie();parts.load(cookie);value=parts['istok_beta'].value
        forged='istok_beta='+value.split('.')[0]+'.'+'0'*64
        self.assertEqual(self.http('/api/reports/'+report,cookie=forged)[0],404)

    def test_owner_api_key_scope_isolation_idempotency_restart_and_revocation(self):
        from hashlib import pbkdf2_hmac
        salt='34'*32
        with patch.dict(os.environ,{'ISTOK_OWNER_USERNAME':'owner','ISTOK_OWNER_SALT':salt,
                'ISTOK_OWNER_DIGEST':pbkdf2_hmac('sha256',b'test-password',bytes.fromhex(salt),600000).hex()}):
            guest,_=self.guest();other,_=self.guest()
            self.assertEqual(self.http('/api/api-keys',cookie=guest)[0],403)
            _,_,cookie=self.http('/api/owner/login',{'username':'owner','password':'test-password'},guest)
            owner=cookie.split(';')[0]
            fields={'label':'Read-only fixture','scopes':['reports:read','quota:read']}
            self.assertEqual(self.http('/api/api-keys',fields,owner,origin=None)[0],403)
            status,result,_=self.http('/api/api-keys',fields,owner)
            self.assertEqual(status,200,result);read='Bearer '+result['token'];key_id=result['key']['id']
            listing=self.http('/api/api-keys',cookie=owner)[1]
            self.assertNotIn(result['token'],json.dumps(listing));self.assertNotIn('digest',json.dumps(listing))
            report=self.check(owner,'8'*32)
            self.assertEqual(self.http('/api/v1/reports/'+report,origin=None,authorization=read)[0],200)
            self.assertEqual(self.http('/api/v1/reports',cookie=owner,origin=None)[0],401)
            self.assertEqual(self.http('/api/v1/reports',origin='https://evil.example',authorization=read)[0],403)
            self.assertEqual(self.http('/api/v1/reports',host='evil.example',origin=None,authorization=read)[0],403)
            self.assertEqual(self.http('/api/v1/checks',{'request_id':'9'*32,'text':TEXT,'internet_consent':True},origin=None,authorization=read)[0],403)
            other_report=self.check(other,'7'*32)
            self.assertEqual(self.http('/api/v1/reports/'+other_report,origin=None,authorization=read)[0],404)
            self.assertEqual(self.http('/api/v1/owner/statistics',origin=None,authorization=read)[0],404)
            self.assertEqual(self.http('/api/api-keys',cookie=other,authorization=read)[0],403)
            fields={'label':'Write fixture','scopes':['checks:write','reports:read','quota:read']}
            status,write_key,_=self.http('/api/api-keys',fields,owner);self.assertEqual(status,200)
            write='Bearer '+write_key['token'];request_id='9'*32
            body={'request_id':request_id,'text':TEXT,'internet_consent':True}
            self.assertEqual(self.http('/api/v1/checks',{**body,'internet_consent':False},origin=None,authorization=write)[0],400)
            self.assertEqual(self.http('/api/v1/checks',body,origin=None,authorization=write)[0],200)
            for _ in range(100):
                job=self.http('/api/v1/jobs/'+request_id,origin=None,authorization=write)[1]['job']
                if job['status'] in {'complete','failed'}:break
                time.sleep(.01)
            self.assertEqual(job['status'],'complete',job)
            before=self.http('/api/v1/quota',origin=None,authorization=write)[1]
            self.stop();self.start()
            reused=self.http('/api/v1/checks',body,origin=None,authorization=write)
            self.assertEqual(reused[0],200);self.assertEqual(reused[1]['job']['status'],'complete')
            self.assertEqual(self.http('/api/v1/quota',origin=None,authorization=write)[1],before)
            self.assertEqual(len(self.calls),3)
            self.assertEqual(self.http('/api/api-keys/'+key_id+'/revoke',{},other)[0],403)
            self.assertEqual(self.http('/api/api-keys/'+key_id+'/revoke',{},owner)[0],200)
            self.assertEqual(self.http('/api/v1/reports',origin=None,authorization=read)[0],401)
            self.assertEqual(self.http('/api/v1/reports',origin=None,authorization=write)[0],200)

    def test_failed_free_quota_preflight_sends_no_search(self):
        with patch('cloud.web_beta.build_opener',side_effect=TimeoutError),patch('cloud.web_beta.TavilySearch') as transport:
            with self.assertRaisesRegex(Exception,'бесплатную квоту'):self.service.beta.run_web(TEXT,'x')
            transport.assert_not_called()

    def test_verified_quota_uses_a_180_query_budget_and_existing_pipeline(self):
        account={'current_plan':'Researcher','plan_limit':1000,'plan_usage':615,'paygo_usage':0}
        opener=Mock();opener.open.return_value=BytesIO(json.dumps({'account':account}).encode())
        with patch('cloud.web_beta.build_opener',return_value=opener),patch('cloud.web_beta.TavilySearch') as transport, \
                patch('cloud.web_beta.web_search.run',return_value={'ok':True}) as run:
            self.assertEqual(self.service.beta.run_web(TEXT,'x'),{'ok':True})
            self.assertEqual(transport.call_args.args[0].limit,180)
            self.assertIn('wiki_searcher',run.call_args.kwargs)

    def test_quota_timeout_retries_only_readonly_preflight(self):
        account={'current_plan':'Researcher','plan_limit':1000,'plan_usage':615,'paygo_usage':0}
        opener=Mock();opener.open.side_effect=[TimeoutError(),BytesIO(json.dumps({'account':account}).encode())]
        with patch('cloud.web_beta.build_opener',return_value=opener),patch('cloud.web_beta.TavilySearch'), \
                patch('cloud.web_beta.web_search.run',return_value={'ok':True}) as run:
            self.assertEqual(self.service.beta.run_web(TEXT,'x'),{'ok':True})
            self.assertEqual(opener.open.call_count,2);run.assert_called_once()
        opener.open.side_effect=TimeoutError()
        with patch('cloud.web_beta.build_opener',return_value=opener),patch('cloud.web_beta.TavilySearch') as transport:
            with self.assertRaises(Exception):self.service.beta.run_web(TEXT,'x')
            transport.assert_not_called()

    def test_secure_cookie_and_exact_origin_required(self):
        with self.assertRaises(ValueError):BetaRuntime(self.root/'invalid','http://public.example')
        runtime=BetaRuntime(self.root/'secure','https://public.example')
        try:
            _,cookie=runtime.identity({})
            self.assertIn('Secure',cookie);self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Lax',cookie)
        finally:runtime.tenants.close()

    # Exercise the real HTTP/job persistence path, including retained identities.
    def outcome(self,cookie,identifier):
        status,result,_=self.http('/api/web-check-jobs',{'request_id':identifier,'text':TEXT,'internet_consent':True},cookie)
        self.assertEqual(status,200,result)
        for _ in range(100):
            result=self.http('/api/jobs/'+identifier,cookie=cookie)[1]
            if result['job']['status'] in {'complete','failed'}:return result['job']
            time.sleep(.01)
        self.fail('Job did not settle')

    def test_preflight_failure_refunds_only_known_zero_and_duplicate_stays_failed_after_restart(self):
        from cloud.web_beta import SearchNotStarted
        cookie,guest=self.guest();calls=[]
        def blocked(*args):
            calls.append(args[1]);raise SearchNotStarted('No search','free_quota_unconfirmed')
        self.service.beta.runner=blocked
        self.assertEqual(self.outcome(cookie,'a'*32)['status'],'failed')
        self.assertEqual(self.service.beta.remaining(guest)['daily_remaining'],6)
        self.service.beta.runner=self.runner
        self.assertEqual(self.outcome(cookie,'a'*32)['status'],'failed');self.assertEqual(len(calls),1)
        self.stop();self.start()
        self.assertEqual(self.outcome(cookie,'a'*32)['status'],'failed');self.assertEqual(self.calls,[])
        self.check(cookie,'b'*32)
        self.assertEqual(self.service.beta.remaining(guest)['daily_remaining'],5)

    def test_unknown_provider_failure_keeps_slot_even_with_same_public_error_code(self):
        from plagiarism.web_search import WebSearchError
        cookie,guest=self.guest()
        def uncertain(*args):raise WebSearchError('Unknown outcome','free_quota_unconfirmed')
        self.service.beta.runner=uncertain
        self.assertEqual(self.outcome(cookie,'c'*32)['status'],'failed')
        self.assertEqual(self.service.beta.remaining(guest)['daily_remaining'],5)

    def test_concurrent_admission_cannot_overbook_and_refund_keeps_fingerprint(self):
        from concurrent.futures import ThreadPoolExecutor
        from plagiarism.access import AccessError
        _,guest=self.guest();runtime=self.service.beta
        def admit(i):
            try:runtime.admit_check(guest,format(i,'032x'),TEXT);return True
            except AccessError:return False
        with ThreadPoolExecutor(max_workers=10) as pool:accepted=list(pool.map(admit,range(10)))
        self.assertEqual(sum(accepted),6)
        # Scheduling order is arbitrary: refund an actually admitted request.
        refunded=format(accepted.index(True),'032x')
        runtime.release_unstarted_check(guest,refunded)
        with self.assertRaises(AccessError):runtime.admit_check(guest,refunded,'other text')
        self.assertTrue(admit(10));self.assertEqual(runtime.remaining(guest)['daily_remaining'],0)

    def test_paid_or_exhausted_quota_never_constructs_search_transport(self):
        from cloud.web_beta import SearchNotStarted
        for usage,paid in [(899,0),(650,1),(1000,0)]:
            account={'current_plan':'Researcher','plan_limit':1000,'plan_usage':usage,'paygo_usage':paid}
            opener=Mock();opener.open.return_value=BytesIO(json.dumps({'account':account}).encode())
            with patch('cloud.web_beta.build_opener',return_value=opener),patch('cloud.web_beta.TavilySearch') as transport:
                with self.assertRaises(SearchNotStarted):self.service.beta.run_web(TEXT,'x')
                transport.assert_not_called()

    def test_rejected_queue_returns_slot_and_same_id_can_be_admitted_later(self):
        cookie,guest=self.guest()
        with self.service.beta.tenants.context(guest) as context:
            with patch.object(context.jobs.coordinator,'reserve',side_effect=ValueError('Queue full')):
                status,_,_=self.http('/api/web-check-jobs',{'request_id':'9'*32,'text':TEXT,'internet_consent':True},cookie)
                self.assertEqual(status,400)
        self.assertEqual(self.service.beta.remaining(guest)['daily_remaining'],6)
        self.check(cookie,'9'*32)
        self.assertEqual(self.service.beta.remaining(guest)['daily_remaining'],5)

    def test_source_delete_is_scoped_and_cannot_delete_another_guests_data(self):
        alice,_=self.guest();bob,_=self.guest()
        status,result,_=self.http('/api/sources',{'title':'Disposable synthetic','text':TEXT},alice)
        self.assertEqual(status,200);identifier=result['source']['id']
        def delete(cookie):
            c=HTTPConnection('127.0.0.1',self.service.server_port,timeout=5)
            try:
                c.request('DELETE','/api/sources/'+identifier,headers={'Host':'beta.example','Origin':'http://beta.example','Cookie':cookie})
                r=c.getresponse();r.read();return r.status
            finally:c.close()
        self.assertEqual(delete(bob),404)
        self.assertEqual(delete(alice),200)
        self.assertEqual(delete(alice),404)

    def test_quota_429_is_not_retried_and_persisted_cooldown_survives_restart(self):
        from urllib.error import HTTPError
        from cloud.web_beta import SearchNotStarted
        opener=Mock();opener.open.side_effect=HTTPError('https://api.tavily.com/usage',429,'Too many',{'Retry-After':'900'},None)
        with patch('cloud.web_beta.time.time',return_value=1000),patch('cloud.web_beta.build_opener',return_value=opener),patch('cloud.web_beta.TavilySearch') as transport:
            with self.assertRaises(SearchNotStarted) as caught:self.service.beta.run_web(TEXT,'x')
            self.assertEqual(caught.exception.code,'free_quota_rate_limited');self.assertEqual(opener.open.call_count,1)
            self.assertGreaterEqual(self.service.beta.quota_cooldown(),900);transport.assert_not_called()
        self.stop();self.start()
        with patch('cloud.web_beta.time.time',return_value=1100),patch('cloud.web_beta.build_opener') as network:
            with self.assertRaises(SearchNotStarted):self.service.beta.run_web(TEXT,'y')
            network.assert_not_called()

    def test_lost_http_response_recovers_by_get_without_duplicate_runner(self):
        import socket
        cookie,_=self.guest();identifier='7'*32
        data=json.dumps({'request_id':identifier,'text':TEXT,'internet_consent':True}).encode()
        request=(f'POST /api/web-check-jobs HTTP/1.1\r\nHost: beta.example\r\nOrigin: http://beta.example\r\nCookie: {cookie}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n').encode()+data
        with socket.create_connection(('127.0.0.1',self.service.server_port),timeout=5) as connection:
            connection.sendall(request)
        for _ in range(100):
            status,result,_=self.http('/api/jobs/'+identifier,cookie=cookie)
            if status==200 and result['job']['status']=='complete':break
            time.sleep(.01)
        self.assertEqual(result['job']['status'],'complete');self.check(cookie,identifier)
        self.assertEqual(len(self.calls),1)

    def test_previous_release_can_still_insert_checks_after_new_release_refund(self):
        cookie,guest=self.guest();self.service.beta.admit_check(guest,'a'*32,TEXT)
        self.service.beta.release_unstarted_check(guest,'a'*32)
        with self.service.beta.connect() as db:
            db.execute('INSERT INTO checks VALUES (?,?,?,?)',(guest,'b'*32,'old-fingerprint','2026-11-13'));db.commit()
        with self.service.beta.connect() as db:self.assertEqual(len(db.execute('PRAGMA table_info(checks)').fetchall()),4)

    def test_document_import_is_scoped_local_processing_without_search_or_daily_charge(self):
        from tests.test_document_import import docx,encoded
        cookie,guest=self.guest()
        status,result,_=self.http('/api/import-document',{'name':'work.docx','data':encoded(docx())},cookie)
        self.assertEqual(status,200,result);self.assertIn('Первый абзац.',result['text'])
        self.assertEqual(self.calls,[]);self.assertEqual(self.service.beta.remaining(guest)['daily_remaining'],6)
        self.assertFalse((self.root/'search-budget.sqlite3').exists())
        self.assertEqual(self.http('/api/import-document',{'name':'work.docx','data':'invalid'},cookie)[0],400)
        self.assertEqual(self.http('/api/import-document',{'name':'work.docx','data':encoded(docx())},cookie,origin='https://evil.example')[0],403)


class BetaUploadTests(unittest.TestCase):
    def test_only_code_static_assets_and_two_public_source_packs_are_uploaded(self):
        names=[r['path'] for r in source_manifest()]
        self.assertTrue(all(not p.startswith(('.runtime/','data/private/','output/','tmp/')) for p in names))
        self.assertEqual(sorted(p for p in names if p.startswith('data/')),sorted([
            'data/source-packs/biology-ru-v1.json','data/source-packs/economics-ru-v1.json',
            *('data/dictionaries/ru_RU/'+name for name in ('ru_RU.aff','ru_RU.dic','README_ru_RU.txt','manifest.json'))]))
        self.assertFalse(any(p.endswith(('.sqlite3','.toml')) for p in names))
