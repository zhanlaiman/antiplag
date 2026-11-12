from copy import deepcopy
import unittest
from unittest.mock import patch

from plagiarism import web_wiki_followup as post


def query(text,start,end):
    from plagiarism.matching import _tokens
    return {'query':'"'+' '.join(t.value for t in _tokens(text[start:end]))+'"',
            'start':start,'end':end}


class WikiQueryDiversityTests(unittest.TestCase):
    def fixture(self):
        intro='Организаторы собирают фотографии и записывают воспоминания владельцев старых семейных альбомов.'
        insert='Историческое описание содержит сведения о первых опытах создания фотографических изображений.'
        text=intro+'\n\n'+insert
        plan=[query(text,0,len(intro)-1),query(text,0,intro.index('старых')-1),
              query(text,len(intro)+2,len(text)-1)]
        return text,plan

    def test_overlapping_introduction_windows_use_a_different_paragraph(self):
        text,plan=self.fixture();before=deepcopy(plan)
        chosen=post.select_post_queries(text,plan)
        self.assertEqual([q['primary_query_index'] for q in chosen],[0,2])
        self.assertEqual(chosen[0]['query'],plan[0]['query'])
        self.assertEqual(plan,before)
        self.assertEqual(len(chosen),2)

    def test_nonoverlapping_windows_keep_original_order(self):
        text,plan=self.fixture();plan=[plan[0],plan[2],plan[1]]
        chosen=post.select_post_queries(text,plan)
        self.assertEqual([q['primary_query_index'] for q in chosen],[0,1])

    def test_single_paragraph_keeps_overlapping_windows(self):
        text,plan=self.fixture();text=text.replace('\n\n','  ')
        chosen=post.select_post_queries(text,plan)
        self.assertEqual([q['primary_query_index'] for q in chosen],[0,1])

    def test_cross_paragraph_window_is_not_a_replacement(self):
        text,plan=self.fixture();plan[2]=query(text,plan[0]['end']-8,len(text)-1)
        chosen=post.select_post_queries(text,plan)
        self.assertEqual([q['primary_query_index'] for q in chosen],[0,1])

    def test_protected_citation_is_not_reused_as_secondary(self):
        text,plan=self.fixture()
        with patch('plagiarism.web_search._citation_queries',return_value=[plan[2]]):
            chosen=post.select_post_queries(text,plan)
        self.assertEqual([q['primary_query_index'] for q in chosen],[0,1])

    def test_single_query_is_unchanged(self):
        text,plan=self.fixture();chosen=post.select_post_queries(text,plan[:1])
        self.assertEqual(len(chosen),1)
        self.assertEqual(chosen[0]['query'],plan[0]['query'])

    def test_legacy_unchanged_policy_retains_original_slots(self):
        text,plan=self.fixture()
        chosen=post.select_post_queries(text,plan,secondary_query_policy=post.UNCHANGED_SECONDARY_QUERY_POLICY)
        self.assertEqual([q['primary_query_index'] for q in chosen],[0,1])

    def test_clause_shrink_still_applies_to_selected_replacement(self):
        text,plan=self.fixture();intro=text[:text.index('\n\n')]
        text=intro+'\n\nУточнение — архивные сведения помогают сотрудникам готовить точные описи старинных документов.'
        plan[2]=query(text,len(intro)+2,len(text)-1)
        chosen=post.select_post_queries(text,plan)
        self.assertEqual(chosen[1]['primary_query_index'],2)
        self.assertGreater(chosen[1]['start'],plan[2]['start'])
        self.assertTrue(chosen[1]['query'].startswith('"архивные сведения'))


if __name__=='__main__':
    unittest.main()
