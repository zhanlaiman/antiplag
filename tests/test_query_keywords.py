from copy import deepcopy
import unittest
from unittest.mock import patch

from plagiarism.matching import _tokens
from plagiarism.query_keywords import KEYWORD_STRATEGY, complete_keyword_queries, metadata_anchor_matches
from plagiarism.query_clauses import CLAUSE_STRATEGY
from plagiarism.web_search import plan_queries, run


WORDS = 'сосна берёза клён кедр липа пихта дуб вяз ель бук ясень осина'


def query(text, fragment):
    start = text.index(fragment)
    return {'query':'"'+' '.join(t.value for t in _tokens(fragment))+'"',
            'start':start,'end':start+len(fragment)}


class KeywordQueryTests(unittest.TestCase):
    def test_metadata_anchor_requires_specific_labelled_field_and_substantial_overlap(self):
        label='экологически безопасная борьба сосущими насекомыми вредителями хлопковых культурах использованием биологических средств'
        text='Ключевые слова: '+label.replace('безопасная','безопасное')
        self.assertTrue(metadata_anchor_matches(text,label.upper()))
        for field,anchor in ((label,label),(text,'средств'),(text,'совсем другая научная статья содержит отличные методы наблюдений'),
                             (text+' лишнее '*80,label),(text,label+' дополнительная тема ' * 15)):
            self.assertFalse(metadata_anchor_matches(field,anchor))

    def test_partial_title_does_not_stop_a_strong_metadata_link_to_the_full_article(self):
        title='сосна берёза пихта рябина осина ольха калина бузина черёмуха можжевельник'
        text='Ключевые слова: '+title+'\n\nНаблюдатель аккуратно записывает температуру воздуха после каждого измерения прибора возле большой исследовательской площадки.'
        class Search:
            def __init__(self):self.calls=[]
            def search(self,phrase,request_id):self.calls.append(phrase);return [{'url':'https://example.org/index'}]
        class Fetch:
            def __init__(self,complete=False):self.calls=[];self.complete=complete
            def fetch(self,url):
                self.calls.append(url)
                return {'url':url,'title':'Artificial publication index' if url.endswith('/index') else 'Full text',
                        'text':text if self.complete or url.endswith('/article') else title.upper(),
                        'links':[{'url':'https://example.org/article','text':title.upper()}] if url.endswith('/index') else []}
        a,b=Fetch(),Fetch();s1,s2=Search(),Search()
        with patch('socket.socket',side_effect=AssertionError('Offline pipeline')):
            old=run(text,searcher=s1,fetcher=a,query_strategy=CLAUSE_STRATEGY)
            new=run(text,searcher=s2,fetcher=b,query_strategy=KEYWORD_STRATEGY)
            complete=Fetch(complete=True);run(text,searcher=Search(),fetcher=complete,query_strategy=KEYWORD_STRATEGY)
        self.assertEqual(len(s1.calls),len(s2.calls));self.assertEqual(len(a.calls),1);self.assertEqual(len(b.calls),2)
        self.assertLess(old['stats']['matched_words'],old['stats']['total_words'])
        self.assertEqual(new['stats']['matched_words'],new['stats']['total_words'])
        self.assertEqual(new['web']['link_discoveries'][0]['basis'],'keyword_metadata_anchor')
        self.assertEqual(len(complete.calls),1)
    def test_russian_and_english_fields_keep_complete_normalized_words_and_unicode_offsets(self):
        for label in ('Ключевые слова', 'KEYWORDS', 'Key words'):
            with self.subTest(label=label):
                text='🙂 Вводная фраза для первой поисковой позиции.\n'+label+': '+WORDS+'\nСледующий абзац.'
                old=[query(text,'Вводная фраза для первой поисковой позиции'),query(text,' '.join(WORDS.split()[:8]))]
                new=complete_keyword_queries(text,old)
                self.assertEqual(new[0],old[0]);self.assertEqual(len(new),len(old))
                self.assertEqual(new[1]['query'],' '.join(t.value for t in _tokens(WORDS)))
                self.assertEqual(text[new[1]['start']:new[1]['end']],WORDS)
                self.assertNotIn('следующий',new[1]['query'])

    def test_protected_citations_first_slot_and_smaller_allowances_stay_unchanged(self):
        text='Начало текста содержит отдельную вводную фразу.\nКлючевые слова: '+WORDS
        plan=[query(text,'Начало текста содержит отдельную вводную фразу'),query(text,' '.join(WORDS.split()[:8]))]
        self.assertEqual(complete_keyword_queries(text,plan,[plan[1]['query']]),plan)
        self.assertEqual(complete_keyword_queries(text,plan[1:]),plan[1:])
        for limit in range(1,6):self.assertEqual(complete_keyword_queries(text,plan,limit=limit),plan)

    def test_prose_short_complete_oversized_and_duplicate_fields_are_not_changed(self):
        for value in ('сосна берёза клён кедр липа пихта',' '.join('term'+str(i)for i in range(31)), 'longword '*50):
            text='Начало текста содержит отдельную вводную фразу.\nKeywords: '+value
            fragment=' '.join(value.split()[:6])
            plan=[query(text,'Начало текста содержит отдельную вводную фразу'),query(text,fragment)]
            self.assertEqual(complete_keyword_queries(text,plan),plan)
        text='Начало текста содержит отдельную вводную фразу. В тексте ключевые слова: '+WORDS
        plan=[query(text,'Начало текста содержит отдельную вводную фразу'),query(text,' '.join(WORDS.split()[:8]))]
        self.assertEqual(complete_keyword_queries(text,plan),plan)
        text+='\nKeywords: '+WORDS
        plan.append(query(text,WORDS))
        self.assertEqual(complete_keyword_queries(text,plan),plan)

    def test_only_one_slot_changes_without_mutating_metadata_or_using_network(self):
        text='Начало текста содержит отдельную вводную фразу.\nKeywords: '+WORDS+'\nКлючевые слова: '+WORDS+' мох папоротник'
        plan=[query(text,'Начало текста содержит отдельную вводную фразу'),query(text,' '.join(WORDS.split()[:8]))]
        plan[1]['metadata']={'nested':[1,2]};original=deepcopy(plan)
        with patch('socket.socket',side_effect=AssertionError('No network in planning')):
            new=complete_keyword_queries(text,plan)
        self.assertEqual(plan,original);self.assertEqual(sum(a!=b for a,b in zip(plan,new)),1)
        self.assertEqual(new[1]['metadata'],plan[1]['metadata']);self.assertIsNot(new[1]['metadata'],plan[1]['metadata'])

    def test_full_field_can_discover_a_source_with_same_primary_budget(self):
        text=('Авторский журнал описывает измерения в учебной мастерской. '*10
              +'\n\nКлючевые слова: '+WORDS+'\n\n'
              +'Другая группа обсуждала устройство прибора и записывала независимые наблюдения. '*10)
        old=plan_queries(text,strategy=CLAUSE_STRATEGY);new=plan_queries(text,strategy=KEYWORD_STRATEGY)
        self.assertEqual(len(old),len(new));self.assertLessEqual(len(new),6)
        changed=[b for a,b in zip(old,new)if a!=b];self.assertEqual(len(changed),1)
        selected=changed[0]['query']
        class Search:
            def __init__(self):self.calls=[]
            def search(self,phrase,request_id):
                self.calls.append((phrase,request_id))
                return [{'url':'https://example.org/source'}]if phrase==selected else []
        class Fetch:
            def fetch(self,url):return {'url':url,'title':'Artificial source','text':WORDS}
        first,second=Search(),Search()
        with patch('socket.socket',side_effect=AssertionError('Offline pipeline')):
            a=run(text,searcher=first,fetcher=Fetch(),query_strategy=CLAUSE_STRATEGY)
            b=run(text,searcher=second,fetcher=Fetch(),query_strategy=KEYWORD_STRATEGY)
        self.assertEqual(len(first.calls),len(second.calls))
        self.assertEqual(a['stats']['matched_words'],0)
        self.assertEqual(b['stats']['matched_words'],12)
        self.assertTrue(any(m['kind']=='exact'and m['excerpt']==WORDS for m in b['matches']))
        for limit in range(1,6):
            self.assertEqual(plan_queries(text,limit=limit,strategy=CLAUSE_STRATEGY),
                             plan_queries(text,limit=limit,strategy=KEYWORD_STRATEGY))


if __name__=='__main__':unittest.main()
