"""Input-only suffix discovery, with real matching and explicit I/O fixtures."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from plagiarism import query_sentences as sentences
from plagiarism.matching import _tokens
from plagiarism.web_search import WebSearchError, plan_queries, run


STRATEGY = 'hybrid-citation-v13-empty-nine-suffix-six'
FIRST = 'Небольшой серебристый спутник медленно облетает далёкую туманную ледяную планету'
SUFFIX = '"медленно облетает далекую туманную ледяную планету"'
SUFFIX_TEXT = 'медленно облетает далёкую туманную ледяную планету'
FILLER = (
    'Организаторы последовательно распределяли пронумерованные демонстрационные принадлежности между временными учебными подразделениями.',
    'Сотрудники предварительно документировали расположение экспериментальных декоративных конструкций внутри подготовительного выставочного пространства.',
    'Руководители независимо сопоставляли регистрационные обозначения индивидуальных экспозиционных элементов посредством вспомогательной инвентаризационной последовательности.',
    'Преподаватели тщательно переупорядочивали организационные карточки соответственно первоначальным рекомендациям ответственного технического руководителя.',
    'Участники постепенно восстанавливали декоративные перегородки между специально подготовленными исследовательскими демонстрационными секциями.',
    'Волонтёры аккуратно группировали разноцветные имитационные конструкции согласно заранее утверждённой образовательной регистрационной процедуре.',
    'Координаторы отдельно проверяли последовательность документирования распределённых принадлежностей внутри пронумерованных подготовительных организационных подразделений.',
)
TEXT = FIRST + '. ' + ' '.join(FILLER)
URL = 'https://example.org/article/synthetic-orbit'


class Search:
    provider = 'serpapi-yandex'

    def __init__(self, *, prior=None):
        self.prior, self.calls = prior, []

    def search(self, query, request_id):
        index = len(self.calls)
        self.calls.append((query, request_id))
        if index == 2:
            if self.prior == 'error':
                raise WebSearchError('Fixture timeout', 'search_provider_error')
            if self.prior == 'schema':
                return {'not': 'a result list'}
            if self.prior == 'unsupported':
                return [{'url': 'javascript:void(0)'}]
            if self.prior == 'url':
                return [{'url': URL}]
        return [{'url': URL}] if query == SUFFIX else []


class Fetch:
    def __init__(self, body=FIRST, *, blocked=False):
        self.body, self.blocked = body, blocked

    def fetch(self, url):
        if self.blocked:
            from plagiarism.web_fetch import FetchError
            raise FetchError('Fixture robots refusal', 'robots_denied')
        return {'url': url, 'text': self.body, 'title': 'Synthetic orbit'}


class Wiki:
    provider = 'mediawiki'

    def __init__(self):
        self.calls = []
        self.last_usage = None

    def search(self, query, request_id):
        self.calls.append(query)
        self.last_usage = {'public_queries': 1, 'cost_kzt': 0, 'cached': False}
        return []


def execute(strategy=STRATEGY, *, text=TEXT, search=None, fetch=None, wiki=None):
    return run(text, request_id='synthetic-nine-suffix', searcher=search or Search(),
               fetcher=fetch or Fetch(), query_strategy=strategy,
               use_wiki_followup=wiki is not None, wiki_searcher=wiki,
               use_related_fallback=False)


class EmptyNineTests(unittest.TestCase):
    def test_v11_misses_but_suffix_recovers_nine_with_same_six_requests(self):
        old, new = Search(), Search()
        baseline = execute(sentences.SHORT_PURE_STRATEGY, search=old)
        candidate = execute(search=new)
        self.assertEqual(baseline['stats']['matched_words'], 0)
        self.assertEqual(candidate['stats']['matched_words'], 9)
        self.assertEqual([q for q, _ in old.calls[:5]], [q for q, _ in new.calls[:5]])
        self.assertEqual(len(new.calls), 6)
        self.assertEqual(new.calls[-1][0], SUFFIX)
        self.assertEqual(candidate['web']['query_strategy'], STRATEGY)
        audit = candidate['web']['input_query_replacements'][-1]
        self.assertEqual(audit['query_words'], 6)
        self.assertEqual(audit['sentence_words'], 9)
        self.assertEqual(TEXT[audit['whole_sentence_span']['start']:audit['whole_sentence_span']['end']], FIRST)
        self.assertEqual(TEXT[audit['selected_span']['start']:audit['selected_span']['end']], SUFFIX_TEXT)
        self.assertEqual(candidate['web']['query_spans'][-1], audit['selected_span'])
        for match in candidate['matches']:
            if match['kind'] == 'exact':
                self.assertEqual(match['excerpt'], match['source_excerpt'])

    def test_static_plans_and_non_suffix_lengths_keep_v11(self):
        for limit in range(1, 7):
            self.assertEqual(plan_queries(TEXT, limit=limit, strategy=STRATEGY),
                             plan_queries(TEXT, limit=limit, strategy=sentences.SHORT_PURE_STRATEGY))
        for length in (6, 7, 8):
            text = ' '.join(FIRST.split()[:length]) + '. ' + ' '.join(FILLER)
            left, right = Search(), Search()
            execute(sentences.SHORT_PURE_STRATEGY, text=text, search=left)
            execute(text=text, search=right)
            self.assertEqual(left.calls, right.calls)

    def test_successful_prior_url_or_any_prior_error_keeps_v11_final(self):
        for prior in ('url', 'error', 'schema', 'unsupported'):
            with self.subTest(prior=prior):
                left, right = Search(prior=prior), Search(prior=prior)
                execute(sentences.SHORT_PURE_STRATEGY, search=left)
                report = execute(search=right)
                self.assertEqual(left.calls, right.calls)
                self.assertNotEqual(right.calls[-1][0], SUFFIX)
                self.assertLessEqual(len(right.calls), 6)
                self.assertFalse(any(r.get('query_words') == 6 for r in report['web']['input_query_replacements']))

    def test_fetch_body_and_robots_are_still_required_for_evidence(self):
        for fetch in (Fetch(blocked=True), Fetch('Совершенно другое содержание независимой опубликованной страницы.')):
            report = execute(fetch=fetch)
            self.assertEqual(report['stats']['matched_words'], 0)
            self.assertIsNone(report['stats']['uniqueness_percent'])

    def test_non_yandex_and_protected_fields_preserve_queries(self):
        for text in (TEXT, TEXT + ' Ключевые слова: внутренний архив, номера карточек, контроль подписей, сверка данных.'):
            left, right = Search(), Search()
            left.provider = right.provider = 'non-yandex-fixture'
            execute(sentences.SHORT_PURE_STRATEGY, text=text, search=left)
            execute(text=text, search=right)
            self.assertEqual(left.calls, right.calls)
        text = TEXT + ' Ключевые слова: внутренний архив, номера карточек, контроль подписей, сверка данных.'
        left, right = Search(), Search()
        execute(sentences.SHORT_PURE_STRATEGY, text=text, search=left)
        execute(text=text, search=right)
        protected = {q['query'] for q in plan_queries(text, strategy=sentences.SHORT_PURE_STRATEGY)
                     if not q['query'].startswith('"')}
        for query in protected:
            self.assertIn(query, [q for q, _ in right.calls])

    def test_original_unicode_offsets_and_limits(self):
        text = '🙂 ' + TEXT
        plan = plan_queries(text, strategy=sentences.SHORT_PURE_STRATEGY)
        before = deepcopy(plan)
        result = sentences.short_suffix_candidate(text, plan)
        self.assertEqual(result['query'], SUFFIX)
        self.assertEqual(text[result['start']:result['end']], SUFFIX_TEXT)
        self.assertEqual(text[result['whole_sentence_span']['start']:result['whole_sentence_span']['end']], FIRST)
        self.assertEqual(plan, before)
        for limit in range(1, 6):
            self.assertIsNone(sentences.short_suffix_candidate(text, plan, limit=limit))
        self.assertIsNone(sentences.short_suffix_candidate(text * 4, plan))
        self.assertIsNone(sentences.short_suffix_candidate(text, plan, [plan[-1]['query']]))
        self.assertIsNone(sentences.short_suffix_candidate(text, plan[:5]))

    def test_actual_wiki_caller_receives_modified_suffix_slot(self):
        wiki = Wiki()
        seen = []

        def select(text, primary_plan, **kwargs):
            seen.append(deepcopy(primary_plan))
            return [{**primary_plan[-1], 'primary_query_index': 5}]

        with patch('plagiarism.web_wiki_followup.select_post_queries', side_effect=select):
            report = execute(fetch=Fetch('Независимый материал содержит исключительно посторонние описания.'), wiki=wiki)
        self.assertEqual(seen[0][-1]['query'], SUFFIX)
        self.assertEqual(wiki.calls, [SUFFIX])
        span = {key: seen[0][-1][key] for key in ('start', 'end')}
        self.assertEqual(report['web']['primary_query_spans'][-1], span)
        self.assertEqual(report['web']['query_spans'][:6][-1], span)
        self.assertEqual(len(_tokens(TEXT[span['start']:span['end']])), 6)

    def test_audit_roundtrip_and_opt_in_default_is_unchanged(self):
        report = json.loads(json.dumps(execute(), ensure_ascii=False))
        self.assertEqual(report['web']['input_query_policy'], 'empty-nine-suffix-six-v1')
        self.assertIn('whole_sentence_span', report['web']['input_query_replacements'][-1])
        from plagiarism import web_search
        with patch.object(web_search, 'selected_provider', return_value='serpapi-yandex'):
            self.assertEqual(web_search.status()['query_strategy'], sentences.SHORT_PURE_STRATEGY)
