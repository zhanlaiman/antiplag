from copy import deepcopy
import unittest
from unittest.mock import patch
from plagiarism import web_wiki_followup as post
from plagiarism.matching import _tokens


class UncoveredWikiQueryTests(unittest.TestCase):
    def fixture(self):
        blocks=['Первый раздел содержит подробное описание старых приборов и результатов наблюдения.',
                'Второй раздел посвящён устройству лаборатории и организации независимых опытов.',
                'Начало заголовка создание общей теории измерений образование единиц физических величин']
        text='\n\n'.join(blocks);plan=[];start=0
        for block in blocks:
            words=_tokens(block)
            plan.append({'query':'"'+' '.join(t.value for t in words)+'"','start':start,'end':start+len(block)})
            start+=len(block)+2
        chosen=[{**q,'primary_query_index':i} for i,q in enumerate(plan[:2])]
        covered={t.start for t in _tokens(text) if t.end<=plan[1]['end']}
        return text,plan,chosen,covered

    def test_second_covered_slot_uses_central_six_uncovered_words(self):
        text,plan,chosen,covered=self.fixture();before=deepcopy((plan,chosen))
        result=post._prefer_uncovered_query(text,plan,chosen,covered)
        self.assertEqual(result[0],chosen[0]);self.assertEqual(result[1]['primary_query_index'],2)
        self.assertEqual(result[1]['query'],'"создание общей теории измерений образование единиц"')
        self.assertEqual(text[result[1]['start']:result[1]['end']],'создание общей теории измерений образование единиц')
        self.assertEqual((plan,chosen),before)

    def test_partially_covered_second_slot_is_preserved(self):
        text,plan,chosen,covered=self.fixture();covered.remove(plan[1]['start'])
        self.assertEqual(post._prefer_uncovered_query(text,plan,chosen,covered),chosen)

    def test_partially_covered_candidate_is_not_reused(self):
        text,plan,chosen,covered=self.fixture();covered.add(plan[2]['start'])
        self.assertEqual(post._prefer_uncovered_query(text,plan,chosen,covered),chosen)

    def test_no_grounded_words_preserves_query_diversity(self):
        text,plan,chosen,_=self.fixture()
        self.assertEqual(post._prefer_uncovered_query(text,plan,chosen,set()),chosen)

    def test_explicit_citation_is_protected(self):
        text,plan,chosen,covered=self.fixture()
        with patch('plagiarism.web_search._citation_queries',return_value=[plan[2]]):
            self.assertEqual(post._prefer_uncovered_query(text,plan,chosen,covered),chosen)

    def test_short_candidates_and_one_slot_remain_unchanged(self):
        text,plan,chosen,covered=self.fixture();plan[2]['end']=plan[2]['start']+15
        self.assertEqual(post._prefer_uncovered_query(text,plan,chosen,covered),chosen)
        self.assertEqual(post._prefer_uncovered_query(text,plan,chosen[:1],covered),chosen[:1])
