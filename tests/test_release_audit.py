from hashlib import sha256
import unittest
from plagiarism import web_search
from scripts.audit_release import evaluate,summarize
from tests.test_web_search import Search,Pages,TEXT


class ReleaseAuditTests(unittest.TestCase):
    def case(self):
        return {'id':'release-control','text_sha256':sha256(TEXT.encode()).hexdigest(),
                'expected_evidence':[{'target_span':[0,len(TEXT)],'source_quote':TEXT,
                                      'source_urls':['https://example.org/article'],'needs_lexical_highlight':True}]}

    def test_grounded_pipeline_report_counts_literal_words_and_all_retrieval_stages(self):
        report=web_search.run(TEXT,searcher=Search(),fetcher=Pages())
        row=evaluate(report,self.case(),[]);e=row['evidence'][0]
        self.assertTrue(e['whole_insertion']);self.assertEqual(e['copied_words'],e['highlighted_words'])
        self.assertTrue(all(e[k] for k in ['internal_phrase','expected_url_returned','expected_url_queued','expected_url_read','source_quote_extracted']))
        self.assertEqual(row['invalid_evidence'],0)

    def test_discovered_but_unread_source_is_not_scored_as_extracted_or_highlighted(self):
        report=web_search.run(TEXT,searcher=Search(),fetcher=Pages(fail=['https://example.org/article']))
        row=evaluate(report,self.case(),[]);e=row['evidence'][0]
        self.assertEqual(e['earliest_observed_loss'],'expected_url_unread')
        self.assertFalse(e['source_quote_extracted']);self.assertEqual(e['highlighted_words'],0)
        stats=summarize([row,{'id':'failed','outcome':'failed'},{'id':'pending','outcome':'unstarted'}])
        self.assertEqual((stats['complete'],stats['failed'],stats['unstarted']),(1,1,1))
        self.assertEqual(stats['completed_without_read_pages'],1)

    def test_extracted_source_without_highlights_is_not_blameable_on_a_phrase_gap(self):
        report=web_search.run(TEXT,searcher=Search(),fetcher=Pages())
        report['matches']=[];report['web']['query_spans']=[]
        row=evaluate(report,self.case(),[])
        self.assertEqual(row['evidence'][0]['earliest_observed_loss'],'highlight_incomplete')
