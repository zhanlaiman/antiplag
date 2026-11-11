"""Independent authored source-proof regressions; no private files or network."""
import unittest
from tests import test_web_wiki_followup as fixtures
from tests.test_web_wiki_followup import fixture, page, NEW, NEGATIVE, A, sha


class SourceProofReview(unittest.TestCase):
    def invoke(self, fetched):
        class Search:
            provider='mediawiki';last_usage=None;halt_code=None
            def search(self,*args):
                self.last_usage={'public_queries':0,'cost_kzt':0,'cached':True}
                return [{'url':A}]
        return fixtures.ProductionEntrypointGuards().invoke(Search(),fetch=lambda _:fetched)

    def pdf(self, primary, view):
        return {**page(A,primary),'content_type':'application/pdf','raw_pdf_sha256':'a'*64,'pdf_pages':1,
            'supplemental_texts':[{'id':'pdfium-v1','parser_version':'5.14.0','extraction':'pdfium-text-v1',
                'raw_pdf_sha256':'a'*64,'page_count':1,'text':view,'text_sha256':sha(view)}]}

    def test_unrelated_new_pdf_view_never_supplies_unproved_copy(self):
        report,audit=self.invoke(self.pdf(NEGATIVE,NEW))
        self.assertEqual(report['stats']['matched_words'],9)
        self.assertFalse(any(m['source_url']==A for m in report['matches']))
        self.assertTrue(audit['prefix_preserved']);self.assertTrue(audit['old_exact_coverage_preserved'])

    def test_supported_spacing_view_keeps_original_pdf_prefix_and_physical_count(self):
        text,old,_=fixture()
        report,audit=self.invoke(self.pdf(NEW.replace('снимки','сни мки'),NEW))
        self.assertEqual(report['sources'][:len(old['sources'])],old['sources'])
        self.assertEqual(report['stats']['matched_words'],19);self.assertEqual(report['web']['sources_checked'],2)
        self.assertTrue(any(m.get('pdf_spacing_evidence') for m in report['matches'] if m['source_url']==A))
        self.assertTrue(audit['old_exact_coverage_preserved'])

    def test_bad_new_body_digest_does_not_replace_primary_or_add_highlight(self):
        text,old,_=fixture();fetched={**page(A,NEW),'text_sha256':'f'*64}
        report,audit=self.invoke(fetched)
        self.assertEqual(report['sources'],old['sources']);self.assertEqual(report['stats']['matched_words'],9)
        self.assertEqual(audit['unknown_extra_urls'],[A]);self.assertTrue(audit['prefix_preserved'])


if __name__=='__main__':unittest.main()
