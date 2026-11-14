from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from plagiarism.matching import MAX_TEXT_CHARS, analyze
from plagiarism.text_policy import owner_target_text
from plagiarism.web_search import plan_queries, run


class OwnerTextPolicyTests(unittest.TestCase):
    def test_owner_analysis_keeps_tail_offsets_and_default_rejection_is_unchanged(self):
        excerpt = 'Сосна берёза клён кедр липа пихта дуб вяз ель.'
        prefix = '🙂 ' + 'Unrelated authored background. ' * 7000
        text = prefix + excerpt
        source = {'id':'s','title':'Контроль','text':excerpt,'url':'https://example.org/fixture'}
        self.assertGreater(len(text),MAX_TEXT_CHARS)
        with self.assertRaises(ValueError):analyze(text,[source])
        with owner_target_text(True):
            result=analyze(text,[source])
            self.assertEqual(result['stats']['total_characters'],len(text))
            match=next(m for m in result['matches'] if m['kind']=='exact')
            self.assertEqual(match['start'],len(prefix))
            self.assertEqual(text[match['start']:match['end']],match['source_excerpt'])
            with self.assertRaises(ValueError):analyze(text,[{**source,'text':'x'*(MAX_TEXT_CHARS+1)}])
        with self.assertRaises(ValueError):analyze(text,[])

    def test_real_web_pipeline_preserves_full_long_text_and_six_query_ceiling_offline(self):
        text='Independent background keeps the notebook dry. ' * 4600
        class Search:
            provider='fixture'
            def __init__(self):self.calls=[]
            def search(self,phrase,identifier):self.calls.append((phrase,identifier));return []
        class Fetch:
            def fetch(self,url):raise AssertionError('No URL was returned')
        with self.assertRaises(ValueError):plan_queries(text)
        search=Search()
        with patch('socket.socket',side_effect=AssertionError('Offline test')):
            with owner_target_text(True):
                result=run(text,searcher=search,fetcher=Fetch(),use_named_references=False,
                           use_zero_fallback=False,use_wiki_followup=False,use_related_fallback=False)
        self.assertEqual(result['text'],text)
        self.assertEqual(result['stats']['total_characters'],len(text))
        self.assertLessEqual(len(search.calls),6)
        self.assertEqual(len({rid for _,rid in search.calls}),len(search.calls))
        self.assertIsNone(result['stats']['uniqueness_percent'])

    def test_shared_worker_resets_owner_permission_after_success_and_exception(self):
        text='x'*(MAX_TEXT_CHARS+1)
        def owner(fail):
            with owner_target_text(True):
                self.assertEqual(analyze(text,[])['stats']['total_characters'],len(text))
                if fail:raise RuntimeError('Synthetic worker failure')
        def guest():
            with self.assertRaises(ValueError):analyze(text,[])
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(owner,False).result();executor.submit(guest).result()
            with self.assertRaises(RuntimeError):executor.submit(owner,True).result()
            executor.submit(guest).result()
        with owner_target_text(True):
            with owner_target_text(False):guest()
        guest()


if __name__=='__main__':unittest.main()
