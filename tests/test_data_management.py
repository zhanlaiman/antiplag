from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from plagiarism.storage import Store


class DataManagementTests(unittest.TestCase):
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
