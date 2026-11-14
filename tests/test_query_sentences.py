from copy import deepcopy
import unittest
from unittest.mock import patch

from plagiarism.matching import _tokens
from plagiarism.query_keywords import KEYWORD_STRATEGY, KEYWORD_LINK_POLICY
from plagiarism.query_sentences import (SHORT_SENTENCE_STRATEGY, SHORT_CENTER_STRATEGY, TITLE_QUERY_POLICY,
                                        short_sentence_queries, short_center_queries, observed_title_query)
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

    def test_spare_slot_adds_middle_window_preserving_all_existing_queries(self):
        text='Первый второй третий четвертый пятый шестой седьмой восьмой девятый десятый одиннадцатый двенадцатый'
        old=[{'query':'"первый второй третий четвертый пятый шестой"','start':0,'end':len('Первый второй третий четвертый пятый шестой')}]
        before=deepcopy(old);new=short_center_queries(text,old)
        self.assertEqual(old,before);self.assertEqual(new[:-1],old)
        self.assertEqual(new[-1]['query'],'"четвертый пятый шестой седьмой восьмой девятый"')
        self.assertEqual(text[new[-1]['start']:new[-1]['end']], 'четвертый пятый шестой седьмой восьмой девятый')
        self.assertEqual(short_center_queries(text,new),new)
        self.assertEqual(short_center_queries(text,old*6),old*6)
        self.assertEqual(short_center_queries(text*20,old),old)
        for limit in range(1,6):self.assertEqual(short_center_queries(text,old,limit=limit),old)

    def test_pdf_hyphen_only_changes_search_clue_and_keeps_raw_offsets(self):
        text='Модель прогнози-\nрования экономического результата компании'
        result=short_center_queries(text,[])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['query'],'"модель прогнозирования экономического результата компании"')
        self.assertEqual(text[result[0]['start']:result[0]['end']],text)
        ordinary='Первая модель дву-мерного расчета результата нашей компании'
        self.assertIn('дву мерного',short_center_queries(ordinary,[])[0]['query'])

    def test_v10_keeps_v9_prefix_and_old_smaller_limits(self):
        for text in (self.text,self.text+' '+self.text):
            old=plan_queries(text,strategy=SHORT_SENTENCE_STRATEGY)
            new=plan_queries(text,strategy=SHORT_CENTER_STRATEGY)
            self.assertEqual([q for q in new if q in old],old)
            self.assertEqual(new[0],old[0])
            self.assertLessEqual(len(new),6)
            self.assertEqual(len(new),len({q['query']for q in new}))
            for limit in range(1,6):
                self.assertEqual(plan_queries(text,strategy=SHORT_CENTER_STRATEGY,limit=limit),
                                 plan_queries(text,strategy=SHORT_SENTENCE_STRATEGY,limit=limit))

    def test_observed_title_is_bounded_and_document_specific(self):
        title='Исследование кинетики биологического разложения...'
        self.assertEqual(observed_title_query('https://example.org/item.asp?id=17',title),
                         '"исследование кинетики биологического разложения"')
        for url,bad in [('https://example.org/',title),('https://[',title),('file:///paper.pdf',title),
                        ('https://example.org/paper.pdf','Скачать полный текст'),
                        ('https://example.org/paper.pdf',title+'\nсекрет'),
                        ('https://example.org/paper.pdf','а'*241)]:
            self.assertIsNone(observed_title_query(url,bad))

    def test_title_followup_uses_one_existing_slot_and_only_fetched_body_is_evidence(self):
        # This five-slot plan receives an extra center clue. A single blocked
        # catalogue record can then guide its final call to an accessible copy.
        text=('Участники учебной мастерской «Бумажный залив» собирали настенную композицию из картонных рамок и прозрачных листов разной толщины. '
              'На первой встрече они договорились обозначать красными точками отверстия крепления и оставлять зелёные полосы для временных пометок. '
              'мировое научное сообщество встревожено сообщениями о появлении микро- и наночастиц. '
              'Затем организатор раздал новые листы и попросил повторить сборку по словесному описанию, не глядя на готовый образец. '
              'В журнале сохранили обе схемы, чтобы следующая группа могла сравнить порядок действий и предложить собственное расположение деталей.')
        plan=plan_queries(text,strategy=SHORT_CENTER_STRATEGY)
        self.assertEqual(len(plan),6)
        title='Исследование кинетики биологического разложения...'
        title_query=observed_title_query('https://example.org/item.asp?id=17',title)
        class Search:
            def __init__(self):self.calls=[]
            def search(self,q,rid):
                self.calls.append(q)
                if q==plan[3]['query']:return [{'url':'https://example.org/item.asp?id=17','title':title}]
                if q==title_query:return [{'url':'https://example.net/article/copy'}]
                return []
        class Fetch:
            def __init__(self,body):self.body=body
            def fetch(self,url):
                if url.startswith('https://example.org'):
                    from plagiarism.web_search import WebSearchError
                    raise WebSearchError('Blocked','robots_denied')
                return {'url':url,'title':title,'text':self.body}
        search=Search();result=run(text,searcher=search,fetcher=Fetch('мировое научное сообщество встревожено сообщениями о появлении микро- и наночастиц'),query_strategy=SHORT_CENTER_STRATEGY)
        self.assertEqual(len(search.calls),6)
        self.assertEqual(search.calls[-1],title_query)
        self.assertEqual(result['stats']['matched_words'],10)
        self.assertEqual(result['web']['metadata_query_policy'],TITLE_QUERY_POLICY)
        self.assertEqual(len(result['web']['metadata_queries']),1)
        self.assertIsNone(result['stats']['uniqueness_percent'])
        false=run(text,searcher=Search(),fetcher=Fetch('Совершенно постороннее содержимое без слов введённой научной вставки'),query_strategy=SHORT_CENTER_STRATEGY)
        self.assertEqual(false['stats']['matched_words'],0)


if __name__=='__main__':unittest.main()
