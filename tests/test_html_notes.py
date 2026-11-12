"""Authored HTML-note guards and fake-wire Fetcher integration; no live I/O."""
from hashlib import sha256
import unittest
from unittest.mock import patch

from plagiarism import html_notes, web_fetch
from plagiarism.matching import analyze
import test_web_fetch as wire_fixtures


class _HTMLProbe:
    """Test the pure mask separately from Fetcher's minimum-text policy."""
    FetchError = web_fetch.FetchError
    NOTE_LINK = html_notes.NOTE_LINK
    PROTECTED_ANCESTORS = html_notes.PROTECTED_ANCESTORS

    @staticmethod
    def extract(source):
        parser = web_fetch._TextExtractor()
        parser.feed(source)
        parser.close()
        return {"text": web_fetch._normalize_whitespace("".join(parser.pieces)),
                "title": web_fetch._normalize_whitespace("".join(parser.title)),
                "links": parser.links, "nodes": parser.nodes,
                "max_final_depth": len(parser.stack)}

    @staticmethod
    def mask_semantic_notes(source):
        masked, ranges, _ = html_notes.mask_semantic_notes(source, void_tags=web_fetch._TextExtractor.VOID)
        return masked, ranges

    @classmethod
    def compare(cls, source):
        before = cls.extract(source)
        masked, ranges, audit = html_notes.mask_semantic_notes(source, void_tags=web_fetch._TextExtractor.VOID)
        after = cls.extract(masked)
        after["title"], after["links"] = before["title"], before["links"][:]
        return {"before": before, "after": after, "masked_ranges": ranges, "ancestor_audit": audit}


probe = _HTMLProbe()
LINK = "<a href='#cite_note-1'>[1]</a>"
UNICODE_LINK = "<a href='#cite_note-автоссылка_1:2.3'>[1]</a>"
NOTE = "<sup class='reference'><a href='#cite_note-авто1'>[1]</a></sup>"
BODY = "Астроном подготовил независимые наблюдения далёкого созвездия и записал время каждого измерения."


TARGET = 'дней. Датой изобретения технологии по решению IX Международного конгресса научной'

def note(n):
    return f'<sup class="reference" id="cite_ref-{n}"><a href="#cite_note-{n}">[{n}]</a></sup>'

def words(text):
    return analyze(TARGET, [{'id': 'authored', 'title': 'Fixture', 'url': 'https://example.test/source', 'text': text}])

class SemanticFootnoteDraftTests(unittest.TestCase):

    def test_authored_reference_markup_reproduces_one_plus_nine_then_ten(self):
        html = '<title>History</title><p>дней' + note(1) + note(2) + '. Датой изобретения технологии по решению IX Международного конгресса научной</p>'
        c = probe.compare(html)
        before, after = (words(c['before']['text']), words(c['after']['text']))
        self.assertEqual(sum((len(pieces['excerpt'].split()) for pieces in before['matches'] if pieces['kind'] == 'exact')), 9)
        self.assertEqual(len(after['matches']), 1)
        self.assertEqual(after['matches'][0]['kind'], 'exact')
        self.assertEqual(len(after['matches'][0]['excerpt'].split()), 10)
        self.assertEqual(len(c['masked_ranges']), 2)
        self.assertEqual(c['before']['title'], c['after']['title'])

    def test_plain_numbers_math_and_equation_links_are_untouched(self):
        for body in ('<p>Матрицы [1][2] обозначают индексы.</p>', '<p>Матрица<sup>[1][2]</sup> является диагональной.</p>', '<p><sup class="reference">[1][2]</sup> без семантической ссылки.</p>', '<p><sup class="reference"><a href="#equation-1">[1][2]</a></sup> математические индексы.</p>'):
            c = probe.compare(body)
            self.assertEqual(c['masked_ranges'], [])
            self.assertEqual(c['before'], c['after'])

    def test_quotes_words_and_reference_bibliography_are_kept(self):
        for body in ('<blockquote>«[1][2] — буквальная цитата из учебника»</blockquote>', '<sup class="reference"><a href="#cite_note-1">Дословная цитата со словами</a></sup>', '<a href="#cite_note-1">Читателю нужны эти слова цитаты</a>', '<ol class="references"><li id="cite_note-1">Текст цитируемого источника [1][2].</li></ol>'):
            c = probe.compare(body)
            self.assertEqual(c['masked_ranges'], [])
            self.assertEqual(c['before']['text'], c['after']['text'])

    def test_only_local_semantic_marker_links_and_bounded_marker_text(self):
        c = probe.compare('<p>слово<a href="#cite_note-1">&#91;1&#93;</a> следующее</p>')
        self.assertEqual(len(c['masked_ranges']), 1)
        self.assertEqual(c['after']['text'], 'слово следующее')
        for body in ('<a href="https://example.test/#cite_note-1">[1]</a>', '<sup class="reference"><a href="#cite_note-1">[1]</a><math>[2]</math></sup>', '<a href="#cite_note-1">[1]' + ' ' * 70 + 'Нельзя потерять эти слова</a>', '<a href="#cite_note-1">' + '[1]' * 30 + '</a>'):
            self.assertEqual(probe.compare(body)['masked_ranges'], [])

    def test_missing_or_mismatched_closure_never_swallows_following_body(self):
        for html in ('<p>Обычный текст <sup class="reference"><a href="#cite_note-1">[1] Далее важная цитата.</p>', '<p>Обычный текст <sup class="reference"><a href="#cite_note-1">[1]</sup> Далее важная цитата.</p>'):
            c = probe.compare(html)
            self.assertEqual(c['masked_ranges'], [])
            self.assertEqual(c['before']['text'], c['after']['text'])

    def test_current_snapshot_offsets_and_html_character_positions_remain_grounded(self):
        html = '<p>дней' + note(1) + note(2) + '. Датой изобретения технологии по решению IX Международного конгресса научной</p>'
        masked, ranges = probe.mask_semantic_notes(html)
        self.assertEqual(len(masked), len(html))
        for r in ranges:
            self.assertEqual(len(masked[r['start']:r['end']]), r['end'] - r['start'])
        source = probe.extract(masked)['text']
        result = words(source)
        for m in result['matches']:
            self.assertEqual(TARGET[m['start']:m['end']], m['excerpt'])
            self.assertEqual(source[m['source_start']:m['source_end']], m['source_excerpt'])

    def test_two_independent_same_topic_controls_do_not_gain_exact_matches(self):
        for target, source in (('Историк изучил развитие фотографической техники и описал устройство старого фотоаппарата для музейного каталога.', '<p>Сотрудница музея датировала портрет по бумаге' + note(1) + ' и сравнила подпись мастера с архивной карточкой.</p>'), ('Фотограф вычислил продолжительность экспозиции и подготовил новый снимок площади при вечернем освещении.', '<p>В лаборатории изучали чувствительность эмульсии' + note(2) + ' и измеряли температуру раствора после проявления.</p>')):
            c = probe.compare(source)
            for label in ('before', 'after'):
                result = analyze(target, [{'id': 'authored', 'title': 'Same topic control', 'url': 'https://example.test/control', 'text': c[label]['text']}])
                self.assertEqual([m for m in result['matches'] if m['kind'] == 'exact'], [])

    def test_hidden_content_optional_endings_and_non_note_links_are_preserved(self):
        html = '<div hidden>Скрытый текст ' + note(1) + '</div><ul><li>Первый пункт' + note(2) + '<li>Второй пункт</ul><p><a href="/article">Текст основной статьи</a></p>'
        c = probe.compare(html)
        self.assertNotIn('Скрытый', c['after']['text'])
        self.assertIn('Второй пункт', c['after']['text'])
        keep = lambda links: [l for l in links if not probe.NOTE_LINK.fullmatch(l['url'])]
        self.assertEqual(keep(c['before']['links']), keep(c['after']['links']))
        with self.assertRaises(probe.FetchError):
            probe.compare('<div>' * 129 + 'body' + '</div>' * 129)

class AncestorProtectionTests(unittest.TestCase):

    def test_all_nine_literal_ancestor_contexts_keep_bracket_examples(self):
        for tag in sorted(probe.PROTECTED_ANCESTORS):
            with self.subTest(tag=tag):
                html = '<div><' + tag + '><span>Пример ' + LINK + ' сохранён.</span></' + tag + '></div>'
                c = probe.compare(html)
                self.assertEqual(c['masked_ranges'], [])
                self.assertEqual(c['before'], c['after'])
                self.assertFalse(c['ancestor_audit']['ancestry_uncertain'])

    def test_protected_parent_ends_then_ordinary_reference_still_removed(self):
        html = '<div><code>Пример ' + LINK + '</code><p>слово' + LINK + ' следующее</p></div>'
        c = probe.compare(html)
        self.assertEqual(len(c['masked_ranges']), 1)
        self.assertIn('Пример [1]', c['after']['text'])
        self.assertTrue(c['after']['text'].endswith('слово следующее'))

    def test_malformed_or_unclosed_ancestry_preserves_even_completed_notes(self):
        for html in ('<p>слово' + LINK + ' следующее</p><code>незакрытый контекст', '<div><code>пример</div><p>слово' + LINK + ' следующее</p>', '<p>слово' + LINK + ' следующее</p></unknown>', '<div><p>слово' + LINK + ' следующее</p>', '<code/><p>слово' + LINK + ' следующее</p>', '<ul><li>слово' + LINK + '<li>Второй пункт</ul>'):
            with self.subTest(html=html):
                c = probe.compare(html)
                self.assertTrue(c['ancestor_audit']['ancestry_uncertain'])
                self.assertEqual(c['masked_ranges'], [])
                self.assertEqual(c['before'], c['after'])

    def test_void_nodes_keep_known_ancestry_and_non_note_links(self):
        c = probe.compare("<p>слово<br/><img src='/pixel'/>" + LINK + " следующее <a href='/article'>статья</a></p>")
        self.assertFalse(c['ancestor_audit']['ancestry_uncertain'])
        self.assertEqual(len(c['masked_ranges']), 1)
        self.assertIn({'url': '/article', 'text': 'статья'}, c['after']['links'])

    def test_relative_url_and_non_marker_semantics_remain_out_of_scope(self):
        for html in ("<p>слово<a href='./Article#cite_note-1'>[1]</a> следующее</p>", "<p>слово<sup class='reference'><a href='./Article#cite_note-1'>[1]</a></sup> следующее</p>", "<p>слово<a href='#cite_ref-1'>[1]</a> следующее</p>", "<p>слово<a href='#cite_note-1'>цитата [1]</a> следующее</p>"):
            with self.subTest(html=html):
                c = probe.compare(html)
                self.assertEqual(c['masked_ranges'], [])
                self.assertEqual(c['before'], c['after'])

class UnicodeAndInventoryTests(unittest.TestCase):

    def test_only_unicode_word_id_characters_are_added(self):
        for suffix in ('автоссылка1-63', 'école_1:2.3', '引用_2', 'ascii-1'):
            with self.subTest(suffix=suffix):
                c = probe.compare("<p>слово<a href='#cite_note-" + suffix + "'>[1]</a> следующее</p>")
                self.assertEqual(len(c['masked_ranges']), 1)
                self.assertEqual(c['after']['text'], 'слово следующее')

    def test_unicode_notes_inside_all_nine_protected_contexts_are_kept(self):
        for tag in sorted(probe.PROTECTED_ANCESTORS):
            with self.subTest(tag=tag):
                c = probe.compare('<div><' + tag + '>Пример ' + UNICODE_LINK + '</' + tag + '></div>')
                self.assertEqual(c['masked_ranges'], [])
                self.assertEqual(c['before'], c['after'])

    def test_percent_relative_external_and_invalid_id_characters_are_not_added(self):
        for href in ('#cite_note-%D0%B0', './Article#cite_note-авто', '/Article#cite_note-авто', 'https://example.test/#cite_note-авто', '#cite_note-авто/name', '#cite_note-авто name', '#cite_note-авто@name', '#cite_note-авто\u200dname'):
            with self.subTest(href=href):
                c = probe.compare("<p>слово<a href='" + href + "'>[1]</a> следующее</p>")
                self.assertEqual(c['masked_ranges'], [])
                self.assertEqual(c['before'], c['after'])

    def test_unicode_does_not_change_marker_text_or_uncertain_ancestry_rules(self):
        for html in ("<p>слово<a href='#cite_note-авто'>[примечание 1]</a> следующее</p>", "<p>слово<a href='#cite_note-авто'>" + '[1]' * 30 + '</a> следующее</p>', '<div><p>слово' + UNICODE_LINK + ' следующее</p>', '<code/><p>слово' + UNICODE_LINK + ' следующее</p>'):
            with self.subTest(html=html):
                c = probe.compare(html)
                self.assertEqual(c['masked_ranges'], [])
                self.assertEqual(c['before'], c['after'])

    def test_old_title_and_1024_capped_link_inventory_are_exactly_retained(self):
        html = '<title>Исходный заголовок</title><div>' + UNICODE_LINK * 1024 + "<a href='/late-category'>Поздняя ссылка</a></div>"
        c = probe.compare(html)
        self.assertEqual(len(c['before']['links']), 1024)
        self.assertEqual(c['after']['links'], c['before']['links'])
        self.assertEqual(c['after']['title'], c['before']['title'])
        self.assertNotIn({'url': '/late-category', 'text': 'Поздняя ссылка'}, c['after']['links'])
        self.assertEqual(len(c['masked_ranges']), 1024)

def literal_controls():
    link = "<a href='#cite_note-1'>[1]</a>"
    rows = []
    for tag in ('code', 'pre', 'math', 'kbd'):
        rows.append({'id': 'literal_' + tag, 'html': '<p>Буквальный пример <' + tag + '>' + link + '</' + tag + '> сохранён.</p>', 'expect': 'preserve_literal', 'required_counterexample': True})
    for tag in ('q', 'blockquote'):
        rows.append({'id': 'literal_quote_' + tag, 'html': '<p>Дословно цитируется обозначение <' + tag + '>' + link + '</' + tag + '>.</p>', 'expect': 'preserve_literal', 'required_counterexample': False})
    rows.extend([{'id': 'escaped_code_example', 'html': '<code>&lt;a href="#cite_note-1"&gt;[1]&lt;/a&gt;</code>', 'expect': 'preserve_literal'}, {'id': 'self_closed_anchor', 'html': "<p>Пример <a href='#cite_note-1'/>[1] конец.</p>", 'expect': 'preserve_literal'}, {'id': 'unclosed_anchor', 'html': "<p>Пример <a href='#cite_note-1'>[1] продолжение цитаты.</p>", 'expect': 'preserve_literal'}, {'id': 'unclosed_sup_anchor', 'html': "<p>Пример <sup class='reference'><a href='#cite_note-1'>[1] конец.</p>", 'expect': 'preserve_literal'}, {'id': 'script_literal', 'html': '<script>const s="' + link + '";</script><p>Видимый текст.</p>', 'expect': 'preserve_visible'}, {'id': 'style_literal', 'html': '<style>.x:after {content:"' + link + '";}</style><p>Видимый текст.</p>', 'expect': 'preserve_visible'}, {'id': 'long_marker', 'html': "<p><a href='#cite_note-1'>[" + '1' * 70 + ']</a> длинный индекс.</p>', 'expect': 'preserve_literal'}, {'id': 'bibliography_backlink', 'html': "<ol class='references'><li id='cite_note-1'><a href='#cite_ref-1'>[1]</a> Текст источника.</li></ol>", 'expect': 'preserve_literal'}, {'id': 'relative_article_note_anchor', 'html': "<p>слово<a href='./Article#cite_note-1'>[1]</a> следующее</p>", 'expect': 'strict_scope_keep', 'scope_limitation': True}, {'id': 'relative_article_sup_note', 'html': "<p>слово<sup class='reference'><a href='./Article#cite_note-1'>[1]</a></sup> следующее</p>", 'expect': 'strict_scope_keep', 'scope_limitation': True}])
    return rows

class FetcherDraftTests(unittest.TestCase):

    def setUp(self):
        for name in ('socket.create_connection', 'socket.getaddrinfo'):
            context = patch(name, side_effect=AssertionError('External network forbidden'))
            context.start()
            self.addCleanup(context.stop)

    def fetch(self, html, enabled=True, headers=None):
        fixture = wire_fixtures.Fixture()
        fixture.add('example.org', '/article', wire_fixtures.Response(html.encode(), headers=headers))
        fetcher = web_fetch.Fetcher(resolver=fixture.resolver, connector=fixture.connector, clock=lambda: fixture.now, semantic_notes=enabled)
        return (fetcher.fetch('https://example.org/article'), fixture)

    def test_primary_is_preserved_and_default_adds_independent_view(self):
        html = '<title>Исходный заголовок</title><p>' + BODY + NOTE + '</p>'
        old, a = self.fetch(html, False)
        new, b = self.fetch(html, True)
        self.assertIn('[1]', old['text'])
        self.assertEqual(new['text'], old['text'])
        self.assertNotIn('[1]', new['html_semantic_view']['text'])
        self.assertEqual(old['title'], new['title'])
        self.assertEqual(old['links'], new['links'])
        self.assertEqual(old['extraction'], 'html-parser-v4-edit-controls')
        self.assertEqual(new['extraction'], 'html-parser-v4-edit-controls')
        self.assertEqual(new['html_semantic_view']['extraction'], 'html-parser-v3-semantic-notes')
        self.assertEqual(len(a.requests), 2)
        self.assertEqual(len(b.requests), 2)
        self.assertTrue(web_fetch.Fetcher().semantic_notes)
        with self.assertRaises(TypeError):
            web_fetch.Fetcher(semantic_notes=1)

    def test_capped_1024_original_links_never_gain_late_link(self):
        ordinary_links = ''.join("<a href='/link-" + str(n) + "'>Ссылка" + str(n) + '</a>' for n in range(1023))
        html = '<div>' + BODY + NOTE + ordinary_links + "<a href='/late'>Поздняя ссылка</a></div>"
        old, _ = self.fetch(html, False)
        new, _ = self.fetch(html, True)
        self.assertEqual(len(old['links']), 1024)
        self.assertIn('html_semantic_view', new)
        self.assertEqual(old['links'], new['links'])
        self.assertFalse(any((l['url'].endswith('/late') for l in new['links'])))

    def test_clean_candidate_shorter_than_35_keeps_original(self):
        html = "<p>Короткая статья.<a href='#cite_note-1'>[" + '1' * 40 + ']</a></p>'
        old, _ = self.fetch(html, False)
        new, _ = self.fetch(html, True)
        self.assertGreaterEqual(len(old['text']), 35)
        self.assertEqual(new['text'], old['text'])

    def test_original_text_limit_is_checked_before_optional_inspection(self):
        html = '<p>' + 'x' * 190000 + "<a href='#cite_note-1'>[1]</a>" * 4000 + '</p>'
        with patch.object(html_notes, 'mask_semantic_notes', side_effect=AssertionError('No original-limit bypass')):
            with self.assertRaises(web_fetch.FetchError) as error:
                self.fetch(html)
        self.assertEqual(error.exception.code, 'text_too_large')

    def test_original_empty_and_depth_limits_precede_optional_inspection(self):
        for html, code in (('<script>hidden text</script>', 'empty_text'), ('<div>' * 129 + BODY + '</div>' * 129, 'html_too_complex')):
            with self.subTest(code=code), patch.object(html_notes, 'mask_semantic_notes', side_effect=AssertionError('Original rejected first')):
                with self.assertRaises(web_fetch.FetchError) as error:
                    self.fetch(html)
                self.assertEqual(error.exception.code, code)

    def test_ambiguous_ancestry_and_optional_inspection_error_retain_original(self):
        html = '<div><p>' + BODY + NOTE + '</p>'
        old, _ = self.fetch(html, False)
        new, _ = self.fetch(html, True)
        self.assertEqual(old['text'], new['text'])
        with patch.object(html_notes, 'mask_semantic_notes', side_effect=html_notes.HTMLNotesError('Authored bound')):
            same, _ = self.fetch('<p>' + BODY + NOTE + '</p>')
        expected, _ = self.fetch('<p>' + BODY + NOTE + '</p>', False)
        self.assertEqual(same['text'], expected['text'])

    def test_second_parser_chunks_remain_bounded_and_do_not_fetch_again(self):
        parent = web_fetch._TextExtractor
        instances = []

        class Spy(parent):

            def __init__(self):
                super().__init__()
                self.chunks = []
                instances.append(self)

            def feed(self, value):
                self.chunks.append(len(value))
                return super().feed(value)
        with patch.object(web_fetch, '_TextExtractor', Spy):
            result, fixture = self.fetch('<p>' + BODY * 500 + NOTE + '</p>')
        self.assertEqual(len(instances), 2)
        self.assertTrue(all((0 < n <= 16384 for parser in instances for n in parser.chunks)))
        self.assertEqual(len(fixture.requests), 2)
        self.assertIn('[1]', result['text'])
        self.assertNotIn('[1]', result['html_semantic_view']['text'])

    def test_same_total_deadline_is_rechecked_after_masking(self):
        fixture = wire_fixtures.Fixture()
        fixture.add('example.org', '/article', wire_fixtures.Response(('<p>' + BODY + NOTE + '</p>').encode()))
        normal = html_notes.mask_semantic_notes

        def expire(*args, **kwargs):
            result = normal(*args, **kwargs)
            fixture.now = 21
            return result
        fetcher = web_fetch.Fetcher(resolver=fixture.resolver, connector=fixture.connector, clock=lambda: fixture.now, semantic_notes=True)
        with patch.object(html_notes, 'mask_semantic_notes', side_effect=expire):
            with self.assertRaises(web_fetch.FetchError) as error:
                fetcher.fetch('https://example.org/article')
        self.assertEqual(error.exception.code, 'fetch_timeout')
        self.assertEqual(len(fixture.requests), 2)

    def test_non_html_does_not_invoke_note_inspection(self):
        with patch.object(html_notes, 'mask_semantic_notes', side_effect=AssertionError('Not HTML')):
            result, _ = self.fetch(BODY, headers={'content-type': 'text/plain; charset=utf-8'})
        self.assertEqual(result['text'], BODY)
        self.assertEqual(result['extraction'], 'plain-v1')

    def test_all_sixteen_frozen_literal_math_quote_controls_preserved(self):
        for fixture in literal_controls():
            with self.subTest(case=fixture['id']):
                result = probe.compare(fixture['html'])
                self.assertEqual(result['before'], result['after'])


if __name__ == "__main__":
    unittest.main()
