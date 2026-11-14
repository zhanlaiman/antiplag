from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from plagiarism.storage import Store


class DataManagementTests(unittest.TestCase):
    def test_previews_include_score_evidence_counts_without_source_text_or_error_details(self):
        with TemporaryDirectory() as root:
            store = Store(Path(root) / 'data.sqlite3')
            source = store.add_source('Источник', 'PRIVATE_SOURCE_SENTINEL')
            report = store.save('report', {'text': 'Мой текст', 'scope': 'web_search',
                'sources': [source], 'stats': {'uniqueness_percent': None, 'uniqueness_upper_bound_percent': 58.3},
                'web': {'status': 'partial', 'sources_checked': 1,
                        'errors': [{'message': 'PRIVATE_ERROR_SENTINEL', 'url': 'https://private.example'}]}})
            for preview in (store.data_summary()['reports'][0], store.report_summaries()[0]):
                self.assertEqual(preview['stats']['uniqueness_upper_bound_percent'], 58.3)
                self.assertEqual(preview['web'], {'status': 'partial', 'sources_checked': 1, 'errors_count': 1})
                self.assertEqual(preview['source_snapshots_count'], 1)
                self.assertNotIn('PRIVATE_SOURCE_SENTINEL', json.dumps(preview))
                self.assertNotIn('PRIVATE_ERROR_SENTINEL', json.dumps(preview))
                self.assertNotIn('private.example', json.dumps(preview))
            store.save('correction', {'text': 'Правки'})
            store.trash_item('report', report['id'])
            store.trash_item('source', source['id'])
            summary = store.data_summary()
            self.assertEqual(summary['active_totals'], {'reports': 0, 'sources': 0, 'corrections': 1})
            self.assertEqual(summary['totals']['reports'], 1)
            self.assertEqual(summary['totals']['trashed'], 2)

    def test_trash_restore_preserves_snapshots_and_operational_receipts(self):
        with TemporaryDirectory() as root:
            path = Path(root) / 'data.sqlite3'
            store = Store(path)
            source = store.add_source('Заголовок', 'Сохранённый источник', source_id='s')
            original = store.save('report', {'text': 'Мой текст', 'sources': [source], 'stats': {}})
            receipt = store.save('model-request', {'id': 'model-request:request', 'fingerprint': 'retained'})
            self.assertTrue(store.trash_item('report', original['id']))
            self.assertFalse(store.trash_item('report', original['id']))
            self.assertIsNone(store.get('report', original['id']))
            self.assertEqual(store.records('report'), [])
            self.assertEqual(store.report_summaries(), [])
            self.assertEqual(store.counts()['reports'], 0)
            self.assertEqual(store.get('model-request', receipt['id']), receipt)
            self.assertTrue(store.trash_item('source', 's'))
            self.assertEqual(store.sources(), [])
            store = Store(path)
            self.assertEqual(store.data_summary()['totals'], {'reports': 1, 'sources': 1, 'trashed': 2})
            self.assertTrue(store.restore_item('report', original['id']))
            self.assertEqual(store.get('report', original['id']), original)
            self.assertTrue(store.restore_item('source', 's'))
            self.assertEqual(store.sources(), [source])
            self.assertFalse(store.trash_item('report', 'foreign'))
            self.assertFalse(store.restore_item('report', 'foreign'))

    def test_archive_is_complete_including_trash_and_excludes_job_receipts(self):
        with TemporaryDirectory() as root:
            store = Store(Path(root) / 'data.sqlite3')
            source = store.add_source('Имя', 'Текст источника')
            report = store.save('report', {'text': 'Текст отчёта', 'sources': [source], 'stats': {}})
            store.save('model-request', {'fingerprint': 'private-operational-receipt'})
            store.trash_item('report', report['id'])
            store.review_match(report['id'], 'match', 'confirmed', 'Моя пометка')
            export = store.export_data()
            with export.stream as stream, ZipFile(stream) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertTrue(manifest['includes_trash'])
                reports = [json.loads(line) for line in archive.read('reports.jsonl').splitlines()]
                snapshots = [json.loads(line) for line in archive.read('source_snapshots.jsonl').splitlines()]
                self.assertEqual(reports[0]['text'], report['text'])
                self.assertEqual(snapshots[0]['hash'], reports[0]['sources_refs'][0])
                self.assertEqual(snapshots[0]['data'], source)
                self.assertEqual(json.loads(archive.read('trash.jsonl'))['id'], report['id'])
                self.assertEqual(json.loads(archive.read('match_reviews.jsonl'))['comment'], 'Моя пометка')
                self.assertNotIn('private-operational-receipt', ''.join(archive.read(n).decode() for n in archive.namelist()))
            self.assertTrue(export.stream.closed)

    def test_other_store_never_sees_or_restores_records(self):
        with TemporaryDirectory() as root:
            alice = Store(Path(root) / 'a.sqlite3')
            bob = Store(Path(root) / 'b.sqlite3')
            report = alice.save('report', {'text': 'Личное', 'stats': {}})
            alice.trash_item('report', report['id'])
            self.assertFalse(bob.restore_item('report', report['id']))
            self.assertEqual(bob.data_summary()['reports'], [])


if __name__ == '__main__':
    unittest.main()
