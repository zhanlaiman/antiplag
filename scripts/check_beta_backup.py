"""Read-only backup validation; prints counts, never texts, identifiers or keys."""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3


# A running Jobs operation has a durable model-request before its terminal
# model-job is written. Counting only nonterminal model-job rows misses it.
UNFINISHED_JOBS_SQL = """
SELECT count(*) FROM (
 SELECT substr(id, length('model-request:') + 1) AS job_id
 FROM records WHERE kind='model-request'
 UNION
 SELECT id AS job_id FROM records WHERE kind='model-job'
) AS requests
LEFT JOIN records AS outcomes
 ON outcomes.kind='model-job' AND outcomes.id=requests.job_id
WHERE outcomes.id IS NULL
 OR coalesce(json_extract(outcomes.data, '$.status'), '')
    NOT IN ('complete', 'failed', 'cancelled')
"""


def unfinished_jobs(connection):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone():
        return 0
    return connection.execute(UNFINISHED_JOBS_SQL).fetchone()[0]


def verify_backup(directory):
    root = Path(directory).resolve()
    manifest = json.loads((root / 'manifest.json').read_text())
    entries = manifest.get('files')
    if not isinstance(entries, list) or not entries:
        raise ValueError('Missing nonempty backup manifest')
    seen, databases, pending = set(), 0, 0
    for entry in entries:
        name = entry.get('path')
        relative = Path(name) if isinstance(name, str) else Path('/')
        if (relative.is_absolute() or '..' in relative.parts or '\\' in str(relative)
                or name in seen or not re.fullmatch('[a-f0-9]{64}', entry.get('sha256', ''))):
            raise ValueError('Invalid or duplicate manifest path/hash')
        seen.add(name)
        path = root / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('Backup entry must be an ordinary file inside its root')
        body = path.read_bytes()
        if sha256(body).hexdigest() != entry['sha256'] or len(body) != entry.get('bytes'):
            raise ValueError('Backup bytes differ from the manifest')
        if name == 'runtime/owner-auth.json':
            owner = json.loads(body)
            if not isinstance(owner, dict) or set(owner) != {'username', 'salt', 'digest'}:
                raise ValueError('Owner backup must contain username and hashes only')
        if path.suffix == '.sqlite3':
            databases += 1
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
                if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('SQLite integrity check failed')
                pending += unfinished_jobs(connection)
    actual_databases = {str(path.relative_to(root)) for path in root.rglob('*.sqlite3')}
    if actual_databases != {name for name in seen if name.endswith('.sqlite3')}:
        raise ValueError('Unlisted or missing SQLite database')
    return {'files_verified': len(entries), 'SQLite_verified': databases,
            'unfinished_or_unresolved_jobs': pending, 'read_only': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    result = verify_backup(args.directory)
    print(json.dumps(result))
    if result['unfinished_or_unresolved_jobs']:
        raise SystemExit(3)


if __name__ == '__main__':
    main()
