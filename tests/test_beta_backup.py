from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.check_beta_backup import unfinished_jobs, verify_backup


class BetaBackupTests(unittest.TestCase):
    def test_reserved_operation_without_terminal_outcome_is_unfinished(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE records(id TEXT PRIMARY KEY,kind TEXT,data TEXT)')
            db.execute('INSERT INTO records VALUES(?,?,?)', ('model-request:one', 'model-request', '{}'))
            self.assertEqual(unfinished_jobs(db), 1)
            db.execute('INSERT INTO records VALUES(?,?,?)', ('one', 'model-job', '{"status":"complete"}'))
            self.assertEqual(unfinished_jobs(db), 0)
            db.execute('INSERT INTO records VALUES(?,?,?)', ('orphan', 'model-job', '{"status":"running"}'))
            self.assertEqual(unfinished_jobs(db), 1)
            db.execute('UPDATE records SET data=? WHERE id=?', ('{"status":"failed"}', 'orphan'))
            self.assertEqual(unfinished_jobs(db), 0)

    def test_manifest_integrity_and_pending_counts_do_not_mutate_the_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'example.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE records(id TEXT PRIMARY KEY,kind TEXT,data TEXT)')
                db.execute('INSERT INTO records VALUES(?,?,?)', ('model-request:one', 'model-request', '{}'))
                db.commit()
            body = path.read_bytes()
            (root / 'manifest.json').write_text(json.dumps({'files': [
                {'path': path.name, 'sha256': sha256(body).hexdigest(), 'bytes': len(body)}]}))
            result = verify_backup(root)
            self.assertEqual(result['unfinished_or_unresolved_jobs'], 1)
            self.assertEqual(result['SQLite_verified'], 1)
            self.assertEqual(path.read_bytes(), body)
            path.write_bytes(body + b'changed')
            with self.assertRaisesRegex(ValueError, 'manifest'):
                verify_backup(root)

    def test_owner_plaintext_and_manifest_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'runtime/owner-auth.json'
            path.parent.mkdir()
            body = b'{"username":"fixture","password":"fixture-only"}'
            path.write_bytes(body)
            entry = {'path': 'runtime/owner-auth.json', 'sha256': sha256(body).hexdigest(), 'bytes': len(body)}
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'files': [entry]}))
            with self.assertRaisesRegex(ValueError, 'hashes only'):
                verify_backup(root)
            entry['path'] = '../outside'
            manifest.write_text(json.dumps({'files': [entry]}))
            with self.assertRaisesRegex(ValueError, 'path/hash'):
                verify_backup(root)


if __name__ == '__main__':
    unittest.main()
