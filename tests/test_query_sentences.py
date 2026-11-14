from copy import deepcopy
import unittest
from unittest.mock import patch

from plagiarism.matching import _tokens
from plagiarism.query_keywords import KEYWORD_STRATEGY, KEYWORD_LINK_POLICY
from plagiarism.query_sentences import SHORT_SENTENCE_STRATEGY, short_sentence_queries
from plagiarism.web_search import _citation_queries, plan_queries, run


class ShortSentenceTests(unittest.TestCase):
    def setUp(self):
        self.text = ('В начале занятия участники собрали вымышленную мастерскую для обсуждения правил хранения карточек. '
                     'На длинном столе стояли цветные коробки и пронумерованные деревянные рамки. '
                     '🙂 Ёлка липа сосна берёза клён кедр тополь рябина. '
                     'В завершение организаторы проверяли заполнение карточек и расположение всех пустых коробок. '
                     'После занятия мастерскую разобрали и передали следующей группе участников.')

    def test_covers_six_word_prefix_with_unicode_offsets_and_keeps_count(self):
        old = plan_queries(self.text, strategy=KEYWORD_STRATEGY)
        original = deepcopy(old)
        # Supply an explicit redundant pair to isolate coverage from rarity ranking.
        words = _tokens(self.text)
        old = [{"query": '"'+' '.join(w.value for w in words[a:b])+'"',
                "start": words[a].start, "end": words[b-1].end}
               for a,b in ((0,10),(1,11),(12,22),(13,23),(35,45),(36,46))]
        before = deepcopy(old)
        with patch('socket.socket', side_effect=AssertionError('Offline planner')):
            new = short_sentence_queries(self.text, old, ())
        self.assertEqual(old, before)
        self.assertEqual(len(new), len(old))
        self.assertEqual(len({q['query'] for q in new}),len(new))
        changed=[q for a,q in zip(old,new) if a!=q]
        self.assertEqual(len(changed),1)
        q=changed[0]
        self.assertEqual(self.text[q['start']:q['end']], 'Ёлка липа сосна берёза клён кедр')
        self.assertEqual(q['query'], '"елка липа сосна береза клен кедр"')
        self.assertEqual(new[0],old[0])
        self.assertEqual(plan_queries(self.text,strategy=KEYWORD_STRATEGY),original)

    def test_protected_slots_smaller_allowance_and_no_redundancy(self):
        plan=plan_queries(self.text,strategy=KEYWORD_STRATEGY)
        self.assertEqual(short_sentence_queries(self.text,plan,(q['query']for q in plan)),plan)
        for limit in range(1,6):
            self.assertEqual(plan_queries(self.text,limit=limit,strategy=KEYWORD_STRATEGY),
                             plan_queries(self.text,limit=limit,strategy=SHORT_SENTENCE_STRATEGY))
        self.assertEqual(short_sentence_queries(self.text,plan[:1],()),plan[:1])

    def test_keyword_metadata_and_citations_survive(self):
        text = (self.text + ' Ключевые слова: сохранение данных, проверка подписей, контроль версий, восстановление записей. '
                'В журнале записали «Каждая карточка должна сохранять номер исходного документа и дату изменения».')
        old=plan_queries(text,strategy=KEYWORD_STRATEGY)
        new=plan_queries(text,strategy=SHORT_SENTENCE_STRATEGY)
        for q in old:
            if q in _citation_queries(text) or not q['query'].startswith('"'):
                self.assertIn(q,new)
        self.assertEqual(new[0],old[0])
        self.assertEqual(len(new),len(old))
        self.assertLessEqual(sum(a!=b for a,b in zip(old,new)),1)

    def test_runner_fetches_full_evidence_without_lowering_match_threshold(self):
        source='имеет негативные последствия для природы, а деградация природы в'
        text=('Во время пробной деловой игры участники придумали артель «Лиловый маршрут», которая обменивала жетоны на время работы мастерской. '
              'В первом раунде бухгалтер записал расходы на три вида бумаги и аренду двух складных столов. '
              'Второй раунд проводили без продаж, чтобы отдельно проверить перенос остатков между карточками. '+source+'. '
              'В заключительной таблице организаторы различали учебные начисления и настоящие денежные операции. '
              'Жетоны существовали только внутри игры, их нельзя было получить за реальные деньги. '
              'После занятия участники свернули плакаты и оставили пустой бланк для следующей группы, не перенося в него предыдущие ответы.')
        old=plan_queries(text,strategy=KEYWORD_STRATEGY)
        new=plan_queries(text,strategy=SHORT_SENTENCE_STRATEGY)
        changed=[q for q in new if q not in old]
        self.assertTrue(changed)
        phrase=changed[0]['query']
        class Search:
            def search(self,q,rid):return [{'url':'https://example.org/source'}] if q==phrase else []
        class Fetch:
            def fetch(self,url):return {'url':url,'title':'Fetched full text','text':source}
        a=run(text,searcher=Search(),fetcher=Fetch(),query_strategy=KEYWORD_STRATEGY)
        b=run(text,searcher=Search(),fetcher=Fetch(),query_strategy=SHORT_SENTENCE_STRATEGY)
        self.assertEqual(a['stats']['matched_words'],0)
        self.assertEqual(b['stats']['matched_words'],9)
        self.assertEqual(b['web']['source_followup_policy'],KEYWORD_LINK_POLICY)
        self.assertEqual(a['web']['queries_attempted'],b['web']['queries_attempted'])
        for m in b['matches']:
            if m['kind']=='exact':self.assertEqual(m['excerpt'],m['source_excerpt'])


if __name__=='__main__':unittest.main()
