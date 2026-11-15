import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tests.test_web_beta as beta_tests
from plagiarism.matching import analyze
from plagiarism.query_sentences import SHORT_PURE_STRATEGY
from plagiarism import web_search
from plagiarism import access
import server


class PoolAPITests(unittest.TestCase):
    setUp=beta_tests.WebBetaTests.setUp
    tearDown=beta_tests.WebBetaTests.tearDown
    start=beta_tests.WebBetaTests.start
    stop=beta_tests.WebBetaTests.stop
    http=beta_tests.WebBetaTests.http
    guest=beta_tests.WebBetaTests.guest

    def await_job(self, identifier, cookie):
        for _ in range(100):
            job=self.http('/api/jobs/'+identifier,cookie=cookie)[1]['job']
            if job['status'] in ('complete','failed'):return job
            time.sleep(.01)
        self.fail('job did not finish')

    def body(self, identifier='a'*32):
        return {'request_id':identifier,'text':beta_tests.TEXT,'internet_consent':True,
                'search_provider':'auto','provider_consent_version':'search-pool-v1'}

    def test_auto_disabled_and_unknown_consent_never_queue_or_run(self):
        cookie,_=self.guest();runner=Mock();self.service.beta.runner=runner
        with patch.dict(os.environ,{'ISTOK_AUTOMATIC_SEARCH':'0'}):
            self.assertEqual(self.http('/api/web-check-jobs',self.body(),cookie)[0],503)
            self.assertFalse(self.http('/api/status',cookie=cookie)[1]['web']['automatic_search']['enabled'])
        with patch.dict(os.environ,{'ISTOK_AUTOMATIC_SEARCH':'1'}):
            for extra in ({'provider_consent_version':None},{'provider_consent_version':'unknown'},
                          {'provider_consent':'auto'},{'search_provider':'tavily'}):
                self.assertEqual(self.http('/api/web-check-jobs',{**self.body(),**extra},cookie)[0],400)
        runner.assert_not_called()
        self.assertEqual(self.http('/api/session',cookie=cookie)[1]['beta']['daily_used'],0)

    def test_exact_auto_fingerprint_replay_restart_and_legacy_conflict(self):
        cookie,_=self.guest();seen=[]
        def runner(text,rid,progress,provider='tavily'):
            seen.append(provider)
            return {**analyze(text,[]),'text':text,'sources':[],'scope':'web_search','web':{'provider':provider}}
        self.runner=self.service.beta.runner=runner
        with patch.dict(os.environ,{'ISTOK_AUTOMATIC_SEARCH':'1'}):
            body=self.body();self.assertEqual(self.http('/api/web-check-jobs',body,cookie)[0],200)
            job=self.await_job(body['request_id'],cookie)
            self.assertEqual(job['status'],'complete',job);self.assertEqual(seen,['auto'])
            self.stop();self.start()
            same=self.http('/api/web-check-jobs',body,cookie)
            self.assertEqual(same[0],200);self.assertEqual(same[1]['job']['result'],job['result']);self.assertEqual(seen,['auto'])
            with patch.dict(os.environ,{'ISTOK_AUTOMATIC_SEARCH':'0'}), \
                 patch('plagiarism.search_provider_policy.CONSENT_VERSION','search-pool-v2'):
                archived=self.http('/api/web-check-jobs',body,cookie)
                self.assertEqual(archived[0],200)
                self.assertEqual(archived[1]['job']['result'],job['result']);self.assertEqual(seen,['auto'])
            legacy={k:v for k,v in body.items() if k not in ('search_provider','provider_consent_version')}
            self.assertEqual(self.http('/api/web-check-jobs',legacy,cookie)[0],409)
            self.assertEqual(seen,['auto'])

    def test_native_automatic_pipeline_uses_v11_independent_of_legacy_default(self):
        search=Mock(provider='serper',skipped=[{'provider':'serpapi-yandex','reason':'confirmed_free_quota_exhausted'}],
                    last_usage={'credits':1})
        search.search.return_value=[]
        text=' '.join('botanical'+str(i)for i in range(180))
        with patch('plagiarism.search_router.select_search',return_value=search) as select, \
             patch('cloud.web_beta.MediaWikiSearch') as wiki, \
             patch.dict(os.environ,{'ISTOK_SEARCH_PROVIDER':'tavily','ISTOK_WEB_WIKI_FOLLOWUP':'0'}):
            result=self.service.beta.run_automatic(text,'b'*32)
        self.assertEqual(select.call_args.args[1],len(web_search.plan_queries(text,strategy=SHORT_PURE_STRATEGY)))
        self.assertEqual(select.call_args.kwargs['limit'],180)
        self.assertEqual(result['web']['query_strategy'],SHORT_PURE_STRATEGY)
        self.assertEqual(result['web']['provider'],'serper')
        self.assertEqual(result['web']['automatic_search']['selected_provider'],'serper')
        self.assertEqual(search.search.call_count,6)

    def test_beta_api_alias_preserves_auto_consent_and_fingerprint(self):
        cookie,guest=self.guest();seen=[]
        token=self.service.beta.api_keys.create(guest,'Synthetic API test',['checks:write','reports:read'],1)['token']
        def runner(text,rid,progress,provider='tavily'):
            seen.append(provider)
            return {**analyze(text,[]),'text':text,'sources':[],'scope':'web_search','web':{'provider':provider}}
        self.service.beta.runner=runner
        with patch.dict(os.environ,{'ISTOK_AUTOMATIC_SEARCH':'1'}):
            body=self.body('c'*32)
            self.assertEqual(self.http('/api/v1/checks',body,authorization='Bearer '+token,origin=None)[0],200)
            job=self.await_job(body['request_id'],cookie)
            self.assertEqual(job['status'],'complete',job);self.assertEqual(seen,['auto'])
            self.assertEqual(self.http('/api/v1/checks',{**body,'provider_consent_version':'unknown'},
                                      authorization='Bearer '+token,origin=None)[0],409)
            self.assertEqual(seen,['auto'])

    def test_generic_server_rejects_new_contract_before_transport_or_ledger(self):
        handler=SimpleNamespace(server=SimpleNamespace(saas=None),tenant=SimpleNamespace(store=Mock(),jobs=Mock()))
        for fields in ({'search_provider':'auto'},{'provider_consent_version':'search-pool-v1'}):
            with patch('server.web_search.run') as transport, self.assertRaises(access.AccessError) as error:
                server.Handler.route(handler,'POST','/api/web-check-jobs',self.body()|fields)
            self.assertEqual(error.exception.code,'automatic_search_disabled');transport.assert_not_called()
        self.assertEqual(handler.tenant.store.mock_calls,[])
        self.assertEqual(handler.tenant.jobs.mock_calls,[])


if __name__=='__main__':unittest.main()
