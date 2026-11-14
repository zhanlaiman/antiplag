import io
import json
import os
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError
from urllib.parse import urlsplit,parse_qs

from plagiarism.search_serpapi import SerpAPISearch,free_quota
from plagiarism.web_search import WebSearchError,run,plan_queries
from tests.test_search_tavily import Budget


def response(value):return io.BytesIO(json.dumps(value).encode())


def account(**changes):
    return {'account_status':'Active','plan_name':'Free Plan','plan_monthly_price':0.0,
            'searches_per_month':250,'plan_searches_left':250,'total_searches_left':250,
            'this_month_usage':0,'extra_credits':0,'api_key':'not-public','account_email':'private@example.org',**changes}


class SerpAPITests(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        env=patch.dict(os.environ,{'ISTOK_SERPAPI_API_KEY':'a'*64,'ISTOK_RUNTIME_DIR':temp.name});env.start();self.addCleanup(env.stop)
        self.budget=Budget();self.search=SerpAPISearch(self.budget);self.search.opener=Mock()

    def confirm(self,needed=6):
        self.search.opener.open.return_value=response(account());return self.search.confirm_free(needed)

    def test_free_plan_is_required_and_sensitive_account_fields_are_not_returned(self):
        quota=self.confirm();self.assertEqual(quota['total_searches_left'],250)
        self.assertNotIn('api_key',quota);self.assertNotIn('account_email',quota)
        self.assertEqual(self.budget.reserved,set())
        for changes in ({'plan_monthly_price':1},{'plan_name':'Starter Plan'},{'account_status':'Inactive'},
                        {'searches_per_month':1000},{'extra_credits':1},{'total_searches_left':True},
                        {'total_searches_left':5},{'plan_searches_left':249},{'plan_monthly_price':False}):
            with self.subTest(changes=changes),self.assertRaises(WebSearchError):free_quota(account(**changes),6)

    def test_fixed_google_request_preserves_phrase_and_only_returns_bounded_urls(self):
        self.confirm();self.search.opener.open.return_value=response({'search_metadata':{'status':'Success'},
            'organic_results':[{'link':'https://EXAMPLE.org/article#first','snippet':'Never matching evidence'},
                               {'link':'https://example.org/article#second'},
                               *({'link':'https://example.org/page'+str(i)}for i in range(8))]})
        phrase='"Сосна берёза клён кедр липа пихта"'
        result=self.search.search(phrase,'rid-1')
        self.assertEqual(len(result),4);self.assertTrue(all(set(row)=={'url'}for row in result))
        request=self.search.opener.open.call_args.args[0];url=urlsplit(request.full_url);parameters=parse_qs(url.query)
        self.assertEqual(url.netloc,'serpapi.com');self.assertEqual(url.path,'/search.json')
        self.assertEqual(parameters,{'api_key':['a'*64],'engine':['google'],'q':[phrase],'num':['5'],
                                    'google_domain':['google.com'],'hl':['ru'],'output':['json']})
        self.assertEqual(self.search.opener.open.call_args.kwargs,{'timeout':25})
        self.assertEqual(self.budget.finished,['rid-1']);self.assertNotIn('a'*64,json.dumps(result))

    def test_missing_or_expired_confirmation_stops_before_budget_or_search(self):
        with self.assertRaises(WebSearchError):self.search.search('valid phrase','rid-1')
        self.search.opener.open.assert_not_called();self.assertEqual(self.budget.reserved,set())
        with patch('plagiarism.search_serpapi.time.monotonic',return_value=0):self.confirm()
        with patch('plagiarism.search_serpapi.time.monotonic',return_value=301),self.assertRaises(WebSearchError):
            self.search.search('valid phrase','rid-1')
        self.assertEqual(self.search.opener.open.call_count,1);self.assertEqual(self.budget.reserved,set())

    def test_unknown_transport_outcome_is_reserved_and_not_automatically_repeated(self):
        self.confirm();self.search.opener.open.side_effect=TimeoutError('a'*64)
        with self.assertRaises(WebSearchError)as caught:self.search.search('valid phrase','rid-1')
        self.assertNotIn('a'*64,str(caught.exception));self.assertIn('rid-1',self.budget.reserved)
        with self.assertRaises(WebSearchError):self.search.search('valid phrase','rid-1')
        self.assertEqual(self.search.opener.open.call_count,2);self.assertEqual(self.budget.finished,[])

    def test_http_error_is_closed_sanitized_and_zero_result_success_is_allowed(self):
        self.confirm();stream=io.BytesIO(b'private provider detail')
        self.search.opener.open.side_effect=HTTPError('https://serpapi.com/search.json?api_key='+'a'*64,429,'private',{},stream)
        with self.assertRaises(WebSearchError)as caught:self.search.search('valid phrase','rid-1')
        self.assertTrue(stream.closed);self.assertNotIn('a'*64,str(caught.exception))
        self.assertEqual(caught.exception.code,'search_rate_limited')
        self.search.opener.open.side_effect=None;self.search.opener.open.return_value=response({'search_metadata':{'status':'Success'}})
        self.assertEqual(self.search.search('another phrase','rid-2'),[])

    def test_invalid_input_and_reflected_credentials_never_reach_evidence(self):
        for phrase in ('','x'*401,'line\nbreak','\ud800'):
            with self.assertRaises(WebSearchError):self.search.search(phrase,'rid-1')
        self.search.opener.open.assert_not_called();self.assertEqual(self.budget.reserved,set())
        self.confirm();self.search.opener.open.return_value=response({'search_metadata':{'status':'Success'},
            'organic_results':[{'link':'https://example.org/'+self.search.key}]})
        with self.assertRaises(WebSearchError)as caught:self.search.search('valid phrase','rid-1')
        self.assertNotIn(self.search.key,str(caught.exception));self.assertEqual(self.budget.finished,[])

    def test_successful_encrypted_google_interstitials_are_errors_not_source_urls(self):
        for index,url in enumerate(('https://www.google.com/goto?url=CAESopaque',
                                    'https://google.com/url?url=CAESopaque')):
            self.confirm()
            self.search.opener.open.return_value=response({'search_metadata':{'status':'Success'},
                'organic_results':[{'link':url,'displayed_link':'https://example.org/source',
                                    'snippet':'A provider snippet is not a source'}]})
            with self.assertRaises(WebSearchError)as caught:self.search.search('valid phrase','opaque-'+str(index))
            self.assertEqual(caught.exception.code,'search_redirect_unresolved')
            self.assertIn('opaque-'+str(index),self.budget.finished)
            self.assertEqual(self.search.last_usage,{'credits':1})
        self.confirm()
        self.search.opener.open.return_value=response({'search_metadata':{'status':'Success'},
            'organic_results':[{'link':'https://www.google.com/intl/en/about/'}]})
        self.assertEqual(self.search.search('another phrase','direct-google'),[{'url':'https://www.google.com/intl/en/about/'}])

    def test_unresolved_results_cannot_be_a_zero_percent_uniqueness_verdict(self):
        self.confirm()
        self.search.opener.open.return_value=response({'search_metadata':{'status':'Success'},
            'organic_results':[{'link':'https://www.google.com/goto?url=CAESopaque'}]})
        fetcher=Mock()
        text=' '.join('botanical'+str(i)for i in range(180))
        self.assertEqual(len(plan_queries(text)),6)
        result=run(text,
                   searcher=self.search,fetcher=fetcher,use_wiki_followup=False,use_related_fallback=False)
        fetcher.fetch.assert_not_called()
        self.assertEqual(result['stats']['checked_source_count'],0)
        self.assertIsNone(result['stats']['uniqueness_percent'])
        self.assertEqual(result['web']['status'],'partial')
        self.assertEqual(result['web']['queries_attempted'],1)
        self.assertEqual(result['web']['queries_unattempted'],5)
        self.assertEqual(result['web']['query_stop_code'],'search_redirect_unresolved')
        self.assertEqual(self.search.opener.open.call_count,2) # Account + one search.
        self.assertTrue(any(error['code']=='search_redirect_unresolved' and 'служебные ссылки' in error['message']
                            for error in result['web']['errors']))


if __name__=='__main__':unittest.main()
