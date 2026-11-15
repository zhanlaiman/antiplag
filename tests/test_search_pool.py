import io
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import time
import unittest
from unittest.mock import Mock, patch

from plagiarism.search_pool import PoolBudget, checked_grant, fingerprint
from plagiarism.search_provider_policy import FreeQuotaExhausted, request_fields, contract
from plagiarism.search_router import select_search, confirm_tavily, SelectedSearch
from plagiarism.search_serpapi import free_quota
from plagiarism.web_search import WebSearchError, run
from plagiarism import access
from tests.test_search_serpapi import account

NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)
KEY = 'synthetic-key-1234567890'


def grant(provider='serper', cap=20):
    return {'provider':provider,'credential_fingerprint':fingerprint(KEY),'approved':True,
            'cash_spend_limit_usd':0,'max_unit_cost_usd':0.004 if provider=='exa' else 0,
            'cap':cap,'expires_at':time.time()+86400,'grant_id':'grant-one','account_ref':'same-account',
            'proof_sha256':'a'*64}


class PoolTests(unittest.TestCase):
    def setUp(self):
        tmp=TemporaryDirectory();self.addCleanup(tmp.cleanup);self.path=Path(tmp.name)/'budget.sqlite3'

    def budget(self, **kwargs):
        return PoolBudget(self.path,provider='serper',resource='grant:same-account:one',
                          credential_fingerprint=fingerprint(KEY),now=lambda:NOW,**kwargs)

    def test_shared_177_of_180_cannot_admit_six_or_touch_provider(self):
        b=self.budget(limit=180)
        with b.connect() as db:
            db.executemany("INSERT INTO calls VALUES(?,'2026-11','complete')",[(str(i),) for i in range(177)])
        factory=Mock()
        with self.assertRaises(WebSearchError) as error:
            select_search(self.path,6,factories={'serpapi-yandex':factory},keys={})
        self.assertEqual(error.exception.code,'search_budget_exhausted');factory.assert_not_called()
        with b.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],177)

    def test_attempt_and_metadata_atomic_survive_restart_and_duplicate(self):
        b=self.budget(grant=grant(cap=2));b.reserve('one')
        b=self.budget(grant=b.grant)
        with self.assertRaises(WebSearchError):b.reserve('one')
        b.reserve('two')
        with self.assertRaises(FreeQuotaExhausted):b.reserve('three')
        with b.connect() as db:
            self.assertEqual(db.execute('SELECT id,state FROM calls ORDER BY id').fetchall(),[('one','started'),('two','started')])
            self.assertEqual(db.execute('SELECT count(*) FROM pool_calls').fetchone()[0],2)

    def test_key_rotation_and_month_change_do_not_reset_grant(self):
        g=grant(cap=1);g['expires_at']=time.time()+86400*90
        b=self.budget(grant=g);b.reserve('one')
        next_month=datetime(2026,12,1,tzinfo=timezone.utc)
        g['expires_at']+=1
        # Changing the grant's expiry/policy is refused rather than replacing history.
        with self.assertRaises(WebSearchError):self.budget(grant=g)
        old=b.grant
        rotated=PoolBudget(self.path,provider='serper',resource=b.resource,credential_fingerprint='b'*64,
                           grant=old,now=lambda:next_month)
        with self.assertRaises(FreeQuotaExhausted):rotated.reserve('new-key-new-month')

    def test_concurrent_last_call_commits_only_one_complete_pair(self):
        g=grant(cap=1);self.budget(grant=g)
        def attempt(identifier):
            try:self.budget(grant=g).reserve(identifier);return 'sent'
            except FreeQuotaExhausted:return 'closed'
        with ThreadPoolExecutor(max_workers=4) as executor:
            results=list(executor.map(attempt,['a','b','c','d']))
        self.assertEqual(results.count('sent'),1)
        with self.budget(grant=g).connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM calls').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM pool_calls').fetchone()[0],1)

    def test_concurrent_conflicting_grant_policies_never_both_activate(self):
        start=Barrier(2);base=grant()
        def register(cap):
            start.wait()
            try:return self.budget(grant={**base,'cap':cap})
            except WebSearchError:return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(register,[2,20]))
        approved=[item for item in results if item is not None]
        self.assertEqual(len(approved),1)
        with approved[0].connect() as db:
            stored=json.loads(db.execute('SELECT policy FROM pool_grants').fetchone()[0])
        self.assertEqual(approved[0].grant['cap'],stored['cap'])

    def test_persist_failure_blocks_transport_but_retains_attempt(self):
        b=self.budget(persist=Mock(side_effect=OSError('durability failed')))
        with self.assertRaises(OSError):b.reserve('uncertain')
        with b.connect() as db:self.assertEqual(db.execute('SELECT state FROM calls').fetchone()[0],'started')
        with self.assertRaises(WebSearchError):self.budget().reserve('uncertain')

    def test_invalid_missing_expired_or_paid_grant_is_not_exhaustion(self):
        for changes in ({'approved':False},{'cash_spend_limit_usd':1},{'cash_spend_limit_usd':False},
                        {'credential_fingerprint':'f'*64},{'expires_at':0},{'cap':True},
                        {'proof_sha256':'not-proof'},{'max_unit_cost_usd':1}):
            with self.subTest(changes=changes),self.assertRaises(WebSearchError) as error:
                checked_grant({**grant(),**changes},'serper',KEY)
            self.assertNotIsInstance(error.exception,FreeQuotaExhausted)

    def test_only_verified_counter_shortage_is_typed_exhaustion(self):
        with self.assertRaises(FreeQuotaExhausted):free_quota(account(plan_searches_left=2,total_searches_left=2,this_month_usage=248),6)
        for value in (account(plan_name='Paid'),account(total_searches_left=2,plan_searches_left=2,this_month_usage=0)):
            with self.assertRaises(WebSearchError) as error:free_quota(value,6)
            self.assertNotIsInstance(error.exception,FreeQuotaExhausted)

    def test_tavily_confirmed_guard_exhaustion_differs_from_unknown_paid_or_429(self):
        opener=Mock()
        opener.open.return_value=io.BytesIO(json.dumps({'account':{'current_plan':'Researcher','plan_limit':1000,
                                    'plan_usage':899,'paygo_usage':0,'paygo_limit':None},'key':{'usage':899,'limit':None}}).encode())
        with self.assertRaises(FreeQuotaExhausted):confirm_tavily(KEY,6,opener)
        for value in ({'current_plan':'Paid','plan_limit':1000,'plan_usage':899,'paygo_usage':0},
                      {'current_plan':'Researcher','plan_limit':1000,'plan_usage':1,'paygo_usage':1}):
            opener.open.return_value=io.BytesIO(json.dumps({'account':value}).encode())
            with self.assertRaises(WebSearchError) as error:confirm_tavily(KEY,6,opener)
            self.assertNotIsInstance(error.exception,FreeQuotaExhausted)

    def test_tavily_native_usage_envelope_accepts_free_headroom_only(self):
        opener=Mock();value={'current_plan':'Researcher','plan_limit':1000,'plan_usage':615,'paygo_usage':0,'paygo_limit':None}
        opener.open.return_value=io.BytesIO(json.dumps({'account':value,'key':{'usage':615,'limit':None}}).encode())
        self.assertEqual(confirm_tavily(KEY,6,opener),{'remaining':285,'guard':900})
        opener.open.return_value=io.BytesIO(json.dumps(value).encode())
        with self.assertRaises(WebSearchError):confirm_tavily(KEY,6,opener)

    def test_ordered_switch_only_before_first_search(self):
        first=Mock(key=KEY,provider='serpapi-yandex');first.confirm_free.side_effect=FreeQuotaExhausted()
        second=Mock(key=KEY,provider='serper')
        f1=Mock(return_value=first);f2=Mock(return_value=second)
        selected=select_search(self.path,6,keys={'serpapi-yandex':KEY,'serper':KEY},
            factories={'serpapi-yandex':f1,'serper':f2},grants={'serper':grant()})
        self.assertEqual(selected.provider,'serper');first.search.assert_not_called()
        self.assertEqual(selected.skipped,[{'provider':'serpapi-yandex','reason':'confirmed_free_quota_exhausted'}])
        first.confirm_free.side_effect=WebSearchError('uncertain','free_quota_unconfirmed');f2.reset_mock()
        with self.assertRaises(WebSearchError):
            select_search(self.path,6,keys={'serpapi-yandex':KEY},factories={'serpapi-yandex':f1,'serper':f2},grants={})
        f2.assert_not_called()

    def test_real_adapter_preflights_reach_exa_without_spending_exhausted_providers(self):
        env={name:KEY for name in ('ISTOK_SERPAPI_API_KEY','ISTOK_SERPER_API_KEY','ISTOK_TAVILY_API_KEY','ISTOK_EXA_API_KEY')}
        serp=Mock();serp.open.return_value=io.BytesIO(json.dumps(account(plan_searches_left=2,
                                total_searches_left=2,this_month_usage=248)).encode())
        usage=Mock();usage.open.return_value=io.BytesIO(json.dumps({'account':{'current_plan':'Researcher',
                                'plan_limit':1000,'plan_usage':899,'paygo_usage':0,'paygo_limit':None},'key':{'usage':899,'limit':None}}).encode())
        exa=Mock();exa.open.return_value=io.BytesIO(json.dumps({'results':[{'url':'https://example.org/paper'}],
                                                              'costDollars':{'total':0.004}}).encode())
        def probe(key, needed):return confirm_tavily(key,needed,usage)
        with patch.dict(os.environ,env),patch('plagiarism.search_serpapi.build_opener',return_value=serp), \
             patch('plagiarism.search_exa.build_opener',return_value=exa):
            selected=select_search(self.path,6,grants={'serper':grant(cap=1),'exa':grant('exa')},tavily_probe=probe)
            self.assertEqual(selected.provider,'exa')
            self.assertEqual([row['provider']for row in selected.skipped],['serpapi-yandex','serper','tavily'])
            self.assertEqual(serp.open.call_count,1);self.assertIn('/account.json?',serp.open.call_args.args[0].full_url)
            exa.open.assert_not_called()
            self.assertEqual(selected.search('a synthetic phrase','one'),[{'url':'https://example.org/paper'}])
        with self.budget().connect() as db:
            self.assertEqual(db.execute('SELECT state FROM calls').fetchall(),[('complete',)])
            self.assertEqual(db.execute('SELECT provider FROM pool_calls').fetchall(),[('exa',)])

    def test_disabled_is_explicit_and_pinned_key_ignores_environment(self):
        search=Mock(key=KEY,provider='serper')
        first=Mock();second=Mock(return_value=search)
        selected=select_search(self.path,1,disabled={'serpapi-yandex'},
            keys={'serper':KEY},factories={'serpapi-yandex':first,'serper':second},
            grants={'serper':grant()})
        first.assert_not_called()
        self.assertEqual(second.call_args.kwargs,{'key':KEY})
        self.assertEqual(selected.skipped,[{'provider':'serpapi-yandex','reason':'operator_disabled'}])
        with patch.dict(os.environ,{'ISTOK_SERPAPI_API_KEY':'a-different-environment-key'}):
            from plagiarism.search_serpapi import SerpAPISearch
            self.assertEqual(SerpAPISearch(self.budget(),key=KEY).key,KEY)

    def test_tavily_nullable_guard_rejects_missing_or_paid_limits(self):
        opener=Mock()
        base={'account':{'current_plan':'Researcher','plan_limit':1000,'plan_usage':809,
                         'paygo_usage':0,'paygo_limit':None},'key':{'usage':809,'limit':None}}
        opener.open.return_value=io.BytesIO(json.dumps(base).encode())
        self.assertEqual(confirm_tavily(KEY,6,opener),{'remaining':91,'guard':900})
        for kind in ('missing_paid','positive_paid','missing_key','bool_key'):
            value=json.loads(json.dumps(base))
            if kind=='missing_paid':del value['account']['paygo_limit']
            if kind=='positive_paid':value['account']['paygo_limit']=1
            if kind=='missing_key':del value['key']['limit']
            if kind=='bool_key':value['key']['limit']=True
            opener.open.return_value=io.BytesIO(json.dumps(value).encode())
            with self.assertRaises(WebSearchError) as error:confirm_tavily(KEY,6,opener)
            self.assertNotIsInstance(error.exception,FreeQuotaExhausted)

    def test_unknown_search_outcome_stops_native_pipeline_without_replay(self):
        search=Mock(provider='serper');search.search.side_effect=WebSearchError('synthetic','search_provider_error')
        selected=SelectedSearch(search,[]);fetcher=Mock()
        result=run(' '.join('botanical'+str(i)for i in range(180)),searcher=selected,fetcher=fetcher,
                   use_wiki_followup=False,use_related_fallback=False)
        self.assertEqual(search.search.call_count,1)
        self.assertEqual(result['web']['query_stop_code'],'search_pool_stopped')
        self.assertEqual(result['web']['queries_attempted'],1)
        self.assertIsNone(result['stats']['uniqueness_percent'])
        fetcher.fetch.assert_not_called()

    def test_policy_requires_exact_new_consent_without_changing_legacy(self):
        self.assertIsNone(request_fields({'internet_consent':True}))
        with patch.dict(os.environ,{'ISTOK_AUTOMATIC_SEARCH':'1'}):
            self.assertEqual(contract()['providers'],['serpapi-yandex','serper','tavily','exa'])
            self.assertEqual(request_fields({'search_provider':'auto','provider_consent_version':'search-pool-v1'}),
                             {'search_provider':'auto','provider_consent_version':'search-pool-v1'})
            for body in ({'search_provider':'auto'},{'search_provider':'auto','provider_consent_version':'v2'},
                         {'search_provider':'tavily','provider_consent_version':'search-pool-v1'},
                         {'search_provider':'auto','provider_consent_version':'search-pool-v1','provider_consent':'auto'}):
                with self.assertRaises(access.AccessError):request_fields(body)


if __name__=='__main__':unittest.main()
