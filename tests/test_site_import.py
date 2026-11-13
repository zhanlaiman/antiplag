import unittest
from unittest.mock import Mock
from plagiarism.site_import import fetch_site,public_page_url,ExcludingFetcher
from plagiarism.web_fetch import FetchError,Fetcher

class SiteImportTests(unittest.TestCase):
    def test_same_origin_attempt_limit_partial_failure_and_no_query_or_private_links(self):
        def page(url):
            if url.endswith('/bad'):raise FetchError('Robots denied','robots_denied')
            return {'url':url,'title':'Открытая страница','text':'Публичный текст для проверки.','links':[{'url':link} for link in ['/a','#top','/bad','/later','https://other.example/path','http://127.0.0.1/','/private?token=x','/image.png']]}
        delegate=Mock();delegate.fetch.side_effect=page
        result=fetch_site('https://example.org/',3,delegate)
        self.assertEqual(result['attempted_urls'],['https://example.org/','https://example.org/a','https://example.org/bad'])
        self.assertEqual(len(result['pages']),2);self.assertEqual(result['errors'][0]['code'],'robots_denied')
        self.assertEqual(delegate.fetch.call_count,3)

    def test_invalid_root_never_reaches_fetcher(self):
        delegate=Mock()
        for url in ['http://127.0.0.1/','http://169.254.169.254/','https://example.org/?token=x','https://name:secret@example.org/','file:///tmp/a']:
            with self.subTest(url=url),self.assertRaises(ValueError):fetch_site(url,fetcher=delegate)
        delegate.fetch.assert_not_called()

    def test_private_dns_answer_is_rejected_before_connection(self):
        connector=Mock()
        fetcher=Fetcher(resolver=lambda host,port,timeout:['127.0.0.1'],connector=connector)
        result=fetch_site('https://example.org/',1,fetcher)
        self.assertEqual(result['pages'],[]);self.assertTrue(result['errors']);connector.assert_not_called()

    def test_self_host_and_redirect_are_excluded_but_external_source_kept(self):
        delegate=Mock();wrapped=ExcludingFetcher('https://example.org/article',delegate)
        with self.assertRaises(FetchError):wrapped.fetch('http://example.org/other')
        delegate.fetch.assert_not_called()
        delegate.fetch.return_value={'url':'https://example.org/redirect','text':'Собственный текст'}
        with self.assertRaises(FetchError):wrapped.fetch('https://other.example/redirect')
        delegate.fetch.return_value={'url':'https://other.example/article','text':'Внешний источник'}
        self.assertEqual(wrapped.fetch('https://other.example/article')['text'],'Внешний источник')

    def test_pipeline_does_not_report_own_page_as_borrowing(self):
        from plagiarism import web_search
        text='Редактор сравнил четыре зелёных листа и записал форму каждой жилки. Затем он описал необычный узор.'
        class Search:
            provider='fixture'
            def search(self,*args):return [{'url':'https://example.org/own'},{'url':'https://other.example/source'}]
        delegate=Mock();delegate.fetch.return_value={'url':'https://other.example/source','title':'Внешний источник','text':text}
        report=web_search.run(text,searcher=Search(),fetcher=ExcludingFetcher('https://example.org/article',delegate),
            use_wiki_followup=False,use_related_fallback=False,use_zero_fallback=False)
        self.assertTrue(report['matches'])
        self.assertEqual({source['url'] for source in report['sources']},{'https://other.example/source'})
        self.assertEqual(delegate.fetch.call_count,1)
        self.assertIsNone(report['stats']['uniqueness_percent'])
        self.assertTrue(any(e['code']=='self_source_excluded' for e in report['web']['errors']))

    def test_character_limit_is_not_silent_truncation(self):
        delegate=Mock();delegate.fetch.return_value={'url':'https://example.org/','title':'Large','text':'x'*200001}
        result=fetch_site('https://example.org/',1,delegate)
        self.assertEqual(result['pages'],[]);self.assertEqual(result['errors'][0]['code'],'site_text_limit')

if __name__=='__main__':unittest.main()
