import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from scripts.prepare_beta import backup,fingerprint


class BetaBackupTests(unittest.TestCase):
    def test_online_snapshot_includes_wal_and_restores_records_without_secrets_in_manifest(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir();runtime=root/'.runtime';runtime.mkdir()
            db=sqlite3.connect(root/'data/istok.sqlite3')
            try:
                db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE example(value TEXT)');db.execute("INSERT INTO example VALUES ('saved report')");db.commit()
                (runtime/'search-api.json').write_text('{"key":"private-test-key"}')
                out=root/'snapshot';manifest=backup(root,out)
                with sqlite3.connect(out/'data/istok.sqlite3') as restored:
                    self.assertEqual(restored.execute('SELECT value FROM example').fetchall(),[('saved report',)])
                self.assertEqual((out/'.runtime/search-api.json').stat().st_mode&0o777,0o600)
                self.assertNotIn('private-test-key',json.dumps(manifest))
                self.assertTrue(all(fingerprint(out/f['path'])==f['sha256'] for f in manifest['files']))
                with self.assertRaises(FileExistsError):backup(root,out)
            finally:db.close()

    def test_corrupt_database_never_produces_complete_manifest(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir();(root/'data/istok.sqlite3').write_bytes(b'not sqlite')
            out=root/'snapshot'
            with self.assertRaises(sqlite3.DatabaseError):backup(root,out)
            self.assertTrue((out/'INCOMPLETE').exists());self.assertFalse((out/'manifest.json').exists())
