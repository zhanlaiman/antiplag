from copy import deepcopy
import unittest

from plagiarism.web_related_fallback import append_related
from plagiarism import web_search as web
from plagiarism.matching import _tokens
from plagiarism.web_fetch import FetchError
from unittest.mock import patch

PARENT = 'https://journal.example/article/parent'
ARTICLE = 'https://journal.example/article/aurora'
NOVEL = ARTICLE + '_(novel)'
SOURCE = ('Aurora observations continued throughout the northern winter as researchers '
          'compared magnetic measurements from several remote stations.')
NEGATIVE = ('During a dark polar evening, the observers noticed green aurora above '
            'their cabin. The instruments stayed indoors because the batteries were empty.')


class Pages:
    def __init__(self, body=SOURCE):
        self.calls = []
        self.body = body
        self.parents = [{'url':PARENT,'links':[{'url':ARTICLE,'text':'Aurora'},{'url':NOVEL,'text':'Aurora'}]}]
    def fetch(self,url):
        self.calls.append(url)
        if url == PARENT:
            return {**self.parents[0],'text':'The index lists publication dates and author affiliations.'}
        return {'url':url,'text':self.body if url==ARTICLE else 'Aurora observations continued throughout the northern winter. The author then discussed an imaginary sailor.'}


def baseline(text=SOURCE, body=SOURCE):
    class Search:
        def search(self,query,request_id):
            return [{'url':PARENT}]
    pages = Pages(body)
    report = web.run(text,searcher=Search(),fetcher=pages,use_wiki_followup=False)
    return report,pages


class RelatedFallbackTests(unittest.TestCase):
    def apply(self,report,pages,**kwargs):
        return append_related(report['text'],report,pages.parents,fetcher=pages,started_at=0,clock=lambda:1,**kwargs)

    def test_recovers_copy_after_a_previous_partial_match(self):
        before,pages = baseline()
        self.assertGreater(before['stats']['matched_words'],0)
        self.assertLess(before['stats']['matched_words'],len(_tokens(SOURCE)))
        saved = deepcopy(before)
        after = self.apply(before,pages)
        self.assertEqual(after['stats']['matched_words'],len(_tokens(SOURCE)))
        self.assertEqual(before,saved)
        self.assertEqual(after['sources'][:len(before['sources'])],before['sources'])
        self.assertEqual(after['matches'][:len(before['matches'])],before['matches'])
        self.assertEqual(after['web']['attempted_urls'][:len(before['web']['attempted_urls'])],before['web']['attempted_urls'])
        self.assertEqual(after['web']['query_spans'],before['web']['query_spans'])
        self.assertEqual(len({m['id'] for m in after['matches']}),len(after['matches']))

    def test_independent_text_on_same_topic_remains_unhighlighted(self):
        before,pages = baseline(NEGATIVE)
        after = self.apply(before,pages)
        self.assertEqual(before['matches'],[])
        self.assertEqual(after['matches'],[])

    def test_full_copy_already_found_does_not_add_fetches(self):
        before,pages = baseline()
        before['text'] = 'Aurora observations continued throughout the northern winter.'
        before['matches'] = web.analyze(before['text'],before['sources'])['matches']
        count = len(pages.calls)
        after = self.apply(before,pages)
        self.assertIs(after,before)
        self.assertEqual(len(pages.calls),count)

    def test_total_page_cap_is_preserved_and_unknown_is_not_complete(self):
        before,pages = baseline()
        before['web']['max_page_attempts'] = len(before['web']['attempted_urls'])
        count = len(pages.calls)
        after = self.apply(before,pages)
        self.assertEqual(len(pages.calls),count)
        self.assertTrue(after['web']['related_fallback']['page_limit_reached'])
        self.assertIsNone(after['stats']['uniqueness_percent'])

    def test_original_deadline_prevents_new_work(self):
        before,pages = baseline()
        count = len(pages.calls)
        after = append_related(SOURCE,before,pages.parents,fetcher=pages,started_at=0,clock=lambda:281)
        self.assertEqual(len(pages.calls),count)
        self.assertTrue(after['web']['related_fallback']['deadline_reached'])
        self.assertIsNone(after['stats']['uniqueness_percent'])

    def test_saved_unknown_does_not_create_evidence_or_unique_percentage(self):
        before,pages = baseline()
        class Missing:
            def fetch(self,url):
                raise FetchError('Unknown saved page','saved_page_cache_miss')
        after = append_related(SOURCE,before,pages.parents,fetcher=Missing(),started_at=0,clock=lambda:1)
        self.assertEqual(after['sources'],before['sources'])
        self.assertEqual(after['matches'],before['matches'])
        self.assertIsNone(after['stats']['uniqueness_percent'])

    def test_redirect_alias_is_not_counted_as_another_source(self):
        before,pages = baseline()
        class Alias:
            def fetch(self,url):
                return {'url':NOVEL,'text':SOURCE}
        after = append_related(SOURCE,before,pages.parents,fetcher=Alias(),started_at=0,clock=lambda:1)
        self.assertEqual(after['sources'],before['sources'])
        self.assertEqual(after['matches'],before['matches'])
        self.assertEqual(after['web']['sources_checked'],before['web']['sources_checked'])
        self.assertEqual(after['web']['urls_deduplicated'],before['web']['urls_deduplicated']+1)

    def test_bad_body_sha_is_not_admitted(self):
        before,pages = baseline()
        class Invalid:
            def fetch(self,url):
                return {'url':ARTICLE,'text':SOURCE,'text_sha256':'0'*64}
        after = append_related(SOURCE,before,pages.parents,fetcher=Invalid(),started_at=0,clock=lambda:1)
        self.assertEqual(after['sources'],before['sources'])
        self.assertEqual(after['matches'],before['matches'])
        self.assertEqual(after['web']['errors'][-1]['code'],'saved_page_invalid')

    def test_two_alternatives_only_and_no_recursion(self):
        before,pages = baseline()
        pages.parents[0]['links'] = [{'url':NOVEL,'text':'Aurora'}] + [{'url':ARTICLE+str(i),'text':'Aurora'} for i in range(6)]
        count = len(pages.calls)
        after = self.apply(before,pages)
        self.assertEqual(len(pages.calls)-count,2)
        self.assertEqual(len(after['web']['related_fallback']['candidates']),2)

    def test_cross_origin_and_query_links_are_not_admitted(self):
        before,pages = baseline()
        pages.parents[0]['links'] = [{'url':'https://outside.example/aurora','text':'Aurora'},
                                    {'url':ARTICLE+'?query=1','text':'Aurora'}]
        count = len(pages.calls)
        after = self.apply(before,pages)
        self.assertIs(after,before)
        self.assertEqual(len(pages.calls),count)

    def test_lower_scoring_anchor_is_not_an_ambiguous_alternative(self):
        before,pages = baseline()
        pages.parents[0]['links'] = [{'url':NOVEL,'text':'Aurora'},
                                    {'url':ARTICLE,'text':'Aurora notebooks'}]
        count = len(pages.calls)
        after = self.apply(before,pages)
        self.assertIs(after,before)
        self.assertEqual(len(pages.calls),count)

    def test_raw_body_cap_includes_whitespace(self):
        before,pages = baseline()
        class Oversized:
            def fetch(self,url):
                return {'url':ARTICLE,'text':SOURCE+' '*web.MAX_TEXT_CHARS}
        after = append_related(SOURCE,before,pages.parents,fetcher=Oversized(),started_at=0,clock=lambda:1)
        self.assertEqual(after['sources'],before['sources'])
        self.assertIsNone(after['stats']['uniqueness_percent'])

    def test_optional_pdf_extraction_status_is_preserved(self):
        before,pages = baseline()
        class PDF:
            def fetch(self,url):
                return {'url':ARTICLE,'text':SOURCE,'content_type':'application/pdf',
                        'optional_status':{'engine':'pdfium','status':'unavailable','code':'not_configured'}}
        after = append_related(SOURCE,before,pages.parents,fetcher=PDF(),started_at=0,clock=lambda:1)
        self.assertEqual(after['web']['optional_pdf_status'][-1]['code'],'not_configured')


class RelatedFallbackRunTests(unittest.TestCase):
    class Search:
        provider = 'tavily'
        def __init__(self): self.calls = []
        def search(self,query,request_id):
            self.calls.append(query)
            return [{'url':PARENT}]

    def test_public_run_option_preserves_query_count_and_old_attempt_prefix(self):
        reports, counts = [], []
        for enabled in (False,True):
            search, pages = self.Search(), Pages()
            reports.append(web.run(SOURCE,searcher=search,fetcher=pages,
                use_wiki_followup=False,use_related_fallback=enabled))
            counts.append(len(search.calls))
        before,after = reports
        self.assertEqual(counts[0],counts[1])
        self.assertEqual(after['stats']['matched_words'],len(_tokens(SOURCE)))
        self.assertEqual(after['web']['attempted_urls'][:len(before['web']['attempted_urls'])],before['web']['attempted_urls'])
        self.assertEqual(after['matches'][:len(before['matches'])],before['matches'])
        self.assertTrue(after['web']['related_fallback_enabled'])

    def test_invalid_option_fails_before_search(self):
        for bad in (1,'yes',[],{}):
            search = self.Search()
            with self.assertRaises(ValueError):
                web.run(SOURCE,searcher=search,fetcher=Pages(),use_related_fallback=bad)
            self.assertEqual(search.calls,[])

    def test_invalid_environment_fails_before_search(self):
        search = self.Search()
        with patch.dict('os.environ',{'ISTOK_WEB_RELATED_FALLBACK':'invalid'}):
            with self.assertRaises(ValueError):
                web.run(SOURCE,searcher=search,fetcher=Pages())
        self.assertEqual(search.calls,[])

    def test_normal_defaults_enable_fallback_without_real_transports(self):
        search,pages = self.Search(),Pages()
        class Wiki:
            provider='mediawiki'
            halt_code=None
            last_usage=None
            def search(self,query,request_id):
                self.last_usage={'public_queries':0,'cached':True,'cost_kzt':0}
                return []
        with patch.dict('os.environ',{},clear=True), \
                patch.object(web,'selected_provider',return_value='tavily'), \
                patch.object(web,'SearchBudget',return_value=object()), \
                patch('plagiarism.search_tavily.TavilySearch',return_value=search), \
                patch('plagiarism.web_fetch.Fetcher',return_value=pages), \
                patch('plagiarism.search_mediawiki.MediaWikiSearch',return_value=Wiki()):
            result=web.run(SOURCE)
        self.assertTrue(result['web']['related_fallback_enabled'])
        self.assertTrue(result['web']['post_wiki_enabled'])
        self.assertEqual(result['stats']['matched_words'],len(_tokens(SOURCE)))
        self.assertLessEqual(result['web']['urls_attempted'],26)


if __name__=='__main__':
    unittest.main(verbosity=2)
