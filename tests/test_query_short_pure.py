from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from plagiarism.matching import _tokens
from plagiarism.query_sentences import (SHORT_CENTER_STRATEGY, SHORT_PURE_STRATEGY,
    INPUT_QUERY_POLICY, short_pure_candidate)
from plagiarism.query_keywords import KEYWORD_LINK_POLICY
from plagiarism.publication_links import POLICY as PUBLICATION_LINK_POLICY
from plagiarism.web_search import _citation_queries, plan_queries, run
from plagiarism.web_fetch import FetchError


SCENE = ('На столе стояли коробки разного цвета и размера. '
    'Участники молча рисовали план маленького сказочного города. '
    'Справа лежали пустые папки и деревянные рамки. '
    'Дети собирали домики из плотной разноцветной бумаги. '
    'В углу висели фотографии старой железнодорожной станции. '
    'Мастер осторожно раскладывал чистые кисточки вдоль стены. '
    'Рядом с окном сидела маленькая плюшевая собака. '
    'Группа проверяла расположение незаполненных клеток большой таблицы. '
    'Потом организаторы сложили цветные карточки внутрь конверта. '
    'После обеда все участники спокойно разошлись домой.')
FIRST = 'На столе стояли коробки разного цвета и размера'
PHRASE = '"на столе стояли коробки разного цвета и размера"'


class Search:
    provider = 'serpapi-yandex'

    def __init__(self, *, snippet=False, title=None):
        self.calls, self.snippet, self.title = [], snippet, title

    def search(self, query, request_id):
        self.calls.append(query)
        if query == PHRASE or self.snippet or self.title:
            row = {'url': 'https://example.org/article/paper'}
            if self.snippet:
                row['snippet'] = FIRST
            if self.title:
                row['title'] = self.title
            return [row]
        return []


class Fetch:
    def __init__(self, body=FIRST, blocked=False):
        self.body, self.blocked = body, blocked

    def fetch(self, url):
        if self.blocked:
            raise FetchError('Blocked fixture', 'robots_denied')
        return {'url': url, 'text': self.body, 'title': 'Fetched fixture'}


class Wiki:
    provider = 'mediawiki'

    def __init__(self):
        self.calls = []

    def search(self, query, request_id):
        self.calls.append(query)
        return []


def execute(strategy=SHORT_PURE_STRATEGY, *, search=None, fetch=None, wiki=None, text=SCENE):
    return run(text, request_id='pure-fixture', searcher=search or Search(),
        fetcher=fetch or Fetch(), query_strategy=strategy,
        use_wiki_followup=wiki is not None, wiki_searcher=wiki,
        use_related_fallback=False)


class ShortPureTests(unittest.TestCase):
    def test_actual_v10_gap_is_reachable_and_plan_is_unchanged(self):
        old = plan_queries(SCENE, strategy=SHORT_CENTER_STRATEGY)
        before = deepcopy(old)
        candidate = short_pure_candidate(SCENE, old)
        self.assertEqual(candidate, {'query': PHRASE, 'start': 0, 'end': 47,
            'covered_words': 0, 'sentence_words': 8})
        self.assertEqual(old, before)
        for limit in range(1, 7):
            self.assertEqual(plan_queries(SCENE, limit=limit, strategy=SHORT_PURE_STRATEGY),
                             plan_queries(SCENE, limit=limit, strategy=SHORT_CENTER_STRATEGY))

    def test_dispatch_recovers_eight_physical_words_with_six_calls(self):
        old, new = Search(), Search()
        a = execute(SHORT_CENTER_STRATEGY, search=old)
        b = execute(search=new)
        self.assertEqual(a['stats']['matched_words'], 0)
        self.assertEqual(b['stats']['matched_words'], 8)
        self.assertEqual(old.calls[:5], new.calls[:5])
        self.assertEqual(len(new.calls), 6)
        self.assertEqual(new.calls[-1], PHRASE)
        self.assertEqual(b['web']['input_query_policy'], INPUT_QUERY_POLICY)
        self.assertEqual(len(b['web']['input_query_replacements']), 1)
        self.assertNotIn('input_query_policy', a['web'])
        self.assertEqual(b['web']['source_followup_policy'], KEYWORD_LINK_POLICY)
        self.assertEqual(b['web']['publication_link_policy'], PUBLICATION_LINK_POLICY)
        for match in b['matches']:
            if match['kind'] == 'exact':
                self.assertEqual(match['excerpt'], match['source_excerpt'])

    def test_changed_span_survives_actual_wiki_caller(self):
        search, wiki = Search(), Wiki()
        report = execute(search=search, wiki=wiki)
        self.assertEqual(len(search.calls), 6)
        self.assertTrue(wiki.calls)
        self.assertLessEqual(len(wiki.calls), 2)
        actual = deepcopy(plan_queries(SCENE, strategy=SHORT_CENTER_STRATEGY))
        actual[-1] = {'query': PHRASE, 'start': 0, 'end': 47}
        spans = [{k: q[k] for k in ('start', 'end')} for q in actual]
        self.assertEqual(report['web']['primary_query_spans'], spans)
        self.assertEqual(report['web']['query_spans'][:6], spans)
        self.assertEqual(report['stats']['matched_words'], 8)

    def test_perfect_snippet_without_matching_fetched_body_is_not_evidence(self):
        for fetch in (Fetch(blocked=True), Fetch('Посторонняя страница описывает другие явления и независимые наблюдения.')):
            with self.subTest(blocked=fetch.blocked):
                report = execute(search=Search(snippet=True), fetch=fetch)
                self.assertEqual(report['stats']['matched_words'], 0)
                self.assertFalse([m for m in report['matches'] if m['kind'] == 'exact'])
                self.assertIsNone(report['stats']['uniqueness_percent'])

    def test_non_yandex_explicit_strategy_does_not_change_requests(self):
        search = Search()
        search.provider = 'other-fixture'
        report = execute(search=search)
        self.assertEqual(search.calls, [q['query'] for q in plan_queries(SCENE, strategy=SHORT_CENTER_STRATEGY)])
        self.assertEqual(report['web']['input_query_replacements'], [])

    def test_final_protection_limits_and_large_input_are_noops(self):
        plan = plan_queries(SCENE, strategy=SHORT_CENTER_STRATEGY)
        self.assertIsNone(short_pure_candidate(SCENE, plan, [plan[-1]['query']]))
        for limit in range(1, 6):
            self.assertIsNone(short_pure_candidate(SCENE, plan, limit=limit))
        self.assertIsNone(short_pure_candidate(SCENE, plan[:5]))
        self.assertIsNone(short_pure_candidate(SCENE * 4, plan))
        unquoted = deepcopy(plan)
        unquoted[-1]['query'] = unquoted[-1]['query'].strip('"')
        self.assertIsNone(short_pure_candidate(SCENE, unquoted))

    def test_legacy_title_route_keeps_priority(self):
        # Existing short-plan eligibility is needed in addition to observed URL.
        text = ' '.join('слово' + str(i) for i in range(12))
        title = 'Архитектурное исследование распределенных вычислительных комплексов'
        search = Search(title=title)
        report = execute(text=text, search=search)
        self.assertTrue(report['web']['metadata_queries'])
        self.assertEqual(report['web']['input_query_replacements'], [])
        self.assertIn('архитектурное исследование', search.calls[-1])

    def test_citation_and_complete_keywords_keep_their_slots(self):
        text = SCENE + ' Ключевые слова: сверка подписей, номера архивов, защита данных, контроль доступа.'
        old = plan_queries(text, strategy=SHORT_CENTER_STRATEGY)
        search = Search()
        execute(text=text, search=search)
        protected = {q['query'] for q in _citation_queries(text)} | {q['query'] for q in old if not q['query'].startswith('"')}
        for index, query in enumerate(old):
            if index == 0 or query['query'] in protected:
                self.assertEqual(search.calls[index], query['query'])

    def test_whole_lengths_unicode_ties_and_duplicate_clues(self):
        for count in (6, 7, 8, 9):
            words = 'Ёлка липа сосна берёза клён кедр тополь рябина вяз'.split()[:count]
            text = '🙂 ' + ' '.join(words) + '. ' + ' '.join('наполнитель' + str(i) for i in range(20))
            tokens = _tokens(text)
            filler = tokens[count:count + 10]
            q = {'query': '"' + ' '.join(w.value for w in filler) + '"', 'start': filler[0].start, 'end': filler[-1].end}
            plan = [deepcopy(q) for _ in range(6)]
            candidate = short_pure_candidate(text, plan)
            self.assertEqual(candidate['sentence_words'], count)
            self.assertEqual(text[candidate['start']:candidate['end']], ' '.join(words))
            plan[2] = {k: candidate[k] for k in ('query', 'start', 'end')}
            self.assertIsNone(short_pure_candidate(text, plan))

    def test_optional_audit_roundtrips_without_exposing_source_text(self):
        from plagiarism.storage import Store
        from server import public_report
        with TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'store.sqlite3')
            saved = store.save('report', execute())
            restored = store.get('report', saved['id'])
            public = public_report(restored)
            self.assertNotIn('sources', public)
            self.assertEqual(json.loads(json.dumps(public))['web']['input_query_policy'], INPUT_QUERY_POLICY)
            self.assertEqual(restored['web']['input_query_replacements'], saved['web']['input_query_replacements'])

    def test_fraction_ranking_beats_raw_covered_count_and_ties_use_offset(self):
        first = 'Берёза клён кедр тополь рябина вяз'
        second = 'Сосна липа яблоня осина верба дуб каштан туя ель'
        text = first + '. ' + second + '. ' + ' '.join('наполнитель' + str(i) for i in range(20))
        words = _tokens(text)
        def query(a, b):
            return {'query': '"' + ' '.join(w.value for w in words[a:b]) + '"',
                    'start': words[a].start, 'end': words[b - 1].end}
        # 3/6 is greater than 4/9 despite having fewer covered words.
        plan = [query(3, 10)] + [query(15, 25) for _ in range(5)]
        candidate = short_pure_candidate(text, plan)
        self.assertEqual(text[candidate['start']:candidate['end']], second)
        self.assertEqual((candidate['covered_words'], candidate['sentence_words']), (4, 9))
        plan[0] = query(15, 25)
        candidate = short_pure_candidate(text, plan)
        self.assertEqual(text[candidate['start']:candidate['end']], first)

    def test_long_words_are_rejected_without_truncating_a_sentence(self):
        text = 'я' * 41 + ' липа сосна берёза клён кедр. ' + ' '.join('наполнитель' + str(i) for i in range(20))
        tokens = _tokens(text)
        filler = tokens[6:16]
        query = {'query': '"' + ' '.join(w.value for w in filler) + '"',
                 'start': filler[0].start, 'end': filler[-1].end}
        self.assertIsNone(short_pure_candidate(text, [query] * 6))


if __name__ == '__main__':
    unittest.main()
