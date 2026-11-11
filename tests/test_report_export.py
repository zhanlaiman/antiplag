from html.parser import HTMLParser
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import server
from plagiarism import report_export
from plagiarism.matching import analyze
from plagiarism.storage import Store


TEXT = 'Редактор сравнил четыре зелёных листа и записал форму каждой жилки.'


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, name, attributes):
        self.elements.append((name, dict(attributes)))


class ReportExportTests(unittest.TestCase):
    def report(self, **source_fields):
        source = {'id': 's1', 'title': 'Сохранённый источник', 'text': TEXT,
                  'url': 'https://example.org/article', **source_fields}
        return {'id': 'report1', 'created_at': '2026-11-11T13:00:00Z', 'text': TEXT,
                'sources': [source], **analyze(TEXT, [source])}

    def web_report(self):
        report = self.report(provenance={'origin': 'web_fetch', 'fetched_at': '2026-11-11T12:00:00Z',
                                         'text_sha256': 'a' * 64, 'license_status': 'not_verified', 'training_consent': False})
        report['scope'] = 'web_search'
        report['stats'].update(uniqueness_percent=70, matched_words=3, total_words=10, matched_word_percent=30)
        report['web'] = {'status': 'complete', 'queries_completed': 2, 'queries_planned': 2, 'sources_checked': 1,
                         'errors': [], 'limitations': ['Закрытые страницы не проверены.']}
        return report

    def test_untrusted_text_links_and_comments_cannot_add_active_content(self):
        attack = '</blockquote><script>alert(1)</script><img src="https://tracker.invalid/pixel">'
        report = self.report(title=attack, url='javascript:alert(1)')
        report['text'] = attack
        report['matches'][0].update(source_title=attack, source_url='javascript:alert(1)', excerpt=attack)
        content = report_export.render(report, {'match-1': {'decision': 'confirmed', 'comment': attack}})
        tags = Tags()
        tags.feed(content)
        self.assertFalse([name for name, _ in tags.elements if name in {'script', 'img', 'iframe', 'form', 'base', 'link'}])
        self.assertFalse([attrs for name, attrs in tags.elements if name == 'a' and not attrs.get('href', '').startswith('https://')])
        self.assertIn('&lt;script&gt;', content)
        self.assertIn("default-src 'none'", content)
        self.assertIn('Совпадение подтверждено редактором', content)

    def test_quote_is_still_a_match_and_scope_does_not_claim_entire_web(self):
        report = self.report(provenance={'attribution': 'Авторы тестовой статьи', 'license_name': 'CC BY-SA 4.0',
                                         'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/'})
        report['matches'][0]['quoted'] = True
        report['issues'] = [{'message': 'В тексте есть невидимые символы.'}]
        content = report_export.render(report)
        self.assertIn(str(report['stats']['similarity_percent']) + '%', content)
        self.assertIn('Корректная цитата может совпадать', content)
        self.assertIn('а не весь интернет', content)
        self.assertIn('невидимые символы', content)
        self.assertIn('Авторы тестовой статьи', content)
        self.assertIn('CC BY-SA 4.0', content)

    def test_web_export_reports_word_uniqueness_scope_counts_and_saved_page_metadata(self):
        report = self.web_report()
        with patch.object(server.llm, '_request', side_effect=AssertionError('Export must be offline')):
            content = report_export.render(report)
        self.assertIn('<h1>Проверка уникальности в интернете</h1>', content)
        self.assertNotIn('Проверка по библиотеке источников', content)
        self.assertNotIn('Библиотека этого отчёта', content)
        self.assertIn('Уникальность среди проверенных страниц: 70%', content)
        self.assertIn('доля слов в дословных совпадениях после нормализации: 3 из 10 (30%)', content)
        self.assertIn('Поисковых запросов выполнено: 2 из 2. Проверено страниц: 1. Ошибок: 0.', content)
        self.assertIn('Qwen не рассчитывает уникальность', content)
        self.assertIn('Дополнительная сверка пар с Qwen', content)
        self.assertIn('а не ко всему интернету', content)
        self.assertIn('Закрытые страницы не проверены.', content)
        self.assertIn('Сохранённые снимки проверенных страниц', content)
        self.assertIn('https://example.org/article', content)
        self.assertIn('Снимок получен: 11.11.2026 12:00 UTC', content)
        self.assertIn('a' * 64, content)
        self.assertIn('Статус лицензии в снимке: не проверена', content)

    def test_partial_web_export_never_claims_final_uniqueness_even_with_stale_numeric_score(self):
        report = self.web_report()
        report['web'].update(status='partial', queries_completed=1, errors=[{
            'stage': 'fetch', 'code': 'robots_disallowed', 'message': 'Страница запрещена.', 'url': 'https://example.org/blocked'}])
        for score in (None, 100):
            with self.subTest(score=score):
                report['stats']['uniqueness_percent'] = score
                content = report_export.render(report)
                self.assertIn('Уникальность среди проверенных страниц: Не установлена', content)
                self.assertIn('Частичная проверка', content)
                self.assertIn('Поисковых запросов выполнено: 1 из 2. Проверено страниц: 1. Ошибок: 1.', content)
                self.assertIn('robots_disallowed', content)
                self.assertIn('Страница запрещена.', content)
                self.assertNotIn('Уникальность среди проверенных страниц: 100%', content)

    def test_partial_web_export_preserves_zero_upper_bound_as_a_bound(self):
        report = self.web_report()
        report['web'].update(status='partial', errors=[{'stage': 'fetch', 'message': 'PDF недоступен.'}])
        report['stats'].update(uniqueness_percent=None, uniqueness_upper_bound_percent=0.0)
        content = report_export.render(report)
        self.assertIn('<strong>Уникальность не выше 0%</strong>', content)
        self.assertIn('Проверка неполная', content)
        self.assertIn('Частичная проверка', content)
        self.assertIn('итоговая уникальность не установлена', content)
        self.assertNotIn('Уникальность среди проверенных страниц: 0%', content)
        del report['stats']['uniqueness_upper_bound_percent']
        content = report_export.render(report)
        self.assertIn('Уникальность среди проверенных страниц: Не установлена', content)
        self.assertNotIn('Уникальность не выше', content)

    def test_old_complete_web_report_without_checked_source_snapshots_has_no_final_score(self):
        for checked, snapshots in ((0, True), (1, False), (0, False)):
            with self.subTest(checked=checked, snapshots=snapshots):
                report = self.web_report()
                report['web']['sources_checked'] = checked
                report['stats']['uniqueness_percent'] = 100
                if not snapshots:
                    report['sources'] = []
                content = report_export.render(report)
                self.assertIn('Уникальность среди проверенных страниц: Не установлена', content)
                self.assertNotIn('Уникальность среди проверенных страниц: 100%', content)

    def test_partial_nonzero_upper_bound_is_not_rendered_as_zero(self):
        report = self.web_report()
        report['web']['status'] = 'partial'
        report['stats'].update(uniqueness_percent=None, uniqueness_upper_bound_percent=0.1)
        self.assertIn('<strong>Уникальность не выше 0.1%</strong>', report_export.render(report))

    def test_web_limitations_errors_counts_and_snapshot_metadata_are_escaped(self):
        report = self.web_report()
        attack = '</p><script>alert(1)</script><img src="https://tracker.invalid/pixel">'
        report['web'].update(queries_completed=attack, status='partial', limitations=[attack],
                             errors=[{'stage': attack, 'code': attack, 'message': attack, 'url': 'javascript:alert(1)'}])
        report['sources'][0].update(title=attack, url='javascript:alert(1)')
        report['sources'][0]['provenance'].update(fetched_at=attack, text_sha256=attack, license_status=attack)
        content = report_export.render(report)
        tags = Tags()
        tags.feed(content)
        self.assertFalse([name for name, _ in tags.elements if name in {'script', 'img', 'iframe', 'form', 'base', 'link'}])
        self.assertFalse([attrs for name, attrs in tags.elements if name == 'a' and not attrs.get('href', '').startswith('https://')])
        self.assertIn('&lt;script&gt;', content)
        self.assertIn("default-src 'none'", content)

    def test_invalid_model_evidence_is_not_exported_as_a_quote(self):
        candidate = {'id': 'c1', 'source_title': 'Материал', 'query_excerpt': TEXT, 'source_excerpt': TEXT, 'rank': 1}
        content = report_export.render(self.report(), retrieval={'candidates': [candidate]},
                                       outcomes=[{'candidate_id': 'c1', 'judgment': {'relation': 'paraphrase',
                                                  'evidence_text': 'ВЫДУМАННАЯ ЦИТАТА', 'evidence_source': TEXT}}])
        self.assertNotIn('ВЫДУМАННАЯ ЦИТАТА', content)
        self.assertIn('Выдержки не подтверждены', content)

    def test_export_uses_saved_sources_and_latest_failed_recheck_without_compute(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'db.sqlite3')
            source = store.add_source('Архивная публикация', TEXT, 'https://example.org/archive')
            report = store.save('report', {'text': TEXT, 'sources': [source], **analyze(TEXT, [source])})
            retrieved = store.save('retrieval', {'report_id': report['id'], 'mode': 'hybrid', 'candidates': [{
                'id': 'c1', 'source_id': source['id'], 'source_title': 'Архивная публикация', 'rank': 1,
                'query_excerpt': TEXT, 'source_excerpt': TEXT}]})
            store.save('candidate-judgment', {'retrieval_id': retrieved['id'], 'candidate_id': 'c1',
                       'judgment': {'relation': 'paraphrase', 'explanation': 'OLD POSITIVE EXPLANATION',
                                    'evidence_text': TEXT, 'evidence_source': TEXT}})
            store.save('candidate-error', {'retrieval_id': retrieved['id'], 'candidate_id': 'c1',
                                          'error': 'Повторная проверка прервана.'})
            store.review_match(report['id'], 'match-1', 'dismissed', 'Оформленная цитата.')
            store.delete_source(source['id'])
            handler = server.Handler.__new__(server.Handler)
            handler.server = SimpleNamespace(store=store)
            with patch.object(server.llm, '_request', side_effect=AssertionError('Must not call a model')):
                exported = handler.route('GET', '/api/report-exports/' + report['id'], {})
            self.assertEqual(exported['filename'], 'istok-report-' + report['id'] + '.html')
            self.assertIn('Архивная публикация', exported['html'])
            self.assertIn('Находка отклонена редактором', exported['html'])
            self.assertIn('Повторная проверка прервана', exported['html'])
            self.assertNotIn('OLD POSITIVE EXPLANATION', exported['html'])

    def test_missing_report_is_not_silently_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = server.Handler.__new__(server.Handler)
            handler.server = SimpleNamespace(store=Store(Path(directory) / 'db.sqlite3'))
            with self.assertRaises(KeyError):
                handler.route('GET', '/api/report-exports/missing', {})


if __name__ == '__main__':
    unittest.main()
