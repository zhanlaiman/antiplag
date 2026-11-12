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

    def http(self,path,body=None,cookie=None,origin='http://beta.example',host='beta.example'):
        connection=HTTPConnection('127.0.0.1',self.service.server_port,timeout=5)
        headers={'Host':host}
        if cookie:headers['Cookie']=cookie
        if origin:headers['Origin']=origin
        if body is not None:headers['Content-Type']='application/json'
        try:
            connection.request('POST' if body is not None else 'GET',path,None if body is None else json.dumps(body).encode(),headers)
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

    def test_two_daily_checks_and_duplicate_request_does_not_run_again(self):
        cookie,_=self.guest();self.check(cookie,'d'*32);self.check(cookie,'d'*32);self.check(cookie,'e'*32)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.http('/api/web-check-jobs',{'request_id':'f'*32,'text':TEXT,'internet_consent':True},cookie)[0],429)

    def test_invalid_cookie_creates_a_new_isolated_guest(self):
        cookie,_=self.guest();report=self.check(cookie,'1'*32)
        parts=SimpleCookie();parts.load(cookie);value=parts['istok_beta'].value
        forged='istok_beta='+value.split('.')[0]+'.'+'0'*64
        self.assertEqual(self.http('/api/reports/'+report,cookie=forged)[0],404)

    def test_failed_free_quota_preflight_sends_no_search(self):
        with patch('cloud.web_beta.build_opener',side_effect=TimeoutError),patch('cloud.web_beta.TavilySearch') as transport:
            with self.assertRaisesRegex(Exception,'бесплатную квоту'):self.service.beta.run_web(TEXT,'x')
            transport.assert_not_called()

    def test_verified_quota_uses_a_sixty_query_budget_and_existing_pipeline(self):
        account={'current_plan':'Researcher','plan_limit':1000,'plan_usage':615,'paygo_usage':0}
        opener=Mock();opener.open.return_value=BytesIO(json.dumps({'account':account}).encode())
        with patch('cloud.web_beta.build_opener',return_value=opener),patch('cloud.web_beta.TavilySearch') as transport, \
                patch('cloud.web_beta.web_search.run',return_value={'ok':True}) as run:
            self.assertEqual(self.service.beta.run_web(TEXT,'x'),{'ok':True})
            self.assertEqual(transport.call_args.args[0].limit,60)
            self.assertIn('wiki_searcher',run.call_args.kwargs)

    def test_secure_cookie_and_exact_origin_required(self):
        with self.assertRaises(ValueError):BetaRuntime(self.root/'invalid','http://public.example')
        runtime=BetaRuntime(self.root/'secure','https://public.example')
        try:
            _,cookie=runtime.identity({})
            self.assertIn('Secure',cookie);self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Lax',cookie)
        finally:runtime.tenants.close()


class BetaUploadTests(unittest.TestCase):
    def test_only_code_static_assets_and_two_public_source_packs_are_uploaded(self):
        names=[r['path'] for r in source_manifest()]
        self.assertTrue(all(not p.startswith(('.runtime/','data/private/','output/','tmp/')) for p in names))
        self.assertEqual(sorted(p for p in names if p.startswith('data/')),['data/source-packs/biology-ru-v1.json','data/source-packs/economics-ru-v1.json'])
        self.assertFalse(any(p.endswith(('.sqlite3','.toml')) for p in names))
