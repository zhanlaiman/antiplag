"""A prior empty whole-sentence query must not suppress a shorter clue."""
import unittest

from plagiarism.matching import _tokens
from plagiarism.query_sentences import SHORT_PURE_STRATEGY, SHORT_SUFFIX_STRATEGY, short_suffix_candidate
from plagiarism.web_search import WebSearchError, plan_queries, run


NINE = ('Миниатюрная голографическая обсерватория последовательно регистрирует '
        'ультрафиолетовые межзвёздные экспериментальные сигналы')
SUFFIX = ('"последовательно регистрирует ультрафиолетовые межзвездные '
          'экспериментальные сигналы"')
CONTEXT = (
    'Утром мы опять поставили на стол все наши вещи и стали ждать начала игры.',
    'На другой день дети сидели на полу у окна и смотрели на пустые коробки.',
    'Потом кто то взял со стола лист бумаги и положил его к себе рядом.',
    'После этого мы стали тихо говорить о том как будем делать наш маленький дом.',
    'В конце дня все эти вещи были на своих местах а дети уже ушли.',
    'Никто из нас не знал что делать дальше и мы стали смотреть друг на друга.',
    'За окном был сильный дождь и мы снова решили что лучше всем остаться здесь.',
    'Пока мы ждали все наши друзья по очереди заходили в комнату и садились за стол.',
)
TEXT = ' '.join((*CONTEXT[:3], NINE + '.', *CONTEXT[3:]))
URL = 'https://example.org/article/invented-observatory'


class Search:
    provider = 'serpapi-yandex'

    def __init__(self, prior=None):
        self.calls, self.prior = [], prior

    def search(self, query, request_id):
        self.calls.append(query)
        if len(self.calls) == 2:
            if self.prior == 'error':
                raise WebSearchError('Synthetic timeout', 'search_provider_error')
            if self.prior == 'url':
                return [{'url': URL}]
        return [{'url': URL}] if query == SUFFIX else []


class Fetch:
    def __init__(self, body=NINE):
        self.body = body

    def fetch(self, url):
        return {'url': url, 'text': self.body, 'title': 'Invented fixture'}


def execute(strategy, search, *, text=TEXT, body=NINE):
    return run(text, request_id='covered-nine-fixture', query_strategy=strategy,
               searcher=search, fetcher=Fetch(body), use_wiki_followup=False,
               use_related_fallback=False)


class CoveredEmptyNineTests(unittest.TestCase):
    def test_empty_full_nine_query_can_recover_with_suffix_in_same_six_slots(self):
        # Native planner already puts the whole nine-word sentence in slot two.
        # Its successful empty reply is a reason to try a shorter clue, not
        # evidence that this input span no longer needs discovery.
        plan = plan_queries(TEXT, strategy=SHORT_SUFFIX_STRATEGY)
        self.assertEqual(len(_tokens(NINE)), 9)
        self.assertEqual(plan[1]['query'],
            '"миниатюрная голографическая обсерватория последовательно регистрирует '
            'ультрафиолетовые межзвездные экспериментальные сигналы"')
        old, new = Search(), Search()
        before = execute(SHORT_PURE_STRATEGY, old)
        after = execute(SHORT_SUFFIX_STRATEGY, new)
        self.assertEqual(before['stats']['matched_words'], 0)
        self.assertEqual(after['stats']['matched_words'], 9)
        self.assertEqual(old.calls[:5], new.calls[:5])
        self.assertEqual(len(old.calls), 6)
        self.assertEqual(len(new.calls), 6)
        self.assertEqual(new.calls[-1], SUFFIX)
        change = after['web']['input_query_replacements'][-1]
        span = change['whole_sentence_span']
        self.assertEqual(TEXT[span['start']:span['end']], NINE)
        self.assertEqual(len(_tokens(new.calls[-1])), 6)
        self.assertTrue(after['matches'])
        for match in after['matches']:
            self.assertEqual(match['excerpt'], match['source_excerpt'])

    def test_new_clue_does_not_turn_unrelated_or_five_word_page_into_evidence(self):
        for body in ('Независимый рассказ про мягкую игрушку рядом с окном.',
                     'Миниатюрная голографическая обсерватория последовательно регистрирует'):
            with self.subTest(body=body):
                search = Search()
                report = execute(SHORT_SUFFIX_STRATEGY, search, body=body)
                self.assertEqual(search.calls[-1], SUFFIX)
                self.assertEqual(report['stats']['matched_words'], 0)
                self.assertIsNone(report['stats']['uniqueness_percent'])

    def test_non_yandex_and_default_pure_policy_do_not_gain_suffix_dispatch(self):
        for provider in ('serper', 'exa', 'tavily'):
            with self.subTest(provider=provider):
                old, new = Search(), Search()
                old.provider = new.provider = provider
                execute(SHORT_PURE_STRATEGY, old)
                report = execute(SHORT_SUFFIX_STRATEGY, new)
                self.assertEqual(old.calls, new.calls)
                self.assertEqual(report['stats']['matched_words'], 0)
                self.assertNotIn(SUFFIX, new.calls)

    def test_existing_url_or_failed_reply_still_blocks_shortening(self):
        for prior in ('url', 'error'):
            with self.subTest(prior=prior):
                old, new = Search(prior), Search(prior)
                execute(SHORT_PURE_STRATEGY, old)
                report = execute(SHORT_SUFFIX_STRATEGY, new)
                self.assertEqual(old.calls, new.calls)
                self.assertNotIn(SUFFIX, new.calls)
                self.assertLessEqual(len(new.calls), 6)
                self.assertFalse(report['web']['input_query_replacements'])

    def test_existing_six_word_suffix_is_not_repeated(self):
        plan = plan_queries(TEXT, strategy=SHORT_SUFFIX_STRATEGY)
        # An existing exact suffix clue has already spent one of the six slots.
        words = _tokens(TEXT)
        start = TEXT.index('последовательно регистрирует')
        selected = [w for w in words if start <= w.start < start + len(NINE)]
        plan[1] = {'query': SUFFIX, 'start': start, 'end': selected[5].end}
        self.assertIsNone(short_suffix_candidate(TEXT, plan))

    def test_unattempted_final_pure_clue_is_preserved(self):
        plan = plan_queries(TEXT, strategy=SHORT_SUFFIX_STRATEGY)
        plan[1], plan[-1] = plan[-1], plan[1]
        self.assertIsNone(short_suffix_candidate(TEXT, plan))

    def test_mixed_lengths_preserve_ordinary_six_to_eight_word_candidate(self):
        # The seven-word arm is the independent SI1 native130-token repro.
        # All first five replies are empty; only the ordinary whole clue returns
        # a page. A nine-word sentence coexists and must not displace that clue.
        for length, short in (
            (6, 'После этого мы снова смотрели вокруг'),
            (7, 'После этого мы снова тихо смотрели вокруг'),
            (8, 'После этого мы снова очень тихо смотрели вокруг'),
        ):
            with self.subTest(length=length):
                text = short + '. ' + NINE + '. ' + ' '.join(CONTEXT)
                phrase = '"' + short.lower() + '"'
                body = short + '. Гравитационная калибровка выполнена сертифицированными наблюдателями спектральной лаборатории.'

                class WholeSearch(Search):
                    def search(self, query, request_id):
                        self.calls.append(query)
                        return [{'url': URL}] if query == phrase else []

                old, new = WholeSearch(), WholeSearch()
                before = execute(SHORT_PURE_STRATEGY, old, text=text, body=body)
                after = execute(SHORT_SUFFIX_STRATEGY, new, text=text, body=body)
                self.assertEqual(len(_tokens(text)), 123 + length)
                self.assertEqual(before['stats']['matched_words'], length)
                self.assertEqual(after['stats']['matched_words'], length)
                self.assertNotIn(phrase, old.calls[:5])
                self.assertEqual(old.calls, new.calls)
                self.assertEqual(len(new.calls), 6)
                self.assertEqual(new.calls[-1], phrase)
                self.assertEqual(after['web']['input_query_replacements'][-1]['reason'],
                                 'short_sentence_without_pure_query')
                self.assertTrue(after['matches'])
                for match in after['matches']:
                    self.assertEqual(match['excerpt'], short)
                    self.assertEqual(match['source_excerpt'], short)


if __name__ == '__main__':
    unittest.main()
